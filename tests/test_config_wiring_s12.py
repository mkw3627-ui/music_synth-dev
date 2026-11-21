"""S12a 修复回归：plugin.toml 的 ``download_*`` 键真正驱动引擎与守护进程。

S11 只把 10 个键写进 plugin.toml（默认值 + 注释），没有运行时读取；
``EngineConfig`` 全靠代码默认值。这里验证接线后用测试锁定。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import download_engine as E
import download_entries as DE

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

#: plugin.toml 里 S11 新增的 10 个键
TOML_KEYS = (
    "download_max_mbps", "download_mirror_preference", "download_check_ssl",
    "download_max_retries", "download_verify_hash", "download_max_parallel",
    "download_resume", "download_idle_sleep_s", "download_cache_days",
    "download_source_strategy",
)


def _paths(**over) -> dict:
    raw = dict(over)
    return {"compute_root": "D:/x", "raw": raw}


def test_all_toml_keys_exist_and_have_defaults():
    """10 个键都在 plugin.toml 里，且有合法默认值。"""
    text = (PLUGIN_ROOT / "plugin.toml").read_text(encoding="utf-8")
    for key in TOML_KEYS:
        assert f"\n{key} = " in text, f"plugin.toml 缺 {key}"


def test_empty_config_keeps_engine_defaults():
    cfg = DE.engine_config({})
    default = E.EngineConfig()
    assert cfg.max_switches == default.max_switches
    assert cfg.resume_enabled is default.resume_enabled
    assert cfg.verify_hash is default.verify_hash
    assert cfg.max_bps == 0
    assert cfg.source_strategy == "fastest"


@pytest.mark.parametrize("value,expect", [
    (1, 1), (4, 4), (8, 8), (16, 16),
])
def test_max_retries_maps_to_max_switches(value, expect):
    assert DE.engine_config(_paths(download_max_retries=value)).max_switches == expect


@pytest.mark.parametrize("value,expect", [
    (0, 0), (1, 1024 * 1024), (2.5, int(2.5 * 1024 * 1024)),
])
def test_max_mbps_maps_to_max_bps(value, expect):
    assert DE.engine_config(_paths(download_max_mbps=value)).max_bps == expect


@pytest.mark.parametrize("value,expect", [
    (True, True), (False, False), ("true", True), ("false", False),
    ("off", False), ("no", False), (0, False),
])
def test_boolean_flags(value, expect):
    cfg = DE.engine_config(_paths(download_verify_hash=value,
                                  download_resume=value,
                                  download_check_ssl=value))
    assert cfg.verify_hash is expect
    assert cfg.resume_enabled is expect
    assert cfg.check_ssl is expect


@pytest.mark.parametrize("value", ["fastest", "mirror_first", "official_first"])
def test_source_strategy_values(value):
    assert DE.engine_config(_paths(download_source_strategy=value)).source_strategy == value


def test_invalid_source_strategy_falls_back():
    cfg = DE.engine_config(_paths(download_source_strategy="nonsense"))
    assert cfg.source_strategy == "fastest"


@pytest.mark.parametrize("value", ["auto", "mirror", "huggingface"])
def test_mirror_preference_values(value):
    assert DE.engine_config(_paths(download_mirror_preference=value)).mirror_preference == value


def test_cache_days_maps_to_probe_ttl():
    cfg = DE.engine_config(_paths(download_cache_days=2))
    assert cfg.probe_ttl_s == 2 * 86400.0


def test_daemon_options():
    opts = DE.daemon_options(_paths(download_max_parallel=5,
                                    download_idle_sleep_s=15))
    assert opts == {"max_parallel": 5, "idle_exit_s": 15.0}


def test_daemon_options_defaults():
    opts = DE.daemon_options({})
    assert opts["max_parallel"] >= 1
    assert opts["idle_exit_s"] > 0


def test_engine_config_accepts_base():
    base = E.EngineConfig(net_wait_budget_s=0.0, chunk_size=12345)
    cfg = DE.engine_config(_paths(download_max_retries=2), base=base)
    assert cfg is base, "应就地覆盖传入的 base"
    assert cfg.chunk_size == 12345, "未涉及的字段不该被改"
    assert cfg.net_wait_budget_s == 0.0
    assert cfg.max_switches == 2


# ---------------------------------------------------------------------------
# 引擎侧真正生效
# ---------------------------------------------------------------------------
class _Row:
    def __init__(self, host, bps, ok=True):
        self.host = host
        self.bps = bps
        self.ok = ok


def _order(strategy, rows):
    eng = E._Engine.__new__(E._Engine)
    eng.cfg = E.EngineConfig(source_strategy=strategy)
    return [r.host for r in sorted(rows, key=eng._order_key)]


def test_strategy_fastest_is_pure_speed():
    rows = [_Row("huggingface.co", 100), _Row("hf-mirror.net", 95)]
    assert _order("fastest", rows)[0] == "huggingface.co"


def test_strategy_mirror_first_when_close():
    rows = [_Row("huggingface.co", 100), _Row("hf-mirror.net", 95)]
    assert _order("mirror_first", rows)[0] == "hf-mirror.net"


def test_strategy_mirror_first_still_speed_when_far():
    """差距超过 30% 时仍按速度——不能为了「国内优先」把慢源排前面。"""
    rows = [_Row("huggingface.co", 100), _Row("hf-mirror.net", 40)]
    assert _order("mirror_first", rows)[0] == "huggingface.co"


def test_strategy_official_first():
    rows = [_Row("huggingface.co", 95), _Row("hf-mirror.net", 100)]
    assert _order("official_first", rows)[0] == "huggingface.co"


def test_failed_sources_always_last():
    rows = [_Row("huggingface.co", 1e9, ok=False), _Row("hf-mirror.net", 1)]
    for strat in ("fastest", "mirror_first", "official_first"):
        assert _order(strat, rows)[-1] == "huggingface.co"


def test_verify_hash_false_skips_checksum(tmp_path, mirrors):
    """download_verify_hash=false：sha 不对也不报错（只校验长度）。"""
    from dl_harness.fake_mirrors import make_content
    content = make_content(128 * 1024, seed=91)
    cluster = mirrors(1, content=content)
    dest = tmp_path / "noverify.bin"
    res = E.download(dest, cluster.urls(), size=content.size,
                     sha256="00" * 32,           # 故意错的 sha
                     cfg=E.EngineConfig(verify_hash=False, probe_ttl_s=0,
                                        chunk_size=64 * 1024, timeout=10,
                                        net_wait_budget_s=0))
    assert res.ok, "关掉校验后不该因 sha 不符失败"
    assert dest.is_file()


def test_verify_hash_true_rejects_checksum(tmp_path, mirrors):
    from dl_harness.fake_mirrors import make_content
    content = make_content(128 * 1024, seed=92)
    cluster = mirrors(1, content=content)
    dest = tmp_path / "verify.bin"
    with pytest.raises(E.ChecksumError):
        E.download(dest, cluster.urls(), size=content.size, sha256="00" * 32,
                   cfg=E.EngineConfig(verify_hash=True, checksum_retries=0,
                                      probe_ttl_s=0, chunk_size=64 * 1024,
                                      timeout=10, net_wait_budget_s=0))


def test_resume_disabled_discards_part(tmp_path, mirrors):
    """download_resume=false：已有 .part 也要从 0 重下。"""
    from dl_harness.fake_mirrors import make_content
    content = make_content(256 * 1024, seed=93)
    cluster = mirrors(1, content=content)
    dest = tmp_path / "noresume.bin"
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(b"\x00" * 100 * 1024)      # 垃圾内容
    res = E.download(dest, cluster.urls(), size=content.size,
                     sha256=content.sha256(),
                     cfg=E.EngineConfig(resume_enabled=False, probe_ttl_s=0,
                                        chunk_size=64 * 1024, timeout=10,
                                        net_wait_budget_s=0))
    assert res.ok
    import hashlib
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == content.sha256()


def test_max_bps_throttles(tmp_path, mirrors):
    """download_max_mbps：限速后耗时应明显变长。"""
    import time

    from dl_harness.fake_mirrors import make_content
    content = make_content(512 * 1024, seed=94)
    cluster = mirrors(1, content=content)
    dest = tmp_path / "throttled.bin"
    cfg = E.EngineConfig(max_bps=256 * 1024, probe_ttl_s=0,
                         chunk_size=64 * 1024, timeout=20, net_wait_budget_s=0)
    t0 = time.time()
    E.download(dest, cluster.urls(), size=content.size, sha256=content.sha256(), cfg=cfg)
    elapsed = time.time() - t0
    # 512KB @ 256KB/s ≈ 2s（无限速时是毫秒级）
    assert elapsed >= 1.5, f"限速没生效，只用了 {elapsed:.2f}s"


def test_check_ssl_flag_accepted():
    """download_check_ssl=false 时构造 HTTPS handler（不崩即可）。"""
    assert E._default_opener(True) is not None
    opener = E._default_opener(False)
    assert opener is not None
    assert any(isinstance(h, __import__("urllib.request", fromlist=["x"]).HTTPSHandler)
               for h in opener.handlers)
