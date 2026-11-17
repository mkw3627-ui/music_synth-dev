"""导演：歌曲分析 + 歌词 + seed → show.json。

把演示页 ``stage_demo.html`` 的 ``buildShow`` 规则改写成 Python，并换成真实
歌曲的段落/拍点/歌词时间轴。所有随机量都从 seed 派生，同一份输入永远得到
同一份 show.json（``generated_at`` 是生成时间，不参与内容哈希）。

契约见 ``舞台编排实施计划_v3.md`` 第 3 节（冻结），改字段先报资助。
"""
from __future__ import annotations

import bisect
import math
import random
import re
import time

from . import GENERATOR, presets, quality
from . import typeface as typeface_mod

# 歌词入场的提前量 / 入场时长（演示页 leadBy / dur）
_REVEAL_LEAD = {
    "rise": None,   # 由 beat 决定，见 _lead_for
    "ink": None,
    "wipe": None,
    "type": 0.3,
    "drop": None,
    "scatter": None,
    "slam": None,
    "glitch": None,
}
_REVEAL_DUR = {
    "rise": None, "ink": 0.85, "slam": 0.36, "scatter": 0.9,
    "glitch": 0.5, "type": 0.06, "drop": 0.7, "wipe": 0.45,
}

_DIMS = ("stagger", "reveal", "layout", "material", "exit")

_PUNCT = re.compile(r"[\s，。！？、,.!?;；:：'\"“”‘’—\-…（）()\[\]{}<>《》~·]")
_META = re.compile(
    r"^\s*(原唱|作词|作曲|编曲|制作人|监制|混音|母带|吉他|贝斯|鼓|和声|"
    r"录音|出品|发行|策划|OP|SP|词|曲)\s*[:：]")
_META_ANY = re.compile(r"(原唱|作词|作曲|编曲|制作人|混音|母带)\s*[:：]")

_CJK = re.compile(r"[㐀-䶿一-鿿぀-ヿㇰ-ㇿ]")
_STOP_CJK = set("的了着过是我你他她它在有和就都也没不这那个们吗呢吧啊呀哦嗯"
                "一二三四五六七八九十上下来去说到要想会能可很太又还只把被让给")
_EN_STOP = {
    "the", "and", "you", "your", "for", "with", "that", "this", "are", "was",
    "were", "have", "has", "had", "not", "but", "all", "can", "will", "just",
    "from", "they", "them", "his", "her", "him", "she", "out", "one", "two",
    "get", "got", "let", "its", "our", "who", "what", "when", "where", "how",
    "why", "into", "over", "than", "then", "some", "more", "very", "too",
}

_IMAGERY = {
    "星": "stars", "光": "flare", "阳": "flare", "火": "flare", "燃": "flare",
    "心": "heart", "爱": "heart", "雨": "rain", "泪": "rain", "水": "rain",
    "海": "wave", "风": "wind", "花": "petals", "梦": "bubble", "云": "clouds",
    "夜": "night", "月": "moon", "雪": "snow", "电": "sparkle", "闪": "sparkle",
    "0": "digits", "1": "digits", "数": "digits",
}


# ---------------------------------------------------------------- 小工具

def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def _fract(value: float) -> float:
    return value - math.floor(value)


def _h01(n: float) -> float:
    """和演示页同一个哈希（0..1），保证不同实现能对齐。"""
    return _fract(math.sin(float(n) * 127.1 + 311.7) * 43758.5453)


def _weighted(rng: random.Random, weights: dict) -> str:
    keys = list(weights.keys())
    total = sum(max(0.0, float(weights[k])) for k in keys)
    if total <= 0:
        return keys[0] if keys else ""
    pick = rng.random() * total
    for key in keys:
        pick -= max(0.0, float(weights[key]))
        if pick <= 0:
            return key
    return keys[-1]


def _fmt_sec(value: float) -> float:
    return round(float(value), 3)


# ---------------------------------------------------------------- 歌词准备

def _strip_text(text: str) -> str:
    return _PUNCT.sub("", str(text or "")).strip()


def _is_metadata(text: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return True
    if _META.search(raw):
        return True
    if len(raw) <= 24 and raw.startswith("《") and raw.endswith("》"):
        return True
    if len(raw) <= 24 and "插曲" in raw and ("《" in raw or "》" in raw):
        return True
    return False


def _display_title(track_title: str) -> str:
    text = str(track_title or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in text:
            text = text.rsplit(sep, 1)[-1]
            break
    text = re.sub(r"[(（\[【].*?[)）\]】]", "", text).strip()
    return text or str(track_title or "").strip()


def scan_keywords(texts: list[str], title: str = "") -> list[str]:
    """P1 无 LLM：用重复出现的 2~3 字词组 / 英文单词当关键词。"""
    from collections import Counter
    counter: Counter = Counter()
    for raw in texts:
        text = _strip_text(raw)
        if not text:
            continue
        run = []
        for ch in text:
            if _CJK.match(ch):
                run.append(ch)
            else:
                for n in (2, 3):
                    for i in range(0, max(0, len(run) - n + 1)):
                        gram = "".join(run[i:i + n])
                        if any(stop in gram for stop in _STOP_CJK):
                            continue
                        counter[gram] += 1
                run = []
        for n in (2, 3):
            for i in range(0, max(0, len(run) - n + 1)):
                gram = "".join(run[i:i + n])
                if any(stop in gram for stop in _STOP_CJK):
                    continue
                counter[gram] += 1
        for word in re.findall(r"[A-Za-z]{3,}", text):
            low = word.lower()
            if low not in _EN_STOP:
                counter[low] += 1

    # 歌名里的词加权：歌名通常就是主题
    title_text = _strip_text(_display_title(title))
    for n in (2, 3):
        for i in range(0, max(0, len(title_text) - n + 1)):
            gram = title_text[i:i + n]
            if all(_CJK.match(c) for c in gram):
                counter[gram] += 2

    ranked = sorted(counter.items(), key=lambda kv: (kv[1], len(kv[0])), reverse=True)
    picked = [word for word, count in ranked if count >= 2][:10]
    if len(picked) < 4:
        for word, _count in ranked:
            if word not in picked:
                picked.append(word)
            if len(picked) >= 8:
                break
    # 去掉被更长的关键词包含的短词
    result = []
    for word in picked:
        if any(word != other and word in other for other in picked):
            continue
        result.append(word)
    return result[:10]


def _extract_lines(timed: list[dict], cues: list, duration: float,
                   beat: float) -> list[dict]:
    """带时间戳的歌词行 + 逐字 cue → 供导演使用的行。"""
    rows = []
    for row in timed or []:
        if not isinstance(row, dict):
            continue
        raw_at = row.get("at_s", row.get("t0"))
        try:
            t0 = max(0.0, float(raw_at))
        except (TypeError, ValueError):
            continue
        text = str(row.get("text") or "").strip()
        if not text or _is_metadata(text):
            continue
        # 歌词文件比音频长（拿到的是完整版/别的版本的歌词）：超出曲长的行丢掉，不能让整首编排校验失败
        if duration and t0 >= duration - 0.5:
            continue
        if not re.search(r"[A-Za-z0-9㐀-鿿぀-ヿ]", text):
            continue
        rows.append({"t0": t0, "text": text})
    rows.sort(key=lambda item: item["t0"])
    for idx, row in enumerate(rows):
        row["_next"] = rows[idx + 1]["t0"] if idx + 1 < len(rows) else duration

    # 只保留时间窗内的 cue
    parsed_cues = []
    for cue in cues or []:
        if not isinstance(cue, (list, tuple)) or len(cue) < 4:
            continue
        try:
            start, end = float(cue[2]), float(cue[3])
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = start + 0.12
        parsed_cues.append({
            "char": str(cue[0] or ""),
            "start": start,
            "end": end,
            "stress": float(cue[5]) if len(cue) > 5 and cue[5] is not None else 0.2,
        })
    parsed_cues.sort(key=lambda item: item["start"])

    for row in rows:
        t0, next_t0 = row["t0"], row["_next"]
        in_window = [c for c in parsed_cues
                     if t0 - 0.22 <= c["start"] < max(t0 + 0.1, next_t0 - 0.04)]
        row["_cues"] = in_window
        if in_window:
            row["t1"] = min(max(t0 + 0.6, max(c["end"] for c in in_window) + 0.12),
                            max(t0 + 0.8, next_t0 - 0.04))
        else:
            row["t1"] = min(t0 + max(1.0, len(_strip_text(row["text"])) * 0.4),
                            max(t0 + 1.0, next_t0 - 0.04))
        if row["t1"] <= t0:
            row["t1"] = t0 + 1.0
    return rows


def _line_chars(text: str, cues: list[dict], t0: float, t1: float,
                beat: float, keywords: list[str]) -> list[list]:
    """把一行文字摊到时间轴上：CJK 优先跟 cue 对齐，英文按字符权重铺。"""
    chars = [c for c in str(text or "") if not _PUNCT.match(c)]
    chars = [c for c in chars if c.strip()]
    if not chars:
        return []
    kw_flags = [0] * len(chars)
    for kw in keywords:
        start = "".join(chars).find(kw)
        if start >= 0:
            for i in range(start, start + len(kw)):
                if i < len(kw_flags):
                    kw_flags[i] = 1

    cue_aligned = False
    aligned: list = []
    if cues:
        cue_chars = [c["char"] for c in cues]
        best_off, best_hit = 0, 1.0
        for off in range(0, max(1, len(cue_chars))):
            window = cue_chars[off:off + len(chars)]
            if not window:
                continue
            hits = sum(1 for a, b in zip(chars, window) if a == b)
            ratio = hits / max(1, len(window))
            if ratio > best_hit:
                best_off, best_hit = off, ratio
        if best_hit >= 0.6 and len(cue_chars) - best_off >= len(chars):
            aligned = cues[best_off:best_off + len(chars)]
            cue_aligned = len(aligned) == len(chars)

    out: list[list] = []
    if cue_aligned:
        for i, ch in enumerate(chars):
            cue = aligned[i]
            start = max(t0 - 0.05, float(cue["start"]))
            end = max(start + 0.12, float(cue["end"]))
            if end > t1 + 0.35:
                end = t1 + 0.35
            stress = _clamp(float(cue.get("stress") or 0.2) + 0.15 * kw_flags[i])
            out.append([ch, _fmt_sec(start), _fmt_sec(end),
                        round(stress, 3), kw_flags[i]])
        return out

    span_start = float(t0)
    if cues:
        span_start = max(span_start, float(cues[0]["start"]))
    span_end = float(t1)
    if span_end <= span_start:
        span_end = span_start + max(0.8, len(chars) * 0.36)
    weights = []
    for ch in chars:
        if _CJK.match(ch):
            weights.append(1.0)
        elif ch.isdigit():
            weights.append(0.62)
        else:
            weights.append(0.58)
    total = sum(weights) or 1.0
    onsets = [float(c["start"]) for c in cues]
    cursor = span_start
    prev_end = span_start - 1.0
    for i, ch in enumerate(chars):
        share = (span_end - span_start) * weights[i] / total
        start = cursor
        # 附近有真实起音就吸附过去，让逐字点亮跟着唱。只吸附到上一个字之后的起音：
        # 以前会吸回同一个（更早的）起音，英文一句里每个字母都挤在同一 0.1 s，后面整句嘴不动
        best = None
        for onset in onsets:
            if onset < prev_end - 0.01:
                continue
            if abs(onset - start) <= 0.16 and (best is None or abs(onset - start) < abs(best - start)):
                best = onset
        if best is not None:
            start = best
        start = max(start, prev_end)
        end = min(span_end, start + share * 0.94)
        if end <= start:
            end = start + 0.12
        prev_end = end
        on_beat = abs(_fract((start - t0) / max(0.1, beat))) < 0.12
        stress = _clamp(0.28 + (0.32 if on_beat else 0.0) + 0.4 * kw_flags[i])
        out.append([ch, _fmt_sec(start), _fmt_sec(end), round(stress, 3), kw_flags[i]])
        cursor = max(cursor + share, start + share * 0.5)
    return out


# ---------------------------------------------------------------- 情绪 / 视线

def _emotion_for(text: str, section: str, preset: dict,
                 mood: tuple[float, float, float, float] | None) -> tuple[str, float, float, float]:
    v, a, d, conf = mood if mood else (0.0, 0.0, 0.0, 0.0)
    default = preset["emo"].get(section, "tender")
    if conf <= 0.12:
        return default, float(v), float(a), float(d)
    if v >= 0.35 and d >= 0.4:
        emo = "playful"
    elif v >= 0.3:
        emo = "joy"
    elif v <= -0.35 and a >= 0.15:
        emo = "determined"
    elif v <= -0.22:
        emo = "sad"
    elif v <= -0.02 and a < 0.1:
        emo = "longing"
    elif d >= 0.45 and a >= 0.2:
        emo = "proud"
    elif v > 0.0:
        emo = "tender"
    else:
        emo = default
    return emo, float(v), float(a), float(d)


def _address_of(text: str) -> str:
    low = str(text or "").lower()
    if "你" in low or re.search(r"\byou\b", low):
        return "you"
    if "我" in low or re.search(r"\b(i|me|my)\b", low):
        return "self"
    return "world"


# ---------------------------------------------------------------- 主流程

def build(track: dict, analysis: dict, timed: list[dict], cues: list,
          line_moods: list | None = None, *, seed: int | None = None,
          preset_name: str | None = None, input_hash: str = "",
          generator: str = GENERATOR,
          semantics: dict | None = None) -> dict:
    """生成一份 show.json（dict）。

    ``semantics`` 是 ``choreo.semantics.analyze()`` 的返回值。只有
    ``source == "llm"`` 时才采用逐行数据；兜底（``fallback``）时完全走 P1 原
    路径（``line_moods`` 关键词表），保证失败也不改变既有行为。
    """
    track = track or {}
    duration = float(track.get("duration_s") or analysis.get("duration")
                     or analysis.get("music_duration") or 0.0)
    if duration <= 0:
        duration = float(analysis.get("duration") or 0.0)
    if duration <= 0:
        raise ValueError("曲长未知")
    bpm = float(analysis.get("bpm") or 100.0)
    beat = float(analysis.get("beat") or (60.0 / max(1.0, bpm)))
    bar = beat * 4.0

    track_id = str(track.get("id") or "")
    if seed is None:
        seed = int(__import__("hashlib").sha256(track_id.encode("utf-8"))
                   .hexdigest()[:8], 16)
    seed = int(seed) & 0x7FFFFFFF
    rng = random.Random(seed)

    rows = _extract_lines(timed, cues, duration, beat)
    texts = [row["text"] for row in rows]
    keywords = scan_keywords(texts, str(track.get("title") or ""))
    title = str(track.get("title") or "")
    sem = dict(semantics or {}) if str((semantics or {}).get("source") or "") == "llm" else {}
    sem_lines = list(sem.get("lines") or [])
    theme_hint = " ".join([str(sem.get("theme") or ""), title, "".join(keywords[:4])])
    preset = presets.get(preset_name or presets.auto_pick(
        bpm, analysis.get("energy_mean", 0.5), analysis.get("energy_var", 0.0),
        theme=theme_hint))

    # 第 10 维：整首一套字体人格（预设 × LLM 主题 × 歌词内容）
    typeface_out = typeface_mod.build(preset, sem or None, texts, title)
    tf_rules = typeface_mod.rules_of(typeface_out)

    sections = _prepare_sections(analysis, duration, preset)
    section_by_id = {}
    for seg in sections:
        section_by_id.setdefault(seg["id"], []).append(seg)

    # ---- 每行基础信息 ----
    mood_rows = list(line_moods or [])
    lines: list[dict] = []
    for index, row in enumerate(rows):
        text = row["text"]
        t0, t1 = row["t0"], row["t1"]
        seg = _section_at(sections, t0)
        section_id = seg["id"] if seg else "verse"
        sem_line = sem_lines[index] if index < len(sem_lines) else None
        if not isinstance(sem_line, dict):
            sem_line = None
        line_kws = [kw for kw in keywords if kw and kw in text][:2]
        if sem_line:
            sem_kws = [str(kw) for kw in (sem_line.get("keywords") or [])
                       if str(kw) and str(kw) in text]
            line_kws = sem_kws[:3] or line_kws
        chars = _line_chars(text, row["_cues"], t0, t1, beat, line_kws)
        if len(chars) < 1:
            continue
        mood = _mood_at(mood_rows, t0)
        emotion, valence, arousal, dominance = _emotion_for(text, section_id, preset, mood)
        intensity = _line_intensity(t0, t1, seg, analysis, preset, bool(line_kws))
        address = _address_of(text)
        image = ""
        if sem_line:
            emotion = str(sem_line.get("emotion") or emotion)
            try:
                valence = float(sem_line.get("valence", valence))
                arousal = float(sem_line.get("arousal", arousal))
            except (TypeError, ValueError):
                pass
            dominance = arousal
            # 语义强度与音乐能量各占一半：LLM 不知道副歌有多响，能量也不知道歌词多重
            try:
                intensity = 0.5 * intensity + 0.5 * float(sem_line.get("intensity"))
            except (TypeError, ValueError):
                pass
            address = str(sem_line.get("address") or address)
            image = str(sem_line.get("image") or "")
        gaze = _gaze_for(rng, preset, section_id, index, lines)
        lines.append({
            "i": len(lines),
            "t0": _fmt_sec(t0),
            "t1": _fmt_sec(t1),
            "text": text,
            "section": section_id,
            "chars": chars,
            "emotion": emotion,
            "valence": round(valence, 3),
            "arousal": round(arousal, 3),
            "dominance": round(dominance, 3),
            "intensity": round(_clamp(intensity), 3),
            "keywords": line_kws,
            "address": address,
            "gaze": gaze,
            "_image": image,
            "_idxInSec": 0,
        })

    # 同段内的序号
    counts: dict[str, int] = {}
    for line in lines:
        counts[line["section"]] = counts.get(line["section"], 0) + 1
        line["_idxInSec"] = counts[line["section"]] - 1

    # ---- 重复句 ----
    seen: dict[str, int] = {}
    for line in lines:
        key = _strip_text(line["text"]).lower()
        if key and key in seen:
            line["lyric"] = None  # 占位，下面统一填
            line["_repeatOf"] = seen[key]
        else:
            if key:
                seen[key] = line["i"]
            line["_repeatOf"] = None

    # ---- 歌词组合（9 维语法 + 约束）----
    hist: list[dict] = []
    first_combo: dict[int, dict] = {}
    for line in lines:
        if line.get("_repeatOf") is not None:
            base = first_combo.get(int(line["_repeatOf"])) or {
                "stagger": "cue", "reveal": preset["signature"],
                "layout": "h", "material": "neon", "exit": "burst",
            }
            combo = dict(base)
            combo.update({"echo": True, "boost": 1.12})
            if combo.get("material") != "neon":
                combo["material"] = "neon"
            if combo.get("exit") == "sink":
                combo["exit"] = "burst"
            if line["_idxInSec"] == 0:
                # 前半首（非 final）的副歌入口不用最强 reveal（slam），最强的留给 final
                entry_early = float(line["t0"]) < duration * 0.5 and line["section"] != "final"
                entry_reveal = "rise" if entry_early else (
                    "slam" if preset["signature"] == "rise" else preset["signature"])
                combo.update({
                    "layout": "giant",
                    "front": True,
                    "reveal": entry_reveal,
                    "stagger": "cue",
                })
        elif line["section"] == "bridge" and line["_idxInSec"] == 0:
            combo = {"stagger": "char", "reveal": "rise", "layout": "center",
                     "material": "solid", "exit": "dissolve",
                     "slow": True, "front": True}
        else:
            early = float(line["t0"]) < duration * 0.5 and line["section"] != "final"
            combo = _choose_combo(rng, preset, line, hist, early=early)
            if line["section"] in ("chorus", "final") and line["_idxInSec"] == 0:
                combo["layout"] = "giant"
                # 前半首（非 final）的副歌入口不用 signature（有的预设 signature 就是 slam），
                # 否则会绕过 _choose_combo 的前半首预算
                if float(line["t0"]) < duration * 0.5 and line["section"] != "final":
                    combo["reveal"] = "rise"
                else:
                    combo["reveal"] = preset["signature"]
                if combo["reveal"] == "type":
                    combo["stagger"] = "cue"
        _constrain_layout(line, combo, tf_rules)
        line["_combo"] = combo
        first_combo[line["i"]] = combo
        hist.append(combo)

    # ---- 歌名 / 尾声关键词（特殊行）----
    specials = _special_lines(lines, sections, preset, duration, bar, beat,
                              keywords, str(track.get("title") or ""), rng)
    all_lines = sorted(lines + specials, key=lambda item: (item["t0"], item["i"]))
    for new_i, line in enumerate(all_lines):
        line["i"] = new_i

    # ---- 入场 / 退场时间 ----
    clears = [seg["t0"] for seg in sections
              if seg["id"] in ("chorus", "bridge", "final", "outro")]
    for line in all_lines:
        _apply_entrance(line, clears, rng, beat, seed)

    # ---- 强度曲线 ----
    hop = float((analysis.get("energy") or {}).get("hop") or 0.1)
    energy_v = list((analysis.get("energy") or {}).get("v") or [])
    intensity_v = _intensity_curve(sections, energy_v, hop, duration, preset)

    # ---- 背景场景 ----
    scenes = _scenes(sections, preset, rng, analysis)

    # ---- 镜头 ----
    camera = _camera(sections, lines, preset, rng, beat, bar, duration)
    prev_role = ""
    for line in all_lines:
        shot = _shot_at(camera, line["t0"])
        line["lyric"] = _lyric_payload(line, shot, preset, rng, prev_role)
        prev_role = str(line["lyric"].get("font_role") or "")
        line["motion"] = {"amp": round(_motion_amp(preset, line, shot), 3),
                          "sway": round(0.6 + 0.8 * line["intensity"], 3),
                          "bounce": round(preset.get("bounceK", 0.5) * (0.4 + line["intensity"]), 3)}

    # ---- 事件 ----
    events = _events(analysis, all_lines, sections, preset, seed, beat, bar, duration)

    # ---- 清理内部字段 ----
    clean_lines = []
    for line in all_lines:
        out = {k: v for k, v in line.items() if not k.startswith("_")}
        out.setdefault("exit_at", out["t1"])
        clean_lines.append(out)

    style = {
        "preset": preset["key"],
        "theme": _theme_sentence(sem, preset, keywords, title),
        "imagery": _imagery_of(keywords, preset, sem),
        "palette": _palette_for(preset, str(sem.get("palette_hint") or "")),
        "skin": preset["skin"],
        "typeface": typeface_out,
        "signature": [preset["signature"], "outline"],
    }
    music = {
        "bpm": round(bpm, 3),
        "beats": _round_list(analysis.get("beats") or []),
        "downbeats": _round_list(analysis.get("downbeats") or []),
        "sections": sections,
        "drops": [round(float(x), 3) for x in (analysis.get("drops") or [])],
        "energy": {"hop": round(hop, 4), "v": [round(float(x), 4) for x in energy_v]},
    }
    desk = {
        "amp_scale": float(preset.get("desk", {}).get("amp_scale") or 0.5),
        "gesture_whitelist": list(preset.get("desk", {}).get("gesture_whitelist") or []),
        "events_ref": "events",
    }
    return {
        "schema": "neko.show/1",
        "track_id": track_id,
        "seed": seed,
        "input_hash": str(input_hash or ""),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "generator": generator,
        "duration": round(duration, 3),
        "style": style,
        "music": music,
        "intensity": {"hop": round(hop, 4), "v": intensity_v},
        "lines": clean_lines,
        "events": events,
        "camera": camera,
        "scenes": scenes,
        "desk": desk,
    }


# ---------------------------------------------------------------- 段落

def _prepare_sections(analysis: dict, duration: float, preset: dict) -> list[dict]:
    raw = analysis.get("sections") or []
    sections = []
    counts: dict[str, int] = {}
    for seg in raw:
        sid = str(seg.get("id") or "verse")
        if sid not in presets.SECTION_LABEL:
            sid = "verse"
        repeat = counts.get(sid, 0)
        counts[sid] = repeat + 1
        t0 = float(seg.get("t0") or 0.0)
        t1 = float(seg.get("t1") or duration)
        if t1 <= t0:
            continue
        sections.append({
            "id": sid,
            "label": presets.SECTION_LABEL[sid],
            "t0": _fmt_sec(max(0.0, t0)),
            "t1": _fmt_sec(min(duration, t1)),
            "energy": round(float(seg.get("energy") or 0.0), 3),
            "repeat": repeat,
        })
    if not sections:
        sections = [{"id": "verse", "label": "主歌", "t0": 0.0,
                     "t1": _fmt_sec(duration), "energy": 0.5, "repeat": 0}]
    sections.sort(key=lambda item: item["t0"])
    return sections


def _section_at(sections: list[dict], t: float) -> dict | None:
    for seg in sections:
        if float(seg["t0"]) - 1e-6 <= t < float(seg["t1"]) - 1e-6:
            return seg
    if sections:
        return sections[-1] if t >= float(sections[-1]["t0"]) else sections[0]
    return None


def _round_list(values) -> list[float]:
    return [round(float(v), 3) for v in values]


# ---------------------------------------------------------------- 情绪 / 强度

def _mood_at(moods: list, t: float):
    for row in moods:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            start, stop, v, a, d, conf = (float(row[0]), float(row[1]), float(row[2]),
                                          float(row[3]), float(row[4]), float(row[5]))
        except (TypeError, ValueError):
            continue
        if start - 0.05 <= t < stop + 0.05:
            return (v, a, d, conf)
    return None


def _line_intensity(t0: float, t1: float, seg: dict | None, analysis: dict,
                    preset: dict, has_keyword: bool) -> float:
    sid = (seg or {}).get("id", "verse")
    base = float(preset["secIntensity"].get(sid, 0.4))
    if sid == "pre" and seg:
        span = max(0.1, float(seg["t1"]) - float(seg["t0"]))
        base = _lerp(0.48, 0.78, _clamp((t0 - float(seg["t0"])) / span))
    mid = (t0 + t1) * 0.5
    energy = _sample_energy(analysis, mid)
    value = _clamp(0.68 * base + 0.32 * energy)
    if has_keyword:
        value = _clamp(value + 0.05)
    return value


def _sample_energy(analysis: dict, t: float) -> float:
    data = analysis.get("energy") or {}
    values = data.get("v") or []
    hop = float(data.get("hop") or 0.1)
    if not values:
        return 0.5
    idx = int(round(t / max(0.01, hop)))
    if idx < 0:
        return float(values[0])
    if idx >= len(values):
        return float(values[-1])
    return float(values[idx])


def _gaze_for(rng: random.Random, preset: dict, section: str, index: int,
              lines: list[dict]) -> str:
    pool = preset.get("gazePool") or ["camera"]
    gaze = pool[int(rng.random() * len(pool)) % len(pool)]
    idx_in_sec = sum(1 for line in lines if line["section"] == section)
    if section in ("chorus", "final") and idx_in_sec in (0, 3):
        gaze = "camera"
    if section == "bridge":
        gaze = "closed"
    if section == "pre" and idx_in_sec == 1:
        gaze = "camera"
    return gaze


def _choose_combo(rng: random.Random, preset: dict, line: dict,
                  hist: list[dict], early: bool = False) -> dict:
    weights = preset["weights"]
    signature = preset["signature"]
    last = None
    for _ in range(60):
        reveal_w = dict(weights["reveal"])
        reveal_w[signature] = float(reveal_w.get(signature, 1.0)) * 1.6
        if line["intensity"] < 0.45:
            reveal_w["slam"] = 0
            reveal_w["drop"] = float(reveal_w.get("drop", 1.0)) * 0.3
        if line["intensity"] > 0.8:
            reveal_w["type"] = float(reveal_w.get("type", 1.0)) * 0.3
        combo = {
            "stagger": _weighted(rng, weights["stagger"]),
            "reveal": _weighted(rng, reveal_w),
            "layout": _weighted(rng, weights["layout"]),
            "material": _weighted(rng, weights["material"]),
            "exit": _weighted(rng, weights["exit"]),
        }
        if combo["reveal"] == "type":
            combo["stagger"] = "cue"
        # v2 §5.1 新鲜感预算：前半首（非 final 段）只花掉冲击力 < 4 的元素，
        # 最强的（glitch/slam，冲击力 ≥ 4）留给后半首与最后一次副歌
        if early and quality.IMPACT.get("reveal:" + combo["reveal"], 0) >= 4:
            continue
        last = combo
        if line["intensity"] < 0.45 and combo["exit"] == "burst":
            continue
        prev = hist[-1] if hist else None
        if prev:
            diff = sum(1 for key in _DIMS if combo[key] != prev.get(key))
            if diff < 3:
                continue
        if any(item.get("reveal") == combo["reveal"] for item in hist[-2:]):
            continue
        return combo
    fallback = last or {"stagger": "cue", "reveal": signature, "layout": "h",
                        "material": "solid", "exit": "sink"}
    # 循环耗尽也不能违反前半首预算：兜底换成冲击力 < 4 的 reveal
    if early and quality.IMPACT.get("reveal:" + fallback["reveal"], 0) >= 4:
        fallback = dict(fallback, reveal="rise")
    return fallback


# ---------------------------------------------------------------- 特殊行

def _special_lines(lines: list[dict], sections: list[dict], preset: dict,
                   duration: float, bar: float, beat: float,
                   keywords: list[str], track_title: str,
                   rng: random.Random) -> list[dict]:
    out: list[dict] = []
    intro = next((s for s in sections if s["id"] == "intro"), None)
    outro = next((s for s in sections if s["id"] == "outro"), None)
    first_line = min((line["t0"] for line in lines), default=duration)
    title = _display_title(track_title)
    # 开场钩子：歌名逐字从深处浮出；太短的前奏就不放，免得压到第一句
    if title and first_line >= 2.4 and len(title) <= 24:
        start = max(0.55, (intro["t0"] if intro else 0.0) + bar * 0.55)
        end = min(first_line - 0.5, start + len(title) * beat * 0.8 + beat)
        if end - start >= 1.0:
            chars = []
            step = max(0.12, (end - start) / max(1, len(title)))
            for i, ch in enumerate(title):
                if _PUNCT.match(ch) or ch.isspace():
                    continue
                cs = start + i * step
                chars.append([ch, _fmt_sec(cs), _fmt_sec(cs + max(0.3, beat * 0.55)), 0.5, 0])
            if chars:
                out.append({
                    "i": -1, "t0": _fmt_sec(chars[0][1]),
                    "t1": _fmt_sec(chars[-1][2]), "text": title, "section": "intro",
                    "chars": chars, "emotion": "tender", "valence": 0.1,
                    "arousal": 0.0, "dominance": 0.0, "intensity": 0.2,
                    "keywords": [], "address": "world", "gaze": "far",
                    "kind": "title",
                    "_idxInSec": 0,
                    "_combo": {"stagger": "char", "reveal": "rise", "layout": "center",
                               "material": "grad", "exit": "dissolve",
                               "slow": True, "big": True, "front": True},
                })
    # 尾声：全曲关键词在深处依次闪回
    if outro and keywords:
        top = [kw for kw in keywords[:4] if len(kw) <= 10]
        span = max(0.0, float(outro["t1"]) - float(outro["t0"]))
        if span >= 4.0 and top:
            step = min(span * 0.9 / len(top), bar * 1.4)
            start = float(outro["t0"]) + max(0.6, beat)
            for j, kw in enumerate(top):
                t0 = start + j * step
                if t0 >= duration - 1.0:
                    break
                chars = []
                for i, ch in enumerate(kw):
                    cs = t0 + i * 0.08
                    chars.append([ch, _fmt_sec(cs), _fmt_sec(cs + 0.3), 0.4, 0])
                out.append({
                    "i": -2 - j, "t0": _fmt_sec(t0), "t1": _fmt_sec(t0 + max(0.6, bar)),
                    "text": kw, "section": "outro", "chars": chars,
                    "emotion": preset["emo"].get("outro", "tender"),
                    "valence": 0.0, "arousal": 0.0, "dominance": 0.0,
                    "intensity": 0.2, "keywords": [kw], "address": "world",
                    "gaze": "far", "kind": "flash", "_idxInSec": j,
                    "_combo": {"stagger": "center", "reveal": "rise", "layout": "h",
                               "material": "grad", "exit": "dissolve",
                               "slow": True},
                    "_pos": [round(0.2 + 0.6 * rng.random(), 3),
                             round(0.22 + 0.48 * rng.random(), 3)],
                })
    return out


# ---------------------------------------------------------------- 入场 / 退场

def _lead_for(reveal: str, beat: float, slow: bool) -> float:
    base = {
        "rise": max(0.9, beat * 2), "ink": beat * 2, "wipe": beat * 1.5,
        "type": 0.3, "drop": beat * 1.5, "scatter": beat * 2,
        "slam": beat, "glitch": beat * 1.2,
    }.get(reveal, 1.0)
    return base * (1.6 if slow else 1.0)


def _apply_entrance(line: dict, clears: list[float], rng: random.Random,
                    beat: float, seed: int) -> None:
    combo = line.setdefault("_combo", {})
    chars = line.get("chars") or []
    n = len(chars)
    if not n:
        line.setdefault("exit_at", line["t1"])
        return
    reveal = combo.get("reveal", "rise")
    lead = _lead_for(reveal, beat, bool(combo.get("slow")))
    appear = float(line["t0"]) - lead
    dur = _REVEAL_DUR.get(reveal)
    if dur is None:
        dur = max(0.7, lead * 0.7)
    stagger = "cue" if reveal == "type" else combo.get("stagger", "char")
    spread = max(0.05, lead - dur * 0.5)
    for i, ch in enumerate(chars):
        cs, ce = float(ch[1]), float(ch[2])
        if line.get("kind") == "title":
            es, ed = cs - 0.5, 1.3
        elif stagger == "cue":
            es, ed = cs - dur * 0.9 - 0.03, dur
        elif reveal == "wipe":
            es, ed = appear + (i / max(1, n)) * lead * 0.85, 0.35
        else:
            if stagger == "char":
                k = i / max(1, n - 1)
            elif stagger == "word":
                k = _word_k(chars, i)
            elif stagger == "center":
                k = abs(i - (n - 1) / 2) / max(0.5, (n - 1) / 2)
            elif stagger == "random":
                k = _h01(seed * 0.01 + line["i"] * 31 + i * 13.7)
            else:
                k = 0.0
            es, ed = appear + k * spread, dur
        ch[1] = _fmt_sec(float(ch[1]))
        ch[2] = _fmt_sec(float(ch[2]))
        line.setdefault("_vis", []).append((_fmt_sec(es), _fmt_sec(ed)))
    line["_vis0"] = _fmt_sec(min(v[0] for v in line["_vis"]) - 0.05)
    exit_at = float(chars[-1][2]) + 0.15
    line["exit_at"] = _fmt_sec(exit_at)
    next_clear = next((t for t in clears if t > exit_at + 0.1), None)
    if combo.get("exit") == "sink":
        linger = 3.0 + rng.random() * 4.0
        fade_at = exit_at + linger
        kill_at = next_clear if next_clear and next_clear < fade_at else None
        line["fade_at"] = _fmt_sec(fade_at)
        line["kill_at"] = _fmt_sec(kill_at) if kill_at is not None else None
        line["gone_at"] = _fmt_sec((kill_at + 0.8) if kill_at else (fade_at + 1.3))
    else:
        line["gone_at"] = _fmt_sec(exit_at + 0.9)


def _word_k(chars: list, i: int) -> float:
    # chars 是扁平列表；没有词边界信息，按「同一段连续 ASCII / CJK」粗分
    return i / max(1, len(chars) - 1)


# ---------------------------------------------------------------- 强度曲线

def _intensity_curve(sections: list[dict], energy_v: list, hop: float,
                     duration: float, preset: dict) -> list[float]:
    n = max(1, int(math.ceil(duration / max(0.01, hop))) + 1)
    out = []
    for i in range(n):
        t = i * hop
        seg = _section_at(sections, t)
        sid = (seg or {}).get("id", "verse")
        base = float(preset["secIntensity"].get(sid, 0.4))
        if sid == "pre" and seg:
            span = max(0.1, float(seg["t1"]) - float(seg["t0"]))
            base = _lerp(0.48, 0.78, _clamp((t - float(seg["t0"])) / span))
        es = float(energy_v[i]) if i < len(energy_v) else 0.5
        value = _clamp(0.64 * base + 0.36 * es)
        if seg:
            value = _clamp(value + 0.015 * int(seg.get("repeat") or 0))
        out.append(round(value, 4))
    # 平滑：三段滑动平均，避免逐帧跳变
    if len(out) >= 3:
        smooth = [out[0]]
        for i in range(1, len(out) - 1):
            smooth.append(round((out[i - 1] + out[i] * 2 + out[i + 1]) / 4.0, 4))
        smooth.append(out[-1])
        out = smooth
    return out


# ---------------------------------------------------------------- 场景

def _scenes(sections: list[dict], preset: dict, rng: random.Random,
            analysis: dict) -> list[dict]:
    pool = list(preset["scenePool"])
    chosen: dict[str, str] = {}
    prev = None
    # 第三十八轮：背景转场 14 种，每段按候选随机挑（同一首相邻段不重复）
    ENTRY_CAND = {
        "intro": ["iris", "diamond", "clock", "fade"],
        "verse": ["sweepL", "sweepR", "columns", "diag", "blinds", "dots", "sweepU"],
        "pre": ["sweepR", "iris", "split", "diag", "sweepD", "dots"],
        "chorus": ["flash", "diamond", "split", "clock", "iris"],
        "final": ["flash", "diamond", "clock", "split"],
        "bridge": ["fade", "blinds", "sweepD", "dots"],
        "outro": ["fade", "iris", "sweepU"],
    }
    entries_seq = []
    def _entry_for(sid):
        cand = [e for e in ENTRY_CAND.get(sid, ["fade"]) if not entries_seq or e != entries_seq[-1]] or ["fade"]
        e = cand[int(rng.random() * len(cand)) % len(cand)]
        entries_seq.append(e)
        return e
    entries = {}
    out = []
    for seg in sections:
        sid = seg["id"]
        if sid == "final" and "chorus" in chosen:
            scene = chosen["chorus"]
        elif sid == "outro" and "intro" in chosen:
            scene = chosen["intro"]
        else:
            used = set(chosen.values())
            candidates = [x for x in pool if x != prev and x not in used]
            scene = candidates[int(rng.random() * len(candidates)) % len(candidates)] \
                if candidates else pool[int(rng.random() * len(pool)) % len(pool)]
        # 副歌要「灯亮了」的场景：演唱会 / 彩带 / 夜城 / 光束类
        if sid in ("chorus", "final") and scene not in ("concert", "confetti", "skyline", "beams", "led", "tunnel", "starstage", "neoncity", "lantern"):
            preferred = [x for x in pool if x in ("concert", "confetti", "skyline", "beams", "led", "tunnel", "starstage", "neoncity", "lantern") and x != prev]
            if preferred:
                scene = preferred[0]
        chosen[sid] = scene
        prev = scene
        out.append({
            "section": sid,
            "t0": _fmt_sec(seg["t0"]),
            "t1": _fmt_sec(seg["t1"]),
            "scene": scene,
            "entry": _entry_for(sid),
            "blur": float(preset["secBlur"].get(sid, 12)),
            "lum": float(preset["secLum"].get(sid, 0.8)),
            "density": 1.45 if sid == "final" else (1.1 if sid == "chorus" else 1.0),
            "var": round(rng.random(), 4),
        })
    return out


# ---------------------------------------------------------------- 镜头

def _camera(sections: list[dict], lines: list[dict], preset: dict,
            rng: random.Random, beat: float, bar: float,
            duration: float) -> list[dict]:
    dutch = float(preset.get("dutch") or 0.0)
    shake = float(preset.get("shake") or 0.0)
    ease = int(preset.get("easeBeats") or 6)
    shots: list[dict] = []

    def add(t: float, kind: str, cx: float, *, cut: bool = False,
            rot: float = 0.0, shake_amt: float = 0.0, ease_beats: int | None = None):
        if t < -0.01 or t > duration + 0.01:
            return
        shots.append({
            "t": _fmt_sec(t), "kind": kind, "cx": round(_clamp(cx), 3),
            "rot": round(rot, 2), "cut": bool(cut),
            "ease_beats": int(ease if ease_beats is None else ease_beats),
            "shake": round(float(shake_amt), 3),
        })

    def side() -> float:
        return 0.36 if rng.random() < 0.5 else 0.64

    add(0.0, "wide", 0.5, cut=True, ease_beats=4)
    if duration > bar * 4:
        add(bar, "base", 0.5, ease_beats=12)

    for seg in sections:
        sid = seg["id"]
        t0, t1 = float(seg["t0"]), float(seg["t1"])
        span = max(bar, t1 - t0)
        if sid == "verse":
            sec_lines = [line for line in lines if line["section"] == "verse"
                         and t0 - 0.3 <= line["t0"] < t1]
            flip = 0.36
            # 镜头的「近/中」配对也吃 seed：同一种段落不同 seed 会得到不同的
            # 近景/中景起手，但仍然是近中交替，整体节奏不变。
            invert = rng.random() < 0.5
            for j in range(0, max(1, len(sec_lines)), 2):
                st = sec_lines[j]["t0"] if sec_lines else t0 + j * 2 * bar
                kind = "close" if (j // 2) % 2 == 0 else "medium"
                if invert:
                    kind = "medium" if kind == "close" else "close"
                if j // 2 == 2:
                    flip = 0.64 if flip < 0.5 else 0.36
                add(st, kind, flip, cut=(j == 0 or rng.random() < 0.5),
                    ease_beats=6)
            if not sec_lines:
                add(t0, "medium", 0.5, cut=True)
        elif sid == "pre":
            add(t0, "medium", 0.5, cut=True)
            add(t0 + span * 0.55, "close", 0.5, ease_beats=8, rot=0.0)
        elif sid in ("chorus", "final"):
            final = sid == "final"
            points = [0.0, 0.28, 0.56, 0.8]
            for j, frac in enumerate(points):
                t = t0 + span * frac
                rot = (rng.random() * 2 - 1) * dutch
                if j == 0:
                    add(t, "base", 0.5, cut=True, shake_amt=shake * (1.0 if final else 0.5))
                elif j == 1:
                    # 二、四拍位的景别由 seed 决定：同一个副歌换 seed 会换「推近/拉开」的顺序。
                    add(t, rng.choice(("medium", "close")), side(), cut=True,
                        rot=(rot if final else 0.0), shake_amt=shake * 0.6)
                elif j == 2:
                    add(t, rng.choice(("base", "wide")), side(), cut=True,
                        rot=rot, shake_amt=shake)
                else:
                    add(t, rng.choice(("medium", "close", "wide")), 0.5,
                        ease_beats=6, shake_amt=shake * 0.5)
            if final and span > bar * 4:
                for k in range(1, 4):
                    t = t0 + bar * k * 2
                    if t < t1:
                        add(t, "medium" if rng.random() < 0.5 else "base",
                            side(), cut=True, rot=-dutch, shake_amt=shake)
        elif sid == "bridge":
            add(t0, "wide", 0.5, cut=True)
            add(t0 + span * 0.55, "base", 0.5, ease_beats=8)
        elif sid == "outro":
            add(t0, "base", 0.5, ease_beats=4)
            add(t0 + span * 0.45, "wide", 0.5, ease_beats=12)
        elif sid == "intro":
            if span > bar * 3:
                add(t0 + bar * 2, "base", 0.5, ease_beats=8)

    shots.sort(key=lambda shot: shot["t"])
    deduped = []
    for shot in shots:
        if deduped and shot["t"] - deduped[-1]["t"] < 0.2:
            continue
        deduped.append(shot)
    if not deduped:
        deduped = [{"t": 0.0, "kind": "wide", "cx": 0.5, "rot": 0.0,
                    "cut": True, "ease_beats": 4, "shake": 0.0}]
    deduped[0]["t"] = 0.0
    deduped[0]["cut"] = True
    return deduped


def _shot_at(camera: list[dict], t: float) -> dict:
    current = camera[0]
    for shot in camera:
        if float(shot["t"]) <= t + 1e-6:
            current = shot
        else:
            break
    return current


# ---------------------------------------------------------------- 行载荷

def _lyric_payload(line: dict, shot: dict, preset: dict,
                   rng: random.Random, prev_role: str = "") -> dict:
    combo = line.get("_combo") or {}
    reveal = combo.get("reveal", "rise")
    layout = combo.get("layout", "h")
    cx = float(shot.get("cx") or 0.5)
    front = bool(combo.get("front"))
    if line.get("kind") == "flash" and "_pos" in line:
        pos = list(line["_pos"])
    elif front:
        pos = [0.5, 0.55]
    elif layout == "v":
        pos = [0.22 if cx > 0.55 else (0.78 if cx < 0.45 else 0.5), 0.5]
    elif layout == "center":
        pos = [0.5, 0.52]
    elif layout == "giant":
        pos = [0.5, 0.58]
    else:
        pos = [0.28 if cx > 0.55 else (0.72 if cx < 0.45 else 0.5), 0.62]
    # 字体角色（第 10 维）：重材质/巨幕行用辅字体；相邻行避免同角色，形成节奏
    role = "accent" if (combo.get("material") in ("neon", "grad")
                        or layout == "giant") else "primary"
    if prev_role and role == prev_role and layout != "giant" \
            and line.get("kind") not in ("title", "flash"):
        role = "accent" if prev_role == "primary" else "primary"
    return {
        "reveal": reveal,
        "stagger": combo.get("stagger", "char"),
        "layout": layout,
        "material": combo.get("material", "solid"),
        "exit": combo.get("exit", "sink"),
        "font_role": role,
        "pos": [round(float(pos[0]), 3), round(float(pos[1]), 3)],
        "front": front,
        "echo": bool(combo.get("echo")),
        "boost": round(float(combo.get("boost") or 1.0), 3),
        "repeat_of": line.get("_repeatOf"),
        "slow": bool(combo.get("slow")),
        "big": bool(combo.get("big")),
    }


def _motion_amp(preset: dict, line: dict, shot: dict) -> float:
    base = float(preset.get("motionK") or 0.7)
    kind_scale = {"close": 0.75, "medium": 0.9, "base": 1.0, "wide": 0.85}.get(
        str(shot.get("kind") or "base"), 1.0)
    return _clamp(base * (0.35 + 0.75 * float(line["intensity"])) * kind_scale,
                  0.05, 1.6)


# ---------------------------------------------------------------- 事件

def _events(analysis: dict, lines: list[dict], sections: list[dict],
            preset: dict, seed: int, beat: float, bar: float,
            duration: float) -> list[dict]:
    events: list[dict] = []
    vocal = analysis.get("vocal") or {}
    # 契约的事件枚举里没有 vibrato：颤音并进 long_note，用 arg 区分，
    # 前端照样能把它当成「长音上的细颤」来处理。
    for key in ("long_note", "high_note", "inhale", "phrase_end", "vibrato"):
        for item in vocal.get(key) or []:
            events.append({
                "t": round(float(item.get("t") or 0.0), 3),
                "type": "long_note" if key == "vibrato" else key,
                "dur": round(float(item.get("dur") or 0.0), 3),
                "amp": round(float(item.get("amp") or 0.4), 3),
                "arg": "vibrato" if key == "vibrato" else str(item.get("arg") or ""),
            })

    # 逐行：关键词、句尾眨眼、重读冲击、意象
    for line in lines:
        if line.get("kind") in ("title", "flash"):
            continue
        text = str(line.get("text") or "")
        for i, ch in enumerate(line.get("chars") or []):
            if len(ch) >= 5 and ch[4]:
                events.append({"t": round(float(ch[1]), 3), "type": "keyword",
                               "dur": 0.0, "amp": round(float(ch[3]), 3),
                               "arg": str(ch[0])})
                im = _IMAGERY.get(str(ch[0]))
                if im and (str(ch[0]) in "".join(line.get("keywords") or [])
                           or float(line["intensity"]) > 0.55):
                    events.append({"t": round(float(ch[1]), 3), "type": "imagery",
                                   "dur": 0.0, "amp": 0.6, "arg": im})
        # LLM 逐行意象：落在契约已有的 imagery 事件上
        image = str(line.get("_image") or "")
        for ch in image:
            token = _IMAGERY.get(ch)
            if token:
                events.append({"t": round(float(line["t0"]) + 0.05, 3),
                               "type": "imagery", "dur": 0.0, "amp": 0.55,
                               "arg": token})
                break
        events.append({"t": round(float(line.get("exit_at") or line["t1"]) + 0.08, 3),
                       "type": "blink", "dur": 0.0, "amp": 0.5, "arg": ""})

    # 段落事件：副歌进入、闪白、黑场、冲击
    chorus_starts = [seg["t0"] for seg in sections if seg["id"] in ("chorus", "final")]
    for idx, t in enumerate(chorus_starts):
        events.append({"t": round(float(t), 3), "type": "chorus_in",
                       "dur": 0.0, "amp": 0.9, "arg": ""})
        events.append({"t": round(float(t), 3), "type": "flash",
                       "dur": 0.0, "amp": 0.85 if idx == 0 else 1.0, "arg": ""})
        for k in range(4):
            tt = float(t) + k * 2 * bar
            if tt < duration:
                events.append({"t": round(tt, 3), "type": "punch",
                               "dur": 0.0, "amp": 0.035 if preset["key"] == "ballad" else 0.05,
                               "arg": ""})
    final = next((seg for seg in sections if seg["id"] == "final"), None)
    # 开场钩子兜底：第一句太早（< 2.4 s）时歌名浮出放不下，就在曲首放一个短黑场
    # （v2 §5.2 ①：前 5 秒一个反常规画面）。它在第一句之前，不压歌词。
    first_line_t = min((float(line["t0"]) for line in lines), default=duration)
    if first_line_t < 2.4:
        events.append({"t": 0.0, "type": "black", "dur": round(beat * 0.5, 3),
                       "amp": 1.0, "arg": ""})
    if final:
        events.append({"t": round(max(0.0, float(final["t0"]) - beat * 0.5), 3),
                       "type": "black", "dur": round(beat * 0.5, 3),
                       "amp": 1.0, "arg": ""})
    if preset["key"] == "edm" and chorus_starts:
        events.append({"t": round(max(0.0, chorus_starts[0] - beat * 0.5), 3),
                       "type": "black", "dur": round(beat * 0.5, 3),
                       "amp": 1.0, "arg": ""})

    for t in analysis.get("drops") or []:
        events.append({"t": round(float(t), 3), "type": "drop",
                       "dur": 0.0, "amp": 1.0, "arg": ""})

    # 器乐间奏：相邻两句之间超过 2.5 秒
    real = [line for line in lines if line.get("kind") not in ("title", "flash")]
    for a, b in zip(real, real[1:]):
        gap = float(b["t0"]) - float(a.get("exit_at") or a["t1"])
        if gap >= 2.5:
            events.append({"t": round(float(a.get("exit_at") or a["t1"]) + 0.2, 3),
                           "type": "inst_break", "dur": round(gap - 0.2, 3),
                           "amp": round(_clamp(gap / 6.0, 0.3, 1.0), 3), "arg": ""})
    if real:
        events.append({"t": round(float(real[-1]["t0"]), 3), "type": "last_line",
                       "dur": 0.0, "amp": 0.7, "arg": ""})

    # 手势：桌面档按脚本触发；同一时刻只给一个主要动作
    # 段落起点不一定在拍上：时刻吸到最近一拍（P5，对拍率），仍限在本段内
    whitelist = list(preset.get("desk", {}).get("gesture_whitelist") or ["nod"])
    beats = sorted(float(x) for x in (analysis.get("beats") or []))
    for seg in sections:
        if seg["id"] in ("intro", "outro"):
            continue
        count = max(1, int((float(seg["t1"]) - float(seg["t0"])) / max(1.0, bar * 4)))
        for k in range(count):
            t = _snap_to_beat(float(seg["t0"]) + k * bar * 4, beats,
                              float(seg["t0"]), float(seg["t1"]))
            if t >= duration:
                break
            arg = whitelist[(k + int(seed)) % len(whitelist)]
            events.append({"t": round(t, 3), "type": "gesture",
                           "dur": round(bar * 0.8, 3), "amp": 0.5, "arg": arg})

    # 待机眨眼：间奏里也要活
    occupied = [(line["t0"] - 0.2, float(line.get("exit_at") or line["t1"]))
                for line in real]
    t = 1.5
    while t < duration:
        if not any(a <= t <= b for a, b in occupied):
            events.append({"t": round(t, 3), "type": "blink", "dur": 0.0,
                           "amp": 0.4, "arg": ""})
        t += 2.8 + 2.3 * _h01(t + seed)

    events.sort(key=lambda item: (item["t"], item["type"]))
    # 去掉同一时刻完全重复的事件
    deduped = []
    for event in events:
        if deduped and event == deduped[-1]:
            continue
        deduped.append(event)
    return deduped


def _snap_to_beat(t: float, beats: list[float], lo: float, hi: float) -> float:
    """最近的一拍（只取 [lo, hi) 里的）；没有拍点就原样返回。"""
    i = bisect.bisect_left(beats, t)
    near = [beats[j] for j in (i - 1, i)
            if 0 <= j < len(beats) and lo <= beats[j] < hi]
    return min(near, key=lambda b: abs(b - t)) if near else t


# ---------------------------------------------------------------- 其它

def _imagery_of(keywords: list[str], preset: dict,
                sem: dict | None = None) -> list[str]:
    out: list[str] = []
    # LLM 给的意象词先落到运行时认识的 token 上
    for word in (sem or {}).get("imagery") or []:
        for ch in str(word):
            im = _IMAGERY.get(ch)
            if im and im not in out:
                out.append(im)
            if len(out) >= 4:
                break
    for kw in keywords:
        for ch in kw:
            im = _IMAGERY.get(ch)
            if im and im not in out:
                out.append(im)
    for im in preset.get("imageryPool") or []:
        if im not in out:
            out.append(im)
        if len(out) >= 4:
            break
    return out[:4]


def _theme_sentence(sem: dict | None, preset: dict, keywords: list[str],
                    title: str) -> str:
    """一句话主题：LLM 有就给 LLM 的（已截成单句），否则按预设+关键词拼一句。"""
    theme = str((sem or {}).get("theme") or "").strip()
    if theme:
        return theme[:80]
    subject = "、".join([kw for kw in keywords[:2] if kw]) or "心事"
    name = _display_title(title) or preset.get("label") or "这首歌"
    return f"「{name}」唱的是{subject}里的{preset.get('cn') or '流行'}心绪"


def _palette_for(preset: dict, hint: str) -> dict:
    """调色板接入 LLM 的冷暖倾向：主色不动，辅色 ink2 在预设的 c1/c2/c3 里选。

    只做可逆的小幅选择，不凭空生成颜色（真正的配色由 T2-B 的皮肤资产决定）。
    """
    palette = dict(preset.get("palette") or {})
    text = str(hint or "")
    warm = any(word in text for word in ("暖", "火", "金", "橙", "粉", "甜", "阳光", "黄"))
    cool = any(word in text for word in ("冷", "蓝", "夜", "青", "紫", "月", "冰", "暗"))
    if warm and not cool and palette.get("c3"):
        palette["ink2"] = palette["c3"]
    elif cool and not warm and palette.get("c2"):
        palette["ink2"] = palette["c2"]
    return palette


def _constrain_layout(line: dict, combo: dict, rules: dict) -> None:
    """字体人格的排版约束：一行超过 max_chars_per_row 就不许横排硬塞。"""
    if line.get("kind") in ("title", "flash"):
        return
    text = str(line.get("text") or "")
    try:
        limit = int(rules.get("max_chars_per_row") or 10)
    except (TypeError, ValueError):
        limit = 10
    if len(text) <= limit:
        return
    layout = combo.get("layout")
    if layout in ("giant", "center", "arc", "stack", "v"):
        return
    if len(text) > limit * 1.7:
        combo["layout"] = "stack"
    elif rules.get("vertical_ok"):
        combo["layout"] = "v"
    else:
        combo["layout"] = "arc"


def default_seed(track: dict) -> int:
    """没人工给 seed 时，用曲目 id（没有就用音频路径）派生的稳定 31 位整数。"""
    import hashlib
    key = str((track or {}).get("id") or (track or {}).get("audio") or "")
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF


def fonts_fingerprint() -> str:
    """static/fonts/fonts.json 的内容指纹（各字体 file + sha256 的组合）。

    fonts.json 缺文件 / 不可读 / 无字体时返回空串 → ``input_fingerprint`` 走
    不含字体的旧路径，与 P1 兼容。字体内容变（sha256 变）或增删字体都会改变
    本指纹，从而让缓存失效；字体不变则指纹不变、缓存继续命中。
    """
    import hashlib
    import json
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "static" / "fonts" / "fonts.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    fonts = data.get("fonts") if isinstance(data, dict) else None
    if not isinstance(fonts, list) or not fonts:
        return ""
    parts = []
    for item in fonts:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file") or "")
        sha = str(item.get("sha256") or "")
        if file or sha:
            parts.append(f"{file}:{sha}")
    if not parts:
        return ""
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def input_fingerprint(*, audio: str, stem: str, lyrics_key: str,
                      cues: int, seed: int, preset: str,
                      audio_mtime: float = 0.0, stem_mtime: float = 0.0,
                      semantics: str = "") -> str:
    """缓存判断用的输入指纹（sha256 前 16 位）。

    ``semantics`` 是歌词语义的来源标记（``llm:<模型>`` / ``fallback``），
    由 ``choreo.semantics.tag_for()`` 读侧车文件给出；传空串时与 P1 的指纹
    完全一致（旧缓存不会失效）。

    字体指纹（``static/fonts/fonts.json`` 各条 sha256）由 ``fonts_fingerprint()``
    自动纳入：装好/改动字体后，同一输入也会让缓存失效、换上新字体人格；字体
    不变则指纹不变。写入端（worker）与读取端（_show_cached）都调本函数，两端
    天然一致，不会出现一端带字体一端不带的错配。
    """
    import hashlib
    import os
    try:
        size = os.path.getsize(audio) if audio else 0
    except OSError:
        size = 0
    parts = [
        str(audio), str(size), f"{audio_mtime:.3f}",
        str(stem), f"{stem_mtime:.3f}",
        str(lyrics_key), str(cues), str(seed), str(preset), GENERATOR,
    ]
    if semantics:
        parts.append(str(semantics))
    fonts_hash = fonts_fingerprint()
    if fonts_hash:
        parts.append("fonts:" + fonts_hash)
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
