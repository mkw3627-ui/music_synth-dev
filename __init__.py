"""为她而唱 · 全自动唱歌模块.

听歌 → 识歌 → 人声分离 → DDSP-SVC 变声（换音色）→ 混音 → 面板出曲 → AI 可调用演唱。

核心链路基于 DDSP-SVC 6.3（RectifiedFlow 变声）：把原唱人声分离出来，用训练好的
变声模型换成指定音色，再和伴奏混回去。训练通过 ms_train 入口在后台跑 reflow。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from plugin.sdk.plugin import (
    NekoPluginBase,
    Ok,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

from . import (
    cache_cleanup,
    lips_server,
    lipsync,
    model_package,
    runtime,
    vram_policy,
    vram_release,
)
from . import setup as setup_mod
from .pipeline import STEP_ORDER as STEP_ORDER
from .pipeline import Pipeline
from .song_listener import SongListener

PLUGIN_ID = "music_synth"


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开", "是"}
    return default


def _as_float(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def _int_setting(raw: dict, key: str, default: int, low: int, high: int) -> int:
    """读取 config.json 里的整数项；坏值/越界一律回到默认值。"""
    try:
        number = int(float((raw or {}).get(key)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _float_setting(raw: dict, key: str, default: float, low: float, high: float) -> float:
    try:
        number = float((raw or {}).get(key))
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


_PLACEHOLDER_TITLE = re.compile(r"^(未命名歌曲(?:\s*\d+)?|歌曲\s*\d+)$", re.IGNORECASE)


def _is_placeholder_title(text, fallback: str = "") -> bool:
    """Placeholder names must never overwrite a real title already on a track."""
    value = str(text or "").strip()
    if not value:
        return True
    if _PLACEHOLDER_TITLE.match(value):
        return True
    if fallback and value.lower() == Path(str(fallback)).stem.strip().lower():
        return True          # bare file name stem, e.g. "sample" / "rec_001"
    return False


def _title_writable(track: dict, source: str, incoming: str = "") -> bool:
    """Can identity ``source`` replace this track's current title?

    - a placeholder / empty title may always be replaced;
    - a user-edited title is never touched by SMTC;
    - a weak SMTC read (video/unknown session) may not overwrite SMTC or a
      title the user changed;
    - strong SMTC keeps the old behaviour (title follows the playing song).
    """
    current = str((track or {}).get("title") or "").strip()
    if (track or {}).get("title_user_edited"):
        return False
    if _is_placeholder_title(current, (track or {}).get("source") or ""):
        return True
    if source == "smtc_weak":
        origin = str((track or {}).get("title_source") or "")
        if origin in ("smtc", "user") or incoming is None:
            return False
    return True


# ffplay 的进度行：`   12.34 M-A:  0.000 fd= ...`（用 \r 覆盖同一行）。
# 第一个数是主时钟＝当前播放位置（音频为主时钟时就是解码器时钟）。
_FFPLAY_POS_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+M-A:")
_FFPLAY_BUF_MAX = 8192


def _pump_ffplay_clock(pipe, gen: int) -> None:
    """把 ffplay 的实时播放位置喂给 lips_server（后台线程，随进程退出结束）。"""
    buf = ""
    try:
        while True:
            chunk = pipe.read(256)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            parts = re.split(r"[\r\n]", buf)
            buf = parts.pop() if parts else ""
            if len(buf) > _FFPLAY_BUF_MAX:
                buf = buf[-512:]
            for line in parts:
                match = _FFPLAY_POS_RE.match(line.strip())
                if not match:
                    continue
                try:
                    lips_server.set_position(float(match.group(1)), gen)
                except (TypeError, ValueError):
                    pass
    except Exception:  # noqa: BLE001 - 时钟断了不影响播放
        pass
    finally:
        lips_server.end_audio_clock(gen)
        try:
            pipe.close()
        except Exception:  # noqa: BLE001
            pass


@neko_plugin
class MusicSynthPlugin(NekoPluginBase):
    """全自动唱歌：采集 → 切歌 → 分离 → DDSP-SVC 变声 → 混音。"""

    # Training-run state carries its own ``data`` key (clip counts); the host
    # unwraps a top-level ``data`` as an Ok/Err envelope, which would strip the
    # whole payload.  These entries name that key ``dataset`` instead.
    TRAINING_RESERVED_KEYS = frozenset({"data"})

    @staticmethod
    def _training_payload(payload: dict) -> dict:
        """Rename host-reserved keys so the export reaches the panel intact."""
        out = {}
        for key, value in payload.items():
            out["dataset" if key in MusicSynthPlugin.TRAINING_RESERVED_KEYS else key] = value
        return out


    @ui.context(id="dashboard")
    async def synth_dashboard(self, **_):
        return self._snapshot()

    @lifecycle(id="startup")
    async def startup(self, **_):
        self._plugin_dir = Path(__file__).resolve().parent
        self._data_dir = self.data_path(".")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._pipeline = Pipeline(self._data_dir, self._plugin_dir)
        # 流水线预生成口型轨时要抓官方歌词：和播放走同一个取词函数（只是
        # 处理时强制重试一次，错过就没有第二次机会了）。
        self._pipeline.lrc_fetcher = lambda _track: self._fetch_lyrics(_track, force=True)
        # 口型服务随插件一起起来：主窗口的 bridge 每 3 秒探一次端口，
        # 等第一首歌开播才监听的话，头 3 秒的对嘴会丢。
        try:
            lips_server.start()
        except Exception as exc:  # noqa: BLE001 - 起不来只是没有口型
            self.logger.warning("[lips] 口型服务启动失败：%s", exc)
        # 插件重启会把处理线程带走：上次遗留的 running/queued 标记为中断，
        # 否则列表里会永远停在一个不动的「正在处理 5%」。
        resume_ids: list[str] = []
        try:
            for _t in self._pipeline.store.all():
                _status = str(_t.get('status') or '')
                _running = _status in ('running', 'queued')
                # 上一次重载已经标成「中断」、但还没续跑的，也要在这里接上，
                # 否则装好新代码再重载一次，那条原创歌就永远停在 failed。
                _already = (_status == 'failed'
                            and str(_t.get('step') or '') == 'interrupted')
                if not _running and not _already:
                    continue
                _tid = str(_t.get('id') or '')
                if not _tid:
                    continue
                _art = _t.get('artifacts') or {}
                _mid = any(
                    Path(str(_v)).is_file()
                    for _k, _v in _art.items()
                    if _k != 'final' and _v
                )
                _attempts = int(_t.get('resume_attempts') or 0)
                _msg = ('处理被中断（插件重载），点「继续唱」继续' if _mid
                        else '处理被中断（插件重载），点「让她唱」重新开始')
                if _running:
                    _attempts += 1
                    self._pipeline.store.update(
                        _tid, status='failed', progress=0, step='interrupted',
                        error=_msg, interrupted=True, resume_attempts=_attempts)
                else:
                    self._pipeline.store.update(_tid, error=_msg, interrupted=True)
                # 「生成并让她唱」是一次性全自动任务：原始歌就在 work/ace，
                # 重载后不该再让用户点一次「让她唱」。只自动续跑这种任务，
                # 最多两次，避免插件反复重载时无限重跑。
                _src = str(_t.get('source') or '')
                _auto = bool(_t.get('auto_sing')) or Path(_src).parent.name == 'ace'
                if _auto and _attempts < 2:
                    resume_ids.append(_tid)
        except Exception:  # noqa: BLE001
            pass
        self._jobs: dict[str, dict] = {}
        self._jobs_lock = threading.Lock()
        self._shutdown = False
        # 独立听歌录制：应用声道隔离 + 整首切分 + 三首缓冲 + 锚点
        self._song_listener = SongListener(self)
        self._pending_sing: dict = {}
        # 处理完要自动播放的曲目 id（AI 点了名但还没唱过的那首）
        self._pending_play: set = set()
        self._setup_job = ""
        self._setup_job_ts = 0.0
        # 首次安装只允许跑一条线程：向导/首页两个入口同时点会抢同一批 .part。
        self._setup_lock = threading.Lock()
        # playback debounce state (2026-11-03)
        self._playback_proc = None
        self._last_play_target = ""
        self._last_play_at = 0.0
        self._train_procs: dict[str, subprocess.Popen] = {}
        self._train_lock = threading.Lock()
        # 同一时间只跑一条唱/处理流水线：每首歌都会连续占用分离、去混响、变声的显存，
        # 多首同时开会把显卡打满，桌面直接卡住。后来的任务排队，等前一首结束再开始。
        self._process_lock = threading.Lock()
        # 语料投递队列：一次只处理一个文件，处理完释放，再取下一个。
        # 以前每个 ms_corpus_add 都直接起一个线程，用户一次投 50 个文件就会有 50 条
        # 分离/切片流水线同时跑（每条都要把音频读进内存），直接把内存吃满。
        self._corpus_queue: list = []
        self._corpus_queue_lock = threading.Lock()
        self._corpus_worker_on = False
        # 当前真正在处理的那条素材：删除时不能直接搬正在被 worker 读的文件
        self._corpus_active_source = ""
        # 分片上传会话：upload_id -> {path, slot, name, size}
        self._uploads: dict[str, dict] = {}
        self._uploads_lock = threading.Lock()
        # 声音包导入会话：upload_id -> {path, name, size}
        self._model_uploads: dict[str, dict] = {}
        # 语料索引的读改写锁：删除/登记/更新可能来自不同线程，不加锁会互相覆盖
        self._corpus_lock = threading.RLock()
        # 录音停止标记：job_id -> 标记文件路径（「录到我说停」用）
        # 旧语料没有 dur_s：启动后台补测一次（ffprobe 只读文件头，很快）
        self._thread("corpus-dur-backfill", self._corpus_backfill_durations)
        # 先清掉「文件和切片都已经不在了」的失效记录，避免一直显示、又删不掉
        self._thread("corpus-drop-missing", self._corpus_drop_missing_sources)
        # 上次被中断的素材（status=processing）重新排队，接着处理
        self._thread("corpus-resume", self._corpus_resume_pending)
        # The host's [music_synth] section seeds first-run defaults; anything
        # already in data/config.json wins (user choices are never overwritten).
        host_section: dict = {}
        try:
            host_cfg = await self.config.dump(timeout=6.0)
            if isinstance(host_cfg, dict):
                host_section = host_cfg.get("music_synth") or {}
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("读取宿主配置失败：%s", exc)
        self._host_section = host_section if isinstance(host_section, dict) else {}
        seed_keys = ("compute_root", "envs_dir", "models_dir", "work_dir", "base_python",
                     "precision", "max_parallel", "sep_backend",
                     "download_source", "auto_setup")
        if self._host_section:
            raw = self._pipeline.paths.get("raw") or {}
            seeds = {k: self._host_section[k] for k in seed_keys
                     if self._host_section.get(k) not in (None, "") and k not in raw}
            if seeds:
                runtime.save_config(self._data_dir, seeds)
            self._pipeline.paths = runtime.resolve_paths(self._data_dir, self._host_section)
            self._pipeline.work_dir = Path(self._pipeline.paths["work_dir"])
            self._pipeline.work_dir.mkdir(parents=True, exist_ok=True)
        try:
            plan = runtime.plan_for(
                runtime.probe_hardware(force=True),
                self._pipeline.paths.get("tier_override", ""),
                self._pipeline.paths.get("precision", "auto"),
                self._pipeline.paths.get("max_parallel", 0))
            self.logger.info("为她而唱启动：%s（%s）", plan["tier"], plan["basis"])
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("档位检测失败：%s", exc)
        # A training run outlives a plugin restart: bring its supervisor back
        # (state.json decides whether it resumes training or keeps waiting).
        try:
            resumed = self._resume_stale_training()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("恢复训练任务失败：%s", exc)
            resumed = []
        try:
            chains = self._resume_train_chains()
            if chains:
                self.logger.info("已恢复 %d 个「翻唱→原创」训练串联", chains)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("恢复训练串联失败：%s", exc)
        # 听歌模式：独立时自动开录；联动时跟随「为她所听」选定的应用自动录。
        # 后台线程每 8 秒核对一次，两边谁先启动都能自动切到正确模式。
        try:
            await asyncio.to_thread(self._song_listener.cleanup_stale_worker)
            self._thread("ms-link-watch", self._watch_link_mode)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("启动听歌监听失败：%s", exc)
        # 后台处理线程要用主事件循环回调「为她所听」的显存让位。
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        # 上次唱歌中途插件被重载：把为她所听从让位状态恢复回来。
        try:
            if (self._data_dir / "perception_priority.flag").is_file():
                await self._perception_priority(False)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("恢复「为她所听」失败：%s", exc)
        # 定期清理缓存：最多每 24 小时一次，后台线程，不拖慢启动
        try:
            self._thread("cache-cleanup", self._auto_cache_cleanup)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("启动缓存清理失败：%s", exc)
        # 原创「生成并让她唱」被打断的话，重启后自己接着唱完，不用再点一次
        if resume_ids:
            self._thread("ms-auto-resume", self._auto_resume_tracks, list(resume_ids))
        return Ok({"status": "ready", "resumed_training": resumed,
                   "resuming_tracks": len(resume_ids)})

    async def shutdown(self, **_):
        self._shutdown = True
        try:
            lips_server.stop()
        except Exception:  # noqa: BLE001 - 卸载时停不掉口型服务不影响退出
            pass
        return Ok({"status": "stopped"})

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    async def _call_entry(self, entry_ref: str, params=None, *, timeout: float = 30.0):
        plugins = getattr(self, "plugins", None)
        call_entry = getattr(plugins, "call_entry", None)
        if not callable(call_entry):
            raise RuntimeError("host_has_no_cross_plugin_api")
        raw = call_entry(entry_ref, params or {}, timeout=float(timeout))
        if hasattr(raw, "__await__"):
            raw = await raw
        return getattr(raw, "value", raw)

    def _setup_state(self) -> dict:
        path = self._data_dir / "setup_state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_setup_state(self, updates: dict) -> dict:
        state = self._setup_state()
        state.update(updates)
        state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        path = self._data_dir / "setup_state.json"
        try:
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            self.logger.warning("保存安装状态失败：%s", exc)
        return state

    @staticmethod
    def _plan_view(plan: dict) -> dict:
        return {k: v for k, v in plan.items() if k in {
            "tier", "label", "basis", "device", "sep_device", "synth_precision",
            "synth_n_steps", "max_segment_s", "parallel", "sep_step", "vram_total_gib",
            "vram_free_gib", "vram_used_gib", "ram_total_gib", "ram_available_gib",
            "gpu_name", "compute_cap", "cpu", "warnings", "override", "auto_tier"}}

    def _snapshot(self, link: dict | None = None) -> dict:
        pipeline = self._pipeline
        paths = pipeline.paths
        raw = paths.get("raw") or {}
        probe = runtime.probe_hardware()
        plan = runtime.plan_for(probe, paths.get("tier_override", ""),
                                paths.get("precision", "auto"),
                                paths.get("max_parallel", 0))
        env = runtime.env_status(paths)
        ckpt_now = Path(str(paths.get("ddsp_ckpt") or ""))
        models = runtime.list_ddsp_models(paths.get("ddsp_root"), ckpt_now,
                                          self._active_train_run_ids(),
                                          Path(str(paths.get("ddsp_pretrain_reflow") or "")))
        for _m in models:
            _bundled = self._bundled_voice(str(_m.get("exp_dir") or ""))
            if _bundled:
                _m["bundled"] = True
                _m["demo"] = _bundled["demo"]
                _m["demo_title"] = _bundled["title"]
        tracks = pipeline.store.all()
        # 标一下「成品到底还在不在」：记录里常见文件早被删了，面板据此禁用试听，而不是点了才报错
        for _t in tracks:
            _fin = str(((_t.get("artifacts") or {}).get("final")) or "")
            _src = str(_t.get("source") or "")
            try:
                _t["playable"] = (bool(_fin) and Path(_fin).is_file()) or (
                    str(_t.get("origin") or "") == "ace"
                    and bool(_src) and Path(_src).is_file())
            except OSError:
                _t["playable"] = False
            # 有没有可以接着用的中间结果：面板据此把按钮显示成「继续唱」
            # 而不是从头再来（成品还在时仍然显示「重新唱一遍」）。
            _art = _t.get("artifacts") or {}
            _t["resumable"] = any(
                bool(_v) and Path(str(_v)).is_file()
                for _k, _v in _art.items()
                if _k in ("vocals", "instrumental", "vocal_clean", "vocal_wav"))
        setup_state = self._setup_state()
        return {
            "ok": True,
            "tier": plan,
            "plan": self._plan_view(plan),
            "hardware": probe,
            "hardware_brief": runtime.probe_brief(probe),
            "env": env,
            "paths": {k: str(v) for k, v in paths.items()
                      if isinstance(v, (str, Path)) and k != "raw"},
            "engine": {
                "name": "DDSP-SVC",
                "version": "6.3 (RectifiedFlow)",
                "ddsp_root": str(paths.get("ddsp_root") or ""),
                "ddsp_ckpt": str(paths.get("ddsp_ckpt") or ""),
                "ckpt_name": Path(paths["ddsp_ckpt"]).name if paths.get("ddsp_ckpt") else "",
                "config_file": str(ckpt_now.parent / "config.yaml") if str(ckpt_now) else "",
                "config_exists": (ckpt_now.parent / "config.yaml").is_file()
                if str(ckpt_now) else False,
                "models": models,
                "preview_src": str(raw.get("preview_src") or ""),
                "key_shift": paths.get("key_shift") or 0,
                "vocal_register_shift": paths.get("vocal_register_shift") or 0,
                "infer_step": paths.get("infer_step") or 80,
                "preview_step": _int_setting(raw, "preview_step", 120, 5, 400),
                "pitch_extractor": str(raw.get("pitch_extractor") or "rmvpe"),
                "method": str(raw.get("method") or "euler"),
                "f0_min": _int_setting(raw, "f0_min", 65, 30, 300),
                "f0_max": _int_setting(raw, "f0_max", 800, 200, 1500),
                "threshold": _int_setting(raw, "threshold", -60, -100, -10),
                "auto_key": bool(raw.get("auto_key", True)),
                "auto_key_max": _int_setting(raw, "auto_key_max", 4, 0, 12),
                "vocal_reverb": _float_setting(raw, "vocal_reverb", 0.12, 0.0, 0.6),
                "reverb_auto": bool(raw.get("reverb_auto", True)),
                "reverb_auto_strength": _float_setting(raw, "reverb_auto_strength", 1.0, 0.0, 1.0),
                "stereo_width": _float_setting(raw, "stereo_width", 1.0, 1.0, 2.0),
                "clarity": _float_setting(raw, "clarity", 1.0, 0.0, 1.5),
                "vocal_level_db": _float_setting(raw, "vocal_level_db", -2.0, -12.0, 6.0),
                "bgm_gain_db": _float_setting(raw, "bgm_gain_db", 0.0, -12.0, 12.0),
                "consonant": _float_setting(raw, "consonant", 0.35, 0.0, 0.8),
                "cache_auto": bool(raw.get("cache_auto", True)),
                "cache_days": _int_setting(raw, "cache_days", 7, 1, 365),
                "train": {
                    "batch": _int_setting(raw, "train_batch", 0, 0, 64),
                    "accum": _int_setting(raw, "train_accum", 1, 1, 16),
                    "duration": _float_setting(raw, "train_duration", 2.0, 1.0, 10.0),
                    "amp": str(raw.get("train_amp") or "bf16"),
                    "cache_fp16": bool(raw.get("train_cache_fp16", False)),
                    "pitch_aug": bool(raw.get("train_pitch_aug", True)),
                },
                "note": "变声用 DDSP-SVC 6.3；训练好的模型在 ms_train 里后台训练（reflow）。",
            },
            "tracks": tracks[-40:],
            "tracks_total": len(tracks),
            "jobs": dict(self._jobs),
            "listener": self._song_listener.status() if hasattr(self, "_song_listener") else {},
            "link": link or {},
            "capture": {"runtime": runtime.locate_app_capture_runtime(self._data_dir, str(self._pipeline.paths.get("synth_python") or ""))},
            "setup": dict(setup_state, progress=self._setup_progress()),
            "first_run": not bool(setup_state.get("completed")),
            "last_run": {
                "probe": pipeline.last_probe,
                "plan": pipeline.last_plan,
                "at": pipeline.last_probe_at,
            },
            "tools": {
                "record": "先录音（或导入已有音频），再自动切歌",
                "auto": "一次触发：录音 → 切歌 → 逐首全链处理",
                "setup": "首次使用：检测依赖 → 选安装路径 → 一键安装缺失项",
            },
        }

    def _thread(self, name: str, target, *args) -> threading.Thread:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        return thread

    def _job_begin(self, kind: str) -> str:
        job_id = f"{kind}-{int(time.time() * 1000)}"
        with self._jobs_lock:
            self._jobs[job_id] = {"job": kind, "state": "running", "started": time.strftime("%H:%M:%S"),
                                  "progress": 0, "detail": ""}
            for old in list(self._jobs)[:-8]:
                self._jobs.pop(old, None)
        return job_id

    def _job_update(self, job_id: str, **fields) -> None:
        with self._jobs_lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    # 真正占用显存的训练阶段；awaiting_choice 只是等用户选检查点，不占 GPU。
    _TRAIN_GPU_PHASES = ("preparing", "preprocessing", "training", "previewing")

    def _training_active(self) -> dict | None:
        """DDSP 训练 supervisor 是否正在占用显存/算力。

        判定依据是「supervisor 进程活着 + 阶段真的在跑」。等待用户选择检查点
        （awaiting_choice）和已结束的 run 不算，否则唱歌会被一个空转的训练永远挡住。
        """
        try:
            root = self._train_runs_root()
            if not root.is_dir():
                return None
            for run_dir in root.iterdir():
                if not run_dir.is_dir() or not runtime.valid_run_id(run_dir.name):
                    continue
                state = runtime.read_training_state(run_dir) or {}
                if not state:
                    continue
                phase = str(state.get("phase") or "")
                if phase not in self._TRAIN_GPU_PHASES:
                    continue
                if (self._train_proc_live(run_dir.name) is not None
                        or runtime.pid_alive(state.get("pid"))):
                    return {"run_id": run_dir.name, "phase": phase,
                            "name": str(state.get("name") or "")}
        except Exception:  # noqa: BLE001 - 互斥判断不能拖垮入口
            return None
        return None

    async def _free_vram_for_training(self) -> dict:
        """训练前腾显存：先让「为她所听」暂停分离/歌词模型，不行再整插件停掉。

        温和那条路走它自己的「唱歌优先」入口：只停 GPU 模型（人声分离、歌词、
        专业歌声音色），录音和采集继续跑；整插件停掉只是兜底，因为那会把它的
        采集/Live2D 联动一起关掉。
        """
        result: dict = {"stopped": [], "paused": [], "skipped": [], "summary": ""}
        try:
            data = await self._perception_priority(True, collect_lyrics=False)
        except Exception as exc:  # noqa: BLE001 - 让位失败就走兜底
            self.logger.warning("[train] 请为她所听让出显存失败：%s", exc)
            data = {}
        if isinstance(data, dict) and data.get("ok"):
            result["paused"] = ["music_perception:stem", "music_perception:lyrics"]
            result["summary"] = ("已暂停为她所听的人声分离/歌词模型"
                                 "（给训练腾显存，训练结束后自动恢复）")
            self.logger.info("[train] %s", result["summary"])
            return result
        try:
            fallback = await asyncio.to_thread(vram_release.release_for_training,
                                               log=self.logger)
        except Exception as exc:  # noqa: BLE001 - 停不下来也继续训练
            self.logger.warning("[train] 停止占显存的插件失败：%s", exc)
            return result
        for key in ("stopped", "pending", "skipped", "summary"):
            if fallback.get(key):
                result[key] = fallback.get(key)
        return result

    def _restore_after_training(self, plugins: list[str],
                                restore_perception: bool = False) -> None:
        """训练收尾后把为训练让出显存的插件重新拉起来。

        判据用 ``_training_active()``：进入 awaiting_choice（等选检查点）或训练
        结束都不占 GPU。连续 45 秒空闲才恢复，免得训练在节点之间短暂停顿就被
        提前拉起来又互相抢显存。
        """
        deadline = time.time() + 12 * 3600
        idle_since = 0.0
        while time.time() < deadline:
            time.sleep(10.0)
            try:
                # 训练之外，ACE 原创训练/翻唱/处理中的曲目同样吃显存，一起等。
                busy = self._training_active() or self._singing_active()
            except Exception:  # noqa: BLE001 - 判断失败就当已经空闲
                busy = None
            if busy is None:
                idle_since = idle_since or time.time()
                if time.time() - idle_since >= 45:
                    break
            else:
                idle_since = 0.0
        else:
            self.logger.info("[train] 等训练结束超时（12h），不再自动恢复插件")
            return
        if restore_perception:
            try:
                restored = self._apply_perception_priority_sync(False)
                self.logger.info("[train] 恢复为她所听的分离/歌词模型：%s", restored)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[train] 恢复为她所听失败：%s", exc)
        if not plugins:
            return
        try:
            vram_release.start_plugins(list(plugins), log=self.logger)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[train] 重新拉起插件失败：%s", exc)

    def _singing_active(self) -> dict | None:
        """唱歌 / 原创生成 / ACE 训练是否正占着 GPU 锁或还在排队。"""
        try:
            if self._process_lock.locked():
                return {"kind": "processing"}
        except Exception:  # noqa: BLE001
            pass
        with self._jobs_lock:
            for job_id, job in self._jobs.items():
                if str(job.get("state") or "") not in ("queued", "running"):
                    continue
                kind = str(job.get("job") or "")
                if kind in ("track", "anchor", "resume", "ace", "ace-train"):
                    return {"job_id": job_id, "kind": kind}
        return None

    def _wait_training_idle(self, job_id: str) -> None:
        """唱歌前等训练跑完：训练占显存时同时跑会直接 OOM/崩溃。"""
        announced = False
        while not getattr(self, "_shutdown", False):
            busy = self._training_active()
            if busy is None:
                return
            if not announced:
                announced = True
                self.logger.info("训练 %s 正在占用显存，唱歌排队等待", busy.get("run_id"))
            self._job_update(job_id, state="queued",
                             detail=("训练中（%s），训练结束后自动开始唱歌；"
                                     "两者不能同时占用显存" % busy.get("phase")))
            time.sleep(3.0)

    # ------------------------------------------------------------------
    # state / environment
    # ------------------------------------------------------------------
    @plugin_entry(id="ms_state", name="为她而唱 · 状态",
                  description="读取档位判定、计算环境、曲目列表与产物路径（只读）",
                  input_schema={"type": "object", "properties": {}})
    async def ms_state(self, **_):
        try:
            link = await self._link_status()
        except Exception:  # noqa: BLE001 - 联动状态只影响展示，不能拖垮快照
            link = {}
        return Ok(self._snapshot(link=link))

    def _reload_paths(self) -> dict:
        self._pipeline.paths = runtime.resolve_paths(
            self._data_dir, getattr(self, "_host_section", {}))
        self._pipeline.work_dir = Path(self._pipeline.paths["work_dir"])
        self._pipeline.work_dir.mkdir(parents=True, exist_ok=True)
        return self._pipeline.paths

    def _record_probe(self, probe: dict, plan: dict) -> None:
        self._pipeline.last_probe = probe
        self._pipeline.last_plan = plan
        self._pipeline.last_probe_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._pipeline.log_probe(probe, plan)

    @plugin_entry(id="ms_vram_plan", name="为她而唱 · 显存兼容方案",
                  description=("只读：按当前空闲显存和可用内存，给出 ACE 生成、变声、分离"
                               "各自会走哪一档（全部放显存 / 部分卸载 / 逐层搬 / 纯 CPU）"),
                  input_schema={"type": "object", "properties": {
                      "model": {"type": "string", "description": "ACE 模型名；留空=当前模型"},
                      "duration_s": {"type": "number"},
                      "batch": {"type": "integer"}}})
    async def ms_vram_plan(self, model: str = "", duration_s: float = 60.0,
                           batch: int = 1, **_):
        probe = await asyncio.to_thread(runtime.probe_hardware, True)
        mem = vram_policy.snapshot(probe)
        name = str(model or self._ace_selected_model()).strip()
        p = self._ace_paths()
        gib = vram_policy.estimate_runtime_gib(p["checkpoints"] / name) if name else 0.0
        plan = vram_policy.ace_plan(mem, gib, is_xl="xl" in name,
                                    duration_s=duration_s, batch=batch)
        return Ok({"ok": True, "memory": mem, "model": name,
                   "model_gib": round(gib, 2),
                   "ace": {**plan, "text": vram_policy.describe(plan)},
                   "ddsp": vram_policy.ddsp_device(mem),
                   "separation": vram_policy.separation_plan(mem)})

    @plugin_entry(id="ms_probe", name="为她而唱 · 重新探测硬件",
                  description="立即重新读取显卡型号/显存总量与空闲/内存/CUDA 计算能力，并给出本次执行参数",
                  input_schema={"type": "object", "properties": {}})
    async def ms_probe(self, **_):
        probe = await asyncio.to_thread(runtime.probe_hardware, True)
        paths = self._pipeline.paths
        plan = runtime.plan_for(probe, paths.get("tier_override", ""),
                                paths.get("precision", "auto"),
                                paths.get("max_parallel", 0))
        self._record_probe(probe, plan)
        return Ok({"ok": True, "probe": probe, "brief": runtime.probe_brief(probe),
                   "plan": self._plan_view(plan)})

    @plugin_entry(id="ms_env_check", name="为她而唱 · 环境体检",
                  description="逐项检查 Python/ffmpeg/驱动/torch/audio-separator/DDSP-SVC 仓库与预训练权重，返回精确缺失项与安装大小",
                  input_schema={"type": "object", "properties": {}})
    async def ms_env_check(self, **_):
        paths = self._reload_paths()
        report = await asyncio.to_thread(setup_mod.check_dependencies, paths)
        checks = [{"item": item["label"], "ok": item["ok"], "status": item["status"],
                   "detail": item["detail"], "fix": item["fix"],
                   "size_gb": item["size_gb"]} for item in report["items"]]
        self._save_setup_state({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "report": report,
                                "completed": bool(report["ready"])})
        return Ok({"ok": report["ready"], "ready": report["ready"], "checks": checks,
                   "report": report,
                   "tier": runtime.plan_for(report["hardware"],
                                            paths.get("tier_override", ""),
                                            paths.get("precision", "auto"),
                                            paths.get("max_parallel", 0))})

    @plugin_entry(id="ms_dl_diag", name="为她而唱 · 下载链路体检",
                  description=("只读探测：代理是否真的可用、国内镜像（gh-proxy / ghfast / hf-mirror / 清华 PyPI）"
                               "能不能连上、各源速度多少。遇到下载慢或下不动时用，不会下载模型。"),
                  input_schema={"type": "object", "properties": {}})
    async def ms_dl_diag(self, **_):
        diag = await asyncio.to_thread(setup_mod.diagnose_download_sources)
        return Ok(dict(diag, ok=True))

    @plugin_entry(id="ms_setup_check", name="为她而唱 · 首次使用检测",
                  description="首次使用向导第 1 步：检测依赖与模型，输出已就绪/缺失/可选清单、下载大小与建议安装目录",
                  input_schema={"type": "object", "properties": {}})
    async def ms_setup_check(self, **_):
        paths = self._reload_paths()
        report = await asyncio.to_thread(setup_mod.check_dependencies, paths)
        self._save_setup_state({"checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "report": report,
                                "completed": bool(report["ready"])})
        return Ok(report)

    @plugin_entry(id="ms_setup_browse", name="为她而唱 · 选择安装目录",
                  description=("第一次使用引导第 4 页：弹出系统目录选择框，返回用户选中的绝对路径。"
                               "对话框在后台线程里打开；本次没选完会返回 state=open，再次调用继续等。"),
                  input_schema={"type": "object", "properties": {
                      "start_path": {"type": "string", "description": "对话框的起始目录"},
                      "title": {"type": "string", "description": "对话框标题"},
                      "wait": {"type": "number",
                               "description": "本次最多等待秒数（默认 20，上限 30）"},
                      "restart": {"type": "boolean",
                                  "description": "true 表示开始一次新的选择（重复调用传 false 只等结果）"}}})
    async def ms_setup_browse(self, start_path: str = "", title: str = "",
                              wait: float = 20.0, restart: bool = False, **_):
        try:
            wait_s = max(0.0, min(float(wait or 20.0), 30.0))
        except (TypeError, ValueError):
            wait_s = 20.0
        setup_mod.browse_start(str(start_path or ""), str(title or ""), bool(restart))
        result = await asyncio.to_thread(setup_mod.browse_wait, wait_s)
        return Ok(dict(result, ok=True))

    @plugin_entry(id="ms_setup_install", name="为她而唱 · 一键安装依赖",
                  description=("首次使用向导第 2 步：按用户选定路径安装缺失依赖与模型。"
                               "立即返回 job_id，安装可中断后重跑（pip 缓存 / HF 断点 / HTTP Range）"),
                  input_schema={"type": "object", "properties": {
                      "path": {"type": "string", "description": "安装根目录（留空沿用当前/建议目录）"},
                      "items": {"type": "array", "items": {"type": "string"},
                                "description": "只补装这些 key；留空=全部缺失项"},
                      "base_python": {"type": "string"},
                      "download_source": {"type": "string", "enum": ["huggingface", "mirror"]},
                      "precision": {"type": "string", "enum": ["auto", "fp32", "fp16", "bf16"]}}})
    async def ms_setup_install(self, path: str = "", items=None, base_python: str = "",
                               download_source: str = "", precision: str = "", **_):
        updates: dict = {}
        current_root = str(self._pipeline.paths.get("compute_root") or "")
        if path:
            target = Path(path).expanduser()
            if not target.is_absolute():
                return Ok({"ok": False,
                           "error": "请填绝对路径，例如 D:\\NEKOMusicSynth"})
            updates["compute_root"] = str(target)
            # 换了安装根目录：清掉旧的派生路径，避免插件还去旧盘找模型。
            if str(target) != current_root:
                for key in ("envs_dir", "models_dir", "work_dir", "sep_model_dir",
                            "ddsp_root", "ddsp_ckpt", "synth_python", "sep_python",
                            "train_runs_dir"):
                    updates[key] = ""
        elif not self._pipeline.paths.get("configured"):
            return Ok({"ok": False, "needs_path": True,
                       "error": "请先选择安装位置：插件不会自动选盘，也不会下载到别的盘"})
        if base_python:
            updates["base_python"] = base_python
        if download_source:
            updates["download_source"] = download_source
        if precision:
            updates["precision"] = precision
        if updates:
            runtime.save_config(self._data_dir, updates)
        paths = self._reload_paths()
        if not paths.get("configured"):
            return Ok({"ok": False, "needs_path": True,
                       "error": "安装位置没有保存成功，请重新选择目录"})
        try:
            Path(paths["compute_root"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Ok({"ok": False, "error": f"无法创建安装目录：{exc}"})
        report = await asyncio.to_thread(setup_mod.check_dependencies, paths)
        selected = [str(x) for x in items] if isinstance(items, (list, tuple)) else None
        if selected is not None:
            selected = [x for x in selected if x]
        if not selected and not report["installable_missing"]:
            if report["ready"]:
                self._save_setup_state({"completed": True, "report": report,
                                        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                return Ok({"ok": True, "already_ready": True, "report": report})
            # 缺的必需项没有自动安装手段（例如安装盘读不到）：明说原因，
            # 别返回 already_ready 把人骗进「完成」页。
            return Ok({"ok": False, "report": report,
                       "error": "以下必需项无法自动安装：" + "、".join(report["missing"])})
        with self._setup_lock:
            with self._jobs_lock:
                cur = dict(self._jobs.get(self._setup_job) or {}) if self._setup_job else {}
            if cur.get("state") == "running" and time.time() - self._setup_job_ts < 6 * 3600:
                # 已经有一条安装线程在跑：把同一个 job 交回去，避免两个线程抢 .part。
                return Ok({"ok": True, "started": True, "running": True,
                           "job_id": self._setup_job,
                           "report": self._setup_state().get("report") or report})
            job_id = self._job_begin("setup")
            self._setup_job = job_id
            self._setup_job_ts = time.time()
            self._setup_pct = {}
        probe = report["hardware"]

        def runner():
            counter = {"n": 0}

            def progress(detail: str) -> None:
                counter["n"] += 1
                self._job_update(job_id, detail=str(detail)[-300:],
                                 progress=min(96, 4 + counter["n"] * 2),
                                 stage=str(detail).split("：")[0][:40])

            try:
                result = setup_mod.install(paths, selected, progress, probe)
                after = result.get("report") or {}
                self._save_setup_state({
                    "completed": bool(after.get("ready")),
                    "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "report": after,
                    "last_install": {k: v for k, v in result.items() if k != "report"},
                })
                errs = [f"{e.get('item')}: {e.get('error')}" for e in result.get("errors", [])]
                if not result.get("ok"):
                    if result.get("error"):
                        errs.insert(0, str(result["error"]))
                    if not errs:
                        miss = result.get("missing_after") or []
                        errs.append(("仍缺少：" + "、".join(miss)) if miss else "未知原因（没有可下载的项，但仍有必需项未就绪）")
                detail = "安装完成" if result.get("ok") else "安装未全部完成：" + "；".join(errs)
                self._job_update(job_id, state="done" if result.get("ok") else "failed",
                                 progress=100, detail=detail[-500:], result=result)
            except Exception as exc:  # noqa: BLE001
                self._job_update(job_id, state="failed", progress=100,
                                 detail=f"{type(exc).__name__}: {exc}")
                self._save_setup_state({"completed": False,
                                        "last_install": {"ok": False,
                                                         "error": f"{type(exc).__name__}: {exc}"}})

        self._thread(f"ms-setup-{job_id}", runner)
        return Ok({"ok": True, "started": True, "job_id": job_id, "report": report,
                   "paths": {"compute_root": str(paths["compute_root"]),
                             "models_dir": str(paths["models_dir"]),
                             "work_dir": str(paths["work_dir"])}})

    @plugin_entry(id="ms_setup_redownload", name="为她而唱 · 重新下载模型",
                  description=("把 ContentVec 编码器 / 人声分离权重挪进 models/_trash 后重新下载，"
                               "用于修复被中断的残缺文件"),
                  input_schema={"type": "object", "properties": {
                      "model": {"type": "string", "enum": ["ddsp_contentvec", "sep_karaoke"]}}})
    async def ms_setup_redownload(self, model: str = "", **_):
        key = str(model or "").strip()
        if key not in setup_mod.REDOWNLOAD_KEYS:
            return Ok({"ok": False, "error": f"不支持重下的模型：{key or '（空）'}"})
        paths = self._reload_paths()
        if not paths.get("configured"):
            return Ok({"ok": False, "needs_path": True,
                       "error": "还没有选择安装位置，请先到安装向导里选一个目录"})
        try:
            moved = await asyncio.to_thread(setup_mod.stash_model_files, paths, key)
        except Exception as exc:  # noqa: BLE001
            return Ok({"ok": False, "error": f"移走旧文件失败：{type(exc).__name__}: {exc}"})
        res = await self.ms_setup_install(items=[key])
        payload = getattr(res, "value", res)
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["redownload"] = key
            payload["moved"] = moved
            return Ok(payload)
        return res

    _setup_pct: dict = {}  # 下载类分项进度只增不减，重启插件后从 0 重新计

    @plugin_entry(id="ms_setup_status", name="为她而唱 · 安装进度",
                  description="读取首次使用检测结果与安装任务进度（只读）",
                  input_schema={"type": "object", "properties": {}})
    async def ms_setup_status(self, **_):
        with self._jobs_lock:
            job = dict(self._jobs.get(self._setup_job) or {}) if self._setup_job else {}
        return Ok({"ok": True, "job": job, "setup": self._setup_state(),
                   "progress": self._setup_progress(),
                   "first_run": not bool(self._setup_state().get("completed"))})

    # -- 首次向导：分项进度（只读，不改安装逻辑） ---------------------------
    # 安装线程的进度回调把「<label>：<细节>」写进 job["stage"]（取「：」前那段）和 job["detail"]，
    # 这里把它整理成每项 {state, pct, done_mb, total_mb}，只给首次向导画进度条用；
    # 安装本身仍由 setup.install 完成，这里不碰任何安装逻辑。
    def _setup_progress(self) -> dict:
        report = self._setup_state().get("report") or {}
        items = report.get("items") if isinstance(report, dict) else None
        items = items if isinstance(items, list) else []
        with self._jobs_lock:
            job = dict(self._jobs.get(self._setup_job) or {}) if self._setup_job else {}
        st = str(job.get("state") or "")
        stage = str(job.get("stage") or "")
        detail = str(job.get("detail") or "")
        out: dict = {}
        labels: dict = {}
        for it in items:
            key = str(it.get("key") or "")
            if not key:
                continue
            labels[str(it.get("label") or key)] = key
            out[key] = {"label": str(it.get("label") or key),
                        "state": "ready" if it.get("status") == "ready" else "wait"}
        # 1) 当前正在装哪一项：先按 detail 里「：」前的 label 精确对上，再按阶段词回落
        head = detail.split("：")[0] if "：" in detail else stage
        current = labels.get(head) or labels.get(stage) or ""
        if not current and st == "running":
            if stage == "venv" and out.get("synth_env", {}).get("state") != "ready":
                current = "synth_env"
            elif stage == "repo":
                current = "ddsp_repo"
            elif "便携版" in stage:
                current = "python_host" if "python_host" in out else "base_python"
        # 2) 下载类：detail 形如「12/345 MB」，换算成 done_mb / total_mb / pct
        pct = done_mb = total_mb = None
        m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(MB|GB)", detail)
        if m and current:
            a, b, unit = float(m.group(1)), float(m.group(2)), m.group(3)
            k = 1.0 if unit == "MB" else 1024.0
            done_mb, total_mb = round(a * k, 1), round(b * k, 1)
            pct = round(a * 100.0 / b, 1) if b > 0 else None
        if current and st == "running":
            if pct is not None:
                last = float((self._setup_pct or {}).get(current) or 0.0)
                pct = max(last, pct)
                self._setup_pct[current] = pct
            out[current].update(state="active", pct=pct, done_mb=done_mb, total_mb=total_mb,
                                stage=stage, detail=detail[-200:])
        # 3) 已经跑完的项 / 全部结束
        for k in (job.get("done") or []):
            if k in out:
                out[k].update(state="done", pct=100)
        if st == "done":
            for v in out.values():
                if v["state"] != "ready":
                    v.update(state="done", pct=100)
        for e in ((job.get("result") or {}).get("errors") or []):
            k = str((e or {}).get("item") or "")
            if k in out:
                out[k].update(state="failed", error=str((e or {}).get("error") or ""))
        return {"items": out, "current": current, "stage": stage,
                "detail": detail[-300:], "job_state": st}


    @plugin_entry(id="ms_set_config", name="为她而唱 · 设置",
                  description="设置档位、精度、并行、安装/模型/产物目录与变声参数；只保存配置，不启动计算",
                  input_schema={"type": "object", "properties": {
                      "tier": {"type": "string", "enum": ["auto", "low", "mid", "high"]},
                      "sep_backend": {"type": "string", "enum": ["bs_roformer"]},
                      "compute_root": {"type": "string"},
                      "envs_dir": {"type": "string"},
                      "models_dir": {"type": "string"},
                      "work_dir": {"type": "string"},
                      "sep_model_dir": {"type": "string"},
                      "base_python": {"type": "string"},
                      "ddsp_root": {"type": "string"},
                      "ddsp_ckpt": {"type": "string"},
                      "ace_model": {"type": "string",
                                    "description": "ACE 原创生成用的 DiT 模型名"},
                      "key_shift": {"type": "integer", "minimum": -24, "maximum": 24},
                      "vocal_register_shift": {"type": "integer", "minimum": -12, "maximum": 12,
                                               "description": "音区偏移：保持音高、只挪发声区；±3 内高保真"},
                      "infer_step": {"type": "integer", "minimum": 5, "maximum": 200},
                      "preview_step": {"type": "integer", "minimum": 5, "maximum": 400,
                                       "description": "训练里程碑试听用的推理步数"},
                      "method": {"type": "string", "enum": ["euler", "rk4"]},
                      "pitch_extractor": {"type": "string",
                                          "enum": ["rmvpe", "harvest", "parselmouth", "fcpe", "crepe", "dio"]},
                      "f0_min": {"type": "integer", "minimum": 30, "maximum": 300},
                      "f0_max": {"type": "integer", "minimum": 200, "maximum": 1500},
                      "threshold": {"type": "integer", "minimum": -100, "maximum": -10},
                      "auto_key": {"type": "boolean"},
                      "auto_key_max": {"type": "integer", "minimum": 0, "maximum": 12},
                      "train_duration": {"type": "number", "minimum": 1, "maximum": 10,
                                         "description": "训练随机片段长度（秒）"},
                      "train_amp": {"type": "string", "enum": ["fp16", "bf16", "fp32"]},
                      "train_batch": {"type": "integer", "minimum": 0, "maximum": 64,
                                      "description": "训练 batch；0=按显存自动"},
                      "train_accum": {"type": "integer", "minimum": 1, "maximum": 16,
                                      "description": "梯度累积步数；等效 batch = train_batch × train_accum"},
                      "train_cache_fp16": {"type": "boolean"},
                      "train_pitch_aug": {"type": "boolean"},
                      "vocal_reverb": {"type": "number", "minimum": 0, "maximum": 0.6},
                      "reverb_auto": {"type": "boolean"},
                      "reverb_auto_strength": {"type": "number", "minimum": 0, "maximum": 1},
                      "stereo_width": {"type": "number", "minimum": 1, "maximum": 2},
                      "clarity": {"type": "number", "minimum": 0, "maximum": 1.5},
                      "vocal_level_db": {"type": "number", "minimum": -12, "maximum": 6},
                      "bgm_gain_db": {"type": "number", "minimum": -12, "maximum": 12},
                      "consonant": {"type": "number", "minimum": 0, "maximum": 0.8},
                      "cache_auto": {"type": "boolean"},
                      "cache_days": {"type": "integer", "minimum": 1, "maximum": 365},
                      "device": {"type": "string", "enum": ["auto", "cuda", "cpu"]},
                      "precision": {"type": "string", "enum": ["auto", "fp32", "fp16", "bf16"]},
                      "max_parallel": {"type": "integer", "minimum": 0, "maximum": 4},
                      "download_source": {"type": "string", "enum": ["huggingface", "mirror"]}}})
    async def ms_set_config(self, tier: str = "", sep_backend: str = "",
                            compute_root=None, envs_dir=None, models_dir=None,
                            work_dir=None, sep_model_dir=None,
                            base_python=None, ddsp_root=None, ddsp_ckpt=None,
                            key_shift: int = None, infer_step: int = None,
                            vocal_register_shift: int = None,
                            preview_step: int = None, method: str = "",
                            pitch_extractor: str = "", f0_min: int = None,
                            f0_max: int = None, threshold: int = None,
                            auto_key: bool = None, auto_key_max: int = None,
                            train_duration: float = None, train_amp: str = "",
                            train_batch: int = None, train_accum: int = None,
                            train_cache_fp16: bool = None,
                            train_pitch_aug: bool = None,
                            vocal_reverb: float = None, stereo_width: float = None,
                            reverb_auto: bool = None, reverb_auto_strength: float = None,
                            clarity: float = None, vocal_level_db: float = None,
                            bgm_gain_db: float = None, consonant: float = None,
                            cache_auto: bool = None, cache_days: int = None,
                            device: str = "", precision: str = "",
                            max_parallel: int = -1, download_source: str = "",
                            ace_model: str = "", **_):
        updates = {}
        if tier:
            updates["tier_override"] = "" if tier == "auto" else tier
        if sep_backend:
            updates["sep_backend"] = sep_backend
        if ace_model:
            name = Path(str(ace_model).strip()).name
            p = self._ace_paths()
            if not self._ace_model_ready(p["checkpoints"] / name):
                return Ok({"ok": False,
                           "error": f"ACE 模型不存在或未下载完整：{name}",
                           "saved": {}})
            updates["ace_model"] = name
        # None = 未传（保持原值）；"" = 显式清空，回到从 compute_root 派生的默认目录。
        if compute_root is not None:
            new_root = str(Path(compute_root).expanduser()) if compute_root else ""
            old_root = str(self._pipeline.paths.get("compute_root") or "")
            updates["compute_root"] = new_root
            # 换根目录时清掉旧的派生路径，避免还去旧盘找模型/环境。
            if new_root and new_root != old_root:
                for key in ("envs_dir", "models_dir", "work_dir", "sep_model_dir",
                            "synth_python", "sep_python", "ddsp_root", "ddsp_ckpt",
                            "train_runs_dir"):
                    updates[key] = ""
        for key, value in (("envs_dir", envs_dir), ("models_dir", models_dir),
                           ("work_dir", work_dir), ("sep_model_dir", sep_model_dir),
                           ("base_python", base_python), ("ddsp_root", ddsp_root),
                           ("ddsp_ckpt", ddsp_ckpt)):
            if value is not None:
                text = str(Path(value).expanduser()) if value else ""
                if key == "ddsp_ckpt" and text and not Path(text).is_file():
                    return Ok({"ok": False, "error": f"模型文件不存在：{text}",
                               "saved": {}})
                if key == "ddsp_ckpt" and text and runtime.is_base_model(
                        text, ddsp_root or self._pipeline.paths.get("ddsp_root"),
                        self._pipeline.paths.get("ddsp_pretrain_reflow")):
                    return Ok({"ok": False, "error": "这是训练用的初始模型，不能当普通声音选",
                               "saved": {}})
                if key == "ddsp_ckpt" and text and not \
                        (Path(text).parent / "config.yaml").is_file():
                    return Ok({"ok": False,
                               "error": "模型同目录缺少 config.yaml（推理读不到模型结构）："
                                        + str(Path(text).parent / "config.yaml"),
                               "saved": {}})
                updates[key] = text
        if ddsp_root is not None and not ddsp_root:
            updates.pop("ddsp_ckpt", None)  # 清空仓库时一并放弃显式权重路径
        if key_shift is not None:
            updates["key_shift"] = int(key_shift)
        if vocal_register_shift is not None:
            if not -12 <= int(vocal_register_shift) <= 12:
                return Ok({"ok": False, "error": "音区偏移要填 -12～12", "saved": {}})
            updates["vocal_register_shift"] = int(vocal_register_shift)
        if infer_step is not None:
            updates["infer_step"] = int(infer_step)
        if preview_step is not None:
            if not 5 <= int(preview_step) <= 400:
                return Ok({"ok": False, "error": "试听步数要填 5～400 之间的整数", "saved": {}})
            updates["preview_step"] = int(preview_step)
        if method:
            if method not in {"euler", "rk4"}:
                return Ok({"ok": False, "error": f"不支持的采样方法：{method}", "saved": {}})
            updates["method"] = method
        if pitch_extractor:
            allowed_pitch = {"rmvpe", "harvest", "parselmouth", "fcpe", "crepe", "dio"}
            if pitch_extractor not in allowed_pitch:
                return Ok({"ok": False, "error": f"不支持的音高提取器：{pitch_extractor}",
                           "saved": {}})
            updates["pitch_extractor"] = pitch_extractor
        if f0_min is not None:
            if not 30 <= int(f0_min) <= 300:
                return Ok({"ok": False, "error": "音高下限要填 30～300 Hz", "saved": {}})
            updates["f0_min"] = int(f0_min)
        if f0_max is not None:
            if not 200 <= int(f0_max) <= 1500:
                return Ok({"ok": False, "error": "音高上限要填 200～1500 Hz", "saved": {}})
            updates["f0_max"] = int(f0_max)
        if "f0_min" in updates and "f0_max" in updates and updates["f0_min"] >= updates["f0_max"]:
            return Ok({"ok": False, "error": "音高上限要高于下限", "saved": {}})
        if threshold is not None:
            if not -100 <= int(threshold) <= -10:
                return Ok({"ok": False, "error": "静音阈值要填 -100～-10 dB", "saved": {}})
            updates["threshold"] = int(threshold)
        if auto_key is not None:
            updates["auto_key"] = bool(auto_key)
        if auto_key_max is not None:
            if not 0 <= int(auto_key_max) <= 12:
                return Ok({"ok": False, "error": "最大移调要填 0～12 半音", "saved": {}})
            updates["auto_key_max"] = int(auto_key_max)
        if train_duration is not None:
            try:
                duration_value = float(train_duration)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "训练片段长度必须是数字", "saved": {}})
            if not 1.0 <= duration_value <= 10.0:
                return Ok({"ok": False, "error": "训练片段长度要填 1～10 秒", "saved": {}})
            updates["train_duration"] = round(duration_value, 2)
        if train_amp:
            if train_amp not in {"fp16", "bf16", "fp32"}:
                return Ok({"ok": False, "error": f"不支持的训练精度：{train_amp}", "saved": {}})
            updates["train_amp"] = train_amp
        if train_batch is not None:
            if not 0 <= int(train_batch) <= 64:
                return Ok({"ok": False, "error": "训练 batch 要填 0～64（0=自动）", "saved": {}})
            updates["train_batch"] = int(train_batch)
        if train_accum is not None:
            if not 1 <= int(train_accum) <= 16:
                return Ok({"ok": False, "error": "梯度累积要填 1～16", "saved": {}})
            updates["train_accum"] = int(train_accum)
        if train_cache_fp16 is not None:
            updates["train_cache_fp16"] = bool(train_cache_fp16)
        if train_pitch_aug is not None:
            updates["train_pitch_aug"] = bool(train_pitch_aug)
        if vocal_reverb is not None:
            try:
                verb = float(vocal_reverb)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "人声混响必须是数字", "saved": {}})
            if not 0.0 <= verb <= 0.6:
                return Ok({"ok": False, "error": "人声混响要填 0～0.6", "saved": {}})
            updates["vocal_reverb"] = round(verb, 3)
        if reverb_auto is not None:
            updates["reverb_auto"] = bool(reverb_auto)
        if reverb_auto_strength is not None:
            try:
                rs = float(reverb_auto_strength)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "混响跟随强度必须是数字", "saved": {}})
            if not 0.0 <= rs <= 1.0:
                return Ok({"ok": False, "error": "混响跟随强度要填 0～1", "saved": {}})
            updates["reverb_auto_strength"] = round(rs, 2)
        if stereo_width is not None:
            try:
                width = float(stereo_width)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "伴奏加宽必须是数字", "saved": {}})
            if not 1.0 <= width <= 2.0:
                return Ok({"ok": False, "error": "伴奏加宽要填 1.0～2.0", "saved": {}})
            updates["stereo_width"] = round(width, 2)
        if clarity is not None:
            try:
                cl = float(clarity)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "人声清晰度必须是数字", "saved": {}})
            if not 0.0 <= cl <= 1.5:
                return Ok({"ok": False, "error": "人声清晰度要填 0～1.5", "saved": {}})
            updates["clarity"] = round(cl, 2)
        if vocal_level_db is not None:
            try:
                vl = float(vocal_level_db)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "人声电平必须是数字", "saved": {}})
            if not -12.0 <= vl <= 6.0:
                return Ok({"ok": False, "error": "人声电平要填 -12～6 dB", "saved": {}})
            updates["vocal_level_db"] = round(vl, 2)
        if bgm_gain_db is not None:
            try:
                bg = float(bgm_gain_db)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "伴奏音量必须是数字", "saved": {}})
            if not -12.0 <= bg <= 12.0:
                return Ok({"ok": False, "error": "伴奏音量要填 -12～12 dB", "saved": {}})
            updates["bgm_gain_db"] = round(bg, 2)
        if consonant is not None:
            try:
                cs = float(consonant)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "咬字增强必须是数字", "saved": {}})
            if not 0.0 <= cs <= 0.8:
                return Ok({"ok": False, "error": "咬字增强要填 0～0.8", "saved": {}})
            updates["consonant"] = round(cs, 2)
        if cache_auto is not None:
            updates["cache_auto"] = bool(cache_auto)
        if cache_days is not None:
            try:
                cd = int(cache_days)
            except (TypeError, ValueError):
                return Ok({"ok": False, "error": "缓存保留天数必须是整数", "saved": {}})
            if not 1 <= cd <= 365:
                return Ok({"ok": False, "error": "缓存保留天数要填 1～365", "saved": {}})
            updates["cache_days"] = cd
        if device:
            if device not in {"auto", "cuda", "cpu"}:
                return Ok({"ok": False, "error": f"不支持的 device：{device}"})
            updates["device"] = device
        if precision:
            updates["precision"] = precision
        if max_parallel >= 0:
            updates["max_parallel"] = int(max_parallel)
        if download_source:
            updates["download_source"] = download_source
        if updates:
            runtime.save_config(self._data_dir, updates)
            self._reload_paths()
        paths = self._pipeline.paths
        plan = runtime.plan_for(runtime.probe_hardware(), paths.get("tier_override", ""),
                                paths.get("precision", "auto"),
                                paths.get("max_parallel", 0))
        return Ok({"ok": True, "saved": updates, "tier": plan,
                   "paths": {k: str(v) for k, v in paths.items()
                             if isinstance(v, (str, Path)) and k != "raw"}})

    @plugin_entry(id="ms_set_timbre", name="为她而唱 · 上传音色样本",
                  description="保存用户演唱/说话的样本留档；DDSP-SVC 训练音色时可直接引用此样本",
                  input_schema={"type": "object", "properties": {
                      "path": {"type": "string"},
                      "audio_base64": {"type": "string"},
                      "filename": {"type": "string"}}})
    async def ms_set_timbre(self, path: str = "", audio_base64: str = "",
                            filename: str = "timbre.wav", **_):
        try:
            if audio_base64:
                raw = base64.b64decode(audio_base64.split(",")[-1], validate=True)
                if len(raw) > 60 * 1024 * 1024:
                    return Ok({"ok": False, "error": "音色样本过大（上限 60MB）"})
                suffix = Path(filename or "timbre.wav").suffix or ".wav"
                target_dir = self._data_dir / "timbre"
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"sample{suffix}"
                target.write_bytes(raw)
            elif path:
                src = Path(path).expanduser()
                if not src.is_file():
                    return Ok({"ok": False, "error": f"文件不存在：{src}"})
                target_dir = self._data_dir / "timbre"
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / f"sample{src.suffix or '.wav'}"
                shutil.copyfile(src, target)
            else:
                return Ok({"ok": False, "error": "需要 path 或 audio_base64"})
            runtime.save_config(self._data_dir, {"timbre_path": str(target)})
            self._reload_paths()
            return Ok({"ok": True, "timbre_path": str(target), "bytes": target.stat().st_size})
        except (binascii.Error, OSError, ValueError) as exc:
            return Ok({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------------
    # training sessions: upload → auto-tune → milestone previews → user picks
    # ------------------------------------------------------------------
    def _train_python(self) -> Path:
        return Path(self._pipeline.paths.get("synth_python") or "")

    def _active_train_run_ids(self) -> set:
        """还没结束的训练 run（用于区分「正在训练的检查点」和残留检查点）。"""
        ids: set = set()
        try:
            for run_dir in self._train_runs_root().iterdir():
                if not run_dir.is_dir() or not runtime.valid_run_id(run_dir.name):
                    continue
                state = runtime.read_training_state(run_dir) or {}
                if state and str(state.get("phase") or "") not in runtime.TRAIN_ENDED:
                    ids.add(run_dir.name)
        except Exception:  # noqa: BLE001
            pass
        return ids

    def _train_repo(self) -> Path:
        return Path(str(self._pipeline.paths.get("ddsp_root") or ""))

    def _train_base_ckpt(self, quality: str = "") -> Path:
        """训练底模：按质量档位取对应权重，缺省回退到内置 6.3 底模。

        档位对应的路径可以在设置里用 ddsp_base_low / ddsp_base_mid /
        ddsp_base_high 指定；没配就用内置的 pretrain/reflow_base/model_0.pt。
        """
        raw = self._pipeline.paths.get("raw") or {}
        tier = str(quality or "mid").strip().lower()
        key = {"low": "ddsp_base_low", "mid": "ddsp_base_mid",
               "high": "ddsp_base_high"}.get(tier, "")
        chosen = str(raw.get(key) or "") if key else ""
        if chosen and Path(chosen).is_file():
            return Path(chosen)
        # 安装清单下载的档位底模（<ddsp_root>/pretrain/bases/low.pt|high.pt）。
        path_key = f"ddsp_base_{tier}" if tier in ("low", "mid", "high") else ""
        if path_key:
            installed = Path(str(self._pipeline.paths.get(path_key) or ""))
            if installed.is_file():
                return installed
        # 没显式配置时，优先找随仓库放在 pretrain/bases 下的官方档位底模。
        defaults = {
            "low": "ddsp6.3_6x512_6x512.sf_dlc",
            "high": "ddsp6.3_6x512_10x2048.sf_dlc",
        }
        if tier in defaults:
            candidate = (Path(str(self._pipeline.paths.get("ddsp_root") or ""))
                         / "pretrain" / "bases" / defaults[tier])
            if candidate.is_file():
                return candidate
        # 同一目录下其它名字的官方包：跟首次检测共用一套判定（结构优先），
        # 免得检测说「已就绪」、训练却悄悄回退到中档。
        if path_key:
            found = setup_mod.resolve_tier_base(self._pipeline.paths, tier, installed)
            if found is not None:
                return found
        return Path(str(self._pipeline.paths.get("ddsp_pretrain_reflow") or ""))

    def _train_runs_root(self) -> Path:
        root = Path(str(self._pipeline.paths.get("train_runs_dir")
                         or self._pipeline.paths.get("compute_root") or ""))
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _train_run_dir(self, run_id: str) -> Path:
        if not runtime.valid_run_id(run_id):
            raise ValueError(f"运行编号格式不对：{run_id!r}（应为 YYYYMMDD_HHMMSS）")
        return self._train_runs_root() / run_id

    def _train_worker(self, name: str) -> Path:
        return self._plugin_dir / "workers" / name

    def _train_writable_dir(self) -> Path:
        """Where uploaded audio / chosen previews are stored (writable check)."""
        root = self._pipeline.paths.get("compute_root")
        for candidate in (root, self._data_dir):
            if candidate:
                try:
                    Path(candidate).mkdir(parents=True, exist_ok=True)
                    return Path(candidate)
                except OSError:
                    continue
        return self._data_dir

    def _train_ready(self) -> list:
        """Everything one real training run needs; empty list means ready."""
        problems = []
        if not self._pipeline.paths.get("configured"):
            return ["安装位置（还没选择）"]
        python = self._train_python()
        repo = self._train_repo()
        if not python.is_file():
            problems.append(f"DDSP 环境 Python {python}")
        if not repo.is_dir():
            problems.append(f"DDSP 仓库 {repo}")
        if not (repo / "train_reflow.py").is_file():
            problems.append("训练入口 train_reflow.py")
        base_ckpt = self._train_base_ckpt()
        if not base_ckpt.is_file():
            problems.append("训练组件（model_0.pt）")
        if not self._train_worker("train_session_worker.py").is_file():
            problems.append("训练后端 workers\\train_session_worker.py")
        if not self._train_worker("train_finalize_worker.py").is_file():
            problems.append("收尾后端 workers\\train_finalize_worker.py")
        return problems

    def _train_proc_live(self, run_id: str) -> subprocess.Popen | None:
        with self._train_lock:
            proc = self._train_procs.get(run_id)
        if proc is not None and proc.poll() is None:
            return proc
        return None

    def _default_preview_src(self) -> str:
        """设置里配置的试听干声（data/preview_src.wav 之类）；没有就返回空。"""
        raw = (self._pipeline.paths.get("raw") or {}) if hasattr(self, "_pipeline") else {}
        path = str(raw.get("preview_src") or "")
        return path if path and Path(path).is_file() else ""

    def _train_launch(self, run_id: str) -> dict:
        """Start (or adopt) the long-running supervisor for one run.

        The worker reads state.json itself, so the same call resumes training
        from the latest checkpoint, keeps waiting on awaiting_choice, or picks
        up a pending control.json — the plugin only supplies the job.
        """
        run_dir = self._train_run_dir(run_id)
        live = self._train_proc_live(run_id)
        if live is not None:
            return {"launched": False, "adopted": True, "pid": live.pid}
        state = runtime.read_training_state(run_dir)
        if state and runtime.pid_alive(state.get("pid")) and \
                (state.get("pid") or 0) != os.getpid():
            return {"launched": False, "adopted": True, "pid": state.get("pid"),
                    "note": "state.json 记录的进程仍在运行"}
        script = self._train_worker("train_session_worker.py")
        python = self._train_python()
        if not python.is_file():
            raise RuntimeError(f"计算环境 Python 不存在：{python}")
        if not script.is_file():
            raise RuntimeError(f"worker 脚本不存在：{script}")
        raw = self._pipeline.paths.get("raw") or {}
        job = {
            "repo": str(self._train_repo()),
            "python": str(python),
            "run_dir": str(run_dir),
            "name": str((state or {}).get("name") or run_dir.name),
            # 训练起点按本次选择的质量档位取底模；老 run 没有 quality 时走 mid。
            "init_from": str(self._train_base_ckpt(
                str((state or {}).get("quality") or "mid"))),
            "base_ckpt": str(self._train_base_ckpt(
                str((state or {}).get("quality") or "mid"))),
            "current_ckpt": str(self._pipeline.paths.get("ddsp_ckpt") or ""),
            "has_accompaniment": bool(((state or {}).get("data") or {}).get("has_accompaniment")),
            "preview_src": str((state or {}).get("preview_src") or self._default_preview_src() or ""),
            "milestones": list((state or {}).get("milestones") or []),
            "auto": bool((state or {}).get("auto")),
            # 高级设置：试听步数与下一次训练的片段长度/精度/缓存/音高增广
            "preview_step": _int_setting(raw, "preview_step", 120, 5, 400),
            "train_duration": _float_setting(raw, "train_duration", 2.0, 1.0, 10.0),
            "train_amp": str(raw.get("train_amp") or "bf16"),
            "train_batch": _int_setting(raw, "train_batch", 0, 0, 64),
            "train_accum": _int_setting(raw, "train_accum", 1, 1, 16),
            "train_cache_fp16": bool(raw.get("train_cache_fp16", False)),
            "train_pitch_aug": bool(raw.get("train_pitch_aug", True)),
            "train_runs_dir": str(self._train_runs_root()),
            "log": str(run_dir / "supervisor.log"),
        }
        job_path = run_dir / "session_job.json"
        runtime._atomic_write(job_path, job)
        log_path = run_dir / "supervisor.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log_path, "ab")
        try:
            # -X utf8 + plain interpreter (no -I: the worker imports nothing from
            # this repo, but PYTHONPATH is stripped by _clean_env like run_worker).
            proc = subprocess.Popen(
                [str(python), "-X", "utf8", "-u", str(script),
                 str(job_path), str(run_dir / "session_result.json")],
                cwd=str(run_dir), env=runtime._clean_env(),
                stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        finally:
            handle.close()
        with self._train_lock:
            self._train_procs[run_id] = proc
        state = runtime.read_training_state(run_dir)
        if state is not None and not state.get("pid"):
            state["pid"] = proc.pid
            try:
                runtime._atomic_write(run_dir / "state.json", state)
            except OSError:
                pass
        self.logger.info("训练任务 %s 已启动 supervisor pid=%s", run_id, proc.pid)
        return {"launched": True, "adopted": False, "pid": proc.pid}

    def _ensure_session(self, run_id: str) -> dict:
        """Give a run its supervisor back if it is gone but not finished."""
        run_dir = self._train_run_dir(run_id)
        state = runtime.read_training_state(run_dir)
        if not state:
            return {"launched": False, "reason": "no_state"}
        if str(state.get("phase") or "") in runtime.TRAIN_ENDED:
            return {"launched": False, "reason": "ended"}
        if runtime.pid_alive(state.get("pid")):
            return {"launched": False, "adopted": True, "pid": state.get("pid")}
        return self._train_launch(run_id)

    def _resume_stale_training(self) -> list:
        """Plugin startup: every unfinished run without a live supervisor."""
        if not self._train_python().is_file():
            return []
        if not self._train_worker("train_session_worker.py").is_file():
            return []
        root = self._train_runs_root()
        resumed = []
        for item in runtime.scan_training_stale(root):
            try:
                info = self._train_launch(item["run_id"])
                resumed.append({**item, **info})
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("恢复训练 %s 失败：%s", item.get("run_id"), exc)
                resumed.append({**item, "launched": False, "error": str(exc)})
        if resumed:
            self.logger.info("已恢复 %d 个训练任务", len(resumed))
        return resumed

    # ------------------------------------------------------------------
    # 「翻唱 → 原创」训练串联
    # ------------------------------------------------------------------
    @staticmethod
    def _read_chain(run_dir: Path) -> dict:
        try:
            data = json.loads((Path(run_dir) / "chain.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_chain(run_dir: Path, updates: dict) -> dict:
        path = Path(run_dir) / "chain.json"
        chain: dict = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                chain = data
        except (OSError, ValueError):
            chain = {}
        chain.update(updates or {})
        chain["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        runtime._atomic_write(path, chain)
        return chain

    def _start_ace_train_sync(self, name: str, models: list,
                              epochs: int = 0, rank: int = 0,
                              alpha: int = 0, batch: int = 0, accum: int = 0,
                              max_duration: float = 0.0) -> tuple[str, str]:
        """Call the async ACE trainer from a plain thread; returns (job_id, error)."""
        try:
            res = asyncio.run(self.ms_ace_train(
                name=name, models=list(models or []),
                epochs=int(epochs or 0), rank=int(rank or 0),
                alpha=int(alpha or 0), batch=int(batch or 0),
                gradient_accumulation=int(accum or 0),
                max_duration=float(max_duration or 0.0)))
            payload = getattr(res, "value", None)
            if not isinstance(payload, dict):
                payload = getattr(res, "data", None)
            if not isinstance(payload, dict):
                payload = res if isinstance(res, dict) else {}
        except Exception as exc:  # noqa: BLE001
            return "", f"{type(exc).__name__}: {exc}"
        if not isinstance(payload, dict) or not payload.get("ok") or not payload.get("job_id"):
            return "", str((payload or {}).get("error") or "原创训练没能开始")
        return str(payload["job_id"]), ""

    def _watch_train_chain(self, run_id: str) -> None:
        """Wait for the cover run to finish, then start ACE LoRA training.

        The cover run is launched with ``auto=True`` by ms_train_start when
        ace_models is present, so it finalizes the best checkpoint itself and
        ends as ``finished``.  Stopped/failed runs abort the chain.
        """
        run_dir = self._train_run_dir(run_id)
        if not run_dir.is_dir():
            return
        chain = self._read_chain(run_dir)
        if not chain or chain.get("done"):
            return
        deadline = time.time() + 7 * 24 * 3600
        # 1) 等翻唱结束
        while time.time() < deadline:
            if getattr(self, "_shutdown", False):
                return
            chain = self._read_chain(run_dir) or chain
            if chain.get("done"):
                return
            state = runtime.read_training_state(run_dir) or {}
            phase = str(state.get("phase") or "")
            if phase == "finished" and not state.get("stopped_at"):
                break
            if phase in ("failed", "stopped") or state.get("stopped_at"):
                self._write_chain(run_dir, {
                    "state": "aborted", "done": True,
                    "error": state.get("error") or "翻唱训练被停止/失败，未继续原创训练"})
                return
            time.sleep(5)
        else:
            self._write_chain(run_dir, {"state": "timeout", "done": True,
                                        "error": "等待翻唱训练完成超时"})
            return

        # 2) 启动 ACE 训练（如果上次重载丢在排队阶段，这里会重新起）
        chain = self._read_chain(run_dir) or chain
        job_id = str(chain.get("ace_job_id") or "")
        if not job_id:
            self._write_chain(run_dir, {"state": "ace_queued"})
            job_id, error = self._start_ace_train_sync(
                str(chain.get("ace_name") or ""), list(chain.get("ace_models") or []),
                int(chain.get("ace_epochs") or 0), int(chain.get("ace_rank") or 0),
                int(chain.get("ace_alpha") or 0), int(chain.get("ace_batch") or 0),
                int(chain.get("ace_accum") or 0),
                float(chain.get("ace_max_duration") or 0.0))
            if not job_id:
                self._write_chain(run_dir, {"state": "failed", "done": True,
                                            "error": error or "原创训练没能开始"})
                return
            self._write_chain(run_dir, {"state": "ace_running", "ace_job_id": job_id})
        else:
            self._write_chain(run_dir, {"state": "ace_running"})

        # 3) 跟到 ACE 训练结束，把结果写回 chain（页面据此显示）
        restarts = 0
        while time.time() < deadline:
            if getattr(self, "_shutdown", False):
                return
            with self._jobs_lock:
                found = job_id in self._jobs
                job = dict(self._jobs.get(job_id) or {})
            if not found:
                # 插件重载会带走内存里的 ACE 任务：重新起一次（最多两次）
                if restarts >= 2:
                    self._write_chain(run_dir, {"state": "failed", "done": True,
                                                "error": "原创训练任务丢失，重试后仍未完成"})
                    return
                restarts += 1
                job_id, error = self._start_ace_train_sync(
                    str(chain.get("ace_name") or ""), list(chain.get("ace_models") or []),
                    int(chain.get("ace_epochs") or 0), int(chain.get("ace_rank") or 0),
                    int(chain.get("ace_alpha") or 0), int(chain.get("ace_batch") or 0),
                    int(chain.get("ace_accum") or 0),
                    float(chain.get("ace_max_duration") or 0.0))
                if not job_id:
                    self._write_chain(run_dir, {"state": "failed", "done": True,
                                                "error": error})
                    return
                self._write_chain(run_dir, {"state": "ace_running", "ace_job_id": job_id})
                continue
            state_name = str(job.get("state") or "")
            if state_name in ("done", "failed"):
                self._write_chain(run_dir, {
                    "state": "done" if state_name == "done" else "failed",
                    "done": True,
                    "ace_detail": str(job.get("detail") or ""),
                    "error": "" if state_name == "done" else str(job.get("detail") or ""),
                    "lora_paths": job.get("lora_paths") or [],
                })
                return
            time.sleep(5)

    def _resume_train_chains(self) -> int:
        """Restart chain watchers after a plugin reload."""
        root = self._train_runs_root()
        resumed = 0
        try:
            dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            return 0
        for d in dirs:
            chain = self._read_chain(d)
            if chain and not chain.get("done"):
                self._thread(f"ms-chain-{d.name}", self._watch_train_chain, d.name)
                resumed += 1
        return resumed


    def _train_inputs(self, inputs, audio_base64, name: str) -> tuple[list, str]:
        """Normalise the requested inputs into paths on disk.

        Each entry of ``inputs`` may be a local path (string / {path: ...}),
        or an object carrying base64 audio ({audio_base64|data|base64,
        filename, mime_type}); those are decoded into the run's src folder.
        Returns (paths, error) — error is "" when everything is usable.
        """
        items = inputs if isinstance(inputs, (list, tuple)) else ([inputs] if inputs else [])
        single = audio_base64 if isinstance(audio_base64, str) else ""
        if single and not items:
            items = [{"audio_base64": single}]
        if not items:
            return [], "没有收到声音素材（inputs / audio_base64 都为空）"
        # Decoded payloads need a folder; make it before the run dir exists.
        staging = self._train_writable_dir() / "train_uploads" / \
            (time.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}")
        paths: list = []
        for index, item in enumerate(items):
            payload = ""
            filename = f"input_{index + 1:02d}.wav"
            if isinstance(item, dict):
                for key in ("path", "file", "source"):
                    if isinstance(item.get(key), str) and item[key].strip():
                        paths.append(item[key].strip())
                        break
                else:
                    for key in ("audio_base64", "data", "base64", "content"):
                        if isinstance(item.get(key), str) and item[key].strip():
                            payload = item[key].strip()
                            break
                    if item.get("filename") or item.get("name"):
                        filename = str(item.get("filename") or item.get("name"))
            elif isinstance(item, str) and item.strip():
                text = item.strip()
                if text.startswith("data:") or (len(text) > 4096 and " " not in text[:64]):
                    payload = text
                else:
                    paths.append(text)
                    continue
            if not payload:
                continue
            data_uri = payload.split(",", 1)[1] if payload.startswith("data:") else payload
            try:
                blob = base64.b64decode(data_uri, validate=False)
            except (binascii.Error, ValueError) as exc:
                return paths, f"第 {index + 1} 个素材 base64 解码失败：{exc}"
            if not blob:
                return paths, f"第 {index + 1} 个素材解码后为空"
            staging.mkdir(parents=True, exist_ok=True)
            safe = re.sub(r"[^\w.\-]+", "_", Path(filename.replace("\\", "/")).name)[:80] \
                or f"input_{index + 1:02d}.wav"
            if "." not in safe:
                safe += ".wav"
            target = staging / safe
            target.write_bytes(blob)
            paths.append(str(target))
        return paths, ""

    def _train_view(self, run_id: str) -> dict:
        run_dir = self._train_run_dir(run_id)
        view = runtime.describe_training_run(run_id, run_dir)
        view["ok"] = True
        # "stopping" is the worker's normal exit phase (it is not in
        # TRAIN_ENDED and no supervisor process survives it), so settle it
        # here instead of leaving the run forever "not ended".
        if view["phase"] == "stopping" and not view.get("alive") \
                and self._train_proc_live(run_id) is None:
            state = runtime.read_training_state(run_dir)
            if state is not None:
                state["phase"] = "finished"
                state["stopped_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                try:
                    runtime._atomic_write(run_dir / "state.json", state)
                except OSError:
                    pass
                view = runtime.describe_training_run(run_id, run_dir)
                view["ok"] = True
        proc = self._train_proc_live(run_id)
        if proc is not None:
            view["supervisor"] = "plugin"
        else:
            view["supervisor"] = "external" if view.get("alive") else "none"
        view["can"] = {
            "continue": view["phase"] in ("training", "awaiting_choice", "previewing", "preparing", "preprocessing"),
            "stop": view["phase"] not in runtime.TRAIN_ENDED and (view.get("alive") or view["supervisor"] == "plugin"),
            "keep": bool(view.get("previews")) and view["phase"] not in ("stopping",),
            "preview": bool(view.get("previews")),
        }
        # 手动模式：advice 判定过拟合/平台期但还没停 → overfit_prompt。
        advice = view.get("advice") or {}
        state = runtime.read_training_state(run_dir) or {}
        if (view["phase"] == "awaiting_choice"
                and not state.get("auto")
                and str(advice.get("code") or "") in ("stop_overfit", "stop_flat")):
            view["overfit_prompt"] = {
                "step": state.get("step"),
                "val_loss": (state.get("_last_val_loss")),
                "best_step": advice.get("best_step") or state.get("best_step"),
                "best_val": advice.get("best_val_loss") or state.get("best_val_loss"),
            }
        chain = self._read_chain(run_dir)
        if chain:
            view["chain"] = chain
        return view

    @plugin_entry(id="ms_base_inspect", name="为她而唱 · 检查训练底模",
                  description="识别底模的框架与网络结构；必要时自动转换成 DDSP-SVC 可训练的格式",
                  input_schema={"type": "object", "properties": {
                      "path": {"type": "string",
                               "description": "底模路径；留空则按 tier 解析"},
                      "tier": {"type": "string", "enum": ["low", "mid", "high"],
                               "description": "按质量档位取底模（path 为空时生效）"},
                      "convert": {"type": "boolean",
                                  "description": "转换并保存成普通 {global_step, model} 检查点"}}})
    async def ms_base_inspect(self, path: str = "", tier: str = "mid",
                              convert=False, **_):
        python = self._train_python()
        if not python.is_file():
            return Ok({"ok": False, "error": "DDSP 环境还没就绪"})
        target = Path(str(path).strip()) if str(path).strip() \
            else self._train_base_ckpt(tier)
        if not target.is_file():
            return Ok({"ok": False, "error": f"底模不存在：{target}",
                       "path": str(target)})
        job = {"path": str(target)}
        if _as_bool(convert):
            out_dir = self._train_runs_root() / "bases"
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return Ok({"ok": False, "error": f"无法创建转换目录：{exc}"})
            job["convert"] = str(out_dir / f"{target.stem}_{int(time.time())}.pt")
        result = runtime.run_worker(
            python, self._train_worker("base_ckpt.py"), job,
            cwd=self._train_repo(), timeout=600,
            log_path=self._pipeline.log_path)
        return Ok(result)

    @plugin_entry(id="ms_base_status", name="为她而唱 · 训练底模状态",
                  description="只读：查看低/中/高三个训练底模是否已安装",
                  input_schema={"type": "object", "properties": {}})
    async def ms_base_status(self, **_):
        tiers = {}
        for tier in ("low", "mid", "high"):
            path = self._train_base_ckpt(tier)
            exists = path.is_file()
            tiers[tier] = {
                "path": str(path), "exists": exists,
                "size_gb": round(path.stat().st_size / 1024 ** 3, 2) if exists else 0,
            }
        return Ok({"ok": True, "tiers": tiers,
                   "bases_dir": str(Path(str(self._pipeline.paths.get("ddsp_root") or ""))
                                     / "pretrain" / "bases")})

    @plugin_entry(id="ms_base_download", name="为她而唱 · 下载训练底模",
                  description="下载低/高质量档位的官方 DDSP 6.3 底模（后台，带进度）",
                  input_schema={"type": "object", "properties": {
                      "tier": {"type": "string", "enum": ["low", "high"]},
                      "force": {"type": "boolean"}}})
    async def ms_base_download(self, tier: str = "", force=False, **_):
        tier = str(tier or "").strip().lower()
        if tier not in ("low", "high"):
            return Ok({"ok": False,
                       "error": "只有 low / high 需要单独下载；mid 用内置底模"})
        if not self._pipeline.paths.get("configured"):
            return Ok({"ok": False, "error": "还没有选择安装位置"})
        spec = setup_mod.MODEL_ITEMS.get(f"ddsp_base_{tier}")
        target = Path(str(self._pipeline.paths.get(f"ddsp_base_{tier}") or ""))
        if not spec or not str(target):
            return Ok({"ok": False, "error": "这个档位没有配置底模路径"})
        if target.is_file() and not _as_bool(force):
            return Ok({"ok": True, "already": True, "tier": tier,
                       "path": str(target)})
        expected = float(spec.get("size_gb") or 0.0) * 1024 ** 3
        job_id = self._job_begin("ddsp-base-download")

        def run():
            self._job_update(job_id, state="running", progress=5,
                             detail=f"正在下载{tier}质量底模…")
            stop = threading.Event()
            tmp = target.with_name(target.name + ".dlc")

            def watch():
                last = -1
                while not stop.wait(2.0):
                    try:
                        cur = tmp.stat().st_size if tmp.is_file() else (
                            target.stat().st_size if target.is_file() else 0)
                    except OSError:
                        cur = 0
                    if expected <= 0:
                        continue
                    pct = max(5, min(95, int(5 + 90 * cur / expected)))
                    if pct == last:
                        continue
                    last = pct
                    self._job_update(
                        job_id, progress=pct,
                        detail=f"正在下载{tier}质量底模："
                               f"{cur / 1024 ** 3:.2f} / {expected / 1024 ** 3:.2f} GB")

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            try:
                setup_mod.install(
                    self._pipeline.paths, [f"ddsp_base_{tier}"],
                    lambda msg: self._job_update(job_id, detail=str(msg)))
            except Exception as exc:  # noqa: BLE001
                self._job_update(job_id, state="failed", progress=100,
                                 detail=f"{type(exc).__name__}: {exc}")
                return
            finally:
                stop.set()
            if target.is_file():
                self._job_update(job_id, state="done", progress=100,
                                 detail=f"{tier}质量底模已就绪：{target.name}")
            else:
                self._job_update(job_id, state="failed", progress=100,
                                 detail="下载结束但底模文件不存在，请重试")

        self._thread(f"ms-base-download-{job_id}", run)
        return Ok({"ok": True, "job_id": job_id, "tier": tier})

    @plugin_entry(id="ms_train_start", name="为她而唱 · 训练新音色",
                  description="用上传的声音素材训练一个属于自己的变声模型：自动整理数据、提特征、定参数，"
                              "每到里程碑停下生成一段试听，等你决定继续/停止/保留",
                  input_schema={"type": "object", "properties": {
                      "inputs": {"type": "array", "items": {"anyOf": [
                          {"type": "string"},
                          {"type": "object", "properties": {
                              "path": {"type": "string"},
                              "audio_base64": {"type": "string"},
                              "filename": {"type": "string"}}}]},
                          "description": "本地路径或 {audio_base64, filename} 列表"},
                      "audio_base64": {"type": "string", "description": "单个素材的 base64（data URI 也可）"},
                      "name": {"type": "string", "description": "音色名字，默认按时间命名"},
                      "has_accompaniment": {"type": "boolean", "description": "素材里带伴奏（会先分离人声）"},
                      "preview_src": {"type": "string", "description": "试听用的干声，留空用默认片段"},
                      "milestones": {"type": "array", "items": {"type": "integer"},
                                     "description": "里程碑步数，默认 1000/2000/4000/…"},
                      "steps": {"type": "integer", "minimum": 4000, "maximum": 200000,
                                "description": "总步数；不传按素材自动算（上限 30000），30000 以上可自定义继续"},
                      "quality": {"type": "string", "enum": ["low", "mid", "high"],
                                  "description": "翻唱训练质量档位；不同档位使用对应底模，并在显存不足时提醒"},
                      "from_corpus": {"type": "boolean",
                                      "description": "从语料库取材训练（质检后切片），而非直接上传素材"},
                      "auto": {"type": "boolean",
                               "description": "全自动：到里程碑不停下来等选择，按防过拟合建议自动继续/停止，"
                                              "停止后用现有收尾后端保留最佳步（不切换当前模型）"},
                      "ace_models": {"type": "array", "items": {"type": "string"},
                                     "description": "翻唱训练完成后接着训练的 ACE 底模列表；传了就自动跑完翻唱并保存最佳节点"},
                      "ace_name": {"type": "string",
                                   "description": "原创歌曲音色的名字（留空沿用翻唱训练的名字）"},
                      "ace_epochs": {"type": "integer", "minimum": 0, "maximum": 100},
                      "ace_rank": {"type": "integer", "minimum": 0, "maximum": 128},
                      "ace_alpha": {"type": "integer", "minimum": 0, "maximum": 256},
                      "ace_batch": {"type": "integer", "minimum": 0, "maximum": 8},
                      "ace_accum": {"type": "integer", "minimum": 0, "maximum": 32},
                      "ace_max_duration": {"type": "number"}}})
    async def ms_train_start(self, inputs=None, audio_base64: str = "", name: str = "",
                             has_accompaniment=False, preview_src: str = "",
                             init_from: str = "", milestones=None, auto=False,
                             steps: int = 0, quality: str = "mid", from_corpus=False,
                             ace_models=None, ace_name: str = "",
                             ace_epochs: int = 0, ace_rank: int = 0,
                             ace_alpha: int = 0, ace_batch: int = 0,
                             ace_accum: int = 0, ace_max_duration: float = 0.0, **_):
        # init_from 只为兼容旧面板而保留；训练起点固定走内置底模，传什么都会被忽略。
        problems = self._train_ready()
        if problems:
            return Ok({"ok": False, "error": "训练后端还没就绪，缺少 " + "、".join(problems)})
        # 训练和唱歌是同一个 GPU 的两条重负载路径：一方在跑，另一方必须等或拒绝。
        singing = self._singing_active()
        if singing:
            return Ok({"ok": False, "busy": singing,
                       "error": "正在唱歌/生成歌曲，训练已暂停：两者不能同时占用显存和算力。"
                                "请等唱歌结束后再训练。（本次没有启动训练）"})
        other_train = self._training_active()
        if other_train:
            return Ok({"ok": False, "busy": other_train,
                       "error": "已有音色训练在占用显存，请等它结束或先停止再开新训练。"})
        quality = str(quality or "mid").strip().lower()
        if quality not in ("low", "mid", "high"):
            quality = "mid"
        qplan = {
            "low": {"label": "低", "vram": 4.0, "steps": 12000},
            "mid": {"label": "中", "vram": 6.0, "steps": 24000},
            "high": {"label": "高", "vram": 10.0, "steps": 40000},
        }[quality]
        quality_warnings = []
        try:
            probe = runtime.probe_hardware(force=False) or {}
            vram_total = float((probe.get("gpu") or {}).get("total_gib") or 0.0)
        except Exception:  # noqa: BLE001
            vram_total = 0.0
        if vram_total and vram_total + 0.5 < qplan["vram"]:
            quality_warnings.append(
                f"{qplan['label']}质量建议 ≥{qplan['vram']:.0f}GB 显存，当前只有 "
                f"{vram_total:.1f}GB；可能很慢或显存不足")
        try:
            free_gb = shutil.disk_usage(str(self._train_runs_root())).free / 1024 ** 3
        except OSError:
            free_gb = 0.0
        if free_gb and free_gb < 5.0:
            quality_warnings.append(
                f"训练盘只剩 {free_gb:.1f}GB，建议先清理出 5GB 以上")
        quality_base = self._train_base_ckpt(quality)
        if not quality_base.is_file():
            return Ok({"ok": False,
                       "error": f"{qplan['label']}质量档位的训练底模不存在：{quality_base}"})
        try:
            mid_base = Path(str(self._pipeline.paths.get("ddsp_pretrain_reflow") or ""))
            if quality in ("low", "high") and mid_base.is_file() and \
                    quality_base.resolve() == mid_base.resolve():
                quality_warnings.append(
                    f"{qplan['label']}质量底模还没安装，本次回退到 6.3 标准底模")
        except OSError:
            pass
        # 「翻唱 → 原创」串联：传了 ace_models 就必须让翻唱自动跑完并保留最佳节点，
        # 否则翻唱会停在 awaiting_choice，后面的原创训练永远等不到。
        if isinstance(ace_models, str):
            ace_models_list = [Path(ace_models).name] if ace_models.strip() else []
        elif isinstance(ace_models, (list, tuple)):
            ace_models_list = [Path(str(m)).name for m in ace_models if str(m).strip()]
        else:
            ace_models_list = []
        auto = _as_bool(auto) or bool(ace_models_list)
        # 语料库取材：从 corpus/index.json 收集质检通过切片，作为训练素材。
        from_corpus = _as_bool(from_corpus)
        if from_corpus:
            steps = int(steps or 0)
            view = self._corpus_scan(self._corpus_index())
            valid_s = float(view.get("total_valid_s") or 0.0)
            files_s = float(view.get("total_files_s") or 0.0)
            # 没指定步数就按「每个素材文件的实际总时长」自动算（前端也是这么显示的）；
            # 但绝不超过已质检切片撑得起的步数，免得算完自己把自己拦住。
            auto_steps = steps <= 0
            if auto_steps:
                steps = self._steps_for_seconds(files_s if files_s > 0 else valid_s)
            steps = max(self._STEPS_MIN, min(self._STEPS_HARD_MAX, steps))
            if auto_steps and valid_s > 0:
                steps = min(steps, self._steps_for_seconds(valid_s))
            min_s, _rec_s = self._req_for_steps(steps)
            # 手填步数（比如 30000 以上接着练）只要求「够训练的最低素材量」，
            # 不再按步数线性抬门槛——否则想多练就永远提示素材不够。
            need_s = min_s if auto_steps else min(min_s, 180.0)
            if valid_s < need_s:
                return Ok({"ok": False, "error": "有效时长不足",
                           "need_s": need_s, "have_s": round(valid_s, 1)})
            corpus_paths = [c.get("path") for c in view.get("clips", [])
                            if c.get("status") == "ok" and c.get("path")]
            if not corpus_paths:
                return Ok({"ok": False, "error": "语料库里没有通过质检的切片"})
            # 复用现有 inputs 归一化逻辑：corpus 切片直接作为 inputs 路径列表。
            stems = [p for p in corpus_paths if Path(p).is_file()]
            if not stems:
                return Ok({"ok": False, "error": "语料切片文件缺失"})
            error = ""
        else:
            stems, error = await asyncio.to_thread(self._train_inputs, inputs, audio_base64, name)
        if error:
            return Ok({"ok": False, "error": error})
        if not stems:
            return Ok({"ok": False, "error": "没有收到可用的声音素材"})
        missing = [p for p in stems if not Path(p).is_file()]
        if missing:
            return Ok({"ok": False, "error": "素材文件不存在：" + "、".join(missing[:3])})
        # A source stem never contains itself: drop duplicates, keep order.
        unique = list(dict.fromkeys(stems))
        root = self._train_runs_root()
        run_id = runtime.new_run_id({d.name for d in root.iterdir() if d.is_dir()})
        run_dir = root / run_id
        for sub in ("src", "previews", "data"):
            (run_dir / sub).mkdir(parents=True, exist_ok=True)
        copied = []
        for src_path in unique:
            src_path = Path(src_path)
            flat = re.sub(r"[^\w.\-]+", "_", src_path.name)[:80] or "input.wav"
            target = run_dir / "src" / flat
            try:
                if src_path.resolve() == target.resolve():
                    continue
                if not target.exists():
                    shutil.copy2(src_path, target)
            except OSError as exc:
                return Ok({"ok": False, "error": f"复制素材失败：{src_path} → {exc}"})
            copied.append({"copied": str(target), "source": str(src_path)})
        if not copied:
            return Ok({"ok": False, "error": "素材复制到 run 目录失败"})
        clean_name = re.sub(r"[\\/:*?\"<>|\s]+", "_", str(name or "").strip())[:40] \
            or ("voice_" + run_id)
        preview_src = str(preview_src or "").strip()
        if preview_src and not Path(preview_src).is_file():
            return Ok({"ok": False, "error": f"试听素材不存在：{preview_src}"})
        if not preview_src:
            # 默认用设置里配的「试听干声」；没配就挑一段 20~60 秒的干净切片当底稿
            _raw = self._pipeline.paths.get("raw") or {}
            preview_src = str(_raw.get("preview_src") or "")
            if preview_src and not Path(preview_src).is_file():
                preview_src = ""
        if not preview_src:
            try:
                _view = self._corpus_scan(self._corpus_index())
                _cands = [c for c in _view.get("clips", [])
                          if c.get("status") == "ok" and c.get("path")
                          and Path(str(c["path"])).is_file()]
                _cands.sort(key=lambda c: abs(float(c.get("dur_s") or 0) - 30.0))
                if _cands:
                    preview_src = str(_cands[0]["path"])
            except Exception:  # noqa: BLE001
                preview_src = ""
        milestone_list = []
        if isinstance(milestones, (list, tuple)):
            for item in milestones:
                try:
                    step = int(item)
                except (TypeError, ValueError):
                    continue
                if step > 0 and step not in milestone_list:
                    milestone_list.append(step)
        steps_total = int(steps or 0)
        if steps_total and not milestone_list:
            # 节点默认每 2000 步；超过 30000 的长训练改成每 5000 步，别弹出上百个节点
            interval = 2000 if steps_total <= 30000 else 5000
            milestone_list = list(range(interval, steps_total + 1, interval))
        milestone_list.sort()
        # 训练起点按质量档位取底模（low/mid/high）；init_from 仍只做兼容。
        base_ckpt = self._train_base_ckpt(quality)
        if not base_ckpt.is_file():
            return Ok({"ok": False, "error": "训练组件缺失，请重新安装唱歌程序"})
        init_choice = str(base_ckpt)
        state = {
            "run_id": run_id, "name": clean_name, "phase": "preparing",
            "step": 0, "next_milestone": (milestone_list or [None])[0],
            "milestones": milestone_list, "params": {}, "data": {
                "inputs": [c["copied"] for c in copied], "clips_train": 0, "clips_val": 0,
                "total_sec": 0, "dropped": [], "has_accompaniment": _as_bool(has_accompaniment),
            },
            "previews": [], "advice": {}, "speed": {"steps_per_sec": None},
            "pid": None, "error": "", "init_from": init_choice, "quality": quality,
            "auto": _as_bool(auto), "best_step": None, "best_val_loss": None,
            "steps_total": steps_total, "pause_at": steps_total, "from_corpus": from_corpus,
            "live": {"step": 0, "base_step": 0, "batch": 0, "batch_per_s": None,
                     "updated_at": ""},
            "finalize": {}, "finalize_error": "",
            "preview_src": preview_src, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        runtime._atomic_write(run_dir / "state.json", state)
        # 训练要显存吃得比较满：先把「为她所听」这些常驻 GPU 模型停掉，
        # 训练收尾再由后台线程拉回来（只重启这次真的停掉的）。
        freed: dict = {}
        try:
            freed = await self._free_vram_for_training()
            if freed.get("summary"):
                quality_warnings.append(freed["summary"])
        except Exception as exc:  # noqa: BLE001 - 停不下来也得继续训练
            self.logger.warning("[train] 释放显存失败：%s", exc)
            quality_warnings.append(f"没能暂停其它占显存的插件：{exc}")
        try:
            info = self._train_launch(run_id)
        except Exception as exc:  # noqa: BLE001
            state["phase"] = "failed"
            state["error"] = f"{type(exc).__name__}: {exc}"
            runtime._atomic_write(run_dir / "state.json", state)
            return Ok({"ok": False, "error": state["error"], "run_id": run_id})
        paused = list(freed.get("stopped") or [])
        yielded = bool(freed.get("paused"))
        if paused or yielded:
            self._thread(f"ms-vram-restore-{run_id}", self._restore_after_training,
                         paused, yielded)
        if ace_models_list:
            chain = {
                "ace_models": ace_models_list,
                "ace_name": str(ace_name or clean_name),
                "ace_epochs": max(0, min(100, int(ace_epochs or 0))),
                "ace_rank": max(0, min(128, int(ace_rank or 0))),
                "ace_alpha": max(0, min(256, int(ace_alpha or 0))),
                "ace_batch": max(0, min(8, int(ace_batch or 0))),
                "ace_accum": max(0, min(32, int(ace_accum or 0))),
                "ace_max_duration": max(0.0, min(240.0, float(ace_max_duration or 0.0))),
                "state": "waiting_cover",
                "ace_job_id": "",
                "error": "",
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "done": False,
            }
            runtime._atomic_write(run_dir / "chain.json", chain)
            self._thread(f"ms-chain-{run_id}", self._watch_train_chain, run_id)
        return Ok({"ok": True, "run_id": run_id, "dir": str(run_dir), "name": clean_name,
                   "quality": quality, "warnings": quality_warnings,
                   "inputs": [c["copied"] for c in copied], "pid": info.get("pid"),
                   "auto": _as_bool(auto), "ace_models": ace_models_list,
                   "vram_paused": paused + (["music_perception:models"] if yielded else [])})

    @plugin_entry(id="ms_train_runs", name="为她而唱 · 训练任务列表",
                  description="列出所有训练任务（正在训练 / 等你选择 / 已结束）",
                  input_schema={"type": "object", "properties": {}})
    async def ms_train_runs(self, **_):
        root = self._train_runs_root()
        runs = []
        for run_dir in sorted(root.iterdir(), reverse=True):
            if not run_dir.is_dir() or not runtime.valid_run_id(run_dir.name):
                continue
            # 没有 state.json 的空目录（训练进程被杀/删除后残留）不算一条训练记录
            if not runtime.read_training_state(run_dir):
                continue
            try:
                view = self._train_view(run_dir.name)
            except Exception as exc:  # noqa: BLE001
                runs.append({"run_id": run_dir.name, "ok": False, "error": str(exc)})
                continue
            view.pop("data", None)
            view.pop("dataset", None)
            runs.append(view)
        active = [r for r in runs if r.get("phase") not in runtime.TRAIN_ENDED]
        return Ok({"ok": True, "runs_dir": str(root), "runs": runs,
                   "active": len(active), "total": len(runs)})

    @plugin_entry(id="ms_train_status", name="为她而唱 · 训练进度",
                  description="查看一次训练：阶段、步数、下一里程碑、试听列表、系统建议（只读）",
                  input_schema={"type": "object", "properties": {
                      "run_id": {"type": "string"}}})
    async def ms_train_status(self, run_id: str = "", **_):
        if not run_id:
            listed = await self.ms_train_runs()
            runs = (getattr(listed, "value", listed) or {}).get("runs") or []
            active = [r for r in runs if r.get("phase") not in runtime.TRAIN_ENDED]
            if not active:
                return Ok({"ok": False, "error": "还没有进行中的训练任务", "runs": runs[:5]})
            run_id = active[0]["run_id"]
        run_dir = self._train_run_dir(run_id)
        if not runtime.read_training_state(run_dir):
            return Ok({"ok": False, "error": f"找不到训练任务 {run_id} 的状态文件"})
        view = self._train_view(run_id)
        # The supervisor writes state.json; if it is gone the plugin revives it.
        if view["supervisor"] == "none" and view["phase"] not in runtime.TRAIN_ENDED:
            if self._singing_active():
                # 唱歌占着显存时不要悄悄把训练 supervisor 拉起来。
                view["resume"] = {"launched": False, "reason": "singing_active"}
            else:
                try:
                    view["resume"] = self._ensure_session(run_id)
                except Exception as exc:  # noqa: BLE001
                    view["resume"] = {"launched": False, "error": str(exc)}
        return Ok(self._training_payload(view))

    @plugin_entry(id="ms_train_control", name="为她而唱 · 训练指令",
                  description="对训练任务下指令：continue=继续训练（可带 steps 再加多少步），stop=停下（保留已产出的检查点）",
                  input_schema={"type": "object", "properties": {
                      "run_id": {"type": "string"},
                      "action": {"type": "string", "enum": ["continue", "stop"]},
                      "steps": {"type": "integer", "minimum": 0, "maximum": 500000}},
                      "required": ["run_id", "action"]})
    async def ms_train_control(self, run_id: str = "", action: str = "", steps: int = 0, **_):
        action = str(action or "").strip().lower()
        if action not in runtime.TRAIN_ACTIONS:
            return Ok({"ok": False, "error": f"未知指令 {action!r}，只能是 continue / stop"})
        run_dir = self._train_run_dir(run_id)
        state = runtime.read_training_state(run_dir)
        if not state:
            return Ok({"ok": False, "error": f"找不到训练任务 {run_id}"})
        if state.get("phase") in runtime.TRAIN_ENDED:
            return Ok({"ok": False, "error": f"任务 {run_id} 已结束（{state.get('phase')}）"})
        if action == "continue":
            singing = self._singing_active()
            if singing:
                return Ok({"ok": False, "busy": singing,
                           "error": "正在唱歌/生成歌曲，暂时不能继续训练：两者不能同时占用显存。"})
        payload = runtime.write_training_control(run_dir, action, steps=int(steps or 0))
        info = {"launched": False, "reason": "already_running"}
        try:
            info = self._ensure_session(run_id)
        except Exception as exc:  # noqa: BLE001
            info = {"launched": False, "error": str(exc)}
        view = self._train_view(run_id)
        return Ok({"ok": True, "run_id": run_id, "action": action, "seq": payload["seq"],
                   "supervisor": info, "phase": view.get("phase")})

    @plugin_entry(id="ms_train_keep", name="为她而唱 · 保留这一步",
                  description="把选中的里程碑保存成正式模型（其余检查点移入回收站不永久删除）；"
                              "set_current=true 时才设为当前变声模型",
                  input_schema={"type": "object", "properties": {
                      "run_id": {"type": "string"},
                      "step": {"type": "integer", "minimum": 1},
                      "name": {"type": "string"},
                      "set_current": {"type": "boolean"}}})
    async def ms_train_keep(self, run_id: str = "", step: int = 0, name: str = "",
                            set_current=False, **_):
        try:
            step = int(step)
        except (TypeError, ValueError):
            return Ok({"ok": False, "error": f"步数不合法：{step!r}"})
        run_dir = self._train_run_dir(run_id)
        state = runtime.read_training_state(run_dir)
        if not state:
            return Ok({"ok": False, "error": f"找不到训练任务 {run_id}"})
        if state.get("phase") in ("training", "preparing", "preprocessing", "previewing"):
            return Ok({"ok": False,
                       "error": "这条训练还在跑，先点“停止”再保留这一步"})
        script = self._train_worker("train_finalize_worker.py")
        if not script.is_file():
            return Ok({"ok": False, "error": f"缺少收尾后端 {script}"})
        ckpt = runtime.find_training_ckpt(run_dir, step)
        if ckpt is None:
            # The session worker owns the expdir; make sure it is current
            # before giving up (it may have recorded it after our state read).
            expdir = run_dir / "expdir.txt"
            try:
                if expdir.is_file():
                    ckpt = Path(str(expdir.read_text(encoding="utf-8").strip())) / f"model_{step}.pt"
                    ckpt = ckpt if ckpt.is_file() else None
            except OSError:
                ckpt = None
        if ckpt is None:
            return Ok({"ok": False, "error": f"任务 {run_id} 没有第 {step} 步的检查点"})
        python = self._train_python()
        job = {"repo": str(self._train_repo()), "python": str(python),
               "run_dir": str(run_dir), "step": step,
               "name": str(name or state.get("name") or run_id)}

        def call():
            return runtime.run_worker(
                python, script, job, cwd=self._train_writable_dir(), timeout=600,
                log_path=run_dir / "finalize.log",
                env_extra={"PYTHONPATH": str(self._train_repo())})

        try:
            result = await asyncio.to_thread(call)
        except Exception as exc:  # noqa: BLE001
            return Ok({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if not result.get("ok"):
            return Ok({"ok": False, "error": result.get("error") or "保留失败",
                       "trace": str(result.get("trace") or "")[-1500:]})
        out = {"ok": True, "run_id": run_id, "step": step,
               "ckpt": result.get("ckpt"), "config": result.get("config"),
               "name": result.get("name") or job["name"],
               "trash_manifest": result.get("trash_manifest"), "set_current": False}
        if _as_bool(set_current) and result.get("ckpt"):
            # Only explicit consent switches the engine; the same checks as
            # ms_set_config run first so a broken checkpoint cannot be saved.
            target = Path(str(result["ckpt"]))
            if not target.is_file():
                out.update({"ok": False, "error": f"保留后的模型文件不存在：{target}"})
                return Ok(out)
            if not (target.parent / "config.yaml").is_file():
                out.update({"ok": False,
                            "error": f"模型同目录缺少 config.yaml：{target.parent / 'config.yaml'}"})
                return Ok(out)
            runtime.save_config(self._data_dir, {"ddsp_ckpt": str(target)})
            self._reload_paths()
            out["set_current"] = True
            out["ddsp_ckpt"] = str(self._pipeline.paths.get("ddsp_ckpt") or "")
        return Ok(out)

    @plugin_entry(id="ms_train_preview", name="为她而唱 · 试听里程碑",
                  description="取某个里程碑的试听音频（base64 WAV，面板直接播放）",
                  input_schema={"type": "object", "properties": {
                      "run_id": {"type": "string"},
                      "step": {"type": "integer", "minimum": 1}},
                      "required": ["run_id"]})
    async def ms_train_preview(self, run_id: str = "", step: int = 0, **_):
        demo = self._bundled_voice(run_id)
        if demo:
            payload = await asyncio.to_thread(self._audio_payload, Path(demo["demo"]))
            return Ok({"ok": True, "run_id": run_id, "step": 0,
                       "file": demo["demo"], "name": Path(demo["demo"]).name,
                       "bundled": True, "stats": {}, **payload})
        run_dir = self._train_run_dir(run_id)
        state = runtime.read_training_state(run_dir)
        if not state:
            return Ok({"ok": False, "error": f"找不到训练任务 {run_id}"})
        previews = [p for p in (state.get("previews") or []) if isinstance(p, dict)]
        item = None
        if step:
            try:
                item = next((p for p in previews if int(p.get("step") or -1) == int(step)), None)
            except (TypeError, ValueError):
                item = None
        if item is None and previews:
            item = previews[-1]
        if item is None or not item.get("wav"):
            return Ok({"ok": False, "error": "这一步还没有试听音频"})
        wav = Path(str(item["wav"]))
        if not wav.is_file():
            return Ok({"ok": False, "error": f"试听文件不存在：{wav}"})
        if wav.stat().st_size > 200 * 1024 * 1024:
            return Ok({"ok": False, "error": f"试听文件过大（{wav.stat().st_size // 1024 // 1024} MB）"})
        try:
            payload = await asyncio.to_thread(self._audio_payload, wav)
        except OSError as exc:
            return Ok({"ok": False, "error": f"读取失败：{exc}"})
        stats = {}
        if wav.suffix.lower() == ".wav":
            try:
                stats = await asyncio.to_thread(runtime.wav_stats, wav)
            except Exception:  # noqa: BLE001
                stats = {}
        return Ok({"ok": True, "run_id": run_id, "step": item.get("step"),
                   "file": str(wav), "name": wav.name,
                   "stats": stats,
                   "val_loss": item.get("val_loss"), "flag": item.get("flag") or "",
                   **payload})

    # ------------------------------------------------------------------
    # capture / split / process
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 磁盘缓存清理
    # ------------------------------------------------------------------
    def _cache_root(self) -> Path:
        return Path(str(self._pipeline.paths.get("compute_root") or ""))

    def _cache_settings(self) -> dict:
        raw = self._pipeline.paths.get("raw") or {}
        return {"days": _int_setting(raw, "cache_days", 7, 1, 365),
                "auto": bool(raw.get("cache_auto", True))}

    def _auto_cache_cleanup(self) -> None:
        settings = self._cache_settings()
        if not settings["auto"]:
            return
        marker = self._data_dir / "cache_cleanup.json"
        try:
            last = float(json.loads(marker.read_text(encoding="utf-8")).get("last_run_ts") or 0)
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < 86400:
            return
        result = cache_cleanup.cleanup(
            self._cache_root(), self._data_dir, self._plugin_dir,
            days=settings["days"], include_trash=True, include_train_data=True,
            include_track_stages=True, include_preview_cache=True, dry_run=False)
        try:
            marker.write_text(json.dumps({
                "last_run_ts": time.time(),
                "removed_bytes": result.get("removed_bytes"),
                "days": settings["days"],
            }, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        self.logger.info("缓存清理完成：释放 %.1f MB（保留 %s 天）",
                         (result.get("removed_bytes") or 0) / 1024 / 1024, settings["days"])

    @plugin_entry(id="ms_cache_report", name="为她而唱 · 磁盘空间",
                  description="只读：统计回收站、训练特征、曲目中间产物、试听缓存各占多少空间",
                  input_schema={"type": "object", "properties": {}})
    async def ms_cache_report(self, **_):
        settings = self._cache_settings()
        usage = await asyncio.to_thread(cache_cleanup.usage, self._cache_root())
        report = await asyncio.to_thread(
            cache_cleanup.scan, self._cache_root(), self._data_dir,
            self._plugin_dir, settings["days"])
        return Ok({"ok": True, "settings": settings, "usage": usage,
                   "scan": report})

    @plugin_entry(id="ms_empty_trash", name="为她而唱 · 清空回收站",
                  description="删除回收站（_trash_*）里的全部内容，不看保留天数、不可恢复",
                  input_schema={"type": "object", "properties": {}})
    async def ms_empty_trash(self, **_):
        job_id = self._job_begin("trash")

        def runner():
            try:
                result = cache_cleanup.empty_trash(self._cache_root(), dry_run=False)
                freed = float(result.get("removed_bytes") or 0) / (1024 ** 3)
                count = len(result.get("removed") or [])
                self._job_update(
                    job_id, state="done" if result.get("ok") else "failed", progress=100,
                    detail=(f"回收站已清空：删除 {count} 项，释放 {freed:.1f} GB"
                            if result.get("ok")
                            else "部分文件删除失败：" + str((result.get("errors") or [{}])[0].get("error"))),
                    removed_bytes=result.get("removed_bytes"))
            except Exception as exc:  # noqa: BLE001
                self._job_update(job_id, state="failed", progress=100,
                                 detail=f"{type(exc).__name__}: {exc}")

        self._thread("trash-empty", runner)
        return Ok({"ok": True, "started": True, "job_id": job_id,
                   "note": "正在后台清空回收站"})

    @plugin_entry(id="ms_cache_cleanup", name="为她而唱 · 清理磁盘缓存",
                  description="清理回收站/训练特征/曲目中间产物/试听缓存；dry_run=true 只统计不删除",
                  input_schema={"type": "object", "properties": {
                      "days": {"type": "integer", "minimum": -1, "maximum": 365,
                               "description": "-1=用设置里的保留天数；0=不按时间，全部清理"},
                      "dry_run": {"type": "boolean"},
                      "include_trash": {"type": "boolean"},
                      "include_train_data": {"type": "boolean"},
                      "include_track_stages": {"type": "boolean"},
                      "include_preview_cache": {"type": "boolean"},
                      "include_experiments": {"type": "boolean"},
                      "include_splits": {"type": "boolean"},
                      "include_exp_checkpoints": {"type": "boolean"}}})
    async def ms_cache_cleanup(self, days: int = -1, dry_run: bool = True,
                               include_trash: bool = False,
                               include_train_data: bool = True,
                               include_track_stages: bool = True,
                               include_preview_cache: bool = True,
                               include_experiments: bool = False,
                               include_splits: bool = False,
                               include_exp_checkpoints: bool = False, **_):
        settings = self._cache_settings()
        days = settings["days"] if int(days) < 0 else int(days)
        kwargs = dict(
            days=days, include_trash=bool(include_trash),
            include_train_data=bool(include_train_data),
            include_track_stages=bool(include_track_stages),
            include_preview_cache=bool(include_preview_cache),
            include_experiments=bool(include_experiments),
            include_splits=bool(include_splits),
            include_exp_checkpoints=bool(include_exp_checkpoints))
        if bool(dry_run):
            return Ok(await asyncio.to_thread(
                cache_cleanup.cleanup, self._cache_root(), self._data_dir,
                self._plugin_dir, dry_run=True, **kwargs))
        # 真正删除可能涉及几十 GB、远超宿主 30 秒上限：后台跑，立即返回 job id。
        job_id = self._job_begin("cache")

        def runner():
            try:
                result = cache_cleanup.cleanup(
                    self._cache_root(), self._data_dir, self._plugin_dir,
                    dry_run=False, **kwargs)
                freed = float(result.get("removed_bytes") or 0) / (1024 ** 3)
                self._job_update(
                    job_id, state="done" if result.get("ok") else "failed", progress=100,
                    detail=f"释放 {freed:.1f} GB" if result.get("ok")
                    else "部分文件删除失败：" + str((result.get("errors") or [{}])[0].get("error")),
                    removed_bytes=result.get("removed_bytes"))
            except Exception as exc:  # noqa: BLE001
                self._job_update(job_id, state="failed", progress=100,
                                 detail=f"{type(exc).__name__}: {exc}")

        self._thread("cache-cleanup", runner)
        return Ok({"ok": True, "started": True, "job_id": job_id,
                   "note": "后台清理中，完成后可在「查看可清理空间」里确认"})

    # ------------------------------------------------------------------
    # 独立听歌录制：应用声道隔离 → 整首切分 → 三首缓冲 → 锚点
    # ------------------------------------------------------------------
    def _auto_recorded_tracks(self) -> list[dict]:
        tracks = [t for t in self._pipeline.store.all() if t.get("auto_recorded")]
        tracks.sort(key=lambda t: str(t.get("created_at") or ""))
        return tracks

    def _enforce_song_slots(self, keep_id: str = "") -> list[str]:
        """只保留最近 3 首自动录下的歌；更旧的记录和录音文件一起删掉。"""
        tracks = self._auto_recorded_tracks()
        removed: list[str] = []
        overflow = tracks[:-self._song_listener.SLOT_LIMIT] \
            if len(tracks) > self._song_listener.SLOT_LIMIT else []
        for track in overflow:
            tid = str(track.get("id") or "")
            if not tid or tid == keep_id:
                continue
            src = str(track.get("source") or "")
            try:
                if src and Path(src).is_file():
                    Path(src).unlink()
            except OSError:
                pass
            if self._pipeline.store.remove(tid):
                removed.append(tid)
        return removed

    def _on_song_recorded(self, event: dict) -> None:
        """listener 线程回调：把录完的整首歌变成 status=recorded 的曲目。"""
        try:
            if not isinstance(event, dict) or not event.get("ok"):
                if isinstance(event, dict) and event.get("error"):
                    self.logger.warning("听歌录制失败：%s", event.get("error"))
                return
            path = str(event.get("path") or "")
            if not path or not Path(path).is_file():
                return
            title = str(event.get("title") or "").strip() or ("录下的歌 " + time.strftime("%H:%M"))
            artist = str(event.get("artist") or "").strip()
            track = self._pipeline.store.add(
                title=title[:120], artist=artist[:120], source=path,
                duration_s=event.get("duration_s"))
            self._pipeline.store.update(
                track["id"], status="recorded", step="recorded", progress=0,
                auto_recorded=True, anchor=True,
                # 播放器报的整首时长/播放起止进度：处理时用它判断这首歌
                # 是不是从头完整放完的（纯音乐另看人声轨）。
                expected_s=event.get("expected_s") or 0.0,
                start_position_s=event.get("start_position_s"),
                end_position_s=event.get("end_position_s"),
                recorded_at=time.strftime("%Y-%m-%d %H:%M:%S"), error="")
            self._song_listener._current_track = track["id"]
            self._enforce_song_slots(keep_id=track["id"])
            self.logger.info("录完一首：%s（%.1fs）", title,
                             float(event.get("duration_s") or 0))
            pending = dict(self._pending_sing or {})
            if pending:
                kind = str(pending.get("kind") or "")
                target = None
                if kind == "current":
                    target = track
                elif kind == "previous":
                    rows = self._auto_recorded_tracks()
                    target = rows[-2] if len(rows) >= 2 else None
                if target:
                    self._pending_sing = {}
                    self._background_process(str(target["id"]), kind="anchor")
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("整首歌入库失败：%s", exc)

    async def _perception_status(self, timeout: float = 6.0,
                                 *, max_age: float = 10.0) -> dict:
        """「为她所听」是否在跑；默认缓存 10 秒，避免频繁跨插件调用。

        录音线程需要跟她的当前曲目切歌，所以允许传入更短的 max_age（2 秒）。
        """
        now = time.time()
        cached = getattr(self, "_perception_cache", None)
        if cached and now - cached[0] < max(0.5, float(max_age)):
            return cached[1]
        info: dict = {"running": False, "monitor": False, "listening": False}
        try:
            st = await self._call_entry("music_perception:get_status", {}, timeout=timeout)
            if isinstance(st, dict) and st.get("ok"):
                live = st.get("live") or {}
                info = {
                    "running": True,
                    "monitor": bool(st.get("monitor_enabled")),
                    "listening": str(live.get("status") or "") not in ("", "not_authorized", "stopped"),
                    "song_id": str(st.get("current_song_id") or ""),
                    "song_title": str(st.get("current_song_title") or ""),
                    "song_artist": str(st.get("current_song_artist") or ""),
                    # 播放器报的整首时长/当前进度：判断这段录音是否完整要用。
                    "song_duration_s": st.get("estimated_media_duration_s"),
                    "song_position_s": st.get("estimated_player_position_s"),
                    "play_history": st.get("play_history") or [],
                    "next_song": st.get("next_song"),
                }
        except Exception:  # noqa: BLE001 - 没装/没开都按「不在跑」处理
            info = {"running": False, "monitor": False, "listening": False}
        self._perception_cache = (now, info)
        return info

    def _watch_link_mode(self) -> None:
        """Keep the recorder in the right mode: linked with 为她所听, or standalone."""
        while not getattr(self, "_shutdown", False):
            try:
                self._sync_link_listener()
            except Exception as exc:  # noqa: BLE001 - 监看线程不能静默死掉
                self.logger.warning("联动监听检查失败：%s", exc)
            time.sleep(8.0)

    def _sync_link_listener(self) -> None:
        if (self._data_dir / "listener" / "manual_off.flag").is_file():
            return
        perception = asyncio.run(self._perception_status(timeout=3.0))
        active = bool(perception.get("running")
                      and (perception.get("listening") or perception.get("monitor")))
        listener = self._song_listener.status()
        running = bool(listener.get("running"))
        mode = str(listener.get("mode") or "standalone")
        if active:
            target = asyncio.run(self._capture_target_async(0))
            if target.get("source") == "music_perception":
                current = listener.get("target") or {}
                same = (running and mode == "linked"
                        and str(current.get("pid")) == str(target.get("pid"))
                        and str(current.get("created")) == str(target.get("created")))
                if same:
                    return
                if running:
                    self._song_listener.stop(manual=False)
                result = self._song_listener.start(target=target)
                if result.get("ok"):
                    self.logger.info("已联动为她所听，自动录制 %s",
                                     target.get("name") or "所选应用")
                return
            # 她在监听但还没选定应用：不要自己另选一个去抢采集。
            if running:
                self._song_listener.stop(manual=False)
                self.logger.info("「为她所听」未选定音源，联动录制暂停")
            return
        # 独立模式：她没在监听，自动录自己的音源。
        if running and mode == "linked":
            self._song_listener.stop(manual=False)
            running = False
        if not running:
            result = self._song_listener.start(autostart=True)
            if not result.get("ok") and not result.get("already"):
                self.logger.info("听歌录制未启动：%s", result.get("error"))

    async def _perception_priority(self, enabled: bool, collect_lyrics: bool = False) -> dict:
        """Ask 为她所听 to free stem/lyrics VRAM while singing.

        Its capture keeps running (the song still records); only the GPU-heavy
        stem separation and lyrics model are paused. Restored after the job.
        """
        try:
            st = await self._perception_status(timeout=3.0)
            if not st.get("running"):
                return {}
            result = await self._call_entry(
                "music_perception:set_sing_priority",
                {"enabled": bool(enabled), "collect_lyrics": bool(collect_lyrics)},
                timeout=30.0 if enabled else 20.0)
            data = result if isinstance(result, dict) else {}
            if enabled and data.get("ok"):
                try:
                    (self._data_dir / "perception_priority.flag").write_text(
                        "1", encoding="utf-8")
                except OSError:
                    pass
            elif not enabled:
                try:
                    (self._data_dir / "perception_priority.flag").unlink(missing_ok=True)
                except OSError:
                    pass
            return data
        except Exception as exc:  # noqa: BLE001 - 让位失败不能阻断唱歌
            self.logger.warning("「为她所听」显存%s失败：%s",
                                "让位" if enabled else "恢复", exc)
            return {}

    async def _perception_lyrics(self) -> dict:
        """Read the current lyrics before the GPU-heavy work is paused."""
        try:
            st = await self._perception_status(timeout=3.0)
            if not st.get("running"):
                return {}
            result = await self._call_entry(
                "music_perception:get_current_lyrics_for_ai",
                {"scope": "current", "max_lines": 40}, timeout=15.0)
            return result if isinstance(result, dict) else {}
        except Exception as exc:  # noqa: BLE001 - 歌词只是锦上添花
            self.logger.warning("读取「为她所听」歌词失败：%s", exc)
            return {}

    def _apply_perception_priority_sync(self, enabled: bool,
                                        collect_lyrics: bool = False) -> dict:
        """Run the async yield/restore call from the worker thread."""
        try:
            return asyncio.run(
                self._perception_priority(enabled, collect_lyrics)) or {}
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("「为她所听」显存%s失败：%s",
                                "让位" if enabled else "恢复", exc)
            return {}

    def _restore_perception_after_sing(self) -> None:
        self._apply_perception_priority_sync(False)

    async def _link_status(self) -> dict:
        """「为她所听」联动状态，给设置页卡片和 AI 用。缓存 10 秒。"""
        now = time.time()
        cached = getattr(self, "_link_cache", None)
        if cached and now - cached[0] < 10.0:
            return dict(cached[1])
        perception = await self._perception_status(timeout=3.0)
        listener = self._song_listener.status()
        info = {
            "running": bool(perception.get("running")),
            "monitor": bool(perception.get("monitor")),
            "ai_delivery": False,
            "source_control": False,
            "selected": {},
            "capture_mode": "",
            "listener_mode": str(listener.get("mode") or "standalone"),
            "listener_running": bool(listener.get("running")),
            "listener_state": str((listener.get("live") or {}).get("state") or ""),
            "reason": "为她所听未运行" if not perception.get("running") else "",
        }
        if info["running"]:
            try:
                ctl = await self._call_entry(
                    "music_perception:get_audio_source_control", {}, timeout=4.0)
            except Exception:  # noqa: BLE001 - 跨插件不可用按未连接处理
                ctl = None
            if isinstance(ctl, dict) and ctl.get("ok"):
                info["source_control"] = True
                info["capture_mode"] = str(ctl.get("capture_mode") or "")
                info["ai_delivery"] = bool(ctl.get("ai_delivery_enabled"))
                selected = ctl.get("selected") or {}
                if isinstance(selected, dict) and selected:
                    info["selected"] = {
                        key: selected.get(key)
                        for key in ("pid", "created", "name", "tag", "label")
                    }
                info["reason"] = ""
            else:
                info["reason"] = "音源中控不可用"
        self._link_cache = (now, info)
        return dict(info)

    async def _capture_target_async(self, pid: int = 0) -> dict:
        """Resolve which application channel to capture (linked first)."""
        if pid:
            rows = await asyncio.to_thread(self._song_listener._scan_targets)
            for row in rows:
                if int(row.get("pid") or 0) == int(pid):
                    return {**row, "source": "manual"}
            return {}
        try:
            status = await self._perception_status()
            if status.get("running"):
                info = await self._call_entry(
                    "music_perception:get_audio_source_control", {}, timeout=8.0)
                selected = (info or {}).get("selected") or {}
                try:
                    spid = int(selected.get("pid") or 0)
                except (TypeError, ValueError):
                    spid = 0
                if spid > 0 and selected.get("created"):
                    name = str(selected.get("name") or "")
                    # 绝不录 AI 自己的发声进程
                    if selected.get("host_ai") or name.casefold() in {
                            "n.e.k.o.exe", "projectneko_server.exe", "neko.exe"}:
                        return {}
                    tag = str(selected.get("tag") or "")
                    path = str(selected.get("path") or "")
                    label = str(selected.get("label") or "")
                    if not tag:
                        # 联动目标通常只带 pid/name。用本插件自己的应用目录补齐
                        # tag，否则 worker 的 assume_music=false；SMTC 不可用时
                        # 它会一直停在 ready，永远不开始录。
                        try:
                            rows = await asyncio.to_thread(
                                self._song_listener._scan_targets)
                            match = next((r for r in rows
                                          if int(r.get("pid") or 0) == spid), None)
                        except Exception:  # noqa: BLE001
                            match = None
                        if match:
                            tag = str(match.get("tag") or "")
                            path = path or str(match.get("path") or "")
                            label = label or str(match.get("label") or "")
                        else:
                            scene = (info or {}).get("scene") or {}
                            if str(scene.get("kind") or "") == "music":
                                tag = "music"
                    return {"pid": spid, "created": str(selected.get("created")),
                            "name": name, "tag": tag, "path": path, "label": label,
                            "source": "music_perception"}
        except Exception:  # noqa: BLE001 - not running / no such entry
            pass
        rows = await asyncio.to_thread(self._song_listener._scan_targets)
        rows = [r for r in rows if r.get("tag") == "music"]
        return {**rows[0], "source": "self"} if rows else {}

    @plugin_entry(id="ms_capture_status", name="为她而唱 · 采集状态",
                  description="只读：独立采集组件是否就绪、当前是联动还是独立模式、正在录什么",
                  input_schema={"type": "object", "properties": {}})
    async def ms_capture_status(self, **_):
        return Ok({"ok": True, "listener": self._song_listener.status(),
                   "runtime": runtime.locate_app_capture_runtime(self._data_dir, str(self._pipeline.paths.get("synth_python") or "")),
                   "perception": await self._perception_status()})

    @plugin_entry(id="ms_capture_targets", name="为她而唱 · 可录的应用",
                  description="只读：列出可以采集声道的音乐/视频应用（视频会被规则排除）",
                  input_schema={"type": "object", "properties": {}})
    async def ms_capture_targets(self, **_):
        rows = await asyncio.to_thread(self._song_listener._scan_targets)
        selected = await self._capture_target_async(0)
        return Ok({"ok": True,
                   "mode": "linked" if selected.get("source") == "music_perception" else "standalone",
                   "selected": selected,
                   "targets": [{"pid": r.get("pid"), "created": r.get("created"),
                                "name": r.get("name"), "tag": r.get("tag"),
                                "label": r.get("label")} for r in rows[:60]]})

    @plugin_entry(id="ms_capture_install", name="为她而唱 · 安装独立采集组件",
                  description="把逐应用声道隔离运行环境复制到本插件（优先硬链接，不占额外空间）",
                  input_schema={"type": "object", "properties": {}})
    async def ms_capture_install(self, **_):
        job_id = self._job_begin("capture")

        def run():
            try:
                result = runtime.copy_app_capture_runtime(self._data_dir)
                self._job_update(
                    job_id, state="done" if result.get("ok") else "failed", progress=100,
                    detail=(result.get("error") or result.get("note")
                            or f"{result.get('files', 0)} 个文件"))
            except Exception as exc:  # noqa: BLE001
                self._job_update(job_id, state="failed", progress=100,
                                 detail=f"{type(exc).__name__}: {exc}")

        self._thread("ms-capture-install", run)
        return Ok({"ok": True, "started": True, "job_id": job_id})

    @plugin_entry(id="ms_song_listen_start", name="为她而唱 · 开始听歌录制",
                  description="开始独立听歌录制：只采集选中的应用声道，整首录完入库，先不翻唱",
                  input_schema={"type": "object", "properties": {
                      "pid": {"type": "integer", "minimum": 0},
                      "exe_name": {"type": "string"}}})
    async def ms_song_listen_start(self, pid: int = 0, exe_name: str = "", **_):
        target = await self._capture_target_async(int(pid or 0))
        if not target:
            return Ok({"ok": False,
                       "error": "没有可采集的音乐应用（先打开网易云 / QQ音乐 / Spotify，或手动选一个）"})
        perception = await self._perception_status(timeout=3.0)
        if (perception.get("running")
                and (perception.get("listening") or perception.get("monitor"))
                and target.get("source") != "music_perception"):
            return Ok({"ok": False,
                       "error": "「为她所听」正在监听；请先在她那里选定要录的应用，联动会自动录制。"})
        result = await asyncio.to_thread(
            self._song_listener.start, int(pid or 0), exe_name, target)
        return Ok(result)

    @plugin_entry(id="ms_song_listen_stop", name="为她而唱 · 停止听歌录制",
                  description="停止听歌录制；已经录完的歌保留，正在录的会先收尾",
                  input_schema={"type": "object", "properties": {}})
    async def ms_song_listen_stop(self, **_):
        return Ok(await asyncio.to_thread(self._song_listener.stop))

    @llm_tool(
        name="music_synth_sing_anchor",
        description=("用户说「唱一下这首歌」「唱上一首」时用它。"
                     "anchor=current 表示当前这首（还在录就排队，录完自动开唱）；"
                     "anchor=previous 表示上一首。开始处理后用 music_synth_sing 播放成品。"
                     "唱歌处理期间会让她所听暂停分离/歌词识别以腾显存，录音继续；"
                     "如果返回 lyrics_from_perception，可以自然地说给用户听。"),
        parameters={"type": "object", "properties": {
            "anchor": {"type": "string", "enum": ["current", "previous"]},
            "track_id": {"type": "string", "description": "指定曲目 id，可不填"}},
        }, timeout=30.0,
    )
    @plugin_entry(id="ms_sing_anchor", name="为她而唱 · 唱锚定的歌",
                  description="AI 调用：唱当前/上一首录下的歌；还在录时排队，录完自动开唱",
                  input_schema={"type": "object", "properties": {
                      "anchor": {"type": "string",
                                 "enum": ["current", "previous", "now", "last"]},
                      "track_id": {"type": "string"},
                      "when_ready": {"type": "boolean"},
                      "force": {"type": "boolean",
                                "description": "纯音乐/残缺录音也想硬唱时才用"}}})
    async def ms_sing_anchor(self, anchor: str = "current", track_id: str = "",
                             when_ready: bool = True, force: bool = False, **_):
        kind = ""
        target = None
        if track_id:
            target = self._pipeline.store.get(track_id)
        else:
            key = (anchor or "current").strip().lower()
            rows = self._auto_recorded_tracks()
            live = self._song_listener.status().get("live") or {}
            recording = str(live.get("state") or "") == "recording"
            if key in ("current", "now", "this", "这首", "现在"):
                kind = "current"
                if not (recording and when_ready):
                    target = rows[-1] if rows else None
            elif key in ("previous", "prev", "last", "上一首"):
                kind = "previous"
                target = rows[-2] if len(rows) >= 2 else None
            else:
                target = self._pipeline.store.get(key)
        if target is None:
            if when_ready and self._song_listener.status().get("running"):
                self._pending_sing = {"kind": kind or "current"}
                return Ok({"ok": True, "queued": True, "pending": dict(self._pending_sing),
                           "note": "正在录这首歌，录完就自动开唱"})
            return Ok({"ok": False, "error": "没有可唱的录音（先开听歌录制）"})
        status = str(target.get("status") or "")
        if status in ("running", "queued"):
            return Ok({"ok": True, "already": True, "track": target,
                       "note": "这首歌正在处理"})
        if status in ("instrumental", "incomplete", "silent"):
            reason = str(target.get("check_reason") or "")
            if not force:
                return Ok({"ok": False, "status": status, "track": target,
                           "error": reason or "这首歌不能翻唱",
                           "note": ("纯音乐没有人声、或者这段录音没放完整。"
                                    "确实想硬唱可以带 force=true，但不建议")})
            # 用户明确要求照唱：记住这个决定，后面的流水线不再拦。
            self._pipeline.store.update(str(target["id"]), check_override=True)
            self.logger.warning("用户要求硬唱 %s（%s）：%s",
                                target.get("title") or target["id"], status, reason)
            target = self._pipeline.store.get(str(target["id"])) or target
        if status == "done":
            return Ok({"ok": True, "already_done": True, "track": target,
                       "note": "这首已经唱好了，可在列表里试听或重新唱"})
        lp = await self._perception_lyrics()
        lyrics = lp.get("songs") or []
        official = lp.get("official_lyrics") or {}
        if lyrics or official:
            self._pipeline.store.update(str(target["id"]),
                                        lyrics_from_perception=lyrics,
                                        official_lyrics=official)
        job_id = self._background_process(str(target["id"]), kind="anchor",
                                          perception_priority=True)
        return Ok({"ok": True, "started": True, "job_id": job_id, "track": target,
                   "lyrics_from_perception": lyrics,
                   "official_lyrics": official,
                   "note": ("唱歌处理时会让她所听暂停分离和歌词识别，录音继续"
                            if lp else "")})

    @plugin_entry(id="ms_import", name="为她而唱 · 导入音频",
                  description="把本地音频文件直接加入待处理队列（跳过采集，便于验证链路）",
                  input_schema={"type": "object", "properties": {
                      "path": {"type": "string"}, "title": {"type": "string"},
                      "split": {"type": "boolean"}}})
    async def ms_import(self, path: str = "", title: str = "", split: bool = False, **_):
        src = Path(path or "").expanduser()
        if not src.is_file():
            return Ok({"ok": False, "error": f"文件不存在：{src}"})
        if split:
            result = self._pipeline.split_recording(src, title_marks=[])
            if not result.get("ok"):
                return Ok({"ok": False, "error": result.get("error"), "detail": result})
            return Ok({"ok": True, "tracks": result["tracks"], "split": True})
        track = self._pipeline.store.add(title=title or src.stem, source=str(src),
                                         duration_s=None)
        return Ok({"ok": True, "track": track, "split": False})

    @plugin_entry(id="ms_split", name="为她而唱 · 切歌",
                  description="按静音能量谷与标题变化把一段录音切成多首",
                  input_schema={"type": "object", "properties": {
                      "path": {"type": "string"},
                      "min_song_s": {"type": "number", "minimum": 8, "maximum": 300},
                      "silence_s": {"type": "number", "minimum": 0.3, "maximum": 10}}})
    async def ms_split(self, path: str = "", min_song_s: float = 25.0,
                       silence_s: float = 2.0, **_):
        src = Path(path or "").expanduser()
        if not src.is_file():
            return Ok({"ok": False, "error": f"文件不存在：{src}"})
        result = await asyncio.to_thread(
            self._pipeline.split_recording, src,
            min_song_s=_as_float(min_song_s, 25.0, 8.0, 300.0),
            silence_s=_as_float(silence_s, 1.2, 0.3, 10.0))
        return Ok(result)

    def _background_process(self, track_id: str, kind: str = "track",
                            force: bool = False,
                            perception_priority: bool = False) -> str:
        """Run the full chain for one track on its own thread; returns job_id.

        ``force=False`` resumes from whatever steps already succeeded (used by
        「继续唱」 after a plugin reload); ``force=True`` redoes everything from
        separation (used by 「重新唱一遍」).
        """
        job_id = self._job_begin(kind)
        probe = runtime.probe_hardware(force=True)
        paths = self._pipeline.paths
        plan = runtime.plan_for(probe, paths.get("tier_override", ""),
                                paths.get("precision", "auto"),
                                paths.get("max_parallel", 0))
        self._record_probe(probe, plan)

        def run():
            # 排队而不是并发：第二首选进来时先等第一首整条链路结束。
            self._job_update(job_id, state="queued",
                             detail="排队中：同一时间只处理一首，避免显存被占满")
            # 训练正在跑时先等它结束：两边同时用 GPU 会直接 OOM/崩溃。
            self._wait_training_idle(job_id)
            if getattr(self, "_shutdown", False):
                self._job_update(job_id, state="canceled", detail="插件已停止")
                return
            with self._process_lock:
                self._job_update(job_id, state="running", detail="")
                if perception_priority:
                    self._apply_perception_priority_sync(True)
                plan_used = plan
                if perception_priority:
                    # 让位后重新看一次显存：按腾出来之后的空闲量选档位，
                    # 不要因为她让位前占着卡而给唱歌降档。
                    try:
                        probe_after = runtime.probe_hardware(force=True)
                        plan_after = runtime.plan_for(
                            probe_after,
                            paths.get("tier_override", ""),
                            paths.get("precision", "auto"),
                            paths.get("max_parallel", 0))
                        self._record_probe(probe_after, plan_after)
                    except Exception as exc:  # noqa: BLE001 - 沿用原计划
                        self.logger.warning("让位后重新探测显存失败，沿用原计划：%s", exc)
                    else:
                        plan_used = plan_after
                try:
                    result = self._pipeline.process_track(
                        track_id, plan_used, resume=not force)
                    self._job_update(job_id, state="done" if result.get("ok") else "failed",
                                     progress=100, detail=result.get("error") or "完成")
                except Exception as exc:  # noqa: BLE001
                    self._job_update(job_id, state="failed", detail=f"{type(exc).__name__}: {exc}")
                finally:
                    # AI 说过「唱这首」的，处理完自己放出来，不用再等一次调用。
                    if track_id in self._pending_play:
                        self._pending_play.discard(track_id)
                        try:
                            fresh = self._pipeline.store.get(track_id) or {}
                            if self._finished(fresh):
                                info = self._sing_track(fresh)
                                self.logger.info("[playback] 处理完自动播放 track=%s ok=%s",
                                                 track_id, info.get("ok"))
                            else:
                                self.logger.info("[playback] 处理完仍无成品，不播 track=%s",
                                                 track_id)
                        except Exception as exc:  # noqa: BLE001
                            self.logger.warning("[playback] 自动播放失败：%s", exc)
                    if perception_priority:
                        self._restore_perception_after_sing()

        self._thread(f"ms-{kind}-{track_id}", run)
        return job_id

    def _auto_resume_tracks(self, track_ids: list) -> None:
        """Resume interrupted ACE「生成并让她唱」tracks after a plugin reload."""
        # 等启动收尾（宿主配置、路径重新解析都完成）再动 GPU，避免和启动抢资源。
        time.sleep(3.0)
        for track_id in track_ids:
            if getattr(self, "_shutdown", False):
                return
            track = self._pipeline.store.get(track_id)
            if not track or str(track.get("status") or "") == "done":
                continue
            try:
                self.logger.info("自动续跑被中断的原创歌曲：%s", track.get("title") or track_id)
                self._background_process(track_id, kind="resume")
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("自动续跑失败（%s）：%s", track_id, exc)

    @plugin_entry(id="ms_process_track", name="为她而唱 · 处理单曲",
                  description="对指定曲目跑完整链路：分离 → 变声 → 混音；后台执行（可从中断处继续）",
                  input_schema={"type": "object", "properties": {
                      "track_id": {"type": "string"},
                      "force": {"type": "boolean",
                                "description": "true=从头重唱；false=复用已完成的步骤"},
                      "post_engine": {"type": "string", "enum": ["", "ddsp", "soulx", "soulx_ddsp"],
                                      "description": "本次变声引擎；留空沿用曲目记录"}}})
    async def ms_process_track(self, track_id: str = "", force: bool = False,
                               post_engine: str = "", **_):
        track = self._pipeline.store.get(track_id)
        if not track:
            return Ok({"ok": False, "error": "曲目不存在"})
        engine = str(post_engine or "").strip().lower()
        if engine in ("ddsp", "soulx", "soulx_ddsp"):
            self._pipeline.store.update(track_id, post_engine=engine)
            track = self._pipeline.store.get(track_id) or track
        lp = await self._perception_lyrics()
        lyrics = lp.get("songs") or []
        official = lp.get("official_lyrics") or {}
        if lyrics or official:
            self._pipeline.store.update(track_id, lyrics_from_perception=lyrics,
                                        official_lyrics=official)
        job_id = self._background_process(track_id, force=_as_bool(force),
                                          perception_priority=True)
        return Ok({"ok": True, "started": True, "job_id": job_id, "track_id": track_id,
                   "resumed": not _as_bool(force), "tier": self._pipeline.last_plan,
                   "lyrics_from_perception": lyrics,
                   "official_lyrics": official})

    def _now_playing(self) -> dict:
        """Standalone now-playing: Windows media session, no 为她所听 needed."""
        smtc = self._read_smtc()
        sessions = [s for s in (smtc.get("sessions") or []) if isinstance(s, dict)]
        playing = [s for s in sessions if s.get("playing") is True]
        music = [s for s in playing if str(s.get("playback_type") or "").lower() == "music"]
        video = [s for s in playing if str(s.get("playback_type") or "").lower() == "video"]
        chosen = (music or playing or ([smtc] if isinstance(smtc, dict) else []))
        row = chosen[0] if chosen else {}
        title = str(row.get("title") or "").strip()
        name, split_artist = self._split_smtc_title(title)
        artist = str(row.get("artist") or "").strip()[:120] or split_artist
        return {
            "ok": True,
            "available": bool(smtc.get("available")),
            "playing": bool(playing),
            "is_music": bool(music),
            "is_video": bool(video),
            "media_type": str(row.get("playback_type") or "unknown"),
            "title": name,
            "artist": artist,
            "app": str(row.get("app") or ""),
            "position_s": row.get("position_s"),
            "duration_s": row.get("duration_s"),
            "evidence": "smtc_playback_type",
            "sessions": [{"title": str(s.get("title") or "")[:120],
                          "artist": str(s.get("artist") or "")[:120],
                          "playing": bool(s.get("playing")),
                          "type": str(s.get("playback_type") or ""),
                          "app": str(s.get("app") or "")} for s in sessions[:8]],
            "note": "只读 Windows 媒体会话；不依赖为她所听，也不证明喇叭一定在响。",
        }

    @llm_tool(
        name="music_synth_now_playing",
        description=("读取电脑现在正在播放的媒体：歌名、作者、是否音乐/是否视频。"
                     "不依赖为她所听插件，可直接调用。用户问「现在在放什么歌」时用它。"),
        parameters={"type": "object", "properties": {}}, timeout=20.0,
    )
    @plugin_entry(id="ms_now_playing", name="为她而唱 · 现在在放什么",
                  description="只读：不依赖为她所听，读取 Windows 媒体会话，返回歌名/作者/是否音乐/是否视频",
                  input_schema={"type": "object", "properties": {}})
    async def ms_now_playing(self, **_):
        info = await asyncio.to_thread(self._now_playing)
        try:
            linked = await self._perception_status()
        except Exception:  # noqa: BLE001
            linked = {"running": False}
        info["linked_available"] = bool(linked.get("running"))
        info["capture_target"] = await self._capture_target_async(0)
        return Ok(info)

    def _read_smtc(self) -> dict:
        """Read Windows SMTC metadata in the isolated synth runtime.

        The frozen host cannot import winrt itself, so the plugin's synth venv
        runs media_session_worker.py with the built-in winrt vendor. No other
        plugin has to be installed.
        """
        try:
            located = runtime.locate_smtc_runtime(self._data_dir, str(self._pipeline.paths.get("synth_python") or ""))
        except Exception as exc:  # noqa: BLE001 - metadata is best effort
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        if not located.get("ok"):
            return {"available": False, "reason": str(located.get("error") or "no_smtc_runtime")}
        python, vendor, worker = located["python"], located["vendor"], located["worker_script"]
        env = {key: value for key, value in os.environ.items()
               if key not in ("PYTHONPATH", "PYTHONHOME", "PYTHONEXECUTABLE",
                              "PYTHONSTARTUP", "VIRTUAL_ENV", "CONDA_PREFIX")}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            response = subprocess.run(
                [python, "-I", "-S", worker, vendor], capture_output=True, timeout=4.0,
                env=env, creationflags=flags, stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
        if response.returncode or len(response.stdout) > 16000:
            return {"available": False, "reason": "metadata_process_failure"}
        try:
            info = json.loads(response.stdout.decode("utf-8"))
        except (UnicodeError, ValueError):
            return {"available": False, "reason": "metadata_unavailable"}
        return info if isinstance(info, dict) else {"available": False, "reason": "metadata_unavailable"}

    @staticmethod
    def _split_smtc_title(text: str) -> tuple[str, str]:
        """'Artist - Title' → (title, artist); a plain title stays a title."""
        raw = str(text or "").strip()
        for sep in (" - ", " – ", " — "):
            if sep in raw:
                artist, _, name = raw.partition(sep)
                if artist.strip() and name.strip():
                    return name.strip()[:120], artist.strip()[:120]
        return raw[:120], ""

    async def _identify_current(self) -> dict:
        """Best-effort identification: SMTC title first, then music_perception."""
        info: dict = {}
        meta = await asyncio.to_thread(self._read_smtc)
        info["smtc"] = meta
        title = str(meta.get("title") or "").strip()
        playback_type = str(meta.get("playback_type") or "").lower()
        if meta.get("available") and meta.get("playing") and title:
            name, artist = self._split_smtc_title(title)
            artist = artist or str(meta.get("artist") or "").strip()[:120]
            info["title"] = name
            if artist:
                info["artist"] = artist
            # Strong: a music (not video) session.  Weak: anything else playing.
            info["title_source"] = "smtc" if playback_type == "music" else "smtc_weak"
        try:
            media = await self._call_entry("music_perception:get_current_media_for_ai", {}, timeout=10.0)
            if isinstance(media, dict):
                info["media"] = media
                title = media.get("title") or (media.get("track") or {}).get("title")
                artist = media.get("artist") or (media.get("track") or {}).get("artist")
                # 「为她所听」在跑时以它的歌名为准（它有自己的识别与歌词校准）
                if title:
                    info["title"] = str(title)[:120]
                    info["title_source"] = "music_perception"
                if artist:
                    info["artist"] = str(artist)[:120]
        except Exception:  # noqa: BLE001
            pass
        try:
            lyrics = await self._call_entry(
                "music_perception:get_current_lyrics_for_ai",
                {"scope": "current", "max_lines": 20}, timeout=15.0)
            if isinstance(lyrics, dict):
                official = lyrics.get("official_lyrics") or {}
                if official.get("ok") and official.get("title"):
                    info["title"] = info.get("title") or official.get("title")
                    info["artist"] = info.get("artist") or official.get("artist")
                    info["official_lyrics"] = [line.get("text") for line in official.get("lines", [])
                                               if isinstance(line, dict)].copy()
                songs = lyrics.get("songs") or []
                if songs and isinstance(songs[0], dict):
                    lines = [row.get("text") for row in songs[0].get("lines", [])
                             if isinstance(row, dict) and row.get("text")]
                    info["lyrics"] = lines[:20]
                    if not info.get("title") and songs[0].get("title"):
                        info["title"] = songs[0]["title"]
                        info["title_source"] = "music_perception_lyrics"
        except Exception:  # noqa: BLE001
            pass
        return info

    @plugin_entry(id="ms_identify", name="为她而唱 · 识歌校准",
                  description="读取当前媒体标题（SMTC 优先）与官方/识别歌词，校准指定曲目的歌名与歌词",
                  input_schema={"type": "object", "properties": {"track_id": {"type": "string"}}})
    async def ms_identify(self, track_id: str = "", **_):
        info = await self._identify_current()
        if track_id:
            track = self._pipeline.store.get(track_id)
            if not track:
                return Ok({"ok": False, "error": "曲目不存在", "identity": info})
            fields = {}
            writable = _title_writable(track, info.get("title_source") or "")
            if info.get("title") and writable:
                fields["title"] = info["title"]
            if info.get("artist") and (writable or not str(track.get("artist") or "").strip()):
                fields["artist"] = info["artist"]
            if info.get("lyrics"):
                fields["lyrics_preview"] = info["lyrics"]
            if fields:
                track = self._pipeline.store.update(track_id, **fields)
            return Ok({"ok": True, "identity": info, "track": track,
                       "title_kept": bool(info.get("title")) and not writable})
        return Ok({"ok": True, "identity": info})

    # ------------------------------------------------------------------
    # playback / AI singing
    # ------------------------------------------------------------------
    def _stop_playback(self) -> None:
        proc = getattr(self, "_playback_proc", None)
        self._playback_proc = None
        self._last_play_target = ""
        self._last_play_at = 0.0
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, 0)
            except Exception:  # noqa: BLE001 - stopping is best effort
                pass
        # 停播就把口型轨一起撤掉，角色嘴立刻收回去。
        try:
            lips_server.clear()
        except Exception:  # noqa: BLE001
            pass

    def _play_audio_file(self, target: str) -> dict:
        """Play a finished track locally and report how it was started.

        ``os.startfile`` alone is unreliable: this machine maps .wav to a Store
        "Media Player" package that exits without producing sound when opened
        from a plugin process (verified 2026-10-31). ffplay ships with the
        compute tooling and gives us a real process; winsound needs nothing and
        handles every WAV this module writes.
        """
        self._stop_playback()
        return self._spawn_player(target)

    def _start_singing(self, target: str) -> dict:
        """起播一首成品：先交给主窗口的 <audio>。

        主窗口那头的桥如果够新，会自己拉 ``/song.wav`` 播，并用
        ``audio.currentTime`` 驱动口型——声音和嘴在同一个时钟里，中间只剩
        「浏览器音频缓冲 + 一帧渲染」，没有跨进程解析、没有 HTTP 轮询。
        桥不接管（页面没刷新 / 自动播放被拦）时由 ``_arm_playback_fallback``
        在 1.8 秒后退回 ffplay，声音不会没有。
        """
        self._stop_playback()
        return {"mode": "browser", "clock": "audio"}

    def _arm_playback_fallback(self, target: str, seq: int) -> None:
        """1.8 秒内网页没确认播放就退回 ffplay（后台线程，不挡主流程）。"""
        def wait() -> None:
            deadline = time.monotonic() + 1.8
            while time.monotonic() < deadline:
                state = lips_server.audio_state(seq)
                if state == "playing":
                    self.logger.info("[playback] 主窗口 <audio> 接管，链路=audio")
                    return
                if state == "failed":
                    break
                time.sleep(0.1)
            if self._last_play_target != target:
                return
            if not lips_server.status().get("active"):
                # 没有口型轨（连包络都没有）也别让人干等：直接出声。
                pass
            else:
                self.logger.warning("[playback] 主窗口没有接管音频，退回 ffplay")
            # 撤掉音频地址：避免网页那头稍后又把声音拉起来，两路一起响。
            lips_server.drop_audio()
            try:
                self._spawn_player(target)
            except OSError as exc:
                self.logger.warning("[playback] 退回 ffplay 失败：%s", exc)

        threading.Thread(target=wait, daemon=True,
                         name="music_synth-play-fallback").start()

    def _spawn_player(self, target: str) -> dict:
        """外部进程播放：ffplay（带真实时钟）优先，winsound 兜底。"""
        ffplay = shutil.which("ffplay") or shutil.which("ffplay.exe")
        if ffplay:
            try:
                gen = lips_server.begin_audio_clock()
                proc = subprocess.Popen(
                    # -stats 让 ffplay 每 ~30ms 打印一行播放位置；口型直接用这个
                    # 真实时钟，不再用墙钟 + 固定延迟去猜（那是「慢半拍」的来源）。
                    [ffplay, "-nodisp", "-autoexit", "-loglevel", "info",
                     "-stats", str(target)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                threading.Thread(target=_pump_ffplay_clock,
                                 args=(proc.stderr, gen), daemon=True,
                                 name="music_synth-ffplay-clock").start()
                self._playback_proc = proc
                self.logger.info("[playback] mode=ffplay pid=%s file=%s", proc.pid, target)
                return {"mode": "ffplay", "pid": proc.pid, "clock": "audio"}
            except OSError as exc:
                self.logger.warning("ffplay 播放失败，改用 winsound：%s", exc)
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(str(target), winsound.SND_FILENAME | winsound.SND_ASYNC)
                self.logger.info("[playback] mode=winsound file=%s", target)
                return {"mode": "winsound"}
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("winsound 播放失败：%s", exc)
        # 2026-11-03：不再用 os.startfile 兜底。实测它会拉起系统/Store 播放器
        # 窗口，而且在插件进程里常常开了就退，正是"反复弹出再关掉"的来源之一；
        # 面板试听统一走界面内 <audio>（ms_track_audio），这里只负责 AI 主动唱。
        self.logger.warning("[playback] 无可用后端（ffplay 缺失且 winsound 失败）file=%s", target)
        return {"mode": "none", "error": "没有可用的本地播放后端（ffplay 缺失且 winsound 失败）"}

    def _lips_start(self, track: dict, target: str, *, audio: bool = False) -> dict:
        """Publish this song's per-character mouth track for the main window.

        The song plays through ffplay/winsound, so the app's own lip-sync (which
        follows TTS audio) never sees it.  ``lips_server`` exposes the cue track
        plus a clock; ``music-groove-bridge.js`` in the main window polls it and
        writes ParamMouthOpenY / ParamMouthForm, which is what makes the mouth
        follow the sung syllables instead of flapping at random.

        ``audio=True`` 时同时把成品文件挂到 ``/song.wav``：主窗口可以用
        ``<audio>`` 播它，嘴直接读 ``currentTime``（同一条时间线）。
        """
        payload = self._lips_payload(track, target)
        cues = payload.get("cues") or []
        duration = float(payload.get("duration") or track.get("duration_s") or 0.0)
        song = str(payload.get("song") or track.get("title") or "")
        track_id = str(track.get("id") or "")
        # audio=True：把成品 WAV 也挂到口型服务上，主窗口的 <audio> 拉起它边下边播，
        # 嘴直接读 audio.currentTime。预演（ms_lips_preview）传 False，保持静音。
        audio_path = str(target or "") if audio else ""
        if cues:
            # v6：随 cue 轨一起发干声包络，桥用它调制张口度——只有五个元音
            # 固定张度的话，嘴在字与字之间是"定格"的，看起来像木偶。
            state = lips_server.set_playing(cues=cues, song=song, track_id=track_id,
                                            duration=duration, audio_path=audio_path,
                                            env=payload.get("env") or [],
                                            hop=float(payload.get("hop") or 0.05))
            return {**state, "source": payload.get("source"),
                    "align": payload.get("align"),
                    "lang": payload.get("lang") or str(track.get("vocal_language") or "")}

        # 没有歌词也得动嘴：用分离出来的干声算响度包络，跟着「真的在唱」的地方开合。
        # 日文/冷门歌经常抓不到歌词，这条兜底决定它到底是「哑唱」还是「唱」。
        env_amp = payload.get("env") or []
        hop = float(payload.get("hop") or 0.05)
        if env_amp:
            # 新版桥按包络平滑驱动；旧版桥只认歌词 cue，就给它一份从包络切出来的
            # 粗口型轨——不刷新页面也能立刻动嘴。
            state = lips_server.set_playing(
                cues=lipsync.envelope_cues({"amp": env_amp, "hop": hop}),
                env=env_amp, hop=hop, mode="vocal_envelope",
                song=song, track_id=track_id, duration=duration,
                audio_path=audio_path)
            return {**state, "source": "vocal_envelope", "stem": payload.get("stem")}
        lips_server.clear()
        return {"ok": True, "active": False, "reason": "no_cues",
                "stem": payload.get("stem")}

    # 口型包络只认这些「干净人声」工件。绝对不能把 final（伴奏+人声的成品）
    # 或 source（原曲）喂进去：伴奏的鼓点会直接变成张嘴，间奏就跟着鼓乱动。
    _LIPS_CLEAN_STEMS = ("vocal_wav", "vocal_mixed", "vocal_clean", "vocals")

    def _lips_stem(self, track: dict) -> tuple[str, str]:
        """曲目 → (干净人声路径, 工件名)。找不到干净人声就返回空。"""
        artifacts = track.get("artifacts") or {}
        # vocal_wav 是变声后的干声（最干净）；vocal_mixed 是混音里的成品人声；
        # 后两个是原唱的分离/清理结果，只在前两者缺失时兜底。
        for key in self._LIPS_CLEAN_STEMS:
            candidate = str(artifacts.get(key) or "")
            if candidate and Path(candidate).is_file():
                return candidate, key
        return "", ""

    def _lips_payload(self, track: dict, target: str) -> dict:
        """这首歌的口型包：流水线算过就直接读 ``lips.json``，没有才现场补算。

        「读进度条的时候把口型数据全弄出来」就是这一步：处理曲目时
        ``Pipeline._build_lips`` 已经算好落盘，播放时只剩一次读盘 + 一次发布。

        包络和逐行对齐只用**干净人声**（见 ``_LIPS_CLEAN_STEMS``）；``target``
        是成品混音，只拿去播放，绝不参与口型计算。
        """
        artifacts = track.get("artifacts") or {}
        stem, stem_kind = self._lips_stem(track)
        cached = str(artifacts.get("lips") or "")
        if cached and Path(cached).is_file():
            try:
                data = json.loads(Path(cached).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict) and data.get("mode") in ("cues", "envelope"):
                # 口型轨 v3 起「不发音的字」沿用邻字口型（句中不再闭嘴）；
                # v4 起每行最后一个 cue 带句尾标记，桥在句尾换气收口；
                # v5 起行尾拖长音由干声包络补延续段；v6 起 cue 轨带包络，
                # 桥按响度调制张口度；v7 起每个 cue 带重音强度；
                # v8 起重音掺入「这一句里谁更响」的局部名次；
                # v9 起包络只许来自干净人声（带 stem_kind），旧缓存重算。
                try:
                    old_v = int(data.get("v") or 0)
                except (TypeError, ValueError):
                    old_v = 0
                if old_v < 9:
                    data = None
                elif isinstance(data, dict):
                    kind = str(data.get("stem_kind") or "")
                    if kind not in self._LIPS_CLEAN_STEMS and not (
                            kind == "" and not data.get("stem")):
                        # 用成品混音/原曲算出来的旧缓存：直接作废。
                        self.logger.warning(
                            "[lips] 口型缓存来源不干净（kind=%r stem=%r），重算",
                            kind, data.get("stem"))
                        data = None
            if isinstance(data, dict):
                # 歌词后来又变了（比如处理完之后「为她所听」才认出这首歌），
                # 缓存的口型轨就过期了，重算一份。
                lyrics_now, _timed, _src = lipsync.track_lyrics(track)
                key_now = lipsync.lyrics_key(lyrics_now) if lyrics_now.strip() else ""
                if str(data.get("lyrics_key") or "") == key_now:
                    return data
        # 老曲目（或者口型那一步被跳过）：现场补算一份，顺手落盘给下次用。
        if not stem:
            self.logger.warning("[lips] 没有干净人声可用（track=%s），"
                                "不拿成品混音凑数", track.get("id"))
        payload = lipsync.build_track_payload(
            track, stem=stem, duration=float(track.get("duration_s") or 0.0),
            lrc_fetcher=self._fetch_lyrics)
        payload["stem_kind"] = stem_kind
        official = payload.pop("official_lyrics", None)
        if official:
            try:
                self._pipeline.store.update(str(track.get("id") or ""),
                                            official_lyrics=official)
            except Exception:  # noqa: BLE001 - 存不下来不影响这次唱
                pass
        track_id = str(track.get("id") or "")
        if track_id and payload.get("mode") in ("cues", "envelope"):
            try:
                folder = self._pipeline.work_dir / "tracks" / track_id
                folder.mkdir(parents=True, exist_ok=True)
                target_file = folder / "lips.json"
                tmp = target_file.with_suffix(".json.part")
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, target_file)
                self._pipeline.store.update(track_id,
                                            artifacts={"lips": str(target_file)})
            except Exception:  # noqa: BLE001 - 缓存不写也能唱
                pass
        return payload

    @staticmethod
    def _clean_song_title(title: str) -> str:
        """「アルカテイル Covered by 加藤うみ」→「アルカテイル」，拿去搜官方歌词。"""
        text = str(title or "")
        text = re.sub(r"cover(?:ed)?\s*by.*$", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bcover\b.*$", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"[（(【\[][^)）】\]]*[)）】\]]", " ", text)
        text = re.sub(r"(翻唱|翻唱版|karaoke|instrumental)\s*$", " ", text,
                      flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def _fetch_lyrics(self, track: dict, force: bool = False) -> tuple[str, str]:
        """标题/歌手 → 带时间戳的官方 LRC。同一首每次进程只试一次。"""
        track_id = str(track.get("id") or "")
        tried = getattr(self, "_lrc_tried", None)
        if tried is None:
            tried = self._lrc_tried = set()
        if not force and track_id and track_id in tried:
            return "", ""
        if track_id:
            tried.add(track_id)
        try:
            from . import lyrics_fetch
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[playback] 歌词模块不可用：%s", exc)
            return "", ""
        title = self._clean_song_title(track.get("title"))
        artist = str(track.get("artist") or "")
        src = str(track.get("source") or "")
        for guess_title, guess_artist in ((title, artist), (title, "")):
            if not guess_title:
                continue
            try:
                lrc, how = lyrics_fetch.fetch_official_lrc(
                    guess_title, guess_artist, src, timeout=6.0)
            except Exception as exc:  # noqa: BLE001 - 网络问题不该挡住唱歌
                lrc, how = None, f"error:{exc}"
            if lrc:
                return lrc, how
        self.logger.info("[playback] 没取到官方歌词 title=%r artist=%r", title, artist)
        return "", ""

    def _finished(self, track: dict | None) -> bool:
        """有成品而且文件还在，才算「能唱」。"""
        target = str(((track or {}).get("artifacts") or {}).get("final") or "")
        return bool(target) and Path(target).is_file()

    def _sing_track(self, track: dict) -> dict:
        """播放一首已经做好的歌，并把口型轨一起发出去（AI 和面板共用）。"""
        target = str((track.get("artifacts") or {}).get("final") or "")
        if not target or not Path(target).is_file():
            return {"ok": False, "error": "这首还没有成品", "track_id": track.get("id")}
        try:
            how = self._start_singing(target)
        except OSError as exc:
            return {"ok": False, "error": f"播放失败：{exc}", "path": target}
        if how.get("mode") == "none":
            return {"ok": False, "error": how.get("error") or "没有可用的播放后端",
                    "path": target}
        self._last_play_target = target
        self._last_play_at = time.monotonic()
        lips: dict = {}
        try:
            lips = self._lips_start(track, target, audio=True)
        except Exception as exc:  # noqa: BLE001 - 口型起不来不能挡播放
            self.logger.warning("[playback] 口型轨启动失败：%s", exc)
            lips = {"ok": False, "error": str(exc)}
        self._arm_playback_fallback(target, int(lips.get("seq") or 0))
        self.logger.info("[playback] 已起播 track=%s mode=%s lips=%s file=%s",
                         track.get("id"), how.get("mode"),
                         {k: lips.get(k) for k in ("active", "cues", "source", "reason")},
                         target)
        return {"ok": True, "singing": True, "playing": True,
                "title": track.get("title"), "track_id": track.get("id"),
                "path": target, "timbre": track.get("timbre_source"),
                "lips": lips, **how}

    # 2026-11-03 面板入口：ui.action 与 plugin_entry 同名，让 hosted-ui 能转发 ms_play。
    # 宿主 hosted-ui 只转发 surface 已声明的 ui.action（实测未声明 = 403），
    # 因此面板调用必须显式声明并 refresh_context=False，避免面板轮询时被强制刷新。
    # 实测：hosted-ui 转发时不会把 arguments 传进来（kwargs 只有 _ctx），
    # 所以面板不再依赖这个入口，改用 ms_track_audio 取音频在界面内播放。
    @ui.action(id="ms_play", label="播放成品", refresh_context=False)
    @plugin_entry(id="ms_play", name="为她而唱 · 播放成品",
                  description="在本机播放某首曲目的成品 WAV（ffplay/winsound，不依赖默认程序关联）",
                  input_schema={"type": "object", "properties": {"track_id": {"type": "string"}}})
    async def ms_play(self, track_id: str = "", **kwargs):
        if not track_id:
            # hosted-ui 转发时参数可能落在单个 "payload"/"args"/"input" 里，兜底解包
            for key in ("payload", "args", "input", "params", "arguments", "body"):
                bag = kwargs.get(key)
                if isinstance(bag, dict) and bag.get("track_id"):
                    track_id = str(bag["track_id"])
                    break
        track = self._pipeline.store.get(track_id)
        if not track:
            # 2026-11-03 取证：需要知道到底收到了什么
            self.logger.warning("[playback] ms_play 未命中 track_id=%r 已载入=%d 首 kwargs=%s",
                                track_id, len(self._pipeline.store.all()),
                                {k: repr(v)[:80] for k, v in kwargs.items()})
            return Ok({"ok": False, "error": "曲目不存在"})
        target = (track.get("artifacts") or {}).get("final") or ""
        if not target or not Path(target).is_file():
            self.logger.warning("[playback] 无成品 path=%r exists=%s",
                                target, Path(target).is_file() if target else False)
            return Ok({"ok": False, "error": "这首还没有成品", "track": track})
        now = time.monotonic()
        # 防抖：同一首在 2.5 秒内重复触发（双击/连点/面板重复派发）直接忽略。
        # 2026-11-03 实测：面板一次点击会派发多次 ms_play，旧条件还要求
        # 上一次的 ffplay 进程仍存活才拦截，而 winsound 模式没有进程对象，
        # 于是连点仍会一次次起播（日志 2.5 秒内 6 次）。现在无条件按时间窗拦截。
        if target == self._last_play_target and now - self._last_play_at < 2.5:
            self.logger.info("[playback] 2.5s 内重复请求同一首，忽略 file=%s", target)
            return Ok({"ok": True, "debounced": True, "playing": target,
                       "title": track.get("title"), "mode": "already_playing"})
        try:
            how = await asyncio.to_thread(self._start_singing, target)
        except OSError as exc:
            return Ok({"ok": False, "error": f"播放失败：{exc}", "path": target})
        if how.get("mode") == "none":
            return Ok({"ok": False, "error": how.get("error") or "没有可用的播放后端", "path": target})
        self._last_play_target = target
        self._last_play_at = now
        lips = {}
        try:
            lips = await asyncio.to_thread(self._lips_start, track, target, audio=True)
        except Exception as exc:  # noqa: BLE001 - 口型起不来不能挡播放
            self.logger.warning("[playback] 口型轨启动失败：%s", exc)
            lips = {"ok": False, "error": str(exc)}
        self._arm_playback_fallback(target, int(lips.get("seq") or 0))
        self.logger.info("[playback] ms_play 已起播 track=%s mode=%s lips=%s file=%s",
                         track_id, how.get("mode"),
                         {k: lips.get(k) for k in ("active", "cues", "source", "reason")},
                         target)
        return Ok({"ok": True, "playing": target, "title": track.get("title"),
                   "lips": lips, **how})

    @ui.action(id="ms_track_audio", label="取成品音频", refresh_context=False)
    @plugin_entry(id="ms_track_audio", name="为她而唱 · 取成品音频",
                  description="把某首曲目的成品 WAV 读成 base64 交给面板在界面内播放（不拉起任何外部播放器）",
                  input_schema={"type": "object", "properties": {"track_id": {"type": "string"}}})
    async def ms_track_audio(self, track_id: str = "", **_):
        """参数从面板经 hosted-ui 转发（实测会丢参数，面板因此改读 /runs 导出结果）。

        只读：不启动任何进程、不打开任何窗口。
        """
        track = self._pipeline.store.get(track_id)
        if not track:
            return Ok({"ok": False, "error": "曲目不存在"})
        target = (track.get("artifacts") or {}).get("final") or ""
        if not target and str(track.get("origin") or "") == "ace":
            # 「只生成」的原创曲还没有 final，直接试听 ACE 生成的原曲
            src = str(track.get("source") or "")
            if src and Path(src).is_file():
                target = src
        path = Path(target) if target else None
        if not path or not path.is_file():
            return Ok({"ok": False, "error": "这首还没有成品"})
        if path.stat().st_size > 200 * 1024 * 1024:
            return Ok({"ok": False, "error": f"成品过大（{path.stat().st_size // 1024 // 1024}MB），请在文件里直接打开：{path}"})
        try:
            payload = await asyncio.to_thread(self._audio_payload, path)
        except OSError as exc:
            return Ok({"ok": False, "error": f"读取失败：{exc}"})
        return Ok({"ok": True, "track_id": track_id, "title": track.get("title"),
                   "file": str(path), **payload})

    @plugin_entry(id="ms_stop_playback", name="为她而唱 · 停止播放",
                  description="停止当前正在播放的成品",
                  input_schema={"type": "object", "properties": {}})
    async def ms_stop_playback(self, **_):
        self._stop_playback()
        return Ok({"ok": True})

    @plugin_entry(id="ms_lips_status", name="为她而唱 · 口型状态",
                  description=("逐字口型服务状态；带 track_id 时顺便检查/重建该曲目的口型轨"
                               "（rebuild=true 强制重建）"),
                  input_schema={"type": "object", "properties": {
                      "track_id": {"type": "string"},
                      "rebuild": {"type": "boolean"}}})
    async def ms_lips_status(self, track_id: str = "", rebuild: bool = False, **_):
        info = {"server": lips_server.status()}
        if not track_id:
            return Ok({"ok": True, **info})
        track = self._pipeline.store.get(track_id)
        if not track:
            return Ok({"ok": False, "error": "曲目不存在", **info})
        target = ((track.get("artifacts") or {}).get("final")
                  or str(track.get("source") or ""))
        if not target or not Path(target).is_file():
            return Ok({"ok": False, "error": "这首还没有音频", **info})
        if rebuild:
            old = str((track.get("artifacts") or {}).get("lips") or "")
            try:
                if old and Path(old).is_file():
                    Path(old).unlink()
            except OSError:
                pass
        try:
            # 和真正播放时同一条路径：只拿干净人声算包络，成品混音只用于播放。
            payload = await asyncio.to_thread(self._lips_payload, track, target)
        except Exception as exc:  # noqa: BLE001
            return Ok({"ok": False, "error": f"{type(exc).__name__}: {exc}", **info})
        cues = payload.get("cues") or []
        track_id_now = str(track.get("id") or track_id)
        sidecar = str(((self._pipeline.store.get(track_id_now) or {})
                       .get("artifacts") or {}).get("lips") or "")
        return Ok({"ok": True, "track_id": track_id, "title": track.get("title"),
                   "cues": len(cues), "source": payload.get("source"),
                   "duration": payload.get("duration"),
                   "stem": payload.get("stem") or "",
                   "stem_kind": payload.get("stem_kind") or "",
                   "env": len(payload.get("env") or []),
                   "sidecar": sidecar,
                   "sample": cues[:6], **info})

    @plugin_entry(id="ms_lips_preview", name="为她而唱 · 口型试音",
                  description=("不出声，只让角色按一段歌词对口型，用来检查嘴是否跟着字走"
                               "（text 留空用默认四句，seconds 为试音时长；"
                               "给 track_id 则直接用那首歌的口型轨，同样不出声）"),
                  input_schema={"type": "object", "properties": {
                      "text": {"type": "string"},
                      "seconds": {"type": "number"},
                      "track_id": {"type": "string"}}})
    async def ms_lips_preview(self, text: str = "", seconds: float = 12.0,
                              track_id: str = "", **_):
        if track_id:
            track = self._pipeline.store.get(track_id) or {}
            target = str((track.get("artifacts") or {}).get("final") or "") \
                or str((track.get("artifacts") or {}).get("vocal_mixed") or "")
            if not track:
                return Ok({"ok": False, "error": "没有这首", "track_id": track_id})
            state = await asyncio.to_thread(self._lips_start, track, target or "")
            return Ok({"ok": bool(state.get("active")), "silent": True,
                       "title": track.get("title"), **state})
        lyrics = str(text or "").strip() or "床前明月光\n疑是地上霜\n举头望明月\n低头思故乡"
        try:
            span = float(seconds or 12.0)
        except (TypeError, ValueError):
            span = 12.0
        span = max(3.0, min(120.0, span))
        cues = lipsync.build_cues(lyrics, span)
        if not cues:
            return Ok({"ok": False, "error": "没有可用的字"})
        state = lips_server.set_playing(cues=cues, song="口型试音",
                                        track_id="preview", duration=span)
        return Ok({"ok": True, **state, "seconds": span, "text": lyrics,
                   "sample": cues[:6]})

    # 2026-11-03 面板「设为当前模型」：只做用户显式点击的切换，复用 ms_set_active_model。
    # 入口本体在文件末尾的 ms_set_active_model 上声明；这里再挂一个 @plugin_entry 会与它
    # 抢同一个 id（后注册的覆盖前面的），所以只保留 @ui.action 作为按钮绑定。
    @ui.action(id="ms_set_active_model", label="设为当前模型", refresh_context=True)
    async def ms_set_active_model_action(self, ckpt: str = "", **_):
        return await self.ms_set_active_model(ckpt=ckpt)

    @llm_tool(
        name="music_synth_sing",
        description=("让她唱出已经做好的歌。【演唱/播放】入口：按歌名或 track_id 找到成品，"
                     "直接用系统播放器放出来，并返回文件路径。"
                     "如果点名的歌在库里但还没唱过（刚录下来/刚生成的原曲），会自动开始处理，"
                     "做完自动播放，这时返回 processing=true —— 告诉用户「正在做，做好就唱」，"
                     "不要说已经唱好了。只有真的拿到 ok=true 且 singing=true 才算唱了；"
                     "ok=false 时必须如实说明原因。"),
        parameters={"type": "object", "properties": {
            "title": {"type": "string", "description": "歌名关键词，可不填"},
            "track_id": {"type": "string", "description": "曲目 id，可不填"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 5}},
        }, timeout=30.0,
    )
    @plugin_entry(id="ms_sing", name="为她而唱 · 唱出来",
                  description="AI 调用：按歌名/id 找到成品并用系统播放器播放",
                  input_schema={"type": "object", "properties": {
                      "title": {"type": "string"}, "track_id": {"type": "string"},
                      "limit": {"type": "integer", "minimum": 1, "maximum": 5}}})
    async def ms_sing(self, title: str = "", track_id: str = "", limit: int = 1, **_):
        """唱出来。有成品就直接放；点了名但还没唱过的，先把处理跑起来。

        以前这里只会挑「已经有成品」的曲目，录下来还没处理过的歌会直接返回
        ok=false，AI 拿到失败结果仍然会回一句「唱好了」——链表断在最开始这一环。
        """
        tracks = self._pipeline.store.all()
        picked = None
        if track_id:
            track = self._pipeline.store.get(str(track_id))
            if not track:
                self.logger.info("[playback] ms_sing 曲目不存在 track_id=%r", track_id)
                return Ok({"ok": False, "error": "曲目不存在", "track_id": track_id})
            if not self._finished(track):
                return await self._begin_sing(track)
            picked = track
        else:
            needle = str(title or "").strip().lower()
            if needle:
                hits = [t for t in tracks if needle in str(t.get("title", "")).lower()]
                if not hits:
                    hits = [t for t in tracks
                            if needle in str(t.get("lyrics_preview", "")).lower()]
            else:
                hits = list(tracks)
            ready = [t for t in hits if self._finished(t)]
            if ready:
                picked = ready[-int(limit or 1):][-1]
            elif hits:
                # 歌在库里，只是还没唱过（录下来的原曲 / 刚生成的原曲）：直接开唱。
                idle = [t for t in hits
                        if str(t.get("status") or "") not in ("running", "queued",
                                                              "instrumental", "incomplete",
                                                              "silent")]
                if idle:
                    return await self._begin_sing(idle[-1])
                return await self._begin_sing(hits[-1])

        if picked is None:
            pending = [t for t in tracks if t.get("status") != "done"]
            self.logger.info("[playback] ms_sing 没有可唱的成品 title=%r tracks=%d",
                             title, len(tracks))
            return Ok({"ok": False, "error": "还没有可以唱的成品",
                       "hint": "用 music_synth_sing_anchor 先把录到的歌跑一遍（分离→变声→混音）",
                       "pending": [{"id": t["id"], "title": t.get("title"),
                                    "status": t.get("status"), "step": t.get("step")}
                                   for t in pending[:5]]})
        res = await asyncio.to_thread(self._sing_track, picked)
        return Ok(res)

    async def _begin_sing(self, track: dict) -> "Ok":
        """还没成品的歌：开始处理，并记下来，做完自动播放。"""
        track_id = str(track.get("id") or "")
        status = str(track.get("status") or "")
        if status in ("instrumental", "incomplete", "silent") and not track.get("check_override"):
            # 纯音乐 / 没放完整 / 空录音：别再排一遍队白等，直接说清楚。
            reason = str(track.get("check_reason") or "") or "这首歌不能翻唱"
            self.logger.info("[playback] ms_sing 拒绝不能唱的歌 track=%s status=%s",
                             track_id, status)
            return Ok({"ok": False, "track_id": track_id, "title": track.get("title"),
                       "status": status, "error": reason,
                       "note": "纯音乐没有人声、或者这段录音没放完整；完整听一遍再唱"})
        self._pending_play.add(track_id)
        if status in ("running", "queued"):
            self.logger.info("[playback] ms_sing 已在处理，登记自动播放 track=%s", track_id)
            return Ok({"ok": True, "processing": True, "already": True,
                       "track_id": track_id, "title": track.get("title"), "status": status,
                       "note": "这首正在处理，做完会自动播放"})
        if not str(track.get("lyrics") or "").strip():
            lp = await self._perception_lyrics()
            if lp and self._lyrics_for_track(track, lp):
                songs = lp.get("songs") or []
                official = lp.get("official_lyrics") or {}
                if songs or official:
                    self._pipeline.store.update(
                        track_id, lyrics_from_perception=songs, official_lyrics=official)
        job_id = self._background_process(track_id, kind="sing", perception_priority=True)
        self.logger.info("[playback] ms_sing 开始处理 track=%s title=%s job=%s",
                         track_id, track.get("title"), job_id)
        return Ok({"ok": True, "started": True, "processing": True,
                   "track_id": track_id, "title": track.get("title"), "job_id": job_id,
                   "note": "这首还没唱过，已经开始处理（分离→变声→混音），做完会自动播放；"
                           "先别催，处理完我再告诉你"})

    @staticmethod
    def _lyrics_for_track(track: dict, lp: dict) -> bool:
        """「为她所听」现在的歌词是不是这首歌的（不是就别往曲目上贴）。"""
        def norm(text) -> str:
            return str(text or "").strip().lower().replace(" ", "")
        current = norm(((lp.get("official_lyrics") or {}) or {}).get("title"))
        if not current:
            songs = lp.get("songs") or []
            current = norm((songs[0] or {}).get("title")) if songs else ""
        mine = norm(track.get("title"))
        if not current or not mine:
            return False
        return current in mine or mine in current

    @plugin_entry(id="ms_delete_track", name="为她而唱 · 删除曲目",
                  description="从曲目列表移除；delete_files=True 时把磁盘上的成品/中间文件也移进回收站",
                  input_schema={"type": "object", "properties": {
                      "track_id": {"type": "string"},
                      "delete_files": {"type": "boolean"}},
                      "required": ["track_id"]})
    async def ms_delete_track(self, track_id: str = "", delete_files: bool = False, **_):
        """删除一首成品。delete_files=True 时把磁盘上的产物也移进回收站。"""
        track = self._pipeline.store.get(track_id)
        if not track:
            return Ok({"ok": False, "error": "曲目不存在"})
        # 处理中：先让流水线在阶段边界停下，再由它自己把记录/文件删掉。
        # 直接删记录会让后台线程继续跑、还把文件写回来，之前就是这么出问题的。
        if str(track.get("status") or "") in ("queued", "processing", "running"):
            if self._pipeline.request_cancel(track_id, bool(delete_files)):
                return Ok({"ok": True, "cancelling": True, "track_id": track_id,
                           "note": "正在停止这一首，停下后自动删除"
                                   + ("（含本地文件）" if delete_files else "")})
            # 没有活着的流水线（插件重启过，状态停在 running）：直接当普通删除处理
            pass
        removed = self._pipeline.store.remove(track_id)
        moved = 0
        if delete_files:
            paths = []
            for value in (track.get("artifacts") or {}).values():
                if value:
                    paths.append(Path(str(value)))
            work_dir = self._pipeline.work_dir / "tracks" / str(track_id)
            if work_dir.is_dir():
                paths.append(work_dir)
            for item in paths:
                try:
                    if item.exists() and self._trash_move(item).get("moved"):
                        moved += 1
                except Exception:  # noqa: BLE001
                    pass
        return Ok({"ok": removed, "track_id": track_id, "files_deleted": moved})

    @plugin_entry(id="ms_delete_run", name="为她而唱 · 删除训练记录",
                  description="删除一条训练记录；delete_files=True 时把 run 目录和它的检查点一起移进回收站",
                  input_schema={"type": "object", "properties": {
                      "run_id": {"type": "string"},
                      "delete_files": {"type": "boolean"}},
                      "required": ["run_id"]})
    async def ms_delete_run(self, run_id: str = "", delete_files: bool = False, **_):
        run_id = str(run_id or "").strip()
        if not runtime.valid_run_id(run_id):
            return Ok({"ok": False, "error": f"运行编号格式不对：{run_id!r}"})
        run_dir = self._train_run_dir(run_id)
        state = runtime.read_training_state(run_dir) or {}
        if str(state.get("phase") or "") not in runtime.TRAIN_ENDED and self._train_proc_live(run_id):
            return Ok({"ok": False, "error": "这条训练还在跑，先「停下来」再删"})
        expdir = self._train_repo() / "exp" / f"_run_{run_id}"
        moved = 0
        if delete_files:
            for item in (run_dir, expdir):
                try:
                    if item.exists() and self._trash_move(item).get("moved"):
                        moved += 1
                except Exception:  # noqa: BLE001
                    pass
        else:
            # 只删记录：run 目录（state/previews）移进回收站，检查点留在 exp 里
            try:
                if run_dir.exists() and self._trash_move(run_dir).get("moved"):
                    moved += 0
            except Exception:  # noqa: BLE001
                pass
        return Ok({"ok": True, "run_id": run_id, "files_deleted": moved})

    @plugin_entry(id="ms_delete_model", name="为她而唱 · 删除训练好的声音",
                  description="删除一个 model_*.pt；delete_files=True 时把文件和它所在实验目录一起移进回收站",
                  input_schema={"type": "object", "properties": {
                      "path": {"type": "string"},
                      "delete_files": {"type": "boolean"}},
                      "required": ["path"]})
    async def ms_delete_model(self, path: str = "", delete_files: bool = False, **_):
        target = Path(str(path or ""))
        repo = self._train_repo()
        try:
            target = target.resolve()
            allowed = (repo / "exp").resolve() in target.parents
        except OSError:
            allowed = False
        if not allowed or target.suffix.lower() != ".pt" or not target.is_file():
            return Ok({"ok": False, "error": "只能删除训练目录里的 model_*.pt"})
        current = Path(str(self._pipeline.paths.get("ddsp_ckpt") or ""))
        if current and target == current.resolve():
            return Ok({"ok": False, "error": "这是当前正在用的声音，先在「我的声音」里换一个再删"})
        moved = 0
        if delete_files:
            exp_dir = target.parent
            try:
                if self._trash_move(exp_dir).get("moved"):
                    moved = 1
            except Exception:  # noqa: BLE001
                try:
                    if self._trash_move(target).get("moved"):
                        moved = 1
                except Exception:  # noqa: BLE001
                    pass
        else:
            try:
                target.unlink()
            except OSError as exc:
                return Ok({"ok": False, "error": f"删除失败：{exc}"})
        return Ok({"ok": True, "path": str(target), "files_deleted": moved})

    # ------------------------------------------------------------------
    # training wizard: hardware profile / corpus library (§5 contract)
    # ------------------------------------------------------------------
    @staticmethod
    def _cpu_threads() -> int:
        try:
            return os.cpu_count() or 1
        except Exception:  # noqa: BLE001
            return 1

    @staticmethod
    def _hw_tier(vram_mb: int, ram_gb: float, has_gpu: bool) -> str:
        if not has_gpu:
            return "cpu"
        if vram_mb < 6000 or ram_gb < 16:
            return "low"
        if vram_mb < 12000 or ram_gb < 32:
            return "mid"
        return "high"

    @plugin_entry(id="ms_hw_profile", name="为她而唱 · 训练硬件探测",
                  description="探测 GPU/显存/CPU/内存，按档位给出训练 batch/num_workers/精度与分离 batch",
                  input_schema={"type": "object", "properties": {}})
    async def ms_hw_profile(self, **_):
        probe = await asyncio.to_thread(runtime.probe_hardware, True)
        gpu = probe.get("gpu") or {}
        has_gpu = bool(gpu)
        vram_mb = int(round(float(gpu.get("total_gib", 0.0)) * 1024))
        ram_gb = float(probe.get("ram_total_gib") or 0.0)
        cpu_threads = self._cpu_threads()
        tier = self._hw_tier(vram_mb, ram_gb, has_gpu)
        # 经验值：训练 batch/num_workers 按档位估，分离 batch 按显存估。
        # 未跑过的数字不写；这里只给已在此机型验证过的经验档。
        train = {"batch_size": 8, "num_workers": 2, "amp": "fp16"}
        if tier in ("low", "cpu"):
            train = {"batch_size": 4, "num_workers": 1, "amp": "fp16"}
        elif tier == "mid":
            train = {"batch_size": 8, "num_workers": 2, "amp": "fp16"}
        else:  # high
            train = {"batch_size": 16, "num_workers": 4, "amp": "fp16"}
        sep_batch = 1
        if has_gpu and vram_mb >= 12000:
            sep_batch = 2
        return Ok({
            "ok": True,
            "gpu": {"name": gpu.get("name"), "vram_mb": vram_mb} if has_gpu else None,
            "cpu_threads": cpu_threads,
            "ram_gb": round(ram_gb, 1),
            "tier": tier,
            "train": train,
            "sep": {"batch_size": sep_batch, "use_autocast": has_gpu},
            "note": "batch/num_workers 为经验档，训练时按 OOM 自动降档重试（最多 2 次）。",
        })

    # 步数经验值：素材越长，训练步数越多。
    # 这张表不是拍脑袋——原来的固定档位就是这个比例：
    #   90s→4000、180s→8000、300s→12000、360s→16000，≈ 每秒素材 44 步。
    # 现在改成按实际有效时长线性算，取整到 1000 步，并限制在 4000~30000 之间。
    _STEPS_PER_SECOND = 44.0
    _STEPS_MIN = 4000
    _STEPS_MAX = 30000        # 自动推荐的上限（再往上收益很小）
    _STEPS_HARD_MAX = 200000  # 手动填写的工程上限：30000 以上想继续练就自己填
    # 推荐时长 ≈ 最低时长 × 1.65（沿用旧表的平均比例）
    _REC_RATIO = 1.65

    @classmethod
    def _steps_for_seconds(cls, seconds: float) -> int:
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 0.0
        if seconds <= 0:
            return cls._STEPS_MIN
        # 向下取整：保证 steps/44 ≤ 实际时长，自动算出来的步数不会因为取整反而"素材不够"
        raw = int(seconds * cls._STEPS_PER_SECOND / 1000.0) * 1000
        return max(cls._STEPS_MIN, min(cls._STEPS_MAX, raw))

    @classmethod
    def _req_for_steps(cls, steps: int):
        steps = max(cls._STEPS_MIN, min(cls._STEPS_HARD_MAX, int(steps)))
        min_s = round(steps / cls._STEPS_PER_SECOND, 1)
        rec_s = round(min_s * cls._REC_RATIO, 1)
        return min_s, rec_s

    @plugin_entry(id="ms_corpus_requirements", name="为她而唱 · 训练时长要求",
                  description="按步数给出素材有效时长的最低/推荐值；不传步数时按每个素材文件的实际总时长自动算（经验值）",
                  input_schema={"type": "object", "properties": {
                      "steps": {"type": "integer", "minimum": 4000, "maximum": 200000},
                      "valid_s": {"type": "number", "minimum": 0},
                      "files_s": {"type": "number", "minimum": 0}}})
    async def ms_corpus_requirements(self, steps: int = 0, valid_s: float = -1,
                                     files_s: float = -1, **_):
        try:
            steps = int(steps or 0)
        except (TypeError, ValueError):
            return Ok({"ok": False, "error": f"步数不合法：{steps!r}"})
        auto = steps <= 0
        try:
            have = float(valid_s)
        except (TypeError, ValueError):
            have = -1.0
        try:
            files_have = float(files_s)
        except (TypeError, ValueError):
            files_have = -1.0
        if have < 0 or files_have < 0:
            view = self._corpus_scan(self._corpus_index())
            if have < 0:
                have = float(view.get("total_valid_s") or 0.0)
            if files_have < 0:
                files_have = float(view.get("total_files_s") or 0.0)
        # 步数按「每个文件的实际时长」算；还没量到时退回质检切片时长
        base_s = files_have if files_have > 0 else have
        if auto:
            steps = self._steps_for_seconds(base_s)
        steps = max(self._STEPS_MIN, min(self._STEPS_HARD_MAX, steps))
        min_s, rec_s = self._req_for_steps(steps)
        reasons = [
            "步数按每个素材文件的实际时长算（每秒约 %d 步），文件和时长越多练得越久、越像她。" % int(self._STEPS_PER_SECOND),
            "质检切片的有效时长（不含 speech 时以 song+dry_sing 计）用来判断素材够不够。",
            "低于 %d 步容易欠拟合，高于 %d 步收益很小、只是更费时间。" % (self._STEPS_MIN, self._STEPS_MAX),
        ]
        return Ok({"ok": True, "steps": steps, "min_s": min_s, "rec_s": rec_s,
                   "valid_s": round(have, 1), "files_s": round(files_have, 1),
                   "auto": auto, "reasons": reasons})

    # ---- corpus storage helpers ----
    def _corpus_root(self) -> Path:
        root = Path(str(self._pipeline.paths.get("compute_root") or "")) / "corpus"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _corpus_index_path(self) -> Path:
        return self._corpus_root() / "index.json"

    def _corpus_index(self) -> dict:
        try:
            data = json.loads(self._corpus_index_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _corpus_write_index(self, index: dict) -> None:
        runtime._atomic_write(self._corpus_index_path(), index)

    @staticmethod
    def _new_source_id() -> str:
        return time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"

    def _trash_dir(self) -> Path:
        # 回收站也放在用户选择的安装根目录里，不写死到别的盘。
        root = Path(str(self._pipeline.paths.get("compute_root") or self._data_dir))
        d = root / "_trash"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _trash_manifest_path(self) -> Path:
        return self._trash_dir() / "manifest.json"

    def _trash_manifest(self) -> dict:
        try:
            data = json.loads(self._trash_manifest_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"items": []}
        except (OSError, ValueError):
            return {"items": []}

    def _trash_write_manifest(self, manifest: dict) -> None:
        runtime._atomic_write(self._trash_manifest_path(), manifest)

    def _trash_move(self, path: Path, hash_file: bool = True) -> dict:
        """Move ``path`` into the trash, recording size + sha256 in the manifest."""
        path = Path(path)
        if not path.exists():
            return {"ok": False, "error": f"不存在：{path}", "moved": False}
        size = 0
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        digest = ""
        if hash_file:
            try:
                import hashlib
                # 分块读，不把整首歌一次性读进内存（原来 read_bytes() 一删大文件就卡住事件循环）
                h = hashlib.sha256()
                with open(path, "rb") as fh:
                    while True:
                        chunk = fh.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                digest = h.hexdigest()
            except OSError:
                digest = ""
        target = self._trash_dir() / f"{int(time.time() * 1000)}_{path.name}"
        try:
            shutil.move(str(path), str(target))
        except OSError as exc:
            return {"ok": False, "error": f"{exc}", "moved": False}
        manifest = self._trash_manifest()
        manifest.setdefault("items", []).append({
            "from": str(path), "to": str(target), "bytes": size, "sha256": digest,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._trash_write_manifest(manifest)
        return {"ok": True, "moved": True, "to": str(target), "bytes": size, "sha256": digest}

    def _trash_tree(self, root: Path) -> int:
        """把一个目录里的文件全部挪进回收站，再删掉空目录；返回挪走的文件数。

        只动文件、只删空目录：万一有文件没搬成功，那个目录会原样留着，不会误删。
        ``hash_file=False`` —— 这些是模型中间产物（往往几百 MB），重算 sha256 没必要。
        """
        root = Path(root)
        if not root.is_dir():
            return 0
        moved = 0
        for f in sorted(root.rglob("*"), reverse=True):
            try:
                if f.is_file() and self._trash_move(f, hash_file=False).get("moved"):
                    moved += 1
            except Exception:  # noqa: BLE001
                pass
        try:
            for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
                if not any(d.iterdir()):
                    d.rmdir()
            if not any(root.iterdir()):
                root.rmdir()
        except Exception:  # noqa: BLE001
            pass
        return moved

    def _corpus_backfill_durations(self) -> None:
        """给老数据补上每个文件的 dur_s（训练步数要用）。"""
        try:
            index = self._corpus_index()
        except Exception:  # noqa: BLE001
            return
        changed = False
        for src in index.get("sources", []):
            for f in src.get("files", []):
                if not isinstance(f, dict) or f.get("dur_s"):
                    continue
                p = Path(str(f.get("path") or ""))
                if not p.is_file():
                    continue
                dur = self._probe_duration(p)
                if dur > 0:
                    f["dur_s"] = dur
                    changed = True
        if changed:
            try:
                self._corpus_write_index(index)
            except Exception:  # noqa: BLE001
                pass

    def _corpus_drop_missing_sources(self) -> None:
        """Remove index entries whose original files and clips are all gone."""
        try:
            with self._corpus_lock:
                index = self._corpus_index()
                keep, dropped = [], []
                for src in index.get("sources", []):
                    files = [f for f in src.get("files", []) if isinstance(f, dict)
                             and Path(str(f.get("path") or "")).is_file()]
                    clips = [c for c in src.get("clips", []) if isinstance(c, dict)
                             and c.get("path") and Path(str(c["path"])).is_file()]
                    if not files and not clips:
                        dropped.append(str(src.get("id") or ""))
                        continue
                    keep.append(src)
                if dropped:
                    index["sources"] = keep
                    self._corpus_write_index(index)
                    try:
                        self.logger.info("清理失效素材记录：%s", dropped[:10])
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            try:
                self.logger.warning("清理失效素材记录失败：%s", exc)
            except Exception:  # noqa: BLE001
                pass

    def _corpus_resume_pending(self) -> None:
        """插件重启后，把上次没跑完（status=queued/processing）的素材重新排队。"""
        try:
            index = self._corpus_index()
        except Exception:  # noqa: BLE001
            return
        for src in list(index.get("sources", [])):
            if str(src.get("status") or "") not in ("queued", "processing"):
                continue
            files = [f for f in src.get("files", [])
                     if isinstance(f, dict) and Path(str(f.get("path") or "")).is_file()]
            slot = str(src.get("slot") or "song")
            if not files:
                try:
                    self._corpus_update_source(str(src.get("id")), status="error", error="文件不在了")
                except Exception:  # noqa: BLE001
                    pass
                continue
            for f in files:
                if not f.get("dur_s"):
                    f["dur_s"] = self._probe_duration(Path(str(f["path"])))
            job_id = self._job_begin(f"corpus_{slot}")
            try:
                self._corpus_update_source(str(src.get("id")), job_id=job_id, status="queued")
            except Exception:  # noqa: BLE001
                pass
            self._corpus_enqueue(job_id, slot, str(src.get("id")), files)
            try:
                self.logger.info("恢复未完成的素材处理：%s（%s）", src.get("id"), slot)
            except Exception:  # noqa: BLE001
                pass

    def _corpus_scan(self, index: dict) -> dict:
        """Rebuild the panel view from index.json + on-disk clip files."""
        slots = {"song": {"files": [], "clips": [], "valid_s": 0.0, "files_s": 0.0},
                 "dry_sing": {"files": [], "clips": [], "valid_s": 0.0, "files_s": 0.0},
                 "speech": {"files": [], "clips": [], "valid_s": 0.0, "files_s": 0.0}}
        clips = []
        total_valid = 0.0
        total_files_s = 0.0
        # 处理中的素材：把当前任务的实时进度一起带出去（恢复/续跑的素材前端没有本地 job，只能靠这个）
        with self._jobs_lock:
            jobs_snap = {str(k): dict(v) for k, v in self._jobs.items()}
        for src in index.get("sources", []):
            slot = str(src.get("slot") or "song")
            if slot not in slots:
                slot = "song"
            entry = src
            if str(src.get("status") or "") in ("queued", "processing") and src.get("job_id"):
                job = jobs_snap.get(str(src.get("job_id")))
                if job:
                    entry = dict(src)
                    entry["progress"] = int(job.get("pct") or 0)
                    entry["stage"] = str(job.get("stage_text") or "")
                    entry["job_phase"] = str(job.get("phase") or "")
            # 隐藏的素材源：还留在 files 里（面板可以"显示已隐藏"），但不计时、不出切片、不参与训练。
            slots[slot]["files"].append(entry)
            if src.get("hidden"):
                continue
            # 每个文件自己的时长（上传时 ffprobe 量的）——训练步数按这个算
            for f in src.get("files", []):
                if isinstance(f, dict) and f.get("dur_s"):
                    slots[slot]["files_s"] = round(slots[slot]["files_s"] + float(f["dur_s"]), 3)
                    total_files_s = round(total_files_s + float(f["dur_s"]), 3)
            for c in src.get("clips", []):
                if c.get("status") == "ok":
                    slots[slot]["valid_s"] = round(slots[slot]["valid_s"] + float(c.get("dur_s", 0)), 3)
                    total_valid = round(total_valid + float(c.get("dur_s", 0)), 3)
                clips.append(c)
        # 未入库的素材文件（以前中断留下的副本，或者手放进目录的音频）：面板上给一个「恢复」入口
        # 目录扫描有成本（几十个文件 + resolve），缓存 10 秒；增删素材时主动失效。
        cached = getattr(self, "_orphans_cache", None)
        now_ts = time.time()
        if cached and now_ts - cached[0] < 10:
            orphans = dict(cached[1])
            return {"slots": slots, "total_valid_s": total_valid,
                    "total_files_s": total_files_s, "clips": clips, "orphans": orphans}
        known = set()
        for src in index.get("sources", []):
            for f in src.get("files", []):
                if isinstance(f, dict) and f.get("path"):
                    try:
                        known.add(str(Path(str(f["path"])).resolve()).lower())
                    except OSError:
                        known.add(str(f["path"]).lower())
        orphans = {"song": 0, "dry_sing": 0, "speech": 0}
        for name in orphans:
            root = self._corpus_root() / name
            if not root.is_dir():
                continue
            for p in root.iterdir():
                if not p.is_file() or p.suffix.lower() not in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"):
                    continue
                try:
                    key = str(p.resolve()).lower()
                except OSError:
                    key = str(p).lower()
                if key not in known:
                    orphans[name] += 1
        self._orphans_cache = (now_ts, dict(orphans))
        return {"slots": slots, "total_valid_s": total_valid,
                "total_files_s": total_files_s, "clips": clips, "orphans": orphans}

    def _corpus_update_source(self, source_id: str, **fields) -> None:
        """更新已登记素材源的状态/切片（不存在就忽略）。"""
        with self._corpus_lock:
            index = self._corpus_index()
            for src in index.get("sources", []):
                if src.get("id") == source_id:
                    src.update(fields)
                    break
            self._corpus_write_index(index)

    def _corpus_materialize_source(self, slot: str, source_id: str, files: list,
                                   clip_items: list, valid_s: float) -> dict:
        source = {
            "id": source_id, "slot": slot, "files": files,
            "clips": clip_items, "valid_s": round(valid_s, 3),
            "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        index = self._corpus_index()
        index.setdefault("sources", []).append(source)
        self._corpus_write_index(index)
        return source

    @staticmethod
    def _probe_duration(path: Path) -> float:
        """用 ffprobe 读音频时长（只读文件头，很快）。拿不到就返回 0，不影响上传。"""
        exe = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
        if not exe:
            return 0.0
        try:
            res = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=20,
            )
            text = (res.stdout or "").strip().splitlines()
            value = float(text[0]) if text else 0.0
            return round(value, 3) if value > 0 else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def _corpus_add_files(self, slot: str, paths: list) -> list:
        """Copy raw source files into the slot dir; return list of dicts.

        每个文件顺手量一次时长（ffprobe，只读文件头，不解码），存进 dur_s：
        训练步数按「用户素材的实际时长」算，而不是按文件个数或处理后的切片。
        """
        root = self._corpus_root() / slot
        root.mkdir(parents=True, exist_ok=True)
        out = []
        for p in paths:
            p = Path(p)
            if not p.is_file():
                continue
            target = root / p.name
            if target.exists():
                # 同名但内容不同（大小不一样）：加序号，别覆盖已入库的素材
                stem, suf = target.stem, target.suffix
                n = 2
                while target.exists():
                    target = root / f"{stem}_{n}{suf}"
                    n += 1
            try:
                shutil.copy2(p, target)
            except OSError:
                continue
            out.append({"path": str(target), "name": target.name, "bytes": target.stat().st_size,
                        "dur_s": self._probe_duration(target)})
            # 分片上传的临时文件复制完就没用了，删掉省磁盘（一首歌 10MB+）
            try:
                if p.parent.name == "_uploads":
                    p.unlink(missing_ok=True)
            except OSError:
                pass
        return out

    # Per-slot separation chain.  Each stage name maps to the separate_worker
    # model + which output stem feeds the next stage.  Ordered as listed.
    _CORPUS_CHAINS = {
        # song: 人声提取 → 去和声/伴唱。只做分离，不碰混响和降噪：
        # 数据集要的是原声，任何"重建波形"的模型都会让高保真变成模型味。
        "song": [
            ("separating", "model_bs_roformer_ep_317_sdr_12.9755.ckpt", "vocals"),
            ("separating", "mel_band_roformer_karaoke_becruily.ckpt", "vocals"),
        ],
        # dry_sing: 只有检测到整首有背景音乐才走这一步；干净的清唱保持原声，
        # 连切片都不碰模型（见 _corpus_pipeline 里的说明）。
        "dry_sing": [
            ("separating", "model_bs_roformer_ep_317_sdr_12.9755.ckpt", "vocals"),
        ],
        # speech: 只去噪（说话素材最需要的是压掉环境底噪）
        "speech": [
            ("denoise", "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt", "dry"),
        ],
    }

    def _corpus_separate_stage(self, sep_py: Path, sep_worker: Path, src: Path,
                               out_dir: Path, model: str, stem_key: str,
                               model_dir: Path, plan: dict, *,
                               job_id: str = "", pct_from: int = 0, pct_to: int = 100,
                               dur_s: float = 0.0) -> Path:
        """Run one separate_worker hop and return the chosen stem path."""
        job = {
            "input": str(src),
            "out_dir": str(out_dir),
            "backend": "audio_separator",
            "model": model,
            "model_dir": str(model_dir),
            "device": plan.get("sep_device", "cuda:0"),
            "use_cuda": plan.get("sep_device", "cuda:0") != "cpu",
            "use_autocast": plan.get("sep_device", "cuda:0") != "cpu",
        }
        # 分离是一整段黑盒（run_worker 阻塞到结束）。按音频时长估个耗时，
        # 每秒往前推一点，进度条在这个阶段内平滑走，不会卡在阶段起点不动。
        stop = threading.Event()
        if job_id:
            def beat() -> None:
                est = max(15.0, float(dur_s) * 0.35) if dur_s else 60.0
                cap = max(pct_from + 1, pct_to - 2)
                t0 = time.time()
                while not stop.wait(1.5):
                    frac = min(1.0, (time.time() - t0) / est)
                    self._job_update(job_id, pct=int(pct_from + (cap - pct_from) * frac))
            threading.Thread(target=beat, name=f"beat-{job_id}", daemon=True).start()
        try:
            res = runtime.run_worker(sep_py, sep_worker, job, cwd=out_dir, timeout=7200,
                                     log_path=out_dir / "separation.log")
        finally:
            stop.set()
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or f"{model} 分离失败")
        stems = res.get("stems") or {}
        # 非分段路径的 worker 不写 stems，只在顶层给 vocals/instrumental；
        # 分段路径写 stems 并带 target_stem。这里按优先级取目标轨。
        stem = stems.get(stem_key) or res.get("target_stem") or ""
        if not stem:
            if stem_key == "vocals":
                stem = res.get("vocals") or ""
            elif stem_key in ("noreverb", "dry"):
                stem = res.get("target_stem") or ""
        if not stem:
            for key in (stem_key, "noreverb", "dry", "vocals", "instrumental"):
                if stems.get(key):
                    stem = stems[key]
                    break
        if not stem and stem_key == "vocals":
            stem = res.get("vocals") or res.get("instrumental") or ""
        if not stem or not Path(stem).is_file():
            raise RuntimeError(f"{model} 未产出 {stem_key} 轨")
        return Path(stem)

    def _corpus_pipeline(self, job_id: str, slot: str, source_id: str, files: list) -> None:
        """Background: per-slot separation chain → slice → QC → index; never raises."""
        result = {"phase": "error", "pct": 100, "error": "", "clips": []}
        try:
            self._job_update(job_id, phase="queued", pct=0, stage_text="排队中")
            paths = [f["path"] for f in files if f.get("path")]
            if not paths:
                raise RuntimeError("没有可处理的素材文件")
            self._corpus_update_source(source_id, status="processing", error="")
            self._corpus_active_source = source_id
            self._job_update(job_id, phase="processing", pct=1, stage_text="开始处理")
            corpus_py = self._train_worker("corpus_worker.py")
            sep_worker = self._train_worker("separate_worker.py")
            sep_py = Path(str(self._pipeline.paths.get("sep_python") or ""))
            model_dir = Path(str(self._pipeline.paths.get("sep_model_dir")
                                 or (self._pipeline.work_dir / "models")))
            if not sep_py.is_file():
                raise RuntimeError(f"分离环境 Python 不存在：{sep_py}")
            if not corpus_py.is_file():
                raise RuntimeError(f"语料切分后端不存在：{corpus_py}")
            if not sep_worker.is_file():
                raise RuntimeError(f"分离后端不存在：{sep_worker}")
            probe = runtime.probe_hardware(force=False)
            plan = runtime.plan_for(probe, self._pipeline.paths.get("tier_override", ""),
                                    self._pipeline.paths.get("precision", "auto"),
                                    self._pipeline.paths.get("max_parallel", 0))
            chain = self._CORPUS_CHAINS.get(slot) or self._CORPUS_CHAINS["song"]

            # Per-source: run the separation chain, then slice the final dry stem.
            all_clips, rejected = [], []
            checks: list = []
            valid_s = 0.0
            out_root = self._corpus_root() / slot / source_id
            out_root.mkdir(parents=True, exist_ok=True)
            log_path = out_root / "corpus.log"

            # 清唱素材：一律先做人声/伴奏分离，再按伴奏轨电平决定用哪一版。
            #
            # 以前先用 bg_probe 粗筛「整首有没有背景音乐」，没有就跳过分离直接切。
            # 实测它的判据太松：伴奏压在 -24 ~ -43 dB 的素材（41972533853 实测
            # -24.5 dB、32558222369 实测 -29.8 dB）全被判成「干净」，结果这些素材
            # 切出来的片段整段都带着伴奏。原因在它的度量方式——只看「唱段附近
            # ±1 s 内最低能掉多深」，唱得密、句间没有长停顿的素材掉不下去，就会
            # 被当成清唱。现在不再靠粗筛，分离本身就是检测：
            #   1) 全部分离（BS-RoFormer，人声/伴奏两轨）；
            #   2) 看伴奏轨相对原素材的电平（stem_check_worker）：
            #      -55 dB 左右 = 模型残渣，本来就没伴奏 -> 丢掉模型输出保留原声；
            #      -43 dB 以上 = 真的有伴奏 -> 用分离后的人声轨切片。
            #
            # 去混响这一步已经彻底拿掉：实测它会把 6~11 kHz（齿音、气声、咬字的清晰度）
            # 压掉 20~37 dB，而 1~5 kHz 只掉 5~11 dB，频谱上被挖出一个洞，人声听着"闷、糊"。
            # 训练数据集要的是原声，这类重建波形的模型只会让数据变差。
            if slot == "dry_sing" and paths:
                chain = list(self._CORPUS_CHAINS["dry_sing"])

            # A single source_id may carry several files; slice each but reuse
            # the chain per file.  Track a running phase index for progress.
            total_stages = len(paths) * len(chain)
            stage_idx = 0
            for idx, raw in enumerate(paths):
                current = Path(raw)
                stems_dir = out_root / "stems" / f"src{idx:02d}"
                for phase, model, stem_key in chain:
                    stage_idx += 1
                    stage_dir = stems_dir / model.rsplit(".", 1)[0]
                    stage_dir.mkdir(parents=True, exist_ok=True)
                    p_from = int(5 + 70 * (stage_idx - 1) / max(total_stages, 1))
                    p_to = int(5 + 70 * stage_idx / max(total_stages, 1))
                    dur_s = 0.0
                    try:
                        dur_s = float((files[idx] or {}).get("dur_s") or 0.0)
                    except (TypeError, ValueError, IndexError):
                        dur_s = 0.0
                    self._job_update(
                        job_id, phase=phase, pct=p_from,
                        stage_text=f"{model.rsplit('.', 1)[0]} · {idx + 1}/{len(paths)}")
                    current = self._corpus_separate_stage(
                        sep_py, sep_worker, current, stage_dir, model, stem_key,
                        model_dir, plan,
                        job_id=job_id, pct_from=p_from, pct_to=p_to, dur_s=dur_s)
                # 分完核对伴奏轨：几乎没东西就说明本来就没有背景音乐，
                # 模型重建过的干声不如直接用原声，回退到原文件再切片。
                if slot == "dry_sing" and chain and current != Path(raw):
                    try:
                        check_worker = self._train_worker("stem_check_worker.py")
                        if check_worker.is_file():
                            ck = runtime.run_worker(
                                sep_py, check_worker,
                                {"input": str(raw), "stem": str(current)},
                                cwd=self._corpus_root(), timeout=300, log_path=log_path)
                            if ck.get("ok"):
                                checks.append({
                                    "file": Path(str(raw)).name,
                                    "music_db": ck.get("background_db"),
                                    "has_music": bool(ck.get("has_music")),
                                    "used": "separated" if ck.get("has_music") else "original",
                                })
                                try:
                                    self.logger.info(
                                        "伴奏轨 %s dB，%s：%s", ck.get("background_db"),
                                        "用分离后的人声" if ck.get("has_music") else "保留原声",
                                        Path(str(raw)).name)
                                except Exception:  # noqa: BLE001
                                    pass
                            if ck.get("ok") and not ck.get("has_music"):
                                current = Path(raw)
                                # 这次分离是白跑的：产物直接清掉，别占着磁盘
                                try:
                                    self._trash_tree(stems_dir)
                                except Exception as exc:  # noqa: BLE001
                                    try:
                                        self.logger.warning("清理分离产物失败：%s", exc)
                                    except Exception:  # noqa: BLE001
                                        pass
                    except Exception as exc:  # noqa: BLE001
                        try:
                            self.logger.warning("伴奏轨核对失败，按分离结果继续：%s", exc)
                        except Exception:  # noqa: BLE001
                            pass
                # 纯音乐检查：整首都没人声的话，分离出来的人声轨就是残渣，
                # 切出来的片段只会教坏模型——直接跳过这首。
                if slot == "song" and chain and current != Path(raw):
                    try:
                        vworker = self._train_worker("song_check_worker.py")
                        synth_py = Path(str(self._pipeline.paths.get("synth_python") or ""))
                        if vworker.is_file() and synth_py.is_file():
                            vc = runtime.run_worker(
                                synth_py, vworker,
                                {"input": str(raw), "vocals": str(current)},
                                cwd=self._corpus_root(), timeout=900, log_path=log_path)
                            if vc.get("ok") and vc.get("has_vocals") is False:
                                self.logger.info(
                                    "纯音乐（人声轨 %.1f dB），跳过：%s",
                                    float((vc.get("detail") or {}).get("vocal_med_db") or 0.0),
                                    Path(str(raw)).name)
                                self._trash_tree(stems_dir)
                                rejected.append({
                                    "id": f"{source_id}_instr{idx:02d}", "slot": slot,
                                    "src": source_id, "path": "", "start_s": 0.0,
                                    "end_s": round(dur_s, 3), "dur_s": round(dur_s, 3),
                                    "status": "rejected", "reason": "纯音乐·没有人声",
                                    "peak": None, "silence_ratio": None,
                                    "f0_hz_median": None, "f0_hz_min": None,
                                    "f0_hz_max": None, "voiced_ratio": None})
                                continue
                    except Exception as exc:  # noqa: BLE001
                        try:
                            self.logger.warning("人声检查失败，按分离结果继续：%s", exc)
                        except Exception:  # noqa: BLE001
                            pass
                # slice + QC the final dry stem
                clips_dir = out_root / "clips"
                clips_dir.mkdir(parents=True, exist_ok=True)
                self._job_update(job_id, phase="slicing",
                                 pct=int(80 + 10 * idx / max(len(paths), 1)),
                                 stage_text=f"切片 {idx + 1}/{len(paths)}")
                job = {"input": str(current), "out_dir": str(clips_dir),
                       "source_id": source_id, "slot": slot}
                try:
                    res = runtime.run_worker(sep_py, corpus_py, job,
                                             cwd=self._corpus_root(), timeout=1800,
                                             log_path=log_path)
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"切分失败：{type(exc).__name__}: {exc}") from exc
                if not res.get("ok"):
                    raise RuntimeError(res.get("error") or "切分后端未产出片段")
                for c in res.get("clips", []):
                    all_clips.append(c)
                    valid_s += float(c.get("dur_s", 0))
                rejected.extend(res.get("rejected", []))
            self._job_update(job_id, phase="checking", pct=95, stage_text="质检完成")
            _instr = any("纯音乐" in str(r.get("reason") or "") for r in rejected)
            self._corpus_update_source(
                source_id, clips=all_clips, valid_s=round(valid_s, 3), status="done",
                checks=checks,
                error=("纯音乐，没有可切的人声" if _instr and not all_clips else ""))
            result = {"phase": "done", "pct": 100, "stage_text": "完成", "error": "",
                      "clips": all_clips, "rejected": rejected}
        except Exception as exc:  # noqa: BLE001
            result = {"phase": "error", "pct": 100, "error": f"{type(exc).__name__}: {exc}"}
            try:
                self._corpus_update_source(source_id, status="error", error=result["error"])
            except Exception:  # noqa: BLE001
                pass
        finally:
            if str(getattr(self, "_corpus_active_source", "")) == str(source_id):
                self._corpus_active_source = ""
            self._job_update(job_id, phase=result["phase"], pct=result["pct"],
                             stage_text=result.get("stage_text", ""),
                             error=result.get("error", ""),
                             clips=result.get("clips", []),
                             state="done" if result["phase"] == "done" else "failed",
                             detail=result.get("error") or result.get("stage_text") or "")

    @plugin_entry(id="ms_corpus_add", name="为她而唱 · 投递训练素材",
                  description="把音频投进语料库（song 全链分离 / dry_sing 检测并分离伴奏 / speech 去噪）→ 自动切片 → 质检；返回 job_id",
                  input_schema={"type": "object", "properties": {
                      "slot": {"type": "string", "enum": ["song", "dry_sing", "speech"]},
                      "path": {"type": "string"},
                      "data_b64": {"type": "string"},
                      "filename": {"type": "string"}},
                      "required": ["slot"]})
    async def ms_corpus_add(self, slot: str = "song", path: str = "",
                            data_b64: str = "", filename: str = "input.wav", **_):
        slot = str(slot or "song").strip()
        if slot not in ("song", "dry_sing", "speech"):
            return Ok({"ok": False, "error": f"未知槽位 {slot!r}（song/dry_sing/speech）"})
        raw_paths: list = []
        if path:
            raw_paths.append(path)
        if data_b64:
            try:
                raw = base64.b64decode(data_b64.split(",", 1)[-1], validate=True)
            except (binascii.Error, ValueError) as exc:
                return Ok({"ok": False, "error": f"base64 解码失败：{exc}"})
            if not raw:
                return Ok({"ok": False, "error": "data_b64 解码后为空"})
            if len(raw) > 200 * 1024 * 1024:
                return Ok({"ok": False, "error": "素材过大（上限 200MB）"})
            suffix = Path(filename or "input.wav").suffix or ".wav"
            staging = self._corpus_root() / slot / "_uploads"
            staging.mkdir(parents=True, exist_ok=True)
            target = staging / f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}{suffix}"
            target.write_bytes(raw)
            raw_paths.append(str(target))
        if not raw_paths:
            return Ok({"ok": False, "error": "需要 path 或 data_b64"})
        return await asyncio.to_thread(self._corpus_add_sources, slot, raw_paths)

    # ---- 语料上传 / 排队 --------------------------------------------------
    def _corpus_add_sources(self, slot: str, raw_paths: list):
        """把已经落在磁盘上的文件登记成一条投递，交给单线程队列慢慢处理。"""
        missing = [p for p in raw_paths if not Path(p).is_file()]
        if missing:
            return Ok({"ok": False, "error": "文件不存在：" + "、".join(missing[:3])})
        # 去重：同一个槽里已经有的素材不再收。
        #   主判据：同名 + 同大小；兜底：同大小 + 同时长（老素材的文件名是上传临时名，
        #   用户重新选原文件时名字对不上，但大小和时长能对上）。
        existing = set()
        existing_meta = set()
        try:
            for src in self._corpus_index().get("sources", []):
                if str(src.get("slot") or "") != slot:
                    continue
                for f in src.get("files", []):
                    if not isinstance(f, dict):
                        continue
                    size = int(f.get("bytes") or 0)
                    existing.add((str(f.get("name") or "").lower(), size))
                    dur = float(f.get("dur_s") or 0)
                    if size > 0 and dur > 0:
                        existing_meta.add((size, round(dur, 1)))
        except Exception:  # noqa: BLE001
            pass
        fresh, dups = [], []
        for p in raw_paths:
            pth = Path(p)
            try:
                size = pth.stat().st_size
            except OSError:
                continue
            dur = self._probe_duration(pth)
            key = (pth.name.lower(), int(size))
            meta = (int(size), round(dur, 1)) if size > 0 and dur > 0 else None
            if key in existing or (meta and meta in existing_meta):
                dups.append(pth.name)
                continue
            existing.add(key)
            if meta:
                existing_meta.add(meta)
            fresh.append(str(pth))
        if not fresh:
            return Ok({"ok": False, "duplicate": True,
                       "error": "这个素材已经加过了：" + "、".join(dups[:3])})
        raw_paths = fresh
        source_id = self._new_source_id()
        files = self._corpus_add_files(slot, raw_paths)
        if not files:
            return Ok({"ok": False, "error": "素材复制失败"})
        # 先登记再排队：插件中途重启也不会丢——启动时会按 status=processing 自动续跑
        job_id = self._job_begin(f"corpus_{slot}")
        with self._corpus_lock:
            index = self._corpus_index()
            index.setdefault("sources", []).append({
                "id": source_id, "slot": slot, "files": files, "clips": [], "valid_s": 0.0,
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "queued", "error": "", "job_id": job_id,
            })
            self._corpus_write_index(index)
        ahead = self._corpus_enqueue(job_id, slot, source_id, files)
        self._job_update(job_id, phase="queued", pct=0,
                         stage_text=("排队中，前面还有 %d 个" % ahead) if ahead else "排队中",
                         slot=slot, source_id=source_id)
        return Ok({"ok": True, "job_id": job_id, "slot": slot, "source_id": source_id,
                   "files": files, "queued": ahead})

    def _corpus_register_existing(self, slot: str, paths: list) -> dict:
        """把已经在语料库目录里的音频登记成待处理素材（不再复制文件）。

        重复的直接跳过：同「文件名 + 大小」或同「大小 + 时长」都算同一份。
        返回 {"added": n, "skipped": n}。
        """
        added = 0
        skipped = 0
        seen_meta = set()
        seen_names = set()
        try:
            for src in self._corpus_index().get("sources", []):
                for f in src.get("files", []):
                    if not isinstance(f, dict):
                        continue
                    if f.get("bytes") and f.get("dur_s"):
                        seen_meta.add((int(f["bytes"]), round(float(f["dur_s"]), 1)))
                    if f.get("name") and f.get("bytes"):
                        seen_names.add((str(f["name"]).lower(), int(f["bytes"])))
        except Exception:  # noqa: BLE001
            pass
        for p in paths:
            pth = Path(p)
            try:
                size = pth.stat().st_size
            except OSError:
                continue
            dur = self._probe_duration(pth)
            key = (str(pth.name).lower(), int(size))
            meta = (int(size), round(dur, 1)) if size > 0 and dur > 0 else None
            if key in seen_names or (meta and meta in seen_meta):
                # 重复副本：内容已经在库里了，把这份多余的文件移到回收站，免得目录里越堆越多
                skipped += 1
                try:
                    self._trash_move(pth)
                except Exception:  # noqa: BLE001
                    pass
                continue
            seen_names.add(key)
            if meta:
                seen_meta.add(meta)
            files = [{"path": str(pth), "name": pth.name, "bytes": size, "dur_s": dur}]
            with self._corpus_lock:
                index = self._corpus_index()
                source_id = self._new_source_id()
                if any(str(s.get("id")) == source_id for s in index.get("sources", [])):
                    source_id = source_id + "_" + uuid.uuid4().hex[:4]
                job_id = self._job_begin(f"corpus_{slot}")
                index.setdefault("sources", []).append({
                    "id": source_id, "slot": slot, "files": files, "clips": [], "valid_s": 0.0,
                    "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "queued", "error": "", "recovered": True, "job_id": job_id,
                })
                self._corpus_write_index(index)
            self._orphans_cache = None
            self._corpus_enqueue(job_id, slot, source_id, files)
            added += 1
        return {"added": added, "skipped": skipped}

    def _corpus_enqueue(self, job_id: str, slot: str, source_id: str, files: list) -> int:
        with self._corpus_queue_lock:
            self._corpus_queue.append((job_id, slot, source_id, files))
            ahead = len(self._corpus_queue) - 1
            start = not self._corpus_worker_on
            if start:
                self._corpus_worker_on = True
        if start:
            self._thread("corpus-queue", self._corpus_worker)
        return max(0, ahead)

    def _corpus_worker(self) -> None:
        """单线程消费：一次只解码一个文件，处理完丢掉引用再取下一个。"""
        while True:
            with self._corpus_queue_lock:
                if not self._corpus_queue:
                    self._corpus_worker_on = False
                    return
                job_id, slot, source_id, files = self._corpus_queue.pop(0)
            try:
                self._corpus_pipeline(job_id, slot, source_id, files)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("语料任务失败 %s：%s", job_id, exc)
                self._job_update(job_id, phase="error", pct=100,
                                 error=f"{type(exc).__name__}: {exc}",
                                 stage_text="失败", state="failed")
            finally:
                files = None  # noqa: F841  让这一条的文件引用立刻可以回收

    def _upload_staging(self, slot: str) -> Path:
        staging = self._corpus_root() / slot / "_uploads"
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    @plugin_entry(id="ms_corpus_upload_begin", name="为她而唱 · 开始上传素材",
                  description="开一个分片上传会话，返回 upload_id；后续按 4MB 分片上传，避免一次性把整首歌读进内存",
                  input_schema={"type": "object", "properties": {
                      "slot": {"type": "string", "enum": ["song", "dry_sing", "speech"]},
                      "filename": {"type": "string"}},
                      "required": ["slot"]})
    async def ms_corpus_upload_begin(self, slot: str = "song", filename: str = "input.wav", **_):
        slot = str(slot or "song").strip()
        if slot not in ("song", "dry_sing", "speech"):
            return Ok({"ok": False, "error": f"未知槽位 {slot!r}（song/dry_sing/speech）"})
        uid = uuid.uuid4().hex
        suffix = Path(str(filename or "input.wav")).suffix or ".wav"
        target = self._upload_staging(slot) / f"upload_{uid}{suffix}"
        target.write_bytes(b"")
        with self._uploads_lock:
            self._uploads[uid] = {"path": target, "slot": slot, "name": filename, "size": 0}
        return Ok({"ok": True, "upload_id": uid, "chunk_size": 4 * 1024 * 1024,
                   "slot": slot, "filename": filename})

    @plugin_entry(id="ms_corpus_upload_chunk", name="为她而唱 · 上传素材分片",
                  description="追加一片音频数据到上传会话（append 到磁盘，不在内存里攒整首）",
                  input_schema={"type": "object", "properties": {
                      "upload_id": {"type": "string"},
                      "seq": {"type": "integer"},
                      "data_b64": {"type": "string"}},
                      "required": ["upload_id", "data_b64"]})
    async def ms_corpus_upload_chunk(self, upload_id: str = "", seq: int = 0,
                                     data_b64: str = "", **_):
        with self._uploads_lock:
            rec = self._uploads.get(str(upload_id))
        if not rec:
            return Ok({"ok": False, "error": "上传会话不存在或已结束"})
        try:
            raw = base64.b64decode(str(data_b64).split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError) as exc:
            return Ok({"ok": False, "error": f"分片解码失败：{exc}"})
        if not raw:
            return Ok({"ok": True, "received": rec["size"]})
        with open(rec["path"], "ab") as fh:
            fh.write(raw)
        rec["size"] += len(raw)
        return Ok({"ok": True, "received": rec["size"], "seq": seq})

    @plugin_entry(id="ms_corpus_upload_end", name="为她而唱 · 结束上传素材",
                  description="收尾分片上传：文件已经完整落在磁盘上，登记成一条投递任务",
                  input_schema={"type": "object", "properties": {"upload_id": {"type": "string"}},
                                "required": ["upload_id"]})
    async def ms_corpus_upload_end(self, upload_id: str = "", **_):
        with self._uploads_lock:
            rec = self._uploads.pop(str(upload_id), None)
        if not rec:
            return Ok({"ok": False, "error": "上传会话不存在或已结束"})
        path = rec["path"]
        if not path.is_file() or rec.get("size", 0) <= 0:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return Ok({"ok": False, "error": "上传的文件是空的"})
        # 用用户原本的文件名入库（uplo_<uid>.wav 只适合当临时名）
        nice = re.sub(r'[\\/:*?"<>|]+', "_", str(rec.get("name") or path.name)).strip(" .") or path.name
        if not Path(nice).suffix:
            nice = nice + path.suffix
        dest = path.with_name(nice)
        if dest != path:
            if dest.exists():
                dest = path.with_name(f"{Path(nice).stem}_{uuid.uuid4().hex[:6]}{Path(nice).suffix}")
            try:
                path.rename(dest)
            except OSError:
                dest = path
        return await asyncio.to_thread(self._corpus_add_sources, rec["slot"], [str(dest)])

    @plugin_entry(id="ms_corpus_recover", name="为她而唱 · 恢复未入库素材",
                  description="把语料库目录里还没登记过的音频重新收进来处理（插件中断/重启后恢复用）",
                  input_schema={"type": "object", "properties": {
                      "slot": {"type": "string", "enum": ["song", "dry_sing", "speech"]}}})
    async def ms_corpus_recover(self, slot: str = "", **_):
        # 文件多时要逐个 ffprobe，放到线程里跑，别堵住事件循环
        return await asyncio.to_thread(self._corpus_recover_sync, slot)

    def _corpus_recover_sync(self, slot: str = ""):
        wanted = [slot] if str(slot) in ("song", "dry_sing", "speech") else ["song", "dry_sing", "speech"]
        index = self._corpus_index()
        known = set()
        for src in index.get("sources", []):
            for f in src.get("files", []):
                if isinstance(f, dict) and f.get("path"):
                    try:
                        known.add(str(Path(str(f["path"])).resolve()).lower())
                    except OSError:
                        known.add(str(f["path"]).lower())
        candidates = []  # (mtime, slot, path)
        for name in wanted:
            root = self._corpus_root() / name
            if not root.is_dir():
                continue
            for p in root.iterdir():
                if not p.is_file() or p.suffix.lower() not in (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus"):
                    continue
                try:
                    key = str(p.resolve()).lower()
                except OSError:
                    key = str(p).lower()
                if key in known:
                    continue
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    mt = 0.0
                candidates.append((mt, name, p))
        # 一次全部恢复（按修改时间从新到旧排）；重复的由 _corpus_register_existing 跳过
        candidates.sort(key=lambda item: item[0], reverse=True)
        by_slot: dict[str, list] = {}
        for _mt, name, p in candidates:
            by_slot.setdefault(name, []).append(p)
        added = 0
        skipped = 0
        for name, paths in by_slot.items():
            res = self._corpus_register_existing(name, paths)
            added += int(res.get("added") or 0)
            skipped += int(res.get("skipped") or 0)
        self._orphans_cache = None
        return Ok({"ok": True, "recovered": added, "duplicates": skipped, "remaining": 0})

    @plugin_entry(id="ms_corpus_job", name="为她而唱 · 语料任务进度",
                  description="读取某次投递任务的阶段/进度/错误",
                  input_schema={"type": "object", "properties": {"job_id": {"type": "string"}}})
    async def ms_corpus_job(self, job_id: str = "", **_):
        with self._jobs_lock:
            job = dict(self._jobs.get(job_id) or {})
        if not job:
            return Ok({"ok": False, "error": f"找不到任务 {job_id!r}"})
        return Ok({"ok": True, "job_id": job_id, **job})

    @plugin_entry(id="ms_corpus_list", name="为她而唱 · 语料库",
                  description="列出三个槽位的素材/切片/有效时长与质检结果",
                  input_schema={"type": "object", "properties": {}})
    async def ms_corpus_list(self, **_):
        index = self._corpus_index()
        view = self._corpus_scan(index)
        return Ok({"ok": True, **view})

    @plugin_entry(id="ms_corpus_clip", name="为她而唱 · 语料切片操作",
                  description="reject=剔除切片 / restore=恢复 / delete=删除这条切片 / preview=返回 base64 试听",
                  input_schema={"type": "object", "properties": {
                      "clip_id": {"type": "string"},
                      "action": {"type": "string", "enum": ["reject", "restore", "delete", "preview"]}},
                      "required": ["clip_id", "action"]})
    async def ms_corpus_clip(self, clip_id: str = "", action: str = "", **_):
        action = str(action or "").strip().lower()
        if action not in ("reject", "restore", "delete", "preview"):
            return Ok({"ok": False, "error": f"未知操作 {action!r}"})
        index = self._corpus_index()
        clip = None
        source = None
        for src in index.get("sources", []):
            for c in src.get("clips", []):
                if c.get("id") == clip_id:
                    clip, source = c, src
                    break
            if clip is not None:
                break
        if clip is None:
            return Ok({"ok": False, "error": f"找不到切片 {clip_id!r}"})
        if action == "preview":
            path = Path(str(clip.get("path") or ""))
            if not path.is_file():
                return Ok({"ok": False, "error": f"切片文件不存在：{path}"})
            try:
                payload = await asyncio.to_thread(self._audio_payload, path)
            except OSError as exc:
                return Ok({"ok": False, "error": f"读取失败：{exc}"})
            return Ok({"ok": True, "clip_id": clip_id, **payload})
        if action == "delete":
            # 真删除：切片文件移到回收站（可找回），条目从 index 里摘掉；
            # 面板上的「可用时长」由 _corpus_scan 按剩下的 ok 片段重算，不需要额外维护。
            path = Path(str(clip.get("path") or ""))
            moved = False
            if path.is_file():
                moved = bool(self._trash_move(path).get("moved"))
            source["clips"] = [c for c in source.get("clips", []) if c.get("id") != clip_id]
            self._corpus_write_index(index)
            return Ok({"ok": True, "clip_id": clip_id, "deleted": True, "trashed": moved})
        # reject / restore: flip status in index (file stays on disk).
        new_status = "rejected" if action == "reject" else "ok"
        if action == "reject":
            clip["reason"] = clip.get("reason") or "手动剔除"
        else:
            clip["reason"] = ""
        clip["status"] = new_status
        self._corpus_write_index(index)
        return Ok({"ok": True, "clip_id": clip_id, "status": new_status})

    @plugin_entry(id="ms_corpus_reprocess", name="为她而唱 · 重新处理素材",
                  description="按当前流程把已有素材重跑一遍（先分离、按伴奏轨电平决定用分离人声还是原声）；"
                              "不传参数就重跑全部清唱素材",
                  input_schema={"type": "object", "properties": {
                      "source_id": {"type": "string"},
                      "slot": {"type": "string"}},
                      "required": []})
    async def ms_corpus_reprocess(self, source_id: str = "", slot: str = "", **_):
        """重跑已有素材，不用重新上传。

        之前上传的清唱都被送进过去混响/去噪模型（频谱上挖掉 6~11 kHz，听感发糊），
        原始文件还在库里，直接按新流程重切一遍就能恢复清晰度。重复上传会被去重拦住，
        所以修完历史素材走这个入口。
        """
        index = self._corpus_index()
        targets = []
        for src in index.get("sources", []):
            sid = str(src.get("id") or "")
            src_slot = str(src.get("slot") or "")
            if source_id and sid != source_id:
                continue
            if not source_id and slot and src_slot != slot:
                continue
            if not source_id and not slot and src_slot != "dry_sing":
                continue
            if str(src.get("status")) == "processing":
                continue
            files = [f for f in (src.get("files") or [])
                     if isinstance(f, dict) and f.get("path") and Path(str(f["path"])).is_file()]
            if not files:
                continue
            targets.append((src, files))
        if not targets:
            return Ok({"ok": False, "error": "没有可重跑的素材（原始文件已经不在了，或者正在处理）"})
        queued = []
        for src, files in targets:
            # 旧切片先清掉：新流程切出的段数可能不同，留着会在磁盘上变成孤儿文件
            for clip in src.get("clips") or []:
                path = Path(str((clip or {}).get("path") or ""))
                try:
                    if path.is_file():
                        self._trash_move(path)
                except Exception:  # noqa: BLE001
                    pass
            # 旧的模型输出（去混响/去噪/分离的中间产物）一起清掉：那是上一版流程
            # 留下的、已经被确认会把声音弄糊的波形，留着既占地又容易看混。
            self._trash_tree(self._corpus_root() / str(src.get("slot") or "dry_sing")
                             / str(src.get("id") or "") / "stems")
            job_id = self._job_begin(f"corpus_{src.get('slot') or 'dry_sing'}")
            src["clips"] = []
            src["valid_s"] = 0.0
            src["status"] = "queued"
            src["error"] = ""
            src["job_id"] = job_id
            queued.append((job_id, str(src.get("slot") or ""), str(src.get("id") or ""), files))
        self._corpus_write_index(index)
        ahead = 0
        for job_id, src_slot, sid, files in queued:
            ahead = self._corpus_enqueue(job_id, src_slot, sid, files)
            self._job_update(job_id, phase="queued", pct=0,
                             stage_text=("排队中，前面还有 %d 个" % ahead) if ahead else "排队中",
                             slot=src_slot, source_id=sid)
        return Ok({"ok": True, "queued": len(queued),
                   "source_ids": [q[2] for q in queued],
                   "job_id": queued[0][0]})

    def _corpus_find_source(self, index: dict, source_id: str):
        for src in index.get("sources", []):
            if src.get("id") == source_id:
                return src
        return None

    def _preview_key(self, path: Path) -> str:
        """试听缓存的键：路径 + 大小 + mtime；同盘用硬链接，不复制文件。"""
        import hashlib
        st = path.stat()
        return hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")).hexdigest()[:16]

    def _audio_payload(self, path: Path) -> dict:
        """试听统一出口：小文件走 base64，大文件走静态 URL。

        宿主的 RPC 结果上限约 4.8MB，整首歌（5~10MB）转 base64 一定超，
        所以大文件硬链接到 plugin/static/_preview，用 /plugin/<id>/ui/_preview/... 直接流式播放。
        """
        size = path.stat().st_size
        suffix = path.suffix.lower()
        mime = "audio/wav" if suffix == ".wav" else "audio/flac" if suffix == ".flac" else "audio/mpeg"
        if size <= 3 * 1024 * 1024:
            return {"mime": mime, "bytes": size,
                    "data_b64": base64.b64encode(path.read_bytes()).decode("ascii")}
        cache = self._preview_dir()
        key = self._preview_key(path)
        dest = cache / (key + (suffix or ".bin"))
        if not dest.is_file() or dest.stat().st_size != size:
            try:
                if dest.exists():
                    dest.unlink()
                os.link(path, dest)          # 同盘硬链接：不复制、不占额外空间
            except OSError:
                shutil.copy2(path, dest)     # 跨盘退化成复制
        return {"mime": mime, "bytes": size,
                "url": f"/plugin/{PLUGIN_ID}/ui/_preview/{dest.name}"}

    def _preview_dir(self) -> Path:
        cache = self._plugin_dir / "static" / "_preview"
        cache.mkdir(parents=True, exist_ok=True)
        return cache

    def _corpus_remove_source(self, source_id: str):
        return self._corpus_remove_sources([str(source_id or "")])

    def _corpus_remove_sources(self, source_ids=None, slot: str = "",
                               all_in_slot: bool = False):
        """批量删除素材源：索引只读写一次，文件搬移/哈希在锁外做。

        单条删除也走这里；正在处理的那一条会跳过并单独提示。
        """
        wanted = {str(x) for x in (source_ids or []) if str(x)}
        with self._corpus_lock:
            index = self._corpus_index()
            targets, skipped_active = [], []
            for src in index.get("sources", []):
                sid = str(src.get("id") or "")
                if not sid:
                    continue
                if all_in_slot:
                    if slot and str(src.get("slot") or "") != slot:
                        continue
                elif sid not in wanted:
                    continue
                if sid == str(getattr(self, "_corpus_active_source", "")):
                    skipped_active.append(sid)
                    continue
                targets.append(dict(src))
        if not targets:
            return Ok({"ok": not skipped_active, "removed": [], "moved": 0,
                       "skipped_active": skipped_active,
                       "errors": [], "error": ("正在处理，稍后再删" if skipped_active else "没有可删除的素材")})
        # 从排队里撤掉（不占索引锁）
        ids = {str(t.get("id")) for t in targets}
        try:
            with self._corpus_queue_lock:
                self._corpus_queue = [it for it in self._corpus_queue if str(it[2]) not in ids]
        except Exception:  # noqa: BLE001
            pass
        # 批量时跳过大文件哈希，否则删几十个会一直读盘
        hash_files = len(targets) <= 3
        moved_total = 0
        removed, errors = [], []
        for src in targets:
            sid = str(src.get("id") or "")
            src_moved, src_errors = 0, []
            src_seen = 0
            preview_keys = []
            for f in src.get("files", []):
                p = Path(str(f.get("path") or ""))
                if not p.is_file():
                    continue
                src_seen += 1
                try:
                    preview_keys.append(self._preview_key(p))
                except OSError:
                    pass
                r = self._trash_move(p, hash_file=hash_files)
                if r.get("moved"):
                    src_moved += 1
                else:
                    src_errors.append(r)
            for c in src.get("clips", []):
                cp = Path(str(c.get("path") or ""))
                if not cp.is_file():
                    continue
                src_seen += 1
                r = self._trash_move(cp, hash_file=False)
                if r.get("moved"):
                    src_moved += 1
                else:
                    src_errors.append(r)
            # 试听缓存是硬链接，源文件进回收站后一起清掉
            try:
                cache = self._plugin_dir / "static" / "_preview"
                for key in preview_keys:
                    for old in cache.glob(key + "*"):
                        try:
                            old.unlink()
                        except OSError:
                            pass
            except OSError:
                pass
            if src_moved or src_seen == 0:
                # 有文件搬走，或者文件/切片本来就已经全不在了（失效记录）：
                # 都从索引里摘掉，避免它一直挂在列表里删不掉。
                removed.append(sid)
                moved_total += src_moved
            if src_errors:
                errors.extend(src_errors)
        if removed:
            with self._corpus_lock:
                index = self._corpus_index()
                drop = set(removed)
                index["sources"] = [s for s in index.get("sources", [])
                                    if str(s.get("id")) not in drop]
                self._corpus_write_index(index)
            self._orphans_cache = None
        return Ok({"ok": bool(removed) or not errors,
                   "removed": removed, "moved": moved_total,
                   "skipped_active": skipped_active,
                   "errors": errors[:20],
                   "error": ("文件正在被占用，暂时删不掉" if errors and not removed else "")})

    @plugin_entry(id="ms_corpus_remove", name="为她而唱 · 移除素材源",
                  description="把某个 source_id 的原始音频/切片移入回收站并记录清单",
                  input_schema={"type": "object", "properties": {"source_id": {"type": "string"}}})
    async def ms_corpus_remove(self, source_id: str = "", **_):
        return await asyncio.to_thread(self._corpus_remove_source, str(source_id or ""))

    @plugin_entry(id="ms_corpus_delete_sources", name="为她而唱 · 批量删除素材",
                  description="按 source_ids 批量删除，或传 all=true + slot 清空某一类素材；都移到回收站",
                  input_schema={"type": "object", "properties": {
                      "source_ids": {"type": "array", "items": {"type": "string"}},
                      "slot": {"type": "string", "enum": ["song", "dry_sing", "speech"]},
                      "all": {"type": "boolean"}}})
    async def ms_corpus_delete_sources(self, source_ids=None, slot: str = "", all=False, **_):
        ids = [str(x) for x in source_ids] if isinstance(source_ids, (list, tuple)) else []
        if not ids and not _as_bool(all):
            return Ok({"ok": False, "error": "没有选择要删除的素材"})
        return await asyncio.to_thread(
            self._corpus_remove_sources, ids, str(slot or ""), _as_bool(all))

    @plugin_entry(id="ms_corpus_source", name="为她而唱 · 素材文件操作",
                  description="preview=试听原文件 / hide=隐藏（不计时不训练，可恢复）/ show=取消隐藏 / delete=移到回收站",
                  input_schema={"type": "object", "properties": {
                      "source_id": {"type": "string"},
                      "action": {"type": "string", "enum": ["preview", "hide", "show", "delete"]}},
                      "required": ["source_id", "action"]})
    async def ms_corpus_source(self, source_id: str = "", action: str = "", **_):
        action = str(action or "").strip().lower()
        if action not in ("preview", "hide", "show", "delete"):
            return Ok({"ok": False, "error": f"未知操作 {action!r}"})
        index = self._corpus_index()
        src = self._corpus_find_source(index, str(source_id or ""))
        if src is None:
            return Ok({"ok": False, "error": f"找不到素材源 {source_id!r}"})
        if action == "delete":
            # 搬文件 + 算哈希比较慢，放到线程里，别堵住插件的事件循环（连点删除会卡死）
            return await asyncio.to_thread(self._corpus_remove_source, str(source_id))
        if action in ("hide", "show"):
            # 和队列的状态更新共用同一把锁：否则后台刚写完索引，这里的 hidden
            # 会被下一次 update 覆盖，看起来就是「隐藏了又回来」。
            with self._corpus_lock:
                index = self._corpus_index()
                src = self._corpus_find_source(index, str(source_id or ""))
                if src is None:
                    return Ok({"ok": False, "error": f"找不到素材源 {source_id!r}"})
                src["hidden"] = (action == "hide")
                self._corpus_write_index(index)
                hidden = src["hidden"]
            return Ok({"ok": True, "source_id": source_id, "hidden": hidden})
        # preview：小文件 base64，大文件给 URL（见 _audio_payload）
        files = [f for f in (src.get("files") or []) if isinstance(f, dict) and f.get("path")]
        if not files:
            return Ok({"ok": False, "error": "这个素材没有可试听的原文件"})
        path = Path(str(files[0]["path"]))
        if not path.is_file():
            return Ok({"ok": False, "error": f"原文件不在了：{path}"})
        try:
            payload = await asyncio.to_thread(self._audio_payload, path)
            return Ok({"ok": True, "source_id": source_id, **payload})
        except Exception as exc:  # noqa: BLE001
            return Ok({"ok": False, "error": f"生成试听失败：{type(exc).__name__}: {exc}"})

    @plugin_entry(id="ms_set_active_model", name="为她而唱 · 切换当前音色模型",
                  description="把某个已训练好的 model_*.pt 设为当前变声模型（单独按钮，用户点才切换）",
                  input_schema={"type": "object", "properties": {"ckpt": {"type": "string"}}})
    async def ms_set_active_model(self, ckpt: str = "", **_):
        ckpt = str(ckpt or "").strip()
        if not ckpt:
            return Ok({"ok": False, "error": "需要 ckpt 路径"})
        target = Path(ckpt)
        if not target.is_file():
            return Ok({"ok": False, "error": f"模型文件不存在：{target}"})
        if runtime.is_base_model(target, self._pipeline.paths.get("ddsp_root"),
                                 self._pipeline.paths.get("ddsp_pretrain_reflow")):
            return Ok({"ok": False, "error": "这是训练用的初始模型，不能当普通声音选"})
        if not (target.parent / "config.yaml").is_file():
            return Ok({"ok": False, "error": f"模型同目录缺少 config.yaml：{target.parent / 'config.yaml'}"})
        runtime.save_config(self._data_dir, {"ddsp_ckpt": str(target)})
        self._reload_paths()
        return Ok({"ok": True, "ddsp_ckpt": str(self._pipeline.paths.get("ddsp_ckpt") or "")})

    # ------------------------------------------------------------------
    # 声音包导入 / 导出（2026-11-05）
    # 导出：训练目录里的 model_*.pt + config.yaml 打成一个 zip。
    # 导入：zip 解到 <ddsp_root>/exp/<名字>/，模型列表下次刷新即可看到。
    # ------------------------------------------------------------------
    def _exports_dir(self) -> Path:
        root = Path(str(self._pipeline.paths.get("compute_root") or self._data_dir))
        out = root / "exports"
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _model_import_staging(self) -> Path:
        root = Path(str(self._pipeline.paths.get("compute_root") or self._data_dir))
        out = root / "work" / "model_imports"
        out.mkdir(parents=True, exist_ok=True)
        return out

    @plugin_entry(id="ms_export_model", name="为她而唱 · 导出声音",
                  description="把训练好的声音打包成 zip（模型 + config.yaml），方便备份、换机或分享",
                  input_schema={"type": "object", "properties": {
                      "path": {"type": "string", "description": "model_*.pt 路径"}},
                      "required": ["path"]})
    async def ms_export_model(self, path: str = "", **_):
        return Ok(await asyncio.to_thread(
            model_package.export_model, path,
            self._pipeline.paths.get("ddsp_root"),
            self._pipeline.paths.get("ddsp_pretrain_reflow"),
            self._exports_dir()))

    @plugin_entry(id="ms_open_export_dir", name="为她而唱 · 打开导出文件夹",
                  description="在资源管理器里打开声音包的导出目录",
                  input_schema={"type": "object", "properties": {}})
    async def ms_open_export_dir(self, **_):
        folder = self._exports_dir()
        try:
            os.startfile(str(folder))  # Windows 资源管理器
        except (AttributeError, OSError) as exc:
            return Ok({"ok": False, "error": f"打不开文件夹：{exc}", "dir": str(folder)})
        return Ok({"ok": True, "dir": str(folder)})

    @plugin_entry(id="ms_model_import_begin", name="为她而唱 · 开始导入声音",
                  description="开一个声音包分片上传会话；只接受 .zip（内含 model_*.pt 和 config.yaml）",
                  input_schema={"type": "object", "properties": {
                      "filename": {"type": "string"}}})
    async def ms_model_import_begin(self, filename: str = "voice.zip", **_):
        name = Path(str(filename or "voice.zip")).name
        if Path(name).suffix.lower() != ".zip":
            return Ok({"ok": False,
                       "error": "请上传 .zip 声音包（里面要同时有 model_*.pt 和 config.yaml）"})
        uid = uuid.uuid4().hex
        target = self._model_import_staging() / f"model_{uid}.zip"
        try:
            target.write_bytes(b"")
        except OSError as exc:
            return Ok({"ok": False, "error": f"无法创建临时文件：{exc}"})
        with self._uploads_lock:
            self._model_uploads[uid] = {"path": target, "name": name, "size": 0}
        return Ok({"ok": True, "upload_id": uid, "chunk_size": 4 * 1024 * 1024,
                   "filename": name})

    @plugin_entry(id="ms_model_import_chunk", name="为她而唱 · 导入声音分片",
                  description="追加一片声音包数据到上传会话（直接写磁盘，不在内存里攒整个包）",
                  input_schema={"type": "object", "properties": {
                      "upload_id": {"type": "string"},
                      "seq": {"type": "integer"},
                      "data_b64": {"type": "string"}},
                      "required": ["upload_id", "data_b64"]})
    async def ms_model_import_chunk(self, upload_id: str = "", seq: int = 0,
                                    data_b64: str = "", **_):
        with self._uploads_lock:
            rec = self._model_uploads.get(str(upload_id))
        if not rec:
            return Ok({"ok": False, "error": "上传会话不存在或已结束"})
        try:
            raw = base64.b64decode(str(data_b64).split(",", 1)[-1], validate=True)
        except (binascii.Error, ValueError) as exc:
            return Ok({"ok": False, "error": f"分片解码失败：{exc}"})
        if raw:
            try:
                with open(rec["path"], "ab") as fh:
                    fh.write(raw)
            except OSError as exc:
                return Ok({"ok": False, "error": f"写入失败：{exc}"})
            rec["size"] = int(rec.get("size") or 0) + len(raw)
        return Ok({"ok": True, "received": rec["size"], "seq": seq})

    @plugin_entry(id="ms_model_import_end", name="为她而唱 · 结束导入声音",
                  description="收尾上传并安装声音包；成功后出现在「我的声音」列表里",
                  input_schema={"type": "object", "properties": {"upload_id": {"type": "string"}},
                                "required": ["upload_id"]})
    async def ms_model_import_end(self, upload_id: str = "", **_):
        with self._uploads_lock:
            rec = self._model_uploads.pop(str(upload_id), None)
        if not rec:
            return Ok({"ok": False, "error": "上传会话不存在或已结束"})
        src = Path(rec["path"])
        try:
            if not src.is_file() or int(rec.get("size") or 0) <= 0:
                return Ok({"ok": False, "error": "上传的声音包是空的"})
            return Ok(await asyncio.to_thread(
                model_package.import_model_zip, src,
                str(rec.get("name") or "voice.zip"),
                str(self._pipeline.paths.get("ddsp_root") or "")))
        finally:
            try:
                src.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # ACE-Step 原创生成（text2music）
    # 生成整首歌 → 交给现有「分离 → DDSP 变声 → 混音」链路换成本插件音色。
    # ------------------------------------------------------------------
    def _ace_paths(self) -> dict:
        paths = self._pipeline.paths
        root = Path(str(paths.get("compute_root") or self._data_dir))
        return {
            "root": root / "acestep",
            # ACE 的轻量依赖（diffusers/loguru/accelerate）装在 pylibs；
            # torch/transformers 复用 DDSP 环境，避免再下一份 2.8GB torch。
            "python": Path(str(paths.get("synth_python") or "")),
            "pylibs": root / "acestep" / "pylibs",
            "checkpoints": root / "acestep" / "checkpoints",
            "output": Path(self._pipeline.work_dir) / "ace",
        }

    ACE_MODEL_DEFAULT = "acestep-v15-turbo"
    # XL 是 4B DiT；turbo 8 步、sft/base 50 步。面板按显存给推荐项。
    ACE_MODEL_CHOICES = (
        "acestep-v15-turbo",
        "acestep-v15-turbo-shift1",
        "acestep-v15-turbo-shift3",
        "acestep-v15-turbo-continuous",
        "acestep-v15-xl-turbo",
        "acestep-v15-xl-sft",
        "acestep-v15-xl-base",
        "acestep-v15-sft",
        "acestep-v15-base",
    )
    # 面板「生成模型」用的目录：每个模型能提升什么、需要多少显存、下载多大。
    ACE_MODEL_CATALOG = (
        {"name": "acestep-v15-turbo", "family": "2B", "steps": 8,
         "quality": "很高", "diversity": "中", "vram_min_gb": 6,
         "download_gb": 4.6,
         "gains": ["速度最快（8 步）", "官方质量：Very High"],
         "best_for": "日常快速出歌"},
        {"name": "acestep-v15-turbo-shift1", "family": "2B", "steps": 8,
         "quality": "很高", "diversity": "中", "vram_min_gb": 6,
         "download_gb": 4.6,
         "gains": ["同 turbo 的 2B 权重，蒸馏目标固定为 timestep shift=1",
                   "噪声分配更平，长段落的细节更容易留下来"],
         "best_for": "对比不同噪声调度下的段落密度"},
        {"name": "acestep-v15-turbo-shift3", "family": "2B", "steps": 8,
         "quality": "很高", "diversity": "中", "vram_min_gb": 6,
         "download_gb": 4.6,
         "gains": ["同 turbo 的 2B 权重，蒸馏目标固定为 timestep shift=3",
                   "偏标准的高噪→低噪节奏"],
         "best_for": "想要 turbo 标准出歌节奏时对比"},
        {"name": "acestep-v15-turbo-continuous", "family": "2B", "steps": 8,
         "quality": "很高", "diversity": "中", "vram_min_gb": 6,
         "download_gb": 4.6,
         "gains": ["同 turbo 的 2B 权重，蒸馏目标为连续时间 shift",
                   "分段过渡更连贯，适合长曲"],
         "best_for": "长曲、连续编曲过渡"},
        {"name": "acestep-v15-sft", "family": "2B", "steps": 50,
         "quality": "高", "diversity": "中", "vram_min_gb": 8,
         "download_gb": 4.6,
         "gains": ["提示词跟随更准", "支持 CFG 引导"],
         "best_for": "想让风格更贴描述"},
        {"name": "acestep-v15-base", "family": "2B", "steps": 50,
         "quality": "中", "diversity": "高", "vram_min_gb": 8,
         "download_gb": 4.6,
         "gains": ["多样性最高", "支持 extract / lego / complete 编辑"],
         "best_for": "想多试几种风格"},
        {"name": "acestep-v15-xl-turbo", "family": "4B", "steps": 8,
         "quality": "很高", "diversity": "中", "vram_min_gb": 12,
         "download_gb": 18.6,
         "gains": ["4B 参数，伴奏层次和整体音质更高", "仍是 8 步，速度快"],
         "best_for": "想要更高音质的默认选择"},
        {"name": "acestep-v15-xl-sft", "family": "4B", "steps": 50,
         "quality": "最高", "diversity": "中", "vram_min_gb": 16,
         "download_gb": 18.6,
         "gains": ["4B + 50 步 + CFG", "官方最高质量，表情和细节更好"],
         "best_for": "追求最高音质，能等"},
        {"name": "acestep-v15-xl-base", "family": "4B", "steps": 50,
         "quality": "高", "diversity": "高", "vram_min_gb": 16,
         "download_gb": 18.6,
         "gains": ["4B 全能编辑", "extract / lego / complete"],
         "best_for": "要提取、分层或补全"},
    )

    def _ace_catalog(self) -> list:
        models = set(self._ace_models())
        try:
            gpu = (runtime.probe_hardware(force=False) or {}).get("gpu") or {}
            vram = float(gpu.get("total_gib") or 0.0)
        except Exception:  # noqa: BLE001
            vram = 0.0
        rows = []
        for item in self.ACE_MODEL_CATALOG:
            row = dict(item)
            row["downloaded"] = row["name"] in models
            row["vram_ok"] = (not vram) or (vram + 0.5) >= float(row["vram_min_gb"])
            row["recommended"] = (
                (vram >= 24 and row["name"] == "acestep-v15-xl-sft")
                or (12 <= vram < 24 and row["name"] == "acestep-v15-xl-turbo")
                or (0 < vram < 12 and row["name"] == "acestep-v15-turbo"))
            rows.append(row)
        return rows

    @staticmethod
    def _ace_model_ready(model_dir: Path) -> bool:
        """A DiT checkpoint is usable when config + every indexed shard exist."""
        try:
            d = Path(model_dir)
            if not (d / "config.json").is_file():
                return False
            single = d / "model.safetensors"
            if single.is_file():
                return single.stat().st_size > 0
            idx = d / "model.safetensors.index.json"
            if not idx.is_file():
                return False
            data = json.loads(idx.read_text(encoding="utf-8"))
            shards = set((data.get("weight_map") or {}).values())
            return bool(shards) and all(
                (d / s).is_file() and (d / s).stat().st_size > 0 for s in shards)
        except (OSError, ValueError):
            return False

    def _ace_models(self) -> list:
        """Downloaded DiT checkpoints under <compute_root>/acestep/checkpoints."""
        base = self._ace_paths()["checkpoints"]
        rows = []
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if (d.is_dir() and d.name.startswith("acestep-v15-")
                        and self._ace_model_ready(d)):
                    rows.append(d.name)
        return rows

    def _ace_selected_model(self, requested: str = "") -> str:
        """Pick the requested/configured model, falling back to a downloaded one."""
        raw = self._pipeline.paths.get("raw") or {}
        raw_name = str(requested or raw.get("ace_model") or self.ACE_MODEL_DEFAULT).strip()
        name = Path(raw_name).name if raw_name else self.ACE_MODEL_DEFAULT
        base = self._ace_paths()["checkpoints"]
        if self._ace_model_ready(base / name):
            return name
        if self._ace_model_ready(base / self.ACE_MODEL_DEFAULT):
            return self.ACE_MODEL_DEFAULT
        models = self._ace_models()
        return models[0] if models else name

    ACE_LORA_SUFFIXES = ("xl_turbo", "xl_sft", "xl_base", "turbo", "sft", "base")
    ACE_LORA_ORDER = {
        "acestep-v15-turbo": 0, "acestep-v15-sft": 1, "acestep-v15-base": 2,
        "acestep-v15-xl-turbo": 3, "acestep-v15-xl-sft": 4, "acestep-v15-xl-base": 5,
    }

    def _soulx_paths(self) -> dict:
        root = Path(str(self._pipeline.paths.get("compute_root") or self._data_dir))
        raw = self._pipeline.paths.get("raw") or {}
        return {
            "root": root / "soulx",
            "python": root / "envs" / "soulx" / "Scripts" / "python.exe",
            "repo": root / "soulx" / "repo",
            "models": root / "soulx" / "models",
            "preprocess": root / "soulx" / "preprocess",
            "model": root / "soulx" / "models" / "model-svc.pt",
            "config": root / "soulx" / "repo" / "soulxsinger" / "config" / "soulxsinger.yaml",
            "rmvpe": root / "soulx" / "preprocess" / "rmvpe" / "rmvpe.pt",
            "timbre": Path(str(raw.get("timbre_path") or "")),
        }

    def _soulx_problems(self) -> list:
        p = self._soulx_paths()
        problems = []
        for key, label in (("python", "SoulX 运行环境 envs\soulx"),
                           ("model", "SoulX SVC 权重 model-svc.pt"),
                           ("config", "SoulX 配置 soulxsinger.yaml"),
                           ("rmvpe", "SoulX RMVPE 音高模型")):
            if not Path(p[key]).is_file():
                problems.append(label)
        if not Path(p["repo"]).is_dir():
            problems.append("SoulX 代码仓库")
        if not Path(p["timbre"]).is_file():
            problems.append("音色样本 timbre_path")
        return problems

    @plugin_entry(id="ms_soulx_status", name="为她而唱 · SoulX 环境",
                  description="只读：检查 SoulX-Singer SVC 的代码、权重、音高模型和音色样本",
                  input_schema={"type": "object", "properties": {}})
    async def ms_soulx_status(self, **_):
        p = self._soulx_paths()
        problems = self._soulx_problems()
        return Ok({"ok": not problems, "ready": not problems,
                   "problems": problems,
                   "paths": {k: str(v) for k, v in p.items()}})

    @plugin_entry(id="ms_soulx_install", name="为她而唱 · 安装 SoulX-Singer",
                  description="下载 SoulX-SVC 权重、RMVPE 与运行环境（后台；可断点续传）",
                  input_schema={"type": "object", "properties": {}})
    async def ms_soulx_install(self, **_):
        if not self._pipeline.paths.get("configured"):
            return Ok({"ok": False,
                       "error": "还没有选择安装位置，请先到安装向导/设置里选一个目录"})
        p = self._soulx_paths()
        if not self._soulx_problems():
            return Ok({"ok": True, "already": True, "ready": True})
        with self._jobs_lock:
            for jid, job in self._jobs.items():
                if job.get("job") == "soulx-install" and job.get("state") in ("queued", "running"):
                    return Ok({"ok": True, "job_id": jid, "running": True})
        try:
            Path(p["root"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Ok({"ok": False, "error": f"无法创建目录：{exc}"})
        py_launcher = shutil.which("py")
        base_python = (py_launcher + " -3.10") if py_launcher else "py -3.10"
        job_id = self._job_begin("soulx-install")
        status_path = Path(p["root"]) / "install_status.json"
        job = {
            "root": str(p["root"]),
            "pylibs": str(self._ace_paths()["pylibs"]),
            "venv": str(Path(p["python"]).parents[1]),
            "base_python": base_python,
        }

        def run():
            stop = threading.Event()

            def watch():
                last = (-1, "")
                while not stop.wait(2.0):
                    try:
                        st = json.loads(status_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        continue
                    pct = int(st.get("pct") or 0)
                    detail = str(st.get("detail") or "")
                    if (pct, detail) != last:
                        last = (pct, detail)
                        self._job_update(job_id, state="running",
                                         progress=pct, detail=detail)

            watcher = threading.Thread(target=watch, daemon=True)
            watcher.start()
            try:
                result = runtime.run_worker(
                    Path(str(self._pipeline.paths["synth_python"])),
                    self._train_worker("soulx_install_worker.py"),
                    job, cwd=Path(p["root"]), timeout=21600,
                    log_path=Path(p["root"]) / "install_worker.log")
            except Exception as exc:  # noqa: BLE001
                self._job_update(job_id, state="failed", progress=100,
                                 detail=f"{type(exc).__name__}: {exc}")
                return
            finally:
                stop.set()
            if result.get("ok"):
                self._job_update(job_id, state="done", progress=100,
                                 detail="SoulX-Singer 安装完成")
            else:
                self._job_update(job_id, state="failed", progress=100,
                                 detail=str(result.get("error") or "SoulX 安装失败"))

        self._thread(f"ms-soulx-install-{job_id}", run)
        return Ok({"ok": True, "job_id": job_id})

    def _bundled_voice(self, name: str) -> dict | None:
        """插件自带的体验音色：data/bundled_voices.json 里登记的名字 → 试听样曲。

        内置音色不是用户训练的，列表里标「内置」；试听直接放打包好的整首歌，
        不占显存、也不用先生成。没登记或文件不在就当普通音色处理。
        """
        stem = Path(str(name or "").strip()).name
        if not stem:
            return None
        try:
            data = json.loads((self._plugin_dir / "data" / "bundled_voices.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        item = data.get(stem) if isinstance(data, dict) else None
        if not isinstance(item, dict):
            return None
        demo = Path(str(item.get("demo") or ""))
        if not demo.is_absolute():
            demo = self._plugin_dir / demo
        if not demo.is_file():
            return None
        return {"name": stem,
                "label": str(item.get("label") or "内置 · 体验"),
                "title": str(item.get("title") or f"{stem} · 试听"),
                "demo": str(demo)}

    def _ace_loras(self) -> list:
        """Installed ACE voices, grouped by name.

        Multi-model training appends a per-base suffix (梦梦op_turbo /
        梦梦op_xl_turbo).  The panel shows one card per voice; the variant is
        picked by the currently selected generation model.
        """
        base = self._ace_paths()["root"] / "loras"
        groups: dict[str, list] = {}
        if not base.is_dir():
            return []
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            has = any((d / f).is_file() for f in (
                "adapter_config.json", "adapter_model.bin",
                "adapter_model.safetensors"))
            if not has:
                continue
            adapter_base = ""
            try:
                adapter = json.loads(
                    (d / "adapter_config.json").read_text(encoding="utf-8"))
                adapter_base = Path(str(
                    adapter.get("base_model_name_or_path") or "")).name
            except (OSError, ValueError):
                adapter_base = ""
            stem = d.name
            for suffix in self.ACE_LORA_SUFFIXES:
                if stem.endswith("_" + suffix):
                    stem = stem[: -len(suffix) - 1]
                    break
            groups.setdefault(stem, []).append({
                "name": d.name, "path": str(d), "base": adapter_base})
        rows = []
        for name, variants in sorted(groups.items()):
            variants.sort(key=lambda v: (
                self.ACE_LORA_ORDER.get(str(v.get("base") or ""), 9),
                str(v.get("name") or "")))
            rows.append({
                "name": name,
                "path": variants[0]["path"],
                "base": variants[0]["base"],
                "bases": [v["base"] for v in variants],
                "variants": variants,
            })
        for row in rows:
            bundled = self._bundled_voice(row["name"])
            if bundled:
                row["bundled"] = True
                row["demo"] = bundled["demo"]
                row["demo_title"] = bundled["title"]
        return rows

    def _ace_lora_group(self, name: str) -> dict | None:
        needle = Path(str(name or "").strip()).name
        if not needle:
            return None
        rows = self._ace_loras()
        for row in rows:
            if row["name"] == needle:
                return row
        # 兼容旧数据/AI 直接传变体目录名
        for row in rows:
            for variant in row["variants"]:
                if variant["name"] == needle:
                    return row
        return None

    @staticmethod
    def _ace_lora_variant(group: dict | None, model: str) -> dict | None:
        if not group:
            return None
        for variant in group.get("variants") or []:
            if variant.get("base") == model:
                return variant
        return None

    def _ace_train_stats(self) -> dict:
        """素材切片 + 总时长，用于动态推荐训练参数。"""
        try:
            index = self._corpus_index()
        except Exception:  # noqa: BLE001
            index = {}
        buckets = {"dry_sing": [], "song": [], "speech": []}
        seconds = {"dry_sing": 0.0, "song": 0.0, "speech": 0.0}
        for src in index.get("sources", []):
            if src.get("hidden"):
                continue
            slot = str(src.get("slot") or "")
            if slot not in buckets:
                continue
            for c in src.get("clips", []):
                p = Path(str((c or {}).get("path") or ""))
                if not p.is_file():
                    continue
                buckets[slot].append(p)
                try:
                    seconds[slot] += float((c or {}).get("dur_s") or 0.0)
                except (TypeError, ValueError):
                    pass
        clips = buckets["dry_sing"] + buckets["song"]
        total = seconds["dry_sing"] + seconds["song"]
        if len(clips) < 20:
            clips += buckets["speech"]
            total += seconds["speech"]
        return {"clips": clips, "count": len(clips), "seconds": round(total, 1)}

    def _ace_train_recommend(self) -> dict:
        """按素材量 + 当前空闲显存给训练轮数 / LoRA rank 推荐。"""
        stats = self._ace_train_stats()
        minutes = float(stats["seconds"]) / 60.0
        count = int(stats["count"])
        try:
            probe = runtime.probe_hardware(force=False)
            gpu = probe.get("gpu") or {}
            free = float(gpu.get("free_gib") or 0.0)
            ram_free = float(probe.get("ram_available_gib") or 0.0)
        except Exception:  # noqa: BLE001
            free, ram_free = 0.0, 0.0
        # rank：先看显存，再按素材量封顶（素材越少，rank 越保守）
        if free >= 16:
            rank = 64
        elif free >= 12:
            rank = 48
        elif free >= 8:
            rank = 32
        else:
            rank = 16
        if minutes < 5:
            rank = min(rank, 16)
        elif minutes < 15:
            rank = min(rank, 32)
        elif minutes < 40:
            rank = min(rank, 48)
        # 轮数：素材越多，单轮信息量越大，轮数反而可以少
        if minutes < 5:
            epochs = 30
        elif minutes < 15:
            epochs = 20
        elif minutes < 40:
            epochs = 12
        elif minutes < 90:
            epochs = 8
        else:
            epochs = 6
        if count and count < 20:
            epochs = min(40, epochs + 5)
        accum = 8 if free < 10 or ram_free < 12 else 4
        return {
            "epochs": int(max(3, min(60, epochs))),
            "rank": int(max(8, min(64, rank))),
            "alpha": int(max(16, min(128, rank * 2))),
            "batch": 1,
            "gradient_accumulation": int(accum),
            "clips": count,
            "minutes": round(minutes, 1),
            "free_gib": round(free, 1),
            "reason": (f"{count} 段 · 约 {minutes:.0f} 分钟素材，"
                       f"空闲显存 {free:.1f}GB"),
        }

    def _ace_train_clips(self) -> list:
        """ACE 训练素材固定来自「准备素材」语料库里已处理的切片。"""
        return self._ace_train_stats()["clips"]

    def _ace_problems(self, model: str = "") -> list:
        p = self._ace_paths()
        model = model or self._ace_selected_model()
        problems = []
        if not p["python"].is_file():
            problems.append("ACE 运行环境（DDSP 的 Python）")
        if not p["pylibs"].is_dir():
            problems.append("ACE 依赖目录 acestep\\pylibs")
        else:
            for pkg in ("transformers", "diffusers", "loguru", "accelerate",
                        "win32_setctime", "peft", "vector_quantize_pytorch"):
                if not (p["pylibs"] / pkg).exists():
                    problems.append(f"ACE 依赖 {pkg}")
                    break
        if not (self._plugin_dir / "acestep" / "__init__.py").is_file():
            problems.append("ACE-Step 代码 acestep/")
        for key in ("Qwen3-Embedding-0.6B", "vae"):
            if not (p["checkpoints"] / key).is_dir():
                problems.append(f"ACE 模型 {key}")
        if not self._ace_model_ready(p["checkpoints"] / model):
            problems.append(f"ACE 模型 {model}")
        if not self._train_worker("ace_worker.py").is_file():
            problems.append("workers\\ace_worker.py")
        return problems

    @staticmethod
    def _ace_guess_language(lyrics: str) -> str:
        text = str(lyrics or "")
        if re.search(r"[぀-ヿ]", text):
            return "ja"
        if re.search(r"[가-힯]", text):
            return "ko"
        if re.search(r"[一-鿿]", text):
            return "zh"
        if re.search(r"[A-Za-z]", text):
            return "en"
        return "unknown"

    @staticmethod
    def _ace_estimate_duration(lyrics: str, instrumental: bool) -> int:
        if instrumental:
            return 120
        text = str(lyrics or "")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        units = len(re.findall(r"[A-Za-z]+", text)) + len(
            re.findall(r"[一-鿿぀-ヿ가-힯]", text))
        seconds = 12.0 + len(lines) * 4.2 + units * 0.34
        return int(round(max(60.0, min(300.0, seconds)) / 5.0) * 5)

    @plugin_entry(id="ms_ace_status", name="为她而唱 · 原创生成环境",
                  description="只读：检查 ACE 环境、模型与代码是否就绪",
                  input_schema={"type": "object", "properties": {}})
    async def ms_ace_status(self, **_):
        p = self._ace_paths()
        model = self._ace_selected_model()
        problems = self._ace_problems(model)
        try:
            probe = runtime.probe_hardware(force=False) or {}
            gpu = probe.get("gpu") or {}
            vram = float(gpu.get("total_gib") or 0.0)
        except Exception:  # noqa: BLE001
            probe, vram = {}, 0.0
        # 显卡能力：能不能用 CUDA、用哪种精度、能不能量化。面板据此提示。
        # 只作为信息返回，不计入 problems —— 不兼容的卡还能用 CPU 跑。
        mem = vram_policy.snapshot(probe)
        compat = vram_policy.gpu_compat(mem)
        recommended = ("acestep-v15-xl-sft" if vram >= 24
                       else "acestep-v15-xl-turbo" if vram >= 12
                       else self.ACE_MODEL_DEFAULT)
        with self._jobs_lock:
            active_jobs = {
                str(jid): dict(j)
                for jid, j in self._jobs.items()
                if j.get("job") in ("ace", "ace-download")
                and j.get("state") in ("queued", "running")
            }
        return Ok({"ok": not problems, "ready": not problems, "problems": problems,
                   "paths": {k: str(v) for k, v in p.items()},
                   "model": model,
                   "model_default": self.ACE_MODEL_DEFAULT,
                   "model_recommended": recommended,
                   "model_choices": list(self.ACE_MODEL_CHOICES),
                   "model_catalog": self._ace_catalog(),
                   "models": self._ace_models(),
                   "gpu": {"name": compat.get("name"),
                           "compute_cap": compat.get("compute_cap"),
                           "generation": compat.get("generation"),
                           "vram_total_gib": mem.get("gpu_total_gib"),
                           "vram_free_gib": mem.get("gpu_free_gib"),
                           "dtype": compat.get("dtype"),
                           "cuda_ok": bool(compat.get("supported")),
                           "notes": compat.get("notes") or [],
                           "quantization": compat.get("quantization") or []},
                   "jobs": active_jobs,
                   "ace_lora": str((self._pipeline.paths.get("raw") or {})
                                   .get("ace_lora") or ""),
                   "loras": self._ace_loras(),
                   "train_clips": len(self._ace_train_clips()),
                   "train_defaults": self._ace_train_recommend()})

    @plugin_entry(id="ms_ace_download", name="为她而唱 · 下载 ACE 模型",
                  description="下载指定的 ACE-Step DiT 模型到计算目录（后台；XL 约 18.6GB，可断点续传）",
                  input_schema={"type": "object", "properties": {
                      "model": {"type": "string",
                                "description": "例如 acestep-v15-xl-turbo"},
                      "force": {"type": "boolean",
                                "description": "配置不够时仍然安装"}}})
    async def ms_ace_download(self, model: str = "", force=False, **_):
        if not self._pipeline.paths.get("configured"):
            return Ok({"ok": False,
                       "error": "还没有选择安装位置，请先到安装向导/设置里选一个目录"})
        p = self._ace_paths()
        name = Path(str(model or "").strip()).name
        if not name.startswith("acestep-v15-"):
            return Ok({"ok": False,
                       "error": "模型名不对，例如 acestep-v15-xl-turbo"})
        if self._ace_model_ready(p["checkpoints"] / name):
            return Ok({"ok": True, "already": True, "model": name})
        if not p["python"].is_file():
            return Ok({"ok": False, "error": f"找不到 DDSP 环境的 Python：{p['python']}"})
        # 安装前先看她这台电脑：显存/磁盘不够就提示，允许用户执意继续。
        meta = next((m for m in self.ACE_MODEL_CATALOG
                     if m["name"] == name), None)
        warnings = []
        vram_total = 0.0
        disk_free_gb = 0.0
        if meta:
            try:
                gpu = (runtime.probe_hardware(force=True) or {}).get("gpu") or {}
                vram_total = float(gpu.get("total_gib") or 0.0)
            except Exception:  # noqa: BLE001
                vram_total = 0.0
            need_disk = float(meta.get("download_gb") or 0.0) * 1.08
            try:
                disk_free_gb = shutil.disk_usage(str(p["root"])).free / 1024 ** 3
            except OSError:
                disk_free_gb = 0.0
            if vram_total and (vram_total + 0.5) < float(meta["vram_min_gb"]):
                warnings.append(
                    f"这台电脑显存 {vram_total:.1f}GB，低于 {name} 建议的 "
                    f"{meta['vram_min_gb']}GB，生成时可能跑不动或很慢")
            if disk_free_gb and disk_free_gb < need_disk:
                warnings.append(
                    f"安装盘只剩 {disk_free_gb:.1f}GB，这个模型约需 "
                    f"{need_disk:.1f}GB")
            if disk_free_gb and disk_free_gb < 1.5:
                return Ok({"ok": False,
                           "error": f"安装盘空间不足：只剩 {disk_free_gb:.1f}GB",
                           "disk_free_gb": round(disk_free_gb, 1)})
        if warnings and not _as_bool(force):
            return Ok({"ok": False, "need_confirm": True, "model": name,
                       "warnings": warnings, "vram_total_gb": round(vram_total, 1),
                       "vram_min_gb": (meta or {}).get("vram_min_gb"),
                       "disk_free_gb": round(disk_free_gb, 1),
                       "warning": "；".join(warnings)})
        job_id = self._job_begin("ace-download")
        _src = str((self._pipeline.paths.get("raw") or {})
                   .get("download_source") or "").strip().lower()
        # 面板里的 "mirror" 对应上游的 modelscope 源（国内更快）。
        prefer_source = "modelscope" if _src in ("mirror", "modelscope") else (
            "huggingface" if _src == "huggingface" else "")
        job = {
            "plugin_dir": str(self._plugin_dir),
            "pylibs": str(p["pylibs"]),
            "checkpoints": str(p["checkpoints"]),
            "model": name,
            "prefer_source": prefer_source,
        }

        def run():
            self._job_update(job_id, state="running", progress=5,
                             detail=f"正在下载 {name}（XL 约 18.6GB，可断点续传）")
            # snapshot_download 不往外报进度，这里按目标目录的实际体积估算，
            # 面板的进度条才有东西可以走。
            stop_watch = threading.Event()
            target_dir = p["checkpoints"] / name
            expected = float((meta or {}).get("download_gb") or 0.0) * (1024 ** 3)

            def watch_download():
                last = -1
                while not stop_watch.wait(2.0):
                    try:
                        cur = sum(f.stat().st_size for f in target_dir.rglob("*")
                                  if f.is_file())
                    except OSError:
                        cur = 0
                    if expected <= 0:
                        continue
                    pct = max(5, min(95, int(5 + 90 * cur / expected)))
                    if pct == last:
                        continue
                    last = pct
                    self._job_update(
                        job_id, progress=pct,
                        detail=(f"正在下载 {name}：{cur / 1024 ** 3:.1f} / "
                                f"{expected / 1024 ** 3:.1f} GB"))

            watcher = threading.Thread(target=watch_download,
                                       name="ms-ace-download-watch", daemon=True)
            watcher.start()
            try:
                result = runtime.run_worker(
                    p["python"], self._train_worker("ace_download_worker.py"),
                    job, cwd=p["checkpoints"], timeout=21600,
                    log_path=self._pipeline.log_path)
            except Exception as exc:  # noqa: BLE001
                self._job_update(job_id, state="failed", progress=100,
                                 detail=f"{type(exc).__name__}: {exc}")
                return
            finally:
                stop_watch.set()
            if result.get("ok"):
                self._job_update(job_id, state="done", progress=100,
                                 detail=result.get("message") or f"{name} 下载完成")
            else:
                self._job_update(job_id, state="failed", progress=100,
                                 detail=result.get("error") or "下载失败")

        self._thread(f"ms-ace-download-{job_id}", run)
        return Ok({"ok": True, "job_id": job_id, "model": name})

    @plugin_entry(id="ms_ace_delete_model", name="为她而唱 · 删除 ACE 模型",
                  description="把已下载的 ACE 模型移进回收站（不会动当前正在用的模型）",
                  input_schema={"type": "object", "properties": {
                      "model": {"type": "string",
                                "description": "例如 acestep-v15-xl-sft"}}})
    async def ms_ace_delete_model(self, model: str = "", **_):
        p = self._ace_paths()
        name = Path(str(model or "").strip()).name
        if not name.startswith("acestep-v15-"):
            return Ok({"ok": False, "error": "模型名不对，例如 acestep-v15-xl-sft"})
        target = p["checkpoints"] / name
        if not target.is_dir():
            return Ok({"ok": False, "error": f"没有找到模型 {name}"})
        current = self._ace_selected_model("")
        if name == current:
            return Ok({"ok": False,
                       "error": f"{name} 是当前正在用的模型；先切到别的模型再删"})
        with self._jobs_lock:
            busy = any(
                j.get("job") in ("ace", "ace-download")
                and j.get("state") in ("queued", "running")
                for j in self._jobs.values())
        if busy:
            return Ok({"ok": False, "error": "还有生成/下载任务在跑，等它结束再删"})
        trash = p["checkpoints"].parent / "_trash_models"
        try:
            trash.mkdir(parents=True, exist_ok=True)
            dest = trash / f"{name}_{int(time.time())}"
            shutil.move(str(target), str(dest))
        except OSError as exc:
            return Ok({"ok": False, "error": f"删除失败：{exc}"})
        return Ok({"ok": True, "model": name, "trash": str(dest)})

    @plugin_entry(id="ms_ace_generate", name="为她而唱 · 原创歌曲",
                  description="用 ACE-Step text2music 按风格描述+歌词生成整首歌；可选 auto_sing 直接走现有变声链路",
                  input_schema={"type": "object", "properties": {
                      "prompt": {"type": "string", "description": "风格/编曲描述"},
                      "lyrics": {"type": "string", "description": "歌词；留空=纯音乐"},
                      "name": {"type": "string", "description": "歌名，留空自动命名"},
                      "duration": {"type": "integer", "description": "秒；0=按歌词估算"},
                      "bpm": {"type": "integer", "description": "0=自动"},
                      "language": {"type": "string", "enum": ["auto", "zh", "ja", "en", "ko", "unknown"]},
                      "instrumental": {"type": "boolean"},
                      "auto_sing": {"type": "boolean", "description": "生成后直接用当前音色唱（未选 ACE LoRA 时）"},
                      "post_sing": {"type": "boolean", "description": "兼容旧面板：等于 post_engine=ddsp"},
                      "post_engine": {"type": "string", "enum": ["", "ddsp", "soulx", "soulx_ddsp"],
                                      "description": "干声后处理引擎：空=不处理，ddsp=翻唱声音，soulx=SoulX-Singer SVC，soulx_ddsp=SoulX 后接 DDSP（保气音）"},
                      "model": {"type": "string",
                                "description": "DiT 模型名；留空=用设置里的当前模型（turbo 8 步，XL/sft/base 50 步）"},
                      "inference_steps": {"type": "integer", "minimum": 0, "maximum": 60,
                                          "description": "0=按模型自动（turbo 8，sft/base 50）"},
                      "guidance_scale": {"type": "number"},
                      "seed": {"type": "integer"},
                      "candidates": {"type": "integer", "minimum": 1, "maximum": 6,
                                     "description": "一次生成几个候选（1-6）；多候选会自动挑频谱最平衡的一条，其余留在「让她唱歌」里试听"},
                      "quantization": {"type": "string",
                                       "enum": ["auto", "off", "int8_weight_only",
                                                "fp8_weight_only"],
                                       "description": "量化策略：auto=按显存自动（小卡自动 int8），off=不量化，int8=权重量化（显存减半）"},
                      "lora": {"type": "string", "description": "ACE 音色 LoRA 名字；留空=默认音色"}}})
    async def ms_ace_generate(self, prompt: str = "", lyrics: str = "", name: str = "",
                              duration: int = 0, bpm: int = 0, language: str = "auto",
                              instrumental=False, auto_sing=False, post_sing=False,
                              post_engine: str = "",
                              model: str = "", inference_steps: int = 0,
                              guidance_scale: float = 7.0,
                              seed: int = -1, candidates: int = 1,
                              quantization: str = "auto",
                              lora: str = "", **_):
        if not self._pipeline.paths.get("configured"):
            return Ok({"ok": False,
                       "error": "还没有选择安装位置，请先到安装向导/设置里选一个目录"})
        if str(model or "").strip():
            wanted = Path(str(model).strip()).name
            if not self._ace_model_ready(self._ace_paths()["checkpoints"] / wanted):
                return Ok({"ok": False,
                           "error": f"ACE 模型还没下载：{wanted}",
                           "model": wanted, "models": self._ace_models()})
        model_name = self._ace_selected_model(model)
        problems = self._ace_problems(model_name)
        if problems:
            return Ok({"ok": False, "error": "原创生成还没就绪，缺少 " + "、".join(problems),
                       "model": model_name, "models": self._ace_models()})
        prompt = str(prompt or "").strip()
        lyrics = str(lyrics or "").strip()
        if not prompt and not lyrics:
            return Ok({"ok": False, "error": "至少写一句风格描述或歌词"})
        if _as_bool(instrumental):
            lyrics = ""
        clean_name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip())[:40] \
            or ("原创_" + time.strftime("%m%d_%H%M%S"))
        try:
            duration = int(float(duration or 0))
        except (TypeError, ValueError):
            duration = 0
        if duration <= 0:
            duration = self._ace_estimate_duration(lyrics, _as_bool(instrumental))
        duration = max(10, min(600, duration))
        try:
            bpm = int(float(bpm or 0))
        except (TypeError, ValueError):
            bpm = 0
        lang = str(language or "auto").strip().lower()
        if lang in ("", "auto"):
            lang = self._ace_guess_language(lyrics)
        if _as_bool(instrumental):
            lang = "unknown"
        p = self._ace_paths()
        lora_name = str(lora or "").strip()
        if not lora_name:
            # 没显式传音色时，用「我的声音」里设的当前原创音色
            lora_name = str((self._pipeline.paths.get("raw") or {})
                            .get("ace_lora") or "").strip()
        lora_path = ""
        if lora_name:
            group = self._ace_lora_group(lora_name)
            if group:
                variant = self._ace_lora_variant(group, model_name)
                if not variant:
                    return Ok({"ok": False,
                               "error": (f"原创音色「{group['name']}」没有 {model_name} 版本；"
                                         f"它支持：{'、'.join(group.get('bases') or [])}"),
                               "name": group["name"],
                               "bases": group.get("bases") or []})
                lora_name = str(variant["name"])
                lora_path = str(variant["path"])
            else:
                cand = p["root"] / "loras" / lora_name
                if not cand.is_dir():
                    return Ok({"ok": False, "error": f"找不到 ACE 音色：{lora_name}"})
                # 兼容旧调用：直接传的是变体目录名，仍按底模校验
                base_name = ""
                try:
                    adapter = json.loads(
                        (cand / "adapter_config.json").read_text(encoding="utf-8"))
                    base_name = Path(str(
                        adapter.get("base_model_name_or_path") or "")).name
                except (OSError, ValueError):
                    base_name = ""
                if base_name and base_name != model_name:
                    return Ok({"ok": False,
                               "error": (f"ACE 音色「{lora_name}」是在 {base_name} 上训练的，"
                                         f"不能直接用在 {model_name} 上；请换回 {base_name}，"
                                         "或用它重新训练一个音色")})
                lora_path = str(cand)
        # 后处理引擎：空=不处理；ddsp=现有翻唱变声；soulx=SoulX-Singer SVC。
        # 旧面板的 post_sing=true 等价于 ddsp。
        engine = str(post_engine or "").strip().lower()
        if engine not in ("", "ddsp", "soulx", "soulx_ddsp"):
            return Ok({"ok": False, "error": f"未知后处理引擎：{post_engine}"})
        if _as_bool(post_sing) and not engine:
            engine = "ddsp"
        if engine in ("soulx", "soulx_ddsp"):
            sx = self._soulx_problems()
            if sx:
                return Ok({"ok": False,
                           "error": "SoulX 还没就绪，缺少 " + "、".join(sx)})
        # 普通 auto_sing 在选了 ACE 音色时仍然跳过 DDSP 二次变声；
        # 显式 post_engine 则一定后处理。
        sing_after = bool(engine) or (_as_bool(auto_sing) and not lora_path)
        try:
            inference_steps = int(inference_steps or 0)
        except (TypeError, ValueError):
            inference_steps = 0
        if inference_steps <= 0:
            inference_steps = 8 if "turbo" in model_name else 50
        inference_steps = max(1, min(60, inference_steps))
        is_xl = "xl" in model_name
        if is_xl:
            # ACE-Step v0.1.8 的 CPU offload 对 4B XL 会原生崩溃（Windows/CUDA，
            # 关 DCW 也一样）；实测 16GB 卡关掉 offload 后 10s/60s/180s 峰值约
            # 11.3GB，可稳定跑。低于 12GB 的卡不建议选 XL。
            try:
                gpu = (runtime.probe_hardware(force=False) or {}).get("gpu") or {}
                total_vram = float(gpu.get("total_gib") or 0.0)
            except Exception:  # noqa: BLE001
                total_vram = 0.0
            if total_vram and (total_vram + 0.5) < 12.0:
                return Ok({"ok": False,
                           "error": (f"XL 模型至少需要 12GB 显存（当前 {total_vram:.1f}GB）；"
                                     "请改用 2B 的 acestep-v15-turbo"),
                           "model": model_name})
        # 显存兼容策略：装不下时把权重留在内存，按层搬上显存（慢但能跑）。
        # n_candidates 必须先算出来：下面的 ace_plan 要用它估并发显存，
        # 原来写在 ace_plan 之后，任何一次原创生成都会 UnboundLocalError。
        try:
            n_candidates = max(1, min(6, int(candidates or 1)))
        except (TypeError, ValueError):
            n_candidates = 1
        mem = vram_policy.snapshot(runtime.probe_hardware(force=False))
        model_gib = vram_policy.estimate_runtime_gib(p["checkpoints"] / model_name)
        policy = vram_policy.ace_plan(
            mem, model_gib, is_xl=is_xl,
            duration_s=float(duration or 60.0), batch=n_candidates)
        if policy.get("fatal"):
            return Ok({"ok": False, "error": policy["fatal"],
                       "vram": {"mode": policy.get("mode"),
                                "reason": policy.get("reason"),
                                "warnings": policy.get("warnings") or []}})
        # 量化：auto 听策略的；off 关掉；显式指定但算力不支持时退回自动。
        requested_quant = str(quantization or "auto").strip().lower()
        supported_quant = vram_policy.quantization_choices(mem)
        if requested_quant in ("", "auto"):
            chosen_quant = policy.get("quantization")
        elif requested_quant in ("off", "none", "no", "0", "false"):
            chosen_quant = None
        elif requested_quant in supported_quant:
            chosen_quant = requested_quant
        else:
            chosen_quant = policy.get("quantization")
        policy["quantization"] = chosen_quant
        # 小显存下一次只出一个候选：多候选等于把激活显存翻倍，4GB 卡会直接 OOM。
        cap_candidates = int(policy.get("max_candidates") or n_candidates)
        if cap_candidates < n_candidates:
            n_candidates = max(1, cap_candidates)
            policy.setdefault("warnings", []).append(
                f"显存有限，这次只生成 {n_candidates} 个候选")
        try:
            p["output"].mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Ok({"ok": False, "error": f"无法创建生成目录：{exc}"})
        fixed_seed = int(seed or -1)
        if n_candidates > 1 and fixed_seed >= 0:
            # 指定 seed 时连续取 N 个，保证可复现；否则交给 ACE 随机抽。
            cand_seeds = [fixed_seed + i for i in range(n_candidates)]
            cand_random = False
        else:
            cand_seeds = None
            cand_random = fixed_seed < 0
        job_id = self._job_begin("ace")
        job = {
            "plugin_dir": str(self._plugin_dir),
            "project_root": str(p["root"]),
            "pylibs": str(p["pylibs"]),
            "output_dir": str(p["output"]),
            "model_name": model_name,
            "lora_path": lora_path,
            "prompt": prompt,
            "lyrics": lyrics,
            "name": clean_name,
            "vocal_language": lang,
            "duration": duration,
            "bpm": bpm or None,
            "instrumental": _as_bool(instrumental),
            "inference_steps": inference_steps,
            "guidance_scale": float(guidance_scale or 7.0),
            "seed": fixed_seed,
            # 面板填了 seed 就一直用它；-1/留空才随机。旧代码没传这个字段，
            # AceStepGenerateRequest 默认 True，导致 seed 参数完全失效。
            "use_random_seed": cand_random,
            "batch_size": n_candidates,
            "seeds": cand_seeds,
            # 由 vram_policy 决定权重放哪、要不要量化：够就全放显存，不够就
            # int8 量化或逐层搬上显存。
            **vram_policy.flags_for(policy["mode"]),
            "vram_mode": policy["mode"],
            "quantization": chosen_quant or "",
            "vae_on_cpu": bool(policy.get("vae_on_cpu")),
        }

        def run():
            track = None
            self._job_update(job_id, state="queued",
                             detail="排队中：同一时间只跑一个生成/唱的任务")
            self._wait_training_idle(job_id)
            if getattr(self, "_shutdown", False):
                self._job_update(job_id, state="canceled", detail="插件已停止")
                return
            with self._process_lock:
                self._job_update(job_id, state="running", progress=5,
                                 detail="正在加载 ACE-Step 并生成（首次较慢，请耐心等）")
                attempts = vram_policy.ace_attempts(job, policy, is_xl=is_xl)
                result = None
                for index, attempt in enumerate(attempts, 1):
                    try:
                        result = runtime.run_worker(
                            p["python"], self._train_worker("ace_worker.py"),
                            attempt, cwd=p["output"], timeout=3600,
                            log_path=self._pipeline.log_path)
                    except Exception as exc:  # noqa: BLE001
                        result = {"ok": False,
                                  "error": f"{type(exc).__name__}: {exc}"}
                    if (result.get("ok") or index == len(attempts)
                            or not (runtime.is_oom(result)
                                    or runtime.is_quant_failure(result))):
                        break
                    nxt = (attempts[index].get("vram_mode")
                           if index < len(attempts) else "cpu")
                    label = vram_policy.MODE_LABELS.get(nxt, nxt)
                    why = ("量化在这张卡上不可用"
                           if runtime.is_quant_failure(result) else "显存不足")
                    self._job_update(
                        job_id, progress=5,
                        detail=f"{why}，自动改用「{label}」重新尝试…")
                if not result.get("ok") or not result.get("wav"):
                    self._job_update(job_id, state="failed", progress=100,
                                     detail=str(result.get("error") or "ACE 生成失败"))
                    return
                wav = Path(str(result["wav"]))
                if not wav.is_file():
                    self._job_update(job_id, state="failed", progress=100,
                                     detail="ACE 返回的音频文件不存在")
                    return
                try:
                    track = self._pipeline.store.add(
                        title=clean_name, source=str(wav), duration_s=None)
                except Exception as exc:  # noqa: BLE001
                    self._job_update(job_id, state="failed", progress=100,
                                     detail=f"入库失败：{exc}")
                    return
                # 记下来源：插件重载打断「生成并让她唱」后，启动时据此自动续跑。
                if track and track.get("id"):
                    # lyrics / vocal_language 存进曲目：播放时要用它们生成口型轨，
                    # 不能只留在本次任务的 job 里。
                    track = self._pipeline.store.update(
                        str(track["id"]), origin="ace",
                        auto_sing=bool(sing_after), post_engine=engine,
                        ace_job=job_id,
                        lyrics=str(lyrics or ""),
                        vocal_language=str(lang or ""),
                        instrumental=bool(job.get("instrumental")),
                        duration_s=(float(result.get("duration") or 0) or None)) or track
                # 多候选：没被自动选中的也入库，用户可以在「让她唱歌」里试听后再决定。
                for idx, audio_path in enumerate(result.get("audios") or []):
                    if str(audio_path) == str(wav):
                        continue
                    try:
                        extra = self._pipeline.store.add(
                            title=f"{clean_name} · 候选 {idx + 1}",
                            source=str(audio_path), duration_s=None)
                        if extra and extra.get("id"):
                            self._pipeline.store.update(
                                str(extra["id"]), origin="ace", auto_sing=False,
                                post_engine=engine, status="done",
                                step="generate", progress=100, error="",
                                lyrics=str(lyrics or ""),
                                vocal_language=str(lang or ""),
                                instrumental=bool(job.get("instrumental")))
                    except Exception:  # noqa: BLE001 - 候选入库失败不影响主流程
                        pass
                if n_candidates > 1:
                    self._job_update(
                        job_id, detail=(f"{n_candidates} 个候选已生成，自动选第 "
                                        f"{int(result.get('best') or 0) + 1} 个进入后续流程"))
                if not (sing_after and track and track.get("id")):
                    # 「只生成」也是完成状态：面板显示「做好啦」，并可直接试听原曲
                    if track and track.get("id"):
                        self._pipeline.store.update(
                            str(track["id"]), status="done", step="generate",
                            progress=100, error="")
                    self._job_update(job_id, state="done", progress=100,
                                     detail=("生成完成：" + clean_name
                                             + (f"（{n_candidates} 个候选，自动选第 "
                                                f"{int(result.get('best') or 0) + 1} 个）"
                                                if n_candidates > 1 else "")),
                                     track_id=track.get("id"))
                    return
                # 「生成并让她唱」：同一个任务继续跑分离→变声→混音，
                # 只有成品出来才算 100%，不把用户丢回「让她唱歌」再点一次。
                track_id = str(track["id"])
                self._job_update(job_id, state="running", progress=55,
                                 detail="生成完成，正在分离人声…")
                stop_watch = threading.Event()

                def watch_progress():
                    last = -1
                    while not stop_watch.wait(2.5):
                        cur = self._pipeline.store.get(track_id) or {}
                        pct = int(cur.get("progress") or 0)
                        mapped = min(99, 55 + int(pct * 0.44))
                        if mapped != last:
                            last = mapped
                            step = str(cur.get("step") or "")
                            self._job_update(job_id, progress=mapped,
                                             detail="正在用当前音色唱："
                                                    + (step or "处理中"))

                watcher = threading.Thread(target=watch_progress,
                                           name="ms-ace-sing-watch", daemon=True)
                watcher.start()
                try:
                    conv = self._pipeline.process_track(track_id, None)
                except Exception as exc:  # noqa: BLE001
                    conv = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                finally:
                    stop_watch.set()
                if conv.get("ok"):
                    self._job_update(job_id, state="done", progress=100,
                                     detail="全部完成：已用当前音色唱好",
                                     track_id=track_id)
                else:
                    self._job_update(job_id, state="failed", progress=100,
                                     detail=str(conv.get("error") or "变声失败"),
                                     track_id=track_id)

        self._thread(f"ms-ace-{job_id}", run)
        self._job_update(job_id, vram_mode=policy["mode"],
                         vram_reason=vram_policy.describe(policy))
        return Ok({"ok": True, "job_id": job_id, "name": clean_name,
                   "duration": duration, "language": lang,
                   "model": model_name, "inference_steps": inference_steps,
                   "vram": {"mode": policy["mode"], "label": policy["label"],
                            "quantization": chosen_quant,
                            "quantization_label": vram_policy.QUANT_LABELS.get(
                                chosen_quant or "", "不量化"),
                            "reason": policy["reason"],
                            "warnings": policy.get("warnings") or []}})

    @plugin_entry(id="ms_ace_job", name="为她而唱 · 原创生成进度",
                  description="查询一次原创生成任务的状态",
                  input_schema={"type": "object", "properties": {"job_id": {"type": "string"}}})
    async def ms_ace_job(self, job_id: str = "", **_):
        with self._jobs_lock:
            job = dict(self._jobs.get(str(job_id)) or {})
        return Ok({"found": bool(job), "job": job})

    @plugin_entry(id="ms_ace_loras", name="为她而唱 · ACE 音色列表",
                  description="列出已经训练好的 ACE 音色 LoRA",
                  input_schema={"type": "object", "properties": {}})
    async def ms_ace_loras(self, **_):
        return Ok({"ok": True, "loras": self._ace_loras()})

    @plugin_entry(id="ms_ace_set_lora", name="为她而唱 · 设为当前原创音色",
                  description="把某个原创歌曲音色设为默认；生成时按所选模型自动挑对应变体",
                  input_schema={"type": "object", "properties": {
                      "name": {"type": "string"}}})
    async def ms_ace_set_lora(self, name: str = "", **_):
        group = self._ace_lora_group(name)
        if not group:
            return Ok({"ok": False, "error": f"找不到原创音色：{name}"})
        runtime.save_config(self._data_dir, {"ace_lora": group["name"]})
        self._reload_paths()
        return Ok({"ok": True, "name": group["name"],
                   "bases": group.get("bases") or []})

    @plugin_entry(id="ms_ace_delete_lora", name="为她而唱 · 删除原创音色",
                  description="把某个原创歌曲音色的所有底模变体移进回收站",
                  input_schema={"type": "object", "properties": {
                      "name": {"type": "string"},
                      "delete_files": {"type": "boolean"}}})
    async def ms_ace_delete_lora(self, name: str = "", delete_files=True, **_):
        group = self._ace_lora_group(name)
        if not group:
            return Ok({"ok": False, "error": f"找不到原创音色：{name}"})
        if not _as_bool(delete_files):
            return Ok({"ok": False,
                       "error": "原创音色没有单独的记录表，只能连文件一起删除（会移进回收站）"})
        moved = []
        for variant in group.get("variants") or []:
            res = self._trash_move(Path(str(variant.get("path") or "")),
                                   hash_file=False)
            if res.get("ok"):
                moved.append(variant.get("name"))
        if str((self._pipeline.paths.get("raw") or {}).get("ace_lora") or "") \
                == group["name"]:
            runtime.save_config(self._data_dir, {"ace_lora": ""})
            self._reload_paths()
        return Ok({"ok": bool(moved), "name": group["name"], "deleted": moved,
                   "error": "" if moved else "没有可删除的文件"})

    @plugin_entry(id="ms_ace_export_lora", name="为她而唱 · 导出原创音色",
                  description="把某个原创歌曲音色的所有底模变体打包成 zip，放到导出目录",
                  input_schema={"type": "object", "properties": {
                      "name": {"type": "string"}}})
    async def ms_ace_export_lora(self, name: str = "", **_):
        group = self._ace_lora_group(name)
        if not group:
            return Ok({"ok": False, "error": f"找不到原创音色：{name}"})

        def build() -> dict:
            import zipfile
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", group["name"])[:50] or "ace_voice"
            out = self._exports_dir() / f"{safe}.zip"
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                for variant in group.get("variants") or []:
                    folder = Path(str(variant.get("path") or ""))
                    if not folder.is_dir():
                        continue
                    for f in folder.rglob("*"):
                        if f.is_file():
                            zf.write(
                                f,
                                arcname=(f"{group['name']}/{folder.name}/"
                                         f"{f.relative_to(folder).as_posix()}"))
            return {"ok": True, "name": group["name"], "path": str(out),
                    "size_mb": round(out.stat().st_size / 1024 / 1024, 1)}

        return Ok(await asyncio.to_thread(build))

    @plugin_entry(id="ms_ace_preview", name="为她而唱 · 试听原创音色",
                  description="用这个原创音色生成一段 10 秒试听（首次较慢，之后走缓存）",
                  input_schema={"type": "object", "properties": {
                      "name": {"type": "string"},
                      "model": {"type": "string"}}})
    async def ms_ace_preview(self, name: str = "", model: str = "", **_):
        demo = self._bundled_voice(name)
        if demo:
            payload = await asyncio.to_thread(self._audio_payload, Path(demo["demo"]))
            return Ok({"ok": True, "name": demo["name"], "model": "bundled",
                       "file": demo["demo"], "bundled": True, **payload})
        busy = self._training_active()
        if busy:
            return Ok({"ok": False, "busy": busy,
                       "error": "训练正在占用显存，试听稍后再试：两者不能同时占用显存。"})
        group = self._ace_lora_group(name)
        if not group:
            return Ok({"ok": False, "error": f"找不到原创音色：{name}"})
        target_model = str(model or "").strip() or self._ace_selected_model()
        variant = self._ace_lora_variant(group, target_model)
        if not variant:
            variants = group.get("variants") or []
            variant = variants[0] if variants else None
        if not variant:
            return Ok({"ok": False, "error": "这个音色没有可用的 LoRA 文件"})
        p = self._ace_paths()
        if not p["python"].is_file():
            return Ok({"ok": False, "error": f"找不到 ACE 运行环境：{p['python']}"})
        import hashlib
        try:
            mtime = Path(str(variant["path"])).stat().st_mtime
        except OSError:
            mtime = 0
        key = hashlib.sha1(
            f"{variant['path']}|{mtime}|{variant['base']}".encode("utf-8")
        ).hexdigest()[:12]
        out_dir = Path(self._pipeline.work_dir) / "ace"
        out = out_dir / f"preview_{key}.wav"
        if not out.is_file():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return Ok({"ok": False, "error": f"无法创建试听目录：{exc}"})
            is_xl = "xl" in str(variant["base"])
            mem = vram_policy.snapshot(runtime.probe_hardware(force=False))
            model_gib = vram_policy.estimate_runtime_gib(p["checkpoints"] / str(variant["base"]))
            policy = vram_policy.ace_plan(mem, model_gib, is_xl=is_xl,
                                          duration_s=10.0, batch=1)
            job = {
                "plugin_dir": str(self._plugin_dir),
                "project_root": str(p["root"]),
                "pylibs": str(p["pylibs"]),
                "output_dir": str(out_dir),
                "model_name": str(variant["base"]),
                "lora_path": str(variant["path"]),
                "prompt": "warm acoustic pop, piano and strings",
                "lyrics": "la la la la, hold me close tonight",
                "name": "ace_preview",
                "vocal_language": "en",
                "duration": 10,
                "bpm": 0,
                "instrumental": False,
                "inference_steps": 8 if "turbo" in str(variant["base"]) else 50,
                "guidance_scale": 7.0,
                "seed": 12345,
                **vram_policy.flags_for(policy["mode"]),
                "vram_mode": policy["mode"],
                "quantization": str(policy.get("quantization") or ""),
                "vae_on_cpu": bool(policy.get("vae_on_cpu")),
                "dcw_enabled": True,
            }
            if not self._process_lock.acquire(blocking=False):
                return Ok({"ok": False,
                           "error": "正在训练或生成，等它结束再试听"})
            attempts = vram_policy.ace_attempts(job, policy, is_xl=is_xl)
            result = None
            try:
                for index, attempt in enumerate(attempts, 1):
                    result = await asyncio.to_thread(
                        lambda a=attempt: runtime.run_worker(
                            p["python"], self._train_worker("ace_worker.py"),
                            a, cwd=out_dir, timeout=1800,
                            log_path=self._pipeline.log_path))
                    if (result.get("ok") or index == len(attempts)
                            or not (runtime.is_oom(result)
                                    or runtime.is_quant_failure(result))):
                        break
            finally:
                self._process_lock.release()
            if not result.get("ok") or not result.get("wav"):
                return Ok({"ok": False,
                           "error": result.get("error") or "试听生成失败"})
            generated = Path(str(result["wav"]))
            if generated != out:
                try:
                    shutil.move(str(generated), str(out))
                except OSError:
                    out = generated
        payload = await asyncio.to_thread(self._audio_payload, out)
        return Ok({"ok": True, "name": group["name"],
                   "model": str(variant["base"]),
                   "file": str(out), **payload})

    @plugin_entry(id="ms_ace_train", name="为她而唱 · 训练 ACE 音色",
                  description="用「准备素材」里已处理的切片训练 ACE-Step 音色 LoRA；可传入多个模型，依次排队训练",
                  input_schema={"type": "object", "properties": {
                      "name": {"type": "string", "description": "音色名"},
                      "models": {"type": "array", "items": {"type": "string"},
                                 "description": "要训练的底模列表；留空=所有已下载模型依次训练"},
                      "epochs": {"type": "integer", "minimum": 0, "maximum": 100},
                      "rank": {"type": "integer", "minimum": 0, "maximum": 128},
                      "alpha": {"type": "integer", "minimum": 0, "maximum": 256},
                      "batch": {"type": "integer", "minimum": 1, "maximum": 8},
                      "gradient_accumulation": {"type": "integer", "minimum": 1, "maximum": 32},
                      "max_duration": {"type": "number"},
                      "max_clips": {"type": "integer", "minimum": 0, "maximum": 2000,
                                    "description": "调试/快速试训：只用前 N 段素材；0=全部"}}})
    async def ms_ace_train(self, name: str = "", models=None,
                           epochs: int = 0, rank: int = 0, alpha: int = 0,
                           batch: int = 0, gradient_accumulation: int = 0,
                           max_duration: float = 60.0, max_clips: int = 0, **_):
        if not self._pipeline.paths.get("configured"):
            return Ok({"ok": False,
                       "error": "还没有选择安装位置，请先到安装向导/设置里选一个目录"})
        busy = self._singing_active() or self._training_active()
        if busy:
            return Ok({"ok": False, "busy": busy,
                       "error": "正在唱歌或已有训练在占用显存，暂时不能开始训练。"
                                "两者不能同时占用显存和算力。"})
        # 不同底模的 LoRA 不能混用：2B turbo 的 adapter 挂不到 4B XL 上。
        # 这里按“每个已下载模型训一个 LoRA”排队，名字后面带变体后缀，避免互相覆盖。
        aliases = {
            "acestep-v15-turbo": "turbo",
            "acestep-v15-base": "base",
            "acestep-v15-sft": "sft",
            "acestep-v15-xl-turbo": "xl_turbo",
            "acestep-v15-xl-base": "xl_base",
            "acestep-v15-xl-sft": "xl_sft",
        }
        available = [m for m in self._ace_models() if m in aliases]
        if isinstance(models, str):
            requested = [models]
        elif isinstance(models, (list, tuple)):
            requested = [str(m) for m in models if str(m).strip()]
        else:
            requested = []
        requested = [Path(m).name for m in requested]
        if not requested:
            requested = available
        if not requested:
            return Ok({"ok": False,
                       "error": "没有可训练的 ACE 模型，请先下载 acestep-v15-turbo 或 XL 模型"})
        missing = [m for m in requested if m not in available]
        if missing:
            return Ok({"ok": False,
                       "error": "这些模型还没下载完整：" + "、".join(missing),
                       "models": available})
        problems = self._ace_problems(requested[0])
        if problems:
            return Ok({"ok": False, "error": "ACE 环境还没就绪，缺少 " + "、".join(problems)})
        clips = self._ace_train_clips()
        try:
            max_clips = int(max_clips or 0)
        except (TypeError, ValueError):
            max_clips = 0
        if max_clips > 0:
            clips = clips[:max_clips]
        count = len(clips)
        if count < 5:
            return Ok({"ok": False,
                       "error": f"「准备素材」里可用切片只有 {count} 段，先去投递素材，至少 5 段"})
        clean_name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(name or "").strip())[:40] \
            or ("ace_" + time.strftime("%m%d_%H%M%S"))
        p = self._ace_paths()
        rec = self._ace_train_recommend()
        epochs = max(1, min(100, int(epochs or rec["epochs"])))
        rank = max(4, min(128, int(rank or rec["rank"])))
        alpha = max(4, min(256, int(alpha or rec["alpha"])))
        batch = max(1, min(8, int(batch or rec["batch"])))
        accum = max(1, min(32, int(gradient_accumulation or rec["gradient_accumulation"])))
        # 显存偏低时把训练参数降到能跑的下限；再不行由尝试链退回 CPU。
        vram_warnings = []
        try:
            _mem = vram_policy.snapshot(runtime.probe_hardware(force=False))
            _free = float(_mem.get("gpu_free_gib") or 0.0)
            if _mem.get("has_gpu") and _free < 6.0:
                batch = 1
                max_duration = min(float(max_duration or 60.0), 20.0)
                vram_warnings.append(
                    f"显存只剩 {_free:.1f}GB：已自动把 batch 降到 1、单段时长降到 "
                    f"{max_duration:.0f} 秒")
            elif not _mem.get("has_gpu"):
                vram_warnings.append("没有检测到可用显卡：训练会走 CPU，可能非常慢")
        except Exception:  # noqa: BLE001 - 兼容策略不能挡住训练入口
            pass
        job_id = self._job_begin("ace-train")
        job_base = {
            "plugin_dir": str(self._plugin_dir),
            "project_root": str(p["root"]),
            "pylibs": str(p["pylibs"]),
            # 固定通道：直接用「准备素材」语料库里已处理好的切片
            "audio_paths": [str(c) for c in clips],
            "epochs": epochs,
            "rank": rank,
            "alpha": alpha,
            "batch": batch,
            "gradient_accumulation": accum,
            "max_duration": max(5.0, min(240.0, float(max_duration or 60.0))),
            "device": "cuda",
            "precision": "bf16",
        }

        def lora_name_for(model_name: str, many: bool) -> str:
            # 只训一个模型时保持原来的名字；多个模型时加变体后缀区分。
            return clean_name if not many else f"{clean_name}_{aliases[model_name]}"

        def run():
            self._job_update(job_id, state="queued",
                             detail=f"排队中：将依次训练 {len(requested)} 个模型（{', '.join(requested)}）")
            with self._process_lock:
                results = []
                errors = []
                total = len(requested)
                for index, model_name in enumerate(requested, 1):
                    alias = aliases[model_name]
                    lora_name = lora_name_for(model_name, total > 1)
                    pct = 5 + int((index - 1) * 90 / total)
                    self._job_update(
                        job_id, state="running", progress=pct,
                        detail=f"排队训练 {index}/{total}：{model_name} → 「{lora_name}」")
                    job = dict(job_base, name=lora_name,
                               model_name=model_name, model_alias=alias)
                    try:
                        result = runtime.run_worker(
                            p["python"], self._train_worker("ace_train_worker.py"),
                            job, cwd=p["output"], timeout=21600,
                            log_path=self._pipeline.log_path)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{model_name}：{type(exc).__name__}: {exc}")
                        continue
                    if not result.get("ok"):
                        errors.append(f"{model_name}：{result.get('error') or '训练失败'}")
                        continue
                    results.append({
                        "model": model_name,
                        "name": lora_name,
                        "path": result.get("lora_path"),
                        "caption": str(result.get("caption_zh")
                                       or result.get("caption") or ""),
                    })
                    self._job_update(
                        job_id, progress=5 + int(index * 90 / total),
                        detail=f"已完成 {index}/{total}：「{lora_name}」")
                if results:
                    detail = (f"训练完成 {len(results)}/{total}："
                              + "、".join(str(r["name"]) for r in results))
                    if errors:
                        detail += "；失败：" + "；".join(errors)[:300]
                    state = "done"
                else:
                    detail = "训练失败：" + "；".join(errors)[:500]
                    state = "failed"
                self._job_update(
                    job_id, state=state, progress=100, detail=detail,
                    lora_paths=[r["path"] for r in results if r.get("path")],
                    lora_path=(results[0]["path"] if len(results) == 1 else None),
                    models=requested, errors=errors)

        self._thread(f"ms-ace-train-{job_id}", run)
        return Ok({"ok": True, "job_id": job_id, "name": clean_name,
                   "models": requested, "samples": count,
                   "warnings": vram_warnings})

    @plugin_entry(id="ms_ace_install", name="为她而唱 · 安装 ACE 依赖",
                  description="把 ACE 需要的 Python 依赖装到计算根目录的 acestep\\pylibs（模型用 ms_ace_download 单独下载）",
                  input_schema={"type": "object", "properties": {}})
    async def ms_ace_install(self, **_):
        if not self._pipeline.paths.get("configured"):
            return Ok({"ok": False,
                       "error": "还没有选择安装位置，请先到安装向导/设置里选一个目录"})
        p = self._ace_paths()
        if not p["python"].is_file():
            return Ok({"ok": False, "error": f"找不到 DDSP 环境的 Python：{p['python']}"})
        try:
            p["pylibs"].mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return Ok({"ok": False, "error": f"无法创建依赖目录：{exc}"})
        job_id = self._job_begin("ace-install")
        packages = [
            "diffusers==0.36.0", "loguru", "accelerate", "win32-setctime",
            "transformers==4.57.1", "tokenizers==0.22.1", "huggingface_hub==0.36.0",
            "peft==0.18.0", "psutil",
            # ACE-Step v0.1.8 的模型代码/训练依赖
            "vector-quantize-pytorch>=1.27.15", "einx>=0.3.0",
            "torch-einops-utils>=0.1.42", "frozendict",
            # DCW 采样校正（v0.1.7+ 默认开启）需要这两个；缺了会自动 no-op
            "pytorch_wavelets", "PyWavelets", "setuptools<81",
            # 国内镜像源（download_source=mirror）走 ModelScope
            "modelscope", "modelscope-hub", "cryptography",
        ]

        def run():
            self._job_update(job_id, progress=5, detail="正在安装 ACE 依赖（约 100MB）")
            env = runtime._clean_env()
            # 用户机器上的本地代理经常是关的；pip 直连失败时不要让它卡在代理上
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                        "https_proxy", "all_proxy"):
                env.pop(key, None)
            env["NO_PROXY"] = "*"
            # pip --target 不会卸载同名旧版本；先清掉这些包的旧 dist-info，
            # 否则 importlib.metadata 仍可能读到 transformers 4.51 的版本号。
            stale = {"transformers", "tokenizers", "huggingface_hub", "peft",
                     "modelscope", "modelscope_hub", "cryptography",
                     "pytorch_wavelets", "pywavelets", "vector_quantize_pytorch",
                     "einx", "torch_einops_utils", "frozendict"}
            for dist in p["pylibs"].glob("*.dist-info"):
                stem = dist.name.split("-", 1)[0].replace("-", "_").lower()
                if stem in stale:
                    shutil.rmtree(dist, ignore_errors=True)
            cmd = [str(p["python"]), "-m", "pip", "install", "--no-deps",
                   "--upgrade", "--target", str(p["pylibs"]), *packages]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                                      env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as exc:  # noqa: BLE001
                self._job_update(job_id, state="failed", progress=100,
                                 detail=f"安装失败：{type(exc).__name__}: {exc}")
                return
            if proc.returncode != 0:
                tail = ((proc.stderr or "") + (proc.stdout or ""))[-1200:]
                self._job_update(job_id, state="failed", progress=100,
                                 detail="安装失败：" + (tail.strip() or f"pip 退出码 {proc.returncode}"))
                return
            self._job_update(job_id, state="done", progress=100, detail="ACE 依赖安装完成")

        self._thread(f"ms-ace-install-{job_id}", run)
        return Ok({"ok": True, "job_id": job_id})
