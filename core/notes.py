# -*- coding: utf-8 -*-
"""记事本存储：富文本（HTML）+ 纯文本 + 生成的 Markdown，落 JSON。

一条笔记：
{
  "id": "20261121-133005-482",
  "title": "第一行文字前 30 字",
  "html": "<p>...</p>",
  "plain": "纯文本，用于列表预览和搜索",
  "md": "导出用的 Markdown 源码",
  "created": "2026-11-21 13:30",
  "updated": "2026-11-21 13:31"
}

最多 60 条；按更新时间倒序；导出时把 md 写到 notes/ 目录。
"""
from __future__ import annotations

import json
import os
import re
import time

from core.appinfo import NOTES_PATH

MAX_NOTES = 60


def _default_export_dir() -> str:
    """默认导出到「文档\\DesktopIsland\\notes」，可用 config.json 的 export_dir 覆盖。"""
    try:
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        if not os.path.isdir(docs):
            docs = os.path.expanduser("~")
        return os.path.join(docs, "DesktopIsland", "notes")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "DesktopIsland-notes")


def export_dir() -> str:
    cfg = os.path.join(os.path.dirname(NOTES_PATH), "config.json")
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            custom = json.load(f).get("export_dir")
        if custom:
            return str(custom)
    except Exception:
        pass
    return _default_export_dir()


def _stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


def _new_id() -> str:
    t = time.localtime()
    return time.strftime("%Y%m%d-%H%M%S", t) + "-%03d" % (int(time.time() * 1000) % 1000)


def make_title(plain: str) -> str:
    """标题取正文第一行非空文字，最多 24 字。"""
    for line in (plain or "").splitlines():
        s = line.strip()
        if s:
            return s[:24] + ("…" if len(s) > 24 else "")
    return "未命名"


def safe_filename(title: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (title or "").strip()) or "未命名"
    return s[:40]


class NoteStore:
    def __init__(self) -> None:
        self.items: list = []
        self.load()

    # ------------------------------------------------------------------ 读写
    def load(self) -> None:
        try:
            with open(NOTES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.items = [d for d in data if isinstance(d, dict)][:MAX_NOTES]
        except Exception:
            self.items = []

    def save(self) -> None:
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            with open(NOTES_PATH, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    # ------------------------------------------------------------------ 增删改
    def create(self) -> dict:
        now = _stamp()
        item = {
            "id": _new_id(),
            "title": "未命名",
            "html": "",
            "plain": "",
            "md": "",
            "created": now,
            "updated": now,
        }
        self.items.insert(0, item)
        self._trim()
        self.save()
        return item

    def add(self, html: str, plain: str, md: str) -> dict:
        now = _stamp()
        item = {
            "id": _new_id(),
            "title": make_title(plain),
            "html": html,
            "plain": plain,
            "md": md,
            "created": now,
            "updated": now,
        }
        self.items.insert(0, item)
        self._trim()
        self.save()
        return item

    def update(self, nid: str, html: str, plain: str, md: str) -> None:
        for it in self.items:
            if it["id"] == nid:
                it["html"] = html
                it["plain"] = plain
                it["md"] = md
                it["title"] = make_title(plain)
                it["updated"] = _stamp()
                self.save()
                return

    def get(self, nid: str):
        for it in self.items:
            if it["id"] == nid:
                return it
        return None

    def remove(self, nid: str) -> None:
        self.items = [it for it in self.items if it["id"] != nid]
        self.save()

    def clear(self) -> None:
        self.items = []
        self.save()

    def _trim(self) -> None:
        if len(self.items) > MAX_NOTES:
            del self.items[MAX_NOTES:]

    # ------------------------------------------------------------------ 导出
    def export_md(self, item: dict) -> str:
        """把一条笔记的 Markdown 写到导出目录，返回文件路径。"""
        out_dir = export_dir()
        os.makedirs(out_dir, exist_ok=True)
        base = safe_filename(item.get("title") or "未命名")
        path = os.path.join(out_dir, base + ".md")
        n = 2
        while os.path.exists(path):
            path = os.path.join(out_dir, "%s_%d.md" % (base, n))
            n += 1
        body = item.get("md") or item.get("plain") or ""
        with open(path, "w", encoding="utf-8") as f:
            f.write(body.rstrip() + "\n")
        return path
