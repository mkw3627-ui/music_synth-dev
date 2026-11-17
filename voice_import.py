"""第五十六轮：声音包后台导入。

以前：面板读文件 → 分片 base64 上传 → 结束时安装。整个过程跑在面板页面里，用户切走页面就断了。
现在：后端弹 Windows 原生「打开文件」框选 .zip，插件进程直接读本地文件、后台线程安装，
面板只轮询作业状态（切走再回来也能看到进度 / 结果）。原来的分片上传入口保留不动（拖拽仍可用）。
"""
from __future__ import annotations

import sys
import threading


def pick_zip_win32(title: str = "选择声音包（.zip）", start_dir: str = "") -> tuple[str, str]:
    """GetOpenFileNameW（comdlg32，纯 ctypes）。返回 (路径, 错误)；路径为空 = 用户取消。"""
    if sys.platform != "win32":
        return "", "只有 Windows 支持系统文件选择框"
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND), ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR), ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR), ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR), ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD), ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD), ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR), ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD), ("FlagsEx", wintypes.DWORD),
        ]

    comdlg32 = ctypes.WinDLL("comdlg32")
    user32 = ctypes.WinDLL("user32")
    ole32 = ctypes.WinDLL("ole32")
    ole32.CoInitializeEx(None, 2)  # 对话框线程要 STA
    buf = ctypes.create_unicode_buffer(4096)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    # 置顶到前台窗口上（插件进程没有窗口，不设 owner 框会藏在后面）
    ofn.hwndOwner = user32.GetForegroundWindow()
    ofn.lpstrFilter = "声音包 (*.zip)\0*.zip\0所有文件 (*.*)\0*.*\0\0"
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 4096
    ofn.lpstrTitle = title
    if start_dir:
        ofn.lpstrInitialDir = start_dir
    # OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_EXPLORER
    ofn.Flags = 0x1000 | 0x800 | 0x8 | 0x80000
    try:
        ok = comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
        if ok:
            return buf.value, ""
        err = comdlg32.CommDlgExtendedError()
        return "", ("" if err == 0 else f"文件选择框出错（{err}）")
    finally:
        try:
            ole32.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


class PickState:
    """一次只开一个选择框；面板轮询拿结果。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"state": "idle", "path": "", "error": "", "gen": 0}
        self.thread: threading.Thread | None = None

    def start(self, start_dir: str = "") -> dict:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return dict(self.state)
            gen = int(self.state.get("gen") or 0) + 1
            self.state = {"state": "open", "path": "", "error": "", "gen": gen}

            def worker():
                path, err = pick_zip_win32(start_dir=start_dir)
                with self.lock:
                    if self.state.get("gen") != gen:
                        return
                    if err:
                        self.state.update(state="error", error=err)
                    elif path:
                        self.state.update(state="picked", path=path)
                    else:
                        self.state.update(state="cancelled")

            self.thread = threading.Thread(target=worker, name="ms-pick-zip", daemon=True)
            self.thread.start()
            return dict(self.state)

    def get(self) -> dict:
        with self.lock:
            return dict(self.state)
