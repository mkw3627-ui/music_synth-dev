"""下载物目录（S1 产物）：每个下载物的层级、来源、大小与 sha256。

本模块**只描述数据**，不做任何网络或文件操作，纯标准库、无相对导入，
便于守护进程与 pytest 直接 ``import download_catalog``。

来源与数值全部来自 2026-11-21 的实测（见 ``tools/dl_sources_report.json``）：

* HF 系列的 sha256 取自 HF tree API 的 ``lfs.oid``——已实测确认它等于整包
  sha256（拿最小的 ``ddsp6.3_6x512_6x512.sf_dlc`` 完整下载比对，一致）。
* GitHub Release 的 sha256 取自 Release API 的 ``digest``，或完整下载后计算。
* PyPI / PyTorch 依赖包不在此列（由 pip 自行解析，见 SKIP 说明）。

层级定义（与 PLAN_download_tiering.md B.1 一致）：

* ``T0`` 基础环境：Python、venv、torch、pip 依赖、ffmpeg。前台装完才能进主页。
* ``T1`` 程序：小体积代码仓库（< 50MB）。与 T0 一起前台装。
* ``T2`` 模型：大权重。进入主页后由后台守护进程静默下载。

被实测剔除的候选源（不进 sources，原因见 ``REJECTED_SOURCES``）：

* ``ghfast.top`` —— TLS 握手超时（2026-11-21）。
* ``hk.gh-proxy.com`` —— 返回 500 ``Domain Not Found``。
* ``gh.llkk.cc`` —— 522；``ghp.ci`` / ``hub.gitmirror.com`` —— 域名不存在。
* ``gh-proxy.net`` / ``gitproxy.click`` —— 返回 text/html（镜像失效页）。
* ``ghproxy.cc`` —— 证书校验失败；``gh.api.99988866.xyz`` —— SSLv3 握手失败。
* ``hf-mirror.com`` —— 308 永久跳转回 huggingface.co，已不再是镜像
  （原先担心的「跳到国内不通 CDN」现在表现为纯跳转，留作 fallback 无意义）。
* ModelScope 上没有 ``lengyue233/content-vec-best`` 与 ``SVCFusion/DDSP6.3-Pretrain``
  的同名仓库（均 404），故这两项只有 HF 系源。
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# 源前缀
# ---------------------------------------------------------------------------
#: GitHub 内容镜像前缀（后接完整 github.com URL）。gh-proxy.com 实测最快且最稳。
GH_MIRRORS = ("https://gh-proxy.com/", "https://ghproxy.net/", "https://cors.isteed.cc/")
#: HF 系镜像主机。注意 hf-mirror.com 已退化为 308 跳转，故不再列入。
HF_MIRRORS = ("hf-mirror.net",)
HF_CANONICAL = "huggingface.co"
#: ModelScope 内容根（``/models/{repo}/resolve/{ref}/{path}``）。
MS_ROOT = "https://www.modelscope.cn/models"


def ms_repo(repo: str, ref: str = "master") -> str:
    """ModelScope 仓库内容根（**已含** ref，调用方只需拼文件路径）。"""
    return f"{MS_ROOT}/{repo}/resolve/{ref}"
#: PyPI 镜像（pip --index-url；实测都可用，按国内速度排序）。
PYPI_INDEXES = (
    "https://mirrors.aliyun.com/pypi/simple/",
    "https://mirrors.ustc.edu.cn/pypi/simple/",
    "https://mirrors.cloud.tencent.com/pypi/simple/",
    "https://pypi.tuna.tsinghua.edu.cn/simple/",
    "https://pypi.org/simple/",
)
#: PyTorch wheel 源（``-f`` find-links 方式）。官方 cu128 实测 0.75MB/s，阿里云 35MB/s。
TORCH_INDEXES = (
    "https://mirrors.aliyun.com/pytorch-wheels/cu128/",
    "https://download.pytorch.org/whl/cu128",
)
#: 便携 Python 的国内镜像（NJU 的 GitHub Release 镜像，实测 44MB/s）。
PYTHON_STANDALONE_MIRROR = (
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
)

#: 被实测剔除的源 → 原因。仅供报告/诊断使用，不参与下载排序。
REJECTED_SOURCES = {
    "ghfast.top": "TLS 握手超时（2026-11-21 实测）",
    "hk.gh-proxy.com": "HTTP 500 Domain Not Found",
    "gh.llkk.cc": "HTTP 522",
    "ghp.ci": "域名解析失败",
    "hub.gitmirror.com": "域名解析失败",
    "gh-proxy.net": "返回 text/html（镜像失效页）",
    "gitproxy.click": "返回 text/html（镜像失效页）",
    "ghproxy.cc": "TLS 证书校验失败",
    "gh.api.99988866.xyz": "SSLv3 握手失败",
    "hf-mirror.com": "308 永久跳转回 huggingface.co，已非镜像",
}


# ---------------------------------------------------------------------------
# 源 URL 构造
# ---------------------------------------------------------------------------
def _hf(repo: str, path: str, host: str = HF_CANONICAL, ref: str = "main") -> str:
    return f"https://{host}/{repo}/resolve/{ref}/{path}"


def _hf_sources(repo: str, path: str) -> list[str]:
    """同一 HF 文件的全部可用源：hf-mirror.net 优先（实测直出字节），再官方。"""
    return ([_hf(repo, path, host) for host in HF_MIRRORS]
            + [_hf(repo, path, HF_CANONICAL)])


def _ms(repo: str, path: str, ref: str = "master") -> str:
    return f"{ms_repo(repo, ref)}/{path}"


def _gh(url: str) -> list[str]:
    """GitHub URL → [镜像×N, 官方]。顺序即默认优先级。"""
    return [mirror + url for mirror in GH_MIRRORS] + [url]


def _codeload(repo: str, commit: str) -> str:
    return f"https://codeload.github.com/{repo}/zip/{commit}"


def _release(repo: str, tag: str, asset: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{asset}"


# ---------------------------------------------------------------------------
# 功能与层级
# ---------------------------------------------------------------------------
#: 功能元数据。``depends`` 为前置功能；``pages`` 为门控页面 id；``gpu_required`` 需 NVIDIA。
FEATURES: dict[str, dict] = {
    "cover": {
        "label": "翻唱",
        "depends": (),
        "pages": ("sing",),
        "gpu_required": False,
        "default": True,
        "note": "其他功能的前置；默认勾选",
    },
    "train": {
        "label": "音色训练",
        "depends": ("cover",),
        "pages": ("materials", "train"),
        "gpu_required": False,
        "default": False,
        "note": "没有 NVIDIA 时可选但不建议（CPU 训练极慢）",
    },
    "create": {
        "label": "原创歌曲",
        "depends": ("cover",),
        "pages": ("create",),
        "gpu_required": True,
        "default": False,
        "note": "ACE-Step；需 NVIDIA 显存 ≥ 6GB",
    },
    "soulx": {
        "label": "SoulX 唱法",
        "depends": ("create",),
        "pages": ("create",),
        "gpu_required": True,
        "default": False,
        "note": "原创页内的后处理引擎区块；本地经验证过才可用",
    },
    "capture": {
        "label": "听歌采集",
        "depends": ("cover",),
        "pages": ("sing",),
        "gpu_required": False,
        "default": True,
        "note": "逐应用声道采集；sing 页局部门控。采集组件是 pip 装进 envs\\ddsp 的，"
                "所以依赖 cover 的环境（计划 B.1 表未标注，实测 install_capture_native 必须有 synth_python）",
    },
    "stage": {
        "label": "唱歌舞台",
        "depends": (),
        "pages": (),
        "gpu_required": False,
        "default": True,
        "note": "随插件发布，无下载，不门控",
    },
}

#: 层级顺序（前台安装顺序即 T0 → T1）。
TIERS = ("T0", "T1", "T2")


# ---------------------------------------------------------------------------
# 下载物
# ---------------------------------------------------------------------------
# 字段：
#   feature   所属功能（"*" 表示多个功能共用，见 features 元组）
#   tier      T0/T1/T2
#   required  是否为该功能必需项（可选档位为 False）
#   kind      file | zip | dlc | sep | repo | pip | winget | venv
#   label     给用户看的名字
#   size      字节（实测；0 表示由 pip 解析、无法预知）
#   sha256    整包 sha256（能取得时；None 表示不适用）
#   sources   候选源（顺序为默认优先级）
#   post      后处理：none | unzip:<mode> | dlc_convert | hf_layout
#   target    paths dict 的键，或相对键（见 target_path()）
ARTIFACTS: dict[str, dict] = {
    # ---------------- T0 基础环境 ----------------
    "base_python": {
        "feature": "cover", "features": ("cover", "train", "create", "soulx", "capture"),
        "tier": "T0", "required": True, "kind": "file",
        "label": "便携版 Python 3.11（python-build-standalone）",
        "size": 49079288,
        "sha256": "5ca7e23bbcc8933a75f4ae9c3a5e5d1ffae5fd143aec9a9d77cf5124cc6e0eef",
        "sources": [
            PYTHON_STANDALONE_MIRROR + "/LatestRelease/"
            "cpython-3.11.18%2B20261110-x86_64-pc-windows-msvc-install_only.tar.gz",
        ] + _gh("https://github.com/astral-sh/python-build-standalone/releases/download/"
                "20261110/cpython-3.11.18+20261110-x86_64-pc-windows-msvc-install_only.tar.gz"),
        "post": "none",
        "target": "compute_root/runtime/python",
        "note": "NJU 镜像实测 44.8MB/s；官方经 gh-proxy 0.72MB/s",
    },
    "base_python_pinned": {
        "feature": "cover", "features": ("cover", "train", "create", "soulx", "capture"),
        "tier": "T0", "required": False, "kind": "file",
        "label": "便携版 Python 3.10.18（固定兜底版本）",
        "size": 38943022,
        "sha256": "d26dba4ec86f49ecbc6800e55f72691b9873115fa7c00f254f28dc04a03e8c13",
        "sources": _gh(
            "https://github.com/astral-sh/python-build-standalone/releases/download/"
            "20250612/cpython-3.10.18+20250612-x86_64-pc-windows-msvc-install_only.tar.gz"),
        "post": "none",
        "target": "compute_root/runtime/python",
        "note": "setup.py 原来的固定 URL 写成 shared-install_only（404），已实测修正",
    },
    "ffmpeg": {
        "feature": "cover", "features": ("cover", "train", "create", "soulx"),
        "tier": "T0", "required": True, "kind": "winget",
        "label": "FFmpeg（winget Gyan.FFmpeg，失败回退 zip）",
        "size": 0, "sha256": None,
        "sources": ["winget:Gyan.FFmpeg"],
        "post": "none",
        "target": "tools/ffmpeg/bin",
        "note": "winget 不可用时的兜底见 ffmpeg_zip",
    },
    "ffmpeg_zip": {
        "feature": "cover", "features": ("cover", "train", "create", "soulx"),
        "tier": "T0", "required": False, "kind": "zip",
        "label": "FFmpeg 便携 zip（winget 兜底，gyan.dev essentials）",
        "size": 119467219,
        "sha256": "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd",
        "sources": [
            "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        ] + _gh("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                "ffmpeg-master-latest-win64-gpl.zip"),
        "post": "unzip:ffmpeg_bin",
        "target": "tools/ffmpeg/bin",
        "note": "gyan 1.6MB/s；BtbN 构建体积不同（205MB），只作备选",
    },
    "synth_env": {
        "feature": "cover", "features": ("cover", "train", "create", "soulx", "capture"),
        "tier": "T0", "required": True, "kind": "venv",
        "label": "合成环境 venv（envs\\ddsp，各功能共用）",
        "size": 0, "sha256": None, "sources": [], "post": "none",
        "target": "synth_python",
        "note": "本地创建，不下载",
    },
    "torch": {
        "feature": "cover", "features": ("cover", "train", "create", "soulx", "capture"),
        "tier": "T0", "required": True, "kind": "pip",
        "label": "PyTorch + torchaudio（cu128 或 cpu）",
        "size": 0, "sha256": None,
        "sources": list(TORCH_INDEXES),
        "post": "none",
        "target": "synth_python",
        "note": "cu128 约 2.9GB、cpu 约 0.25GB；阿里云 35MB/s vs 官方 0.75MB/s",
    },
    "ddsp_deps": {
        "feature": "cover", "features": ("cover", "train", "create", "soulx", "capture"),
        "tier": "T0", "required": True, "kind": "pip",
        "label": "DDSP-SVC 依赖（librosa/pyworld/transformers 等）",
        "size": 0, "sha256": None, "sources": list(PYPI_INDEXES), "post": "none",
        "target": "synth_python",
    },
    "audio_separator": {
        "feature": "cover", "features": ("cover", "train"),
        "tier": "T0", "required": True, "kind": "pip",
        "label": "audio-separator（人声分离）",
        "size": 0, "sha256": None, "sources": list(PYPI_INDEXES), "post": "none",
        "target": "sep_python",
    },
    "capture_native": {
        "feature": "capture", "features": ("capture",),
        "tier": "T0", "required": True, "kind": "pip",
        "label": "采集组件（proc-tap / winrt）",
        "size": 0, "sha256": None, "sources": list(PYPI_INDEXES), "post": "none",
        "target": "synth_python",
    },
    "ace_deps": {
        "feature": "create", "features": ("create", "soulx"),
        "tier": "T0", "required": True, "kind": "pip",
        "label": "ACE 依赖（diffusers/peft/torchao 等，装进 acestep\\pylibs）",
        "size": 0, "sha256": None, "sources": list(PYPI_INDEXES), "post": "none",
        "target": "acestep/pylibs",
    },

    # ---------------- T1 程序 ----------------
    "ddsp_repo": {
        "feature": "cover", "features": ("cover", "train"),
        "tier": "T1", "required": True, "kind": "zip",
        "label": "DDSP-SVC 6.3 仓库（commit b9d4e6a）",
        "size": 95763,
        "sha256": "c7a01d73448ebf07be5778c3386a71c811648884b3d23e879e55bdcd3436d617",
        "sources": _gh(_codeload("yxlllc/DDSP-SVC",
                                 "b9d4e6a58182448aa7259a636570d53c49c92b1d")),
        "post": "unzip:repo_root",
        "target": "ddsp_root",
        "note": "72 个条目 / 283KB 纯代码（仓库本身不含权重）；实测确认完整，非截断",
    },
    "soulx_code": {
        "feature": "soulx", "features": ("soulx",),
        "tier": "T1", "required": True, "kind": "zip",
        "label": "SoulX-Singer 代码（GitHub main 分支）",
        "size": 13046966,
        "sha256": "29aee5e232758c5b59aed37e67f8f061788e1c4fe5177410b06c822174f300b9",
        "sources": _gh("https://github.com/Soul-AILab/SoulX-Singer/archive/refs/heads/main.zip"),
        "post": "unzip:repo_root",
        "target": "soulx_root",
        "note": "main 分支 zip 无稳定 sha（会随上游更新），安装时以实际下载为准",
    },

    # ---------------- T2 模型：翻唱 ----------------
    "ddsp_contentvec": {
        "feature": "cover", "features": ("cover", "train"),
        "tier": "T2", "required": True, "kind": "file",
        "label": "ContentVec 编码器（contentvec768l12tta2x）",
        "size": 378342945,
        "sha256": "d8dd400e054ddf4e6be75dab5a2549db748cc99e756a097c496c099f65a4854e",
        "sources": _hf_sources("lengyue233/content-vec-best", "pytorch_model.bin"),
        "post": "none",
        "target": "ddsp_pretrain_contentvec",
        "note": "ModelScope 无同名仓库（404）→ 只有 HF 系两个源",
    },
    "ddsp_vocoder": {
        "feature": "cover", "features": ("cover", "train"),
        "tier": "T2", "required": True, "kind": "zip",
        "label": "NSF-HiFiGAN 声码器（pc_nsf_hifigan 44.1k）",
        "size": 52675337,
        "sha256": "9d98ba73727f2abb75172cf8249d75182237e8472fc3b6ed09c721ae8b0e83c6",
        "sources": _gh(_release("openvpi/vocoders",
                                "pc-nsf-hifigan-44.1k-hop512-128bin-2025.02",
                                "pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.zip")),
        "post": "unzip:flatten",
        "target": "ddsp_pretrain_vocoder",
        "rename": {"model.ckpt": "model"},
        "note": "完整下载计算所得 sha256（Release 资产无 digest）",
    },
    "ddsp_rmvpe": {
        "feature": "cover", "features": ("cover", "train"),
        "tier": "T2", "required": True, "kind": "zip",
        "label": "RMVPE 音高提取器（230917）",
        "size": 340638958,
        "sha256": "54ae40d9c066d998b94574f6ef0deea19ed1565bd655b3f0d9b1ad612fb5309c",
        "sources": _gh(_release("yxlllc/RMVPE", "230917", "rmvpe.zip")),
        "post": "unzip:into_base",
        "target": "ddsp_pretrain_rmvpe",
        "note": "完整下载计算所得 sha256",
    },
    "sep_karaoke": {
        "feature": "cover", "features": ("cover", "train"),
        "tier": "T2", "required": True, "kind": "sep",
        "label": "人声分离权重（Mel-Band RoFormer Karaoke）",
        "size": 1719139254,
        "sha256": "d3aa262ac01df870b9fc033e9c7b6cad33fe04fc9c148b6c40841326a515a0e0",
        "sources": (
            [_ms("Soul-AILab/SoulX-Singer-Preprocess",
                 "mel-band-roformer-karaoke/mel_band_roformer_karaoke_becruily.ckpt")]
            + _gh(_release("nomadkaraoke/python-audio-separator", "model-configs",
                           "mel_band_roformer_karaoke_becruily.ckpt"))
            + [_hf("Soul-AILab/SoulX-Singer-Preprocess",
                   "mel-band-roformer-karaoke/mel_band_roformer_karaoke_becruily.ckpt")]
        ),
        "files": (
            ("mel_band_roformer_karaoke_becruily.ckpt",
             "d3aa262ac01df870b9fc033e9c7b6cad33fe04fc9c148b6c40841326a515a0e0",
             1719139254),
            # yaml 不在 ckpt 的源里：第 4 项给它自己的源（GitHub Release，S13 实测 sha 一致）
            ("config_mel_band_roformer_karaoke_becruily.yaml",
             "cd37b0dcc285fc22d88090415722ac7127ee1d9ea2f3346c3b8d8fcc61e0c74b",
             1724,
             tuple(_gh(_release("nomadkaraoke/python-audio-separator", "model-configs",
                                "config_mel_band_roformer_karaoke_becruily.yaml")))),
        ),
        "post": "none",
        "target": "sep_model_dir",
        "note": "ModelScope 副本实测 60MB/s 且首 1MB 与 GitHub 完全一致；yaml 在 HF 副本里是另一个变体（batch_size/num_overlap 不同），不要混用",
    },
    "ddsp_reflow_base": {
        "feature": "train", "features": ("train",),
        "tier": "T2", "required": True, "kind": "dlc",
        "label": "训练底模（DDSP-SVC 6.3 PT，6x512_6x1024）",
        "size": 219737219,
        "sha256": "83cebc670fc759a8946f74e7b18871993807277065a91f7727c0577412d06bab",
        "sources": _hf_sources("SVCFusion/DDSP6.3-Pretrain", "ddsp6.3_6x512_6x1024.sf_dlc"),
        "post": "dlc_convert",
        "target": "ddsp_pretrain_reflow",
        "note": ".sf_dlc 是 torch zip，post 时转成 model_0.pt；ModelScope 无同名仓库",
    },
    "ddsp_base_low": {
        "feature": "train", "features": ("train",),
        "tier": "T2", "required": False, "kind": "dlc",
        "label": "低质量训练底模（6x512_6x512）",
        "size": 98915227,
        "sha256": "80b72cd652f94aba5ddb72358cbf1f1bed2335490cd069085a11794979005c25",
        "sources": _hf_sources("SVCFusion/DDSP6.3-Pretrain", "ddsp6.3_6x512_6x512.sf_dlc"),
        "post": "dlc_convert",
        "target": "ddsp_base_low",
        "note": "可选档位；不装则自动回退到中档",
    },
    "ddsp_base_high": {
        "feature": "train", "features": ("train",),
        "tier": "T2", "required": False, "kind": "dlc",
        "label": "高质量训练底模（6x512_10x2048）",
        "size": 1037295691,
        "sha256": "d0c7ac7d41a0632c62c505b7ec86140f71d334cf57d14c5d5c9144cefe2b4748",
        "sources": _hf_sources("SVCFusion/DDSP6.3-Pretrain", "ddsp6.3_6x512_10x2048.sf_dlc"),
        "post": "dlc_convert",
        "target": "ddsp_base_high",
        "note": "可选档位；约 0.97GB",
    },

    # ---------------- T2 模型：原创（repo 型，文件清单由 API 动态列举） ----------------
    "ace_model": {
        "feature": "create", "features": ("create",),
        "tier": "T2", "required": True, "kind": "repo",
        "label": "ACE-Step 基础模型（turbo + vae + Qwen3-Embedding）",
        "size": 6317393408,
        "sha256": None,
        "repo_id": "ACE-Step/Ace-Step1.5",
        "components": ("acestep-v15-turbo", "vae", "Qwen3-Embedding-0.6B"),
        "sources_meta": {
            "modelscope": ms_repo("ACE-Step/Ace-Step1.5"),
            "hf_mirror": "https://hf-mirror.net/ACE-Step/Ace-Step1.5",
            "hf": "https://huggingface.co/ACE-Step/Ace-Step1.5",
        },
        "post": "hf_layout",
        "target": "acestep/checkpoints",
        "note": "实测三源（ModelScope/hf-mirror.net/huggingface.co）同名文件逐字节一致；ModelScope 25–60MB/s 最快。注意 acestep-v15-turbo 没有独立仓库，只存在于主仓库",
        # API 列不出文件时的最小清单（S7 第三级回落）。size/sha256 来自 2026-11-21 实测：
        # ModelScope repo/files 与 HF tree API 两边的 sha256 一致。
        "minimal_files": (
            ("acestep-v15-turbo/config.json", 1968,
             "74745ff704ea49164c3d2d1c99fc0670f3fc635a869f0aec2d1311e6a52d400a"),
            ("acestep-v15-turbo/model.safetensors", 4787825604,
             "3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7"),
            ("acestep-v15-turbo/silence_latent.pt", 3841215,
             "a778e9dd942f5e8b2c09c55370782d318834432b03dabbcdf70e6ed49ad6358b"),
            ("vae/config.json", 425,
             "14e019904df567f26df750317a70e2bd08f9f8f3c40ff4a24c97d1cd3f20ccd2"),
            ("vae/diffusion_pytorch_model.safetensors", 337431388,
             "da17edb604c40deaf09e9b24974e590d1ca83a374070e5d0884cfa4bed9a99b0"),
            ("Qwen3-Embedding-0.6B/config.json", 1359,
             "bb23c1607cfe059a58d8f0196cf1cebb52082b1056b8e358a579da80a5759420"),
            ("Qwen3-Embedding-0.6B/model.safetensors", 1191586416,
             "0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd"),
            ("Qwen3-Embedding-0.6B/tokenizer.json", 11423705,
             "def76fb086971c7867b829c23a26261e38d9d74e02139253b38aeb9df8b4b50a"),
            ("Qwen3-Embedding-0.6B/vocab.json", 2776833,
             "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910"),
            ("Qwen3-Embedding-0.6B/merges.txt", 1671853,
             "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5"),
            ("Qwen3-Embedding-0.6B/tokenizer_config.json", 5404,
             "443bfa629eb16387a12edbf92a76f6a6f10b2af3b53d87ba1550adfcf45f7fa0"),
            ("Qwen3-Embedding-0.6B/special_tokens_map.json", 613,
             "76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd"),
            ("Qwen3-Embedding-0.6B/chat_template.jinja", 4116,
             "87a2728cb8dc9fe424d624542f6060ec05a1d285ebbec578bb078900e33396b5"),
        ),
    },

    # ---------------- T2 模型：SoulX ----------------
    "soulx_weights": {
        "feature": "soulx", "features": ("soulx",),
        "tier": "T2", "required": True, "kind": "repo",
        "label": "SoulX-Singer 权重（svc + dit）",
        "size": 5612057432,
        "sha256": None,
        "repo_id": "Soul-AILab/SoulX-Singer",
        "components": ("model-svc.pt", "model.pt"),
        "sources_meta": {
            "modelscope": ms_repo("Soul-AILab/SoulX-Singer"),
            "hf_mirror": "https://hf-mirror.net/Soul-AILab/SoulX-Singer",
            "hf": "https://huggingface.co/Soul-AILab/SoulX-Singer",
        },
        "post": "hf_layout",
        "target": "soulx_root/models",
        "note": "ModelScope 13MB/s；HF 0.77MB/s",
        "minimal_files": (
            ("model-svc.pt", 2793965154,
             "38e72d9e15be6eef3cc937daaf81421a7b5b546816786530bb9aa346cee5d43c"),
            ("model.pt", 2818092278,
             "447eaf41f91a6b6659d55e9ec3c9b809221724fb8592aebaec35a23751a5b500"),
        ),
    },
    "soulx_rmvpe": {
        "feature": "soulx", "features": ("soulx",),
        "tier": "T2", "required": True, "kind": "repo",
        "label": "SoulX 预处理 RMVPE 音高模型",
        "size": 181184272,
        "sha256": "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193",
        "repo_id": "Soul-AILab/SoulX-Singer-Preprocess",
        "allow_patterns": ("rmvpe/*",),
        "sources_meta": {
            "modelscope": ms_repo("Soul-AILab/SoulX-Singer-Preprocess"),
            "hf_mirror": "https://hf-mirror.net/Soul-AILab/SoulX-Singer-Preprocess",
            "hf": "https://huggingface.co/Soul-AILab/SoulX-Singer-Preprocess",
        },
        "post": "hf_layout",
        "target": "soulx_root/preprocess",
        "note": "只取 rmvpe/*（同仓库的 parakeet/rosvot 等 4GB+ 不需要）",
        "minimal_files": (
            ("rmvpe/rmvpe.pt", 181184272,
             "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193"),
        ),
    },
}

#: ACE 可选 DiT 模型（repo 型，按需下载；不进默认 T2 集合）。
ACE_OPTIONAL_MODELS: dict[str, dict] = {
    "acestep-v15-turbo-shift1": {"repo_id": "ACE-Step/acestep-v15-turbo-shift1", "size": 4791782185},
    "acestep-v15-turbo-shift3": {"repo_id": "ACE-Step/acestep-v15-turbo-shift3", "size": 4791782251},
    "acestep-v15-turbo-continuous": {"repo_id": "ACE-Step/acestep-v15-turbo-continuous", "size": 4791784060},
    "acestep-v15-sft": {"repo_id": "ACE-Step/acestep-v15-sft", "size": 4791792772},
    "acestep-v15-base": {"repo_id": "ACE-Step/acestep-v15-base", "size": 4791792407},
    "acestep-v15-xl-turbo": {"repo_id": "ACE-Step/acestep-v15-xl-turbo", "size": 19953378268},
    "acestep-v15-xl-sft": {"repo_id": "ACE-Step/acestep-v15-xl-sft", "size": 19953388893},
    "acestep-v15-xl-base": {"repo_id": "ACE-Step/acestep-v15-xl-base", "size": 19953389529},
}


# ---------------------------------------------------------------------------
# 查询函数
# ---------------------------------------------------------------------------
def _expand(feature: str) -> set[str]:
    """功能 + 全部前置功能。"""
    out: set[str] = set()
    stack = [feature]
    while stack:
        f = stack.pop()
        if f in out or f not in FEATURES:
            continue
        out.add(f)
        stack.extend(FEATURES[f]["depends"])
    return out


def artifacts_for(features, tiers=None, allow_optional: bool = False) -> list[str]:
    """给定功能集合与层级，返回需要下载/安装的 artifact id 列表。

    ``features`` 为空/None 表示全部功能；前置功能会自动带入。
    ``tiers`` 为空/None 表示 T0/T1/T2 全要。
    ``allow_optional`` 为真时把可选档位与兜底源也包含进来（默认不）。
    """
    wanted_tiers = set(tiers) if tiers else set(TIERS)
    if not features:
        wanted = set(FEATURES)
    else:
        wanted = set()
        for f in features:
            wanted |= _expand(str(f))
    out: list[str] = []
    for key, spec in ARTIFACTS.items():
        if spec["tier"] not in wanted_tiers:
            continue
        # 可选档位（低/高质量底模）与兜底项（固定版本 Python、ffmpeg zip）默认不入队，
        # 由调用方显式点名才装。
        if not spec.get("required", True) and not allow_optional:
            continue
        if not wanted & set(spec.get("features") or (spec["feature"],)):
            continue
        out.append(key)
    return out


def feature_items(feature: str, allow_optional: bool = False) -> dict[str, list[str]]:
    """某个功能自身的分层清单（含前置功能，不含下游）→ {"T0": [...], ...}。

    默认只列必需项；``allow_optional`` 为真时加上低/高质量底模等可选档位。
    """
    wanted = _expand(feature)
    out: dict[str, list[str]] = {t: [] for t in TIERS}
    for key, spec in ARTIFACTS.items():
        if spec["tier"] not in out:
            continue
        if not spec.get("required", True) and not allow_optional:
            continue
        if not wanted & set(spec.get("features") or (spec["feature"],)):
            continue
        out[spec["tier"]].append(key)
    return out


def artifact_size(key: str) -> int:
    return int(ARTIFACTS.get(key, {}).get("size") or 0)


def feature_sizes(feature: str, allow_optional: bool = False) -> dict[str, int]:
    """按层级汇总字节数：{"T0": n, "T1": n, "T2": n, "total": n}。必需项口径。"""
    items = feature_items(feature, allow_optional=allow_optional)
    out = {t: sum(artifact_size(k) for k in items[t]) for t in TIERS}
    out["total"] = sum(out[t] for t in TIERS)
    return out


def target_path(key: str, paths: dict) -> Path:
    """解析目标路径。``paths`` 与 runtime.resolve_paths() 的返回一致。"""
    spec = ARTIFACTS[key]
    target = spec.get("target") or ""
    if target in paths:
        return Path(str(paths[target]))
    root = Path(str(paths.get("compute_root") or "."))
    if "/" in target:
        head, _, rest = target.partition("/")
        base = Path(str(paths.get(head) or root / head)) if head in paths else root / head
        return base / rest
    if target in ("compute_root", ""):
        return root
    return root / target


def sources_for(key: str) -> list[str]:
    return list(ARTIFACTS.get(key, {}).get("sources") or [])


def file_entries(spec: dict) -> list[tuple[str, str, int, list[str]]]:
    """多文件条目 → ``[(name, sha256, size, urls)]``。

    ``files`` 每项可带第 4 项专属源；没有时从 ``sources`` 里挑含文件名的 URL。
    只有单文件时才回落到全部 ``sources``——多文件时回落会让别的文件的字节冒充它。
    """
    files = list(spec.get("files") or ())
    sources = list(spec.get("sources") or [])
    out = []
    for entry in files:
        if isinstance(entry, (list, tuple)):
            name = str(entry[0])
            sha = str(entry[1] or "") if len(entry) > 1 else ""
            size = int(entry[2] or 0) if len(entry) > 2 else 0
            own = list(entry[3] or ()) if len(entry) > 3 else []
        else:
            name, sha, size, own = str(entry), "", 0, []
        urls = own or [u for u in sources if name in u]
        if not urls and len(files) == 1:
            urls = sources
        out.append((name, sha, size, urls))
    return out


__all__ = [
    "FEATURES", "TIERS", "ARTIFACTS", "ACE_OPTIONAL_MODELS", "REJECTED_SOURCES",
    "GH_MIRRORS", "HF_MIRRORS", "HF_CANONICAL", "MS_ROOT", "PYPI_INDEXES", "TORCH_INDEXES",
    "PYTHON_STANDALONE_MIRROR", "ms_repo",
    "artifacts_for", "feature_items", "artifact_size", "feature_sizes",
    "target_path", "sources_for", "file_entries",
]
