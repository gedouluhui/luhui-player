"""一次性探针（可删）：把 QSlider::handle 的几种 QSS 写法真画出来，量把手的外接框。

跑法：PYTHONIOENCODING=utf-8 QT_QPA_PLATFORM=offscreen GELU_DATA=tests/.selftest-data \
      python tests/probe_handle_shape.py

为什么要探而不是直接改：上一版把手"想画成圆、画出来是竖圆角方块"，而三种成因
（height 生效 / groove+2×|margin| / 被控件矩形截断）给出的修法完全不同。
这一趟先扫出规律，再用**产品自己那张样式表**（theme.qss()）量改前改后各是什么形状，
test_ui 里那把常驻尺子（handle_px）就是这个扫法的定格版。
只用**外接框宽高**不用绝对像素数：离屏装配 dpr=1.0、真机 1.5，绝对数跨装配不可比，
而"宽等于高"两边一致。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtCore import Qt                                    # noqa: E402
from PyQt5.QtGui import QImage                                  # noqa: E402
from PyQt5.QtWidgets import QApplication, QSlider               # noqa: E402

from app.main import _enable_high_dpi                           # noqa: E402
from app.ui import theme                                        # noqa: E402


def sheet_for(w: int, h: int, m: int, r: int) -> str:
    """拼一张只够画一根滑轨的样式表（字符串拼接，不用 format：QSS 的花括号太多，
    f-string / format 都要把 CSS 的 { } 写成 {{ }}，一处漏写就是整表静默失效）。"""
    return ("QWidget { background: transparent; }\n"
            "QSlider::groove:horizontal { height: 4px; background: " + theme.BORDER +
            "; border-radius: 2px; }\n"
            "QSlider::sub-page:horizontal { background: " + theme.ACCENT +
            "; border-radius: 2px; }\n"
            "QSlider::handle:horizontal { width: " + str(w) + "px; height: " + str(h) +
            "px; margin: " + str(m) + "px 0; border-radius: " + str(r) + "px; background: " +
            theme.TEXT + "; }\n")


def handle_bbox(app: QApplication, sheet: str, widget_w: int = 200,
                widget_h: int = 22) -> tuple[int, int, int]:
    old = app.styleSheet()
    app.setStyleSheet(sheet)
    try:
        s = QSlider(Qt.Horizontal)
        s.setRange(0, 100)
        s.setValue(50)
        s.setFixedSize(widget_w, widget_h)
        s.ensurePolished()
        img = s.grab().toImage().convertToFormat(QImage.Format_ARGB32)
        pts = []
        for j in range(img.height()):
            for i in range(img.width()):
                c = img.pixelColor(i, j)
                if c.alpha() >= 200 and min(c.red(), c.green(), c.blue()) >= 190:
                    pts.append((i, j))
        if not pts:
            return (0, 0, 0)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1, len(pts))
    finally:
        app.setStyleSheet(old)


def show(tag: str, bw: int, bh: int, n: int) -> None:
    ratio = (bw / bh) if bh else 0.0
    print(f"[{tag}] bbox={bw}x{bh} ratio={ratio:.2f} 近白像素={n}", flush=True)


def main() -> int:
    _enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.install(app, theme.ACCENT_DEFAULT)
    print(f"[env] platform={app.platformName()} dpr={app.primaryScreen().devicePixelRatio()}",
          flush=True)

    # ① 规律扫描：height 固定、只动 margin，看画出来的高度跟谁走
    print("—— A. height 是否生效（margin=0 时换 height）——", flush=True)
    for h in (8, 12, 16, 22):
        show(f"height={h}", *handle_bbox(app, sheet_for(12, h, 0, 6)))
    print("—— B. groove+2×|margin| 假设（width=12 height=12）——", flush=True)
    for m in (0, -2, -3, -4, -5, -6, -8):
        show(f"margin={m}", *handle_bbox(app, sheet_for(12, 12, m, 6)))
    print("—— C. 控件矩形会不会截断（同一份 QSS 换控件高）——", flush=True)
    for wh in (10, 14, 18, 22, 32):
        show(f"widget高={wh}", *handle_bbox(app, sheet_for(14, 14, -5, 7), widget_h=wh))

    # ② 产品自己那张表：现状 vs 改前那一条（把新值换回旧值就是"改前"）
    print("—— D. 产品样式表 theme.qss() ——", flush=True)
    now = theme.qss()
    new_rule = "width: 14px; height: 14px; margin: -5px 0; border-radius: 7px"
    old_rule = "width: 12px; height: 12px; margin: -5px 0; border-radius: 6px"
    show("现状(14/-5)", *handle_bbox(app, now))
    if new_rule in now:
        show("改回旧值(12/-5)", *handle_bbox(app, now.replace(new_rule, old_rule)))
        show("margin 放宽(-8)", *handle_bbox(app, now.replace(new_rule, old_rule.replace("-5", "-8"))))
        no_rule = "\n".join(ln for ln in now.splitlines() if "QSlider::handle" not in ln)
        show("整条规则摘掉", *handle_bbox(app, no_rule))
    else:
        print("[warn] 现状样式表里没有预期的那一条把手规则，D 段的对照不作数", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
