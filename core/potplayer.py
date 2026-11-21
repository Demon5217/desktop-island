# -*- coding: utf-8 -*-
"""PotPlayer 后台播放控制。

做法：
1. 已有实例时发 `/current /play "<文件>"` —— 复用同一实例换歌，不会新开窗口；
   没有实例时用 `/play` 起一个新的。
2. 启动/换歌后把 PotPlayer 主窗口（类名 PotPlayer64 / PotPlayer）和
   「播放列表」顶层窗口 SW_HIDE 掉，播放继续，桌面不再被打断。
   实测：隐藏之后再用 /current 换歌，窗口不会自己冒出来。
3. `show()` 供「显示播放器」按钮把主窗口叫回来。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import threading
import time

u32 = ctypes.windll.user32
try:
    u32.SetProcessDPIAware()
except Exception:
    pass

# PotPlayer 的主窗口类名（64 位是 PotPlayer64，32 位是 PotPlayer）
MAIN_CLASSES = {"PotPlayer64", "PotPlayer", "PotPlayerMini64", "PotPlayerMini"}
# 播放列表是一个独立的顶层窗口，冷启动时会自己显示出来，得一起藏
PLAYLIST_TITLE = "播放列表"

SW_HIDE = 0
SW_SHOW = 5

# 用户手动点了「显示播放器」后，后台的隐藏线程要立刻停手，不然会把他刚叫出来的窗口又藏掉
_stop_hide = threading.Event()
_pid_cache = (0.0, set())

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def pot_pids() -> set:
    """当前所有 PotPlayer 相关进程的 pid（带 1.5 秒缓存，守卫轮询时不至于一直全表扫）。"""
    global _pid_cache
    ts, ids = _pid_cache
    if ids and time.time() - ts < 1.5:
        return ids
    try:
        import psutil
    except Exception:
        return set()
    found = set()
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info.get("name") or "").lower()
        except Exception:
            continue
        if "potplayer" in name:
            found.add(p.pid)
    _pid_cache = (time.time(), found)
    return found


def find_windows() -> list:
    """返回 [{hwnd,pid,cls,title,visible}]，只含 PotPlayer 进程的顶层窗口。"""
    pids = pot_pids()
    if not pids:
        return []
    out = []

    def cb(hwnd, _lp):
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids:
            return True
        cls = ctypes.create_unicode_buffer(128)
        u32.GetClassNameW(hwnd, cls, 128)
        title = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, title, 512)
        out.append(
            {
                "hwnd": hwnd,
                "pid": pid.value,
                "cls": cls.value,
                "title": title.value,
                "visible": bool(u32.IsWindowVisible(hwnd)),
            }
        )
        return True

    u32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def main_hwnd():
    for w in find_windows():
        if w["cls"] in MAIN_CLASSES:
            return w["hwnd"]
    return None


def is_running() -> bool:
    return bool(pot_pids())


def hide_now() -> bool:
    """立刻把主窗口和播放列表藏起来。返回是否至少碰到了一个窗口。"""
    hit = False
    for w in find_windows():
        if w["cls"] in MAIN_CLASSES or w["title"] == PLAYLIST_TITLE:
            u32.ShowWindow(w["hwnd"], SW_HIDE)
            hit = True
    return hit


def visible_targets() -> list:
    """还露在外面的目标窗口（主窗 / 播放列表）。"""
    return [
        w for w in find_windows()
        if (w["cls"] in MAIN_CLASSES or w["title"] == PLAYLIST_TITLE) and w["visible"]
    ]


def hide_soon(duration: float = 20.0, interval: float = 0.4) -> None:
    """后台线程里盯着藏，默认盯 20 秒。

    冷启动的时间线实测：1 秒时窗口创建、主窗藏住；3.5 秒时「播放列表」自己又冒出来。
    所以不能「藏住就收工」，得继续盯着，后面偶尔再冒出来也一并按下去。
    用户点「显示播放器」会 set _stop_hide，这里立刻退出。
    """
    _stop_hide.clear()

    def worker():
        t0 = time.time()
        stable_since = None
        while not _stop_hide.is_set():
            elapsed = time.time() - t0
            if elapsed > duration:
                return
            hide_now()
            if not visible_targets():
                if stable_since is None:
                    stable_since = time.time()
                # 前 12 秒加密观察，之后放慢
                elif elapsed > 12 and time.time() - stable_since >= 6.0:
                    return
            else:
                stable_since = None
            time.sleep(interval if elapsed < 12 else 1.2)

    threading.Thread(target=worker, daemon=True).start()


def show() -> bool:
    """把主窗口叫回前台。"""
    _stop_hide.set()  # 别再藏了
    hwnd = main_hwnd()
    if not hwnd:
        return False
    u32.ShowWindow(hwnd, SW_SHOW)
    u32.SetForegroundWindow(hwnd)
    try:
        u32.SwitchToThisWindow(hwnd, True)
    except Exception:
        pass
    return True


def launch(exe: str, path: str, background: bool = True) -> tuple:
    """播放一个文件。background=True 时顺便把窗口藏掉。"""
    try:
        if is_running():
            args = [exe, "/current", "/play", path]
        else:
            args = [exe, "/play", path]
        subprocess.Popen(args, close_fds=True)
    except Exception as e:
        return False, f"启动失败：{e}"
    if background:
        hide_soon()
    return True, "ok"
