"""训练前把别的常驻 GPU 模型停掉，训练收尾后再拉起来。

「为她所听」(music_perception) 的分离 / 歌词 / 分析模型都是常驻子进程，各占
几百 MB 到 1 GB 显存；DDSP 训练本身要显存吃得比较满，留着它们训练会明显变慢，
严重时直接 OOM。这里走宿主的插件接口整插件停掉——宿主关闭插件时会连带回收它的
worker 子进程，比按 PID 杀干净，也不会留下半死不活的状态。

端口按「环境变量 → resources/bin/core_config.txt → 默认 48916」的顺序找，最后
用 ``GET /plugin/status`` 验证一下真的能通，所以端口写错也只会退化成「没停成」，
不会去乱杀别的进程。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# 占显存最狠、又和训练不共用的插件。
HEAVY_PLUGINS = ("music_perception",)
DEFAULT_PORT = 48916

# 本机接口一律不走代理（系统代理开着时 urllib 会把它套到 127.0.0.1 上）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_BASE_CACHE: dict = {"at": 0.0, "base": ""}


def _request(url: str, *, method: str = "GET", timeout: float = 5.0):
    data = b"" if method == "POST" else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Accept": "application/json"})
    with _OPENER.open(request, timeout=timeout) as resp:
        raw = resp.read(4_000_000)
    try:
        return json.loads(raw.decode("utf-8", "replace") or "null")
    except ValueError:
        return None


def _core_config_paths() -> list[Path]:
    """宿主把插件端口写在 ``<app>/resources/bin/core_config.txt``。"""
    out: list[Path] = []
    for name in ("NEKO_APP_DIR", "NEKO_HOME", "NEKO_ROOT"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            out.append(Path(value) / "resources" / "bin" / "core_config.txt")
    try:
        exe = Path(sys.executable).resolve()
        for base in [exe.parent, *list(exe.parents)[:4]]:
            out.append(base / "resources" / "bin" / "core_config.txt")
    except OSError:
        pass
    for root in (Path("E:/SteamLibrary/steamapps/common"),
                 Path("D:/SteamLibrary/steamapps/common"),
                 Path("C:/Program Files (x86)/Steam/steamapps/common")):
        try:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                candidate = entry / "resources" / "bin" / "core_config.txt"
                if candidate.is_file():
                    out.append(candidate)
        except OSError:
            continue
    seen, ordered = set(), []
    for path in out:
        key = str(path)
        if key not in seen:
            seen.add(key)
            ordered.append(path)
    return ordered


def port_candidates() -> list[int]:
    ports: list[int] = []
    raw = str(os.environ.get("USER_PLUGIN_SERVER_PORT") or "").strip()
    if raw.isdigit():
        ports.append(int(raw))
    for config in _core_config_paths()[:8]:
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        table = data.get("customPorts") if isinstance(data, dict) else None
        value = str((table or {}).get("USER_PLUGIN_SERVER_PORT") or "").strip()
        if value.isdigit():
            ports.append(int(value))
    ports.append(DEFAULT_PORT)
    seen, ordered = set(), []
    for port in ports:
        if port not in seen:
            seen.add(port)
            ordered.append(port)
    return ordered


def host_base(force: bool = False, ttl: float = 30.0) -> str:
    """能回答 ``/plugin/status`` 的那个端口；找不到返回空串。"""
    now = time.time()
    if not force and _BASE_CACHE.get("base") and now - float(_BASE_CACHE.get("at") or 0) < ttl:
        return str(_BASE_CACHE["base"])
    base = ""
    for port in port_candidates():
        url = f"http://127.0.0.1:{port}"
        try:
            data = _request(url + "/plugin/status", timeout=1.5)
        except Exception:  # noqa: BLE001 - 端口没开就是这个异常，继续试下一个
            continue
        if isinstance(data, dict) and isinstance(data.get("plugins"), dict):
            base = url
            break
    _BASE_CACHE.update({"at": now, "base": base})
    return base


def plugin_states(base: str) -> dict[str, str]:
    try:
        data = _request(f"{base}/plugin/status", timeout=3.0)
    except Exception:  # noqa: BLE001
        return {}
    table = data.get("plugins") if isinstance(data, dict) else None
    out: dict[str, str] = {}
    for plugin_id, row in (table or {}).items():
        status = row.get("status") if isinstance(row, dict) else None
        out[str(plugin_id)] = str((status or {}).get("status") or "") \
            if isinstance(status, dict) else str(status or "")
    return out


def _wait_gone(base: str, plugins, wait_s: float) -> set[str]:
    """等到这些插件不再是 running/starting；返回还在跑的。"""
    pending = {str(p) for p in plugins}
    deadline = time.time() + max(0.0, wait_s)
    while pending and time.time() < deadline:
        time.sleep(0.4)
        states = plugin_states(base)
        pending = {p for p in pending if states.get(p) in ("running", "starting")}
    return pending


def _wait_running(base: str, plugins, wait_s: float) -> set[str]:
    """等到这些插件都变成 running；返回还没起来的。"""
    pending = {str(p) for p in plugins}
    deadline = time.time() + max(0.0, wait_s)
    while pending and time.time() < deadline:
        time.sleep(0.4)
        states = plugin_states(base)
        pending = {p for p in pending if states.get(p) != "running"}
    return pending


def stop_plugins(plugins=HEAVY_PLUGINS, *, log=None, wait_s: float = 25.0) -> dict:
    """把占用显存的插件停掉，等它们真的退出后再返回。"""
    base = host_base()
    if not base:
        return {"ok": False, "error": "连不上插件宿主（主程序没开？）",
                "stopped": [], "pending": [], "skipped": []}
    states = plugin_states(base)
    requested: list[str] = []
    skipped: list[dict] = []
    for plugin_id in plugins:
        plugin_id = str(plugin_id)
        state = states.get(plugin_id)
        if state is None:
            skipped.append({"plugin": plugin_id, "reason": "没装"})
            continue
        if state not in ("running", "starting"):
            skipped.append({"plugin": plugin_id, "reason": state or "已停止"})
            continue
        try:
            _request(f"{base}/plugin/{plugin_id}/stop", method="POST", timeout=30.0)
        except Exception as exc:  # noqa: BLE001
            skipped.append({"plugin": plugin_id,
                            "reason": f"{type(exc).__name__}: {exc}"})
            continue
        requested.append(plugin_id)
        if log is not None:
            log.info("[train] 训练前暂停 %s，把显存让给训练", plugin_id)
    pending = _wait_gone(base, requested, wait_s)
    stopped = sorted(set(requested) - pending)
    return {"ok": True, "base": base, "stopped": stopped,
            "pending": sorted(pending), "skipped": skipped}


def start_plugins(plugins, *, log=None, wait_s: float = 40.0) -> dict:
    """训练结束后把它们重新拉起来（只重启之前被停掉的）。"""
    base = host_base(force=True)
    if not base:
        return {"ok": False, "error": "连不上插件宿主", "started": [], "pending": []}
    started: list[str] = []
    failed: list[dict] = []
    for plugin_id in plugins:
        plugin_id = str(plugin_id)
        try:
            _request(f"{base}/plugin/{plugin_id}/start", method="POST", timeout=60.0)
            started.append(plugin_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"plugin": plugin_id, "reason": f"{type(exc).__name__}: {exc}"})
    pending = _wait_running(base, started, wait_s)
    if log is not None and started:
        log.info("[train] 训练结束，重新拉起 %s（pending=%s）", started, sorted(pending))
    return {"ok": bool(started), "started": started,
            "pending": sorted(pending), "failed": failed}


def release_for_training(*, log=None) -> dict:
    """给入口用的一步封装：停掉高占用插件并返回一句话摘要。"""
    result = stop_plugins(HEAVY_PLUGINS, log=log)
    stopped = result.get("stopped") or []
    pending = result.get("pending") or []
    if stopped:
        result["summary"] = "已暂停 " + "、".join(stopped) + "（给训练腾显存）"
    elif pending:
        result["summary"] = "已请求暂停 " + "、".join(pending) + "，但还没退出"
    else:
        result["summary"] = ""
    return result
