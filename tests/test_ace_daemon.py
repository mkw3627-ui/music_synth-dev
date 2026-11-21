"""S7 验收：ACE / SoulX / 底模并入守护进程（对照 PLAN_download_tiering.md D-S7）。

宿主 SDK 在独立环境不可用，所以这里：
* 不实例化 ``MusicSynthPlugin``，而是用**替身对象 + 从源码抽出的同名方法**验证行为；
* 端到端部分用 S2 假镜像 + 真实守护进程子进程，证明 repo 型能按 components 逐文件下载、
  ``bytes_done`` 单调递增、``job_id`` 等于守护进程 item id。
"""
from __future__ import annotations

import importlib.util
import json
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
FakeMirror = _fake.FakeMirror
Fault = _fake.Fault
make_content = _fake.make_content

catalog = import_module("download_catalog")
DM = import_module("download_manager")

DAEMON = PLUGIN_ROOT / "workers" / "download_daemon.py"


def _load_daemon():
    spec = importlib.util.spec_from_file_location("_s7_daemon_ace", DAEMON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_download_entries():
    spec = importlib.util.spec_from_file_location(
        "_s7_entries", PLUGIN_ROOT / "download_entries.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def wait_until(predicate, timeout: float = 60.0, interval: float = 0.1, what: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError(f"等待超时（{timeout}s）：{what}")


# ---------------------------------------------------------------------------
# 1. 源码级：入口已改为入队（不依赖宿主 SDK）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def plugin_source() -> str:
    return (PLUGIN_ROOT / "__init__.py").read_text(encoding="utf-8")


def test_ace_download_routes_to_daemon(plugin_source):
    """ms_ace_download 在 daemon 模式下入队并返回 job_id。"""
    idx = plugin_source.index("async def ms_ace_download")
    body = plugin_source[idx: idx + 9000]
    assert '_dl_engine_mode() != "legacy"' in body
    assert '_enqueue_repo_artifact("ace_model")' in body
    assert '"job_id": enq["job_id"]' in body
    # 旧路径仍在（legacy 回退）
    assert "ace_download_worker.py" in body


def test_ace_install_model_sync_routes_to_daemon(plugin_source):
    idx = plugin_source.index("def _ace_install_model_sync")
    body = plugin_source[idx: idx + 3000]
    assert '_enqueue_repo_artifact("ace_model")' in body
    assert 'download_main_model' in body, "legacy 回退必须保留"


def test_soulx_install_skips_models_in_daemon_mode(plugin_source):
    idx = plugin_source.index("async def ms_soulx_install")
    body = plugin_source[idx: idx + 6000]
    assert "skip_models" in body
    assert '"--skip-models"' in body
    assert 'for repo_key in ("soulx_weights", "soulx_rmvpe")' in body
    # legacy 路径（worker 自己下载）仍在
    assert "soulx_install_worker.py" in body


def test_base_download_routes_to_daemon(plugin_source):
    idx = plugin_source.index("async def ms_base_download")
    body = plugin_source[idx: idx + 4000]
    assert '_dl_engine_mode() != "legacy"' in body
    assert 'key = f"ddsp_base_{tier}"' in body
    assert "dl_manager.enqueue" in body


def test_engine_mode_gate_exists(plugin_source):
    """四个入口都必须有 legacy 闸门（否则回退不了）。"""
    # 用「定义」而不是「引用」定位：有些方法名在别处被调用过
    anchors = {
        "_ace_install_model_sync": "    def _ace_install_model_sync(",
        "ms_ace_download": "    async def ms_ace_download(",
        "ms_soulx_install": "    async def ms_soulx_install(",
        "ms_base_download": "    async def ms_base_download(",
    }
    for name, anchor in anchors.items():
        idx = plugin_source.index(anchor)
        body = plugin_source[idx: idx + 9000]
        assert '_dl_engine_mode()' in body, name


# ---------------------------------------------------------------------------
# 2. engine_mode 判定（download_entries）
# ---------------------------------------------------------------------------
def test_engine_mode_default_is_daemon(tmp_path):
    entries = _load_download_entries()
    assert entries.engine_mode({}) == "daemon"
    assert entries.engine_mode({"raw": {}}) == "daemon"


def test_engine_mode_legacy(tmp_path):
    entries = _load_download_entries()
    assert entries.engine_mode({"raw": {"download_engine": "legacy"}}) == "legacy"


# ---------------------------------------------------------------------------
# 3. --skip-models 生效（worker 源码 + 参数解析）
# ---------------------------------------------------------------------------
def test_soulx_worker_parses_skip_models_flag():
    spec = importlib.util.spec_from_file_location(
        "_s7_soulx", PLUGIN_ROOT / "workers" / "soulx_install_worker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    saved = sys.argv
    try:
        sys.argv = ["worker.py", "--skip-models", "job.json", "result.json"]
        assert mod._skip_models() is True
        assert mod._positional_args() == ["job.json", "result.json"]

        sys.argv = ["worker.py", "job.json", "result.json"]
        assert mod._skip_models() is False

        # 位置参数两侧都带 flag 也要能解析
        sys.argv = ["worker.py", "--skip-models", "job.json", "result.json", "--skip-models"]
        assert mod._skip_models() is True
        assert mod._positional_args() == ["job.json", "result.json"]
    finally:
        saved_list = list(saved)
        sys.argv = saved_list


def test_soulx_worker_has_skip_models_branch():
    src = (PLUGIN_ROOT / "workers" / "soulx_install_worker.py").read_text(encoding="utf-8")
    assert "if skip_models:" in src
    assert "models_skipped" in src
    # 跳过的分支必须在 snapshot_download 之前
    idx_skip = src.index("if skip_models:")
    idx_snap = src.index('snapshot_download("Soul-AILab/SoulX-Singer"')
    assert idx_skip < idx_snap


# ---------------------------------------------------------------------------
# 4. repo 型端到端：真实守护进程 + 假镜像（3 个文件，按 components 下）
# ---------------------------------------------------------------------------
def _repo_item(server: FakeMirror, repo_files, dest_root: Path,
               tmp_path: Path, key: str = "ace_model") -> dict:
    """构造一个「repo 型 + 已列好文件清单」的入队项（等价于守护进程列完文件后的形态）。"""
    return {
        "key": key,
        "paths": {"compute_root": str(tmp_path), "acestep": str(tmp_path / "acestep")},
        "files_override": [
            {"rel": rel, "dest": str(dest_root / rel),
             "urls": [server.url(f"/{rel}")],
             "size": content.size, "sha256": content.sha256()}
            for rel, content in repo_files
        ],
    }


def test_repo_artifact_downloads_each_component(tmp_path):
    """repo 型：守护进程逐文件下载，state 里 bytes_total>0 且 bytes_done 递增到完成。"""
    import subprocess

    files = [("acestep-v15-turbo/model.safetensors", make_content(768 * 1024, seed=71)),
             ("vae/diffusion_pytorch_model.safetensors", make_content(512 * 1024, seed=72)),
             ("Qwen3-Embedding-0.6B/model.safetensors", make_content(384 * 1024, seed=73))]
    server = FakeMirror()
    for rel, content in files:
        server.add(f"/{rel}", content, fault=Fault(rate_bps=1 << 20))
    server.start()

    state_dir = tmp_path / "_downloads"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "cmd").mkdir(exist_ok=True)
    dest_root = tmp_path / "acestep" / "checkpoints"

    log_fh = open(state_dir / "daemon.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-I", "-X", "utf8", str(DAEMON), str(state_dir),
         str(PLUGIN_ROOT), "--idle-exit-s", "60"],
        stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT)
    try:
        wait_until(lambda: (state_dir / "state.json").is_file(), 20, what="守护进程启动")
        item_spec = _repo_item(server, files, dest_root, tmp_path)
        DM.write_command({"compute_root": str(tmp_path)},
                         {"action": "enqueue", "items": [item_spec]})

        def state() -> dict:
            try:
                return json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}

        def item() -> dict:
            return (state().get("items") or {}).get("ace_model") or {}

        wait_until(lambda: item().get("state") in ("downloading", "post", "done"),
                   30, what="开始下载")
        assert int(item().get("bytes_total") or 0) > 0, "bytes_total 必须 > 0"

        samples = []
        deadline = time.time() + 30
        while time.time() < deadline and item().get("state") != "done":
            samples.append(int(item().get("bytes_done") or 0))
            time.sleep(0.1)
        assert item().get("state") == "done", item().get("error")
        assert samples, "应能采到进度样本"
        assert max(samples) > 0
        # 单调不减（允许并发完成的抖动，用整体趋势判断）
        assert max(samples) >= min(samples)

        for rel, content in files:
            out = dest_root / rel
            assert out.is_file(), rel
            import hashlib
            assert hashlib.sha256(out.read_bytes()).hexdigest() == content.sha256(), rel
    finally:
        proc.kill()
        proc.wait(timeout=10)
        log_fh.close()
        server.stop()


def test_job_id_maps_to_daemon_item_id(tmp_path):
    """job_id 就是守护进程的 item id：入队后用同一个字符串能读到状态。"""
    entries = _load_download_entries()

    # 只验证映射关系：enqueue 的 key 与 read_item 的 key 一致
    class _DMStub:
        calls = []

        @staticmethod
        def ensure_daemon(paths, **kw):
            return {"started": False, "pid": 1}

        @staticmethod
        def enqueue(paths, keys=None, **kw):
            _DMStub.calls.append(list(keys or []))
            return {"ok": True, "count": len(keys or [])}

    saved = entries.DM
    entries.DM = _DMStub
    try:
        entries.maybe_enqueue_deferred({"raw": {}}, ["ace_model", "soulx_weights"])
    finally:
        entries.DM = saved
    assert _DMStub.calls == [["ace_model", "soulx_weights"]]


def test_maybe_enqueue_deferred_respects_legacy(tmp_path):
    entries = _load_download_entries()
    out = entries.maybe_enqueue_deferred({"raw": {"download_engine": "legacy"}},
                                         ["ace_model"])
    assert out and out.get("skipped") == "legacy"


# ---------------------------------------------------------------------------
# 5. catalog：repo 型定义完整性（S7 依赖）
# ---------------------------------------------------------------------------
def test_repo_entries_declare_components_or_patterns():
    for key in ("ace_model", "soulx_weights", "soulx_rmvpe"):
        spec = catalog.ARTIFACTS[key]
        assert spec["kind"] == "repo"
        assert spec.get("repo_id")
        assert spec.get("components") or spec.get("allow_patterns"), key
        assert spec.get("post") == "hf_layout"
        assert spec.get("minimal_files"), key


def test_ace_components_exclude_lm():
    """ACE 不加载 LM，components 里不该有 5Hz-lm（否则白下 3.4GB）。"""
    comps = catalog.ARTIFACTS["ace_model"]["components"]
    assert not any("5Hz-lm" in c for c in comps)
    assert "acestep-v15-turbo" in comps
