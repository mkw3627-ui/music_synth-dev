"""S12 修复回归：插件作为子包加载时（插件根不在 sys.path）模块必须可用。

真宿主实测（S12a）：插件是 ``plugin.plugins.music_synth`` 子包，裸的
``import download_catalog`` 会抛 ``ModuleNotFoundError``，导致 ``ms_feature_status``
等入口失败（``ms_dl_status`` 侥幸能用，是因为它只触及 ``download_manager`` 的顶层）。

修复方式：所有跨模块的 catalog 导入改成「先相对、失败回退绝对」。

**用子进程隔离验证**：pytest 自己的 ``sys.path`` 里通常已经有插件根（rootdir /
conftest 注入），在进程内断言「不在 path 上」永远不成立。子进程用 ``-I``
（隔离模式：忽略 PYTHONPATH 与用户 site）跑，才能真实复现宿主条件。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

#: 这几条断言「sys.executable 指向 launcher 时要另找 python」，依赖 Windows 的
#: 解释器命名与发现规则（python.exe / py launcher）；Linux 上解释器一律叫 python3，
#: 语义不同，跳过。
_IS_WINDOWS = os.name == "nt"
_windows_only = pytest.mark.skipif(not _IS_WINDOWS, reason="依赖 Windows 解释器命名/发现规则")

#: 子进程脚本：把插件挂成临时包名，且不动 sys.path。
_PROBE = r"""
import importlib, json, sys, types
base = sys.argv[1]
pkg = types.ModuleType("_s12_probe")
pkg.__path__ = [base]
sys.modules["_s12_probe"] = pkg
root_on_path = any(str(p) == base for p in sys.path)
out = {"root_on_path": root_on_path, "modules": {}, "errors": {}}
for mod in ("download_catalog", "download_engine", "download_manager",
            "install_tiering", "download_entries"):
    try:
        importlib.import_module(f"_s12_probe.{mod}")
        out["modules"][mod] = "ok"
    except Exception as exc:
        out["errors"][mod] = f"{type(exc).__name__}: {exc}"
if not out["errors"]:
    dm = sys.modules["_s12_probe.download_manager"]
    it = sys.modules["_s12_probe.install_tiering"]
    eng = sys.modules["_s12_probe.download_engine"]
    try:
        out["items_for"] = dm.items_for(["cover"], ["T2"])
        out["tier_items"] = it.tier_items(["cover"], ["T2"])
        out["feature_ready_missing"] = dm.feature_ready(
            {"compute_root": sys.argv[2]}, "cover")["missing"]
        out["engine_catalog"] = eng._load_catalog().ARTIFACTS["ddsp_contentvec"]["kind"]
    except Exception as exc:
        out["errors"]["calls"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out, ensure_ascii=False))
"""


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    """在隔离的子进程里加载插件模块，返回结构化结果。"""
    root = tmp_path_factory.mktemp("s12_probe")
    proc = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, str(PLUGIN_ROOT), str(root)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert proc.returncode == 0, f"子进程失败：{proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_precondition_plugin_root_off_path(probe):
    """前提：子进程里插件根目录确实不在 sys.path（否则测不出宿主条件）。"""
    assert probe["root_on_path"] is False


def test_all_modules_import_as_subpackage(probe):
    assert probe["errors"] == {}, probe["errors"]
    assert set(probe["modules"].values()) == {"ok"}
    assert len(probe["modules"]) == 5


def test_manager_items_for_works(probe):
    assert set(probe["items_for"]) == {
        "ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke"}


def test_tiering_works(probe):
    assert set(probe["tier_items"]) == {
        "ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke"}


def test_feature_ready_works(probe):
    assert "ddsp_contentvec" in probe["feature_ready_missing"]


def test_engine_loads_catalog(probe):
    assert probe["engine_catalog"] == "file"


def test_no_bare_absolute_catalog_import():
    """静态检查：裸的 ``import download_catalog`` 只能出现在 except 回退分支里。"""
    offenders = []
    for name in ("download_manager.py", "install_tiering.py",
                 "download_engine.py", "download_entries.py"):
        path = PLUGIN_ROOT / name
        lines = path.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped.startswith("import download_catalog"):
                continue
            # 往上找最近的非空行：必须是 ``except ImportError:``
            prev = ""
            for back in range(lineno - 2, max(-1, lineno - 6), -1):
                if lines[back].strip():
                    prev = lines[back].strip()
                    break
            if not prev.startswith("except ImportError"):
                offenders.append(f"{name}:{lineno}: {stripped}  (上一行: {prev})")
    assert not offenders, "存在裸的绝对导入：\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# 真宿主实测的第二个 bug：paths 里的 Path 不能 JSON 序列化
# ---------------------------------------------------------------------------
def test_json_safe_handles_paths(tmp_path):
    """``runtime.resolve_paths()`` 的返回值含 Path，写命令文件前必须转字符串。"""
    dm = _import_manager_in_subpackage()
    paths = {
        "compute_root": tmp_path,
        "synth_python": tmp_path / "envs" / "ddsp" / "Scripts" / "python.exe",
        "nested": {"a": [tmp_path, 1, None, True]},
        "raw": {"download_engine": "daemon"},
    }
    safe = dm._json_safe(paths)
    import json
    text = json.dumps(safe)          # 不抛 TypeError 即通过
    assert "python.exe" in text
    assert isinstance(safe["compute_root"], str)
    assert safe["nested"]["a"][0] == str(tmp_path)


def test_write_command_accepts_path_values(tmp_path):
    """端到端：write_command 收到带 Path 的 payload 不会炸。"""
    dm = _import_manager_in_subpackage()
    paths = {"compute_root": tmp_path}
    dm.write_command(paths, {"action": "enqueue",
                             "items": [{"key": "x", "paths": paths}]})
    files = list((tmp_path / "_downloads" / "cmd").glob("*.json"))
    assert len(files) == 1
    import json
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert isinstance(payload["items"][0]["paths"]["compute_root"], str)


def _import_manager_in_subpackage():
    """在「插件根不在 sys.path」的语境下拿 download_manager 模块。"""
    import importlib
    import sys
    import types
    name = "_s12_jsonhost"
    if name not in sys.modules:
        pkg = types.ModuleType(name)
        pkg.__path__ = [str(PLUGIN_ROOT)]
        sys.modules[name] = pkg
    return importlib.import_module(f"{name}.download_manager")


# ---------------------------------------------------------------------------
# 真宿主实测的第三个 bug：sys.executable 是宿主 launcher，不是 python
# ---------------------------------------------------------------------------
@_windows_only
def test_find_python_executable_skips_non_python(monkeypatch):
    """``sys.executable`` 指向 projectneko_server.exe 时必须另找 python。"""
    dm = _import_manager_in_subpackage()
    monkeypatch.setattr(sys, "executable",
                        r"E:\SteamLibrary\common\n.e.k.o\bin\projectneko_server.exe")
    found = dm._find_python_executable()
    assert "projectneko_server" not in found.lower(), f"不能拿宿主 launcher 当解释器：{found}"


def test_find_python_executable_prefers_real_python(monkeypatch, tmp_path):
    dm = _import_manager_in_subpackage()
    fake = tmp_path / "python.exe"
    fake.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(fake))
    assert dm._find_python_executable() == str(fake)


@_windows_only
def test_daemon_python_is_not_host_binary(monkeypatch):
    """回归：真宿主条件下 ensure_daemon 的命令行第一项必须是 python 类可执行文件。"""
    dm = _import_manager_in_subpackage()
    monkeypatch.setattr(sys, "executable",
                        r"E:\SteamLibrary\common\n.e.k.o\bin\projectneko_server.exe")
    exe = dm._find_python_executable()
    name = Path(exe).name.lower()
    assert name.startswith("python") or name.startswith("py"), name
