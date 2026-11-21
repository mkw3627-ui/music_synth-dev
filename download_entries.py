"""S6 插件入口层：分级下载 / 安装的用户可见入口逻辑。

纯标准库、无相对导入、不 import 宿主 SDK。插件 ``__init__.py`` 里的 ``@plugin_entry``
方法只是薄委托，真正的逻辑在这里，便于 pytest 用 DM / S5 替身直接测（见
``tests/test_entries_dl.py``）。

依赖注入与职责边界：

* **下载** 一律走 ``download_manager``（S4 门面），不直接碰 ``download_engine``。
* **分级安装** 一律走 ``install_tiering``（S5 后端），安装器对象（``setup`` 模块）由调用方注入。
* 本模块只做：入队集合解析、状态合并（S5 文件/环境就绪 + S4 下载进度）、
  T0/T1 装完后的 T2 自动入队、以及 startup/shutdown 需要的配置开关读取。

跨上下文导入：插件里本模块是 ``plugin.plugins.music_synth.download_entries``（相对导入），
独立运行 / pytest 时是顶层模块（绝对导入）。对兄弟模块统一用「先相对、失败回退绝对」。
"""
from __future__ import annotations

try:  # 插件包内
    from . import download_catalog as catalog
    from . import download_engine as engine
    from . import download_manager as DM
    from . import install_tiering as tiering
except ImportError:  # 独立运行 / pytest
    import download_catalog as catalog  # type: ignore[no-redef]
    import download_engine as engine  # type: ignore[no-redef]
    import download_manager as DM  # type: ignore[no-redef]
    import install_tiering as tiering  # type: ignore[no-redef]

__all__ = [
    "dl_status", "dl_enqueue", "dl_control", "dl_speedtest", "feature_status",
    "run_install", "maybe_enqueue_deferred", "resume_on_startup", "stop_on_shutdown",
    "engine_mode", "auto_resume", "keep_after_exit", "VALID_ACTIONS",
    "engine_config", "daemon_options",
]

VALID_ACTIONS = ("pause", "resume", "cancel", "retry", "prioritize")
#: 守护进程里「还在忙」的状态（用于功能就绪合并时区分 running / missing）。
_BUSY_STATES = ("queued", "probing", "downloading", "verifying", "post",
                "pending_post", "paused", "paused_disk_full")
_FAILED_STATES = ("failed",)


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(x) for x in value if x is not None and str(x) != ""]


def _raw(paths) -> dict:
    raw = (paths or {}).get("raw")
    return raw if isinstance(raw, dict) else {}


def _flag(paths, key: str, default: bool = True) -> bool:
    raw = _raw(paths)
    val = str(raw.get(key) if raw.get(key) is not None else "").strip().lower()
    if not val:
        return default
    return val not in ("0", "false", "off", "no", "关", "否")


def engine_mode(paths) -> str:
    """下载引擎模式：``daemon``（后台守护进程）或 ``legacy``（一次性安装内联下载）。"""
    return str(_raw(paths).get("download_engine") or "daemon")


def auto_resume(paths) -> bool:
    """``download_auto_resume``：启动时是否自动续下未完成任务。缺省 true。"""
    return _flag(paths, "download_auto_resume", True)


def keep_after_exit(paths) -> bool:
    """``download_keep_after_exit``：关 N.E.K.O 后是否继续下载。缺省 true。"""
    return _flag(paths, "download_keep_after_exit", True)


# ---------------------------------------------------------------------------
# 状态 / 入队 / 控制
# ---------------------------------------------------------------------------
def dl_status(paths) -> dict:
    """只读 ``state.json``，不拉起守护进程。"""
    return DM.read_state(paths)


def dl_enqueue(paths, features=None, artifacts=None, *, tiers=None,
               priority: int = 0) -> dict:
    """把所选功能 / 下载物的 T2 模型入队到守护进程。

    * ``artifacts`` 给定时直接用（不受 tier 过滤）。
    * 否则按 ``features``（缺省=全部功能）与 ``tiers``（缺省=``["T2"]``）解析。
    """
    artifacts = _as_list(artifacts)
    if artifacts:
        keys = artifacts
    else:
        features = _as_list(features)
        tiers = _as_list(tiers) or ["T2"]
        keys = catalog.artifacts_for(features or None, tiers)
    return DM.enqueue(paths, keys, priority=priority)


def dl_control(paths, action: str, target: str = "all") -> dict:
    """``pause`` / ``resume`` / ``cancel`` / ``retry`` / ``prioritize``。"""
    action = str(action or "").strip()
    if action not in VALID_ACTIONS:
        return {"ok": False,
                "error": f"未知控制动作：{action or '（空）'}（可选 {', '.join(VALID_ACTIONS)}）"}
    return DM.control(paths, action, str(target or "all"))


# ---------------------------------------------------------------------------
# 测速（按下载物逐源）
# ---------------------------------------------------------------------------
def _probe_to_dict(row) -> dict:
    return {
        "url": row.url,
        "ok": bool(row.ok),
        "status": int(row.status or 0),
        "bps": float(row.bps or 0.0),
        "total": int(row.total or 0),
        "content_type": str(row.content_type or ""),
        "range_ok": bool(row.range_ok),
        "error": str(row.error or ""),
        "seconds": float(row.seconds or 0.0),
        "host": getattr(row, "host", ""),
    }


def dl_speedtest(paths, artifact=None, *, probe=None) -> dict:
    """按下载物逐源测速。``artifact`` 给定时只测那一个；否则测所有有源的下载物。

    ``probe`` 可注入（测试用），缺省用 ``download_engine.probe_sources``。

    D1 修复：所有下载物的所有源**合并成一批**并行测速（全局并发受
    ``probe_workers`` 上限约束），并把测速参数调小（256KB / 2s 预算），
    避免逐个下载物串行测速累加超过宿主的 30 秒执行上限。
    """
    probe = probe or engine.probe_sources
    if artifact:
        keys = [str(artifact)]
    else:
        keys = [k for k, spec in catalog.ARTIFACTS.items()
                if spec.get("sources") and spec.get("kind") != "repo"]

    # 拍平 (artifact, url) 对，保留归属关系
    pairs: list[tuple[str, str]] = []
    for key in keys:
        for url in catalog.sources_for(key):
            pairs.append((key, url))

    cfg = engine.EngineConfig(probe_bytes=256 * 1024, probe_budget_s=2.0)
    rows = probe([url for _, url in pairs], cfg=cfg)
    by_url = {r.url: r for r in rows}

    items: dict = {}
    chosen: dict = {}
    for key, url in pairs:
        row = by_url.get(url)
        if row is None:
            continue
        items.setdefault(key, []).append(_probe_to_dict(row))
    for key in items:
        ok_rows = [r for r in items[key] if r.get("ok")]
        if ok_rows:
            chosen[key] = sorted(ok_rows, key=lambda r: -r.get("bps", 0))[0]
        else:
            chosen[key] = None
    return {"ok": True, "items": items, "chosen": chosen}


# ---------------------------------------------------------------------------
# 功能就绪状态（合并 S5 文件/环境就绪 + S4 下载进度）
# ---------------------------------------------------------------------------
def feature_status(paths, *, tier_status=None, state=None) -> dict:
    """每个功能的就绪状态。

    * ``tier_status``：``install_tiering.tier_status(...)`` 的结果（基于磁盘/环境检查）。
      缺省不调用（保持只读），只用守护进程状态补 T2 的 running/failed。
    * ``state``：``DM.read_state(paths)``；缺省自动读。

    返回 ``{ok, features, tiers, base_completed, daemon, totals}``。
    """
    state = state if state is not None else DM.read_state(paths)
    state = state if isinstance(state, dict) else {}
    ts = tier_status if isinstance(tier_status, dict) else {}
    ts_features = ts.get("features") or {}
    daemon_items = state.get("items") or {}

    features: dict = {}
    for feature, meta in catalog.FEATURES.items():
        base = dict(ts_features.get(feature) or {})
        t2_keys = set(catalog.feature_items(feature).get("T2") or [])
        running: list[str] = []
        failed: list[str] = []
        downloads: dict = {}
        for key, item in daemon_items.items():
            item_feature = item.get("feature") if isinstance(item, dict) else None
            belongs = item_feature == feature or (item_feature is None and key in t2_keys)
            if not belongs:
                continue
            st = (item.get("state") if isinstance(item, dict) else None)
            if st in _BUSY_STATES:
                running.append(key)
            elif st in _FAILED_STATES:
                failed.append(key)
            if isinstance(item, dict):
                downloads[key] = item
        base.setdefault("label", meta.get("label") or feature)
        base.setdefault("depends", list(meta.get("depends") or ()))
        base.setdefault("pages", list(meta.get("pages") or ()))
        base.setdefault("gpu_required", bool(meta.get("gpu_required", False)))
        base["running"] = running
        base["failed"] = failed
        base["downloads"] = downloads
        features[feature] = base

    return {
        "ok": True,
        "features": features,
        "tiers": ts.get("tiers") or {},
        "base_completed": bool(ts.get("base_completed")),
        "daemon": state.get("daemon") or {},
        "totals": state.get("totals") or {},
    }


# ---------------------------------------------------------------------------
# 分级安装路由 + T2 自动入队
# ---------------------------------------------------------------------------
def run_install(installer, paths, *, features=None, tiers=None, items=None,
                progress=None, probe=None) -> dict:
    """S6 安装路由。

    * ``features`` / ``tiers`` 给定时 → ``install_tiering.install_tiered``（T0/T1 前台、
      T2 放进 ``deferred``）。
    * 否则 → ``installer.install(paths, items, progress, probe)``（旧行为：``items=None``
      装全部缺失项，含 T2 模型；``items`` 给定时只装这些）。
    """
    features = _as_list(features) or None
    tiers = _as_list(tiers) or None
    if features or tiers:
        return tiering.install_tiered(
            installer, paths, features=features, tiers=tiers,
            progress=progress, probe=probe)
    if items is not None and not isinstance(items, (list, tuple)):
        items = [str(items)]
    return installer.install(paths, items, progress, probe)


def maybe_enqueue_deferred(paths, deferred) -> dict | None:
    """装完 T0/T1 后把 T2 交给守护进程（``download_engine != "legacy"`` 时）。"""
    keys = _as_list(deferred)
    if not keys:
        return None
    if engine_mode(paths) == "legacy":
        return {"ok": True, "count": 0, "skipped": "legacy"}
    return DM.enqueue(paths, keys)


# ---------------------------------------------------------------------------
# startup / shutdown
# ---------------------------------------------------------------------------
def resume_on_startup(paths) -> dict:
    """启动时：``download_auto_resume`` 为真且 state 里有未完成任务 → 拉起守护进程。"""
    if not auto_resume(paths):
        return {"started": False, "pid": 0, "reason": "auto_resume=off"}
    state = DM.read_state(paths)
    items = state.get("items") or {}
    if not items:
        return {"started": False, "pid": 0, "reason": "no-items"}
    return DM.ensure_daemon(paths)


def stop_on_shutdown(paths) -> dict:
    """关闭时：``download_keep_after_exit`` 为假 → 让守护进程收尾退出。"""
    if keep_after_exit(paths):
        return {"stopped": False, "pid": DM.daemon_pid(paths), "reason": "keep-after-exit"}
    return DM.stop_daemon(paths)


# ---------------------------------------------------------------------------
# 配置 → 引擎参数（S12a：plugin.toml 的 download_* 键真正生效）
# ---------------------------------------------------------------------------
#: 老键 ``download_source`` 的值 → 引擎排序策略（S13 修复：设置页下拉框原本无效）。
SOURCE_TO_STRATEGY = {
    "auto": "fastest",
    "mirror": "mirror_first",
    "modelscope": "mirror_first",
    "huggingface": "official_first",
    "official": "official_first",
}

#: plugin.toml 键 → EngineConfig 字段。缺省值即 EngineConfig 自己的默认值；
#: 只有用户显式改过的键才会覆盖。
_ENGINE_FIELD_MAP = {
    "download_max_retries": "max_switches",
    "download_max_parallel": "max_parallel",     # 只给守护进程用，不进 EngineConfig
    "download_resume": "resume_enabled",
    "download_idle_sleep_s": "idle_exit_s",      # 只给守护进程用
    "download_cache_days": "probe_ttl_days",
    "download_source_strategy": "source_strategy",
    "download_mirror_preference": "mirror_preference",
    "download_verify_hash": "verify_hash",
    "download_max_mbps": "max_mbps",
    "download_check_ssl": "check_ssl",
}


def _int_or(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_or(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def engine_config(paths, *, base=None):
    """按 plugin.toml 的 ``download_*`` 键构造 ``EngineConfig``。

    ``base`` 给定时在它上面覆盖（守护进程用自己的一份做底）。键不存在或值非法时
    保持 ``EngineConfig`` 的默认值——所以老配置不需要迁移。
    """
    try:
        from . import download_engine as engine  # noqa: PLC0415 - 包内
    except ImportError:
        import download_engine as engine  # type: ignore[no-redef]  # noqa: PLC0415

    cfg = base if base is not None else engine.EngineConfig()
    raw = _raw(paths)
    if not raw:
        return cfg

    def has(key: str) -> bool:
        return raw.get(key) is not None and str(raw.get(key)).strip() != ""

    if has("download_max_retries"):
        cfg.max_switches = max(1, _int_or(raw["download_max_retries"], cfg.max_switches))
    if has("download_cache_days"):
        days = _float_or(raw["download_cache_days"], 7.0)
        if days >= 0:
            cfg.probe_ttl_s = days * 86400.0
    if has("download_source_strategy"):
        val = str(raw["download_source_strategy"]).strip().lower()
        if val in ("fastest", "mirror_first", "official_first"):
            cfg.source_strategy = val
    elif has("download_source"):
        # 设置页的「下载源偏好」存的是老键 ``download_source``（历史值：auto/mirror/
        # huggingface）。没有显式 strategy 时按它映射，否则那个下拉框没有任何效果。
        mapped = SOURCE_TO_STRATEGY.get(str(raw["download_source"]).strip().lower())
        if mapped:
            cfg.source_strategy = mapped
    if has("download_mirror_preference"):
        val = str(raw["download_mirror_preference"]).strip().lower()
        if val in ("auto", "mirror", "huggingface"):
            cfg.mirror_preference = val
    if has("download_verify_hash"):
        cfg.verify_hash = _flag(paths, "download_verify_hash", True)
    if has("download_check_ssl"):
        cfg.check_ssl = _flag(paths, "download_check_ssl", True)
    if has("download_resume"):
        cfg.resume_enabled = _flag(paths, "download_resume", True)
    if has("download_max_mbps"):
        mbps = _float_or(raw["download_max_mbps"], 0.0)
        cfg.max_bps = int(mbps * 1024 * 1024) if mbps > 0 else 0
    return cfg


def daemon_options(paths) -> dict:
    """守护进程启动参数（``download_max_parallel`` / ``download_idle_sleep_s``）。"""
    raw = _raw(paths)
    return {
        "max_parallel": max(1, _int_or(raw.get("download_max_parallel"),
                                       DM.DEFAULT_MAX_PARALLEL)),
        "idle_exit_s": max(0.0, _float_or(raw.get("download_idle_sleep_s"),
                                          DM.DEFAULT_IDLE_EXIT_S)),
    }


