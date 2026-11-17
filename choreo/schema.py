#!/usr/bin/env python
"""show.json 契约校验（neko.show/1）。

单独跑（``-I`` 下也不会去 import 同目录的包，所以本文件只用标准库）：

    python -I choreo/schema.py <show.json>
    python -I choreo/schema.py --lenient <show.json>     # 字体/皮肤缺失只告警

退出码：0 = 通过；1 = 有错误；2 = 文件读不了/不是 JSON。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

SCHEMA = "neko.show/1"

PRESETS = {"ballad", "pop", "rock", "edm", "cute", "ancient", "jazz", "dark"}
SKINS = {"anime_pastel", "anime_night", "anime_neon", "anime_ink"}
PERSONAS = {"cute_round", "soft_hand", "elegant_serif", "bold_pop",
            "tech_mono", "brush_ancient"}
SECTION_IDS = {"intro", "verse", "pre", "chorus", "bridge", "final", "outro"}
REVEALS = {"rise", "ink", "wipe", "type", "drop", "scatter", "slam", "glitch"}
STAGGERS = {"char", "word", "center", "random", "cue", "whole"}
LAYOUTS = {"h", "v", "stack", "arc", "giant", "center"}
MATERIALS = {"solid", "outline", "neon", "grad"}
EXITS = {"sink", "burst", "push", "dissolve"}
EMOTIONS = {"joy", "tender", "longing", "sad", "proud", "determined", "playful"}
GAZES = {"camera", "far", "down", "side", "closed"}
ADDRESSES = {"self", "you", "world"}
EVENT_TYPES = {
    "long_note", "high_note", "inhale", "phrase_end", "keyword", "chorus_in",
    "drop", "inst_break", "last_line", "gesture", "blink", "flash", "black",
    "punch", "shake", "imagery",
}
SIGNATURE_TOKENS = ({*REVEALS} | {*MATERIALS} |
                   {"outline", "glow", "softglow", "shadow", "echo", "shine",
                    "sticker", "grain", "grid", "glitch", "sparkle"})
SHOT_KINDS = {"close", "medium", "base", "wide"}
ENTRIES = {"iris", "sweepL", "sweepR", "columns", "flash", "fade",
           "sweepU", "sweepD", "diag", "diamond", "blinds", "dots", "split", "clock"}


class _Ctx:
    def __init__(self, strict: bool = True):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.strict = strict

    def err(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(f"{path}: {message}")

    def need(self, obj, key: str, path: str, types=None, *, optional=False):
        if not isinstance(obj, dict):
            self.err(path, "不是对象")
            return None
        if key not in obj:
            if optional:
                return None
            self.err(f"{path}.{key}", "缺少字段")
            return None
        value = obj[key]
        if types is not None and not isinstance(value, types):
            # bool 是 int 的子类：数字字段单独判断
            if not (isinstance(value, bool) and bool in (types if isinstance(types, tuple) else (types,))):
                self.err(f"{path}.{key}", f"类型不对（{type(value).__name__}）")
                return None
        return value

    def number(self, obj, key: str, path: str, *, optional=False,
               low=None, high=None):
        value = self.need(obj, key, path, (int, float), optional=optional)
        if value is None or isinstance(value, bool):
            return None
        value = float(value)
        if not math.isfinite(value):
            self.err(f"{path}.{key}", "不是有限数")
            return None
        if low is not None and value < low:
            self.err(f"{path}.{key}", f"小于下限 {low}")
        if high is not None and value > high:
            self.err(f"{path}.{key}", f"大于上限 {high}")
        return value

    def enum(self, obj, key: str, path: str, allowed: set, *, optional=False):
        value = self.need(obj, key, path, str, optional=optional)
        if value is None:
            return None
        if value not in allowed:
            self.err(f"{path}.{key}", f"不在枚举里：{value!r}")
        return value

    def arr(self, obj, key: str, path: str, *, optional=False):
        value = self.need(obj, key, path, list, optional=optional)
        if value is None:
            return None
        if not isinstance(value, list):
            self.err(f"{path}.{key}", "不是数组")
            return None
        return value


def _check_time(ctx: _Ctx, obj, key: str, path: str, duration: float,
                *, optional=False):
    value = ctx.number(obj, key, path, optional=optional)
    if value is None:
        return None
    if value < -0.5:
        ctx.err(f"{path}.{key}", "时间为负")
    if duration and value > duration + 1.0:
        ctx.err(f"{path}.{key}", f"超出曲长 {duration:.1f}s")
    return value


def _check_style(ctx: _Ctx, style: dict, path: str) -> None:
    ctx.enum(style, "preset", path, PRESETS)
    if "theme" in style and not isinstance(style["theme"], str):
        ctx.err(f"{path}.theme", "不是字符串")
    imagery = ctx.arr(style, "imagery", path, optional=True)
    if imagery is not None:
        for i, item in enumerate(imagery):
            if not isinstance(item, str):
                ctx.err(f"{path}.imagery[{i}]", "不是字符串")
    palette = ctx.need(style, "palette", path, dict)
    if isinstance(palette, dict):
        for key in ("bgA", "bgB", "c1", "c2", "c3", "ink", "ink2", "hot"):
            ctx.need(palette, key, f"{path}.palette", str)
    skin = ctx.need(style, "skin", path, str, optional=not ctx.strict)
    if skin is not None:
        if skin not in SKINS:
            if ctx.strict:
                ctx.err(f"{path}.skin", f"不在枚举里：{skin!r}")
            else:
                ctx.warn(f"{path}.skin", f"未知皮肤：{skin!r}")
    elif not ctx.strict:
        ctx.warn(f"{path}.skin", "缺字段（lenient 只告警）")
    typeface = ctx.need(style, "typeface", path, dict, optional=not ctx.strict)
    if isinstance(typeface, dict):
        ctx.enum(typeface, "persona", f"{path}.typeface", PERSONAS)
        for key in ("primary", "accent"):
            ctx.need(typeface, key, f"{path}.typeface", str)
        ctx.number(typeface, "weight", f"{path}.typeface", low=100, high=1000)
        ctx.number(typeface, "skew", f"{path}.typeface", low=-45, high=45)
        rules = ctx.need(typeface, "rules", f"{path}.typeface", dict)
        if isinstance(rules, dict):
            ctx.need(rules, "vertical_ok", f"{path}.typeface.rules", bool)
            ctx.number(rules, "max_chars_per_row", f"{path}.typeface.rules", low=1, high=40)
            ctx.number(rules, "kw_scale", f"{path}.typeface.rules", low=0.5, high=3.0)
            ctx.arr(rules, "decor", f"{path}.typeface.rules")
    elif not ctx.strict:
        ctx.warn(f"{path}.typeface", "缺字段（lenient 只告警）")
    signature = ctx.arr(style, "signature", path, optional=True)
    if signature is not None:
        for i, item in enumerate(signature):
            if item not in SIGNATURE_TOKENS:
                ctx.err(f"{path}.signature[{i}]", f"未知签名动效：{item!r}")


def _check_music(ctx: _Ctx, music: dict, path: str, duration: float) -> None:
    ctx.number(music, "bpm", path, low=30, high=260)
    beats = ctx.arr(music, "beats", path)
    if beats is not None:
        last = -1e9
        for i, value in enumerate(beats):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                ctx.err(f"{path}.beats[{i}]", "不是数字")
                continue
            if float(value) + 1e-6 < last:
                ctx.err(f"{path}.beats[{i}]", "拍点不是递增的")
            last = float(value)
    downbeats = ctx.arr(music, "downbeats", path)
    if downbeats is not None:
        for i, value in enumerate(downbeats):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                ctx.err(f"{path}.downbeats[{i}]", "不是数字")
    sections = ctx.arr(music, "sections", path)
    if sections is not None:
        if not sections:
            ctx.err(f"{path}.sections", "为空")
        for i, seg in enumerate(sections):
            sp = f"{path}.sections[{i}]"
            if not isinstance(seg, dict):
                ctx.err(sp, "不是对象")
                continue
            ctx.enum(seg, "id", sp, SECTION_IDS)
            ctx.need(seg, "label", sp, str)
            t0 = _check_time(ctx, seg, "t0", sp, duration)
            t1 = _check_time(ctx, seg, "t1", sp, duration)
            if t0 is not None and t1 is not None and t1 < t0:
                ctx.err(sp, "t1 < t0")
            ctx.number(seg, "energy", sp, low=0, high=1.5)
            repeat = ctx.need(seg, "repeat", sp, int)
            if isinstance(repeat, bool):
                ctx.err(f"{sp}.repeat", "不是整数")
    drops = ctx.arr(music, "drops", path)
    if drops is not None:
        for i, value in enumerate(drops):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                ctx.err(f"{path}.drops[{i}]", "不是数字")
    energy = ctx.need(music, "energy", path, dict)
    if isinstance(energy, dict):
        ctx.number(energy, "hop", f"{path}.energy", low=0.001)
        values = ctx.arr(energy, "v", f"{path}.energy")
        if values is not None and not values:
            ctx.err(f"{path}.energy.v", "为空")


def _check_intensity(ctx: _Ctx, intensity: dict, path: str) -> None:
    ctx.number(intensity, "hop", path, low=0.001)
    values = ctx.arr(intensity, "v", path)
    if values is None:
        return
    if not values:
        ctx.err(f"{path}.v", "为空")
        return
    bad = 0
    for i, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            ctx.err(f"{path}.v[{i}]", "不是数字")
            bad += 1
        elif not (0.0 <= float(value) <= 1.0):
            ctx.err(f"{path}.v[{i}]", f"超出 0..1：{value}")
            bad += 1
        if bad >= 5:
            break


def _check_lines(ctx: _Ctx, lines: list, path: str, duration: float) -> None:
    if not lines:
        ctx.warn(path, "没有歌词行（纯器乐？）")
        return
    for i, line in enumerate(lines):
        lp = f"{path}[{i}]"
        if not isinstance(line, dict):
            ctx.err(lp, "不是对象")
            continue
        ctx.number(line, "i", lp)
        t0 = _check_time(ctx, line, "t0", lp, duration)
        t1 = _check_time(ctx, line, "t1", lp, duration)
        if t0 is not None and t1 is not None and t1 < t0 - 0.01:
            ctx.err(lp, "t1 < t0")
        _check_time(ctx, line, "exit_at", lp, duration, optional=True)
        ctx.need(line, "text", lp, str)
        ctx.need(line, "section", lp, str)
        chars = ctx.arr(line, "chars", lp)
        if chars is not None:
            for j, ch in enumerate(chars):
                cp = f"{lp}.chars[{j}]"
                if not isinstance(ch, (list, tuple)) or len(ch) < 5:
                    ctx.err(cp, "不是 [字, 起, 止, 重音, 关键词]")
                    continue
                if not isinstance(ch[0], str) or not ch[0]:
                    ctx.err(cp, "第一个元素不是字")
                for k in (1, 2, 3):
                    if isinstance(ch[k], bool) or not isinstance(ch[k], (int, float)):
                        ctx.err(cp, f"第 {k + 1} 个元素不是数字")
                if ch[4] not in (0, 1, 0.0, 1.0, True, False):
                    ctx.err(cp, "关键词标记不是 0/1")
                if isinstance(ch[1], (int, float)) and isinstance(ch[2], (int, float)):
                    if float(ch[2]) < float(ch[1]) - 1e-3:
                        ctx.err(cp, "止 < 起")
        ctx.enum(line, "emotion", lp, EMOTIONS)
        for key in ("valence", "arousal"):
            ctx.number(line, key, lp, low=-1, high=1)
        ctx.number(line, "intensity", lp, low=0, high=1)
        kws = ctx.arr(line, "keywords", lp, optional=True)
        if kws is not None:
            for j, item in enumerate(kws):
                if not isinstance(item, str):
                    ctx.err(f"{lp}.keywords[{j}]", "不是字符串")
        ctx.enum(line, "address", lp, ADDRESSES)
        ctx.enum(line, "gaze", lp, GAZES)
        lyric = ctx.need(line, "lyric", lp, dict)
        if isinstance(lyric, dict):
            ctx.enum(lyric, "reveal", f"{lp}.lyric", REVEALS)
            ctx.enum(lyric, "stagger", f"{lp}.lyric", STAGGERS)
            ctx.enum(lyric, "layout", f"{lp}.lyric", LAYOUTS)
            ctx.enum(lyric, "material", f"{lp}.lyric", MATERIALS)
            ctx.enum(lyric, "exit", f"{lp}.lyric", EXITS)
            ctx.enum(lyric, "font_role", f"{lp}.lyric", {"primary", "accent"}, optional=True)
            pos = ctx.arr(lyric, "pos", f"{lp}.lyric", optional=True)
            if pos is not None:
                if len(pos) != 2 or any(
                        isinstance(v, bool) or not isinstance(v, (int, float)) for v in pos):
                    ctx.err(f"{lp}.lyric.pos", "不是 [x, y]")
                elif not (0.0 <= float(pos[0]) <= 1.0 and 0.0 <= float(pos[1]) <= 1.0):
                    ctx.err(f"{lp}.lyric.pos", "超出 0..1")
            for key in ("front", "echo"):
                if key in lyric and not isinstance(lyric[key], bool):
                    ctx.err(f"{lp}.lyric.{key}", "不是布尔")
            ctx.number(lyric, "boost", f"{lp}.lyric", low=0, high=4, optional=True)
            repeat_of = lyric.get("repeat_of")
            if repeat_of is not None:
                if isinstance(repeat_of, bool) or not isinstance(repeat_of, int):
                    ctx.err(f"{lp}.lyric.repeat_of", "不是整数或 null")
        motion = ctx.need(line, "motion", lp, dict)
        if isinstance(motion, dict):
            ctx.number(motion, "amp", f"{lp}.motion", low=0, high=2)


def _check_events(ctx: _Ctx, events: list, path: str, duration: float) -> None:
    for i, event in enumerate(events):
        ep = f"{path}[{i}]"
        if not isinstance(event, dict):
            ctx.err(ep, "不是对象")
            continue
        _check_time(ctx, event, "t", ep, duration)
        ctx.enum(event, "type", ep, EVENT_TYPES)
        for key in ("dur", "amp"):
            if key in event:
                ctx.number(event, key, ep, low=0)
        if "arg" in event and not isinstance(event["arg"], str):
            ctx.err(f"{ep}.arg", "不是字符串")


def _check_camera(ctx: _Ctx, camera: list, path: str, duration: float) -> None:
    last = -1e9
    for i, shot in enumerate(camera):
        sp = f"{path}[{i}]"
        if not isinstance(shot, dict):
            ctx.err(sp, "不是对象")
            continue
        t = _check_time(ctx, shot, "t", sp, duration)
        if t is not None and t + 1e-6 < last:
            ctx.err(sp, "镜头时间不是递增的")
        if t is not None:
            last = t
        ctx.enum(shot, "kind", sp, SHOT_KINDS)
        ctx.number(shot, "cx", sp, low=0, high=1)
        ctx.number(shot, "rot", sp, low=-45, high=45)
        if "cut" in shot and not isinstance(shot["cut"], bool):
            ctx.err(f"{sp}.cut", "不是布尔")
        ctx.number(shot, "ease_beats", sp, low=0, high=64, optional=True)
        ctx.number(shot, "shake", sp, low=0, high=10, optional=True)


def _check_scenes(ctx: _Ctx, scenes: list, path: str, duration: float) -> None:
    for i, scene in enumerate(scenes):
        sp = f"{path}[{i}]"
        if not isinstance(scene, dict):
            ctx.err(sp, "不是对象")
            continue
        ctx.need(scene, "section", sp, str)
        _check_time(ctx, scene, "t0", sp, duration)
        _check_time(ctx, scene, "t1", sp, duration)
        ctx.need(scene, "scene", sp, str)
        ctx.enum(scene, "entry", sp, ENTRIES)
        ctx.number(scene, "blur", sp, low=0, high=80)
        ctx.number(scene, "lum", sp, low=0, high=3)
        ctx.number(scene, "density", sp, low=0, high=4)


def _check_desk(ctx: _Ctx, desk: dict, path: str) -> None:
    ctx.number(desk, "amp_scale", path, low=0, high=1)
    whitelist = ctx.arr(desk, "gesture_whitelist", path)
    if whitelist is not None:
        for i, item in enumerate(whitelist):
            if not isinstance(item, str):
                ctx.err(f"{path}.gesture_whitelist[{i}]", "不是字符串")
    ctx.need(desk, "events_ref", path, str)


def validate(obj, *, strict: bool = True) -> dict:
    """校验一份 show.json。返回 ``{ok, errors, warnings}``。"""
    ctx = _Ctx(strict=strict)
    if not isinstance(obj, dict):
        ctx.err("$", "顶层不是对象")
        return {"ok": False, "errors": ctx.errors, "warnings": ctx.warnings}
    if obj.get("schema") != SCHEMA:
        ctx.err("$.schema", f"应为 {SCHEMA!r}，实际 {obj.get('schema')!r}")
    ctx.need(obj, "track_id", "$", str)
    seed = obj.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        ctx.err("$.seed", "不是整数")
    ctx.need(obj, "input_hash", "$", str)
    ctx.need(obj, "generated_at", "$", str)
    ctx.need(obj, "generator", "$", str)
    duration = ctx.number(obj, "duration", "$", low=0.1)
    duration = float(duration or 0.0)

    style = ctx.need(obj, "style", "$", dict)
    if isinstance(style, dict):
        _check_style(ctx, style, "$.style")
    music = ctx.need(obj, "music", "$", dict)
    if isinstance(music, dict):
        _check_music(ctx, music, "$.music", duration)
    intensity = ctx.need(obj, "intensity", "$", dict)
    if isinstance(intensity, dict):
        _check_intensity(ctx, intensity, "$.intensity")
    lines = ctx.arr(obj, "lines", "$")
    if lines is not None:
        _check_lines(ctx, lines, "$.lines", duration)
    events = ctx.arr(obj, "events", "$")
    if events is not None:
        _check_events(ctx, events, "$.events", duration)
    camera = ctx.arr(obj, "camera", "$")
    if camera is not None:
        _check_camera(ctx, camera, "$.camera", duration)
    scenes = ctx.arr(obj, "scenes", "$")
    if scenes is not None:
        _check_scenes(ctx, scenes, "$.scenes", duration)
    desk = ctx.need(obj, "desk", "$", dict)
    if isinstance(desk, dict):
        _check_desk(ctx, desk, "$.desk")
    return {"ok": not ctx.errors, "errors": ctx.errors, "warnings": ctx.warnings}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    strict = True
    if "--lenient" in argv:
        strict = False
        argv.remove("--lenient")
    if len(argv) != 1:
        print("用法: python -I choreo/schema.py [--lenient] <show.json>")
        return 2
    path = Path(argv[0])
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"FAIL 读不了 {path}: {exc}")
        return 2
    except ValueError as exc:
        print(f"FAIL 不是合法 JSON {path}: {exc}")
        return 2
    result = validate(obj, strict=strict)
    if result["warnings"]:
        print(f"警告 {len(result['warnings'])} 条：")
        for item in result["warnings"][:40]:
            print("  -", item)
    if result["errors"]:
        print(f"FAIL {path}：{len(result['errors'])} 个错误")
        for item in result["errors"][:80]:
            print("  -", item)
        if len(result["errors"]) > 80:
            print(f"  … 还有 {len(result['errors']) - 80} 条")
        return 1
    lines = len(obj.get("lines") or [])
    sections = len((obj.get("music") or {}).get("sections") or [])
    print(f"PASS {path}  lines={lines} sections={sections} "
          f"duration={obj.get('duration')} bpm={(obj.get('music') or {}).get('bpm')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
