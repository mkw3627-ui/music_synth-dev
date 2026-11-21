"""插件侧的门面（S4）：拉起守护进程、入队、控制、读状态。

插件（``__init__.py``，S6 接入）只需要这几个函数；所有真正的下载都在
``workers/download_daemon.py`` 的独立进程里做，插件重启/被宿主杀掉都不影响下载。

用法::

    from . import download_manager as DM

    DM.ensure_daemon(paths)                       # 插件 startup 时
    DM.enqueue(paths, ["ddsp_contentvec", "ddsp_vocabler"])   # 入队
    DM.control(paths, "pause", "all")
    state = DM.read_state(paths)                  # 只读 state.json，<200ms
    DM.feature_ready(paths, "cover")

本模块纯标准库，**不 import 宿主 SDK**，也不 import ``setup.py`` / ``__init__.py``，
便于 pytest 直接 import（``from download_manager import ...``）。
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

__all__ = [
    "state_dir", "daemon_pid", "daemon_alive", "ensure_daemon", "stop_daemon",
    "enqueue", "control", "read_state", "read_item", "feature_ready", "items_for",
    "write_command", "log_tail",
]

#: 守护进程默认并发与空闲退出（plugin.toml 的 download_max_parallel /
#: download_idle_sleep_s 会覆盖它们）。
DEFAULT_MAX_PARALLEL = 2
DEFAULT_IDLE_EXIT_S = 60.0

HEARTBEAT_STALE_S = 10.0
LOCK_STALE_S = 10.0
#: 插件目录（本文件所在目录）——守护进程脚本在这里的 workers/ 下
_PLUGIN_DIR = Path(__file__).resolve().parent
DAEMON_SCRIPT = _PLUGIN_DIR / "workers" / "download_daemon.py"

def _load_catalog():
    """导入 download_catalog：包内走相对导入，独立运行走顶层导入。

    真宿主里插件是 ``plugin.plugins.music_synth`` 子包，绝对 ``import download_catalog``
    会失败（插件根目录不在 ``sys.path``）——S12 真机实测确认。
    """
    try:
        from . import download_catalog  # noqa: PLC0415 - 包内
    except ImportError:
        import download_catalog  # noqa: PLC0415 - 顶层
    return download_catalog

_POPEN_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
def state_dir(paths) -> Path:
    """``<compute_root>\\_downloads``（与计划 B.2 一致）。"""
    root = ""
    if isinstance(paths, dict):
        root = str(paths.get("compute_root") or "")
    elif paths:
        root = str(paths)
    if not root:
        root = str(Path.home() / "_NekoDownloads")
    return Path(root) / "_downloads"


def _state_path(paths) -> Path:
    return state_dir(paths) / "state.json"


# ---------------------------------------------------------------------------
# 读状态（只读，不拉起）
# ---------------------------------------------------------------------------
def read_state(paths) -> dict:
    """读 ``state.json``；不存在/损坏返回空结构。只读，绝不写。"""
    path = _state_path(paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "daemon": {}, "totals": {}, "items": {}}
    if not isinstance(data, dict):
        return {"version": 1, "daemon": {}, "totals": {}, "items": {}}
    data.setdefault("version", 1)
    data.setdefault("daemon", {})
    data.setdefault("totals", {})
    data.setdefault("items", {})
    return data


def read_item(paths, key: str) -> dict | None:
    return read_state(paths)["items"].get(key)


def _heartbeat_age(paths) -> float | None:
    hb = read_state(paths).get("daemon", {}).get("heartbeat_at") or 0
    if not hb:
        return None
    return max(0.0, time.time() - float(hb))


def daemon_pid(paths) -> int:
    try:
        return int(read_state(paths).get("daemon", {}).get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows：用 OpenProcess 探测（不依赖 psutil）
        try:
            import ctypes  # noqa: PLC0415
            PROCESS_QUERY_LIMITED = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return bool(ok) and code.value == 259      # STILL_ACTIVE
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def daemon_alive(paths) -> bool:
    """心跳新鲜且 pid 存活。"""
    age = _heartbeat_age(paths)
    if age is None or age > HEARTBEAT_STALE_S:
        return False
    return _pid_alive(daemon_pid(paths))


# ---------------------------------------------------------------------------
# 拉起 / 停止
# ---------------------------------------------------------------------------
def _find_python_executable() -> str:
    """找一个真正能跑守护进程脚本的 Python 解释器。

    **真宿主实测**：插件跑在 ``projectneko_server.exe`` 里，``sys.executable`` 指向
    那个可执行文件而不是 python.exe——拿它去跑 ``download_daemon.py`` 只会再拉起
    一个 N.E.K.O 实例（被单实例锁拦下，日志被写进我们的 log 文件）。
    所以按顺序找：``python.exe`` / ``py`` launcher / 宿主自带运行时 / 最后的兜底。
    """
    import shutil  # noqa: PLC0415
    # 1) sys.executable 本身是 python 时直接用
    exe = str(getattr(sys, "executable", "") or "")
    if exe and Path(exe).name.lower().startswith("python"):
        return exe
    # 2) PATH 里的 python
    for name in ("python.exe", "python3.exe", "python", "python3"):
        hit = shutil.which(name)
        if hit:
            return hit
    # 3) Windows py launcher
    hit = shutil.which("py")
    if hit:
        return hit
    # 4) 常见安装位置
    for minor in ("310", "311", "312", "39"):
        cand = (Path.home() / "AppData" / "Local" / "Programs" / "Python"
                / f"Python{minor}" / "python.exe")
        if cand.is_file():
            return str(cand)
    # 5) 兜底：还是原来的（至少给个明确错误）
    return exe or "python"


def _load_entries():
    """导入 download_entries：包内相对导入优先，独立运行回退顶层导入。

    真宿主里插件是 ``plugin.plugins.music_synth`` 子包，插件根不在 ``sys.path``，
    裸的 ``import download_entries`` 会抛异常（S13 复现：被 except 吞掉后
    ``download_max_parallel`` / ``download_idle_sleep_s`` 永远是默认值）。
    """
    try:
        from . import download_entries  # noqa: PLC0415 - 包内
    except ImportError:
        import download_entries  # noqa: PLC0415 - 顶层（测试/独立运行）
    return download_entries


def _daemon_config_options(paths) -> dict:
    """从配置读守护进程参数；任何异常都退回默认值。"""
    try:
        return _load_entries().daemon_options(paths)
    except Exception:  # noqa: BLE001 - 配置读不到不影响拉起
        return {"max_parallel": DEFAULT_MAX_PARALLEL, "idle_exit_s": DEFAULT_IDLE_EXIT_S}


def _daemon_log(paths) -> Path:
    return state_dir(paths) / "daemon.log"


def ensure_daemon(paths, *, python: str | None = None,
                  max_parallel: int | None = None,
                  idle_exit_s: float | None = None, wait_s: float = 0.0) -> dict:
    """确保守护进程在跑。返回 ``{"started": bool, "pid": int, "reason": str}``。

    ``python`` 缺省用 ``sys.executable``（宿主 Python，装 T0 之前也一定存在）。
    """
    with _POPEN_LOCK:
        if daemon_alive(paths):
            return {"started": False, "pid": daemon_pid(paths), "reason": "already-running"}

        sd = state_dir(paths)
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "cmd").mkdir(parents=True, exist_ok=True)
        script = DAEMON_SCRIPT
        if not script.is_file():
            return {"started": False, "pid": 0,
                    "reason": f"守护进程脚本不存在：{script}"}

        exe = python or _find_python_executable()
        # 未显式给参数时读配置（plugin.toml 的 download_max_parallel /
        # download_idle_sleep_s）；读不到就用默认值。
        opts = _daemon_config_options(paths)
        if max_parallel is None:
            max_parallel = opts["max_parallel"]
        if idle_exit_s is None:
            idle_exit_s = opts["idle_exit_s"]
        cmd = [str(exe), "-I", "-X", "utf8", str(script), str(sd), str(_PLUGIN_DIR),
               "--max-parallel", str(int(max_parallel)),
               "--idle-exit-s", str(float(idle_exit_s))]

        creationflags = 0
        if os.name == "nt":
            creationflags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log_path = _daemon_log(paths)
        try:
            with open(log_path, "ab") as log_fh:
                proc = subprocess.Popen(                     # noqa: S603 - 本机固定命令
                    cmd, stdin=subprocess.DEVNULL, stdout=log_fh,
                    stderr=subprocess.STDOUT, close_fds=True,
                    creationflags=creationflags, cwd=str(_PLUGIN_DIR),
                )
        except OSError as exc:
            return {"started": False, "pid": 0, "reason": f"启动失败：{exc}"}

        if wait_s > 0:
            deadline = time.time() + wait_s
            while time.time() < deadline:
                if daemon_alive(paths):
                    break
                time.sleep(0.1)
        return {"started": True, "pid": proc.pid, "reason": "spawned"}


def stop_daemon(paths, *, timeout_s: float = 5.0) -> dict:
    """让守护进程收尾退出（发 shutdown 命令，必要时强杀）。"""
    write_command(paths, {"action": "shutdown"})
    pid = daemon_pid(paths)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _pid_alive(pid):
            return {"stopped": True, "pid": pid, "forced": False}
        time.sleep(0.15)
    if _pid_alive(pid):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10, check=False)
            else:
                os.kill(pid, 9)
        except OSError:
            pass
        return {"stopped": True, "pid": pid, "forced": True}
    return {"stopped": True, "pid": pid, "forced": False}


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def _json_safe(value):
    """把 paths 里的 ``Path`` / 其它非 JSON 值转成字符串（真宿主实测会抛 TypeError）。

    ``runtime.resolve_paths()`` 返回的 dict 里大量值是 ``Path``，直接 json.dumps
    会报 ``Object of type WindowsPath is not JSON serializable``。这里递归转换。
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - 极端对象就别让它阻断入队
        return ""


def write_command(paths, payload: dict) -> Path:
    """写一个 ``cmd/<ts>-<rand>.json``（原子写），守护进程 0.4 秒内处理。"""
    cmd_dir = state_dir(paths) / "cmd"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time() * 1000)}-{random.randrange(1 << 30):08x}.json"
    path = cmd_dir / name
    tmp = cmd_dir / (name + ".tmp")
    tmp.write_text(json.dumps(_json_safe(payload), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


def enqueue(paths, keys=None, *, items=None, auto_start: bool = True,
            priority: int = 0, files_override=None, reset: bool = False) -> dict:
    """入队一个或多个下载物。

    ``keys``：catalog key 列表；``items``：直接给 ``[{key, paths?, ...}]``（测试用）。
    ``auto_start``：守护进程没在跑时自动拉起。
    """
    entries = []
    for spec in (items or []):
        entries.append(_json_safe(spec))
    for key in (keys or []):
        entries.append({"key": key, "paths": _json_safe(dict(paths or {}))
                        if isinstance(paths, dict) else {},
                        "priority": priority, "files_override": _json_safe(files_override),
                        "reset": reset})

    started = {"started": False, "pid": 0, "reason": "not-needed"}
    if auto_start:
        started = ensure_daemon(paths)
    write_command(paths, {"action": "enqueue", "items": entries})
    return {"ok": True, "count": len(entries), "daemon": started}


def control(paths, action: str, target: str = "all", *, auto_start: bool = False) -> dict:
    """``pause`` / ``resume`` / ``cancel`` / ``retry`` / ``prioritize``。"""
    if action not in ("pause", "resume", "cancel", "retry", "prioritize"):
        raise ValueError(f"未知控制动作：{action}")
    if auto_start and not daemon_alive(paths):
        ensure_daemon(paths)
    write_command(paths, {"action": action, "target": str(target or "all")})
    return {"ok": True, "action": action, "target": target}


# ---------------------------------------------------------------------------
# 功能就绪判定（S5/S6/S10 用）
# ---------------------------------------------------------------------------
def items_for(features, tiers=None) -> list[str]:
    """按功能与层级列出 catalog key（re-export catalog 的查询，避免各模块重复 import）。"""
    return _load_catalog().artifacts_for(features, tiers)


def feature_ready(paths, feature: str) -> dict:
    """功能是否就绪：该功能所有必需项都已 ``done``。

    返回 ``{"ready": bool, "missing": [keys], "running": [keys], "failed": [keys]}``。
    """
    catalog = _load_catalog()
    state = read_state(paths)
    items = state.get("items") or {}
    keys = catalog.feature_items(feature).get("T2", []) + \
        catalog.feature_items(feature).get("T0", []) + \
        catalog.feature_items(feature).get("T1", [])
    missing, running, failed = [], [], []
    for key in keys:
        spec = catalog.ARTIFACTS.get(key) or {}
        if not spec.get("required", True):
            continue
        item = items.get(key) or {}
        st = item.get("state")
        if st == "done":
            continue
        if st in ("probing", "downloading", "verifying", "post", "pending_post", "queued"):
            running.append(key)
        elif st in ("failed",):
            failed.append(key)
        else:
            missing.append(key)
    return {"ready": not missing and not running and not failed,
            "missing": missing, "running": running, "failed": failed}


def log_tail(paths, lines: int = 40) -> list[str]:
    """读守护进程日志末尾（诊断用）。"""
    path = _daemon_log(paths)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-int(lines):]
