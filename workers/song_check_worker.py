"""判断一首歌能不能拿来翻唱：是不是纯音乐、有没有完整放完、有没有声音。

规则（全部可解释，不靠猜）：

1) 有没有人声 —— 看分离出来的人声轨相对原曲的电平。这是开源社区通用
   做法（UVR / audio-separator 的 vocal 轨能量阈值）：真正的歌人声轨和
   原曲只差几个 dB；纯音乐的人声轨只剩模型残渣，通常低 30 dB 以上。
   同时要求「人声活跃帧占比」也低才算纯音乐，两个条件都满足才下结论，
   避免把伴奏很密的歌误判。

2) 有没有完整放完 —— 优先用播放器自己报的数（SMTC / 她那边拿到的媒体
   信息）：整首时长 expected_s、开始录时的进度 start_position_s、收尾时
   的进度 end_position_s。从中间开始录、或者没放到结尾就停了，都能直接
   看出来。播放器不提供这些数（比如老版本网易云）时，只能确认「缺头」
   这一类，其余情况标成「未确认」放行——宁可让她唱，也不凭猜测拦歌。

3) 有没有声音 —— 整首 p90 电平太低就是空录音（采集到静音）。
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def _levels(path: str, hop_s: float = 0.05, block_s: float = 20.0) -> dict:
    """按块读音频，返回帧级 dB、整体 RMS；不整首读进内存。"""
    import numpy as np
    import soundfile as sf

    info = sf.info(path)
    rate = int(info.samplerate)
    hop = max(1, int(rate * hop_s))
    frames: list = []
    tail = np.empty(0, dtype=np.float64)
    total_sq = 0.0
    total_n = 0
    for block in sf.blocks(path, blocksize=max(hop, int(rate * block_s)),
                           overlap=0, dtype="float32", always_2d=True):
        mono = block.mean(axis=1).astype(np.float64)
        total_sq += float((mono ** 2).sum())
        total_n += int(mono.size)
        if tail.size:
            mono = np.concatenate([tail, mono])
        n = mono.size // hop
        if n:
            seg = mono[:n * hop].reshape(n, hop)
            rms = np.sqrt((seg ** 2).mean(axis=1))
            frames.append(20.0 * np.log10(np.maximum(rms, 1e-9)))
        tail = mono[n * hop:]
    db = np.concatenate(frames) if frames else np.empty(0, dtype=np.float64)
    return {"rate": rate, "db": db,
            "rms": (total_sq / total_n) ** 0.5 if total_n else 0.0,
            "duration_s": total_n / float(rate) if rate else 0.0}


def _vocal_ratio(mix: dict, vocals: dict) -> dict:
    import numpy as np

    m_db, v_db = mix["db"], vocals["db"]
    n = min(m_db.size, v_db.size)
    out = {"vocal_rel_db": None, "vocal_med_db": None, "vocal_active_frac": None}
    if n <= 0:
        return out
    m_db, v_db = m_db[:n], v_db[:n]
    loud = float(np.percentile(m_db, 90))
    active = m_db > loud - 25.0
    if not active.any():
        active = np.ones(n, dtype=bool)
    rel = v_db - m_db
    out["vocal_rel_db"] = round(20.0 * np.log10(
        max(vocals["rms"], 1e-9) / max(mix["rms"], 1e-9)), 2)
    out["vocal_med_db"] = round(float(np.median(rel[active])), 2)
    out["vocal_active_frac"] = round(float(np.mean(rel[active] > -15.0)), 3)
    return out


def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    result: dict = {"ok": False, "status": "unknown", "singable": True,
                    "has_vocals": None, "complete": None, "reason": ""}
    try:
        import numpy as np  # noqa: F401 - 缺依赖时尽早报错

        src = str(job.get("input") or "")
        if not src or not Path(src).is_file():
            raise FileNotFoundError(f"找不到音频：{src}")
        vocal_db = float(job.get("vocal_db") or -20.0)
        vocal_frac = float(job.get("vocal_active_frac") or 0.15)
        min_song_s = float(job.get("min_song_s") or 25.0)
        start_tol_s = float(job.get("start_tol_s") or 5.0)
        silent_db = float(job.get("silent_db") or -50.0)

        mix = _levels(src)
        duration = float(mix["duration_s"])
        loud = float(np.percentile(mix["db"], 90)) if mix["db"].size else -180.0
        detail: dict = {"duration_s": round(duration, 2), "loud_db": round(loud, 1)}

        # ---- 1) 空录音 ----
        if loud <= silent_db:
            result.update({"ok": True, "status": "silent", "singable": False,
                           "complete": False, "has_vocals": False,
                           "reason": "这段录音几乎没声音（采集到的是静音），换一首重新听"})
            result["detail"] = detail
            out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            return 0

        # ---- 2) 纯音乐 ----
        vocals_path = str(job.get("vocals") or "")
        has_vocals = None
        if vocals_path and Path(vocals_path).is_file():
            try:
                vocals = _levels(vocals_path)
                ratio = _vocal_ratio(mix, vocals)
                detail.update(ratio)
                if ratio["vocal_med_db"] is not None:
                    has_vocals = not (ratio["vocal_med_db"] < vocal_db
                                      and ratio["vocal_active_frac"] < vocal_frac)
            except Exception as exc:  # noqa: BLE001 - 读不了人声轨就当没结论
                detail["vocal_error"] = f"{type(exc).__name__}: {exc}"

        # ---- 3) 完整性 ----
        expected = float(job.get("expected_s") or 0.0)
        # 播放器报的时长很准（8s/5%）；联网查来的唱片时长可能和 Live/翻唱版本
        # 差十几秒，容差放宽，宁可放过也别把真歌拦了。
        from_lookup = str(job.get("expected_from") or "") == "lookup"
        start_pos = job.get("start_position_s")
        end_pos = job.get("end_position_s")
        try:
            start_pos = float(start_pos) if start_pos is not None else -1.0
        except (TypeError, ValueError):
            start_pos = -1.0
        try:
            end_pos = float(end_pos) if end_pos is not None else -1.0
        except (TypeError, ValueError):
            end_pos = -1.0
        detail.update({"expected_s": round(expected, 2),
                       "start_position_s": round(start_pos, 2),
                       "end_position_s": round(end_pos, 2)})

        complete = None
        incomplete_reason = ""
        if start_pos > start_tol_s:
            complete = False
            incomplete_reason = (f"这首歌是从第 {start_pos:.0f} 秒开始录的，"
                                 f"前面 {start_pos:.0f} 秒没听到，缺了开头")
        elif expected > 0.0:
            tol = (max(12.0, expected * 0.10) if from_lookup
                   else max(8.0, expected * 0.05))
            if end_pos >= 0.0:
                complete = end_pos >= expected - tol
                if not complete:
                    incomplete_reason = (f"只放到 {end_pos:.0f} 秒就停了，"
                                         f"这首歌一共 {expected:.0f} 秒")
            else:
                complete = duration >= expected - tol
                if not complete:
                    incomplete_reason = (f"录音只有 {duration:.0f} 秒，"
                                         f"这首歌一共 {expected:.0f} 秒")
        elif duration < min_song_s:
            complete = False
            incomplete_reason = f"录音只有 {duration:.0f} 秒，太短了，不像完整的一首歌"

        # ---- 结论 ----
        if has_vocals is False:
            result.update({"status": "instrumental", "singable": False,
                           "has_vocals": False, "complete": complete,
                           "reason": "这首是纯音乐，没有人声，翻唱不了"})
        elif complete is False:
            result.update({"status": "incomplete", "singable": False,
                           "has_vocals": has_vocals, "complete": False,
                           "reason": incomplete_reason + "；完整听一遍再唱"})
        else:
            note = ""
            if complete is None:
                note = "播放器没报整首时长，没法确认是否完整"
            result.update({"status": "ok", "singable": True, "has_vocals": has_vocals,
                           "complete": complete, "reason": note})
        result["ok"] = True
        result["detail"] = detail
    except Exception as exc:  # noqa: BLE001 - worker 永远要写结果
        result.update({"ok": False, "singable": True,
                       "error": f"{type(exc).__name__}: {exc}",
                       "trace": traceback.format_exc()[-1500:]})
    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
