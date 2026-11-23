# -*- coding: utf-8 -*-
"""用 Win32 API 批量枚举进程。

psutil 每读一个属性都要 OpenProcess 一次，300+ 进程就要 2.5 秒；
这里一次 EnumProcesses + 每个进程一次 OpenProcess 做完
（名字 / CPU 时间 / 工作集），实测能压到 0.1 秒级。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from typing import List

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL
psapi.EnumProcesses.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]
psapi.EnumProcesses.restype = wintypes.BOOL
k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
k32.QueryFullProcessImageNameW.restype = wintypes.BOOL


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32FirstW.restype = wintypes.BOOL
k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Process32NextW.restype = wintypes.BOOL


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


k32.GetProcessIoCounters.argtypes = [wintypes.HANDLE, ctypes.POINTER(IO_COUNTERS)]
k32.GetProcessIoCounters.restype = wintypes.BOOL


k32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
k32.GetProcessTimes.restype = wintypes.BOOL
psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS), wintypes.DWORD
]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def _ticks(ft: FILETIME) -> int:
    return int(ft.dwHighDateTime) * 0x100000000 + int(ft.dwLowDateTime)


class ProcessScanner:
    """每调一次 scan() 得到一个进程快照（CPU% 为两次调用的平均）。"""

    def __init__(self) -> None:
        self._last_cpu: dict[int, int] = {}
        self._last_io: dict[int, tuple[int, int]] = {}
        self._last_time = time.perf_counter()
        self._cpu_count = os.cpu_count() or 1

    def _name(self, handle) -> str:
        buf = ctypes.create_unicode_buffer(520)
        size = wintypes.DWORD(520)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            path = buf.value.replace("/", "\\")
            return path.rsplit("\\", 1)[-1]
        return ""

    def _all_names(self) -> dict[int, str]:
        """Toolhelp 枚举全部进程名：不需要 OpenProcess 权限，
        vmwp.exe 这类受保护进程只有靠它才拿得到名字。"""
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        names: dict[int, str] = {}
        if not snap or snap == wintypes.HANDLE(-1).value:
            return names
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                names[int(entry.th32ProcessID)] = entry.szExeFile
                ok = k32.Process32NextW(snap, ctypes.byref(entry))
        except Exception:
            pass
        finally:
            k32.CloseHandle(snap)
        return names

    def scan(self) -> List[dict]:
        now = time.perf_counter()
        dt = max(now - self._last_time, 1e-6)
        self._last_time = now

        names = self._all_names()  # 全局进程名，作为打不开时的兜底
        buf = (wintypes.DWORD * 4096)()
        needed = wintypes.DWORD(0)
        if not psapi.EnumProcesses(buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return []
        count = int(needed.value) // ctypes.sizeof(wintypes.DWORD)

        out: List[dict] = []
        seen = set()
        for i in range(count):
            pid = int(buf[i])
            if pid == 0 or pid in seen:
                continue
            seen.add(pid)

            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                # 受保护进程（vmwp / csrss 等）：拿不到 CPU 和内存，
                # 但显存/GPU 是从 PDH 按 PID 取的，照样能显示，不能整行丢掉
                name = names.get(pid, "")
                if not name:
                    continue
                out.append(
                    {
                        "pid": pid,
                        "name": name,
                        "cpu": 0.0,
                        "mem": 0.0,
                        "commit": 0.0,
                        "disk_r": 0.0,
                        "disk_w": 0.0,
                        "restricted": True,
                    }
                )
                continue
            try:
                name = self._name(handle)
                if not name:
                    continue

                c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                if not k32.GetProcessTimes(
                    handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)
                ):
                    continue
                ticks = _ticks(kt) + _ticks(ut)
                prev = self._last_cpu.get(pid)
                self._last_cpu[pid] = ticks
                # 100ns 时间片 → 占整机多少百分比（和任务管理器口径一致）
                cpu = 0.0 if prev is None else max(ticks - prev, 0) / (dt * 1e7) * 100.0 / self._cpu_count

                pmc = PROCESS_MEMORY_COUNTERS()
                pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                mem = 0.0
                commit_mb = 0.0
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                    mem = pmc.WorkingSetSize / 1024 ** 2
                    # PagefileUsage 就是任务管理器的「提交大小」：该进程已提交的私有字节
                    commit_mb = pmc.PagefileUsage / 1024 ** 2

                # 磁盘读写：累计字节差分 → B/s，和任务管理器的"磁盘"列同源
                disk_r = disk_w = 0.0
                io = IO_COUNTERS()
                if k32.GetProcessIoCounters(handle, ctypes.byref(io)):
                    cur = (int(io.ReadTransferCount), int(io.WriteTransferCount))
                    prev_io = self._last_io.get(pid)
                    self._last_io[pid] = cur
                    if prev_io is not None:
                        disk_r = max(cur[0] - prev_io[0], 0) / dt
                        disk_w = max(cur[1] - prev_io[1], 0) / dt

                out.append(
                    {
                        "pid": pid,
                        "name": name,
                        "cpu": min(cpu, 100.0),
                        "mem": mem,
                        "commit": commit_mb,
                        "disk_r": disk_r,
                        "disk_w": disk_w,
                        "restricted": False,
                    }
                )
            except Exception:
                continue
            finally:
                k32.CloseHandle(handle)

        for pid in list(self._last_cpu.keys()):
            if pid not in seen:
                self._last_cpu.pop(pid, None)
                self._last_io.pop(pid, None)
        return out
