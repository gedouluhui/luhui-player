"""一次性探针（可删）：字号放大档里，调节页的内容到底被**哪一行**撑到比视口还宽。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_min_width.py [scale]

为什么问"哪一行"而不是直接把侧栏加宽：capture_sweep --font-scale 1.3 报的
「QLabel『1.00x』需 40px，只露出 37px」是"被视口右边切掉"的形状——
`QScrollArea` 装了 `setWidgetResizable(True)` + 横向滚动条关掉，于是
**内容的最小宽一旦超过视口宽，超出部分既不滚动也不压缩，直接从右边掉出去**。
要修就得让超宽的那一行自己变短（或让它能缩），加宽侧栏只是把这条口子挪到别的字号档。

所以这里逐层打三样：视口宽、内容 minimumSizeHint 宽、以及每一行控件的
minimumSizeHint/sizeHint 宽，按超出量排序——第一名就是该改的那一行。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtWidgets import (QApplication, QScrollArea, QWidget)  # noqa: E402

import capture_sweep as CS                            # noqa: E402
import test_ui as TU                                  # noqa: E402
from app import config as cfgmod                      # noqa: E402
from app.ui import theme                              # noqa: E402

SCALE = float(sys.argv[1]) if len(sys.argv) > 1 else 1.3


def txt(w: QWidget) -> str:
    """安全地拿一行字：QComboBox/QSlider 没有 text()，别拿 QLabel 的接口去问它们。"""
    for attr in ("text", "currentText"):
        f = getattr(w, attr, None)
        if callable(f):
            try:
                s = str(f())
            except Exception:
                continue
            if s:
                return s
    return ""


def demand(w: QWidget) -> str:
    """一行控件的"最少要多宽"和"想要多宽"，加上它自己是谁。"""
    return (f"{type(w).__name__}「{txt(w)[:18] or w.objectName()}」"
            f" min={w.minimumSizeHint().width()} want={w.sizeHint().width()} 实={w.width()}")


def main() -> int:
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    app.setStyle("Fusion")
    CS._scale_qss(SCALE)
    cfg = cfgmod.Config()
    theme.install(app, str(cfg["theme_accent"]))
    print(f"字号档 = {SCALE:g}")
    win = TU.build_window(cfg)
    TU.pump(0.3)
    from app.ui import form_rows
    print("每页「排得开至少要多宽」（form_rows.content_width）：")
    # 名单按栈取：抄一份"五页"的固定名单，第 6 页（声音均衡）会被读成前一页的名字，
    # 而打印出来的那一行照样有数字——"漏了一页"在这份输出里没有任何症状。
    for i, name in enumerate(str(d[0]) for d in win.tab_defs):
        win._set_sidebar(True, i)
        TU.pump(0.25)
        pg = win.sidebar_stack.widget(i)
        print(f"   {name}: 要 {form_rows.content_width(pg)}px（侧栏现在 {win.sidebar.width()}）")
    win._set_sidebar(True, 2)
    TU.pump(0.3)
    tp = win.tweak_panel
    scroll = tp.scroll
    body = tp._body
    vp = scroll.viewport().width()
    print(f"视口宽={vp}  内容 min 需求={body.minimumSizeHint().width()} "
          f"内容实宽={body.width()}  侧栏宽={win.sidebar.width()}")
    rows = [w for w in body.findChildren(QWidget) if w.parentWidget() is body]
    rows = sorted(rows, key=lambda w: -w.minimumSizeHint().width())
    for r in rows[:8]:
        print("  · " + demand(r))
    print("  —— 按 body 的**布局树**逐格问最小宽（子控件各自报的宽度加起来"
          "不等于布局要的宽，所以要问布局本身）：")

    def walk(lay, depth=0):
        if lay is None:
            return
        for i in range(lay.count()):
            it = lay.itemAt(i)
            if it is None:
                continue
            pad = "  " * (depth + 2)
            if it.layout() is not None:
                sub = it.layout()
                parts = [sub.itemAt(j).minimumSize().width() for j in range(sub.count())
                         if sub.itemAt(j) is not None]
                print(f"{pad}L {type(sub).__name__} min={sub.minimumSize().width()} "
                      f"格={parts} 缝={sub.spacing()} 边={sub.getContentsMargins()}")
                if sub.minimumSize().width() >= 250:
                    walk(sub, depth + 1)
            elif it.widget() is not None:
                w = it.widget()
                print(f"{pad}W {demand(w)} minSize={w.minimumSize().width()} "
                      f"minHint={w.minimumSizeHint().width()}")
            else:
                print(f"{pad}spacer min={it.minimumSize().width()}")

    walk(body.layout())
    print(f"  body.minimumSize()={body.minimumSize().width()} "
          f"minimumSizeHint()={body.minimumSizeHint().width()}")
    print("\n=== 画面页（transform_panel，同一套 form_rows） ===")
    win._set_sidebar(True, 3)
    TU.pump(0.3)
    xf = win.transform_panel
    print(f"视口宽={xf.scroll.viewport().width()} 内容 min 需求={xf._body.minimumSizeHint().width()}")
    for r in sorted([w for w in xf._body.findChildren(QWidget) if w.parentWidget() is xf._body],
                    key=lambda w: -w.minimumSizeHint().width())[:5]:
        print("  · " + demand(r))

    print("\n=== 设置页（同一个滚动区形状，问的是「窗口要多宽才不切字」） ===")
    from app.ui.settings_dialog import SettingsDialog
    dlg = SettingsDialog(cfg, win)
    dlg.reload_from_cfg()
    dlg.resize(760, 620)
    dlg.show()
    TU.pump(0.4)
    dsc = [s for s in dlg.findChildren(QScrollArea) if s.widget() is not None]
    for s in dsc:
        b = s.widget()
        print(f"滚动区宽={s.width()} 视口={s.viewport().width()} 内容 min={b.minimumSizeHint().width()}")
        lay = b.layout()
        worst = sorted([lay.itemAt(i) for i in range(lay.count())
                        if lay.itemAt(i) is not None and lay.itemAt(i).widget() is not None],
                       key=lambda it: -it.widget().minimumSizeHint().width())
        for it in worst[:5]:
            w = it.widget()
            inner = w.layout()
            parts = ([inner.itemAt(j).minimumSize().width() for j in range(inner.count())
                      if inner.itemAt(j) is not None] if inner is not None else [])
            print(f"  · {demand(w)} 格={parts}")
    dlg.close()
    win.player.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
