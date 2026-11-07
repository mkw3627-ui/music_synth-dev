"""Corpus slicing + QC worker (训练素材库切分与质检).

Runs in the separation venv (numpy + soundfile + scipy guaranteed there).

Contract (same as the other workers): ``python -I corpus_worker.py <job.json>
<result.json>``; the result is written even on failure.

Pipeline (per source file, called from ms_corpus_add):
  1. (optional, song slot only) run the dry-vocal separation chain, stage by
     stage; each stage calls separate_worker-style chunk logic via the
     ``separate_worker`` module — but to stay self-contained this worker only
     does the *slicing + QC* on an already-produced dry stem.  The full chain
     orchestration lives in the plugin (ms_corpus_add → _corpus_pipeline),
     which reuses ``separate_worker`` for each model hop.
  2. Slice the dry vocal into 2–15 s clips at sentence boundaries: long
     silences plus short breath gaps (see ``_slice_bounds``), so each clip is
     roughly one sung phrase instead of an arbitrary equal-length chunk.
  3. QC each clip: peak >= 0.999 => clipping (reject); silence ratio > 50%
     => reject; record pitch range (median F0 via librosa if present, else
     "未跑").

Slots: song / dry_sing / speech (see plan §3).  The worker itself is
slot-agnostic: it receives ``slot`` only for naming; the caller decides
which upstream chain fed it.

Input job keys:
  input       (str)  path to the dry-vocal WAV to slice
  out_dir     (str)  directory under which ``clips`` is written
  source_id   (str)  stable id of the source file (for clip ids)
  slot        (str)  song | dry_sing | speech
  min_s       (float, default 2.0)
  max_s       (float, default 15.0)
  silence_s   (float, default 0.35)  quiet run that counts as a cut point
  silence_db  (float, default 28.0)  dB below loud body treated as quiet
  breath_db   (float, default 14.0)  dB below loud body treated as a breath gap
  breath_s    (float, default 0.20)  breath gap that can end a sentence
  peak_limit  (float, default 0.999)  >= this => reject (clipping)
  max_silence_ratio (float, default 0.5)  silence ratio above which reject
Output result keys:
  ok, clips, rejected, valid_s, source_id, slot, sample_rate, error
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np


def _read(path: str):
    import soundfile as sf
    data, rate = sf.read(path, dtype="float32", always_2d=True)
    return data, int(rate)


def _envelope(mono: np.ndarray, rate: int, hop_s: float = 0.05):
    hop = max(1, int(rate * hop_s))
    frames = len(mono) // hop
    if frames <= 0:
        return np.zeros(1, dtype=np.float32), hop
    trimmed = mono[: frames * hop].reshape(frames, hop)
    rms = np.sqrt((trimmed ** 2).mean(axis=1) + 1e-12)
    return rms, hop


def _quiet_mask(rms: np.ndarray, silence_db: float) -> np.ndarray:
    loud = float(np.percentile(rms, 90))
    floor = max(loud * (10 ** (-silence_db / 20.0)), 1e-5)
    return rms < floor


def _pitch_range(mono: np.ndarray, rate: int) -> dict:
    """Median F0 and voiced ratio via librosa when available; else 未跑."""
    try:
        import librosa  # type: ignore
    except Exception:  # noqa: BLE001
        return {"f0_hz_median": None, "f0_hz_min": None, "f0_hz_max": None,
                "voiced_ratio": None, "method": "未跑（无 librosa）"}
    try:
        x = mono.astype(np.float32)
        f0, voiced, _ = librosa.pyin(
            x, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"),
            sr=rate, frame_length=2048, hop_length=512, fill_na=np.nan)
    except Exception:  # noqa: BLE001
        return {"f0_hz_median": None, "f0_hz_min": None, "f0_hz_max": None,
                "voiced_ratio": None, "method": "未跑（pyin 失败）"}
    vals = f0[voiced & ~np.isnan(f0)] if f0 is not None else np.array([])
    if vals.size == 0:
        return {"f0_hz_median": None, "f0_hz_min": None, "f0_hz_max": None,
                "voiced_ratio": 0.0, "method": "librosa.pyin"}
    return {
        "f0_hz_median": round(float(np.median(vals)), 1),
        "f0_hz_min": round(float(np.min(vals)), 1),
        "f0_hz_max": round(float(np.max(vals)), 1),
        "voiced_ratio": round(float(voiced.mean()), 3) if voiced is not None else None,
        "method": "librosa.pyin",
    }


def _quality(data: np.ndarray, rate: int, peak_limit: float,
             max_silence_ratio: float, silence_db: float) -> dict:
    """Return QC verdict for one clip's samples."""
    mono = data.mean(axis=1) if data.ndim > 1 else data
    peak = float(np.abs(mono).max()) if mono.size else 0.0
    rms, _ = _envelope(mono, rate)
    quiet = _quiet_mask(rms, silence_db)
    silence_ratio = float(quiet.mean()) if quiet.size else 0.0
    pitch = _pitch_range(mono, rate)
    reasons = []
    if peak >= peak_limit:
        reasons.append("clipping")
    if silence_ratio > max_silence_ratio:
        reasons.append("silence")
    status = "rejected" if reasons else "ok"
    return {
        "peak": round(peak, 4),
        "silence_ratio": round(silence_ratio, 4),
        "status": status,
        "reason": "、".join(reasons) if reasons else "",
        **pitch,
    }


def _snap_cut(rms: np.ndarray, hop_s: float, ideal: float, lo: float, hi: float,
              search_s: float = 1.0) -> float:
    """把切口从理想位置挪到 ±search_s 内能量最低的一帧。

    长段没有长静音时只能按 max_s 均分，均分点经常正落在字中间或长音上；
    挪到附近最安静的地方，切出来的片段更像一句完整的句子。
    """
    if rms.size == 0:
        return ideal
    lo = max(lo, 0.0)
    hi = max(hi, lo)
    a = int(max(0, np.floor((ideal - search_s) / hop_s)))
    b = int(min(rms.size, np.ceil((ideal + search_s) / hop_s) + 1))
    if b <= a:
        return ideal
    idx = a + int(np.argmin(rms[a:b]))
    return float(min(max(idx * hop_s, lo), hi))


def _slice_bounds(mono: np.ndarray, rate: int, min_s: float, max_s: float,
                  silence_s: float, silence_db: float,
                  breath_db: float = 14.0, breath_s: float = 0.20) -> list[tuple[float, float]]:
    """Return (start_s, end_s) segments, 2–15 s, cut at sentence boundaries.

    两级切口：
      1. 长静音（``silence_s`` 以上、低于响段 ``silence_db``）——一定有；
      2. 换气/句间停顿（低于响段 ``breath_db`` 且持续 ``breath_s`` 以上）——
         清唱很少掉到 -28 dB，但句子之间会明显掉下去，靠这一级才能切到「每一句」。
    切口放在该段能量最低的一帧，而不是中点。
    """
    total = len(mono) / float(rate)
    if total <= max_s:
        return [(0.0, total)] if total >= min_s else []
    rms, hop = _envelope(mono, rate)
    quiet = _quiet_mask(rms, silence_db)
    hop_s = hop / float(rate)
    need = max(1, int(silence_s / hop_s))

    # Candidate cut times: centres of quiet runs >= silence_s.
    cuts: list[float] = []
    run_start = None
    for idx, is_q in enumerate(quiet):
        if is_q and run_start is None:
            run_start = idx
        elif not is_q and run_start is not None:
            if idx - run_start >= need:
                cuts.append((run_start + idx) / 2.0 * hop_s)
            run_start = None
    if run_start is not None and len(quiet) - run_start >= need:
        cuts.append((run_start + len(quiet)) / 2.0 * hop_s)

    # 第二级：句间换气。找「明显低于响段」的短暂停顿，切口取其中最低的一帧。
    db = 20.0 * np.log10(np.maximum(rms, 1e-9))
    breath_floor = float(np.percentile(db, 90)) - float(breath_db)
    breath_need = max(1, int(float(breath_s) / hop_s))
    below = db < breath_floor
    run_start = None
    for idx, is_gap in enumerate(below):
        if is_gap and run_start is None:
            run_start = idx
        elif not is_gap and run_start is not None:
            if idx - run_start >= breath_need:
                span = rms[run_start:idx]
                cuts.append((run_start + int(np.argmin(span))) * hop_s)
            run_start = None
    if run_start is not None and len(below) - run_start >= breath_need:
        span = rms[run_start:]
        cuts.append((run_start + int(np.argmin(span))) * hop_s)
    cuts = sorted(set(round(c, 3) for c in cuts))

    # Walk boundaries, never produce a segment outside [min_s, max_s].
    bounds = [0.0] + [c for c in cuts if min_s < c < total - min_s] + [total]
    segs: list[tuple[float, float]] = []
    cursor = 0.0
    for b in bounds[1:]:
        if b <= cursor:
            continue
        span = b - cursor
        if span >= max_s:
            # Split long spans into max_s-sized chunks; each cut is nudged to the
            # quietest frame nearby so a segment rarely ends mid-syllable.
            n = int(np.ceil(span / max_s))
            step = span / n
            start = cursor
            for i in range(1, n):
                ideal = cursor + i * step
                lo = max(start + min_s, b - (n - i) * max_s)
                hi = min(start + max_s, b - min_s)
                cut = _snap_cut(rms, hop_s, ideal, lo, hi)
                if cut - start >= min_s:
                    segs.append((start, cut))
                    start = cut
            if b - start >= min_s:
                segs.append((start, b))
            cursor = b
        elif span >= min_s:
            segs.append((cursor, b))
            cursor = b
        else:
            # too short to stand alone: extend cursor forward, don't emit.
            pass
    return segs


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    result: dict = {"ok": False, "step": "corpus_slice"}
    try:
        src = Path(job["input"]).resolve()
        out_dir = Path(job["out_dir"]).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        slot = str(job.get("slot") or "song")
        source_id = str(job.get("source_id") or src.stem)
        min_s = float(job.get("min_s") or 2.0)
        max_s = float(job.get("max_s") or 15.0)
        silence_s = float(job.get("silence_s") or 0.35)
        silence_db = float(job.get("silence_db") or 28.0)
        breath_db = float(job.get("breath_db") or 14.0)
        breath_s = float(job.get("breath_s") or 0.20)
        peak_limit = float(job.get("peak_limit") or 0.999)
        max_silence_ratio = float(job.get("max_silence_ratio") or 0.5)

        import soundfile as sf
        data, rate = _read(str(src))
        mono = data.mean(axis=1) if data.ndim > 1 else data
        bounds = _slice_bounds(mono, rate, min_s, max_s, silence_s, silence_db,
                               breath_db, breath_s)

        clips, rejected = [], []
        valid_s = 0.0
        for idx, (s, e) in enumerate(bounds):
            a, b = int(s * rate), int(e * rate)
            seg = data[a:b]
            if seg.size == 0:
                continue
            clip_id = f"{source_id}_{idx:03d}"
            target = out_dir / f"{clip_id}.wav"
            sf.write(str(target), seg, rate, subtype="PCM_16")
            qc = _quality(seg, rate, peak_limit, max_silence_ratio, silence_db)
            item = {
                "id": clip_id,
                "slot": slot,
                "src": source_id,
                "path": str(target),
                "start_s": round(s, 3),
                "end_s": round(e, 3),
                "dur_s": round(e - s, 3),
                "status": qc["status"],
                "reason": qc["reason"],
                "peak": qc["peak"],
                "silence_ratio": qc["silence_ratio"],
                "f0_hz_median": qc.get("f0_hz_median"),
                "f0_hz_min": qc.get("f0_hz_min"),
                "f0_hz_max": qc.get("f0_hz_max"),
                "voiced_ratio": qc.get("voiced_ratio"),
            }
            if qc["status"] == "ok":
                clips.append(item)
                valid_s += item["dur_s"]
            else:
                rejected.append(item)

        result.update({
            "ok": bool(clips),
            "source": str(src),
            "source_id": source_id,
            "slot": slot,
            "sample_rate": rate,
            "source_duration_s": round(len(mono) / float(rate), 3),
            "clips": clips,
            "rejected": rejected,
            "valid_s": round(valid_s, 3),
            "error": None if clips else "没有切出可用片段（素材过短或全程静音/削波）",
        })
    except Exception as exc:  # noqa: BLE001
        result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
