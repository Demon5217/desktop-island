# -*- coding: utf-8 -*-
"""开机自启：写 HKCU 的 Run 键，不牵扯系统服务、不需要管理员。

打包成 exe 后指向 exe 本身；用源码跑时指向 pythonw + main.py。
"""
from __future__ import annotations

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "DesktopIsland"
OLD_NAME = "DesktopHub"  # 改名前的旧值，迁移时清掉，免得开机起两份
BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)


def target_command() -> str:
    if getattr(sys, "frozen", False):
        return '"%s"' % os.path.abspath(sys.executable)
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s"' % (pyw, os.path.join(ROOT, "main.py"))


def is_enabled() -> bool:
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            val, _ = winreg.QueryValueEx(key, NAME)
            return bool(val)
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def enable() -> bool:
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, target_command())
        winreg.CloseKey(key)
        return True
    except Exception:
        return False


def disable() -> bool:
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, NAME)
        finally:
            winreg.CloseKey(key)
        return True
    except Exception:
        return False


def set_enabled(on: bool) -> bool:
    return enable() if on else disable()


def drop_legacy() -> bool:
    """删掉旧版 DesktopHub 的自启值（迁移用）。返回是否真的删掉了。"""
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            winreg.DeleteValue(key, OLD_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def migrate_legacy() -> bool:
    """旧版开着自启的，改写成新版路径，返回是否做了迁移。"""
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            old, _ = winreg.QueryValueEx(key, OLD_NAME)
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False
    if not old:
        return False
    ok = enable()
    if ok:
        drop_legacy()
    return ok
