"""一次性探针（可删）：字号放大档里，侧栏每一页有没有内容**掉出视口右边**（四把尺看不见的那类）。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_viewport_clip.py [scale ...]

为什么单独问这个：`capture_sweep` 的截字尺/压扁尺/裁而未告尺比的都是"控件自己的宽 vs 它要的字宽"。
可 `QScrollArea` + `setWidgetResizable(True)` + 横向滚动条关闭这一套，坏的形状是**另一件事**：
内容块整体比视口宽，右边的控件**自己一点没被压**（字是全的、格子是全的），只是落在了裁剪框外面——
布局"排开了"，用户看不见也点不到。四把尺一条都不报，`--font-scale 3.0` 全绿就是这个原因。
所以这里按页打两组数：
  ① 视口宽 / `form_rows.content_width` 要宽（产品拿来当加宽目标的那个数）；
  ② 真的越过视口右沿的控件有几个、各自是谁、它在布局树里被哪一格顶起来的。

**2026-10-10 订正（这份探针最初的读数不算数）**：它在一个进程里连着换档
（`for scale in SCALES: CS._scale_qss(scale)`），而当时 `_scale_qss` 的起算点是"当前的
`theme.qss`"，于是第二档是拿第一档的产物再乘一遍——所谓"3.0 档内容宽 1199、越过右沿 66 个"
其实是 1.8×2.5×3.0＝13.5 倍字号的形状。`_scale_qss` 改成从原始那份起算后重跑
（`tests/logs/probe-fifth-clean-*.log`、`probe-fifth-headroom-*.log`）：1.0–3.5 一档都不越界，
真整块掉出去的形状要到 6.0 档才出现（调节/画面各 5 个）。留这份探针是为了"按内容块坐标算右沿"
那个算法本身（`w.x()` 是父级坐标，直接跟视口宽比会漏），不是为了那两个错数。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtCore import QPoint                                # noqa: E402
from PyQt5.QtWidgets import (QApplication, QScrollArea, QWidget)   # noqa: E402

import capture_sweep as CS                                         # noqa: E402
import test_ui as TU                                               # noqa: E402
from app import config as cfgmod                                   # noqa: E402
from app.ui import form_rows, theme                                # noqa: E402

SCALES = [float(x) for x in sys.argv[1:]] or [3.0]


def page_name(win, i: int) -> str:
    try:
        return str(win.tab_defs[i][0])
    except Exception:
        return f"#{i}"


def txt(w: QWidget) -> str:
    for attr in ("text", "currentText", "placeholderText"):
        f = getattr(w, attr, None)
        if callable(f):
            try:
                s = str(f())
            except Exception:
                continue
            if s:
                return s
    return w.objectName() or type(w).__name__


def scrolls(page: QWidget) -> list[QScrollArea]:
    return [s for s in page.findChildren(QScrollArea) if s.widget() is not None]


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
        print(f"==== 字号档 {scale:g} 侧栏={win.sidebar.width()} ====")
        for i in range(win.sidebar_stack.count()):
            win._set_sidebar(True, i)
            TU.pump(0.25)
            page = win.sidebar_stack.widget(i)
            need = form_rows.content_width(page)
            print(f"-- {page_name(win, i)}：content_width 要 {need}（侧栏 {win.sidebar.width()}）")
            for s in scrolls(page) + ([] if scrolls(page) else [None]):
                if s is None:
                    print("   （这页没有滚动区，内容宽就是页宽，超出会被布局压——四把尺看得见那类）")
                    continue
                vp = s.viewport().width()
                body = s.widget()
                # 右沿要按**内容块坐标系**算：`w.x()` 是相对它自己的父级（一行、一个盒子），
                # 直接拿它跟视口宽比，会把"父级在左边、自己宽 300"的控件算成没越界。
                def right(w: QWidget) -> int:
                    return int(w.mapTo(body, QPoint(0, 0)).x()) + int(w.width())
                over = [w for w in body.findChildren(QWidget) if right(w) > vp + 1]
                show = [w for w in over if w.isVisible() and w.width() > 0]
                print(f"   视口 {vp} / 内容实宽 {body.width()} / 内容min {body.minimumSizeHint().width()}"
                      f" → 越过右沿的控件 {len(show)} 个")
                for w in sorted(show, key=lambda x: -right(x))[:6]:
                    print(f"     · {type(w).__name__}「{txt(w)[:22]}」 右沿={right(w)} w={w.width()} "
                          f"minHint={w.minimumSizeHint().width()}")
        win.close()
        TU.pump(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
