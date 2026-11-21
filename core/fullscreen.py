# -*- coding: utf-8 -*-
"""判断有没有「真的全屏」的窗口，用来决定要不要把灵动岛收起来。

踩过的两个坑：
1. 直接用 GetForegroundWindow() 不可靠 —— Win11 上有时会长期返回一个 shell 的
   「Windows.UI.Core.CoreWindow」，真正在打游戏的前台窗口反而取不到。所以这里按
   z-order 从最上层往下扫，找第一个铺满整块显示器、又不在跳过名单里的窗口。
2. 进程不是 DPI-aware 时 GetWindowRect 会返回缩放后的逻辑坐标（150% 缩放下
   2560x1440 会变成 1707x960），跟 GetMonitorInfo 的物理像素对不上。所以不用像素
   比，改成比「宽/高各自占显示器的比例」是否一致。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

SKIP_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Windows.UI.Core.CoreWindow",
    "ApplicationFrameWindow",
    "SysShadow",
    "TaskListThumbnailWnd",
}
GW_HWNDNEXT = 2
MAX_SCAN = 80


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _class_of(u, hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _covers_monitor(u, hwnd) -> bool:
    r = wintypes.RECT()
    if not u.GetWindowRect(hwnd, ctypes.byref(r)):
        return False
    hmon = u.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not hmon:
        return False
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(mi)
    if not u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return False
    m = mi.rcMonitor
    mw = m.right - m.left
    mh = m.bottom - m.top
    if mw <= 0 or mh <= 0:
        return False

    ww = r.right - r.left
    wh = r.bottom - r.top
    rx = ww / float(mw)
    ry = wh / float(mh)
    # 至少要盖住大半个屏幕
    if rx < 0.6 or ry < 0.6:
        return False
    # 宽、高的缩放比要一致（同一个 DPI 缩放出来的矩形）
    if abs(rx - ry) > 0.03:
        return False
    # 左边和上边要贴住显示器左上角
    if abs((r.left - m.left) / float(mw)) > 0.01:
        return False
    if abs((r.top - m.top) / float(mh)) > 0.01:
        return False
    return True


def foreground_is_fullscreen() -> bool:
    try:
        u = ctypes.windll.user32
        u.GetTopWindow.restype = wintypes.HWND
        u.GetWindow.restype = wintypes.HWND
        u.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        my_pid = os.getpid()

        hwnd = u.GetTopWindow(None)
        for _ in range(MAX_SCAN):
            if not hwnd:
                break
            if u.IsWindowVisible(hwnd) and not u.IsIconic(hwnd):
                cls = _class_of(u, hwnd)
                if cls not in SKIP_CLASSES:
                    pid = wintypes.DWORD(0)
                    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value and pid.value != my_pid and _covers_monitor(u, hwnd):
                        return True
            hwnd = u.GetWindow(hwnd, GW_HWNDNEXT)
        return False
    except Exception:
        return False
