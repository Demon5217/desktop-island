# -*- coding: utf-8 -*-
"""全局视觉常量与格式化工具：配色、字体、数值换算。

改这里的颜色 / 字号，整个灵动岛的外观就会跟着变，
后面加页面时也统一从这儿取样式，别在各处散着写死。
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont

# ---------- 配色 ----------
BG_TOP = QColor(34, 37, 45, 244)      # 胶囊顶部（略亮，玻璃高光带）
BG_BOTTOM = QColor(9, 10, 14, 244)    # 胶囊底部（更深，压出体积感）
STROKE = QColor(255, 255, 255, 30)    # 内描边，给出玻璃边缘感
CARD_BG = QColor(255, 255, 255, 13)   # 展开态卡片底色
TEXT = QColor(232, 234, 240)
MUTED = QColor(138, 143, 163)
DIM = QColor(92, 97, 115)

CPU = QColor(34, 211, 238)    # 青
GPU = QColor(167, 139, 250)   # 紫
RAM = QColor(52, 211, 153)    # 绿
VRAM = QColor(244, 114, 182)  # 粉
NET = QColor(251, 146, 60)    # 橙
DISK = QColor(96, 165, 250)   # 蓝
WARN = QColor(248, 113, 113)  # 高负载提示色

FAMILY_UI = ["Microsoft YaHei UI", "Segoe UI Variable", "Segoe UI"]
FAMILY_NUM = ["Bahnschrift", "Segoe UI Variable Display", "Microsoft YaHei UI"]


def font(size: int, bold: bool = False, numeric: bool = False,
         spacing: float = 0.0, weight: int = -1) -> QFont:
    """取一个字体对象。

    numeric=True 时数字用 Bahnschrift，看着更有仪表感；
    spacing 收紧/放开字距（数字排版常要 -0.2 左右的负字距）；
    weight 传 0-100 可覆盖字重（-1 表示只用 bold 开关）。
    """
    f = QFont()
    f.setFamilies(FAMILY_NUM if numeric else FAMILY_UI)
    f.setPixelSize(size)
    if weight >= 0:
        f.setWeight(QFont.Weight(int(weight)))
    else:
        f.setBold(bold)
    if spacing:
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
    return f


# ---------- 数值格式化 ----------
def fmt_rate(bps: float) -> str:
    """字节/秒 → 人类可读速率，保留 1 位小数。"""
    if bps < 0:
        bps = 0
    units = ["B", "KB", "MB", "GB"]
    i = 0
    while bps >= 1024 and i < len(units) - 1:
        bps /= 1024.0
        i += 1
    if i == 0:
        return f"{bps:.0f} B/s"
    return f"{bps:.1f} {units[i]}/s"


def fmt_gb(value_gb: float) -> str:
    return f"{value_gb:.1f}"


def fmt_mem(mb: float) -> str:
    """进程显存：小于 1G 用 M，超过就换 G，免得出个 5836M。"""
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    return f"{mb:.0f}M"


def load_color(percent: float, base: QColor) -> QColor:
    """负载越高越往警示色偏，让眼睛一眼看出谁在吃满。"""
    if percent >= 90:
        return WARN
    if percent >= 75:
        return QColor(
            int(base.red() * 0.6 + WARN.red() * 0.4),
            int(base.green() * 0.6 + WARN.green() * 0.4),
            int(base.blue() * 0.6 + WARN.blue() * 0.4),
        )
    return base
