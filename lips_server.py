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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ffplay/winsound 起播有 100~200ms 的延迟；把时间轴往前挪一点，口型不会抢拍。
PLAY_LATENCY_S = 0.12
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
}
# 主窗口到底有没有在拉口型：有命中才说明桥真的加载了。
_HITS: dict = {"polls": 0, "last": 0.0, "since": -1, "version": 0}
_SERVER: ThreadingHTTPServer | None = None
_THREAD: threading.Thread | None = None
_LAST_ERROR = ""


def _now() -> float:
    return time.monotonic()


def _note_version(query: dict) -> None:
    """记下网页桥自报的版本号（``?v=13``），用来判断它有没有刷新。"""
    raw = (query or {}).get("v")
    if not raw:
        return
    try:
        version = int(str(raw[0]).split(".")[0])
    except (TypeError, ValueError, IndexError):
        return
    with _LOCK:
        _HITS["version"] = version
        _HITS["version_at"] = time.time()


def _elapsed_locked() -> float:
    if not _STATE["active"] or not _STATE["started"]:
        return 0.0
    return max(0.0, _now() - float(_STATE["started"]) - PLAY_LATENCY_S)


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
            "paused": bool(_STATE["paused"]),
            "latency": PLAY_LATENCY_S,
            "port": _port(),
            "mode": _STATE["mode"],
            "hop": float(_STATE["hop"] or 0.05),
        }
        changed = since is None or int(since) != int(_STATE["seq"])
        # 只有换了歌才带整条轨（歌词口型或干声包络）；同一首的后续轮询只回时间。
        payload["track"] = changed
        payload["cues"] = _STATE["cues"] if changed else None
        payload["env"] = _STATE["env"] if changed else None
        return payload


def set_playing(*, cues=None, env=None, hop: float = 0.05, mode: str = "",
                song: str, track_id: str, duration: float) -> dict:
    """Publish a new lip-sync track (lyric cues and/or vocal envelope) and start its clock."""
    cues = list(cues or [])
    env = list(env or [])
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
        seq = _STATE["seq"]
        active = _STATE["active"]
        mode_out = _STATE["mode"]
    start()
    return {"ok": True, "active": active, "seq": seq,
            "cues": len(cues), "env": len(env), "mode": mode_out}


def clear() -> None:
    with _LOCK:
        _STATE["active"] = False
        _STATE["paused"] = False
        _STATE["cues"] = []
        _STATE["env"] = []
        _STATE["mode"] = ""
        _STATE["song"] = ""


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

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler contract
        parsed = urlparse(self.path)
        route = parsed.path
        # 桥每次请求都带 ?v=<版本号>：记下来就能知道网页那头跑的是不是新版。
        _note_version(parse_qs(parsed.query))
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
            "elapsed": round(_elapsed_locked(), 3),
        }
