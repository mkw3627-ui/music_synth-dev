"""Standalone lip-sync feed for the main window (为她而唱).

The plugin plays finished songs with ffplay/winsound — outside the app's own
audio pipeline, so ``app-audio-playback.js`` never sees them and the Live2D
mouth does nothing.  This tiny HTTP server publishes "what the character is
singing right now":

    GET /lips.json[?since=<seq>]  -> {ok, active, seq, song, duration,
                                      elapsed, cues}
    GET /hello                    -> diagnostics
    GET /show.json[?track=<id>]   -> 离线编排产出的 show.json（当前曲目，或指定曲目）
    GET /stage/<file>             -> 只读静态资源（舞台页/字体），根限定在
                                     static/stage/ 与 static/fonts/，拒绝穿越

``music-groove-bridge.js`` in the main window polls it and writes
``ParamMouthOpenY`` / ``ParamMouthForm`` from the cue that covers *elapsed*.
It is local-only (127.0.0.1) and costs a few KB per request.

``/show.json`` 和 ``/stage/`` 是给全景舞台页用的：舞台页跨端口（主窗口端口）
取这两样，所以必须带 ``Access-Control-Allow-Origin: *``；两者都只读、只 GET。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# 没有真实播放位置时的兜底：按墙钟推算，再往前挪一点抵消起播延迟。
# ffplay 起播后会持续回报解码器时钟（见 set_position），那时用真时钟，误差 <10ms。
PLAY_LATENCY_S = 0.05
PORT_FIRST = 48766
PORT_TRIES = 5

_LOCK = threading.Lock()
_STATE: dict = {
    "active": False,
    "seq": 0,
    "song": "",
    "track_id": "",
    "duration": 0.0,
    "started": 0.0,
    "cues": [],
    "env": [],
    "moods": [],
    "hop": 0.05,
    "mode": "",
    "paused": False,
    "audio": "",        # 浏览器可直接播的地址（/song.wav）；空 = 走 ffplay
    "audio_path": "",   # 实际文件，/song.wav 按 Range 分块发出去
    "stem_path": "",    # 干净人声（干声）文件，/stem.wav 给桥做实时音高分析
}
# 播放器的真实时钟：ffplay 每 ~30ms 打印一行「<秒> M-A: …」状态，
# 由播放线程喂进来。有它就不再靠墙钟猜，嘴和听到的声音逐帧对齐。
_POS: dict = {"pos": 0.0, "at": 0.0, "live": False, "gen": 0}
# 主窗口到底有没有在拉口型：有命中才说明桥真的加载了。
_HITS: dict = {"polls": 0, "last": 0.0, "since": -1, "version": 0,
               "audio_seq": 0, "audio_state": "",
               # 桥上报的表情诊断：参数掩码、写入帧数、事件是否真的触发、当前叠加值
               "face": "", "face_writes": 0, "face_hooked": -1, "face_values": "",
               # g64：贴图特效（脸红/流泪/花花/脸黑）的实际写入量，排查用
               "fx": "",
               # 干声实时分析（音高/颤音）与伴奏段落：桥每轮都带，方便排查
               "vox": 0, "vox_energy": 0.0, "vox_f0": 0, "sect": ""}
_SERVER: ThreadingHTTPServer | None = None
_THREAD: threading.Thread | None = None
_LAST_ERROR = ""

# ---- 全景舞台的只读资源 -------------------------------------------------
# show.json 不走「播放中」状态：它是曲目工件，换歌时用 set_show 登记当前曲目，
# 也可以随时用 ?track=<id> 点播。路径都限制在插件的 work 目录里。
_SHOWS: dict[str, str] = {}      # track_id -> show.json 绝对路径
_SHOW_ROOT = ""                  # <work>；兜底找 <work>/tracks/<id>/show.json
_STATIC_ROOTS: list[str] = []    # static/stage、static/fonts（绝对路径）
# 试听缓存：放在插件数据目录的 preview/ 下，不放插件安装目录。
# 插件目录里的文件在宿主升级替换时可能被占用，导致「替换文件」失败。
_PREVIEW_ROOT = ""
_TRACK_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MIME = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".webmanifest": "application/manifest+json",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mp4": "video/mp4",
}


def _now() -> float:
    return time.monotonic()


def _note_version(query: dict) -> None:
    """记下网页桥自报的版本号（``?v=13``）和表情诊断，用来判断它有没有刷新。"""
    raw = (query or {}).get("v")
    face = (query or {}).get("face")
    fv = (query or {}).get("fv")
    vox = (query or {}).get("vx")
    ve = (query or {}).get("ve")
    vf0 = (query or {}).get("vf0")
    msc = (query or {}).get("msc")
    if not raw and not face and not fv and not vox and not ve and not vf0 and not msc:
        return
    with _LOCK:
        if raw:
            try:
                version = int(str(raw[0]).split(".")[0])
            except (TypeError, ValueError, IndexError):
                version = None
            if version is not None:
                _HITS["version"] = version
                _HITS["version_at"] = time.time()
        if face:
            _HITS["face"] = str(face[0])[:24]
        if fv:
            _HITS["face_values"] = str(fv[0])[:60]
        # 干声分析（vox）和伴奏段落（sect）：桥每轮轮询都带，唱的时候能看到
        # 基频/响度有没有真的在动——不动就说明 /stem.wav 没挂上或被静音了。
        for item, field, cast in ((vox, "vox", int), (ve, "vox_energy", float),
                                  (vf0, "vox_f0", int)):
            if not item:
                continue
            try:
                value = cast(float(str(item[0])))
                _HITS[field] = round(value, 2) if field == "vox_energy" else value
            except (TypeError, ValueError):
                pass
        if msc:
            _HITS["sect"] = str(msc[0])[:12]
        # g64：贴图特效的实时值（脸红,眼泪,花花,脸黑），只做展示。
        fx = (query or {}).get("fx")
        if fx:
            _HITS["fx"] = str(fx[0])[:40]
        for key, field in (("fh", "face_hooked"), ("fw", "face_writes")):
            item = (query or {}).get(key)
            if not item:
                continue
            try:
                _HITS[field] = int(str(item[0]))
            except (TypeError, ValueError):
                pass
        # 情绪→身体（桥 g47）：bs 是身体参数支持表（BodyX/Y/Z、呼吸、三束头发），
        # bd 是本帧真正叠上去的偏移 x,y,z,呼吸。只做展示，不参与任何判定。
        # ey/ed 是眼睛探针（桥 g50）：ey=相邻帧最大跳变 + 调用次数，
        # ed=眼开合/笑眼/眼球的「当前值[区间]默认值」+ 主程序眨眼器配置 + 常驻表情名。
        # g55：ey 里多带口型统计（|lp 时钟跳变/限速次数/响度/秒数），
        # ed 里多带嘴和头的绝对值——ed 的截断长度从 300 提到 460，只是显示用。
        for key, field, limit in (("bs", "body_support", 16), ("bd", "body", 40),
                                  ("ey", "eye_probe", 200),
                                  ("ed", "eye_dump", 460)):
            item = (query or {}).get(key)
            if item:
                _HITS[field] = str(item[0])[:limit]


POS_FRESH_S = 0.6   # 真时钟超过这么久没更新就退回墙钟（播放器挂了/管道断了）


def begin_audio_clock() -> int:
    """新一轮播放：清掉上一个进程的时钟，返回代号防止旧进程的尾巴串台。"""
    with _LOCK:
        _POS["gen"] = int(_POS.get("gen") or 0) + 1
        _POS["live"] = False
        _POS["pos"] = 0.0
        _POS["at"] = 0.0
        return int(_POS["gen"])


def set_position(pos: float, gen: int = 0) -> None:
    """播放线程喂进来的真实播放位置（秒）——ffplay 解码器时钟。"""
    try:
        value = max(0.0, float(pos))
    except (TypeError, ValueError):
        return
    with _LOCK:
        if gen and gen != int(_POS.get("gen") or 0):
            return
        _POS["pos"] = value
        _POS["at"] = _now()
        _POS["live"] = True


def end_audio_clock(gen: int = 0) -> None:
    """播放进程结束：停止使用它的时钟。"""
    with _LOCK:
        if gen and gen != int(_POS.get("gen") or 0):
            return
        _POS["live"] = False


def _elapsed_locked() -> float:
    if not _STATE["active"] or not _STATE["started"]:
        return 0.0
    now = _now()
    if _POS["live"]:
        age = now - float(_POS["at"] or 0.0)
        if age <= POS_FRESH_S:
            return max(0.0, float(_POS["pos"]) + age)
    return max(0.0, now - float(_STATE["started"]) - PLAY_LATENCY_S)


def _clock_kind_locked() -> str:
    if _POS["live"] and (_now() - float(_POS["at"] or 0.0)) <= POS_FRESH_S:
        return "audio"
    return "wall"


def snapshot(since: int | None = None) -> dict:
    with _LOCK:
        elapsed = _elapsed_locked()
        duration = float(_STATE["duration"] or 0.0)
        active = bool(_STATE["active"])
        if active and duration > 0 and elapsed > duration + 0.5:
            # 播放已经自然结束（winsound 没有进程对象可等）。
            _STATE["active"] = active = False
        payload = {
            "ok": True,
            "active": active,
            "seq": int(_STATE["seq"]),
            "song": _STATE["song"],
            "track_id": _STATE["track_id"],
            "duration": round(duration, 3),
            "elapsed": round(elapsed, 3),
            "clock": _clock_kind_locked(),
            "paused": bool(_STATE["paused"]),
            "latency": PLAY_LATENCY_S,
            "port": _port(),
            "mode": _STATE["mode"],
            "hop": float(_STATE["hop"] or 0.05),
            "audio": _STATE["audio"],
            # 干净人声的地址：桥用它做实时 F0/音色分析（长音微开合 +
            # 表情的「人声亮度」）。没有干净人声时为空，桥退回旧行为。
            "stem": "/stem.wav" if _STATE["stem_path"] else "",
            # 全景舞台：这首歌有没有编排好的 show.json（有就给绝对路径的入口）。
            "show": "/show.json" if _lookup_show_locked(
                str(_STATE.get("track_id") or "")) else "",
        }
        changed = since is None or int(since) != int(_STATE["seq"])
        # 只有换了歌才带整条轨（歌词口型或干声包络）；同一首的后续轮询只回时间。
        payload["track"] = changed
        payload["cues"] = _STATE["cues"] if changed else None
        payload["env"] = _STATE["env"] if changed else None
        payload["moods"] = _STATE["moods"] if changed else None
        # v6 起歌词轨也带包络，桥（g29）拿它调制张口度。旧桥（≤g28）见到 env
        # 会当成「没有歌词，按响度开合」而完全忽略 cue 轨——所以旧客户端不下发，
        # 行为退回 g28 原来的样子，刷新页面后才启用微动作。
        if changed and _STATE["mode"] == "cues" and int(_HITS.get("version") or 0) < 29:
            payload["env"] = []
        return payload


def set_playing(*, cues=None, env=None, moods=None, hop: float = 0.05, mode: str = "",
                song: str, track_id: str, duration: float,
                audio_path: str = "", stem_path: str = "") -> dict:
    """Publish a new lip-sync track (lyric cues and/or vocal envelope) and start its clock."""
    cues = list(cues or [])
    env = list(env or [])
    moods = list(moods or [])
    audio = ""
    audio_path = str(audio_path or "")
    if audio_path and os.path.isfile(audio_path):
        audio = "/song.wav"
    else:
        audio_path = ""
    stem_path = str(stem_path or "")
    if stem_path and not os.path.isfile(stem_path):
        stem_path = ""
    with _LOCK:
        _STATE["seq"] = int(_STATE["seq"]) + 1
        _STATE["active"] = bool(cues or env)
        _STATE["song"] = str(song or "")
        _STATE["track_id"] = str(track_id or "")
        _STATE["duration"] = float(duration or 0.0)
        _STATE["cues"] = cues
        _STATE["env"] = env
        _STATE["moods"] = moods
        _STATE["hop"] = float(hop or 0.05)
        _STATE["mode"] = str(mode or ("cues" if cues else "env"))
        _STATE["paused"] = False
        _STATE["started"] = _now()
        _STATE["audio"] = audio
        _STATE["audio_path"] = audio_path
        _STATE["stem_path"] = stem_path
        _HITS["audio_seq"] = 0
        _HITS["audio_state"] = ""
        seq = _STATE["seq"]
        active = _STATE["active"]
        mode_out = _STATE["mode"]
    start()
    return {"ok": True, "active": active, "seq": seq,
            "cues": len(cues), "env": len(env), "mode": mode_out,
            "audio": bool(audio)}


def drop_audio() -> None:
    """退回 ffplay 播放：别再让网页那头去拉音频，免得两路声音一起响。"""
    with _LOCK:
        _STATE["audio"] = ""
        _STATE["audio_path"] = ""


def audio_state(seq: int) -> str:
    """网页那头的播放状态：``playing`` / ``failed`` / ``""``（还没回应）。"""
    with _LOCK:
        if int(seq or 0) != int(_HITS.get("audio_seq") or 0):
            return ""
        return str(_HITS.get("audio_state") or "")


def clear() -> None:
    with _LOCK:
        _STATE["active"] = False
        _STATE["paused"] = False
        _STATE["cues"] = []
        _STATE["env"] = []
        _STATE["moods"] = []
        _STATE["mode"] = ""
        _STATE["song"] = ""
        _STATE["audio"] = ""
        _STATE["audio_path"] = ""
        _STATE["stem_path"] = ""
        _POS["live"] = False


def set_show_root(root: str | os.PathLike) -> None:
    """登记 show.json 的根（插件 work 目录）；``?track=<id>`` 靠它兜底找文件。"""
    global _SHOW_ROOT
    try:
        _SHOW_ROOT = str(Path(root).expanduser()) if str(root or "").strip() else ""
    except (OSError, ValueError):
        _SHOW_ROOT = ""


def set_static_roots(*roots: str | os.PathLike) -> None:
    """登记舞台静态资源的根（static/stage、static/fonts）。只读，不能穿越出去。"""
    cleaned: list[str] = []
    for root in roots:
        text = str(root or "").strip()
        if not text:
            continue
        try:
            cleaned.append(str(Path(text).resolve()))
        except (OSError, ValueError):
            continue
    with _LOCK:
        _STATIC_ROOTS[:] = cleaned


def set_preview_root(path: str | os.PathLike) -> None:
    """登记试听缓存目录（插件数据目录下的 preview/）。只读、只发单层文件名。"""
    global _PREVIEW_ROOT
    try:
        text = str(Path(path).expanduser().resolve()) if str(path or "").strip() else ""
    except (OSError, ValueError):
        text = ""
    with _LOCK:
        _PREVIEW_ROOT = text


def base_url() -> str:
    """当前口型服务的根地址；没起来时返回空串。"""
    port = _port()
    return f"http://127.0.0.1:{port}" if port else ""


def set_show(track_id: str, path: str | os.PathLike) -> dict:
    """登记某首歌的 show.json（换歌时调一次，/show.json 直接发它）。"""
    tid = str(track_id or "").strip()
    text = str(path or "").strip()
    with _LOCK:
        if not tid:
            return {"ok": False, "error": "没有 track_id"}
        if not text:
            _SHOWS.pop(tid, None)
            return {"ok": True, "track_id": tid, "show": ""}
        _SHOWS[tid] = text
        exists = os.path.isfile(text)
    return {"ok": True, "track_id": tid, "show": text if exists else "",
            "exists": exists}


def _lookup_show_locked(track_id: str) -> str:
    """（持锁调用）曲目 → 可用的 show.json 路径；没有就空串。"""
    tid = str(track_id or "").strip()
    if not tid:
        return ""
    path = _SHOWS.get(tid) or ""
    if path and os.path.isfile(path):
        return path
    if _SHOW_ROOT:
        # 兜底：<work>/tracks/<id>/show.json（离线生成时的固定位置）。
        cand = os.path.join(_SHOW_ROOT, "tracks", tid, "show.json")
        if os.path.isfile(cand):
            return cand
    return ""


def show_path(track_id: str = "") -> str:
    """当前（或指定）曲目的 show.json 路径；没有返回空串。"""
    with _LOCK:
        tid = str(track_id or "").strip() or str(_STATE.get("track_id") or "")
        if tid and not _TRACK_RE.match(tid):
            return ""
        return _lookup_show_locked(tid)


def _port() -> int:
    return int(getattr(_SERVER, "server_port", 0) or 0)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401 - keep the plugin log quiet
        return

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 主窗口是同源外的 http://127.0.0.1:<app port>，需要放开 CORS。
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _send_blob(self, code: int, body: bytes, content_type: str) -> None:
        """非 JSON 的只读响应（show.json / 舞台静态资源），同样放开 CORS。"""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _serve_show(self, query: dict) -> None:
        ask = str((query.get("track") or [""])[0] or "").strip()
        with _LOCK:
            tid = ask if (ask and _TRACK_RE.match(ask)) else ""
            if not ask:
                tid = str(_STATE.get("track_id") or "")
            path = _lookup_show_locked(tid)
        if not path:
            self._send(404, b'{"ok": false, "error": "no show"}')
            return
        try:
            body = Path(path).read_bytes()
        except OSError:
            self._send(404, b'{"ok": false, "error": "show unreadable"}')
            return
        self._send_blob(200, body, "application/json; charset=utf-8")

    def _serve_stage(self, rel: str) -> None:
        """``/stage/<file>``：只发 static/stage 和 static/fonts 里的文件。"""
        decoded = unquote(str(rel or ""))
        parts = decoded.split("/")
        if (not decoded or "\x00" in decoded or "\\" in decoded
                or decoded.startswith("/")
                or any(p in ("", ".", "..") for p in parts)
                or re.match(r"^[A-Za-z]:", decoded)):
            self._send(403, b'{"ok": false, "error": "bad path"}')
            return
        with _LOCK:
            roots = list(_STATIC_ROOTS)
        target = ""
        for root in roots:
            try:
                base = Path(root).resolve()
                cand = (base / decoded).resolve()
            except (OSError, ValueError):
                continue
            if cand == base or base not in cand.parents:
                continue
            if cand.is_file():
                target = str(cand)
                break
        if not target:
            self._send(404, b'{"ok": false, "error": "not found"}')
            return
        try:
            body = Path(target).read_bytes()
        except OSError:
            self._send(404, b'{"ok": false, "error": "unreadable"}')
            return
        ctype = _MIME.get(Path(target).suffix.lower(), "application/octet-stream")
        self._send_blob(200, body, ctype)

    def do_OPTIONS(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        self._send(204, b"")

    def _send_audio(self, state_key: str = "audio_path") -> None:
        """把成品 WAV / 干声 WAV 按 Range 分块发出去，主窗口的 <audio> 边下边播。

        这样声音和嘴在同一个时钟里（``audio.currentTime``），不用再经过
        「另一个进程解码 → 解析进度 → HTTP 轮询」这几跳。干声另走
        ``/stem.wav``，只给桥做音高/音色分析，不接到扬声器。
        """
        with _LOCK:
            path = str(_STATE.get(state_key) or "")
        try:
            size = os.path.getsize(path) if path else 0
        except OSError:
            size = 0
        if not path or size <= 0:
            self._send(404, b'{"ok": false, "error": "no audio"}')
            return
        self._send_file(path, "audio/wav", size)

    def _send_file(self, path: str, content_type: str, size: int | None = None) -> None:
        """按 Range 分块发一个本地文件（成品/干声 WAV、试听缓存）。"""
        if size is None:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
        if size <= 0:
            self._send(404, b'{"ok": false, "error": "no audio"}')
            return
        start, end = 0, size - 1
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            try:
                spec = rng[6:].split(",")[0]
                lo, _, hi = spec.partition("-")
                if lo:
                    start = int(lo)
                    end = int(hi) if hi else size - 1
                elif hi:                       # bytes=-N：最后 N 字节
                    start = max(0, size - int(hi))
            except (TypeError, ValueError):
                start, end = 0, size - 1
        start = max(0, min(start, size - 1))
        end = max(start, min(end, size - 1))
        length = end - start + 1
        partial = bool(rng)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        remaining = length
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            pass

    def _serve_preview(self, rel: str) -> None:
        """``/preview/<file>``：只发数据目录 preview/ 里的试听缓存（单层文件名）。"""
        name = unquote(str(rel or ""))
        if (not name or "/" in name or "\\" in name or "\x00" in name
                or name in (".", "..") or re.match(r"^[A-Za-z]:", name)):
            self._send(403, b'{"ok": false, "error": "bad path"}')
            return
        with _LOCK:
            root = _PREVIEW_ROOT
        if not root:
            self._send(404, b'{"ok": false, "error": "no preview root"}')
            return
        try:
            base = Path(root).resolve()
            cand = (base / name).resolve()
        except (OSError, ValueError):
            self._send(404, b'{"ok": false, "error": "not found"}')
            return
        if cand.parent != base or not cand.is_file():
            self._send(404, b'{"ok": false, "error": "not found"}')
            return
        ctype = _MIME.get(cand.suffix.lower(), "application/octet-stream")
        self._send_file(str(cand), ctype)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        parsed = urlparse(self.path)
        route = parsed.path
        # 桥每次请求都带 ?v=<版本号>：记下来就能知道网页那头跑的是不是新版。
        _note_version(parse_qs(parsed.query))
        if route == "/song.wav":
            self._send_audio("audio_path")
            return
        if route == "/stem.wav":
            self._send_audio("stem_path")
            return
        if route in ("/audio_play", "/audio_fail"):
            query = parse_qs(parsed.query)
            try:
                seq = int((query.get("seq") or ["0"])[0])
            except (TypeError, ValueError):
                seq = 0
            with _LOCK:
                _HITS["audio_seq"] = seq
                _HITS["audio_state"] = "playing" if route == "/audio_play" else "failed"
            self._send(200, b'{"ok": true}')
            return
        if route in ("/lips.json", "/state", "/lips"):
            query = parse_qs(parsed.query)
            since = None
            if query.get("since"):
                try:
                    since = int(query["since"][0])
                except (TypeError, ValueError):
                    since = None
            with _LOCK:
                _HITS["polls"] = int(_HITS["polls"]) + 1
                _HITS["last"] = time.time()
                _HITS["since"] = int(_STATE["seq"])
            self._send(200, json.dumps(snapshot(since), ensure_ascii=False).encode("utf-8"))
            return
        if route == "/hello":
            with _LOCK:
                body = {
                    "ok": True,
                    "active": bool(_STATE["active"]),
                    "seq": int(_STATE["seq"]),
                    "cues": len(_STATE["cues"]),
                    "env": len(_STATE["env"]),
                    "mode": _STATE["mode"],
                    "song": _STATE["song"],
                    "polls": int(_HITS["polls"]),
                    "polled_s_ago": round(time.time() - float(_HITS["last"]), 1)
                    if _HITS["last"] else None,
                    "client_version": int(_HITS.get("version") or 0),
                    "face": _HITS.get("face") or "",
                    "face_writes": int(_HITS.get("face_writes") or 0),
                    "face_hooked": int(_HITS.get("face_hooked", -1)),
                    "face_values": _HITS.get("face_values") or "",
                    "fx": _HITS.get("fx") or "",
                    "body_support": _HITS.get("body_support") or "",
                    "body_values": _HITS.get("body") or "",
                    "eye_probe": _HITS.get("eye_probe") or "",
                    "eye_dump": _HITS.get("eye_dump") or "",
                    # 干声实时分析：vox=1 表示桥已建起 analyser；ve/vf0 是当前
                    # 响度和基频（唱的时候应该在动）；sect 是伴奏段落。
                    "vox": int(_HITS.get("vox") or 0),
                    "vox_energy": float(_HITS.get("vox_energy") or 0.0),
                    "vox_f0": int(_HITS.get("vox_f0") or 0),
                    "sect": _HITS.get("sect") or "",
                    "stem": bool(_STATE["stem_path"]),
                    # 全景舞台：当前曲目有没有 show.json、登记了几个静态根。
                    "show": bool(_lookup_show_locked(str(_STATE.get("track_id") or ""))),
                    "show_track": str(_STATE.get("track_id") or ""),
                    "static_roots": len(_STATIC_ROOTS),
                    "error": _LAST_ERROR,
                }
            self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))
            return
        if route == "/show.json":
            self._serve_show(parse_qs(parsed.query))
            return
        if route.startswith("/stage/"):
            self._serve_stage(route[len("/stage/"):])
            return
        if route.startswith("/preview/"):
            self._serve_preview(route[len("/preview/"):])
            return
        self._send(404, b'{"ok": false, "error": "not found"}')


def start() -> dict:
    """Bind the local feed server (idempotent)."""
    global _SERVER, _THREAD, _LAST_ERROR
    with _LOCK:
        if _SERVER is not None:
            return {"ok": True, "port": _port(), "reused": True}
    last_exc = ""
    for offset in range(PORT_TRIES):
        port = PORT_FIRST + offset
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        except OSError as exc:
            last_exc = str(exc)
            continue
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever,
                                  name="music_synth-lips", daemon=True)
        thread.start()
        with _LOCK:
            _SERVER = server
            _THREAD = thread
            _LAST_ERROR = ""
        return {"ok": True, "port": port}
    with _LOCK:
        _LAST_ERROR = last_exc or "端口被占用"
    return {"ok": False, "error": _LAST_ERROR}


def stop() -> None:
    global _SERVER, _THREAD
    with _LOCK:
        server, _SERVER, _THREAD = _SERVER, None, None
    clear()
    if server is not None:
        try:
            server.shutdown()
            server.server_close()
        except Exception:  # noqa: BLE001 - best effort on plugin unload
            pass


def status() -> dict:
    with _LOCK:
        return {
            "ok": _SERVER is not None,
            "port": _port(),
            "error": _LAST_ERROR,
            "active": bool(_STATE["active"]),
            "song": _STATE["song"],
            "cues": len(_STATE["cues"]),
            "env": len(_STATE["env"]),
            "mode": _STATE["mode"],
            "polls": int(_HITS["polls"]),
            "client_version": int(_HITS.get("version") or 0),
            "face": _HITS.get("face") or "",
            "face_writes": int(_HITS.get("face_writes") or 0),
            "face_hooked": int(_HITS.get("face_hooked", -1)),
            "face_values": _HITS.get("face_values") or "",
            "fx": _HITS.get("fx") or "",
            "body_support": _HITS.get("body_support") or "",
            "body_values": _HITS.get("body") or "",
            "eye_probe": _HITS.get("eye_probe") or "",
            "eye_dump": _HITS.get("eye_dump") or "",
            "vox": int(_HITS.get("vox") or 0),
            "vox_energy": float(_HITS.get("vox_energy") or 0.0),
            "vox_f0": int(_HITS.get("vox_f0") or 0),
            "sect": _HITS.get("sect") or "",
            "stem": bool(_STATE["stem_path"]),
            "show": bool(_lookup_show_locked(str(_STATE.get("track_id") or ""))),
            "show_track": str(_STATE.get("track_id") or ""),
            "static_roots": len(_STATIC_ROOTS),
            "elapsed": round(_elapsed_locked(), 3),
        }
