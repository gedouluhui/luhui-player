"""一次性探针（可删）：`_pages_need()` 在"页没被展示过"时量到的是不是同一个数。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_pages_visit.py [scale ...]

为什么问这个：`probe_page_need.py` 在 2.5 档读到"页面要 973px"，而 `probe_min_width.py`
逐页切过去再量，同一档最宽的一页只有 434px。两边差的不是字号档，是**量之前有没有把那一页
展示过**：`QStackedWidget` 里非当前页是隐藏的，隐藏widget 的 `minimumSizeHint()` 依赖布局
有没有算过、QSS 有没有 polish 过。如果"没展示过"会报出一个虚高的数，那 `_fit_sidebar` 就是
拿一个假的页面需求在加宽侧栏——用户在 1.0 档看到侧栏被顶到 270，可能根本不是内容要的。

所以每档打三遍同一组数：① 建窗后立刻（页没切过）；② 逐页切一遍再回来；③ 每页各自切过去当场量。
①和③对不上，就说明 `_pages_need` 得先"把页摆出来"才准。
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

SCALES = [float(x) for x in sys.argv[1:]] or [1.0, 2.5]


def page_name(win, i: int) -> str:
    """页签文字按**索引**问产品，不照记忆写死一个名字表——猜错会把读数挂到别的页上。"""
    try:
        return str(win.tab_defs[i][0])
    except Exception:
        return f"#{i}"


def per_page(win) -> dict:
    return {page_name(win, i): form_rows.content_width(win.sidebar_stack.widget(i))
            for i in range(win.sidebar_stack.count())}


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
        cold = per_page(win)
        print(f"  ① 建窗后立刻          {cold}  max={max(cold.values())}")
        for i in range(win.sidebar_stack.count()):
            win._set_sidebar(True, i)
            TU.pump(0.2)
        hot = per_page(win)
        print(f"  ② 全部切过一遍之后    {hot}  max={max(hot.values())}")
        seen = {}
        for i in range(win.sidebar_stack.count()):
            win._set_sidebar(True, i)
            TU.pump(0.2)
            seen[page_name(win, i)] = form_rows.content_width(win.sidebar_stack.widget(i))
        print(f"  ③ 逐页当场量（切到它）{seen}  max={max(seen.values())}")
        # 产品自己那一步用的是哪一数：把侧栏钉回量程下限，让 _fit_sidebar 重新量一遍
        win.sidebar_width = 240
        got = win._fit_sidebar()
        TU.pump(0.25)
        best = max(max(hot.values()), max(seen.values()))
        print(f"  产品读数：tab_row_need={win.tab_row_need} sidebar_page_need={win.sidebar_page_need}"
              f" → 侧栏实得 {got}   （切过之后的最大值 {best}，没切过时的最大值 {max(cold.values())}）")
        if max(cold.values()) != best:
            print("  !! ①与②③对不上：`_pages_need` 会把没展示过的页量虚，产品是在拿虚高的数加宽侧栏")
        else:
            print("  ①与②③一致：隐藏页的 minimumSizeHint 不受'切没切过'影响")
        win.close()
        TU.pump(0.2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
