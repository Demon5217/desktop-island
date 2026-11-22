# -*- coding: utf-8 -*-
"""灵动岛主窗口。

v0.2 起分三页：
  monitor  硬件总览（CPU/GPU/内存/显存 + 网络磁盘）
  process  进程占用排行，可结束进程
  music    系统媒体会话（SMTC）控制与显示

设计要点：
1. 完全自绘（paintEvent），动画只改 geometry，不重建控件树，过程丝滑。
2. 只做普通顶层窗口（Tool + Frameless + StaysOnTop），不碰 Progman/WorkerW，
   因此 Wallpaper Engine 的桌面层完全不受影响。
3. 所有可点击区域在绘制时记录成矩形，鼠标事件里做命中测试。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
from collections import deque

from PySide6.QtCore import (
    QEasingCurve,
    QMimeData,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDrag,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
    QTextListFormat,
)
from PySide6.QtWidgets import QApplication, QLineEdit, QMenu, QTextEdit, QWidget

from core.notes import NoteStore
from core.mdconv import html_to_md, plain_to_md
from core import appinfo, fullscreen
from ui import theme as T

CFG_DIR = appinfo.CFG_DIR
CFG_PATH = appinfo.CONFIG_PATH


class NoteEdit(QTextEdit):
    """记事编辑器：标题行回车后自动回到正文，避免大字/缩进一路粘下去。"""

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            cur = self.textCursor()
            bf = cur.blockFormat()
            f = cur.charFormat().font()
            big = bf.headingLevel() > 0 or (f.pixelSize() > 0 and f.pixelSize() > 16)
            super().keyPressEvent(e)
            if big:
                c = self.textCursor()
                nb = c.blockFormat()
                nb.setHeadingLevel(0)
                nb.setLeftMargin(0)
                nb.setIndent(0)
                c.setBlockFormat(nb)
                cf = QTextCharFormat()
                cf.setFont(T.font(14))
                cf.setFontWeight(400)
                cf.setFontItalic(False)
                cf.setFontUnderline(False)
                c.setCharFormat(cf)
                self.setTextCursor(c)
            return
        super().keyPressEvent(e)


PAGE_LABELS = (
    ("monitor", "监控"),
    ("music", "音乐"),
    ("clipboard", "剪贴板"),
    ("notes", "记事"),
)
# 监控页四张卡片 → 点进去时进程列表按哪个指标排序
CARD_SORTS = ("cpu", "gpu", "mem", "vram")
# 进程页只展示这一项，避免无关列干扰
FOCUS_META = {
    "cpu": "CPU",
    "gpu": "GPU",
    "mem": "内存",
    "vram": "显存",
    "net_up": "上传",
    "net_down": "下载",
    "disk_r": "读盘",
    "disk_w": "写盘",
}
# 这些指标是速率，换算成 B/s 显示
RATE_FOCUS = {"net_up", "net_down", "disk_r", "disk_w"}


class IslandWindow(QWidget):
    INSET = 12
    SNAP_DIST = 48          # 胶囊本体离屏幕任一边小于这个距离，松手就吸附上去
    COLLAPSED_W = 480
    COLLAPSED_H_IDLE = 52
    COLLAPSED_H_PLAY = 68
    SIDE_W = 62             # 贴左右两侧时的竖向胶囊（本体尺寸，不含 INSET）
    SIDE_H = 232
    PAGE_SIZES = {
        "monitor": QSize(640, 350),
        "process": QSize(640, 412),
        "music": QSize(640, 500),
        "clipboard": QSize(640, 412),
        "notes": QSize(640, 460),
    }
    LIB_ROW_H = 30.0
    HIST_MAX = 60

    def __init__(self, worker, proc_worker=None, media_worker=None, netflow=None,
                 audio_worker=None, volume_worker=None) -> None:
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setWindowTitle(appinfo.APP_TITLE)

        self._worker = worker
        self._proc_worker = proc_worker
        self._media_worker = media_worker
        self._netflow = netflow
        self._audio = audio_worker
        self._volume = volume_worker
        self._spec: list = [0.0] * 32
        self._peak: list = [0.0] * 32       # 频谱峰值保持
        self._spec_level = 0.0
        self._vol_value = 1.0
        self._vol_hit: QRectF | None = None
        self._vol_drag = False
        self._theme_colors = (QColor(167, 139, 250), QColor(56, 189, 248))
        self._theme_title = None
        self._lib: list = []
        self._lib_scroll = 0
        self._lib_rows = 5
        self._lib_hits: list = []
        self._clip = None
        self._clip_scroll = 0
        self._clip_hits: list = []
        self._press_row = None
        self._press_pos = None
        self._suppress_capture = False

        # 记事本
        self._notes = NoteStore()
        self._note_cur: str | None = None
        self._note_scroll = 0
        self._note_rows = 8
        self._note_hits: list = []
        self._note_btn_hits: dict = {}
        self._note_loading = False
        self._note_editor: QTextEdit | None = None
        self._note_search: QLineEdit | None = None
        self._note_save_timer = QTimer(self)
        self._note_save_timer.setSingleShot(True)
        self._note_save_timer.setInterval(900)
        self._note_save_timer.timeout.connect(self._save_note_now)
        self._mk_note_editor()
        self._mk_note_search()

        self._data: dict = {}
        self._hist = {k: deque(maxlen=self.HIST_MAX) for k in ("cpu", "gpu", "ram", "vram")}
        self._procs: list = []
        self._proc_focus: str | None = None
        self._media: dict = {}
        self._cover_pm: QPixmap | None = None
        self._cover_title = None

        self._expanded = False
        self._page = "monitor"
        self._dragging = False
        self._drag_offset: QPoint | None = None
        self._clock = ""
        self._date = ""

        self._proc_scroll = 0
        self._sort_key = "cpu"
        self._sort_desc = True
        self._shown_rows: list = []
        self._confirm: dict | None = None
        self._toast = ""
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(2600)
        self._toast_timer.timeout.connect(self._clear_toast)

        # 命中区域（绘制时填充）
        self._tab_hits: dict = {}
        self._head_hits: dict = {}
        self._row_hits: list = []
        self._close_hits: list = []
        self._music_hits: dict = {}
        self._player_btn_hit = None
        self._lib_add_hit = None
        self._hold_open = False
        self._pot_bg = True
        self._confirm_hits: dict = {}
        self._card_hits: dict = {}
        self._io_hits: dict = {}

        self._anim = QPropertyAnimation(self, b"geometry", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._snap_pending = False
        self._anim.finished.connect(self._on_anim_finished)

        # 标题栏的下划线指示器：切页时从旧位置滑过去
        self._tab_ind = None            # (x, w) 当前指示器位置，窗口坐标
        self._tab_active = (0.0, 0.0)   # 当前页 tab 的 (x, w)，指示器未初始化时兜底
        self._tab_from = (0.0, 0.0)
        self._tab_to = (0.0, 0.0)
        self._tab_anim = QVariantAnimation(self)
        self._tab_anim.setDuration(220)
        self._tab_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._tab_anim.setStartValue(0.0)
        self._tab_anim.setEndValue(1.0)
        self._tab_anim.valueChanged.connect(self._on_tab_anim)

        # 内容动效：展开时整体淡入 + 卡片错峰出现；切页时新页淡入上浮
        self._content_op = 1.0
        self._stagger = 1.0
        self._page_op = 1.0
        self._fx = QVariantAnimation(self)          # 整体淡入淡出
        self._fx.setEasingCurve(QEasingCurve.OutCubic)
        self._fx.valueChanged.connect(self._on_content_fx)
        self._fx_card = QVariantAnimation(self)     # 卡片错峰
        self._fx_card.setDuration(380)
        self._fx_card.setEasingCurve(QEasingCurve.OutCubic)
        self._fx_card.setStartValue(0.0)
        self._fx_card.setEndValue(1.0)
        self._fx_card.valueChanged.connect(self._on_stagger_fx)
        self._page_fx = QVariantAnimation(self)     # 切页
        self._page_fx.setDuration(200)
        self._page_fx.setEasingCurve(QEasingCurve.OutCubic)
        self._page_fx.setStartValue(0.0)
        self._page_fx.setEndValue(1.0)
        self._page_fx.valueChanged.connect(self._on_page_fx)

        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.setInterval(480)
        self._collapse_timer.timeout.connect(self._maybe_collapse)

        # 收起时先淡出内容（130ms），再切布局和尺寸
        self._collapse_anim = QTimer(self)
        self._collapse_anim.setSingleShot(True)
        self._collapse_anim.setInterval(130)
        self._collapse_anim.timeout.connect(self._apply_collapse)

        # 滚轮惯性
        self._scroll_vel = 0.0
        self._scroll_acc = 0.0
        self._inertia = QTimer(self)
        self._inertia.setInterval(26)
        self._inertia.timeout.connect(self._on_inertia)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)

        self._top_timer = QTimer(self)
        self._top_timer.setInterval(5000)
        self._top_timer.timeout.connect(self._keep_topmost)

        cfg = self._load_cfg()
        self._fs_enabled = bool(cfg.get("hide_on_fullscreen", True))
        self._pot_bg = bool(cfg.get("potplayer_bg", True))
        self._fs_hidden = False
        self._fs_suppress = False
        self._fs_timer = QTimer(self)
        self._fs_timer.setInterval(1200)
        self._fs_timer.timeout.connect(self._check_fullscreen)

        worker.sampled.connect(self._on_sample)
        if proc_worker is not None:
            proc_worker.sampled.connect(self._on_procs)
            proc_worker.error.connect(self._show_toast)
        if media_worker is not None:
            media_worker.updated.connect(self._on_media)
        if audio_worker is not None:
            audio_worker.spectrum.connect(self._on_spectrum)
        try:
            from core.clipstore import ClipStore

            self._clip = ClipStore()
        except Exception:
            self._clip = None
        try:
            QApplication.clipboard().dataChanged.connect(self._on_clip_changed)
        except Exception:
            pass
        self.setAcceptDrops(True)

        self._dock = "top"              # top / left / right / bottom / free
        self._dock_rect = None          # 收起态窗口矩形（尺寸变化都以它为准，不会漂）
        self._restore_geometry()
        self._on_tick()

    # ================================================================== 生命周期
    def start(self) -> None:
        self.show()
        self._apply_win32_style()
        self._tick.start()
        self._top_timer.start()
        if self._fs_enabled:
            self._fs_timer.start()

    def _apply_win32_style(self) -> None:
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_TOOLWINDOW = 0x00000080
            WS_EX_NOACTIVATE = 0x08000000
            u = ctypes.windll.user32
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        except Exception:
            pass

    def _set_activatable(self, on: bool) -> None:
        """记事页要打字，才临时允许窗口被激活；其它页保持"点击不抢焦点"。"""
        try:
            hwnd = int(self.winId())
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            u = ctypes.windll.user32
            style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if on:
                style &= ~WS_EX_NOACTIVATE
            else:
                style |= WS_EX_NOACTIVATE
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            if on:
                self.activateWindow()
        except Exception:
            pass

    def _keep_topmost(self) -> None:
        if not self.isVisible():
            return
        try:
            hwnd = int(self.winId())
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
            ctypes.windll.user32.SetWindowPos(
                hwnd, -1, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
            )
        except Exception:
            pass

    # ------------------------------------------------------------ 全屏自动隐藏
    def set_fullscreen_hide(self, on: bool) -> None:
        self._fs_enabled = bool(on)
        self._write_cfg(hide_on_fullscreen=self._fs_enabled)
        if self._fs_enabled:
            self._fs_timer.start()
        else:
            self._fs_timer.stop()
            self._fs_suppress = False
            if self._fs_hidden:
                self._fs_show()

    def fullscreen_hide_enabled(self) -> bool:
        return self._fs_enabled

    # ------------------------------------------------------------ PotPlayer 后台播放
    def set_pot_background(self, on: bool) -> None:
        """点曲库时是否把 PotPlayer 窗口藏起来（默认开）。"""
        self._pot_bg = bool(on)
        self._write_cfg(potplayer_bg=self._pot_bg)

    def pot_background_enabled(self) -> bool:
        return self._pot_bg

    def _fs_show(self) -> None:
        self._fs_hidden = False
        self.show()
        self._keep_topmost()

    def _check_fullscreen(self) -> None:
        if not self._fs_enabled:
            return
        try:
            full = fullscreen.foreground_is_fullscreen()
        except Exception:
            return
        if full:
            if self._fs_suppress:
                return
            if self.isVisible() and not self._fs_hidden:
                self._fs_hidden = True
                self.hide()
        else:
            # 退出全屏，用户手动叫出来的屏蔽也解除
            self._fs_suppress = False
            if self._fs_hidden:
                self._fs_show()

    # ------------------------------------------------------------ 配置读写
    def _load_cfg(self) -> dict:
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _write_cfg(self, **kv) -> None:
        cfg = self._load_cfg()
        cfg.update(kv)
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ================================================================== 数据
    def _on_tick(self) -> None:
        from datetime import datetime

        now = datetime.now()
        self._clock = now.strftime("%H:%M")
        week = "一二三四五六日"[now.weekday()]
        self._date = f"周{week}  {now.month}-{now.day:02d}"
        self.update()

    def _on_sample(self, d: dict) -> None:
        self._data = d
        self._hist["cpu"].append(d.get("cpu", 0.0) or 0.0)
        self._hist["ram"].append(d.get("ram", 0.0) or 0.0)
        gpu = d.get("gpu_util")
        if gpu is not None:
            self._hist["gpu"].append(float(gpu))
        total = d.get("vram_total") or 0.0
        if total > 0:
            self._hist["vram"].append(d.get("vram_used", 0.0) / total * 100.0)
        self.update()

    def _on_procs(self, rows: list) -> None:
        self._procs = rows
        if self._page == "process" and self._expanded:
            self.update()

    def _on_media(self, d: dict) -> None:
        self._media = d
        title = d.get("title") if d.get("has_session") else None
        if title != self._cover_title:
            self._cover_title = title
            cover = d.get("cover")
            pm = QPixmap()
            if cover and pm.loadFromData(cover):
                self._cover_pm = pm.scaled(
                    224, 224, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                )
            else:
                self._cover_pm = None
            self._update_theme()
        if not self._expanded or self._page in ("music", "monitor"):
            self.update()
        self._sync_collapsed_size()

    def _on_spectrum(self, bands: list, level: float) -> None:
        self._spec = bands
        self._spec_level = level
        # 峰值保持：掉得比涨得慢，柱子顶端留一小截，看起来更像有惯性的声电平
        for i, v in enumerate(bands):
            if i >= len(self._peak):
                break
            if v >= self._peak[i]:
                self._peak[i] = float(v)
            else:
                self._peak[i] = max(0.0, self._peak[i] - 0.045)
        playing = bool(self._media.get("playing"))
        if not self._expanded:
            # 收起胶囊：只有真有声音时重绘，不然 25fps 白刷
            if level > 0.015 or playing:
                self.update()
        elif self._page == "music":
            self.update()

    def _update_theme(self) -> None:
        """从封面抽一对主色，用作频谱渐变；没封面就回落到默认紫青。"""
        title = self._cover_title
        if title == self._theme_title:
            return
        self._theme_title = title
        c1 = QColor(167, 139, 250)
        c2 = QColor(56, 189, 248)
        pm = self._cover_pm
        if pm is not None and not pm.isNull():
            img = pm.scaled(16, 16, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).toImage()
            best = None
            best_score = -1.0
            ar = ag = ab = 0
            n = 0
            for y in range(img.height()):
                for x in range(img.width()):
                    c = img.pixelColor(x, y)
                    ar += c.red()
                    ag += c.green()
                    ab += c.blue()
                    n += 1
                    score = c.hsvSaturation() + c.value() * 0.45
                    if score > best_score:
                        best_score = score
                        best = c
            if n:
                avg = QColor(ar // n, ag // n, ab // n)
                if best is not None:
                    c1 = best
                c2 = avg
        # 太暗的颜色在深色底上看不见，提亮
        if c1.value() < 150:
            c1 = c1.lighter(150)
        if c2.value() < 95:
            c2 = c2.lighter(160)
        self._theme_colors = (c1, c2)

    def _spectrum_color(self, t: float) -> QColor:
        a, b = self._theme_colors
        t = max(0.0, min(1.0, t))
        return QColor(
            int(a.red() + (b.red() - a.red()) * t),
            int(a.green() + (b.green() - a.green()) * t),
            int(a.blue() + (b.blue() - a.blue()) * t),
        )

    def _show_toast(self, text: str) -> None:
        self._toast = text
        self._toast_timer.start()
        self.update()

    def _clear_toast(self) -> None:
        self._toast = ""
        self.update()

    # ================================================================== 尺寸与停靠
    DOCK_SIDES = ("top", "left", "right", "bottom", "free")

    def _is_side(self) -> bool:
        return self._dock in ("left", "right")

    def _collapsed_base(self) -> QSize:
        """胶囊本体尺寸（不含 INSET）：贴左右两侧时是竖的。"""
        if self._is_side():
            return QSize(self.SIDE_W, self.SIDE_H)
        return QSize(self.COLLAPSED_W, self._collapsed_h())

    def _content_size(self, expanded: bool) -> QSize:
        if expanded:
            base = self.PAGE_SIZES.get(self._page, self.PAGE_SIZES["monitor"])
        else:
            base = self._collapsed_base()
        return QSize(base.width() + 2 * self.INSET, base.height() + 2 * self.INSET)

    def _collapsed_h(self) -> int:
        """没音乐时胶囊压扁成一条细条，有音乐才给频谱留高度。"""
        m = self._media
        if m.get("has_session") and m.get("playing"):
            return self.COLLAPSED_H_PLAY
        return self.COLLAPSED_H_IDLE

    def _screen_area(self) -> QRect:
        screen = self.screen() or QApplication.primaryScreen()
        return screen.availableGeometry()

    @staticmethod
    def _cx(r: QRect) -> float:
        """几何中心（浮点，避免 QRect.center() 的 w-1 取整误差一层层累积）。"""
        return r.left() + r.width() / 2.0

    @staticmethod
    def _cy(r: QRect) -> float:
        return r.top() + r.height() / 2.0

    def _dock_rect_for(self, dock: str, size: QSize, ref: QRect | None = None) -> QRect:
        """按停靠边算出收起态窗口矩形。ref 用来保持「沿边居中」的那一侧不动。"""
        a = self._screen_area()
        if ref is None:
            ref = QRect(a.left() + (a.width() - size.width()) // 2, a.top(),
                        size.width(), size.height())
        if dock == "top":
            x = int(round(self._cx(ref) - size.width() / 2.0))
            y = a.top()
        elif dock == "bottom":
            x = int(round(self._cx(ref) - size.width() / 2.0))
            y = a.bottom() - size.height() + 1      # 底边贴任务栏上方
        elif dock == "left":
            x = a.left()
            y = int(round(self._cy(ref) - size.height() / 2.0))
        elif dock == "right":
            x = a.right() - size.width() + 1
            y = int(round(self._cy(ref) - size.height() / 2.0))
        else:
            x, y = ref.x(), ref.y()
        x = min(max(x, a.left()), a.right() - size.width() + 1)
        y = min(max(y, a.top()), a.bottom() - size.height() + 1)
        return QRect(x, y, size.width(), size.height())

    def _target_rect(self, expanded: bool) -> QRect:
        """展开态的目标矩形：顶/底向下或向上长，左/右向内展开。"""
        if not expanded:
            return QRect(self._dock_rect)
        size = self._content_size(True)
        d = self._dock_rect
        a = self._screen_area()
        if self._dock == "bottom":
            x = int(round(self._cx(d) - size.width() / 2.0))
            y = d.bottom() - size.height() + 1
        elif self._dock == "left":
            x = d.left()
            y = int(round(self._cy(d) - size.height() / 2.0))
        elif self._dock == "right":
            x = d.right() - size.width() + 1
            y = int(round(self._cy(d) - size.height() / 2.0))
        else:
            x = int(round(self._cx(d) - size.width() / 2.0))
            y = d.top()
        x = min(max(x, a.left()), a.right() - size.width() + 1)
        y = min(max(y, a.top()), a.bottom() - size.height() + 1)
        return QRect(int(x), int(y), size.width(), size.height())

    def _sync_collapsed_size(self) -> None:
        """播放状态变化时，收起胶囊的高度跟着变（沿停靠边对齐的那一侧不动）。"""
        size = self._content_size(False)
        if self._dock_rect is not None and self._dock_rect.size() != size:
            self._dock_rect = self._dock_rect_for(self._dock, size, QRect(self._dock_rect))
        if self._expanded or self._dragging:
            return
        g = self.geometry()
        if g.width() == size.width() and g.height() == size.height():
            return
        self._anim.stop()
        self.setGeometry(self._dock_rect)
        self.update()

    def _restore_geometry(self) -> None:
        cfg = {}
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception:
            cfg = {}
        dock = cfg.get("dock")
        self._dock = dock if dock in self.DOCK_SIDES else "top"
        size = self._content_size(False)
        pos = cfg.get("pos")
        ref = None
        if isinstance(pos, list) and len(pos) == 2:
            ref = QRect(int(pos[0]), int(pos[1]), size.width(), size.height())
        self._dock_rect = self._dock_rect_for(self._dock, size, ref)
        self.setGeometry(self._dock_rect)

    def _place_default(self) -> None:
        """回到顶部居中。"""
        self._apply_dock("top", animate=False, center=True)

    def snap_to_top(self, animate: bool = False) -> None:
        self._apply_dock("top", animate=animate, center=True)

    def _apply_dock(self, dock: str, animate: bool = True, center: bool = False) -> None:
        """换停靠边：重算收起态矩形，展开着就顺带把面板挪过去。"""
        if dock not in self.DOCK_SIDES:
            return
        old, self._dock = self._dock, dock
        size = self._content_size(False)
        ref = QRect(self._dock_rect or self.geometry())
        a = self._screen_area()
        # 横排 ↔ 竖排切换时，另一根轴没有沿边中心，直接回到屏幕中央
        if dock in ("top", "bottom") and (center or old in ("left", "right")):
            ref.setRect(a.left() + (a.width() - size.width()) // 2, ref.top(),
                        size.width(), size.height())
        elif dock in ("left", "right") and old in ("top", "bottom", "free"):
            ref.setRect(ref.left(), a.top() + (a.height() - size.height()) // 2,
                        size.width(), size.height())
        self._dock_rect = self._dock_rect_for(dock, size, ref)
        target = self._target_rect(self._expanded)
        if not animate:
            self._anim.stop()
            self.setGeometry(target)
            self._save_position()
            return
        self._snap_pending = True
        self._anim.stop()
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutBack)
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(target)
        self._anim.start()

    def _clamp_into_screen(self) -> None:
        size = self._content_size(False)
        self._dock_rect = self._dock_rect_for(self._dock, size, QRect(self.geometry()))
        self.setGeometry(self._dock_rect)

    def _on_anim_finished(self) -> None:
        if self._snap_pending:
            self._snap_pending = False
            self._save_position()

    # ------------------------------------------------------------ 动效回调
    def _on_tab_anim(self, v) -> None:
        fx, fw = self._tab_from
        tx, tw = self._tab_to
        self._tab_ind = (fx + (tx - fx) * float(v), fw + (tw - fw) * float(v))
        self.update()

    def _tab_rects(self) -> dict:
        """和 _paint_header 同一套算法，算出每个 tab 的 (x, 宽)。"""
        pad = 18
        x = self.INSET + pad + 100
        labels = PAGE_LABELS
        if self._page == "process":
            labels = tuple((k, "← 监控" if k == "monitor" else lb) for k, lb in PAGE_LABELS)
        out = {}
        for key, label in labels:
            f = T.font(10, key == self._page)
            w = QFontMetrics(f).horizontalAdvance(label) + 24
            out[key] = (x, w)
            x += w + 6
        return out

    def _start_tab_anim(self) -> None:
        tgt = self._tab_rects().get(self._page)
        if tgt is None:
            return
        if self._tab_ind is None:
            self._tab_ind = (float(tgt[0]), float(tgt[1]))
            return
        if abs(self._tab_ind[0] - tgt[0]) < 0.4 and abs(self._tab_ind[1] - tgt[1]) < 0.4:
            self._tab_ind = (float(tgt[0]), float(tgt[1]))
            return
        self._tab_from = (self._tab_ind[0], self._tab_ind[1])
        self._tab_to = (float(tgt[0]), float(tgt[1]))
        self._tab_anim.stop()
        self._tab_anim.start()

    def _on_content_fx(self, v) -> None:
        self._content_op = float(v)
        self.update()

    def _on_stagger_fx(self, v) -> None:
        self._stagger = float(v)
        self.update()

    def _on_page_fx(self, v) -> None:
        self._page_op = float(v)
        self.update()

    def _play_content_fx(self, opening: bool) -> None:
        self._fx.stop()
        self._fx_card.stop()
        if opening:
            self._fx.setDuration(220)
            self._fx.setStartValue(0.0)
            self._fx.setEndValue(1.0)
            self._fx.start()
            self._stagger = 0.0
            QTimer.singleShot(40, self._fx_card.start)
        else:
            self._fx.setDuration(140)
            self._fx.setStartValue(self._content_op)
            self._fx.setEndValue(0.0)
            self._fx.start()

    def _play_page_fx(self) -> None:
        self._page_fx.stop()
        self._page_op = 0.0
        self._page_fx.start()

    def _card_op(self, index: int) -> float:
        """卡片错峰：第 index 张在第 index*0.11 的进度之后才起，4 张依次铺开。"""
        s = self._stagger
        start = index * 0.11
        span = 0.6
        return max(0.0, min(1.0, (s - start) / span)) * self._content_op

    def _snap_on_release(self) -> bool:
        """松手时看离哪条边最近：48px 内就吸附，左右会顺带转成竖向。"""
        a = self._screen_area()
        g = self.geometry()
        body = g.adjusted(self.INSET, self.INSET, -self.INSET, -self.INSET)
        cands = [
            ("top", body.top() - a.top()),
            ("left", body.left() - a.left()),
            ("right", a.right() - body.right()),
            ("bottom", a.bottom() - body.bottom()),
        ]
        dock, dist = min(cands, key=lambda kv: kv[1])
        if dist > self.SNAP_DIST:
            self._dock = "free"
            cw = self._content_size(False)
            self._dock_rect = QRect(int(g.center().x() - cw.width() / 2), g.top(),
                                    cw.width(), cw.height())
            return False
        self._dock_rect = self._dock_rect_for(dock, self._content_size(False), QRect(g))
        self._apply_dock(dock, animate=True)
        return True

    def _animate_to(self, expanded: bool) -> None:
        if expanded:
            if self._collapse_anim.isActive():
                self._collapse_anim.stop()
            if self._expanded and self._content_op >= 0.99 and self._page_op >= 0.99:
                self._resize_to(True)      # 已经展开着切页，只补一次尺寸/位置
                return
            opening = not self._expanded or self._content_op < 0.99
            self._expanded = True
            self._page_op = 1.0
            if opening:
                self._play_content_fx(True)
            self._resize_to(True)
            return

        # 收起：先让面板内容淡出，再切成胶囊并收缩（否则会闪一下新布局）
        if self._expanded and not self._collapse_anim.isActive():
            self._play_content_fx(False)
            self._collapse_anim.start()
            return
        self._expanded = False
        self._resize_to(False)

    def _apply_collapse(self) -> None:
        self._expanded = False
        # 淡出只为「面板退场」服务，切回胶囊后必须复位，否则胶囊会一直是 0 透明度
        self._fx.stop()
        self._content_op = 1.0
        self._page_op = 1.0
        self._resize_to(False)
        self.update()

    def _resize_to(self, expanded: bool) -> None:
        cur = self.geometry()
        target = self._target_rect(expanded)
        self._anim.stop()
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.setStartValue(cur)
        self._anim.setEndValue(target)
        self._anim.start()
        self._sync_workers()

    def _sync_workers(self) -> None:
        """进程采样只在「展开 + 进程页」时才跑，收起时零开销。"""
        if self._proc_worker is not None:
            self._proc_worker.set_active(self._expanded and self._page == "process")

    def set_page(self, page: str) -> None:
        if page == self._page:
            return
        self._page = page
        self._proc_scroll = 0
        self._lib_scroll = 0
        self._clip_scroll = 0
        self._note_scroll = 0
        if page == "music" and not self._lib:
            self._load_library()
        if page == "notes":
            self._set_activatable(True)
            if self._note_cur is None and self._notes.items:
                self._note_load(self._notes.items[0]["id"])
        else:
            self._set_activatable(False)
            self._save_note_now()
        self._sync_workers()
        self._start_tab_anim()
        if self._expanded:
            self._play_page_fx()
            self._animate_to(True)
        else:
            self.update()

    def _open_process_for(self, key: str) -> None:
        """从监控卡片钻进进程列表：只看这张卡的指标，按它降序。"""
        self._proc_focus = key
        self._sort_key = key
        self._sort_desc = True
        self._proc_scroll = 0
        if self._page != "process":
            self.set_page("process")
        self.update()

    # ------------------------------------------------------------ 剪贴板
    def _on_clip_changed(self) -> None:
        if self._suppress_capture or self._clip is None:
            return
        try:
            md = QApplication.clipboard().mimeData()
        except Exception:
            return
        if md is None:
            return
        from core.clipstore import owner_process

        src = owner_process()
        added = False
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                added = self._clip.add_files(paths, src)
        if not added and md.hasText():
            added = self._clip.add_text(md.text(), src)
        if added and self._expanded and self._page == "clipboard":
            self.update()

    def _copy_clip_item(self, idx: int) -> None:
        if self._clip is None:
            return
        try:
            item = self._clip.items[idx]
        except IndexError:
            return
        self._suppress_capture = True
        cb = QApplication.clipboard()
        if item["kind"] == "text":
            cb.setText(item["text"])
        else:
            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(p) for p in item.get("files", [])])
            cb.setMimeData(md)
        QTimer.singleShot(400, self._release_capture)
        self._show_toast("已复制回剪贴板")

    def _save_clip_as_note(self, idx: int) -> None:
        """把一条剪贴板记录存成记事；文件条目存成一行一个路径的文本。"""
        if self._clip is None:
            return
        try:
            item = self._clip.items[idx]
        except IndexError:
            return
        if item["kind"] == "files":
            plain = "\n".join(item.get("files") or [])
        else:
            plain = item.get("text") or ""
        plain = plain.strip()
        if not plain:
            self._show_toast("这条是空的，没法存")
            return
        esc = plain.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = "<p>" + esc.replace("\n", "<br/>") + "</p>"
        it = self._notes.add(html, plain, plain_to_md(plain))
        self._show_toast("已存为记事：%s" % it.get("title", ""))

    def _release_capture(self) -> None:
        self._suppress_capture = False

    def _start_clip_drag(self, idx: int) -> None:
        if self._clip is None:
            return
        try:
            item = self._clip.items[idx]
        except IndexError:
            return
        md = QMimeData()
        if item["kind"] == "files":
            md.setUrls([QUrl.fromLocalFile(p) for p in item.get("files", [])])
        else:
            md.setText(item["text"])
        drag = QDrag(self)
        drag.setMimeData(md)
        drag.exec(Qt.CopyAction)

    def dragEnterEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasUrls() or md.hasText():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        if self._clip is None:
            return
        md = e.mimeData()
        from core.clipstore import owner_process

        src = owner_process()
        added = False
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                added = self._clip.add_files(paths, src)
                self._show_toast(f"已记入剪贴板：{len(paths)} 个文件")
        if not added and md.hasText():
            added = self._clip.add_text(md.text(), "拖入")
            self._show_toast("已记入剪贴板")
        e.acceptProposedAction()
        if added and self._page == "clipboard":
            self.update()

    def _clip_visible(self) -> int:
        size = self.PAGE_SIZES["clipboard"]
        inner_h = size.height() - 18 - (18 + 40) - 18
        return max(1, int((inner_h - 28 - 20) // 32))

    # ------------------------------------------------------------ 记事（富文本）
    NOTE_LIST_W = 176.0
    NOTE_ROW_H = 44.0

    def _mk_note_editor(self) -> None:
        ed = NoteEdit(self)
        ed.setAcceptRichText(True)
        ed.setUndoRedoEnabled(True)
        ed.setFrameShape(QTextEdit.NoFrame)
        ed.setStyleSheet(
            "QTextEdit{background: rgba(255,255,255,9); border:1px solid rgba(255,255,255,18);"
            " border-radius:10px; color:#E8E8F0; padding:8px 10px;"
            " selection-background-color:#6D5AE6; selection-color:#ffffff;}"
            " QScrollBar:vertical{background:transparent; width:8px; margin:2px;}"
            " QScrollBar::handle:vertical{background:rgba(255,255,255,40); border-radius:4px;"
            " min-height:24px;}"
            " QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical{height:0;}"
        )
        f = T.font(14)
        ed.setFont(f)
        ed.setTabChangesFocus(False)
        ed.hide()
        ed.textChanged.connect(self._on_note_text)
        self._note_editor = ed

    def _mk_note_search(self) -> None:
        s = QLineEdit(self)
        s.setPlaceholderText("搜标题 / 正文")
        s.setClearButtonEnabled(True)
        s.setFont(T.font(11))
        s.setStyleSheet(
            "QLineEdit{background: rgba(255,255,255,8); border:1px solid rgba(255,255,255,16);"
            " border-radius:8px; color:#E8E8F0; padding:2px 8px;}"
            " QLineEdit:focus{border:1px solid rgba(255,255,255,45);}"
        )
        s.hide()
        s.textChanged.connect(self._on_note_search)
        self._note_search = s

    def _on_note_search(self, _t: str) -> None:
        self._note_scroll = 0
        self.update()

    def _note_filtered(self) -> list:
        items = self._notes.items
        q = (self._note_search.text() if self._note_search is not None else "").strip().lower()
        if not q:
            return items
        return [
            it for it in items
            if q in (it.get("title") or "").lower() or q in (it.get("plain") or "").lower()
        ]

    def _layout_note_editor(self) -> None:
        ed = self._note_editor
        if ed is None:
            return
        if not self._expanded or self._page != "notes":
            if ed.isVisible():
                ed.hide()
            if self._note_search is not None and self._note_search.isVisible():
                self._note_search.hide()
            return
        body = QRectF(
            self.INSET,
            self.INSET,
            self.width() - 2 * self.INSET,
            self.height() - 2 * self.INSET,
        )
        inner = body.adjusted(18, 58, -18, -18)
        r = QRectF(
            inner.left() + self.NOTE_LIST_W + 16,
            inner.top() + 52,
            inner.width() - self.NOTE_LIST_W - 16,
            inner.height() - 52,
        )
        target = QRect(int(r.left()), int(r.top()), max(80, int(r.width())), max(60, int(r.height())))
        if ed.geometry() != target:
            ed.setGeometry(target)
        if not ed.isVisible():
            ed.show()
        ed.raise_()

        s = self._note_search
        if s is not None:
            box = QRect(int(inner.left()), int(inner.top() + 25),
                        max(80, int(self.NOTE_LIST_W)), 24)
            if s.geometry() != box:
                s.setGeometry(box)
            if not s.isVisible():
                s.show()
            s.raise_()

    def _note_load(self, nid: str | None) -> None:
        """把某条笔记载入编辑区；nid=None 表示清空新建。"""
        ed = self._note_editor
        if ed is None:
            return
        item = self._notes.get(nid) if nid else None
        self._note_cur = item["id"] if item else None
        self._note_loading = True
        if item:
            ed.setHtml(item.get("html") or "")
            ed.moveCursor(QTextCursor.Start)
        else:
            ed.clear()
        self._note_loading = False
        ed.setEnabled(item is not None)
        self.update()

    def _on_note_text(self) -> None:
        if self._note_loading or self._note_cur is None:
            return
        self._note_save_timer.start()

    def _save_note_now(self) -> None:
        ed = self._note_editor
        if ed is None or self._note_cur is None:
            return
        html = ed.toHtml()
        plain = ed.toPlainText()
        md = html_to_md(html) if plain.strip() else ""
        self._notes.update(self._note_cur, html, plain, md)
        # 标题可能变了，列表要重画
        self.update()

    def _new_note(self) -> None:
        self._save_note_now()
        # 搜索词会把新笔记（标题「未命名」）挡在外面，先清掉
        if self._note_search is not None and self._note_search.text():
            self._note_search.clear()
        item = self._notes.create()
        self._note_scroll = 0
        self._note_load(item["id"])
        if self._note_editor is not None:
            self._note_editor.setFocus()

    def _note_delete(self) -> None:
        if self._note_cur is None:
            self._show_toast("先选中一条笔记")
            return
        self._notes.remove(self._note_cur)
        self._note_cur = None
        self._note_scroll = 0
        self._note_load(None)
        self._show_toast("已删除")

    def _note_export(self) -> None:
        self._save_note_now()
        nid = self._note_cur
        if nid is None:
            self._show_toast("先选中一条笔记")
            return
        item = self._notes.get(nid)
        if not item:
            return
        try:
            path = self._notes.export_md(item)
            self._show_toast("已导出 %s" % os.path.basename(path))
        except Exception as e:
            self._show_toast("导出失败：%s" % e)

    def _note_visible(self) -> int:
        return max(1, self._note_rows)

    def _note_apply_fmt(self, kind: str) -> None:
        ed = self._note_editor
        if ed is None or self._note_cur is None:
            self._show_toast("先新建或选中一条笔记")
            return
        cur = ed.textCursor()
        fmt = cur.charFormat()
        if kind == "bold":
            w = 400 if fmt.fontWeight() >= 700 else 700
            fmt.setFontWeight(w)
            cur.mergeCharFormat(fmt)
            ed.setFontWeight(w)
        elif kind == "italic":
            v = not fmt.fontItalic()
            fmt.setFontItalic(v)
            cur.mergeCharFormat(fmt)
            ed.setFontItalic(v)
        elif kind == "underline":
            v = not fmt.fontUnderline()
            fmt.setFontUnderline(v)
            cur.mergeCharFormat(fmt)
            ed.setFontUnderline(v)
        elif kind == "h2":
            # Qt 的 headingLevel 只改段落标记，字号要自己跟上，否则看不出是标题
            bf = cur.blockFormat()
            on = bf.headingLevel() == 0
            bf.setHeadingLevel(2 if on else 0)
            cur.setBlockFormat(bf)
            sel = QTextCursor(cur)
            sel.select(QTextCursor.BlockUnderCursor)
            cf = QTextCharFormat()
            cf.setFont(T.font(20 if on else 14, on))
            sel.mergeCharFormat(cf)
            # 光标落到块尾，别把整块标题一直选着（一打字就替换掉）
            tail = QTextCursor(sel)
            tail.movePosition(QTextCursor.EndOfBlock)
            ed.setTextCursor(tail)
            # 空块 merge 不生效，得同时改「之后输入」的格式
            ed.setCurrentCharFormat(cf)
        elif kind == "ul":
            if cur.currentList():
                cur.setBlockFormat(cur.blockFormat())
                cur.currentList().remove(cur.block())
            else:
                cur.createList(QTextListFormat.ListDisc)
        elif kind == "clear":
            # 段落格式复位：PySide6 的 QTextBlockFormat 没有 setListFormat，
            # 去掉列表用 currentList().remove()，缩进用 setLeftMargin / setIndent
            cf = QTextCharFormat()
            cf.setFont(T.font(14))
            cf.setFontWeight(400)
            cf.setFontItalic(False)
            cf.setFontUnderline(False)
            if cur.hasSelection():
                cur.mergeCharFormat(cf)
            else:
                # 无选区：对「之后输入的文字」生效
                cur.setCharFormat(cf)
            if cur.currentList():
                try:
                    cur.currentList().remove(cur.block())
                except Exception:
                    pass
            bf = cur.blockFormat()
            bf.setHeadingLevel(0)
            bf.setLeftMargin(0)
            bf.setIndent(0)
            cur.setBlockFormat(bf)
            ed.setTextCursor(cur)
            ed.setCurrentCharFormat(cf)
        ed.setFocus()
        self.update()

    def _paint_notes(self, p: QPainter, body: QRectF) -> None:
        pad = 18
        inner = body.adjusted(pad, pad + 40, -pad, -pad)
        self._note_hits = []
        self._note_btn_hits = {}

        # 顶栏：列表标题 + 右侧三个操作
        p.setFont(T.font(9))
        p.setPen(T.DIM)
        total = len(self._notes.items)
        q = (self._note_search.text() if self._note_search is not None else "").strip()
        head = "记事 · %d 篇" % total if not q else "记事 · 命中 %d / %d" % (
            len(self._note_filtered()), total)
        p.drawText(QRectF(inner.left(), inner.top(), self.NOTE_LIST_W + 40, 22),
                   Qt.AlignLeft | Qt.AlignVCenter, head)

        btns = [
            ("new", "＋ 新建", -1),
            ("export", "导出 MD", -2),
            ("del", "删除", -3),
        ]
        bx = inner.right()
        for key, label, _ in btns:
            p.setFont(T.font(9))
            w = QFontMetrics(p.font()).horizontalAdvance(label) + 22
            rect = QRectF(bx - w, inner.top() - 1, w, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 18))
            rp = QPainterPath()
            rp.addRoundedRect(rect, 11, 11)
            p.drawPath(rp)
            p.setPen(T.MUTED)
            p.drawText(rect, Qt.AlignCenter, label)
            self._note_btn_hits[key] = rect
            bx -= w + 8

        # 竖分隔
        p.setPen(QPen(QColor(255, 255, 255, 12), 1.0))
        p.drawLine(
            QPointF(inner.left() + self.NOTE_LIST_W + 3, inner.top() + 24),
            QPointF(inner.left() + self.NOTE_LIST_W + 3, inner.bottom()),
        )

        # 编辑区上方的格式条
        ex = inner.left() + self.NOTE_LIST_W + 16
        ew = inner.width() - self.NOTE_LIST_W - 16
        fmts = [
            ("bold", "B", True),
            ("italic", "I", False),
            ("underline", "U", False),
            ("h2", "H", False),
            ("ul", "• 列表", False),
            ("clear", "清格式", False),
        ]
        fx = ex
        for key, label, strong in fmts:
            p.setFont(T.font(9, strong))
            w = QFontMetrics(p.font()).horizontalAdvance(label) + 18
            fr = QRectF(fx, inner.top() + 26, w, 21)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 16))
            fp = QPainterPath()
            fp.addRoundedRect(fr, 8, 8)
            p.drawPath(fp)
            p.setPen(T.MUTED)
            p.drawText(fr, Qt.AlignCenter, label)
            self._note_btn_hits["fmt_" + key] = fr
            fx += w + 6

        # 左侧列表（上方是搜索框）
        list_top = inner.top() + 58
        visible = max(1, int((inner.bottom() - list_top) // self.NOTE_ROW_H))
        self._note_rows = visible
        items = self._note_filtered()

        if not items:
            p.setFont(T.font(10))
            p.setPen(T.MUTED)
            has_q = bool((self._note_search.text() if self._note_search else "").strip())
            p.drawText(QRectF(inner.left(), list_top + 10, self.NOTE_LIST_W, 60),
                       Qt.AlignLeft | Qt.TextWordWrap,
                       "没有匹配的笔记。\n换个关键词试试。" if has_q
                       else "还没有笔记。\n点右上角「＋ 新建」开始写。")
            return

        if self._note_scroll > max(0, len(items) - visible):
            self._note_scroll = max(0, len(items) - visible)
        shown = items[self._note_scroll:self._note_scroll + visible]

        for i, it in enumerate(shown):
            ry = list_top + i * self.NOTE_ROW_H
            r = QRectF(inner.left(), ry, self.NOTE_LIST_W, self.NOTE_ROW_H - 5)
            self._note_hits.append((it["id"], r))

            p.setPen(Qt.NoPen)
            if it["id"] == self._note_cur:
                c = QColor(T.GPU)
                c.setAlpha(42)
                p.setBrush(c)
            elif i % 2 == 0:
                p.setBrush(QColor(255, 255, 255, 6))
            else:
                p.setBrush(Qt.NoBrush)
            if p.brush().style() != Qt.NoBrush:
                path = QPainterPath()
                path.addRoundedRect(r, 8, 8)
                p.drawPath(path)

            p.setFont(T.font(10))
            p.setPen(T.TEXT if it["id"] == self._note_cur else T.MUTED)
            fm = QFontMetrics(p.font())
            title = fm.elidedText(it.get("title") or "未命名", Qt.ElideRight,
                                  int(r.width() - 16))
            p.drawText(QRectF(r.left() + 8, ry + 2, r.width() - 16, 18),
                       Qt.AlignLeft | Qt.AlignVCenter, title)

            p.setFont(T.font(8))
            p.setPen(T.DIM)
            p.drawText(QRectF(r.left() + 8, ry + 20, r.width() - 16, 14),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       (it.get("updated") or "")[-5:] + "  ·  "
                       + " ".join((it.get("plain") or "").split())[:14])

        if len(items) > visible:
            p.setFont(T.font(9))
            p.setPen(T.DIM)
            p.drawText(QRectF(inner.left(), inner.bottom() - 14, self.NOTE_LIST_W, 14),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       "%d-%d / %d" % (self._note_scroll + 1,
                                       min(self._note_scroll + visible, len(items)), len(items)))

        # 右侧被编辑框覆盖，这里只画个空态提示补位
        if self._note_cur is None:
            p.setFont(T.font(10))
            p.setPen(T.MUTED)
            p.drawText(QRectF(inner.left() + self.NOTE_LIST_W + 16, inner.top() + 58,
                              inner.width() - self.NOTE_LIST_W - 16, 80),
                       Qt.AlignLeft | Qt.AlignVCenter, "  选中左边一条，或者点「＋ 新建」")

    # ------------------------------------------------------------ 曲库
    def _load_library(self) -> None:
        try:
            from core import library as L

            self._lib = L.scan()
        except Exception as e:
            self._lib = []
            self._show_toast(f"曲库扫描失败：{e}")

    def _play_lib(self, idx: int) -> None:
        try:
            item = self._lib[idx]
        except IndexError:
            return
        from core import library as L

        ok, msg = L.launch(item["path"], background=self._pot_bg)
        if ok:
            tail = "（后台播放）" if self._pot_bg else ""
            self._show_toast(f"已交给 PotPlayer：{item['title']}{tail}")
        else:
            self._show_toast(msg)

    def _add_music_dir(self) -> None:
        """选一个音乐文件夹加进曲库（写进 config.json 的 music_dirs）。"""
        from PySide6.QtWidgets import QFileDialog

        from core import library as L

        self._hold_open = True
        self._set_activatable(True)
        try:
            start = (L.music_dirs() or [os.path.expanduser("~")])[0]
            path = QFileDialog.getExistingDirectory(self, "选择音乐文件夹", start)
        finally:
            self._set_activatable(False)
            self._hold_open = False
        if not path:
            return
        ok, msg = L.add_dir(path)
        self._load_library()
        if ok:
            self._show_toast("%s（曲库 %d 首）" % (msg, len(self._lib)))
        else:
            self._show_toast(msg)

    def _maybe_collapse(self) -> None:
        if self._dragging or self._hold_open:
            return
        if self.rect().contains(self.mapFromGlobal(QPoint(*_cursor_pos()))):
            return
        if self._confirm is not None:
            return
        if self._page == "notes" and self._note_editor is not None \
                and self._note_editor.hasFocus():
            return
        if self._expanded:
            if self._page == "notes":
                self._save_note_now()
            self._animate_to(False)

    # ================================================================== 鼠标
    def enterEvent(self, e) -> None:
        self._collapse_timer.stop()
        # 正在淡出（130ms 的过渡期）时回来，也要立刻取消收起并重新淡入
        if not self._expanded or self._content_op < 0.99 or self._collapse_anim.isActive():
            self._animate_to(True)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        if not self._dragging:
            self._collapse_timer.start()
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:
        pos = e.position()
        if e.button() == Qt.LeftButton:
            if self._confirm is not None:
                self._handle_confirm_click(pos)
                return
            if self._expanded and self._handle_page_click(pos):
                return
            self._dragging = True
            self._anim.stop()
            self._drag_offset = e.globalPosition().toPoint() - self.pos()
            self._collapse_timer.stop()
        super().mousePressEvent(e)

    def _handle_page_click(self, pos: QPointF) -> bool:
        for key, rect in self._tab_hits.items():
            if rect.contains(pos):
                if key != self._page:
                    self.set_page(key)
                return True

        if self._page == "monitor":
            # 点哪张卡片，就按哪个指标排序看进程
            for key, rect in self._card_hits.items():
                if rect.contains(pos):
                    self._open_process_for(key)
                    return True
            # 底栏的上传/下载/读盘/写盘也能钻进去
            for key, rect in self._io_hits.items():
                if rect.contains(pos):
                    self._open_process_for(key)
                    return True

        if self._page == "process":
            for key, rect in self._head_hits.items():
                if rect.contains(pos):
                    if key == self._sort_key:
                        self._sort_desc = not self._sort_desc
                    else:
                        self._sort_key = key
                        self._sort_desc = True
                    self._proc_scroll = 0
                    self.update()
                    return True
            for row_idx, rect in self._close_hits:
                if rect.contains(pos):
                    self._ask_kill(row_idx)
                    return True
            for row_idx, rect in self._row_hits:
                if rect.contains(pos):
                    return True
            return False

        if self._page == "music":
            if self._lib_add_hit is not None and self._lib_add_hit.contains(pos):
                self._add_music_dir()
                return True
            for idx, rect in self._lib_hits:
                if rect.contains(pos):
                    self._play_lib(idx)
                    return True
            for act, rect in self._music_hits.items():
                if rect.contains(pos):
                    if self._media_worker is not None:
                        self._media_worker.post(act)
                    return True
            if self._player_btn_hit is not None and self._player_btn_hit.contains(pos):
                try:
                    from core import potplayer

                    if potplayer.show():
                        self._show_toast("已显示 PotPlayer 窗口")
                    else:
                        self._show_toast("没找到 PotPlayer 窗口")
                except Exception as e:
                    self._show_toast(f"显示失败：{e}")
                return True
            if self._vol_hit is not None and self._vol_hit.contains(pos):
                self._vol_drag = True
                self._set_volume_from_x(pos.x())
                return True

        if self._page == "notes":
            for key, rect in self._note_btn_hits.items():
                if rect.contains(pos):
                    if key.startswith("fmt_"):
                        self._note_apply_fmt(key[4:])
                        return True
                    if key == "new":
                        self._new_note()
                    elif key == "export":
                        self._note_export()
                    elif key == "del":
                        self._note_delete()
                    return True
            for nid, rect in self._note_hits:
                if rect.contains(pos):
                    if nid != self._note_cur:
                        self._save_note_now()
                        self._note_load(nid)
                    return True
            return False

        if self._page == "clipboard":
            for idx, rect, del_rect, save_rect in self._clip_hits:
                if del_rect.contains(pos):
                    if self._clip is not None:
                        self._clip.remove(idx)
                    self.update()
                    return True
                if save_rect.contains(pos):
                    self._save_clip_as_note(idx)
                    return True
                if rect.contains(pos):
                    # 先按住，松开才复制；中途拖动就变成拖出文件
                    self._press_row = idx
                    self._press_pos = pos
                    return True
            return False
        return False

    def _proc_hint(self) -> str:
        """把 SMTC 的 aumid 转成能匹配到音频会话的进程名关键字。"""
        a = (self._media.get("aumid") or "").lower()
        for key, name in (
            ("potplayer", "potplayer"),
            ("kugou", "kugou"),
            ("msedge", "msedge"),
            ("spotify", "spotify"),
            ("vlc", "vlc"),
            ("chrome", "chrome"),
            ("foobar", "foobar"),
            ("aimp", "aimp"),
        ):
            if key in a:
                return name
        base = a.split("!")[0]
        return base[:-4] if base.endswith(".exe") else base

    def _set_volume_from_x(self, x: float) -> None:
        h = self._vol_hit
        if h is None:
            return
        x0 = h.left() + 8
        w = max(1.0, h.width() - 16)
        v = max(0.0, min(1.0, (x - x0) / w))
        self._vol_value = v
        if self._volume is not None:
            hint = self._proc_hint()
            if hint:
                self._volume.set_volume(hint, v)
        self.update()

    def mouseMoveEvent(self, e) -> None:
        if self._vol_drag:
            self._set_volume_from_x(e.position().x())
            return
        if (self._press_row is not None and self._press_pos is not None
                and (e.position() - self._press_pos).manhattanLength() > 10):
            idx = self._press_row
            self._press_row = None
            self._press_pos = None
            self._start_clip_drag(idx)
            return
        if self._dragging and self._drag_offset is not None:
            self.move(e.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if self._vol_drag:
            self._vol_drag = False
            hint = self._proc_hint() or "当前播放器"
            self._show_toast(f"音量 {int(round(self._vol_value * 100))}% · {hint}")
            return
        if self._press_row is not None:
            idx = self._press_row
            self._press_row = None
            self._press_pos = None
            self._copy_clip_item(idx)
            return
        if self._dragging:
            self._dragging = False
            self._drag_offset = None
            if not self._snap_on_release():
                self._save_position()
            self._collapse_timer.start()
        super().mouseReleaseEvent(e)

    def wheelEvent(self, e) -> None:
        if not self._expanded:
            return
        delta = -1 if e.angleDelta().y() > 0 else 1
        if not self._scroll_step(delta):
            return
        # 松手后靠惯性再滑一点，滚轮才不显得「一格一格硬跳」
        self._scroll_vel += delta * 2.2
        self._inertia.start()
        self.update()

    def _scroll_step(self, delta: int) -> bool:
        """把列表滚动 delta 行，越界就夹住；返回是否真的动了。"""
        if self._page == "process":
            rows = self._visible_procs()
            visible = self._visible_rows()
            max_scroll = max(0, len(rows) - visible)
            cur = self._proc_scroll
            self._proc_scroll = min(max(cur + delta, 0), max_scroll)
            return self._proc_scroll != cur
        if self._page == "music":
            visible = self._lib_visible()
            max_scroll = max(0, len(self._lib) - visible)
            cur = self._lib_scroll
            self._lib_scroll = min(max(cur + delta, 0), max_scroll)
            return self._lib_scroll != cur
        if self._page == "clipboard":
            items = self._clip.items if self._clip is not None else []
            visible = self._clip_visible()
            max_scroll = max(0, len(items) - visible)
            cur = self._clip_scroll
            self._clip_scroll = min(max(cur + delta, 0), max_scroll)
            return self._clip_scroll != cur
        if self._page == "notes":
            visible = self._note_visible()
            max_scroll = max(0, len(self._note_filtered()) - visible)
            cur = self._note_scroll
            self._note_scroll = min(max(cur + delta, 0), max_scroll)
            return self._note_scroll != cur
        return False

    def _on_inertia(self) -> None:
        if abs(self._scroll_vel) < 0.35:
            self._inertia.stop()
            return
        self._scroll_acc += self._scroll_vel
        step = int(self._scroll_acc)
        if step:
            if not self._scroll_step(step):
                self._inertia.stop()
                self._scroll_vel = 0.0
                self._scroll_acc = 0.0
                return
            self._scroll_acc -= step
            self.update()
        self._scroll_vel *= 0.74

    def _save_position(self) -> None:
        """存停靠边 + 收起态左上角。

        展开面板和竖向胶囊的尺寸都不一样，所以只存「收起态」坐标 + dock 边，
        启动时由 _dock_rect_for 还原，不会因为宽度/高度变化整体漂掉。
        """
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            cfg = {}
            if os.path.exists(CFG_PATH):
                with open(CFG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            if self._dock_rect is None:
                self._dock_rect = self._dock_rect_for(self._dock, self._content_size(False),
                                                      QRect(self.geometry()))
            cfg["dock"] = self._dock
            cfg["pos"] = [int(self._dock_rect.x()), int(self._dock_rect.y())]
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def contextMenuEvent(self, e) -> None:
        menu = QMenu(self)
        act_reset = menu.addAction("回到顶部居中")
        act_bottom = menu.addAction("停靠在任务栏上方")
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        chosen = menu.exec(e.globalPos())
        if chosen is act_reset:
            self._place_default()
            self._save_position()
        elif chosen is act_bottom:
            self._apply_dock("bottom", animate=True)
        elif chosen is act_quit:
            QApplication.quit()

    # ================================================================== 结束进程
    def _sorted_procs(self) -> list:
        key = self._sort_key
        if key in ("net_up", "net_down") and self._net_is_conn():
            getter = lambda r: r["conns"]
        else:
            getter = lambda r: r.get(key, 0)
        return sorted(self._procs, key=lambda r: getter(r) or 0, reverse=self._sort_desc)

    def _visible_procs(self) -> list:
        """GPU / 显存 / 网络 / 磁盘视图只保留真有占用的进程，空行不占地方。"""
        rows = self._sorted_procs()
        f = self._proc_focus
        if f == "gpu":
            rows = [r for r in rows if r["gpu"] >= 0.5]
        elif f == "vram":
            rows = [r for r in rows if r["vram"] >= 1]
        elif f in ("net_up", "net_down"):
            if self._net_is_conn():
                rows = [r for r in rows if r["conns"]]
            else:
                rows = [r for r in rows if (r["net_up"] + r["net_down"]) >= 1]
        elif f in ("disk_r", "disk_w"):
            rows = [r for r in rows if r[f] >= 1]
        return rows

    def _visible_rows(self) -> int:
        return 9

    def _ask_kill(self, row_idx: int) -> None:
        try:
            row = self._shown_rows[row_idx]
        except Exception:
            return
        if row["protected"]:
            self._show_toast("系统进程受保护，不允许结束")
            return
        if row.get("restricted"):
            self._show_toast("权限不足，无法结束该系统进程（需管理员）")
            return
        self._confirm = {"pid": row["pid"], "name": row["name"], "protected": False}
        self.update()

    def _handle_confirm_click(self, pos) -> None:
        for act, rect in self._confirm_hits.items():
            if not rect.contains(pos):
                continue
            if act == "yes":
                self._kill_process()
            else:
                self._confirm = None
            self.update()
            return
        self._confirm = None
        self.update()

    def _kill_process(self) -> None:
        if not self._confirm:
            return
        pid = self._confirm["pid"]
        name = self._confirm["name"]
        self._confirm = None
        try:
            import psutil

            psutil.Process(pid).terminate()
            self._show_toast(f"已结束 {name} ({pid})")
        except Exception as e:
            self._show_toast(f"结束失败：{e}")

    # ================================================================== 绘制
    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)

        body = QRectF(
            self.INSET,
            self.INSET,
            self.width() - 2 * self.INSET,
            self.height() - 2 * self.INSET,
        )
        radius = 22.0 if self._expanded else min(body.width(), body.height()) / 2.0

        self._paint_shadow(p, body, radius)
        self._paint_body(p, body, radius)

        path = QPainterPath()
        path.addRoundedRect(body, radius, radius)
        p.setClipPath(path)

        self._tab_hits = {}
        self._head_hits = {}
        self._row_hits = []
        self._close_hits = []
        self._music_hits = {}
        self._confirm_hits = {}
        self._io_hits = {}
        self._vol_hit = None
        self._lib_hits = []
        self._clip_hits = []

        if not self._expanded:
            p.setOpacity(max(0.0, min(1.0, self._content_op)))
            self._paint_collapsed(p, body)
            p.setOpacity(1.0)
        else:
            op = max(0.0, min(1.0, self._content_op))
            p.setOpacity(op)
            self._paint_header(p, body)
            # 页面额外叠一层淡入 + 轻微上浮，切页时不会「啪」地一下换了
            page_op = max(0.0, min(1.0, self._page_op))
            p.setOpacity(op * page_op)
            p.translate(0, (1.0 - page_op) * 6.0)
            if self._page == "monitor":
                self._paint_monitor(p, body)
            elif self._page == "process":
                self._paint_process(p, body)
            elif self._page == "clipboard":
                self._paint_clipboard(p, body)
            elif self._page == "notes":
                self._paint_notes(p, body)
            else:
                self._paint_music(p, body)
            p.translate(0, -(1.0 - page_op) * 6.0)
            p.setOpacity(1.0)

        if self._toast:
            self._paint_toast(p, body)
        if self._confirm is not None:
            self._paint_confirm(p, body)
        p.end()
        self._layout_note_editor()

    def _paint_shadow(self, p: QPainter, body: QRectF, radius: float) -> None:
        p.setPen(Qt.NoPen)
        for i in range(self.INSET, 0, -1):
            alpha = int(16 * (1.0 - i / (self.INSET + 2)))
            p.setBrush(QColor(0, 0, 0, max(alpha, 0)))
            r = body.adjusted(-i, -i + 2, i, i + 3)
            path = QPainterPath()
            path.addRoundedRect(r, radius + i, radius + i)
            p.drawPath(path)

    def _paint_body(self, p: QPainter, body: QRectF, radius: float) -> None:
        grad = QLinearGradient(body.topLeft(), body.bottomLeft())
        grad.setColorAt(0.0, T.BG_TOP)
        grad.setColorAt(0.55, QColor(
            int(T.BG_TOP.red() * 0.45 + T.BG_BOTTOM.red() * 0.55),
            int(T.BG_TOP.green() * 0.45 + T.BG_BOTTOM.green() * 0.55),
            int(T.BG_TOP.blue() * 0.45 + T.BG_BOTTOM.blue() * 0.55),
            T.BG_BOTTOM.alpha(),
        ))
        grad.setColorAt(1.0, T.BG_BOTTOM)
        path = QPainterPath()
        path.addRoundedRect(body, radius, radius)
        p.fillPath(path, QBrush(grad))

        # 顶部内高光：一条中间亮、两头淡的 1px 白线，玻璃感全靠它
        hl_grad = QLinearGradient(body.left(), body.top(), body.right(), body.top())
        hl_grad.setColorAt(0.0, QColor(255, 255, 255, 0))
        hl_grad.setColorAt(0.35, QColor(255, 255, 255, 46))
        hl_grad.setColorAt(0.5, QColor(255, 255, 255, 58))
        hl_grad.setColorAt(0.65, QColor(255, 255, 255, 46))
        hl_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(QPen(QBrush(hl_grad), 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(body.left() + radius, body.top() + 0.5),
                   QPointF(body.right() - radius, body.top() + 0.5))

        hl = QPainterPath()
        hl.addRoundedRect(body.adjusted(1, 1, -1, -1), max(radius - 1, 2), max(radius - 1, 2))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(T.STROKE, 1.0))
        p.drawPath(hl)

    # ------------------------------------------------------------ 收起态
    def _paint_collapsed(self, p: QPainter, body: QRectF) -> None:
        if self._is_side():
            self._paint_collapsed_side(p, body)
            return
        d = self._data
        tall = body.height() > 58.0  # 有音乐时胶囊更高，指标行上移给频谱让位
        cy = body.top() + (11 if tall else 21)
        margin = 20
        left = body.left() + margin

        cpu = d.get("cpu", 0.0) or 0.0
        p.setPen(Qt.NoPen)
        p.setBrush(T.load_color(cpu, T.CPU))
        p.drawEllipse(QPointF(left, cy), 3.6, 3.6)
        x = left + 13

        vram_pct = 0.0
        if d.get("vram_total"):
            vram_pct = d.get("vram_used", 0.0) / d["vram_total"] * 100.0
        gpu = d.get("gpu_util")

        items = [
            ("CPU", f"{cpu:.0f}%", cpu, T.CPU),
            ("GPU", "--" if gpu is None else f"{gpu:.0f}%", gpu or 0.0, T.GPU),
            ("RAM", f"{d.get('ram', 0.0):.0f}%", d.get("ram", 0.0), T.RAM),
            ("VRAM", f"{vram_pct:.0f}%" if d.get("vram_total") else "--", vram_pct, T.VRAM),
        ]
        lf = T.font(9)
        vf = T.font(12, True, True)
        for label, value, pct, color in items:
            p.setFont(lf)
            p.setPen(T.MUTED)
            lw = QFontMetrics(lf).horizontalAdvance(label)
            p.drawText(QRectF(x, cy - 12, lw + 2, 24), Qt.AlignLeft | Qt.AlignVCenter, label)
            x += lw + 4
            p.setFont(vf)
            p.setPen(T.load_color(pct, color))
            vw = QFontMetrics(vf).horizontalAdvance(value)
            p.drawText(QRectF(x, cy - 12, vw + 2, 24), Qt.AlignLeft | Qt.AlignVCenter, value)
            x += vw + 12

        # 右侧：有音乐就显示歌名，否则显示网速
        right_w = body.right() - margin - (x + 8)
        rect = QRectF(x + 8, cy - 12, right_w, 24)
        media = self._media
        if media.get("has_session") and media.get("title"):
            p.setFont(T.font(10, False, True))
            prefix = "♪ " if media.get("playing") else "❚❚ "
            text = prefix + str(media.get("title"))
            fm = QFontMetrics(p.font())
            text = fm.elidedText(text, Qt.ElideRight, int(right_w))
            p.setPen(T.TEXT if media.get("playing") else T.MUTED)
            p.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, text)
        else:
            up = T.fmt_rate(d.get("net_up", 0.0))
            down = T.fmt_rate(d.get("net_down", 0.0))
            p.setFont(T.font(9, False, True))
            p.setPen(T.NET)
            p.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, f"↑ {up}   ↓ {down}")

        # 底部：有声音就变成频谱律动，没声音回到四段占用条
        bar_y = body.bottom() - 3
        if self._spec_level > 0.01 or media.get("playing"):
            self._paint_micro_spec(
                p,
                QRectF(left, body.top() + 25, body.width() - margin * 2, body.height() - 25),
            )
            return
        bar_h = 2.2
        gap = 10
        seg_w = (body.width() - margin * 2 - gap * 3) / 4.0
        bx = left
        for _, _, pct, color in items:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 22))
            p.drawRoundedRect(QRectF(bx, bar_y, seg_w, bar_h), 1.1, 1.1)
            c = T.load_color(pct, color)
            c.setAlpha(190)
            p.setBrush(c)
            fill_w = seg_w * max(0.0, min(pct, 100.0)) / 100.0
            if fill_w > 1:
                p.drawRoundedRect(QRectF(bx, bar_y, fill_w, bar_h), 1.1, 1.1)
            bx += seg_w + gap

    @staticmethod
    def _brief_rate(v: float) -> str:
        """竖向胶囊空间小，网速用 1.3M / 351K / 18B 这种紧凑写法。"""
        try:
            v = float(v or 0.0)
        except Exception:
            return "0B"
        if v >= 1048576:
            return f"{v / 1048576:.1f}M"
        if v >= 1024:
            return f"{v / 1024:.0f}K"
        return f"{v:.0f}B"

    def _paint_collapsed_side(self, p: QPainter, body: QRectF) -> None:
        """竖向胶囊：指标从上往下排，左侧细占用条；播放时右缘一条竖频谱。"""
        d = self._data
        cpu = d.get("cpu", 0.0) or 0.0
        gpu = d.get("gpu_util")
        vram_pct = 0.0
        if d.get("vram_total"):
            vram_pct = d.get("vram_used", 0.0) / d["vram_total"] * 100.0
        up = d.get("net_up", 0.0) or 0.0
        down = d.get("net_down", 0.0) or 0.0
        net_pct = min(100.0, max(up, down) / 1048576.0 * 100.0)
        items = [
            ("CPU", f"{cpu:.0f}%", cpu, T.CPU),
            ("GPU", "--" if gpu is None else f"{gpu:.0f}%", gpu or 0.0, T.GPU),
            ("RAM", f"{d.get('ram', 0.0):.0f}%", d.get("ram", 0.0), T.RAM),
            ("VRAM", f"{vram_pct:.0f}%" if d.get("vram_total") else "--", vram_pct, T.VRAM),
            ("NET", f"↑{self._brief_rate(up)} ↓{self._brief_rate(down)}", net_pct, T.NET),
        ]
        top = body.top() + 8.0
        row_h = (body.height() - 8.0 - (top - body.top())) / len(items)
        lf = T.font(9)
        vf = T.font(13, True, True)
        vf_small = T.font(9, True, True)
        tx = body.left() + 13.0
        tw = body.width() - 18.0
        for i, (label, value, pct, color) in enumerate(items):
            y = top + row_h * i
            bx = body.left() + 6.0
            inner_h = row_h - 8.0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 20))
            p.drawRoundedRect(QRectF(bx, y + 4.0, 2.2, inner_h), 1.1, 1.1)
            c = T.load_color(pct, color)
            c.setAlpha(205)
            p.setBrush(c)
            fill = inner_h * max(0.0, min(float(pct), 100.0)) / 100.0
            if fill > 1.0:
                p.drawRoundedRect(QRectF(bx, y + 4.0 + inner_h - fill, 2.2, fill), 1.1, 1.1)
            p.setFont(lf)
            p.setPen(T.MUTED)
            p.drawText(QRectF(tx, y + 1, tw, 11), Qt.AlignLeft | Qt.AlignVCenter, label)
            small = label == "NET"
            p.setFont(vf_small if small else vf)
            p.setPen(color if small else T.load_color(pct, color))
            p.drawText(QRectF(tx, y + (14 if small else 12), tw, 18 if small else 18),
                       Qt.AlignLeft | Qt.AlignVCenter, value)

        media = self._media
        if self._spec_level > 0.01 or media.get("playing"):
            self._paint_side_spec(p,
                                  QRectF(body.right() - 6.0, body.top() + 10.0,
                                         3.2, body.height() - 20.0))

    def _paint_side_spec(self, p: QPainter, rect: QRectF) -> None:
        """竖向胶囊右缘的竖频谱：32 段细横条，长度随音量。"""
        n = len(self._spec) or 32
        slot = rect.height() / n
        bh = max(1.2, slot * 0.55)
        p.setPen(Qt.NoPen)
        for i, v in enumerate(self._spec):
            v = max(0.0, min(1.0, float(v)))
            w = 1.2 + v * (rect.width() - 1.2)
            c = self._spectrum_color(i / max(1, n - 1))
            c.setAlpha(90 + int(140 * v))
            p.setBrush(c)
            y = rect.top() + slot * i + (slot - bh) / 2.0
            p.drawRoundedRect(QRectF(rect.left(), y, w, bh), bh / 2.0, bh / 2.0)

    def _paint_micro_spec(self, p: QPainter, rect: QRectF) -> None:
        """收起胶囊底部那条细频谱。"""
        n = len(self._spec) or 32
        slot = rect.width() / n
        bw = max(1.2, slot * 0.55)
        p.setPen(Qt.NoPen)
        for i, v in enumerate(self._spec):
            v = max(0.0, min(1.0, float(v)))
            h = 1.8 + v * max(1.0, rect.height() - 1.8)
            c = self._spectrum_color(i / max(1, n - 1))
            c.setAlpha(190)
            p.setBrush(c)
            x = rect.left() + slot * i + (slot - bw) / 2
            p.drawRoundedRect(QRectF(x, rect.bottom() - h, bw, h), bw / 2, bw / 2)
            pk = self._peak[i] if i < len(self._peak) else 0.0
            if pk > 0.08:
                ph = 1.8 + pk * max(1.0, rect.height() - 1.8)
                pc = self._spectrum_color(i / max(1, n - 1))
                pc.setAlpha(55)
                p.setBrush(pc)
                p.drawRoundedRect(
                    QRectF(x, rect.bottom() - ph - 0.8, bw, 1.4), 0.7, 0.7)

    def _paint_spec(self, p: QPainter, rect: QRectF) -> None:
        """音乐页的 32 根频谱柱。"""
        n = len(self._spec) or 32
        slot = rect.width() / n
        bw = max(2.0, slot * 0.48)
        p.setPen(Qt.NoPen)
        for i, v in enumerate(self._spec):
            v = max(0.0, min(1.0, float(v)))
            h = 2.5 + v * max(1.0, rect.height() - 2.5)
            c = self._spectrum_color(i / max(1, n - 1))
            c.setAlpha(110 + int(145 * v))
            p.setBrush(c)
            x = rect.left() + slot * i + (slot - bw) / 2
            p.drawRoundedRect(QRectF(x, rect.bottom() - h, bw, h), bw / 2, bw / 2)
            # 峰值保持：顶端留一截淡色
            pk = self._peak[i] if i < len(self._peak) else 0.0
            if pk > 0.06:
                ph = 2.5 + pk * max(1.0, rect.height() - 2.5)
                pc = self._spectrum_color(i / max(1, n - 1))
                pc.setAlpha(70)
                p.setBrush(pc)
                p.drawRoundedRect(
                    QRectF(x, rect.bottom() - ph - 1.2, bw, 2.2), 1.1, 1.1)

    # ------------------------------------------------------------ 标题栏
    def _paint_header(self, p: QPainter, body: QRectF) -> None:
        pad = 18
        top = body.top() + pad
        p.setFont(T.font(13, True))
        p.setPen(T.TEXT)
        p.drawText(
            QRectF(body.left() + pad, top, 96, 26),
            Qt.AlignLeft | Qt.AlignVCenter,
            "桌面灵动岛",
        )

        x = body.left() + pad + 100
        labels = PAGE_LABELS
        if self._page == "process":
            # 进程页没有独立入口，这里给一个"← 监控"返回
            labels = tuple(
                (k, "← 监控" if k == "monitor" else lb) for k, lb in PAGE_LABELS
            )
        for key, label in labels:
            active = key == self._page
            p.setFont(T.font(10, active, spacing=0.3))
            w = QFontMetrics(p.font()).horizontalAdvance(label) + 24
            rect = QRectF(x, top + 2, w, 22)
            p.setPen(Qt.NoPen)
            # hover 只给一点点底面提示，当前页靠下面的下划线说话
            p.setBrush(QColor(255, 255, 255, 10) if active else QColor(0, 0, 0, 0))
            rp = QPainterPath()
            rp.addRoundedRect(rect, 11, 11)
            p.drawPath(rp)
            p.setPen(T.TEXT if active else T.MUTED)
            p.drawText(rect, Qt.AlignCenter, label)
            self._tab_hits[key] = rect
            if active:
                self._tab_active = (rect.left(), rect.width())
            x += w + 6

        # 2px 下划线指示器：切页时滑过去，而不是整块 pill 跳一下
        ind = self._tab_ind
        if ind is None:
            ind = getattr(self, "_tab_active", (x, 40))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 120))
        p.drawRoundedRect(
            QRectF(ind[0] + 6, top + 24.5, max(8.0, ind[1] - 12), 2.0), 1.0, 1.0
        )

        p.setFont(T.font(11))
        p.setPen(T.MUTED)
        p.drawText(
            QRectF(body.right() - pad - 190, top, 190, 26),
            Qt.AlignRight | Qt.AlignVCenter,
            f"{self._date}   {self._clock}",
        )

        # 分隔线
        y = top + 32
        p.setPen(QPen(QColor(255, 255, 255, 14), 1.0))
        p.drawLine(
            QPointF(body.left() + pad, y), QPointF(body.right() - pad, y)
        )

    # ------------------------------------------------------------ 监控页
    def _paint_monitor(self, p: QPainter, body: QRectF) -> None:
        d = self._data
        pad = 16
        inner = body.adjusted(pad, pad + 40, -pad, -pad)

        gap = 10
        card_w = (inner.width() - gap) / 2.0
        card_h = 100.0

        cpu = d.get("cpu", 0.0) or 0.0
        gpu = d.get("gpu_util")
        temp = d.get("gpu_temp")
        gpu_sub = "未检测到 NVIDIA 显卡"
        if gpu is not None:
            gpu_sub = f"{d.get('gpu_name') or self._worker.gpu_name}"
            if temp is not None:
                gpu_sub = f"{gpu_sub} · {temp}°C"
        vram_pct = 0.0
        if d.get("vram_total"):
            vram_pct = d.get("vram_used", 0.0) / d["vram_total"] * 100.0

        cards = [
            ("CPU", cpu, f"{cpu:.0f}%",
             f"{d.get('cpu_ghz', 0.0):.1f} GHz · {d.get('cpu_cores', 0)} 核", T.CPU, self._hist["cpu"]),
            ("GPU", gpu or 0.0, "--" if gpu is None else f"{gpu:.0f}%", gpu_sub, T.GPU, self._hist["gpu"]),
            ("内存", d.get("ram", 0.0), f"{d.get('ram', 0.0):.0f}%",
             f"{d.get('ram_used', 0.0):.1f} / {d.get('ram_total', 0.0):.1f} GB", T.RAM, self._hist["ram"]),
            ("显存", vram_pct, f"{vram_pct:.0f}%" if d.get("vram_total") else "--",
             f"{d.get('vram_used', 0.0):.1f} / {d.get('vram_total', 0.0):.1f} GB" if d.get("vram_total") else "不可用",
             T.VRAM, self._hist["vram"]),
        ]

        self._card_hits.clear()
        for i, card in enumerate(cards):
            col, row = i % 2, i // 2
            r = QRectF(
                inner.left() + col * (card_w + gap),
                inner.top() + row * (card_h + gap),
                card_w,
                card_h,
            )
            op = self._card_op(i)
            if op <= 0.02:
                continue
            p.save()
            p.setOpacity(p.opacity() * op)
            p.translate(0, (1.0 - op) * 5.0)
            self._paint_card(p, r, *card)
            p.restore()
            self._card_hits[CARD_SORTS[i]] = r

        bar_top = inner.top() + 2 * card_h + gap + 6
        bar = QRectF(inner.left(), bar_top, inner.width(), inner.bottom() - bar_top)
        bar_op = self._card_op(len(cards))
        if bar_op > 0.02:
            p.save()
            p.setOpacity(p.opacity() * bar_op)
            p.translate(0, (1.0 - bar_op) * 5.0)
            p.setPen(Qt.NoPen)
            p.setBrush(T.CARD_BG)
            bg = QPainterPath()
            bg.addRoundedRect(bar, 18, 18)
            p.drawPath(bg)
            self._paint_io(p, bar, d)
            p.restore()

    def _paint_card(self, p, rect, title, pct, value_text, sub_text, color, hist) -> None:
        p.setPen(Qt.NoPen)
        p.setBrush(T.CARD_BG)
        path = QPainterPath()
        path.addRoundedRect(rect, 18, 18)
        p.drawPath(path)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 18), 1.0))
        p.drawPath(path)

        x_inset = 16
        p.setFont(T.font(10, spacing=0.2))
        p.setPen(T.MUTED)
        p.drawText(QRectF(rect.left() + x_inset, rect.top() + 13, 120, 16),
                   Qt.AlignLeft | Qt.AlignVCenter, title)

        p.setFont(T.font(23, True, True, spacing=-0.3))
        p.setPen(T.load_color(pct, color))
        p.drawText(QRectF(rect.right() - x_inset - 120, rect.top() + 9, 120, 30),
                   Qt.AlignRight | Qt.AlignVCenter, value_text)

        p.setFont(T.font(9))
        p.setPen(T.DIM)
        fm = QFontMetrics(p.font())
        sub = fm.elidedText(sub_text, Qt.ElideRight, int(rect.width() - x_inset * 2 - 54))
        p.drawText(QRectF(rect.left() + x_inset, rect.top() + 37, rect.width() - x_inset * 2 - 54, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, sub)

        # 卡片是进程列表的入口：右下角给个提示
        p.setPen(T.DIM)
        p.drawText(QRectF(rect.right() - x_inset - 54, rect.top() + 37, 54, 14),
                   Qt.AlignRight | Qt.AlignVCenter, "进程 ›")

        spark = QRectF(rect.left() + x_inset, rect.top() + 55,
                       rect.width() - x_inset * 2, rect.height() - 55 - 14)
        self._paint_spark(p, spark, hist, color)

        # 卡底一条 2px 分类色细条，比胶囊底边四段条更克制
        strip = QRectF(rect.left() + x_inset, rect.bottom() - 7.5,
                       rect.width() - x_inset * 2, 2.0)
        c = T.load_color(pct, color)
        c.setAlpha(165)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawRoundedRect(strip, 1.0, 1.0)

    def _paint_spark(self, p, rect, hist, color) -> None:
        # 底线交给卡片底部那条分类色，这里只留一条 75% 参考线
        p.setPen(QPen(QColor(255, 255, 255, 12), 1.0))
        y75 = rect.bottom() - rect.height() * 0.75
        p.drawLine(QPointF(rect.left(), y75), QPointF(rect.right(), y75))

        n = len(hist)
        if n < 2:
            return
        step = rect.width() / (self.HIST_MAX - 1)
        x = rect.right() - (n - 1) * step
        pts = []
        for v in hist:
            y = rect.bottom() - max(0.0, min(float(v), 100.0)) / 100.0 * rect.height()
            pts.append(QPointF(x, y))
            x += step

        line = QPainterPath(pts[0])
        for pt in pts[1:]:
            line.lineTo(pt)

        fill = QPainterPath(line)
        fill.lineTo(QPointF(rect.right(), rect.bottom()))
        fill.lineTo(QPointF(pts[0].x(), rect.bottom()))
        fill.closeSubpath()

        g = QLinearGradient(0, rect.top(), 0, rect.bottom())
        c_top = QColor(color)
        c_top.setAlpha(76)
        c_bottom = QColor(color)
        c_bottom.setAlpha(6)
        g.setColorAt(0.0, c_top)
        g.setColorAt(1.0, c_bottom)
        p.setPen(Qt.NoPen)
        p.fillPath(fill, QBrush(g))

        pen = QPen(QColor(color))
        pen.setWidthF(1.4)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPath(line)

    def _paint_io(self, p, bar, d) -> None:
        entries = [
            ("net_up", "↑", T.fmt_rate(d.get("net_up", 0.0)), "上传", T.NET),
            ("net_down", "↓", T.fmt_rate(d.get("net_down", 0.0)), "下载", T.NET),
            ("disk_r", "R", T.fmt_rate(d.get("disk_r", 0.0)), "读盘", T.DISK),
            ("disk_w", "W", T.fmt_rate(d.get("disk_w", 0.0)), "写盘", T.DISK),
        ]
        slot_w = bar.width() / len(entries)
        self._io_hits.clear()
        for i, (key, icon, value, label, color) in enumerate(entries):
            cx = bar.left() + slot_w * i + slot_w / 2
            if i > 0:
                p.setPen(QPen(QColor(255, 255, 255, 18), 1.0))
                p.drawLine(QPointF(bar.left() + slot_w * i, bar.top() + 8),
                           QPointF(bar.left() + slot_w * i, bar.bottom() - 8))
            # 数值 20 + 标签 14 = 34，按整块垂直居中（+2 补字体基线的视觉下沉）
            top0 = bar.top() + (bar.height() - 34) / 2.0 + 2.0
            p.setFont(T.font(12, True, True))
            p.setPen(color)
            p.drawText(QRectF(cx - 60, top0, 120, 20), Qt.AlignCenter, value)
            p.setFont(T.font(9))
            p.setPen(T.DIM)
            p.drawText(QRectF(cx - 40, top0 + 20, 80, 14), Qt.AlignCenter, f"{icon} {label}")
            self._io_hits[key] = QRectF(bar.left() + slot_w * i, bar.top(), slot_w, bar.height())

    # ------------------------------------------------------------ 进程页
    def _paint_process(self, p: QPainter, body: QRectF) -> None:
        pad = 18
        inner = body.adjusted(pad, pad + 40, -pad, -pad)
        focus = self._proc_focus if self._proc_focus in FOCUS_META else "cpu"
        label = FOCUS_META[focus]

        close_w = 26.0
        metric_w = 200.0
        bar_w = 100.0
        name_w = inner.width() - metric_w - close_w

        head_h = 22
        y = inner.top()

        p.setFont(T.font(9))
        p.setPen(T.DIM)
        p.drawText(QRectF(inner.left(), y, name_w, head_h), Qt.AlignLeft | Qt.AlignVCenter, "进程")
        self._head_hits["name"] = QRectF(inner.left(), y, name_w, head_h)

        arrow = " ▾" if self._sort_desc else " ▴"
        head_rect = QRectF(inner.left() + name_w, y, metric_w - 10, head_h)
        p.setPen(T.TEXT)
        p.drawText(head_rect, Qt.AlignRight | Qt.AlignVCenter, label + arrow)
        self._head_hits[focus] = head_rect

        p.setPen(QPen(QColor(255, 255, 255, 14), 1.0))
        p.drawLine(QPointF(inner.left(), y + head_h + 2), QPointF(inner.right(), y + head_h + 2))

        rows = self._visible_procs()
        if not rows:
            p.setFont(T.font(11))
            p.setPen(T.MUTED)
            empty_text = {
                "gpu": "当前没有进程在占用 GPU",
                "vram": "当前没有进程占用显存",
                "net_up": "当前没有进程在收发数据",
                "net_down": "当前没有进程在收发数据",
                "disk_r": "当前没有进程在读盘",
                "disk_w": "当前没有进程在写盘",
            }.get(focus, "正在读取进程…")
            p.drawText(inner, Qt.AlignCenter, empty_text)
            return
        visible = self._visible_rows()
        self._shown_rows = rows[self._proc_scroll:self._proc_scroll + visible]

        row_h = 28.0
        top_row = y + head_h + 6
        etw = self._netflow is not None and self._netflow.mode == "etw"
        # 条状图按"本视图最大值"归一化，谁最猛一眼看得出来
        max_val = max((self._metric_value(r, focus) for r in rows), default=0.0) or 1.0

        for i, row in enumerate(self._shown_rows):
            ry = top_row + i * row_h
            row_rect = QRectF(inner.left(), ry, inner.width(), row_h - 3)
            self._row_hits.append((i, row_rect))

            if i % 2 == 0:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 6))
                rp = QPainterPath()
                rp.addRoundedRect(row_rect.adjusted(-4, 0, 4, 0), 8, 8)
                p.drawPath(rp)

            p.setPen(Qt.NoPen)
            p.setBrush(T.load_color(row["cpu"] * 3, T.CPU))
            p.drawEllipse(QPointF(inner.left() + 6, ry + (row_h - 3) / 2), 3.0, 3.0)
            p.setFont(T.font(10))
            p.setPen(T.TEXT if not (row["protected"] or row["restricted"]) else T.DIM)
            fm = QFontMetrics(p.font())
            name = fm.elidedText(row["name"], Qt.ElideRight, int(name_w - 76))
            p.drawText(QRectF(inner.left() + 16, ry, name_w - 22, row_h - 3),
                       Qt.AlignLeft | Qt.AlignVCenter, f"{name}  {row['pid']}")

            no_perm = row["protected"] or row["restricted"]
            val = self._metric_value(row, focus)
            text, color = self._focus_text(row, focus)

            # 指标列：左边条状图，右边数值
            bx = inner.left() + name_w
            bar_h = 5.0
            track = QRectF(bx + 4, ry + (row_h - 3) / 2 - bar_h / 2, bar_w, bar_h)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 18))
            p.drawRoundedRect(track, 2.5, 2.5)
            if val > 0:
                fill_w = max(bar_w * min(val / max_val, 1.0), 3.0)
                p.setBrush(QColor(color))
                p.drawRoundedRect(QRectF(track.left(), track.top(), fill_w, bar_h), 2.5, 2.5)

            p.setFont(T.font(11, False, True))
            p.setPen(color if val > 0 else T.DIM)
            p.drawText(QRectF(bx + bar_w + 10, ry, metric_w - bar_w - 16, row_h - 3),
                       Qt.AlignRight | Qt.AlignVCenter, text)

            # 结束按钮
            cx = inner.left() + name_w + metric_w
            btn = QRectF(cx + 2, ry + 4, close_w - 6, row_h - 12)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 20) if not no_perm else QColor(255, 255, 255, 8))
            bp = QPainterPath()
            bp.addRoundedRect(btn, 6, 6)
            p.drawPath(bp)
            p.setPen(T.DIM)
            p.setFont(T.font(11))
            p.drawText(btn, Qt.AlignCenter, "×")
            self._close_hits.append((i, btn))

        # 底部说明
        foot = QRectF(inner.left(), inner.bottom() - 16, inner.width(), 16)
        p.setFont(T.font(9))
        p.setPen(T.DIM)
        if focus in ("net_up", "net_down") and not etw:
            foot_text = (
                f"按{label}排序 · {len(rows)} 个进程有连接 · "
                "连接数模式（管理员启动可看真实流量）· 滚轮翻页"
            )
        else:
            foot_text = f"按{label}排序 · {len(rows)} 个进程 · 滚轮翻页 · 点「← 监控」返回"
        p.drawText(foot, Qt.AlignLeft | Qt.AlignVCenter, foot_text)

    def _net_is_conn(self) -> bool:
        """普通权限拿不到 ETW 流量，网络视图改成看连接数。"""
        return self._netflow is None or self._netflow.mode != "etw"

    def _metric_value(self, row: dict, focus: str) -> float:
        if focus in ("net_up", "net_down") and self._net_is_conn():
            return float(row.get("conns", 0) or 0)
        return float(row.get(focus, 0.0) or 0.0)

    def _focus_text(self, row: dict, focus: str) -> tuple:
        """返回 (文本, 颜色)：拿不到的显示「—」，别假装是 0。"""
        v = self._metric_value(row, focus)
        if focus == "cpu":
            if row["restricted"]:
                return "—", T.DIM
            return f"{v:.1f}%", T.load_color(v * 3, T.CPU)
        if focus == "gpu":
            # GPU 和显存走 PDH 按 PID 取，不需要 OpenProcess 权限，受保护进程也照常显示
            if v < 0.5:
                return "—", T.DIM
            return f"{v:.1f}%", T.load_color(v, T.GPU)
        if focus == "mem":
            if v <= 0:
                return "—", T.DIM
            return f"{v:.0f}M", T.RAM
        if focus == "vram":
            if v < 1:
                return "—", T.DIM
            return T.fmt_mem(v), T.VRAM
        if focus in ("net_up", "net_down"):
            if v <= 0:
                return "—", T.DIM
            if self._net_is_conn():
                return f"{int(v)} 连接", T.NET
            return T.fmt_rate(v), T.NET
        if focus in ("disk_r", "disk_w"):
            if row["restricted"] or v < 1:
                return "—", T.DIM
            return T.fmt_rate(v), T.DISK
        return "—", T.DIM

    # ------------------------------------------------------------ 曲库（音乐页下半区）
    LIB_ROW_H = 30.0

    def _lib_visible(self) -> int:
        """当前音乐页能放几行曲库，供滚轮用。"""
        return max(1, self._lib_rows)

    def _paint_library_section(self, p: QPainter, inner: QRectF, top: float) -> None:
        """画曲库标题 + 列表，返回本次可见行数。"""
        items = self._lib
        now = (self._media.get("title") or "").strip()

        # 上方分隔线
        p.setPen(QPen(QColor(255, 255, 255, 14), 1.0))
        p.drawLine(QPointF(inner.left(), top - 10), QPointF(inner.right(), top - 10))

        p.setFont(T.font(9))
        p.setPen(T.DIM)
        p.drawText(QRectF(inner.left(), top - 4, 120, 18),
                   Qt.AlignLeft | Qt.AlignVCenter, "曲库")

        # 「＋ 目录」：在界面里直接选文件夹加进曲库
        add_btn = QRectF(inner.right() - 62, top - 6, 62, 20)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 14))
        bp = QPainterPath()
        bp.addRoundedRect(add_btn, 7, 7)
        p.drawPath(bp)
        p.setFont(T.font(9))
        p.setPen(T.MUTED)
        p.drawText(add_btn, Qt.AlignCenter, "＋ 目录")
        self._lib_add_hit = add_btn

        p.setPen(T.DIM)
        p.drawText(QRectF(inner.left(), top - 4, inner.width() - 70, 18),
                   Qt.AlignRight | Qt.AlignVCenter,
                   "%d 首 · 点一行播放 · 滚轮翻页" % len(items))

        list_top = top + 22
        visible = max(1, int((inner.bottom() - 22 - list_top) // self.LIB_ROW_H))
        self._lib_rows = visible

        if not items:
            p.setFont(T.font(11))
            p.setPen(T.MUTED)
            p.drawText(QRectF(inner.left(), list_top, inner.width(), inner.bottom() - list_top),
                       Qt.AlignCenter,
                       "曲库还没有目录\n点右上角「＋ 目录」加一个音乐文件夹\n（也可以写进 config.json 的 music_dirs）")
            return

        if self._lib_scroll > max(0, len(items) - visible):
            self._lib_scroll = max(0, len(items) - visible)
        shown = items[self._lib_scroll:self._lib_scroll + visible]

        for i, it in enumerate(shown):
            idx = self._lib_scroll + i
            ry = list_top + i * self.LIB_ROW_H
            r = QRectF(inner.left(), ry, inner.width(), self.LIB_ROW_H - 4)
            self._lib_hits.append((idx, r))

            p.setPen(Qt.NoPen)
            if now and it["title"] == now:
                c = QColor(self._theme_colors[0])
                c.setAlpha(34)
                p.setBrush(c)
            elif i % 2 == 0:
                p.setBrush(QColor(255, 255, 255, 6))
            else:
                p.setBrush(Qt.NoBrush)
            if p.brush().style() != Qt.NoBrush:
                path = QPainterPath()
                path.addRoundedRect(r, 8, 8)
                p.drawPath(path)

            p.setFont(T.font(10))
            p.setPen(T.TEXT if (now and it["title"] == now) else T.MUTED)
            fm = QFontMetrics(p.font())
            title = fm.elidedText(it["title"], Qt.ElideRight, int(r.width() - 240))
            p.drawText(QRectF(r.left() + 10, ry, r.width() - 240, self.LIB_ROW_H - 4),
                       Qt.AlignLeft | Qt.AlignVCenter, title)

            p.setFont(T.font(9))
            p.setPen(T.DIM)
            p.drawText(QRectF(r.right() - 210, ry, 150, self.LIB_ROW_H - 4),
                       Qt.AlignRight | Qt.AlignVCenter,
                       fm.elidedText(it["artist"], Qt.ElideRight, 146))
            p.drawText(QRectF(r.right() - 52, ry, 44, self.LIB_ROW_H - 4),
                       Qt.AlignRight | Qt.AlignVCenter, it["ext"])

        p.setFont(T.font(9))
        p.setPen(T.DIM)
        p.drawText(QRectF(inner.left(), inner.bottom() - 16, inner.width(), 16),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   "第 %d-%d 首 / 共 %d"
                   % (self._lib_scroll + 1,
                      min(self._lib_scroll + visible, len(items)), len(items)))

    # ------------------------------------------------------------ 剪贴板页
    CLIP_ROW_H = 32.0

    def _paint_clipboard(self, p: QPainter, body: QRectF) -> None:
        pad = 18
        inner = body.adjusted(pad, pad + 40, -pad, -pad)
        items = self._clip.items if self._clip is not None else []

        p.setFont(T.font(9))
        p.setPen(T.DIM)
        p.drawText(QRectF(inner.left(), inner.top(), 120, 18),
                   Qt.AlignLeft | Qt.AlignVCenter, "剪贴板")
        p.drawText(QRectF(inner.left(), inner.top(), inner.width(), 18),
                   Qt.AlignRight | Qt.AlignVCenter,
                   "%d / 30 条 · 点一条复制回去 · 可拖到别处" % len(items))
        p.setPen(QPen(QColor(255, 255, 255, 14), 1.0))
        p.drawLine(QPointF(inner.left(), inner.top() + 22),
                   QPointF(inner.right(), inner.top() + 22))

        if not items:
            p.setFont(T.font(11))
            p.setPen(T.MUTED)
            p.drawText(inner, Qt.AlignCenter,
                       "还没有记录\n复制一段文字，或者从外面拖个文件进来")
            return

        from core.clipstore import ClipStore as CS

        row_h = self.CLIP_ROW_H
        top = inner.top() + 28
        visible = self._clip_visible()
        if self._clip_scroll > max(0, len(items) - visible):
            self._clip_scroll = max(0, len(items) - visible)
        shown = items[self._clip_scroll:self._clip_scroll + visible]

        for i, it in enumerate(shown):
            idx = self._clip_scroll + i
            ry = top + i * row_h
            r = QRectF(inner.left(), ry, inner.width(), row_h - 4)
            del_r = QRectF(r.right() - 30, ry + 5, 24, row_h - 14)
            save_r = QRectF(del_r.left() - 28, ry + 5, 24, row_h - 14)
            self._clip_hits.append((idx, r, del_r, save_r))

            p.setPen(Qt.NoPen)
            if i % 2 == 0:
                p.setBrush(QColor(255, 255, 255, 6))
                path = QPainterPath()
                path.addRoundedRect(r, 8, 8)
                p.drawPath(path)

            is_file = it["kind"] == "files"
            accent = T.DISK if is_file else T.CPU
            badge = QRectF(r.left() + 8, ry + (row_h - 4) / 2 - 8, 32, 16)
            c = QColor(accent)
            c.setAlpha(38)
            p.setBrush(c)
            bp = QPainterPath()
            bp.addRoundedRect(badge, 4, 4)
            p.drawPath(bp)
            p.setFont(T.font(8, False, True))
            p.setPen(accent)
            p.drawText(badge, Qt.AlignCenter, "文件" if is_file else "文本")

            p.setFont(T.font(10))
            p.setPen(T.TEXT)
            fm = QFontMetrics(p.font())
            text = CS.preview(it, 46)
            p.drawText(QRectF(badge.right() + 10, ry, r.width() - 380, row_h - 4),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(text, Qt.ElideRight, int(r.width() - 390)))

            p.setFont(T.font(9))
            p.setPen(T.MUTED)
            p.drawText(QRectF(r.right() - 280, ry, 150, row_h - 4),
                       Qt.AlignRight | Qt.AlignVCenter, str(it.get("src", ""))[:22])
            p.setPen(T.DIM)
            p.drawText(QRectF(r.right() - 122, ry, 56, row_h - 4),
                       Qt.AlignRight | Qt.AlignVCenter, str(it.get("time", "")))

            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 24))
            dp = QPainterPath()
            dp.addRoundedRect(del_r, 7, 7)
            p.drawPath(dp)
            cx0, cy0 = del_r.center().x(), del_r.center().y()
            s = 4.2
            p.setPen(QPen(T.MUTED, 1.7, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(cx0 - s, cy0 - s), QPointF(cx0 + s, cy0 + s))
            p.drawLine(QPointF(cx0 + s, cy0 - s), QPointF(cx0 - s, cy0 + s))

            # 存成一条记事（文字条目用紫色，文件条目用琥珀色区分）
            p.setPen(Qt.NoPen)
            c = QColor(T.GPU if not is_file else T.DISK)
            c.setAlpha(30)
            p.setBrush(c)
            sp = QPainterPath()
            sp.addRoundedRect(save_r, 7, 7)
            p.drawPath(sp)
            p.setFont(T.font(8))
            p.setPen(T.TEXT if not is_file else T.DISK)
            p.drawText(save_r, Qt.AlignCenter, "存")

        p.setFont(T.font(9))
        p.setPen(T.DIM)
        p.drawText(QRectF(inner.left(), inner.bottom() - 16, inner.width(), 16),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   "滚轮翻页 · 第 %d-%d 条 / 共 %d"
                   % (self._clip_scroll + 1,
                      min(self._clip_scroll + visible, len(items)), len(items)))

    # ------------------------------------------------------------ 音乐页
    def _paint_music(self, p: QPainter, body: QRectF) -> None:
        pad = 18
        inner = body.adjusted(pad, pad + 40, -pad, -pad)
        m = self._media
        self._lib_hits = []
        self._player_btn_hit = None
        self._lib_add_hit = None
        if not self._lib:
            self._load_library()

        if m.get("has_session"):
            self._paint_player(p, inner, m)
            list_top = inner.top() + 224
        else:
            p.setFont(T.font(11))
            p.setPen(T.MUTED)
            p.drawText(QRectF(inner.left(), inner.top(), inner.width(), 46),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       "未检测到正在播放的媒体 — 用酷狗或 PotPlayer 播一首，这里就会亮起来")
            list_top = inner.top() + 54

        self._paint_library_section(p, inner, list_top)

    def _paint_player(self, p: QPainter, inner: QRectF, m: dict) -> None:
        # 封面
        cov = QRectF(inner.left(), inner.top(), 78, 78)
        p.setPen(Qt.NoPen)
        if self._cover_pm is not None:
            p.save()
            path = QPainterPath()
            path.addRoundedRect(cov, 14, 14)
            p.setClipPath(path, Qt.IntersectClip)
            p.drawPixmap(cov, self._cover_pm, QRectF(self._cover_pm.rect()))
            p.restore()
        else:
            g = QLinearGradient(cov.topLeft(), cov.bottomRight())
            g.setColorAt(0, QColor(60, 66, 90))
            g.setColorAt(1, QColor(28, 30, 40))
            p.setBrush(QBrush(g))
            rp = QPainterPath()
            rp.addRoundedRect(cov, 14, 14)
            p.drawPath(rp)
            p.setPen(T.MUTED)
            p.setFont(T.font(22, True))
            p.drawText(cov, Qt.AlignCenter, "♪")

        # 文本区
        tx = cov.right() + 18
        tw = inner.right() - tx
        p.setFont(T.font(10))
        p.setPen(T.DIM)
        p.drawText(QRectF(tx, cov.top() + 2, tw, 16), Qt.AlignLeft | Qt.AlignVCenter,
                   m.get("app", "媒体") + ("  ·  播放中" if m.get("playing") else "  ·  已暂停"))

        p.setFont(T.font(17, True))
        p.setPen(T.TEXT)
        fm = QFontMetrics(p.font())
        title = fm.elidedText(str(m.get("title", "")), Qt.ElideRight, int(tw))
        p.drawText(QRectF(tx, cov.top() + 18, tw, 26), Qt.AlignLeft | Qt.AlignVCenter, title)

        p.setFont(T.font(10))
        p.setPen(T.MUTED)
        artist = str(m.get("artist") or "未知艺术家")
        p.drawText(QRectF(tx, cov.top() + 46, tw, 18), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(artist, Qt.ElideRight, int(tw)))

        p.setFont(T.font(9))
        p.setPen(T.DIM)
        album = str(m.get("album") or "")
        if album:
            p.drawText(QRectF(tx, cov.top() + 64, tw, 14), Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(album, Qt.ElideRight, int(tw)))

        # 进度条
        pb = QRectF(inner.left(), cov.bottom() + 16, inner.width(), 3)
        dur = float(m.get("duration") or 0.0)
        pos = float(m.get("position") or 0.0)
        ratio = max(0.0, min(pos / dur, 1.0)) if dur > 0 else 0.0
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(pb, 1.5, 1.5)
        if ratio > 0:
            p.setBrush(QColor(167, 139, 250))
            p.drawRoundedRect(QRectF(pb.left(), pb.top(), pb.width() * ratio, pb.height()), 1.5, 1.5)
            p.setBrush(QColor(230, 225, 255))
            p.drawEllipse(QPointF(pb.left() + pb.width() * ratio, pb.center().y()), 3.2, 3.2)

        p.setFont(T.font(9, False, True))
        p.setPen(T.DIM)
        p.drawText(QRectF(pb.left(), pb.bottom() + 6, 100, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   _fmt_time(pos))
        p.drawText(QRectF(pb.right() - 100, pb.bottom() + 6, 100, 14),
                   Qt.AlignRight | Qt.AlignVCenter, _fmt_time(dur))

        # 频谱：进度条和按钮之间，用封面主色渐变
        spec = QRectF(inner.left(), pb.bottom() + 24, inner.width(), 44)
        self._paint_spec(p, spec)

        # 控制按钮
        cy = inner.top() + 192
        cx = inner.center().x()
        buttons = [
            ("prev", cx - 62, 17, self._icon_prev),
            ("toggle", cx, 21, self._icon_toggle),
            ("next", cx + 62, 17, self._icon_next),
        ]
        for act, bx, br, drawer in buttons:
            rect = QRectF(bx - br, cy - br, br * 2, br * 2)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 26) if act == "toggle" else QColor(255, 255, 255, 12))
            p.drawEllipse(rect)
            p.setPen(QPen(T.TEXT, 1.6))
            drawer(p, QPointF(bx, cy), br * 0.55, m.get("playing", False))
            self._music_hits[act] = rect

        # 音量滑块（只动播放器这个进程）
        self._paint_volume(p, inner, cy)

        # 「显示播放器」：后台播放时想瞄一眼 PotPlayer 就点这里，只在播放中显示
        if m.get("playing"):
            btn = QRectF(inner.left(), cy - 13, 92, 26)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 14))
            p.drawRoundedRect(btn, 8, 8)
            p.setPen(T.MUTED)
            p.setFont(T.font(9))
            p.drawText(btn, Qt.AlignCenter, "显示播放器")
            self._player_btn_hit = btn

    def _paint_volume(self, p: QPainter, inner: QRectF, cy: float) -> None:
        p.setFont(T.font(9))
        p.setPen(T.DIM)
        p.drawText(QRectF(inner.right() - 156, cy - 7, 40, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, "音量")
        track = QRectF(inner.right() - 112, cy - 1.5, 76, 3)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 26))
        p.drawRoundedRect(track, 1.5, 1.5)
        w = track.width() * self._vol_value
        if w > 0.5:
            p.setBrush(self._theme_colors[0])
            p.drawRoundedRect(QRectF(track.left(), track.top(), w, track.height()), 1.5, 1.5)
        p.setBrush(QColor(235, 235, 245))
        p.drawEllipse(QPointF(track.left() + w, track.center().y()), 3.6, 3.6)
        p.setFont(T.font(9, False, True))
        p.setPen(T.MUTED)
        p.drawText(QRectF(track.right() + 6, cy - 7, 32, 14),
                   Qt.AlignLeft | Qt.AlignVCenter, "%d" % int(round(self._vol_value * 100)))
        self._vol_hit = QRectF(track.left() - 8, cy - 12, track.width() + 16, 24)

    def _icon_toggle(self, p, c, s, playing) -> None:
        if playing:
            p.setBrush(Qt.NoBrush)
            p.drawLine(QPointF(c.x() - s * 0.38, c.y() - s * 0.7), QPointF(c.x() - s * 0.38, c.y() + s * 0.7))
            p.drawLine(QPointF(c.x() + s * 0.38, c.y() - s * 0.7), QPointF(c.x() + s * 0.38, c.y() + s * 0.7))
        else:
            path = QPainterPath()
            path.moveTo(c.x() - s * 0.5, c.y() - s * 0.75)
            path.lineTo(c.x() - s * 0.5, c.y() + s * 0.75)
            path.lineTo(c.x() + s * 0.75, c.y())
            path.closeSubpath()
            p.setBrush(T.TEXT)
            p.drawPath(path)

    def _icon_prev(self, p, c, s, playing=False) -> None:
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(c.x() - s * 0.75, c.y() - s * 0.7), QPointF(c.x() - s * 0.75, c.y() + s * 0.7))
        path = QPainterPath()
        path.moveTo(c.x() + s * 0.6, c.y() - s * 0.7)
        path.lineTo(c.x() - s * 0.5, c.y())
        path.lineTo(c.x() + s * 0.6, c.y() + s * 0.7)
        path.closeSubpath()
        p.setBrush(T.TEXT)
        p.drawPath(path)

    def _icon_next(self, p, c, s, playing=False) -> None:
        p.setBrush(Qt.NoBrush)
        p.drawLine(QPointF(c.x() + s * 0.75, c.y() - s * 0.7), QPointF(c.x() + s * 0.75, c.y() + s * 0.7))
        path = QPainterPath()
        path.moveTo(c.x() - s * 0.6, c.y() - s * 0.7)
        path.lineTo(c.x() + s * 0.5, c.y())
        path.lineTo(c.x() - s * 0.6, c.y() + s * 0.7)
        path.closeSubpath()
        p.setBrush(T.TEXT)
        p.drawPath(path)

    # ------------------------------------------------------------ 浮层
    def _paint_toast(self, p: QPainter, body: QRectF) -> None:
        p.setFont(T.font(10))
        fm = QFontMetrics(p.font())
        w = min(fm.horizontalAdvance(self._toast) + 28, body.width() - 40)
        rect = QRectF(body.center().x() - w / 2, body.bottom() - 34, w, 24)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 190))
        rp = QPainterPath()
        rp.addRoundedRect(rect, 12, 12)
        p.drawPath(rp)
        p.setPen(T.TEXT)
        p.drawText(rect, Qt.AlignCenter, self._toast)

    def _paint_confirm(self, p: QPainter, body: QRectF) -> None:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRect(body)

        card = QRectF(0, 0, 380, 158)
        card.moveCenter(body.center())
        p.setBrush(QColor(26, 28, 34, 252))
        cp = QPainterPath()
        cp.addRoundedRect(card, 16, 16)
        p.drawPath(cp)
        p.setPen(QPen(QColor(255, 255, 255, 34), 1.0))
        p.drawPath(cp)

        p.setFont(T.font(12, True))
        p.setPen(T.TEXT)
        p.drawText(QRectF(card.left() + 20, card.top() + 16, card.width() - 40, 20),
                   Qt.AlignLeft | Qt.AlignVCenter, "结束这个进程？")

        p.setFont(T.font(10))
        p.setPen(T.MUTED)
        name = self._confirm.get("name", "")
        pid = self._confirm.get("pid", 0)
        p.drawText(QRectF(card.left() + 20, card.top() + 40, card.width() - 40, 18),
                   Qt.AlignLeft | Qt.AlignVCenter, f"{name}   PID {pid}")

        p.setFont(T.font(9))
        p.setPen(T.WARN)
        p.drawText(QRectF(card.left() + 20, card.top() + 60, card.width() - 40, 16),
                   Qt.AlignLeft | Qt.AlignVCenter, "未保存的数据可能丢失，请确认这是你要关掉的那个")

        # 按钮
        bw, bh = 120, 30
        by = card.bottom() - 18 - bh
        no_rect = QRectF(card.left() + 24, by, bw, bh)
        yes_rect = QRectF(card.right() - 24 - bw, by, bw, bh)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 18))
        rp = QPainterPath()
        rp.addRoundedRect(no_rect, 10, 10)
        p.drawPath(rp)
        p.setPen(T.MUTED)
        p.setFont(T.font(10))
        p.drawText(no_rect, Qt.AlignCenter, "取消")

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(248, 113, 113, 190))
        rp2 = QPainterPath()
        rp2.addRoundedRect(yes_rect, 10, 10)
        p.drawPath(rp2)
        p.setPen(QColor(30, 12, 12))
        p.setFont(T.font(10, True))
        p.drawText(yes_rect, Qt.AlignCenter, "结束进程")

        self._confirm_hits = {"no": no_rect, "yes": yes_rect}


def _fmt_speed(bps: float) -> str:
    """进程表里的紧凑速率：1.2M / 340K / 0B。"""
    bps = max(float(bps or 0.0), 0.0)
    if bps < 1024:
        return f"{bps:.0f}B"
    if bps < 1024 ** 2:
        return f"{bps / 1024:.0f}K"
    if bps < 1024 ** 3:
        return f"{bps / 1024 ** 2:.1f}M"
    return f"{bps / 1024 ** 3:.1f}G"


def _fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _cursor_pos() -> tuple[int, int]:
    try:
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y
    except Exception:
        from PySide6.QtGui import QCursor

        g = QCursor.pos()
        return g.x(), g.y()
