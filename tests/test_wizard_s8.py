"""S8 前端向导逻辑自测（pytest 包装）。

前端是编译产物的单文件 ``static/index.html``（内联 JSX/等价 React），逻辑无法直接
用 pytest 导入；这里把纯函数块（``S8_PURE``）与全 shell 脚本的语法校验交给 node 跑。

对应 PLAN_download_tiering.md D-S8 里「步数映射、清单过滤、体积汇总、就绪判定」这些
不依赖真机的部分；需要真机的 CDP 场景（功能选择步显示、无 NVIDIA 置灰、进入主页入队）
列入 S12 待验清单。
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
        pytest.skip("未安装 node，跳过 S8 前端逻辑自测")
    return node


def test_wizard_s8_pure_functions_and_syntax():
    """运行 tests/test_wizard_s8.js：纯函数断言 + 全 shell 脚本语法校验。"""
    node = _node()
    script = _ROOT / "tests" / "test_wizard_s8.js"
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


def test_wizard_s8_backup_exists():
    assert (_ROOT / "static" / "index.html.bak_dltier_20261121").is_file(), \
        "缺少 static/index.html.bak_dltier_20261121 备份"
