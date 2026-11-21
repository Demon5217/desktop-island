# -*- coding: utf-8 -*-
"""剪贴板历史：文本 + 文件，带来源进程和时间，最多 30 条。

比系统剪贴板强的地方：
1. 记 30 条，能回滚到更早的内容；
2. 文件粘过来会记住整组路径，点一下能整组写回；
3. 每条都记来源进程名和时间，找得到是谁复制的。

来源是这么拿的：GetClipboardOwner() 取持有剪贴板的窗口，再 GetWindowThreadProcessId 查 PID，
不用 OpenClipboard，所以不会和别的程序抢剪贴板。
"""
from __future__ import annotations

import ctypes
import json
import os
import time

from core.appinfo import CLIP_PATH  # noqa: E402

MAX_ITEMS = 30


def owner_process() -> str:
    """返回当前剪贴板内容是谁放进去的（进程名）。拿不到就返回问号。"""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetClipboardOwner()
        if not hwnd:
            return "未知来源"
        pid = ctypes.c_ulong(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return "未知来源"
        import psutil

        return psutil.Process(pid.value).name()
    except Exception:
        return "未知来源"


def _now() -> tuple:
    t = time.localtime()
    return time.strftime("%H:%M", t), time.strftime("%Y-%m-%d %H:%M", t)


class ClipStore:
    def __init__(self) -> None:
        self.items: list = []
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        try:
            with open(CLIP_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.items = data[:MAX_ITEMS]
        except Exception:
            self.items = []

    def save(self) -> None:
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            with open(CLIP_PATH, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False)
        except Exception:
            pass

    # ------------------------------------------------------------------ 记录
    def add_text(self, text: str, src: str) -> bool:
        text = (text or "").strip("\n")
        if not text.strip():
            return False
        # 同内容连续复制不重复记，但会把旧的那条提到最前
        for i, it in enumerate(self.items):
            if it["kind"] == "text" and it.get("text") == text:
                item = self.items.pop(i)
                item["src"] = src
                item["time"], item["full"] = _now()
                self.items.insert(0, item)
                self.save()
                return True
        self.items.insert(0, {
            "kind": "text", "text": text, "src": src,
            "time": _now()[0], "full": _now()[1], "size": len(text),
        })
        self._trim()
        self.save()
        return True

    def add_files(self, paths: list, src: str) -> bool:
        paths = [p for p in paths if p]
        if not paths:
            return False
        key = "|".join(paths)
        for i, it in enumerate(self.items):
            if it["kind"] == "files" and "|".join(it.get("files", [])) == key:
                item = self.items.pop(i)
                item["src"] = src
                item["time"] = _now()[0]
                self.items.insert(0, item)
                self.save()
                return True
        self.items.insert(0, {
            "kind": "files", "files": paths, "src": src,
            "time": _now()[0], "full": _now()[1],
        })
        self._trim()
        self.save()
        return True

    def _trim(self) -> None:
        if len(self.items) > MAX_ITEMS:
            del self.items[MAX_ITEMS:]

    def remove(self, idx: int) -> None:
        if 0 <= idx < len(self.items):
            self.items.pop(idx)
            self.save()

    def clear(self) -> None:
        self.items = []
        self.save()

    # ------------------------------------------------------------------ 展示
    @staticmethod
    def preview(item: dict, width: int = 54) -> str:
        if item["kind"] == "text":
            s = " ".join(str(item.get("text", "")).split())
            return s[:width] + ("…" if len(s) > width else "")
        files = item.get("files") or []
        names = [os.path.basename(p) for p in files]
        s = "、".join(names)
        if len(names) > 2:
            return f"{names[0]} 等 {len(names)} 个文件"
        return s[:width]
