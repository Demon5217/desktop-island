import os
import sys

sys.path.insert(0, os.path.join(os.environ["APPDATA"], "Python", "Python313", "site-packages"))

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QApplication

app = QApplication(sys.argv)


def render(size: int) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    s = size / 64.0
    p.setBrush(QColor(16, 17, 21))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4 * s, 12 * s, 56 * s, 40 * s, 20 * s, 20 * s)
    p.setBrush(QColor(34, 211, 238))
    p.drawEllipse(16 * s, 28 * s, 12 * s, 12 * s)
    p.setBrush(QColor(167, 139, 250))
    p.drawEllipse(36 * s, 28 * s, 12 * s, 12 * s)
    p.end()
    return pm


out = os.path.join(r"D:\Doubao\桌面Hub\tools", "desktopisland.ico")
big = render(256)
# Qt 默认不写 .ico，先用 PNG 存一版，再手工拼一个 ICO 头
png = os.path.join(r"D:\Doubao\桌面Hub\tools", "desktopisland.png")
big.save(png, "PNG")
data = open(png, "rb").read()
import struct

n = 1
header = struct.pack("<HHH", 0, 1, n)
entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(data), 22)
with open(out, "wb") as f:
    f.write(header)
    f.write(entry)
    f.write(data)
print("ico ->", out, os.path.getsize(out), flush=True)
os._exit(0)
