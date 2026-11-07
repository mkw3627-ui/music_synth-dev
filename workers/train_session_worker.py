"""DDSP-SVC 6.3 训练会话 worker（supervisor，长驻进程）。

完整实现「插件内训练新音色」后端流程（train_integration_plan_20261103.md
第 1 节 1)–6) 与第 2 节契约）。本文件只被 flash 的插件入口以子进程方式调用：:

    <synth_python> train_session_worker.py <job.json> <result.json>

job（由插件 _train_launch 生成）= {
    repo, python, run_dir, name, init_from, base_ckpt, has_accompaniment,
    preview_src, milestones, train_runs_dir, log
}
训练起点固定为内置底模 6.3（``pretrain/reflow_base/model_0.pt``），
``init_from``/``base_ckpt`` 只用来传这个路径，旧面板的 auto/scratch 已不再生效。
注意：job 不含 inputs；素材已由插件复制进 run_dir/src/ 并写进
state["data"]["inputs"]。本 worker 只读 state.json + control.json，
写 state.json（原子写 tmp+os.replace），长驻直到 finished/failed 才退出。

阶段：
  1) 整理数据（src → 44.1k 单声道 → 响度归一 → slicer 切静音 2–15s →
     丢弃过短/削波/近静音 → 留 val ≥2 条约 5%）；总时长 <60s 拒绝。
  2) preprocess.py 提特征（独立 run 数据目录，不碰 repo/data）。
  3) 自动定参数（写 state.params + reasons，并生成 run_config.yaml）。
  4) 里程碑训练 → 到点停训 → 用该 ckpt 对预听片段推理 → awaiting_choice。
  5) 建议判定（只给建议，不替用户决定）。
  6) continue 续训下一里程碑 / stop 停。保留由 train_finalize_worker 完成。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from base_ckpt import normalize_checkpoint  # noqa: E402

MILESTONES_DEFAULT = [1000, 2000, 4000, 8000, 12000, 16000]
MIN_TOTAL_SEC = 60.0        # 总时长 <60s 拒绝
WARN_TOTAL_SEC = 180.0      # 总时长 <180s 警告（不拒绝）
CLIP_MIN_SEC = 2.0
CLIP_MAX_SEC = 15.0
# 经验值：素材时长上限对应的自动训练步数上限（<3 分钟 8000 步，<10 分钟 16000 步，
# ≥10 分钟 32000 步）。每一档都必须给出上限：cap 留成 None 会让 while 去和 None 比大小。
AUTO_STEP_CAP_SHORT_SEC = 180.0
AUTO_STEP_CAP_SHORT = 8000
AUTO_STEP_CAP_MID_SEC = 600.0
AUTO_STEP_CAP_MID = 16000
AUTO_STEP_CAP_LONG = 32000
# 全自动防过拟合判定（经验值）：连续 2 个里程碑比最佳 val_loss 差 3% 以上 → 停；
# 连续 2 个里程碑相对最佳改善都不足 2% → 停。
AUTO_WORSE_RATIO = 0.03
AUTO_IMPROVE_RATIO = 0.02
AUTO_ROUNDS = 2
LOUDNESS_DBFS = -23.0       # 响度归一目标（RMS 近似）
_ATOMIC_TMP = ".state.tmp"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _log(msg: str, log_path: Path | None) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if log_path is not None:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def _atomic_write(path: Path, obj: dict) -> None:
    tmp = path.with_name(_ATOMIC_TMP)
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _subprocess_env(repo: Path) -> dict:
    """复用现有 worker 约定：清污染变量，把仓库根补进 PYTHONPATH。"""
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(key, None)
    env["PYTHONPATH"] = str(repo)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    return env


def _run(cmd: list[str], cwd: Path, repo: Path, log_path: Path | None,
         timeout: float | None,
         on_line: "Callable[[str], None] | None" = None,
         stop_check: "Callable[[], bool] | None" = None) -> tuple[int, list[str], float]:
    """运行子进程，返回 (returncode, tail_lines, elapsed)。stdout/stderr 逐行进 supervisor.log。

    ``on_line`` 可选：每读到一行就回调一次（train_session 用它解析实时进度）；
    回调抛出的异常一律吞掉，不能让日志解析问题中断训练进程的读取。
    """
    started = time.time()
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=_subprocess_env(repo),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.stdout is not None
    tail: list[str] = []
    stopped = False
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            tail.append(line)
            tail = tail[-600:]
            if log_path is not None:
                try:
                    with open(log_path, "a", encoding="utf-8") as f:
                        f.write(line + "\n")
                except OSError:
                    pass
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:  # noqa: BLE001 - 进度回调不得影响训练
                    pass
            if stop_check is not None:
                try:
                    if stop_check():
                        stopped = True
                        break
                except Exception:  # noqa: BLE001 - 停止检查不得影响主循环
                    pass
    finally:
        if stopped:
            # 手动停止：连 DataLoader 子进程一起结束，避免显存残留。
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   capture_output=True, timeout=20,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=30)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except OSError:
                    pass
            code = -999
        else:
            code = proc.wait(timeout=timeout) if timeout else proc.wait()
    return code, tail, time.time() - started


# solver 每个 interval_log 打印一行（reflow/solver.py）：
#   epoch: 3 |  50/120 | <expdir> | batch/s: 2.13 | lr: 0.0005 | loss: 1.234 | time: .. | step: 400
_TRAIN_LINE_RE = re.compile(r"batch/s:\s*([0-9.]+).*?\bstep:\s*(\d+)")


def _parse_train_progress(line: str) -> dict | None:
    """从 solver 日志行解析 {step, batch_per_s, batch, num_batches}。"""
    m = _TRAIN_LINE_RE.search(line)
    if not m:
        return None
    try:
        rate = float(m.group(1))
        step = int(m.group(2))
    except ValueError:
        return None
    batch = total = None
    mm = re.search(r"\|\s*(\d+)\s*/\s*(\d+)\s*\|", line)
    if mm:
        batch, total = int(mm.group(1)), int(mm.group(2))
    return {"step": step, "batch_per_s": rate, "batch": batch, "num_batches": total}



def _read_ckpt_steps(expdir: Path) -> list[int]:
    if not expdir.is_dir():
        return []
    return sorted(int(m.group(1)) for p in expdir.glob("model_*.pt")
                  if (m := re.search(r"model_(\d+)\.pt$", p.name)))


def _is_oom_text(lines) -> bool:
    """True when a training/preprocess log tail looks like a GPU OOM."""
    text = "\n".join(str(x) for x in (lines or [])).lower()
    return any(key in text for key in (
        "out of memory", "allocation failed", "not enough memory",
        "cuda error", "cublas_status_alloc_failed", "oom",
    ))


def _train_device(state: dict, params: dict | None = None) -> str:
    value = str(state.get("device")
                or (params or {}).get("device")
                or (state.get("params") or {}).get("device")
                or "cuda")
    return "cpu" if value.strip().lower() == "cpu" else "cuda"


def _parse_val_loss(tail: list[str]) -> float | None:
    """从训练日志解析最近一次 validation loss。

    solver 打印 `<validation>` 后一行 `loss: X.`（详见 solver.py test()）。
    只认 `<validation>` 块里的 loss，避免误抓普通 train loss。
    """
    for i in range(len(tail) - 1, -1, -1):
        line = tail[i].strip()
        if "<validation>" in line:
            for j in range(i + 1, min(i + 6, len(tail))):
                # solver 打印 `loss: 1.010.`（格式化后缀点）；只取数字和一个小数点
                m = re.search(r"loss:\s*(\d+\.\d+)", tail[j].strip())
                if m:
                    return float(m.group(1))
    return None


# ---------------------------------------------------------------------------
# 阶段 1：整理数据 + 切片（slicer 在 repo 根，用 repo 的 slicer.py）
# ---------------------------------------------------------------------------
_SLICE_PY = r'''
import json, sys
from pathlib import Path
import numpy as np
import librosa
import soundfile as sf
from slicer import Slicer

def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    train_audio = Path(job["train_audio"]); val_audio = Path(job["val_audio"])
    train_audio.mkdir(parents=True, exist_ok=True)
    val_audio.mkdir(parents=True, exist_ok=True)
    clip_min = float(job["clip_min"]); clip_max = float(job["clip_max"])
    loudness = float(job["loudness"])
    dropped = []
    clips_train = clips_val = 0
    total_sec = 0.0
    candidates = []
    for p in job["inputs"]:
        try:
            audio, sr = librosa.load(str(p), sr=44100, mono=True)
        except Exception as e:
            dropped.append({"file": str(p), "reason": f"读取失败: {e}"})
            continue
        if audio.size == 0:
            dropped.append({"file": str(p), "reason": "空音频"})
            continue
        rms = float(np.sqrt((audio ** 2).mean())) + 1e-9
        audio = audio * (10 ** (loudness / 20) / rms)
        peak = float(np.max(np.abs(audio)))
        if peak > 0.99:
            audio = audio * (0.99 / peak)
        slicer = Slicer(sr=44100, threshold=-40., min_length=int(clip_min * 44100),
                        min_interval=300, hop_size=20, max_sil_kept=2000)
        chunks = slicer.slice(audio)
        for k in sorted(chunks, key=lambda x: int(x)):
            c = chunks[k]
            if c["slice"]:
                continue  # 静音段
            a, b = c["split_time"].split(",")
            seg = audio[int(a):int(b)]
            dur = len(seg) / 44100
            if dur < clip_min:
                dropped.append({"file": str(p), "reason": f"片段过短 {dur:.2f}s"})
                continue
            while dur > clip_max:
                candidates.append(seg[:int(clip_max * 44100)].copy())
                seg = seg[int(clip_max * 44100):]
                dur = len(seg) / 44100
            candidates.append(seg.copy())
    if not candidates:
        raise RuntimeError("切片后没有任何可用片段")
    # 时长按切片结果统计（total_sec 在后面写文件时才累加，之前恒为 0）
    total_sec = sum(len(s) / 44100 for s in candidates)
    good = []
    for seg in candidates:
        r = float(np.sqrt((seg ** 2).mean()))
        pk = float(np.max(np.abs(seg)))
        if r < 1e-5:
            dropped.append({"file": "-", "reason": "近静音"})
            continue
        if pk > 0.999:
            dropped.append({"file": "-", "reason": "削波"})
            continue
        good.append(seg)
    if not good:
        raise RuntimeError("质检后没有任何可用片段")
    # 验证集太小时 val_loss 抖动，判不了过拟合：素材 ≥120s 时至少 3 段、约 10%。
    val_ratio = 0.05 if total_sec < 120.0 else 0.10
    n_val = max(2, int(len(good) * val_ratio))
    if total_sec >= 120.0:
        n_val = max(n_val, 3)
    n_val = min(n_val, len(good) - 1)
    idx = np.random.permutation(len(good))
    val_idx = set(idx[:n_val].tolist())
    for i, seg in enumerate(good):
        if i in val_idx:
            outdir = val_audio; clips_val += 1
        else:
            outdir = train_audio; clips_train += 1
        sf.write(str(outdir / f"clip_{i:04d}.wav"), seg, 44100)
    Path(out).write_text(json.dumps({"ok": True, "data": {
        "clips_train": clips_train, "clips_val": clips_val,
        "total_sec": round(total_sec, 2), "dropped": dropped,
        "val_ratio": val_ratio, "val_min": 3 if total_sec >= 120.0 else 2,
        "val_segments": clips_val,
        "val_reason": ((
            "素材 ≥120s：验证集至少 3 段、约 10%（减小 val_loss 抖动）；"
            f"本次 {clips_val} 段 = 片段总数 {len(good)} × {val_ratio:.0%}"
            + ("" if clips_val >= 3 else "（片段太短、可用段不足 3，已取上限）"))
            if total_sec >= 120.0 else
            "素材 <120s：验证集按 5%、至少 2 段（素材过少，不能再切）")}},
        ensure_ascii=False), encoding="utf-8")

main()
'''


def _prepare_data(state: dict, job: dict, run_dir: Path, python: Path, repo: Path,
                  log_path: Path, log) -> None:
    """阶段 1：整理数据。素材已在 state['data']['inputs']（run_dir/src/ 内）。"""
    inputs = state.get("data", {}).get("inputs") or []
    if not inputs:
        raise RuntimeError("state.json 里没有素材（data.inputs 为空）")

    has_accomp = bool((state.get("data", {}).get("has_accompaniment"))
                      or job.get("has_accompaniment"))
    if has_accomp:
        raise RuntimeError(
            "素材含伴奏：当前训练后端未接入分离环境（separate_worker 运行在独立 sep venv）。"
            "请先单独分离人声，或取消勾选「含伴奏」直接用干声训练。")

    train_audio = run_dir / "data" / "train" / "audio"
    val_audio = run_dir / "data" / "val" / "audio"
    train_audio.mkdir(parents=True, exist_ok=True)
    val_audio.mkdir(parents=True, exist_ok=True)

    slice_script = run_dir / "data" / "_slice.py"
    slice_script.write_text(_SLICE_PY, encoding="utf-8")
    job_path = run_dir / "data" / "_slice_job.json"
    result_path = run_dir / "data" / "_slice_result.json"
    job_path.write_text(json.dumps({
        "inputs": [str(p) for p in inputs],
        "train_audio": str(train_audio), "val_audio": str(val_audio),
        "clip_min": CLIP_MIN_SEC, "clip_max": CLIP_MAX_SEC, "loudness": LOUDNESS_DBFS,
    }, ensure_ascii=False), encoding="utf-8")

    code, tail, _ = _run([str(python), "-X", "utf8", str(slice_script),
                             str(job_path), str(result_path)], repo, repo, log_path, timeout=None)
    if code != 0 or not result_path.is_file():
        raise RuntimeError("切片任务失败：\n" + "\n".join(tail[-40:]))
    res = _read_json(result_path)
    if not res.get("ok"):
        raise RuntimeError("切片任务返回错误：" + str(res.get("error")))

    data = res.get("data", {})
    total_sec = float(data.get("total_sec") or 0.0)
    state["data"]["inputs"] = [str(p) for p in inputs]
    state["data"]["clips_train"] = int(data.get("clips_train") or 0)
    state["data"]["clips_val"] = int(data.get("clips_val") or 0)
    state["data"]["total_sec"] = round(total_sec, 2)
    state["data"]["dropped"] = data.get("dropped") or []
    state["data"]["val_ratio"] = data.get("val_ratio")
    state["data"]["val_reason"] = data.get("val_reason") or ""
    log(f"数据整理完成：train {state['data']['clips_train']} 段 / val "
        f"{state['data']['clips_val']} 段 / 总时长 {total_sec:.1f}s，"
        f"丢弃 {len(state['data']['dropped'])} 段")

    if total_sec < MIN_TOTAL_SEC:
        raise RuntimeError(
            f"素材有效时长仅 {total_sec:.1f}s，低于最低要求 {MIN_TOTAL_SEC:.0f}s，请提供更长素材")
    if total_sec < WARN_TOTAL_SEC:
        log(f"警告：素材总时长 {total_sec:.1f}s < {WARN_TOTAL_SEC:.0f}s，建议提供更长素材")


# ---------------------------------------------------------------------------
# 阶段 3：自动定参数
# ---------------------------------------------------------------------------
def _free_vram(python: Path) -> float | None:
    code, tail, _ = _run([str(python), "-c",
                          "import torch;print(torch.cuda.mem_get_info()[1]/1024**3 if torch.cuda.is_available() else 'cpu')"],
                      Path.cwd(), Path.cwd(), None, timeout=180)
    if code != 0:
        return None
    try:
        return float([line for line in tail if line.strip()][-1])
    except (ValueError, IndexError):
        return None


def _auto_params(state: dict, job: dict, python: Path) -> dict:
    clips_train = int(state["data"]["clips_train"])
    total_sec = float(state["data"]["total_sec"])

    try:
        accum_steps = max(1, min(16, int(job.get("train_accum") or 1)))
    except (TypeError, ValueError):
        accum_steps = 1
    try:
        forced_batch = int(job.get("train_batch") or 0)
    except (TypeError, ValueError):
        forced_batch = 0
    if forced_batch > 0:
        batch = max(1, min(64, forced_batch))
        reason = f"高级设置指定 batch_size={batch}（显存不够会自动 OOM 失败，请自行回调）"
    else:
        free_vram = _free_vram(python)
        if free_vram is None:
            batch = 8
            reason = "无法读取空闲显存，取保守值 8"
        else:
            # 对齐 barbara 整合包的吞吐：约 0.7GB/batch（16GB 卡通常到 16）。
            # 底模更大时按层数/通道数放大每 batch 的显存估计，避免大底模直接 OOM。
            arch = state.get("arch") or {}
            try:
                model_scale = max(
                    1.0,
                    (float(arch.get("n_chans") or 1024) / 1024.0)
                    * (float(arch.get("n_layers") or 6) / 6.0))
            except (TypeError, ValueError):
                model_scale = 1.0
            per_batch = 0.7 * model_scale
            batch = max(1, int(free_vram // per_batch))
            reason = (f"空闲显存 {free_vram:.1f}GB，按 ~{per_batch:.1f}GB/batch 估算"
                      f"（底模 {arch.get('n_layers', '?')} 层 / "
                      f"{arch.get('n_chans', '?')} 通道）")
        p = 1
        while p * 2 <= batch:
            p *= 2
        batch = max(1, min(p, clips_train))
        reason += f"；取 2 的幂且不超过训练片段数 {clips_train}，最终 batch_size={batch}"

    try:
        duration = float(job.get("train_duration") or 2.0)
    except (TypeError, ValueError):
        duration = 2.0
    duration = max(1.0, min(10.0, duration))
    dur_reason = (f"高级设置：随机片段的长度 {duration:g}s"
                  f"（切片最短 {CLIP_MIN_SEC:g}s，取不超过最短片段的长度）")

    amp = str(job.get("train_amp") or "bf16")
    if amp not in {"fp16", "bf16", "fp32"}:
        amp = "bf16"
    use_pitch_aug = bool(job.get("train_pitch_aug", True))
    cache_fp16 = bool(job.get("train_cache_fp16", False))
    try:
        preview_step = int(job.get("preview_step") or 120)
    except (TypeError, ValueError):
        preview_step = 120
    preview_step = max(5, min(400, preview_step))

    milestones = job.get("milestones")
    if isinstance(milestones, (list, tuple)):
        clean: list[int] = []
        for m in milestones:
            try:
                v = int(m)
            except (TypeError, ValueError):
                continue          # job 里可能带 null / 空串，跳过而不是让整次训练崩掉
            if v > 0:
                clean.append(v)
        milestones = sorted(set(clean))
    else:
        milestones = list(MILESTONES_DEFAULT)
    if not milestones:
        milestones = list(MILESTONES_DEFAULT)

    # 全自动：里程碑一直排到步数上限，期间不停下来等用户。
    auto = bool(job.get("auto"))
    params_extra: dict = {}
    cap = None
    if auto:
        if total_sec < AUTO_STEP_CAP_SHORT_SEC:
            cap, cap_rule = AUTO_STEP_CAP_SHORT, f"素材 <{AUTO_STEP_CAP_SHORT_SEC:.0f}s"
        elif total_sec < AUTO_STEP_CAP_MID_SEC:
            cap, cap_rule = AUTO_STEP_CAP_MID, f"素材 <{AUTO_STEP_CAP_MID_SEC:.0f}s"
        else:
            cap, cap_rule = AUTO_STEP_CAP_LONG, f"素材 ≥{AUTO_STEP_CAP_MID_SEC:.0f}s"
        if not cap:               # 防御：分支漏配也不让训练以 TypeError 崩掉
            cap, cap_rule = AUTO_STEP_CAP_MID, "默认上限"
        step = max(milestones[-1], milestones[0] or 1000)
        while step < cap:
            step = min(step * 2, cap)
            milestones.append(step)
        milestones = sorted(m for m in set(milestones) if m <= cap)
        params_extra["cap_reason"] = (
            f"全自动步数上限 {cap}（{cap_rule}；经验值，非放开的阈值）")
        params_extra["auto_cap"] = cap
        params_extra["auto"] = True

    interval = milestones[0]
    iv_reason = (f"interval_val=interval_force_save={interval}（=首个里程碑，"
                 f"保证里程碑 ckpt 不被 solver 删除）")

    # 训练起点固定为内置底模 6.3（DDSP-SVC 预训练 RectifiedFlow 权重），
    # 用户不再选择从零/续训/某个已有声音。
    init_from = str(job.get("base_ckpt") or job.get("init_from") or "").strip()
    if not init_from or init_from in ("auto", "scratch"):
        init_from = "固定起点"
    init_reason = "从固定起点开始"

    # 从固定起点起步固定用更低学习率（对齐 barbara 整合包的 1e-4），
    # 避免把起点模型的先验冲掉。
    lr = 0.0001
    lr_reason = "从固定起点起步 → lr=1e-4"

    # 训练设备：显存不足/读不到显存时自动退回 CPU；CPU 只能用 fp32、batch=1。
    free_vram_now = _free_vram(python)
    device = "cuda"
    if free_vram_now is None or free_vram_now < 2.5:
        device = "cpu"
        batch = 1
        amp = "fp32"
        reason = ("空闲显存不足或不可用：自动改用 CPU 训练"
                  "（batch=1、fp32，速度会慢很多）")

    return {
        "batch_size": batch, "accum_steps": accum_steps,
        "duration": duration, "lr": lr, "amp": amp,
        "device": device,
        "preview_step": preview_step, "use_pitch_aug": use_pitch_aug,
        "cache_fp16": cache_fp16,
        "init_from": init_from, "milestones": milestones,
        "interval_val": interval, "interval_force_save": interval,
        "reasons": {
            "batch_size": reason,
            "accum_steps": (f"梯度累积 {accum_steps}（等效 batch={batch * accum_steps}）"
                            if accum_steps > 1 else "未启用梯度累积"),
            "duration": dur_reason,
            "interval": iv_reason,
            "init_from": init_reason,
            "lr": lr_reason,
            "amp": f"{amp}（高级设置；RTX 5070 Ti 支持 bf16，比 fp16 更稳）",
            "preview_step": f"试听推理 {preview_step} 步（高级设置）",
            "use_pitch_aug": ("开" if use_pitch_aug else "关") + "（高级设置：音高随机增广）",
            "cache_fp16": ("开" if cache_fp16 else "关") + "（高级设置：特征缓存半精度）",
            **params_extra,
        },
    }


# ---------------------------------------------------------------------------
# 配置生成（基于 repo configs/reflow.yaml 覆盖本 run 字段）
# ---------------------------------------------------------------------------
def _load_reflow(repo: Path) -> dict:
    import yaml
    return yaml.safe_load((repo / "configs" / "reflow.yaml").read_text(encoding="utf-8")) or {}


def _dump_yaml(path: Path, obj: dict) -> None:
    import yaml
    path.write_text(yaml.safe_dump(obj, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _build_config(state: dict, params: dict, run_dir: Path, repo: Path) -> Path:
    cfg = _load_reflow(repo)
    # 底模自描述结构：训练前由 base_ckpt.py 从权重形状推断出来，写进 state["arch"]。
    # 这里把 reflow.yaml 的默认结构覆盖成底模实际的结构，避免架构不匹配。
    arch = state.get("arch") or {}
    if arch:
        dcfg = cfg.setdefault("data", {})
        for key in ("sampling_rate", "block_size"):
            if arch.get(key):
                dcfg[key] = int(arch[key])
        if arch.get("encoder"):
            dcfg["encoder"] = arch["encoder"]
            dcfg["encoder_hop_size"] = int(arch.get("encoder_hop_size") or 160)
            dcfg["encoder_out_channels"] = int(arch.get("encoder_out_channels") or 768)
        mcfg = cfg.setdefault("model", {})
        for key in ("win_length", "n_layers", "n_chans", "n_aux_layers", "n_aux_chans"):
            if arch.get(key):
                mcfg[key] = int(arch[key])
        mcfg["use_pitch_aug"] = bool(arch.get("use_pitch_aug", True))
        mcfg["n_spk"] = int(arch.get("n_spk") or 1)
    # 训练设备跟随自动定参/降级结果；CPU 时强制 fp32。
    train_device = str(state.get("device") or params.get("device") or "cuda")
    cfg["device"] = train_device
    if train_device == "cpu":
        cfg["train"]["amp_dtype"] = "fp32"
        cfg["train"]["cache_device"] = "cpu"
    cfg["data"]["train_path"] = str(run_dir / "data" / "train")
    cfg["data"]["valid_path"] = str(run_dir / "data" / "val")
    cfg["data"]["duration"] = float(params["duration"])
    cfg["train"]["batch_size"] = int(params["batch_size"])
    cfg["train"]["amp_dtype"] = params["amp"]
    cfg["train"]["lr"] = float(params.get("lr") or 0.0005)
    cfg["train"]["num_workers"] = 2
    cfg["train"]["interval_val"] = int(params["interval_val"])
    cfg["train"]["interval_force_save"] = int(params["interval_force_save"])
    # 不存优化器状态：检查点从 ~440MB 降到 ~220MB（底模也只有 220MB）。
    # 续训时缺 optimizer 仍能正常加载（logger/utils.load_model 会跳过）。
    cfg["train"]["save_opt"] = False
    # solver 循环只有 `epochs` 一个停机条件，且每个 epoch 恰好推进 num_batches
    # 步（num_batches = ceil(训练片段数 / batch_size)）。train_reflow.py 没有
    # "到第 N 步就停"的开关，因此按本里程碑把 epochs 设成"刚好越过 target"，
    # 让进程自己退出，之后 worker 从 expdir 读到最新 milestone ckpt 进入 previewing。
    clips = max(int(state.get("data", {}).get("clips_train") or 1), 1)
    batch = max(int(params["batch_size"]) or 1, 1)
    accum = max(1, int(params.get("accum_steps") or 1))
    cfg["train"]["accum_steps"] = accum
    # 一个 epoch 的 micro-batch 数；真正参数更新次数 = 除以累积步数
    micro_batches = max((clips + batch - 1) // batch, 1)
    steps_per_epoch = max(1, (micro_batches + accum - 1) // accum)
    target = int(state.get("next_milestone") or int(params["milestones"][0]))
    cfg["train"]["epochs"] = (target // steps_per_epoch) + 2
    # 防过拟合关键项显式落地，不依赖 yaml 默认值的隐式继承。
    use_pitch_aug = bool(params.get("use_pitch_aug", True))
    cfg.setdefault("data", {})["use_pitch_aug"] = use_pitch_aug
    cfg.setdefault("train", {})
    cfg["train"]["cache_fp16"] = bool(params.get("cache_fp16", False))
    wd_source = "沿用 reflow.yaml"
    if cfg["train"].get("weight_decay") is None:
        cfg["train"]["weight_decay"] = 0.1
        wd_source = "reflow.yaml 缺失，取默认 0.1"
    reasons = dict(params.get("reasons") or {})
    reasons["use_pitch_aug"] = (
        f"显式写 use_pitch_aug={str(use_pitch_aug).lower()}"
        + ("（音高随机增广，防过拟合）" if use_pitch_aug else "（高级设置关闭）"))
    reasons["cache_fp16"] = (
        "cache_fp16=" + str(bool(params.get("cache_fp16", False))).lower()
        + "（特征缓存半精度；显存紧张时可开）")
    reasons["weight_decay"] = f"显式写 weight_decay={cfg['train']['weight_decay']}（{wd_source}，抑制参数膨胀）"
    if state.get("data", {}).get("val_reason"):
        reasons["val_split"] = state["data"]["val_reason"]
    params["reasons"] = reasons
    cfg["env"]["expdir"] = str(repo / "exp" / f"_run_{state['run_id']}")
    cfg_path = run_dir / "run_config.yaml"
    _dump_yaml(cfg_path, cfg)
    return cfg_path


# ---------------------------------------------------------------------------
# 阶段 2：preprocess
# ---------------------------------------------------------------------------
def _run_preprocess(state: dict, params: dict, run_dir: Path, python: Path,
                    repo: Path, log_path: Path, log) -> None:
    cfg_path = _build_config(state, params, run_dir, repo)
    dev = _train_device(state, params)
    dtype = "fp32" if dev == "cpu" else "fp16"
    log(f"运行 preprocess（train={run_dir / 'data' / 'train'}，设备 {dev}）")
    code, tail, _ = _run([str(python), "-X", "utf8", str(repo / "preprocess.py"),
                          "-c", str(cfg_path), "-d", dev, "-j", "2",
                          "--store_dtype", dtype],
                          repo, repo, log_path, timeout=None)
    if code != 0:
        raise RuntimeError("preprocess 失败：\n" + "\n".join(tail[-40:]))


# ---------------------------------------------------------------------------
# 阶段 4：预听推理
# ---------------------------------------------------------------------------
def _run_preview(state: dict, ckpt: Path, preview_src: str, python: Path,
                 repo: Path, log_path: Path, log) -> dict:
    previews_dir = Path(state["_run_dir"]) / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    wav_out = previews_dir / f"step_{state['step']}.wav"

    src = Path(preview_src) if preview_src else Path("")
    if not src.is_file():
        val_audio = Path(state["_run_dir"]) / "data" / "val" / "audio"
        if val_audio.is_dir():
            clips = sorted(val_audio.glob("*.wav"))
            if clips:
                src = clips[0]
    base = {"step": state["step"], "ckpt": str(ckpt), "wav": "",
            "val_loss": state.get("_last_val_loss"), "seconds": 0.0,
            "peak": 0.0, "rms": 0.0, "flag": "ok"}
    if not src.is_file():
        log("无预听源（preview_src 未给且 val 集无片段），跳过推理")
        return base

    try:
        preview_step = int((state.get("params") or {}).get("preview_step") or 120)
    except (TypeError, ValueError):
        preview_step = 120
    preview_step = max(5, min(400, preview_step))
    dev = _train_device(state)

    def _preview_cmd(device: str) -> list[str]:
        return [str(python), "-X", "utf8", str(repo / "main_reflow.py"),
                "-m", str(ckpt), "-i", str(src), "-o", str(wav_out),
                "-k", "0", "-step", str(preview_step), "-pe", "rmvpe",
                "-d", device, "-fmin", "65", "-fmax", "800", "-th", "-60"]

    code, tail, _ = _run(_preview_cmd(dev), repo, repo, log_path, timeout=None)
    if (code != 0 or not wav_out.is_file()) and dev != "cpu" \
            and _is_oom_text(tail):
        log("预听推理显存不足，改用 CPU 重试")
        code, tail, _ = _run(_preview_cmd("cpu"), repo, repo, log_path,
                             timeout=None)
    if code != 0 or not wav_out.is_file():
        log("预听推理失败：\n" + "\n".join(tail[-20:]))
        return base

    seconds = peak = rms = 0.0
    flag = "ok"
    try:
        import numpy as np
        import soundfile as sf
        info = sf.info(str(wav_out))
        data, sr = sf.read(str(wav_out), dtype="float32", always_2d=True)
        mono = data.mean(axis=1) if data.ndim > 1 else data
        seconds = round(int(info.frames) / int(sr), 3)
        peak = round(float(np.max(np.abs(mono))), 5)
        rms = round(float(np.sqrt((mono.astype(np.float64) ** 2).mean())), 5)
        if rms < 1e-4:
            flag = "silent"
        elif peak >= 0.999:
            flag = "clip"
    except Exception as exc:  # noqa: BLE001
        log(f"读取预听音频信息失败：{exc}")

    return {"step": state["step"], "ckpt": str(ckpt), "wav": str(wav_out),
            "val_loss": state.get("_last_val_loss"), "seconds": seconds,
            "peak": peak, "rms": rms, "flag": flag}


def _advice(previews: list, auto: bool = False, best: dict | None = None) -> dict:
    """里程碑建议：以「最佳 val_loss」为基准，而不是只比上两次。

    判定（经验值，均未放宽原有阈值）：
      * 试听静音/削波 → 立即 stop_abnormal；
      * 连续 2 个里程碑比最佳值差 ≥3% → stop_overfit；
      * 连续 2 个里程碑相对最佳值改善都 <2% → stop_plateau；
      * 其余 continue（手动模式仅换建议文字，行为不变）。
    返回里 best_step/best_val_loss/worse_streak/flat_streak 会写回 state。
    """
    best = dict(best or {})
    last = previews[-1] if previews else {}
    best_val = best.get("best_val_loss")
    best_step = best.get("best_step")
    worse = int(best.get("worse_streak") or 0)
    flat = int(best.get("flat_streak") or 0)
    val = last.get("val_loss")
    text_extra = ""
    if isinstance(val, (int, float)):
        if best_val is None or val < best_val:
            # 新最佳也要计入平台期：改善幅度 <AUTO_IMPROVE_RATIO 时 flat 仍累加。
            gain_new = (best_val - val) / best_val if best_val else 1.0
            flat = flat + 1 if gain_new < AUTO_IMPROVE_RATIO else 0
            best_val, best_step = float(val), last.get("step")
            if best_step is not None:
                text_extra = f"已刷新最佳 val loss {best_val:.3f}（step {best_step}）。"
            worse = 0
        else:
            drop = (val - best_val) / best_val if best_val else 0.0
            gain = (best_val - val) / best_val if best_val else 0.0
            worse = worse + 1 if drop >= AUTO_WORSE_RATIO else 0
            flat = flat + 1 if gain < AUTO_IMPROVE_RATIO else 0
            text_extra = (f"最佳 val loss {best_val:.3f}（step {best_step}），本次 {val:.3f}"
                          f"（比最佳 {'差' if drop > 0 else '好'} {abs(drop) * 100:.1f}%）。")

    if last.get("flag") in ("silent", "clip"):
        code = "stop_abnormal"
        text = "试听片段异常（静音或削波），立即停止并标记为异常，建议先检查素材。"
    elif worse >= AUTO_ROUNDS:
        code = "stop_overfit"
        text = (f"连续 {worse} 个里程碑的 val loss 都比最佳值 {best_val:.3f} 高 {AUTO_WORSE_RATIO * 100:.0f}% 以上，"
                f"判定过拟合，停止并保留 step {best_step}。")
    elif flat >= AUTO_ROUNDS:
        code = "stop_plateau"
        text = (f"连续 {flat} 个里程碑相对最佳 val loss {best_val:.3f} 改善都不到 {AUTO_IMPROVE_RATIO * 100:.0f}%，"
                f"已进入平台期，停止并保留 step {best_step}。")
    elif auto:
        code = "continue"
        text = "全自动：未触发停止条件，继续训练到下一里程碑。"
    else:
        code = "continue"
        text = "可继续训练到下一里程碑。"
    if text_extra:
        text = text + text_extra
    return {"code": code, "text": text,
            "best_step": best_step, "best_val_loss": best_val,
            "worse_streak": worse, "flat_streak": flat,
            "auto": bool(auto)}



def _ckpt_steps(path: Path) -> int:
    """从 model_<step>.pt 的文件名里取步数。"""
    m = re.search(r"model_(\d+)\.pt$", path.name)
    return int(m.group(1)) if m else 0


def _pick_init_ckpt(state: dict, job: dict) -> Path | None:
    """训练起点：统一返回内置底模 6.3；旧的 auto/scratch 选择一律忽略。

    只接受 job 里的 base_ckpt（插件传入的内置底模路径），读不到时退回
    <repo>/pretrain/reflow_base/model_0.pt。
    """
    choice = str(job.get("base_ckpt") or "").strip()
    if choice not in ("", "auto", "scratch"):
        p = Path(choice)
        if p.is_file():
            return p
    repo = Path(str(job.get("repo") or ""))
    for fallback in (repo / "pretrain" / "reflow_base" / "model_0.pt",
                     Path(str(job.get("init_from") or ""))):
        if str(fallback) and fallback.is_file():
            return fallback
    return None


# ---------------------------------------------------------------------------
# 状态恢复（插件重启后）
# ---------------------------------------------------------------------------
def _restore(state: dict, expdir: Path, log) -> str:
    phase = state.get("phase")
    steps = _read_ckpt_steps(expdir)
    if phase == "awaiting_choice":
        log("恢复：上次处于 awaiting_choice，重新进入等待指令")
        return "awaiting_choice"
    if phase == "previewing":
        # 上次在里程碑推理前被打断；若该里程碑 ckpt 已保存，补一次预听再进入等待。
        milestone_ckpts = [s for s in steps if s in state.get("milestones", [])]
        if milestone_ckpts:
            state["step"] = max(milestone_ckpts)
            log(f"恢复：补做里程碑 {state['step']} 的预听")
            return "preview_pending"
        state["step"] = max(steps) if steps else 0
        log(f"恢复：从最新 ckpt（step {state['step']}）续训")
        return "training"
    if phase in ("preparing", "preprocessing"):
        state["step"] = max(steps) if steps else 0
        log(f"恢复：从最新 ckpt（step {state['step']}）续训")
        return "training"
    if phase == "training":
        state["step"] = max(steps) if steps else 0
        log(f"恢复：续训（step {state['step']}）")
        return "training"
    return phase or "failed"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out_path = Path(sys.argv[2])
    result: dict = {"ok": False, "step": "train_session"}

    run_dir: Path | None = None
    state: dict = {}
    try:
        repo = Path(job["repo"]).resolve()
        python = Path(job["python"]).resolve()
        run_dir = Path(job["run_dir"]).resolve()
        name = str(job.get("name") or "未命名")
        run_id = run_dir.name
        log_path = run_dir / "supervisor.log"
        state_path = run_dir / "state.json"
        control_path = run_dir / "control.json"
        expdir = repo / "exp" / f"_run_{run_id}"
        (run_dir / "data" / "train" / "audio").mkdir(parents=True, exist_ok=True)
        (run_dir / "data" / "val" / "audio").mkdir(parents=True, exist_ok=True)
        (run_dir / "previews").mkdir(parents=True, exist_ok=True)

        def log(msg: str) -> None:
            _log(msg, log_path)

        if state_path.is_file():
            state = _read_json(state_path)
            state["_run_dir"] = str(run_dir)
            state["_repo"] = str(repo)
            log(f"恢复 run {run_id}（phase={state.get('phase')}）")
        else:
            state = {
                "run_id": run_id, "name": name, "phase": "preparing", "step": 0,
                "next_milestone": 0, "milestones": [], "params": {},
                "data": {"inputs": [], "clips_train": 0, "clips_val": 0,
                         "total_sec": 0, "dropped": []},
                "previews": [], "advice": {"code": "continue", "text": ""},
                "speed": {"steps_per_sec": None}, "pid": os.getpid(),
                "error": "", "updated_at": datetime.now().isoformat(),
                "expdir": str(expdir), "repo": str(repo),
                # 全自动 + 中途进度：auto 由插件传入；live 由 solver 日志行驱动。
                "auto": bool(job.get("auto")),
                "best_step": None, "best_val_loss": None,
                "live": {"step": 0, "base_step": 0, "batch": 0, "batch_per_s": None,
                         "updated_at": ""},
                "finalize": {}, "finalize_error": "",
                "_run_dir": str(run_dir), "_repo": str(repo),
            }
            log(f"初始化 run {run_id}（name={name}）")

        auto_mode = bool(state.get("auto") if "auto" in state else job.get("auto"))
        state["auto"] = auto_mode
        state.setdefault("best_step", None)
        state.setdefault("best_val_loss", None)
        state.setdefault("live", {"step": 0, "base_step": 0, "batch": 0,
                                  "batch_per_s": None, "updated_at": ""})
        # expdir.txt：插件重启后不必猜 exp 目录名。
        try:
            (run_dir / "expdir.txt").write_text(str(expdir), encoding="utf-8")
        except OSError:
            pass

        def save_state() -> None:
            state["updated_at"] = datetime.now().isoformat()
            _atomic_write(state_path, state)

        def read_control() -> tuple[str | None, int]:
            if not control_path.is_file():
                return None, 0
            try:
                ctl = _read_json(control_path)
                return ctl.get("action"), int(ctl.get("seq") or 0)
            except (OSError, ValueError, TypeError):
                return None, 0

        # 阶段 1：数据整理（幂等：total_sec 已记录就跳过）
        if state.get("phase") == "preparing" and not state.get("data", {}).get("total_sec"):
            _prepare_data(state, job, run_dir, python, repo, log_path, log)
            state["phase"] = "preprocessing"
            save_state()
            log("阶段 1 完成，进入 preprocessing")


        # 阶段 1.5：从内置底模 6.3 起步（warm start，固定不可选）。
        # train_reflow.py 只认 expdir 里已有的 model_*.pt 续训，所以把底模复制进去，
        # 训练就会以底模为先验开始，而不是从零开始。
        if not state.get("init_seeded") and not list(expdir.glob("model_*.pt")):
            init_src = _pick_init_ckpt(state, job)
            if init_src is not None:
                steps0 = _ckpt_steps(init_src)
                try:
                    expdir.mkdir(parents=True, exist_ok=True)
                    # 自动识别底模框架/格式并转成 DDSP-SVC 需要的
                    # {'global_step','model'} 普通检查点；同时把结构写进 state。
                    info = normalize_checkpoint(init_src, expdir / f"model_{steps0}.pt")
                    state["arch"] = info.get("arch") or {}
                    state["base_info"] = {k: v for k, v in info.items() if k != "arch"}
                    state["init_seeded"] = str(init_src)
                    state["init_step"] = steps0
                    state["step"] = max(int(state.get("step") or 0), steps0)
                    save_state()
                    log(f"底模：{info.get('framework')} · "
                        f"{state['arch'].get('n_layers')} 层 / "
                        f"{state['arch'].get('n_chans')} 通道 · 已转换并加载：{init_src}")
                except Exception as exc:  # noqa: BLE001
                    state["phase"] = "failed"
                    state["error"] = f"底模不兼容：{type(exc).__name__}: {exc}"
                    save_state()
                    raise RuntimeError(state["error"]) from exc

        # 阶段 3 + 2：定参 + preprocess（幂等：units 目录已存在就跳过）
        # 注：auto / auto_cap 由 _auto_params 依据 job 决定；恢复时沿用已存的 milestones。
        if state.get("phase") in ("preparing", "preprocessing"):
            if not state.get("params") or not state["params"].get("milestones"):
                params = _auto_params(state, job, python)
                state["params"] = params
                state["milestones"] = params["milestones"]
                state["next_milestone"] = params["milestones"][0]
            units_dir = run_dir / "data" / "train" / "units"
            if not units_dir.is_dir():
                _run_preprocess(state, state["params"], run_dir, python, repo, log_path, log)
            if auto_mode and state["params"].get("auto_cap"):
                state["milestones"] = [m for m in state["milestones"]
                                       if int(m) <= int(state["params"]["auto_cap"])]
            state["phase"] = "training"
            save_state()
            log("阶段 2/3 完成，进入 training")

        milestones = state.get("milestones") or MILESTONES_DEFAULT

        def next_milestone_after(step: int) -> int | None:
            # 必须每次读 state：continue 会替换 state["milestones"]，
            # 闭包里捕获的旧列表会让刚追加的里程碑不可见。
            for m in (state.get("milestones") or milestones):
                if m > step:
                    return m
            return None

        phase = _restore(state, expdir, log)
        state["phase"] = phase
        state["live"] = {"step": 0, "base_step": int(state.get("step") or 0),
                         "batch": 0, "batch_per_s": None, "updated_at": ""}
        save_state()

        def apply_advice(advice: dict) -> None:
            state["advice"] = advice
            state["best_step"] = advice.get("best_step")
            state["best_val_loss"] = advice.get("best_val_loss")

        def finalize_best() -> None:
            """全自动停止：用现有 train_finalize_worker 保留 best_step，不切 ddsp_ckpt。"""
            step = state.get("best_step")
            if not step:
                state["phase"] = "finished"
                state["advice"] = {**(state.get("advice") or {}), "code": "stop_no_best",
                                   "text": "没有可用的最佳里程碑，未保留任何 ckpt。"}
                save_state()
                return
            fjob = {"repo": str(repo), "run_dir": str(run_dir), "step": int(step),
                    "name": str(state.get("name") or name)}
            fjob_path = run_dir / "finalize_job.json"
            fresh_path = run_dir / "finalize_result.json"
            fjob_path.write_text(json.dumps(fjob, ensure_ascii=False), encoding="utf-8")
            if fresh_path.exists():
                fresh_path.unlink()
            code, tail, _ = _run([str(python), "-X", "utf8", "-u",
                                   str(Path(__file__).resolve().parent / "train_finalize_worker.py"),
                                   str(fjob_path), str(fresh_path)],
                                  run_dir, repo, log_path, timeout=1800)
            try:
                res = _read_json(fresh_path) if fresh_path.is_file() else {}
            except (OSError, ValueError):
                res = {}
            if code != 0 or not res.get("ok"):
                state["finalize_error"] = (res.get("error") or chr(10).join(tail[-20:]))[:2000]
                state["finalize"] = {}
                adv = dict(state.get("advice") or {})
                adv["keep_text"] = f"自动保留失败：{state['finalize_error'][:200]}"
                state["advice"] = adv
                log("自动保留失败：" + state["finalize_error"][-800:])
            else:
                state["finalize"] = {"ok": True, "ckpt": res.get("ckpt"),
                                     "final_name": res.get("final_name"),
                                     "trash_manifest": res.get("trash_manifest")}
                adv = dict(state.get("advice") or {})
                adv["keep_text"] = (f"已自动保留 step {step} 为 {res.get('final_name')}"
                                    f"（{res.get('ckpt')}），未切换 ddsp_ckpt。")
                state["advice"] = adv
                log(adv["keep_text"])
            state["phase"] = "finished"
            save_state()

        clips_n = max(int(state.get("data", {}).get("clips_train") or 1), 1)
        batch_n = max(int(state.get("params", {}).get("batch_size") or 1), 1)
        state["params"]["total_batches_per_step"] = max((clips_n + batch_n - 1) // batch_n, 1)
        # 训练期间的实时进度：solver 日志行 → state["live"]，最多每 2s 落盘一次。
        # 回调与主线程同进程串行（Popen 在 _run 内），无跨线程写 state；
        # on_line 每轮训练重新绑定 cur_step，闭包里的值总对应当前这一段。
        live_box = {"last_save": 0.0, "base": 0}

        def on_line(line: str) -> None:
            parsed = _parse_train_progress(line)
            if not parsed:
                return
            state["live"] = {"step": parsed["step"], "base_step": live_box["base"],
                             "batch": parsed.get("batch"),
                             "batch_per_s": parsed["batch_per_s"],
                             "updated_at": datetime.now().isoformat()}
            now = time.time()
            if now - live_box["last_save"] >= 2.0:
                live_box["last_save"] = now
                save_state()

        while state["phase"] not in ("finished", "failed", "stopping"):
            # awaiting_choice：等 control.json（只处理更大的 seq）
            if state["phase"] == "awaiting_choice":
                while True:
                    action = None
                    seq = 0
                    if control_path.is_file():
                        try:
                            ctl = _read_json(control_path)
                            action = ctl.get("action")
                            seq = int(ctl.get("seq") or 0)
                        except (OSError, ValueError):
                            pass
                    if action == "continue" and seq > int(state.get("last_seq") or 0):
                        state["last_seq"] = seq
                        try:
                            extra = int(ctl.get("steps") or 0)
                        except (AttributeError, TypeError, ValueError):
                            extra = 0
                        if extra > 0:
                            # 用户确认后要求「再练 N 步」：目标向上取整到 2000，
                            # 中途每 2000 步只出试听不暂停，练到新目标才停。
                            new_total = int(state.get("step") or 0) + extra
                            new_total = ((new_total + 1999) // 2000) * 2000
                            state["milestones"] = list(range(2000, new_total + 1, 2000))
                            milestones = state["milestones"]
                            state["steps_total"] = new_total
                            state["pause_at"] = new_total
                            log(f"继续训练：再加 {extra} 步 → 目标 {new_total}（每 2000 步出试听，中途不停）")
                        else:
                            state["pause_at"] = int(state.get("steps_total") or state.get("step") or 0)
                        state["phase"] = "training"
                        save_state()
                        break
                    if action == "stop" and seq > int(state.get("last_seq") or 0):
                        state["last_seq"] = seq
                        state["phase"] = "stopping"
                        save_state()
                        log(f"收到 stop（seq={seq}）")
                        break
                    time.sleep(0.5)
                continue

            # preview_pending：恢复时补做上次里程碑的预听，然后回到等待。
            if state["phase"] == "preview_pending":
                ckpt = expdir / f"model_{state['step']}.pt"
                if ckpt.is_file():
                    preview_src = str(job.get("preview_src") or state.get("preview_src") or "")
                    preview = _run_preview(state, ckpt, preview_src, python, repo, log_path, log)
                    state["previews"].append(preview)
                    apply_advice(_advice(state["previews"], auto=auto_mode, best={
                        "best_step": state.get("best_step"),
                        "best_val_loss": state.get("best_val_loss"),
                        "worse_streak": state.get("worse_streak"),
                        "flat_streak": state.get("flat_streak")}))
                state["phase"] = "training" if auto_mode else "awaiting_choice"
                save_state()
                log(f"补做预听后进入 {state['phase']}（step {state['step']}）")
                continue

            cur_step = int(state.get("step") or 0)
            live_box["base"] = cur_step
            target = next_milestone_after(cur_step)
            if target is None:
                log("已到达最后一个里程碑")
                if auto_mode:
                    finalize_best()
                else:
                    state["phase"] = "finished"
                    save_state()
                break
            state["next_milestone"] = target
            state["phase"] = "training"
            save_state()

            log(f"开始训练到里程碑 {target}（当前 step {cur_step}）")
            stop_hit = {"v": False}
            if int(state.get("step") or 0) >= int(target):
                log(f"当前 step 已到里程碑 {target}，跳过一次训练")
                code, tail, elapsed = 0, [], 0.0
            else:
                state["live"] = {"step": 0, "base_step": cur_step, "batch": 0,
                                 "batch_per_s": None, "updated_at": ""}
                save_state()

                def _check_stop() -> bool:
                    if stop_hit["v"]:
                        return False
                    action_now, seq_now = read_control()
                    if action_now != "stop" or seq_now <= int(state.get("last_seq") or 0):
                        return False
                    state["last_seq"] = seq_now
                    stop_hit["v"] = True
                    return True

                params_now = state.get("params", {})
                attempts = 0
                while True:
                    cfg_path = _build_config(state, params_now, run_dir, repo)
                    code, tail, elapsed = _run(
                        [str(python), "-X", "utf8",
                         str(repo / "train_reflow.py"), "-c", str(cfg_path)],
                        repo, repo, log_path, timeout=None,
                        on_line=on_line, stop_check=_check_stop)
                    if stop_hit["v"] or code == 0 or attempts >= 3 \
                            or not _is_oom_text(tail):
                        break
                    attempts += 1
                    if int(params_now.get("batch_size") or 1) > 1:
                        params_now["batch_size"] = max(
                            1, int(params_now["batch_size"]) // 2)
                        state["params"] = params_now
                        log(f"训练显存不足：batch_size 降到 "
                            f"{params_now['batch_size']} 重试")
                    elif _train_device(state, params_now) != "cpu":
                        state["device"] = "cpu"
                        params_now["device"] = "cpu"
                        params_now["amp"] = "fp32"
                        log("训练显存不足且 batch 已为 1：改用 CPU 训练"
                            "（会非常慢）")
                    else:
                        break
                    save_state()
            elapsed = max(elapsed, 1e-6)
            if stop_hit["v"]:
                state["phase"] = "stopping"
                state["stopped_at"] = datetime.now().isoformat()
                state["live"] = {"step": 0, "base_step": int(state.get("step") or cur_step),
                                 "batch": 0, "batch_per_s": None, "updated_at": ""}
                save_state()
                log("收到 stop：已中止当前训练，进入收尾")
                break
            state["live"] = {"step": 0, "base_step": int(state.get("step") or cur_step),
                             "batch": 0, "batch_per_s": None, "updated_at": ""}

            steps_now = _read_ckpt_steps(expdir)
            val_loss = _parse_val_loss(tail)
            state["_last_val_loss"] = val_loss
            if code != 0:
                state["phase"] = "failed"
                state["error"] = f"训练退出码 {code}：\n" + "\n".join(tail[-40:])
                save_state()
                log(state["error"])
                break

            new_step = max(steps_now) if steps_now else cur_step
            state["step"] = new_step
            if new_step > cur_step:
                state["speed"]["steps_per_sec"] = round((new_step - cur_step) / elapsed, 3)
            else:
                state["speed"]["steps_per_sec"] = None
            log(f"训练到 step {new_step}（{elapsed:.1f}s，"
                f"{state['speed']['steps_per_sec']} steps/s，val_loss={val_loss}）")

            # 训练进程按 epochs 停机后，未必正好落在 milestone 上；把 step 归一化
            # 到最近一个已保存的 milestone ckpt，避免预览/保留用到非里程碑步骤。
            milestone_ckpts = [s for s in steps_now if s in state.get("milestones", [])]
            if milestone_ckpts:
                state["step"] = max(milestone_ckpts)

            state["phase"] = "previewing"
            save_state()
            ckpt = expdir / f"model_{state['step']}.pt"
            preview_src = str(job.get("preview_src") or state.get("preview_src") or "")
            preview = _run_preview(state, ckpt, preview_src, python, repo, log_path, log)
            state["previews"].append(preview)
            apply_advice(_advice(state["previews"], auto=auto_mode, best={
                "best_step": state.get("best_step"),
                "best_val_loss": state.get("best_val_loss"),
                "worse_streak": state.get("worse_streak"),
                "flat_streak": state.get("flat_streak")}))

            if auto_mode and state["step"] >= int(state["params"].get("auto_cap")
                                                 or state["milestones"][-1]):
                adv = dict(state["advice"])
                adv["code"] = adv.get("code") if adv["code"].startswith("stop") else "stop_reached_cap"
                adv["text"] = (f"全自动已到步数上限 {state['step']}，停止并保留最佳 step "
                               f"{state.get('best_step')}。") + adv.get("text", "")
                state["advice"] = adv

            code_now = str((state.get("advice") or {}).get("code") or "continue")
            if auto_mode:
                state["worse_streak"] = (state.get("advice") or {}).get("worse_streak")
                state["flat_streak"] = (state.get("advice") or {}).get("flat_streak")
                if code_now.startswith("stop"):
                    log(f"全自动停止（step {state['step']}，{code_now}）：{state['advice'].get('text')}")
                    finalize_best()
                    break
                # 全自动：不停下来等用户，只响应还没消费过的 stop（用户中止）。
                action, seq = read_control()
                if action == "stop" and seq > int(state.get("last_seq") or 0):
                    state["last_seq"] = seq
                    state["phase"] = "stopping"
                    save_state()
                    log(f"全自动中收到用户 stop（seq={seq}）")
                    break
                state["phase"] = "training"
                save_state()
                log(f"全自动继续：step {state['step']}，建议 {code_now}，下一里程碑 "
                    f"{next_milestone_after(state['step'])}")
                continue

            action_now, seq_now = read_control()
            if action_now == "stop" and seq_now > int(state.get("last_seq") or 0):
                state["last_seq"] = seq_now
                state["phase"] = "stopping"
                state["stopped_at"] = datetime.now().isoformat()
                save_state()
                log("收到 stop（里程碑后）：进入收尾")
                break
            pause_at = int(state.get("pause_at") or state.get("steps_total") or state["step"])
            if int(state["step"]) < pause_at:
                # 还没到用户要停的步数：试听已经生成，直接接着练
                state["phase"] = "training"
                save_state()
                log(f"继续训练（step {state['step']} < 暂停点 {pause_at}），建议 {state['advice']['code']}")
                continue
            state["phase"] = "awaiting_choice"
            save_state()
            log(f"进入 awaiting_choice（step {state['step']}，建议 {state['advice']['code']}）")

        result.update({
            "ok": state.get("phase") in ("finished", "stopping", "awaiting_choice"),
            "run_id": run_id, "phase": state.get("phase"),
        })
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["trace"] = traceback.format_exc()[-3000:]
        if run_dir is not None:
            state["phase"] = "failed"
            state["error"] = result["error"]
            state["updated_at"] = datetime.now().isoformat()
            try:
                _atomic_write(run_dir / "state.json", state)
            except OSError:
                pass
    out_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
