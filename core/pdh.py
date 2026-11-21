# -*- coding: utf-8 -*-
"""PDH（性能计数器）封装。

Windows 提供两组我们需要的计数器：
  \\GPU Engine(*)\\Utilization Percentage      每个进程每个引擎的 GPU 利用率
  \\GPU Process Memory(*)\\Dedicated Usage     每个进程的专用显存（字节）

实例名形如 ``pid_11524_luid_0x00000000_0x00011a37_phys_0_eng_0_engtype_3D``，
第二段就是 PID。用 PdhAddEnglishCounterW 注册，中文系统也能用。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import List, Tuple

try:
    _pdh = ctypes.WinDLL("pdh.dll")
except Exception:  # 非 Windows
    _pdh = None

PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = -2147481646  # 0x800007D2（有符号）


class _FmtValue(ctypes.Structure):
    _fields_ = [
        ("CStatus", wintypes.DWORD),
        ("doubleValue", ctypes.c_double),
    ]


class _Item(ctypes.Structure):
    _fields_ = [
        ("szName", wintypes.LPWSTR),
        ("FmtValue", _FmtValue),
    ]


if _pdh is not None:
    _pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
    _pdh.PdhOpenQueryW.restype = wintypes.DWORD
    _pdh.PdhAddEnglishCounterW.argtypes = [
        ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)
    ]
    _pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
    _pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
    _pdh.PdhCollectQueryData.restype = wintypes.DWORD
    _pdh.PdhGetFormattedCounterArrayW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(_Item),
    ]
    _pdh.PdhGetFormattedCounterArrayW.restype = wintypes.DWORD
    _pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
    _pdh.PdhCloseQuery.restype = wintypes.DWORD


def parse_pid(instance: str) -> int:
    """从实例名里抠出 PID，失败返回 0。"""
    try:
        parts = instance.split("_")
        if len(parts) >= 2 and parts[0] == "pid":
            return int(parts[1])
    except Exception:
        pass
    return 0


class MultiCounter:
    """一个通配符计数器（可展开成多个实例）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.ok = False
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        if _pdh is None:
            return
        try:
            if _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
                return
            if _pdh.PdhAddEnglishCounterW(self._query, path, 0, ctypes.byref(self._counter)) != 0:
                return
            _pdh.PdhCollectQueryData(self._query)  # 初始化，第一次往往没有值
            self.ok = True
        except Exception:
            self.ok = False

    def sample(self) -> List[Tuple[str, float]]:
        """采一次，返回 [(实例名, 值)]。调用频率决定了速率类计数器的平均窗口。"""
        if not self.ok or _pdh is None:
            return []
        try:
            if _pdh.PdhCollectQueryData(self._query) != 0:
                return []
            size = wintypes.DWORD(0)
            count = wintypes.DWORD(0)
            # 第一次只是为了问出缓冲区大小（正常返回 PDH_MORE_DATA），
            # 所以这里不判断返回码，只看 size 有没有给出来
            _pdh.PdhGetFormattedCounterArrayW(
                self._counter, PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(count), None
            )
            if size.value == 0:
                return []
            buf = ctypes.create_string_buffer(size.value)
            rc = _pdh.PdhGetFormattedCounterArrayW(
                self._counter,
                PDH_FMT_DOUBLE,
                ctypes.byref(size),
                ctypes.byref(count),
                ctypes.cast(buf, ctypes.POINTER(_Item)),
            )
            if rc != 0:
                return []
            items = ctypes.cast(buf, ctypes.POINTER(_Item))
            out: List[Tuple[str, float]] = []
            for i in range(count.value):
                it = items[i]
                if it.FmtValue.CStatus == 0:
                    out.append((it.szName or "", float(it.FmtValue.doubleValue)))
            return out
        except Exception:
            return []

    def close(self) -> None:
        try:
            if self.ok and _pdh is not None:
                _pdh.PdhCloseQuery(self._query)
        except Exception:
            pass
        self.ok = False


class GpuCounters:
    """把三张计数器合成「每个 PID 的 GPU% 和显存 MB」。"""

    def __init__(self) -> None:
        self.engine = MultiCounter(r"\GPU Engine(*)\Utilization Percentage")
        self.vram = MultiCounter(r"\GPU Process Memory(*)\Dedicated Usage")
        # 共享显存：dwm、浏览器这类进程大头在这里，只算专用会显得"读不到"
        self.shared = MultiCounter(r"\GPU Process Memory(*)\Shared Usage")

    @property
    def ok(self) -> bool:
        return self.engine.ok

    def sample(self) -> dict:
        """返回 {pid: {"gpu": %, "vram": MB, "vram_ded": MB, "vram_sha": MB}}。"""
        out: dict = {}
        for name, value in self.engine.sample():
            pid = parse_pid(name)
            if not pid:
                continue
            rec = out.setdefault(pid, {"gpu": 0.0, "vram": 0.0, "vram_ded": 0.0, "vram_sha": 0.0})
            # 同一进程会挂在多个引擎实例上（3D/VideoDecode/Copy/虚拟化），
            # 相加会轻松爆到 100%，任务管理器取的是其中最高的那一路
            rec["gpu"] = max(rec["gpu"], value)
        for name, value in self.vram.sample():
            pid = parse_pid(name)
            if not pid:
                continue
            rec = out.setdefault(pid, {"gpu": 0.0, "vram": 0.0, "vram_ded": 0.0, "vram_sha": 0.0})
            rec["vram_ded"] += value / 1024 ** 2
        for name, value in self.shared.sample():
            pid = parse_pid(name)
            if not pid:
                continue
            rec = out.setdefault(pid, {"gpu": 0.0, "vram": 0.0, "vram_ded": 0.0, "vram_sha": 0.0})
            rec["vram_sha"] += value / 1024 ** 2
        for rec in out.values():
            rec["gpu"] = min(rec["gpu"], 100.0)
            # 与任务管理器"专用 + 共享"口径一致
            rec["vram"] = rec["vram_ded"] + rec["vram_sha"]
        return out

    def close(self) -> None:
        self.engine.close()
        self.vram.close()
        self.shared.close()
