"""风格预设（8 套：ballad / pop / rock / edm / cute / ancient / jazz / dark）。

前 3 套（ballad / pop / edm）从演示页 ``chat-2026-10-29-1/_pt3/stage_demo.html``
的三首示意歌搬过来，配色向「二次元」方向调了一档：更高亮度、更干净的粉彩、
描边/柔光。P2 补齐契约枚举里的另外 5 套，每套都写清动作性格、背景池、歌词
权重、默认 ``skin``（只能是 anime_* 四个值）与默认 ``persona``。

``style.typeface`` 的最终值由 ``choreo/typeface.py`` 结合 LLM 主题再定，这里只
是预设默认值。
"""
from __future__ import annotations

from copy import deepcopy

# 段落 → 强度 / 模糊 / 亮度（演示页 SEC_I / SEC_BLUR / SEC_LUM）
SEC_I = {
    "intro": 0.15, "verse": 0.38, "pre": 0.55, "chorus": 0.85,
    "bridge": 0.28, "final": 1.0, "outro": 0.2,
}
SEC_BLUR = {
    "intro": 22, "verse": 13, "pre": 9, "chorus": 5,
    "bridge": 24, "final": 3, "outro": 18,
}
SEC_LUM = {
    "intro": 0.5, "verse": 0.68, "pre": 0.78, "chorus": 1.05,
    "bridge": 0.45, "final": 1.15, "outro": 0.5,
}
SECTION_LABEL = {
    "intro": "前奏", "verse": "主歌", "pre": "导歌", "chorus": "副歌",
    "bridge": "桥段", "final": "最终副歌", "outro": "尾声",
}

# 眼睛能看到的情绪（和演示页 EMO 表同一套）
EMOTIONS = ("joy", "tender", "longing", "sad", "proud", "determined", "playful")

# 每套预设的公共默认值
_BASE = {
    "motionK": 0.7,
    "swayBeats": 3,
    "bounceK": 0.5,
    "dutch": 3,
    "shake": 0.35,
    "easeBeats": 6,
    "desk": {
        "amp_scale": 0.5,
        "gesture_whitelist": ["nod", "tilt", "lean", "look_away", "smile_cam"],
    },
}

PRESETS: dict[str, dict] = {
    "ballad": {
        "key": "ballad",
        "label": "星夜抒情",
        "cn": "抒情",
        "font": '"KaiTi","STKaiti","楷体",serif',
        "weight": 400,
        "skew": 0,
        "fsBase": 0.074,
        "palette": {
            "bgA": "#040817", "bgB": "#141c46",
            "c1": "#4a6cff", "c2": "#9fd8ff", "c3": "#ffd39a",
            "ink": "#f4f8ff", "ink2": "#9cc2ff", "hot": "#ffe0a8",
        },
        "skin": "anime_night",
        "typeface": {
            "persona": "soft_hand",
            "primary": "neko-soft-hand",
            "accent": "neko-elegant-serif",
            "weight": 400, "skew": 0,
            "rules": {
                "vertical_ok": True, "max_chars_per_row": 9,
                "kw_scale": 1.3, "decor": ["outline", "softglow"],
            },
        },
        "motionK": 0.55, "swayBeats": 4, "bounceK": 0.22,
        "dutch": 2.0, "shake": 0.18,
        "signature": "rise",
        "weights": {
            "stagger": {"char": 3, "word": 2, "center": 2, "random": 1, "cue": 3, "whole": 1},
            "reveal": {"rise": 5, "ink": 4, "wipe": 2, "type": 1, "drop": 1,
                       "scatter": 1, "slam": 0, "glitch": 0},
            "layout": {"h": 3, "v": 4, "stack": 2, "arc": 1, "giant": 1, "center": 1},
            "material": {"solid": 3, "grad": 3, "outline": 1, "neon": 1},
            "exit": {"sink": 5, "dissolve": 3, "push": 1, "burst": 1},
        },
        "scenePool": ["bokeh", "wave", "rings", "beams", "led"],
        "emo": {
            "intro": "tender", "verse": "longing", "pre": "tender",
            "chorus": "tender", "bridge": "sad", "final": "joy", "outro": "tender",
        },
        "gazePool": ["far", "down", "camera", "far", "side"],
        "imageryPool": ["stars", "flare", "rain", "petals", "bubble"],
        "desk": {
            "amp_scale": 0.48,
            "gesture_whitelist": ["nod", "tilt", "lean", "look_away", "smile_cam"],
        },
    },
    "pop": {
        "key": "pop",
        "label": "霓虹流行",
        "cn": "流行",
        "font": '"Microsoft YaHei","PingFang SC",sans-serif',
        "weight": 900,
        "skew": -4,
        "fsBase": 0.07,
        "palette": {
            "bgA": "#12021a", "bgB": "#33104a",
            "c1": "#ff4fa8", "c2": "#34e4ff", "c3": "#ffe45c",
            "ink": "#ffffff", "ink2": "#ff6fbd", "hot": "#5ff0ff",
        },
        "skin": "anime_pastel",
        "typeface": {
            "persona": "cute_round",
            "primary": "neko-cute-round",
            "accent": "neko-bold-pop",
            "weight": 900, "skew": -4,
            "rules": {
                "vertical_ok": True, "max_chars_per_row": 8,
                "kw_scale": 1.4, "decor": ["sticker", "outline", "glow"],
            },
        },
        "motionK": 0.85, "swayBeats": 2, "bounceK": 0.9,
        "dutch": 4.0, "shake": 0.45,
        "signature": "slam",
        "weights": {
            "stagger": {"char": 3, "word": 3, "center": 2, "random": 2, "cue": 2, "whole": 1},
            "reveal": {"slam": 4, "wipe": 4, "scatter": 3, "type": 2, "drop": 2,
                       "rise": 1, "ink": 0, "glitch": 1},
            "layout": {"h": 3, "stack": 4, "arc": 2, "v": 1, "giant": 1, "center": 1},
            "material": {"neon": 4, "solid": 2, "outline": 3, "grad": 2},
            "exit": {"burst": 3, "push": 3, "sink": 2, "dissolve": 1},
        },
        "scenePool": ["led", "rings", "tunnel", "beams", "wave"],
        "emo": {
            "intro": "playful", "verse": "playful", "pre": "joy",
            "chorus": "joy", "bridge": "playful", "final": "joy", "outro": "tender",
        },
        "gazePool": ["camera", "side", "camera", "far"],
        "imageryPool": ["heart", "stars", "flare", "confetti", "sparkle"],
        "desk": {
            "amp_scale": 0.58,
            "gesture_whitelist": ["nod", "tilt", "lean", "look_away", "smile_cam", "wave"],
        },
    },
    "edm": {
        "key": "edm",
        "label": "电子脉冲",
        "cn": "电子",
        "font": '"SimHei","Microsoft YaHei",sans-serif',
        "weight": 700,
        "skew": -8,
        "fsBase": 0.068,
        "palette": {
            "bgA": "#03020a", "bgB": "#180a3c",
            "c1": "#8a4bff", "c2": "#c6ff4a", "c3": "#ff3d66",
            "ink": "#f6f2ff", "ink2": "#a86bff", "hot": "#c6ff4a",
        },
        "skin": "anime_neon",
        "typeface": {
            "persona": "tech_mono",
            "primary": "neko-tech-mono",
            "accent": "neko-bold-pop",
            "weight": 700, "skew": -8,
            "rules": {
                "vertical_ok": False, "max_chars_per_row": 10,
                "kw_scale": 1.45, "decor": ["glitch", "glow", "grid"],
            },
        },
        "motionK": 0.95, "swayBeats": 2, "bounceK": 1.1,
        "dutch": 6.0, "shake": 0.9,
        "signature": "glitch",
        "weights": {
            "stagger": {"char": 3, "word": 2, "center": 2, "random": 3, "cue": 2, "whole": 2},
            "reveal": {"glitch": 5, "slam": 3, "wipe": 3, "type": 3, "scatter": 1,
                       "rise": 1, "drop": 0, "ink": 0},
            "layout": {"h": 3, "stack": 3, "v": 1, "arc": 1, "giant": 1, "center": 1},
            "material": {"outline": 3, "neon": 3, "solid": 2, "grad": 1},
            "exit": {"burst": 3, "dissolve": 3, "push": 2, "sink": 1},
        },
        "scenePool": ["tunnel", "led", "beams", "rings", "wave"],
        "emo": {
            "intro": "determined", "verse": "determined", "pre": "proud",
            "chorus": "proud", "bridge": "longing", "final": "proud",
            "outro": "determined",
        },
        "gazePool": ["camera", "camera", "side", "down"],
        "imageryPool": ["digits", "heart", "flare", "grid", "sparkle"],
        "desk": {
            "amp_scale": 0.6,
            "gesture_whitelist": ["nod", "tilt", "lean", "look_away", "smile_cam", "wave"],
        },
    },
    "rock": {
        "key": "rock",
        "label": "摇滚热浪",
        "cn": "摇滚",
        "font": '"Impact","Arial Black",sans-serif',
        "weight": 800,
        "skew": -5,
        "fsBase": 0.072,
        "palette": {
            "bgA": "#0a0403", "bgB": "#2a0b0b",
            "c1": "#ff3b30", "c2": "#ffd166", "c3": "#9aa0a6",
            "ink": "#fff7f2", "ink2": "#ff6b5a", "hot": "#ffcf4a",
        },
        "skin": "anime_ink",
        "typeface": {
            "persona": "bold_pop",
            "primary": "neko-bold-pop",
            "accent": "neko-tech-mono",
            "weight": 800, "skew": -5,
            "rules": {
                "vertical_ok": False, "max_chars_per_row": 8,
                "kw_scale": 1.5, "decor": ["outline", "glow", "shine"],
            },
        },
        "motionK": 1.0, "swayBeats": 2, "bounceK": 1.0,
        "dutch": 5.5, "shake": 0.75,
        "signature": "slam",
        "weights": {
            "stagger": {"char": 3, "word": 3, "center": 2, "random": 2, "cue": 2, "whole": 2},
            "reveal": {"slam": 5, "wipe": 3, "scatter": 2, "type": 2, "drop": 2,
                       "rise": 1, "ink": 0, "glitch": 1},
            "layout": {"h": 3, "stack": 4, "arc": 1, "v": 1, "giant": 2, "center": 1},
            "material": {"solid": 3, "outline": 3, "neon": 2, "grad": 1},
            "exit": {"burst": 3, "push": 3, "sink": 1, "dissolve": 1},
        },
        "scenePool": ["beams", "rings", "tunnel", "led", "wave"],
        "emo": {
            "intro": "determined", "verse": "determined", "pre": "proud",
            "chorus": "proud", "bridge": "longing", "final": "proud",
            "outro": "determined",
        },
        "gazePool": ["camera", "camera", "side", "down"],
        "imageryPool": ["flare", "sparkle", "heart", "grid"],
        "desk": {
            "amp_scale": 0.62,
            "gesture_whitelist": ["nod", "tilt", "lean", "look_away", "smile_cam", "wave"],
        },
    },
    "cute": {
        "key": "cute",
        "label": "糖果心跳",
        "cn": "可爱",
        "font": '"YouYuan","Microsoft YaHei",sans-serif',
        "weight": 700,
        "skew": -2,
        "fsBase": 0.076,
        "palette": {
            "bgA": "#1a0b1a", "bgB": "#4a1b3a",
            "c1": "#ff8fc7", "c2": "#ffd6e8", "c3": "#9fe8ff",
            "ink": "#fff5fb", "ink2": "#ffb3d9", "hot": "#ffe066",
        },
        "skin": "anime_pastel",
        "typeface": {
            "persona": "cute_round",
            "primary": "neko-cute-round",
            "accent": "neko-bold-pop",
            "weight": 800, "skew": -2,
            "rules": {
                "vertical_ok": True, "max_chars_per_row": 8,
                "kw_scale": 1.5, "decor": ["sticker", "outline", "glow"],
            },
        },
        "motionK": 0.9, "swayBeats": 2, "bounceK": 1.2,
        "dutch": 3.5, "shake": 0.5,
        "signature": "scatter",
        "weights": {
            "stagger": {"char": 4, "word": 3, "center": 2, "random": 2, "cue": 2, "whole": 1},
            "reveal": {"rise": 3, "drop": 4, "type": 3, "wipe": 2, "scatter": 4,
                       "slam": 1, "ink": 0, "glitch": 0},
            "layout": {"h": 4, "stack": 3, "arc": 3, "v": 1, "giant": 1, "center": 2},
            "material": {"solid": 3, "grad": 3, "neon": 2, "outline": 2},
            "exit": {"dissolve": 3, "sink": 3, "push": 2, "burst": 1},
        },
        "scenePool": ["bokeh", "wave", "rings", "beams", "led"],
        "emo": {
            "intro": "playful", "verse": "playful", "pre": "joy",
            "chorus": "joy", "bridge": "playful", "final": "joy", "outro": "tender",
        },
        "gazePool": ["camera", "side", "camera", "down"],
        "imageryPool": ["heart", "stars", "confetti", "sparkle", "bubble"],
        "desk": {
            "amp_scale": 0.56,
            "gesture_whitelist": ["nod", "tilt", "lean", "look_away", "smile_cam", "wave"],
        },
    },
    "ancient": {
        "key": "ancient",
        "label": "古风长歌",
        "cn": "古风",
        "font": '"STKaiti","KaiTi","楷体",serif',
        "weight": 500,
        "skew": 0,
        "fsBase": 0.075,
        "palette": {
            "bgA": "#0b0705", "bgB": "#241a12",
            "c1": "#b98a4b", "c2": "#e8d3a9", "c3": "#7f9a8b",
            "ink": "#f7efe2", "ink2": "#cbb18a", "hot": "#d9a441",
        },
        "skin": "anime_ink",
        "typeface": {
            "persona": "brush_ancient",
            "primary": "neko-brush-ancient",
            "accent": "neko-elegant-serif",
            "weight": 500, "skew": 2,
            "rules": {
                "vertical_ok": True, "max_chars_per_row": 7,
                "kw_scale": 1.35, "decor": ["ink", "grain"],
            },
        },
        "motionK": 0.55, "swayBeats": 4, "bounceK": 0.2,
        "dutch": 2.0, "shake": 0.15,
        "signature": "ink",
        "weights": {
            "stagger": {"char": 4, "word": 2, "center": 3, "random": 1, "cue": 2, "whole": 2},
            "reveal": {"ink": 5, "rise": 4, "wipe": 2, "type": 2, "drop": 1,
                       "scatter": 1, "slam": 0, "glitch": 0},
            "layout": {"v": 4, "h": 2, "stack": 2, "arc": 2, "giant": 1, "center": 2},
            "material": {"solid": 3, "grad": 3, "outline": 2, "neon": 0},
            "exit": {"dissolve": 4, "sink": 3, "push": 1, "burst": 0},
        },
        "scenePool": ["bokeh", "wave", "beams", "rings", "led"],
        "emo": {
            "intro": "tender", "verse": "longing", "pre": "tender",
            "chorus": "tender", "bridge": "sad", "final": "proud", "outro": "longing",
        },
        "gazePool": ["down", "far", "side", "camera"],
        "imageryPool": ["moon", "petals", "snow", "rain", "stars"],
        "desk": {
            "amp_scale": 0.46,
            "gesture_whitelist": ["nod", "tilt", "lean", "look_away", "smile_cam"],
        },
    },
    "jazz": {
        "key": "jazz",
        "label": "夜色爵士",
        "cn": "爵士",
        "font": '"Georgia","Times New Roman",serif',
        "weight": 500,
        "skew": 2,
        "fsBase": 0.07,
        "palette": {
            "bgA": "#0a0a12", "bgB": "#241428",
            "c1": "#e0a458", "c2": "#6fb3c9", "c3": "#c96fb0",
            "ink": "#fbf6ee", "ink2": "#d8c3a5", "hot": "#f0b75e",
        },
        "skin": "anime_night",
        "typeface": {
            "persona": "elegant_serif",
            "primary": "neko-elegant-serif",
            "accent": "neko-soft-hand",
            "weight": 500, "skew": 0,
            "rules": {
                "vertical_ok": True, "max_chars_per_row": 11,
                "kw_scale": 1.25, "decor": ["shadow", "grain"],
            },
        },
        "motionK": 0.65, "swayBeats": 3, "bounceK": 0.35,
        "dutch": 3.0, "shake": 0.22,
        "signature": "wipe",
        "weights": {
            "stagger": {"char": 3, "word": 3, "center": 2, "random": 2, "cue": 3, "whole": 1},
            "reveal": {"wipe": 4, "rise": 3, "ink": 3, "type": 3, "drop": 1,
                       "scatter": 1, "slam": 0, "glitch": 0},
            "layout": {"h": 3, "v": 3, "stack": 2, "arc": 2, "giant": 1, "center": 1},
            "material": {"grad": 4, "solid": 3, "outline": 1, "neon": 1},
            "exit": {"dissolve": 4, "sink": 3, "push": 1, "burst": 1},
        },
        "scenePool": ["bokeh", "beams", "rings", "wave", "led"],
        "emo": {
            "intro": "tender", "verse": "longing", "pre": "playful",
            "chorus": "tender", "bridge": "sad", "final": "joy", "outro": "tender",
        },
        "gazePool": ["side", "camera", "far", "down"],
        "imageryPool": ["rain", "stars", "bubble", "petals"],
        "desk": {
            "amp_scale": 0.5,
            "gesture_whitelist": ["nod", "tilt", "lean", "look_away", "smile_cam"],
        },
    },
    "dark": {
        "key": "dark",
        "label": "暗夜回声",
        "cn": "暗黑",
        "font": '"Microsoft YaHei","PingFang SC",sans-serif',
        "weight": 800,
        "skew": -3,
        "fsBase": 0.07,
        "palette": {
            "bgA": "#05050a", "bgB": "#12121f",
            "c1": "#7a1f3d", "c2": "#3a3f6b", "c3": "#9aa7c7",
            "ink": "#e8e6f2", "ink2": "#8a7f9e", "hot": "#b03a5b",
        },
        "skin": "anime_ink",
        "typeface": {
            "persona": "elegant_serif",
            "primary": "neko-elegant-serif",
            "accent": "neko-tech-mono",
            "weight": 800, "skew": -3,
            "rules": {
                "vertical_ok": True, "max_chars_per_row": 10,
                "kw_scale": 1.3, "decor": ["shadow", "grain"],
            },
        },
        "motionK": 0.6, "swayBeats": 3, "bounceK": 0.3,
        "dutch": 3.5, "shake": 0.3,
        "signature": "ink",
        "weights": {
            "stagger": {"char": 3, "word": 2, "center": 2, "random": 2, "cue": 3, "whole": 2},
            "reveal": {"ink": 4, "glitch": 3, "rise": 3, "wipe": 2, "type": 1,
                       "drop": 2, "scatter": 1, "slam": 1},
            "layout": {"h": 3, "v": 3, "stack": 3, "arc": 1, "giant": 1, "center": 2},
            "material": {"solid": 4, "outline": 2, "grad": 2, "neon": 1},
            "exit": {"dissolve": 4, "sink": 4, "push": 1, "burst": 1},
        },
        "scenePool": ["bokeh", "tunnel", "rings", "beams", "led"],
        "emo": {
            "intro": "sad", "verse": "longing", "pre": "determined",
            "chorus": "determined", "bridge": "sad", "final": "proud",
            "outro": "sad",
        },
        "gazePool": ["down", "far", "side", "closed"],
        "imageryPool": ["rain", "snow", "moon", "bubble"],
        "desk": {
            "amp_scale": 0.44,
            "gesture_whitelist": ["nod", "tilt", "lean", "look_away"],
        },
    },
}

def normalize(name: str) -> str:
    """预设名 → 真正支持的 key；不认识的返回空串。"""
    key = str(name or "").strip().lower()
    return key if key in PRESETS else ""


def get(name: str) -> dict:
    """取一套预设（返回深拷贝，调用方可以安全改）。"""
    key = normalize(name) or "pop"
    preset = PRESETS[key]
    merged = deepcopy(_BASE)
    merged.update(deepcopy(preset))
    # 预设自己带的 desk 覆盖公共默认值
    merged["desk"] = {**_BASE["desk"], **(preset.get("desk") or {})}
    merged["secIntensity"] = dict(SEC_I)
    merged["secBlur"] = dict(SEC_BLUR)
    merged["secLum"] = dict(SEC_LUM)
    return merged


# 主题词 → 预设（LLM 主题/歌名/关键词里命中就优先，权重最高）
_THEME_HINTS = (
    ("ancient", ("古风", "江湖", "剑", "山河", "长安", "红尘", "侠", "诗酒", "青衫")),
    ("cute", ("可爱", "糖果", "初恋", "恋爱", "少女", "撒娇", "约会", "萌", "元气")),
    ("dark", ("黑暗", "深渊", "孤独", "绝望", "破碎", "埋葬", "坠落")),
    ("rock", ("摇滚", "呐喊", "不服", "燃烧", "嘶吼", "冲破", "热血")),
)


def auto_pick(bpm: float, energy_mean: float, energy_var: float,
              theme: str = "") -> str:
    """没有人工指定时按（主题词、速度、能量）选一套，8 套都可能被选中。"""
    bpm = float(bpm or 100.0)
    mean = max(0.0, min(1.0, float(energy_mean or 0.5)))
    var = max(0.0, float(energy_var or 0.0))
    text = str(theme or "").lower()
    for key, words in _THEME_HINTS:
        if any(word.lower() in text for word in words):
            return key
    if bpm < 92:
        return "dark" if mean < 0.34 else "ballad"
    if bpm < 112:
        if mean <= 0.5 and var < 0.05:
            return "jazz"
        return "ballad" if mean <= 0.46 else "pop"
    if bpm >= 138 or (bpm >= 128 and mean >= 0.7):
        return "edm"
    if bpm >= 118 and mean >= 0.6:
        return "rock"
    if mean <= 0.46:
        return "ballad"
    return "pop"
