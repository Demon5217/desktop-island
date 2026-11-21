# -*- coding: utf-8 -*-
"""本地曲库：扫描用户指定的音乐目录，给「曲库」页做列表。

文件名按「歌手 - 歌名.mp3」解析；解析不出就整个当歌名。
**目录完全由用户配置**：config.json 的 "music_dirs"，界面上「＋ 目录」也能加。
不自动猜路径——不知道用户把歌放哪儿，猜错了只会显示一堆乱七八糟的东西。
"""
from __future__ import annotations

import json
import os

from core.appinfo import CONFIG_PATH

EXTS = {".mp3", ".flac", ".wav", ".m4a", ".ogg", ".wma", ".aac", ".ape"}


# PotPlayer 的命令行入口，用来点歌就播
POT_CANDIDATES = [
    r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe",
    r"C:\Program Files\DAUM\PotPlayer\PotPlayer64.exe",
    r"C:\Program Files (x86)\DAUM\PotPlayer\PotPlayerMini.exe",
]


def _read_cfg() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_cfg(cfg: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def music_dirs() -> list:
    """用户配置的音乐目录，没配就是空列表。"""
    dirs = _read_cfg().get("music_dirs") or []
    return [str(d) for d in dirs if str(d).strip()]


def add_dir(path: str) -> tuple:
    """把一个目录加进配置（去重、只收真实存在的目录）。返回 (成功, 说明)。"""
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(path):
        return False, "这个路径不是文件夹"
    cfg = _read_cfg()
    dirs = [str(d) for d in (cfg.get("music_dirs") or [])]
    key = os.path.normcase(path)
    if any(os.path.normcase(os.path.abspath(str(d))) == key for d in dirs):
        return True, "这个目录已经在曲库里了"
    dirs.append(path)
    cfg["music_dirs"] = dirs
    if not _write_cfg(cfg):
        return False, "写配置失败"
    return True, "已添加 %s" % path


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
