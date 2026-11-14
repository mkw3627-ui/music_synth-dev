# -*- coding: utf-8 -*-
"""作业终态 → 对外说法（纯函数，不依赖宿主 SDK，可离线回归）。

背景（用户投诉「说假话 / 嘴巴比脑子快」）：流水线判定「不能翻唱」时返回体里
``ok=True, blocked=True``，旧版把它当成功，作业记成 ``done + 完成``；模型拿到
「完成」就告诉主人「唱完了」，而其实一个成品都没有。这里把「什么算完成」写成
唯一口径，插件里所有对外回执 / 状态核对都走它。

约定：
- 只有 **真产出成品文件** 才算 ``done``；
- ``blocked``（纯音乐 / 录音不完整 / 没产出成品）不算完成，也不算失败；
- 拿不准就是 ``unknown``，绝不默认成功。
"""

from __future__ import annotations

__all__ = [
    "SPEECH_FORBID",
    "SPEECH_TEXT",
    "claim_state",
    "claims_in_text",
    "job_outcome",
    "speech_for",
]

SPEECH_FORBID = ["已经唱好", "唱完了", "做好了", "成品出来了", "可以听了", "放完了"]

SPEECH_TEXT = {
    "done": ("已经做出成品，可以试听 / 可以如实说做好了。", True),
    "submitted": ("刚提交，后台才开始做，还没有成品；在做好之前不许报完成。", False),
    "queued": ("还在排队，没开始做；还没有成品。", False),
    "processing": ("正在做，还没有成品；在做好之前不许报完成。", False),
    "blocked": ("这首做不了 / 被拦下了，没有成品；要如实说明原因。", False),
    "failed": ("这次没做成，没有成品；要如实说明原因。", False),
    "idle": ("后台没有可对照的作业记录，不能凭空说完成。", False),
    "unknown": ("后台状态不明，不能凭空说完成。", False),
}

_CLAIMS = ("唱完", "唱好", "做完", "做好", "弄好", "搞定", "放完", "播完",
           "听完", "听过", "成品", "可以听", "能听", "完工", "完成", "结束")


def job_outcome(result, *, finished=None):
    """流水线返回值 → ``(state, detail)``。

    ``finished`` 由调用方查真实成品文件后传入：``False`` 表示「跑完了但没有成品」，
    同样不能算 done；``None`` 表示调用方查不到（按有成品处理，保持兼容）。
    """
    result = result if isinstance(result, dict) else {}
    if result.get("blocked"):
        return "blocked", str(result.get("note") or result.get("error")
                               or "这首不能翻唱，没有产出成品")
    if not result.get("ok"):
        return "failed", str(result.get("error") or "处理失败")
    if finished is False:
        return "blocked", str(result.get("note") or result.get("error")
                              or "处理结束但没有产出可试听的成品")
    return "done", "完成"


def claim_state(*, job_state: str = "", track_status: str = "",
                final_exists: bool = False, has_track: bool = True) -> str:
    """作业 / 曲目状态 → 对外真状态（done / processing / blocked / failed / idle）。

    ``job_state`` 是插件自己记的作业终态（running / queued / blocked / failed / done），
    ``track_status`` 是曲目记录状态。成品文件是否存在是最后一道硬证据：
    没有文件的 done 一律降级为 blocked。
    """
    job_state = str(job_state or "")
    track_status = str(track_status or "")
    if job_state in ("running", "queued"):
        return "processing"
    if job_state == "blocked":
        return "blocked" if not final_exists else "done"
    if job_state == "failed":
        return "failed"
    if job_state == "done":
        # 作业说完成，但成品文件不在：照样不能算完成。
        return "done" if final_exists else "blocked"
    if track_status == "done" and final_exists:
        return "done"
    if track_status in ("instrumental", "incomplete", "silent"):
        return "blocked"
    if track_status in ("running", "queued"):
        return "processing"
    if track_status == "failed":
        return "failed"
    if final_exists:
        # 有旧成品、但没有明确的新作业终态：可以试听，不能说「这次刚做好」。
        return "done"
    return "idle" if has_track else "idle"


def speech_for(state: str, detail: str = "", track=None) -> dict:
    """真状态 → 给模型看的「能说什么 / 不能说什么」。"""
    text, can_claim = SPEECH_TEXT.get(str(state or ""), SPEECH_TEXT["unknown"])
    row = {
        "state": str(state or "unknown"),
        "can_claim_done": bool(can_claim),
        "say": text,
        "forbid_claims": [] if can_claim else list(SPEECH_FORBID),
    }
    if detail:
        row["detail"] = str(detail)[:160]
    if isinstance(track, dict) and track:
        row["track_id"] = str(track.get("id") or "")
        row["title"] = str(track.get("title") or "")
    return row


def claims_in_text(text) -> list[str]:
    """文本里出现的完成类说法（用于核对，不用于改写）。"""
    body = str(text or "")
    return [word for word in _CLAIMS if word in body]
