"""Standalone lip-sync feed for the main window (为她而唱).

The plugin plays finished songs with ffplay/winsound — outside the app's own
audio pipeline, so ``app-audio-playback.js`` never sees them and the Live2D
mouth does nothing.  This tiny HTTP server publishes "what the character is
singing right now":

    GET /lips.json[?since=<seq>]  -> {ok, active, seq, song, duration,
                                      elapsed, cues}
    GET /hello                    -> diagnostics

``music-groove-bridge.js`` in the main window polls it and writes
``ParamMouthOpenY`` / ``ParamMouthForm`` from the cue that covers *elapsed*.
It is local-only (127.0.0.1) and costs a few KB per request.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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
    "hop": 0.05,
    "mode": "",
    "paused": False,
    "audio": "",        # 浏览器可直接播的地址（/song.wav）；空 = 走 ffplay
    "audio_path": "",   # 实际文件，/song.wav 按 Range 分块发出去
}
# 播放器的真实时钟：ffplay 每 ~30ms 打印一行「<秒> M-A: …」状态，
# 由播放线程喂进来。有它就不再靠墙钟猜，嘴和听到的声音逐帧对齐。
_POS: dict = {"pos": 0.0, "at": 0.0, "live": False, "gen": 0}
# 主窗口到底有没有在拉口型：有命中才说明桥真的加载了。
_HITS: dict = {"polls": 0, "last": 0.0, "since": -1, "version": 0,
               "audio_seq": 0, "audio_state": "",
               # 桥上报的表情诊断：参数掩码、写入帧数、事件是否真的触发、当前叠加值
               "face": "", "face_writes": 0, "face_hooked": -1, "face_values": ""}
_SERVER: ThreadingHTTPServer | None = None
_THREAD: threading.Thread | None = None
_LAST_ERROR = ""


def _now() -> float:
    return time.monotonic()


def _note_version(query: dict) -> None:
    """记下网页桥自报的版本号（``?v=13``）和表情诊断，用来判断它有没有刷新。"""
    raw = (query or {}).get("v")
    face = (query or {}).get("face")
    fv = (query or {}).get("fv")
    if not raw and not face and not fv:
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
        for key, field in (("fh", "face_hooked"), ("fw", "face_writes")):
            item = (query or {}).get(key)
            if not item:
                continue
            try:
                _HITS[field] = int(str(item[0]))
            except (TypeError, ValueError):
                pass


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
        }
        changed = since is None or int(since) != int(_STATE["seq"])
        # 只有换了歌才带整条轨（歌词口型或干声包络）；同一首的后续轮询只回时间。
        payload["track"] = changed
        payload["cues"] = _STATE["cues"] if changed else None
        payload["env"] = _STATE["env"] if changed else None
        # v6 起歌词轨也带包络，桥（g29）拿它调制张口度。旧桥（≤g28）见到 env
        # 会当成「没有歌词，按响度开合」而完全忽略 cue 轨——所以旧客户端不下发，
        # 行为退回 g28 原来的样子，刷新页面后才启用微动作。
        if changed and _STATE["mode"] == "cues" and int(_HITS.get("version") or 0) < 29:
            payload["env"] = []
        return payload


def set_playing(*, cues=None, env=None, hop: float = 0.05, mode: str = "",
                song: str, track_id: str, duration: float,
                audio_path: str = "") -> dict:
    """Publish a new lip-sync track (lyric cues and/or vocal envelope) and start its clock."""
    cues = list(cues or [])
    env = list(env or [])
    audio = ""
    audio_path = str(audio_path or "")
    if audio_path and os.path.isfile(audio_path):
        audio = "/song.wav"
    else:
        audio_path = ""
    with _LOCK:
        _STATE["seq"] = int(_STATE["seq"]) + 1
        _STATE["active"] = bool(cues or env)
        _STATE["song"] = str(song or "")
        _STATE["track_id"] = str(track_id or "")
        _STATE["duration"] = float(duration or 0.0)
        _STATE["cues"] = cues
        _STATE["env"] = env
        _STATE["hop"] = float(hop or 0.05)
        _STATE["mode"] = str(mode or ("cues" if cues else "env"))
        _STATE["paused"] = False
        _STATE["started"] = _now()
        _STATE["audio"] = audio
        _STATE["audio_path"] = audio_path
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
        _STATE["mode"] = ""
        _STATE["song"] = ""
        _STATE["audio"] = ""
        _STATE["audio_path"] = ""
        _POS["live"] = False


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

    def do_OPTIONS(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        self._send(204, b"")

    def _send_audio(self) -> None:
        """把成品 WAV 按 Range 分块发出去，主窗口的 <audio> 边下边播。

        这样声音和嘴在同一个时钟里（``audio.currentTime``），不用再经过
        「另一个进程解码 → 解析进度 → HTTP 轮询」这几跳。
        """
        with _LOCK:
            path = str(_STATE.get("audio_path") or "")
        try:
            size = os.path.getsize(path) if path else 0
        except OSError:
            size = 0
        if not path or size <= 0:
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
        self.send_header("Content-Type", "audio/wav")
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

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        parsed = urlparse(self.path)
        route = parsed.path
        # 桥每次请求都带 ?v=<版本号>：记下来就能知道网页那头跑的是不是新版。
        _note_version(parse_qs(parsed.query))
        if route == "/song.wav":
            self._send_audio()
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
                    "error": _LAST_ERROR,
                }
            self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"))
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
            "elapsed": round(_elapsed_locked(), 3),
        }
