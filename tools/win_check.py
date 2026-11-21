import ctypes
from ctypes import wintypes

u = ctypes.windll.user32
u.EnumWindows.restype = wintypes.BOOL
found = []


def cb(hwnd, lparam):
    buf = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(hwnd, buf, 256)
    if buf.value and ("桌面灵动岛" in buf.value or "Desktop Island" in buf.value):
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        found.append((hwnd, buf.value, u.IsWindowVisible(hwnd),
                      (r.left, r.top, r.right - r.left, r.bottom - r.top)))
    return True


proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
u.EnumWindows(proc, 0)
for f in found:
    print("hwnd=%s title=%r visible=%s rect=%s" % f, flush=True)
print("count =", len(found))
