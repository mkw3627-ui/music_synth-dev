"""S6 入口层验收（对应 PLAN_download_tiering.md D-S6）。

测试对象是 ``download_entries`` 里的纯逻辑，不实例化插件类（插件类需要宿主 SDK，
按计划 D-S6「实例化成本过高则在 S12 真宿主用 48915 验证」）。所有用例不依赖外网：
DM 门面用 monkeypatch 替身，S5 安装器用假 ``installer``，状态文件写临时目录。

覆盖 D-S6 全部场景 + 任务要求的「至少包括」清单：
- ``ms_dl_status`` 在 5MB state 文件下 <200ms；
- 选级 / 选功能后入队集合正确；
- 暂停 / 恢复 / 取消 / 重试命令生效；
- 功能就绪状态正确显示（合并磁盘就绪 + 下载中/失败）；
- 全源失败时错误展示 + 重试入口；
- ``ms_setup_install({})`` 旧行为不变；``features+tiers`` 完成后 base_completed 且自动入队 T2；
- startup 自动续下 / shutdown 关停。
"""
from __future__ import annotations

import json
import time
from importlib import import_module
from pathlib import Path

import pytest

catalog = import_module("download_catalog")
de = import_module("download_entries")


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def make_paths(tmp_path: Path, **raw) -> dict:
    return {
        "compute_root": str(tmp_path / "root"),
        "data_dir": str(tmp_path / "data"),
        "raw": dict(raw),
    }


def _state_json(tmp_path: Path, payload: dict) -> Path:
    root = tmp_path / "root"
    sd = root / "_downloads"
    sd.mkdir(parents=True, exist_ok=True)
    path = sd / "state.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class FakeInstaller:
    """S5 installer 替身：记录 install 调用，返回可配置结果。"""

    def __init__(self, install_result=None, check_result=None) -> None:
        self.install_calls: list[tuple] = []
        self._install_result = install_result or {"ok": True, "installed": [], "errors": []}
        self._check_result = check_result or {"items": [], "ready": False, "missing": []}

    def check_dependencies(self, paths, probe=None):
        return dict(self._check_result)

    def install(self, paths, items, progress, probe=None):
        self.install_calls.append((list(items) if items is not None else None))
        return dict(self._install_result)

    def download_portable_python(self, paths, progress):
        return "C:/fake/python.exe"


def _busy_item(state="downloading", feature="cover", error="") -> dict:
    return {"key": "ddsp_contentvec", "feature": feature, "tier": "T2", "state": state,
            "bytes_done": 1000, "bytes_total": 378342945, "speed_bps": 200000.0,
            "source": "https://hf-mirror.net/x", "source_id": "hf-mirror.net",
            "switches": 0, "error": error}


# ---------------------------------------------------------------------------
# ms_dl_status：<200ms 且只读
# ---------------------------------------------------------------------------
def test_dl_status_reads_state_json(tmp_path):
    paths = make_paths(tmp_path)
    _state_json(tmp_path, {"version": 1, "daemon": {"pid": 123, "running": True},
                           "totals": {"bytes_done": 5}, "items": {"a": {"state": "done"}}})
    out = de.dl_status(paths)
    assert out["daemon"]["pid"] == 123
    assert out["items"]["a"]["state"] == "done"


def test_dl_status_under_200ms_on_5mb_state(tmp_path):
    # 构造一个约 5MB 的 state.json：大量 item，每个带 files 数组
    items = {}
    for i in range(14000):
        items[f"item_{i}"] = {
            "key": f"item_{i}", "feature": "cover", "tier": "T2", "state": "downloading",
            "bytes_done": i, "bytes_total": 100000, "speed_bps": 12345.0,
            "source": "https://hf-mirror.net/a/b", "source_id": "hf-mirror.net",
            "switches": 0, "error": "",
            "files": [{"rel": f"f{i}.bin", "state": "downloading",
                       "bytes_done": i, "bytes_total": 100000, "dest": "D:\\x\\f.bin"}],
        }
    _state_json(tmp_path, {"version": 1, "daemon": {"pid": 1, "running": True},
                           "totals": {}, "items": items})
    size = (tmp_path / "root" / "_downloads" / "state.json").stat().st_size
    assert size > 4_000_000, f"state.json 只有 {size} 字节，测不出 <200ms 意义"

    paths = make_paths(tmp_path)
    t0 = time.perf_counter()
    out = de.dl_status(paths)
    elapsed = time.perf_counter() - t0
    assert len(out["items"]) == 14000
    assert elapsed < 0.2, f"ms_dl_status 用了 {elapsed:.3f}s（要求 <0.2s）"


# ---------------------------------------------------------------------------
# ms_dl_enqueue：选级 / 选功能 → 正确集合
# ---------------------------------------------------------------------------
def test_dl_enqueue_features_defaults_to_t2(monkeypatch):
    captured = {}
    monkeypatch.setattr(de.DM, "enqueue",
                        lambda paths, keys, **kw: captured.update(keys=keys, kw=kw) or {"ok": True})
    de.dl_enqueue({"compute_root": "C:/x"}, features=["cover"])
    assert set(captured["keys"]) == set(catalog.artifacts_for(["cover"], ["T2"]))


def test_dl_enqueue_train_includes_cover_t2(monkeypatch):
    captured = {}
    monkeypatch.setattr(de.DM, "enqueue",
                        lambda paths, keys, **kw: captured.update(keys=keys) or {"ok": True})
    de.dl_enqueue({"compute_root": "C:/x"}, features=["train"])
    keys = set(captured["keys"])
    # 训练底模 + cover 的 T2 全部（依赖自动展开）
    assert "ddsp_reflow_base" in keys
    assert "ddsp_contentvec" in keys
    assert "sep_karaoke" in keys
    # 可选的低/高质量底模默认不入队
    assert "ddsp_base_low" not in keys
    assert "ddsp_base_high" not in keys


def test_dl_enqueue_artifacts_override(monkeypatch):
    captured = {}
    monkeypatch.setattr(de.DM, "enqueue",
                        lambda paths, keys, **kw: captured.update(keys=keys) or {"ok": True})
    de.dl_enqueue({"compute_root": "C:/x"}, artifacts=["ddsp_vocoder", "ddsp_rmvpe"])
    assert captured["keys"] == ["ddsp_vocoder", "ddsp_rmvpe"]


def test_dl_enqueue_explicit_tiers(monkeypatch):
    captured = {}
    monkeypatch.setattr(de.DM, "enqueue",
                        lambda paths, keys, **kw: captured.update(keys=keys) or {"ok": True})
    de.dl_enqueue({"compute_root": "C:/x"}, features=["create"], tiers=["T2"])
    assert set(captured["keys"]) == set(catalog.artifacts_for(["create"], ["T2"]))


# ---------------------------------------------------------------------------
# ms_dl_control：暂停/恢复/取消/重试
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["pause", "resume", "cancel", "retry", "prioritize"])
def test_dl_control_valid_actions(monkeypatch, action):
    calls = []
    monkeypatch.setattr(de.DM, "control",
                        lambda paths, a, target="all", **kw: calls.append((a, target)) or {"ok": True})
    res = de.dl_control({"compute_root": "C:/x"}, action, "all")
    assert res["ok"] is True
    assert calls == [(action, "all")]


def test_dl_control_single_target(monkeypatch):
    calls = []
    monkeypatch.setattr(de.DM, "control",
                        lambda paths, a, target="all", **kw: calls.append((a, target)))
    de.dl_control({"compute_root": "C:/x"}, "pause", "ddsp_contentvec")
    assert calls == [("pause", "ddsp_contentvec")]


def test_dl_control_rejects_unknown_action():
    res = de.dl_control({"compute_root": "C:/x"}, "nuke", "all")
    assert res["ok"] is False
    assert "未知控制动作" in res["error"]


# ---------------------------------------------------------------------------
# ms_feature_status：功能就绪（合并磁盘就绪 + 下载中/失败）
# ---------------------------------------------------------------------------
def test_feature_status_merges_running_and_failed(tmp_path):
    state = {"daemon": {"pid": 9, "running": True}, "totals": {"active": 1, "failed": 1},
             "items": {
                 "ddsp_contentvec": _busy_item("downloading", "cover"),
                 "ddsp_reflow_base": _busy_item("failed", "train", "校验失败"),
             }}
    ts = {"base_completed": True, "tiers": {},
          "features": {
              "cover": {"label": "翻唱", "ready": False, "t0_ready": True,
                        "t1_ready": True, "t2_ready": False, "missing": []},
              "train": {"label": "音色训练", "ready": False, "t0_ready": True,
                        "t1_ready": True, "t2_ready": False, "missing": []},
          }}
    out = de.feature_status(make_paths(tmp_path), tier_status=ts, state=state)

    assert out["base_completed"] is True
    assert out["features"]["cover"]["running"] == ["ddsp_contentvec"]
    assert out["features"]["cover"]["failed"] == []
    assert out["features"]["train"]["running"] == []
    assert out["features"]["train"]["failed"] == ["ddsp_reflow_base"]
    # 下载中项的完整进度透传给 UI
    assert out["features"]["cover"]["downloads"]["ddsp_contentvec"]["bytes_total"] == 378342945


def test_feature_status_all_sources_failed_shows_error_and_retry(tmp_path, monkeypatch):
    """全源失败：feature 的 failed 里带错误信息；retry 入口是 ms_dl_control(action=retry)。"""
    state = {"daemon": {}, "totals": {"failed": 1}, "items": {
        "ddsp_contentvec": _busy_item("failed", "cover", "校验失败（文件已损坏），已从两个源重试")}}
    out = de.feature_status(make_paths(tmp_path), state=state)
    failed_item = out["features"]["cover"]["downloads"]["ddsp_contentvec"]
    assert "校验失败" in failed_item["error"]
    assert "ddsp_contentvec" in out["features"]["cover"]["failed"]

    # 重试入口：UI 点「重试」→ ms_dl_control(action="retry", target=key)
    calls = []
    monkeypatch.setattr(de.DM, "control",
                        lambda paths, a, target="all", **kw: calls.append((a, target)))
    de.dl_control(make_paths(tmp_path), "retry", "ddsp_contentvec")
    assert calls == [("retry", "ddsp_contentvec")]


def test_feature_status_without_tier_status_still_reports_downloads(tmp_path):
    state = {"daemon": {}, "totals": {}, "items": {"ddsp_vocoder": _busy_item("downloading", "cover")}}
    out = de.feature_status(make_paths(tmp_path), state=state)
    assert "running" in out["features"]["cover"]
    assert out["features"]["cover"]["downloads"]["ddsp_vocoder"]["state"] == "downloading"


# ---------------------------------------------------------------------------
# ms_setup_install：旧行为不变 + features/tiers 分级
# ---------------------------------------------------------------------------
def test_run_install_legacy_passes_items_through():
    installer = FakeInstaller()
    res = de.run_install(installer, make_paths(Path("C:/x")), items=["ddsp_repo"])
    assert installer.install_calls == [["ddsp_repo"]]
    assert res["ok"] is True


def test_run_install_legacy_none_means_all():
    installer = FakeInstaller()
    de.run_install(installer, make_paths(Path("C:/x")), items=None)
    assert installer.install_calls == [None]  # items=None → 装全部缺失项


def test_run_install_tiered_calls_install_tiering(monkeypatch):
    """features/tiers 给定时走 install_tiering，T2 进 deferred。"""
    calls = {}

    def fake_install_tiered(installer, paths, **kw):
        calls.update(kw=kw, installer=installer)
        return {"ok": True, "deferred": ["ddsp_contentvec", "ddsp_vocoder"],
                "base_completed": True, "report": {"ready": False}, "installed": ["ddsp_repo"]}

    monkeypatch.setattr(de.tiering, "install_tiered", fake_install_tiered)
    installer = FakeInstaller()
    res = de.run_install(installer, make_paths(Path("C:/x")),
                         features=["cover"], tiers=["T0", "T1"])
    assert calls["kw"]["features"] == ["cover"]
    assert calls["kw"]["tiers"] == ["T0", "T1"]
    assert calls["installer"] is installer
    assert res["deferred"] == ["ddsp_contentvec", "ddsp_vocoder"]
    assert res["base_completed"] is True


def test_maybe_enqueue_deferred_daemon(monkeypatch):
    captured = {}
    monkeypatch.setattr(de.DM, "enqueue",
                        lambda paths, keys, **kw: captured.update(keys=keys) or {"ok": True})
    paths = make_paths(Path("C:/x"), download_engine="daemon")
    out = de.maybe_enqueue_deferred(paths, ["ddsp_contentvec", "ddsp_vocoder"])
    assert captured["keys"] == ["ddsp_contentvec", "ddsp_vocoder"]
    assert out["ok"] is True


def test_maybe_enqueue_deferred_legacy_skips(monkeypatch):
    monkeypatch.setattr(de.DM, "enqueue", lambda *a, **kw: pytest.fail("legacy 不应入队"))
    paths = make_paths(Path("C:/x"), download_engine="legacy")
    out = de.maybe_enqueue_deferred(paths, ["ddsp_contentvec"])
    assert out["skipped"] == "legacy"
    assert out["count"] == 0


def test_maybe_enqueue_deferred_empty():
    assert de.maybe_enqueue_deferred(make_paths(Path("C:/x")), []) is None


# ---------------------------------------------------------------------------
# startup 自动续下 / shutdown 关停
# ---------------------------------------------------------------------------
def test_resume_on_startup_when_items_pending(monkeypatch):
    monkeypatch.setattr(de.DM, "read_state",
                        lambda paths: {"daemon": {}, "totals": {}, "items": {"x": {"state": "downloading"}}})
    started = []
    monkeypatch.setattr(de.DM, "ensure_daemon", lambda paths, **kw: started.append(paths) or {"started": True, "pid": 1})
    out = de.resume_on_startup(make_paths(Path("C:/x"), download_auto_resume="true"))
    assert out["started"] is True
    assert started == [make_paths(Path("C:/x"), download_auto_resume="true")]


def test_resume_on_startup_no_items_noop(monkeypatch):
    monkeypatch.setattr(de.DM, "read_state", lambda paths: {"daemon": {}, "totals": {}, "items": {}})
    monkeypatch.setattr(de.DM, "ensure_daemon", lambda *a, **kw: pytest.fail("不应拉起"))
    out = de.resume_on_startup(make_paths(Path("C:/x")))
    assert out["started"] is False
    assert out["reason"] == "no-items"


def test_resume_on_startup_disabled(monkeypatch):
    monkeypatch.setattr(de.DM, "ensure_daemon", lambda *a, **kw: pytest.fail("不应拉起"))
    out = de.resume_on_startup(make_paths(Path("C:/x"), download_auto_resume="false"))
    assert out["started"] is False
    assert out["reason"] == "auto_resume=off"


def test_stop_on_shutdown_keep_after_exit(monkeypatch):
    monkeypatch.setattr(de.DM, "daemon_pid", lambda paths: 42)
    monkeypatch.setattr(de.DM, "stop_daemon", lambda *a, **kw: pytest.fail("keep 时不应停"))
    out = de.stop_on_shutdown(make_paths(Path("C:/x"), download_keep_after_exit="true"))
    assert out["stopped"] is False
    assert out["reason"] == "keep-after-exit"


def test_stop_on_shutdown_stops(monkeypatch):
    monkeypatch.setattr(de.DM, "daemon_pid", lambda paths: 42)
    stopped = []
    monkeypatch.setattr(de.DM, "stop_daemon", lambda paths, **kw: stopped.append(paths) or {"stopped": True, "pid": 42})
    out = de.stop_on_shutdown(make_paths(Path("C:/x"), download_keep_after_exit="false"))
    assert out["stopped"] is True
    assert stopped == [make_paths(Path("C:/x"), download_keep_after_exit="false")]


# ---------------------------------------------------------------------------
# 配置开关默认值
# ---------------------------------------------------------------------------
def test_config_defaults():
    paths = {"raw": {}}
    assert de.engine_mode(paths) == "daemon"
    assert de.auto_resume(paths) is True
    assert de.keep_after_exit(paths) is True


def test_config_explicit_values():
    assert de.engine_mode({"raw": {"download_engine": "legacy"}}) == "legacy"
    assert de.auto_resume({"raw": {"download_auto_resume": "0"}}) is False
    assert de.keep_after_exit({"raw": {"download_keep_after_exit": "false"}}) is False


# ---------------------------------------------------------------------------
# D1 修复：dl_speedtest 合并成一批并行测速，避免超宿主 30s 上限
# ---------------------------------------------------------------------------
def test_dl_speedtest_flattens_and_groups(monkeypatch):
    """所有下载物的所有源拍平成一批，按 url 归组回 artifact。"""
    import download_engine as engine

    def fake_probe(urls, *, cfg=None, opener=None):
        # 每个 url 返回一个 ProbeResult，速度用 url 里的序号模拟
        out = []
        for u in urls:
            ok = "bad" not in u
            bps = 100.0 if ok else 0.0
            out.append(engine.ProbeResult(url=u, ok=ok, status=206 if ok else 0,
                                          bps=bps, total=0, content_type="",
                                          range_ok=True, error="" if ok else "x",
                                          seconds=0.1))
        return out

    monkeypatch.setattr(de, "engine", engine)
    monkeypatch.setattr(engine, "EngineConfig", lambda **kw: type("C", (), {"probe_bytes": 0, "probe_budget_s": 0})())
    # 用两个真实 catalog key 的源，验证归组
    r = de.dl_speedtest({"compute_root": "C:/x"}, artifact="sep_karaoke", probe=fake_probe)
    assert r["ok"] is True
    assert "sep_karaoke" in r["items"]
    assert len(r["items"]["sep_karaoke"]) == len(catalog.sources_for("sep_karaoke"))
    # chosen 取速度最快的可用源
    assert r["chosen"]["sep_karaoke"]["ok"] is True


def test_dl_speedtest_probe_called_once_with_all_sources(monkeypatch):
    """所有下载物一次性测速，而不是逐个串行调用。"""
    import download_engine as engine

    calls = []
    def fake_probe(urls, *, cfg=None, opener=None):
        calls.append(list(urls))
        return [engine.ProbeResult(url=u, ok=True, bps=1.0, status=206) for u in urls]

    monkeypatch.setattr(de, "engine", engine)
    monkeypatch.setattr(engine, "EngineConfig", lambda **kw: type("C", (), {"probe_bytes": 0, "probe_budget_s": 0})())
    de.dl_speedtest({"compute_root": "C:/x"}, probe=fake_probe)
    assert len(calls) == 1, f"应一次拍平测速，实际调用了 {len(calls)} 次"
    # 拍平后的源数量 = 所有非 repo 下载物源数之和
    expected = sum(len(catalog.sources_for(k))
                   for k, spec in catalog.ARTIFACTS.items()
                   if spec.get("sources") and spec.get("kind") != "repo")
    assert len(calls[0]) == expected, f"应覆盖所有源 {expected}，实际 {len(calls[0])}"
