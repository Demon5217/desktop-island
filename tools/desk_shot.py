# -*- coding: utf-8 -*-
"""DPI-aware 真实桌面截图：截取灵动岛所在区域，验证贴边渲染。"""
import ctypes
import sys
from ctypes import wintypes

OUT = sys.argv[1] if len(sys.argv) > 1 else r"D:\Doubao\桌面Hub\tools\desk_shot.png"
RECT = (0, 0, 1707, 912)     # 逻辑坐标，整屏可用区

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

gdi = ctypes.windll.gdi32
user = ctypes.windll.user32

scale = user.GetDpiForSystem() / 96.0
w = int(RECT[2] * scale)
h = int(RECT[3] * scale)

hdc = user.GetDC(0)
mem = gdi.CreateCompatibleDC(hdc)
bmp = gdi.CreateCompatibleBitmap(hdc, w, h)
gdi.SelectObject(mem, bmp)
gdi.StretchBlt(mem, 0, 0, w, h, hdc, int(RECT[0] * scale), int(RECT[1] * scale),
               w, h, 0x00CC0020)

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


info = BITMAPINFOHEADER()
info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
info.biWidth = w
info.biHeight = -h
info.biPlanes = 1
info.biBitCount = 32
info.biCompression = 0

buf = ctypes.create_string_buffer(w * h * 4)
gdi.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
gdi.DeleteObject(bmp)
gdi.DeleteDC(mem)
user.ReleaseDC(0, hdc)

from PIL import Image
img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
img.convert("RGB").save(OUT, "PNG")
print("saved", OUT, img.size, "scale", scale)
