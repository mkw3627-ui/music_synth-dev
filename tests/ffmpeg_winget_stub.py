"""install_ffmpeg_winget 桩验证：不真正执行 winget，覆盖 4 种返回路径。

直接运行：python tests/ffmpeg_winget_stub.py
（repo 根 __init__.py 会 import 宿主 SDK，pytest 收集不了，故用独立脚本。）
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_PKG = "_ms_synth_testpkg"


def _load() -> tuple:
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = []  # 空 path：不让 import 系统去真实目录找 __init__.py
    sys.modules[_PKG] = pkg

    def _load_module(name: str, filename: str):
        spec = importlib.util.spec_from_file_location(name, _ROOT / filename)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    runtime_mod = _load_module(f"{_PKG}.runtime", "runtime.py")
    setup_mod = _load_module(f"{_PKG}.setup", "setup.py")
    return runtime_mod, setup_mod


RUNTIME, SETUP = _load()

_real_which = shutil.which
_real_find = RUNTIME._find_ffmpeg
_real_run_stream = SETUP._run_stream


def _run(*, winget, run_code, found_after) -> tuple[dict, list[str]]:
    prog: list[str] = []
    shutil.which = lambda name: winget if name == "winget" else None
    RUNTIME._find_ffmpeg = lambda: found_after
    SETUP._run_stream = lambda cmd, progress, label, **kw: (run_code, ["第1行", "第2行", "第3行"])
    try:
        return SETUP.install_ffmpeg_winget(progress=prog.append), prog
    finally:
        shutil.which = _real_which
        RUNTIME._find_ffmpeg = _real_find
        SETUP._run_stream = _real_run_stream


def _check(name: str, cond: bool, detail) -> bool:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f"  <- {detail}"))
    return bool(cond)


def main() -> int:
    ok = True

    r, prog = _run(winget=None, run_code=0, found_after="")
    ok &= _check("winget 不存在", r["ok"] is False and r["installed"] is False
                 and "没有 winget" in r["error"] and prog == [], r)

    r, _ = _run(winget="winget.exe", run_code=0, found_after=r"C:\ffmpeg\bin\ffmpeg.exe")
    ok &= _check("安装成功且检测到", r["ok"] is True and r["detected"] is True
                 and r["path"] == r"C:\ffmpeg\bin\ffmpeg.exe", r)

    r, _ = _run(winget="winget.exe", run_code=0, found_after="")
    ok &= _check("安装成功但未检测到（需重启）", r["installed"] is True
                 and r["need_restart"] is True and "重启" in r["error"], r)

    r, _ = _run(winget="winget.exe", run_code=1, found_after="")
    ok &= _check("安装失败退出码非 0", r["ok"] is False and r["installed"] is False
                 and "退出码 1" in r["error"] and r["manual"].startswith("https://www.gyan.dev"), r)

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
