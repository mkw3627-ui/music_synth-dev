"""Install SoulX-Singer runtime: SVC weights + RMVPE + venv.

Runs under the DDSP Python (it only needs modelscope/huggingface_hub from
acestep/pylibs).  Progress is written to ``<root>/install_status.json`` every
few seconds so the plugin can show a live bar in 设置与环境.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import zipfile
from pathlib import Path

MODEL_SVC = 2793965154
MODEL_DIT = 2818092278
MODEL_RMVPE = 181184272
TOTAL = MODEL_SVC + MODEL_DIT + MODEL_RMVPE


def _atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _downloaded(root: Path) -> int:
    total = 0
    for name, expected in (("model-svc.pt", MODEL_SVC), ("model.pt", MODEL_DIT)):
        base = root / "models" / name
        got = max(_size(base), _size(root / "models" / (name + ".incomplete")))
        total += min(got, expected)
    total += min(_size(root / "preprocess" / "rmvpe" / "rmvpe.pt"), MODEL_RMVPE)
    return total


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    result = {"ok": False, "step": "soulx_install"}
    root = Path(str(job["root"]))
    status_path = root / "install_status.json"
    log_path = root / "install_worker.log"
    pylibs = Path(str(job.get("pylibs") or ""))
    base_python = str(job.get("base_python") or "py -3.10")
    venv_dir = Path(str(job["venv"]))
    venv_py = venv_dir / "Scripts" / "python.exe"
    stop = threading.Event()

    def status(pct: int, phase: str, detail: str, error: str = "", done: bool = False):
        _atomic(status_path, {
            "pct": max(0, min(100, int(pct))), "phase": phase,
            "detail": detail, "error": error, "done": done,
            "bytes": _downloaded(root), "total_bytes": TOTAL,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

    def watch_models():
        while not stop.wait(2.0):
            got = _downloaded(root)
            pct = int(got * 60 / TOTAL) if TOTAL else 0
            status(pct, "models",
                   f"正在下载模型 {got / 1024**3:.2f} / {TOTAL / 1024**3:.2f} GB")

    def run(cmd, timeout=7200, env_extra=None):
        env = dict(os.environ)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                    "https_proxy", "all_proxy"):
            env.pop(key, None)
        env["NO_PROXY"] = "*"
        if env_extra:
            env.update(env_extra)
        print("[soulx-install] $ " + " ".join(cmd), flush=True)
        with log_path.open("a", encoding="utf-8", errors="backslashreplace") as fh:
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=timeout, env=env,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            raise RuntimeError(f"命令退出码 {proc.returncode}：{' '.join(cmd[:4])}…")

    try:
        root.mkdir(parents=True, exist_ok=True)
        status(0, "starting", "准备安装 SoulX-Singer…")
        if str(pylibs) not in sys.path:
            sys.path.insert(0, str(pylibs))
        from modelscope import snapshot_download

        # 1) repo (already copied by the plugin if present; fall back to GitHub)
        repo = root / "repo"
        if not (repo / "soulxsinger").is_dir():
            status(1, "repo", "正在下载 SoulX 代码…")
            zip_path = root / "repo.zip"
            urllib.request.urlretrieve(
                "https://github.com/Soul-AILab/SoulX-Singer/archive/refs/heads/main.zip",
                zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(root)
            src = next(root.glob("SoulX-Singer-*"))
            if repo.exists():
                shutil.rmtree(repo, ignore_errors=True)
            src.rename(repo)
            zip_path.unlink(missing_ok=True)

        # 2) SVC weights + RMVPE
        watcher = threading.Thread(target=watch_models, daemon=True)
        watcher.start()
        status(0, "models", "正在下载模型（可断点续传）…")
        snapshot_download("Soul-AILab/SoulX-Singer",
                          local_dir=str(root / "models"))
        status(61, "rmvpe", "正在下载 RMVPE 音高模型…")
        snapshot_download("Soul-AILab/SoulX-Singer-Preprocess",
                          local_dir=str(root / "preprocess"),
                          allow_patterns=["rmvpe/*"])
        stop.set()
        status(65, "models_done", "模型下载完成")

        # 3) 运行环境：第五十八轮起和人声分离共用 envs\sep（同为 torch 2.10 + numpy 2.x），
        #    不再单独建一个 4GB 多的 torch 环境。分离环境还没装时才自己建。
        if not venv_py.is_file():
            status(68, "venv", "正在创建运行环境…")
            base = base_python.split()
            run(base + ["-m", "venv", str(venv_dir)], timeout=600)
            run([str(venv_py), "-m", "pip", "install", "--upgrade",
                 "pip", "setuptools", "wheel"], timeout=1800)
        probe = subprocess.run([str(venv_py), "-c", "import torch, torchaudio"],
                               capture_output=True, timeout=300,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if probe.returncode != 0:
            status(72, "torch", "正在安装 torch（约 2.9GB，较慢，请耐心等）…")
            run([str(venv_py), "-m", "pip", "install", "--timeout", "120",
                 "--retries", "20", "torch==2.10.0", "torchaudio==2.10.0",
                 "--index-url", "https://download.pytorch.org/whl/cu128"],
                timeout=21600)

        # 4) SoulX 变声推理只要这几个包（版本和原来独立环境里的一致）。
        #    不装 preprocess/requirements.txt：那是歌词转写/训练用的，钉 numpy<2，会把分离环境弄坏。
        status(88, "deps", "正在安装 SoulX 依赖…")
        run([str(venv_py), "-m", "pip", "install", "--timeout", "120", "--retries", "20",
             "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
             "transformers==4.41.2", "tokenizers==0.19.1", "huggingface_hub==0.36.2",
             "safetensors==0.8.0", "omegaconf==2.3.0", "accelerate==1.15.0",
             "librosa", "soundfile", "scipy"], timeout=21600)
        status(100, "done", "SoulX-Singer 安装完成", done=True)
        result.update({"ok": True, "venv": str(venv_py),
                       "repo": str(repo), "models": str(root / "models"),
                       "status": str(status_path)})
    except Exception as exc:  # noqa: BLE001
        stop.set()
        msg = f"{type(exc).__name__}: {exc}"
        status(0, "failed", "安装失败：" + msg, error=msg, done=True)
        result["error"] = msg
        result["trace"] = traceback.format_exc()[-6000:]
        print("[soulx-install] FAILED: " + msg, flush=True)
    finally:
        stop.set()
        try:
            out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
