"""一次性探针（可删）：`_fit_sidebar` 的"页面需求"那半边到底在哪一档是**活的**。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_page_need.py [scale ...]

为什么单独问这个：`_fit_sidebar` 取的是 `max(页签那一排要宽, 五页内容要宽)`，而 U49/U50/U51
三条注入全都只夹得住页签那半边（每档 page_need 都比 tab_need 小 ⇒ 把 `_pages_need` 摘掉也照样绿）。
要给它补一条判据，得先知道**有没有哪一档 page_need 真的胜出**——没有的话那条判据就是恒真，
写了等于没写。所以这里逐档打四个数：
      用户设的宽度 / 页签那排要宽 / 页面最宽那页要宽 / 侧栏实际宽
外加"摘掉 `_pages_need` 之后侧栏落在多少"——只有摘掉后侧栏 < 页面需求，判据才有牙。
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
from app.ui import form_rows, theme                          # noqa: E402

SCALES = [float(x) for x in sys.argv[1:]] or [1.0, 1.3, 1.8, 2.5]


def one(win, user_w: int) -> str:
    """一个 (字号档, 用户宽度) 组合：产品排一次，再把 `_pages_need` 摘掉排一次。"""
    win.sidebar_width = user_w
    got = win._fit_sidebar()
    TU.pump(0.25)
    tabs, pages = win.tab_row_need, win.sidebar_page_need
    side = win.sidebar.width()
    orig = win._pages_need
    try:
        win._pages_need = lambda: 0                    # 注入：把"页面需求"那半边摘掉
        win.sidebar.setFixedWidth(user_w)              # 退回"没加宽"的起点，让摘掉的后果看得见
        got2 = win._fit_sidebar()
        TU.pump(0.25)
        side2 = win.sidebar.width()
    finally:
        win._pages_need = orig
        win.sidebar_width = user_w
        win._fit_sidebar()
    return (f"用户{user_w} → 页签要{tabs} 页面要{pages} 侧栏实得{side}"
            f"   摘掉页面那半边后侧栏={side2}"
            f"  {'判据有牙' if side2 < pages <= 520 else '这档摘不摘都一样'}")


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
        print(f"==== 字号档 {scale:g} ====")
        for user_w in (240, 300, 380):
            print("  " + one(win, user_w))
        win.close()
        TU.pump(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
