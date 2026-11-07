from __future__ import annotations

import importlib
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_OUTPUT_DIR = os.path.join(PROJECT_ROOT, "generated_songs", "acestep_local")


@dataclass
class AceStepRuntimeConfig:
    project_root: str = os.getenv("ACESTEP_PROJECT_ROOT", PROJECT_ROOT)
    model_name: str = "acestep-v15-turbo"
    device: str = "auto"
    use_flash_attention: bool = False
    compile_model: bool = False
    offload_to_cpu: bool = False
    offload_dit_to_cpu: bool = False
    # 低显存兼容：DiT 每一层按需搬到 GPU，模型可以大于显存（很慢但能跑）。
    layer_offload: bool = False
    quantization: Optional[str] = None
    # 权重加载完成后再设的默认 dtype（"float16"）。老卡（Turing/Volta）需要
    # 但它不能在加载前设：torch.set_default_dtype(float16) + 自定义模型加载
    # 会在 safetensors 物化阶段原生崩溃（access violation，可复现）。
    default_dtype: str = ""
    prefer_source: Optional[str] = None
    use_mlx_dit: bool = True
    output_dir: str = DEFAULT_OUTPUT_DIR
    lora_path: Optional[str] = None


@dataclass
class AceStepGenerateRequest:
    prompt: str = ""
    lyrics: str = ""
    task_type: str = "cover"
    instruction: Optional[str] = None
    model_name: Optional[str] = None

    reference_audio_path: Optional[str] = None
    src_audio_path: Optional[str] = None
    audio_codes: str = ""

    instrumental: bool = False
    vocal_language: str = "unknown"
    bpm: Optional[int] = None
    keyscale: str = ""
    timesignature: str = ""
    duration: Optional[float] = None

    enable_normalization: bool = True
    normalization_db: float = -1.0
    latent_shift: float = 0.0
    latent_rescale: float = 1.0

    inference_steps: int = 12
    guidance_scale: float = 7.0
    use_random_seed: bool = True
    seed: int = -1
    seeds: Optional[Sequence[int]] = None
    batch_size: int = 1

    use_adg: bool = False
    cfg_interval_start: float = 0.0
    cfg_interval_end: float = 1.0
    infer_method: str = "ode"
    shift: float = 3.0
    timesteps: Optional[Sequence[float] | str] = None

    repainting_start: float = 0.0
    repainting_end: Optional[float] = None
    audio_cover_strength: float = 1.0
    cover_noise_strength: float = 0.0

    # ACE-Step v0.1.7+ DCW 采样校正（默认开；关掉可用于排查显存/兼容问题）
    dcw_enabled: bool = True
    dcw_mode: str = "double"
    dcw_scaler: float = 0.05
    dcw_high_scaler: float = 0.02
    dcw_wavelet: str = "haar"

    audio_format: str = "wav"


@dataclass
class AceStepGenerateResult:
    success: bool
    status_message: str
    audio_paths: List[str] = field(default_factory=list)
    audios: List[Dict[str, Any]] = field(default_factory=list)
    prompt: str = ""
    lyrics: str = ""
    metas: Dict[str, Any] = field(default_factory=dict)
    extra_outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class AceStepLocalRuntime:
    """Embedded ACE-Step runtime that does not depend on the original project path."""

    def __init__(self, config: Optional[AceStepRuntimeConfig] = None):
        self.config = config or AceStepRuntimeConfig()
        self._lock = threading.Lock()
        self._dit_handler = None
        self._active_model: Optional[str] = None

    def initialize(self, model_name: Optional[str] = None) -> None:
        target_model = model_name or self.config.model_name
        with self._lock:
            if self._dit_handler is not None and self._active_model == target_model:
                return

            runtime = self._import_runtime()
            dit_handler = runtime["AceStepHandler"]()
            status, ok = dit_handler.initialize_service(
                project_root=self.config.project_root,
                config_path=target_model,
                device=self.config.device,
                use_flash_attention=self.config.use_flash_attention,
                compile_model=self.config.compile_model,
                offload_to_cpu=self.config.offload_to_cpu,
                offload_dit_to_cpu=self.config.offload_dit_to_cpu,
                quantization=self.config.quantization,
                prefer_source=self.config.prefer_source,
                use_mlx_dit=self.config.use_mlx_dit,
            )
            if not ok:
                raise RuntimeError(status)

            if self.config.lora_path:
                dit_handler.load_lora(self.config.lora_path)
            if self.config.layer_offload:
                # 必须放在 LoRA 之后：LoRA 会重建/替换部分层参数。
                enable_layer_offload(dit_handler, self.config.device)
            if self.config.default_dtype:
                try:
                    import torch
                    torch.set_default_dtype(
                        getattr(torch, str(self.config.default_dtype)))
                except Exception:  # noqa: BLE001 - 设不上就按框架默认走
                    pass
            self._dit_handler = dit_handler
            self._active_model = target_model

    def unload(self) -> None:
        with self._lock:
            handler = self._dit_handler
            self._dit_handler = None
            self._active_model = None

        if handler is not None:
            for attr in (
                "model",
                "vae",
                "text_encoder",
                "text_tokenizer",
                "silence_latent",
                "reward_model",
                "config",
                "mlx_decoder",
                "mlx_vae",
            ):
                if hasattr(handler, attr):
                    try:
                        setattr(handler, attr, None)
                    except Exception:
                        pass
            del handler

        try:
            import gc
            gc.collect()
        except Exception:
            pass
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
            elif hasattr(torch, "xpu") and torch.xpu.is_available():
                torch.xpu.empty_cache()
                torch.xpu.synchronize()
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                torch.mps.empty_cache()
        except Exception:
            pass

    def generate(self, request: AceStepGenerateRequest, save_dir: Optional[str] = None) -> AceStepGenerateResult:
        self._validate_request(request)
        self.initialize(request.model_name)

        runtime = self._import_runtime()
        params = runtime["GenerationParams"](
            task_type=request.task_type,
            instruction=request.instruction or runtime["DEFAULT_DIT_INSTRUCTION"],
            reference_audio=request.reference_audio_path,
            src_audio=request.src_audio_path,
            audio_codes=request.audio_codes,
            caption=request.prompt,
            lyrics=request.lyrics,
            instrumental=request.instrumental,
            vocal_language=request.vocal_language,
            bpm=request.bpm,
            keyscale=request.keyscale,
            timesignature=request.timesignature,
            duration=float(request.duration) if request.duration is not None else -1.0,
            enable_normalization=request.enable_normalization,
            normalization_db=request.normalization_db,
            latent_shift=request.latent_shift,
            latent_rescale=request.latent_rescale,
            inference_steps=request.inference_steps,
            seed=request.seed,
            guidance_scale=request.guidance_scale,
            use_adg=request.use_adg,
            cfg_interval_start=request.cfg_interval_start,
            cfg_interval_end=request.cfg_interval_end,
            shift=request.shift,
            infer_method=request.infer_method,
            timesteps=self._normalize_timesteps(request.timesteps),
            repainting_start=request.repainting_start,
            repainting_end=-1 if request.repainting_end is None else request.repainting_end,
            audio_cover_strength=request.audio_cover_strength,
            cover_noise_strength=request.cover_noise_strength,
            dcw_enabled=bool(request.dcw_enabled),
            dcw_mode=str(request.dcw_mode or "double"),
            dcw_scaler=float(request.dcw_scaler),
            dcw_high_scaler=float(request.dcw_high_scaler),
            dcw_wavelet=str(request.dcw_wavelet or "haar"),
            thinking=False,
            use_cot_metas=False,
            use_cot_caption=False,
            use_cot_lyrics=False,
            use_cot_language=False,
            use_constrained_decoding=False,
        )

        config = runtime["GenerationConfig"](
            batch_size=max(1, int(request.batch_size)),
            allow_lm_batch=False,
            use_random_seed=request.use_random_seed,
            seeds=self._normalize_seeds(request),
            lm_batch_chunk_size=1,
            constrained_decoding_debug=False,
            audio_format=request.audio_format,
        )

        output_dir = save_dir or self.config.output_dir
        os.makedirs(output_dir, exist_ok=True)

        result = runtime["generate_music"](
            self._dit_handler,
            None,
            params,
            config,
            save_dir=output_dir,
        )

        audio_paths = [audio.get("path") for audio in result.audios if audio.get("path")]
        metas = {
            "bpm": params.bpm,
            "duration": params.duration,
            "keyscale": params.keyscale,
            "timesignature": params.timesignature,
            "vocal_language": params.vocal_language,
            "task_type": params.task_type,
            "audio_cover_strength": params.audio_cover_strength,
            "cover_noise_strength": params.cover_noise_strength,
        }
        return AceStepGenerateResult(
            success=result.success,
            status_message=result.status_message,
            audio_paths=audio_paths,
            audios=result.audios,
            prompt=params.caption,
            lyrics=params.lyrics,
            metas=metas,
            extra_outputs=result.extra_outputs or {},
            error=result.error,
        )

    def _import_runtime(self) -> Dict[str, Any]:
        # Vendored ACE-Step code lives under ./vendor (Market Ruff excludes it).
        # Older installs kept it at the plugin root; keep both on sys.path so an
        # in-place upgrade of an existing installation still works.
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parent
        for candidate in (root / "vendor", root):
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
        try:
            handler_module = importlib.import_module("acestep.handler")
            inference_module = importlib.import_module("acestep.inference")
            constants_module = importlib.import_module("acestep.constants")
        except ImportError as exc:
            raise RuntimeError(
                "Embedded ACE-Step runtime is present in the project, but its Python dependencies are not fully installed."
            ) from exc

        return {
            "AceStepHandler": handler_module.AceStepHandler,
            "GenerationParams": inference_module.GenerationParams,
            "GenerationConfig": inference_module.GenerationConfig,
            "generate_music": inference_module.generate_music,
            "DEFAULT_DIT_INSTRUCTION": constants_module.DEFAULT_DIT_INSTRUCTION,
        }

    def _validate_request(self, request: AceStepGenerateRequest) -> None:
        if request.reference_audio_path:
            self._validate_audio_path(request.reference_audio_path, "reference_audio_path")
        if request.src_audio_path:
            self._validate_audio_path(request.src_audio_path, "src_audio_path")
        if request.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if request.task_type in {"cover", "repaint", "lego", "extract", "complete"} and not request.src_audio_path:
            raise ValueError(f"task_type='{request.task_type}' requires src_audio_path")
        if request.task_type in {"cover", "repaint", "lego"} and not request.reference_audio_path:
            raise ValueError(f"task_type='{request.task_type}' requires reference_audio_path")

    @staticmethod
    def _validate_audio_path(path: str, field_name: str) -> None:
        if not os.path.isfile(path):
            raise FileNotFoundError(f"{field_name} not found: {path}")

    @staticmethod
    def _normalize_seeds(request: AceStepGenerateRequest) -> Optional[List[int]]:
        if request.seeds:
            return [int(seed) for seed in request.seeds]
        if request.use_random_seed or request.seed is None or int(request.seed) < 0:
            return None
        return [int(request.seed)]

    @staticmethod
    def _normalize_timesteps(timesteps: Optional[Sequence[float] | str]) -> Optional[List[float]]:
        if timesteps is None:
            return None
        if isinstance(timesteps, str):
            values = [item.strip() for item in timesteps.split(",") if item.strip()]
            if not values:
                return None
            return [float(item) for item in values]
        values = [float(item) for item in timesteps]
        return values or None


def enable_layer_offload(handler, device: str = "cuda") -> None:
    """Stream DiT transformer blocks instead of keeping the whole model on GPU.

    ACE-Step's own offload still moves the entire DiT to the GPU for the
    diffusion stage, so a model larger than VRAM cannot run there.  Holding
    each transformer block in system RAM and moving one block at a time is
    what makes such a model run at all — slower, because every block crosses
    PCIe on every step, but it does not need the whole model in VRAM.

    Everything that is not a DiT block (patch projection, rotary embedding,
    condition encoder, tokenizer) stays resident, so it must still fit.
    """
    import contextlib

    import torch

    model = getattr(handler, "model", None)
    if model is None:
        raise RuntimeError("layer_offload：模型尚未加载")
    decoder = getattr(model, "decoder", None)
    blocks = list(getattr(decoder, "layers", []) or [])
    if not blocks:
        raise RuntimeError("layer_offload：找不到 DiT 的 transformer 层")
    device = str(device or "")
    if device in ("", "auto"):
        device = str(getattr(handler, "device", "cuda") or "cuda")
    if device.startswith("cpu") or not torch.cuda.is_available():
        raise RuntimeError("layer_offload 需要可用的 CUDA 设备")

    # 先整体回内存，再只把“非逐层”的部分搬上去，峰值不会超过整模型。
    model.to("cpu")
    prefix = "decoder.layers."
    for name, param in model.named_parameters():
        if not name.startswith(prefix):
            param.data = param.data.to(device)
    for name, buf in model.named_buffers():
        if not name.startswith(prefix):
            try:
                buf.data = buf.data.to(device)
            except (AttributeError, RuntimeError, TypeError):
                pass
    latent = getattr(handler, "silence_latent", None)
    if latent is not None:
        handler.silence_latent = latent.to(device)

    wrapped = 0
    for block in blocks:
        original = block.forward

        def forward(*args, __orig=original, __block=block, **kwargs):
            __block.to(device)
            try:
                return __orig(*args, **kwargs)
            finally:
                __block.to("cpu")

        block.forward = forward
        wrapped += 1

    original_context = handler._load_model_context

    @contextlib.contextmanager
    def _model_context(name):
        if name == "model":
            # 不再整体搬运：每一层进出显存由上面的 forward 负责。
            yield
            return
        with original_context(name):
            yield

    handler._load_model_context = _model_context
    handler.layer_offload_blocks = wrapped


def generate_with_acestep_local(
    request: AceStepGenerateRequest,
    runtime_config: Optional[AceStepRuntimeConfig] = None,
    save_dir: Optional[str] = None,
) -> AceStepGenerateResult:
    runtime = AceStepLocalRuntime(runtime_config)
    try:
        return runtime.generate(request, save_dir=save_dir)
    finally:
        runtime.unload()
