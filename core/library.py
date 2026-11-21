# -*- coding: utf-8 -*-
"""本地曲库：扫描播放器的下载目录，给「曲库」页做列表。

文件名按「歌手 - 歌名.mp3」解析；解析不出就整个当歌名。
目录默认 D:\\KuGou（酷狗下载目录），可以在 config.json 里加 "music_dirs" 覆盖。
"""
from __future__ import annotations

import json
import os

from core.appinfo import CONFIG_PATH

EXTS = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".wma", ".aac", ".ape"}

# 曲库默认目录：先找酷狗常见下载位置，都没有就是空列表（在 config.json 里配 music_dirs）
_DEFAULT_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), "Music"),
    os.path.join(os.path.expanduser("~"), "Documents", "KuGouMusic"),
    r"D:\KuGou",
]


def _default_dirs() -> list:
    dirs = [d for d in _DEFAULT_CANDIDATES if os.path.isdir(d)]
    return dirs or []

# PotPlayer 的命令行入口，用来点歌就播
POT_CANDIDATES = [
    r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
    r"C:\Program Files\DAUM\PotPlayer\PotPlayer64.exe",
    r"C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe",
]


def music_dirs() -> list:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            dirs = json.load(f).get("music_dirs")
        if dirs:
            return [str(d) for d in dirs]
    except Exception:
        pass
    return _default_dirs()


def player_exe() -> str | None:
    for p in POT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def scan(dirs: list | None = None, max_depth: int = 2) -> list:
    """返回 [{path,title,artist,ext,size}]，按歌手+歌名排序。目录扫描很快，25 首级别无感。"""
    dirs = dirs or music_dirs()
    items = []
    seen = set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        base_depth = d.rstrip("\\").count(os.sep)
        for root, subs, files in os.walk(d):
            depth = root.rstrip("\\").count(os.sep) - base_depth
            if depth >= max_depth:
                subs[:] = []
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in EXTS:
                    continue
                full = os.path.join(root, f)
                if full.lower() in seen:
                    continue
                seen.add(full.lower())
                stem = os.path.splitext(f)[0]
                if " - " in stem:
                    artist, title = stem.split(" - ", 1)
                else:
                    artist, title = "", stem
                try:
                    size = os.path.getsize(full)
                except Exception:
                    size = 0
                items.append(
                    {
                        "path": full,
                        "title": title.strip(),
                        "artist": artist.strip() or "未知歌手",
                        "ext": ext.lstrip("."),
                        "size": size,
                    }
                )
    items.sort(key=lambda x: (x["artist"].lower(), x["title"].lower()))
    return items


def launch(path: str, background: bool = True) -> tuple:
    """用 PotPlayer 打开一首。返回 (成功, 说明)。

    background=True 时交给 core.potplayer：复用实例换歌并把窗口藏起来。
    """
    exe = player_exe()
    if exe is None:
        return False, "没找到 PotPlayer，装一个或者改 config.json 里的播放器路径"
    if not os.path.exists(path):
        return False, "文件不见了"
    try:
        from core import potplayer
    except Exception:
        potplayer = None
    if potplayer is not None:
        return potplayer.launch(exe, path, background=background)
    try:
        import subprocess

        subprocess.Popen([exe, path], close_fds=True)
        return True, "ok"
    except Exception as e:
        return False, f"启动失败：{e}"
