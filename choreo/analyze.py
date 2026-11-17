"""离线音频分析：节拍 / 小节 / 段落 / 能量 / 人声事件。

P1 只用 librosa，不引入 allin1 / NATTEN 这类重依赖。分析结果全部落成
show.json 需要的 ``music`` 段，运行时不再碰音频。

约定：
- 所有时间单位是秒，基准是成品音频（和口型轨同一个时钟）。
- 结果必须可复现：不依赖随机数。
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np

HOP = 0.1                 # 能量/强度曲线的时间步长
CUE_HOP = 0.05            # 口型包络的时间步长（lips.json 的 hop）


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _smooth(values: np.ndarray, width: int = 5) -> np.ndarray:
    if values.size == 0 or width <= 1:
        return values
    kernel = np.ones(int(width), dtype=np.float64) / float(width)
    return np.convolve(values.astype(np.float64), kernel, mode="same")


def _round_list(values: Iterable[float], digits: int = 3) -> list[float]:
    return [round(float(v), digits) for v in values]


def _load_mono(path: str, sr: int = 22050) -> tuple[np.ndarray, int]:
    import librosa
    y, _sr = librosa.load(path, sr=sr, mono=True)
    return y, sr


def _beat_grid(y: np.ndarray, sr: int) -> tuple[float, np.ndarray, np.ndarray]:
    """返回 (bpm, 拍点秒, 下拍秒)。下拍按 4/4 的相位取 onset 最强的那个。"""
    import librosa
    tempo, beats = librosa.beat.beat_track(y=y, sr=sr, units="time", trim=False)
    tempo = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 100.0
    beats = np.asarray(beats, dtype=np.float64)
    if beats.size < 2:
        return tempo, beats, beats
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
    frames = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=512),
                     0, max(0, onset.size - 1))
    best_phase, best_score = 0, -1.0
    for phase in range(4):
        picks = frames[phase::4]
        if picks.size == 0:
            continue
        score = float(np.sum(onset[picks] ** 2))
        if score > best_score:
            best_phase, best_score = phase, score
    downbeats = beats[best_phase::4] if beats.size > best_phase else beats[:1]
    return tempo, beats, downbeats


def _beat_features(y: np.ndarray, sr: int, beats: np.ndarray):
    """按拍点聚合色度 + MFCC，并返回每拍归一化 RMS。"""
    import librosa
    hop = 512
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop)
    feat = np.vstack([
        librosa.util.normalize(chroma, axis=0),
        librosa.util.normalize(mfcc, axis=0),
    ])
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    frames = np.clip(np.round(beats * sr / hop).astype(int), 0, feat.shape[1] - 1)
    bf = np.nan_to_num(librosa.util.sync(feat, frames, aggregate=np.mean))
    bt = np.nan_to_num(librosa.util.sync(rms[None, :], frames, aggregate=np.mean)[0])
    scale = float(np.percentile(bt, 95)) if bt.size else 0.0
    bt = np.clip(bt / (scale + 1e-9), 0.0, 1.0) if scale > 0 else np.zeros_like(bt)
    if bf.shape[1] > 1:
        bf = (bf - bf.mean(axis=1, keepdims=True)) / (bf.std(axis=1, keepdims=True) + 1e-9)
    return bf, bt


def _segment_bounds(bf: np.ndarray, duration: float) -> list[tuple[int, int]]:
    """时间约束的层次聚类 → 相邻拍段。返回值是拍索引区间。"""
    import librosa
    n = int(bf.shape[1])
    if n <= 2:
        return [(0, max(1, n))]
    # 每段大约 18~24 秒；太少会切碎，太多会把主歌副歌混在一起。
    k = int(np.clip(round(duration / 21.0), 6, 18))
    k = max(2, min(k, max(2, n // 3)))
    try:
        bounds = list(librosa.segment.agglomerative(bf, k))
    except Exception:
        step = max(1, n // k)
        bounds = list(range(0, n, step))
    bounds = sorted({int(b) for b in bounds if 0 <= b < n})
    if not bounds or bounds[0] != 0:
        bounds.insert(0, 0)
    bounds.append(n)
    # 合并过短片段（< 1.5 小节）到相邻段
    cleaned: list[list[int]] = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if b <= a:
            continue
        if cleaned and (b - a) < 3:
            cleaned[-1][1] = b
        else:
            cleaned.append([a, b])
    if len(cleaned) > 1 and (cleaned[0][1] - cleaned[0][0]) < 3:
        cleaned[1][0] = 0
        cleaned.pop(0)
    return [(a, b) for a, b in cleaned]


def _segment_groups(seg_feats: list[np.ndarray]) -> list[list[int]]:
    """按色度相似度把「同一段材料」归组（副歌通常至少出现两次）。"""
    n = len(seg_feats)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            a, b = seg_feats[i], seg_feats[j]
            denom = float(np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9
            if float(np.dot(a, b) / denom) >= 0.92:
                union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _label_sections(segments: list[dict], duration: float) -> list[str]:
    """按能量/重复度给段落命名：intro / verse / pre / chorus / bridge / final / outro。"""
    n = len(segments)
    if n == 0:
        return []
    if n == 1:
        return ["verse"]
    energies = [float(s["energy"]) for s in segments]
    max_e = max(energies) or 1.0
    feats = [s.pop("_feat") for s in segments]
    groups = _segment_groups(feats)
    group_stats = []
    for members in groups:
        avg = float(np.mean([energies[i] for i in members]))
        group_stats.append((avg * (1.0 + 0.08 * (len(members) - 1)), members))
    group_stats.sort(reverse=True, key=lambda item: item[0])
    # 副歌材料：优先选「重复出现且能量高」的组
    chorus_members: list[int] = []
    for _score, members in group_stats:
        members = [i for i in members if i not in (0, n - 1)]
        if len(members) >= 2 and float(np.mean([energies[i] for i in members])) >= 0.45 * max_e:
            chorus_members = members
            break
    if not chorus_members:
        # 没有重复材料：把能量最高的几段当副歌（至少最高的那段）
        middle = list(range(1, max(1, n - 1)))
        ranked = sorted(middle, key=lambda i: energies[i], reverse=True)
        if ranked:
            top = energies[ranked[0]]
            chorus_members = [i for i in ranked
                              if energies[i] >= max(0.55 * top, 0.6 * max_e)][:3]
            chorus_members.sort()
    chorus_set = set(chorus_members)
    # 全曲最强的段若够强、又没被选进副歌组，纳入副歌（v2 §5.2：最后一次副歌是全曲最强）
    if chorus_members:
        middle = list(range(1, max(1, n - 1)))
        strongest = max(middle, key=lambda i: energies[i])
        if strongest not in chorus_set and energies[strongest] >= 0.92 * max(energies[i] for i in chorus_members):
            chorus_members = sorted(chorus_members + [strongest])
            chorus_set = set(chorus_members)
    chorus_e = float(np.mean([energies[i] for i in chorus_members])) if chorus_members else max_e

    labels = ["verse"] * n
    labels[0] = "intro"
    labels[-1] = "outro"
    # 前奏太短（没有独立的器乐段）就把第一段并回主歌
    bar = max(0.3, float(segments[0].get("_bar") or 1.0))
    if segments[0]["t1"] - segments[0]["t0"] < bar * 2:
        labels[0] = "verse"
    if segments[-1]["t1"] - segments[-1]["t0"] < bar * 2:
        labels[-1] = "final" if chorus_members else "verse"

    first_chorus = min(chorus_members) if chorus_members else n
    last_chorus = max(chorus_members) if chorus_members else n
    for i in chorus_members:
        labels[i] = "chorus"
    if chorus_members:
        labels[max(chorus_members)] = "final"

    # 导歌：第一次副歌前的一段，通常比主歌短、能量在爬升
    pre_idx = first_chorus - 1
    if 0 < pre_idx < n and pre_idx not in chorus_set and labels[pre_idx] not in ("intro", "outro"):
        seg = segments[pre_idx]
        bars = (seg["t1"] - seg["t0"]) / bar
        if bars <= 9 or energies[pre_idx] > energies[pre_idx - 1] + 0.08:
            labels[pre_idx] = "pre"

    # 桥段：副歌之间能量明显掉下去、材料又不重复的一段（只取一段）
    group_of: dict[int, int] = {}
    for gi, members in enumerate(groups):
        for member in members:
            group_of[member] = gi
    group_size = {gi: len(members) for gi, members in enumerate(groups)}
    bridge_candidates = []
    for i in range(first_chorus + 1, last_chorus):
        if i in chorus_set or labels[i] in ("intro", "outro", "pre"):
            continue
        gi = group_of.get(i, -1)
        if energies[i] <= 0.62 * chorus_e and group_size.get(gi, 1) == 1:
            bridge_candidates.append(i)
    if bridge_candidates:
        labels[min(bridge_candidates, key=lambda i: energies[i])] = "bridge"

    # 最后副歌之后的低能量段是尾声
    if chorus_members and labels[-1] not in ("outro", "final"):
        labels[-1] = "outro"
    if chorus_members:
        final_idx = max(chorus_members)
        # 最终副歌之后的段落：只要不是另一个高潮，都归尾声
        for i in range(final_idx + 1, n):
            if labels[i] in ("verse", "pre") and energies[i] < 0.92 * chorus_e:
                labels[i] = "outro"
        # 紧挨最终副歌的短段是导歌（最后的 build-up）
        if 0 < final_idx - 1 < n and labels[final_idx - 1] == "verse":
            seg = segments[final_idx - 1]
            if (float(seg["t1"]) - float(seg["t0"])) / bar <= 4.5:
                labels[final_idx - 1] = "pre"
    return labels


def _fallback_sections(duration: float, beat: float) -> list[dict]:
    """结构识别失败时的兜底：按比例切，但边界仍然落在拍上。"""
    bar = beat * 4
    plan = [
        ("intro", 0.00, 0.07), ("verse", 0.07, 0.27), ("pre", 0.27, 0.32),
        ("chorus", 0.32, 0.50), ("verse", 0.50, 0.62),
        ("chorus", 0.62, 0.80), ("final", 0.80, 0.92), ("outro", 0.92, 1.00),
    ]
    out = []
    for sid, a, b in plan:
        t0 = round(duration * a / bar) * bar
        t1 = round(duration * b / bar) * bar
        out.append({"id": sid, "t0": max(0.0, t0), "t1": max(t0 + bar, min(duration, t1))})
    out[-1]["t1"] = duration
    return out


def analyze(audio: str, *, stem: str = "", duration: float | None = None,
            lines: list[dict] | None = None, cues: list | None = None,
            hop: float = HOP, want_vocal: bool = True) -> dict:
    """分析一段成品音频，返回 show.json 的 ``music`` 段 + 人声事件。

    参数：
      audio     成品混音（节拍/段落/能量都从这里来）
      stem      干净人声（高音/颤音从这里来；没有就跳过这两类事件）
      duration  已知曲长（可省）
      lines     带 t0/text 的歌词行（用来找高音、颤音、句尾、换气）
      cues      逐字口型轨（用来找长音/句尾/换气）
      hop       能量曲线步长
    """
    import librosa

    y, sr = _load_mono(audio)
    dur = float(duration or 0.0) or float(len(y) / sr)
    if dur <= 0:
        raise RuntimeError("空音频")

    tempo, beats, downbeats = _beat_grid(y, sr)
    if tempo <= 0 or not math.isfinite(tempo):
        tempo = 100.0
    beat = 60.0 / tempo
    bar = beat * 4.0

    # ---- 段落 ----
    sections: list[dict] = []
    if beats.size >= 8 and dur >= 24:
        bf, bt = _beat_features(y, sr, beats)
        bounds = _segment_bounds(bf, dur)
        raw = []
        for a, b in bounds:
            if b <= a:
                continue
            t0 = float(beats[a]) if a < beats.size else dur
            t1 = float(beats[b]) if b < beats.size else dur
            if b == bf.shape[1]:
                t1 = dur
            if t1 - t0 < bar * 0.75:
                continue
            feat = bf[:, a:b].mean(axis=1) if b > a else np.zeros(bf.shape[0])
            raw.append({"t0": t0, "t1": t1, "energy": float(np.mean(bt[a:b]) if b > a else 0.0),
                        "_feat": feat, "_bar": bar})
        if len(raw) >= 3:
            labels = _label_sections(raw, dur)
            # 第一次副歌前如果是一大段主歌，把最后几小节切出来当导歌（build-up）。
            try:
                first_chorus = next(i for i, lab in enumerate(labels)
                                    if lab in ("chorus", "final"))
            except StopIteration:
                first_chorus = -1
            prev = first_chorus - 1
            if 0 <= prev < len(raw) and labels[prev] in ("verse", "pre"):
                seg = raw[prev]
                bars = (float(seg["t1"]) - float(seg["t0"])) / bar
                if bars > 10:
                    pre_bars = min(8, max(4, int(round(bars * 0.3))))
                    split = float(seg["t1"]) - pre_bars * bar
                    if split > float(seg["t0"]) + 4 * bar:
                        build = {
                            "t0": split, "t1": float(seg["t1"]),
                            "energy": min(1.0, float(seg.get("energy") or 0.4) * 1.05),
                        }
                        seg["t1"] = split
                        if labels[prev] == "pre":
                            labels[prev] = "verse"
                        raw.insert(first_chorus, build)
                        labels.insert(first_chorus, "pre")
            for item, label in zip(raw, labels):
                item.pop("_feat", None)
                item.pop("_bar", None)
                sections.append({"id": label, **item})
        else:
            sections = _fallback_sections(dur, beat)
    else:
        sections = _fallback_sections(dur, beat)

    # 补齐首尾、去掉空隙
    if sections:
        sections[0]["t0"] = 0.0
        sections[-1]["t1"] = dur
        cleaned = [sections[0]]
        for seg in sections[1:]:
            if seg["t0"] < cleaned[-1]["t1"] - 0.02:
                # 真重叠才合并；相邻段（t0 == 上一段 t1）必须保留
                cleaned[-1]["t1"] = max(cleaned[-1]["t1"], seg["t1"])
                cleaned[-1]["energy"] = max(cleaned[-1]["energy"], seg["energy"])
            else:
                if seg["t0"] > cleaned[-1]["t1"] + 0.02:
                    # 聚类的边界可能漏掉半拍：用下一段起点补上空隙
                    cleaned[-1]["t1"] = seg["t0"]
                cleaned.append(seg)
        sections = cleaned
        for seg in sections:
            seg["t0"] = round(float(seg["t0"]), 3)
            seg["t1"] = round(float(seg["t1"]), 3)
            seg["energy"] = round(float(seg.get("energy") or 0.0), 3)

    # ---- 能量曲线（hop 固定）----
    rms = librosa.feature.rms(y=y, frame_length=2048,
                              hop_length=max(1, int(sr * hop)))[0]
    if rms.size:
        scale = float(np.percentile(rms, 95))
        rms = np.clip(rms / (scale + 1e-9), 0.0, 1.0) if scale > 0 else np.zeros_like(rms)
        rms = np.clip(_smooth(rms, 3), 0.0, 1.0)
    energy_v = [round(float(v), 4) for v in rms]
    energy_mean = float(np.mean(rms)) if rms.size else 0.0
    energy_var = float(np.var(rms)) if rms.size else 0.0

    # ---- drops ----
    drops: list[float] = []

    def amp_between(a: float, b: float) -> float:
        if not rms.size:
            return 0.0
        lo = max(0, int(round(a / hop)))
        hi = min(rms.size, int(round(b / hop)))
        return float(np.mean(rms[lo:hi])) if hi > lo else 0.0

    for seg in sections:
        if seg["id"] not in ("chorus", "final"):
            continue
        before = amp_between(seg["t0"] - 1.6, seg["t0"] - 0.2)
        after = amp_between(seg["t0"] + 0.05, seg["t0"] + 1.2)
        if after - before >= 0.12:
            drops.append(round(float(seg["t0"]), 3))
    drops = sorted(set(drops))

    # ---- 人声事件 ----
    vocal = {"long_note": [], "phrase_end": [], "inhale": [],
             "high_note": [], "vibrato": []}
    if want_vocal:
        vocal = _vocal_from_cues(cues or [], lines or [], beat, dur)
        if stem:
            try:
                vocal.update(_pitch_events(stem, lines or [], sr=sr))
            except Exception:
                pass

    return {
        "bpm": round(float(tempo), 3),
        "beat": round(float(beat), 4),
        "bar": round(float(bar), 4),
        "beats": _round_list(beats),
        "downbeats": _round_list(downbeats),
        "sections": sections,
        "drops": drops,
        "energy": {"hop": float(hop), "v": energy_v},
        "energy_mean": round(energy_mean, 4),
        "energy_var": round(energy_var, 6),
        "vocal": vocal,
    }


def _vocal_from_cues(cues: list, lines: list[dict], beat: float,
                     duration: float) -> dict:
    """从逐字口型轨和行时间轴求长音 / 句尾 / 换气。"""
    out = {"long_note": [], "phrase_end": [], "inhale": [],
           "high_note": [], "vibrato": []}
    parsed = []
    for cue in cues or []:
        if not isinstance(cue, (list, tuple)) or len(cue) < 4:
            continue
        try:
            start, end = float(cue[2]), float(cue[3])
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        breath = bool(int(cue[4])) if len(cue) > 4 and cue[4] is not None else False
        stress = float(cue[5]) if len(cue) > 5 and cue[5] is not None else 0.2
        parsed.append((start, end, breath, stress))
    parsed.sort()
    long_min = max(0.72, beat * 1.25)
    for start, end, _breath, stress in parsed:
        span = end - start
        if span >= long_min:
            out["long_note"].append({
                "t": round(start, 3), "dur": round(span, 3),
                "amp": round(_clip(0.35 + span / (beat * 6.0), 0.25, 1.0), 3)})
        if stress >= 0.5:
            # 重音不单独成事件，交给 director 的 keyword/punch 处理
            pass

    ordered = sorted(
        [(float(row.get("t0") or 0.0), str(row.get("text") or ""))
         for row in (lines or []) if isinstance(row, dict)],
        key=lambda item: item[0])
    for idx, (t0, _text) in enumerate(ordered):
        t1 = ordered[idx + 1][0] if idx + 1 < len(ordered) else t0 + 4.0
        if t1 <= t0:
            continue
        out["phrase_end"].append({"t": round(t1 - 0.05, 3), "amp": 0.4})
        if idx + 1 < len(ordered):
            gap = ordered[idx + 1][0] - t1
            if gap >= 0.7:
                out["inhale"].append({
                    "t": round(max(t0, ordered[idx + 1][0] - min(0.38, gap * 0.35)), 3),
                    "dur": round(min(0.45, gap * 0.5), 3),
                    "amp": round(_clip(gap / 2.2, 0.25, 0.9), 3)})
    # 行首如果离上一行很久，也算一次换气
    return out


def _pitch_events(stem: str, lines: list[dict], sr: int = 22050) -> dict:
    """在干声上求高音和颤音。只返回真正明显的事件。"""
    import librosa
    y, sr = _load_mono(stem, sr=sr)
    if y.size == 0:
        return {}
    hop_len = max(1, int(sr * CUE_HOP))
    f0, voiced, _prob = librosa.pyin(
        y, fmin=80, fmax=1000, sr=sr, frame_length=1024, hop_length=hop_len)
    times = np.arange(f0.size) * CUE_HOP
    out = {"high_note": [], "vibrato": []}
    ordered = sorted(
        [(float(row.get("t0") or 0.0), str(row.get("text") or ""))
         for row in (lines or []) if isinstance(row, dict)],
        key=lambda item: item[0])
    for idx, (t0, _text) in enumerate(ordered):
        t1 = ordered[idx + 1][0] if idx + 1 < len(ordered) else float(times[-1]) if times.size else t0 + 4.0
        if t1 <= t0:
            continue
        mask = voiced & (times >= t0) & (times < t1) & np.isfinite(f0)
        vals = f0[mask]
        if vals.size < 8:
            continue
        med = float(np.median(vals))
        p85 = float(np.percentile(vals, 85))
        if med <= 0:
            continue
        # 高音：比本句中位数高 3 个半音以上，持续 >= 0.12s
        semis = 12.0 * np.log2(np.maximum(vals, 1.0) / med)
        high_mask = semis >= 3.0
        run_start = None
        for j, flag in enumerate(high_mask):
            if flag and run_start is None:
                run_start = j
            if (not flag or j == len(high_mask) - 1) and run_start is not None:
                jj = j if not flag else j + 1
                if (jj - run_start) * CUE_HOP >= 0.12:
                    local_times = times[mask][run_start:jj]
                    if local_times.size:
                        out["high_note"].append({
                            "t": round(float(local_times[0]), 3),
                            "dur": round(float((jj - run_start) * CUE_HOP), 3),
                            "amp": round(_clip(float(np.max(semis[run_start:jj])) / 8.0 + 0.35), 3),
                            "arg": f"{float(np.max(vals[run_start:jj])):.0f}Hz"})
                run_start = None
        # 颤音：0.35s 窗口里去趋势后的半音标准差 0.08~0.5，过零 2~5 次
        t_local = times[mask]
        dev = 12.0 * np.log2(np.maximum(vals, 1.0) / med)
        win = max(4, int(round(0.35 / CUE_HOP)))
        best = None
        for start in range(0, max(0, dev.size - win), max(1, win // 2)):
            chunk = dev[start:start + win]
            if chunk.size < 4:
                continue
            std = float(np.std(chunk - np.mean(chunk)))
            if not (0.08 <= std <= 0.55):
                continue
            centered = chunk - np.mean(chunk)
            crossings = int(np.sum(np.diff(np.signbit(centered)) != 0))
            if 2 <= crossings <= 6:
                if best is None or std > best[0]:
                    best = (std, float(t_local[start]))
        if best is not None:
            out["vibrato"].append({
                "t": round(best[1], 3), "dur": round(win * CUE_HOP, 3),
                "amp": round(_clip(best[0] * 2.0 + 0.25), 3)})
    return out
