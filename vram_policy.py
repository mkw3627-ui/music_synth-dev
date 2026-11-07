"""Low-VRAM compatibility policy for 为她而唱.

A model bigger than free VRAM is not "unsupported".  It can still run by
keeping the weights in system RAM and moving only the part that is computing
onto the GPU.  That is much slower, and it needs enough RAM, so the choice is
made here — as pure functions over a memory snapshot, so it can be tested
without a GPU.

Modes, cheapest-to-run first:

``gpu``     everything resident, fastest
``offload`` VAE + text encoder in system RAM, DiT resident on the GPU
``layer``   DiT blocks in system RAM, one block streamed to the GPU at a time
            (this is what makes a model larger than VRAM actually run)
``cpu``     no VRAM at all, pure CPU inference (slowest)

Nothing here imports torch.
"""
from __future__ import annotations

import os
from pathlib import Path

RESERVE_GIB = 1.0          # 留给桌面/采集/其他进程的显存（大卡）
RESERVE_MIN_GIB = 0.35     # 小卡上的下限：4GB 卡再留 1GB 就什么都不剩了
RAM_HEADROOM_GIB = 2.0     # 卸载到内存时给系统留的余量
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".ckpt",
                   ".sf_dlc", ".onnx", ".gguf")

# int8 权重量化后 DiT 约占原来的多少。解码器线性层 1 字节/参数（原来 2 字节），
# 非解码器部分（嵌入、条件编码器）仍是 fp16。实测 2B turbo：4.46GiB → 2.82GiB
# （比值 0.63），比“只有解码器减半”的理论值高。
QUANT_WEIGHT_RATIO = 0.63

MODE_ORDER = ("gpu", "quant", "offload", "layer", "cpu")

MODE_LABELS = {
    "gpu": "全部放显存（最快）",
    "quant": "int8 量化后放显存（体积减半，能塞进小显存）",
    "offload": "VAE/文本编码器放内存，DiT 留显存",
    "layer": "DiT 逐层放内存、按需搬上显存（能跑，但慢）",
    "cpu": "全部放内存、纯 CPU 计算（最慢）",
}

# torchao 的三种量化模式。键是传给 ACE-Step handler 的原名。
QUANT_LABELS = {
    "int8_weight_only": "int8 权重量化（体积≈一半，画质基本不变）",
    "w8a8_dynamic": "int8 权重+激活量化（更快，画质略降）",
    "fp8_weight_only": "fp8 权重量化（仅 RTX 40/50 系，体积≈一半）",
}

# CUDA 计算能力 → 这一代卡能用的东西。
# 这张表只描述硬件能力；torch wheel 是否带对应内核由 gpu_compat 单独判断。
_GPU_GENERATIONS = (
    (12.0, "Blackwell（RTX 50 系）"),
    (8.9, "Ada（RTX 40 系）"),
    (8.6, "Ampere（RTX 30 系）"),
    (8.0, "Ampere（A100 / RTX 30 系）"),
    (7.5, "Turing（GTX 16 / RTX 20 系）"),
    (7.0, "Volta（V100）"),
    (6.0, "Pascal（GTX 10 系）"),
    (5.0, "Maxwell（GTX 9 系）"),
)


def weight_gib(path) -> float:
    """Total size of model weight files under *path* (file or directory)."""
    p = Path(str(path or ""))
    try:
        if p.is_file():
            return p.stat().st_size / 1024 ** 3
        if not p.is_dir():
            return 0.0
        total = 0
        for root, _dirs, files in os.walk(p):
            for name in files:
                if name.casefold().endswith(WEIGHT_SUFFIXES):
                    try:
                        total += (Path(root) / name).stat().st_size
                    except OSError:
                        pass
        return total / 1024 ** 3
    except OSError:
        return 0.0


def _safetensors_params(path) -> float:
    """Count parameters from safetensors headers (no torch, no weight load)."""
    import json
    import struct

    total = 0.0
    root = Path(str(path or ""))
    files = [root] if root.is_file() else sorted(root.glob("*.safetensors"))
    for file in files:
        if not str(file).casefold().endswith(".safetensors"):
            continue
        try:
            with open(file, "rb") as handle:
                head = handle.read(8)
                if len(head) < 8:
                    continue
                size = struct.unpack("<Q", head)[0]
                if size <= 0 or size > 64 * 1024 * 1024:
                    continue
                header = json.loads(handle.read(size))
        except (OSError, ValueError, struct.error):
            continue
        for key, meta in header.items():
            if key == "__metadata__" or not isinstance(meta, dict):
                continue
            count = 1
            for dim in meta.get("shape") or []:
                count *= int(dim or 0)
            total += count
    return total


def estimate_runtime_gib(checkpoint_dir, disk_gib: float | None = None) -> float:
    """Estimate the *resident* size of a DiT checkpoint in bf16.

    The directory on disk is not the runtime footprint (safetensors may be
    fp32 and the file size includes headers/aliases), so the safest source is
    the parameter count in the headers times 2 bytes — that is exactly what
    lands in VRAM.  Layers × hidden² is only the fallback when the headers
    cannot be read.
    """
    import json

    disk = float(disk_gib if disk_gib is not None else weight_gib(checkpoint_dir))
    params = _safetensors_params(checkpoint_dir)
    if params > 0:
        est = params * 2.0 / 1024 ** 3
    else:
        est = 0.0
        try:
            data = json.loads((Path(str(checkpoint_dir)) / "config.json")
                              .read_text(encoding="utf-8"))
            hidden = int(data.get("hidden_size") or 0)
            layers = int(data.get("num_hidden_layers") or 0)
            if hidden and layers:
                est = layers * 12.0 * hidden * hidden * 2.0 / 1024 ** 3 * 1.6
        except (OSError, ValueError, TypeError):
            est = 0.0
        if est <= 0:
            est = disk * 0.6
    if disk > 0:
        est = min(disk, est)
    return round(max(0.5, est), 2)


def snapshot(probe: dict | None) -> dict:
    """Normalise a ``runtime.probe_hardware`` result for the policy functions."""
    probe = probe or {}
    gpu = probe.get("gpu") or {}
    return {
        "has_gpu": bool(gpu),
        "gpu_name": str(gpu.get("name") or ""),
        "gpu_total_gib": float(gpu.get("total_gib") or 0.0),
        "gpu_free_gib": float(gpu.get("free_gib") or 0.0),
        # nvidia-smi 给的是 "7.5" 这样的字符串；解析失败按 0 处理，
        # 后面 capability_tuple 会退回"当新卡看"，不因为缺字段就误降级。
        "gpu_compute_cap": str(gpu.get("compute_cap") or ""),
        "ram_total_gib": float(probe.get("ram_total_gib") or 0.0),
        "ram_available_gib": float(probe.get("ram_available_gib") or 0.0),
    }


def capability_tuple(mem: dict) -> tuple[int, int]:
    """Parse ``compute_cap`` ("7.5") into ``(7, 5)``; (0, 0) when unknown."""
    raw = str((mem or {}).get("gpu_compute_cap") or "").strip()
    if "." in raw:
        head, tail = raw.split(".", 1)
        try:
            return int(head), int(tail)
        except ValueError:
            return 0, 0
    try:
        return int(raw), 0
    except ValueError:
        return 0, 0


def reserve_gib(mem: dict) -> float:
    """VRAM kept free for the desktop; scales down on small cards."""
    total = float((mem or {}).get("gpu_total_gib") or 0.0)
    if total <= 0:
        return RESERVE_GIB
    return max(RESERVE_MIN_GIB, min(RESERVE_GIB, total * 0.06))


def usable_gib(mem: dict) -> float:
    """Free VRAM minus the desktop reserve (never negative)."""
    return max(0.0, float((mem or {}).get("gpu_free_gib") or 0.0) - reserve_gib(mem))


def dtype_for(mem: dict) -> str:
    """Native compute dtype for this GPU.

    bf16 needs Ampere (sm_80+); Turing/Pascal fall back to fp16, and CPU is
    fp32.  Getting this wrong is fatal, not cosmetic: the weights are loaded
    in this dtype while every tensor built without an explicit dtype uses
    ``torch.set_default_dtype`` — a mismatch raises on the first matmul.
    """
    major, _minor = capability_tuple(mem)
    if major >= 8:
        return "bfloat16"
    if major >= 6:
        return "float16"
    return "float32"


def quantization_choices(mem: dict) -> list[str]:
    """Quantization modes this GPU can actually execute."""
    major, _minor = capability_tuple(mem)
    out = []
    # int8 走 torch 的 int8pack/int_mm 内核，Turing（sm_75）起才硬件支持。
    if major >= 7:
        out.append("int8_weight_only")
    # 动态 int8 激活依赖 Ampere 的 GEMM 内核。
    if major >= 8:
        out.append("w8a8_dynamic")
    # fp8 需要 Ada/Blackwell 的 fp8 tensor core。
    if major >= 8.9:
        out.append("fp8_weight_only")
    return out


def pick_quantization(mem: dict, weight_gib: float) -> str | None:
    """Choose a quantization mode that lets *weight_gib* fit, or ``None``.

    Weights-only int8 is preferred over the activation-quantized variants:
    it keeps activations in fp16 (same numerics as an unquantized run) and
    saves exactly what a full-VRAM residency needs — weight bytes.
    """
    available = quantization_choices(mem)
    if "int8_weight_only" not in available:
        return None
    return "int8_weight_only"


def gpu_compat(mem: dict, torch_archs: list[str] | None = None) -> dict:
    """Describe what this exact GPU + torch build can do with ACE-Step.

    ``torch_archs`` is ``torch.cuda.get_arch_list()`` when available; without
    it the wheel check is skipped (hardware facts still reported).
    """
    major, minor = capability_tuple(mem)
    cap = f"{major}.{minor}" if major else "unknown"
    generation = "未知型号"
    for floor, label in _GPU_GENERATIONS:
        if major and major + minor / 10.0 >= floor:
            generation = label
            break
    notes = []
    supported = bool(mem.get("has_gpu"))
    if supported and not major:
        notes.append("读不到计算能力，按新卡处理")
    if supported and major and major < 6:
        supported = False
        notes.append(f"sm_{major}{minor} 太老，ACE-Step 的算子用不了这张卡")
    # 随插件安装的是 cu128 的 torch（最低 sm_70）。Pascal/Maxwell 虽然有
    # 计算能力、却没有内核，硬跑会在第一个 kernel 报 "no kernel image"。
    if supported and major and major < 7 and not torch_archs:
        supported = False
        notes.append(
            f"sm_{major}{minor} 不在当前 PyTorch（cu128，最低 sm_70）的支持范围内；"
            "要用这张卡需要换 cu118 时代的 torch 构建，否则只能走 CPU 生成")
    if supported and torch_archs:
        ok = False
        for tag in torch_archs:
            # torch 用 "sm_75" / "compute_120" 这种写法：数字是大小版本拼在一起的
            # （75 → 7.5，120 → 12.0），不是点的两段。
            digits = "".join(ch for ch in str(tag) if ch.isdigit())
            if not digits:
                continue
            if len(digits) == 1:
                arch_major, arch_minor = int(digits), 0
            else:
                arch_major, arch_minor = int(digits[:-1]), int(digits[-1])
            # 同一大版本、小版本不高于本卡的 cubin 可以向前兼容；
            # PTX（compute_XX）还能继续 JIT 到更新的卡。
            if arch_major == major and arch_minor <= minor:
                ok = True
                break
            if str(tag).startswith("compute_") and arch_major <= major:
                ok = True
                break
        if not ok:
            supported = False
            notes.append(
                f"当前 PyTorch 只编译了 {', '.join(torch_archs)}，没有 sm_{major}{minor} "
                "能用的内核；换 cu118 时代的 torch（含 sm_50~sm_89）才能用这张卡")
    return {
        "name": str(mem.get("gpu_name") or ""),
        "compute_cap": cap,
        "generation": generation,
        "dtype": dtype_for(mem),
        "supported": supported,
        "quantization": quantization_choices(mem),
        "notes": notes,
    }


def _plan(mode: str, reason: str, *, warnings=(), fatal: str = "",
          quantization: str | None = None, vae_on_cpu: bool = False,
          max_candidates: int | None = None) -> dict:
    return {"mode": mode, "label": MODE_LABELS.get(mode, mode),
            "reason": reason, "warnings": list(warnings),
            "fatal": fatal,
            "quantization": quantization,
            "vae_on_cpu": bool(vae_on_cpu),
            "max_candidates": max_candidates}


def ace_plan(mem: dict, model_gib: float, *, is_xl: bool,
             duration_s: float = 60.0, batch: int = 1,
             torch_archs: list[str] | None = None) -> dict:
    """Pick a runnable strategy for one ACE-Step generation.

    ``model_gib`` is the model's resident size (see ``estimate_runtime_gib``).
    Activation memory grows with duration and candidate count, and CFG doubles
    it; the estimate is deliberately pessimistic because an OOM mid-generation
    wastes minutes.

    The ladder is: full VRAM → int8 in VRAM → VAE/encoder offload → per-layer
    streaming → CPU.  On a 4GB card the int8 model (2.82GiB measured) plus the
    CUDA context and activations does not fit reliably, so 4GB lands on
    per-layer streaming — only ~1.7GiB of non-block weights stay resident.
    6GB and up use int8 residency, which is much faster.
    """
    model_gib = max(0.1, float(model_gib or 0.1))
    batch = max(1, int(batch or 1))
    act = (1.2 if is_xl else 0.5) * max(1.0, float(duration_s or 60.0) / 60.0) * batch
    weights = model_gib
    warnings = []
    if not mem.get("has_gpu"):
        return _plan("cpu", "没有检测到可用显卡，全部放内存用 CPU 生成（会很慢）",
                     vae_on_cpu=True, max_candidates=1)
    compat = gpu_compat(mem, torch_archs)
    if not compat["supported"]:
        # 显卡在，但这一版 torch 没编进这张卡的算子：硬跑会在第一个 kernel
        # 就 CUDA error，不如直接说清楚并退回 CPU。
        return _plan("cpu",
                     "这张卡和当前 PyTorch 不匹配：" + "；".join(compat["notes"]),
                     vae_on_cpu=True, max_candidates=1)
    usable = usable_gib(mem)
    ram_ok = (float(mem.get("ram_available_gib") or 0.0)
              >= weights * 1.15 + RAM_HEADROOM_GIB)
    # 6GB 以下的卡 VAE 解码容易顶到天花板，直接用 CPU 解码（慢一点但稳）。
    vae_on_cpu = usable < 6.0
    dtype_note = "" if dtype_for(mem) == "bfloat16" else f"，{dtype_for(mem)} 精度"
    # 4B XL 在 ACE 自带的 CPU offload 路径上有原生崩溃记录；12GB 以上的卡
    # 保持“全部驻留”这一条已知可用的路，不走量化/卸载那两档。
    if is_xl and float(mem.get("gpu_total_gib") or 0.0) >= 12.0:
        return _plan("gpu", f"XL 模型放在 {usable:.1f}GB 显存里跑（不卸载）{dtype_note}",
                     warnings=["XL 需要至少 12GB 显存"], vae_on_cpu=False,
                     max_candidates=batch)
    if usable >= weights * 1.12 + act:
        return _plan("gpu", f"显存够用：模型约 {weights:.1f}GB，空闲 {usable:.1f}GB{dtype_note}",
                     vae_on_cpu=vae_on_cpu, max_candidates=batch)
    # 量化只用在 2B 上：XL 的卸载/量化路径有原生崩溃记录，收益抵不上风险。
    quant = None if is_xl else pick_quantization(mem, weights)
    quant_weights = weights * QUANT_WEIGHT_RATIO
    # 实测 2B turbo 量化后常驻 2.82GiB；4GB 卡上整模型搬上卡仍会 OOM
    # （CUDA context/驱动本身也要几百 MB），所以额外留 0.8GB 余量。
    if quant and usable >= quant_weights + act + 0.8:
        return _plan(
            "quant",
            f"显存偏小：模型约 {weights:.1f}GB，量化后约 {quant_weights:.1f}GB，"
            f"可以放进 {usable:.1f}GB{dtype_note}",
            warnings=warnings, quantization=quant, vae_on_cpu=vae_on_cpu,
            max_candidates=1 if batch > 1 else batch)
    if usable >= weights * 0.9 + act:
        return _plan("offload",
                     f"显存偏紧：VAE/文本编码器移到内存，DiT 仍放显存"
                     f"（约 {weights * 0.9:.1f}GB）",
                     vae_on_cpu=vae_on_cpu, max_candidates=batch)
    layer_need = max(0.8, weights * 0.12) * (1.6 if is_xl else 1.0)
    # 4B XL 在 ACE 自带的 offload_dit_to_cpu 路径上有原生崩溃记录，
    # 逐层卸载复用了同一套“先放 CPU”的加载方式，所以 XL 不走这一档。
    if ram_ok and not is_xl and usable >= layer_need + act:
        if quant:
            warnings.append("连 int8 都放不下，改用逐层卸载（速度会明显变慢）")
        else:
            warnings.append("这张卡的算力/依赖不支持 int8 量化，只能逐层搬运")
        return _plan("layer",
                     f"显存不足但内存够：DiT 留在内存，按层搬上显存"
                     f"（峰值约 {layer_need + act:.1f}GB，速度会明显变慢）",
                     warnings=warnings, vae_on_cpu=True, max_candidates=1)
    if not ram_ok:
        return _plan(
            "cpu",
            f"内存也不够：模型约 {weights:.1f}GB，可用内存只有 "
            f"{float(mem.get('ram_available_gib') or 0.0):.1f}GB；"
            f"请关闭其他程序或换更小的模型",
            warnings=[f"需要至少 {weights * 1.15 + RAM_HEADROOM_GIB:.1f}GB 可用内存"],
            fatal="内存不足，无法把模型放进内存",
            vae_on_cpu=True, max_candidates=1)
    if is_xl:
        warnings.append("4B XL 模型在低显存下逐层卸载很慢，建议改用 2B 模型")
    if usable < 0.4:
        warnings.append("空闲显存过低，逐层搬运也可能失败，失败后会自动改用 CPU")
    return _plan("cpu",
                 f"显存不足以逐层搬运（空闲 {usable:.1f}GB），改用 CPU 生成",
                 warnings=warnings, vae_on_cpu=True, max_candidates=1)


def ace_attempts(job: dict, plan: dict, *, is_xl: bool) -> list[dict]:
    """OOM ladder: start at the chosen mode, then fall back further.

    Each step keeps the same request; only where the weights live (and whether
    they are quantized) changes.  Quantization is dropped before switching to
    layer streaming: a quantized tensor that OOMs means the card is too small
    for residency, and streaming unquantized blocks is the reliable path.
    """
    mode = plan.get("mode") or "gpu"
    quant = plan.get("quantization")
    steps: list[tuple[str, str | None]] = []
    if mode == "gpu":
        steps.append(("gpu", None))
    elif mode == "quant":
        if quant:
            steps.append(("quant", quant))
        steps.append(("layer", None))
    elif mode == "offload":
        steps.append(("offload", None))
        steps.append(("layer", None))
    elif mode == "layer":
        steps.append(("layer", None))
    steps.append(("cpu", None))
    # 4B XL 的逐层卸载会触发 ACE 的原生崩溃，直接跳 CPU
    steps = [s for s in steps if not (is_xl and s[0] == "layer")]
    out: list[dict] = []
    seen = set()
    for step_mode, step_quant in steps:
        key = (step_mode, step_quant)
        if key in seen:
            continue
        seen.add(key)
        attempt = dict(job)
        attempt.update(flags_for(step_mode))
        attempt["vram_mode"] = step_mode
        attempt["quantization"] = step_quant
        out.append(attempt)
        if step_mode == "cpu":
            break
    return out


def flags_for(mode: str) -> dict:
    """Concrete flags understood by ace_worker / AceStepRuntimeConfig."""
    if mode == "gpu":
        return {"device": "cuda", "offload_to_cpu": False,
                "offload_dit_to_cpu": False, "layer_offload": False}
    if mode == "quant":
        # 关键：offload_dit_to_cpu=True 让模型在 CPU 上量化、之后按需搬上显存。
        # 若为 False，ACE 会在加载阶段就把 4.8GB 的 fp16 权重塞进显存，
        # 4GB 的卡在 initialize 就 OOM 了，根本走不到量化那一步。
        return {"device": "cuda", "offload_to_cpu": True,
                "offload_dit_to_cpu": True, "layer_offload": False}
    if mode == "offload":
        return {"device": "cuda", "offload_to_cpu": True,
                "offload_dit_to_cpu": False, "layer_offload": False}
    if mode == "layer":
        # DiT 留在内存；ace_runtime 会逐层搬到 GPU 再搬回。
        return {"device": "cuda", "offload_to_cpu": True,
                "offload_dit_to_cpu": True, "layer_offload": True}
    return {"device": "cpu", "offload_to_cpu": True,
            "offload_dit_to_cpu": False, "layer_offload": False}


def ddsp_device(mem: dict, *, need_gib: float = 1.2) -> dict:
    """DDSP-SVC is small; only fall back to CPU when VRAM is really tight."""
    if not mem.get("has_gpu"):
        return {"device": "cpu", "reason": "没有可用显卡，改用 CPU 变声"}
    usable = usable_gib(mem)
    if usable >= need_gib:
        return {"device": "cuda", "reason": f"显存可用 {usable:.1f}GB"}
    return {"device": "cpu",
            "reason": f"显存只剩 {max(0.0, usable):.1f}GB，改用 CPU 变声（慢但不失败）"}


def separation_plan(mem: dict) -> dict:
    """Separation already chunks long audio; choose the device the same way."""
    if not mem.get("has_gpu"):
        return {"device": "cpu", "reason": "没有可用显卡，CPU 分离"}
    usable = usable_gib(mem)
    if usable >= 2.0:
        return {"device": "cuda:0", "reason": f"显存可用 {usable:.1f}GB"}
    return {"device": "cpu",
            "reason": f"显存只剩 {max(0.0, usable):.1f}GB，改用 CPU 分离"}


def describe(plan: dict) -> str:
    text = f"{plan.get('label', plan.get('mode'))}：{plan.get('reason', '')}"
    quant = plan.get("quantization")
    if quant:
        text += f"（{QUANT_LABELS.get(quant, quant)}）"
    extra = [w for w in (plan.get("warnings") or []) if w]
    if extra:
        text += "；" + "；".join(extra)
    return text
