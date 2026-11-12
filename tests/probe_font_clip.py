"""一次性探针（可删）：字号放大档里那几条红，到底是**哪一层**把字裁掉的。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_font_clip.py [scale]

为什么问"哪一层"而不是直接加宽：capture_sweep --font-scale 1.3 报了
「调节页 QLabel『1.00x』需 40px，只露出 37px（自身 49px）」——自身够宽、露出不够，
说明切它的是某个祖先。但"某个祖先"有五种可能（列最小宽、面板 margin、滚动区视口、
分隔线占位、布局把格子压小），修法各不相同，猜一个改法就是把三处一起改错。
所以这里把祖先链逐层打出来：每层自己的矩形、以及求交到这里还剩多宽。
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtWidgets import (QAbstractButton, QApplication, QLabel, QScrollArea, QWidget)  # noqa: E402

import capture_sweep as CS                                   # noqa: E402
import test_ui as TU                                         # noqa: E402
from app import config as cfgmod                             # noqa: E402
from app.ui import theme                                     # noqa: E402

SCALE = float(sys.argv[1]) if len(sys.argv) > 1 else 1.5


def chain(w: QWidget, top: QWidget) -> str:
    """从控件往上逐层求交，打印"裁到这一层还剩多宽"——最后一层掉宽的就是切字的那位。"""
    from PyQt5.QtCore import QRect
    r = CS.QRectOf(w, top)
    out = [f"{type(w).__name__} 自身 {w.width()}×{w.height()} @({w.x()},{w.y()}) → 露出 {r.width()}"]
    a = w.parentWidget()
    while a is not None:
        box = a.viewport() if isinstance(a, QScrollArea) else a
        r = r.intersected(CS.QRectOf(box, top))
        out.append(f"  ↑ {type(a).__name__}{'(滚动区视口)' if isinstance(a, QScrollArea) else ''} "
                   f"{a.width()}×{a.height()} → 求交后 {r.width()}")
        if a is top:
            break
        a = a.parentWidget()
    return "\n".join(out)


def hits(root: QWidget, label: str) -> None:
    """四把尺走一遍，但**先问"这一棵树到底有没有在被量"**。

    上一版这里就是吃了自己的亏：探针只调了 `_sidebar_page(page)` 而没 `_set_sidebar(True, …)`，
    侧栏整棵子树是隐藏的，四把尺按设计跳过隐藏控件，于是打印出"全绿"——那是"没量到"，
    不是"量过且没问题"。所以现在把"看得见且带文字的控件数"打出来，少于阈值直接点名。
    """
    seen = [w for w in root.findChildren(QLabel) + root.findChildren(QAbstractButton)
            if w.isVisible() and w.text()]
    bad = CS.clipped_without_hint(root) + CS.colliding_text(root) + CS.squeezed_widgets(root)
    print(f"\n=== {label} === 可见带文字控件 {len(seen)} 个")
    if len(seen) < 6:
        print("  !! 这一棵树几乎没在被量（隐藏／没建好）——上面的「全绿」不作数")
        return
    if not bad:
        print("  四把尺全绿")
        return
    for b in bad[:6]:
        print(f"  · {b}")
    for w in root.findChildren(QLabel):
        # 只给**看得见**的那几个打祖先链：QStackedWidget 里没当前页的那几页尺寸是悬空的
        # （上一版探针就是这么打出一堆"90×30 的面板"——那是没参与布局的页，不是产品的形状）
        if not w.isVisible() or not w.text():
            continue
        if w.fontMetrics().horizontalAdvance(w.text()) > CS.clipped_rect(w, w.window()).width():
            if "…" in w.text() or str(w.toolTip()).strip():
                continue
            print(f"  祖先链（「{w.text()[:10]}」）：\n{chain(w, w.window())}")


def main() -> int:
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    app.setStyle("Fusion")
    CS._scale_qss(SCALE)
    cfg = cfgmod.Config()
    theme.install(app, str(cfg["theme_accent"]))
    sizes = [int(s.split("px")[0]) for s in theme.qss().split("font-size:")[1:] if "px" in s]
    print(f"字号档 = {SCALE}（QSS 里现在的 font-size 档位：{sorted(set(sizes))}）")
    win = TU.build_window(cfg)
    win.add_paths(TU.FILES)
    TU.wait_for(lambda: float(win.player.prop("duration", 0) or 0) > 0, 12.0)
    TU.pump(0.4)
    win._set_sidebar(True, 0)
    TU.pump(0.3)
    # 页签重叠的机制要先看清：是"每格被布局压破固定宽"还是"整排超出父容器"，修法不一样。
    print("页签条：侧栏宽=" + str(win.sidebar.width()) + " 这一排需要=" +
          str(win._layout_tabs(win.sidebar.width())) + " 缝=" + str(win.tab_row_spacing))
    for b, (label, _t) in zip(win.tab_btns, win.tab_defs):
        print(f"   {label}: x={b.x()} 宽={b.width()} sizeHint={b.sizeHint().width()} "
              f"字={b.fontMetrics().horizontalAdvance(label)} 父宽={b.parentWidget().width()}")
    # 页名从 `tab_defs` 现取（第 6 页「声音均衡」2026-10-12 加进来时，这里抄的那份
    # 五页名单会把它整个漏掉，而漏掉在输出里长得和"这一页干净"一模一样）。
    for page, name in enumerate(str(d[0]) for d in win.tab_defs):
        # 必须 _set_sidebar(True, page)：只切页不显形，整棵子树是隐藏的，四把尺会"量不到而全绿"
        win._set_sidebar(True, page)
        TU.pump(0.25)
        hits(win.sidebar, f"侧栏·{name}（页签条也在这一棵树上）")
    dlg = None
    try:
        from app.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog(cfg, win)
        dlg.reload_from_cfg()
        dlg.resize(760, 620)
        dlg.show()
        TU.pump(0.4)
        hits(dlg, "设置页")
    finally:
        if dlg is not None:
            dlg.close()
    win.player.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
