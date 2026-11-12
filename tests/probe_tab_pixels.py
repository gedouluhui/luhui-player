"""一次性探针（可删）：把侧栏页签在**最窄一档**（设置页下限 240）真画出来看。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_tab_pixels.py

为什么几何全绿还要再画一次：`tabs_fit` 量的是"每格 ≥ 样式要的宽"，这是**必要条件**不是充分证据——
缝从 4px 收到 2px 之后五个页签会不会挤成一坨、选中那一格的底色会不会压住字，只有像素知道。
离屏 `render_to()` 那半不看屏幕，所以显示器休眠也照样出图。

两处自己踩过的坑，写在原地免得下一次又拿探针的失真当产品缺陷：
1. 必须先把主题 QSS 装到 QApplication 上再建窗口（和 `app/main.py` 同一个顺序）。
   少了这一步，`_layout_tabs()` 里的 `sizeHint()` 读的是 Fusion 默认字号，
   量出来的"这一排需要"会比真机大出一截——第一版就报了个 393 的假数字。
2. 要看的是**这次 `_layout_tabs(side_w)` 的返回值**，不是 `win.tab_row_need`：
   那个属性只在窗口 resize 和设置页"应用"里更新，探针直接 setFixedWidth 不触发它，
   读它等于把上一档的结论贴到这一档上。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtWidgets import QApplication                     # noqa: E402

import capture_sweep as CS                                   # noqa: E402
import test_ui as TU                                         # noqa: E402
from app import config as cfgmod                             # noqa: E402
from app.ui import theme                                     # noqa: E402
from app.ui.main_window import MainWindow                    # noqa: E402

OUT = os.path.join(TU.OUT, "sweep")


def main() -> int:
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    app.setStyle("Fusion")
    cfg = cfgmod.Config()
    theme.install(app, str(cfg["theme_accent"]))     # 见文件头第 1 条
    win = MainWindow(cfg, files=[])
    win.move(60, 60)
    win.resize(1180, 720)
    win.show()
    TU.pump(0.6)
    win._set_sidebar(True, 0)
    TU.pump(0.3)
    os.makedirs(OUT, exist_ok=True)
    for side_w in (240, 260, 300, 520):
        need = win._layout_tabs(side_w)
        win.sidebar.setFixedWidth(side_w)
        TU.pump(0.25)
        geo = []
        for b, (label, _tip) in zip(win.tab_btns, win.tab_defs):
            fm = b.fontMetrics().horizontalAdvance(label)
            geo.append(f"{label}:{b.width()}/字{fm}/需{b.sizeHint().width()}")
        img = CS.render_to(win.sidebar, "#17181c")
        path = os.path.join(OUT, f"sidebar-tabs-{side_w}.png")
        img.save(path)
        ink, detail = TU.has_ink(img)
        # 四把尺单独对**整条页签条**再走一遍：整屏那轮把它们用在所有页上，
        # 这里只看页签条，避免别的页的问题把这一排的结论糊掉。
        # 根取 `tab_strip` 而不是 `tab_btns[0].parentWidget()`：换成"每排一个独立盒"之后后者
        # 只是**第一排**，宽字号档下面那两三排根本没人量（量具自己把盲区留掉了）。
        band = CS.squeezed_widgets(win.tab_strip)
        cuts = CS.clipped_labels(win.tab_strip)
        cols = CS.colliding_text(win.tab_strip)
        print(f"侧栏 {side_w}：缝={win.tab_row_spacing} 这一排需要={need} "
              f"出图={img.width()}×{img.height()} 有墨={ink} 存={path}  ({detail})")
        print("     " + " ".join(geo))
        print(f"     排数={len(win._tab_rows)} 压扁={band or '无'} 截字={cuts or '无'} 叠字={cols or '无'}")
    win.player.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
