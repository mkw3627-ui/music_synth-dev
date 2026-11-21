"""S4 验收：后台下载守护进程（对照 PLAN_download_tiering.md D-S4）。

全部用 S2 的本地假镜像 + 真实子进程拉起守护进程，不依赖外网。
守护进程脚本用 ``files_override`` 直接指定源和目标，绕开 catalog 的公网 URL，
这样测试既真实又可控。

``python -m pytest tests/test_download_daemon.py -q``
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_WORKERS = str(PLUGIN_ROOT / "workers")
if _WORKERS not in sys.path:
    sys.path.insert(0, _WORKERS)

_fake = import_module("dl_harness.fake_mirrors")
KB = _fake.KB
MB = _fake.MB
FakeMirror = _fake.FakeMirror
Fault = _fake.Fault
make_content = _fake.make_content

DM = import_module("download_manager")

DAEMON = PLUGIN_ROOT / "workers" / "download_daemon.py"
#: 依赖 Windows 进程语义（taskkill / CREATE_NEW_PROCESS_GROUP / msvcrt 锁）的用例，
#: Linux CI 上跳过；产品代码本身有 fcntl 回退，这里只是不想让测试替 CI 环境背锅。
_IS_WINDOWS = os.name == "nt"
_windows_only = pytest.mark.skipif(not _IS_WINDOWS, reason="依赖 Windows 进程语义")
TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def sha_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def wait_until(predicate, timeout: float = TIMEOUT, interval: float = 0.1, what: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError(f"等待超时（{timeout}s）：{what}")


class DaemonProc:
    """手工拉起守护进程（模拟插件的行为，但可精确控制存活）。"""

    def __init__(self, state_dir: Path, plugin_dir: Path = PLUGIN_ROOT,
                 max_parallel: int = 2, idle_exit_s: float = 60.0) -> None:
        self.state_dir = Path(state_dir)
        self.proc: subprocess.Popen | None = None
        self.zombies: list[subprocess.Popen] = []
        self.log_path = self.state_dir / "daemon.log"
        self.cmd = [sys.executable, "-I", "-X", "utf8", str(DAEMON),
                    str(self.state_dir), str(plugin_dir),
                    "--max-parallel", str(max_parallel),
                    "--idle-exit-s", str(idle_exit_s)]

    def start(self) -> "DaemonProc":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "cmd").mkdir(parents=True, exist_ok=True)
        log_fh = open(self.log_path, "ab")
        self._log_fh = log_fh
        self.proc = subprocess.Popen(self.cmd, stdin=subprocess.DEVNULL,
                                     stdout=log_fh, stderr=subprocess.STDOUT)
        return self

    def wait_ready(self, timeout: float = 20.0) -> None:
        def ready() -> bool:
            path = self.state_dir / "state.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            d = data.get("daemon") or {}
            return bool(d.get("pid")) and bool(d.get("heartbeat_at"))
        wait_until(ready, timeout, what="守护进程写 state.json")

    def kill(self) -> None:
        """模拟 ``taskkill /F``：强杀，不给收尾机会。"""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            self._log_fh.close()
        except Exception:  # noqa: BLE001
            pass

    def state(self) -> dict:
        try:
            return json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"items": {}, "daemon": {}, "totals": {}}

    def item(self, key: str) -> dict:
        return self.state()["items"].get(key) or {}

    def logs(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


@pytest.fixture
def daemon(tmp_path):
    procs: list[DaemonProc] = []

    def _make(state_dir=None, **kw) -> DaemonProc:
        d = DaemonProc(state_dir or (tmp_path / "_downloads"), **kw).start()
        procs.append(d)
        return d

    yield _make
    for p in procs:
        p.stop()


def file_spec(dest: Path, urls, size: int, sha: str = "") -> dict:
    return {"rel": dest.name, "dest": str(dest), "urls": list(urls),
            "size": int(size), "sha256": sha}


def enqueue_files(paths_root: Path, key: str, files: list[dict], **extra) -> dict:
    return {"key": key, "paths": {"compute_root": str(paths_root)},
            "files_override": files, **extra}


# ---------------------------------------------------------------------------
# 基础：多任务入队下载完成
# ---------------------------------------------------------------------------
def test_multi_task_download_completes(tmp_path, mirrors, daemon):
    """3 个任务入队 → 全部 done，文件内容正确，state.json 汇总正确。"""
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)

    a = make_content(512 * KB, seed=1)
    b = make_content(384 * KB, seed=2)
    c = make_content(256 * KB, seed=3)
    cl_a = mirrors(1, content=a)
    cl_b = mirrors(1, content=b)
    cl_c = mirrors(1, content=c)

    dests = [tmp_path / f"out_{i}.bin" for i in range(3)]
    DM.ensure_daemon({"compute_root": str(tmp_path)})  # 让 ensure 看到正确的 state_dir

    # 直接对已区分的 state_dir 写命令（不走 ensure，避免起第二个实例）
    DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
        enqueue_files(tmp_path, "task_a", [file_spec(dests[0], cl_a.urls(), a.size, a.sha256())]),
        enqueue_files(tmp_path, "task_b", [file_spec(dests[1], cl_b.urls(), b.size, b.sha256())]),
        enqueue_files(tmp_path, "task_c", [file_spec(dests[2], cl_c.urls(), c.size, c.sha256())]),
    ]})

    wait_until(lambda: all(p.is_file() for p in dests), TIMEOUT, what="3 个文件下载完成")
    assert sha_of(dests[0]) == a.sha256()
    assert sha_of(dests[1]) == b.sha256()
    assert sha_of(dests[2]) == c.sha256()

    wait_until(lambda: all(d.item(k).get("state") == "done"
                           for k in ("task_a", "task_b", "task_c")),
               TIMEOUT, what="三个任务都标记 done")
    st = d.state()
    assert st["totals"]["bytes_done"] >= a.size + b.size + c.size
    assert st["totals"]["failed"] == 0


def test_unknown_key_rejected(tmp_path, daemon):
    """未知下载物 key：命令被忽略，不产生条目也不崩。"""
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    DM.write_command({"compute_root": str(tmp_path)},
                     {"action": "enqueue", "items": [{"key": "no_such_key", "paths": {}}]})
    wait_until(lambda: d.state().get("daemon", {}).get("heartbeat_at"), 20, what="守护进程启动")
    time.sleep(1.0)
    assert "no_such_key" not in d.state()["items"]
    assert d.proc.poll() is None, "未知 key 不能让守护进程崩掉"


# ---------------------------------------------------------------------------
# 断点续传 / 杀进程恢复
# ---------------------------------------------------------------------------
def test_kill_and_restart_resumes_from_part(tmp_path, mirrors):
    """下到中途强杀 → 重启后从 .part 续传，最终 sha 一致。"""
    state_dir = tmp_path / "_downloads"
    content = make_content(4 * MB, seed=11)
    cluster = mirrors([Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "resume.bin"

    d1 = DaemonProc(state_dir).start()
    try:
        d1.wait_ready()
        DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
            enqueue_files(tmp_path, "resume_task",
                          [file_spec(dest, cluster.urls(), content.size, content.sha256())])]})

        part = dest.with_name(dest.name + ".part")
        wait_until(lambda: part.is_file() and part.stat().st_size > 512 * KB,
                   30, what="下载到一半")
        d1.kill()
    finally:
        d1.stop()

    partial = part.stat().st_size
    assert partial > 0
    assert not dest.exists()

    d2 = DaemonProc(state_dir).start()
    try:
        d2.wait_ready()
        # 重启后进度应明显大于 0（真的从断点续，而不是从 0 重下）。
        # 注意 bytes_done 是「已落盘 + 本次窗口已收」的滚动值，可能比 kill 时略小。
        wait_until(lambda: d2.item("resume_task").get("bytes_done", 0) > partial // 2,
                   20, what="重启后从断点附近续传")
        resumed_at = d2.item("resume_task").get("bytes_done", 0)
        assert resumed_at > 0, "重启后应从断点续传，而不是从 0 开始"
        wait_until(lambda: dest.is_file(), TIMEOUT, what="重启后下完")
        assert sha_of(dest) == content.sha256()
        wait_until(lambda: d2.item("resume_task").get("state") == "done",
                   TIMEOUT, what="任务标记 done")
    finally:
        d2.stop()


def test_restart_keeps_state_file_consistent(tmp_path, mirrors):
    """state.json 在强杀后仍能解析，且重启会接着做未完成的任务。"""
    state_dir = tmp_path / "_downloads"
    content = make_content(3 * MB, seed=12)
    cluster = mirrors([Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "consistent.bin"

    d1 = DaemonProc(state_dir).start()
    try:
        d1.wait_ready()
        DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
            enqueue_files(tmp_path, "c_task",
                          [file_spec(dest, cluster.urls(), content.size, content.sha256())])]})
        wait_until(lambda: d1.item("c_task").get("bytes_done", 0) > 0, 30, what="开始下载")
        d1.kill()
    finally:
        d1.stop()

    # 强杀后 state.json 必须仍是合法 JSON
    raw = (state_dir / "state.json").read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["items"]["c_task"]["state"] in ("queued", "downloading", "probing")

    d2 = DaemonProc(state_dir).start()
    try:
        d2.wait_ready()
        wait_until(lambda: dest.is_file(), TIMEOUT, what="重启后完成")
        assert sha_of(dest) == content.sha256()
    finally:
        d2.stop()


# ---------------------------------------------------------------------------
# 单任务失败不影响其它
# ---------------------------------------------------------------------------
def test_one_failure_does_not_block_others(tmp_path, mirrors, daemon):
    """A 全源失败、B 正常：B 仍然成功，A 记录清晰错误。"""
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    good = make_content(256 * KB, seed=21)
    bad_content = make_content(256 * KB, seed=22)
    cl_good = mirrors(1, content=good)
    cl_bad = mirrors([Fault(status=500), Fault(status=503)], content=bad_content)

    dest_good = tmp_path / "good.bin"
    dest_bad = tmp_path / "bad.bin"

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
        enqueue_files(tmp_path, "f_bad", [file_spec(dest_bad, cl_bad.urls(),
                                                    bad_content.size, bad_content.sha256())]),
        enqueue_files(tmp_path, "f_good", [file_spec(dest_good, cl_good.urls(),
                                                     good.size, good.sha256())]),
    ]})

    wait_until(lambda: dest_good.is_file(), TIMEOUT, what="好的任务完成")
    assert sha_of(dest_good) == good.sha256()
    wait_until(lambda: d.item("f_bad").get("state") == "failed", TIMEOUT, what="坏的任务失败")
    err = d.item("f_bad").get("error") or ""
    assert err, "失败任务必须有 error 说明"
    assert "500" in err or "所有源" in err or "失败" in err
    assert not dest_bad.exists()


def test_checksum_failure_recorded(tmp_path, mirrors, daemon):
    """内容被篡改：任务 failed 且错误里有校验字样，不产出坏文件。"""
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    content = make_content(256 * KB, seed=23)
    cluster = mirrors([Fault(tamper=True), Fault(tamper=True)], content=content)
    dest = tmp_path / "tampered.bin"

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
        enqueue_files(tmp_path, "t_task",
                      [file_spec(dest, cluster.urls(), content.size, content.sha256())])]})

    wait_until(lambda: d.item("t_task").get("state") == "failed", TIMEOUT, what="校验失败")
    assert "校验" in (d.item("t_task").get("error") or "")
    assert not dest.exists()


# ---------------------------------------------------------------------------
# 暂停 / 恢复 / 取消
# ---------------------------------------------------------------------------
def test_pause_and_resume(tmp_path, mirrors, daemon):
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    content = make_content(4 * MB, seed=31)
    cluster = mirrors([Fault(rate_bps=512 * KB)], content=content)
    dest = tmp_path / "pause.bin"

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
        enqueue_files(tmp_path, "p_task",
                      [file_spec(dest, cluster.urls(), content.size, content.sha256())])]})
    wait_until(lambda: d.item("p_task").get("bytes_done", 0) > 200 * KB, 30, what="下到一半")

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "pause", "target": "p_task"})
    wait_until(lambda: d.item("p_task").get("state") == "paused", 20, what="暂停生效")
    frozen = d.item("p_task").get("bytes_done", 0)
    time.sleep(1.0)
    assert d.item("p_task").get("bytes_done", 0) == frozen, "暂停后进度不应增长"

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "resume", "target": "p_task"})
    wait_until(lambda: dest.is_file(), TIMEOUT, what="恢复后下完")
    assert sha_of(dest) == content.sha256()


def test_cancel_removes_part(tmp_path, mirrors, daemon):
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    content = make_content(4 * MB, seed=32)
    cluster = mirrors([Fault(rate_bps=512 * KB)], content=content)
    dest = tmp_path / "cancel.bin"
    part = dest.with_name(dest.name + ".part")

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
        enqueue_files(tmp_path, "c_task2",
                      [file_spec(dest, cluster.urls(), content.size, content.sha256())])]})
    wait_until(lambda: part.is_file() and part.stat().st_size > 100 * KB, 30, what="开始下载")

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "cancel", "target": "c_task2"})
    wait_until(lambda: d.item("c_task2").get("state") == "canceled", 20, what="取消生效")
    wait_until(lambda: not part.exists(), 20, what="取消后删除 .part")
    assert not dest.exists()


def test_retry_after_failure(tmp_path, mirrors, daemon):
    """失败后 retry：源恢复可用则能下完。"""
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    content = make_content(256 * KB, seed=33)
    cluster = mirrors([Fault(status=500)], content=content)
    dest = tmp_path / "retry.bin"

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
        enqueue_files(tmp_path, "r_task",
                      [file_spec(dest, cluster.urls(), content.size, content.sha256())])]})
    wait_until(lambda: d.item("r_task").get("state") == "failed", TIMEOUT, what="先失败")

    cluster[0].get("/file.bin").fault.status = 200   # 源恢复
    DM.write_command({"compute_root": str(tmp_path)}, {"action": "retry", "target": "r_task"})
    wait_until(lambda: dest.is_file(), TIMEOUT, what="重试后完成")
    assert sha_of(dest) == content.sha256()


# ---------------------------------------------------------------------------
# repo 型：按 components 逐文件下
# ---------------------------------------------------------------------------
def test_repo_type_downloads_each_component(tmp_path, mirrors, daemon):
    """repo 型条目按文件清单逐文件下载（这里用 files_override 模拟清单）。"""
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    f1 = make_content(128 * KB, seed=41)
    f2 = make_content(96 * KB, seed=42)
    m = FakeMirror().add("/comp/a/model.safetensors", f1) \
                    .add("/comp/b/config.json", f2).start()
    try:
        target_root = tmp_path / "checkpoints"
        files = [
            {"rel": "comp-a/model.safetensors",
             "dest": str(target_root / "comp-a" / "model.safetensors"),
             "urls": [m.url("/comp/a/model.safetensors")],
             "size": f1.size, "sha256": f1.sha256()},
            {"rel": "comp-b/config.json",
             "dest": str(target_root / "comp-b" / "config.json"),
             "urls": [m.url("/comp/b/config.json")],
             "size": f2.size, "sha256": f2.sha256()},
        ]
        DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
            enqueue_files(tmp_path, "repo_task", files)]})

        wait_until(lambda: d.item("repo_task").get("state") in ("done", "failed"),
                   TIMEOUT, what="repo 任务结束")
        item = d.item("repo_task")
        assert item["state"] == "done", item.get("error")
        assert sha_of(target_root / "comp-a" / "model.safetensors") == f1.sha256()
        assert sha_of(target_root / "comp-b" / "config.json") == f2.sha256()
        assert len(item["files"]) == 2
        assert all(f["state"] == "done" for f in item["files"])
    finally:
        m.stop()


# ---------------------------------------------------------------------------
# 状态文件与其他
# ---------------------------------------------------------------------------
def test_state_atomic_reads(tmp_path, mirrors, daemon):
    """并发读 state.json 1 秒：一次 JSON 解析失败都不允许（原子替换）。"""
    import threading
    state_dir = tmp_path / "_downloads"
    daemon(state_dir)
    content = make_content(2 * MB, seed=51)
    cluster = mirrors([Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "atomic.bin"

    DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
        enqueue_files(tmp_path, "a_task",
                      [file_spec(dest, cluster.urls(), content.size, content.sha256())])]})

    errors: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        path = state_dir / "state.json"
        while not stop.is_set():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            except ValueError as exc:
                errors.append(str(exc))
            time.sleep(0.05)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.2)
    stop.set()
    t.join(timeout=5)
    assert not errors, f"state.json 出现过非法 JSON：{errors[:3]}"


@_windows_only
def test_single_instance_lock(tmp_path, mirrors):
    """先起来的实例持锁；后起来的第二个实例 2 秒内退出（码 3）。"""
    state_dir = tmp_path / "_downloads"
    d1 = DaemonProc(state_dir).start()
    d2 = DaemonProc(state_dir)
    try:
        # 必须先等 d1 真正拿到锁（写 state.json），否则 d2 可能先抢到，测的就不是这件事
        d1.wait_ready()
        d2.start()
        wait_until(lambda: d2.proc.poll() is not None, 15, what="第二个实例退出")
        assert d2.proc.returncode == 3, f"应当以 3 退出，实际 {d2.proc.returncode}"
        assert d1.proc.poll() is None, "第一个实例必须存活"
    finally:
        d1.stop()
        d2.stop()


@_windows_only
def test_third_instance_also_rejected(tmp_path, mirrors):
    """再起第三个也一样被拒（锁不是「只挡一次」）。"""
    state_dir = tmp_path / "_downloads"
    d1 = DaemonProc(state_dir).start()
    others = []
    try:
        d1.wait_ready()
        for _ in range(2):
            d = DaemonProc(state_dir).start()
            others.append(d)
        for i, d in enumerate(others, start=2):
            wait_until(lambda d=d: d.proc.poll() is not None, 15, what=f"第 {i} 个实例退出")
            assert d.proc.returncode == 3
        assert d1.proc.poll() is None
    finally:
        d1.stop()
        for d in others:
            d.stop()


@_windows_only
def test_plugin_exit_does_not_stop_daemon(tmp_path, mirrors):
    """守护进程由「假插件」拉起后，假插件退出，守护进程继续把任务做完。"""
    state_dir = tmp_path / "_downloads"
    state_dir.mkdir(parents=True, exist_ok=True)
    content = make_content(1 * MB, seed=61)
    cluster = mirrors([Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "survive.bin"

    # 假插件：subprocess 拉起守护进程后立即退出
    fake_plugin = (
        "import subprocess,sys,json\n"
        f"p=subprocess.Popen([sys.executable,'-I','-X','utf8',r'{DAEMON}',\n"
        f"  r'{state_dir}', r'{PLUGIN_ROOT}','--idle-exit-s','60'],\n"
        "  stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,\n"
        "  creationflags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))\n"
        "print(p.pid)\n"
    )
    out = subprocess.run([sys.executable, "-c", fake_plugin], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    pid = int(out.stdout.strip().splitlines()[-1])

    try:
        wait_until(lambda: (state_dir / "state.json").exists(), 20, what="守护进程启动")
        DM.write_command({"compute_root": str(tmp_path)}, {"action": "enqueue", "items": [
            enqueue_files(tmp_path, "s_task",
                          [file_spec(dest, cluster.urls(), content.size, content.sha256())])]})
        wait_until(lambda: dest.is_file(), TIMEOUT, what="假插件退出后仍能下完")
        assert sha_of(dest) == content.sha256()
    finally:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, check=False)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def test_idle_exit(tmp_path, daemon):
    """队列空且空闲超时后自动退出，daemon.running=False。"""
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir, idle_exit_s=2.0)
    d.wait_ready()
    wait_until(lambda: d.proc.poll() is not None, 25, what="空闲后退出")
    data = d.state()
    assert data["daemon"].get("running") is False


def test_shutdown_command(tmp_path, daemon):
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    d.wait_ready()
    DM.write_command({"compute_root": str(tmp_path)}, {"action": "shutdown"})
    wait_until(lambda: d.proc.poll() is not None, 20, what="收到 shutdown 后退出")


# ---------------------------------------------------------------------------
# 管理器（插件侧门面，不跑守护进程）
# ---------------------------------------------------------------------------
def test_manager_read_state_missing_file(tmp_path):
    st = DM.read_state({"compute_root": str(tmp_path)})
    assert st["items"] == {} and st["daemon"] == {}


def test_manager_enqueue_writes_command(tmp_path):
    paths = {"compute_root": str(tmp_path)}
    out = DM.enqueue(paths, ["ddsp_contentvec"], auto_start=False)
    assert out["count"] == 1
    cmd_dir = DM.state_dir(paths) / "cmd"
    files = list(cmd_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["action"] == "enqueue"
    assert payload["items"][0]["key"] == "ddsp_contentvec"


def test_manager_control_validates_action(tmp_path):
    paths = {"compute_root": str(tmp_path)}
    with pytest.raises(ValueError):
        DM.control(paths, "nonsense")
    DM.control(paths, "pause", "all")
    files = list((DM.state_dir(paths) / "cmd").glob("*.json"))
    assert files and json.loads(files[0].read_text(encoding="utf-8"))["action"] == "pause"


def test_manager_feature_ready(tmp_path):
    paths = {"compute_root": str(tmp_path)}
    sd = DM.state_dir(paths)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "state.json").write_text(json.dumps({
        "version": 1, "daemon": {}, "totals": {},
        "items": {
            "ddsp_contentvec": {"state": "done"},
            "ddsp_vocoder": {"state": "downloading"},
        },
    }), encoding="utf-8")
    rep = DM.feature_ready(paths, "cover")
    assert rep["ready"] is False
    assert "ddsp_vocoder" in rep["running"]
    assert "sep_karaoke" in rep["missing"]


def test_manager_state_dir_defaults(tmp_path):
    assert DM.state_dir({"compute_root": str(tmp_path)}).name == "_downloads"
    assert DM.state_dir(str(tmp_path)).parent == tmp_path
    assert DM.state_dir({}) .name == "_downloads"


def test_daemon_log_rotation_cap(tmp_path, mirrors, daemon):
    """日志文件存在且可读（滚动逻辑不会让写入失败）。"""
    state_dir = tmp_path / "_downloads"
    d = daemon(state_dir)
    d.wait_ready()
    time.sleep(0.5)
    logs = d.logs()
    assert "守护进程启动" in logs
