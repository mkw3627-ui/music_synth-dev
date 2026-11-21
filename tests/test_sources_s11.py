"""S11 下载速度与来源设置 逻辑自测（pytest 包装）。

前端是编译产物单文件 ``static/index.html``，逻辑无法直接 import；这里把纯函数块
（``S11_PURE``）与全 shell 脚本语法校验交给 node 跑。
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
        pytest.skip("未安装 node，跳过 S11 前端逻辑自测")
    return node


def test_sources_s11_pure_functions_and_syntax():
    node = _node()
    script = _ROOT / "tests" / "test_sources_s11.js"
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


def test_plugin_toml_download_keys():
    """plugin.toml 新增的 10 个 download_* 键齐全、类型正确、旧键未改名。"""
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    doc = tomllib.loads((_ROOT / "plugin.toml").read_text(encoding="utf-8"))
    ms = doc.get("music_synth") or {}
    new_keys = [
        "download_max_mbps", "download_mirror_preference", "download_check_ssl",
        "download_max_retries", "download_verify_hash", "download_max_parallel",
        "download_resume", "download_idle_sleep_s", "download_cache_days",
        "download_source_strategy",
    ]
    for key in new_keys:
        assert key in ms, f"plugin.toml 缺少 {key}"
    # 旧键仍在、未改名
    for old in ("download_source", "auto_setup", "compute_root", "max_parallel", "precision"):
        assert old in ms, f"旧键 {old} 丢失"
    # 类型
    assert isinstance(ms["download_max_mbps"], int)
    assert isinstance(ms["download_max_retries"], int)
    assert isinstance(ms["download_max_parallel"], int)
    assert isinstance(ms["download_verify_hash"], bool)
    assert isinstance(ms["download_resume"], bool)
    assert isinstance(ms["download_check_ssl"], bool)
    assert ms["download_mirror_preference"] in ("auto", "mirror", "huggingface")
    assert ms["download_source_strategy"] in ("fastest", "mirror_first", "official_first")
