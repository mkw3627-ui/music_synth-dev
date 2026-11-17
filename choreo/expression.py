"""逐句情绪 → 脸部参数（P4 增补 · 任务 A）。

舞台档（static/stage/stage-runtime.js）与桌面档（music_perception/live2d/bridge.js）
共用这一张映射表的同一组数值，两档只差在「乘多大的幅度」。

约束（来自既有事故，不能破）：
- 眼形（ParamEyeLSmile / ParamEyeRSmile）**不进共用映射**。桌面档 g60 起一律不写：
  这个模型的 EyeSmile 会把下眼睑拉下、眼珠显小，且叠加写、停手不复位。
  舞台档仍按计划 v3 §7-11 限幅到 EYE_SMILE_CAP 后写入，属于舞台专属。
- 只输出眉、脸颊、嘴形、开合四类；数值都在 [-1, 1] 或 [0, 1] 之内。

强度分档（弱 / 中 / 强）：按逐句 intensity 切三档，档内取档中值的倍率，
句间用时间常数平滑过渡（见 ``smooth``），避免换句时参数硬跳。
"""
from __future__ import annotations

import math

# 情绪 → 基础参数（强度 1.0 时的值）。数值沿用舞台 EMO 表里对应的眉/脸颊/嘴/开合项，
# 并去掉眼形，保证两档一致。
EXPRESSION = {
    "joy":        {"browY": 0.30, "browF": 0.30, "browA": 0.00, "mouth": 0.60, "cheek": 0.30, "open": 0.95},
    "playful":    {"browY": 0.25, "browF": 0.40, "browA": 0.00, "mouth": 0.55, "cheek": 0.35, "open": 0.92},
    "tender":     {"browY": 0.10, "browF": 0.20, "browA": 0.00, "mouth": 0.35, "cheek": 0.45, "open": 0.85},
    "longing":    {"browY": 0.40, "browF": -0.20, "browA": 0.50, "mouth": 0.05, "cheek": 0.10, "open": 0.78},
    "sad":        {"browY": 0.50, "browF": -0.50, "browA": 0.70, "mouth": -0.40, "cheek": 0.00, "open": 0.65},
    "proud":      {"browY": 0.20, "browF": 0.40, "browA": -0.10, "mouth": 0.40, "cheek": 0.00, "open": 1.00},
    "determined": {"browY": -0.30, "browF": -0.30, "browA": -0.40, "mouth": 0.05, "cheek": 0.00, "open": 1.00},
}
NEUTRAL = {"browY": 0.0, "browF": 0.0, "browA": 0.0, "mouth": 0.0, "cheek": 0.0, "open": 1.0}

# 强度分档：intensity < 0.45 为弱，< 0.70 为中，其余为强。倍率取各档中值。
TIERS = (("weak", 0.45, 0.6), ("mid", 0.70, 0.85), ("strong", 1.01, 1.0))

# 句间平滑的时间常数（秒）：情绪变强用 attack，变弱用 release，与 bridge 的 EMO.attack/release 同一思路
ATTACK_S = 0.35
RELEASE_S = 0.9


def tier_of(intensity: float) -> str:
    """强度 → 弱 / 中 / 强。输入不是数值时按中档处理，不抛异常。"""
    try:
        x = float(intensity)
    except (TypeError, ValueError):
        return "mid"
    if not math.isfinite(x):
        return "mid"
    for name, edge, _ in TIERS:
        if x < edge:
            return name
    return "strong"


def _mult(tier: str) -> float:
    for name, _, mult in TIERS:
        if name == tier:
            return mult
    return 0.85


def params_for(emotion: str, intensity: float) -> dict:
    """一句的目标脸部参数（未平滑）。未知情绪退回 tender（与舞台 EMO 的兜底一致）。"""
    base = EXPRESSION.get(str(emotion or ""), EXPRESSION["tender"])
    k = _mult(tier_of(intensity))
    out = {}
    for key, val in base.items():
        if key == "open":
            # 开合以 1.0 为基准向基础值收，强度越低越接近自然睁开
            out[key] = 1.0 + (val - 1.0) * k
        else:
            out[key] = val * k
    return {key: round(v, 4) for key, v in out.items()}


def smooth(prev: dict, target: dict, dt: float, rising: bool) -> dict:
    """句间平滑：向目标靠拢。变强（rising）用 ATTACK_S，变弱用 RELEASE_S。"""
    tau = ATTACK_S if rising else RELEASE_S
    k = 1.0 - math.exp(-max(0.0, dt) / tau)
    return {key: prev.get(key, NEUTRAL[key]) + (target[key] - prev.get(key, NEUTRAL[key])) * k
            for key in target}
