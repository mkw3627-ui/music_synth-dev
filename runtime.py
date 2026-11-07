"""Shared runtime helpers for the 为她而唱 (music_synth) plugin.

Nothing in this module imports the N.E.K.O host SDK, so the same code can be
used from the plugin process and from small diagnostic scripts.

Design rules for distribution (2026-10-31):

* Never assume a fixed install drive.  Paths come from config.json, then the
  host's ``[music_synth]`` section, then a legacy install if one is found, then
  the largest non-system drive with enough free space.
* Hardware is probed on **every** run.  The plan is derived from *free* VRAM
  (not the card's nameplate) and the GPU's compute capability, so a busy
  16GB card and an idle 6GB card get different settings.
* No CUDA => CPU route with an explicit warning, never a silent crash.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Legacy defaults (kept only so an existing dev install keeps working)
# ---------------------------------------------------------------------------
LEGACY_COMPUTE_ROOT = Path(r"E:\NekoMusicSynth")
DEFAULT_DOWNLOAD_OWNER = "Soul-AILab"

TIER_ORDER = ("low", "mid", "high")

# Ceiling per tier.  The runtime may reduce these further when the machine is
# busy; it never raises them above what free VRAM allows.
TIER_PLANS = {
    "low": {
        "label": "低配 · 放开跑",
        "stems": 2,
        "sep_model": "mel_band_roformer_karaoke_becruily.ckpt",
        "sep_step": 8,
        "synth_precision": "fp16",
        "synth_n_steps": 16,
        "parallel": 1,
        "sample_rate": 44100,
    },
    "mid": {
        "label": "中配 · 串行预取",
        "stems": 2,
        "sep_model": "mel_band_roformer_karaoke_becruily.ckpt",
        "sep_step": 4,
        "synth_precision": "fp16",
        "synth_n_steps": 24,
        "parallel": 1,
        "sample_rate": 48000,
    },
    "high": {
        "label": "高配 · 主动分配",
        "stems": 2,
        "sep_model": "mel_band_roformer_karaoke_becruily.ckpt",
        "sep_step": 2,
        "synth_precision": "fp16",
        "synth_n_steps": 32,
        "parallel": 2,
        "sample_rate": 48000,
    },
}

# DDSP-SVC (RectifiedFlow + NSF-HiFiGAN) runs on the GPU; 5.8GB free is the
# measured floor for the synth stage with context + a busy card, not the model
# alone.  Below it the stage falls back to CPU.
SYNTH_GPU_MIN_GIB = 5.8
SEP_GPU_MIN_GIB = 2.0

_PROBE_CACHE: dict = {"at": 0.0, "value": None}
_PROBE_TTL_S = 2.0


# ---------------------------------------------------------------------------
# Hardware probe
# ---------------------------------------------------------------------------
def _run_quiet(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def _windows_memory_gib() -> tuple[float, float]:
    """Total / available physical RAM without requiring psutil."""
    try:
        import ctypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        state = _MEMORYSTATUSEX()
        state.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            return state.ullTotalPhys / 2**30, state.ullAvailPhys / 2**30
    except Exception:  # noqa: BLE001 - probe must never raise
        pass
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        return vm.total / 2**30, vm.available / 2**30
    except Exception:  # noqa: BLE001
        return 0.0, 0.0


def _nvidia_smi_gpu() -> dict | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        # Common install locations, in case PATH is minimal.
        for candidate in (
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            r"C:\Windows\System32\nvidia-smi.exe",
        ):
            if Path(candidate).is_file():
                exe = candidate
                break
    if not exe:
        return None
    raw = _run_quiet([
        exe, "--query-gpu=name,memory.total,memory.used,memory.free,"
              "compute_cap,driver_version",
        "--format=csv,noheader,nounits",
    ], timeout=8.0)
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    if not line:
        return None
    parts = [p.strip() for p in line.split(",")]
    try:
        name = parts[0]
        total = float(parts[1])
        used = float(parts[2])
        free = float(parts[3])
    except (IndexError, ValueError):
        return None
    cc_major = cc_minor = 0
    if len(parts) > 4 and "." in parts[4]:
        try:
            cc_major, cc_minor = (int(x) for x in parts[4].split(".", 1))
        except ValueError:
            cc_major = cc_minor = 0
    return {
        "name": name,
        "total_gib": round(total / 1024.0, 2),
        "used_gib": round(used / 1024.0, 2),
        "free_gib": round(free / 1024.0, 2),
        "compute_cap": f"{cc_major}.{cc_minor}" if cc_major else "unknown",
        "cc_major": cc_major,
        "cc_minor": cc_minor,
        "driver": parts[5] if len(parts) > 5 else "",
        "source": "nvidia-smi",
    }


def _disk_free_gib(path: Path) -> float:
    try:
        return shutil.disk_usage(str(path)).free / 2**30
    except OSError:
        return 0.0


def _drives() -> list[dict]:
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        try:
            root = Path(f"{letter}:\\")
            if not root.exists():
                continue
            free = _disk_free_gib(root)
        except OSError:  # e.g. WinError 87 for a disconnected/empty drive letter
            continue
        if free <= 0:
            continue
        out.append({"drive": f"{letter}:", "free_gib": round(free, 1)})
    return out


def probe_hardware(force: bool = False) -> dict:
    """Return a fresh hardware snapshot (GPU, RAM, CPU, disks)."""
    now = time.time()
    if (not force and _PROBE_CACHE["value"] is not None
            and now - _PROBE_CACHE["at"] < _PROBE_TTL_S):
        return dict(_PROBE_CACHE["value"])
    ram_total, ram_free = _windows_memory_gib()
    value = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gpu": _nvidia_smi_gpu(),
        "ram_total_gib": round(ram_total, 1),
        "ram_available_gib": round(ram_free, 1),
        "cpu": platform.processor() or platform.machine(),
        "os": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "drives": _drives(),
    }
    _PROBE_CACHE["at"] = now
    _PROBE_CACHE["value"] = value
    return dict(value)


def probe_brief(probe: dict | None = None) -> str:
    probe = probe or probe_hardware()
    gpu = probe.get("gpu")
    if gpu:
        return (f"{gpu['name']} · 显存 {gpu['total_gib']}GB（空闲 {gpu['free_gib']}GB）"
                f" · sm_{gpu['compute_cap']} · 驱动 {gpu.get('driver') or '?'}")
    return ("未检测到 NVIDIA 显卡（nvidia-smi 不可用）"
            f" · 内存 {probe.get('ram_total_gib')}GB（空闲 "
            f"{probe.get('ram_available_gib')}GB）")


# ---------------------------------------------------------------------------
# Install root discovery (empty user machines)
# ---------------------------------------------------------------------------
def _has_install(root: Path) -> bool:
    try:
        return ((root / "envs").is_dir() and any((root / "envs").iterdir())) \
            or ((root / "models").is_dir() and any((root / "models").iterdir()))
    except OSError:
        return False


def default_install_root() -> Path:
    """Prefer the non-system drive with the most free space (>= 8GB)."""
    candidates = [d for d in _drives() if d["drive"].upper() != "C:"]
    candidates.sort(key=lambda d: d["free_gib"], reverse=True)
    if candidates and candidates[0]["free_gib"] >= 8.0:
        return Path(candidates[0]["drive"] + "\\NEKOMusicSynth")
    # Everything is a system drive or tight: use the user profile (writable).
    try:
        home = Path.home()
    except RuntimeError:
        home = Path(os.environ.get("USERPROFILE") or "C:\\")
    return home / "NEKOMusicSynth"


def legacy_install_root() -> Path | None:
    for candidate in (LEGACY_COMPUTE_ROOT, Path(r"D:\NekoMusicSynth")):
        if _has_install(candidate):
            return candidate
    return None


def _cfg_value(cfg: dict, key: str, default: str = "") -> str:
    value = cfg.get(key)
    if value is None:
        return default
    value = str(value).strip()
    return value or default


# ---------------------------------------------------------------------------
# 底模（训练起点）永远不算「训练好的声音」，任何列表/当前模型都不显示。
# ---------------------------------------------------------------------------
BASE_DIR_MARKERS = ("底模", "底膜", "base", "pretrain", "预训练", "初始模型")


def is_base_model(path, ddsp_root=None, base_ckpt=None) -> bool:
    """True when ``path`` is a training base/seed checkpoint, not a trained voice."""
    text = str(path or "").strip()
    if not text:
        return False
    p = Path(text)
    try:
        p = p.resolve()
    except OSError:
        pass
    if base_ckpt:
        try:
            b = Path(str(base_ckpt)).resolve()
            if p == b or p.parent == b.parent:
                return True
        except OSError:
            pass
    root = Path(str(ddsp_root or ""))
    try:
        rel = p.relative_to(root.resolve()) if str(root) else p
        dirs = [x.lower() for x in rel.parts[:-1]]
    except (OSError, ValueError):
        dirs = [x.lower() for x in p.parts[:-1]]
    if any("pretrain" in x for x in dirs):
        return True
    if any(marker in x for x in dirs for marker in BASE_DIR_MARKERS):
        return True
    # model_0.pt 是底模/刚开始训练的种子检查点，不是练好的声音。
    if _ckpt_steps(p.name) <= 0:
        return True
    return False


def _find_ddsp_ckpt(ddsp_root: Path) -> Path:
    """Newest trained RectifiedFlow checkpoint under <repo>/exp (recursive)."""
    if not ddsp_root.is_dir():
        return Path("")
    try:
        candidates = [p for p in ddsp_root.glob("exp/**/model_*.pt")
                      if p.is_file() and not is_base_model(p, ddsp_root)]
    except OSError:
        return Path("")
    if not candidates:
        return Path("")
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _ckpt_steps(name: str) -> int:
    """``model_400.pt`` -> 400 (0 when the name carries no step count)."""
    match = re.search(r"model_(\d+)\.pt$", name)
    return int(match.group(1)) if match else 0


def _ckpt_spks(cfg_path: Path) -> list:
    """Speaker list from the config.yaml next to a checkpoint.

    Handles all three shapes seen in the wild: block sequence
    (``spks:\n- a\n- b``), flow (``spks: [a, b]``) and a single bare name.
    """
    try:
        text = cfg_path.read_text(encoding="utf-8")
    except OSError:
        return []
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        match = re.match(r"spks:\s*(.*)$", line)
        if not match:
            continue
        tail = match.group(1).strip().split(" #")[0].strip()
        if tail.startswith("["):
            return [s.strip().strip("'\"") for s in tail.strip("[]").split(",") if s.strip()]
        if tail:
            return [tail.strip("'\"")]
        out = []
        for nxt in lines[idx + 1:]:
            item = re.match(r"\s*-\s*(.+?)\s*$", nxt)
            if not item:
                break
            out.append(item.group(1).strip("'\""))
        return out
    return []


def list_ddsp_models(ddsp_root: Path, current: Path | None = None,
                     active_runs: set | None = None,
                     base_ckpt: Path | None = None) -> list:
    """Every trained ``exp/**/model_*.pt`` right now, newest first.

    Rescanned on every call, so a model imported while the panel is open
    shows up on the next refresh.  ``main_reflow.py`` reads ``config.yaml``
    from the checkpoint's own folder, so each row reports whether that file
    is there.  Base/seed checkpoints are never listed.
    """
    ddsp_root = Path(ddsp_root or "")
    if not ddsp_root.is_dir():
        return []
    try:
        found = [p for p in ddsp_root.glob("exp/**/model_*.pt")
                 if p.is_file() and not is_base_model(p, ddsp_root, base_ckpt)]
    except OSError:
        return []
    # 同一个实验目录只留最新一个检查点：否则一次训练每 2000 步存一个 ckpt，
    # 面板上就会冒出几十张「还没训完」的重复卡片。
    newest: dict = {}
    for item in found:
        try:
            mt = item.stat().st_mtime
        except OSError:
            continue
        key = str(item.parent)
        if key not in newest or mt > newest[key][0]:
            newest[key] = (mt, item)

    rows = []
    for path in [pair[1] for pair in newest.values()]:
        try:
            st = path.stat()
        except OSError:
            continue
        # _run_<id> 的检查点只在「这次训练还活着」时才算模型；
        # 训练记录已删/已结束的，不在这里显示（文件仍在磁盘上）。
        if active_runs is not None and path.parent.name.startswith("_run_"):
            run_id = path.parent.name[len("_run_"):]
            if run_id not in active_runs:
                continue
        cfg = path.parent / "config.yaml"
        rows.append({
            "path": str(path),
            "exp_dir": path.parent.name,
            "name": path.name,
            "steps": _ckpt_steps(path.name),
            "size_mb": round(st.st_size / (1024 * 1024), 1),
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "has_config": cfg.is_file(),
            "spks": _ckpt_spks(cfg) if cfg.is_file() else [],
            # exp\_run_<run_id> belongs to an unfinished training session and
            # is not a usable model yet; the panel keeps it out of the list.
            "training": path.parent.name.startswith("_run_"),
            "_mtime": st.st_mtime,
        })
    current_text = str(current or "")
    for row in rows:
        row["current"] = bool(current_text) and os.path.normcase(row["path"]) == \
            os.path.normcase(os.path.abspath(current_text))
    rows.sort(key=lambda r: (not r["current"], -r["_mtime"]))
    for row in rows:
        row.pop("_mtime", None)
    return rows


def resolve_paths(data_dir: Path, host_section: dict | None = None) -> dict:
    """Resolve every path from config.json, host config, legacy install, defaults."""
    cfg_path = Path(data_dir) / "config.json"
    cfg: dict = {}
    if cfg_path.is_file():
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg = raw
        except (OSError, ValueError):
            cfg = {}
    host_section = host_section if isinstance(host_section, dict) else {}

    def pick(key: str) -> str:
        return _cfg_value(cfg, key, _cfg_value(host_section, key, ""))

    root_text = pick("compute_root")
    configured = bool(root_text)
    if root_text:
        root = Path(root_text).expanduser()
    else:
        # 不再自动挑“剩余空间最大的盘”：新用户必须先自己选安装位置。
        # 只有已经存在的旧安装（legacy）可以直接复用，因为它不是新下载。
        legacy = legacy_install_root()
        if legacy is not None:
            root = legacy
            configured = True
        else:
            root = Path("")

    envs_dir = Path(pick("envs_dir") or (root / "envs"))
    models_dir = Path(pick("models_dir") or (root / "models"))
    work_dir = Path(pick("work_dir") or (root / "work"))

    # Separation weights: keep an existing dev layout if present.
    sep_dir_text = pick("sep_model_dir")
    if sep_dir_text:
        sep_model_dir = Path(sep_dir_text)
    else:
        legacy_sep = root / "work" / "models"
        sep_model_dir = legacy_sep if (legacy_sep / "mel_band_roformer_karaoke_becruily.ckpt").is_file() \
            else (models_dir / "separator")

    # DDSP-SVC 6.3: the repo lives under the compute root, its venv is envs\ddsp,
    # and the trained RectifiedFlow checkpoint + pretrain weights live inside
    # the repo (paths are resolved relative to the repo dir).
    ddsp_root = Path(pick("ddsp_root") or (root / "ddsp" / "DDSP-SVC"))
    base_ckpt = ddsp_root / "pretrain" / "reflow_base" / "model_0.pt"
    ddsp_ckpt = Path(pick("ddsp_ckpt") or "")
    # 底模不算「当前声音」：配置里即使存的是底模，也回退到第一个真正训练好的模型。
    if not ddsp_ckpt.is_file() or is_base_model(ddsp_ckpt, ddsp_root, base_ckpt):
        ddsp_ckpt = _find_ddsp_ckpt(ddsp_root)

    return {
        "config_file": cfg_path,
        "compute_root": root,
        "configured": configured,
        "envs_dir": envs_dir,
        "models_dir": models_dir,
        "work_dir": work_dir,
        "base_python": Path(pick("base_python")) if pick("base_python") else "",
        "synth_python": Path(pick("synth_python") or (envs_dir / "ddsp" / "Scripts" / "python.exe")),
        "sep_python": Path(pick("sep_python") or (envs_dir / "sep" / "Scripts" / "python.exe")),
        "ddsp_root": ddsp_root,
        "ddsp_ckpt": ddsp_ckpt,
        "ddsp_config": ddsp_root / "configs" / "reflow.yaml",
        # 内置训练底模：DDSP-SVC 6.3 的预训练 RectifiedFlow 权重。
        # 训练一律从这里 warm start，界面上不显示、也不允许当普通声音选。
        "ddsp_pretrain_reflow": base_ckpt,
        # 翻唱训练质量档位底模：低/高由安装清单下载到 pretrain/bases。
        "ddsp_base_low": ddsp_root / "pretrain" / "bases" / "low.pt",
        "ddsp_base_mid": base_ckpt,
        "ddsp_base_high": ddsp_root / "pretrain" / "bases" / "high.pt",
        "ddsp_pretrain_contentvec": ddsp_root / "pretrain" / "contentvec" / "pytorch_model.bin",
        "ddsp_pretrain_rmvpe": ddsp_root / "pretrain" / "rmvpe" / "model.pt",
        "ddsp_pretrain_vocoder": ddsp_root / "pretrain" / "nsf_hifigan",
        "ddsp_vocoder_config": ddsp_root / "pretrain" / "nsf_hifigan" / "config.json",
        "sep_model_dir": sep_model_dir,
        "tier_override": str(pick("tier_override") or ""),
        "precision": str(pick("precision") or "auto"),
        "sep_backend": str(pick("sep_backend") or "bs_roformer"),
        "key_shift": int(pick("key_shift") or 0) if str(pick("key_shift") or "0").lstrip("-").isdigit() else 0,
        "vocal_register_shift": max(-12, min(12, int(pick("vocal_register_shift") or 0)))
        if str(pick("vocal_register_shift") or "0").lstrip("-").isdigit() else 0,
        "infer_step": int(pick("infer_step") or 50) if str(pick("infer_step") or "50").isdigit() else 50,
        "download_source": str(pick("download_source") or "huggingface"),
        "timbre_path": str(pick("timbre_path") or ""),
        "max_parallel": int(pick("max_parallel") or 0) if str(pick("max_parallel") or "").isdigit() else 0,
        "auto_setup": str(pick("auto_setup") or "true").lower() not in {"0", "false", "off", "no"},
        "train_runs_dir": Path(pick("train_runs_dir") or (root / "train_runs")),
        "raw": cfg,
        "host_section": host_section,
    }


def save_config(data_dir: Path, updates: dict) -> dict:
    paths = resolve_paths(data_dir)
    cfg = dict(paths["raw"])
    for key, value in updates.items():
        if value is None:
            continue
        cfg[key] = value
    cfg_path = Path(data_dir) / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cfg_path)
    return cfg


# ---------------------------------------------------------------------------
# Planning: free VRAM first, total VRAM only labels the machine
# ---------------------------------------------------------------------------
def _hardware_tier(vram_gib: float, ram_total_gib: float, has_gpu: bool) -> str:
    if not has_gpu:
        return "low"
    if vram_gib < 6 or (ram_total_gib and ram_total_gib < 16):
        return "low"
    if vram_gib < 12 or (ram_total_gib and ram_total_gib < 32):
        return "mid"
    return "high"


def _precision_for(gpu: dict | None, configured: str) -> str:
    configured = (configured or "auto").lower()
    if configured in {"fp32", "fp16", "bf16"}:
        return configured
    if not gpu:
        return "fp32"
    # DDSP-SVC traces the graph and runs the RectifiedFlow sampler on CUDA;
    # cc<7 cards fall back to fp32.  fp16 is kept as a plan-level label.
    if gpu.get("cc_major", 0) < 7:
        return "fp32"
    return "fp16"


def plan_for(probe: dict | None = None, override: str = "",
             configured_precision: str = "auto", max_parallel: int = 0) -> dict:
    """Build the execution plan for the *current* machine state."""
    probe = probe or probe_hardware(force=True)
    gpu = probe.get("gpu")
    override = (override or "").strip().lower()
    warnings: list[str] = []

    vram_total = float(gpu["total_gib"]) if gpu else 0.0
    vram_free = float(gpu["free_gib"]) if gpu else 0.0
    ram_total = float(probe.get("ram_total_gib") or 0.0)
    ram_free = float(probe.get("ram_available_gib") or 0.0)
    cc_major = int(gpu.get("cc_major") or 0) if gpu else 0

    auto_tier = _hardware_tier(vram_total, ram_total, bool(gpu))
    tier = override if override in TIER_ORDER else auto_tier
    plan = dict(TIER_PLANS[tier])
    plan["stems"] = 2

    # Defaults before clamping.
    precision = _precision_for(gpu, configured_precision)
    device = "cuda:0"
    sep_device = "cuda:0"
    steps = int(plan["synth_n_steps"])
    sep_step = int(plan["sep_step"])
    parallel = int(plan["parallel"])
    max_segment = 30.0

    if not gpu:
        device = sep_device = "cpu"
        precision = "fp32"
        steps = min(steps, 12)
        sep_step = max(sep_step, 8)
        parallel = 1
        max_segment = 15.0
        warnings.append("未检测到 NVIDIA 显卡：分离/变声全部走 CPU（一首歌可能十几分钟）。")
    elif cc_major < 6:
        device = sep_device = "cpu"
        precision = "fp32"
        steps = min(steps, 12)
        sep_step = max(sep_step, 8)
        parallel = 1
        warnings.append(f"显卡计算能力 sm_{gpu.get('compute_cap')} 过旧，当前 CUDA 轮子不支持，自动改走 CPU。")
    else:
        if vram_free < SEP_GPU_MIN_GIB:
            sep_device = "cpu"
            warnings.append(f"空闲显存仅 {vram_free:.1f}GB，分离改走 CPU（需要约 {SEP_GPU_MIN_GIB}GB）。")
        if vram_free < SYNTH_GPU_MIN_GIB:
            if override in TIER_ORDER:
                warnings.append(
                    f"手动档位 {override} 高于当前空闲显存（{vram_free:.1f}GB < "
                    f"{SYNTH_GPU_MIN_GIB}GB），会先尝试 GPU，OOM 时自动降级到 CPU。")
            else:
                device = "cpu"
                precision = "fp32"
                steps = min(steps, 12)
                max_segment = 15.0
                warnings.append(
                    f"空闲显存仅 {vram_free:.1f}GB（< {SYNTH_GPU_MIN_GIB}GB），变声改走 CPU；"
                    "关闭占用显存的程序后下次运行会自动回到 GPU。")
        else:
            if not override:
                # Auto mode follows free VRAM, not the nameplate.
                if vram_free >= 16.0:
                    steps = max(steps, 32)
                    parallel = 2
                elif vram_free >= 12.0:
                    steps = 28
                    parallel = 2
                elif vram_free >= 9.0:
                    steps = 24
                    parallel = 1
                elif vram_free >= 7.0:
                    steps = 20
                    parallel = 1
                else:
                    steps = 16
                    parallel = 1
                max_segment = 30.0 if vram_free >= 10 else (20.0 if vram_free >= 6.5 else 15.0)
                sep_step = 2 if vram_free >= 12 else (4 if vram_free >= 8 else 8)
            else:
                max_segment = 30.0 if vram_free >= 10 else (20.0 if vram_free >= 7 else 15.0)
            # Parallel needs room for one extra render and RAM for its buffers.
            width = max(1, int(vram_free // 6.0))
            if ram_free and ram_free < 12:
                width = 1
            parallel = max(1, min(parallel, width))
            if max_parallel > 0:
                parallel = min(parallel, max_parallel)
            if parallel == 1 and int(TIER_PLANS[tier]["parallel"]) > 1:
                warnings.append(
                    f"空闲显存 {vram_free:.1f}GB 只够单路渲染，并发自动降为 1。")

    if precision == "bf16" and cc_major and cc_major < 8:
        precision = "fp16"
        warnings.append("该显卡不支持 bf16，自动改用 fp16。")
    if device == "cpu" and precision != "fp32":
        precision = "fp32"

    if device == "cpu":
        parallel = 1

    plan.update({
        "tier": tier,
        "label": TIER_PLANS[tier]["label"],
        "override": override if override in TIER_ORDER else "",
        "auto_tier": auto_tier,
        "basis": (f"{probe_brief(probe)}；执行 "
                  f"{'CPU' if device == 'cpu' else 'GPU ' + str(device)} · {precision} · "
                  f"{steps} steps · 并发 {parallel}"),
        "vram_total_gib": round(vram_total, 1),
        "vram_free_gib": round(vram_free, 1),
        "vram_used_gib": round(float(gpu.get("used_gib") or 0.0) if gpu else 0.0, 1),
        "ram_total_gib": round(ram_total, 1),
        "ram_available_gib": round(ram_free, 1),
        "gpu_name": gpu.get("name") if gpu else "",
        "compute_cap": gpu.get("compute_cap") if gpu else "",
        "cpu": probe.get("cpu", ""),
        "synth_precision": precision,
        "synth_n_steps": int(steps),
        "max_segment_s": float(max_segment),
        "parallel": int(parallel),
        "sep_step": int(sep_step),
        "device": device,
        "sep_device": sep_device,
        "sep_workers": 1,
        "probe": probe,
        "warnings": warnings,
        "effective_parallel": int(parallel),
    })
    return plan


def detect_tier(override: str = "") -> dict:
    """Backwards-compatible alias used by the panel and older call sites."""
    return plan_for(probe_hardware(force=True), override=override)


def dynamic_parallel(plan: dict) -> int:
    """Re-check free VRAM/RAM right before a batch and lower concurrency."""
    ceiling = max(1, int(plan.get("parallel", 1)))
    if ceiling <= 1 or plan.get("device") == "cpu":
        return 1
    probe = probe_hardware(force=True)
    gpu = probe.get("gpu")
    if not gpu:
        return 1
    free = float(gpu["free_gib"])
    ram_free = float(probe.get("ram_available_gib") or 0.0)
    if free < SYNTH_GPU_MIN_GIB + 1.0:
        return 1
    if ram_free and ram_free < 12:
        return 1
    return max(1, min(ceiling, int(free // 6.0)))


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------
def _clean_env(extra: dict | None = None) -> dict:
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(key, None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    env.setdefault("PYTHONUNBUFFERED", "1")
    if extra:
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[str(key)] = str(value)
    return env


def run_worker(python: Path, script: Path, job: dict, *, cwd: Path,
               timeout: float = 3600.0, log_path: Path | None = None,
               env_extra: dict | None = None) -> dict:
    """Run one worker script and return its JSON result.

    The worker contract: ``python -I script <job.json> <result.json>``; the
    script must write the result file even on failure. A non-zero exit code or
    a missing result file is reported as an explicit failure, never as success.
    """
    python = Path(python)
    # ``script`` may carry extra leading flags (``[path, "--out", dir]``);
    # the contract is still: <python> -I <script + flags> <job> <result>.
    if isinstance(script, (list, tuple)):
        script_args = [str(part) for part in script]
    else:
        script_args = [str(script)]
    script = Path(script_args[0])
    if not python.is_file():
        raise RuntimeError(f"计算环境 Python 不存在：{python}")
    if not Path(script).is_file():
        raise RuntimeError(f"worker 脚本不存在：{script}")
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    stamp = f"{int(time.time() * 1000)}"
    job_path = cwd / f".job_{script.stem}_{stamp}.json"
    result_path = cwd / f".result_{script.stem}_{stamp}.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    cmd = [str(python), "-I", "-X", "utf8", *script_args,
           str(job_path), str(result_path)]

    log_file = None
    try:
        if log_path is not None:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "a", encoding="utf-8", errors="backslashreplace")
            log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {script.name} =====\n")
            log_file.flush()
        proc = subprocess.run(
            cmd, cwd=str(cwd), timeout=timeout,
            stdout=(log_file or subprocess.PIPE), stderr=subprocess.STDOUT,
            env=_clean_env(env_extra),
            # 离线渲染不需要抢桌面：降一档 CPU/IO 优先级，唱歌时系统仍能正常响应。
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                           | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)),
        )
        tail = ""
        if log_file is None:
            tail = (proc.stdout or b"").decode("utf-8", "replace")[-2000:]
        if not result_path.is_file():
            raise RuntimeError(
                f"worker {script.name} 未产出结果文件（退出码 {proc.returncode}）"
                + (f"；输出尾部：{tail}" if tail else "")
            )
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"worker {script.name} 返回了非法结果")
        if proc.returncode != 0 and payload.get("ok"):
            raise RuntimeError(f"worker {script.name} 退出码 {proc.returncode} 却报告成功")
        if (not payload.get("ok")) and payload.get("trace") and log_file is not None:
            try:
                log_file.write(f"[{script.name} trace]\n{str(payload.get('trace')).rstrip()}\n")
                log_file.flush()
            except Exception:
                pass
        if proc.returncode != 0:
            # The worker wrote a result saying ok is false: that is a *failed
            # measurement*, not a rendered take.  Returning it as a success
            # would hide worker crashes, so keep it as a failure with the
            # worker's own error text surfaced to the caller.
            payload = dict(payload)
            payload["worker_exit"] = proc.returncode
            if "error" not in payload:
                payload["error"] = f"worker {script.name} 退出码 {proc.returncode}"
        return payload
    finally:
        if log_file is not None:
            log_file.close()
        for p in (job_path, result_path):
            try:
                p.unlink()
            except OSError:
                pass


def is_quant_failure(payload: dict | None) -> bool:
    """量化本身跑不起来（不是显存不够）：换下一种策略而不是直接失败。

    老卡（sm_75）上 torchao 的 int8 内核不一定有；某些 torch 版本还会
    报 NotImplementedError。这种情况继续走 layer/cpu 更稳妥。
    """
    if not isinstance(payload, dict):
        return False
    text = " ".join(str(payload.get(k) or "") for k in ("error", "trace", "hint")).lower()
    return any(token in text for token in (
        "_weight_int8pack_mm", "int8pack", "torchao", "quantize_",
        "quantization", "quantized", "not implemented for",
    ))


def oom_advice(plan: dict | None = None) -> dict:
    plan = plan or {}
    free = plan.get("vram_free_gib", "?")
    total = plan.get("vram_total_gib", "?")
    used = plan.get("vram_used_gib", "?")
    return {
        "reason": "显存不足",
        "message": (
            f"显存不足：当前空闲 {free}GB / 总 {total}GB（其他程序约占用 {used}GB）。"
            "已自动尝试降低并发/步长与切到 CPU。"
        ),
        "actions": [
            "关闭占用显存的程序（浏览器硬件加速、游戏、其他 AI 工具）后重试",
            "把档位手动设为 low（面板「运行参数」）",
            "继续失败可改用 CPU 路线：设置 tier=low 并关闭 GPU 占用",
        ],
    }


# ---------------------------------------------------------------------------
# Environment introspection (fast: reads dist-info, avoids importing torch)
# ---------------------------------------------------------------------------
def _site_packages(python: Path) -> Path:
    return Path(python).parent.parent / "Lib" / "site-packages"


def dist_version(python: Path, prefix: str) -> str:
    site = _site_packages(python)
    try:
        for entry in site.glob(f"{prefix}-*.dist-info"):
            name = entry.name[len(prefix) + 1:]
            return name[:-len(".dist-info")] if name.endswith(".dist-info") else name
    except OSError:
        pass
    return ""


def has_cuda_build(python: Path) -> bool:
    version = dist_version(python, "torch")
    if "+cu" in version:
        return True
    site = _site_packages(python)
    try:
        return any(site.glob("nvidia/cuda_runtime/bin/cudart64_*.dll")) or \
            any((site / "torch" / "lib").glob("cudart64_*.dll"))
    except OSError:
        return False


def python_version(python: Path) -> str:
    if not Path(python).is_file():
        return "missing"
    try:
        out = subprocess.run([str(python), "-c", "import sys;print(sys.version.split()[0])"],
                             capture_output=True, text=True, timeout=20)
        return (out.stdout or "").strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "missing"


def module_available(python: Path, module: str) -> bool:
    try:
        out = subprocess.run([str(python), "-c", f"import {module};print('ok')"],
                             capture_output=True, text=True, timeout=40)
        return "ok" in (out.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


_FFMPEG_HINTS = (
    r"%LOCALAPPDATA%\Microsoft\WinGet\Linksfmpeg.exe",
    r"%LOCALAPPDATA%\Microsoft\WinGet\Linksfmpeg.EXE",
    r"%ProgramData%\chocolateyinfmpeg.exe",
    r"%ProgramFiles%fmpeginfmpeg.exe",
    r"%ProgramFiles(x86)%fmpeginfmpeg.exe",
    r"C:fmpeginfmpeg.exe",
)


def _find_ffmpeg() -> str:
    """先看 PATH，再看 winget / chocolatey / 常见安装目录。

    宿主的插件进程 PATH 往往不含 WinGet Links，只靠 shutil.which 会误报「缺 ffmpeg」。
    """
    exe = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if exe:
        return exe
    for raw in _FFMPEG_HINTS:
        candidate = Path(os.path.expandvars(raw))
        if candidate.is_file():
            return str(candidate)
    return ""


def ffmpeg_status(configured: str = "") -> dict:
    exe = str(configured or "").strip()
    if not exe or not Path(exe).is_file():
        exe = _find_ffmpeg()
    if not exe:
        return {"available": False, "ok": False, "exists": False, "path": "", "version": ""}
    text = _run_quiet([exe, "-version"], timeout=6.0)
    first = text.splitlines()[0] if text else ""
    match = re.search(r"ffmpeg version (\S+)", first)
    # ok/exists 是给面板用的同义字段：面板早期按这两个名字判断，缺了就会误显示「缺东西」
    return {"available": True, "ok": True, "exists": True, "path": exe,
            "version": match.group(1) if match else "unknown"}


def env_status(paths: dict) -> dict:
    """Report what exists, so the panel can tell the user exactly what is missing."""
    def file_state(p: Path) -> dict:
        return {"path": str(p), "exists": p.is_file()}

    synth_python = Path(paths["synth_python"])
    sep_python = Path(paths["sep_python"])
    ddsp_root = Path(paths["ddsp_root"])
    ddsp_ckpt = Path(paths.get("ddsp_ckpt") or "")
    sep_dir = Path(paths.get("sep_model_dir") or "")
    vocoder_dir = Path(paths["ddsp_pretrain_vocoder"])
    torch_version = dist_version(synth_python, "torch") if synth_python.is_file() else ""
    entry = ddsp_root / "main_reflow.py"
    return {
        "ddsp_env": {"python": str(synth_python), "exists": synth_python.is_file(),
                     "torch": torch_version, "cuda_build": has_cuda_build(synth_python)
                     if synth_python.is_file() else False},
        "sep_env": {"python": str(sep_python), "exists": sep_python.is_file(),
                    "audio_separator": dist_version(sep_python, "audio_separator")
                    if sep_python.is_file() else ""},
        "ddsp_repo": {
            "path": str(ddsp_root),
            "exists": ddsp_root.is_dir(),
            "entry": entry.is_file(),
        },
        "ddsp_ckpt": {
            "path": str(ddsp_ckpt),
            "exists": ddsp_ckpt.is_file(),
        },
        "pretrain": {
            "reflow_base": file_state(Path(paths["ddsp_pretrain_reflow"])),
            "contentvec": file_state(Path(paths["ddsp_pretrain_contentvec"])),
            "rmvpe": file_state(Path(paths["ddsp_pretrain_rmvpe"])),
            # DDSP-SVC reads the vocoder files straight from pretrain/nsf_hifigan.
            "vocoder": file_state(vocoder_dir / "model"),
            "vocoder_config": file_state(vocoder_dir / "config.json"),
        },
        "sep_model": {
            "dir": str(sep_dir),
            "ckpt": (sep_dir / "mel_band_roformer_karaoke_becruily.ckpt").is_file(),
            "config": (sep_dir / "config_mel_band_roformer_karaoke_becruily.yaml").is_file(),
        },
        "ffmpeg": ffmpeg_status(str((paths.get("raw") or {}).get("ffmpeg_path") or "")),
        "base_python": str(paths.get("base_python") or ""),
    }


def _newest(paths) -> Path | None:
    items = [p for p in paths if p.exists()]
    if not items:
        return None
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return items[0]


# ---------------------------------------------------------------------------
# Training sessions: <root>/train_runs/<run_id>/ driven by a long-running
# supervisor (train_session_worker.py).  This module only knows the on-disk
# contract from the plan (section 2): state.json is written by the supervisor,
# control.json by the plugin, both atomically as tmp + os.replace.
# ---------------------------------------------------------------------------
TRAIN_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}$")
TRAIN_PHASES = ("preparing", "preprocessing", "training", "previewing",
                "awaiting_choice", "stopping", "finished", "failed")
TRAIN_ENDED = ("finished", "failed")
TRAIN_ACTIONS = ("continue", "stop")


def valid_run_id(run_id: str) -> bool:
    return bool(TRAIN_RUN_ID_RE.match(str(run_id or "").strip()))


def new_run_id(taken=()) -> str:
    """``YYYYMMDD_HHMMSS``, bumped by a second while a folder already uses it."""
    base = int(time.time())
    for bump in range(120):
        candidate = time.strftime("%Y%m%d_%H%M%S", time.localtime(base + bump))
        if candidate not in taken:
            return candidate
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(base + 121))


def _atomic_write(path: Path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_training_state(run_dir: Path) -> dict | None:
    try:
        data = json.loads((Path(run_dir) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_training_control(run_dir: Path, action: str, steps: int = 0) -> dict:
    """Append one command to control.json; ``seq`` only ever increases.

    The file can be rewritten by a supervisor finishing an earlier command, so
    never reuse its ``seq``; track the highest one this process has issued.
    """
    path = Path(run_dir) / "control.json"
    previous = 0
    try:
        previous = int(json.loads(path.read_text(encoding="utf-8")).get("seq") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        previous = 0
    issued = getattr(write_training_control, "_last", {})
    seq = max(previous, int(issued.get(str(path), 0))) + 1
    issued[str(path)] = seq
    write_training_control._last = issued
    payload = {"action": action, "seq": seq, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if int(steps or 0) > 0:
        payload["steps"] = int(steps)
    _atomic_write(path, payload)
    return payload


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) raises WinError 87 on Windows, so ask the API.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return bool(ok) and exit_code.value == 259  # STILL_ACTIVE
        except Exception:  # noqa: BLE001 - unknown process = assume gone
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def safe_stop_pid(pid, log=print) -> bool:
    """Terminate a supervisor this plugin launched (never an unknown process).

    The pid always comes from a run's own state.json, i.e. from a process this
    plugin started with ``train_session_worker.py``; terminating it only ends
    that run's supervisor (its training child is killed through /T).
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not pid_alive(pid):
        return False
    try:
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log(f"taskkill {pid} -> {proc.returncode}")
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        log(f"taskkill {pid} failed: {exc}")
        return False


def scan_training_stale(runs_root: Path) -> list:
    """Runs that need their supervisor back after a plugin restart.

    A run whose phase is not finished/failed but whose pid is gone (plugin or
    host restarted, machine slept, worker crashed) is listed here; the caller
    re-launches the supervisor, which resumes from state.json.
    """
    runs_root = Path(runs_root)
    if not runs_root.is_dir():
        return []
    stale = []
    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir() or not valid_run_id(run_dir.name):
            continue
        state = read_training_state(run_dir)
        if not state:
            continue
        phase = str(state.get("phase") or "")
        if phase in TRAIN_ENDED:
            continue
        if pid_alive(state.get("pid")):
            continue
        stale.append({"run_id": run_dir.name, "phase": phase, "pid": state.get("pid")})
    return stale


_PROC_TAP_NAME = "proc-tap-1.1.1-4a0fbb7d05"


def _plugin_asset_addon():
    """proc-tap addon shipped inside the plugin package (no download needed)."""
    root = Path(__file__).resolve().parent
    for base in (root / "vendor", root / "assets"):
        candidate = base / _PROC_TAP_NAME
        if (candidate / "proctap").is_dir():
            return candidate
    return None


def _plugin_asset_smtc_vendor():
    """winrt (+typing_extensions) shipped for reading Windows media sessions."""
    root = Path(__file__).resolve().parent
    for base in (root / "vendor", root / "assets"):
        candidate = base / "smtc_vendor"
        if (candidate / "winrt").is_dir():
            return candidate
    return None


def _venv_site_packages(python) -> Path | None:
    python = Path(python)
    root = python.parent.parent
    candidates = [root / "Lib" / "site-packages", *sorted(root.glob("lib/python*/site-packages"))]
    for candidate in candidates:
        if (candidate / "numpy").is_dir():
            return candidate
    return None


def _capture_own_paths(data_dir: Path, synth_python: str = "") -> dict:
    """Best runtime: the plugin's own synth venv + shipped proc-tap addon."""
    data = Path(data_dir).resolve()
    own = data / "capture"
    addon = _newest(p for p in (own / "app_capture").glob("proc-tap-*"))         if (own / "app_capture").is_dir() else None
    if addon is None:
        addon = _plugin_asset_addon()
    if synth_python:
        python = Path(synth_python)
        vendor = _venv_site_packages(python)
        if python.is_file() and vendor is not None and addon is not None:
            return {"root": own, "python": python, "vendor": vendor, "addon": addon}
    # legacy fully-copied runtime (kept for older installs)
    python = _newest((own / "managed-python").glob("*/python.exe"))         if (own / "managed-python").is_dir() else None
    vendor = own / "vendor"
    return {"root": own, "python": python,
            "vendor": vendor if vendor.is_dir() else None, "addon": addon}


def _capture_perception_paths(data_dir: Path) -> dict:
    env_dir = Path(data_dir).resolve().parent.parent / "music_perception" / "data" / "environment"
    python = _newest((env_dir / "managed-python").glob("*/python.exe"))         if (env_dir / "managed-python").is_dir() else None
    vendor = _newest(p for p in env_dir.glob(".audio-runtime-*") if (p / "numpy").is_dir())         if env_dir.is_dir() else None
    addon = _newest(p for p in (env_dir / "app_capture").glob("proc-tap-*"))         if (env_dir / "app_capture").is_dir() else None
    return {"root": env_dir, "python": python, "vendor": vendor, "addon": addon}


def _proctap_native_ok(base) -> bool:
    """proc-tap 的 _native 扩展在不在：目录在但缺原生扩展，import 一样会炸。"""
    if not base:
        return False
    pkg = Path(base) / "proctap"
    if not pkg.is_dir():
        return False
    return bool(list(pkg.glob("_native*.pyd")) or list(pkg.glob("_native*.so")))


def _capture_paths_ok(paths: dict) -> bool:
    """目录存在 != 能 import：proc-tap 的 _native 是硬依赖，必须一并检查。"""
    python, vendor, addon = paths.get("python"), paths.get("vendor"), paths.get("addon")
    if not (python and Path(python).is_file()
            and vendor and (Path(vendor) / "numpy").is_dir()):
        return False
    # 市场禁止插件包携带二进制文件，proc-tap 改由安装流程 pip 装进合成环境；
    # 这时它在 venv 的 site-packages 里，没有单独的 app_capture 目录。
    return _proctap_native_ok(addon) or _proctap_native_ok(vendor)


def locate_app_capture_runtime(data_dir: Path, synth_python: str = "") -> dict:
    """Find a runtime able to capture ONE application's WASAPI channel.

    Order: the plugin's own synth venv + shipped proc-tap addon, then a legacy
    copied runtime, then 为她所听's environment (read-only). No download.
    """
    if sys.platform != "win32":
        return {"ok": False, "error": "逐应用声道隔离只在 Windows 上可用"}
    own = _capture_own_paths(data_dir, synth_python)
    if _capture_paths_ok(own):
        used_synth = bool(synth_python) and Path(synth_python).resolve() == Path(own["python"]).resolve()
        return {"ok": True,
                "source": "self_synth" if used_synth else "self",
                "python": str(own["python"]), "vendor": str(own["vendor"]),
                "addon": str(own["addon"] or own["vendor"]), "root": str(own["root"])}
    mp = _capture_perception_paths(data_dir)
    if _capture_paths_ok(mp):
        return {"ok": True, "source": "perception", "python": str(mp["python"]),
                "vendor": str(mp["vendor"]), "addon": str(mp["addon"]),
                "root": str(mp["root"])}
    return {"ok": False,
            "error": "逐应用声道采集组件缺失（合成环境 numpy 或 proc-tap）",
            "hint": "先完成一次合成环境安装；若 proc-tap 目录在但缺 _native*.pyd，说明原生扩展未入库，请在合成环境里执行 pip install proc-tap==1.1.1 后重试。"}


def _link_or_copy_tree(src: Path, dst: Path) -> int:
    """Mirror a directory tree, hard-linking files when the volume allows.

    Returns the number of files created. Existing destinations are replaced so a
    partial copy from an interrupted install can be healed on the next run.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.is_dir():
        raise FileNotFoundError(str(src))
    count = 0
    for item in src.rglob("*"):
        target = dst / item.relative_to(src)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        try:
            os.link(item, target)
        except OSError:
            shutil.copy2(item, target)
        count += 1
    return count


def copy_app_capture_runtime(data_dir: Path) -> dict:
    """Make the capture addon available under our data dir (or use the shipped one)."""
    data = Path(data_dir).resolve()
    own = data / "capture"
    (own / "app_capture").mkdir(parents=True, exist_ok=True)
    asset = _plugin_asset_addon()
    files = 0
    if asset is not None:
        dst = own / "app_capture" / asset.name
        if not dst.is_dir():
            files += _link_or_copy_tree(asset, dst)
        vendor_asset = _plugin_asset_smtc_vendor()
        if vendor_asset is not None:
            smtc = own / "smtc_vendor"
            if not smtc.is_dir():
                files += _link_or_copy_tree(vendor_asset, smtc)
        return {"ok": True, "files": files, "path": str(own), "source": "builtin",
                "note": "采集组件已随插件内置，不需要额外安装或下载。"}
    mp = _capture_perception_paths(data_dir)
    if not _capture_paths_ok(mp):
        return {"ok": False,
                "error": "采集组件缺失，且本机没有可复制的为她所听运行环境",
                "hint": "需要重新安装本插件以恢复内置的 proc-tap 组件。"}
    files += _link_or_copy_tree(Path(mp["addon"]), own / "app_capture" / Path(mp["addon"]).name)
    return {"ok": True, "files": files, "path": str(own), "source": "perception",
            "note": "已从为她所听复制采集组件。"}


def locate_smtc_runtime(data_dir: Path, synth_python: str = "") -> dict:
    """Find a runtime that can read Windows SMTC metadata.

    Prefer the synth venv plus the shipped winrt vendor, then a legacy copy,
    then 为她所听's environment. Never imports another plugin's package.
    """
    data_dir = Path(data_dir).resolve()
    script = Path(__file__).resolve().parent / "workers" / "media_session_worker.py"
    if sys.platform != "win32":
        return {"ok": False, "error": "系统媒体信息（SMTC）只在 Windows 上可用"}
    if not script.is_file():
        return {"ok": False, "error": "media_session_worker.py 缺失"}

    vendors = []
    own = data_dir / "capture"
    if (own / "smtc_vendor" / "winrt").is_dir():
        vendors.append(own / "smtc_vendor")
    asset = _plugin_asset_smtc_vendor()
    if asset is not None:
        vendors.append(asset)
    legacy = own / "vendor"
    if (legacy / "winrt").is_dir():
        vendors.append(legacy)
    if synth_python and Path(synth_python).is_file():
        for vendor in vendors:
            return {"ok": True, "python": str(synth_python), "vendor": str(vendor),
                    "worker_script": str(script), "source": "self"}
        # 市场禁止插件包带 .pyd，winrt 改由安装流程 pip 装进合成环境。
        venv = _venv_site_packages(Path(synth_python))
        if venv and (venv / "winrt" / "windows" / "media" / "control").is_dir():
            return {"ok": True, "python": str(synth_python), "vendor": str(venv),
                    "worker_script": str(script), "source": "self_synth_pip"}

    for source, paths in (("self", _capture_own_paths(data_dir)),
                          ("perception", _capture_perception_paths(data_dir))):
        vendor = paths.get("vendor")
        python = paths.get("python")
        if python and Path(python).is_file() and vendor and (Path(vendor) / "winrt").is_dir():
            return {"ok": True, "python": str(python), "vendor": str(vendor),
                    "worker_script": str(script), "source": source}
    return {"ok": False,
            "error": "系统媒体信息组件缺失：winrt 依赖",
            "hint": "本插件已内置 winrt；重装插件可恢复。"}


def _training_progress(state: dict) -> dict:
    """Turn live ``step`` into a monotone panel bar (never goes backwards)."""
    phase = str(state.get("phase") or "")
    step = int(state.get("step") or 0)
    live = state.get("live") if isinstance(state.get("live"), dict) else {}
    try:
        live_step = max(0, int(live.get("step") or 0))
    except (TypeError, ValueError):
        live_step = 0
    base = int(live.get("base_step") or 0)
    # A stale ``live`` from an earlier epoch must not exceed the reported step.
    # live 可能属于上一段（重启后尚未刷新）：只认不小于本段起点的 live
    current = max(step, live_step) if live_step and live_step >= base else step
    # batch/s 只取最新一行：`batch/s: 0.05` 是含首步预热的均值，会把剩余时间放大约百倍
    try:
        rate = float(live.get("batch_per_s") or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    try:
        total_bps = float((state.get("params") or {}).get("total_batches_per_step") or 0.0)
    except (TypeError, ValueError):
        total_bps = 0.0
    milestones = [int(m) for m in (state.get("milestones") or []) if isinstance(m, (int, float))]
    nxt = state.get("next_milestone") if phase == "training" else None
    try:
        nxt = int(nxt)
    except (TypeError, ValueError):
        nxt = next((m for m in milestones if m > current), None)
    done = max([m for m in milestones if m <= current], default=0)
    stage = {"preparing": "正在准备训练环境与参数",
             "preprocessing": "正在切分与预处理素材",
             "stopping": "正在停止并保存当前结果",
             "finished": "训练已结束",
             "failed": "训练失败"}.get(phase, "")
    pct = None
    eta = None
    if nxt and nxt > done:
        pct = max(0, min(100, int(round((current - done) * 100.0 / (nxt - done)))))
        # 剩余时间 = 到下一里程碑还差的步数 × 每步 batch 数 ÷ batch/s
        if rate > 0 and total_bps > 0:
            remain = max(0.0, (nxt - current) * total_bps - max(0.0, float(live.get("batch") or 0.0)))
            eta = remain / rate
    elif nxt and current >= nxt:
        pct = 100  # 已越过里程碑，正在保存/验证
    elif phase == "finished":
        pct = 100
    return {"step": current, "target": nxt, "pct": pct,
            "eta_s": None if eta is None else round(eta, 1),
            "rate_bps": rate or None, "total_bps": total_bps or None,
            "batch_per_s": live.get("batch_per_s"), "stage_text": stage,
            "updated_at": live.get("updated_at") or ""}


def describe_training_run(run_id: str, run_dir: Path) -> dict:
    """Compact, panel-shaped view of one run (state.json + liveness)."""
    run_dir = Path(run_dir)
    state = read_training_state(run_dir) or {}
    phase = str(state.get("phase") or "unknown")
    previews = state.get("previews") if isinstance(state.get("previews"), list) else []
    live = state.get("live") if isinstance(state.get("live"), dict) else {}
    return {
        "auto": bool(state.get("auto")),
        "best_step": state.get("best_step"),
        "best_val_loss": state.get("best_val_loss"),
        "live": live,
        "progress": _training_progress({**state, "live": live}),
        "run_id": run_id,
        "dir": str(run_dir),
        "name": state.get("name") or run_id,
        "phase": phase,
        "alive": pid_alive(state.get("pid")),
        "step": state.get("step"),
        "next_milestone": state.get("next_milestone"),
        "milestones": state.get("milestones") or [],
        "params": state.get("params") or {},
        "data": state.get("data") or {},
        "previews": previews,
        "advice": state.get("advice") or {},
        "speed": state.get("speed") or {},
        "last_seq": state.get("last_seq"),
        "error": state.get("error") or "",
        "updated_at": state.get("updated_at") or "",
    }


def find_training_ckpt(run_dir: Path, step: int) -> Path | None:
    """The milestone checkpoint a run produced for ``step``.

    The expdir follows the plan's contract (``<repo>/exp/_run_<run_id>/
    model_<step>.pt``; the session worker records it in state.json as
    ``expdir``), so it is checked first.  Other milestones may have been
    moved to the trash folder by an earlier "keep", so those locations are
    searched last.
    """
    run_dir = Path(run_dir)
    state = read_training_state(run_dir) or {}
    step = int(step)
    for item in state.get("previews") or []:
        try:
            if int(item.get("step") or -1) == step and item.get("ckpt"):
                candidate = Path(str(item["ckpt"]))
                if candidate.is_file():
                    return candidate
        except (TypeError, ValueError):
            continue
    expdir = Path(str(state.get("expdir") or (run_dir / "expdir.txt")))
    if expdir.is_file():
        try:
            expdir = Path(expdir.read_text(encoding="utf-8").strip())
        except OSError:
            expdir = Path("")
    if expdir.name:
        candidate = expdir / f"model_{step}.pt"
        if candidate.is_file():
            return candidate
    for candidate in run_dir.glob(f"**/model_{step}.pt"):
        if candidate.is_file():
            return candidate
    root = Path(state.get("repo") or "") if state.get("repo") else None
    if root is not None and root.is_dir():
        candidate = root / "exp" / f"_run_{run_dir.name}" / f"model_{step}.pt"
        if candidate.is_file():
            return candidate
    return None


def wav_stats(path: Path) -> dict:
    """duration / peak / rms of a WAV, read without extra dependencies."""
    import wave

    path = Path(path)
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate() or 1
        width = handle.getsampwidth()
        data = handle.readframes(frames)
    peak = 0.0
    rms = 0.0
    if width == 2 and data:
        import array
        if sys.byteorder == "big":
            samples = array.array("h")
            samples.frombytes(data)
            samples.byteswap()
        else:
            samples = array.array("h")
            samples.frombytes(data)
        if samples:
            peak = max(abs(min(samples)), abs(max(samples))) / 32768.0
            rms = (sum(float(s) * s for s in samples) / len(samples)) ** 0.5 / 32768.0
    elif width == 4 and data:
        import array
        samples = array.array("i")
        samples.frombytes(data)
        if sys.byteorder == "big":
            samples.byteswap()
        if samples:
            peak = max(abs(min(samples)), abs(max(samples))) / 2147483648.0
            rms = (sum(float(s) * s for s in samples) / len(samples)) ** 0.5 / 2147483648.0
    return {"sr": rate, "seconds": round(frames / float(rate), 3),
            "peak": round(peak, 4), "rms": round(rms, 4), "bytes": path.stat().st_size}


if __name__ == "__main__":  # tiny CLI for manual diagnostics
    data = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"
    paths = resolve_paths(data)
    probe = probe_hardware(force=True)
    print(json.dumps({
        "probe": probe,
        "plan": plan_for(probe, paths.get("tier_override", ""),
                         paths.get("precision", "auto"), paths.get("max_parallel", 0)),
        "env": env_status(paths),
        "paths": {k: str(v) for k, v in paths.items() if isinstance(v, (str, Path))},
    }, ensure_ascii=False, indent=2))
