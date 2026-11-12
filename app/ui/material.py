"""标题栏与侧栏的材质面：极暗的多层渐变 + 细噪点 + 受光边，做出 Mica 那种"不是一块死黑"的深度。

为什么是自绘而不是真正的 DWM Mica/Acrylic：那两条路都要求顶层窗口
`WA_TranslucentBackground`（整窗逐像素透明）。而 mpv 是用 `--wid` 挂进来的**原生子窗口**，
顶层一旦进入 layered/透明合成，子窗口的画面上不上屏就不再由 Qt 保证——黑屏是这类改动
的常见下场。加上验收纪律不许开前台窗口，DWM 合成的实际效果也就没法目检。
所以这里取可控的等价物：渐变、噪点、内阴影全在 Qt 的绘制体系里，Win10/11 表现一致，
视频区仍是一块纯黑，不碰任何窗口合成路径。

约定：
  · 表面**不透明**：渐变第一站 alpha=255，噪点只改 1–3 级亮度，不引入穿透。
  · 只画该让出的那一两行：标题栏留出底部分隔线、侧栏留出左侧发丝线，
    QSS 画的那两根线原样露着，不在这里重描一遍（两处都描会在高 DPI 下发虚）。
"""
from __future__ import annotations

import random

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QImage, QLinearGradient, QPainter, QPixmap
from PyQt5.QtWidgets import QWidget

_TILE: QPixmap | None = None


def _noise_tile() -> QPixmap:
    """128×128 的细噪点瓦片：深色大面上的一道防 banding 砂纸，透明度低到只在该看见时看见。"""
    global _TILE
    if _TILE is not None:
        return _TILE
    size = 128
    rnd = random.Random(0x6D696361)      # "mica"：固定种子，同一台机器每次启动纹理一致
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(Qt.transparent)
    for y in range(size):
        for x in range(size):
            v = rnd.random()
            if v < 0.30:
                img.setPixelColor(x, y, QColor(255, 255, 255, 3))
            elif v < 0.60:
                img.setPixelColor(x, y, QColor(0, 0, 0, 3))
    _TILE = QPixmap.fromImage(img)
    return _TILE


def paint_surface(w: QWidget, role: str) -> None:
    """在 w 上画一层材质。role: "title"（标题栏）/ "side"（侧栏）。"""
    p = QPainter(w)
    p.setRenderHint(QPainter.Antialiasing, False)
    r = w.rect()
    if role == "title":
        # 最后一行留给 QSS 的 border-bottom
        body = r.adjusted(0, 0, 0, -1)
        g = QLinearGradient(0, body.top(), 0, max(body.top() + 1, body.bottom()))
        g.setColorAt(0.0, QColor("#111512"))
        g.setColorAt(0.42, QColor("#0A0D0B"))
        g.setColorAt(1.0, QColor("#080A09"))
    else:
        # 第一列留给 QSS 的 border-left
        body = r.adjusted(1, 0, 0, 0)
        g = QLinearGradient(0, body.top(), 0, max(body.top() + 1, body.bottom()))
        g.setColorAt(0.0, QColor("#141814"))
        g.setColorAt(0.28, QColor("#0F1310"))
        g.setColorAt(1.0, QColor("#0B0E0C"))
    p.fillRect(body, g)
    p.drawTiledPixmap(body, _noise_tile())

    # 顶部受光：1px 亮线 + 14px 衰减。PWM 调光下这一道决定"有没有厚度"。
    hi = QLinearGradient(0, body.top(), 0, body.top() + 14)
    hi.setColorAt(0.0, QColor(255, 255, 255, 16))
    hi.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillRect(QRectF(body.left(), body.top(), body.width(), 14), hi)

    if role == "side":
        # 左侧内阴影：玻璃板贴着画面那一侧收进去一点，侧栏才像"压在"画面上而不是并排。
        sh = QLinearGradient(body.left(), 0, body.left() + 12, 0)
        sh.setColorAt(0.0, QColor(0, 0, 0, 46))
        sh.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(body.left(), body.top(), 12, body.height()), sh)
    else:
        # 标题栏底部一道淡影：它下面就是画面，平切会显得两层贴纸。
        sh = QLinearGradient(0, body.bottom() - 6, 0, body.bottom())
        sh.setColorAt(0.0, QColor(0, 0, 0, 0))
        sh.setColorAt(1.0, QColor(0, 0, 0, 34))
        p.fillRect(QRectF(body.left(), body.bottom() - 6, body.width(), 6), sh)


class Surface(QWidget):
    """一块自绘材质的普通容器（侧栏用）；其余行为与 QWidget 相同。"""

    def __init__(self, parent=None, role: str = "side"):
        super().__init__(parent)
        self._role = role
        # 子类默认不画 QSS 背景/边框；侧栏那条左侧发丝线与底色交给样式画，
        # 材质只覆盖它让出的区域（见 paint_surface 的 adjusted）。
        self.setAttribute(Qt.WA_StyledBackground, True)

    def paintEvent(self, _ev):          # noqa: N802 — Qt 命名
        paint_surface(self, self._role)
