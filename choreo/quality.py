"""编排质量评分器（P5 · T5-A）。

只读一份 show.json（dict），按设计 v2 的可量化条款打分；``build_best`` 在
不合格时换种子重抽（最多 ``MAX_TRIES`` 次），挑硬性项全过、软分最高的一份。

每一项都标出依据条款；条款没给数值的，只记实测值，不参与「合格」判定。
"""
from __future__ import annotations

import bisect

MAX_TRIES = 6
_DIMS = ("stagger", "reveal", "layout", "material", "exit")

# 元素冲击力（1 弱 ~ 5 强）。v2 §5.1「把所有可用元素按冲击力从小到大排」；
# 生成器的新鲜感预算和这里的评分共用这一张表。
IMPACT = {
    "reveal:rise": 1, "reveal:type": 1, "reveal:wipe": 2, "reveal:ink": 2,
    "reveal:drop": 3, "reveal:scatter": 3, "reveal:glitch": 4, "reveal:slam": 5,
    "layout:h": 1, "layout:v": 2, "layout:stack": 2, "layout:arc": 3,
    "layout:center": 3, "layout:giant": 5,
    "material:solid": 1, "material:outline": 2, "material:grad": 2, "material:neon": 4,
    "exit:sink": 1, "exit:dissolve": 1, "exit:push": 2, "exit:burst": 4,
    "stagger:whole": 1, "stagger:char": 1, "stagger:word": 1, "stagger:cue": 2,
    "stagger:center": 2, "stagger:random": 3,
    "shot:wide": 1, "shot:base": 1, "shot:medium": 2, "shot:close": 3, "shot:dutch": 4,
    "kind:title": 3, "kind:flash": 3, "echo": 3, "front": 4,
    "ev:flash": 4, "ev:shake": 4, "ev:drop": 5, "ev:black": 5,
}


# 舞台运行时（static/stage/stage-scenes.js drawForeground）真正画出来的意象；
# 其它意象名只写进 show.json、画面上看不到，不算「新元素」。
RENDERED_IMAGERY = {"stars", "flare", "heart", "digits"}


def impact(token: str) -> int:
    if token in IMPACT:
        return IMPACT[token]
    if token.startswith("scene:") or token.startswith("img:"):
        return 2
    return 1


def _real(show: dict) -> list[dict]:
    return [L for L in show.get("lines") or [] if L.get("kind") not in ("title", "flash")]


def _seg_mean(show: dict, t0: float, t1: float) -> float:
    iv = show.get("intensity") or {}
    hop, v = float(iv.get("hop") or 0.1), list(iv.get("v") or [])
    a, b = int(t0 / hop), max(int(t0 / hop) + 1, int(t1 / hop))
    part = v[a:b]
    return sum(part) / len(part) if part else 0.0


# ---------------------------------------------------------------- 重复度

def repetition(show: dict) -> dict:
    """v2 §2.2-3：相邻 2 句至少 3 维不同；同一完整组合全曲 ≤2 次（重复句除外）；
    同一种揭示方式间隔至少 3 句。相邻和间隔按全部歌词句（含重复句）的时间顺序算，
    组合计数排除重复句。"""
    lines = _real(show)
    adj_bad, gap_bad, combos = 0, 0, {}
    for k, L in enumerate(lines):
        c = L["lyric"]
        if c.get("repeat_of") is None:
            key = tuple(c.get(d) for d in _DIMS)
            combos[key] = combos.get(key, 0) + 1
        if k:
            p = lines[k - 1]["lyric"]
            if sum(1 for d in _DIMS if c.get(d) != p.get(d)) < 3:
                adj_bad += 1
        if any(lines[j]["lyric"].get("reveal") == c.get("reveal") for j in range(max(0, k - 2), k)):
            gap_bad += 1
    over = sum(1 for n in combos.values() if n > 2)
    return {"lines": len(lines), "adjacent_lt3": adj_bad, "reveal_gap_lt3": gap_bad,
            "combo_over2": over, "distinct_combos": len(combos),
            "ok": adj_bad == 0 and gap_bad == 0 and over == 0}


# ---------------------------------------------------------------- 签名动效

def signature_share(show: dict) -> dict:
    """v2 §2.2-4：签名动效全曲出现 25~35%，副歌高潮必用。"""
    sig = (show.get("style") or {}).get("signature") or [""]
    sig = sig[0]
    lines = _real(show)
    hit = [L for L in lines if L["lyric"].get("reveal") == sig]
    share = len(hit) / max(1, len(lines))
    climax = [L for L in lines if L.get("section") in ("chorus", "final")]
    climax_used = any(L["lyric"].get("reveal") == sig for L in climax) if climax else None
    return {"signature": sig, "share": round(share, 3), "climax_used": climax_used,
            "ok": 0.25 <= share <= 0.35 and climax_used is not False}


# ---------------------------------------------------------------- 前排

def front_budget(show: dict) -> dict:
    """v2 §2.1 关系维度：「在人物前」全曲只用 1~3 次（人物前的字才可能挡脸）。"""
    n = sum(1 for L in _real(show) if L["lyric"].get("front"))
    return {"front_lines": n, "ok": 1 <= n <= 3}


# ---------------------------------------------------------------- 新鲜感

def _first_seen(show: dict) -> list[tuple[float, str]]:
    seen: dict[str, float] = {}

    def mark(t, token):
        if token not in seen:
            seen[token] = float(t)

    for L in show.get("lines") or []:
        c = L.get("lyric") or {}
        t = float(L["t0"])
        for d in _DIMS:
            mark(t, f"{d}:{c.get(d)}")
        if c.get("front"):
            mark(t, "front")
        if c.get("echo"):
            mark(t, "echo")
        if L.get("kind"):
            mark(t, f"kind:{L['kind']}")
        mark(t, f"gaze:{L.get('gaze')}")
    for s in show.get("camera") or []:
        mark(s["t"], f"shot:{s['kind']}")
        if abs(float(s.get("rot") or 0)) > 0.5:
            mark(s["t"], "shot:dutch")
    for s in show.get("scenes") or []:
        mark(s["t0"], f"scene:{s['scene']}")
    for e in show.get("events") or []:
        if e["type"] == "imagery" and e.get("arg") in RENDERED_IMAGERY:
            mark(e["t"], f"img:{e.get('arg')}")
        elif e["type"] in ("flash", "black", "drop", "shake"):
            mark(e["t"], f"ev:{e['type']}")
    return sorted((t, k) for k, t in seen.items())


def freshness(show: dict, window: float = 15.0) -> dict:
    """v2 §8 P5：每 15 秒内都有新元素（录屏口径：开头到结尾任意 15 秒）。
    v2 §5.1：按冲击力排的预算，前半首只花掉 50%（按冲击力加权）；
    最强的元素留给最后一次副歌（最后一次副歌里有冲击力 ≥4 的元素首次出现）。"""
    dur = float(show.get("duration") or 0)
    firsts = _first_seen(show)
    times = [t for t, _ in firsts]
    gaps, prev = [], 0.0
    for t in times + [dur]:
        gaps.append(t - prev)
        prev = max(prev, t)
    max_gap = max(gaps) if gaps else dur
    total_w = sum(impact(k) for _, k in firsts)
    half_w = sum(impact(k) for t, k in firsts if t < dur / 2) / max(1, total_w)
    half_n = sum(1 for t in times if t < dur / 2) / max(1, len(times))
    secs = show["music"]["sections"]
    last = next((s for s in secs if s["id"] == "final"), None) or         next((s for s in reversed(secs) if s["id"] == "chorus"), None)
    in_final = [k for t, k in firsts if last and last["t0"] - 1.0 <= t < last["t1"]]
    strong = any(impact(k) >= 4 for k in in_final)
    return {"elements": len(firsts), "max_gap_s": round(max_gap, 2),
            "first_half_share_weighted": round(half_w, 3),
            "first_half_share_count": round(half_n, 3),
            "new_in_final": in_final, "final_has_strong_new": strong,
            "ok_15s": max_gap <= window, "ok_half": half_w <= 0.5,
            "ok": max_gap <= window and half_w <= 0.5 and strong}


# ---------------------------------------------------------------- 五个时刻

def moments(show: dict) -> dict:
    """v2 §5.2 五个必有时刻；v2 §8 P5「最后一次副歌是全曲最强」。"""
    secs = show["music"]["sections"]
    dur = float(show["duration"])
    ev = show.get("events") or []
    lines = show.get("lines") or []
    chorus = [s for s in secs if s["id"] == "chorus"]
    final = next((s for s in secs if s["id"] == "final"), None)
    first = chorus[0] if chorus else final
    # 1 开场钩子：前 5 秒有歌名浮出 / 黑场 / 闪白
    hook = any(L.get("kind") == "title" and float(L["t0"]) < 5 for L in lines) or \
        any(e["type"] in ("black", "flash") and e["t"] < 5 for e in ev)
    # 2 第一次兑现：第一次副歌入口有闪白 + 硬切 + 巨型字
    pay = False
    if first:
        t0 = float(first["t0"])
        pay = any(e["type"] == "flash" and abs(e["t"] - t0) < 0.05 for e in ev) and \
            any(s.get("cut") and abs(s["t"] - t0) < 0.05 for s in show.get("camera") or []) and \
            any(L.get("lyric", {}).get("layout") == "giant" and first["t0"] <= L["t0"] < first["t1"]
                for L in _real(show))
    # 3 静场：桥段或 ≥2.5 s 的器乐间奏，强度低于第一次副歌
    quiet_segs = [(s["t0"], s["t1"]) for s in secs if s["id"] == "bridge"] + \
        [(e["t"], e["t"] + e["dur"]) for e in ev if e["type"] == "inst_break"]
    ref = _seg_mean(show, first["t0"], first["t1"]) if first else 1.0
    quiet = any(_seg_mean(show, a, b) < ref for a, b in quiet_segs)
    # 4 高潮：最后一次副歌（final）平均强度是全曲各段最高；并召回前面出现过的签名/重复句
    climax_peak, recall = False, False
    last = final or (chorus[-1] if chorus else None)
    if last:
        m_last = _seg_mean(show, last["t0"], last["t1"])
        others = [_seg_mean(show, s["t0"], s["t1"]) for s in secs if s is not last]
        climax_peak = all(m_last >= o for o in others)
        sig = ((show.get("style") or {}).get("signature") or [""])[0]
        in_last = [L for L in _real(show) if last["t0"] <= L["t0"] < last["t1"]]
        recall = any(L["lyric"].get("echo") or L["lyric"].get("reveal") == sig for L in in_last)
    # 5 余韵：尾声关键词闪回，最后一个字留到音乐结束（最后一句消失时刻 ≥ 曲长 − 3 s）
    flash_back = any(L.get("kind") == "flash" for L in lines)
    gone = max((float(L.get("gone_at") or L.get("exit_at") or L["t1"]) for L in lines), default=0)
    tail = gone >= dur - 3.0
    # 5.3 期待感（只记实测，v2 没给数值门槛，不参与 ok）：
    # 预副歌前有 stagger 逐字加速（间隔逐句变短）；爆点（第一次副歌）前 1 拍内有定格/黑场
    pre_accel, pre_freeze = None, None
    if first:
        t0 = float(first["t0"])
        pre_lines = [L for L in _real(show) if t0 - 6.0 <= float(L["t0"]) < t0]
        gaps = [float(b["t0"]) - float(a["t0"]) for a, b in zip(pre_lines, pre_lines[1:])]
        pre_accel = bool(len(gaps) >= 2 and gaps[-1] < gaps[0])
        pre_freeze = any(e["type"] in ("black", "drop") and t0 - 1.0 <= e["t"] <= t0 for e in ev)
    out = {"hook": hook, "first_payoff": pay, "quiet": quiet,
           "climax_peak": climax_peak, "climax_recall": recall,
           "afterglow_flashback": flash_back, "afterglow_tail": tail,
           "pre_chorus_accel": pre_accel, "pre_burst_freeze": pre_freeze,
           "last_gone_s": round(gone, 2), "duration": dur}
    out["ok"] = all(out[k] for k in ("hook", "first_payoff", "quiet", "climax_peak",
                                     "climax_recall", "afterglow_flashback", "afterglow_tail"))
    return out


# ---------------------------------------------------------------- 对拍 / 可读性

def beat_align(show: dict) -> dict:
    """v2 §4.2「应对齐的事件是否都落在拍上」：手势与副歌入口；v2 §8 P3 硬切落重拍 <50 ms。"""
    beats = sorted(show["music"].get("beats") or [])
    downs = sorted(show["music"].get("downbeats") or [])

    def dist(t, arr):
        i = bisect.bisect_left(arr, t)
        c = [arr[j] for j in (i - 1, i) if 0 <= j < len(arr)]
        return min(abs(t - x) for x in c) * 1000 if c else None

    g = [dist(e["t"], beats) for e in show["events"] if e["type"] == "gesture"]
    ci = [dist(e["t"], downs) for e in show["events"] if e["type"] == "chorus_in"]
    g = [x for x in g if x is not None]
    ci = [x for x in ci if x is not None]
    return {"gesture_max_ms": round(max(g), 1) if g else None,
            "chorus_in_downbeat_max_ms": round(max(ci), 1) if ci else None,
            "ok": (not g or max(g) < 1.0)}


def _lum(hexcolor: str) -> float:
    h = hexcolor.lstrip("#")
    if len(h) != 6:
        return 0.0
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def readability(show: dict) -> dict:
    """v2 §4.2 可读性：文字与背景亮度对比度。条款没给阈值，只记实测值。"""
    pal = (show.get("style") or {}).get("palette") or {}
    out = {}
    for ink in ("ink", "ink2"):
        for bg in ("bgA", "bgB"):
            if pal.get(ink) and pal.get(bg):
                a, b = _lum(pal[ink]), _lum(pal[bg])
                out[f"{ink}/{bg}"] = round((max(a, b) + 0.05) / (min(a, b) + 0.05), 2)
    return {"contrast": out, "min": min(out.values()) if out else None}


# ---------------------------------------------------------------- 汇总 / 重抽

HARD = ("repetition", "front", "freshness", "moments", "beat")


def score(show: dict) -> dict:
    rep = {
        "repetition": repetition(show),
        "signature": signature_share(show),
        "front": front_budget(show),
        "freshness": freshness(show),
        "moments": moments(show),
        "beat": beat_align(show),
        "readability": readability(show),
    }
    hard_fail = [k for k in HARD if not rep[k]["ok"]]
    soft = (rep["repetition"]["distinct_combos"] / max(1, rep["repetition"]["lines"])
            - rep["repetition"]["adjacent_lt3"] * 0.1
            - abs(rep["signature"]["share"] - 0.3)
            - max(0.0, rep["freshness"]["max_gap_s"] - 15) * 0.05)
    rep["hard_fail"] = hard_fail
    rep["ok"] = not hard_fail
    rep["soft"] = round(soft, 4)
    return rep


def build_best(build, seed: int, tries: int = MAX_TRIES):
    """``build(seed) -> show``。第 0 次用原种子；不合格就换种子重抽，挑最好的一份。

    排序：硬性不合格项少的优先，其次软分高的。返回 (show, report, attempts)。
    同一输入同一种子，结果完全确定。
    """
    best = None
    attempts = []
    for k in range(max(1, tries)):
        s = (int(seed) + k * 7919) & 0x7FFFFFFF
        show = build(s)
        rep = score(show)
        attempts.append({"seed": s, "hard_fail": rep["hard_fail"], "soft": rep["soft"]})
        key = (-len(rep["hard_fail"]), rep["soft"])
        if best is None or key > best[0]:
            best = (key, show, rep)
        if rep["ok"]:
            break
    return best[1], best[2], attempts
