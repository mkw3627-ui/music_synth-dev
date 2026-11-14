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


def logged_entry(method):
    """包住插件入口方法：异常先写日志，再原样抛出。支持 async 和普通函数。"""
    name = method.__name__

    if inspect.iscoroutinefunction(method):
        @functools.wraps(method)
        async def wrapper(self, *args, **kwargs):
            try:
                return await method(self, *args, **kwargs)
            except Exception as exc:
                record_error(self._data_dir, f"entry:{name}", exc)
                raise
        return wrapper

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            record_error(self._data_dir, f"entry:{name}", exc)
            raise
    return wrapper
