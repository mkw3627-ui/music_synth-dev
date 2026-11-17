"""GPU 调度与显存均衡（第四十八轮）。

用户要求：
1. 唱歌（分离→变声→混音 / ACE 作曲）和「舞台演出」不能同时进行——普通电脑扛不住。
   唱歌处理时不起舞台；舞台在演时，新的处理排队等演完。
2. 「为她而唱」和「为她所听」都要加载大模型，只能单线程用 GPU：
   - 处理前看显存够不够；不够就先让「为她所听」让位（释放分离/歌词模型），
     还不够就把它整个插件停掉，处理完再拉起来；
   - 本地大模型（Ollama：编排读歌词用的 14B）用完立刻卸载，不常驻 11 GB。
3. 歌词：网上搜不到、本地也没有 → 用「为她所听」的歌声识别模型转写干声。
   识别前如果「为她所听」插件是关的，先把它打开（借它的环境），识别完按原状态还回去。

这里只放「判断 + 动作」，不持有曲目状态；__init__ 在合适的位置调用。
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request

from . import vram_release

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OLLAMA = "http://127.0.0.1:11434"

# 各类 GPU 工作大概要多少空闲显存（GiB）；不够就逐级腾地方
NEED_GIB = {"sing": 6.0, "ace": 8.0, "asr": 4.0, "show_llm": 9.5}

_LOCK = threading.RLock()          # 进程内：同一时间只有一个 GPU 重活
_STATE = {"holder": "", "since": 0.0, "log": []}


def _log(msg: str, logger=None) -> None:
    line = time.strftime("%H:%M:%S ") + msg
    _STATE["log"] = (_STATE.get("log") or [])[-59:] + [line]
    if logger is not None:
        try:
            logger.info("[gpu] %s", msg)
        except Exception:  # noqa: BLE001
            pass


def gpu_mem() -> dict:
    """nvidia-smi 读整卡显存（GiB）。读不到返回空字典（当作不限制）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip().splitlines()
        used, total = [float(x) for x in out[0].split(",")[:2]]
        return {"used": used / 1024, "total": total / 1024, "free": (total - used) / 1024}
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------ Ollama ------------------------------
def ollama_loaded() -> list[dict]:
    try:
        with _OPENER.open(OLLAMA + "/api/ps", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return [{"name": m.get("name"), "vram_gib": round((m.get("size_vram") or 0) / 2**30, 2)}
                for m in data.get("models") or []]
    except Exception:  # noqa: BLE001
        return []


def ollama_unload(names=None, logger=None) -> list[str]:
    """把 Ollama 里常驻的模型卸掉（keep_alive=0）。names=None 时卸全部。"""
    done = []
    for m in ollama_loaded():
        if names and m["name"] not in names:
            continue
        try:
            body = json.dumps({"model": m["name"], "keep_alive": 0}).encode()
            req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            _OPENER.open(req, timeout=20).read()
            done.append(m["name"])
            _log(f"卸载本地大模型 {m['name']}（{m['vram_gib']} GiB）", logger)
        except Exception as exc:  # noqa: BLE001
            _log(f"卸载 {m['name']} 失败：{exc}", logger)
    return done


# ------------------------------ 「为她所听」 ------------------------------
def perception_state() -> str:
    base = vram_release.host_base()
    if not base:
        return ""
    return vram_release.plugin_states(base).get("music_perception", "")


def ensure_perception_running(logger=None) -> bool:
    """识别歌词要借「为她所听」：没开就打开。返回「是不是我打开的」（用完要还原）。"""
    st = perception_state()
    if st in ("running", "starting") or not st and not vram_release.host_base():
        return False
    res = vram_release.start_plugins(["music_perception"], wait_s=40.0)
    ok = "music_perception" in (res.get("started") or []) and not res.get("pending")
    _log("「为她所听」原本是关的，为识别歌词临时打开" + ("" if ok else "（未确认启动）"), logger)
    return True


def stop_perception(logger=None, why: str = "显存不够：暂时关闭「为她所听」，处理完再打开") -> bool:
    res = vram_release.stop_plugins(["music_perception"], wait_s=25.0)
    stopped = "music_perception" in (res.get("stopped") or [])
    if stopped:
        _log(why, logger)
    return stopped


def start_perception(logger=None) -> None:
    vram_release.start_plugins(["music_perception"], wait_s=40.0)
    _log("处理完：重新打开「为她所听」", logger)


# ------------------------------ 腾显存 ------------------------------
def make_room(kind: str, *, logger=None, soft_yield=None) -> dict:
    """按需逐级腾显存。返回 {"stopped_perception": bool, ...} 交给 restore()。

    1) 先卸本地大模型（Ollama，最占也最好恢复：下次用自动加载）；
    2) 还不够 → 请「为她所听」让位（soft_yield 回调，释放分离/歌词模型，录音不停）；
    3) 还不够 → 整个停掉「为她所听」，处理完再拉起。
    """
    need = NEED_GIB.get(kind, 6.0)
    out = {"kind": kind, "need": need, "stopped_perception": False, "steps": []}
    mem = gpu_mem()
    if not mem:
        return out
    out["free_before"] = round(mem["free"], 2)
    if mem["free"] >= need:
        _log(f"{kind}：空闲显存 {mem['free']:.1f} GiB ≥ 需要 {need} GiB，不用腾", logger)
        return out
    if ollama_unload(logger=logger):
        out["steps"].append("ollama")
        time.sleep(1.5)
        mem = gpu_mem() or mem
        if mem["free"] >= need:
            out["free_after"] = round(mem["free"], 2)
            return out
    if soft_yield is not None:
        try:
            soft_yield()
            out["steps"].append("perception_yield")
            time.sleep(2.0)
            mem = gpu_mem() or mem
        except Exception as exc:  # noqa: BLE001
            _log(f"让位失败：{exc}", logger)
        if mem["free"] >= need:
            out["free_after"] = round(mem["free"], 2)
            return out
    if perception_state() in ("running", "starting") and stop_perception(logger):
        out["stopped_perception"] = True
        out["steps"].append("perception_stop")
        time.sleep(1.5)
        mem = gpu_mem() or mem
    out["free_after"] = round(mem.get("free", 0), 2)
    _log(f"{kind}：腾完空闲 {out['free_after']} GiB（步骤 {out['steps'] or '无'}）", logger)
    return out


def restore(room: dict, *, logger=None) -> None:
    if room and room.get("stopped_perception"):
        try:
            start_perception(logger)
        except Exception as exc:  # noqa: BLE001
            _log(f"重新打开「为她所听」失败：{exc}", logger)


# ------------------------------ 单线程 GPU ------------------------------
class gpu_slot:
    """with gpu_slot("sing", wait_stage=..., logger=...): 独占 GPU 重活。

    wait_stage: 回调，返回 True 表示舞台正在演出——这时先等演出结束（唱歌处理和演出不同时）。
    """

    def __init__(self, kind: str, *, wait_stage=None, logger=None, on_wait=None, max_wait_s: float = 1800):
        self.kind, self.wait_stage, self.logger, self.on_wait, self.max_wait = kind, wait_stage, logger, on_wait, max_wait_s

    def __enter__(self):
        t0 = time.time()
        announced = False
        while self.wait_stage is not None and time.time() - t0 < self.max_wait:
            try:
                busy = bool(self.wait_stage())
            except Exception:  # noqa: BLE001
                busy = False
            if not busy:
                break
            if not announced:
                announced = True
                _log(f"{self.kind}：舞台正在演出，等演完再开始（演出和处理不同时进行）", self.logger)
                if self.on_wait:
                    try:
                        self.on_wait("舞台正在演出，演完后自动开始处理")
                    except Exception:  # noqa: BLE001
                        pass
            time.sleep(2.0)
        _LOCK.acquire()
        _STATE.update(holder=self.kind, since=time.time())
        return self

    def __exit__(self, *exc):
        _STATE.update(holder="", since=0.0)
        _LOCK.release()
        return False


def busy() -> str:
    return str(_STATE.get("holder") or "")


def status() -> dict:
    mem = gpu_mem()
    return {"holder": busy(), "since": _STATE.get("since"), "gpu": mem,
            "ollama": ollama_loaded(), "perception": perception_state(),
            "log": list(_STATE.get("log") or [])[-20:]}
