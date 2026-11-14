"""磁盘缓存清理：回收站、训练特征、曲目中间产物、插件试听缓存。

只删除**可再生成**或**已经进回收站**的东西：
  * _trash_* 里超过保留天数的条目（回收站本身）；
  * 已结束训练任务的 data/（mel/units/f0 特征缓存，续训会重新生成）；
  * 已结束曲目的 separate/clean/convert（重唱会重新生成，final 永远保留）；
  * 插件 static/_preview 里超过保留天数的试听缓存。

绝不删除：final 成品、训练好的 model_*.pt、语料库、模型权重、虚拟环境。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

TRASH_GLOB = "_trash_*"
TRACK_STAGES = ("separate", "clean", "convert")


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cutoff(days: float) -> float:
    return time.time() - max(0.0, float(days)) * 86400.0


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _track_store(data_dir: Path) -> list[dict]:
    payload = _read_json(Path(data_dir) / "tracks.json")
    tracks = payload.get("tracks") or []
    return [t for t in tracks if isinstance(t, dict)]


def scan(compute_root: Path, data_dir: Path, plugin_dir: Path,
         days: float = 7.0) -> dict:
    """统计各类可清理项；不删除任何东西。"""
    root = Path(compute_root)
    cutoff = _cutoff(days)
    data_dir = Path(data_dir)
    plugin_dir = Path(plugin_dir)

    categories: dict[str, dict] = {
        "trash": {"label": "回收站（超过保留天数）", "bytes": 0, "items": []},
        "train_data": {"label": "训练特征缓存（已结束的任务）", "bytes": 0, "items": []},
        "track_stages": {"label": "曲目中间产物（成品保留）", "bytes": 0, "items": []},
        "preview_cache": {"label": "试听缓存", "bytes": 0, "items": []},
        "experiments": {"label": "实验临时目录（ab_test / model_trial）", "bytes": 0, "items": []},
        "splits": {"label": "切歌临时文件", "bytes": 0, "items": []},
        "exp_checkpoints": {"label": "旧检查点（需手动选择）", "bytes": 0, "items": []},
    }

    # 1) 回收站
    for trash in root.glob(TRASH_GLOB):
        if not trash.is_dir():
            continue
        for item in trash.iterdir():
            if _mtime(item) < cutoff:
                size = _dir_size(item)
                categories["trash"]["bytes"] += size
                categories["trash"]["items"].append({"path": str(item), "bytes": size})

    # 2) 已结束训练的特征缓存
    runs_root = root / "train_runs"
    if runs_root.is_dir():
        for run in runs_root.iterdir():
            if not run.is_dir():
                continue
            state = _read_json(run / "state.json")
            phase = str(state.get("phase") or "")
            data = run / "data"
            if phase in ("finished", "failed") and data.is_dir() and _mtime(data) < cutoff:
                size = _dir_size(data)
                categories["train_data"]["bytes"] += size
                categories["train_data"]["items"].append({"path": str(data), "bytes": size})

    # 3) 已完成曲目的中间产物
    tracks_root = root / "work" / "tracks"
    if tracks_root.is_dir():
        status_by_id = {str(t.get("id")): str(t.get("status") or "")
                        for t in _track_store(data_dir)}
        for track_dir in tracks_root.iterdir():
            if not track_dir.is_dir():
                continue
            status = status_by_id.get(track_dir.name, "")
            if status in ("pending", "waiting", "queued", "running"):
                continue
            for stage in TRACK_STAGES:
                stage_dir = track_dir / stage
                if stage_dir.is_dir() and _mtime(stage_dir) < cutoff:
                    size = _dir_size(stage_dir)
                    categories["track_stages"]["bytes"] += size
                    categories["track_stages"]["items"].append(
                        {"path": str(stage_dir), "bytes": size})

    # 4) 插件试听缓存（新位置在数据目录；旧版本写在插件 static/_preview，一并扫）
    for preview in (Path(data_dir) / "preview",
                    Path(plugin_dir) / "static" / "_preview"):
        if not preview.is_dir():
            continue
        for item in preview.iterdir():
            if item.is_file() and _mtime(item) < cutoff:
                size = _dir_size(item)
                categories["preview_cache"]["bytes"] += size
                categories["preview_cache"]["items"].append(
                    {"path": str(item), "bytes": size})

    # 5) 实验临时目录
    for name in ("ab_test", "model_trial"):
        path = root / "work" / name
        if path.is_dir() and _mtime(path) < cutoff:
            size = _dir_size(path)
            categories["experiments"]["bytes"] += size
            categories["experiments"]["items"].append({"path": str(path), "bytes": size})

    # 6) 切歌临时文件：只保留仍被曲目记录引用的切片，其余全部可清
    splits = root / "work" / "splits"
    if splits.is_dir():
        referenced = set()
        for track in _track_store(data_dir):
            src = str(track.get("source") or "")
            if src:
                try:
                    referenced.add(str(Path(src).resolve()))
                except OSError:
                    pass
        for item in splits.iterdir():
            files = [p for p in item.rglob("*") if p.is_file()] if item.is_dir() else [item]
            if files and all(str(f.resolve()) in referenced for f in files):
                continue
            if item.is_dir() and any(str(f.resolve()) in referenced for f in files):
                continue
            size = _dir_size(item)
            categories["splits"]["bytes"] += size
            categories["splits"]["items"].append({"path": str(item), "bytes": size})

    # 7) 旧检查点：只清已经结束的训练；进行中的 run 一个都不动
    exp = root / "ddsp" / "DDSP-SVC" / "exp"
    if exp.is_dir():
        for run in exp.glob("_run_*"):
            if not run.is_dir():
                continue
            run_id = run.name[len("_run_"):]
            run_state = _read_json(root / "train_runs" / run_id / "state.json")
            if str(run_state.get("phase") or "") not in ("finished", "failed"):
                continue
            ckpts = sorted(run.glob("model_*.pt"),
                           key=lambda p: int(p.stem.split("_")[1]) if "_" in p.stem else 0)
            for ckpt in ckpts[:-1]:
                if _mtime(ckpt) < cutoff:
                    size = _dir_size(ckpt)
                    categories["exp_checkpoints"]["bytes"] += size
                    categories["exp_checkpoints"]["items"].append(
                        {"path": str(ckpt), "bytes": size})

    total = sum(c["bytes"] for c in categories.values())
    return {"days": days, "categories": categories, "total_bytes": total}


def cleanup(compute_root: Path, data_dir: Path, plugin_dir: Path, *,
            days: float = 7.0,
            include_trash: bool = True,
            include_train_data: bool = True,
            include_track_stages: bool = True,
            include_preview_cache: bool = True,
            include_experiments: bool = False,
            include_splits: bool = False,
            include_exp_checkpoints: bool = False,
            dry_run: bool = True) -> dict:
    report = scan(compute_root, data_dir, plugin_dir, days)
    allowed = {
        "trash": include_trash,
        "train_data": include_train_data,
        "track_stages": include_track_stages,
        "preview_cache": include_preview_cache,
        "experiments": include_experiments,
        "splits": include_splits,
        "exp_checkpoints": include_exp_checkpoints,
    }
    removed_bytes = 0
    removed = []
    errors = []
    for key, category in report["categories"].items():
        if not allowed.get(key):
            continue
        for item in category["items"]:
            path = Path(item["path"])
            if dry_run:
                removed_bytes += int(item["bytes"])
                removed.append({"path": str(path), "bytes": int(item["bytes"])})
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=False)
                else:
                    path.unlink(missing_ok=True)
                removed_bytes += int(item["bytes"])
                removed.append({"path": str(path), "bytes": int(item["bytes"])})
            except OSError as exc:
                errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return {
        "ok": not errors,
        "dry_run": dry_run,
        "days": days,
        "removed_bytes": removed_bytes,
        "removed": removed,
        "errors": errors,
        "scan": report,
    }


def empty_trash(compute_root: Path, *, dry_run: bool = True) -> dict:
    """清空回收站：把 _trash_* 整个目录连同清单一起删掉，不看保留天数。"""
    root = Path(compute_root)
    removed_bytes = 0
    removed: list[dict] = []
    errors: list[dict] = []
    for trash in sorted(root.glob(TRASH_GLOB)):
        if not trash.is_dir():
            continue
        size = _dir_size(trash)
        count = sum(1 for _ in trash.iterdir())
        if dry_run:
            removed_bytes += size
            removed.append({"path": str(trash), "bytes": size, "items": count})
            continue
        try:
            shutil.rmtree(trash, ignore_errors=False)
            removed_bytes += size
            removed.append({"path": str(trash), "bytes": size, "items": count})
        except OSError as exc:
            errors.append({"path": str(trash), "error": f"{type(exc).__name__}: {exc}"})
    return {"ok": not errors, "dry_run": dry_run, "removed_bytes": removed_bytes,
            "removed": removed, "errors": errors}


def usage(compute_root: Path) -> dict:
    root = Path(compute_root)
    items = {
        "trash": sum(_dir_size(p) for p in root.glob(TRASH_GLOB)),
        "work": _dir_size(root / "work"),
        "train_runs": _dir_size(root / "train_runs"),
        "corpus": _dir_size(root / "corpus"),
        "ddsp": _dir_size(root / "ddsp"),
        "envs": _dir_size(root / "envs"),
    }
    return {"root": str(root), "items": items,
            "total_bytes": sum(items.values())}
