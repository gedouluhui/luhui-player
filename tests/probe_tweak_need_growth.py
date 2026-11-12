"""一次性探针：调节页的 content_width 是哪一刻从 378 变 402 的？

背景：`probe_shape_guards.py` 量到"构造期 `_pages_need()`=378、侧栏因此钉在 378，
但真打开过调节页之后 content_width(调节)=402"——差的那 24px 落在行尾数值胶囊上
（`clipped_without_hint` 当场点名「1.00x」需 45px 只露 40px）。
要修得先知道是谁把宽度顶上去的：设备下拉填上真名字？还是隐藏页在构造期量到的宽本就偏小
（QStackedWidget 的隐藏页没有真视口，`content_width` 只能照滚动条 sizeHint 预留）。
本文件不产生判据，只打印事实。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from PyQt5.QtWidgets import QApplication             # noqa: E402
import test_ui as TU                                   # noqa: E402
import capture_sweep as SW                             # noqa: E402
from app.ui import form_rows                            # noqa: E402


def snap(win, tag: str) -> None:
    stack = win.sidebar_stack
    tw = stack.widget(2)
    cb = win.tweak_panel.cb_device
    fm = cb.fontMetrics()
    widest = max((fm.horizontalAdvance(cb.itemText(i)) for i in range(cb.count())), default=0)
    body = None
    for sc in tw.findChildren(TU.QScrollArea):
        b = sc.widget()
        if b is not None and body is None:
            body = b
    print(f"  [{tag}] _pages_need={win._pages_need()} content_width(调节)={form_rows.content_width(tw)} "
          f"侧栏={win.sidebar.width()} 调节页可见={tw.isVisible()} "
          f"body最小宽={int(body.minimumSizeHint().width()) if body else '?'} "
          f"下拉条数={cb.count()} 最宽条目文字={widest} 下拉sizeHint={int(cb.sizeHint().width())} "
          f"下拉宽={cb.width()}")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    TU.APP = app
    cfg = TU.make_cfg()
    win = TU.build_window(cfg)
    win.add_paths(TU.FILES[:1])
    TU.wait_for(lambda: float(win.player.prop("duration", 0) or 0) > 0, 12.0)
    TU.pump(0.5)
    win.show()
    TU.pump(0.4)
    snap(win, "刚建好窗（没开侧栏）")
    win._set_sidebar(True, 0)
    TU.pump(0.4)
    snap(win, "开侧栏、停在列表页")
    win._sidebar_page(2)
    TU.pump(0.6)
    snap(win, "切到调节页（page_shown 已跑完）")
    win._fit_sidebar()
    TU.pump(0.4)
    snap(win, "手动重排之后")
    overs = []
    for sc in win.sidebar_stack.widget(2).findChildren(TU.QScrollArea):
        overs += SW.clipped_without_hint(sc.widget() or sc)[:3]
    print(f"  重排后裁而未告={overs}")
    cb = win.tweak_panel.cb_device
    print(f"  设备条目（前 3 条，长度即宽度来源）："
          f"{[(cb.itemText(i), len(cb.itemText(i))) for i in range(min(3, cb.count()))]}")
    win.close()
    TU.pump(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
