"""离线演出编排（show.json 生产端）。

P1 阶段：只做规则导演，不接本地大模型。
- ``analyze``  librosa：节拍/小节/段落/能量/人声事件
- ``presets``  风格预设（ballad / pop / edm）
- ``director`` 歌曲 + 分析 + 歌词 → show.json（契约见
  ``舞台编排实施计划_v3.md`` 第 3 节，冻结接口）
- ``schema``   show.json 结构校验（可 `python -I choreo/schema.py <file>` 单独跑）

运行时不在这里：舞台前端和 bridge.js 只消费 show.json，按「演出脚本 + 当前
时间」求值，保持时间纯函数。
"""
from __future__ import annotations

GENERATOR = "choreo 0.1"

__all__ = ["GENERATOR"]
