# -*- coding: utf-8 -*-
"""进程级采样：谁在吃 CPU / 内存 / GPU / 显存 / 网络。

CPU、内存走 psutil；GPU、显存走 PDH 计数器（按 PID）；
网络来自 NetFlow（管理员下精确，否则连接数）。
"""
from __future__ import annotations

import threading
import time

from PySide6.QtCore import QThread, Signal

from core.pdh import GpuCounters
from core.netflow import NetFlow
from core.winsys import ProcessScanner

# 这些进程一旦结束，轻则桌面重启，重则系统蓝屏 —— 界面上直接禁掉
PROTECTED_NAMES = {
    "system",
    "registry",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "svchost.exe",
    "fontdrvhost.exe",
    "dwm.exe",
    "explorer.exe",
    "spoolsv.exe",
    "audiodg.exe",
    "sihost.exe",
    "ctfmon.exe",
    "taskhostw.exe",
    "SearchIndexer.exe",
    "MsMpEng.exe",
    "SecurityHealthService.exe",
    "WUDFHost.exe",
    "conhost.exe",
    "RuntimeBroker.exe",
    "ShellExperienceHost.exe",
    "StartMenuExperienceHost.exe",
    "TextInputHost.exe",
    "MoUsoCoreWorker.exe",
    "python.exe",
    "pythonw.exe",
}


def is_protected(name: str) -> bool:
    return (name or "").lower() in {n.lower() for n in PROTECTED_NAMES}


class ProcessWorker(QThread):
    """每 2 秒吐一次进程快照。只在「进程页」打开时真正采样。"""

    sampled = Signal(list)
    error = Signal(str)

    def __init__(self, netflow: NetFlow, interval: float = 2.0, parent=None) -> None:
        super().__init__(parent)
        self.interval = interval
        self.netflow = netflow
        self._active = threading.Event()
        self._stop = threading.Event()
        self._scanner = ProcessScanner()

    def set_active(self, active: bool) -> None:
        if active:
            self._active.set()
        else:
            self._active.clear()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # PDH 的英文计数器内部会走 COM 查语言，线程没有 apartment 时偶发 RPC 异常
        co_inited = False
        try:
            import ctypes

            co_inited = ctypes.windll.ole32.CoInitializeEx(None, 0) == 0  # MTA
        except Exception:
            co_inited = False

        # PDH 首次注册计数器约 2 秒：线程一启动就建好，别让用户点开进程页再等
        gpu: GpuCounters | None = GpuCounters()
        if not gpu.ok:
            self.error.emit("GPU 计数器不可用，GPU/显存列会显示为空")
        primed = False

        while not self._stop.is_set():
            if not self._active.is_set():
                primed = False  # 下次进入进程页要重新建 CPU 基线
                self.msleep(600)
                continue

            if not primed:
                self._scanner.scan()  # 只建基线，结果丢掉
                primed = True
                self.msleep(900)
                continue

            t0 = time.perf_counter()
            try:
                self.sampled.emit(self._snapshot(gpu))
            except Exception as e:  # 采集失败不该把整个线程搞死
                self.error.emit(f"进程采样失败：{e}")
            spent = (time.perf_counter() - t0) * 1000
            self.msleep(max(1, int(self.interval * 1000 - spent)))

        if gpu is not None:
            gpu.close()
        if co_inited:
            try:
                import ctypes

                ctypes.windll.ole32.CoUninitialize()
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _snapshot(self, gpu: GpuCounters) -> list:
        gpu_map = gpu.sample() if gpu.ok else {}
        if self.netflow.mode == "etw":
            net_rates = self.netflow.take_rates()
            conns = {}
        else:
            net_rates = {}
            conns = self.netflow.connection_counts()

        rows = []
        for item in self._scanner.scan():
            pid = item["pid"]
            name = item["name"]
            g = gpu_map.get(pid, {})
            up, down = net_rates.get(pid, (0.0, 0.0))
            rows.append(
                {
                    "pid": pid,
                    "name": name,
                    "cpu": item["cpu"],
                    "mem": item["mem"],
                    "commit": float(item.get("commit", 0.0)),
                    "gpu": float(g.get("gpu", 0.0)),
                    "vram": float(g.get("vram", 0.0)),
                    "net_up": up,
                    "net_down": down,
                    "disk_r": item.get("disk_r", 0.0),
                    "disk_w": item.get("disk_w", 0.0),
                    "conns": conns.get(pid, 0),
                    "protected": is_protected(name),
                    "restricted": bool(item.get("restricted", False)),
                }
            )

        rows.sort(key=lambda r: r["cpu"], reverse=True)
        return rows
