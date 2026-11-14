"""Download one ACE-Step DiT checkpoint (HF / ModelScope, resumable).

Run inside the DDSP Python with the plugin dir + acestep/pylibs on sys.path,
matching the other ACE workers.  ``snapshot_download`` resumes interrupted
downloads, so an 18.6GB XL repo can be retried without starting over.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# HuggingFace 官方在国内容易连不上；ModelScope 源失败时会回落到 HF，
# 这里把 HF 端点指到镜像（用户自己设过就不覆盖）。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def _complete(model_dir: Path) -> bool:
    """True when every shard referenced by the index is present and non-empty."""
    idx = model_dir / "model.safetensors.index.json"
    if idx.is_file():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            shards = set((data.get("weight_map") or {}).values())
            return bool(shards) and all(
                (model_dir / s).is_file() and (model_dir / s).stat().st_size > 0
                for s in shards)
        except (OSError, ValueError):
            return False
    weights = model_dir / "model.safetensors"
    try:
        return weights.is_file() and weights.stat().st_size > 0
    except OSError:
        return False


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    result = {"ok": False, "step": "ace_download"}
    try:
        sys.path.insert(0, str(job["plugin_dir"]))
        sys.path.insert(0, str(Path(str(job["plugin_dir"])) / "vendor"))
        sys.path.insert(0, str(job["pylibs"]))
        from acestep.model_downloader import (
            SUBMODEL_REGISTRY,
            _sync_model_code_files,
            download_submodel,
        )

        name = str(job.get("model") or "").strip()
        checkpoints = Path(str(job["checkpoints"]))
        target = checkpoints / name
        if name not in SUBMODEL_REGISTRY:
            raise RuntimeError(f"未知模型：{name or '（空）'}")
        print(f"[ace-download] {name} -> {target}", flush=True)
        if _complete(target):
            result.update(ok=True, message=f"{name} 已经完整")
        else:
            ok, msg = download_submodel(
                name, checkpoints, force=True,
                prefer_source=str(job.get("prefer_source") or "") or None)
            if not ok:
                result.update(ok=False, error=msg)
            else:
                synced = _sync_model_code_files(name, checkpoints)
                result.update(
                    ok=_complete(target),
                    message=msg + (f"；已同步模型代码：{synced}" if synced else ""))
                if not result["ok"]:
                    result["error"] = "下载结束但分片不完整，请重试（会断点续传）"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["trace"] = traceback.format_exc()[-4000:]
        print("[ace-download] FAILED: " + result["error"], flush=True)
    finally:
        try:
            out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
