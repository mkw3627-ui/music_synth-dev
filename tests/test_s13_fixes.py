"""S13 验收缺陷修复的回归测试（D-A / D-B / D-C / D-D / D-I）。

对照 `_reports/dl_tiering_e2e/ACCEPTANCE_S13.md` 第 5 节。

* **D-A** `setup.py` 的源表还有已剔除的死源（hf-mirror.com、ghfast.top 等）
* **D-B** 守护进程读配置用错键名（`_raw` ≠ `raw`），10 个 `download_*` 键在真机不生效
* **D-C** `download_manager._daemon_config_options` 绝对导入，宿主里静默失败
* **D-D** 设置页存的 `download_source` 引擎不读，下拉框无效果
* **D-I** 换源阈值比计划收紧（1MB/s + 3 倍那条被 200KB/s 门槛挡住）
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from importlib import import_module
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_WORKERS = str(PLUGIN_ROOT / "workers")
if _WORKERS not in sys.path:
    sys.path.insert(0, _WORKERS)

catalog = import_module("download_catalog")
engine = import_module("download_engine")
entries = import_module("download_entries")
dm = import_module("download_manager")

DAEMON = PLUGIN_ROOT / "workers" / "download_daemon.py"


def _load_setup():
    """把 setup 当子包导入（包内相对导入需要）。"""
    name = "_s13_setup_probe"
    if name in sys.modules:
        mods = sys.modules[name]
        if hasattr(mods, "setup"):
            return mods.setup
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[name] = pkg
    mod = importlib.import_module(f"{name}.setup")
    return mod


def _load_compat():
    name = "_s13_compat_probe"
    if name in sys.modules:
        mods = sys.modules[name]
        if hasattr(mods, "compat"):
            return mods.compat
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[name] = pkg
    return importlib.import_module(f"{name}.compat")


@pytest.fixture(scope="module")
def daemon():
    spec = importlib.util.spec_from_file_location("_s13_daemon", DAEMON)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# D-A：源表里不许有已剔除的死源
# ---------------------------------------------------------------------------
REJECTED_HOSTS = (
    "hf-mirror.com",          # 已退化为 308 跳回 huggingface.co
    "ghfast.top",             # TLS 握手超时
    "hk.gh-proxy.com",        # 500 Domain Not Found
    "gh.llkk.cc", "ghp.ci", "hub.gitmirror.com",
    "gh-proxy.net", "gitproxy.click", "ghproxy.cc",
    "gh.api.99988866.xyz",
)


def test_setup_gh_mirrors_from_catalog():
    S = _load_setup()
    assert tuple(S.GH_MIRRORS) == tuple(catalog.GH_MIRRORS)


def test_setup_hf_mirrors_from_catalog():
    S = _load_setup()
    assert tuple(S.HF_MIRROR_HOSTS) == tuple(catalog.HF_MIRRORS)


@pytest.mark.parametrize("host", REJECTED_HOSTS)
def test_no_rejected_host_in_setup_tables(host):
    S = _load_setup()
    blob = " ".join(list(S.GH_MIRRORS) + list(S.HF_MIRROR_HOSTS)
                    + list(S.CONTENTVEC_URLS))
    assert host not in blob, f"{host} 仍在 setup 源表里"


def test_contentvec_urls_have_no_dead_mirror():
    S = _load_setup()
    for url in S.CONTENTVEC_URLS:
        assert "hf-mirror.com/" not in url
    assert any("hf-mirror.net" in u for u in S.CONTENTVEC_URLS)
    assert any("huggingface.co" in u for u in S.CONTENTVEC_URLS)


def test_github_mirrors_have_three_sources():
    """GitHub 类下载物需要 ≥3 个实测可用的源（S1 完成定义）。"""
    assert len(catalog.GH_MIRRORS) >= 3
    assert any("cors.isteed.cc" in m for m in catalog.GH_MIRRORS)


def test_speed_targets_use_live_sources_only():
    """设置页「测下载速度」（compat.speed_targets）不许再测死源。"""
    C = _load_compat()
    rows = C.speed_targets()
    assert rows
    for row in rows:
        for host in REJECTED_HOSTS:
            assert host not in row["url"], f"{row['label']} 指向死源 {host}"


def test_mirror_urls_only_expands_github():
    """`_mirror_urls` 只给 GitHub 域名加镜像前缀，不碰 HF。"""
    S = _load_setup()
    hf = "https://huggingface.co/a/b/resolve/main/c.bin"
    assert S._mirror_urls(hf) == [hf]
    gh = "https://github.com/a/b/releases/download/v1/c.zip"
    expanded = S._mirror_urls(gh)
    assert len(expanded) == len(catalog.GH_MIRRORS) + 1
    assert any("gh-proxy.com" in u for u in expanded)


# ---------------------------------------------------------------------------
# D-B：守护进程真的读到配置（走 _engine_cfg_for 这条真实路径）
# ---------------------------------------------------------------------------
def _item(raw: dict, tmp_path: Path) -> dict:
    """构造一个入队项，paths 的形态与 DM.enqueue 写进命令文件的一致。"""
    paths = {
        "compute_root": str(tmp_path),
        "configured": "True",
        "raw": raw,
    }
    return {"key": "ddsp_contentvec", "paths": paths, "files_override": []}


def _cfg_for(daemon_mod, raw: dict, tmp_path: Path):
    d = daemon_mod.DownloadDaemon(tmp_path / "_downloads", idle_exit_s=0)
    try:
        return d._engine_cfg_for(_item(raw, tmp_path))
    finally:
        d._stop.set()


def test_daemon_reads_raw_config(daemon, tmp_path):
    """核心回归：raw 里的键必须真的影响 EngineConfig（S13 复现此条失效）。"""
    cfg = _cfg_for(daemon, {
        "download_verify_hash": False,
        "download_max_mbps": 1,
        "download_source_strategy": "mirror_first",
        "download_resume": False,
        "download_check_ssl": False,
        "download_cache_days": 2,
        "download_max_retries": 3,
    }, tmp_path)
    assert cfg.verify_hash is False, "download_verify_hash 未生效"
    assert cfg.max_bps == 1024 * 1024, "download_max_mbps 未生效"
    assert cfg.source_strategy == "mirror_first", "download_source_strategy 未生效"
    assert cfg.resume_enabled is False, "download_resume 未生效"
    assert cfg.check_ssl is False, "download_check_ssl 未生效"
    assert cfg.probe_ttl_s == 2 * 86400.0, "download_cache_days 未生效"
    assert cfg.max_switches == 3, "download_max_retries 未生效"


def test_daemon_keeps_net_wait_zero(daemon, tmp_path):
    """守护进程语义：常驻进程不等网络（这条不能被配置覆盖）。"""
    cfg = _cfg_for(daemon, {}, tmp_path)
    assert cfg.net_wait_budget_s == 0.0


def test_daemon_config_defaults_when_missing(daemon, tmp_path):
    cfg = _cfg_for(daemon, {}, tmp_path)
    default = engine.EngineConfig()
    assert cfg.verify_hash is default.verify_hash
    assert cfg.max_switches == default.max_switches
    assert cfg.source_strategy == default.source_strategy


def test_daemon_accepts_json_string_raw(daemon, tmp_path):
    """raw 若是 JSON 字符串（老命令文件）也要能解析。"""
    cfg = _cfg_for(daemon, {}, tmp_path)
    assert cfg is not None
    coerced = daemon._coerce_config_dict('{"download_max_retries": 5}')
    assert coerced == {"download_max_retries": 5}
    assert daemon._coerce_config_dict(None) == {}
    assert daemon._coerce_config_dict("not json") == {}


def test_daemon_caches_by_raw_and_paths(daemon, tmp_path):
    """同一份配置只算一次；配置变了要重算。"""
    d = daemon_mod = daemon.DownloadDaemon(tmp_path / "_dl", idle_exit_s=0)
    try:
        c1 = d._engine_cfg_for(_item({"download_max_retries": 2}, tmp_path))
        c2 = d._engine_cfg_for(_item({"download_max_retries": 2}, tmp_path))
        assert c1 is c2, "相同配置应命中缓存"
        c3 = d._engine_cfg_for(_item({"download_max_retries": 7}, tmp_path))
        assert c3 is not c1 and c3.max_switches == 7, "配置变化必须重算"
    finally:
        d._stop.set()
    _ = daemon_mod


def test_daemon_enqueue_then_cfg_keeps_raw(daemon, tmp_path):
    """真机复现：守护进程 enqueue 把 paths 存进 state 后，raw 仍要能读回来。

    之前 ``_as_paths`` 把 dict 转成 Python repr（单引号），json.loads 失败 → 全部默认值。
    """
    d = daemon.DownloadDaemon(tmp_path / "_downloads", idle_exit_s=0)
    try:
        raw = {"download_max_mbps": 1, "download_source": "mirror", "download_verify_hash": False}
        item = d.enqueue("ddsp_contentvec", {"compute_root": str(tmp_path), "raw": raw})
        stored = item["paths"]["raw"]
        assert isinstance(stored, str) and json.loads(stored) == raw
        cfg = d._engine_cfg_for(item)
        assert cfg.max_bps == 1024 * 1024
        assert cfg.source_strategy == "mirror_first"
        assert cfg.verify_hash is False
    finally:
        d._stop.set()


def test_daemon_reads_legacy_repr_raw(daemon):
    """S13 之前入队、已落在 state.json 里的 repr 字符串也要能解析。"""
    legacy = repr({"download_max_mbps": 1, "auto_setup": True, "x": None})
    assert daemon._coerce_config_dict(legacy) == {"download_max_mbps": 1, "auto_setup": True, "x": None}


def test_enqueue_serializes_raw_dict(tmp_path):
    """DM.enqueue 写进命令文件的 raw 必须是可解析的 dict（不是被转成字符串的值）。"""
    raw = {"download_max_retries": 4, "download_source": "mirror"}
    paths = {"compute_root": tmp_path, "raw": raw}
    dm.enqueue(paths, ["ddsp_contentvec"], auto_start=False)
    cmd_dir = dm.state_dir(paths) / "cmd"
    files = list(cmd_dir.glob("*.json"))
    assert files
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    sent = payload["items"][0]["paths"]["raw"]
    assert isinstance(sent, dict), f"raw 被序列化成了 {type(sent)}：{sent}"
    assert sent["download_max_retries"] == 4
    assert sent["download_source"] == "mirror"


# ---------------------------------------------------------------------------
# D-C：download_manager 的配置读取在宿主子包环境里也要работать
# ---------------------------------------------------------------------------
def test_manager_load_entries_helper_exists():
    assert hasattr(dm, "_load_entries")
    mod = dm._load_entries()
    assert mod is entries or mod.__name__.endswith("download_entries")


def test_daemon_config_options_reads_values(tmp_path):
    paths = {"compute_root": tmp_path,
             "raw": {"download_max_parallel": 5, "download_idle_sleep_s": 7}}
    opts = dm._daemon_config_options(paths)
    assert opts["max_parallel"] == 5, "download_max_parallel 未生效"
    assert opts["idle_exit_s"] == 7.0, "download_idle_sleep_s 未生效"


def test_daemon_config_options_falls_back(tmp_path):
    opts = dm._daemon_config_options({"compute_root": tmp_path, "raw": {}})
    assert opts["max_parallel"] == dm.DEFAULT_MAX_PARALLEL
    assert opts["idle_exit_s"] == dm.DEFAULT_IDLE_EXIT_S


# ---------------------------------------------------------------------------
# D-D：设置页的 download_source 必须影响引擎排序
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expect", [
    ("auto", "fastest"),
    ("mirror", "mirror_first"),
    ("modelscope", "mirror_first"),
    ("huggingface", "official_first"),
])
def test_download_source_maps_to_strategy(value, expect):
    cfg = entries.engine_config({"raw": {"download_source": value}})
    assert cfg.source_strategy == expect


def test_explicit_strategy_wins_over_download_source():
    cfg = entries.engine_config({"raw": {
        "download_source": "mirror",
        "download_source_strategy": "fastest",
    }})
    assert cfg.source_strategy == "fastest", "显式 strategy 应优先于老键映射"


def test_unknown_download_source_keeps_default():
    cfg = entries.engine_config({"raw": {"download_source": "nonsense"}})
    assert cfg.source_strategy == "fastest"


def test_source_preference_changes_real_ordering():
    """改设置后引擎的排序真的变化（不只是配置字段变了）。"""
    class Row:
        def __init__(self, host, bps):
            self.host, self.bps, self.ok = host, bps, True

    rows = [Row("huggingface.co", 100), Row("hf-mirror.net", 95)]

    def order(raw):
        eng = engine._Engine.__new__(engine._Engine)
        eng.cfg = entries.engine_config({"raw": raw})
        return [r.host for r in sorted(rows, key=eng._order_key)]

    assert order({"download_source": "auto"})[0] == "huggingface.co"
    assert order({"download_source": "mirror"})[0] == "hf-mirror.net"
    assert order({"download_source": "huggingface"})[0] == "huggingface.co"


# ---------------------------------------------------------------------------
# D-I：换源阈值回到计划默认值
# ---------------------------------------------------------------------------
class _Row:
    def __init__(self, url, bps):
        self.url, self.bps, self.ok = url, bps, True


def _switch(current_bps, other_bps, *, slow_windows_hit=0, cfg=None):
    eng = engine._Engine.__new__(engine._Engine)
    eng.switches = 0
    eng.cfg = cfg or engine.EngineConfig()
    eng.last_rank = [_Row("cur", current_bps), _Row("other", other_bps)]
    return eng._should_switch("cur", current_bps, slow_windows_hit)


def test_switch_better_ratio_below_1mbps():
    """计划原文：当前 < 1MB/s 且备选快 3 倍以上就换（不受 200KB/s 门槛限制）。"""
    assert _switch(300 * 1024, 2000 * 1024) is True


def test_switch_not_triggered_above_1mbps():
    """当前已经 > 1MB/s：即使备选更快也不该频繁换（避免抖动）。"""
    assert _switch(2 * 1024 * 1024, 8 * 1024 * 1024) is False


def test_switch_needs_three_times():
    """900KB/s vs 2MB/s（2.2 倍）不满足 3 倍，不换。"""
    assert _switch(900 * 1024, 2000 * 1024) is False


def test_switch_ratio_rule_still_works():
    """沿用规则：低于 slow_bps 且不到最快备选的 35%。"""
    assert _switch(60 * 1024, 500 * 1024) is True


def test_switch_consecutive_slow_windows():
    cfg = engine.EngineConfig()
    assert _switch(150 * 1024, 180 * 1024,
                   slow_windows_hit=cfg.slow_windows) is True
    assert _switch(150 * 1024, 180 * 1024, slow_windows_hit=0) is False


def test_plan_default_thresholds():
    """计划默认值必须原样保留。"""
    cfg = engine.EngineConfig()
    assert cfg.slow_window_s == 15.0
    assert cfg.slow_bps == 200 * 1024
    assert cfg.slow_windows == 2
    assert cfg.better_ratio == 3.0
    assert cfg.better_floor_bps == 1024 * 1024
    assert cfg.stall_s == 20.0
    assert cfg.stall_bytes == 512 * 1024
    assert cfg.max_switches == 8
    assert cfg.overlap_bytes == 64 * 1024


def test_max_switches_cap_respected():
    eng = engine._Engine.__new__(engine._Engine)
    eng.cfg = engine.EngineConfig()
    eng.switches = eng.cfg.max_switches
    eng.last_rank = [_Row("cur", 10 * 1024), _Row("other", 10 * 1024 * 1024)]
    assert eng._should_switch("cur", 10 * 1024, 5) is False, "到上限后必须停"


# ---------------------------------------------------------------------------
# S13 复验新发现：多文件条目的每个文件都要有自己的源
# （sep_karaoke 的 yaml 曾回落到 ckpt 的源，下回 1.7GB 字节、sha 必然失败）
# ---------------------------------------------------------------------------
def _multi_file_keys():
    return [k for k, v in catalog.ARTIFACTS.items() if len(v.get("files") or ()) > 1]


@pytest.mark.parametrize("key", _multi_file_keys())
def test_every_file_has_own_sources(key):
    for name, _sha, _size, urls in catalog.file_entries(catalog.ARTIFACTS[key]):
        assert urls, f"{key}/{name} 没有源"
        assert all(name in u for u in urls), f"{key}/{name} 混入了别的文件的源：{urls}"


def test_sep_karaoke_yaml_sources():
    rows = {r[0]: r for r in catalog.file_entries(catalog.ARTIFACTS["sep_karaoke"])}
    yaml = rows["config_mel_band_roformer_karaoke_becruily.yaml"]
    assert yaml[2] == 1724 and len(yaml[3]) >= 3
    assert any("github.com/nomadkaraoke/python-audio-separator/releases" in u for u in yaml[3])
    ckpt = rows["mel_band_roformer_karaoke_becruily.ckpt"]
    assert not any(".yaml" in u for u in ckpt[3])


def test_file_entries_single_file_falls_back_to_all_sources():
    spec = {"files": (("a.bin", "x", 1),), "sources": ["https://h/other-name"]}
    assert catalog.file_entries(spec)[0][3] == ["https://h/other-name"]


def test_file_entries_multi_file_never_borrows_sources():
    spec = {"files": (("a.bin", "", 1), ("b.yaml", "", 1)), "sources": ["https://h/a.bin"]}
    rows = catalog.file_entries(spec)
    assert rows[0][3] == ["https://h/a.bin"] and rows[1][3] == []


def test_daemon_plan_item_uses_per_file_sources(daemon, tmp_path):
    item = {"key": "sep_karaoke", "paths": {"compute_root": str(tmp_path),
                                            "sep_model_dir": str(tmp_path / "sep")}}
    plan = {r["rel"]: r for r in daemon.plan_item(item, tmp_path / "_downloads")}
    yaml = plan["config_mel_band_roformer_karaoke_becruily.yaml"]
    assert yaml["size"] == 1724 and yaml["urls"]
    assert all(u.endswith(".yaml") for u in yaml["urls"])
