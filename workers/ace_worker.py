"""ACE-Step text2music worker（在 envs/ace 的独立 Python 里运行）。

输入 job（由插件 ms_ace_generate 写入）：
    plugin_dir, project_root, output_dir, prompt, lyrics, vocal_language,
    duration, bpm, keyscale, timesignature, instrumental, inference_steps,
    guidance_scale, seed, name

输出 result：
    {ok, wav, audios, prompt, lyrics, duration, error}

这个 worker 只负责“生成一首完整的歌”，不做分离/变声；后续由插件现有的
分离 → DDSP 变声 → 混音链路处理。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path


def _default_dtype(torch):
    """Match the dtype ACE-Step will load the weights in.

    临时张量（transformers 里的位置编码、mask 等）不写 dtype，用的是
    ``torch.set_default_dtype``。默认 dtype 和权重 dtype 不一致，第一层
    matmul 就会报 "expected scalar type" —— 这在 Turing（GTX 16/RTX 20）
    上必然发生，因为那些卡没有 bf16 内核，ACE 会把权重装成 fp16。
    """
    try:
        if torch.cuda.is_available():
            major = int(torch.cuda.get_device_capability()[0])
            if major >= 8:      # Ampere 及以后：bf16
                return torch.bfloat16
            if major >= 6:      # Turing/Pascal：只有 fp16
                return torch.float16
    except Exception:  # noqa: BLE001 - 探测失败就按 fp32 走（CPU 场景）
        pass
    return torch.float32


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    result = {"ok": False, "step": "ace_generate"}
    try:
        plugin_dir = Path(job["plugin_dir"]).resolve()
        sys.path.insert(0, str(plugin_dir))
        # ACE-Step 是 vendored 第三方代码，放在 vendor/ 下（Market 的 Ruff 会跳过）。
        sys.path.insert(0, str(plugin_dir / "vendor"))
        pylibs = Path(str(job.get("pylibs") or ""))
        if str(pylibs) and pylibs.is_dir():
            sys.path.insert(0, str(pylibs))

        # 小显存卡：VAE 解码挪到内存，显存留给 DiT；分配器换可扩展段，
        # 减少 4GB 卡上反复申请/释放造成的碎片（Windows 不支持该选项）。
        if job.get("vae_on_cpu"):
            os.environ.setdefault("ACESTEP_VAE_ON_CPU", "1")
        if not sys.platform.startswith("win"):
            os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

        import ace_runtime  # noqa: E402

        post_dtype = ""
        try:
            import torch
            wanted = _default_dtype(torch)
            if wanted is torch.float16:
                # 老卡（Turing/Volta）权重是 fp16，默认 dtype 也得是 fp16，
                # 但只能在权重加载完之后设——加载前设会让 safetensors 物化
                # 阶段原生崩溃（access violation，实测必现）。
                post_dtype = "float16"
            else:
                torch.set_default_dtype(wanted)
        except Exception:
            pass

        def maybe_float(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def maybe_int(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        duration = maybe_float(job.get("duration"))
        bpm = maybe_int(job.get("bpm"))
        request = ace_runtime.AceStepGenerateRequest(
            prompt=str(job.get("prompt") or ""),
            lyrics=str(job.get("lyrics") or ""),
            task_type="text2music",
            instrumental=bool(job.get("instrumental")),
            vocal_language=str(job.get("vocal_language") or "unknown"),
            bpm=bpm,
            keyscale=str(job.get("keyscale") or ""),
            timesignature=str(job.get("timesignature") or ""),
            duration=duration if duration and duration > 0 else -1.0,
            inference_steps=max(1, min(60, int(job.get("inference_steps") or 12))),
            guidance_scale=float(job.get("guidance_scale") or 7.0),
            seed=maybe_int(job.get("seed")) if maybe_int(job.get("seed")) is not None else -1,
            use_random_seed=bool(job.get("use_random_seed", True)),
            batch_size=max(1, min(8, int(job.get("batch_size") or 1))),
            seeds=job.get("seeds") or None,
            dcw_enabled=bool(job.get("dcw_enabled", True)),
            dcw_mode=str(job.get("dcw_mode") or "double"),
            dcw_scaler=float(job.get("dcw_scaler") or 0.05),
            dcw_high_scaler=float(job.get("dcw_high_scaler") or 0.02),
            dcw_wavelet=str(job.get("dcw_wavelet") or "haar"),
            audio_format="wav",
        )
        runtime_config = ace_runtime.AceStepRuntimeConfig(
            project_root=str(job["project_root"]),
            model_name=str(job.get("model_name") or "acestep-v15-turbo"),
            device=str(job.get("device") or "auto"),
            offload_to_cpu=bool(job.get("offload_to_cpu", True)),
            offload_dit_to_cpu=bool(job.get("offload_dit_to_cpu", True)),
            layer_offload=bool(job.get("layer_offload", False)),
            quantization=str(job.get("quantization") or "") or None,
            default_dtype=post_dtype,
            output_dir=str(job["output_dir"]),
            lora_path=str(job.get("lora_path") or "") or None,
        )
        generated = ace_runtime.generate_with_acestep_local(
            request, runtime_config, save_dir=str(job["output_dir"]))
        if not generated.success:
            raise RuntimeError(generated.error or generated.status_message
                               or "ACE-Step 生成失败")
        if not generated.audio_paths:
            raise RuntimeError("ACE-Step 没有返回音频")
        audios = [str(p) for p in generated.audio_paths]
        # 多候选时按频谱平衡打分，默认把最不闷、最不轰头的那条排第一。
        # 分数只是「自动挑一个」，其余候选仍会回到面板里给用户试听。
        best, scores = 0, []
        try:
            import librosa
            import numpy as np
            import soundfile as sf

            vals = []
            for audio_path in audios:
                y, sr = sf.read(audio_path, dtype="float32", always_2d=True)
                mono = y.mean(axis=1)
                if len(mono) < sr * 2:
                    vals.append(0.0)
                    continue
                spec = np.abs(librosa.stft(mono, n_fft=4096, hop_length=1024)) ** 2
                freqs = librosa.fft_frequencies(sr=sr, n_fft=4096)
                power = spec.mean(axis=1)
                total = float(power.sum()) + 1e-12

                def band(lo, hi):
                    mask = (freqs >= lo) & (freqs < hi)
                    return float(power[mask].sum()) / total

                r01, r14 = band(0, 1000), band(1000, 4000)
                r48, r812 = band(4000, 8000), band(8000, 12000)
                rms = float(np.sqrt((mono ** 2).mean()) + 1e-12)
                crest = 20 * np.log10(float(np.abs(mono).max()) / rms + 1e-12)
                corr = 1.0
                if y.shape[1] > 1:
                    corr = float(np.corrcoef(y[:, 0], y[:, 1])[0, 1])
                score = 0.0
                score += 2.0 * min(1.0, r14 / 0.18)       # 中高频存在感
                score += 1.0 * min(1.0, r48 / 0.03)      # 亮度
                score += 1.0 * min(1.0, r812 / 0.012)    # 空气感
                score -= 2.0 * max(0.0, r01 - 0.75)      # 低频轰头
                score -= 0.5 * abs(crest - 13.0) / 10.0  # 动态太极端
                score -= 2.0 * max(0.0, corr - 0.99)     # 几乎是单声道
                vals.append(round(float(score), 4))
            if vals:
                best = int(np.argmax(vals))
                scores = vals
        except Exception:  # noqa: BLE001 - 打分失败就退回第一条
            pass
        result.update({
            "ok": True,
            "wav": audios[best],
            "audios": audios,
            "best": best,
            "scores": scores,
            "prompt": generated.prompt,
            "lyrics": generated.lyrics,
            "duration": (generated.metas or {}).get("duration"),
        })
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["trace"] = traceback.format_exc()[-6000:]
    finally:
        try:
            out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
