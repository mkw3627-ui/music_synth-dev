"""第 10 维：字体人格（typeface）。

按「风格预设 × 歌词语义（theme / 逐行 emotion）× 歌词内容」给整首歌定一套字体
人格（``persona`` 枚举见 ``choreo/schema.py``），并给出 ``primary`` / ``accent``
字体 id、字重、倾斜与排版规则（``vertical_ok`` / ``max_chars_per_row`` /
``kw_scale`` / ``decor``）。

字体 id 只认 ``static/fonts/fonts.json`` 里登记过的 id。T2-B 交付字体之前该文件
不存在，这里统一用占位 id ``sys-default``，等 T2-B 换真字体（本阶段不下载、
不打包任何字体文件）。
"""
from __future__ import annotations

import json
import os

FONT_PLACEHOLDER = "sys-default"

# persona → 基础字重 / 倾斜 / 排版规则（字典里的数都落在 schema 的区间内）
PERSONAS: dict[str, dict] = {
    "cute_round": {
        "weight": 900, "skew": -3,
        "rules": {"vertical_ok": True, "max_chars_per_row": 8, "kw_scale": 1.45,
                  "decor": ["sticker", "outline", "glow"]},
    },
    "soft_hand": {
        "weight": 400, "skew": 0,
        "rules": {"vertical_ok": True, "max_chars_per_row": 10, "kw_scale": 1.30,
                  "decor": ["softglow", "outline"]},
    },
    "elegant_serif": {
        "weight": 500, "skew": 0,
        "rules": {"vertical_ok": True, "max_chars_per_row": 11, "kw_scale": 1.25,
                  "decor": ["shadow", "grain"]},
    },
    "bold_pop": {
        "weight": 900, "skew": -6,
        "rules": {"vertical_ok": False, "max_chars_per_row": 7, "kw_scale": 1.55,
                  "decor": ["outline", "glow", "shine"]},
    },
    "tech_mono": {
        "weight": 700, "skew": -8,
        "rules": {"vertical_ok": False, "max_chars_per_row": 12, "kw_scale": 1.40,
                  "decor": ["glitch", "grid", "glow"]},
    },
    "brush_ancient": {
        "weight": 500, "skew": 4,
        "rules": {"vertical_ok": True, "max_chars_per_row": 7, "kw_scale": 1.35,
                  "decor": ["ink", "grain"]},
    },
}

ORDER = ("cute_round", "soft_hand", "elegant_serif", "bold_pop", "tech_mono",
         "brush_ancient")

# 主题里出现这些词就更偏向某个人格（主题权重 2.0/次）
THEME_HINTS = {
    "cute_round": ("可爱", "甜", "糖果", "撒娇", "初恋", "喜欢", "恋爱", "萌",
                   "心跳", "粉色", "拥抱", "亲亲", "约会", "少女", "元气"),
    "soft_hand": ("思念", "想念", "等待", "雨", "远方", "故乡", "回忆", "温柔",
                  "安静", "孤单", "离别", "月光", "深夜", "约定", "怀旧",
                  "失去", "遗憾", "错过", "成长", "忘记", "記得", "记得",
                  "懐かしい", "過去", "夕日", "夕焼け", "朝焼け", "大人", "成長",
                  "ノスタルジー", "思い出", "記憶", "遠い", "さよなら", "失った"),
    "elegant_serif": ("岁月", "时光", "永恒", "誓言", "古典", "诗", "旧信",
                      "优雅", "花事", "年轮"),
    "bold_pop": ("燃", "燃烧", "战斗", "火焰", "奔跑", "呐喊", "热血", "不服",
                 "爆发", "冲刺", "最高", "向前", "冲破", "战斗"),
    "tech_mono": ("赛博", "数字", "机器", "霓虹", "信号", "虚拟",
                  "代码", "电子", "脉冲", "电流"),
    "brush_ancient": ("剑", "江湖", "山河", "千秋", "红尘", "侠", "长安",
                      "青衫", "相思", "古诗", "江南", "月下", "酒"),
}
# 歌词正文里的命中权重低一些（0.5/次，最多计 4 次）；宽泛的日文词只看主题句，
# 免得正文里出现一两个「過去 / 大人」就把整首带偏
_BODY_SKIP = ("過去", "大人", "成長", "夕日", "夕焼け", "朝焼け", "ノスタルジー")
LYRIC_HINTS = {persona: tuple(word for word in words if word not in _BODY_SKIP)
               for persona, words in THEME_HINTS.items()}


def _load_fonts(fonts_path: str) -> dict[str, list[str]]:
    """读 fonts.json → persona → 已登记字体 id 列表；文件缺失就是空表。"""
    try:
        with open(fonts_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    entries = data.get("fonts") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return {}
    out: dict[str, list[str]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        font_id = str(item.get("id") or "").strip()
        persona = str(item.get("persona") or "").strip()
        if font_id and persona in PERSONAS and font_id not in out.get(persona, []):
            out.setdefault(persona, []).append(font_id)
    return out


def _count_hits(text: str, words) -> int:
    low = str(text or "").lower()
    return sum(low.count(str(word).lower()) for word in words or ())


def _persona(preset: dict, semantics: dict | None, texts: list, title: str) -> str:
    sem = semantics or {}
    theme = str(sem.get("theme") or "") + " " + str(title or "")
    body = " ".join(str(t or "") for t in (texts or []))[:600]
    default = str((preset or {}).get("typeface", {}).get("persona") or "")
    scores = {}
    for persona in ORDER:
        score = 0.6 if persona == default else 0.0
        score += 2.0 * min(4, _count_hits(theme, THEME_HINTS.get(persona)))
        score += 0.5 * min(4, _count_hits(body, LYRIC_HINTS.get(persona)))
        scores[persona] = score
    # 逐行情绪的分布是主要依据（LLM 语义）；预设默认值只用来打破平手
    rows = [row for row in (sem.get("lines") or []) if isinstance(row, dict)]
    if rows:
        total = float(len(rows))
        share: dict[str, float] = {}
        for row in rows:
            emo = str(row.get("emotion") or "")
            share[emo] = share.get(emo, 0.0) + 1.0 / total
        scores["cute_round"] += 2.4 * (share.get("joy", 0.0) + share.get("playful", 0.0))
        scores["soft_hand"] += 3.4 * (share.get("tender", 0.0) + share.get("longing", 0.0))
        scores["soft_hand"] += 1.2 * share.get("sad", 0.0)
        scores["elegant_serif"] += 2.0 * share.get("sad", 0.0)
        scores["elegant_serif"] += 1.2 * share.get("longing", 0.0)
        scores["bold_pop"] += 2.6 * (share.get("determined", 0.0)
                                     + share.get("proud", 0.0))
    # 感叹号多 → 外放；省略号多 → 内敛（只在平手时起作用）
    if str(body).count("！") + str(body).count("!") >= 3:
        scores["bold_pop"] += 0.8
    if str(body).count("…") >= 3:
        scores["soft_hand"] += 0.8
    best = ORDER[0]
    for persona in ORDER[1:]:
        if scores[persona] > scores[best] + 1e-9:
            best = persona
    return best


def build(preset: dict, semantics: dict | None = None, texts: list | None = None,
          title: str = "", fonts_path: str | None = None) -> dict:
    """整首歌的字体人格（一首歌一套，保证统一）。"""
    preset = preset or {}
    persona = _persona(preset, semantics, list(texts or []), title)
    base = PERSONAS[persona]
    fonts = _load_fonts(str(fonts_path or _default_fonts_path()))
    primary_ids = fonts.get(persona) or []
    primary = primary_ids[0] if primary_ids else FONT_PLACEHOLDER
    # 辅字体：优先取对比最强的人格（粗黑/圆体），没有登记就退占位
    accent = ""
    for other in ("bold_pop", "cute_round", "elegant_serif"):
        if other != persona and fonts.get(other):
            accent = fonts[other][0]
            break
    if not accent:
        accent = primary_ids[1] if len(primary_ids) > 1 else primary
    rules = dict(base["rules"])
    rules["decor"] = list(base["rules"].get("decor") or [])
    return {
        "persona": persona,
        "primary": primary,
        "accent": accent,
        "weight": int(base["weight"]),
        "skew": int(base["skew"]),
        "rules": rules,
    }


def rules_of(typeface: dict | None) -> dict:
    """取排版规则（缺字段时用 soft_hand 的默认值，保证 director 永远拿到完整表）。"""
    base = dict(PERSONAS["soft_hand"]["rules"])
    if isinstance(typeface, dict) and isinstance(typeface.get("rules"), dict):
        base.update(typeface["rules"])
    return base


def default_fonts_path(plugin_dir) -> str:
    return os.path.join(str(plugin_dir), "static", "fonts", "fonts.json")


def _default_fonts_path() -> str:
    """插件根下的 static/fonts/fonts.json（本文件在 choreo/ 里）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "static", "fonts", "fonts.json")
