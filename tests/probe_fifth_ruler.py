"""一次性探针（可删）：第五把尺（掉出视口）在真产品树上到底量到了什么。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_fifth_ruler.py [scale ...]

为什么还要再问一遍：`probe_viewport_clip.py` 按"右沿越过视口"数出 2.5 档 50 个、3.0 档 66 个，
而 `capture_sweep --font-scale 2.5/3.0` 挂上第五把尺之后**一条都没报**（48→48 条判据全绿）。
两个数不可能同时对：要么探针数错了（把"露出 1px"也算越界，第五把尺按设计不报那类），
要么第五把尺在真树上根本没生效（合成件里 CAUGHT，产品树里却沉默＝第二套盲区）。
所以这里对同一棵树同时打三组数，逐层摊开：
  ① 滚动区的横向策略与 `horizontalScrollBar().maximum()`（第五把尺"能滚就不报"的那道闸）；
  ② 越右沿的控件分成"整块在外面"和"只露出一点"两堆，各报几个；
  ③ 第五把尺自己的返回条数，以及它看到的裁剪来源。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtCore import QPoint, Qt                              # noqa: E402
from PyQt5.QtWidgets import (QApplication, QLabel, QScrollArea, QWidget)  # noqa: E402

import capture_sweep as CS                                          # noqa: E402
import test_ui as TU                                                # noqa: E402
from app import config as cfgmod                                    # noqa: E402
from app.ui import theme                                            # noqa: E402

SCALES = [float(x) for x in sys.argv[1:]] or [2.5, 3.0]


def txt(w: QWidget) -> str:
    s = CS._label_of(w)
    return s or w.objectName() or type(w).__name__


def main() -> int:
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    app.setStyle("Fusion")
    cfg = cfgmod.Config()
    theme.install(app, str(cfg["theme_accent"]))
    for scale in SCALES:
        CS._scale_qss(scale)
        app.setStyleSheet(theme.qss())
        win = TU.build_window(cfg)
        TU.pump(0.3)
        print(f"==== 字号档 {scale:g} 侧栏={win.sidebar.width()} 窗口={win.width()}×{win.height()} ====")
        for i in range(win.sidebar_stack.count()):
            win._set_sidebar(True, i)
            TU.pump(0.3)
            page = win.sidebar_stack.widget(i)
            name = str(win.tab_defs[i][0])
            areas = [s for s in page.findChildren(QScrollArea) if s.widget() is not None]
            # ① 滚动区那一层
            for s in areas:
                pol = s.horizontalScrollBarPolicy()
                print(f"-- {name}: 滚动区 横向策略="
                      f"{'AlwaysOff' if pol == Qt.ScrollBarAlwaysOff else ('AsNeeded' if pol == Qt.ScrollBarAsNeeded else 'AlwaysOn')}"
                      f" range.max={int(s.horizontalScrollBar().maximum())}"
                      f" 视口宽={s.viewport().width()} 内容宽={s.widget().width()}")
            if not areas:
                print(f"-- {name}: 这页没有滚动区")
            # ② 越右沿的两堆
            for s in areas:
                body = s.widget()
                vpr = s.viewport().width()

                def right(w: QWidget) -> int:
                    return int(w.mapTo(body, QPoint(0, 0)).x()) + int(w.width())

                def left(w: QWidget) -> int:
                    return int(w.mapTo(body, QPoint(0, 0)).x())

                cands = [w for w in body.findChildren(QLabel)
                         if w.isVisible() and w.width() > 0 and CS._label_of(w)]
                cands += [w for w in body.findChildren(QWidget)
                          if w.isVisible() and w.width() > 0 and type(w).__name__ != "QLabel"
                          and CS._label_of(w)]
                over = [w for w in cands if right(w) > vpr + 1]
                full = [w for w in over if left(w) >= vpr]
                part = [w for w in over if left(w) < vpr]
                print(f"   带字控件 {len(cands)} 个：越右沿 {len(over)}（整块在外 {len(full)} / 只露一点 {len(part)}）")
                for w in sorted(full, key=lambda x: -right(x))[:4]:
                    print(f"     · 整块在外 {type(w).__name__}「{txt(w)[:18]}」 x={left(w)}..{right(w)} 视口右沿={vpr}")
                for w in sorted(part, key=lambda x: -right(x))[:3]:
                    print(f"     · 只露一点 {type(w).__name__}「{txt(w)[:18]}」 x={left(w)}..{right(w)} 视口右沿={vpr}")
            # ③ 第五把尺在整条侧栏上的返回
            hits = CS.beyond_viewport(win.sidebar)
            print(f"   beyond_viewport(整条侧栏) = {len(hits)} 条")
            for h in hits[:4]:
                print("     ↳ " + h[:200])
        win.close()
        TU.pump(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
