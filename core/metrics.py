# -*- coding: utf-8 -*-
"""设备指标采集线程。

一条独立线程每秒采一次，通过信号把结果丢给 UI，
UI 线程只负责画，永远不做 IO / 系统调用，这样动画才不会卡。

数据源：
  CPU / 内存 / 网络 / 磁盘  -> psutil
  GPU 占用 / 显存 / 温度    -> NVML（NVIDIA 驱动自带，N 卡可用）
"""
from __future__ import annotations

import ctypes
import json
import subprocess
import threading
import time
from ctypes import wintypes
from typing import Optional

import psutil
from PySide6.QtCore import QThread, Signal

try:
    import pynvml
except Exception:  # 没装 nvidia-ml-py 也不影响其它指标
    pynvml = None


class _PerfInfo(ctypes.Structure):
    """psapi.GetPerformanceInfo 的结构体（提交内存、页文件、物理内存都在这里）。"""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wintypes.DWORD),
        ("ProcessCount", wintypes.DWORD),
        ("ThreadCount", wintypes.DWORD),
    ]


def read_commit() -> dict:
    """读提交内存（Commit）与页文件用量，不需要管理员权限。

    返回 GB：commit_used / commit_limit / pagefile_total / pagefile_used。
    """
    out = {}
    try:
        pi = _PerfInfo()
        pi.cb = ctypes.sizeof(_PerfInfo)
        if ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
            gb = pi.PageSize / float(2 ** 30)      # PageSize 单位是字节
            out["commit_used"] = pi.CommitTotal * gb
            out["commit_limit"] = pi.CommitLimit * gb
    except Exception:
        return out
    try:
        swap = psutil.swap_memory()
        out["pagefile_total"] = swap.total / float(2 ** 30)
        out["pagefile_used"] = swap.used / float(2 ** 30)
    except Exception:
        pass
    return out


_PF_PS = (
    "Get-CimInstance Win32_PageFileUsage | "
    "Select-Object Name,AllocatedBaseSize,CurrentUsage | "
    "ConvertTo-Json -Compress"
)


def read_pagefiles() -> Optional[list]:
    """各盘页面文件的分布（名字 / 分配大小 / 当前用量）。

    WMI 走 PowerShell 子进程要 0.6~1 秒，所以不放在每秒的采样里，
    由采集线程每 30 秒刷一次，界面只读缓存。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PF_PS],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000,          # CREATE_NO_WINDOW，不闪黑窗
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        raw = json.loads(out.stdout.strip())
        if isinstance(raw, dict):
            raw = [raw]
        files = []
        for item in raw:
            name = str(item.get("Name") or "")
            total_mb = float(item.get("AllocatedBaseSize") or 0)
            used_mb = float(item.get("CurrentUsage") or 0)
            if total_mb <= 0:
                continue
            files.append({
                "name": name,
                "drive": name[:1].upper() or "?",
                "total_gb": total_mb / 1024.0,
                "used_gb": used_mb / 1024.0,
                "pct": used_mb / total_mb * 100.0,
            })
        files.sort(key=lambda f: f["name"])
        return files
    except Exception:
        return None


class GpuReader:
    """包一层 NVML，读不到就安静地返回 None，不让整个组件挂掉。"""

    def __init__(self) -> None:
        self.ok = False
        self.name = "GPU"
        self.total_gb = 0.0
        if pynvml is None:
            return
        try:
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            raw_name = pynvml.nvmlDeviceGetName(self.handle)
            if isinstance(raw_name, bytes):
                raw_name = raw_name.decode("utf-8", "ignore")
            self.name = str(raw_name).replace("NVIDIA ", "")
            total = pynvml.nvmlDeviceGetMemoryInfo(self.handle).total
            self.total_gb = total / 1024 ** 3
            self.ok = True
        except Exception:
            self.ok = False

    def read(self) -> Optional[dict]:
        if not self.ok:
            return None
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(self.handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(self.handle)
            try:
                temp = pynvml.nvmlDeviceGetTemperature(
                    self.handle, pynvml.NVML_TEMPERATURE_GPU
                )
            except Exception:
                temp = None
            return {
                "gpu_util": float(util.gpu),
                "vram_used": mem.used / 1024 ** 3,
                "vram_total": mem.total / 1024 ** 3,
                "gpu_temp": temp,
            }
        except Exception:
            return None


class MetricsWorker(QThread):
    """每秒采样一次，emit 一个扁平 dict 给 UI。"""

    sampled = Signal(dict)

    def __init__(self, interval: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self.interval = interval
        self._stop = threading.Event()
        self._gpu = GpuReader()
        self._pagefiles: list = []
        self._pf_poller: Optional[threading.Thread] = None

    # 给 UI 用来显示显卡名 / 总显存
    @property
    def gpu_name(self) -> str:
        return self._gpu.name

    @property
    def gpu_total_gb(self) -> float:
        return self._gpu.total_gb

    def stop(self) -> None:
        self._stop.set()

    def _poll_pagefiles(self) -> None:
        """WMI 走子进程要 0.8 秒左右，单独线程每 30 秒刷一次，别拖慢每秒采样。"""
        while not self._stop.is_set():
            pf = read_pagefiles()
            if pf is not None:
                self._pagefiles = pf
            for _ in range(300):          # 30 秒，分小段睡以便及时退出
                if self._stop.is_set():
                    return
                time.sleep(0.1)

    def run(self) -> None:
        # 第一次调用返回 0，先丢掉，让后面的百分比有意义
        psutil.cpu_percent(None)
        net_prev = psutil.net_io_counters()
        disk_prev = psutil.disk_io_counters()
        t_prev = time.perf_counter()
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 0
        cpu_name = _cpu_brand()
        first = True
        self._pf_poller = threading.Thread(target=self._poll_pagefiles, daemon=True)
        self._pf_poller.start()

        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                dt = max(t0 - t_prev, 1e-6)
                cpu = psutil.cpu_percent(None)
                vm = psutil.virtual_memory()
                net = psutil.net_io_counters()
                disk = psutil.disk_io_counters()

                up = max(net.bytes_sent - net_prev.bytes_sent, 0) / dt
                down = max(net.bytes_recv - net_prev.bytes_recv, 0) / dt
                disk_r = max(disk.read_bytes - disk_prev.read_bytes, 0) / dt if disk and disk_prev else 0
                disk_w = max(disk.write_bytes - disk_prev.write_bytes, 0) / dt if disk and disk_prev else 0
                if first:
                    # 首帧的 dt 只有几毫秒，算出来的速率会虚高几十倍，直接归零
                    up = down = disk_r = disk_w = 0.0
                    first = False

                net_prev, disk_prev = net, disk
                t_prev = t0

                try:
                    freq = psutil.cpu_freq()
                    cpu_ghz = freq.current / 1000.0 if freq else 0.0
                except Exception:
                    cpu_ghz = 0.0

                data = {
                    "cpu": cpu,
                    "cpu_ghz": cpu_ghz,
                    "cpu_cores": cores,
                    "cpu_name": cpu_name,
                    "ram": vm.percent,
                    "ram_used": vm.used / 1024 ** 3,
                    "ram_total": vm.total / 1024 ** 3,
                    "net_up": up,
                    "net_down": down,
                    "disk_r": disk_r,
                    "disk_w": disk_w,
                    "gpu_util": None,
                    "vram_used": 0.0,
                    "vram_total": self._gpu.total_gb,
                    "gpu_temp": None,
                }
                commit = read_commit()
                if commit:
                    limit = commit.get("commit_limit") or 0.0
                    used = commit.get("commit_used") or 0.0
                    data["commit_used"] = used
                    data["commit_limit"] = limit
                    data["commit_pct"] = (used / limit * 100.0) if limit else 0.0
                    data["pagefile_total"] = commit.get("pagefile_total", 0.0)
                    data["pagefile_used"] = commit.get("pagefile_used", 0.0)
                # 各盘页面文件分布由后台线程 30 秒刷一次，这里只读缓存
                data["pagefiles"] = self._pagefiles
                gpu = self._gpu.read()
                if gpu:
                    data.update(gpu)
                self.sampled.emit(data)
            except Exception:
                pass

            spent = (time.perf_counter() - t0) * 1000
            self.msleep(max(1, int(self.interval * 1000 - spent)))


def _cpu_brand() -> str:
    """从注册表拿 CPU 型号，拿不到就退回 "CPU"。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
        name = str(name).replace("(R)", "").replace("(TM)", "").replace("CPU", "").strip()
        return " ".join(name.split())
    except Exception:
        return "CPU"
