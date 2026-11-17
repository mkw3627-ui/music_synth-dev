"""歌词兜底识别 worker（独立进程，job/result JSON，与 f0_worker 同模式）。

拿不到原创歌词、也抓不到网上歌词时，用「为她所听」装好的本地歌声识别模型
（专用 faster-whisper large-v3，带逐段时间戳）把**干净人声**转成带时间的歌词行。
不改动、不常驻「为她所听」的任何进程；只借用它的私有 Python 环境与模型文件（只读）。

job: {"stem": 干声 wav, "perception_root": music_perception 插件目录,
      "env_dir": 为她所听 data/environment, "model_dir": 模型目录, "model": 模型 id,
      "language": auto|zh|ja|en, "device": auto|cuda|cpu}
result: {"ok", "lines": [{"at_s","end_s","text"}], "language", "model", "device", "seconds"}
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# whisper 在无声/器乐处常见的「幻觉」句（视频字幕腔），出现即丢
HALLU = ("ご視聴ありがとうございました", "チャンネル登録", "字幕", "请不吝点赞", "订阅", "感谢观看",
         "谢谢观看", "Thank you for watching", "thanks for watching", "Subtitles by", "Amara.org",
         "ありがとうございました")
CHUNK_S = 28.0     # 每段 ≤ 30 s（whisper 窗口），段间 2 s 重叠防止切断字
OVERLAP_S = 2.0
RATE = 16000


def _load_wav_16k(path: str):
    import numpy as np
    import soundfile as sf
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != RATE:
        n = int(round(len(mono) * RATE / sr))
        x_old = np.linspace(0.0, 1.0, num=len(mono), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
        mono = np.interp(x_new, x_old, mono).astype("float32")
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if peak > 0:
        mono = (mono / peak * 0.9).astype("float32")
    return mono


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    t_start = time.time()
    result: dict = {"ok": False}
    try:
        root = Path(job["perception_root"]).resolve()
        sys.path.insert(0, str(root.parent))       # 让 import music_perception.* 生效
        import numpy as np
        from faster_whisper import WhisperModel  # 来自为她所听的 singing_asr_venv

        model_dir = Path(job["model_dir"])
        device = str(job.get("device") or "auto")
        try:
            model = WhisperModel(str(model_dir), device="cuda" if device != "cpu" else "cpu",
                                 compute_type="float16" if device != "cpu" else "int8")
            used = "cuda" if device != "cpu" else "cpu"
        except Exception:  # noqa: BLE001 - 没 GPU / 显存不够就退 CPU
            model = WhisperModel(str(model_dir), device="cpu", compute_type="int8")
            used = "cpu"

        audio = _load_wav_16k(job["stem"])
        lang = str(job.get("language") or "auto")
        if lang == "auto":
            # 整首先定一种语言（取人声最响的 30 s 判一次），再按这个语言逐段识别：
            # 逐段自动判语言会把个别句子判成越南语之类，整句乱码
            win = int(30 * RATE)
            if len(audio) > win:
                e = np.convolve(audio * audio, np.ones(RATE, dtype="float32"), mode="valid")[::RATE]
                k = int(np.argmax(np.convolve(e, np.ones(30), mode="valid"))) * RATE
                probe = audio[k:k + win]
            else:
                probe = audio
            try:
                code, prob, _all = model.detect_language(probe)
            except Exception:  # noqa: BLE001
                code, prob = "", 0.0
            if code in ("zh", "ja", "en", "ko") and prob >= 0.5:
                lang = code
        lines: list[dict] = []
        detected = ""
        total = len(audio) / RATE
        # 按干声的安静处切段（不切在句子中间），每段 ≤ CHUNK_S
        hop = int(0.05 * RATE)
        frames = len(audio) // hop
        rms = np.array([float(np.sqrt(np.mean(audio[i * hop:(i + 1) * hop] ** 2))) for i in range(frames)])
        quiet = rms < max(0.01, float(np.percentile(rms, 40)))
        cuts = [0.0]
        while cuts[-1] < total - 0.5:
            lo, hi = cuts[-1] + 8.0, min(total, cuts[-1] + CHUNK_S)
            if hi >= total:
                cuts.append(total)
                break
            best = None
            for f in range(int(hi / 0.05), int(lo / 0.05), -1):
                if f < len(quiet) and quiet[f]:
                    best = f * 0.05
                    break
            cuts.append(best if best else hi)
        spans = list(zip(cuts, cuts[1:]))
        for pos, end in spans:
            a, b = int(pos * RATE), int(end * RATE)
            seg = audio[a:b]
            if len(seg) < RATE:
                continue
            if float(np.sqrt(np.mean(seg * seg))) >= 0.003:
                chunks, info = model.transcribe(
                    seg, task="transcribe", language=None if lang == "auto" else lang,
                    beam_size=4, temperature=0.0, condition_on_previous_text=False,
                    vad_filter=False, word_timestamps=False,
                    no_speech_threshold=0.85, log_prob_threshold=-1.5)
                detected = detected or str(getattr(info, "language", "") or "")
                for piece in chunks:
                    text = str(getattr(piece, "text", "") or "").strip()
                    s0 = pos + float(getattr(piece, "start", 0.0))
                    s1 = pos + float(getattr(piece, "end", 0.0))
                    if not text or s1 <= s0 or len(text) > 120:
                        continue
                    if any(h.lower() in text.lower() for h in HALLU):
                        continue
                    # 这一段干声基本没声（识别器自己编的）
                    ia, ib = int(s0 * RATE), int(s1 * RATE)
                    part = audio[ia:ib]
                    if part.size and float(np.sqrt(np.mean(part * part))) < 0.012:
                        continue
                    if float(getattr(piece, "no_speech_prob", 0.0) or 0.0) > 0.85:
                        continue
                    if float(getattr(piece, "avg_logprob", 0.0) or 0.0) < -1.5:
                        continue
                    if float(getattr(piece, "compression_ratio", 0.0) or 0.0) > 2.6:
                        continue
                    # 重叠区里上一段已经出过的行不要重复；紧挨着的同一句（识别器幻觉重复）也去掉
                    if lines and text == lines[-1]["text"] and s0 - lines[-1]["end_s"] < 1.0:
                        lines[-1]["end_s"] = round(max(lines[-1]["end_s"], s1), 3)
                        continue
                    if lines and s0 < lines[-1]["at_s"] + 0.3:
                        continue
                    lines.append({"at_s": round(s0, 3), "end_s": round(s1, 3), "text": text})
        # 识别的段起点常常落在前面的无声处（whisper 段时间不准）：吸附到段内第一次真正起声的位置
        hop2 = int(0.05 * RATE)
        for L in lines:
            ia, ib = int(L["at_s"] * RATE), int(L["end_s"] * RATE)
            seg = audio[ia:ib]
            if seg.size < hop2 * 4:
                continue
            fr = np.array([float(np.sqrt(np.mean(seg[i:i + hop2] ** 2))) for i in range(0, seg.size - hop2, hop2)])
            thr = max(0.02, float(fr.max()) * 0.25)
            on = int(np.argmax(fr >= thr)) if (fr >= thr).any() else 0
            if on * 0.05 >= 0.6:
                L["at_s"] = round(L["at_s"] + on * 0.05, 3)
        result = {"ok": True, "lines": lines, "language": detected or lang,
                  "model": Path(model_dir).name, "device": used,
                  "seconds": round(time.time() - t_start, 1)}
    except Exception as exc:  # noqa: BLE001
        import traceback
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                  "trace": traceback.format_exc()[-1500:]}
    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
