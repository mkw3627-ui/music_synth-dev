"""Standalone "listen to a song and keep it" listener for 为她而唱.

Audio source:
  * Always the copied per-application WASAPI capture (one process tree only),
    so notifications, video and the AI's own voice never leak in.
  * If 为她所听 (music_perception) is running, its selected application and
    media/lyrics metadata drive the same listener ("linked" mode). It has no
    entry that hands raw isolated PCM to another plugin, so audio is still
    captured here; everything else (which app, is it music/video, title/artist)
    is delegated to it.

The worker cuts one full song per SMTC title change / playback stop and appends
a JSON event. This module turns events into tracks with status="recorded",
keeps only the newest three auto-recorded songs, and honours a pending anchor.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import runtime


def _load_app_channels(workers: Path):
    try:
        spec = importlib.util.spec_from_file_location(
            "ms_app_channels", str(Path(workers) / "app_channels.py"))
        module = importlib.util.module_from_spec(spec)
        # module_from_spec() does not register the module. CPython 3.11's
        # dataclasses resolves deferred annotations through
        # sys.modules[cls.__module__], so @dataclass in app_channels raises
        # AttributeError when loaded this way in the frozen host.
        sys.modules[spec.name] = module  # type: ignore[union-attr]
        try:
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception:
            sys.modules.pop(spec.name, None)  # type: ignore[union-attr]
            raise
        return module
    except Exception:  # noqa: BLE001
        return None


class SongListener:
    SLOT_LIMIT = 3

    def __init__(self, plugin):
        self.plugin = plugin
        self._data = Path(plugin._data_dir) / "listener"
        self._data.mkdir(parents=True, exist_ok=True)
        (self._data / "recordings").mkdir(parents=True, exist_ok=True)
        self._events = self._data / "events.jsonl"
        self._status_path = self._data / "status.json"
        self._stop_flag = self._data / "stop.flag"
        self._manual_off = self._data / "manual_off.flag"
        self._worker_pid_path = self._data / "worker.pid"
        self._perception_song = self._data / "perception_song.json"
        self._perception_song_at = 0.0
        self._thread: threading.Thread | None = None
        self._running = False
        self._pid_created = ("", "")
        self._lock = threading.RLock()
        self._last_error = ""
        self._mode = "standalone"
        self._target: dict = {}
        self._current_track = ""
        self._pending_sing = ""

    # ------------------------------------------------------------------ state
    def status(self) -> dict:
        live = {}
        try:
            worker_state = self._data / "worker_status.json"
            if worker_state.is_file():
                live = json.loads(worker_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            live = {}
        with self._lock:
            return {
                "running": bool(self._running),
                "mode": self._mode,
                "source": "应用声道（逐进程）",
                "target": dict(self._target),
                "live": live if isinstance(live, dict) else {},
                "current_track": self._current_track,
                "pending_sing": self._pending_sing,
                "error": self._last_error,
                "slots": self.SLOT_LIMIT,
            }

    def _write_status(self, **fields) -> None:
        try:
            base = self.status()
            base.update(fields)
            tmp = self._status_path.with_suffix(".tmp.json")
            tmp.write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._status_path)
        except OSError:
            pass

    # ---------------------------------------------------------------- targets
    def _scan_targets(self) -> list[dict]:
        module = _load_app_channels(self.plugin._pipeline.workers)
        if module is None:
            return []
        try:
            rows = module.list_app_processes(limit=200)
        except Exception:  # noqa: BLE001
            return []
        music = set(getattr(module, "MUSIC_APPLICATIONS", set()))
        video = set(getattr(module, "VIDEO_APPLICATIONS", set()))
        browsers = set(getattr(module, "BROWSER_APPLICATIONS", set()))
        out = []
        for row in rows:
            name = str(row.get("name") or "").casefold()
            if name in music:
                out.append({**row, "tag": "music", "label": "音乐播放器"})
            elif name in video:
                out.append({**row, "tag": "video", "label": "视频播放器（不录）"})
            elif name in browsers:
                # 浏览器不预判音乐/视频：只有 SMTC 说在放音乐时才录
                out.append({**row, "tag": "browser", "label": "浏览器（仅当它在放音乐）"})
        return out

    def _resolve_target(self) -> dict:
        """Standalone fallback: first running known music player."""
        self._mode = "standalone"
        rows = [r for r in self._scan_targets() if r.get("tag") == "music"]
        if not rows:
            return {}
        # Prefer an application chosen earlier.
        preferred = str(self.plugin._pipeline.paths.get("raw", {}).get("capture_exe") or "")
        for row in rows:
            if preferred and str(row.get("name") or "").casefold() == preferred.casefold():
                return {**row, "source": "self"}
        return {**rows[0], "source": "self"}

    # ------------------------------------------------------------- lifecycle
    def start(self, pid: int = 0, exe_name: str = "", target: dict | None = None,
              *, autostart: bool = False) -> dict:
        with self._lock:
            if self._running:
                return {"ok": True, "already": True, **self.status()}
            # target is None = autostart: reuse the last choice. An explicit
            # dict (even empty) comes from the plugin and is authoritative.
            if target is None and pid:
                module = _load_app_channels(self.plugin._pipeline.workers)
                try:
                    rows = module.list_app_processes(limit=500, pid_allowlist={int(pid)}) \
                        if module else []
                except Exception:  # noqa: BLE001
                    rows = []
                row = rows[0] if rows else {}
                target = {"pid": int(pid), "created": str(row.get("created") or ""),
                          "name": str(row.get("name") or exe_name or ""),
                          "tag": str(row.get("tag") or "music"), "source": "manual"}
            if target is None:
                try:
                    saved = json.loads((self._data / "last_target.json").read_text(encoding="utf-8"))
                    if isinstance(saved, dict) and saved.get("pid") and saved.get("created"):
                        target = saved
                except (OSError, ValueError):
                    target = None
            if target is None:
                target = self._resolve_target()
            target = dict(target or {})
            # A worker left over from a previous plugin process must not keep
            # recording alongside the new one.
            try:
                old = int(self._worker_pid_path.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                old = 0
            if old > 0 and old != os.getpid():
                runtime.safe_stop_pid(old, log=lambda *_: None)
            self._worker_pid_path.unlink(missing_ok=True)
            if not target or not target.get("created"):
                if not autostart:
                    return {"ok": False, "error": "没有可采集的音乐应用（先打开网易云/QQ音乐/Spotify）"}
                # 插件启动时先进入等待；播放器晚点打开也能自动接上。
                target = {}
            if target.get("tag") == "video":
                return {"ok": False, "error": "当前选中的是视频应用；规则不允许录视频"}
            try:
                self._stop_flag.unlink(missing_ok=True)
                # A song id left over from a previous run would name the first
                # recording of this run after an old track.
                self._perception_song.unlink(missing_ok=True)
                self._events.parent.mkdir(parents=True, exist_ok=True)
                self._events.touch(exist_ok=True)
                (self._data / "autostart.flag").write_text("1", encoding="utf-8")
            except OSError:
                pass
            self._target = target
            self._pid_created = (str(target.get("pid") or ""),
                                 str(target.get("created") or ""))
            self._mode = "linked" if target.get("source") == "music_perception" else "standalone"
            if target:
                try:
                    (self._data / "last_target.json").write_text(
                        json.dumps(target, ensure_ascii=False), encoding="utf-8")
                except OSError:
                    pass
            if not autostart:
                # 用户主动开始：清除「手动关闭」记忆，下次启动继续自动听。
                try:
                    self._manual_off.unlink(missing_ok=True)
                except OSError:
                    pass
            self._last_error = ""
            self._running = True
            self._thread = threading.Thread(target=self._run, name="ms-song-listener", daemon=True)
            self._thread.start()
            return {"ok": True, "mode": self._mode, "target": target}

    def cleanup_stale_worker(self) -> None:
        """Kill a capture worker left over from a previous plugin process.

        A plugin reload does not take child processes with it. When auto-start
        is skipped (for example 为她所听 is listening), start() is never called,
        so without this the old worker would keep capturing alongside it.
        """
        try:
            old = int(self._worker_pid_path.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            old = 0
        if old > 0 and old != os.getpid():
            runtime.safe_stop_pid(old, log=lambda *_: None)
        self._worker_pid_path.unlink(missing_ok=True)
        try:
            (self._data / "worker_status.json").unlink(missing_ok=True)
        except OSError:
            pass

    def stop(self, manual: bool = True) -> dict:
        with self._lock:
            if not self._running:
                return {"ok": True, "already": True}
            self._running = False
            try:
                self._stop_flag.write_text("stop", encoding="utf-8")
                (self._data / "autostart.flag").unlink(missing_ok=True)
                # 只有用户主动关掉才记「不再自动开」；联动切换/让位不算。
                if manual:
                    self._manual_off.write_text("off", encoding="utf-8")
            except OSError:
                pass
            thread = self._thread
        if thread is not None:
            thread.join(timeout=20)
        return {"ok": True, **self.status()}

    # -------------------------------------------------------------- recording
    def _event_offset(self) -> int:
        try:
            return self._events.stat().st_size
        except OSError:
            return 0

    def _read_events(self) -> list[dict]:
        out = []
        try:
            with self._events.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return out

    def _worker_job(self, target: dict, capture: dict, smtc: dict) -> dict:
        workers = self.plugin._pipeline.workers
        raw = self.plugin._pipeline.paths.get("raw") or {}
        return {
            "capture_python": capture["python"],
            "capture_vendor": capture["vendor"],
            "capture_addon": capture["addon"],
            "app_worker": str(workers / "application_loopback_worker.py"),
            "pid": int(target["pid"]),
            "token": str(target["created"]),
            "exe_name": str(target.get("name") or ""),
            "out_dir": str(self._data / "recordings"),
            "events_path": str(self._events),
            "status_path": str(self._data / "worker_status.json"),
            "stop_path": str(self._stop_flag),
            "smtc_python": smtc.get("python", ""),
            "smtc_vendor": smtc.get("vendor", ""),
            "smtc_worker": str(workers / "media_session_worker.py"),
            "perception_song_path": str(self._perception_song),
            "assume_music": str(target.get("tag") or "") == "music",
            "min_song_s": float(raw.get("song_min_s") or 20.0),
            "max_song_s": float(raw.get("song_max_s") or 900.0),
            "preroll_frames": 3,
            "silence_end_frames": 4,
            "smtc_every_frames": 2,
        }

    def _write_perception_song(self) -> None:
        """Publish her current song id so the worker cuts exactly where she did.

        Linked mode must not guess boundaries a second time: 为她所听 already
        owns the source and the boundary decision, so the worker only follows
        its ``current_song_id`` and reuses its title/artist.
        """
        if self._mode != "linked":
            return
        now = time.monotonic()
        if now - self._perception_song_at < 2.0:
            return
        self._perception_song_at = now
        try:
            status = asyncio.run(
                self.plugin._perception_status(timeout=3.0, max_age=2.0))
        except Exception:  # noqa: BLE001 - 她暂时不可达就沿用上一份
            return
        payload = {"song_id": str(status.get("song_id") or ""),
                   "title": str(status.get("song_title") or ""),
                   "artist": str(status.get("song_artist") or ""),
                   # 播放器报的整首时长和当前进度（没有就是 None）：录音线程
                   # 靠这两个数判断「这首歌是不是从头完整放完的」。
                   "duration_s": status.get("song_duration_s"),
                   "position_s": status.get("song_position_s"),
                   "at": time.time()}
        tmp = self._perception_song.with_suffix(".tmp.json")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._perception_song)
        except OSError:
            pass

    def _run(self) -> None:
        seen = self._event_offset()
        failures = 0
        while self._running:
            capture = runtime.locate_app_capture_runtime(
                self.plugin._data_dir, str(self.plugin._pipeline.paths.get("synth_python") or ""))
            if not capture.get("ok"):
                self._last_error = str(capture.get("error") or "采集组件缺失")
                self._write_status(error=self._last_error, state="missing_capture")
                time.sleep(5)
                continue
            smtc = runtime.locate_smtc_runtime(
                self.plugin._data_dir, str(self.plugin._pipeline.paths.get("synth_python") or ""))
            target = dict(self._target)
            # Re-resolve the target when it exited (player restarted).
            module = _load_app_channels(self.plugin._pipeline.workers)
            alive = True
            if module is not None and target:
                try:
                    alive = bool(module.verify_selected_process(
                        int(target["pid"]), str(target.get("created") or "")))
                except Exception:  # noqa: BLE001
                    alive = True
            if not target or not alive:
                target = self._resolve_target()
                if not target:
                    self._write_status(state="waiting_for_source", error="")
                    time.sleep(5)
                    continue
                self._target = target
            job = self._worker_job(target, capture, smtc)
            job_path = self._data / "song_job.json"
            result_path = self._data / "song_result.json"
            job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            log_path = self._data / "listener.log"
            try:
                handle = log_path.open("ab")
                try:
                    proc = subprocess.Popen(
                        [str(self.plugin._pipeline.paths["synth_python"]), "-X", "utf8", "-u",
                         str(self.plugin._pipeline.workers / "app_song_worker.py"),
                         str(job_path), str(result_path)],
                        cwd=str(self._data), env=runtime._clean_env(),
                        stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                finally:
                    handle.close()
            except OSError as exc:
                self._last_error = f"启动录音进程失败：{exc}"
                self._write_status(error=self._last_error, state="failed")
                time.sleep(5)
                continue
            try:
                self._worker_pid_path.write_text(str(proc.pid), encoding="utf-8")
            except OSError:
                pass

            self._write_status(state="recording", target=target, error="")
            while self._running and proc.poll() is None:
                self._write_perception_song()
                events, seen = self._consume_events(seen)
                for event in events:
                    self.plugin._on_song_recorded(event)
                time.sleep(1.0)
            # The worker finalizes the in-progress song (WAV + event) only
            # after it sees stop.flag. Wait for it instead of terminating it
            # straight away, otherwise clicking 停止 silently discards a song.
            if not self._running:
                deadline = time.monotonic() + 15.0
                while proc.poll() is None and time.monotonic() < deadline:
                    events, seen = self._consume_events(seen)
                    for event in events:
                        self.plugin._on_song_recorded(event)
                    time.sleep(0.5)
            # Drain whatever the worker finished while stopping.
            events, seen = self._consume_events(seen)
            for event in events:
                self.plugin._on_song_recorded(event)
            if proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            if not self._running:
                break
            failures += 1
            self._last_error = "录音进程退出，正在重试"
            self._write_status(state="restarting", error=self._last_error)
            time.sleep(min(15, 2 + failures * 2))
        self._write_status(state="stopped", error=self._last_error,
                           title="", seconds=0.0)
        try:
            self._worker_pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        self._running = False

    def _consume_events(self, offset: int) -> tuple[list[dict], int]:
        """Read complete JSON lines after ``offset``; return (events, new_offset)."""
        out = []
        consumed = offset
        try:
            with self._events.open("rb") as fh:
                fh.seek(offset)
                for raw in fh:
                    if not raw.endswith(b"\n"):
                        break
                    consumed += len(raw)
                    try:
                        out.append(json.loads(raw.decode("utf-8")))
                    except (UnicodeError, ValueError):
                        continue
        except OSError:
            return [], offset
        return out, consumed
