"""离线编排 worker（独立进程，``python -I``）。

用法：
    python -I choreo_worker.py <job.json> <result.json>

job（由 ``__init__.py`` 组装）：
    audio       成品混音路径（节拍/段落/能量）
    stem        干净人声路径（高音/颤音；可为空）
    duration    曲长（秒）
    cues        逐字口型轨（lips.json 的 cues）
    env         人声包络（可选，分析用不到时忽略）
    hop         包络步长（默认 0.05）
    lines       [{"t0": 秒, "text": "…"}]，来自官方/感知歌词时间轴
    line_moods  [[t0, v, a, d, conf], …]，由 lipsync.line_mood 算好
    track       {"id","title","artist","duration_s"}
    seed        整数（缺省由 director 从 track_id 派生）
    preset      ballad|pop|rock|edm|cute|ancient|jazz|dark（可选，缺省自动挑）
    llm         {"enabled","endpoint","model","timeout_s","force"}（可选，语义调用参数）
    out         生成的 show.json 路径

result：
    {"ok": true, "path": "...", "seed": ..., "preset": ...,
     "input_hash": "...", "lines": N, "sections": [...],
     "schema": {"ok": true, "errors": [], "warnings": []}, "seconds": 12.3}

失败时也会写 result 文件（``ok: false`` + ``error`` + ``trace``），
调用方（runtime.run_worker）据此判定失败，绝不把半成品当成功。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path


def _write(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _mtime(path: str) -> float:
    try:
        return float(os.path.getmtime(path)) if path else 0.0
    except OSError:
        return 0.0


def main() -> int:
    job_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    job = json.loads(job_path.read_text(encoding="utf-8"))
    result: dict = {"ok": False, "state": "running", "step": "start"}
    started = time.time()

    def progress(step: str, **extra) -> None:
        result.update({"state": "running", "step": step, **extra})
        try:
            _write(out_path, result)
        except OSError:
            pass

    try:
        plugin_root = Path(__file__).resolve().parent.parent
        if str(plugin_root) not in sys.path:
            sys.path.insert(0, str(plugin_root))
        from choreo import analyze as analyze_mod
        from choreo import director, presets, schema, semantics

        audio = str(job.get("audio") or "")
        stem = str(job.get("stem") or "")
        if not audio or not Path(audio).is_file():
            raise RuntimeError(f"成品音频不存在：{audio!r}")
        duration = float(job.get("duration") or 0.0)
        lines = list(job.get("lines") or [])
        cues = list(job.get("cues") or [])
        line_moods = list(job.get("line_moods") or [])
        track = dict(job.get("track") or {})
        if not duration:
            duration = float(track.get("duration_s") or 0.0)
        if not duration:
            raise RuntimeError("缺少曲长")

        seed = job.get("seed")
        seed = int(seed) if seed is not None else None
        preset_name = str(job.get("preset") or "").strip().lower() or None
        if preset_name and preset_name not in presets.PRESETS:
            preset_name = None

        progress("analyze")
        analysis = analyze_mod.analyze(
            audio, stem=stem, duration=duration, lines=lines, cues=cues,
            want_vocal=bool(job.get("want_vocal", True)))
        analysis["duration"] = duration
        analysis["music_duration"] = duration

        if seed is None:
            seed = director.default_seed(track)
        seed = int(seed) & 0x7FFFFFFF

        out = Path(str(job.get("out") or "")).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

        # ---- 歌词语义（本地 Ollama；失败/超时一律兜底，绝不抛）----
        llm = dict(job.get("llm") or {})
        progress("semantics")
        if llm.get("enabled") is False:
            sem = {"ok": False, "source": "fallback", "tag": "fallback",
                   "reason": "LLM 已禁用（job.llm.enabled=false）",
                   "theme": "", "imagery": [], "palette_hint": "", "lines": [],
                   "cache_hit": False,
                   "metrics": {"endpoint": None, "model": None}}
        else:
            sem = semantics.analyze(
                track, lines,
                endpoint=llm.get("endpoint"),
                model=llm.get("model"),
                timeout=llm.get("timeout_s"),
                cache_path=out.parent / semantics.CACHE_NAME,
                force=bool(llm.get("force")))

        theme_hint = " ".join([str(sem.get("theme") or ""),
                               str(track.get("title") or "")])
        chosen = presets.get(preset_name or presets.auto_pick(
            analysis.get("bpm", 100.0), analysis.get("energy_mean", 0.5),
            analysis.get("energy_var", 0.0), theme=theme_hint)).get("key", "pop")
        lyrics_key = str(job.get("lyrics_key") or "")
        input_hash = director.input_fingerprint(
            audio=audio, stem=stem, lyrics_key=lyrics_key, cues=len(cues),
            seed=seed, preset=chosen, audio_mtime=_mtime(audio),
            stem_mtime=_mtime(stem), semantics=str(sem.get("tag") or ""))

        progress("direct")
        show = director.build(
            track, analysis, lines, cues, line_moods, seed=seed,
            preset_name=chosen, input_hash=input_hash, semantics=sem)
        show["duration"] = round(duration, 3)

        progress("validate")
        report = schema.validate(show)
        if not report["ok"]:
            raise RuntimeError("show.json 校验失败：" + "；".join(report["errors"][:6]))

        _write(out, show)

        result = {
            "ok": True,
            "state": "done",
            "step": "written",
            "path": str(out),
            "seed": seed,
            "preset": chosen,
            "input_hash": input_hash,
            "lines": len(show.get("lines") or []),
            "sections": show.get("music", {}).get("sections") or [],
            "semantics": {
                "source": sem.get("source"),
                "tag": sem.get("tag"),
                "reason": sem.get("reason"),
                "cache_hit": sem.get("cache_hit"),
                "theme": sem.get("theme"),
                "imagery": sem.get("imagery"),
                "metrics": sem.get("metrics"),
            },
            "typeface": (show.get("style") or {}).get("typeface"),
            "bpm": show.get("music", {}).get("bpm"),
            "duration": show.get("duration"),
            "schema": {"ok": True,
                       "errors": [],
                       "warnings": report.get("warnings") or []},
            "seconds": round(time.time() - started, 2),
        }
        _write(out_path, result)
        return 0
    except Exception as exc:  # noqa: BLE001 - worker 必须写失败结果
        result = {
            "ok": False,
            "state": "failed",
            "step": result.get("step") or "unknown",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-4000:],
            "seconds": round(time.time() - started, 2),
        }
        try:
            _write(out_path, result)
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
