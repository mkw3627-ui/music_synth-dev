"""Job orchestration and the on-disk track database for 为她而唱.

The pipeline is deliberately boring: every step is a separate worker process in
its own Python environment, every step writes its artifacts to disk, and the
track record only advances when the step really produced its output. A failed
step keeps the artifacts of the steps that did succeed.

Voice-conversion flow (DDSP-SVC 6.3): separate the vocals from the original
accompaniment, re-voice the dry vocal with DDSP-SVC, then mix the converted
vocal back into the instrumental and write the final take.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import runtime

STEP_ORDER = ("separate", "convert", "mix")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _trim_plan(plan: dict) -> dict:
    """Plan snapshot for logs/state without the nested probe duplicate."""
    return {k: v for k, v in plan.items() if k != "probe"}


def _slug(text: str, fallback: str = "song") -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(text or ""))[:60].strip()
    return text or fallback


class TrackStore:
    """JSON-file track database, safe for the plugin's worker thread."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "tracks.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tracks: list[dict] = []
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("tracks") or []
            if isinstance(data, list):
                self._tracks = [t for t in data if isinstance(t, dict)]
        except (OSError, ValueError):
            self._tracks = []

    def _flush(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"tracks": self._tracks}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def all(self) -> list[dict]:
        with self._lock:
            self._load()
            return [dict(t) for t in self._tracks]

    def get(self, track_id: str) -> dict | None:
        with self._lock:
            self._load()
            for t in self._tracks:
                if t.get("id") == track_id:
                    return dict(t)
            return None

    def add(self, **fields) -> dict:
        with self._lock:
            self._load()
            track = {
                "id": uuid.uuid4().hex[:12],
                "title": fields.get("title") or "未命名歌曲",
                "artist": fields.get("artist") or "",
                "source": fields.get("source") or "",
                "start_s": fields.get("start_s"),
                "end_s": fields.get("end_s"),
                "duration_s": fields.get("duration_s"),
                "status": "pending",
                "step": "waiting",
                "progress": 0,
                "steps": {},
                "artifacts": {},
                "notes_count": 0,
                "lyrics_preview": [],
                "timbre_source": "",
                "plan": {},
                "error": "",
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._tracks.append(track)
            self._flush()
            return dict(track)

    def update(self, track_id: str, **fields) -> dict | None:
        with self._lock:
            self._load()
            for t in self._tracks:
                if t.get("id") != track_id:
                    continue
                for key, value in fields.items():
                    if key in {"steps", "artifacts"} and isinstance(value, dict):
                        t.setdefault(key, {}).update(value)
                    else:
                        t[key] = value
                t["updated_at"] = _now()
                self._flush()
                return dict(t)
            return None

    def remove(self, track_id: str) -> bool:
        with self._lock:
            self._load()
            before = len(self._tracks)
            self._tracks = [t for t in self._tracks if t.get("id") != track_id]
            if len(self._tracks) != before:
                self._flush()
                return True
            return False


class _TrackCancelled(Exception):
    """用户在处理中要求删除，流水线在阶段边界停止。"""


class Pipeline:
    def __init__(self, data_dir: Path, plugin_dir: Path):
        self.data_dir = Path(data_dir)
        self.plugin_dir = Path(plugin_dir)
        self.workers = self.plugin_dir / "workers"
        self.store = TrackStore(self.data_dir)
        self.paths = runtime.resolve_paths(self.data_dir)
        self.work_dir = Path(self.paths["work_dir"])
        # 还没选安装位置时不要在当前目录/别的盘悄悄建 work 目录。
        if self.paths.get("configured", True):
            self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.data_dir / "pipeline.log"
        self._run_lock = threading.Lock()
        self._busy: set[str] = set()
        # 用户在处理中点了删除：置取消标记，流水线在阶段边界停下来再真删
        self._cancelled: set[str] = set()
        self._pending_delete: dict[str, bool] = {}
        self.last_probe: dict = {}
        self.last_plan: dict = {}
        self.last_probe_at = ""
        # 插件构造时挂上：track -> (lrc_text, source)。流水线里预生成口型轨
        # 要用它去抓官方歌词，和播放时走同一条逻辑。
        self.lrc_fetcher = None

    # ---------------- helpers ----------------
    def _log(self, message: str) -> None:
        line = f"{_now()} {message}\n"
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass

    def _track_dir(self, track_id: str) -> Path:
        d = self.work_dir / "tracks" / track_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _file_ok(value) -> bool:
        """A path is reusable only when it exists as a non-empty file."""
        try:
            p = Path(str(value or ""))
            return bool(value) and p.is_file() and p.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _newer(path, ref) -> bool:
        """True when ``path`` was written at/after ``ref`` (1s slack for FAT/NTFS)."""
        try:
            return Path(path).stat().st_mtime + 1.0 >= Path(ref).stat().st_mtime
        except OSError:
            return False

    @staticmethod
    def _same_size_class(path, ref) -> bool:
        """Guard against a half-written stem from an interrupted merge.

        Separation workers write the merged stem only after all chunks succeed,
        but a kill during that final write can leave a short file.  A same-length
        PCM stem is within 10% of its input; anything much smaller is not reused.
        """
        try:
            return Path(path).stat().st_size >= Path(ref).stat().st_size * 0.9
        except OSError:
            return False

    def log_probe(self, probe: dict, plan: dict) -> None:
        """Persist one hardware probe + the plan derived from it."""
        self._log("PROBE " + json.dumps(probe, ensure_ascii=False))
        self._log("PLAN " + json.dumps(_trim_plan(plan), ensure_ascii=False))

    def _set(self, track_id: str, **fields) -> None:
        self.store.update(track_id, **fields)

    def _step(self, track_id: str, name: str, state: str, **extra) -> None:
        self.store.update(track_id, step=name, steps={name: {"state": state, "at": _now(), **extra}})

    @staticmethod
    def _plan_snapshot(plan: dict) -> dict:
        keys = ("tier", "label", "basis", "device", "sep_device", "synth_precision",
                "synth_n_steps", "max_segment_s", "parallel", "sep_step",
                "vram_total_gib", "vram_free_gib", "vram_used_gib",
                "ram_available_gib", "gpu_name", "compute_cap", "warnings")
        return {k: plan.get(k) for k in keys if k in plan}

    # ---------------- step runners ----------------
    def _run_attempts(self, python, script: Path, job: dict, out_dir: Path,
                      timeout: float, attempts: list[dict], label: str) -> dict:
        """Run a worker with explicit degradation attempts; retry only on OOM."""
        last: dict = {"ok": False, "error": "未执行"}
        for index, attempt in enumerate(attempts, 1):
            merged = dict(job)
            merged.update(attempt.get("job") or {})
            note = attempt.get("note") or f"第 {index} 次"
            self._log(f"{label}: {note}")
            try:
                result = runtime.run_worker(
                    python, script, merged, cwd=out_dir, timeout=timeout,
                    log_path=self.log_path, env_extra=attempt.get("env"))
            except Exception as exc:  # noqa: BLE001 - report, then try next
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if result.get("ok"):
                result.setdefault("attempts", index)
                return result
            last = result
            if not runtime.is_oom(result):
                return result
            self._log(f"{label}: 检测到显存不足，自动降级（{note}）")
        if runtime.is_oom(last):
            last["oom"] = True
            last["advice"] = runtime.oom_advice(getattr(self, "last_plan", {}) or {})
        return last

    # ------------------------------------------------------------------
    # voice conversion (DDSP-SVC 6.3): re-voice the separated dry vocal
    # ------------------------------------------------------------------
    def _convert(self, track_id: str, out_dir: Path, vocals: str, plan: dict, key: int | None = None) -> dict:
        """Step 2: run the DDSP-SVC RectifiedFlow model over the dry vocal.

        The DDSP environment's own Python drives the repo entry script
        (``main_reflow.py``) with ``cwd=repo root`` because the vocoder loads
        ``pretrain/rmvpe/model.pt`` by a relative path.  The job carries only
        the keys ``ddsp_worker.main()`` consumes, so a stray key can never
        reach the subprocess.
        """
        repo = Path(str(self.paths.get("ddsp_root") or ""))
        python = Path(self.paths["synth_python"])
        ckpt = Path(str(self.paths.get("ddsp_ckpt") or ""))
        problems = []
        if not python.is_file():
            problems.append(f"DDSP 环境 {python}")
        if not (repo / "main_reflow.py").is_file():
            problems.append(f"DDSP 仓库 {repo}")
        if not ckpt.is_file():
            problems.append(f"训练好的模型 {ckpt}")
        if problems:
            message = ("缺少 " + "、".join(problems)
                       + "；请先打开「首次使用向导」训练 DDSP-SVC 模型")
            self._step(track_id, "convert", "failed", error=message)
            self._set(track_id, status="failed", progress=60, error=message)
            return {"ok": False, "step": "convert", "error": message}

        device = str(plan.get("device") or "cuda")
        self._step(track_id, "convert", "running", device=device, engine="ddsp",
                   model=ckpt.name)
        raw = self.paths.get("raw") or {}
        job = {
            "repo": str(repo),
            "python": str(python),
            "model_ckpt": str(ckpt),
            "input": str(vocals),
            "out_dir": str(out_dir / "convert"),
            "key": int(self.paths.get("key_shift") or 0) if key is None else int(key),
            "vocal_register_shift": int(self.paths.get("vocal_register_shift") or 0),
            "infer_step": int(self.paths.get("infer_step") or 80),
            "method": str(raw.get("method") or "euler"),
            "pitch_extractor": str(raw.get("pitch_extractor") or "rmvpe"),
            "device": device,
            "f0_min": int(raw.get("f0_min") or 65),
            "f0_max": int(raw.get("f0_max") or 800),
            "threshold": int(raw.get("threshold") or -60),
            "timeout": 7200,
        }
        result = self._run_attempts(
            python, self.workers / "ddsp_worker.py", job, out_dir, 7200,
            [{"job": job, "note": f"设备 {device}"}] + ([{
                "job": {"device": "cpu"},
                "env": {"CUDA_VISIBLE_DEVICES": "-1"},
                "note": "GPU 显存不足，改走 CPU 变声",
            }] if device != "cpu" else []),
            f"[{track_id}] 变声")
        if not result.get("ok"):
            self._step(track_id, "convert", "failed", error=result.get("error"))
            self._set(track_id, status="failed", progress=60,
                      error=result.get("error") or "变声失败")
            return {"ok": False, "step": "convert", "error": result.get("error"),
                    "detail": result}
        self._step(track_id, "convert", "done", engine="ddsp", model=ckpt.name,
                   key=job["key"], infer_step=job["infer_step"],
                   input=str(vocals),
                   duration_s=result.get("duration_s"), peak=result.get("peak"),
                   rms=result.get("rms"))
        self._set(track_id, progress=75,
                  artifacts={"vocal_wav": result.get("wav", "")})
        self._log(f"[{track_id}] convert: {ckpt.name} key={job['key']} "
                  f"step={job['infer_step']} {result.get('duration_s')}s "
                  f"peak={result.get('peak')} rms={result.get('rms')}")
        return result

    def _soulx_convert(self, track_id: str, out_dir: Path, vocals: str,
                       plan: dict) -> dict:
        """Step 2 alternative: SoulX-Singer SVC on the ACE dry vocal."""
        raw = self.paths.get("raw") or {}
        root = Path(str(self.paths.get("compute_root") or ""))
        python = Path(str(raw.get("soulx_python")
                          or (root / "envs" / "soulx" / "Scripts" / "python.exe")))
        repo = Path(str(raw.get("soulx_repo") or (root / "soulx" / "repo")))
        model = Path(str(raw.get("soulx_model")
                         or (root / "soulx" / "models" / "model-svc.pt")))
        config = repo / "soulxsinger" / "config" / "soulxsinger.yaml"
        rmvpe = Path(str(raw.get("soulx_rmvpe")
                         or (root / "soulx" / "preprocess" / "rmvpe" / "rmvpe.pt")))
        timbre = Path(str(raw.get("timbre_path") or ""))
        problems = []
        if not python.is_file():
            problems.append(f"SoulX 环境 {python}")
        if not (repo / "soulxsinger").is_dir():
            problems.append(f"SoulX 仓库 {repo}")
        if not model.is_file():
            problems.append(f"SoulX 权重 {model}")
        if not config.is_file():
            problems.append(f"SoulX 配置 {config}")
        if not rmvpe.is_file():
            problems.append(f"RMVPE 音高模型 {rmvpe}")
        if not timbre.is_file():
            problems.append(f"音色样本 {timbre}")
        if problems:
            message = ("缺少 " + "、".join(problems)
                       + "；请先安装 SoulX-Singer")
            self._step(track_id, "convert", "failed", error=message)
            self._set(track_id, status="failed", progress=60, error=message)
            return {"ok": False, "step": "convert", "error": message}

        out = out_dir / "convert"
        out.mkdir(parents=True, exist_ok=True)
        self._step(track_id, "convert", "running", engine="soulx",
                   device=plan.get("device"))
        job = {
            "repo": str(repo),
            "model_path": str(model),
            "config_path": str(config),
            "rmvpe_path": str(rmvpe),
            "prompt_wav": str(timbre),
            "target_wav": str(vocals),
            "out_dir": str(out),
            "device": "cuda" if str(plan.get("device", "cuda")).startswith("cuda") else "cpu",
            "n_steps": 32,
            "cfg": 3.0,
            "fp16": True,
        }
        self._log(f"[{track_id}] SoulX-SVC: {timbre.name} -> {Path(vocals).name}")
        result = self._run_attempts(
            python, self.workers / "soulx_svc_worker.py", job, out, 3600,
            [{"job": job, "note": f"设备 {job['device']}"}] + ([{
                "job": {"device": "cpu", "fp16": False},
                "env": {"CUDA_VISIBLE_DEVICES": "-1"},
                "note": "GPU 显存不足，改走 CPU 变声",
            }] if job["device"] != "cpu" else []),
            f"[{track_id}] SoulX 变声")
        if not result.get("ok"):
            self._step(track_id, "convert", "failed", error=result.get("error"))
            self._set(track_id, status="failed", progress=60,
                      error=result.get("error") or "SoulX 变声失败")
            return {"ok": False, "step": "convert", "error": result.get("error"),
                    "detail": result}
        self._step(track_id, "convert", "done", engine="soulx",
                   duration_s=result.get("duration_s"),
                   peak=result.get("peak"), rms=result.get("rms"))
        self._set(track_id, progress=75,
                  artifacts={"vocal_wav": result.get("wav", ""),
                             "vocal_source": vocals})
        self._log(f"[{track_id}] soulx convert: {result.get('wav')}")
        return result

    # 开唱前的人声清理链：只去噪，不去混响。
    # 去混响（anvuew）会把 6~11 kHz（齿音、气声、咬字的清晰度）压掉 20~37 dB，
    # 人声立刻发闷发糊；训练素材那边也已经把这一步整个拿掉了，两边保持一致。
    _CLEAN_MODELS = [
        ("denoise", "denoise_mel_band_roformer_aufr33_sdr_27.9959.ckpt", "dry"),
    ]
    # 2 = 不含去混响。旧目录里的 clean/denoise/_dry.wav 是拿去混响之后的结果再降噪的，
    # 不能当缓存复用，否则等于把去混响偷偷带回来。
    _CLEAN_VERSION = "2"

    def _clean_vocal(self, track_id: str, out_dir: Path, vocals: str, plan: dict,
                     resume: bool = True) -> dict:
        """把分离出来的人声做去混响 + 去噪，让它和训练素材同分布。

        训练语料走的就是这两步；开唱时若直接用分离结果，混响/残留会让 f0 抖动，
        DDSP 的输出就会发飘、发金属（用户说的「电音感」）。
        """
        python = Path(self.paths["sep_python"])
        if not python.is_file() or not vocals:
            return {"ok": False, "error": "分离环境未就绪"}
        # 清理链版本对不上就重算（旧缓存是「去混响 + 去噪」两步出来的）
        ver_file = out_dir / "clean" / "version.txt"
        try:
            cached_ver = ver_file.read_text(encoding="utf-8").strip()
        except OSError:
            cached_ver = ""
        if cached_ver != self._CLEAN_VERSION:
            resume = False
        cur = vocals
        for name, model, stem in self._CLEAN_MODELS:
            stage_dir = out_dir / "clean" / name
            # 续跑：上一轮已经做完的清理阶段直接接着用。输出必须比当前输入新，
            # 否则可能是更早一次处理留下的旧文件。
            cached = stage_dir / f"_{stem}.wav"
            if (resume and self._file_ok(str(cached)) and self._newer(cached, cur)
                    and self._same_size_class(cached, cur)):
                cur = str(cached)
                self._log(f"[{track_id}] resume: 复用 clean/{name} -> {cur}")
                continue
            self._step(track_id, "clean", "running", stage=name)
            job = {
                "input": cur,
                "out_dir": str(stage_dir),
                "backend": "audio_separator",
                "model": model,
                "model_dir": str(self.paths.get("sep_model_dir") or (self.work_dir / "models")),
                "device": plan.get("sep_device", "cuda:0"),
                "use_cuda": plan.get("sep_device", "cuda:0") != "cpu",
                "use_autocast": plan.get("sep_device", "cuda:0") != "cpu",
            }
            attempts = [{"job": job, "note": f"设备 {job['device']}"}]
            if job["device"] != "cpu":
                attempts.append({
                    "job": {"device": "cpu", "use_cuda": False, "use_autocast": False},
                    "env": {"CUDA_VISIBLE_DEVICES": "-1"},
                    "note": "GPU 失败，改走 CPU",
                })
            res = self._run_attempts(python, self.workers / "separate_worker.py", job,
                                     stage_dir, 3600, attempts, f"[{track_id}] {name}")
            if not res.get("ok"):
                return {"ok": False, "step": name, "error": res.get("error") or f"{name} 失败"}
            stems = res.get("stems") or {}
            nxt = stems.get(stem) or res.get("target_stem") or ""
            if not nxt or not Path(str(nxt)).is_file():
                return {"ok": False, "step": name, "error": f"{name} 未产出 {stem} 轨"}
            cur = str(nxt)
            self._log(f"[{track_id}] clean/{name} -> {cur}")
        try:
            ver_file.parent.mkdir(parents=True, exist_ok=True)
            ver_file.write_text(self._CLEAN_VERSION, encoding="utf-8")
        except OSError:
            pass
        return {"ok": True, "wav": cur}

    def _ref_f0_hz(self) -> float:
        """训练语料的参考音高（合格切片 f0 中位数的中位数）；拿不到返回 0。"""
        try:
            index_path = Path(str(self.paths.get("compute_root") or "")) / "corpus" / "index.json"
            data = json.loads(index_path.read_text(encoding="utf-8"))
            vals = []
            for src in data.get("sources", []):
                for clip in src.get("clips", []):
                    if clip.get("status") == "ok" and clip.get("f0_hz_median"):
                        vals.append(float(clip["f0_hz_median"]))
            if not vals:
                return 0.0
            vals.sort()
            return float(vals[len(vals) // 2])
        except Exception:  # noqa: BLE001
            return 0.0

    def _auto_key(self, track_id: str, vocal_wav: str, plan: dict) -> int:
        """按「源人声音高 / 训练语料音高」算半音差，把嗓音挪到模型熟悉的音域。

        返回交给 main_reflow 的 -k 值；伴奏会在混音时同向移调，成品仍保持原调关系。
        """
        raw = self.paths.get("raw") or {}
        if not bool(raw.get("auto_key", True)):
            return 0
        python = Path(self.paths["sep_python"])
        if not python.is_file() or not vocal_wav:
            return 0
        ref = float(raw.get("ref_f0") or 0) or self._ref_f0_hz()
        if ref <= 0:
            return 0
        probe = runtime.run_worker(python, self.workers / "f0_worker.py",
                                   {"input": str(vocal_wav)}, cwd=self.work_dir,
                                   timeout=600, log_path=self.log_path)
        if not probe.get("ok"):
            self._log(f"[{track_id}] auto-key 跳过：{probe.get('error')}")
            return 0
        med = float(probe.get("median_hz") or 0)
        if med <= 0:
            return 0
        shift = int(round(12.0 * math.log2(ref / med)))
        cap = int(raw.get("auto_key_max") or 4)   # 移调太大，伴奏的相位声码器会明显失真
        shift = max(-cap, min(cap, shift))
        if abs(shift) < 2:
            shift = 0
        self._log(f"[{track_id}] auto-key: 源 {med:.0f}Hz / 训练参考 {ref:.0f}Hz "
                  f"→ {shift:+d} 半音 (voiced={probe.get('voiced_ratio')})")
        self._step(track_id, "key", "done", source_f0=med, ref_f0=ref, shift=shift)
        return shift

    def _separate(self, track: dict, out_dir: Path, plan: dict) -> dict:
        backend = self.paths.get("sep_backend") or "bs_roformer"
        model = plan.get("sep_model") or "mel_band_roformer_karaoke_becruily.ckpt"
        if self.paths.get("sep_backend") == "demucs":
            model = "htdemucs"
        job = {
            "input": track["source"],
            "out_dir": str(out_dir / "separate"),
            "backend": "demucs" if backend == "demucs" else "audio_separator",
            "model": model,
            "model_dir": str(self.paths.get("sep_model_dir") or (self.work_dir / "models")),
            "device": plan.get("sep_device", "cuda:0"),
            "use_cuda": plan.get("sep_device", "cuda:0") != "cpu",
            "use_autocast": plan.get("sep_device", "cuda:0") != "cpu",
            "sep_step": int(plan.get("sep_step") or 4),
        }
        python = Path(self.paths["sep_python"])
        if not Path(python).is_file():
            return {"ok": False, "step": "separate",
                    "error": f"分离环境未安装（缺少 {python}）；请先运行首次使用向导 ms_setup_install"}
        attempts = [{"job": job, "note": f"设备 {job['device']}"}]
        if job["device"] != "cpu":
            attempts.append({
                "job": {"device": "cpu", "use_cuda": False, "use_autocast": False},
                "env": {"CUDA_VISIBLE_DEVICES": "-1"},
                "note": "GPU 失败，改走 CPU 分离",
            })
        return self._run_attempts(python, self.workers / "separate_worker.py", job,
                                  out_dir, 3600, attempts, f"[{track.get('id')}] 分离")

    def _check_song(self, track: dict, vocals: str) -> dict:
        """能不能翻唱：纯音乐没人声、录音残缺、采集到静音。

        人声判断直接用刚分离出来的人声轨（不额外占显存）；完整性优先用
        播放器报的整首时长和播放进度，播放器不报就只做「缺头 / 太短」这类
        能确定的判断，其余标成未确认——没证据就不拦歌。
        """
        python = Path(str(self.paths.get("synth_python") or ""))
        if not python.is_file():
            return {"ok": False, "error": f"计算环境未就绪（缺少 {python}）"}
        job = {
            "input": str(track.get("source") or ""),
            "vocals": str(vocals or ""),
            "expected_s": 0.0,
            "start_position_s": track.get("start_position_s"),
            "end_position_s": track.get("end_position_s"),
            "min_song_s": float(self.paths.get("min_song_s") or 25.0),
        }
        expected = float(track.get("expected_s") or 0.0)
        expected_from = "player" if expected > 0 else ""
        if expected <= 0 and track.get("auto_recorded"):
            # 播放器（老版本网易云）不报整首时长时，用公开搜索接口查唱片版本
            # 的时长兜底；查不到就保持未知，不拦歌。
            try:
                from . import lyrics_fetch
                sec = lyrics_fetch.fetch_song_duration(
                    str(track.get("title") or ""), str(track.get("artist") or ""),
                    timeout=6.0)
            except Exception as exc:  # noqa: BLE001 - 查不到不影响判断
                sec = None
                self._log(f"[{track.get('id')}] 查整首时长失败：{exc}")
            if sec:
                expected, expected_from = float(sec), "lookup"
                self._log(f"[{track.get('id')}] 整首时长（查询）：{expected:.1f}s")
        job["expected_s"] = expected
        job["expected_from"] = expected_from
        return runtime.run_worker(python, self.workers / "song_check_worker.py", job,
                                  cwd=self.work_dir, timeout=900,
                                  log_path=self.log_path)

    # ---------------- 口型轨 ----------------
    def _build_lips(self, track: dict, out_dir: Path, stem: str,
                    duration: float = 0.0) -> dict:
        """逐字口型轨算一次、落盘成 ``<track>/lips.json``。

        唱的时候读进度条只要把这份数据直接发布出去就行，不用现场抓歌词、
        算包络、做对齐——这几个动作加起来就是几秒的「嘴不动」。
        """
        from . import lipsync

        track_id = str(track.get("id") or "")
        payload = lipsync.build_track_payload(
            track, stem=stem, duration=duration, lrc_fetcher=self.lrc_fetcher)
        target = out_dir / "lips.json"
        tmp = target.with_suffix(".json.part")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        official = payload.get("official_lyrics")
        if official and track_id:
            # 歌词顺手存回曲目，下次播放/重跑不用再联网找一遍。
            try:
                self.store.update(track_id, official_lyrics=official)
            except Exception:  # noqa: BLE001 - 存不下来不影响口型
                pass
        return {"path": str(target), "mode": payload.get("mode"),
                "cues": len(payload.get("cues") or []),
                "env": len(payload.get("env") or []),
                "align": payload.get("align"),
                "source": payload.get("source"),
                "chars": sum(1 for cue in (payload.get("cues") or [])
                             if isinstance(cue, list) and len(cue) > 1
                             and cue[1] != "rest")}

    # ---------------- full track ----------------
    def _trash_path(self, path: Path) -> None:
        """移到回收站（和插件里 _trash_move 同一个目录），不直接删。"""
        path = Path(path)
        if not path.exists():
            return
        root = Path(str(self.paths.get("compute_root") or self.work_dir)) / "_trash_20261103"
        try:
            root.mkdir(parents=True, exist_ok=True)
            dest = root / f"{int(time.time() * 1000)}_{path.name}"
            shutil.move(str(path), str(dest))
        except OSError:
            pass

    def request_cancel(self, track_id: str, delete_files: bool) -> bool:
        """处理中的曲目要删除：先登记，流水线在下一个阶段边界收尾并删除。"""
        with self._run_lock:
            if track_id not in self._busy:
                return False
            self._cancelled.add(track_id)
            self._pending_delete[track_id] = bool(delete_files)
        self._log(f"[{track_id}] 收到取消请求（delete_files={bool(delete_files)}）")
        return True

    def process_track(self, track_id: str, plan: dict | None = None,
                      *, resume: bool = True) -> dict:
        with self._run_lock:
            if track_id in self._busy:
                return {"ok": False, "error": "该曲目正在处理中"}
            self._busy.add(track_id)
        return self._process_track_body(track_id, plan, resume=resume)

    def _process_track_body(self, track_id: str, plan: dict | None = None,
                            *, resume: bool = True) -> dict:
        try:
            track = self.store.get(track_id)
            if not track:
                return {"ok": False, "error": "曲目不存在"}
            # 上一次已经产出的中间结果。resume=True 时能复用就复用，插件重载后
            # 点「继续唱」只补没做完的步骤，而不是从分离重新跑一遍。
            art0 = track.get("artifacts") or {}
            steps0 = track.get("steps") or {}
            # Probe again for *this* track: free VRAM changes between songs.
            probe = runtime.probe_hardware(force=True)
            plan = runtime.plan_for(
                probe,
                self.paths.get("tier_override", ""),
                self.paths.get("precision", "auto"),
                self.paths.get("max_parallel", 0),
            )
            self.last_probe = probe
            self.last_plan = plan
            self.last_probe_at = _now()
            self._log(f"[{track_id}] PROBE " + json.dumps(probe, ensure_ascii=False))
            self._log(f"[{track_id}] PLAN " + json.dumps(_trim_plan(plan), ensure_ascii=False))
            out_dir = self._track_dir(track_id)
            self._set(track_id, status="running", progress=5, error="",
                      plan=self._plan_snapshot(plan))
            self._log(f"[{track_id}] start: {track['title']} ({track['source']})")

            # 1) separation -------------------------------------------------
            # 续跑：上一轮已经分离成功的人声/伴奏直接复用。插件重载时最贵的一步
            # 往往早就做完了，没必要让用户再等一遍。
            vocals = str(art0.get("vocals") or "")
            instrumental = str(art0.get("instrumental") or "")
            sep_step = steps0.get("separate") or {}
            if (resume and str(sep_step.get("state") or "") == "done"
                    and self._file_ok(vocals) and self._file_ok(instrumental)):
                self._step(track_id, "separate", "done", reused=True,
                           engine=sep_step.get("engine"), model=sep_step.get("model"))
                self._set(track_id, progress=30,
                          artifacts={"vocals": vocals, "instrumental": instrumental,
                                     "separate_files": art0.get("separate_files") or []})
                self._log(f"[{track_id}] resume: 复用已分离的人声/伴奏")
            else:
                self._step(track_id, "separate", "running", device=plan.get("sep_device"))
                sep = self._separate(track, out_dir, plan)
                if not sep.get("ok"):
                    self._step(track_id, "separate", "failed", error=sep.get("error"))
                    self._set(track_id, status="failed", error=sep.get("error") or "分离失败",
                              progress=10)
                    return {"ok": False, "step": "separate", "error": sep.get("error"),
                            "detail": sep}
                vocals = sep.get("vocals") or ""
                instrumental = sep.get("instrumental") or ""
                self._step(track_id, "separate", "done", engine=sep.get("engine"), model=sep.get("model"))
                if track_id in self._cancelled:
                    raise _TrackCancelled()
                self._set(track_id, progress=30,
                          artifacts={"vocals": vocals, "instrumental": instrumental,
                                     "separate_files": sep.get("files", [])})

            # 1.2) 能不能唱：纯音乐 / 残缺录音 / 空录音 → 在这里停住 -----
            # 后面是最吃显存的变声和混音；不能唱的歌不该白跑一遍。
            check_step = steps0.get("check") or {}
            if bool(track.get("check_override")):
                self._step(track_id, "check", "skipped", note="用户要求照唱")
                self._log(f"[{track_id}] 用户要求照唱，跳过可用性判断")
            else:
                reused_check = (resume and str(check_step.get("state") or "") == "done"
                                and check_step.get("singable") is not None)
                if reused_check:
                    verdict = {"singable": bool(check_step.get("singable")),
                               "status": str(check_step.get("verdict") or "ok"),
                               "reason": str(check_step.get("reason") or ""),
                               "detail": check_step.get("detail") or {}}
                    self._log(f"[{track_id}] resume: 复用可用性判断 "
                              f"{verdict['status']} singable={verdict['singable']}")
                else:
                    self._step(track_id, "check", "running")
                    chk = self._check_song(track, vocals)
                    if chk.get("ok"):
                        verdict = chk
                        self._step(track_id, "check", "done",
                                   singable=bool(chk.get("singable")),
                                   verdict=chk.get("status"),
                                   reason=chk.get("reason") or "",
                                   detail=chk.get("detail") or {})
                        self._log(f"[{track_id}] 可用性：{chk.get('status')} "
                                  f"has_vocals={chk.get('has_vocals')} "
                                  f"complete={chk.get('complete')} "
                                  f"{chk.get('reason') or ''}")
                    else:
                        # 判断不了不能当成「不能唱」：宁可放过去，也不能凭
                        # 一次检查失败把歌拦死。
                        verdict = {"singable": True, "status": "ok", "reason": ""}
                        self._step(track_id, "check", "skipped", error=chk.get("error"))
                        self._log(f"[{track_id}] 可用性判断没做成，按能唱继续："
                                  f"{chk.get('error')}")
                if verdict.get("singable") is False:
                    status = str(verdict.get("status") or "incomplete")
                    reason = str(verdict.get("reason") or "这首歌暂时不能翻唱")
                    self._set(track_id, status=status, progress=100, error="",
                              check_status=status, check_reason=reason,
                              check_detail=verdict.get("detail") or {})
                    self._log(f"[{track_id}] 不能翻唱（{status}）：{reason}")
                    return {"ok": True, "blocked": True, "step": "check",
                            "status": status, "note": reason,
                            "detail": verdict}

            # 1.5) 人声清理（去混响 → 去噪），和训练素材对齐 ---------------
            clean_step = steps0.get("clean") or {}
            vocal_clean = str(art0.get("vocal_clean") or "")
            if (resume and str(clean_step.get("state") or "") == "done"
                    and self._file_ok(vocal_clean)):
                vocal_input = vocal_clean
                self._step(track_id, "clean", "done", wav=vocal_input, reused=True)
                self._log(f"[{track_id}] resume: 复用已清理的人声")
            else:
                self._step(track_id, "clean", "running")
                cleaned = self._clean_vocal(track_id, out_dir, vocals, plan, resume=resume)
                if cleaned.get("ok"):
                    vocal_input = cleaned.get("wav") or vocals
                    self._step(track_id, "clean", "done", wav=vocal_input)
                else:
                    vocal_input = vocals
                    self._step(track_id, "clean", "skipped", error=cleaned.get("error"))
                    self._log(f"[{track_id}] 清理失败，直接用分离结果：{cleaned.get('error')}")
            self._set(track_id, progress=45, artifacts={"vocal_clean": vocal_input})

            # 1.5) 口型轨：不占 GPU，和后面的配调/变声/混音并行算 ------------
            # 读整首干声的包络 + 逐行对齐是几秒纯 CPU，正好搭变声那段的便车。
            # 算完落盘成 lips.json，唱的时候直接读，不再现场抓歌词/算包络。
            lips_holder: dict = {}
            lips_error = ""

            def _lips_job() -> None:
                nonlocal lips_error
                try:
                    lips_holder.update(self._build_lips(
                        self.store.get(track_id) or track, out_dir, vocal_input,
                        float(track.get("duration_s") or 0.0)))
                except Exception as exc:  # noqa: BLE001 - 口型失败不能拖垮出歌
                    lips_error = f"{type(exc).__name__}: {exc}"
                    self._log(f"[{track_id}] 口型轨计算失败：{lips_error}")

            self._step(track_id, "lips", "running")
            lips_thread = threading.Thread(target=_lips_job,
                                           name=f"ms-lips-{track_id}", daemon=True)
            lips_thread.start()

            # 1.55) 混响跟随原曲：用原始分离人声估计原曲人声的混响量 --------
            reverb_raw = self.paths.get("raw") or {}
            auto_reverb = bool(reverb_raw.get("reverb_auto", True))
            try:
                auto_strength = float(reverb_raw.get("reverb_auto_strength", 1.0) or 1.0)
            except (TypeError, ValueError):
                auto_strength = 1.0
            auto_strength = max(0.0, min(1.0, auto_strength))
            estimated_wet = None
            reverb_step = steps0.get("reverb") or {}
            if (resume and auto_reverb and str(reverb_step.get("state") or "") == "done"
                    and reverb_step.get("wet") is not None):
                estimated_wet = float(reverb_step["wet"])
                self._log(f"[{track_id}] resume: 复用混响估计 wet={estimated_wet:.3f}")
            elif auto_reverb:
                est = runtime.run_worker(
                    Path(self.paths["synth_python"]), self.workers / "reverb_worker.py",
                    {"input": str(vocals)}, cwd=self.work_dir, timeout=600,
                    log_path=self.log_path)
                if est.get("ok") and est.get("wet") is not None:
                    estimated_wet = float(est["wet"])
                    self._step(track_id, "reverb", "done", cpp=est.get("cpp"),
                               wet=round(estimated_wet, 3), strength=auto_strength)
                    self._log(f"[{track_id}] 混响估计：CPP={est.get('cpp')} "
                              f"wet={estimated_wet:.3f}")
                else:
                    self._log(f"[{track_id}] 混响估计跳过："
                              f"{est.get('error') or est.get('note')}")

            if track_id in self._cancelled:
                raise _TrackCancelled()
            # 1.6) 自动配调：把嗓音挪到模型熟悉的音域 ---------------------
            key_step = steps0.get("key") or {}
            if (resume and str(key_step.get("state") or "") == "done"
                    and key_step.get("shift") is not None):
                key_shift = int(key_step["shift"])
                self._log(f"[{track_id}] resume: 复用配调 {key_shift:+d} 半音")
            else:
                key_shift = self._auto_key(track_id, vocal_input, plan)

            # 2) voice conversion -------------------------------------------
            # post_engine=soulx 时走 SoulX-Singer SVC；否则走 DDSP-SVC。
            # 只有「人声没换、模型/音高/步数都没变」时才能复用上一轮的变声结果。
            # 用户点「重新唱一遍」会传 resume=False，强制重跑。
            engine = str(track.get("post_engine") or "ddsp").strip().lower()
            if engine in ("soulx", "soulx_ddsp"):
                # SoulX 保持原调，不要让伴奏跟着 auto-key 移调
                key_shift = 0
            ckpt_name = Path(str(self.paths.get("ddsp_ckpt") or "")).name
            infer_step = int(self.paths.get("infer_step") or 80)
            conv_step = steps0.get("convert") or {}
            can_reuse_convert = (
                resume and engine == "ddsp"
                and str(conv_step.get("engine") or "ddsp") == "ddsp"
                and str(conv_step.get("state") or "") == "done"
                and self._file_ok(str(art0.get("vocal_wav") or ""))
                and str(conv_step.get("input") or "") == str(vocal_input)
                and str(conv_step.get("model") or "") == ckpt_name
                and int(conv_step.get("key") or 0) == int(key_shift)
                and int(conv_step.get("infer_step") or 0) == infer_step
            )
            if can_reuse_convert:
                converted_vocal = str(art0.get("vocal_wav"))
                self._step(track_id, "convert", "done", engine="ddsp",
                           model=ckpt_name, key=key_shift, infer_step=infer_step,
                           input=str(vocal_input), reused=True)
                self._set(track_id, progress=75,
                          artifacts={"vocal_wav": converted_vocal,
                                     "vocal_source": vocals})
                self._log(f"[{track_id}] resume: 复用已变声人声 {converted_vocal}")
            elif engine in ("soulx", "soulx_ddsp"):
                convert = self._soulx_convert(track_id, out_dir, vocal_input, plan)
                if not convert.get("ok"):
                    self._set(track_id, status="failed", progress=60,
                              error=convert.get("error") or "SoulX 变声失败")
                    return {"ok": False, "step": "convert",
                            "error": convert.get("error"), "detail": convert}
                converted_vocal = convert.get("wav") or ""
                if engine == "soulx_ddsp":
                    # 二级 DDSP：SoulX 负责唱法/咬字（保持原调），DDSP 再用
                    # 44.1 kHz 的模型补回气音和音色。中间干声已在 SoulX worker
                    # 里对齐到原干声 RMS，所以 DDSP 的输入电平是正常的。
                    ddsp_dir = out_dir / "ddsp"
                    ddsp_dir.mkdir(parents=True, exist_ok=True)
                    self._log(f"[{track_id}] SoulX→DDSP: "
                              f"{Path(converted_vocal).name} → DDSP")
                    polish = self._convert(track_id, ddsp_dir, converted_vocal,
                                           plan, key=0)
                    if not polish.get("ok"):
                        self._set(track_id, status="failed", progress=70,
                                  error=polish.get("error")
                                  or "SoulX→DDSP 二次变声失败")
                        return {"ok": False, "step": "convert",
                                "error": polish.get("error"), "detail": polish}
                    converted_vocal = polish.get("wav") or ""
                    # _convert 内部会把这一步标成 ddsp；这里改写成本次真实
                    # 的引擎，面板和曲目记录才能区分。
                    self._step(track_id, "convert", "done",
                               engine="soulx_ddsp",
                               duration_s=polish.get("duration_s"),
                               peak=polish.get("peak"),
                               rms=polish.get("rms"))
                    self._set(track_id, progress=75,
                              artifacts={"vocal_wav": converted_vocal,
                                         "vocal_source": vocal_input})
                    self._log(f"[{track_id}] soulx→ddsp convert: "
                              f"{converted_vocal}")
            else:
                self._step(track_id, "convert", "running", device=plan.get("device"))
                convert = self._convert(track_id, out_dir, vocal_input, plan, key=key_shift)
                if not convert.get("ok"):
                    self._step(track_id, "convert", "failed", error=convert.get("error"))
                    self._set(track_id, status="failed", progress=60,
                              error=convert.get("error") or "变声失败")
                    return {"ok": False, "step": "convert", "error": convert.get("error"),
                            "detail": convert}
                converted_vocal = convert.get("wav") or ""
                self._set(track_id, progress=75,
                          artifacts={"vocal_wav": converted_vocal,
                                     "vocal_source": vocals})

            if track_id in self._cancelled:
                raise _TrackCancelled()
            # 3) mix ----------------------------------------------------------
            self._step(track_id, "mix", "running")
            if estimated_wet is not None and auto_reverb:
                vocal_reverb = max(0.0, min(0.6, estimated_wet * auto_strength))
            else:
                vocal_reverb = float(reverb_raw.get("vocal_reverb", 0.12))
            mix = runtime.run_worker(
                Path(self.paths["synth_python"]), self.workers / "mix_worker.py",
                {"vocal": converted_vocal, "instrumental": instrumental,
                 "out_dir": str(out_dir / "final"),
                 # 人声被移了 key 半音，伴奏同向移同样多，成品保持原调
                 "accompaniment_shift": -int(key_shift),
                 # 伴奏加宽 >1 能更宽，但会削薄中间声部；默认 1.0 保持原样
                 "stereo_width": float((self.paths.get("raw") or {}).get("stereo_width") or 1.0),
                 "vocal_reverb": vocal_reverb,
                 # 人声清晰度/电平与伴奏音量（高级设置）
                 "clarity": float((self.paths.get("raw") or {}).get("clarity", 1.0)),
                 "vocal_level_db": float((self.paths.get("raw") or {}).get("vocal_level_db", -2.0)),
                 "bgm_gain_db": float((self.paths.get("raw") or {}).get("bgm_gain_db", 0.0)),
                 # 咬字增强用原始分离人声（去噪前）的高频，补回辅音
                 "reference_vocal": str(vocals),
                 "consonant": float((self.paths.get("raw") or {}).get("consonant", 0.35))},
                cwd=out_dir, timeout=900, log_path=self.log_path,
            )
            if not mix.get("ok"):
                self._step(track_id, "mix", "failed", error=mix.get("error"))
                self._set(track_id, status="failed", progress=95,
                          error=mix.get("error") or "混音失败")
                return {"ok": False, "step": "mix", "error": mix.get("error"),
                        "detail": mix}
            # 口型轨一般早就跟变声一起跑完了；这里只是收个尾。
            lips_thread.join(timeout=240)
            lips_info = dict(lips_holder)
            if lips_info.get("path"):
                self._step(track_id, "lips", "done", mode=lips_info.get("mode"),
                           cues=lips_info.get("cues"), align=lips_info.get("align"),
                           source=lips_info.get("source"))
                self._log(f"[{track_id}] 口型轨就绪：{lips_info.get('mode')} "
                          f"cues={lips_info.get('cues')} align={lips_info.get('align')} "
                          f"source={lips_info.get('source')}")
            else:
                self._step(track_id, "lips", "skipped",
                           error=lips_error or "口型轨没算完，播放时再补")
            self._step(track_id, "mix", "done", engine="ddsp")
            self._set(track_id, status="done", progress=100, error="",
                      artifacts={"final": mix.get("final", ""),
                                 "vocal_mixed": mix.get("vocal_only", ""),
                                 "lips": lips_info.get("path", "")})
            self._log(f"[{track_id}] done: {mix.get('final')}")
            return {"ok": True, "track": self.store.get(track_id), "detail": mix}
        except _TrackCancelled:
            with self._run_lock:
                pending = self._pending_delete.pop(track_id, None)
            self._log(f"[{track_id}] 已按用户要求取消"
                      f"（{'删除文件' if pending else '保留文件'}）")
            if pending is not None:
                track_now = self.store.get(track_id) or {}
                if pending:
                    for value in (track_now.get("artifacts") or {}).values():
                        try:
                            if value:
                                self._trash_path(Path(str(value)))
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        self._trash_path(self.work_dir / "tracks" / str(track_id))
                    except Exception:  # noqa: BLE001
                        pass
                self.store.remove(track_id)
            else:
                self._set(track_id, status="cancelled", progress=0, error="用户取消了这一首")
            return {"ok": False, "cancelled": True,
                    "deleted": pending is not None}
        except Exception as exc:  # noqa: BLE001
            self._log(f"[{track_id}] crashed: {type(exc).__name__}: {exc}")
            self._log(f"[{track_id}] traceback:\n{traceback.format_exc().rstrip()}")


            self._set(track_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            with self._run_lock:
                self._busy.discard(track_id)
                self._cancelled.discard(track_id)
                self._pending_delete.pop(track_id, None)

    # ---------------- recording ----------------
    # ---------------- splitting ----------------
    def split_recording(self, wav_path: Path, *, min_song_s: float = 25.0,
                        silence_s: float = 2.0,
                        title_marks: list[dict] | None = None) -> dict:
        out_dir = self.work_dir / "splits" / time.strftime("%Y%m%d_%H%M%S")
        job = {"input": str(wav_path), "out_dir": str(out_dir),
               "min_song_s": min_song_s, "silence_s": silence_s,
               "title_marks": title_marks or []}
        result = runtime.run_worker(Path(self.paths["synth_python"]),
                                    self.workers / "split_worker.py", job,
                                    cwd=self.work_dir, timeout=600,
                                    log_path=self.log_path)
        if result.get("ok"):
            result["tracks"] = [
                dict(item, track_id=self.store.add(
                    title=item.get("title") or f"歌曲 {item['index']}",
                    artist=item.get("artist") or "",
                    source=item.get("path"),
                    start_s=item.get("start_s"),
                    end_s=item.get("end_s"),
                    duration_s=item.get("duration_s"),
                )["id"])
                for item in result.get("tracks", [])
            ]
        return result
