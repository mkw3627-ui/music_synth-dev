"""S5 验收：分级安装后端（对照 PLAN_download_tiering.md D-S5）。

不依赖外网、不真装环境：所有 installer 都用可注入的替身（``FakeInstaller``），
只记录调用序列。``setup.py`` / ``runtime.py`` 的真实对象只在「结构一致性」类用例里
按需导入（用合成包名，避免碰宿主 SDK）。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

import download_catalog as catalog
import install_tiering as T

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 替身 installer
# ---------------------------------------------------------------------------
class FakeInstaller:
    """最小 installer 协议实现：只记录调用，不做任何真实安装。"""

    def __init__(self, *, ready_keys=None, fail_keys=None, known_keys=None):
        self.ready_keys = set(ready_keys or ())
        self.fail_keys = set(fail_keys or ())
        self.calls: list[dict] = []
        self.known_keys = known_keys      # None = 用 catalog 的 key 全集

    def _known(self) -> set[str]:
        if self.known_keys is not None:
            return set(self.known_keys)
        return set(catalog.ARTIFACTS)

    def check_dependencies(self, paths, probe=None):
        items = []
        for key in sorted(self._known()):
            spec = catalog.ARTIFACTS.get(key) or {}
            ready = key in self.ready_keys
            items.append({
                "key": key,
                "label": spec.get("label", key),
                "status": "ready" if ready else "missing",
                "ok": ready,
                "required": bool(spec.get("required", True)),
                "installable": not ready,
                "detail": "",
                "fix": "",
                "size_gb": round(int(spec.get("size") or 0) / 2 ** 30, 2),
                "action": "",
            })
        missing = [i["key"] for i in items
                   if i["status"] not in ("ready", "optional", "na") and i["required"]]
        return {"ok": not missing, "ready": not missing, "items": items,
                "missing": missing, "optional_missing": [],
                "installable_missing": [i["key"] for i in items if i["installable"]],
                "actions": [], "total_missing_gb": 0,
                "required_missing_gb": 0, "optional_missing_gb": 0}

    def install(self, paths, items, progress, probe=None):
        self.calls.append({"items": list(items) if items is not None else None,
                           "paths": dict(paths or {})})
        installed, errors = [], []
        for key in (items or []):
            if key in self.fail_keys:
                errors.append({"item": key, "error": "模拟失败"})
            else:
                installed.append(key)
                self.ready_keys.add(key)
        return {"ok": not errors, "installed": installed, "skipped": [],
                "errors": errors, "missing_after": []}

    def download_portable_python(self, paths, progress):
        return str(Path(paths.get("compute_root") or ".") / "python.exe")


@pytest.fixture
def paths(tmp_path):
    """一个最小的 paths 结构（只用到 compute_root / data_dir）。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return {
        "compute_root": tmp_path / "root",
        "configured": True,
        "data_dir": data_dir,
        "envs_dir": tmp_path / "root" / "envs",
        "models_dir": tmp_path / "root" / "models",
        "work_dir": tmp_path / "work",
        "sep_model_dir": tmp_path / "root" / "models" / "sep",
        "ddsp_root": tmp_path / "root" / "ddsp" / "DDSP-SVC",
        "synth_python": tmp_path / "root" / "envs" / "ddsp" / "Scripts" / "python.exe",
        "sep_python": tmp_path / "root" / "envs" / "ddsp" / "Scripts" / "python.exe",
    }


# ---------------------------------------------------------------------------
# 集合分层（与 catalog 的 tier 完全一致）
# ---------------------------------------------------------------------------
def test_foreground_items_are_t0_t1_only():
    keys = T.foreground_items()
    assert keys, "应有前台项"
    for key in keys:
        assert catalog.ARTIFACTS[key]["tier"] in ("T0", "T1"), key


def test_deferred_items_are_t2_only():
    keys = T.deferred_items()
    assert keys
    for key in keys:
        assert catalog.ARTIFACTS[key]["tier"] == "T2", key


def test_tier_items_match_catalog_tiers():
    """每一层的集合与 catalog 的 tier 字段严格一致。"""
    for tier in T.TIERS:
        got = set(T.tier_items(None, [tier]))
        want = {k for k, v in catalog.ARTIFACTS.items()
                if v["tier"] == tier and v.get("required", True)}
        assert got == want, f"{tier} 集合不一致：多 {got - want} 少 {want - got}"


def test_cover_foreground_and_deferred():
    """cover：前台 6 个 T0 + ddsp_repo；延后 4 个 T2，且不含 ACE。"""
    fg = set(T.foreground_items(["cover"]))
    assert fg == {"base_python", "ffmpeg", "synth_env", "torch", "ddsp_deps",
                  "audio_separator", "ddsp_repo"}
    deferred = set(T.deferred_items(["cover"]))
    assert deferred == {"ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke"}
    assert "ace_model" not in deferred


def test_create_pulls_cover_and_adds_ace():
    fg = set(T.foreground_items(["create"]))
    assert {"synth_env", "torch", "ddsp_repo"} <= fg
    assert "ace_deps" in fg, "原创要在前台装 ACE 依赖"
    deferred = set(T.deferred_items(["create"]))
    assert "ace_model" in deferred
    assert "soulx_weights" not in deferred, "soulx 不是 create 的下游"


def test_train_excludes_optional_bases_by_default():
    deferred = set(T.deferred_items(["train"]))
    assert "ddsp_reflow_base" in deferred
    assert "ddsp_base_low" not in deferred
    assert "ddsp_base_high" not in deferred
    with_opt = set(T.deferred_items(["train"])) | set()
    assert with_opt == deferred


def test_capture_pulls_cover_env():
    fg = set(T.foreground_items(["capture"]))
    assert {"synth_env", "torch", "ddsp_deps", "capture_native"} <= fg


# ---------------------------------------------------------------------------
# feature_plan / 体积
# ---------------------------------------------------------------------------
def test_feature_plan_sizes_match_catalog():
    plan = T.feature_plan("cover")
    sizes = catalog.feature_sizes("cover")
    assert plan["sizes"]["T0"] == sizes["T0"]
    assert plan["sizes"]["T2"] == sizes["T2"]
    assert plan["sizes"]["total"] == sizes["total"]
    assert set(plan["T0"]) == set(catalog.feature_items("cover")["T0"])


def test_feature_plan_all_features():
    for feature in catalog.FEATURES:
        plan = T.feature_plan(feature)
        assert set(plan) >= {"T0", "T1", "T2", "sizes"}
        assert plan["sizes"]["total"] == sum(plan["sizes"][t] for t in T.TIERS)


# ---------------------------------------------------------------------------
# check_tiered：标注与汇总
# ---------------------------------------------------------------------------
def test_check_tiered_annotates_items(paths):
    fake = FakeInstaller()
    rep = T.check_tiered(fake, paths)
    for item in rep["items"]:
        assert "tier" in item and item["tier"] in T.TIERS, item["key"]
        assert "feature" in item
    cover = rep["features"]["cover"]
    assert cover["ready"] is False
    assert cover["fg_gb"] > 0 and cover["bg_gb"] > 0


def test_fg_bg_gb_match_catalog_sums(paths):
    rep = T.check_tiered(FakeInstaller(), paths)
    for feature, summary in rep["features"].items():
        sizes = catalog.feature_sizes(feature)
        want_fg = round((sizes["T0"] + sizes["T1"]) / 2 ** 30, 2)
        want_bg = round(sizes["T2"] / 2 ** 30, 2)
        assert summary["fg_gb"] == pytest.approx(want_fg, abs=0.01), feature
        assert summary["bg_gb"] == pytest.approx(want_bg, abs=0.01), feature


def test_check_tiered_ready_when_all_present(paths):
    fake = FakeInstaller(ready_keys=list(catalog.ARTIFACTS))
    rep = T.check_tiered(fake, paths)
    assert rep["ready"] is True
    assert rep["features"]["cover"]["ready"] is True
    assert rep["features"]["cover"]["missing"] == []


def test_tier_status_base_completed(paths):
    fake = FakeInstaller()
    status = T.tier_status(fake, paths)
    assert status["base_completed"] is False

    base_keys = [k for k in T.foreground_items()
                 if catalog.ARTIFACTS[k].get("required", True)]
    fake2 = FakeInstaller(ready_keys=base_keys)
    status2 = T.tier_status(fake2, paths)
    assert status2["tiers"]["T0"]["ready"] is True
    assert status2["tiers"]["T1"]["ready"] is True
    assert status2["base_completed"] is True
    # T2 还空着，所以整体不算 ready
    assert status2["ready"] is False


def test_annotate_is_idempotent(paths):
    rep = T.check_tiered(FakeInstaller(), paths)
    again = T.annotate_report_with_paths(rep, paths)
    assert again["features"].keys() == rep["features"].keys()
    for item in again["items"]:
        assert item["tier"] in T.TIERS


# ---------------------------------------------------------------------------
# install_tiered：只装前台、T2 延后
# ---------------------------------------------------------------------------
def test_install_cover_t0_t1_only(paths):
    """D-S5 核心场景：install(features=["cover"], tiers=["T0","T1"])。"""
    fake = FakeInstaller()
    res = T.install_tiered(fake, paths, features=["cover"], tiers=["T0", "T1"])

    assert len(fake.calls) == 1
    called = set(fake.calls[0]["items"])
    expected = {"base_python", "ffmpeg", "synth_env", "torch", "ddsp_deps",
                "audio_separator", "ddsp_repo"}
    assert called == expected, f"多装 {called - expected} 少装 {expected - called}"
    assert "ace_deps" not in called, "cover 不该装 ACE"
    assert set(res["deferred"]) == {"ddsp_contentvec", "ddsp_vocoder",
                                    "ddsp_rmvpe", "sep_karaoke"}
    assert res["requested_tiers"] == ["T0", "T1"]


def test_install_default_tiers_are_foreground(paths):
    fake = FakeInstaller()
    res = T.install_tiered(fake, paths, features=["cover"])
    assert res["requested_tiers"] == ["T0", "T1"]
    assert set(res["deferred"]) == {"ddsp_contentvec", "ddsp_vocoder",
                                    "ddsp_rmvpe", "sep_karaoke"}


def test_install_train_defers_reflow_only(paths):
    fake = FakeInstaller()
    res = T.install_tiered(fake, paths, features=["train"], tiers=["T0", "T1"])
    assert "ddsp_reflow_base" in res["deferred"]
    assert "ddsp_base_low" not in res["deferred"]
    assert "ddsp_base_high" not in res["deferred"]


def test_install_create_includes_ace_deps_foreground(paths):
    fake = FakeInstaller()
    res = T.install_tiered(fake, paths, features=["create"], tiers=["T0", "T1"])
    called = set(fake.calls[0]["items"])
    assert "ace_deps" in called
    assert "ace_model" in res["deferred"]


def test_install_t2_not_downloaded_by_default(paths):
    """即使 tiers 全给，T2 默认也不进 installer（交守护进程）。"""
    fake = FakeInstaller()
    res = T.install_tiered(fake, paths, features=["cover"], tiers=list(T.TIERS))
    called = set(fake.calls[0]["items"])
    for key in ("ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke"):
        assert key not in called, key
        assert key in res["deferred"], key


def test_install_t2_when_include_t2_explicit(paths):
    """legacy 回退：显式 include_t2=True 才真装 T2。"""
    fake = FakeInstaller()
    res = T.install_tiered(fake, paths, features=["cover"], tiers=list(T.TIERS),
                           include_t2=True)
    called = set(fake.calls[0]["items"])
    assert {"ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke"} <= called
    assert res["deferred"] == []


def test_install_reports_failures_and_can_retry(paths):
    """失败项记录在 errors 里；再次调用只装缺的。"""
    fake = FakeInstaller(fail_keys={"torch"})
    res1 = T.install_tiered(fake, paths, features=["cover"], tiers=["T0", "T1"])
    assert any(e["item"] == "torch" for e in res1["errors"])
    assert res1["ok"] is False

    fake.fail_keys.clear()
    res2 = T.install_tiered(fake, paths, features=["cover"], tiers=["T0", "T1"])
    assert res2["ok"] is True
    assert len(fake.calls) == 2


def test_install_records_base_completed(paths):
    fake = FakeInstaller()
    res = T.install_tiered(fake, paths, features=["cover"], tiers=["T0", "T1"])
    assert res["base_completed"] is True
    state_path = Path(paths["data_dir"]) / "setup_state.json"
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["base_completed"] is True
    assert state["completed"] is False, "T2 还没下，completed 应为 False"


def test_install_ignores_unknown_catalog_keys(paths):
    """installer 不认识的 key（如宿主自检项）应过滤掉，不进 install()。"""
    fake = FakeInstaller(known_keys={"synth_env", "torch"})
    T.install_tiered(fake, paths, features=["cover"], tiers=["T0", "T1"])
    assert set(fake.calls[0]["items"]) == {"synth_env", "torch"}


# ---------------------------------------------------------------------------
# 回归：不传 features/tiers 的旧行为
# ---------------------------------------------------------------------------
def test_legacy_install_sequence_unchanged(paths):
    """``install(items=None)`` 的旧调用（直接调 setup.install）不受本模块影响。"""
    fake = FakeInstaller()
    # 模拟旧调用：items=None 表示"装所有缺失"
    fake.install(paths, None, lambda *_: None)
    assert fake.calls[0]["items"] is None


def test_recording_installer_captures_calls(paths):
    inner = FakeInstaller()
    rec = T.RecordingInstaller(inner)
    T.install_tiered(rec, paths, features=["cover"], tiers=["T0", "T1"])
    assert len(rec.calls) == 1
    assert set(rec.calls[0]["items"]) == {
        "base_python", "ffmpeg", "synth_env", "torch", "ddsp_deps",
        "audio_separator", "ddsp_repo"}


def test_install_tiered_without_features_covers_all(paths):
    """features=None：前台装所有功能的 T0/T1，T2 全部延后。"""
    fake = FakeInstaller()
    res = T.install_tiered(fake, paths, tiers=["T0", "T1"])
    called = set(fake.calls[0]["items"])
    want = {k for k, v in catalog.ARTIFACTS.items()
            if v["tier"] in ("T0", "T1") and v.get("required", True)}
    assert called == want
    assert set(res["deferred"]) == {k for k, v in catalog.ARTIFACTS.items()
                                    if v["tier"] == "T2" and v.get("required", True)}


# ---------------------------------------------------------------------------
# setup.py / runtime.py 的结构一致性（合成包名导入，不碰宿主 SDK）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def plugin_modules():
    """用合成包名导入 setup / runtime，避免插件目录名带连字符与宿主 SDK 依赖。"""
    name = "_ms_tiering_probe"
    if name in sys.modules:
        return sys.modules[name]
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[name] = pkg
    import importlib
    mods = types.SimpleNamespace(
        setup=importlib.import_module(f"{name}.setup"),
        runtime=importlib.import_module(f"{name}.runtime"),
        tiering=importlib.import_module("install_tiering"),
        # install_tiering 里内部 import download_catalog 是顶层的，已可正常用
    )
    sys.modules[name] = mods
    return mods


def test_setup_exposes_tiered_entrypoints(plugin_modules):
    S = plugin_modules.setup
    assert hasattr(S, "install_tiered")
    assert hasattr(S, "check_dependencies_tiered")
    # 旧入口必须仍在
    assert hasattr(S, "install") and hasattr(S, "check_dependencies")


def test_install_signature_is_backward_compatible(plugin_modules):
    """``install(paths, items, progress, probe)`` 的旧位置参数仍能调用。"""
    import inspect
    sig = inspect.signature(plugin_modules.setup.install)
    params = list(sig.parameters)
    assert params[:4] == ["paths", "items", "progress", "probe"], params


def test_portable_python_url_is_fixed(plugin_modules):
    """S1 实测：shared-install_only 变体不存在（404），必须用 install_only。"""
    url = plugin_modules.setup.PYTHON_STANDALONE_PINNED
    assert "shared-install_only" not in url, f"仍是 404 的 URL：{url}"
    assert url.endswith("cpython-3.10.18+20250612-x86_64-pc-windows-msvc-install_only.tar.gz")


def test_python_standalone_url_matches_catalog(plugin_modules):
    """setup 里的固定 URL 与 catalog 的 base_python_pinned 必须指向同一个产物。"""
    import urllib.parse
    setup_name = urllib.parse.unquote(
        plugin_modules.setup.PYTHON_STANDALONE_PINNED.rsplit("/", 1)[-1])
    cat_src = catalog.ARTIFACTS["base_python_pinned"]["sources"]
    assert any(urllib.parse.unquote(u).endswith(setup_name) for u in cat_src), \
        f"catalog 里没有 {setup_name}"


def test_ffmpeg_signature_compatible(plugin_modules):
    """``ffmpeg_status(configured)`` 的旧单参调用仍可用（新参数有默认值）。"""
    import inspect
    sig = inspect.signature(plugin_modules.runtime.ffmpeg_status)
    params = list(sig.parameters)
    assert params[0] == "configured"
    assert "compute_root" in params
    # 新增参数必须有默认值（旧调用不用改）
    for name in params[1:]:
        assert sig.parameters[name].default is not inspect.Parameter.empty


def test_find_ffmpeg_accepts_compute_root(tmp_path, plugin_modules, monkeypatch):
    """兜底目录 tools/ffmpeg/bin 能被识别（屏蔽 PATH/winget 以免本机干扰）。"""
    import shutil as _shutil
    monkeypatch.setattr(plugin_modules.runtime.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(plugin_modules.runtime, "_FFMPEG_HINTS", ())
    monkeypatch.setattr(plugin_modules.runtime, "_find_winget_package_ffmpeg", lambda: "")
    exe = tmp_path / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    found = plugin_modules.runtime._find_ffmpeg(tmp_path)
    assert found == str(exe)
    assert _shutil  # 保持导入有使用


def test_ffmpeg_fallback_placed_zip(tmp_path, plugin_modules, monkeypatch):
    """ffmpeg 兜底：zip 下载成功 → 解出 bin → tools/ffmpeg/bin/ffmpeg.exe。"""
    import zipfile
    S = plugin_modules.setup
    root = tmp_path
    zip_path = root / "tools" / "_ffmpeg_dl" / "ffmpeg.zip"
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ffmpeg-9.0-essentials_build/bin/ffmpeg.exe", b"binary-stub")
        zf.writestr("ffmpeg-9.0-essentials_build/bin/ffprobe.exe", b"probe-stub")
        zf.writestr("ffmpeg-9.0-essentials_build/README.txt", b"readme")

    dest = root / "tools" / "ffmpeg" / "bin"
    S._place_ffmpeg_zip(zip_path, dest)
    assert (dest / "ffmpeg.exe").is_file()
    assert (dest / "ffprobe.exe").is_file()
    assert not (dest / "README.txt").exists(), "只铺 bin 里的内容"


def test_ffmpeg_fallback_install_path(tmp_path, plugin_modules, monkeypatch):
    """_install_ffmpeg_zip 走完整流程（下载用替身），产出 tools/ffmpeg/bin/ffmpeg.exe。"""
    import zipfile
    S = plugin_modules.setup
    root = tmp_path

    def fake_download(dest, sources, **kw):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("ffmpeg/bin/ffmpeg.exe", b"stub")

        class R:
            ok = True
            bytes = 4
            sha256 = ""
            source = "stub"
            switches = 0
            elapsed_s = 0.0
            path = str(dest)
            error = ""
        return R()

    import importlib
    pkg_name = "download_engine"
    real = importlib.import_module(pkg_name)
    monkeypatch.setattr(real, "download", fake_download)
    # setup 内部是 from . import download_engine，指向同一模块对象
    res = S._install_ffmpeg_zip({"compute_root": root})
    assert res["ok"] is True, res
    assert Path(res["path"]).is_file()
    assert Path(res["path"]).name == "ffmpeg.exe"


def test_setup_state_roundtrip(paths):
    state_path = Path(paths["data_dir"]) / "setup_state.json"
    state_path.write_text(json.dumps({"base_completed": True, "completed": True}),
                          encoding="utf-8")
    state = T.setup_state(paths)
    assert state["base_completed"] is True
    assert T.setup_state({"data_dir": paths["data_dir"] / "missing"}) == {}


def test_non_artifact_items_not_tiered():
    """宿主自检项不在 catalog 里，不应被当成下载物。"""
    for key in T.NON_ARTIFACT_ITEMS:
        assert key not in catalog.ARTIFACTS, key


# ---------------------------------------------------------------------------
# 与真实 setup 的集成（不联网、不装东西：只跑 check + 分级标注）
# ---------------------------------------------------------------------------
def test_real_setup_check_dependencies_is_tiered(plugin_modules, tmp_path):
    """真实 check_dependencies 的报告能被分级标注（不联网，只读本机状态）。"""
    S = plugin_modules.setup
    fake_paths = _full_paths(tmp_path)
    report = S.check_dependencies(fake_paths, probe={"gpu": None})
    assert "items" in report
    tiered = T.annotate_report_with_paths(report, fake_paths)
    for item in tiered["items"]:
        assert "tier" in item, item.get("key")
    # 需要真正下载的项必须带正确层级
    for key in ("synth_env", "torch", "ddsp_contentvec"):
        item = next((i for i in tiered["items"] if i["key"] == key), None)
        if item is not None:
            assert item["tier"] == catalog.ARTIFACTS[key]["tier"]


def test_real_setup_install_tiered_dispatch(plugin_modules, tmp_path, monkeypatch):
    """真实 setup.install_tiered 把调用转发给 install_tiering（install 被替身拦截）。"""
    S = plugin_modules.setup
    captured: list[list] = []

    def fake_install(paths, items, progress, probe=None):
        captured.append(list(items) if items is not None else None)
        return {"ok": True, "installed": list(items or []), "skipped": [], "errors": []}

    monkeypatch.setattr(S, "install", fake_install)
    paths = _full_paths(tmp_path)
    res = S.install_tiered(paths, lambda *_: None, features=["cover"],
                           tiers=["T0", "T1"], probe={"gpu": None})
    assert captured, "install 应被调用一次"
    called = set(captured[0] or [])
    assert "synth_env" in called and "ddsp_repo" in called
    assert "ddsp_contentvec" not in called, "T2 不应进前台安装"
    assert set(res["deferred"]) >= {"ddsp_contentvec", "ddsp_vocoder",
                                    "ddsp_rmvpe", "sep_karaoke"}


def _full_paths(tmp_path) -> dict:
    """setup.check_dependencies 需要的一整套 paths（与 runtime.resolve_paths 的键一致）。"""
    root = tmp_path / "root"
    ddsp = root / "ddsp" / "DDSP-SVC"
    return {
        "compute_root": root, "configured": True,
        "data_dir": tmp_path / "data",
        "envs_dir": root / "envs", "models_dir": root / "models",
        "work_dir": tmp_path / "work", "sep_model_dir": root / "models" / "sep",
        "ddsp_root": ddsp, "ddsp_ckpt": "", "ddsp_config": ddsp / "configs" / "reflow.yaml",
        "ddsp_pretrain_reflow": ddsp / "pretrain" / "model_0.pt",
        "ddsp_base_low": ddsp / "pretrain" / "bases" / "low.pt",
        "ddsp_base_mid": ddsp / "pretrain" / "model_0.pt",
        "ddsp_base_high": ddsp / "pretrain" / "bases" / "high.pt",
        "ddsp_pretrain_contentvec": ddsp / "pretrain" / "contentvec" / "pytorch_model.bin",
        "ddsp_pretrain_rmvpe": ddsp / "pretrain" / "rmvpe" / "model.pt",
        "ddsp_pretrain_vocoder": ddsp / "pretrain" / "nsf_hifigan",
        "ddsp_vocoder_config": ddsp / "pretrain" / "nsf_hifigan" / "config.json",
        "synth_python": root / "envs" / "ddsp" / "Scripts" / "python.exe",
        "sep_python": root / "envs" / "ddsp" / "Scripts" / "python.exe",
        "raw": {},
    }
