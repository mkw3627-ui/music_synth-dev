"""Per-application song recorder for 为她而唱 (standalone capture).

Launched by ``song_listener.py`` with the *synth* venv Python.  It spawns the
copy of ``application_loopback_worker.py`` using the isolated capture runtime
(managed CPython + proc-tap), reads 48 kHz stereo frames, and cuts a full song
whenever Windows' media session (SMTC) changes track or playback stops.

It writes one WAV per finished song and appends JSON lines to an events file so
the plugin can create a track and honour a "record, then sing" anchor.

No microphone, no system-wide loopback: only the selected process tree.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import time
import traceback
from pathlib import Path

from song_titles import parse_player_window_title

_HEADER = struct.Struct("!cI")
_MAX_PACKET = 2_000_000
_SOFT_CLIP_KNEE = 0.95


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _read_json(path) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_TITLE_CACHE = {"at": 0.0, "pid": 0, "title": ""}


def _window_title(pid: int, *, ttl: float = 3.0) -> str:
    """Title-bar text of the captured process; read-only, best effort.

    Windows never maps a media session to a PID, so for a known player this
    title is the only per-process song evidence. It is a hint, not proof.
    """
    if sys.platform != "win32" or pid <= 0:
        return ""
    now = time.monotonic()
    if _TITLE_CACHE["pid"] == pid and now - _TITLE_CACHE["at"] < ttl:
        return _TITLE_CACHE["title"]
    title = ""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        found = []

        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                      wintypes.LPARAM)

        def _collect(hwnd, _param):
            if found:
                return False
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0 or length > 1024:
                    return True
                buf = ctypes.create_unicode_buffer(260)
                if not user32.GetWindowTextW(hwnd, buf, 260):
                    return True
                owner = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if int(owner.value) == pid:
                    found.append(buf.value.strip())
                    return False
            except (OSError, ValueError, AttributeError):
                return True
            return True

        user32.EnumWindows(EnumProc(_collect), 0)
        title = found[0] if found else ""
    except (OSError, AttributeError, ValueError):
        title = ""
    _TITLE_CACHE.update({"at": now, "pid": pid, "title": title})
    return title



def _read_exact(pipe, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        part = pipe.read(size - len(out))
        if not part:
            raise EOFError(f"capture pipe closed ({len(out)}/{size})")
        out.extend(part)
    return bytes(out)


def _read_packet(pipe):
    tag, length = _HEADER.unpack(_read_exact(pipe, _HEADER.size))
    if tag not in {b"R", b"D", b"E", b"H"} or length > _MAX_PACKET:
        raise RuntimeError(f"invalid capture frame {tag!r}/{length}")
    return tag, _read_exact(pipe, length)


def _apply_gain(audio, *, target_peak: float = 0.707, max_gain: float = 20.0):
    import numpy as np
    arr = np.asarray(audio, dtype=np.float32)
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    if peak <= 1e-6:
        return arr
    gain = min(max_gain, target_peak / peak)
    out = arr if abs(gain - 1.0) < 1e-4 else arr * gain
    over = np.abs(out) > _SOFT_CLIP_KNEE
    if over.any():
        clipped = out[over]
        sign = np.sign(clipped)
        mag = np.abs(clipped)
        soft = _SOFT_CLIP_KNEE + (1.0 - _SOFT_CLIP_KNEE) * np.tanh(
            (mag - _SOFT_CLIP_KNEE) / max(1e-6, 1.0 - _SOFT_CLIP_KNEE))
        out[over] = sign * soft
    return out.astype(np.float32)


def _split_title(text: str) -> tuple[str, str]:
    raw = str(text or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in raw:
            left, _, right = raw.partition(sep)
            if left.strip() and right.strip():
                return right.strip()[:120], left.strip()[:120]
    return raw[:120], ""


class Capture:
    def __init__(self, job: dict):
        self.job = job
        self.proc = None
        self._err_log = None

    def _err_log_path(self) -> Path:
        base = Path(str(self.job.get("out_dir") or ".")) / "capture_stderr.log"
        return base

    def _open_err_log(self):
        """采集子进程的 stderr 不能丢：缺 _native、DLL 缺失这类真因全在 stderr 里。
        实测原始实现用 DEVNULL，用户只能看到「录不到声音」，无从排障。"""
        try:
            path = self._err_log_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 2_000_000:
                path.unlink()
            return open(path, "ab")
        except OSError:
            return None

    def stderr_tail(self, limit: int = 12) -> str:
        try:
            path = self._err_log_path()
            if not path.is_file():
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(text.splitlines()[-limit:])
        except OSError:
            return ""

    def start(self) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        argv = [
            str(self.job["capture_python"]), "-I", "-S",
            str(self.job["app_worker"]),
            str(self.job["capture_vendor"]), str(self.job["capture_addon"]),
            str(int(self.job["pid"])), str(self.job["token"]), "stereo",
        ]
        self._err_log = self._open_err_log()
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=self._err_log if self._err_log is not None else subprocess.DEVNULL,
            creationflags=flags, bufsize=0)
        return None

    def packet(self):
        if self.proc is None or self.proc.stdout is None:
            raise RuntimeError("capture process is not running")
        try:
            return _read_packet(self.proc.stdout)
        except Exception as exc:
            tail = self.stderr_tail()
            detail = f"\n采集子进程 stderr（末尾）:\n{tail}" if tail else ""
            raise RuntimeError(f"{exc}{detail}") from exc

    def alive(self) -> bool:
        return bool(self.proc is not None and self.proc.poll() is None)

    def stop(self) -> None:
        try:
            if self.proc is not None and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=3)
                except Exception:  # noqa: BLE001
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
        finally:
            if self._err_log is not None:
                try:
                    self._err_log.close()
                except OSError:
                    pass
                self._err_log = None


def _smtc(job: dict) -> dict:
    """Read the Windows media session once (best effort, bounded)."""
    python = job.get("smtc_python")
    vendor = job.get("smtc_vendor")
    worker = job.get("smtc_worker")
    if not (python and vendor and worker):
        return {}
    if not (Path(str(python)).is_file() and Path(str(worker)).is_file()):
        return {}
    try:
        proc = subprocess.run(
            [str(python), "-I", "-S", str(worker), str(vendor)],
            capture_output=True, timeout=5.0, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    try:
        data = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except (UnicodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _music_session(job: dict, smtc: dict) -> dict | None:
    """Pick the playing music session; prefer the captured executable's app id."""
    wants = str(job.get("exe_name") or "").strip().casefold()
    stem = wants[:-4] if wants.endswith(".exe") else wants
    rows = [row for row in (smtc.get("sessions") or []) if isinstance(row, dict)]
    rows = [row for row in rows if row.get("playing") is True
            and str(row.get("playback_type") or "").lower() == "music"]
    if not rows:
        return None
    if stem:
        for row in rows:
            app = str(row.get("app") or "").casefold()
            if stem and stem in app:
                return row
    return rows[0]


BROWSER_EXES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                "opera.exe", "vivaldi.exe", "chromium.exe"}


def _target_session(job: dict, smtc: dict) -> dict | None:
    """SMTC session of the captured app, whether playing or paused.

    Chromium reports ``playback_type=unknown`` for web media, and paused
    sessions are absent from ``sessions`` (that list is playing-only). The
    top-level result still carries the current paused session, so it is used
    as a candidate here.
    """
    rows = [row for row in (smtc.get("sessions") or []) if isinstance(row, dict)]
    if (smtc.get("available") and (smtc.get("app") or smtc.get("title"))
            and not any(row.get("app") == smtc.get("app") for row in rows)):
        rows.append({key: smtc.get(key) for key in
                     ("app", "title", "artist", "album", "playing",
                      "playback_type", "position_s", "duration_s")})
    wants = str(job.get("exe_name") or "").strip().casefold()
    stem = wants[:-4] if wants.endswith(".exe") else wants
    if stem:
        for row in rows:
            if stem and stem in str(row.get("app") or "").casefold():
                return row
    return rows[0] if rows else None



def _match_timing(meta: dict, timing: dict) -> tuple[float, float]:
    """把播放器刚报的整首时长/进度对到这首歌上；标题对不上就当没有。

    SMTC 的标题可能是「歌名 - 歌手」，窗口标题/她给的歌名只是其中一段，
    所以两边只要互相包含就算同一首。对不上返回 (0, -1)，让音频兜底。
    """
    row_title = str((timing or {}).get("title") or "").strip().casefold()
    if not row_title:
        return 0.0, -1.0
    title = str((meta or {}).get("title") or "").strip().casefold()
    if title and title not in row_title and row_title not in title:
        return 0.0, -1.0
    try:
        duration = float((timing or {}).get("duration_s") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        position = float((timing or {}).get("position_s"))
    except (TypeError, ValueError):
        position = -1.0
    return duration, position


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    out_dir = Path(job["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = Path(job["events_path"])
    events_path.parent.mkdir(parents=True, exist_ok=True)
    status_path = Path(job["status_path"])
    stop_path = Path(str(job["stop_path"])) if job.get("stop_path") else None
    capture = Capture(job)
    result = {"ok": False, "step": "song_capture"}

    try:
        import numpy as np
        import soundfile as sf
    except Exception as exc:  # noqa: BLE001
        out_path.write_text(json.dumps({"ok": False, "error": f"synth venv 缺少 numpy/soundfile: {exc}"},
                                       ensure_ascii=False), encoding="utf-8")
        return 1

    state = {"state": "waiting", "title": "", "artist": "", "started_at": "",
             "seconds": 0.0, "frames": 0, "songs": 0, "error": "",
             "paused": False, "pid": int(job["pid"]), "mode": "app_channel"}
    try:
        min_s = max(5.0, float(job.get("min_song_s") or 20.0))
        max_s = max(30.0, float(job.get("max_song_s") or 900.0))
        preroll_frames = max(0, min(5, int(job.get("preroll_frames") or 3)))
        silence_end = max(1, int(job.get("silence_end_frames") or 4))
        smtc_every = max(1, int(job.get("smtc_every_frames") or 2))
        sample_rate = 48000
        # 播放/暂停：逐进程采集在播放器暂停时会持续送全零帧，所以全零是最
        # 可靠的暂停证据；正常歌曲的弱奏不会全零，不会误判。
        audible_level = 5e-4
        digital_zero = 1e-6
        pause_frames = 4          # 4 帧 × 2 秒 = 8 秒全零 → 判定暂停
        max_pause_s = 900.0       # 暂停超过 15 分钟就收尾，避免文件一直开着
        rolling = []
        current_sf = None
        current_tmp: Path | None = None
        current_meta = {"title": "", "artist": ""}
        current_frames = 0
        current_started = 0.0
        # 播放器自己报的时长/进度，用来判断这首是不是从头完整放的：
        # expected_s=整首时长，song_pos_first=开始录时的进度，song_pos_last=收尾时
        # 的进度。播放器不提供（网易云等）就留空，由音频兜底判据处理。
        song_expected = 0.0
        song_pos_first = -1.0
        song_pos_last = -1.0
        # 每次轮询 SMTC 得到的最新时长/进度，开新歌时按标题对号入座。
        latest_timing = {"title": "", "duration_s": 0.0, "position_s": -1.0}
        silence_count = 0
        frame_index = 0
        last_status = 0.0
        song_seq = 0
        last_linked_id = ""
        paused = False
        silent_frames = 0
        paused_since = 0.0
        paused_meta = None
        stopped = False

        def write_status(force: bool = False) -> None:
            nonlocal last_status
            now = time.monotonic()
            if not force and now - last_status < 2.0:
                return
            last_status = now
            if current_sf is not None:
                state.update({"state": "paused" if paused else "recording",
                              "paused": bool(paused),
                              "title": current_meta["title"], "artist": current_meta["artist"],
                              "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current_started)),
                              "seconds": round(current_frames / sample_rate, 1)})
            else:
                state["paused"] = False
            tmp = status_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, status_path)

        def _timing_for(meta: dict) -> tuple[float, float]:
            """把 SMTC 刚读到的时长/进度对到这首歌上；标题对不上就当没有。"""
            return _match_timing(meta, latest_timing)

        def open_song(meta: dict, with_preroll: bool = True) -> None:
            nonlocal current_sf, current_tmp, current_meta, current_frames, current_started, song_seq
            nonlocal song_expected, song_pos_first, song_pos_last
            song_seq += 1
            current_meta = dict(meta)
            # 预卷是开录之前就已经放掉的一段，算进起始进度里，不然每首都会
            # 被当成「从第 0 秒开始」；这首歌是不是完整就靠这两个数判断。
            preroll_s = (sum(len(f) for f in rolling) / float(sample_rate)
                         if with_preroll else 0.0)
            song_expected, pos = _timing_for(current_meta)
            song_pos_first = max(0.0, pos - preroll_s) if pos >= 0 else -1.0
            song_pos_last = pos
            current_tmp = out_dir / f"._recording_{os.getpid()}_{song_seq}.wav"
            current_sf = sf.SoundFile(str(current_tmp), mode="w", samplerate=sample_rate,
                                      channels=2, subtype="FLOAT")
            current_frames = 0
            current_started = time.time()
            if with_preroll:
                for frame in rolling:
                    current_sf.write(frame)
                    current_frames += len(frame)
            # 预卷已经归属这一首；不清空会在下一帧被重复写入。
            rolling.clear()

        def flush_pending() -> None:
            """把还没落盘的最近几帧写进当前歌（正常收尾时用）。"""
            nonlocal current_frames
            if current_sf is None:
                return
            while rolling:
                frame = rolling.pop(0)
                current_sf.write(frame)
                current_frames += len(frame)

        def finish_song(reason: str = "", *, keep_pending: bool = False) -> None:
            nonlocal current_sf, current_tmp, current_frames, song_seq
            if current_sf is None:
                return
            if not keep_pending:
                flush_pending()
            try:
                current_sf.close()
            except Exception:  # noqa: BLE001
                pass
            tmp = current_tmp
            current_sf = None
            current_tmp = None
            if tmp is None or not tmp.is_file():
                current_frames = 0
                return
            seconds = current_frames / float(sample_rate)
            if seconds < min_s:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                current_frames = 0
                return
            # Trim trailing silence (we keep writing while SMTC still says
            # playing, so a paused player may leave a quiet tail).
            try:
                data, rate = sf.read(str(tmp), dtype="float32", always_2d=True)
            except Exception as exc:  # noqa: BLE001
                data, rate = None, sample_rate
                state["error"] = f"读取录音失败：{exc}"
            final_path = out_dir / f"song_{time.strftime('%Y%m%d_%H%M%S')}_{song_seq}.wav"
            duration = 0.0
            if data is not None and len(data):
                env = np.max(np.abs(data), axis=1)
                threshold = max(0.0015, float(np.percentile(env, 95)) * 0.02)
                keep = np.where(env > threshold)[0]
                if keep.size:
                    end = int(keep[-1]) + int(rate * 0.4)
                    data = data[:min(len(data), end)]
                data = _apply_gain(data.mean(axis=1, keepdims=True).repeat(2, axis=1)
                                   if data.shape[1] == 1 else data)
                sf.write(str(final_path), data, rate, subtype="PCM_24")
                duration = round(len(data) / float(rate), 2)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            event = {"ok": duration > 0, "path": str(final_path),
                     "title": current_meta.get("title") or "",
                     "artist": current_meta.get("artist") or "",
                     "duration_s": duration, "ended_at": _now(), "reason": reason,
                     "sample_rate": sample_rate,
                     # 播放器报的整首时长 / 这段录音开始与结束时的播放进度，
                     # 后台用它判断「是不是完整放完了」。
                     "expected_s": round(float(song_expected or 0.0), 2),
                     "start_position_s": round(float(song_pos_first or 0.0), 2),
                     "end_position_s": round(float(song_pos_last or 0.0), 2)}
            if not event["ok"]:
                event["error"] = state.get("error") or "录音为空"
            with events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            state["songs"] = int(state.get("songs") or 0) + 1
            current_frames = 0
            write_status(force=True)

        capture.start()
        tag, payload = capture.packet()
        if tag == b"E":
            raise RuntimeError("采集组件报错：" + payload.decode("utf-8", "replace")[:300])
        if tag != b"R":
            raise RuntimeError("采集组件没有发送 READY")
        state.update({"state": "ready", "device": "应用声道（逐进程）"})
        write_status(force=True)

        while True:
            if stop_path is not None and stop_path.exists():
                stopped = True
                break
            if not capture.alive():
                raise RuntimeError("采集进程已退出（目标应用可能关闭）")
            tag, payload = capture.packet()
            if tag == b"E":
                raise RuntimeError("采集组件报错：" + payload.decode("utf-8", "replace")[:300])
            if tag != b"H" or not payload or len(payload) % 8:
                continue
            frame = np.frombuffer(payload, dtype="<f4").reshape(-1, 2).copy()
            frame_index += 1
            level = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
            audible = level > audible_level
            rolling.append(frame)
            if current_sf is None:
                # 还没开始录音：只保留最近几帧做预卷。
                if len(rolling) > preroll_frames:
                    rolling.pop(0)
            elif len(rolling) > preroll_frames:
                # 录音中：最新几帧先不落盘，留给下一首的开头。
                # SMTC/窗口标题是秒级轮询，直接写盘会把新歌开头切进上一首。
                pending = rolling.pop(0)
                if not paused:
                    current_sf.write(pending)
                    current_frames += len(pending)

            # ---------------- 播放 / 暂停 ----------------
            if current_sf is not None:
                if level <= digital_zero:
                    silent_frames += 1
                else:
                    silent_frames = 0
                if not paused and silent_frames >= pause_frames:
                    paused = True
                    paused_since = time.time()
                    paused_meta = None
                    write_status(force=True)
                elif paused and audible:
                    paused = False
                    silent_frames = 0
                    if (paused_meta and paused_meta.get("title")
                            and paused_meta["title"] != current_meta.get("title", "")):
                        # 暂停期间换了歌：旧歌停在暂停点，新歌用预卷接上开头。
                        finish_song("song_changed_while_paused", keep_pending=True)
                        open_song(paused_meta)
                    paused_meta = None
                    paused_since = 0.0
                    write_status(force=True)
                elif (paused and paused_since
                      and time.time() - paused_since > max_pause_s):
                    finish_song("paused")
                    paused = False
                    silent_frames = 0
                    paused_since = 0.0
                    paused_meta = None

            if frame_index % smtc_every == 0:
                smtc = _smtc(job)
                music = _music_session(job, smtc)
                assume_music = bool(job.get("assume_music"))
                if (music is None and assume_music
                        and not (smtc.get("sessions") or [])):
                    # SMTC 整体不可用，但目标应用已被判定为音乐播放器：
                    # 仍然保持录音，靠静音/最长时长兜底。
                    music = {"playing": True, "playback_type": "music",
                             "title": "", "artist": "", "app": ""}
                linked = _read_json(job.get("perception_song_path"))
                linked_id = str(linked.get("song_id") or "")
                linked_meta = {"title": str(linked.get("title") or "").strip()[:120],
                               "artist": str(linked.get("artist") or "").strip()[:120]}
                if linked_id and linked.get("duration_s"):
                    # 她那边知道整首时长/播放进度（同样的播放器接口），
                    # 但系统媒体会话可能读不到，用它兜底。
                    try:
                        latest_timing.update({
                            "title": linked_meta["title"],
                            "duration_s": float(linked.get("duration_s") or 0.0),
                            "position_s": float(linked.get("position_s")
                                                if linked.get("position_s") is not None else -1.0),
                        })
                    except (TypeError, ValueError):
                        pass
                meta = None
                if music:
                    title, artist = _split_title(str(music.get("title") or ""))
                    artist = artist or str(music.get("artist") or "").strip()[:120]
                    meta = {"title": title, "artist": artist}
                if not (meta and meta["title"]) and (assume_music or linked_id):
                    # 这个网易云版本没有系统媒体会话；窗口标题是唯一
                    # 逐进程可用的歌名来源（"歌名 - 歌手"）。
                    parsed = parse_player_window_title(
                        _window_title(int(job["pid"])))
                    if parsed:
                        meta = {"title": parsed[0], "artist": parsed[1]}
                browser_target = (str(job.get("exe_name") or "").casefold()
                                  in BROWSER_EXES)
                if browser_target:
                    # 浏览器的媒体状态由 Windows 官方 SMTC 提供：网页播放器
                    # 会上报标题/歌手/播放状态，且类型通常是 unknown。
                    session = _target_session(job, smtc)
                    if session:
                        session_title = str(session.get("title") or "").strip()
                        if session_title:
                            meta = {"title": session_title[:120],
                                    "artist": str(session.get("artist") or "")[:120]}
                        if session.get("playing") is False:
                            # 官方控件说已暂停：不等静音计数，直接进入暂停态。
                            if current_sf is not None and not paused:
                                paused = True
                                paused_since = time.time()
                                paused_meta = None
                                write_status(force=True)
                        elif session.get("playing") is True and music is None:
                            # Chromium 的 playback_type 不是 music，也接受。
                            music = dict(session)
                row = (session if browser_target and session else None) or music
                if row:
                    try:
                        row_dur = float(row.get("duration_s") or 0.0)
                        row_pos = float(row.get("position_s") or -1.0)
                    except (TypeError, ValueError):
                        row_dur, row_pos = 0.0, -1.0
                    if str(row.get("title") or "") or row_dur > 0:
                        latest_timing.update({"title": str(row.get("title") or ""),
                                              "duration_s": row_dur,
                                              "position_s": row_pos})
                    if current_sf is not None and row_pos >= 0:
                        # 进度只增：播放器偶尔报 0（切歌/读不到）时不能把
                        # 已经记下的结尾进度冲掉。
                        if song_pos_last < 0:
                            song_pos_last = row_pos
                        elif row_pos >= song_pos_last - 1.0:
                            song_pos_last = max(song_pos_last, row_pos)
                        if song_pos_first < 0:
                            song_pos_first = row_pos
                if paused:
                    # 暂停时不切歌：只记下新标题，恢复播放时再接上开头。
                    candidate = linked_meta if linked_id else meta
                    if candidate and candidate.get("title"):
                        paused_meta = dict(candidate)
                    silence_count = 0
                elif linked_id and linked_id != last_linked_id:
                    # 联动：边界完全跟随她的 song id，不再自己猜一遍。
                    same_title = bool(
                        current_sf is not None and linked_meta.get("title")
                        and current_meta.get("title") == linked_meta["title"])
                    last_linked_id = linked_id
                    if same_title:
                        # 她只是刚补上 song id，同一首歌不能切两段。
                        current_meta.update(
                            {k: v for k, v in linked_meta.items() if v})
                    else:
                        if current_sf is not None:
                            finish_song("linked_song_change", keep_pending=True)
                        open_song(linked_meta or meta or {"title": "", "artist": ""})
                    silence_count = 0
                elif linked_id:
                    # 同一首：她的标题晚到时补上名字；切段后继续用她的歌名。
                    if current_sf is None:
                        if audible:
                            open_song(linked_meta or meta or {"title": "", "artist": ""})
                    elif linked_meta["title"] and not current_meta.get("title"):
                        current_meta.update(linked_meta)
                    silence_count = 0
                elif meta and meta["title"]:
                    if current_sf is None:
                        # 只有真的听到声音才开一首，避免“静音歌”。
                        if audible:
                            open_song(meta)
                    elif meta["title"] != current_meta["title"]:
                        # keep_pending: 最近几帧属于新歌开头，不能留在旧文件。
                        finish_song("title_change", keep_pending=True)
                        open_song(meta)
                    elif meta["artist"] and meta["artist"] != current_meta["artist"]:
                        current_meta.update(meta)
                    silence_count = 0
                elif music is not None and audible:
                    # 真正有声音才开始一首；空闲时不再生成“静音歌”。
                    if current_sf is None:
                        open_song(meta or {"title": "", "artist": ""})
                    silence_count = 0
                else:
                    if current_sf is not None:
                        silence_count += 1
                        if silence_count >= silence_end:
                            finish_song("playback_stopped")
                            silence_count = 0

            if current_sf is not None and not paused:
                if level < 0.0015:
                    silence_count += 1
                    if silence_count >= max(silence_end * 2, 8):
                        finish_song("silence")
                        silence_count = 0
                else:
                    silence_count = 0
                if current_frames / float(sample_rate) >= max_s:
                    finish_song("max_length")
            write_status()

        finish_song("stopped")
        state.update({"state": "stopped", "title": "", "artist": "", "seconds": 0.0})
        write_status(force=True)
        result = {"ok": True, "songs": state.get("songs", 0), "stopped": stopped}
    except Exception as exc:  # noqa: BLE001
        try:
            finish_song("capture_lost")  # type: ignore[name-defined]
        except Exception:  # noqa: BLE001
            pass
        state["error"] = f"{type(exc).__name__}: {exc}"
        state["state"] = "failed"
        try:
            write_status(force=True)  # type: ignore[name-defined]
        except Exception:  # noqa: BLE001
            pass
        result = {"ok": False, "error": state["error"], "trace": traceback.format_exc()[-2000:]}
    finally:
        try:
            capture.stop()
        except Exception:  # noqa: BLE001
            pass
        out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
