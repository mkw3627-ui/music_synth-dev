"""声音包导入 / 导出（为她而唱）。

导出：把训练目录里的 ``model_*.pt`` + ``config.yaml`` 打成一个 zip。
导入：把 zip 解到 ``<ddsp_root>/exp/<名字>/``，模型列表下一次刷新就能看到。

这里只做文件操作，不依赖宿主 SDK；导入的包来自外部，路径必须严格校验，
只解出 config.yaml 和模型权重两个文件，不执行包里任何内容。
"""
from __future__ import annotations

import re
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

# config.yaml 必须带上的段（DDSP-SVC 的 reflow 配置）
_CONFIG_MARKERS = ("model:", "data:")


def _safe_name(raw: str, fallback: str = "voice") -> str:
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(raw or "")).strip(" .")[:40]
    return name or fallback


def _steps(name: str) -> int:
    match = re.search(r"model_(\d+)\.pt$", str(name or ""))
    return int(match.group(1)) if match else 0


def export_model(ckpt, ddsp_root, base_ckpt, exports_dir) -> dict:
    """Package one trained model as ``exports/<name>_<step>_<time>.zip``."""
    target = Path(str(ckpt or "").strip())
    if not target.is_file():
        return {"ok": False, "error": f"模型文件不存在：{target}"}
    root = Path(str(ddsp_root or ""))
    from_base = Path(str(base_ckpt or ""))
    try:
        target.resolve().relative_to((root / "exp").resolve())
    except (OSError, ValueError):
        return {"ok": False, "error": "只能导出训练目录里的模型"}
    if str(from_base) and (target.resolve() == from_base.resolve()
                           or target.parent.resolve() == from_base.parent.resolve()):
        return {"ok": False, "error": "这是训练用的初始模型，不能导出为声音"}
    cfg = target.parent / "config.yaml"
    if not cfg.is_file():
        return {"ok": False, "error": "模型目录缺少 config.yaml，无法导出"}
    name = target.parent.name or target.stem
    safe = _safe_name(name)
    out_dir = Path(str(exports_dir))
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{safe}_{target.stem}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            zf.write(cfg, f"{name}/config.yaml")
            zf.write(target, f"{name}/{target.name}")
            zf.writestr(f"{name}/README.txt",
                        "为她而唱 声音包\n"
                        f"名字：{name}\n模型：{target.name}\n"
                        "用法：在「准备素材 → 导入声音」里选择这个 zip。\n")
    except OSError as exc:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "error": f"打包失败：{exc}"}
    return {"ok": True, "path": str(out), "dir": str(out.parent), "name": name,
            "step": _steps(target.name),
            "size_mb": round(out.stat().st_size / 1048576, 1)}


def import_model_zip(src, uploaded_name, ddsp_root, acestep_loras=None) -> dict:
    """Extract a voice package into ``<ddsp_root>/exp/<name>/``.

    Only ``config.yaml`` and one ``*.pt`` are written; everything else in the
    zip is ignored.  Path traversal, oversize archives and non-torch payloads
    are rejected before anything is written.

    A 原创音色 (ACE LoRA) export has ``adapter_config.json`` inside its variant
    folders instead; those go to ``acestep_loras`` when it is given.
    """
    src = Path(str(src))
    root = Path(str(ddsp_root or ""))
    try:
        zf = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as exc:
        return {"ok": False, "error": f"不是有效的 zip 文件：{exc}"}
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            return {"ok": False, "error": "声音包是空的"}
        if len(infos) > 4000:
            return {"ok": False, "error": "声音包里的文件太多，拒绝导入"}
        if sum(i.file_size for i in infos) > 4 * 1024 ** 3:
            return {"ok": False, "error": "声音包超过 4GB，拒绝导入"}
        paths = []
        for info in infos:
            p = PurePosixPath(info.filename.replace("\\", "/"))
            if p.is_absolute() or ".." in p.parts:
                return {"ok": False, "error": "声音包路径不安全，拒绝导入"}
            paths.append(p)
        if acestep_loras is not None and any(
                p.name == "adapter_config.json" for p in paths):
            return _import_ace_lora(zf, infos, paths, uploaded_name, Path(str(acestep_loras)))
        cfg_idx = [k for k, p in enumerate(paths)
                   if p.name.lower() in ("config.yaml", "config.yml")]
        pt_idx = [k for k, p in enumerate(paths) if p.suffix.lower() == ".pt"]
        if not cfg_idx or not pt_idx:
            return {"ok": False,
                    "error": "声音包需要同时包含 model_*.pt 和 config.yaml"
                             "（用「我的声音 → 导出」生成的 zip 可以直接导入）"}
        # Prefer a .pt that sits next to a config.yaml, highest step wins.
        cfg_by_dir: dict = {}
        for k in cfg_idx:
            cfg_by_dir.setdefault(str(paths[k].parent), []).append(k)
        chosen_pt = -1
        for k in pt_idx:
            if str(paths[k].parent) not in cfg_by_dir:
                continue
            if chosen_pt < 0 or _steps(paths[k].name) > _steps(paths[chosen_pt].name):
                chosen_pt = k
        if chosen_pt < 0:
            chosen_pt = max(pt_idx, key=lambda k: infos[k].file_size)
            cfg_k = cfg_idx[0]
        else:
            cfg_k = cfg_by_dir[str(paths[chosen_pt].parent)][0]
        if infos[chosen_pt].file_size < 512 * 1024:
            return {"ok": False, "error": "模型文件太小，可能不是完整的 .pt"}
        with zf.open(infos[chosen_pt]) as fh:
            magic = fh.read(4)
        if magic[:2] != b"PK" and magic[:1] != b"\x80":
            return {"ok": False, "error": "模型文件不是有效的 .pt 权重"}
        try:
            cfg_text = zf.read(infos[cfg_k]).decode("utf-8", "ignore")
        except (OSError, KeyError) as exc:
            return {"ok": False, "error": f"读不到 config.yaml：{exc}"}
        if not all(marker in cfg_text for marker in _CONFIG_MARKERS):
            return {"ok": False, "error": "config.yaml 不像 DDSP-SVC 的配置"}
        raw_name = str(paths[chosen_pt].parent.name or Path(str(uploaded_name)).stem)
        safe = _safe_name(raw_name)
        exp_root = root / "exp"
        exp_root.mkdir(parents=True, exist_ok=True)
        dest = exp_root / safe
        n = 2
        while dest.exists():
            dest = exp_root / f"{safe}_{n}"
            n += 1
        pt_name = paths[chosen_pt].name
        try:
            dest.mkdir(parents=True)
            (dest / "config.yaml").write_bytes(zf.read(infos[cfg_k]))
            with zf.open(infos[chosen_pt]) as rf, open(dest / pt_name, "wb") as wf:
                shutil.copyfileobj(rf, wf, 8 * 1024 * 1024)
        except OSError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            return {"ok": False, "error": f"安装声音包失败：{exc}"}
        return {"ok": True, "name": dest.name, "exp_dir": dest.name,
                "path": str(dest / pt_name), "steps": _steps(pt_name),
                "size_mb": round((dest / pt_name).stat().st_size / 1048576, 1)}


_ACE_LORA_FILES = ("adapter_config.json", "adapter_model.safetensors",
                   "adapter_model.bin", "README.md")


def _import_ace_lora(zf, infos, paths, uploaded_name, loras_root) -> dict:
    """Install a 原创音色 export: each variant folder goes to ``loras/<variant>/``.

    Only the adapter files are copied; a variant needs both its adapter config
    and weights, otherwise it is skipped.  Existing folders are never overwritten.
    """
    variants: dict = {}
    for k, p in enumerate(paths):
        if p.name not in _ACE_LORA_FILES or len(p.parts) < 2:
            continue
        variants.setdefault(p.parent.name, []).append(k)
    installed, skipped = [], []
    # 第五十六轮：同名音色已存在时整包改名（梦梦 → 梦梦_2，变体 梦梦_turbo → 梦梦_2_turbo），
    # 不再整包导入失败；整包用同一个新名字，面板才能把各底模变体归到同一个音色下。
    _SFX = ("_xl_turbo", "_xl_sft", "_xl_base", "_turbo", "_sft", "_base")

    def _split(name):
        for sfx in _SFX:
            if name.endswith(sfx):
                return name[: -len(sfx)], sfx
        return name, ""

    planned = {v: _safe_name(v, fallback="ace_lora") for v in variants}
    if any((loras_root / n).exists() for n in planned.values()):
        stems = {_split(n)[0] for n in planned.values()}
        k = 2
        while any((loras_root / f"{st}_{k}{_split(n)[1]}").exists()
                  for n in planned.values() for st in [_split(n)[0]]):
            k += 1
        planned = {v: f"{_split(n)[0]}_{k}{_split(n)[1]}" for v, n in planned.items()}
        skipped.append("同名音色已存在，改名为：" + "、".join(sorted({f"{st}_{k}" for st in stems})))
    for variant, idxs in variants.items():
        names = {paths[k].name for k in idxs}
        safe = planned[variant]
        if "adapter_config.json" not in names or not names & {
                "adapter_model.safetensors", "adapter_model.bin"}:
            skipped.append(f"{variant}（缺少 adapter 配置或权重）")
            continue
        dest = loras_root / safe
        if dest.exists():
            skipped.append(f"{variant}（已存在：{safe}）")
            continue
        try:
            dest.mkdir(parents=True)
            for k in idxs:
                with zf.open(infos[k]) as rf, open(dest / paths[k].name, "wb") as wf:
                    shutil.copyfileobj(rf, wf, 8 * 1024 * 1024)
        except OSError as exc:
            shutil.rmtree(dest, ignore_errors=True)
            return {"ok": False, "error": f"安装原创音色失败：{exc}"}
        installed.append(safe)
    if not installed:
        return {"ok": False, "error": "原创音色包里没有可安装的变体：" + "；".join(skipped)}
    return {"ok": True, "name": installed[0], "exp_dir": installed[0],
            "installed": installed, "skipped": skipped, "kind": "ace_lora",
            "uploaded_name": Path(str(uploaded_name)).name}
