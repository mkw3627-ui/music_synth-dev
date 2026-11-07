"""判断一段「清唱」录音里背景声（伴奏 / 音乐 / 环境噪声）是不是太大。

用法（和其它 worker 一样）：python -I bg_probe_worker.py <job.json> <result.json>

job:
  input      (str)   音频路径（wav/mp3/flac 都行，soundfile 读）
  floor_db   (float, default -25)  「唱段附近能掉多深」的中位数高于这个值，
                                   说明底下一直垫着东西

result:
  ok, noisy(是否建议先做人声分离), floor_db, gap_db, quiet_db, loud_db,
  silence_ratio, duration_s
  ok=False 时带 error（调用方按「不分离」继续，不阻断流程）

判定思路：不看整首有多安静（有长前奏/尾奏的文件会误导），只看「唱得响的地方
附近 ±1 s 能掉多深」，再取中位数。
  · 真清唱：句间换气几十毫秒就能掉 25~50 dB，中位数一般在 -30 以下；
  · 垫着伴奏：哪怕伴奏只有人声的 -20 dB，停顿处也被托在这个高度，掉不下去。
实测：伴奏 0/-10/-20 dB 的混音 => -10.8/-16.3/-23.4 dB（判为有背景音乐）；
      干净清唱 => -27 ~ -50 dB（判为没有）。
伴奏只有 -30 dB 时和清唱分不开，也不再分——那个音量本来就听不出来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _read(path: Path):
    import soundfile as sf
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    return data, int(rate)


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    result: dict = {"ok": False, "step": "bg_probe"}
    try:
        import numpy as np

        src = Path(job["input"]).resolve()
        data, rate = _read(src)
        mono = data.mean(axis=1) if getattr(data, "ndim", 1) > 1 else data
        if rate <= 0 or mono.size == 0:
            raise RuntimeError("空音频")

        hop = max(1, int(rate * 0.05))
        usable = (mono.size // hop) * hop
        frames = mono[:usable].reshape(-1, hop)
        rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1)) if frames.size else np.zeros(1)
        db = 20.0 * np.log10(np.maximum(rms, 1e-8))

        loud = float(np.percentile(db, 90))
        quiet = float(np.percentile(db, 10))
        gap = loud - quiet
        silence_ratio = float((db < (loud - 25.0)).mean())

        # 只统计「唱得响」的帧（p90 往下 12 dB 以内），在它们附近 ±1 s 找最低帧。
        loud_idx = np.flatnonzero(db > loud - 12.0)
        win = max(1, int(1.0 / 0.05))
        step = max(1, loud_idx.size // 2000)
        mins = []
        for i in loud_idx[::step]:
            i = int(i)
            mins.append(float(db[max(0, i - win):min(db.size, i + win + 1)].min()))
        floor_db = (float(np.median(mins)) - loud) if mins else (quiet - loud)

        threshold = float(job.get("floor_db") or -25.0)
        noisy = bool(floor_db > threshold)

        result = {
            "ok": True,
            "noisy": noisy,
            "reason": "background-loud" if noisy else "quiet-enough",
            "floor_db": round(floor_db, 2),
            "floor_db_threshold": threshold,
            "gap_db": round(gap, 2),
            "quiet_db": round(quiet, 2),
            "loud_db": round(loud, 2),
            "silence_ratio": round(silence_ratio, 3),
            "duration_s": round(mono.size / float(rate), 2),
        }
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "step": "bg_probe", "error": f"{type(exc).__name__}: {exc}"}

    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
