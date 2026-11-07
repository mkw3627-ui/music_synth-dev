"""核对分离出来的「伴奏轨」是不是真的存在。

用法和其它 worker 一样：python -I stem_check_worker.py <job.json> <result.json>

job:
  input          (str)   原始素材路径
  stem           (str)   分离出来的目标轨（人声）
  threshold_db   (float, default 48)  伴奏轨比原素材低多少 dB 以内算「有背景音乐」

result:
  ok, has_music, background_db, vocal_db, stem, background_stem, error

判定思路：真的带伴奏的歌，伴奏轨和原素材是同一个量级（一般 -3 ~ -15 dB，
伴奏压得很低的素材也有 -21 ~ -43 dB 的实测值）；本来就干净的清唱，分离器
只能抠出一点模型残渣（实测 -50 ~ -55 dB）。电平之外再看频谱平坦度：
伴奏有稳定的谐波峰，平坦度 0.02~0.20；模型残渣是噪声，平坦度 0.29 以上。

规则：伴奏轨不低于 -48 dB，或者不低于 -56 dB 且频谱像乐器/伴奏，就当「有背景
音乐」——宁可让人声过一遍模型，也不能把伴奏留在训练数据里。只有电平极低且
平坦度像噪声时，才判定本来就是清唱、让调用方保留原声。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

# 不同分离器给「非目标轨」的命名。注意 separate_worker 合并后的轨名是全小写
# （_vocals.wav / _instrumental.wav），而 audio-separator 原始输出是
# "(Instrumental)"，所以匹配必须忽略大小写——这里大小写敏感过，导致永远找不到
# 伴奏轨、永远判「没有背景音乐」、回退原声，训练片段里一直带着伴奏。
BACKGROUND_HINTS = ("instrumental", "(other)", "_other", "(noise)", "_noise",
                    "accompaniment", "no_vocals")


def _rms(path: Path, frames: int | None = None) -> tuple[float, int]:
    import numpy as np
    import soundfile as sf

    # 注意：这个版本的 soundfile 不接受 frames=None，要传就不传这个参数。
    extra = {} if frames is None else {"frames": int(frames)}
    data, rate = sf.read(str(path), dtype="float32", always_2d=True, **extra)
    mono = data.mean(axis=1)
    if mono.size == 0:
        return 0.0, int(rate)
    return float(np.sqrt((mono.astype(np.float64) ** 2).mean())), int(rate)


def _flatness(path: Path, win: int = 2048) -> float:
    """频谱平坦度（几何均值/算术均值）中位数。

    乐器/伴奏有稳定的谐波峰，平坦度明显偏低（实测 0.02~0.12）；
    分离器在纯清唱上留下的残渣是噪声，平坦度接近 1（实测 0.29 以上）。
    """
    import numpy as np
    import soundfile as sf

    vals: list[float] = []
    hop = win // 2
    with sf.SoundFile(str(path)) as f:
        while True:
            block = f.read(hop * 400, dtype="float32", always_2d=True)
            if block.size == 0:
                break
            mono = block.mean(axis=1)
            n = 1 + (mono.size - win) // hop
            if n < 1:
                continue
            idx = np.arange(win)[None, :] + hop * np.arange(n)[:, None]
            seg = mono[idx] * np.hanning(win)[None, :]
            mag = np.abs(np.fft.rfft(seg, axis=1)) + 1e-12
            geo = np.exp(np.mean(np.log(mag), axis=1))
            vals.extend((geo / mag.mean(axis=1)).tolist())
    if not vals:
        return 1.0
    return float(np.median(np.asarray(vals, dtype=np.float64)))


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    result: dict = {"ok": False, "step": "stem_check"}
    try:
        raw = Path(job["input"]).resolve()
        stem = Path(job["stem"]).resolve()
        threshold_db = float(job.get("threshold_db") or 48.0)
        if not raw.is_file():
            raise RuntimeError(f"原素材不存在：{raw}")
        if not stem.is_file():
            raise RuntimeError(f"分离轨不存在：{stem}")

        # 目标轨和伴奏轨来自同一次分离，时长一致；按较短的对齐即可。
        vocal_rms, _ = _rms(stem)
        input_rms, _ = _rms(raw)

        siblings = sorted(stem.parent.glob("*.wav"))
        stem_name = stem.name.lower()
        background = next(
            (p for p in siblings
             if p.name.lower() != stem_name
             and any(h in p.name.lower() for h in BACKGROUND_HINTS)), None)
        if background is None:
            # 找不到伴奏轨就无从判断；按「有背景音乐」处理、保留分离结果。
            # 反过来（按「没有」）会把没分开的伴奏原样留在训练数据里，代价更大。
            result.update({
                "ok": True, "has_music": True, "background_db": None,
                "vocal_db": round(20 * math.log10(max(vocal_rms, 1e-12)), 2),
                "stem": str(stem), "background_stem": "",
                "error": "没找到伴奏轨，按有背景音乐处理（保留分离结果）",
            })
            out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return 0

        bg_rms, _ = _rms(background)
        bg_flat = _flatness(background)
        vocal_db = 20 * math.log10(max(vocal_rms, 1e-12))
        bg_db = 20 * math.log10(max(bg_rms, 1e-12))
        rel_db = bg_db - (20 * math.log10(max(input_rms, 1e-12)))
        # 电平够高，或者电平很低但频谱像乐器（不是噪声残渣）——都算有伴奏。
        has_music = bool(rel_db >= -threshold_db
                         or (rel_db >= -56.0 and bg_flat <= 0.23))
        result.update({
            "ok": True,
            "has_music": has_music,
            "background_db": round(rel_db, 2),
            "background_flatness": round(bg_flat, 4),
            "vocal_db": round(vocal_db, 2),
            "stem": str(stem),
            "background_stem": str(background),
            "threshold_db": threshold_db,
            "error": "",
        })
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "step": "stem_check", "error": f"{type(exc).__name__}: {exc}"}
    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
