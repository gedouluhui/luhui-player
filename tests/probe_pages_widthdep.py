"""一次性探针（可删）：`form_rows.content_width()` 的读数会不会跟着"侧栏现在多宽"变。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_pages_widthdep.py [scale ...]

为什么问这个：`_fit_sidebar` 拿 `_pages_need()` 当"要把侧栏加到多宽"的目标，而 `content_width`
量的是滚动区肚子里那块 widget 的 `minimumSizeHint().width()`——那个数是**布局按当前宽度算出来的**。
如果它随当前宽度变（窄的时候报得宽、宽的时候报得窄），那这一步就不是"加到刚好排开"，
而是"加到一个会自己挪的数上"：可能加过头（用户 240 被顶到量程上限），也可能来回抖。
`probe_page_need.py` 在 2.5 档看到"页面要 973"，而 `probe_pages_visit.py` 量同一档只有 434——
两边差的就是"量的那一刻侧栏是 240 还是 513"。所以这里把侧栏逐个钉宽、每次都逐页量一遍。
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

SCALES = [float(x) for x in sys.argv[1:]] or [1.0, 2.5, 3.0]
WIDTHS = (240, 300, 400, 513, 520)


def page_name(win, i: int) -> str:
    try:
        return str(win.tab_defs[i][0])
    except Exception:
        return f"#{i}"


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
        rows: dict[int, dict] = {}
        for w in WIDTHS:
            win.sidebar.setFixedWidth(w)       # 只钉容器宽，不走 _fit_sidebar（要看"当前宽"的影响）
            TU.pump(0.25)
            got = {page_name(win, i): form_rows.content_width(win.sidebar_stack.widget(i))
                   for i in range(win.sidebar_stack.count())}
            rows[w] = got
            print(f"  侧栏现在 {win.sidebar.width():>4} → {got}  max={max(got.values())}")
        vals = {max(v.values()) for v in rows.values()}
        print("  " + ("读数与当前宽度无关：_pages_need 可以当加宽目标"
                      if len(vals) == 1 else
                      f"!! 读数随当前宽度变（{sorted(vals)}）：拿它当加宽目标会加过头/会抖"))
        # 产品那一步在同一档上实际落到哪
        for w in (240, 300):
            win.sidebar_width = w
            got = win._fit_sidebar()
            TU.pump(0.25)
            print(f"  产品：用户 {w} → 侧栏 {got}（tab_need={win.tab_row_need} page_need={win.sidebar_page_need}）")
        win.close()
        TU.pump(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
