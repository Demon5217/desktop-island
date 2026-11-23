# -*- coding: utf-8 -*-
"""量竖版磁盘行单行需要多宽，确定 SIDE_W。"""
import os
import sys

sys.path.insert(0, r"D:\Doubao\桌面Hub")
sys.path.insert(0, os.path.join(os.environ["APPDATA"], "Python", "Python313", "site-packages"))

from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QApplication
from ui import theme as T

app = QApplication(sys.argv)
for size in (8, 7):
    fm = QFontMetrics(T.font(size, True, True))
    for s in ("R0B W0B", "R541K W223K", "R1.2M W3.4M", "R12.3M W45.6M", "R541K，W223K"):
        print(f"{size}px  {s!r:16} -> {fm.horizontalAdvance(s)}px", flush=True)
print("现有文字区宽：SIDE_W 62 时从 bar_x 起 =", 62 - 12, "px；从 tx 起 =", 62 - 19, "px", flush=True)
os._exit(0)
