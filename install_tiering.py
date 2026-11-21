"""分级安装后端（S5）：T0/T1 前台装、T2 交给后台守护进程。

设计原则
--------
* **不 import 宿主 SDK**，也不 import ``setup.py`` / ``__init__.py``：插件侧是
  ``from . import setup``，独立运行时是 ``import setup``，两种方式都不能硬编码。
  所以本模块接受一个 **installer 对象**（见下），由调用方注入真实的 ``setup`` 模块。
* 分级集合全部来自 ``download_catalog``（S1 的唯一事实来源），不在这里重复定义 key。
* T2 一律**不在这里下载**，返回 ``deferred`` 交给 S4 的守护进程（计划 B.1/B.2）。

``installer`` 需要提供四个函数（``setup`` 模块满足，测试可注入替身）::

    check_dependencies(paths, probe=None) -> {"items": [...], "ready": bool, ...}
    install(paths, items, progress, probe=None) -> {"installed": [...], "errors": [...], ...}
    download_portable_python(paths, progress) -> str      # 便携 Python 路径
    _item_map(report) -> {key: item}                       # 可选，内部会自己算

对外接口
--------
``TIER_ITEMS`` / ``feature_plan`` / ``tier_items`` / ``check_tiered`` /
``install_tiered`` / ``deferred_items`` / ``setup_state``

调用示例（S6 插件入口）::

    from . import setup as S
    import install_tiering as T

    rep = T.check_tiered(S, paths)                       # 报告里带 features 段
    res = T.install_tiered(S, paths, features=["cover"], tiers=["T0", "T1"],
                           progress=cb)
    # res["deferred"] -> ["ddsp_contentvec", ...] 交给 DM.enqueue
"""
from __future__ import annotations

from pathlib import Path

TIERS = ("T0", "T1", "T2")

#: 只有 T2 是"后台下载"，T0/T1 必须前台装完才能进主页（计划 B.1）。
FOREGROUND_TIERS = ("T0", "T1")

#: setup.check_dependencies 里**不在** catalog 的条目（宿主自检项），不参与分级。
#: 它们不是"下载物"，只是环境探测结果。
NON_ARTIFACT_ITEMS = frozenset({
    "install_path", "python_host", "cuda_driver", "disk", "extra_check_error",
})


# ---------------------------------------------------------------------------
# catalog 访问（延迟导入，保持本模块可单测）
# ---------------------------------------------------------------------------
def _cat():
    try:
        from . import download_catalog  # noqa: PLC0415 - 包内
    except ImportError:
        import download_catalog  # noqa: PLC0415 - 顶层（测试/独立运行）
    return download_catalog


# ---------------------------------------------------------------------------
# 集合
# ---------------------------------------------------------------------------
def tier_items(features=None, tiers=None, *, allow_optional: bool = False) -> list[str]:
    """按功能与层级列出 catalog key（已含前置功能展开）。"""
    return _cat().artifacts_for(features, list(tiers or TIERS),
                                allow_optional=allow_optional)


def feature_plan(feature: str, *, include_optional: bool = False) -> dict:
    """单个功能的分层计划：``{"T0": [...], "T1": [...], "T2": [...], "sizes": {...}}``。"""
    cat = _cat()
    items = cat.feature_items(feature, allow_optional=include_optional)
    return {"T0": items["T0"], "T1": items["T1"], "T2": items["T2"],
            "sizes": cat.feature_sizes(feature, allow_optional=include_optional)}


def deferred_items(features=None, tiers=None) -> list[str]:
    """本次不下载、要交给守护进程的 T2 项。

    只有 ``tiers`` 显式含 T2 时才有意义；否则按计划默认把 T2 全部延后。
    """
    return tier_items(features, ["T2"])


def foreground_items(features=None) -> list[str]:
    """前台要装的 T0 + T1。"""
    return tier_items(features, list(FOREGROUND_TIERS))


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
def setup_state(paths) -> dict:
    """读 ``setup_state.json``（与既有安装器的状态文件同一位置）。

    只读，不存在返回 ``{}``。
    """
    import json  # noqa: PLC0415

    try:
        data_dir = Path(str((paths or {}).get("data_dir") or ""))
    except (AttributeError, TypeError):
        return {}
    if not data_dir.is_dir():
        return {}
    for name in ("setup_state.json", "setup_state.json"):
        path = data_dir / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def _write_setup_state(paths, payload: dict) -> None:
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    data_dir = Path(str((paths or {}).get("data_dir") or ""))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / "setup_state.json"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 报告：给每个 item 补 feature / tier，并汇总 features 段
# ---------------------------------------------------------------------------
def _tier_of(key: str) -> str:
    """catalog 里查这一项属于哪一层；查不到按 T0（自检类）。"""
    spec = _cat().ARTIFACTS.get(key)
    return str(spec.get("tier") or "T0") if spec else "T0"


def _features_of(key: str) -> tuple[str, ...]:
    spec = _cat().ARTIFACTS.get(key)
    if not spec:
        return ()
    return tuple(spec.get("features") or (spec.get("feature"),))


def annotate_report(report: dict, features=None) -> dict:
    """给 ``check_dependencies`` 的报告补 ``feature`` / ``tier`` 字段与 ``features`` 段。

    幂等：重复调用不会改变已有字段。
    """
    cat = _cat()
    items = report.get("items") or []
    by_key = {}
    for item in items:
        key = item.get("key")
        if not key:
            continue
        spec = cat.ARTIFACTS.get(key)
        item.setdefault("tier", _tier_of(key))
        if spec:
            item.setdefault("feature", spec.get("feature") or "")
            item.setdefault("features", list(spec.get("features") or ()))
        else:
            item.setdefault("feature", "")
            item.setdefault("features", [])
        by_key[key] = item

    cat_state = setup_state(_paths_of(report))
    wanted = list(features) if features else list(cat.FEATURES)
    feat_report: dict = {}
    for feature in wanted:
        if feature not in cat.FEATURES:
            continue
        feat_report[feature] = _feature_summary(feature, by_key, cat_state)
    report["features"] = feat_report
    report["tiers"] = _tier_summary(items, features)
    return report


def _paths_of(report: dict) -> dict:
    # check_dependencies 的报告里没有 paths；setup_state 需要 data_dir，
    # 由调用方用 annotate_report_with_paths 补。这里返回空，setup_state 会安全返回 {}。
    return report.get("_paths") or {}


def annotate_report_with_paths(report: dict, paths, features=None) -> dict:
    """同 ``annotate_report``，但带上 paths 以读取 setup_state.json。"""
    report = dict(report)
    report["_paths"] = dict(paths or {})
    out = annotate_report(report, features)
    out.pop("_paths", None)
    return out


def _feature_summary(feature: str, by_key: dict, cat_state: dict) -> dict:
    cat = _cat()
    items = cat.feature_items(feature)
    required_keys = [k for tier in TIERS for k in items[tier]
                     if cat.ARTIFACTS.get(k, {}).get("required", True)]
    missing = []
    for key in required_keys:
        item = by_key.get(key)
        if item is None:
            missing.append(key)          # 报告里没有 → 当作缺
        elif item.get("status") not in ("ready", "optional", "na"):
            missing.append(key)

    def tier_ready(tier: str) -> bool:
        keys = [k for k in items[tier]
                if cat.ARTIFACTS.get(k, {}).get("required", True)]
        for key in keys:
            item = by_key.get(key)
            if item is None or item.get("status") not in ("ready", "optional", "na"):
                return False
        return True

    sizes = cat.feature_sizes(feature)
    meta = cat.FEATURES.get(feature) or {}
    return {
        "label": meta.get("label") or feature,
        "ready": not missing,
        "t0_ready": tier_ready("T0"),
        "t1_ready": tier_ready("T1"),
        "t2_ready": tier_ready("T2"),
        "missing": missing,
        "required_items": required_keys,
        # fg = 前台（T0+T1）；bg = 后台（T2）
        "fg_gb": round((sizes["T0"] + sizes["T1"]) / 2 ** 30, 2),
        "bg_gb": round(sizes["T2"] / 2 ** 30, 2),
        "total_gb": round(sizes["total"] / 2 ** 30, 2),
        "gpu_ok": not meta.get("gpu_required", False),
        "depends": list(meta.get("depends") or ()),
        "pages": list(meta.get("pages") or ()),
        "base_completed": bool(cat_state.get(f"base_completed_{feature}")
                               or cat_state.get("base_completed")),
    }


def _tier_summary(items: list[dict], features=None) -> dict:
    """按层级汇总。``features`` 给定时只看这些功能覆盖的 item。

    只有 ``required`` 项参与 ``ready`` 判定；可选兜底（``base_python_pinned``、
    ``ffmpeg_zip``、低/高质量底模）缺了不算未装。
    """
    scope: set[str] | None = set(catalog_keys_for(features)) if features else None
    out: dict = {}
    for tier in TIERS:
        in_scope = [i for i in items
                    if i.get("tier") == tier and (scope is None or i["key"] in scope)]
        keys = [i["key"] for i in in_scope]
        required = [i for i in in_scope
                    if _cat().ARTIFACTS.get(i["key"], {}).get("required", True)]
        missing = [i["key"] for i in required
                   if i.get("status") not in ("ready", "optional", "na")]
        out[tier] = {"items": keys, "missing": missing, "ready": not missing}
    return out


def catalog_keys_for(features) -> list[str]:
    """给定功能集合覆盖的全部 catalog key（含依赖展开，不分层）。"""
    return _cat().artifacts_for(features)


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------
def check_tiered(installer, paths, probe=None, features=None) -> dict:
    """``check_dependencies`` + 分级标注。"""
    report = installer.check_dependencies(paths, probe)
    return annotate_report_with_paths(report, paths, features)


def tier_status(installer, paths, features=None, probe=None) -> dict:
    """``{tier: {items, missing, ready}}`` + ``{feature: {...}}`` + ``base_completed``。"""
    report = check_tiered(installer, paths, probe, features)
    base_completed = bool(
        report.get("tiers", {}).get("T0", {}).get("ready")
        and report.get("tiers", {}).get("T1", {}).get("ready"))
    return {
        "tiers": report.get("tiers") or {},
        "features": report.get("features") or {},
        "base_completed": base_completed,
        "ready": bool(report.get("ready")),
    }


# ---------------------------------------------------------------------------
# 安装
# ---------------------------------------------------------------------------
def install_tiered(installer, paths, *, features=None, tiers=None, progress=None,
                   probe=None, allow_optional: bool = False,
                   include_t2: bool = False) -> dict:
    """按功能与层级安装。

    * ``tiers`` 缺省 = ``("T0", "T1")``（计划 B.1：T2 后台下）。
    * T2 项**默认不在这里下**，放进 ``deferred``。
    * ``include_t2=True`` 且 ``tiers`` 含 T2 时才真的交给 installer 安装（legacy 回退）。
    """
    requested_tiers = tuple(tiers or FOREGROUND_TIERS)
    # 「前台」永远是 T0+T1（计划 B.1）：即使调用方把 T2 放进 tiers，
    # T2 也默认不在这里下，除非显式 include_t2=True。
    foreground_tiers = [t for t in requested_tiers if t in FOREGROUND_TIERS]
    if not foreground_tiers:
        foreground_tiers = list(FOREGROUND_TIERS)
    wanted_keys = tier_items(features, foreground_tiers, allow_optional=allow_optional)

    # installer.install 只认它自己认识的 key（pip/venv/模型/ACE 等）
    report_before = installer.check_dependencies(paths, probe)
    known = {i.get("key") for i in (report_before.get("items") or []) if i.get("key")}
    foreground = [k for k in wanted_keys if k in known]

    # T2：默认延后
    t2_all = tier_items(features, ["T2"], allow_optional=allow_optional)
    deferred = [k for k in t2_all if k not in foreground]
    to_install = list(foreground)
    if include_t2:
        in_t2 = [k for k in t2_all if k in known]
        to_install = list(dict.fromkeys(foreground + in_t2))
        deferred = [k for k in t2_all if k not in to_install]

    cb = progress or (lambda *_: None)
    result = installer.install(paths, to_install or None if include_t2 else to_install,
                               cb, probe)

    report_after = annotate_report_with_paths(
        installer.check_dependencies(paths, probe), paths, features)

    # 记录"基础环境已完成"，供 App 决定是否还要强制开向导（计划 S8）
    state = setup_state(paths)
    tiers_after = report_after.get("tiers") or {}
    base_done = bool(tiers_after.get("T0", {}).get("ready")
                     and tiers_after.get("T1", {}).get("ready"))
    if base_done:
        state["base_completed"] = True
        state["completed"] = bool(report_after.get("ready"))
        state["features"] = [f for f, v in (report_after.get("features") or {}).items()
                             if v.get("ready")]
        state["updated_at"] = __import__("time").time()
        _write_setup_state(paths, state)

    out = dict(result or {})
    out["deferred"] = deferred
    out["requested_tiers"] = list(requested_tiers)
    out["requested_features"] = list(features) if features else None
    out["tiers"] = tiers_after
    out["features"] = report_after.get("features") or {}
    out["base_completed"] = base_done
    out["report"] = report_after
    # 兼容旧调用：ok 仍表示"前台部分是否装好"
    out.setdefault("ok", bool(result.get("ok")) if isinstance(result, dict) else False)
    return out


# ---------------------------------------------------------------------------
# 回归用的调用序列录制
# ---------------------------------------------------------------------------
class RecordingInstaller:
    """包一层 installer，只记录 ``install()`` 的调用参数（回归基线用）。

    ``inner`` 可以是任何满足 installer 协议的对象；测试里用假的即可。
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[dict] = []

    def check_dependencies(self, paths, probe=None):
        return self.inner.check_dependencies(paths, probe)

    def install(self, paths, items, progress, probe=None):
        self.calls.append({"items": list(items) if items is not None else None})
        return self.inner.install(paths, items, progress, probe)

    def download_portable_python(self, paths, progress):
        return self.inner.download_portable_python(paths, progress)
