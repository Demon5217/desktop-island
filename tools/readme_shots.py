# -*- coding: utf-8 -*-
"""给 README 生成干净截图：名字用新名「桌面灵动岛」，数据全用假数据，不碰真实剪贴板/笔记。"""
import os
import sys

sys.path.insert(0, r"D:\Doubao\桌面Hub")
sys.path.insert(0, os.path.join(os.environ["APPDATA"], "Python", "Python313", "site-packages"))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from core.media import MediaWorker
from core.metrics import MetricsWorker
from core.netflow import NetFlow
from core.processes import ProcessWorker
from ui.island import IslandWindow

OUT = r"D:\Doubao\桌面Hub\docs\screenshots"

FAKE_MEDIA = {"has_session": True, "playing": True, "title": "Snowdreams (雪之梦)",
              "artist": "Bandari", "album": "Mist 迷雾森林", "app": "PotPlayer",
              "duration": 316.0, "position": 132.0, "aumid": "PotPlayerMini64.exe"}

FAKE_CLIPS = [
    {"kind": "files", "files": [r"D:\Music\Bandari - Snowdreams.mp3"],
     "src": "Explorer.exe", "time": "18:41"},
    {"kind": "text", "text": "https://github.com/Demon5217", "src": "msedge.exe", "time": "18:39"},
    {"kind": "text", "text": "明天要买的：咖啡豆、护眼灯、USB 转接头", "src": "Notepad.exe", "time": "18:35"},
    {"kind": "text", "text": "def render(pm): return pm.scaled(256, 256)", "src": "Code.exe", "time": "18:30"},
    {"kind": "files", "files": [r"D:\Docs\周报.md", r"D:\Docs\设计稿.psd"],
     "src": "Explorer.exe", "time": "18:22"},
    {"kind": "text", "text": "PotPlayer /current /play 换歌不会弹窗", "src": "notepad++.exe", "time": "18:15"},
]

FAKE_NOTES = [
    {"id": "n1", "title": "灵动岛的窗口层级笔记",
     "plain": "Qt.Tool + Frameless + StaysOnTop，加 WS_EX_TOOLWINDOW / WS_EX_NOACTIVATE。",
     "html": "<p><b>Qt.Tool + Frameless + StaysOnTop</b>，再加 WS_EX_TOOLWINDOW / WS_EX_NOACTIVATE。</p>",
     "md": "**Qt.Tool + Frameless + StaysOnTop**", "created": "2026-11-20 21:10",
     "updated": "2026-11-20 21:10"},
    {"id": "n2", "title": "全屏检测的两个坑",
     "plain": "GetForegroundWindow 会被 shell 的 CoreWindow 占住，改成按 z-order 往下扫。",
     "html": "<p>GetForegroundWindow 会被 shell 的 CoreWindow 占住，改成按 z-order 往下扫。</p>",
     "md": "GetForegroundWindow 会被 shell 的 CoreWindow 占住", "created": "2026-11-21 10:02",
     "updated": "2026-11-21 10:05"},
    {"id": "n3", "title": "频谱 AGC 调参",
     "plain": "低频压得太狠就整条线不动，给每段加自适应上限后 32 段都会跳。",
     "html": "<p>低频压得太狠就整条线不动，给每段加自适应上限后 32 段都会跳。</p>",
     "md": "低频压得太狠就整条线不动", "created": "2026-11-20 23:40",
     "updated": "2026-11-20 23:41"},
]

app = QApplication(sys.argv)
nf = NetFlow()
m = MetricsWorker(1.0)
p = ProcessWorker(nf, 2.0)
md = MediaWorker()
win = IslandWindow(m, p, md, nf)
win.show()
win._maybe_collapse = lambda: None  # 探针模式下鼠标不在窗口上，禁用自动收起
m.start()
p.start()


def save(name):
    win.grab().save(os.path.join(OUT, name + ".png"), "PNG")
    print("[shot]", name, flush=True)


def run():
    # 监控页：先攒够折线图的历史样本（每秒一个点）再截，否则曲线几乎是空的
    win.set_page("monitor")
    win._animate_to(True)
    QTimer.singleShot(45000, lambda: (save("monitor"), step_process()))


def step_process():
    win.set_page("process")
    win._animate_to(True)
    QTimer.singleShot(2600, lambda: (save("process"), step_music()))


def step_music():
    win._on_media(FAKE_MEDIA)
    win.set_page("music")
    win._animate_to(True)
    QTimer.singleShot(1500, lambda: (save("music"), step_clip()))


def step_clip():
    if win._clip is not None:
        win._clip.items = list(FAKE_CLIPS)
    win.set_page("clipboard")
    win._animate_to(True)
    QTimer.singleShot(900, lambda: (save("clipboard"), step_notes()))


def step_notes():
    win._notes.items = list(FAKE_NOTES)
    win.set_page("notes")
    win._animate_to(True)
    QTimer.singleShot(600, step_notes2)


def step_notes2():
    win._note_load("n1")
    QTimer.singleShot(900, lambda: (save("notes"), step_capsule()))


def step_capsule():
    win._animate_to(False)
    QTimer.singleShot(1600, lambda: (save("capsule"), app.quit()))


QTimer.singleShot(900, run)
app.exec()
os._exit(0)
