# -*- coding: utf-8 -*-
"""设备指标采集线程。

一条独立线程每秒采一次，通过信号把结果丢给 UI，
UI 线程只负责画，永远不做 IO / 系统调用，这样动画才不会卡。

数据源：
  CPU / 内存 / 网络 / 磁盘  -> psutil
  GPU 占用 / 显存 / 温度    -> NVML（NVIDIA 驱动自带，N 卡可用）
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import psutil
from PySide6.QtCore import QThread, Signal

try:
    import pynvml
except Exception:  # 没装 nvidia-ml-py 也不影响其它指标
    pynvml = None


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

    # 给 UI 用来显示显卡名 / 总显存
    @property
    def gpu_name(self) -> str:
        return self._gpu.name

    @property
    def gpu_total_gb(self) -> float:
        return self._gpu.total_gb

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # 第一次调用返回 0，先丢掉，让后面的百分比有意义
        psutil.cpu_percent(None)
        net_prev = psutil.net_io_counters()
        disk_prev = psutil.disk_io_counters()
        t_prev = time.perf_counter()
        cores = psutil.cpu_count(logical=False) or psutil.cpu_count() or 0
        cpu_name = _cpu_brand()
        first = True

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
