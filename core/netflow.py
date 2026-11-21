# -*- coding: utf-8 -*-
"""按进程统计网络流量。

Windows 没有现成 API，只有两条路：
  1) ETW（Microsoft-Windows-Kernel-Network）—— 精确，但**必须管理员权限**
  2) 降级：只统计每个进程的活跃连接数

程序普通权限启动时自动降级，并在界面上标出当前模式，不假装有数据。
"""
from __future__ import annotations

import threading
import time
from collections import Counter

import psutil

KERNEL_NETWORK_GUID = "7dd42a49-5329-4832-8dfd-43d979153a88"

# Microsoft-Windows-Kernel-Network 的数据事件
SENT_IDS = {10, 17}   # TcpIp/DataSent, UdpIp/DataSent
RECV_IDS = {11, 18}   # TcpIp/DataRecv, UdpIp/DataRecv


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _num(ev, names) -> int:
    for n in names:
        for getter in ("get_u32", "get_u64"):
            try:
                v = getattr(ev, getter)(n)
                if v:
                    return int(v)
            except Exception:
                continue
    for n in names:
        try:
            v = ev.get(n)
            if isinstance(v, (int, float)):
                return int(v)
        except Exception:
            continue
    return 0


class NetFlow:
    """按 PID 提供网络数据。mode: "etw" 精确速率 / "conn" 连接数降级。"""

    def __init__(self) -> None:
        self.mode = "conn"
        self.detail = ""
        self._acc: dict[int, list[int]] = {}
        self._lock = threading.Lock()
        self._last = time.monotonic()
        self._running = False
        self._listener = None
        self._start()

    # ------------------------------------------------------------------ 启动
    def _start(self) -> None:
        if not is_admin():
            self.mode = "conn"
            self.detail = "普通权限运行：网络列显示连接数，以管理员启动可显示真实流量"
            return
        try:
            from pyetwkit import EtwListener
            from pyetwkit.providers import KernelProvider
        except Exception:
            self.mode = "conn"
            self.detail = "未安装 pyetwkit，网络列降级为连接数"
            return

        try:
            provider = KernelProvider.network()
            self._listener = EtwListener(
                [provider], buffer_size_kb=512, channel_capacity=200_000
            )
            self._listener.start()
        except Exception as e:
            self.mode = "conn"
            self.detail = f"ETW 启动失败（{type(e).__name__}），网络列降级为连接数"
            return

        self.mode = "etw"
        self.detail = "ETW 精确流量"
        self._running = True
        threading.Thread(target=self._loop, name="etw-net", daemon=True).start()

    def _loop(self) -> None:
        while self._running:
            try:
                for ev in self._listener.events(timeout=0.5):
                    self._handle(ev)
            except Exception:
                time.sleep(0.3)

    def _handle(self, ev) -> None:
        try:
            eid = ev.event_id
        except Exception:
            return
        if eid not in SENT_IDS and eid not in RECV_IDS:
            return
        size = _num(ev, ("size", "Size", "bytes", "Bytes"))
        if size <= 0:
            return
        pid = 0
        try:
            pid = int(getattr(ev, "process_id", 0) or 0)
        except Exception:
            pid = 0
        if not pid:
            pid = _num(ev, ("PID", "ProcessId"))
        if not pid:
            return
        with self._lock:
            rec = self._acc.setdefault(pid, [0, 0])
            if eid in SENT_IDS:
                rec[0] += size
            else:
                rec[1] += size

    # ------------------------------------------------------------------ 取数
    def take_rates(self) -> dict[int, tuple[float, float]]:
        """取走这期间的累计字节，换算成 B/s。"""
        with self._lock:
            acc = self._acc
            self._acc = {}
        now = time.monotonic()
        elapsed = max(now - self._last, 1e-6)
        self._last = now
        return {pid: (up / elapsed, down / elapsed) for pid, (up, down) in acc.items()}

    def connection_counts(self) -> dict[int, int]:
        """降级模式：每个进程的活跃 TCP/UDP 连接数。"""
        counter: Counter = Counter()
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.pid:
                    counter[conn.pid] += 1
        except Exception:
            pass
        return dict(counter)

    def close(self) -> None:
        self._running = False
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:
            pass
