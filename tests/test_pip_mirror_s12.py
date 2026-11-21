"""S12a 修复回归：pip / torch 镜像顺序按 S1 实测速度排列（S5 偏差 #3 收口）。"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def setup_mod():
    name = "_s12_pip_probe"
    if name in sys.modules:
        mods = sys.modules[name]
        return mods.setup  # type: ignore[attr-defined]
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(PLUGIN_ROOT)]
    sys.modules[name] = pkg
    mods = types.SimpleNamespace(setup=importlib.import_module(f"{name}.setup"))
    sys.modules[name] = mods  # type: ignore[assignment]
    return mods.setup


def test_aliyun_is_first_pypi_mirror(setup_mod):
    """S1 实测阿里云 24–28MB/s，比清华快约 6 倍，应排第一。"""
    assert setup_mod.PYPI_INDEX.startswith("https://mirrors.aliyun.com/")
    assert setup_mod.PYPI_MIRRORS[0] == setup_mod.PYPI_INDEX


def test_pypi_mirror_order(setup_mod):
    hosts = [setup_mod.PYPI_MIRRORS[i] for i in range(len(setup_mod.PYPI_MIRRORS))]
    assert any("aliyun" in h for h in hosts[:1])
    assert any("ustc" in h for h in hosts), "USTC 实测可用，应作为候选"
    assert any("tencent" in h for h in hosts), "腾讯云实测可用，应作为候选"
    assert any("tuna" in h for h in hosts), "清华仍保留作兜底"


def test_no_duplicate_mirrors(setup_mod):
    assert len(set(setup_mod.PYPI_MIRRORS)) == len(setup_mod.PYPI_MIRRORS)


def test_torch_fallback_only_for_cu128(setup_mod):
    fb = setup_mod._torch_fallback_args(setup_mod.TORCH_CU128_INDEX)
    assert fb, "cu128 应有镜像回落"
    assert "-f" in fb
    assert any("aliyun" in x for x in fb)
    assert setup_mod._torch_fallback_args(setup_mod.TORCH_CPU_INDEX) == []


def test_torch_fallback_keeps_packages(setup_mod):
    fb = setup_mod._torch_fallback_args(setup_mod.TORCH_CU128_INDEX)
    assert "torch" in fb and "torchaudio" in fb


def test_pip_failure_switches_mirror(setup_mod, monkeypatch):
    """模拟第一档镜像 pip 失败 → 自动换下一档（命令行 --index-url 已更换）。"""
    calls: list[list[str]] = []

    def fake_run_stream(cmd, progress, label, head=None, **kw):
        calls.append(list(cmd))
        # 第一次（阿里云）失败，第二次成功
        return (1, "ERROR: connection reset") if len(calls) == 1 else (0, "")

    monkeypatch.setattr(setup_mod, "_run_stream", fake_run_stream)
    monkeypatch.setattr(setup_mod, "_pip_raw_progress_ok", lambda py: False)
    monkeypatch.setattr(setup_mod, "_network_down_error", lambda x: False)
    monkeypatch.setattr(setup_mod, "_dead_proxy_text", lambda x: False)

    setup_mod._pip(Path("python.exe"), ["requests"], lambda *_: None, "装依赖")
    assert len(calls) == 2, f"应尝试两档镜像，实际 {len(calls)}"
    assert "mirrors.aliyun.com" in " ".join(calls[0])
    assert "mirrors.ustc.edu.cn" in " ".join(calls[1]), calls[1]


# ---------------------------------------------------------------------------
# 真机实测：带本地版本号的 torch 被普通 PyPI 换成了 CPU 版
# ---------------------------------------------------------------------------
def test_torch_pin_uses_index_url_not_extra(setup_mod, tmp_path, monkeypatch):
    """``torch==2.14.1+cu128`` 这类版本只存在于 PyTorch index。

    用 ``--extra-index-url`` 时 pip 会从默认 PyPI 解析到无后缀的同名版本
    （真机实测装成 2.14.1+cpu，把 GPU 版换掉了）。必须用 ``--index-url``。
    """
    calls: list[list[str]] = []

    monkeypatch.setattr(setup_mod, "_run_stream",
                        lambda cmd, *a, **k: (calls.append([str(c) for c in cmd]), (0, ""))[1])
    monkeypatch.setattr(setup_mod, "_pip_raw_progress_ok", lambda py: False)
    monkeypatch.setattr(setup_mod, "_torch_pin", lambda py: "2.14.1+cu128")
    monkeypatch.setattr(setup_mod, "_pip", lambda *a, **k: calls.append(["<pip>"] + list(a[1])))
    monkeypatch.setattr(setup_mod, "invalidate_deps_cache", lambda py: None)
    monkeypatch.setattr(setup_mod, "check_dependencies",
                        lambda paths, probe=None: {"items": [], "ready": True,
                                                   "missing": [], "installable_missing": []})
    monkeypatch.setattr(setup_mod.runtime, "probe_hardware",
                        lambda force=False: {"gpu": {"cc_major": 8, "compute_cap": "8.9",
                                                     "name": "RTX", "total_gib": 12,
                                                     "free_gib": 11}})
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    paths = {
        "compute_root": tmp_path, "configured": True, "data_dir": tmp_path / "data",
        "envs_dir": tmp_path / "envs", "models_dir": tmp_path / "models",
        "work_dir": tmp_path / "work", "sep_model_dir": tmp_path / "sep",
        "ddsp_root": tmp_path / "ddsp", "ddsp_config": tmp_path / "ddsp" / "c.yaml",
        "ddsp_pretrain_reflow": tmp_path / "m.pt", "ddsp_base_low": tmp_path / "l.pt",
        "ddsp_base_mid": tmp_path / "m.pt", "ddsp_base_high": tmp_path / "h.pt",
        "ddsp_pretrain_contentvec": tmp_path / "c.bin",
        "ddsp_pretrain_rmvpe": tmp_path / "r.pt",
        "ddsp_pretrain_vocoder": tmp_path / "v",
        "ddsp_vocoder_config": tmp_path / "v" / "config.json",
        "synth_python": py, "sep_python": py,
    }
    setup_mod.install(paths, ["ddsp_deps"], lambda *_: None,
                      probe={"gpu": {"cc_major": 8, "compute_cap": "8.9",
                                     "name": "RTX", "total_gib": 12, "free_gib": 11}})
    torch_calls = [c for c in calls if any("torch==" in str(x) for x in c)]
    assert torch_calls, f"没看到 torch pin 调用：{calls}"
    for c in torch_calls:
        joined = " ".join(str(x) for x in c)
        assert "--index-url" in joined, f"torch pin 必须用 --index-url：{joined}"
        assert "--extra-index-url" not in joined, f"不能用 --extra-index-url：{joined}"


def test_torch_install_attempts_never_use_extra_index(setup_mod):
    """torch 的每条尝试都必须是 --index-url 或 -f，不能用 --extra-index-url。"""
    for index in (setup_mod.TORCH_CU128_INDEX, setup_mod.TORCH_CPU_INDEX):
        for args, tag in setup_mod._torch_install_args(index):
            joined = " ".join(args)
            assert "--extra-index-url" not in joined, f"{tag} 用了 extra-index：{joined}"
            assert ("--index-url" in joined) or ("-f" in args), f"{tag} 没有来源：{joined}"


def test_torch_install_has_mirror_for_cu128(setup_mod):
    attempts = setup_mod._torch_install_args(setup_mod.TORCH_CU128_INDEX)
    assert len(attempts) >= 2
    assert any("-f" in a for a, _ in attempts), "cu128 应有阿里云 find-links 回退"
    assert any("aliyun" in " ".join(a) for a, _ in attempts)


def test_torch_install_cpu_has_no_mirror(setup_mod):
    attempts = setup_mod._torch_install_args(setup_mod.TORCH_CPU_INDEX)
    assert len(attempts) == 1, "CPU 版官方源够用，不需要镜像"


# ---------------------------------------------------------------------------
# D2 修复：GPU 机上已装 CPU torch 时，pip 不 downgrade → 必须 force-reinstall
# ---------------------------------------------------------------------------
def test_torch_force_reinstall_when_cpu_build(setup_mod):
    """GPU 机 + CPU torch → 每条尝试都带 --force-reinstall。"""
    for args, tag in setup_mod._torch_install_args(setup_mod.TORCH_CU128_INDEX, force=True):
        assert "--force-reinstall" in args, f"{tag} 应带 --force-reinstall"
        assert "torch" in args and "torchaudio" in args


def test_torch_no_force_when_normal(setup_mod):
    """普通安装（无 force）不带 --force-reinstall。"""
    for args, tag in setup_mod._torch_install_args(setup_mod.TORCH_CU128_INDEX, force=False):
        assert "--force-reinstall" not in args, f"{tag} 不该带 --force-reinstall"


def test_install_forces_torch_reinstall_on_cpu_build(setup_mod, tmp_path, monkeypatch):
    """GPU 机 + 已装 CPU torch → install() 传 force=True（带 --force-reinstall）。"""
    pip_calls: list[list[str]] = []
    monkeypatch.setattr(setup_mod, "_pip", lambda py, args, *a, **k: pip_calls.append(list(args)))
    monkeypatch.setattr(setup_mod, "_torch_index", lambda gpu: setup_mod.TORCH_CU128_INDEX)
    monkeypatch.setattr(setup_mod, "_torch_install_args",
                        lambda index, force=False: [(["--index-url", index]
                                                     + (["--force-reinstall"] if force else [])
                                                     + ["torch", "torchaudio"], "t")])
    monkeypatch.setattr(setup_mod.runtime, "has_cuda_build", lambda py: False)  # CPU torch
    monkeypatch.setattr(setup_mod, "_pip_raw_progress_ok", lambda py: False)
    monkeypatch.setattr(setup_mod, "_torch_pin", lambda py: "")
    monkeypatch.setattr(setup_mod, "invalidate_deps_cache", lambda py: None)
    monkeypatch.setattr(setup_mod, "check_dependencies",
                        lambda paths, probe=None: {"items": [], "ready": True,
                                                   "missing": [], "installable_missing": []})
    monkeypatch.setattr(setup_mod.runtime, "probe_hardware",
                        lambda force=False: {"gpu": {"cc_major": 8, "compute_cap": "8.9",
                                                     "name": "RTX", "total_gib": 12,
                                                     "free_gib": 11}})
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    paths = {
        "compute_root": tmp_path, "configured": True, "data_dir": tmp_path / "data",
        "envs_dir": tmp_path / "envs", "models_dir": tmp_path / "models",
        "work_dir": tmp_path / "work", "sep_model_dir": tmp_path / "sep",
        "ddsp_root": tmp_path / "ddsp", "ddsp_config": tmp_path / "ddsp" / "c.yaml",
        "ddsp_pretrain_reflow": tmp_path / "m.pt", "ddsp_base_low": tmp_path / "l.pt",
        "ddsp_base_mid": tmp_path / "m.pt", "ddsp_base_high": tmp_path / "h.pt",
        "ddsp_pretrain_contentvec": tmp_path / "c.bin",
        "ddsp_pretrain_rmvpe": tmp_path / "r.pt",
        "ddsp_pretrain_vocoder": tmp_path / "v",
        "ddsp_vocoder_config": tmp_path / "v" / "config.json",
        "synth_python": py, "sep_python": py,
    }
    setup_mod.install(paths, ["torch"], lambda *_: None,
                      probe={"gpu": {"cc_major": 8, "compute_cap": "8.9",
                                     "name": "RTX", "total_gib": 12, "free_gib": 11}})
    torch_calls = [c for c in pip_calls if "torch" in c]
    assert torch_calls, "没看到 torch 安装调用"
    assert any("--force-reinstall" in c for c in torch_calls), \
        f"CPU torch 应触发 --force-reinstall：{torch_calls}"
