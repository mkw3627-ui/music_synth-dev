"""后台下载守护进程（S4）。

独立于插件进程运行：关面板、切页、关 N.E.K.O 都不停；被杀掉后插件重新拉起，
从 ``.part`` 断点续传。

启动方式（插件侧用 ``download_manager.ensure_daemon``）::

    python -I -X utf8 <plugin_dir>/workers/download_daemon.py <state_dir> <plugin_dir>

目录结构（``<state_dir>`` 默认 ``<compute_root>\\_downloads``）::

    state.json            守护进程唯一写者；tmp + os.replace；最多每秒写一次
    cmd/<ts>-<rand>.json  插件写，守护进程读后删除
    archives/<key>/...    zip / dlc 的暂存区（下完再解压/转换）
    repo_index/<key>.json repo 型文件清单缓存（ModelScope/HF API 的结果）
    daemon.lock           独占字节锁（防双开）
    daemon.log            滚动日志 5MB × 2

设计要点
--------
* **单实例**：``msvcrt.locking``（POSIX 用 ``fcntl.flock``）锁住 ``daemon.lock`` 的
  第 0 字节；抢不到锁立即退出。进程死掉时 OS 自动释放锁，因此「锁能拿到」= 上一
  个实例已死，可安全接管。
* **心跳**：``state.json`` 的 ``daemon.heartbeat_at`` 每 2 秒更新（随状态写盘），
  插件用「超过 10 秒没更新」判死并重拉。
* **断点续传**：``.part`` 与 ``.part.json`` 由 ``download_engine`` 维护在目标文件旁边；
  守护进程重启后按 state.json 里的 item 重新入队，引擎自动从 ``.part`` 续。
* **并发**：默认 2 个下载线程（``--max-parallel``）；单个 item 失败不影响其它。
* **空队列自退**：没有排队/下载中的任务持续 ``--idle-exit-s``（默认 60）秒后退出。
* **磁盘满**：引擎抛 ``DiskFullError`` → item 记 ``paused_disk_full``，每 30 秒复查
  剩余空间，够了自动恢复。

本模块只用标准库 + 同目录的 ``download_engine`` / ``download_catalog``（纯标准库模块），
不 import 宿主 SDK，也不 import ``setup.py`` / ``__init__.py``。
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
import zipfile
from importlib import import_module
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------------------
# 引导：``-I`` 不会把脚本目录加进 sys.path，必须自己加。
# 用 importlib 在注入路径之后加载同目录模块——这样文件顶部没有
# 「import 在语句之后」，市场 CI 的 E402 检查能通过，行为与直接 import 相同。
# ---------------------------------------------------------------------------
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

catalog = import_module("download_catalog")
engine = import_module("download_engine")

STATE_VERSION = 1
HEARTBEAT_S = 2.0
STATE_WRITE_MIN_S = 1.0
DISK_RECHECK_S = 30.0
CMD_POLL_S = 0.4
LOG_MAX_BYTES = 5 * 1024 * 1024

#: item 状态枚举（与计划 B.2 一致）
ITEM_STATES = ("queued", "probing", "downloading", "verifying", "post", "pending_post",
               "done", "paused", "paused_disk_full", "failed", "canceled")

#: 重启时需要继续跑的状态
_RESUMABLE = {"queued", "probing", "downloading", "verifying", "post",
              "pending_post", "paused_disk_full"}


# ---------------------------------------------------------------------------
# 日志（滚动 5MB × 2）
# ---------------------------------------------------------------------------
class _Log:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size < LOG_MAX_BYTES:
                return
        except OSError:
            return
        backup = self.path.with_name(self.path.name + ".1")
        try:
            if backup.exists():
                backup.unlink()
            os.replace(self.path, backup)
        except OSError:
            pass

    def __call__(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
        with self._lock:
            self._rotate()
            try:
                with open(self.path, "a", encoding="utf-8", errors="backslashreplace") as fh:
                    fh.write(line)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 单实例锁
# ---------------------------------------------------------------------------
class _InstanceLock:
    """独占 ``daemon.lock``；抢不到就认为已有存活实例。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fh = open(self.path, "a+b")
        except OSError:
            return False
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415 - Windows 专用
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415 - POSIX 专用
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl  # noqa: PLC0415
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = None


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------
def _atomic_json(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def empty_state() -> dict:
    return {"version": STATE_VERSION,
            "daemon": {"pid": 0, "heartbeat_at": 0.0, "running": False, "started_at": 0.0},
            "totals": {"bytes_done": 0, "bytes_total": 0, "speed_bps": 0,
                       "eta_s": 0, "active": 0, "queued": 0, "failed": 0},
            "items": {}}


def load_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    data.setdefault("version", STATE_VERSION)
    data.setdefault("daemon", {})
    data.setdefault("items", {})
    data.setdefault("totals", {})
    return data


# ---------------------------------------------------------------------------
# zip 放置（与 setup._install_zip 的放置语义保持一致，但不依赖 setup.py）
# ---------------------------------------------------------------------------
def _safe_extract(archive: Path, dest: Path) -> None:
    dest = Path(dest)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or ".." in pure.parts:
                raise RuntimeError(f"压缩包内路径不安全：{name}")
            target = (dest / pure).resolve()
            try:
                target.relative_to(dest.resolve())
            except ValueError as exc:
                raise RuntimeError(f"压缩包内路径逃逸：{name}") from exc
        zf.extractall(dest)


def place_zip(mode: str, archive: Path, dest_root: Path,
              rename: dict | None = None) -> None:
    """把 ``archive`` 按 ``mode`` 放到 ``dest_root``。

    * ``flatten`` / ``into_base`` / ``repo_root``：单顶层目录时把其**内容**铺进
      ``dest_root``（vocoder / rmvpe / 代码仓库都要求这样）。
    * ``nest``：单顶层目录保留目录名放进 ``dest_root``。
    * ``ffmpeg_bin``：找到压缩包里的 ``*/bin`` 目录，把 ``bin`` 的内容铺进 ``dest_root``。
    ``rename`` 在放置后改名（vocoder 的 ``model.ckpt`` → ``model``）。
    """
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(dest_root.parent)) as tmp:
        tmp_path = Path(tmp)
        _safe_extract(Path(archive), tmp_path)
        entries = list(tmp_path.iterdir())
        folders = [p for p in entries if p.is_dir()]
        files = [p for p in entries if p.is_file()]
        single_top = len(folders) == 1 and not files

        if mode == "ffmpeg_bin":
            bin_dir = _find_named_dir(tmp_path, "bin")
            if bin_dir is None:
                raise RuntimeError("ffmpeg 压缩包里找不到 bin 目录")
            _replace_into(bin_dir, dest_root)
        elif mode in ("flatten", "into_base", "repo_root"):
            src_root = folders[0] if single_top else tmp_path
            _replace_into(src_root, dest_root)
        elif mode == "nest":
            for entry in entries:
                target = dest_root / entry.name
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
                shutil.move(str(entry), str(target))
        else:
            raise RuntimeError(f"未知的 zip 放置模式：{mode}")

    for old, new in (rename or {}).items():
        src, dest = dest_root / old, dest_root / new
        if src.is_file() and not dest.exists():
            src.rename(dest)


def _replace_into(src_dir: Path, dest_root: Path) -> None:
    """把 ``src_dir`` 的每个条目移动到 ``dest_root``（同名先删）。

    Windows 上 ``shutil.move`` 往「已存在的目录」里移动会抛
    ``FileExistsError: [WinError 183]``（真机实测：rmvpe.zip 解压后目标
    ``model.pt`` 已存在）。所以先彻底清掉目标，再 move；move 仍失败时
    退化为逐文件 copy。
    """
    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    for entry in list(Path(src_dir).iterdir()):
        target = dest_root / entry.name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            try:
                target.unlink()
            except OSError:
                pass
        if target.exists():
            # 删不掉（被占用）：改为覆盖式拷贝
            _copy_over(entry, target)
            continue
        try:
            shutil.move(str(entry), str(target))
        except (OSError, shutil.Error):
            _copy_over(entry, target)
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except OSError:
                pass


def _copy_over(src: Path, dst: Path) -> None:
    """把 ``src`` 覆盖拷贝到 ``dst``（目录递归）。目标已存在时先尽力清理。"""
    src, dst = Path(src), Path(dst)
    if src.is_dir():
        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            _copy_over(item, dst / item.name)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_dir():
        shutil.rmtree(dst, ignore_errors=True)
    if dst.exists():
        try:
            dst.unlink()
        except OSError:
            pass
    shutil.copy2(src, dst)


def _find_named_dir(root: Path, name: str) -> Path | None:
    for path in sorted(Path(root).rglob(name)):
        if path.is_dir():
            return path
    return None


# ---------------------------------------------------------------------------
# repo 型：列文件清单
# ---------------------------------------------------------------------------
def _repo_listing_urls(spec: dict) -> list[tuple[str, str]]:
    """(source_id, listing URL) 列表，按 catalog 实测速度排序。"""
    repo_id = spec.get("repo_id") or ""
    out: list[tuple[str, str]] = []
    meta = spec.get("sources_meta") or {}
    if meta.get("modelscope"):
        out.append(("modelscope",
                    "https://www.modelscope.cn/api/v1/models/"
                    f"{repo_id}/repo/files?Recursive=true"))
    for host, root in meta.items():
        if host == "modelscope" or not root:
            continue
        out.append((host, f"{root.replace('/resolve/main', '')}/api/models/{repo_id}/tree/main?recursive=true"
                    if "/resolve/" in root else
                    f"https://huggingface.co/api/models/{repo_id}/tree/main?recursive=true"))
    return out


def _fetch_json(url: str, timeout: float = 25.0) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "NEKO-music-synth/0.3 (+daemon)"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _normalize_listing(payload, spec: dict, key: str) -> list[dict]:
    """把 ModelScope / HF 的两种返回结构统一成 ``[{rel, size, sha256, url}]``。"""
    files: list[dict] = []
    meta = spec.get("sources_meta") or {}
    ms_root = meta.get("modelscope") or ""
    hf_root = meta.get("hf_mirror") or meta.get("hf") or ""

    if isinstance(payload, dict):                       # ModelScope
        rows = (payload.get("Data") or {}).get("Files") or []
        for row in rows:
            if row.get("Type") == "tree":
                continue
            rel = str(row.get("Path") or "")
            if not rel:
                continue
            files.append({"rel": rel, "size": int(row.get("Size") or 0),
                          "sha256": (row.get("Sha256") or "").lower(),
                          "url": f"{ms_root}/{rel}" if ms_root else ""})
    else:                                               # HF tree
        for row in payload:
            if not isinstance(row, dict) or row.get("type") != "file":
                continue
            rel = str(row.get("path") or "")
            lfs = row.get("lfs") or {}
            files.append({"rel": rel, "size": int(row.get("size") or 0),
                          "sha256": str(lfs.get("oid") or row.get("oid") or "").lower(),
                          "url": f"{hf_root}/resolve/main/{rel}" if hf_root else ""})
    return _filter_repo_files(files, spec)


def _filter_repo_files(files: list[dict], spec: dict) -> list[dict]:
    """按 components / allow_patterns 过滤，并丢掉 .gitattributes 之类的无关文件。"""
    components = tuple(spec.get("components") or ())
    patterns = tuple(spec.get("allow_patterns") or ())

    def keep(rel: str) -> bool:
        if rel in (".gitattributes", "README.md", "configuration.json"):
            return False
        if patterns:
            return any(fnmatch.fnmatch(rel, pat) for pat in patterns)
        if not components:
            return True
        for comp in components:
            if rel == comp or rel.startswith(comp + "/"):
                return True
        return False

    return [f for f in files if f.get("rel") and keep(f["rel"])]


def _minimal_repo_rows(spec: dict) -> list[dict]:
    """第三级回落：catalog 内置的最小文件清单 → ``[{rel, size, sha256, url}]``。

    清单里没有 url；由调用方按 sources_meta 拼（ModelScope 优先）。
    """
    entries = spec.get("minimal_files") or ()
    if not entries:
        return []
    meta = spec.get("sources_meta") or {}
    ms_root = meta.get("modelscope") or ""
    hf_root = meta.get("hf_mirror") or meta.get("hf") or ""
    rows: list[dict] = []
    for entry in entries:
        if isinstance(entry, (list, tuple)):
            rel = str(entry[0])
            size = int(entry[1]) if len(entry) > 1 else 0
            sha = str(entry[2]).lower() if len(entry) > 2 else ""
        else:
            rel, size, sha = str(entry), 0, ""
        urls = []
        if ms_root:
            urls.append(f"{ms_root}/{rel}")
        if hf_root:
            urls.append(f"{hf_root}/resolve/main/{rel}")
        if not urls:
            continue
        rows.append({"rel": rel, "size": size, "sha256": sha,
                     "url": urls[0], "urls": urls})
    return rows


def list_repo_files(key: str, spec: dict, state_dir: Path, log=None,
                    fetch=None) -> list[dict]:
    """repo 型文件清单，三级回落：

    1. ModelScope ``/repo/files``（含 size + sha256）
    2. HF tree API（``lfs.oid`` 即 sha256）
    3. catalog 内置最小文件清单（``minimal_files``）

    ``fetch`` 可注入（测试用），签名 ``fetch(url) -> payload``。
    缓存存在 ``<state_dir>/repo_index/<key>.json``；三级都失败才抛错。
    """
    cache_dir = Path(state_dir) / "repo_index"
    cache = cache_dir / f"{key}.json"
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            rows = data.get("files") if isinstance(data, dict) else data
            if rows:
                return list(rows)
        except (OSError, ValueError):
            pass

    fetcher = fetch or _fetch_json
    errors: list[str] = []
    for source_id, url in _repo_listing_urls(spec):
        try:
            payload = fetcher(url)
            rows = _normalize_listing(payload, spec, key)
            if rows:
                _cache_repo_rows(cache_dir, cache, key, source_id, rows, log)
                return rows
            errors.append(f"{source_id}: 返回空清单")
        except Exception as exc:  # noqa: BLE001 - 换下一个源
            errors.append(f"{source_id}: {type(exc).__name__}: {str(exc)[:120]}")

    # 第三级：catalog 内置最小清单（API 全挂时仍能下起来）
    minimal = _minimal_repo_rows(spec)
    if minimal:
        _cache_repo_rows(cache_dir, cache, key, "minimal", minimal, log)
        if log:
            log(f"[{key}] 列文件 API 都不可用，改用内置最小清单（{len(minimal)} 个文件）")
        return minimal

    raise RuntimeError(
        f"{key} 列文件失败（{spec.get('repo_id')}）：" + "；".join(errors)[-600:])


def _cache_repo_rows(cache_dir: Path, cache: Path, key: str, source_id: str,
                     rows: list[dict], log=None) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json(cache, {"key": key, "source": source_id,
                             "fetched_at": time.time(), "files": rows})
        if log:
            log(f"[{key}] 文件清单来自 {source_id}：{len(rows)} 个文件")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 计划：把一个 item 拆成若干文件任务
# ---------------------------------------------------------------------------
def _load_entries_module():
    """导入 download_entries：包内相对导入优先，独立运行回退顶层导入。

    真宿主里插件是 ``plugin.plugins.music_synth`` 子包，插件根不在 ``sys.path``，
    裸的 ``import download_entries`` 会失败（S13 复现）。
    """
    try:
        from . import download_entries  # noqa: PLC0415 - 包内
    except ImportError:
        import download_entries  # noqa: PLC0415 - 顶层（测试/独立运行）
    return download_entries


def _coerce_config_dict(raw) -> dict:
    """``raw`` 配置可能是 dict、JSON 字符串或 None；统一成 dict。"""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            # S13 之前入队的条目存的是 Python repr（单引号），按字面量兜底解析
            try:
                parsed = ast.literal_eval(raw)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_paths(raw: dict) -> dict:
    """路径表统一成字符串值；dict/list（如 ``raw`` 配置）存成 JSON，读回时还能解析。"""
    def one(v):
        if v is None:
            return ""
        if isinstance(v, (dict, list, tuple)):
            return json.dumps(v, ensure_ascii=False, default=str)
        return str(v)
    return {str(k): one(v) for k, v in (raw or {}).items()}


def plan_item(item: dict, state_dir: Path, log=None) -> list[dict]:
    """返回 ``[{rel, dest, urls, size, sha256}]``。

    ``item["files_override"]`` 优先（插件可指定自定义源 / 测试用）；否则按
    catalog 的 kind 拆解。
    """
    key = item["key"]
    spec = catalog.ARTIFACTS.get(key) or {"kind": "file", "sources": [], "target": ""}
    paths = _as_paths(item.get("paths") or {})

    override = item.get("files_override")
    if override:
        return [{"rel": f.get("rel") or f.get("name") or "file",
                 "dest": str(f["dest"]),
                 "urls": list(f.get("urls") or f.get("sources") or []),
                 "size": int(f.get("size") or 0),
                 "sha256": str(f.get("sha256") or "")} for f in override]

    kind = spec["kind"]
    staging = Path(state_dir) / "archives" / key

    if kind == "repo":
        rows = list_repo_files(key, spec, state_dir, log)
        dest_root = Path(catalog.target_path(key, paths))
        return [{"rel": row["rel"],
                 "dest": str(dest_root / PurePosixPath(row["rel"])),
                 # 优先用清单里给出的全部源（minimal 清单会同时给 ModelScope 与 HF），
                 # 再并上 catalog 的 sources（如果有）。引擎会测速挑最快的。
                 "urls": list(dict.fromkeys(
                     list(row.get("urls") or ([row["url"]] if row.get("url") else []))
                     + list(spec.get("sources") or []))),
                 "size": int(row.get("size") or 0),
                 "sha256": str(row.get("sha256") or "")} for row in rows]

    if kind == "sep":
        dest_root = Path(catalog.target_path(key, paths))
        out = []
        for name, sha, size, urls in catalog.file_entries(spec):
            out.append({"rel": name, "dest": str(dest_root / name),
                        "urls": urls, "size": size, "sha256": sha})
        return out

    if kind == "zip":
        # 先下到暂存区，post 时解压到目标
        name = Path(urllib.parse.urlsplit((spec.get("sources") or [""])[0]).path).name
        name = name or f"{key}.zip"
        return [{"rel": name, "dest": str(staging / name),
                 "urls": list(spec.get("sources") or []),
                 "size": int(spec.get("size") or 0),
                 "sha256": str(spec.get("sha256") or "")}]

    if kind == "dlc":
        name = Path(urllib.parse.urlsplit((spec.get("sources") or [""])[0]).path).name
        name = name or f"{key}.sf_dlc"
        return [{"rel": name, "dest": str(staging / name),
                 "urls": list(spec.get("sources") or []),
                 "size": int(spec.get("size") or 0),
                 "sha256": str(spec.get("sha256") or "")}]

    # file（单文件直下到目标）
    target = Path(catalog.target_path(key, paths))
    return [{"rel": target.name, "dest": str(target),
             "urls": list(spec.get("sources") or []),
             "size": int(spec.get("size") or 0),
             "sha256": str(spec.get("sha256") or "")}]


# ---------------------------------------------------------------------------
# 后处理
# ---------------------------------------------------------------------------
def _dlc_convert(archive: Path, target: Path, python: str) -> str:
    """把 .sf_dlc 转成 model_0.pt；返回状态字符串。"""
    py = Path(str(python or ""))
    if not py.is_file():
        return "pending_post"
    target.parent.mkdir(parents=True, exist_ok=True)
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
    import subprocess  # noqa: PLC0415 - 只在真正转换时用
    proc = subprocess.run([str(py), "-I", "-c", code, str(archive), str(target)],
                          capture_output=True, text=True, errors="replace", timeout=900,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        raise RuntimeError("底模转换失败：" + ((proc.stderr or proc.stdout or "")[-800:]))
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("底模转换后文件异常")
    return "done"


def _zip_dest_dir(target: Path, key: str) -> Path:
    r"""zip 解压的目标**目录**。

    catalog 里 ``ddsp_rmvpe`` 的 target 是**文件**（``...
mvpe\model.pt``），
    而解压需要目录（``...
mvpe``）。与 ``setup._model_base()`` 同样的判断：
    带文件后缀就当文件、取其父目录；无后缀的（vocoder）本身就是目录。
    """
    target = Path(target)
    if target.suffix and target.suffix.lower() in (".pt", ".bin", ".ckpt", ".zip"):
        return target.parent
    return target


def _resolve_target(key: str, spec: dict, item: dict, paths: dict) -> Path:
    """解析目标路径；不在 catalog 里的 key（带 files_override 的自定义任务）用第一个文件。"""
    if key in catalog.ARTIFACTS:
        return Path(catalog.target_path(key, paths))
    files = item.get("files") or item.get("files_override") or []
    if files:
        first = files[0]
        return Path(str(first.get("dest") or "")).parent
    return Path(str(paths.get("compute_root") or "."))


def run_post(key: str, item: dict, state_dir: Path, log=None) -> tuple[str, str]:
    """执行 post 动作。返回 ``(state, error)``；``pending_post`` 表示缺环境。"""
    spec = catalog.ARTIFACTS.get(key) or {}
    post = str(spec.get("post") or "none")
    paths = _as_paths(item.get("paths") or {})
    target = _resolve_target(key, spec, item, paths)
    staging = Path(state_dir) / "archives" / key
    files = [f for f in item.get("files") or [] if f.get("state") == "done"]
    archive = Path(files[0]["dest"]) if files else None

    if post == "none":
        return "done", ""
    if post == "hf_layout":
        # repo 型已经直接下到最终位置，这里只做存在性确认
        missing = [f["rel"] for f in files if not Path(f["dest"]).is_file()]
        return ("failed", f"放置后缺文件：{', '.join(missing[:3])}") if missing else ("done", "")
    if post.startswith("unzip:"):
        mode = post.split(":", 1)[1]
        if archive is None or not archive.is_file():
            return "failed", "压缩包不存在"
        try:
            place_zip(mode, archive, _zip_dest_dir(target, key), spec.get("rename"))
        except Exception as exc:  # noqa: BLE001
            return "failed", f"解压失败：{type(exc).__name__}: {str(exc)[:300]}"
        _cleanup_staging(staging)
        return "done", ""
    if post == "dlc_convert":
        if archive is None or not archive.is_file():
            return "failed", "底模包不存在"
        python = paths.get("synth_python") or ""
        try:
            state = _dlc_convert(archive, target, python)
        except Exception as exc:  # noqa: BLE001
            return "failed", f"底模转换失败：{type(exc).__name__}: {str(exc)[:300]}"
        if state == "done":
            _cleanup_staging(staging)
        return state, ""
    return "failed", f"未知 post 动作：{post}"


def _cleanup_staging(staging: Path) -> None:
    try:
        if Path(staging).is_dir():
            shutil.rmtree(staging, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 守护进程本体
# ---------------------------------------------------------------------------
class DownloadDaemon:
    def __init__(self, state_dir, plugin_dir=None, *, max_parallel: int = 2,
                 idle_exit_s: float = 60.0, log=None) -> None:
        self.state_dir = Path(state_dir)
        self.plugin_dir = Path(plugin_dir or _PLUGIN_DIR)
        self.state_path = self.state_dir / "state.json"
        self.cmd_dir = self.state_dir / "cmd"
        self.lock = _InstanceLock(self.state_dir / "daemon.lock")
        self.max_parallel = max(1, int(max_parallel))
        self.idle_exit_s = float(idle_exit_s)
        # 守护进程常驻、会自己重试：不要像一次性安装那样长时间等网络恢复，
        # 单个 item 失败后由用户 retry 或下一次 enqueue 再试。
        self.engine_cfg = engine.EngineConfig(net_wait_budget_s=0.0)
        self._engine_cfg_cache: dict[str, object] = {}
        self.log = log or _Log(self.state_dir / "daemon.log")

        self.state = empty_state()
        self._state_lock = threading.RLock()
        self._last_write = 0.0
        self._dirty = True
        self._stop = threading.Event()
        self._pending: list[str] = []
        self._active: set[str] = set()
        self._paused_events: dict[str, threading.Event] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._last_idle = time.time()
        self._last_disk_check = 0.0
        self.pid = os.getpid()

    # -- 状态 -------------------------------------------------------------
    def save_state(self, force: bool = False) -> None:
        with self._state_lock:
            now = time.time()
            if not force and now - self._last_write < STATE_WRITE_MIN_S and not self._dirty:
                return
            self.state["daemon"] = {
                "pid": self.pid,
                "heartbeat_at": round(now, 3),
                "running": not self._stop.is_set(),
                "started_at": self.state.get("daemon", {}).get("started_at") or round(now, 3),
            }
            self.state["totals"] = self._compute_totals()
            _atomic_json(self.state_path, self.state)
            self._last_write = now
            self._dirty = False

    def _compute_totals(self) -> dict:
        items = self.state["items"].values()
        done_bytes = sum(int(i.get("bytes_done") or 0) for i in items)
        total_bytes = sum(int(i.get("bytes_total") or 0) for i in items)
        speed = sum(float(i.get("speed_bps") or 0) for i in items
                    if i.get("state") in ("downloading", "probing"))
        active = sum(1 for i in items if i.get("state") in
                     ("probing", "downloading", "verifying", "post"))
        queued = sum(1 for i in items if i.get("state") == "queued")
        failed = sum(1 for i in items if i.get("state") == "failed")
        eta = int((total_bytes - done_bytes) / speed) if speed > 0 else 0
        return {"bytes_done": done_bytes, "bytes_total": total_bytes,
                "speed_bps": round(speed, 1), "eta_s": eta,
                "active": active, "queued": queued, "failed": failed}

    def item(self, key: str) -> dict | None:
        with self._state_lock:
            return self.state["items"].get(key)

    def _update_item(self, key: str, **fields) -> None:
        with self._state_lock:
            item = self.state["items"].get(key)
            if not item:
                return
            item.update(fields)
            item["updated_at"] = round(time.time(), 3)
            self._dirty = True

    # -- 入队 -------------------------------------------------------------
    def enqueue(self, key: str, paths: dict, *, feature: str = "", tier: str = "",
                label: str = "", priority: int = 0, files_override=None,
                reset: bool = False) -> dict:
        spec = catalog.ARTIFACTS.get(key)
        if spec is None and not files_override:
            raise KeyError(f"未知下载物：{key}")
        if spec is None:
            # 测试 / 自定义任务：只有 files_override 时允许一个不在 catalog 里的 key
            spec = {"feature": "", "tier": "", "label": key, "size": 0}
        with self._state_lock:
            existing = self.state["items"].get(key)
            if existing and not reset and existing.get("state") in _RESUMABLE:
                return existing            # 已在队列里，不重复入队
            item = {
                "key": key, "feature": feature or spec.get("feature", ""),
                "tier": tier or spec.get("tier", ""),
                "label": label or spec.get("label", ""),
                "state": "queued", "bytes_done": 0,
                "bytes_total": int(spec.get("size") or 0),
                "speed_bps": 0.0, "source": "", "source_id": "",
                "switches": 0, "error": "", "attempts": 0,
                "priority": int(priority), "added_at": round(time.time(), 3),
                "updated_at": round(time.time(), 3),
                "paths": _as_paths(paths), "files": [],
            }
            if files_override:
                item["files_override"] = files_override
            self.state["items"][key] = item
            self._dirty = True
        if key not in self._pending and key not in self._active:
            self._pending.append(key)
            self._sort_pending()
        self._last_idle = time.time()
        self.save_state(force=True)
        self.log(f"[{key}] 入队（{item['label']}）")
        return item

    def _sort_pending(self) -> None:
        def prio(k: str) -> tuple:
            item = self.state["items"].get(k) or {}
            return (-int(item.get("priority") or 0), item.get("added_at") or 0)
        self._pending.sort(key=prio)

    # -- 控制 -------------------------------------------------------------
    def control(self, action: str, target: str) -> dict:
        keys = list(self.state["items"]) if target in ("all", "", None) else [target]
        touched = []
        for key in keys:
            item = self.state["items"].get(key)
            if not item:
                continue
            state = item.get("state")
            if action == "pause":
                if state in ("queued",) or key in self._pending:
                    self._remove_pending(key)
                    self._update_item(key, state="paused", speed_bps=0.0)
                elif state in ("probing", "downloading", "verifying"):
                    self._paused_events.setdefault(key, threading.Event()).set()
                touched.append(key)
            elif action == "resume":
                if state in ("paused", "paused_disk_full"):
                    self._paused_events.setdefault(key, threading.Event()).clear()
                    self._update_item(key, state="queued", error="")
                    self._requeue(key)
                touched.append(key)
            elif action == "cancel":
                self._cancel_events.setdefault(key, threading.Event()).set()
                self._paused_events.setdefault(key, threading.Event()).clear()
                self._remove_pending(key)
                self._update_item(key, state="canceled", speed_bps=0.0, error="")
                touched.append(key)
            elif action == "retry":
                if state in ("failed", "canceled", "done"):
                    self._cancel_events.setdefault(key, threading.Event()).clear()
                    self._paused_events.setdefault(key, threading.Event()).clear()
                    self._update_item(key, state="queued", error="", attempts=0,
                                      bytes_done=0, switches=0, files=[])
                    self._requeue(key)
                touched.append(key)
            elif action == "prioritize":
                self._update_item(key, priority=int(time.time()))
                if key in self._pending:
                    self._sort_pending()
                touched.append(key)
        self.save_state(force=True)
        return {"action": action, "target": target, "touched": touched}

    def _remove_pending(self, key: str) -> None:
        try:
            self._pending.remove(key)
        except ValueError:
            pass

    def _requeue(self, key: str) -> None:
        if key not in self._pending and key not in self._active:
            self._pending.append(key)
            self._sort_pending()

    # -- 命令目录 ---------------------------------------------------------
    def process_commands(self) -> int:
        if not self.cmd_dir.is_dir():
            return 0
        handled = 0
        for path in sorted(self.cmd_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue
            try:
                action = str(payload.get("action") or "")
                if action == "enqueue":
                    for spec in payload.get("items") or []:
                        try:
                            self.enqueue(spec["key"], spec.get("paths") or {},
                                         feature=spec.get("feature", ""),
                                         tier=spec.get("tier", ""),
                                         label=spec.get("label", ""),
                                         priority=int(spec.get("priority") or 0),
                                         files_override=spec.get("files_override"),
                                         reset=bool(spec.get("reset")))
                        except (KeyError, TypeError, ValueError) as exc:
                            # 单条坏命令不能让守护进程退出
                            self.log(f"入队失败（{spec.get('key')!r}）：{exc}")
                    if payload.get("key"):
                        self.enqueue(payload["key"], payload.get("paths") or {},
                                     priority=int(payload.get("priority") or 0),
                                     files_override=payload.get("files_override"))
                elif action in ("pause", "resume", "cancel", "retry", "prioritize"):
                    self.control(action, str(payload.get("target") or "all"))
                elif action in ("pause_all", "resume_all"):
                    self.control(action.split("_")[0],
                                 str(payload.get("target") or "all"))
                elif action == "shutdown":
                    self.log("收到 shutdown 命令，退出")
                    self._stop.set()
                else:
                    self.log(f"未知命令：{action}")
                handled += 1
            finally:
                path.unlink(missing_ok=True)
        return handled

    # -- 下载 -------------------------------------------------------------
    def _runner(self) -> None:
        while not self._stop.is_set():
            key = None
            with self._state_lock:
                if self._pending:
                    key = self._pending.pop(0)
            if key is None:
                if time.time() - self._last_idle > 0.2:
                    time.sleep(0.1)
                continue
            self._active.add(key)
            try:
                self._run_item(key)
            except Exception as exc:  # noqa: BLE001 - 单个 item 失败不影响其它
                self.log(f"[{key}] 未捕获异常：{exc}\n{traceback.format_exc()}")
                self._update_item(key, state="failed",
                                  error=f"{type(exc).__name__}: {str(exc)[:400]}")
            finally:
                self._active.discard(key)
                self._last_idle = time.time()
                self.save_state(force=True)

    def _run_item(self, key: str) -> None:
        item = self.item(key)
        if item is None:
            return
        if item.get("state") == "canceled":
            return
        pause = self._paused_events.setdefault(key, threading.Event())
        cancel = self._cancel_events.setdefault(key, threading.Event())
        pause.clear()
        cancel.clear()

        self._update_item(key, state="probing", error="", speed_bps=0.0)
        self.save_state(force=True)

        try:
            plans = plan_item(item, self.state_dir, self.log)
        except Exception as exc:  # noqa: BLE001
            self._update_item(key, state="failed", error=str(exc)[:600])
            self.log(f"[{key}] 计划失败：{exc}")
            return
        if not plans:
            self._update_item(key, state="failed", error="没有可下载的文件")
            return

        total = sum(int(p["size"] or 0) for p in plans)
        self._update_item(key, bytes_total=total or item.get("bytes_total") or 0)
        # 已有文件的进度回填（重启续传时显示正确进度）
        done_before = 0
        for plan in plans:
            dest = Path(plan["dest"])
            part = dest.with_name(dest.name + ".part")
            for cand in (dest, part):
                try:
                    if cand.is_file():
                        done_before += min(cand.stat().st_size, int(plan["size"] or 0) or cand.stat().st_size)
                        break
                except OSError:
                    continue

        files_state: list[dict] = []
        for plan in plans:
            files_state.append({"rel": plan["rel"], "dest": plan["dest"],
                                "state": "queued", "bytes_done": 0,
                                "bytes_total": int(plan["size"] or 0)})
        self._update_item(key, files=files_state, bytes_done=done_before,
                          state="downloading")
        self.save_state(force=True)

        per_file_totals: dict[str, int] = {}
        for idx, plan in enumerate(plans):
            if cancel.is_set():
                self._update_item(key, state="canceled", speed_bps=0.0)
                self._cleanup_archives(key)
                return
            if pause.is_set():
                self._update_item(key, state="paused", speed_bps=0.0)
                return
            per_file_totals[f"f{idx}"] = 0

            def on_progress(p, _idx=idx, _plan=plan):
                d = p.as_dict()
                per_file_totals[f"f{_idx}"] = int(d["bytes_done"])
                with self._state_lock:
                    files = self.state["items"][key]["files"]
                    if _idx < len(files):
                        files[_idx].update({"state": d["state"],
                                            "bytes_done": int(d["bytes_done"]),
                                            "bytes_total": int(d["bytes_total"])})
                    total_done = sum(per_file_totals.values())
                    self.state["items"][key].update({
                        "bytes_done": total_done,
                        "bytes_total": max(total, total_done),
                        "speed_bps": d["speed_bps"],
                        "source": d["source"], "source_id": d["source_id"],
                        "switches": d["switches"],
                        "state": d["state"] if d["state"] in
                        ("downloading", "verifying", "probing") else "downloading",
                    })
                    self._dirty = True

            try:
                result = engine.download(
                    Path(plan["dest"]), list(plan["urls"]),
                    size=int(plan["size"] or 0), sha256=str(plan["sha256"] or ""),
                    on_progress=on_progress,
                    pause=pause, cancel=cancel,
                    cfg=self._engine_cfg_for(item))
            except engine.Paused:
                self._update_item(key, state="paused", speed_bps=0.0)
                self.save_state(force=True)
                return
            except engine.Canceled:
                self._update_item(key, state="canceled", speed_bps=0.0)
                self._cleanup_archives(key)
                self.save_state(force=True)
                return
            except engine.DiskFullError as exc:
                self._update_item(key, state="paused_disk_full", speed_bps=0.0,
                                  error=str(exc)[:400])
                self.log(f"[{key}] 磁盘空间不足，暂停：{exc}")
                self.save_state(force=True)
                return
            except Exception as exc:  # noqa: BLE001
                with self._state_lock:
                    files = self.state["items"][key]["files"]
                    if idx < len(files):
                        files[idx]["state"] = "failed"
                self._update_item(key, state="failed", speed_bps=0.0,
                                  error=f"{type(exc).__name__}: {str(exc)[:400]}")
                self.log(f"[{key}] 文件 {plan['rel']} 失败：{exc}")
                self.save_state(force=True)
                return

            with self._state_lock:
                files = self.state["items"][key]["files"]
                if idx < len(files):
                    files[idx].update({"state": "done",
                                       "bytes_done": result.bytes,
                                       "bytes_total": result.bytes})
            self._dirty = True
            self.save_state(force=True)

        # 全部文件下完 → post
        self._update_item(key, state="post", speed_bps=0.0)
        self.save_state(force=True)
        state, error = run_post(key, self.item(key) or {}, self.state_dir, self.log)
        if state == "done":
            self._update_item(key, state="done", error="", speed_bps=0.0)
            self.log(f"[{key}] 完成")
        elif state == "pending_post":
            self._update_item(key, state="pending_post",
                              error="需要 DDSP 环境才能转换底模，环境就绪后自动续做")
            self.log(f"[{key}] 等待环境后再做后处理")
        else:
            self._update_item(key, state="failed", error=error[:600])
            self.log(f"[{key}] 后处理失败：{error}")
        self.save_state(force=True)

    def _engine_cfg_for(self, item: dict):
        """按该下载物配置构造 EngineConfig（plugin.toml 的 download_* 键）。

        守护进程语义固定 ``net_wait_budget_s=0``（常驻进程不等网络），其余
        （限速/校验/续传/策略）全部来自配置。

        注意两点（S13 真机复现过的坑）：
        * 配置字典的键名是 ``raw``（``runtime.resolve_paths`` 的返回值），不是 ``_raw``；
          ``_as_paths()`` 会把所有值转成字符串，所以必须在它之前取原始 dict。
        * ``download_entries`` 必须「先相对、失败回退绝对」导入——真宿主里插件是
          ``plugin.plugins.music_synth`` 子包，绝对导入会抛异常并被 except 吞掉，
          结果永远用默认值。
        """
        import copy  # noqa: PLC0415
        base = engine.EngineConfig(net_wait_budget_s=0.0)
        raw_paths = item.get("paths") or {}
        raw_cfg = _coerce_config_dict(raw_paths.get("raw") if isinstance(raw_paths, dict) else None)
        paths = _as_paths(raw_paths)
        key = json.dumps([paths, raw_cfg], sort_keys=True, ensure_ascii=False)
        cached = self._engine_cfg_cache.get(key)
        if cached is not None:
            return cached
        try:
            entries = _load_entries_module()
            cfg = entries.engine_config({"raw": raw_cfg}, base=base)
        except Exception:  # noqa: BLE001 - 配置坏掉就用默认
            cfg = copy.copy(base)
        self._engine_cfg_cache[key] = cfg
        return cfg

    def _cleanup_archives(self, key: str) -> None:
        _cleanup_staging(self.state_dir / "archives" / key)

    # -- 磁盘复查 ---------------------------------------------------------
    def check_disk(self) -> None:
        now = time.time()
        if now - self._last_disk_check < DISK_RECHECK_S:
            return
        self._last_disk_check = now
        items = [i for i in self.state["items"].values()
                 if i.get("state") == "paused_disk_full"]
        if not items:
            return
        root = ""
        for item in items:
            root = _as_paths(item.get("paths") or {}).get("compute_root") or root
        if not root:
            return
        try:
            free = shutil.disk_usage(str(Path(root))).free
        except OSError:
            return
        need = sum(_remaining_bytes(i) for i in items)
        if free >= need * 1.1 + 512 * 1024 * 1024 or free >= need:
            for item in items:
                key = item["key"]
                self._update_item(key, state="queued", error="")
                self._requeue(key)
                self.log(f"[{key}] 磁盘空间已恢复，继续下载")
            self.save_state(force=True)

    # -- 主循环 -----------------------------------------------------------
    def run(self) -> int:
        if not self.lock.acquire():
            self.log("已有一个守护进程在运行，本实例退出")
            return 3
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.cmd_dir.mkdir(parents=True, exist_ok=True)
            self.state = load_state(self.state_path)
            started = time.time()
            self.state.setdefault("daemon", {})["started_at"] = round(started, 3)
            self.log(f"守护进程启动 pid={self.pid} state_dir={self.state_dir}")

            # 恢复未完成任务
            for key, item in list(self.state["items"].items()):
                if key not in catalog.ARTIFACTS and not item.get("files_override"):
                    continue
                if item.get("state") in _RESUMABLE:
                    item["state"] = "queued"
                    self._pending.append(key)
                elif item.get("state") == "paused":
                    pass
            self._sort_pending()
            if self._pending:
                self.log(f"恢复 {len(self._pending)} 个未完成任务")
            self.save_state(force=True)

            threads = [threading.Thread(target=self._runner, name=f"dl-{i}", daemon=True)
                       for i in range(self.max_parallel)]
            for t in threads:
                t.start()

            idle_since = time.time()
            while not self._stop.is_set():
                self.process_commands()
                self.check_disk()
                busy = bool(self._pending) or bool(self._active)
                if busy:
                    idle_since = time.time()
                elif self.idle_exit_s and time.time() - idle_since > self.idle_exit_s:
                    self.log(f"队列空闲 {self.idle_exit_s:.0f} 秒，退出")
                    break
                self._dirty = True
                self.save_state()
                time.sleep(CMD_POLL_S)
            self._stop.set()
            for t in threads:
                t.join(timeout=3)
            self.save_state(force=True)
            self.state["daemon"]["running"] = False
            _atomic_json(self.state_path, self.state)
            self.log("守护进程退出")
            return 0
        finally:
            self.lock.release()


def _remaining_bytes(item: dict) -> int:
    total = int(item.get("bytes_total") or 0)
    done = int(item.get("bytes_done") or 0)
    return max(0, total - done)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="为她而唱 · 后台下载守护进程")
    ap.add_argument("state_dir")
    ap.add_argument("plugin_dir", nargs="?", default="")
    ap.add_argument("--max-parallel", type=int, default=2)
    ap.add_argument("--idle-exit-s", type=float, default=60.0)
    args = ap.parse_args(argv)

    daemon = DownloadDaemon(args.state_dir, args.plugin_dir or _PLUGIN_DIR,
                            max_parallel=args.max_parallel,
                            idle_exit_s=args.idle_exit_s)
    try:
        return daemon.run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
