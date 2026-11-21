# -*- coding: utf-8 -*-
"""桌面灵动岛入口。

用法：
    python main.py              正常启动（普通权限：网络列为连接数模式）
    python main.py --check      只采一次系统数据并打印
    python main.py --snapshot   渲染自检图到 tools/（调试外观用）

以管理员身份启动时，网络列自动切换为 ETW 精确流量。
"""
from __future__ import annotations

import json
import logging
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DEPS = os.path.join(
    os.environ.get("APPDATA", ""), "Python", "Python313", "site-packages"
)
if getattr(sys, "frozen", False):
    # 打包后依赖都在 exe 里，不能再把外部 site-packages 插到前面，
    # 否则可能加载到版本不一致的 PySide6
    sys.path.insert(0, BASE)
else:
    for path in (DEPS, BASE):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)

from core import appinfo  # noqa: E402

appinfo.migrate_config()
LOG_DIR = appinfo.CFG_DIR
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=appinfo.LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("island")


def _single_instance() -> bool:
    try:
        import ctypes

        handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DoubaoDesktopIsland")
        ctypes.windll.kernel32.SetLastError(0)
        err = ctypes.windll.kernel32.GetLastError()
        if err == 183:  # ERROR_ALREADY_EXISTS
            return False
        return bool(handle)
    except Exception:
        return True


def run_check() -> None:
    from PySide6.QtCore import QCoreApplication, QTimer

    from core.metrics import MetricsWorker
    from core.netflow import NetFlow

    app = QCoreApplication(sys.argv)
    worker = MetricsWorker(1.0)
    netflow = NetFlow()

    def on_sample(d):
        print(json.dumps({k: d[k] for k in d}, ensure_ascii=False, indent=2))
        print("netflow mode:", netflow.mode, "|", netflow.detail)
        app.quit()

    worker.sampled.connect(on_sample)
    worker.start()
    QTimer.singleShot(5000, app.quit)
    app.exec()
    worker.stop()
    worker.wait(2000)


def run_snapshot() -> None:
    """渲染 监控 / 进程 / 音乐 三张自检图。"""
    import math
    import random
    from collections import deque

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from core.audio import AudioWorker, VolumeWorker
    from core.media import MediaWorker
    from core.metrics import MetricsWorker
    from core.netflow import NetFlow
    from core.processes import ProcessWorker
    from ui.island import IslandWindow

    app = QApplication(sys.argv)
    netflow = NetFlow()
    worker = MetricsWorker(1.0)
    proc = ProcessWorker(netflow, 2.0)
    media = MediaWorker()
    audio = AudioWorker()
    volume = VolumeWorker()
    win = IslandWindow(worker, proc, media, netflow, audio, volume)
    win.show()
    worker.start()
    proc.start()
    media.start()
    audio.start()
    volume.start()

    out_dir = os.path.join(BASE, "tools")
    os.makedirs(out_dir, exist_ok=True)

    base = {"cpu": 34, "gpu": 62, "ram": 78, "vram": 41}
    phase = {"cpu": 0.0, "gpu": 2.1, "ram": 4.2, "vram": 1.3}
    for k in base:
        win._hist[k] = deque(
            [
                max(4.0, min(99.0, base[k] + 16 * math.sin(i / 5.0 + phase[k]) + random.uniform(-3, 3)))
                for i in range(60)
            ],
            maxlen=60,
        )

    def _save(name):
        path = os.path.join(out_dir, name)
        win.grab().save(path, "PNG")
        log.info("snapshot -> %s", path)

    def shot(page, name, delay=600):
        if page:
            win.set_page(page)
            win._animate_to(True)
        QTimer.singleShot(delay, lambda: _save(name))

    win._animate_to(True)
    QTimer.singleShot(3600, lambda: shot("monitor", "page_monitor.png"))
    QTimer.singleShot(5600, lambda: shot("process", "page_process.png", 4200))
    QTimer.singleShot(10200, lambda: shot("music", "page_music.png", 900))
    QTimer.singleShot(12000, app.quit)
    app.exec()

    for w in (worker, proc, media, audio, volume):
        w.stop()
    for w in (worker, proc, media, audio, volume):
        w.wait(2000)
    netflow.close()


def run_app() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

    from core.audio import AudioWorker, VolumeWorker
    from core.media import MediaWorker
    from core.metrics import MetricsWorker
    from core.netflow import NetFlow
    from core.processes import ProcessWorker
    from core import autostart, fullscreen
    from ui.island import IslandWindow

    autostart.migrate_legacy()  # 旧版 DesktopHub 的自启值改写成新版，别开机起两份

    QApplication.setAttribute(Qt.AA_DontCreateNativeWidgetSiblings, True)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("桌面灵动岛")

    def make_icon() -> QIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QColor(16, 17, 21))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4, 12, 56, 40, 20, 20)
        p.setBrush(QColor(34, 211, 238))
        p.drawEllipse(16, 28, 12, 12)
        p.setBrush(QColor(167, 139, 250))
        p.drawEllipse(36, 28, 12, 12)
        p.end()
        return QIcon(pm)

    netflow = NetFlow()
    log.info("netflow mode=%s (%s)", netflow.mode, netflow.detail)

    worker = MetricsWorker(1.0)
    proc = ProcessWorker(netflow, 2.0)
    media = MediaWorker()
    audio = AudioWorker()
    volume = VolumeWorker()
    win = IslandWindow(worker, proc, media, netflow, audio, volume)
    win.start()
    worker.start()
    proc.start()
    media.start()
    audio.start()
    volume.start()

    tray = QSystemTrayIcon(make_icon(), app)
    tray.setToolTip(f"桌面灵动岛 — 拖动换位置；网络：{netflow.mode}")
    menu = QMenu()
    act_show = menu.addAction("显示 / 隐藏")
    menu.addSeparator()

    act_fs = menu.addAction("全屏时自动收起")
    act_fs.setCheckable(True)
    act_fs.setChecked(win.fullscreen_hide_enabled())
    act_fs.toggled.connect(win.set_fullscreen_hide)

    act_pot = menu.addAction("PotPlayer 后台播放")
    act_pot.setCheckable(True)
    act_pot.setChecked(win.pot_background_enabled())
    act_pot.toggled.connect(win.set_pot_background)

    act_auto = menu.addAction("开机自启")
    act_auto.setCheckable(True)
    act_auto.setChecked(autostart.is_enabled())
    def on_auto(on: bool) -> None:
        ok = autostart.set_enabled(on)
        if not ok:
            act_auto.blockSignals(True)
            act_auto.setChecked(not on)
            act_auto.blockSignals(False)
            tray.showMessage("桌面灵动岛", "写入开机自启失败（注册表被拦住）",
                             QSystemTrayIcon.Warning, 4000)
        else:
            tray.showMessage("桌面灵动岛", "开机自启已%s" % ("开启" if on else "关闭"),
                             QSystemTrayIcon.Information, 2500)

    act_auto.toggled.connect(on_auto)

    menu.addSeparator()
    act_snap = menu.addAction("贴回屏幕顶部居中")
    act_snap.triggered.connect(lambda: win.snap_to_top(animate=True))

    act_quit = menu.addAction("退出")

    def toggle():
        if win.isVisible():
            win.hide()
        else:
            # 正在全屏（游戏 / 全屏视频）时用户主动叫出来，就先别自动藏回去
            try:
                if fullscreen.foreground_is_fullscreen():
                    win._fs_suppress = True
            except Exception:
                pass
            win._fs_hidden = False
            win.show()
            win._keep_topmost()

    act_show.triggered.connect(toggle)
    act_quit.triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.show()

    def shutdown():
        for w in (worker, proc, media, audio, volume):
            w.stop()
        netflow.close()
        for w in (worker, proc, media, audio, volume):
            w.wait(2000)

    app.aboutToQuit.connect(shutdown)
    log.info("桌面灵动岛 已启动")
    sys.exit(app.exec())


def main() -> None:
    args = sys.argv[1:]
    if "--check" in args:
        run_check()
        return
    if "--snapshot" in args:
        run_snapshot()
        return
    if not _single_instance():
        log.info("已有实例在运行，退出本次启动")
        return
    try:
        run_app()
    except Exception:
        log.exception("启动失败")
        raise


if __name__ == "__main__":
    main()
