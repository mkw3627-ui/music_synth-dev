"""S13 D-E 打包校验：确认 `_reports/`、`PLAN_*.md` 等不进插件包。

直接测 `tools/build_package.py` 的排除函数（`skip_dir` / `keep`），不真正跑打包
（避免每次测试都重打 13MB 的包）；另外在 test_build_excludes_leaky_paths 里跑一次
`main()`，用临时产物核对包内确实不含这些路径。
"""
from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_build():
    spec = importlib.util.spec_from_file_location("_s13_build_package", ROOT / "tools" / "build_package.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_s13_build_package"] = mod
    spec.loader.exec_module(mod)
    return mod


BUILD = _load_build()


@pytest.mark.parametrize("path", [
    "_reports/dl_tiering_e2e/REPORT.md",
    "_reports/dl_tiering_e2e/backup/config.json",
    "_reports/dl_tiering_e2e/evidence/2_snapshot_a.json",
])
def test_reports_dir_excluded(path):
    assert BUILD.keep(path) is False, f"{path} 不该进插件包"


@pytest.mark.parametrize("path", [
    "PLAN_download_tiering.md",
    "PLAN_other.md",
])
def test_plan_md_excluded(path):
    assert BUILD.keep(path) is False, f"{path} 不该进插件包"


@pytest.mark.parametrize("path", [
    "tests/test_smoke.py",
    "tests/dl_harness/fake_mirrors.py",
])
def test_tests_dir_still_excluded(path):
    assert BUILD.keep(path) is False, f"{path} 不该进插件包"


@pytest.mark.parametrize("path", [
    "_test/s13_rework/run.py",
    "_test/other/script.js",
])
def test_test_dir_excluded(path):
    assert BUILD.keep(path) is False, f"{path} 不该进插件包"


def test_bak_still_excluded():
    assert BUILD.keep("static/index.html.bak_dltier_20261121") is False


@pytest.mark.parametrize("path", [
    "download_engine.py",
    "download_catalog.py",
    "download_manager.py",
    "download_entries.py",
    "install_tiering.py",
    "workers/download_daemon.py",
    "static/index.html",
])
def test_new_modules_still_included(path):
    assert BUILD.keep(path) is True, f"{path} 必须进插件包"


def test_build_excludes_leaky_paths(tmp_path, monkeypatch):
    """端到端跑一次打包，断言产物里没有 _reports/、PLAN_、tests/、.bak。"""
    # 把产物指到临时目录，不污染 dist/
    monkeypatch.setattr(BUILD, "OUT", tmp_path / "music_synth.neko-plugin")
    rc = BUILD.main()
    assert rc == 0

    out = tmp_path / "music_synth.neko-plugin"
    assert out.is_file()
    names = zipfile.ZipFile(out).namelist()
    payload = [n for n in names if n.startswith("payload/plugins/music_synth/")]
    assert payload, "包内应有插件 payload"
    for n in payload:
        rel = n[len("payload/plugins/music_synth/"):]
        assert not rel.startswith("_reports/"), f"泄漏 _reports：{rel}"
        assert not rel.startswith("tests/"), f"泄漏 tests：{rel}"
        assert not rel.startswith("_test/"), f"泄漏 _test：{rel}"
        assert not rel.startswith("tools/"), f"泄漏 tools：{rel}"
        base = rel.split("/")[-1]
        assert not (base.startswith("PLAN_") and base.endswith(".md")), f"泄漏 PLAN：{rel}"
        assert ".bak" not in base, f"泄漏 .bak：{rel}"
