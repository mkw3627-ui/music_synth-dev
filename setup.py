"""First-run dependency & model bootstrap for 为她而唱 (DDSP-SVC 6.3).

Everything here is idempotent and resumable:

* venvs are created only when missing, and ``pip install`` reuses its cache;
* DDSP-SVC pretrain weights are downloaded as zips/release assets (no git);
* separation weights use HTTP Range requests into a ``.part`` file.

The module never imports the host SDK and uses only the stdlib plus the
compute Python it is bootstrapping.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import runtime

TORCH_CU128_INDEX = "https://download.pytorch.org/whl/cu128"
TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"

# 国内加速：GitHub 直连在不少宽带上只有几十 KB/s，先走镜像，镜像全挂再回落官方。
GH_MIRRORS = ("https://gh-proxy.com/", "https://ghfast.top/")
GH_HOSTS = {"github.com", "codeload.github.com", "objects.githubusercontent.com",
            "raw.githubusercontent.com"}
# PyPI 走清华镜像；镜像连不上时 _pip 会自动退回官方源重试一次。
PYPI_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

# DDSP-SVC 6.3 repo + pretrain assets (all release assets / codeload zips, so
# no git binary is required on the target machine).
DDSP_ZIP = ("https://codeload.github.com/yxlllc/DDSP-SVC/zip/"
            "b9d4e6a58182448aa7259a636570d53c49c92b1d")
# ContentVec encoder (contentvec768l12tta2x) — direct HF resolve link.
# ContentVec encoder (contentvec768l12tta2x). 国内优先镜像，失败再试官方 HF。
CONTENTVEC_URLS = [
    "https://hf-mirror.com/lengyue233/content-vec-best/resolve/main/pytorch_model.bin",
    "https://huggingface.co/lengyue233/content-vec-best/resolve/main/pytorch_model.bin",
]
CONTENTVEC_URL = CONTENTVEC_URLS[0]
# NSF-HiFiGAN vocoder (pc_nsf_hifigan 44.1k hop512 128bin 2025.02).
VOCODER_ZIP = ("https://github.com/openvpi/vocoders/releases/download/"
               "pc-nsf-hifigan-44.1k-hop512-128bin-2025.02/"
               "pc_nsf_hifigan_44.1k_hop512_128bin_2025.02.zip")
# RMVPE f0 extractor (230917 release).
RMVPE_ZIP = "https://github.com/yxlllc/RMVPE/releases/download/230917/rmvpe.zip"
# DDSP-SVC 6.3 训练底模（RectifiedFlow pretrain）。上游只有 SVCFusion 的
# .sf_dlc 包（本质是 torch zip，里面 files.model_0.pt 才是标准检查点），
# 安装时下载后转成 model_0.pt。国内优先 hf-mirror，失败再试 huggingface。
REFLOW_BASE_URLS = [
    ("https://hf-mirror.com/SVCFusion/DDSP6.3-Pretrain/resolve/main/"
     "ddsp6.3_6x512_6x1024.sf_dlc"),
    ("https://huggingface.co/SVCFusion/DDSP6.3-Pretrain/resolve/main/"
     "ddsp6.3_6x512_6x1024.sf_dlc"),
]
# 翻唱训练的低/高两档底模（同一个官方仓库的不同结构变体）。
LOW_BASE_URLS = [
    ("https://hf-mirror.com/SVCFusion/DDSP6.3-Pretrain/resolve/main/"
     "ddsp6.3_6x512_6x512.sf_dlc"),
    ("https://huggingface.co/SVCFusion/DDSP6.3-Pretrain/resolve/main/"
     "ddsp6.3_6x512_6x512.sf_dlc"),
]
HIGH_BASE_URLS = [
    ("https://hf-mirror.com/SVCFusion/DDSP6.3-Pretrain/resolve/main/"
     "ddsp6.3_6x512_10x2048.sf_dlc"),
    ("https://huggingface.co/SVCFusion/DDSP6.3-Pretrain/resolve/main/"
     "ddsp6.3_6x512_10x2048.sf_dlc"),
]
# Fallback only if the GitHub API is unreachable.
PYTHON_STANDALONE_PINNED = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/"
    "20250612/cpython-3.10.18+20250612-x86_64-pc-windows-msvc-shared-install_only.tar.gz"
)

MODEL_ITEMS = {
    "ddsp_contentvec": {
        "label": "DDSP-SVC ContentVec 编码器（contentvec768l12tta2x）",
        "kind": "http",
        "url": CONTENTVEC_URL,
        "urls": CONTENTVEC_URLS,
        "target": "ddsp_pretrain_contentvec",
        "subdir": "",
        "size_gb": 0.40,
        "files": ("pytorch_model.bin",),
    },
    "ddsp_vocoder": {
        "label": "DDSP-SVC NSF-HiFiGAN 声码器（pc_nsf_hifigan 44.1k）",
        "kind": "zip",
        "url": VOCODER_ZIP,
        "target": "ddsp_pretrain_vocoder",
        "zip_mode": "flatten",
        "rename": {"model.ckpt": "model"},
        "size_gb": 0.05,
        "files": ("model", "config.json"),
    },
    "ddsp_rmvpe": {
        "label": "DDSP-SVC RMVPE 音高提取器（230917）",
        "kind": "zip",
        "url": RMVPE_ZIP,
        "target": "ddsp_pretrain_rmvpe",
        "zip_mode": "into_base",
        "size_gb": 0.06,
        "files": ("model.pt",),
    },
    "ddsp_reflow_base": {
        "label": "训练底模（DDSP-SVC 6.3 PT）",
        "kind": "dlc",
        "url": REFLOW_BASE_URLS[0],
        "urls": REFLOW_BASE_URLS,
        "target": "ddsp_pretrain_reflow",
        "size_gb": 0.21,
        "files": ("model_0.pt",),
    },
    "ddsp_base_low": {
        "label": "低质量训练底模（6x512）",
        "kind": "dlc",
        "url": LOW_BASE_URLS[0],
        "urls": LOW_BASE_URLS,
        "target": "ddsp_base_low",
        "size_gb": 0.09,
        "files": ("low.pt",),
        # 质量档位，不装也能训练（自动回退到中档），所以不是必需项。
        "optional": True,
        # 同一个 bases 目录里直接放官方 .sf_dlc 包也算就绪（见 _find_tier_base）。
        "tier": "low",
    },
    "ddsp_base_high": {
        "label": "高质量训练底模（10x2048）",
        "kind": "dlc",
        "url": HIGH_BASE_URLS[0],
        "urls": HIGH_BASE_URLS,
        "target": "ddsp_base_high",
        "size_gb": 0.97,
        "files": ("high.pt",),
        # 质量档位，不装也能训练（自动回退到中档），所以不是必需项。
        "optional": True,
        "tier": "high",
    },
    "sep_karaoke": {
        "label": "人声分离权重（Mel-Band RoFormer Karaoke）",
        "kind": "sep",
        "target": "sep_model_dir",
        "size_gb": 1.75,
        "files": (
            "mel_band_roformer_karaoke_becruily.ckpt",
            "config_mel_band_roformer_karaoke_becruily.yaml",
        ),
    },
}

# DDSP-SVC 6.3 requirements.txt (torch is installed separately with the CUDA
# index so we never swap a working cu128 build for a CPU wheel).
DDSP_PACKAGES = [
    "librosa", "matplotlib", "numpy==1.26.4", "praat-parselmouth", "pyworld",
    "PyYAML", "resampy", "scikit_learn", "scipy", "sounddevice", "soundfile",
    "tensorboard", "torchcrepe", "torchfcpe", "tqdm", "transformers",
]

SEP_PACKAGES = [
    "audio-separator>=0.48", "soundfile", "librosa", "psutil", "pyyaml",
]

# 原生扩展：市场禁止插件包带二进制，只能安装时装进合成环境。
# proc-tap 提供逐应用 WASAPI 采集，winrt 提供系统媒体信息（SMTC）。
CAPTURE_NATIVE_PACKAGES = [
    "proc-tap==1.1.1", "winrt-Windows.Media.Control",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _say(message: str) -> None:
    print(f"[setup] {message}", flush=True)


def _free_gib(path: Path) -> float | None:
    """Free space on ``path``'s volume; ``None`` when it can't be measured."""
    try:
        probe = Path(path)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        return shutil.disk_usage(str(probe)).free / 2**30
    except OSError:
        return None

def _run_stream(cmd: list[str], progress, label: str, env: dict | None = None,
                cwd: Path | None = None, timeout: float = 7200.0) -> tuple[int, str]:
    """Run a command, streaming its last line into ``progress``."""
    _say(f"{label}: {' '.join(str(c) for c in cmd[:4])} …")
    proc = subprocess.Popen(
        [str(c) for c in cmd], cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        env=_proxy_free_env(env or runtime._clean_env()),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    tail: list[str] = []
    started = time.time()
    state = {"killed": False}

    def _watchdog() -> None:
        # 只在「一行输出都没有」时也能触发超时：原来只在读到行时才检查时间。
        while proc.poll() is None:
            if time.time() - started > timeout:
                state["killed"] = True
                try:
                    proc.kill()
                except OSError:
                    pass
                return
            time.sleep(5)

    threading.Thread(target=_watchdog, name="ms-run-timeout", daemon=True).start()
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        tail.append(line)
        tail = tail[-40:]
        if progress:
            progress(f"{label}：{line[-160:]}")
    code = proc.wait()
    if state["killed"]:
        return -1, "\n".join(tail + ["（超时被终止）"])
    return code, "\n".join(tail)


def _dead_proxy_text(text: str) -> bool:
    """pip 输出的错误里有没有「代理拒绝连接」。"""
    t = str(text or "").lower()
    return "10061" in t or "connection refused" in t


def _pip(python: Path, args: list[str], progress, label: str) -> None:
    base = [str(python), "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "--retries", "5", "--timeout", "60"]
    has_index = any(str(a).startswith("--index-url") or str(a).startswith("-i") for a in args)
    # 默认走清华 PyPI 镜像；镜像不通就退回官方源再试一次，避免安装卡死在网络上。
    cmd = base + ([] if has_index else ["--index-url", PYPI_INDEX]) + list(args)
    code, tail = _run_stream(cmd, progress, label)
    if code != 0 and not has_index:
        _say(f"{label}: 清华镜像失败，改用官方 PyPI 重试")
        code, tail = _run_stream(base + list(args), progress, f"{label}（官方源）")
    if code != 0 and _dead_proxy_text(tail):
        # 代理在插件启动后退出：缓存里仍是「代理可用」，子进程环境还带着死代理。
        # 重新探测一次，确认代理真的死了就用干净环境再试一回。
        _reset_proxy_state()
        if not runtime.proxy_alive():
            _say(f"{label}: 代理已失效，去掉代理变量重试")
            retry = base + list(args) if has_index else cmd
            code, tail = _run_stream(retry, progress, f"{label}（直连）")
    if code != 0:
        raise RuntimeError(f"{label} 失败（退出码 {code}）：{tail[-1500:]}")



# ---------------------------------------------------------------------------
# Base Python
# ---------------------------------------------------------------------------
def _python_ok(exe: str | Path) -> tuple[bool, str]:
    exe = str(exe)
    if not Path(exe).is_file():
        return False, "missing"
    try:
        out = subprocess.run([exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=15)
        version = (out.stdout or "").strip()
        major, _, minor = version.partition(".")
        if major.isdigit() and minor.isdigit() and (int(major), int(minor)) in {(3, 10), (3, 11), (3, 12)}:
            return True, version
        return False, version or "unknown"
    except (OSError, subprocess.SubprocessError):
        return False, "missing"


def find_base_python(paths: dict | None = None) -> tuple[str | None, str, str]:
    """Return (executable, version, source), preferring stable 3.10/3.11."""
    paths = paths or {}
    configured = paths.get("base_python")
    if configured and Path(configured).is_file():
        ok, version = _python_ok(configured)
        if ok:
            return str(configured), version, "用户配置"
    for minor in ("10", "11", "12"):
        try:
            out = subprocess.run(["py", f"-3.{minor}", "-c", "import sys;print(sys.executable)"],
                                 capture_output=True, text=True, timeout=15,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            exe = (out.stdout or "").strip()
            if exe and Path(exe).is_file():
                ok, version = _python_ok(exe)
                if ok:
                    return exe, version, f"py -3.{minor}"
        except (OSError, subprocess.SubprocessError):
            pass
    for name in ("python", "python3", "python.exe"):
        exe = shutil.which(name)
        if exe:
            ok, version = _python_ok(exe)
            if ok:
                return exe, version, "PATH"
    for minor in ("310", "311", "312"):
        candidate = Path.home() / "AppData" / "Local" / "Programs" / "Python" / f"Python{minor}" / "python.exe"
        if candidate.is_file():
            ok, version = _python_ok(candidate)
            if ok:
                return str(candidate), version, "本机 Python 安装"
    return None, "", "not_found"


_OPENER: list = [None]


def proxy_alive() -> bool:
    """代理是否真的连得上（实现放在 runtime，worker / ACE 子进程共用同一判断）。"""
    alive = runtime.proxy_alive()
    if not alive and not _OPENER[0]:
        _say("代理不可用（拒绝连接），下载改走直连")
    return alive


def _opener():
    """urllib opener：代理挂了就返回强制直连的 opener。"""
    if _OPENER[0] is None:
        handlers = []
        if not proxy_alive():
            handlers.append(urllib.request.ProxyHandler({}))
        _OPENER[0] = urllib.request.build_opener(*handlers)
    return _OPENER[0]


def _proxy_free_env(env: dict) -> dict:
    """子进程（pip / venv）用的环境：代理不可用时把代理变量摘掉。"""
    return runtime.without_dead_proxy(env)


def _dead_proxy_detected(exc: BaseException) -> bool:
    """异常是不是「代理端口拒绝连接」（10061 / Connection refused）。"""
    if isinstance(exc, RuntimeError):
        return False
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ConnectionRefusedError):
            return True
        if int(getattr(cur, "winerror", 0) or 0) == 10061:
            return True
        text = str(cur).lower()
        if "10061" in text or "connection refused" in text:
            return True
        nxt = getattr(cur, "reason", None)
        cur = nxt if isinstance(nxt, BaseException) else (cur.__cause__ or cur.__context__)
    return False


def _reset_proxy_state(force_check: bool = True) -> None:
    """丢掉代理探测/opener 缓存，让下一次请求按当前真实状态走。"""
    try:
        runtime.proxy_alive(force=force_check)
    except TypeError:  # 老 runtime 没 force 参数时直接清缓存
        try:
            runtime._PROXY_ALIVE[0] = None  # noqa: SLF001
        except (AttributeError, IndexError):
            pass
    _OPENER[0] = None


def _mirror_urls(url: str) -> list[str]:
    """GitHub 地址 → 国内加速镜像 + 原地址，按顺序尝试。"""
    out: list[str] = []
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host in GH_HOSTS:
        out.extend(m + url for m in GH_MIRRORS)
    out.append(url)
    return out


_MODEL_SUFFIXES = {".bin", ".ckpt", ".pt", ".pth", ".safetensors", ".onnx", ".gguf"}


def _write_marker(target: Path, size: int) -> None:
    """Record the size of a *fully* downloaded file (``<name>.ms_ok``).

    Later readiness checks compare against it, so a file that was cut short by
    an interrupted download can't keep passing as "ready" forever.
    """
    try:
        target.with_name(target.name + ".ms_ok").write_text(
            json.dumps({"bytes": int(size), "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _legacy_min_bytes(spec: dict, name: str) -> int:
    """Plausibility floor for a model file that predates the ``.ms_ok`` marker."""
    if Path(name).suffix.lower() not in _MODEL_SUFFIXES:
        # config.json / *.yaml 本来就只有几百字节，存在且非空即可。
        return 1
    floor = 8 * 1024 * 1024
    try:
        floor = max(floor, int(float(spec.get("size_gb") or 0) * 1024 ** 3 * 0.25))
    except (TypeError, ValueError):
        pass
    return floor


def _file_ready(spec: dict, path: Path) -> bool:
    """Is this downloaded model file complete enough to use?"""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
    except OSError:
        return False
    marker = path.with_name(path.name + ".ms_ok")
    if marker.is_file():
        try:
            want = int((json.loads(marker.read_text(encoding="utf-8")) or {}).get("bytes") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            want = 0
        if want > 0:
            return size >= want
    return size >= _legacy_min_bytes(spec, path.name)


def _download_file(url: str, target: Path, progress, label: str,
                   expected_bytes: int = 0, timeout: float = 120.0) -> None:
    """Resumable download with mirrors, dead-proxy bypass and slow-source failover."""
    errors: list[str] = []
    proxy_retried = False
    for candidate in _mirror_urls(url):
        while True:
            try:
                _download_one(candidate, target, progress, label, expected_bytes, timeout)
                if candidate != url:
                    _say(f"{label}: 已通过国内镜像 {urllib.parse.urlsplit(candidate).hostname} 下载")
                return
            except (OSError, urllib.error.URLError, urllib.error.HTTPError,
                    http.client.HTTPException, RuntimeError) as exc:
                errors.append(f"{urllib.parse.urlsplit(candidate).hostname}: {exc}")
                _say(f"{label}: {errors[-1][:160]}，换源重试")
                if not proxy_retried and _dead_proxy_detected(exc):
                    # 插件启动时代理还活着、之后被关掉：清缓存直连，同一个源再试一次。
                    proxy_retried = True
                    _reset_proxy_state()
                    if not runtime.proxy_alive():
                        _say(f"{label}: 代理已失效，改走直连重试")
                        continue
                break
    raise RuntimeError(f"{label} 下载失败：" + "; ".join(errors)[-800:])


def _download_one(url: str, target: Path, progress, label: str,
                  expected_bytes: int, timeout: float) -> None:
    """One source: Range-resume into ``target.part`` then rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    pos = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "NEKO-music-synth/0.2"}
    if pos:
        headers["Range"] = f"bytes={pos}-"
    req = urllib.request.Request(url, headers=headers)
    started = time.time()
    try:
        resp = _opener().open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and part.exists():
            size = part.stat().st_size
            os.replace(part, target)  # 已经下完了
            _write_marker(target, size)
            return
        raise
    with resp:
        status = getattr(resp, "status", 200)
        if status == 200 and pos:
            pos = 0  # server ignored Range: restart cleanly
            part.unlink(missing_ok=True)
        total = int(resp.headers.get("Content-Length") or 0) + pos
        mode = "ab" if pos else "wb"
        done = pos
        base = pos
        with part.open(mode) as fh:
            while True:
                # 连接卡死时 read() 会阻塞到 socket 超时，先按「20 秒没进度」判死。
                if time.time() - started > 20 and done - base < 512 * 1024:
                    raise RuntimeError(f"速度过慢（20 秒 {done - base} 字节）")
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(f"{label}：{done / 2**20:.0f}/{total / 2**20:.0f} MB")
                elif progress and expected_bytes:
                    progress(f"{label}：{done / 2**30:.2f}/{expected_bytes / 2**30:.2f} GB")
                # 20 秒还不到 512KB 的源直接判死，换下一个；.part 保留给后面的源续传。
                if time.time() - started > 20 and done - base < 512 * 1024:
                    raise RuntimeError(f"速度过慢（20 秒 {done - base} 字节）")
    # 连接被掐断时 http.client 的 read(amt) 只是返回空串、不抛 IncompleteRead，
    # 所以必须拿 Content-Length 对账；否则半个文件会被 os.replace 成「成品」。
    size = part.stat().st_size
    if total and done < total:
        raise RuntimeError(f"{label} 下载不完整：{done} / {total} 字节")
    if total and size < total:
        raise RuntimeError(f"{label} 下载不完整：{size} / {total} 字节")
    if expected_bytes and size < expected_bytes * 0.9:
        raise RuntimeError(f"{label} 下载不完整：{size} / {expected_bytes} 字节")
    os.replace(part, target)
    _write_marker(target, size)



def download_portable_python(paths: dict, progress) -> str:
    """Fetch python-build-standalone 3.10 when the machine has no usable Python."""
    root = Path(paths["compute_root"]) / "runtime" / "python"
    exe = root / "python.exe"
    if exe.is_file():
        return str(exe)
    url = ""
    try:
        api = urllib.request.Request(
            "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest",
            headers={"User-Agent": "NEKO-music-synth/0.2", "Accept": "application/vnd.github+json"})
        with _opener().open(api, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        pattern = re.compile(r"cpython-3\.10\.\d+\+.*x86_64-pc-windows-msvc-shared-install_only\.tar\.gz$")
        for asset in data.get("assets") or []:
            if pattern.search(asset.get("name", "")):
                url = asset.get("browser_download_url", "")
                break
    except (OSError, ValueError, urllib.error.URLError):
        url = ""
    if not url:
        url = PYTHON_STANDALONE_PINNED
    archive = Path(paths["compute_root"]) / "runtime" / "python-standalone.tar.gz"
    progress("下载便携版 Python 3.10（约 25MB）…")
    _download_file(url, archive, progress, "Python 3.10", expected_bytes=0)
    with tempfile.TemporaryDirectory(dir=str(archive.parent)) as tmp:
        tmp_path = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp_path)  # noqa: S202 - official standalone archive
        source = tmp_path / "python"
        if not source.is_dir():
            raise RuntimeError("便携版 Python 压缩包结构异常")
        if root.exists():
            shutil.rmtree(root, ignore_errors=True)
        shutil.move(str(source), str(root))
    archive.unlink(missing_ok=True)
    if not exe.is_file():
        raise RuntimeError("便携版 Python 解压后缺少 python.exe")
    return str(exe)


# ---------------------------------------------------------------------------
# venv / repo / models
# ---------------------------------------------------------------------------
def _venv_ok(python: Path) -> bool:
    """venv 里的 python 能不能用（建到一半的 venv 常常连 pip 都没有）。"""
    try:
        out = subprocess.run([str(python), "-c", "import pip,sys;print('ok')"],
                             capture_output=True, text=True, timeout=60,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return out.returncode == 0 and "ok" in (out.stdout or "")
    except (OSError, subprocess.SubprocessError):
        return False


def _make_venv(base_python: str, venv_dir: Path, progress, label: str) -> Path:
    python = venv_dir / "Scripts" / "python.exe"
    if python.is_file() and _venv_ok(python):
        return python
    if venv_dir.exists():
        # 半成品 venv 不删，挪到旁边留证；否则 venv 命令会因目录非空直接失败。
        bak = venv_dir.with_name(f"{venv_dir.name}.broken-{time.strftime('%Y%m%d_%H%M%S')}")
        progress(f"{label} 虚拟环境不完整，移到 {bak.name} 后重建…")
        try:
            shutil.move(str(venv_dir), str(bak))
        except OSError as exc:
            raise RuntimeError(
                f"{label} 虚拟环境不完整且无法移开（{exc}）；关掉占用它的程序后重试") from exc
    progress(f"创建 {label} 虚拟环境…")
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    code, tail = _run_stream([base_python, "-m", "venv", str(venv_dir)], progress, f"创建 {label}")
    if code != 0 or not python.is_file():
        raise RuntimeError(f"创建 {label} 虚拟环境失败：{tail[-1200:]}")
    _pip(python, ["--upgrade", "pip", "setuptools", "wheel"], progress, f"{label} 基础工具")
    return python


_DDSP_PROBE_MODULES = ("librosa", "pyworld", "parselmouth", "torchcrepe", "transformers")
_DEPS_CACHE: dict[tuple[str, int], tuple[bool, str]] = {}


def ddsp_deps_status(python) -> tuple[bool, str]:
    """Import DDSP-SVC's key deps inside the synth venv.

    ``check_dependencies`` used to verify only ``python.exe`` and ``torch``, so a
    half-finished ``pip install DDSP_PACKAGES`` still reported "ready" and gave
    the user a cryptic ModuleNotFoundError on the first song.
    """
    p = Path(str(python))
    if not p.is_file():
        return False, "缺少 Python"
    try:
        key = (str(p), p.stat().st_mtime_ns)
    except OSError:
        key = (str(p), 0)
    if key in _DEPS_CACHE:
        return _DEPS_CACHE[key]
    code = (
        "import importlib,json,sys\n"
        "miss=[]\n"
        "for m in json.loads(sys.argv[1]):\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except Exception:\n"
        "        miss.append(m)\n"
        "print('MISS='+json.dumps(miss))\n"
    )
    try:
        proc = subprocess.run(
            [str(p), "-c", code, json.dumps(list(_DDSP_PROBE_MODULES))],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        res = (False, f"检测失败：{type(exc).__name__}: {exc}")
        _DEPS_CACHE[key] = res
        return res
    if proc.returncode != 0:
        tail = ((proc.stderr or proc.stdout or "")[-400:]).strip().replace("\n", " ")
        res = (False, f"检测失败：{tail or '退出码 ' + str(proc.returncode)}")
    else:
        miss: list = []
        for line in reversed((proc.stdout or "").splitlines()):
            if line.startswith("MISS="):
                try:
                    miss = json.loads(line[5:])
                except ValueError:
                    miss = ["?"]
                break
        res = (not miss, "完整" if not miss else "缺少：" + "、".join(str(m) for m in miss))
    _DEPS_CACHE[key] = res
    return res


def invalidate_deps_cache(python) -> None:
    target = str(python)
    for k in list(_DEPS_CACHE):
        if k[0] == target:
            _DEPS_CACHE.pop(k, None)


def _torch_index(gpu: dict | None) -> str:
    if not gpu:
        return TORCH_CPU_INDEX
    return TORCH_CU128_INDEX if int(gpu.get("cc_major") or 0) >= 7 else TORCH_CPU_INDEX


def _ensure_repo(paths: dict, progress) -> None:
    repo = Path(paths["ddsp_root"])
    if (repo / "main_reflow.py").is_file():
        return
    _ensure_zip_repo(DDSP_ZIP, repo, progress, "DDSP-SVC", marker="main_reflow.py")


def _ensure_zip_repo(url: str, repo: Path, progress, label: str, marker: str = "") -> None:
    marker_path = repo / marker if marker else repo
    if marker_path.exists():
        return
    progress(f"下载 {label} 仓库…")
    archive = repo.parent / f"_{label.replace(' ', '_')}.zip"
    repo.parent.mkdir(parents=True, exist_ok=True)
    _download_file(url, archive, progress, label)
    with tempfile.TemporaryDirectory(dir=str(repo.parent)) as tmp:
        _safe_extract(archive, Path(tmp))
        root = next((p for p in Path(tmp).iterdir() if p.is_dir()), None)
        if root is None:
            raise RuntimeError(f"{label} 仓库压缩包结构异常")
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        shutil.move(str(root), str(repo))
    archive.unlink(missing_ok=True)


def _safe_extract(archive: Path, target: Path) -> None:
    """Extract an official zip, rejecting path traversal entries."""
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise RuntimeError(f"压缩包包含非法路径：{member.filename}")
        zf.extractall(target)  # noqa: S202 - validated above


def _model_base(spec: dict, target: Path) -> Path:
    """Folder that receives the model; ``target`` may be the model file itself.

    ContentVec points at ``...\contentvec\pytorch_model.bin`` and RMVPE at
    ``...\rmvpe\model.pt``; the folder is then ``target.parent``. A target
    without a file suffix (...\vocoder) is already the folder.
    """
    return target if not target.suffix else target.parent


def _model_zip_dir(spec: dict, target: Path) -> Path:
    """Folder a model zip unpacks into (the single top folder inside the zip).

    The zip top folder is named after the model file the target names
    (rmvpe / nsf_hifigan). When the target already is the model folder
    (pretrain\vocoder) the files go straight in; rmvpe unpacks as
    pretrain\rmvpe\rmvpe\model.pt next to the configured model.pt.
    """
    base = _model_base(spec, target)
    return base if base == target else base / target.name


def _missing_files(spec: dict, target: Path) -> list[str]:
    """Missing entries for a model item. A pattern that is a plain relative path
    (no wildcard) is checked exactly, so a sibling file such as
    ``model.raw_sd.pt`` or ``config.json.bak`` never masks a really-missing
    ``model`` / ``config.json``.
    Anything containing ``*`` / ``?`` / ``[`` keeps the recursive match.
    Paths with a suffix must resolve to a *file* (a same-named directory from a
    bad zip extraction must not count as ready)."""
    missing = []
    for rel in spec.get("files", ()):
        try:
            if any(ch in rel for ch in "*?["):
                found = any(target.rglob(Path(rel).name))
            else:
                need_file = bool(Path(rel).suffix)
                found = False
                for cand in (target / rel, target / Path(rel).name):
                    if not cand.exists() or not (cand.is_file() or not need_file):
                        continue
                    if need_file and not _file_ready(spec, cand):
                        continue
                    found = True
                    break
        except OSError:
            found = False
        if not found:
            missing.append(rel)
    return missing


# 低/高两档底模有两种落盘方式：安装清单转好的 low.pt / high.pt，或者用户
# 直接放进来的官方 .sf_dlc 包。后者是官方发布格式，训练侧本来就会用
# （见 _train_base_ckpt 的 bases 回退），检测不能只看 low.pt / high.pt。
SF_DLC_TIER_ARCH = {"low": (6, 512), "high": (10, 2048)}
_SF_DLC_CACHE: dict[tuple, dict] = {}
_SF_DLC_CACHE_LOCK = threading.Lock()


def _tier_of_arch(n_layers, n_chans) -> str:
    """把结构参数归到质量档位；不是官方档位就返回空串。"""
    try:
        layers, chans = int(n_layers or 0), int(n_chans or 0)
    except (TypeError, ValueError):
        return ""
    for tier, (want_layers, want_chans) in SF_DLC_TIER_ARCH.items():
        if layers == want_layers and chans == want_chans:
            return tier
    return ""


def _tier_of_name(path: Path) -> str:
    """官方命名 ddsp6.3_6x512_<层>x<通道>.sf_dlc 的兜底判断（解析器不可用时）。"""
    match = re.search(r"_(\d+)x(\d+)$", Path(path).stem)
    if not match:
        return ""
    return _tier_of_arch(match.group(1), match.group(2))


def _inspect_sf_dlc(path: Path, python: Path, cwd: Path) -> dict:
    """用 DDSP 环境自己的底模解析器读 .sf_dlc 的网络结构。

    返回 {"ok": True, "n_layers": int, "n_chans": int} 或 {"ok": False, "error": str}；
    解析器缺失 / 报错 / 超时都只当作「没读到」，由调用者退回文件名判断。
    """
    script = Path(__file__).resolve().parent / "workers" / "base_ckpt.py"
    if not Path(python).is_file() or not script.is_file():
        return {"ok": False, "error": "没有可用的 DDSP 环境或底模解析器"}
    try:
        info = runtime.run_worker(Path(python), script, {"path": str(path)},
                                  cwd=cwd, timeout=180)
    except Exception as exc:  # noqa: BLE001 - 检测失败不是致命错误
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    arch = info.get("arch") if isinstance(info, dict) else None
    if not (isinstance(info, dict) and info.get("ok")
            and info.get("supported") and isinstance(arch, dict)):
        return {"ok": False, "error": str(
            (info or {}).get("error") or "底模解析器没读出结构")}
    layers, chans = arch.get("n_layers"), arch.get("n_chans")
    if not layers or not chans:
        return {"ok": False, "error": "底模里没有网络结构信息"}
    return {"ok": True, "n_layers": int(layers), "n_chans": int(chans)}


def _sf_dlc_arch(path: Path, python: Path, cwd: Path) -> dict:
    """带缓存的结构探测：同一文件（大小+mtime 不变）只解析一次。"""
    try:
        stat = path.stat()
    except OSError as exc:
        return {"ok": False, "error": f"读不到文件：{exc}"}
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _SF_DLC_CACHE_LOCK:
        hit = _SF_DLC_CACHE.get(key)
    if hit is not None:
        return hit
    info = _inspect_sf_dlc(path, python, cwd)
    if info.get("ok"):
        with _SF_DLC_CACHE_LOCK:
            _SF_DLC_CACHE[key] = info
            while len(_SF_DLC_CACHE) > 32:
                _SF_DLC_CACHE.pop(next(iter(_SF_DLC_CACHE)))
    return info


def _find_tier_base(paths: dict, spec: dict, target: Path) -> tuple[Path | None, str, str]:
    """在 bases 目录里找与该档位匹配的官方 .sf_dlc。

    返回 (路径, 判定说明, 判定方式)；判定方式为 ``arch``（读结构）或
    ``filename``（文件名兜底）。档位优先按底模解析器读出来的结构（层数/
    通道数）判定；解析器不可用时才退回官方文件名。结构读出来但不符档位的，
    不会因为文件名像就放行。
    """
    tier = str(spec.get("tier") or "")
    if tier not in SF_DLC_TIER_ARCH:
        return None, "", ""
    bases = target.parent
    try:
        candidates = sorted(p for p in bases.glob("*.sf_dlc") if p.is_file())
    except OSError:
        return None, "", ""
    python = Path(str(paths.get("synth_python") or ""))
    cwd = Path(tempfile.gettempdir()) / "neko_music_synth_base_scan"
    for cand in candidates:
        info = _sf_dlc_arch(cand, python, cwd)
        if info.get("ok"):
            if _tier_of_arch(info.get("n_layers"), info.get("n_chans")) == tier:
                return (cand, f"结构 {info['n_layers']} 层 / {info['n_chans']} 通道",
                        "arch")
            continue
        if _tier_of_name(cand) == tier:
            return cand, "官方文件名档位", "filename"
    return None, "", ""


def resolve_tier_base(paths: dict, tier: str, target: Path | None = None) -> Path | None:
    """给训练侧用：bases 下对应这个质量档位的官方 .sf_dlc（没有就 None）。

    和首次检测共用同一套判定，避免「检测说已就绪、训练却回退中档」。
    """
    tier = str(tier or "").strip().lower()
    if tier not in SF_DLC_TIER_ARCH:
        return None
    if target is None:
        root = Path(str(paths.get("ddsp_root") or ""))
        target = root / "pretrain" / "bases" / f"{tier}.pt"
    found, _how, _method = _find_tier_base(paths, {"tier": tier}, Path(target))
    return found


def _install_zip(spec: dict, dest_root: Path, progress, label: str) -> None:
    """Download a release zip and place its contents under ``dest_root``.

    ``zip_mode``:
      * ``nest`` (default): a single top folder becomes ``dest_root/<top>``.
      * ``flatten``: a single top folder's *contents* go directly into
        ``dest_root`` (vocoder zip).
      * ``into_base``: loose files / a top folder are flattened into
        ``dest_root`` itself (rmvpe zip, where dest_root is ``pretrain/rmvpe``).
    ``rename`` renames extracted entries after placement (model.ckpt -> model).
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    # 用 url 的摘要而不是 hash()：Python 字符串 hash 每进程随机化，重启插件后
    # 文件名会变，上次的 .part 就续不上、还留在盘上变成垃圾。
    tag = hashlib.md5(str(spec["url"]).encode("utf-8")).hexdigest()[:8]
    archive = dest_root.parent / f"_{dest_root.name}_{tag}.zip"
    _download_file(spec["url"], archive, progress, label)
    mode = str(spec.get("zip_mode") or "nest")
    with tempfile.TemporaryDirectory(dir=str(dest_root.parent)) as tmp:
        _safe_extract(archive, Path(tmp))
        entries = list(Path(tmp).iterdir())
        folders = [p for p in entries if p.is_dir()]
        files = [p for p in entries if p.is_file()]

        if mode in ("flatten", "into_base"):
            src_root = folders[0] if len(folders) == 1 and not files else Path(tmp)
            for entry in list(src_root.iterdir()):
                dest = dest_root / entry.name
                if dest.is_dir():
                    shutil.rmtree(dest, ignore_errors=True)
                elif dest.exists():
                    dest.unlink()
                shutil.move(str(entry), str(dest))
        else:
            moves: list[tuple[Path, Path]] = []
            if len(folders) == 1 and not files:
                top = folders[0]
                if dest_root.name == top.name:
                    moves = [(top, dest_root)]
                else:
                    moves = [(top, dest_root / top.name)]
            else:
                moves = [(entry, dest_root / entry.name) for entry in entries]
            for src, dest in moves:
                if dest.is_dir():
                    if dest == dest_root or dest.parent == dest_root:
                        shutil.rmtree(dest, ignore_errors=True)
                    else:
                        raise RuntimeError("目标位置已存在同名目录：" + str(dest))
                elif dest.exists():
                    dest.unlink()
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))

        for old, new in (spec.get("rename") or {}).items():
            src, dest = dest_root / old, dest_root / new
            if src.is_file() and not dest.exists():
                src.rename(dest)
    archive.unlink(missing_ok=True)


def _download_http_model(paths: dict, spec: dict, progress, label: str) -> None:
    """Download a single-file HTTP model (e.g. ContentVec) with resume support."""
    target_dir = _model_base(spec, Path(paths[spec["target"]]))
    target_dir.mkdir(parents=True, exist_ok=True)
    urls = spec.get("urls") or [spec["url"]]
    for filename in spec["files"]:
        target = target_dir / filename
        if _file_ready(spec, target):
            continue
        errors = []
        for url in urls:
            try:
                _download_file(url, target, progress, label, timeout=40)
                break
            except (OSError, urllib.error.URLError, RuntimeError) as exc:
                errors.append(f"{url}: {exc}")
        else:
            raise RuntimeError(f"{filename} 下载失败：" + "; ".join(errors)[-1200:])


def _install_dlc_model(paths: dict, spec: dict, progress, label: str, python) -> None:
    """Download a SVCFusion .sf_dlc package and convert it to model_0.pt.

    .sf_dlc is a torch zip whose ``files['model_0.pt']`` is the real
    checkpoint ({'global_step', 'model'}); DDSP-SVC needs the inner dict saved
    as a plain .pt.  Conversion runs in the freshly installed DDSP env.
    """
    target = Path(paths[spec["target"]])
    target.parent.mkdir(parents=True, exist_ok=True)
    dlc = target.with_name(target.name + ".dlc")
    errors = []
    urls = list(spec.get("urls") or [])
    if spec.get("url") and spec["url"] not in urls:
        urls.insert(0, spec["url"])
    if not urls:
        urls = list(REFLOW_BASE_URLS)
    for url in urls:
        try:
            _download_file(url, dlc, progress, label, timeout=600)
            break
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            errors.append(f"{url}: {exc}")
            try:
                dlc.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        raise RuntimeError("底模下载失败：" + "; ".join(errors)[-1200:])

    py = Path(str(python))
    if not py.is_file():
        raise RuntimeError("需要先安装 DDSP 环境才能转换底模")
    code = (
        "import sys,torch\n"
        "p,o=sys.argv[1],sys.argv[2]\n"
        "try:\n"
        "    c=torch.load(p,map_location='cpu',weights_only=False)\n"
        "except TypeError:\n"
        "    c=torch.load(p,map_location='cpu')\n"
        "inner=(c.get('files') or {}).get('model_0.pt') if isinstance(c,dict) else None\n"
        "assert inner is not None,'DLC 里没有 model_0.pt'\n"
        "torch.save(inner,o)\n"
    )
    try:
        proc = subprocess.run([str(py), "-c", code, str(dlc), str(target)],
                              capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("底模转换超时（15 分钟）：文件可能不完整，请重试") from exc
    if proc.returncode != 0:
        raise RuntimeError("底模转换失败：" + ((proc.stderr or proc.stdout or "")[-1000:]))
    try:
        dlc.unlink(missing_ok=True)
    except OSError:
        pass
    min_bytes = max(20 * 1024 * 1024,
                    int(float(spec.get("size_gb") or 0.2) * 1024 ** 3 * 0.5))
    if not target.is_file() or target.stat().st_size < min_bytes:
        raise RuntimeError("底模转换后文件异常")


def _sep_weight_urls(filename: str) -> list[str]:
    mirror = ("https://github.com/nomadkaraoke/python-audio-separator/releases/"
              f"download/model-configs/{filename}")
    if filename.endswith(".yaml"):
        upstream = ("https://github.com/TRvlvr/model_repo/releases/download/"
                    f"all_public_uvr_models/mdx_model_data/mdx_c_configs/{filename}")
    else:
        upstream = ("https://github.com/TRvlvr/model_repo/releases/download/"
                    f"all_public_uvr_models/{filename}")
    return [mirror, upstream]


def _download_sep_weights(paths: dict, progress, label: str) -> None:
    target_dir = Path(paths["sep_model_dir"])
    target_dir.mkdir(parents=True, exist_ok=True)
    for filename in MODEL_ITEMS["sep_karaoke"]["files"]:
        target = target_dir / filename
        if _file_ready(MODEL_ITEMS["sep_karaoke"], target):
            continue
        errors = []
        for url in _sep_weight_urls(filename):
            try:
                _download_file(url, target, progress, label, timeout=40)
                break
            except (OSError, urllib.error.URLError, RuntimeError) as exc:
                errors.append(f"{url}: {exc}")
        else:
            raise RuntimeError(f"{filename} 下载失败：" + "; ".join(errors)[-1200:])


REDOWNLOAD_KEYS = ("ddsp_contentvec", "sep_karaoke")


def stash_model_files(paths: dict, key: str) -> list[str]:
    """把某个模型的文件挪进 ``models_dir/_trash``，供面板「重新下载」使用。

    直接删文件太粗暴：挪走既让安装器看到「缺失」，又保留旧文件可人工找回。
    """
    spec = MODEL_ITEMS.get(str(key))
    if not spec or key not in REDOWNLOAD_KEYS:
        raise RuntimeError(f"不支持重下的模型：{key or '（空）'}")
    target = Path(paths[spec["target"]])
    base = _model_base(spec, target)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash = Path(paths["models_dir"]) / "_trash" / f"{stamp}_{key}"
    moved: list[str] = []
    for rel in spec.get("files", ()):
        for cand in (base / rel, base / Path(rel).name):
            if not cand.is_file():
                continue
            trash.mkdir(parents=True, exist_ok=True)
            dest = trash / cand.name
            n = 1
            while dest.exists():
                dest = trash / f"{cand.stem}.{n}{cand.suffix}"
                n += 1
            shutil.move(str(cand), str(dest))
            for side in (cand.with_name(cand.name + ".ms_ok"),
                         cand.with_name(cand.name + ".part")):
                try:
                    side.unlink(missing_ok=True)
                except OSError:
                    pass
            moved.append(str(dest))
            break
    if not moved:
        raise RuntimeError("没有找到可重下的文件")
    return moved


# ---------------------------------------------------------------------------
# Dependency report
# ---------------------------------------------------------------------------
def _item(key: str, label: str, status: str, detail: str = "", *,
          required: bool = True, installable: bool = False, fix: str = "",
          size_gb: float = 0.0, action: str = "") -> dict:
    return {
        "key": key, "label": label, "status": status, "detail": detail,
        "ok": status in {"ready", "optional", "na"},
        "required": required, "installable": installable,
        "fix": fix, "size_gb": round(size_gb, 2), "action": action,
    }


def check_dependencies(paths: dict, probe: dict | None = None) -> dict:
    probe = probe or runtime.probe_hardware(force=True)
    gpu = probe.get("gpu")
    items: list[dict] = []

    # 没选安装位置：不探测、不建议任何盘，必须先让用户填。
    if not paths.get("configured"):
        items.append(_item(
            "install_path", "安装位置", "missing", "还没有选择安装目录",
            required=True, installable=False,
            fix="在下面选择或输入一个目录（插件不会自动选盘，也不会下到别的盘）"))
        return {
            "ok": False, "ready": False, "needs_path": True,
            "items": items, "missing": ["install_path"],
            "optional_missing": [], "installable_missing": [], "actions": [],
            "total_missing_gb": 0, "required_missing_gb": 0, "optional_missing_gb": 0,
            "suggested_root": "", "default_root": "",
            "base_python": {"path": "", "version": "", "source": ""},
            "hardware": probe,
            "hardware_brief": runtime.probe_brief(probe),
            "paths": {k: str(v) for k, v in paths.items()
                      if isinstance(v, (str, Path)) and k not in {"raw", "host_section"}},
        }

    # host runtime ---------------------------------------------------------
    host_version = sys.version.split()[0]
    host_ok = tuple(int(x) for x in host_version.split(".")[:2]) >= (3, 9)
    items.append(_item("python_host", "宿主 Python", "ready" if host_ok else "mismatch",
                       host_version, required=True,
                       fix="" if host_ok else "需要 Python 3.9+ 运行插件"))

    ff = runtime.ffmpeg_status(str((paths.get("raw") or {}).get("ffmpeg_path") or ""))
    items.append(_item(
        "ffmpeg", "FFmpeg（可选，用于视频导入/试听）",
        "ready" if ff["available"] else "optional",
        ff.get("path") or "未找到；成品播放会自动回退 winsound/系统播放器",
        required=False, installable=not ff["available"],
        fix="可在计算环境安装 imageio-ffmpeg，或用 winget install Gyan.FFmpeg",
        size_gb=0.07, action="" if ff["available"] else "pip:synth:ffmpeg"))

    # GPU / driver ---------------------------------------------------------
    if gpu:
        cc = int(gpu.get("cc_major") or 0)
        # sm<70 只是不能用 CUDA 加速，CPU 路线完全可用：别把它当「缺必需项」，
        # 否则向导的 ready 永远为 False，用户装完 17GB 也过不了第 4 页。
        status = "ready" if cc >= 7 else "optional"
        items.append(_item(
            "cuda_driver", "NVIDIA 驱动 / CUDA",
            status,
            f"{gpu['name']} · sm_{gpu['compute_cap']} · 驱动 {gpu.get('driver') or '?'}"
            f" · 显存 {gpu['total_gib']}GB（空闲 {gpu['free_gib']}GB）",
            required=cc >= 7,
            fix="" if cc >= 7 else "显卡过旧，将自动走 CPU 路线（可继续使用，只是慢）"))
    else:
        items.append(_item("cuda_driver", "NVIDIA 驱动 / CUDA", "optional",
                           "未检测到 NVIDIA 显卡；全流程会自动走 CPU",
                           required=False,
                           fix="CPU 路线可用但很慢；如需 GPU 请安装 NVIDIA 驱动"))

    # base python ----------------------------------------------------------
    base_src = find_base_python(paths)
    base, base_version, base_source = (
        (str(base_src[0]), base_src[1], base_src[2]) if base_src else (None, "", ""))
    items.append(_item(
        "base_python", "基础 Python（建虚拟环境用）",
        "ready" if base else "missing",
        f"{base_version} · {base_source}" if base else "未找到可用的 Python 3.10/3.11/3.12",
        required=True, installable=not bool(base),
        fix="可自动下载便携版 Python 3.10（约 25MB），无需管理员权限",
        size_gb=0.03, action="" if base else "python:portable"))

    # compute env ----------------------------------------------------------
    ddsp_py = Path(paths["synth_python"])
    ddsp_exists = ddsp_py.is_file()
    items.append(_item(
        "synth_env", "DDSP-SVC 环境（envs\\ddsp venv）",
        "ready" if ddsp_exists else "missing",
        str(ddsp_py) if ddsp_exists else f"缺少 {ddsp_py}",
        required=True, installable=not ddsp_exists,
        fix="自动创建 venv 并安装 torch/torchaudio（CUDA 12.8）+ DDSP-SVC 依赖（约 8GB）",
        size_gb=8.0, action="" if ddsp_exists else "venv:synth"))

    torch_version = runtime.dist_version(ddsp_py, "torch") if ddsp_exists else ""
    torch_cuda = runtime.has_cuda_build(ddsp_py) if ddsp_exists else False
    if not ddsp_exists:
        torch_status, torch_detail = "missing", "随 DDSP-SVC 环境一起安装"
    elif not torch_version:
        torch_status, torch_detail = "missing", "DDSP-SVC 环境里没有 torch"
    elif gpu and int(gpu.get("cc_major") or 0) >= 7 and not torch_cuda:
        torch_status, torch_detail = "mismatch", f"torch {torch_version} 是 CPU 版，检测到 GPU 却用不上"
    else:
        torch_status, torch_detail = "ready", f"{torch_version}{'（CUDA）' if torch_cuda else '（CPU）'}"
    items.append(_item(
        "torch", "PyTorch / torchaudio", torch_status, torch_detail,
        required=True, installable=torch_status != "ready",
        fix=f"安装 {'CUDA 12.8' if gpu and int(gpu.get('cc_major') or 0) >= 7 else 'CPU'} 版 torch",
        size_gb=2.9 if gpu and int(gpu.get("cc_major") or 0) >= 7 else 0.25,
        action="" if torch_status == "ready" else "pip:synth:torch"))

    # DDSP runtime packages: 以前只查 python.exe + torch，pip 断在中途也显示「就绪」，
    # 第一次生成才报 ModuleNotFoundError，而且没有任何补装入口。
    if not ddsp_exists:
        items.append(_item(
            "ddsp_deps", "安装 DDSP-SVC 依赖", "missing", "随 DDSP-SVC 环境一起安装",
            required=True, installable=False,
            fix="随 DDSP-SVC 环境创建后自动安装"))
    else:
        deps_ok, deps_detail = ddsp_deps_status(ddsp_py)
        items.append(_item(
            "ddsp_deps", "安装 DDSP-SVC 依赖",
            "ready" if deps_ok else "missing", deps_detail,
            required=True, installable=not deps_ok,
            fix="" if deps_ok else "重装 librosa/pyworld/parselmouth/torchcrepe/transformers 等（走 pip 镜像）",
            size_gb=0.6, action="" if deps_ok else "pip:synth:deps"))

    # separation env -------------------------------------------------------
    sep_py = Path(paths["sep_python"])
    sep_exists = sep_py.is_file()
    items.append(_item(
        "sep_env", "分离环境（audio-separator venv）",
        "ready" if sep_exists else "missing",
        str(sep_py) if sep_exists else f"缺少 {sep_py}",
        required=True, installable=not sep_exists,
        fix="自动创建 venv 并安装 audio-separator + torch（约 4GB）",
        size_gb=4.0, action="" if sep_exists else "venv:sep"))

    sep_version = runtime.dist_version(sep_py, "audio_separator") if sep_exists else ""
    items.append(_item(
        "audio_separator", "audio-separator", "ready" if sep_version else "missing",
        sep_version or ("随分离环境一起安装" if not sep_exists else "未安装"),
        required=True, installable=not bool(sep_version),
        fix="pip install audio-separator", size_gb=0.3,
        action="" if sep_version else "pip:sep:deps"))

    # DDSP-SVC repo ---------------------------------------------------------
    repo = Path(paths["ddsp_root"])
    repo_ok = (repo / "main_reflow.py").is_file() and (repo / "train_reflow.py").is_file()
    items.append(_item(
        "ddsp_repo", "DDSP-SVC 6.3 仓库（RectifiedFlow）", "ready" if repo_ok else "missing",
        str(repo) if repo_ok else f"缺少 {repo}",
        required=True, installable=not repo_ok,
        fix="自动从 GitHub 下载 zip 并解压（无需 git）",
        size_gb=0.03, action="" if repo_ok else "repo:ddsp"))

    # models ---------------------------------------------------------------
    for key, spec in MODEL_ITEMS.items():
        target = Path(paths[spec["target"]])
        base = _model_base(spec, target)
        dest = str(_model_zip_dir(spec, target) if spec["kind"] == "zip" else base)
        missing = _missing_files(spec, base)
        resolved, detected_by, detected_method = None, "", ""
        if missing and spec.get("tier"):
            # 没转好的 low.pt / high.pt 时，bases 里的官方 .sf_dlc 也算就绪。
            resolved, detected_by, detected_method = _find_tier_base(paths, spec, target)
            if resolved is not None:
                missing = []
        optional = bool(spec.get("optional"))
        item = _item(
            key, spec["label"], "ready" if not missing else "missing",
            (f"{resolved}（已就绪：{detected_by}）" if resolved is not None else
             f"{dest}（已就绪）" if not missing
             else f"缺少 {len(missing)}/{len(spec['files'])} 个文件 · {dest}"),
            required=not optional, installable=bool(missing),
            fix=("自动下载并支持断点续传" if not missing else
                 f"自动下载到 {dest}（约 {spec['size_gb']}GB，支持断点续传）"),
            size_gb=0.0 if not missing else float(spec["size_gb"]),
            action="" if not missing else f"model:{key}")
        if resolved is not None:
            # 报告里标明是哪个文件、按什么判定的（arch=结构，filename=文件名）。
            item["resolved_path"] = str(resolved)
            item["detected_by"] = detected_method
        items.append(item)

    # disk -----------------------------------------------------------------
    root = Path(paths["compute_root"])
    where = str(root.drive or root)
    free = _free_gib(root)
    # 余量要覆盖本次实际会下载的东西（「一键安装」连可选底模一起下），
    # 但只有还缺东西时才需要预留；全部装好后不再因为「剩余 < 20GB」判失败。
    download_gb = sum(float(i["size_gb"]) for i in items
                      if i["status"] == "missing" and i["installable"])
    optional_gb = sum(float(i["size_gb"]) for i in items
                      if i["status"] == "missing" and i["installable"] and not i["required"])
    opt_txt = f"（其中可选 {optional_gb:.1f}GB）" if optional_gb else ""
    if download_gb <= 0:
        disk_status, disk_ok = "ready", True
        disk_detail = (f"{where} 剩余空间读不到；本次没有要下载的项" if free is None
                       else f"{where} 剩余 {free:.1f}GB；本次没有要下载的项")
        disk_fix = ""
    elif free is None:
        disk_status, disk_ok = "optional", True
        disk_detail = f"{where} 剩余空间读不到；本次约需 {download_gb:.1f}GB{opt_txt}"
        disk_fix = "读不到磁盘剩余空间，安装前请自行确认空间足够"
    else:
        need = download_gb * 1.15
        disk_ok = free >= need
        disk_status = "ready" if disk_ok else "mismatch"
        disk_detail = f"{where} 剩余 {free:.1f}GB；还需约 {download_gb:.1f}GB{opt_txt}"
        disk_fix = "" if disk_ok else "换一个剩余空间更大的目录（首次向导里可改）"
    items.append(_item(
        "disk", "安装磁盘空间", disk_status, disk_detail,
        required=True, installable=False, fix=disk_fix))

    missing_keys = [i["key"] for i in items if i["required"] and i["status"] != "ready"]
    optional_missing = [i["key"] for i in items if not i["required"] and not i["ok"]]
    required_missing_gb = round(sum(float(i["size_gb"]) for i in items
                                    if i["installable"] and i["status"] != "ready"
                                    and i["required"]), 1)
    optional_missing_gb = round(sum(float(i["size_gb"]) for i in items
                                    if i["installable"] and i["status"] != "ready"
                                    and not i["required"]), 1)
    return {
        "ok": not missing_keys,
        "ready": not missing_keys,
        "items": items,
        "missing": missing_keys,
        "optional_missing": optional_missing,
        "installable_missing": [i["key"] for i in items
                                if i["installable"] and i["status"] != "ready"],
        "actions": [i["action"] for i in items
                    if i["action"] and i["status"] != "ready"],
        # total_missing_gb 保留旧字段名，但现在只统计必需项（可选档位单独给）。
        "total_missing_gb": required_missing_gb,
        "required_missing_gb": required_missing_gb,
        "optional_missing_gb": optional_missing_gb,
        "suggested_root": str(root),
        "default_root": str(runtime.default_install_root()),
        "base_python": {"path": str(base or ""), "version": base_version, "source": base_source},
        "hardware": probe,
        "hardware_brief": runtime.probe_brief(probe),
        "paths": {k: str(v) for k, v in paths.items()
                  if isinstance(v, (str, Path)) and k not in {"raw", "host_section"}},
    }


# ---------------------------------------------------------------------------
# Native folder picker (首次引导的「选择文件夹」)
# ---------------------------------------------------------------------------
# 面板跑在宿主的 iframe 里，拿不到 Electron 的 dialog；这里让插件进程自己弹
# 系统目录框。对话框会阻塞线程，所以放在后台线程里，entry 只负责「打开 + 等一会
# 儿」，没选完就返回 open，面板下次调用接着等——避开宿主的单次调用超时。
_BROWSE_LOCK = threading.Lock()
_BROWSE: dict = {"state": "idle", "path": "", "error": ""}
_BROWSE_THREAD: list = [None]


def _browse_dialog_win32(start_path: str, title: str) -> tuple[str, str]:
    """Windows 原生文件夹选择框（shell32!SHBrowseForFolderW，纯 ctypes）。

    宿主是 PyInstaller 打的 exe，tkinter 只带了 __init__，没有 filedialog 子模块，
    所以 Windows 上直接调系统 API，不依赖任何额外组件。
    """
    import ctypes
    from ctypes import wintypes

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", wintypes.LPWSTR),
            ("lpszTitle", wintypes.LPCWSTR),
            ("ulFlags", ctypes.c_uint),
            ("lpfn", ctypes.c_void_p),
            ("lParam", ctypes.c_void_p),
            ("iImage", ctypes.c_int),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                    ctypes.c_size_t, ctypes.c_void_p]
    user32.SendMessageW.restype = ctypes.c_void_p
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL

    BFFM_INITIALIZED = 1
    BFFM_SETSELECTIONW = 0x400 + 103
    initial = str(start_path or "")
    if not (initial and Path(initial).is_dir()):
        initial = ""

    @ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, ctypes.c_uint,
                        ctypes.c_void_p, ctypes.c_void_p)
    def on_msg(hwnd, msg, lparam, data):  # noqa: ANN001
        if msg == BFFM_INITIALIZED:
            try:
                user32.SetForegroundWindow(hwnd)
                if initial:
                    ptr = ctypes.cast(ctypes.c_wchar_p(initial), ctypes.c_void_p)
                    user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1, ptr)
            except Exception:  # noqa: BLE001
                pass
        return 0

    display = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFOW()
    bi.hwndOwner = None
    bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
    bi.lpszTitle = title or "选择安装文件夹"
    # BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE
    bi.ulFlags = 0x1 | 0x10 | 0x40
    bi.lpfn = ctypes.cast(on_msg, ctypes.c_void_p)
    bi.lParam = None
    bi.iImage = 0

    try:
        ole32.CoInitialize(None)
    except Exception:  # noqa: BLE001
        pass
    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return "", ""  # 用户取消
    try:
        buf = ctypes.create_unicode_buffer(32768)
        if not shell32.SHGetPathFromIDListW(pidl, buf):
            return "", "无法读取所选目录"
        return os.path.normpath(buf.value), ""
    finally:
        try:
            ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        except Exception:  # noqa: BLE001
            pass


def _browse_dialog_tk(start_path: str, title: str) -> tuple[str, str]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
    except Exception:  # noqa: BLE001
        pass
    root.update_idletasks()
    initial = str(start_path or "")
    if not (initial and Path(initial).is_dir()):
        initial = ""
    try:
        picked = filedialog.askdirectory(
            parent=root, title=title or "选择安装文件夹",
            initialdir=initial or None, mustexist=False)
        return (os.path.normpath(picked) if picked else ""), ""
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _browse_dialog(start_path: str, title: str) -> tuple[str, str]:
    """弹系统目录选择框。返回 (路径, 错误)；路径为空表示用户取消。"""
    errors: list[str] = []
    if sys.platform == "win32":
        try:
            return _browse_dialog_win32(start_path, title)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"win32: {type(exc).__name__}: {exc}")
    try:
        return _browse_dialog_tk(start_path, title)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"tkinter: {type(exc).__name__}: {exc}")
    return "", "；".join(errors) or "当前环境没有可用的目录选择器"


def browse_start(start_path: str = "", title: str = "", restart: bool = False) -> dict:
    """确保系统目录框已经打开（已开着就复用），立即返回当前状态。

    上一次已经选完/取消/失败时，只有 ``restart=True`` 才重新弹框；否则原样返回
    上次结果，避免轮询把刚取消的窗口又弹出来。
    """
    with _BROWSE_LOCK:
        th = _BROWSE_THREAD[0]
        if th is not None and th.is_alive():
            return dict(_BROWSE)
        if not restart and _BROWSE["state"] in {"picked", "cancelled", "error"}:
            return dict(_BROWSE)

        def worker() -> None:
            path, err = _browse_dialog(start_path, title)
            with _BROWSE_LOCK:
                if err:
                    _BROWSE.update(state="error", path="", error=err)
                elif path:
                    _BROWSE.update(state="picked", path=path, error="")
                else:
                    _BROWSE.update(state="cancelled", path="", error="")

        _BROWSE.update(state="open", path="", error="")
        th = threading.Thread(target=worker, name="ms-setup-browse", daemon=True)
        _BROWSE_THREAD[0] = th
        th.start()
        return dict(_BROWSE)


def browse_wait(seconds: float = 20.0) -> dict:
    """等用户选完（最多 seconds 秒）。没等到就返回 state=open，面板可以再调。"""
    deadline = time.time() + max(0.0, float(seconds))
    while time.time() < deadline:
        with _BROWSE_LOCK:
            if _BROWSE["state"] in {"picked", "cancelled", "error"}:
                return dict(_BROWSE)
        time.sleep(0.1)
    with _BROWSE_LOCK:
        return dict(_BROWSE)


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------
def _item_map(report: dict) -> dict:
    return {i["key"]: i for i in report.get("items", [])}


def install(paths: dict, items: list[str] | None, progress, probe: dict | None = None) -> dict:
    """Install the requested missing items. ``items=None`` means everything missing."""
    probe = probe or runtime.probe_hardware(force=True)
    gpu = probe.get("gpu")
    report = check_dependencies(paths, probe)
    if not paths.get("configured"):
        return {"ok": False, "error": "还没有选择安装位置；插件不会自动选盘下载",
                "installed": [], "skipped": [],
                "errors": [{"item": "install_path",
                            "error": "请先选择安装位置（绝对路径）"}],
                "missing_after": report.get("missing", []), "report": report}
    requested = items or [i["key"] for i in report["items"]
                          if i["installable"] and i["status"] != "ready"]
    installed: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []

    root = Path(paths["compute_root"])
    try:
        root.mkdir(parents=True, exist_ok=True)
        for key in ("envs_dir", "models_dir", "work_dir", "sep_model_dir"):
            Path(paths[key]).mkdir(parents=True, exist_ok=True)
        Path(paths["ddsp_root"]).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "error": f"无法创建安装目录：{exc}", "installed": [],
                "skipped": skipped, "errors": [{"item": "root", "error": str(exc)}]}

    # base python first if we need to build a venv / download models
    base, base_version, base_source = find_base_python(paths)
    if base is None and any(k in requested for k in (
            "synth_env", "sep_env", "python_host", "base_python",
            "ddsp_contentvec", "ddsp_vocoder", "ddsp_rmvpe", "sep_karaoke")):
        try:
            progress("未找到 Python，下载便携版 3.10…")
            base = download_portable_python(paths, lambda d: progress(d))
            runtime.save_config(Path(paths["config_file"]).parent, {"base_python": base})
            paths["base_python"] = Path(base)
            installed.append("base_python")
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": "base_python", "error": f"{type(exc).__name__}: {exc}"})

    def need_venv(key: str) -> Path | None:
        spec = {"synth_env": ("envs_dir", "ddsp"), "sep_env": ("envs_dir", "sep")}[key]
        if not base:
            errors.append({"item": key, "error": "没有可用的基础 Python，无法创建虚拟环境"})
            return None
        target = Path(paths[spec[0]]) / spec[1]
        try:
            python = _make_venv(base, target, progress, spec[1])
            installed.append(key)
            return python
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": key, "error": str(exc)})
            return None

    # DDSP-SVC repo
    if "ddsp_repo" in requested:
        repo = Path(paths["ddsp_root"])
        if not (repo / "main_reflow.py").is_file():
            try:
                _ensure_repo(paths, progress)
                installed.append("ddsp_repo")
            except Exception as exc:  # noqa: BLE001
                errors.append({"item": "ddsp_repo", "error": f"{type(exc).__name__}: {exc}"})

    # venvs
    synth_py = Path(paths["synth_python"])
    if "synth_env" in requested and not synth_py.is_file():
        made = need_venv("synth_env")
        if made:
            synth_py = made
            paths["synth_python"] = made
    sep_py = Path(paths["sep_python"])
    if "sep_env" in requested and not sep_py.is_file():
        made = need_venv("sep_env")
        if made:
            sep_py = made
            paths["sep_python"] = made

    # torch + deps
    index = _torch_index(gpu)
    torch_label = "CUDA 12.8" if index == TORCH_CU128_INDEX else "CPU"
    if "torch" in requested and synth_py.is_file():
        try:
            _pip(synth_py, ["--index-url", index, "torch", "torchaudio"], progress,
                 f"安装 torch/torchaudio（{torch_label}）")
            installed.append("torch")
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": "torch", "error": str(exc)})
    if ("synth_env" in requested or "ddsp_deps" in requested) and synth_py.is_file():
        try:
            _pip(synth_py, DDSP_PACKAGES, progress, "安装 DDSP-SVC 依赖")
            installed.append("synth_deps")
            invalidate_deps_cache(synth_py)
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": "ddsp_deps", "error": str(exc)})
        # 市场不允许插件包携带 .pyd/.exe/.dll，逐应用采集（proc-tap）和系统媒体
        # 信息（winrt）改用 pip 装进合成环境；缺了只是这两个功能不可用，不阻塞安装。
        try:
            _pip(synth_py, CAPTURE_NATIVE_PACKAGES, progress, "安装采集 / 系统媒体组件")
            installed.append("capture_native")
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": "capture_native", "error": str(exc)})
    if "ffmpeg" in requested and synth_py.is_file():
        try:
            _pip(synth_py, ["imageio-ffmpeg"], progress, "安装 imageio-ffmpeg")
            installed.append("ffmpeg")
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": "ffmpeg", "error": str(exc)})

    if sep_py.is_file() and ("sep_env" in requested or "audio_separator" in requested):
        try:
            _pip(sep_py, ["--index-url", index, "torch", "torchaudio"], progress,
                 f"安装分离环境 torch（{torch_label}）")
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": "sep_torch", "error": str(exc)})
        try:
            _pip(sep_py, SEP_PACKAGES, progress, "安装 audio-separator 依赖")
            installed.append("audio_separator")
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": "audio_separator", "error": str(exc)})
        try:
            _pip(sep_py, ["onnxruntime-gpu" if gpu else "onnxruntime"], progress,
                 "安装 onnxruntime")
        except Exception as exc:  # noqa: BLE001
            # CPU wheel is a safe fallback for older drivers.
            errors.append({"item": "sep_onnxruntime", "error": str(exc)})

    # models
    for key, spec in MODEL_ITEMS.items():
        if key not in requested:
            continue
        target = Path(paths[spec["target"]])
        if not _missing_files(spec, _model_base(spec, target)):
            skipped.append(key)
            continue
        try:
            if spec["kind"] == "zip":
                if str(spec.get("zip_mode") or "") == "into_base":
                    zip_dest = _model_base(spec, target)
                else:
                    zip_dest = _model_zip_dir(spec, target)
                _install_zip(spec, zip_dest, progress, spec["label"])
            elif spec["kind"] == "http":
                _download_http_model(paths, spec, progress, spec["label"])
            elif spec["kind"] == "dlc":
                _install_dlc_model(paths, spec, progress, spec["label"], synth_py)
            else:
                _download_sep_weights(paths, progress, spec["label"])
            installed.append(key)
        except Exception as exc:  # noqa: BLE001
            errors.append({"item": key, "error": f"{type(exc).__name__}: {exc}"})

    report_after = check_dependencies(paths, probe)
    return {
        "ok": report_after["ready"],
        "installed": installed,
        "skipped": skipped,
        "errors": errors,
        "missing_after": report_after["missing"],
        "report": report_after,
    }


# ---------------------------------------------------------------------------
# 下载源体检（只读，只取 64KB）
# ---------------------------------------------------------------------------
def diagnose_download_sources(probe_bytes: int = 65536, timeout: float = 8.0) -> dict:
    """探测代理是否真的可用，以及每个下载源能不能连上、速度多少。

    面板「环境体检」里单独列出来，用户遇到下载慢/下不动时一眼能看出是哪一段断了。
    """
    sources = [
        ("GitHub 直连", DDSP_ZIP),
        ("GitHub 镜像 gh-proxy", GH_MIRRORS[0] + VOCODER_ZIP),
        ("GitHub 镜像 ghfast", GH_MIRRORS[1] + VOCODER_ZIP),
        ("HF 镜像 hf-mirror", CONTENTVEC_URLS[0]),
        ("HuggingFace 官方", CONTENTVEC_URLS[1]),
        ("PyPI 清华镜像", PYPI_INDEX + "/pip/"),
        ("PyTorch 官方", TORCH_CU128_INDEX + "/"),
    ]
    rows: list[dict] = []
    opener = _opener()
    for label, url in sources:
        started = time.time()
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "NEKO-music-synth/0.2",
                "Range": f"bytes=0-{int(probe_bytes) - 1}"})
            with opener.open(req, timeout=timeout) as resp:
                chunk = resp.read(int(probe_bytes))
                elapsed = max(0.001, time.time() - started)
                total = resp.headers.get("Content-Range") or resp.headers.get("Content-Length") or ""
                rows.append({
                    "label": label, "url": url, "ok": True,
                    "status": getattr(resp, "status", 200),
                    "seconds": round(elapsed, 2),
                    "kb_per_s": round(len(chunk) / elapsed / 1024, 1),
                    "total": str(total)[-32:],
                })
        except Exception as exc:  # noqa: BLE001 - 体检要把失败原因带回去
            rows.append({"label": label, "url": url, "ok": False,
                         "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                         "seconds": round(time.time() - started, 2)})
    alive = proxy_alive()
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001
        proxies = {}
    return {
        "proxy": {
            "alive": alive,
            "bypassed": not alive,
            "raw": {k: proxies.get(k) for k in ("http", "https", "all") if proxies.get(k)},
        },
        "sources": rows,
        "reachable": [r["label"] for r in rows if r["ok"]],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    data_dir = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent / "data"
    action = argv[2] if len(argv) > 2 else "check"
    paths = runtime.resolve_paths(data_dir)
    if action == "check":
        print(json.dumps(check_dependencies(paths), ensure_ascii=False, indent=2))
        return 0
    if action == "install":
        result = install(paths, None, lambda d: print(d, flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    print("usage: python -m music_synth.setup <data_dir> [check|install]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
