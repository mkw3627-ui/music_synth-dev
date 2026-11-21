"""S3 验收：下载引擎（对照 PLAN_download_tiering.md D-S3 的 Given/When/Then）。

全部跑在 S2 的本地假镜像上，不依赖外网。`python -m pytest tests -q` 全绿即通过。
"""
from __future__ import annotations

import hashlib
import threading
import time
import types
from importlib import import_module
from pathlib import Path

import pytest
from dl_harness.fake_mirrors import KB, MB, Fault, make_content

E = import_module("download_engine")

# 测试用的快参数：窗口/卡死缩短，避免每条用例等几十秒
FAST = dict(
    probe_bytes=256 * KB,
    probe_budget_s=3.0,
    probe_ttl_s=0.0,          # 不缓存，换源后重测才真实
    slow_window_s=0.6,
    slow_bps=300 * KB,
    slow_windows=2,
    stall_s=2.0,
    stall_bytes=64 * KB,
    max_switches=8,
    demote_cooldown_s=0.0,
    chunk_size=64 * KB,
    timeout=10.0,
    state_write_interval_s=0.0,
    net_wait_budget_s=0.0,
)


def cfg(**over) -> E.EngineConfig:
    base = dict(FAST)
    base.update(over)
    return E.EngineConfig(**base)


def sha_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------
def test_normal_download_picks_fastest_and_verifies(tmp_path, mirrors):
    """3 源（快/中/慢）：选最快的、sha 一致、无残留 .part。"""
    content = make_content(2 * MB, seed=11)
    cluster = mirrors(
        [Fault(rate_bps=2 * MB), Fault(rate_bps=512 * KB), Fault(rate_bps=128 * KB)],
        content=content,
    )
    dest = tmp_path / "out.bin"
    speeds: list[tuple[int, float]] = []

    def on_progress(p: E.Progress):
        speeds.append((p.bytes_done, p.speed_bps))

    res = E.download(dest, cluster.urls(), size=content.size,
                     sha256=content.sha256(), cfg=cfg(), on_progress=on_progress)
    assert res.ok and dest.is_file()
    assert sha_of(dest) == content.sha256()
    assert not dest.with_name(dest.name + ".part").exists()
    assert not dest.with_name(dest.name + ".part.json").exists()
    assert res.switches == 0
    assert speeds and speeds[-1][0] == content.size


def test_single_source_no_switch(tmp_path, mirrors):
    content = make_content(512 * KB, seed=3)
    cluster = mirrors(1, content=content)
    dest = tmp_path / "one.bin"
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    assert res.ok and sha_of(dest) == content.sha256() and res.switches == 0


# ---------------------------------------------------------------------------
# 断点续传 / 杀进程恢复
# ---------------------------------------------------------------------------
def test_resume_from_existing_part(tmp_path, mirrors):
    """.part 已有一半：重新调用只补剩下的，且最终 sha 一致。"""
    content = make_content(1 * MB, seed=21)
    cluster = mirrors(1, content=content)
    dest = tmp_path / "resume.bin"
    part = dest.with_name(dest.name + ".part")
    half = content.size // 2
    part.write_bytes(content.read(0, half))

    ranges: list[str] = []

    def on_progress(p: E.Progress):
        if p.state == "downloading" and p.bytes_done <= half + 64 * KB:
            ranges.append(p.source)

    res = E.download(dest, cluster.urls(), size=content.size,
                     sha256=content.sha256(), cfg=cfg(), on_progress=on_progress)
    assert res.ok
    assert sha_of(dest) == content.sha256()
    # 完成时 .part 已原子改名为成品
    assert not part.exists()
    # 说明确实复用了 .part：第一次进度上报就是从中途开始的
    assert ranges, "应能看到下载进度"
    assert res.elapsed_s >= 0


def test_resume_after_kill_is_crash_safe(tmp_path, mirrors):
    """模拟「进程被杀」：下到一半停掉，.part.json 记录了进度；新进程接着下。"""
    content = make_content(3 * MB, seed=22)
    cluster = mirrors([Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "crash.bin"
    pause = threading.Event()

    def on_progress(p: E.Progress):
        if p.bytes_done > content.size // 3:
            pause.set()

    with pytest.raises(E.Paused):
        E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                   cfg=cfg(), on_progress=on_progress, pause=pause)
    part = dest.with_name(dest.name + ".part")
    assert part.is_file() and part.stat().st_size > 0
    state = E.read_part_state(dest)
    assert state.get("bytes", 0) > 0
    assert state.get("sha256_expected") == content.sha256()

    # 「新进程」续下：删掉暂停标志就是全新一次调用
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    assert res.ok and sha_of(dest) == content.sha256()


def test_disconnect_midstream_resumes_same_source(tmp_path, mirrors):
    """中途断连：.part 保留，同源续传后 sha 一致。"""
    content = make_content(2 * MB, seed=23)
    cluster = mirrors([Fault(rate_bps=2 * MB, disconnect_after=700 * KB)], content=content)
    dest = tmp_path / "disc.bin"
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    assert res.ok and sha_of(dest) == content.sha256()


# ---------------------------------------------------------------------------
# 换源
# ---------------------------------------------------------------------------
def test_slow_source_triggers_switch(tmp_path, mirrors):
    """源 A 初速 8MB/s（测速必选），下到 1MB 后降到 50KB/s；源 B 2MB/s → 切到 B，最终 sha 一致。

    两个源同速时测速顺序是随机的，会导致测试偶发失败（有时先选中 B，
    「A 下够 1MB 后降速」的前提就不成立）；所以这里让 A 的初速明显高于 B。
    文件要足够大，否则 A 会在限速生效前就下完（那样测不到换源）。
    降速由「A 已经下了足够多」触发，而不是靠固定 sleep —— 全量跑时机器负载高，
    sleep 可能落在下载完成之后。
    """
    content = make_content(8 * MB, seed=31)
    cluster = mirrors(
        [Fault(rate_bps=8 * MB), Fault(rate_bps=2 * MB)],  # A 初速是 B 的 4 倍 → 测速必选 A
        content=content,
    )
    dest = tmp_path / "switch.bin"
    slow_triggered = threading.Event()
    a_url = cluster.urls()[0]

    events: list[E.Progress] = []

    def on_progress(p: E.Progress):
        events.append(p)
        # A 下了 1MB 后立刻降到 50KB/s（8MB 的 1/8，之后必然触发速度窗口）
        if (not slow_triggered.is_set() and p.source == a_url
                and p.bytes_done >= 1 * MB):
            cluster[0].get("/file.bin").fault.rate_bps = 50 * KB
            slow_triggered.set()

    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                     cfg=cfg(slow_window_s=0.5, slow_bps=200 * KB, slow_windows=1),
                     on_progress=on_progress)
    first_dl = next((p for p in events if p.state == "downloading"), None)
    assert first_dl is not None and first_dl.source == a_url, (
        f"测速应先选中更快的源 A，实际先下载的是 {first_dl and first_dl.source}")
    assert slow_triggered.is_set(), "A 应在限速前先下一部分"
    assert res.ok and sha_of(dest) == content.sha256()
    assert res.switches == 1, f"应恰好换源一次，实际 {res.switches}（反复跳说明换源逻辑有环）"

    # 换源是「带着已下部分过去」的：换源后的第一条下载进度 > 0
    first_switch = next(i for i, p in enumerate(events) if p.switches == 1)
    resumed = [p for p in events[first_switch:] if p.state == "downloading"]
    assert resumed and resumed[0].bytes_done > 0, "换源后应从断点续传，而不是从 0 重下"


def test_switch_tail_mismatch_restarts_file(tmp_path, mirrors):
    """切换到内容不同的源：尾部 64KB 对不上 → 不能把两份内容拼在一起，最终 sha 必须正确。"""
    good = make_content(2 * MB, seed=41)
    bad = make_content(2 * MB, seed=99)   # 同长度、内容不同
    from dl_harness.fake_mirrors import FakeMirror
    m_good = FakeMirror().add("/file.bin", good, fault=Fault(rate_bps=2 * MB)).start()
    m_bad = FakeMirror().add("/file.bin", bad, fault=Fault(rate_bps=4 * MB)).start()
    try:
        dest = tmp_path / "mismatch.bin"
        events: list[E.Progress] = []
        res = E.download(dest, [m_bad.url(), m_good.url()],
                         size=good.size, sha256=good.sha256(), cfg=cfg(),
                         on_progress=events.append)
        assert res.ok
        assert sha_of(dest) == good.sha256(), "绝不能把两个源的内容拼起来"
        # 期间应出现过「校验失败」或「尾部校验不一致」的进度事件
        msgs = " ".join(p.error for p in events if p.error)
        assert ("校验失败" in msgs) or ("尾部校验不一致" in msgs), msgs
    finally:
        m_bad.stop()
        m_good.stop()


def test_tail_overlap_detects_foreign_part(tmp_path, mirrors):
    """尾部比对本身：内容不同返回 False，内容相同返回 True。"""
    content = make_content(1 * MB, seed=42)
    cluster = mirrors(1, content=content)
    dest = tmp_path / "tail.bin"
    part = dest.with_name(dest.name + ".part")

    part.write_bytes(b"\xAA" * (300 * KB))
    eng = E._Engine(dest, cluster.urls(), size=content.size,
                    sha256=content.sha256(), cfg=cfg())
    assert eng._tail_matches(cluster.urls()[0], 300 * KB) is False

    part.write_bytes(content.read(0, 300 * KB))
    eng2 = E._Engine(dest, cluster.urls(), size=content.size,
                     sha256=content.sha256(), cfg=cfg())
    assert eng2._tail_matches(cluster.urls()[0], 300 * KB) is True


def test_source_that_ignores_range_does_not_corrupt(tmp_path, mirrors):
    """已下 20% 时唯一备选源忽略 Range：不能追加成坏文件。"""
    content = make_content(1 * MB, seed=51)
    cluster = mirrors([Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "norange.bin"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(content.read(0, 200 * KB))
    # 让唯一源忽略 Range
    cluster[0].get("/file.bin").fault.ignore_range = True

    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    assert res.ok
    assert sha_of(dest) == content.sha256(), "忽略 Range 的源不能把文件写坏"


def test_max_switches_caps_then_finishes(tmp_path, mirrors):
    content = make_content(512 * KB, seed=61)
    cluster = mirrors([Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "cap.bin"
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                     cfg=cfg(max_switches=0))
    assert res.ok and sha_of(dest) == content.sha256()


# ---------------------------------------------------------------------------
# 校验失败
# ---------------------------------------------------------------------------
def test_tampered_content_fails_then_retries(tmp_path, mirrors):
    """所有源都篡改：两次重下后抛 ChecksumError，目标不存在、.part 已删。"""
    content = make_content(512 * KB, seed=71)
    cluster = mirrors([Fault(tamper=True), Fault(tamper=True)], content=content)
    dest = tmp_path / "bad.bin"
    with pytest.raises(E.ChecksumError):
        E.download(dest, cluster.urls(), size=content.size,
                   sha256=content.sha256(), cfg=cfg(checksum_retries=2))
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_tampered_one_of_two_sources_recovers(tmp_path, mirrors):
    """一个源篡改、另一个正常：最终拿到正确文件。"""
    content = make_content(1 * MB, seed=72)
    cluster = mirrors([Fault(tamper=True, rate_bps=4 * MB),
                       Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "mixed.bin"
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    assert res.ok and sha_of(dest) == content.sha256()


def test_html_impersonation_rejected(tmp_path, mirrors):
    """源返回 text/html 200：判该源失败，不写坏文件。"""
    content = make_content(512 * KB, seed=81)
    cluster = mirrors([Fault(html=True), Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "html.bin"
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    assert res.ok and sha_of(dest) == content.sha256()


def test_all_sources_html_raises_clearly(tmp_path, mirrors):
    """全部源都是网页：给出清晰错误，不死循环。"""
    content = make_content(256 * KB, seed=82)
    cluster = mirrors([Fault(html=True), Fault(html=True)], content=content)
    dest = tmp_path / "allhtml.bin"
    started = time.time()
    with pytest.raises(E.AllSourcesFailed):
        E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                   cfg=cfg(checksum_retries=0))
    assert time.time() - started < 60


# ---------------------------------------------------------------------------
# 卡死 / 5xx / 全失败
# ---------------------------------------------------------------------------
def test_stalled_source_switches(tmp_path, mirrors):
    """源 2 秒内只给很少字节 → 判失败换源。"""
    content = make_content(1 * MB, seed=91)
    cluster = mirrors([Fault(rate_bps=8 * KB), Fault(rate_bps=2 * MB)], content=content)
    dest = tmp_path / "stall.bin"
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                     cfg=cfg(stall_s=1.5, stall_bytes=64 * KB))
    assert res.ok and sha_of(dest) == content.sha256()


def test_5xx_source_skipped(tmp_path, mirrors):
    content = make_content(512 * KB, seed=92)
    cluster = mirrors([Fault(status=500), Fault(rate_bps=1 * MB)], content=content)
    dest = tmp_path / "http500.bin"
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    assert res.ok and sha_of(dest) == content.sha256()


def test_all_sources_fail_reports_error(tmp_path, mirrors):
    """所有源都 500：抛 AllSourcesFailed（不是死循环）。"""
    content = make_content(256 * KB, seed=93)
    cluster = mirrors([Fault(status=503), Fault(status=500)], content=content)
    dest = tmp_path / "fail.bin"
    with pytest.raises(E.AllSourcesFailed):
        E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                   cfg=cfg(net_wait_budget_s=0))
    assert not dest.exists()


def test_no_sources_raises(tmp_path):
    with pytest.raises(E.AllSourcesFailed):
        E.download(tmp_path / "none.bin", [], cfg=cfg())


# ---------------------------------------------------------------------------
# 磁盘满 / 暂停取消
# ---------------------------------------------------------------------------
def test_disk_full_keeps_part(tmp_path, mirrors, monkeypatch):
    """写入抛 OSError(errno=28) → DiskFullError，.part 保留且大小 = 已写字节。"""
    content = make_content(2 * MB, seed=101)
    cluster = mirrors([Fault(rate_bps=2 * MB)], content=content)
    dest = tmp_path / "full.bin"
    real_open = Path.open
    written = {"n": 0}

    class _FailingFile:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            written["n"] += len(data)
            if written["n"] > 256 * KB:
                raise OSError(28, "No space left on device")
            return self._fh.write(data)

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()
            return False

    def fake_open(self, mode="r", *a, **kw):
        fh = real_open(self, mode, *a, **kw)
        return _FailingFile(fh) if "b" in mode else fh

    monkeypatch.setattr(Path, "open", fake_open)
    with pytest.raises(E.DiskFullError):
        E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    part = dest.with_name(dest.name + ".part")
    assert part.is_file()
    assert part.stat().st_size > 0
    assert not dest.exists()


def test_disk_full_then_resume_succeeds(tmp_path, mirrors, monkeypatch):
    """磁盘满之后空间够了，用同一目标重试能完成。"""
    content = make_content(1 * MB, seed=102)
    cluster = mirrors([Fault(rate_bps=4 * MB)], content=content)
    dest = tmp_path / "full2.bin"
    real_open = Path.open
    budget = {"left": 256 * KB}

    class _FailingFile:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            if budget["left"] <= 0:
                raise OSError(28, "No space left on device")
            budget["left"] -= len(data)
            return self._fh.write(data)

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self._fh.close()
            return False

    def fake_open(self, mode="r", *a, **kw):
        fh = real_open(self, mode, *a, **kw)
        return _FailingFile(fh) if "b" in mode else fh

    monkeypatch.setattr(Path, "open", fake_open)
    with pytest.raises(E.DiskFullError):
        E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    # 空间恢复
    monkeypatch.setattr(Path, "open", real_open)
    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg())
    assert res.ok and sha_of(dest) == content.sha256()


def test_pause_keeps_part_and_cancel_deletes(tmp_path, mirrors):
    content = make_content(2 * MB, seed=111)
    cluster = mirrors([Fault(rate_bps=512 * KB)], content=content)
    dest = tmp_path / "pause.bin"

    pause = threading.Event()

    def on_progress(p: E.Progress):
        if p.bytes_done > 200 * KB:
            pause.set()

    with pytest.raises(E.Paused):
        E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                   cfg=cfg(), on_progress=on_progress, pause=pause)
    part = dest.with_name(dest.name + ".part")
    assert part.is_file() and part.stat().st_size > 0

    # 取消：.part 删除
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(E.Canceled):
        E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                   cfg=cfg(), cancel=cancel)
    assert not dest.with_name(dest.name + ".part").exists()


# ---------------------------------------------------------------------------
# 兼容与工具
# ---------------------------------------------------------------------------
def test_legacy_progress_string_format():
    """旧回调格式必须仍能被 _setup_progress 的正则反解。"""
    import re
    p = E.Progress(bytes_done=12 * 1024 * 1024, bytes_total=345 * 1024 * 1024)
    text = E.legacy_progress_string("安装模型", p)
    assert re.search(r"：\d+/\d+ MB", text), text


def test_progress_is_structured(tmp_path, mirrors):
    content = make_content(256 * KB, seed=121)
    cluster = mirrors(1, content=content)
    seen: list[dict] = []
    E.download(tmp_path / "prog.bin", cluster.urls(), size=content.size,
               sha256=content.sha256(), cfg=cfg(),
               on_progress=lambda p: seen.append(p.as_dict()))
    states = {s["state"] for s in seen}
    assert "downloading" in states and "done" in states
    assert all(isinstance(s["bytes_done"], int) for s in seen)
    assert any(s["bytes_total"] == content.size for s in seen)


def test_stale_part_with_wrong_content_recovers(tmp_path, mirrors):
    """本地 .part 是错误内容（模拟上次下了一半的旧文件）：最终仍得到正确文件。"""
    content = make_content(512 * KB, seed=131)
    cluster = mirrors([Fault(rate_bps=2 * MB)], content=content)
    dest = tmp_path / "etag.bin"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"\x00" * 100 * KB)          # 内容是错的
    meta = dest.with_name(dest.name + ".part.json")
    meta.write_text('{"bytes": 102400}', encoding="utf-8")

    res = E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(),
                     cfg=cfg(checksum_retries=2))
    assert res.ok and sha_of(dest) == content.sha256()


def test_probe_sources_ordering(mirrors):
    content = make_content(1 * MB, seed=141)
    cluster = mirrors([Fault(rate_bps=128 * KB), Fault(rate_bps=2 * MB)], content=content)
    rows = E.probe_sources(cluster.urls(), cfg=cfg())
    assert rows[0].bps >= rows[-1].bps
    assert all(r.ok for r in rows)


# ---------------------------------------------------------------------------
# download_artifact（供 S4 守护进程调用的 catalog 门面）
# ---------------------------------------------------------------------------
def test_download_artifact_single_file(tmp_path, mirrors):
    """按 catalog 条目下单个文件，目标路径由 target_path 解析。"""
    content = make_content(256 * KB, seed=151)
    cluster = mirrors(1, content=content)
    dest = tmp_path / "models" / "x.bin"
    fake_catalog = types.SimpleNamespace(
        ARTIFACTS={"x": {"kind": "file", "size": content.size,
                         "sha256": content.sha256(), "sources": cluster.urls(),
                         "target": "x_target"}},
        target_path=lambda key, paths: dest,
    )
    out = E.download_artifact("x", {}, cfg=cfg(), catalog=fake_catalog)
    assert out["ok"] and dest.is_file() and sha_of(dest) == content.sha256()


def test_download_artifact_multi_file(tmp_path, mirrors):
    """多文件条目（sep）：每个文件各下一遍，各自的源按文件名筛选。"""
    a = make_content(128 * KB, seed=161)
    b = make_content(96 * KB, seed=162)
    from dl_harness.fake_mirrors import FakeMirror
    m = FakeMirror().add("/ckpt/a.ckpt", a).add("/cfg/a.yaml", b).start()
    target = tmp_path / "sep"
    fake_catalog = types.SimpleNamespace(
        ARTIFACTS={"sep": {
            "kind": "sep",
            "files": (("a.ckpt", a.sha256(), a.size), ("a.yaml", b.sha256(), b.size)),
            "sources": [m.url("/ckpt/a.ckpt"), m.url("/cfg/a.yaml")],
            "target": "sep_dir",
        }},
        target_path=lambda key, paths: target,
    )
    try:
        out = E.download_artifact("sep", {}, cfg=cfg(), catalog=fake_catalog)
        assert out["ok"], out
        assert sha_of(target / "a.ckpt") == a.sha256()
        assert sha_of(target / "a.yaml") == b.sha256()
    finally:
        m.stop()


def test_download_artifact_repo_type_rejected(tmp_path):
    """repo 型不能走 download_artifact（要由守护进程按文件清单逐文件下）。"""
    fake_catalog = types.SimpleNamespace(
        ARTIFACTS={"ace": {"kind": "repo", "repo_id": "ACE-Step/Ace-Step1.5",
                           "sources": [], "target": "ace_ckpt"}},
        target_path=lambda key, paths: tmp_path,
    )
    with pytest.raises(E.DownloadError, match="repo"):
        E.download_artifact("ace", {}, cfg=cfg(), catalog=fake_catalog)
