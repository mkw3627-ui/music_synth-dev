"""0.3.10 回归：市场包的排除规则必须写在 ``pyproject.toml [tool.neko.build]``。

背景（0.3.9 实测）：市场包由 CI 的 ``neko_plugin_cli`` 打包，它只读插件目录下
``pyproject.toml`` 的 ``[tool.neko.build]``；本地的 ``tools/build_package.py``
CI 根本不调用。所以规则写错地方时，「本地核对干净、线上包却带着 tests/ 和计划文档」。

这里做两层检查：
1. **本地**：自己解析 toml，用 ``fnmatch`` 实现与上游 ``should_skip_path`` 等价的最小
   判定，对样例路径断言。不依赖上游 CLI（本地环境通常没有）。
2. **CI**：如果 ``plugin.neko_plugin_cli.core.build_rules`` 能导入（CI 跑测试时能），
   就用上游真实的 ``load_build_rules`` + ``should_skip_path`` 再跑一遍同样的样例。
"""
from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"


# ---------------------------------------------------------------------------
# 读取 [tool.neko.build]
# ---------------------------------------------------------------------------
def _load_toml(path: Path) -> dict:
    """优先 tomllib（3.11+），其次 tomli，最后用最小正则兜底——不允许因为缺库而跳过。"""
    try:
        import tomllib  # type: ignore[import-not-found]

        with path.open("rb") as fh:
            return tomllib.load(fh)
    except ModuleNotFoundError:
        pass
    try:
        import tomli  # type: ignore[import-not-found]

        with path.open("rb") as fh:
            return tomli.load(fh)
    except ModuleNotFoundError:
        pass
    return _parse_build_table_regex(path.read_text(encoding="utf-8"))


def _parse_build_table_regex(text: str) -> dict:
    """朴素兜底：只解析 ``[tool.neko.build]`` 下的 ``key = ["a", "b"]`` 形式。"""
    out: dict = {}
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_table = line == "[tool.neko.build]"
            continue
        if not in_table or "=" not in line:
            continue
        key, _, value = line.partition("=")
        items = re.findall(r'"([^"]*)"', value)
        out[key.strip()] = items
    return {"tool": {"neko": {"build": out}}}


def _build_rules() -> dict:
    data = _load_toml(_PYPROJECT)
    table = (data.get("tool") or {}).get("neko") or {}
    return table.get("build") or {}


# ---------------------------------------------------------------------------
# 与上游 should_skip_path 等价的最小实现（只覆盖本仓库用到的语义）
# ---------------------------------------------------------------------------
def _skip(rel: str, *, is_dir: bool, rules: dict) -> bool:
    path = PurePosixPath(rel)
    parts = path.parts if is_dir else path.parts[:-1]
    # exclude：对整条路径做 fnmatch
    if any(fnmatchcase(rel, pat) for pat in rules.get("exclude") or []):
        return True
    # exclude_dirs：任一层目录名命中
    dir_patterns = rules.get("exclude_dirs") or []
    if any(fnmatchcase(part, pat) for part in parts for pat in dir_patterns):
        return True
    if is_dir:
        return False
    # exclude_files：文件名或整条路径
    file_patterns = rules.get("exclude_files") or []
    if path.name in file_patterns:
        return True
    return any(fnmatchcase(rel, pat) for pat in file_patterns)


#: 必须被排除的样例（0.3.9 市场包里真实混进去的就是前两个）
_EXCLUDED = [
    "tests/test_smoke.py",
    "tests/dl_harness/fake_mirrors.py",
    "PLAN_download_tiering.md",
    "tools/build_package.py",
    "tools/dl_sources_report.json",
    "host/install_stage.py",
    "static/index.html.bak_dltier_20261121",
    "_trash/old.py",
    "_reports/dl_tiering_e2e/REPORT.md",
    "_test/s13_rework/run.py",
    ".gitignore",
]

#: 必须保留的样例（运行时 / 用户文档 / 第三方库）
_KEPT = [
    "download_engine.py",
    "download_catalog.py",
    "download_entries.py",
    "download_manager.py",
    "install_tiering.py",
    "workers/download_daemon.py",
    "workers/soulx_install_worker.py",
    "static/index.html",
    "vendor/acestep/__init__.py",
    "choreo/director.py",
    "data/lips_pinyin.json",
    "html/panel.html",
    "README.md",
    "README_user_intro.md",
    "LICENSE",
    "plugin.toml",
    "pyproject.toml",
]


# ---------------------------------------------------------------------------
# 1) 规则本身写对了
# ---------------------------------------------------------------------------
def test_rules_block_exists():
    rules = _build_rules()
    assert rules, "pyproject.toml 缺 [tool.neko.build]"


def test_binary_suffixes_still_excluded():
    """原有的 6 个二进制后缀一个都不能少（市场不允许包内二进制）。"""
    exclude = _build_rules().get("exclude") or []
    for suffix in ("*.pyd", "*.exe", "*.dll", "*.so", "*.dylib", "*.gz"):
        assert suffix in exclude, f"exclude 里缺 {suffix}"


def test_backup_patterns_excluded():
    exclude = _build_rules().get("exclude") or []
    assert "*.bak*" in exclude
    assert "*_bak*" in exclude


def test_exclude_dirs_cover_runtime_unneeded():
    dirs = _build_rules().get("exclude_dirs") or []
    for name in ("tests", "_reports", "_test", "_trash", "backup", "dist", "tools", "host"):
        assert name in dirs, f"exclude_dirs 里缺 {name}"


def test_exclude_files_cover_plan_docs():
    files = _build_rules().get("exclude_files") or []
    assert "PLAN_*.md" in files
    assert ".gitignore" in files


# ---------------------------------------------------------------------------
# 2) 样例路径判定（本地等价实现）
# ---------------------------------------------------------------------------
def test_samples_excluded_locally():
    rules = _build_rules()
    for rel in _EXCLUDED:
        assert _skip(rel, is_dir=False, rules=rules), f"{rel} 应被排除"


def test_samples_kept_locally():
    rules = _build_rules()
    for rel in _KEPT:
        assert not _skip(rel, is_dir=False, rules=rules), f"{rel} 应保留"


def test_directory_itself_excluded():
    rules = _build_rules()
    for rel in ("tests", "tools", "host", "_reports", "_test"):
        assert _skip(rel, is_dir=True, rules=rules), f"目录 {rel} 应被排除"


def test_vendor_is_kept():
    """vendor/ 是运行时依赖（ACE 上游代码），绝不能排除。"""
    rules = _build_rules()
    assert not _skip("vendor/acestep/model_downloader.py", is_dir=False, rules=rules)
    assert "vendor" not in (rules.get("exclude_dirs") or [])


# ---------------------------------------------------------------------------
# 3) CI 环境：用上游真实规则再验一遍
# ---------------------------------------------------------------------------
def _upstream_rules():
    """导入上游 build_rules；本地没有就返回 None（CI 上有）。"""
    try:
        from plugin.neko_plugin_cli.core.build_rules import (  # type: ignore[import-not-found]
            load_build_rules,
            should_skip_path,
        )
    except Exception:  # noqa: BLE001 - 本地无上游包，跳过这一段
        return None
    return load_build_rules, should_skip_path


def test_upstream_rules_agree_if_available():
    """CI 上会真的执行；本地没有上游包时只跳过这一段（规则本身仍由上面几条覆盖）。"""
    upstream = _upstream_rules()
    if upstream is None:
        return
    load_build_rules, should_skip_path = upstream
    rules = load_build_rules(_load_toml(_PYPROJECT))
    for rel in _EXCLUDED:
        assert should_skip_path(PurePosixPath(rel), is_dir=False, rules=rules), \
            f"上游判定应排除 {rel}"
    for rel in _KEPT:
        assert not should_skip_path(PurePosixPath(rel), is_dir=False, rules=rules), \
            f"上游判定应保留 {rel}"


def test_local_impl_matches_upstream_semantics():
    """本地等价实现与上游对同一批样例判定一致（CI 上才真正比对）。"""
    upstream = _upstream_rules()
    if upstream is None:
        return
    load_build_rules, should_skip_path = upstream
    up_rules = load_build_rules(_load_toml(_PYPROJECT))
    local_rules = _build_rules()
    for rel in _EXCLUDED + _KEPT:
        a = should_skip_path(PurePosixPath(rel), is_dir=False, rules=up_rules)
        b = _skip(rel, is_dir=False, rules=local_rules)
        assert a == b, f"{rel}: 上游={a} 本地={b}"
