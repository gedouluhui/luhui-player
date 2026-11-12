"""一次性探针（可删）：把开关画出来看一眼，并把"能不能立住判据"需要的读数打出来。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_switch.py

要看清三件事（都只能看像素，读 QSS 不算）：
  ① 两态各画出了什么：关＝把手靠左、开＝把手靠右，还是两张图根本没吃进去；
  ② 把手的外接框是不是近正圆、有没有戳到轨道外面；
  ③ 把手对轨道的对比、轨道对面板底的对比（WCAG 1.4.11 那条 3:1 到底落不落得下）。
同时存一张放大 6 倍的接触表到 tests/out/switch-probe-r12.png，给人眼核对布局（标题贴左、开关贴右）。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtCore import Qt                                # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter, QPixmap   # noqa: E402
from PyQt5.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel,
                             QVBoxLayout, QWidget)          # noqa: E402

import test_ui as TU                                        # noqa: E402
from app.ui import theme                                    # noqa: E402


def measure(img: QImage) -> dict:
    """把一张渲染图拆成"把手像素"（近白）与"轨道像素"（其余不透明），返回几何与均色。"""
    dpr = img.devicePixelRatio() or 1.0
    knob, track = [], []
    for j in range(img.height()):
        for i in range(img.width()):
            c = img.pixelColor(i, j)
            if c.alpha() < 200:
                continue
            if min(c.red(), c.green(), c.blue()) >= 190:
                knob.append((i, j, c))
            else:
                track.append((i, j, c))
    out = {"knob_n": len(knob), "track_n": len(track), "dpr": dpr}
    if knob:
        xs = [p[0] for p in knob]
        ys = [p[1] for p in knob]
        out["knob"] = (round((max(xs) - min(xs) + 1) / dpr, 2),
                       round((max(ys) - min(ys) + 1) / dpr, 2))
        out["knob_x0"] = round(min(xs) / dpr, 2)
        out["knob_x1"] = round(max(xs) / dpr, 2)
        n = len(knob)
        out["knob_hex"] = QColor(round(sum(p[2].red() for p in knob) / n),
                                 round(sum(p[2].green() for p in knob) / n),
                                 round(sum(p[2].blue() for p in knob) / n)).name()
    if track:
        xs = [p[0] for p in track]
        ys = [p[1] for p in track]
        out["track"] = (round((max(xs) - min(xs) + 1) / dpr, 2),
                        round((max(ys) - min(ys) + 1) / dpr, 2))
        out["track_x0"] = round(min(xs) / dpr, 2)
        out["track_x1"] = round(max(xs) / dpr, 2)
        n = len(track)
        out["track_hex"] = QColor(round(sum(p[2].red() for p in track) / n),
                                  round(sum(p[2].green() for p in track) / n),
                                  round(sum(p[2].blue() for p in track) / n)).name()
    return out


def render(sheet: str, checked: bool, size=None) -> QImage:
    old = QApplication.instance().styleSheet()
    QApplication.instance().setStyleSheet(sheet)
    try:
        c = QCheckBox("")
        c.setChecked(checked)
        if size:
            c.setFixedSize(*size)
        c.adjustSize()
        c.ensurePolished()
        return c.grab().toImage().convertToFormat(QImage.Format_ARGB32)
    finally:
        QApplication.instance().setStyleSheet(old)


def main() -> int:
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.install(app, theme.ACCENT_DEFAULT)
    print(f"[env] dpr={app.primaryScreen().devicePixelRatio()}")
    for accent in theme.ACCENTS:
        theme.set_accent(accent[0])
        app.setStyleSheet(theme.qss())
        for checked in (False, True):
            img = render(theme.qss(), checked)
            m = measure(img)
            ct = (theme.contrast(m.get("knob_hex", "#000000"), m.get("track_hex", "#000000"))
                  if m.get("knob_hex") and m.get("track_hex") else 0.0)
            print(f"[case] {accent[1]:>6} {'开' if checked else '关'} "
                  f"widget={img.width() / (img.devicePixelRatio() or 1):.0f}x"
                  f"{img.height() / (img.devicePixelRatio() or 1):.0f} "
                  f"knob={m.get('knob')} x=({m.get('knob_x0')},{m.get('knob_x1')}) "
                  f"n={m['knob_n']} {m.get('knob_hex')} | "
                  f"track={m.get('track')} x=({m.get('track_x0')},{m.get('track_x1')}) "
                  f"{m.get('track_hex')} | 把手/轨道={ct:.2f}:1")
    # 接触表：三行（默认蓝 / 橙 / 灰）× 两态，外加一整行真实排法（标题贴左、开关贴右）
    theme.set_accent(theme.ACCENT_DEFAULT)
    app.setStyleSheet(theme.qss())
    rows = QWidget()
    lay = QVBoxLayout(rows)
    for label in ("避开视频边框", "字幕粗体", "上下镜像"):
        r = QHBoxLayout()
        c = QCheckBox(rows)
        c.setAccessibleName(label)
        lab = QLabel(label, rows)
        lab.setObjectName("AppTitle")
        r.addWidget(lab)
        r.addStretch(1)
        r.addWidget(c)
        if label == "上下镜像":
            c.setChecked(True)
        lay.addLayout(r)
    rows.ensurePolished()
    rows.adjustSize()
    shot = rows.grab()
    big = QPixmap(shot.width() * 6, shot.height() * 6)
    big.fill(QColor(theme.BG))
    p = QPainter(big)
    p.setRenderHint(QPainter.SmoothPixmapTransform, False)
    p.drawPixmap(big.rect(), shot)
    p.end()
    outdir = os.path.join(HERE, "out")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "switch-probe-r12.png")
    big.save(path)
    print(f"[shot] {path}  {os.path.getsize(path)}B")
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
