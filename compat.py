"""第五十五轮：全插件部署清单 + 下载测速 + 硬件适配（向下兼容）。

三件事，全部只读、不安装任何东西：
1. ``detect_system()``  —— 不只看 NVIDIA：用 WMI 列出所有显卡（NVIDIA / AMD / Intel / 其他），
   CPU 厂商、核数、指令集（AVX2 / AVX-512 / AMX），内存。
2. ``adapt_plan()``     —— 按检测结果给出每个功能的执行后端和质量档位：
   配置好就全开，差一点就降步数/分辨率，不降到听感明显变差。
3. ``deploy_manifest()`` + ``speed_test()`` —— 插件所有要部署/下载的东西（翻唱、原创、歌词识别、
   舞台字体），每一项的来源、大小、是否就绪；对每个下载源做真实测速（下 1~4MB），给出能不能通、多快。

关于 AMD / Intel 显卡：本插件的模型（DDSP-SVC、Mel-Band RoFormer、ACE-Step、Whisper）都是 PyTorch，
CUDA 只有 NVIDIA 有。Windows 上 AMD/Intel 没有可直接替换的 CUDA，因此这些机器按「CPU 加速」路线跑；
舞台（浏览器 WebGL/Canvas）三家显卡都能用。这里如实标注，不假装支持。
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import runtime

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# 1) 系统检测
# ---------------------------------------------------------------------------
def _ps_json(script: str, timeout: float = 12.0):
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              script + " | ConvertTo-Json -Compress"],
                             capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        raw = (out.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else [data]
    except Exception:  # noqa: BLE001 - 探测失败不抛
        return []


def _vendor_of(name: str) -> str:
    n = (name or "").lower()
    if "nvidia" in n or "geforce" in n or "quadro" in n or "rtx" in n:
        return "nvidia"
    if "amd" in n or "radeon" in n or "ati " in n:
        return "amd"
    if "intel" in n or "arc" in n or "iris" in n or "uhd" in n:
        return "intel"
    return "other"


_VIRTUAL = re.compile(r"virtual|todesk|parsec|remote|basic display|meta|sunlogin|oray|idd|mirage", re.I)


def _gpus() -> list[dict]:
    rows = _ps_json("Get-CimInstance Win32_VideoController | "
                    "Select-Object Name,AdapterRAM,DriverVersion,PNPDeviceID")
    out = []
    for r in rows:
        name = str(r.get("Name") or "").strip()
        if not name or _VIRTUAL.search(name):
            continue
        out.append({"name": name, "vendor": _vendor_of(name),
                    "driver": str(r.get("DriverVersion") or ""),
                    # WMI 的 AdapterRAM 是 32 位，>4GB 的卡会被截断，只作参考
                    "wmi_ram_gib": round(float(r.get("AdapterRAM") or 0) / 2**30, 1)})
    return out


def _cpu() -> dict:
    rows = _ps_json("Get-CimInstance Win32_Processor | "
                    "Select-Object Name,Manufacturer,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed")
    r = rows[0] if rows else {}
    name = str(r.get("Name") or platform.processor() or "").strip()
    manu = str(r.get("Manufacturer") or "").lower()
    vendor = "amd" if ("amd" in manu or "amd" in name.lower()) else (
        "intel" if ("intel" in manu or "intel" in name.lower()) else "other")
    logical = int(r.get("NumberOfLogicalProcessors") or os.cpu_count() or 1)
    cores = int(r.get("NumberOfCores") or max(1, logical // 2))
    return {"name": name, "vendor": vendor, "cores": cores, "threads": logical,
            "mhz": int(r.get("MaxClockSpeed") or 0), "isa": _isa()}


def _isa() -> dict:
    """CPU 指令集：Windows 用 IsProcessorFeaturePresent（不依赖第三方包）。"""
    feats = {"sse4_2": 38, "avx": 39, "avx2": 40, "avx512": 41}
    out = {}
    try:
        import ctypes
        f = ctypes.windll.kernel32.IsProcessorFeaturePresent
        for k, v in feats.items():
            out[k] = bool(f(v))
    except Exception:  # noqa: BLE001
        pass
    return out


def detect_system(force: bool = False) -> dict:
    probe = runtime.probe_hardware(force=force)
    gpus = _gpus()
    nv = probe.get("gpu")  # nvidia-smi 读到的才是真显存
    for g in gpus:
        if g["vendor"] == "nvidia" and nv:
            g.update({"vram_gib": nv.get("total_gib"), "free_gib": nv.get("free_gib"),
                      "compute_cap": nv.get("compute_cap"), "cuda_driver": nv.get("driver")})
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "cpu": _cpu(), "gpus": gpus, "nvidia": nv,
            "ram_total_gib": probe.get("ram_total_gib"), "ram_free_gib": probe.get("ram_available_gib"),
            "drives": probe.get("drives")}


# ---------------------------------------------------------------------------
# 2) 适配方案
# ---------------------------------------------------------------------------
def _cpu_threads_for_infer(cpu: dict) -> int:
    """推理线程：用物理核数（超线程对 MKL/oneDNN 帮助小，还会和桌面抢）；留 1~2 核给系统和舞台。"""
    cores = int(cpu.get("cores") or 1)
    if cores <= 2:
        return max(1, cores)
    return max(2, min(cores - 1 if cores <= 8 else cores - 2, 32))


def adapt_plan(sysinfo: dict | None = None) -> dict:
    s = sysinfo or detect_system()
    cpu, nv, ram = s["cpu"], s.get("nvidia"), float(s.get("ram_total_gib") or 0)
    vendors = sorted({g["vendor"] for g in s.get("gpus") or []})
    vram = float(nv.get("total_gib") or 0) if nv else 0.0
    cc = int(nv.get("cc_major") or 0) if nv else 0
    cuda_ok = bool(nv) and cc >= 6
    threads = _cpu_threads_for_infer(cpu)
    isa = cpu.get("isa") or {}
    notes: list[str] = []

    # 后端：只有 NVIDIA（sm≥6.0）走 CUDA；其余全部 CPU 加速
    if cuda_ok:
        backend = "cuda"
        tier = "high" if vram >= 12 and ram >= 32 else ("mid" if vram >= 8 else "low")
    else:
        backend = "cpu"
        tier = "cpu_fast" if (cpu["cores"] >= 8 and ram >= 16) else "cpu_min"
        if "amd" in vendors or "intel" in vendors:
            notes.append("检测到 AMD/Intel 显卡：本插件的 AI 模型只提供 CUDA（NVIDIA）加速，"
                         "这类显卡上模型改用 CPU 加速运行；唱歌舞台照常用显卡画。")
        elif nv and cc < 6:
            notes.append(f"NVIDIA 显卡算力 sm_{nv.get('compute_cap')} 过旧，当前 CUDA 轮子不支持，改走 CPU 加速。")
        else:
            notes.append("没有可用的独立显卡：模型全部走 CPU 加速。")
    if not isa.get("avx2"):
        notes.append("CPU 不支持 AVX2：PyTorch 仍能跑，但会明显变慢（约 2~4 倍）。")

    cpu_env = {"OMP_NUM_THREADS": threads, "MKL_NUM_THREADS": threads,
               "TORCH_NUM_THREADS": threads, "KMP_BLOCKTIME": 1}
    if cpu["vendor"] == "amd":
        # MKL 在 AMD 上默认不走最快的 AVX2 内核；oneDNN（PyTorch 卷积主路径）不受影响
        notes.append("AMD CPU：已按物理核设置线程数；PyTorch 卷积走 oneDNN，对 AMD 同样有效。")

    big = cpu["cores"] >= 8
    features = {
        "cover": {  # 翻唱：分离 + DDSP 变声
            "label": "翻唱（人声分离 + 变声）",
            "device": "cuda" if cuda_ok and vram >= 6 else "cpu",
            "sep_device": "cuda" if cuda_ok and vram >= 2 else "cpu",
            "synth_steps": 32 if (cuda_ok and vram >= 12) else 24 if (cuda_ok and vram >= 8) else 20 if cuda_ok else (16 if big else 12),
            "precision": "fp16" if cuda_ok and cc >= 7 else "fp32",
            "quality_note": "完整质量" if cuda_ok and vram >= 8 else
                            "步数略降（听感差别很小）" if cuda_ok else
                            "CPU：步数 12~16（实测 20 秒人声约 13~23 秒，与显卡结果频谱相关 0.985）",
        },
        "create": {  # 原创：ACE-Step
            "label": "原创歌曲（ACE-Step）",
            "model": ("acestep-v15-xl-turbo" if cuda_ok and vram >= 12 else
                      "acestep-v15-turbo" if cuda_ok and vram >= 4 else "acestep-v15-turbo"),
            "device": "cuda" if cuda_ok and vram >= 4 else "cpu",
            "offload": bool(cuda_ok and vram < 8),
            "vae_on_cpu": bool(cuda_ok and vram < 6),
            "quality_note": ("4B 模型满质量" if cuda_ok and vram >= 12 else
                             "2B turbo（8 步），显存不足时自动把部分层放内存" if cuda_ok else
                             "CPU：暂不支持——实测 PyTorch 在 CPU 上跑 ACE 会随机原生崩溃（偶尔能出歌，约 1 分钟/10 秒），需要 NVIDIA 显卡"),
            "supported": bool(cuda_ok),
        },
        "asr": {  # 歌词识别 faster-whisper（CTranslate2，CPU int8 本身就是加速路线）
            "label": "歌词识别（Whisper）",
            "device": "cuda" if cuda_ok else "cpu",
            "compute_type": "float16" if cuda_ok else "int8",
            "model": "large-v3" if (cuda_ok or (big and ram >= 16)) else "small",
            "quality_note": "large-v3" if (cuda_ok or (big and ram >= 16)) else "低配用 small 模型，识别率略降（CPU int8：20 秒约 9~13 秒）",
        },
        "train": {
            "label": "训练音色",
            "device": "cuda" if cuda_ok and vram >= 6 else "cpu",
            "supported": bool(cuda_ok and vram >= 6),
            "quality_note": "GPU 训练" if cuda_ok and vram >= 6 else "CPU 训练极慢（数小时~数天），建议只用现成音色",
        },
    }
    # 舞台（浏览器渲染）：按 CPU 核数/内存/显卡粗分，画质只降分辨率和粒子，不砍功能
    # 独显：NVIDIA，或带型号的 Radeon（RX/Pro）/ Intel Arc；核显（Radeon Graphics、Iris、UHD）按均衡档
    dgpu = cuda_ok or any(re.search(r"radeon.*(rx|pro)\s*\d|arc\s*a?\d|geforce|rtx|gtx", g["name"], re.I)
                          for g in s.get("gpus") or [])
    if ram >= 16 and cpu["cores"] >= 6 and dgpu:
        stage = {"tier": "full", "bg_max_w": 1920, "fx_scale": 1.0, "fps_cap": 0}
    elif ram >= 8 and cpu["cores"] >= 4:
        stage = {"tier": "balanced", "bg_max_w": 1440, "fx_scale": .75, "fps_cap": 60}
    else:
        stage = {"tier": "lite", "bg_max_w": 1280, "fx_scale": .5, "fps_cap": 30}
    stage["label"] = {"full": "全效果", "balanced": "均衡（背景 1440 宽、特效 3/4 分辨率、限 60 帧）",
                      "lite": "轻量（背景 1280 宽、特效半分辨率、限 30 帧）"}[stage["tier"]]
    features["stage"] = dict(stage, label_name="唱歌舞台", device="gpu-webgl")

    single_cpu = cpu["cores"] <= 2
    if single_cpu:
        notes.append("单核/双核 CPU：可以运行，但所有 AI 处理会很慢；舞台用轻量档。")

    return {"backend": backend, "tier": tier, "vendors": vendors, "cuda_ok": cuda_ok,
            "cpu_threads": threads, "cpu_env": cpu_env, "features": features, "notes": notes,
            "summary": _summary(backend, tier, s, threads)}


def _summary(backend, tier, s, threads) -> str:
    cpu = s["cpu"]
    g = ", ".join(f"{x['name']}" for x in s.get("gpus") or []) or "无独立显卡"
    tl = {"high": "高配全开", "mid": "中配", "low": "低配", "cpu_fast": "CPU 加速（多核）", "cpu_min": "CPU 最低配"}[tier]
    return (f"{tl} · {'CUDA' if backend == 'cuda' else 'CPU'} · CPU {cpu['name']}（{cpu['cores']} 核 {cpu['threads']} 线程，"
            f"推理用 {threads} 线程）· 显卡 {g} · 内存 {s.get('ram_total_gib')}GB")


def simulate(profile: str) -> dict:
    """按典型机型模拟检测结果，用来核对每种机器会拿到什么方案（不读本机）。"""
    base = {"os": "Windows 10", "drives": [], "ram_free_gib": 8}
    P = {
        "single_core": dict(cpu={"name": "Intel Celeron G1840", "vendor": "intel", "cores": 1, "threads": 1, "isa": {"avx2": False}},
                            gpus=[{"name": "Intel HD Graphics", "vendor": "intel"}], nvidia=None, ram_total_gib=4),
        "intel_igpu": dict(cpu={"name": "Intel Core i5-1135G7", "vendor": "intel", "cores": 4, "threads": 8, "isa": {"avx2": True, "avx512": True}},
                           gpus=[{"name": "Intel Iris Xe Graphics", "vendor": "intel"}], nvidia=None, ram_total_gib=16),
        "intel_arc": dict(cpu={"name": "Intel Core i7-12700", "vendor": "intel", "cores": 12, "threads": 20, "isa": {"avx2": True}},
                          gpus=[{"name": "Intel Arc A770", "vendor": "intel"}], nvidia=None, ram_total_gib=32),
        "amd_cpu_amd_gpu": dict(cpu={"name": "AMD Ryzen 7 5800X", "vendor": "amd", "cores": 8, "threads": 16, "isa": {"avx2": True}},
                                gpus=[{"name": "AMD Radeon RX 6700 XT", "vendor": "amd"}], nvidia=None, ram_total_gib=32),
        "amd_apu": dict(cpu={"name": "AMD Ryzen 5 5600G", "vendor": "amd", "cores": 6, "threads": 12, "isa": {"avx2": True}},
                        gpus=[{"name": "AMD Radeon Graphics", "vendor": "amd"}], nvidia=None, ram_total_gib=16),
        "amd_cpu_nvidia": dict(cpu={"name": "AMD Ryzen 9 7950X", "vendor": "amd", "cores": 16, "threads": 32, "isa": {"avx2": True, "avx512": True}},
                               gpus=[{"name": "NVIDIA GeForce RTX 4070", "vendor": "nvidia"}],
                               nvidia={"name": "RTX 4070", "total_gib": 12, "free_gib": 11, "cc_major": 8, "compute_cap": "8.9"}, ram_total_gib=32),
        "nvidia_low": dict(cpu={"name": "Intel Core i5-9400F", "vendor": "intel", "cores": 6, "threads": 6, "isa": {"avx2": True}},
                           gpus=[{"name": "NVIDIA GeForce GTX 1650", "vendor": "nvidia"}],
                           nvidia={"name": "GTX 1650", "total_gib": 4, "free_gib": 3.5, "cc_major": 7, "compute_cap": "7.5"}, ram_total_gib=16),
        "nvidia_old": dict(cpu={"name": "Intel Core i5-4590", "vendor": "intel", "cores": 4, "threads": 4, "isa": {"avx2": True}},
                           gpus=[{"name": "NVIDIA GeForce GTX 750 Ti", "vendor": "nvidia"}],
                           nvidia={"name": "GTX 750 Ti", "total_gib": 2, "free_gib": 1.8, "cc_major": 5, "compute_cap": "5.0"}, ram_total_gib=8),
    }
    s = dict(base, **P[profile])
    return {"profile": profile, "plan": adapt_plan(s)}


SIM_PROFILES = ("single_core", "intel_igpu", "intel_arc", "amd_cpu_amd_gpu", "amd_apu",
                "amd_cpu_nvidia", "nvidia_low", "nvidia_old")


# ---------------------------------------------------------------------------
# 3) 部署清单 + 测速
# ---------------------------------------------------------------------------
def deploy_manifest(paths: dict, setup_report: dict | None, ace: dict | None,
                    asr: dict | None, plugin_dir: Path) -> list[dict]:
    """整个插件要部署的东西，按功能分组。setup_report 是 setup.check_dependencies 的结果。"""
    rows: list[dict] = []
    by_key = {i["key"]: i for i in (setup_report or {}).get("items") or []}

    def add(group, key, label, ready, size_gb, source, kind, detail="", required=True, action=""):
        rows.append({"group": group, "key": key, "label": label, "ready": bool(ready),
                     "size_gb": round(float(size_gb or 0), 2), "source": source, "kind": kind,
                     "detail": detail, "required": required, "action": action})

    G1 = "基础环境（所有功能都要）"
    for k, src, kind in (("python_host", "宿主自带", "环境"), ("ffmpeg", "winget Gyan.FFmpeg", "程序"),
                         ("base_python", "NJU 镜像 / python-build-standalone", "环境"),
                         ("cuda_driver", "NVIDIA 官网驱动（AMD/Intel 不需要）", "驱动"), ("disk", "本机", "检查")):
        i = by_key.get(k)
        if i:
            add(G1, k, i["label"], i["ok"], i.get("size_gb", 0) if not i["ok"] else 0, src, kind,
                i.get("detail", ""), bool(i.get("required")), i.get("action", ""))
    G2 = "翻唱（人声分离 + 变声 + 训练）"
    srcmap = {"synth_env": "PyPI 清华/阿里云 + PyTorch 官方", "torch": "PyTorch 官方 cu128 / cpu",
              "ddsp_deps": "PyPI 清华/阿里云", "capture_native": "PyPI 清华", "sep_env": "PyPI 清华/阿里云",
              "audio_separator": "PyPI 清华", "ddsp_repo": "GitHub（gh-proxy 镜像）",
              "ddsp_contentvec": "hf-mirror", "ddsp_vocoder": "GitHub Release（镜像）", "ddsp_rmvpe": "GitHub Release（镜像）",
              "ddsp_reflow_base": "hf-mirror", "ddsp_base_low": "hf-mirror", "ddsp_base_high": "hf-mirror",
              "sep_karaoke": "GitHub Release（镜像）"}
    for k, src in srcmap.items():
        i = by_key.get(k)
        if i:
            add(G2, k, i["label"], i["ok"], i.get("size_gb", 0) if not i["ok"] else 0, src,
                "模型" if k.startswith(("ddsp_c", "ddsp_v", "ddsp_r", "ddsp_b", "sep_k")) else "环境",
                i.get("detail", ""), bool(i.get("required")), i.get("action", ""))
    G3 = "原创歌曲（ACE-Step）"
    a = ace or {}
    deps_ok = not any("依赖" in str(p) for p in (a.get("problems") or []))
    add(G3, "ace_deps", "ACE 依赖（diffusers / transformers / peft 等，约 0.1GB）", bool(a) and deps_ok, 0 if deps_ok else .1,
        "PyPI 清华", "环境", action="" if deps_ok else "ace:install")
    have = set(a.get("models") or [])
    for m in (a.get("model_catalog") or []):
        if m["name"] not in ("acestep-v15-turbo", "acestep-v15-xl-turbo", "acestep-v15-sft"):
            continue
        rdy = m["name"] in have
        add(G3, "ace:" + m["name"], f"ACE 模型 {m['name']}（{m['family']}，显存 ≥{m['vram_min_gb']}GB）", rdy,
            0 if rdy else m.get("download_gb", 0), "ModelScope 国内源 / HuggingFace", "模型",
            m.get("best_for", ""), required=m["name"] == "acestep-v15-turbo",
            action="" if rdy else "ace:download:" + m["name"])
    G4 = "歌词识别（Whisper，借用「为她所听」插件的环境）"
    add(G4, "asr", "faster-whisper 歌声模型", bool(asr), 0 if asr else 1.6,
        "随「为她所听」插件安装", "模型", str((asr or {}).get("model_dir") or "未找到：没有时用平台歌词/手填歌词"),
        required=False)
    G5 = "唱歌舞台（已随插件发布，无需下载）"
    st = Path(plugin_dir) / "static"
    for f, lab in (("fonts/zcool-kuaile/ZCOOLKuaiLe-Regular.woff2", "站酷快乐体（OFL）"),
                   ("fonts/lxgw-wenkai/WenkaiSubset-Regular.woff2", "霞鹜文楷子集（OFL）"),
                   ("fonts/zen-maru-gothic/ZenMaruGothic-Regular.woff2", "Zen Maru Gothic（OFL）"),
                   ("stage/vendor/pixi.min.js", "PixiJS（MIT）"), ("stage/vendor/rough.js", "Rough.js（MIT）"),
                   ("stage/vendor/doodles.json", "doodle-icons 线稿（MIT）")):
        p = st / f
        add(G5, "stage:" + f, lab, p.is_file(), 0, "插件自带", "素材",
            f"{p.stat().st_size // 1024} KB" if p.is_file() else "缺失", required=True)
    return rows


# 测速目标：每个下载源挑一个真实要下的大文件，Range 下前 N MB。
def speed_targets() -> list[dict]:
    from . import setup as S  # 延迟导入，避免循环
    gh = [("GitHub 官方", S.VOCODER_ZIP)] + [(f"GitHub 镜像 {urllib.parse.urlsplit(m).hostname}", m + S.VOCODER_ZIP)
                                             for m in S.GH_MIRRORS]
    hf = [(f"HF 镜像 {h}", f"https://{h}/lengyue233/content-vec-best/resolve/main/pytorch_model.bin") for h in S.HF_MIRROR_HOSTS]
    hf.append(("HuggingFace 官方", "https://huggingface.co/lengyue233/content-vec-best/resolve/main/pytorch_model.bin"))
    whl = "packages/a3/dd/4b822569d6b96c39d1215dbae0582fd99954dcbcf0c1a13c61783feaca3f/numpy-2.2.6-cp310-cp310-win_amd64.whl"
    pypi = [("PyPI 清华", "https://pypi.tuna.tsinghua.edu.cn/" + whl),
            ("PyPI 阿里云", "https://mirrors.aliyun.com/pypi/" + whl),
            ("PyPI 官方", "https://files.pythonhosted.org/" + whl)]
    ta = "torchaudio-2.9.1%2Bcu128-cp310-cp310-win_amd64.whl"
    torch = [("PyTorch 官方 (cu128)", "https://download.pytorch.org/whl/cu128/" + ta),
             ("PyTorch 阿里云镜像", "https://mirrors.aliyun.com/pytorch-wheels/cu128/" + ta)]
    vae = "ACE-Step/Ace-Step1.5/resolve/{}/vae/diffusion_pytorch_model.safetensors"
    ms = [("ModelScope（ACE 国内源）", "https://www.modelscope.cn/models/" + vae.format("master")),
          ("HF 镜像 hf-mirror.net（ACE）", "https://hf-mirror.net/" + vae.format("main")),
          ("HuggingFace 官方（ACE）", "https://huggingface.co/" + vae.format("main"))]
    py = [("Python 便携版 NJU 镜像", S.PYTHON_STANDALONE_MIRROR + "/LatestRelease/cpython-3.11.18%2B20261110-x86_64-pc-windows-msvc-install_only.tar.gz")]
    out = []
    for grp, lst in (("GitHub（仓库/声码器/分离权重）", gh), ("HuggingFace（编码器/底模）", hf), ("PyPI（Python 包）", pypi),
                     ("PyTorch", torch), ("ACE 模型源", ms), ("Python 便携版", py)):
        for label, url in lst:
            out.append({"group": grp, "label": label, "url": url})
    return out


def _probe_one(t: dict, nbytes: int, timeout: float) -> dict:
    from . import setup as S
    r = dict(t)
    started = time.time()
    try:
        req = urllib.request.Request(t["url"], headers={"User-Agent": "NEKO-music-synth/0.3",
                                                        "Range": f"bytes=0-{nbytes - 1}"})
        with S._opener().open(req, timeout=timeout) as resp:
            ttfb = time.time() - started
            got, t0 = 0, time.time()
            while got < nbytes and time.time() - t0 < timeout:
                chunk = resp.read(65536)
                if not chunk:
                    break
                got += len(chunk)
            el = max(.001, time.time() - t0)
            ctype = resp.headers.get("Content-Type", "")
            html = "text/html" in ctype and got < 200000
            r.update(ok=not html, status=getattr(resp, "status", 200), ttfb_ms=int(ttfb * 1000),
                     bytes=got, seconds=round(el, 2), mb_per_s=round(got / el / 2**20, 2),
                     error="返回的是网页而不是文件（镜像失效）" if html else "")
    except Exception as exc:  # noqa: BLE001
        r.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:140]}", seconds=round(time.time() - started, 2))
    sp = r.get("mb_per_s") or 0
    r["verdict"] = ("不通" if not r.get("ok") else "很快" if sp >= 5 else "快" if sp >= 1.5 else "一般" if sp >= .4 else "慢")
    return r


def speed_test(nbytes: int = 4 * 2**20, timeout: float = 15.0) -> dict:
    targets = speed_targets()
    with ThreadPoolExecutor(max_workers=4) as ex:
        rows = list(ex.map(lambda t: _probe_one(t, nbytes, timeout), targets))
    # 慢或不通的源单独重测一次（不并发），取好的那次：避免一次网络抖动把能用的源判成「慢」
    for i, r in enumerate(rows):
        if r["verdict"] in ("慢", "不通") and "网页" not in (r.get("error") or ""):
            r2 = _probe_one(targets[i], nbytes, timeout)
            if (r2.get("mb_per_s") or 0) > (r.get("mb_per_s") or 0):
                r2["retried"] = True
                rows[i] = r2
    best: dict = {}
    for r in rows:
        if r.get("ok") and (r["group"] not in best or r["mb_per_s"] > best[r["group"]]["mb_per_s"]):
            best[r["group"]] = r
    groups = []
    for g in dict.fromkeys(t["group"] for t in targets):
        b = best.get(g)
        groups.append({"group": g, "ok": bool(b), "best": b["label"] if b else "",
                       "mb_per_s": b["mb_per_s"] if b else 0, "verdict": b["verdict"] if b else "不通"})
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "probe_mb": round(nbytes / 2**20, 1),
            "rows": rows, "groups": groups, "all_ok": all(g["ok"] for g in groups)}
