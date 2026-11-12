"""一次性探针（可删）：标题栏/侧栏的材质面是否真的画出来了、画成了什么梯度。

跑法（离屏）：
  QT_QPA_PLATFORM=offscreen GELU_DATA=tests/.selftest-data python tests/probe_material.py

量四件事：
  ① 接线：标题栏与侧栏重绘时都走到 `material.paint_surface`，角色各是 title/side；
  ② 梯度：上亮下暗、顶部一道受光；侧栏左缘有一道内阴影；
  ③ 噪点：平坦处的亮度不是一整块常数（防 banding 的那层砂纸在）；
  ④ 材质没盖掉 QSS 让出的线：标题栏最后一行/侧栏第一列不被材质涂改。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtCore import Qt                                         # noqa: E402
from PyQt5.QtGui import QColor, QImage                               # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget                    # noqa: E402

import test_ui as TU                                                  # noqa: E402
from app import config as cfgmod                                      # noqa: E402
from app.ui import material, theme                                    # noqa: E402

OK = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        OK = False


def luma(c: QColor) -> float:
    return 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()


def patch_spread(img: QImage, x: int, y: int, w: int, h: int) -> int:
    lo, hi = 255.0, 0.0
    for yy in range(y, min(y + h, img.height())):
        for xx in range(x, min(x + w, img.width())):
            v = luma(img.pixelColor(xx, yy))
            lo, hi = min(lo, v), max(hi, v)
    return int(round(hi - lo))


def main() -> int:
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    app.setStyle("Fusion")
    cfg = cfgmod.Config()
    theme.install(app, str(cfg["theme_accent"]))
    app.setStyleSheet(theme.qss())
    win = TU.build_window(cfg)
    TU.pump(0.4)
    win._set_sidebar(True, 0)      # 侧栏出厂是收起的：重绘接线要在它真可见时量
    TU.pump(0.3)

    # ① 接线：真重绘时走到材质函数
    seen: list[str] = []
    orig = material.paint_surface

    def spy(w, role):
        seen.append(role)
        return orig(w, role)

    material.paint_surface = spy
    try:
        win.title.repaint()
        win.sidebar.repaint()
        TU.pump(0.15)
    finally:
        material.paint_surface = orig
    check("标题栏重绘走到材质面", "title" in seen, f"roles={seen}")
    check("侧栏重绘走到材质面", "side" in seen, f"roles={seen}")

    # ②③ 标题栏的实际像素
    tb = win.title
    pm = tb.grab()
    img = pm.toImage()
    dpr = pm.devicePixelRatio()
    cw = max(1, int(round(tb.width() * dpr)))
    ch = max(1, int(round(tb.height() * dpr)))
    check("标题栏抓图尺寸对得上", img.width() == cw and img.height() == ch,
          f"img={img.width()}x{img.height()} want={cw}x{ch}")

    xm = img.width() // 2
    top = luma(img.pixelColor(xm, 2))
    mid = luma(img.pixelColor(xm, img.height() // 2))
    bot = luma(img.pixelColor(xm, img.height() - 4))
    check("标题栏上亮下暗", top > mid >= bot, f"top={top:.1f} mid={mid:.1f} bot={bot:.1f}")

    light = luma(img.pixelColor(xm, 1))
    below = luma(img.pixelColor(xm, 16))
    check("顶部有受光边", light > below + 0.4, f"y1={light:.1f} y16={below:.1f}")

    spread = patch_spread(img, xm - 24, img.height() // 2 - 8, 48, 12)
    check("平坦处有细噪点（不是一整块常数）", spread >= 1, f"spread={spread}")

    # 侧栏材质：单独渲染一块，避免子控件盖住读数
    s = material.Surface(role="side")
    s.resize(240, 600)
    simg = QImage(240, 600, QImage.Format_ARGB32)
    simg.fill(QColor("#FF00FF"))       # 哨兵色：没画到的地方一眼就露
    s.render(simg)
    st = luma(simg.pixelColor(120, 2))
    sm = luma(simg.pixelColor(120, 300))
    sb = luma(simg.pixelColor(120, 596))
    check("侧栏上亮下暗", st > sm >= sb, f"top={st:.1f} mid={sm:.1f} bot={sb:.1f}")
    edge = luma(simg.pixelColor(3, 300))
    inner = luma(simg.pixelColor(30, 300))
    check("侧栏左缘有内阴影", edge < inner - 1.0, f"x3={edge:.1f} x30={inner:.1f}")
    check("材质不涂侧栏首列（留给 QSS 发丝线）",
          simg.pixelColor(0, 300) == QColor("#FF00FF"), f"x0={simg.pixelColor(0, 300).name()}")
    last = img.pixelColor(xm, img.height() - 1)
    check("材质不涂标题栏最后一行（QSS 分隔线还在）",
          abs(luma(last) - luma(QColor(theme.BORDER_SOFT))) < 3,
          f"last={last.name()} want~{theme.BORDER_SOFT}")

    # ④ 视频区没有被材质波及：材质只挂在标题栏与侧栏上
    check("视频容器不是材质面", not isinstance(win.video, material.Surface))
    check("标题栏与侧栏是两处材质面",
          isinstance(win.sidebar, material.Surface) and isinstance(win.title, QWidget)
          and win.title.testAttribute(Qt.WA_StyledBackground))

    win.close()
    TU.pump(0.2)
    print("RESULT:", "PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
