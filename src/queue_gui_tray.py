# SPDX-License-Identifier: GPL-3.0-only

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QPixmap

from queue_gui_common import app_icon

# Animated states are redrawn on a timer; the others are drawn once per change.
ANIMATED_TRAY_STATES = {"busy", "reviewing"}
TRAY_FRAME_MS = 80
SPIN_PERIOD = 1.2
PULSE_PERIOD = 1.4
SIZE = 64
BLUE = QColor("#2563eb")
SPINNER_BLUE = QColor("#3b82f6")
GREEN = QColor("#22c55e")
AMBER = QColor("#f59e0b")

_rabbits: dict[bool, QPixmap] = {}


def rabbit(grey: bool = False) -> QPixmap:
    if grey not in _rabbits:
        pixmap = app_icon().pixmap(SIZE, SIZE)
        if grey:
            image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
            for y in range(image.height()):
                for x in range(image.width()):
                    color = image.pixelColor(x, y)
                    gray = round(0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue())
                    image.setPixelColor(x, y, QColor(gray, gray, gray, round(color.alpha() * 0.55)))
            pixmap = QPixmap.fromImage(image)
        _rabbits[grey] = pixmap
    return _rabbits[grey]


def _cut_out(painter: QPainter, path: QPainterPath) -> None:
    # A transparent gap separates badges from the rabbit on any panel colour.
    painter.save()
    painter.setCompositionMode(QPainter.CompositionMode_Clear)
    painter.setPen(QPen(Qt.black, 5))
    painter.setBrush(Qt.black)
    painter.drawPath(path)
    painter.restore()


def _badge(painter: QPainter, color: QColor) -> None:
    path = QPainterPath()
    path.addEllipse(QPointF(49, 49), 13, 13)
    _cut_out(painter, path)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)


def _fitted_font(text: str, width: float, start: int) -> QFont:
    font = QFont()
    font.setBold(True)
    for size in range(start, 7, -1):
        font.setPixelSize(size)
        if QFontMetrics(font).horizontalAdvance(text) <= width:
            break
    return font


def _spinner(painter: QPainter, phase: float) -> None:
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QColor(37, 99, 235, 77), 5))
    painter.drawEllipse(QPointF(32, 32), 29, 29)
    painter.setPen(QPen(SPINNER_BLUE, 5, Qt.SolidLine, Qt.RoundCap))
    start = -360 * ((phase / SPIN_PERIOD) % 1)
    painter.drawArc(QRectF(3, 3, 58, 58), round(start * 16), -110 * 16)


def _time_pill(painter: QPainter, label: str) -> None:
    pill = QPainterPath()
    pill.addRoundedRect(QRectF(4, 40, 56, 22), 11, 11)
    _cut_out(painter, pill)
    painter.setPen(Qt.NoPen)
    painter.setBrush(BLUE)
    painter.drawPath(pill)
    painter.setPen(Qt.white)
    painter.setFont(_fitted_font(label, 50, 17))
    painter.drawText(QRectF(4, 40, 56, 22), Qt.AlignCenter, label)


def render_tray_icon(state: str, label: str = "", count: int = 0, phase: float = 0.0) -> QPixmap:
    pixmap = QPixmap(SIZE, SIZE)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    if state == "countdown":
        painter.drawPixmap(QRectF(10, 0, 44, 44), rabbit(), QRectF(0, 0, SIZE, SIZE))
        _time_pill(painter, label)
    elif state == "available":
        painter.drawPixmap(QRectF(2, 2, 60, 60), rabbit(), QRectF(0, 0, SIZE, SIZE))
        _badge(painter, GREEN)
        check = QPainterPath(QPointF(43, 49))
        check.lineTo(47, 53)
        check.lineTo(55, 45)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(Qt.white, 3.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPath(check)
    elif state == "stopped":
        painter.drawPixmap(QRectF(2, 2, 60, 60), rabbit(grey=True), QRectF(0, 0, SIZE, SIZE))
        _badge(painter, AMBER)
        painter.setBrush(Qt.white)
        painter.drawRoundedRect(QRectF(44, 43, 3.5, 12), 1, 1)
        painter.drawRoundedRect(QRectF(50.5, 43, 3.5, 12), 1, 1)
    elif state == "busy":
        painter.setOpacity(0.7 + 0.3 * math.cos(2 * math.pi * phase / PULSE_PERIOD))
        painter.drawPixmap(QRectF(2, 2, 60, 60), rabbit(), QRectF(0, 0, SIZE, SIZE))
    elif state == "reviewing" and label:
        _spinner(painter, phase)
        painter.drawPixmap(QRectF(14, 6, 36, 36), rabbit(), QRectF(0, 0, SIZE, SIZE))
        _time_pill(painter, label)
    elif state == "reviewing":
        _spinner(painter, phase)
        painter.drawPixmap(QRectF(10, 10, 44, 44), rabbit(), QRectF(0, 0, SIZE, SIZE))
        if count:
            _badge(painter, BLUE)
            text = str(count) if count < 10 else "9+"
            painter.setPen(Qt.white)
            painter.setFont(_fitted_font(text, 20, 17))
            painter.drawText(QRectF(36, 36, 26, 26), Qt.AlignCenter, text)
    else:
        painter.drawPixmap(QRectF(0, 0, SIZE, SIZE), rabbit(), QRectF(0, 0, SIZE, SIZE))
    painter.end()
    return pixmap
