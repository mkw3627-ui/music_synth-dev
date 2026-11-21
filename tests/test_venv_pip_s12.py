"""S12a 修复回归：新建 venv 里没有 pip 时要用 ensurepip 补上。

真机实测：便携/裁剪版 Python 建出来的 venv 只有 python.exe，没有 pip，
导致后续每个 ``_pip`` 都报 ``No module named pip``，整个 T0 安装失败。
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup_mod():
    """每个用例独立导入（monkeypatch 的替换不能互相污染）。"""
    name = f"_s12_venv_probe_{id(object())}"
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[name] = pkg
    mod = importlib.import_module(f"{name}.setup")
    yield mod
    for k in [k for k in list(sys.modules) if k == name or k.startswith(name + ".")]:
        sys.modules.pop(k, None)


def test_venv_has_pip_returns_bool(setup_mod, tmp_path):
    fake = tmp_path / "python.exe"
    fake.write_bytes(b"")
    # 不是真解释器 → 应为 False（不抛异常）
    assert setup_mod._venv_has_pip(fake) is False


def test_venv_has_pip_missing_binary(setup_mod, tmp_path):
    assert setup_mod._venv_has_pip(tmp_path / "nope.exe") is False


def test_make_venv_calls_ensurepip_when_pip_missing(setup_mod, tmp_path, monkeypatch):
    """venv 创建后没有 pip → 必须调用 ensurepip。"""
    venv_dir = tmp_path / "envs" / "ddsp"
    calls: list[list[str]] = []

    def fake_run_stream(cmd, progress, label, **kw):
        calls.append([str(c) for c in cmd])
        text = " ".join(str(c) for c in cmd)
        if "-m venv" in text:
            scripts = venv_dir / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_bytes(b"")
            return 0, ""
        return 0, ""

    monkeypatch.setattr(setup_mod, "_run_stream", fake_run_stream)
    monkeypatch.setattr(setup_mod, "_venv_ok", lambda py: False)
    # 第一次检查返回 False（没 pip），ensurepip 之后返回 True
    state = {"n": 0}

    def fake_has_pip(py):
        state["n"] += 1
        return state["n"] > 1

    monkeypatch.setattr(setup_mod, "_venv_has_pip", fake_has_pip)
    monkeypatch.setattr(setup_mod, "_pip", lambda *a, **k: None)
    monkeypatch.setattr(setup_mod, "_say", lambda *a, **k: None)

    setup_mod._make_venv(str(tmp_path / "base" / "python.exe"), venv_dir,
                         lambda *_: None, "DDSP 环境")

    joined = [" ".join(c) for c in calls]
    assert any("-m ensurepip" in j for j in joined), f"没调用 ensurepip：{joined}"


def test_make_venv_skips_ensurepip_when_pip_present(setup_mod, tmp_path, monkeypatch):
    venv_dir = tmp_path / "envs" / "ddsp"
    calls: list[str] = []
    # 断言用的模块必须是 _make_venv 实际查找的那个（fixture 每次新导入）
    assert setup_mod.__name__.endswith(".setup")

    def fake_run_stream(cmd, progress, label, **kw):
        calls.append(" ".join(str(c) for c in cmd))
        if "-m venv" in calls[-1]:
            scripts = venv_dir / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_bytes(b"")
        return 0, ""

    monkeypatch.setattr(setup_mod, "_run_stream", fake_run_stream)
    monkeypatch.setattr(setup_mod, "_venv_ok", lambda py: False)
    monkeypatch.setattr(setup_mod, "_venv_has_pip", lambda py: True)
    monkeypatch.setattr(setup_mod, "_pip", lambda *a, **k: None)
    monkeypatch.setattr(setup_mod, "_say", lambda *a, **k: None)

    setup_mod._make_venv(str(tmp_path / "base" / "python.exe"), venv_dir,
                         lambda *_: None, "DDSP 环境")
    assert not any("-m ensurepip" in c for c in calls), f"有 pip 时不该重复补装。calls={calls}"


def test_make_venv_raises_when_pip_unfixable(setup_mod, tmp_path, monkeypatch):
    venv_dir = tmp_path / "envs" / "ddsp"

    def fake_run_stream(cmd, progress, label, **kw):
        if "-m venv" in " ".join(str(c) for c in cmd):
            scripts = venv_dir / "Scripts"
            scripts.mkdir(parents=True, exist_ok=True)
            (scripts / "python.exe").write_bytes(b"")
        return 0, ""

    monkeypatch.setattr(setup_mod, "_run_stream", fake_run_stream)
    monkeypatch.setattr(setup_mod, "_venv_ok", lambda py: False)
    monkeypatch.setattr(setup_mod, "_venv_has_pip", lambda py: False)  # 始终没有
    monkeypatch.setattr(setup_mod, "_pip", lambda *a, **k: None)
    monkeypatch.setattr(setup_mod, "_say", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="pip"):
        setup_mod._make_venv(str(tmp_path / "base" / "python.exe"), venv_dir,
                             lambda *_: None, "DDSP 环境")
