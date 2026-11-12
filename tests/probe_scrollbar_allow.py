"""一次性探针：构造期那一页少算的滚动条，到底该照哪个数预留？

`form_rows.content_width` 的注释写着"宁可多算 10px 也不少算"，但 2026-10-11 实测它少算了 24px：
构造期隐藏页的 `verticalScrollBar().sizeHint().width()` 给多少、样式给多少、排过版之后真实差值
给多少，三个来源各自多大要先量清楚，才知道兜底该取哪一个（取小了照样切字）。

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

from PyQt5.QtWidgets import QApplication, QScrollArea, QStyle   # noqa: E402
import test_ui as TU                                    # noqa: E402
from app.ui import form_rows                             # noqa: E402

def page_name(win, i: int) -> str:
    """页名从 `win.tab_defs` 现取，不在模块里抄一份名单。

    抄的那份是"五页时代"的：加第六页（声音均衡）之后，这一行打印出来的数字仍然在，
    只是名字指向别的一页——"读错了页"在这份输出里没有任何症状。"""
    d = getattr(win, "tab_defs", ())
    return str(d[i][0]) if i < len(d) else str(i)


def dump(win, tag: str) -> None:
    print(f"\n---- {tag} ----")
    stack = win.sidebar_stack
    for i in range(stack.count()):
        pg = stack.widget(i)
        for sc in pg.findChildren(QScrollArea):
            v = sc.verticalScrollBar()
            print(f"  [{page_name(win, i)}] 页可见={pg.isVisible()} "
                  f"sc宽={int(sc.width())} 视口宽={int(sc.viewport().width())} "
                  f"真实差值={int(sc.width()) - int(sc.viewport().width())} "
                  f"vbar.sizeHint={int(v.sizeHint().width())} "
                  f"样式PM_ScrollBarExtent={int(sc.style().pixelMetric(QStyle.PM_ScrollBarExtent))} "
                  f"frame={int(sc.frameWidth())} body最小宽={int(sc.widget().minimumSizeHint().width()) if sc.widget() else '?'} "
                  f"content_width={form_rows.content_width(pg)}")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    TU.APP = app
    cfg = TU.make_cfg()
    win = TU.build_window(cfg)          # 构造期：_fit_sidebar 已经跑过
    dump(win, "刚建好窗（除首页外都是隐藏页）")
    win.show()
    TU.pump(0.4)
    dump(win, "主窗 show 之后（侧栏还没开）")
    win._set_sidebar(True, 2)
    TU.pump(0.6)
    dump(win, "开侧栏并切到调节页之后")
    print(f"\n  此刻 _pages_need={win._pages_need()} 侧栏={win.sidebar.width()} "
          f"sidebar_page_need={win.sidebar_page_need}")
    win.close()
    TU.pump(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
