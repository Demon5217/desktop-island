# -*- coding: utf-8 -*-
"""应用身份信息：名字、版本、配置目录。

改名（DesktopHub → DesktopIsland）后旧配置还在 %APPDATA%\\DesktopHub，
migrate_config() 会把它原样搬过来（只补缺失文件，不覆盖新目录里已有的）。
"""
from __future__ import annotations

import os
import shutil

APP_NAME = "DesktopIsland"
APP_TITLE = "桌面灵动岛"
APP_VERSION = "1.3"

_APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
CFG_DIR = os.path.join(_APPDATA, APP_NAME)
LEGACY_CFG_DIR = os.path.join(_APPDATA, "DesktopHub")

CONFIG_PATH = os.path.join(CFG_DIR, "config.json")
CLIP_PATH = os.path.join(CFG_DIR, "clipboard.json")
NOTES_PATH = os.path.join(CFG_DIR, "notes.json")
LOG_PATH = os.path.join(CFG_DIR, "app.log")


def migrate_config() -> list:
    """把旧目录里的文件补到新目录，返回搬过来的文件名。"""
    moved = []
    if not os.path.isdir(LEGACY_CFG_DIR):
        return moved
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
    except Exception:
        return moved
    for name in os.listdir(LEGACY_CFG_DIR):
        src = os.path.join(LEGACY_CFG_DIR, name)
        dst = os.path.join(CFG_DIR, name)
        try:
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.copy2(src, dst)
                moved.append(name)
        except Exception:
            pass
    return moved
