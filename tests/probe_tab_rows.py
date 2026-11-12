"""一次性探针（可删）：4.0 档页签条为什么还会重叠——逐颗按钮摆出来的几何对账。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_tab_rows.py [scale ...]

`capture_sweep --font-scale 4.0` 报的是
`重叠：QToolButton「播放列表」 与 QToolButton「媒体库」 压在一起 23×72px @(210,8)`，
而 3.0 档（#44 那轮修好的换行）是绿的。换行这件事在 3.0 生效、4.0 失效，可能的形状有三种，
光看报错分不出来，所以这里把数摆开：
  ① 侧栏宽 / 页签条宿主宽与高（宿主没长高 ⇒ 两排挤在一排的高度里，就是重叠）；
  ② 每颗按钮的 fixedWidth、网格行/列、以及在宿主坐标里的 x/y/宽/高；
  ③ `_layout_tabs` 两遍各自返回的"这一排要多宽"（`tab_row_need`）与 wrap 有没有被走到。

（第一版探针没开侧栏就量，五颗按钮全报 (0,0)、重叠尺也报 0 条——隐藏件不参与布局，
那不是"没重叠"是"没摆过"。所以下面先 `_set_sidebar(True, 0)` 再读数。

**量出来的根因（`logs/probe-tab-rows-042303.log`，那时候还是网格）**：3.0/3.5 两档
`tab_row_need`（416~514）都 ≤ 侧栏 520，重叠尺 0 条；4.0 档报的是 416，可五颗按钮在宿主坐标里是
`播放列表 x=10..233`、`媒体库 x=210..385` ⇒ 压在一起 23×72px。差的 127px 来自**网格的列宽跨排取最大**：
第 0 排两格（223/175）、第 1 排三格（127/127/127），布局真正要的是 223+175+127＋边距 18＝543，
而"最宽那排"只算出 416。⇒ 产品侧的修法是把"每排一个独立横向盒"落地（`_place_tab_rows`），
让"最宽那排"这个数等于布局真正要的数，而不是再去给网格补一个跨排账。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtWidgets import QApplication                            # noqa: E402

import capture_sweep as CS                                            # noqa: E402
import test_ui as TU                                                  # noqa: E402
from app import config as cfgmod                                      # noqa: E402
from app.ui import theme                                              # noqa: E402

SCALES = [float(x) for x in sys.argv[1:]] or [3.0, 4.0]


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
        # 必须先把侧栏打开再量：出厂时 `sidebar.setVisible(False)`，隐藏件的布局不算几何，
        # 上一版没开就量，五颗按钮全在 (0,0)——那不是重叠，那是没摆过（探针自己骗自己）。
        win._set_sidebar(True, 0)
        TU.pump(0.3)
        host = win.tab_row_layout.parentWidget()
        v = win.tab_row_layout
        print(f"==== 字号档 {scale:g} 侧栏={win.sidebar.width()} tab_row_need={win.tab_row_need} "
              f"page_need={win.sidebar_page_need} ====")
        print(f"   页签条宿主：{host.width()}×{host.height()}  "
              f"sizeHint={host.sizeHint().width()}×{host.sizeHint().height()}  "
              f"minimum={host.minimumSizeHint().width()}×{host.minimumSizeHint().height()}  "
              f"纵向盒条目={v.count()} 排数={len(win._tab_rows)}")
        for r, box in enumerate(win._tab_rows):
            lay = box.layout()
            # 每排只算自己那几颗——这一行就是"排独立"的全部含义：上一版这里是跨排取列最大，
            # 报出来的需求比真实需求小 127px，于是"说排开了其实压在一起"。
            parts = [lay.itemAt(i).widget().width() for i in range(lay.count())
                     if lay.itemAt(i) is not None and lay.itemAt(i).widget() is not None]
            print(f"   第{r}排：盒 {box.width()}×{box.height()} 缝={lay.spacing()} "
                  f"格={parts} 排合计={sum(parts)} "
                  f"按钮={[b.text() for b in win.tab_btns if b.parentWidget() is box]}")
        for b in win.tab_btns:
            print(f"   · {b.text():<6} fixedW={b.minimumWidth()} 实宽={b.width()} "
                  f"高={b.height()} 宿主坐标 x={b.x()} y={b.y()} "
                  f"sizeHint高={b.sizeHint().height()} 父级={'排盒' if b.parentWidget() is not host else '宿主'}")
        hits = CS.colliding_text(win.sidebar)
        print(f"   重叠尺（整条侧栏）= {len(hits)} 条")
        for h in hits[:3]:
            print("     ↳ " + h)
        win.close()
        TU.pump(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
