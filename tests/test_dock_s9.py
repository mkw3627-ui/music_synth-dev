"""S9 常驻下载条 DownloadDock 逻辑自测（pytest 包装）。

前端是编译产物单文件 ``static/index.html``，逻辑无法直接 import；这里把纯函数块
（``S9_PURE``）与全 shell 脚本的语法校验交给 node 跑。

对应 PLAN_download_tiering.md D-S9 里不依赖真机的部分（轮询节流、文案拼装、eta、
状态→颜色/动作映射、完成 8 秒收起、失败保持）；需真机 CDP 的条目列入 S12 待验清单。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _node() -> str:
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        pytest.skip("未安装 node，跳过 S9 前端逻辑自测")
    return node


def test_dock_s9_pure_functions_and_syntax():
    node = _node()
    script = _ROOT / "tests" / "test_dock_s9.js"
    proc = subprocess.run(
        [node, str(script)],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"node 自测失败（exit={proc.returncode}）:\n{output}"
    assert "ALL PASS" in output, f"未看到 ALL PASS：\n{output}"
