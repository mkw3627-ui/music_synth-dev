"""错误日志：插件里任何异常都写进 data/errors.log，带时间、来源和完整堆栈。

只追加、不删除、不轮转（错误记录本身很小）。写日志失败时静默跳过，
绝不能因为记日志把原本的错误盖掉或再抛出新错误。
"""
from __future__ import annotations

import functools
import inspect
import threading
import time
import traceback
from pathlib import Path

_LOCK = threading.Lock()
_FILE_NAME = "errors.log"


def error_log_path(data_dir) -> Path:
    return Path(data_dir) / _FILE_NAME


def record_error(data_dir, where: str, exc: BaseException | None = None, detail: str = "") -> None:
    """把一条错误追加到 errors.log。exc 为空时只记 detail（用于非异常的失败）。"""
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"===== {stamp} {where} ====="]
        if detail:
            lines.append(detail)
        if exc is not None:
            lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        text = "\n".join(lines) + "\n"
        path = error_log_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, path.open("a", encoding="utf-8", errors="backslashreplace") as fh:
            fh.write(text)
    except Exception:  # noqa: BLE001,S110 - 记日志失败不能影响原错误，没有更好的地方可记
        pass


def _failed_return(value) -> str:
    """入口返回 ok=False 时取出错误文本，否则返回空串。

    返回值可能是 Ok(...) 包装（取 .data）或裸 dict；两种都兼容。
    """
    payload = getattr(value, "data", value)
    if isinstance(payload, dict) and payload.get("ok") is False:
        return str(payload.get("error") or "未给出错误原因")
    return ""


def logged_entry(method):
    """包住插件入口方法：异常先写日志再原样抛出；返回 ok=False 也写日志，但照常返回。"""
    name = method.__name__

    if inspect.iscoroutinefunction(method):
        @functools.wraps(method)
        async def wrapper(self, *args, **kwargs):
            try:
                result = await method(self, *args, **kwargs)
            except Exception as exc:
                record_error(self._data_dir, f"entry:{name}", exc)
                raise
            err = _failed_return(result)
            if err:
                record_error(self._data_dir, f"entry:{name}", detail=f"返回 ok=False：{err}")
            return result
        return wrapper

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            result = method(self, *args, **kwargs)
        except Exception as exc:
            record_error(self._data_dir, f"entry:{name}", exc)
            raise
        err = _failed_return(result)
        if err:
            record_error(self._data_dir, f"entry:{name}", detail=f"返回 ok=False：{err}")
        return result
    return wrapper
