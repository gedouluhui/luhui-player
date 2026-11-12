"""生成界面用的小像素资产：开关把手的两张画布 `switch-off.png` / `switch-on.png`。

为什么要落成文件而不是代码里画：QSS 的 `QCheckBox::indicator` 一旦点名给了
background/border，Qt 就**不再画原生那一样东西**（实测：勾上的框只剩一块实心圆角方块，
未勾时是个描边空框——状态能分，但没有把手，看着就是"没做完"）。
而 QSS 里要画自己的东西只有 `image: url(...)` 一条路，url 只认盘上的文件（不支持 data URI、
也不支持代码里现画的 QPixmap）。所以这里把图形画出来存成 PNG，theme.qss() 再引用它。

开关为什么是**两张**而不是一张加偏移：QSS 没有"把 image 往左/往右挪几像素"的写法
（padding 在 indicator 上不生效，实测过）。把手要"关在左、开在右"，就得让它落在画布的
不同位置上——所以两态各一张画布，画布尺寸与轨道内容区同比例，Qt 缩放时不产生位移。

画的内容刻意保持"与主色无关"：把手是近白的哑面圆，不掺任何主题色。因为轨道那一圈的
主色已经被对比度闸门压到"白字 ≥5.2:1"（见 theme._fit_fill），六档主色深浅差得远，
把手只有保持同一份近白才在六档上都读得清。

（曾经还有第三样：勾选框里那颗对勾 `check.png`。布尔项改成开关之后没人引它了，
留着就是"盘上有张图、代码里没有它"的那种死资产，已连同 make_check 一起摘掉，
退役件挪到 tests/out/pre-r12-20261012/ 存着。）

用法：python packaging/make_ui_assets.py        （幂等，重跑覆盖）
"""
from __future__ import annotations

import os
import sys

from PyQt5.QtCore import QPointF, Qt, QRectF
from PyQt5.QtGui import (QColor, QGuiApplication, QImage, QLinearGradient, QPainter,
                         QPen, QPolygonF)

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "ui")
SCALE = 2                   # 开关画布 = 逻辑尺寸 × 2，让 Qt 往下缩而不是往上放大
SW_W, SW_H = 38, 22         # 轨道的逻辑尺寸（和 theme.SWITCH_W/H 必须是同一对数）
KNOB_D = 18                 # 把手直径 = 轨道高 − 上下各 2 的呼吸位
KNOB_INSET = 2              # 把手边缘到轨道边缘的距离

KNOB_TOP = "#F3F5F9"        # 哑面：上沿略亮、下沿略暗，单调下降
KNOB_BOTTOM = "#DFE3EB"     # 最小通道 223，离"近白"判据的 190 还有一截余量
KNOB_RIM = "#C9CED8"        # 一圈收边：把手压在 BG_HOVER（浅档）上时靠这条边分得开


def make_switch(path: str, on: bool) -> None:
    """一张"整条轨道内容区"的画布，透明底＋一颗落在左/右的哑面把手。

    透明底是有意的：轨道那层的底色和描边归 QSS 画（它才拿得到运行时主色），
    这张图只负责"把手在这、长这样"。于是换主题色不需要重生成资产，
    而资产缺失时 theme.qss() 宁可不写 image（轨道仍在、只是没有把手）也不把整张表带崩。
    """
    w, h = SW_W * SCALE, SW_H * SCALE
    img = QImage(w, h, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    d = KNOB_D * SCALE
    inset = KNOB_INSET * SCALE
    # 关＝靠左，开＝靠右：把手左边缘落在 inset 或 (画布宽 − inset − 直径)
    x = inset if not on else w - inset - d
    rect = QRectF(x, (h - d) / 2.0, d, d)
    grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
    grad.setColorAt(0.0, QColor(KNOB_TOP))
    grad.setColorAt(1.0, QColor(KNOB_BOTTOM))
    p.setPen(Qt.NoPen)
    p.setBrush(grad)
    p.drawEllipse(rect)
    p.setBrush(Qt.NoBrush)
    rim = QPen(QColor(KNOB_RIM))
    rim.setWidthF(1.6)
    p.setPen(rim)
    p.drawEllipse(rect.adjusted(0.8, 0.8, -0.8, -0.8))
    p.end()
    if not img.save(path):
        raise SystemExit(f"写不出 {path}")


# 小箭头/对勾：24×24 的逻辑网格画成 48×48，QSS 再缩到 10~14px 显示。
# 和 `app/ui/icons.py` 的矢量图标同一套坐标，改一处口径两边不会漂。
GLYPH_SCALE = 2
GLYPH_DIM = "#A8AFA5"       # TEXT_DIM
GLYPH_TEXT = "#F2EFE6"      # TEXT


def make_glyph(path: str, points: list[tuple[float, float]], color: str,
               width: float) -> None:
    img = QImage(24 * GLYPH_SCALE, 24 * GLYPH_SCALE, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(GLYPH_SCALE, GLYPH_SCALE)
    pen = QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))
    p.end()
    if not img.save(path):
        raise SystemExit(f"写不出 {path}")


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    os.makedirs(OUT, exist_ok=True)
    for name, fn, args in (
            ("switch-off.png", make_switch, (False,)),
            ("switch-on.png", make_switch, (True,))):
        out = os.path.join(OUT, name)
        fn(out, *args)
        print(f"[ok] {out}  {os.path.getsize(out)}B", flush=True)
    for name, points, color, width in (
            ("chevron-down.png", [(6.0, 9.5), (12.0, 15.0), (18.0, 9.5)], GLYPH_DIM, 1.9),
            ("chevron-up.png", [(6.0, 14.5), (12.0, 9.0), (18.0, 14.5)], GLYPH_DIM, 1.9),
            ("chevron-right.png", [(9.5, 6.0), (15.0, 12.0), (9.5, 18.0)], GLYPH_DIM, 1.9),
            ("check.png", [(5.0, 12.6), (9.8, 17.4), (19.0, 7.6)], GLYPH_TEXT, 2.2)):
        out = os.path.join(OUT, name)
        make_glyph(out, points, color, width)
        print(f"[ok] {out}  {os.path.getsize(out)}B", flush=True)
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
