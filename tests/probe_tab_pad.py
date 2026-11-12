"""一次性探针（可删）：QSS 的 padding 到底给 QToolButton 的 sizeHint 加了几个像素。

为什么要量：`#TabBtn { padding: 4px 10px }` 下，「播放列表」的文字宽 48、sizeHint 却是 79，
多出来的 31 里有 20 是左右 padding，剩下 11 是 Qt 自己加的（按钮边距/焦点框一类）。
这 11 直接决定"侧栏多窄时页签开始放不下"，而它是**样式算出来的**，不是我能指定的。
所以这里把三档 padding 与 `min-width` 的写法各测一遍，顺便量"文字实际能用的宽"
（= 控件宽 − 样式吃掉的边距）与 sizeHint 的关系——截字尺要用哪个数，看完再定。

跑法：python tests/probe_tab_pad.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtCore import Qt                                  # noqa: E402
from PyQt5.QtGui import QFontMetrics                         # noqa: E402
from PyQt5.QtWidgets import QApplication, QToolButton        # noqa: E402

TEXTS = ("列表", "播放列表", "媒体库", "调节")


def build(app: QApplication, qss: str) -> list[tuple[str, int, int, int, int]]:
    app.setStyleSheet(qss)
    rows = []
    for t in TEXTS:
        b = QToolButton()
        b.setObjectName("TabBtn")
        b.setText(t)
        b.setFont(app.font())
        fm = QFontMetrics(b.font())
        text_w = fm.horizontalAdvance(t)
        hint = b.sizeHint().width()
        minhint = b.minimumSizeHint().width()
        # 实际给字的宽：把控件摆成 hint 宽，再看 contentsRect
        b.resize(hint, b.sizeHint().height())
        rows.append((t, text_w, hint, minhint, b.contentsRect().width()))
    return rows


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    base = ("#TabBtn { background: transparent; border: none; border-radius: 8px;"
            " color: #DDD; font-size: 12px; font-weight: 600; padding: __PAD__; }")
    for pad in (10, 8, 6, 4):
        print(f"--- padding 左右 {pad}px（4px {pad}px）")
        for t, w, hint, mh, cr in build(app, base.replace("__PAD__", f"4px {pad}px")):
            print(f"  {t:<5} 文字={w} sizeHint={hint} minimumSizeHint={mh} "
                  f"contentsRect={cr} ⇒ 样式吃掉 {hint - w}px")
    print("--- padding 4px 6px + min-width: 0px")
    qss = base.replace("__PAD__", "4px 6px") + "\n#TabBtn { min-width: 0px; }"
    for t, w, hint, mh, cr in build(app, qss):
        print(f"  {t:<5} 文字={w} sizeHint={hint} minimumSizeHint={mh} "
              f"contentsRect={cr} ⇒ 样式吃掉 {hint - w}px")
    print("--- 只有 min-width（Qt 默认 padding，不写 padding）")
    app.setStyleSheet("#TabBtn { }")
    for t, w, hint, mh, cr in build(app, "#TabBtn { }"):
        print(f"  {t:<5} 文字={w} sizeHint={hint} minimumSizeHint={mh} contentsRect={cr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
