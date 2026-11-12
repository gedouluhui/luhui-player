"""一次性探针：U52 的尺那条腿——把调节页字号放大之后，量程下沿 240 那一档咬不咬得住？

上一轮（`tests/logs/test-ui-falsify-20261011-014254.log`）读到的形状：
    真实需求=270 内容块最小宽=238 摘闸侧栏=240 红=0 条
钉到"内容块最小宽 − 60"（178）那一档，摘闸后侧栏仍停在 **229**（页签单排要 229，
`need = max(tabs_need, page_need)` 把它抬上去了），视口只剩 30px 的缺口，
而长行里的标签带省略号——缺口先被"字变短"吸收，裁而未告/越界两把尺一条都不点名。

所以这一轮量的是"造一个真装不下的形状"：调节页字号 ×1.5（R5 那一族本来就是这件事），
看①需求还在 520 上限以内吗（对照组要成立，装着闸的那一侧必须清场）；
②摘闸后两把尺点不点名。只打印事实，不产生判据；用完即可丢弃。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from PyQt5.QtGui import QFont                        # noqa: E402
from PyQt5.QtWidgets import (QComboBox, QLabel, QScrollArea, QToolButton)  # noqa: E402
import test_ui as TU                                 # noqa: E402
import capture_sweep as SW                           # noqa: E402
from app.ui import form_rows                         # noqa: E402
from app.ui.settings_dialog import INT_RANGE         # noqa: E402


def _kids(page):
    return page.findChildren(QLabel) + page.findChildren(QComboBox) + page.findChildren(QToolButton)


def report(app, win, page, tag):
    body = sc = None
    for s in page.findChildren(QScrollArea):
        if s.widget() is not None:
            body, sc = s.widget(), s
            break
    cw = form_rows.content_width(page)
    need = win._pages_need()
    side = int(win.sidebar.width())
    vp = int(sc.viewport().width()) if sc is not None else 0
    c = SW.clipped_without_hint(page)
    o = SW.beyond_viewport(page)
    q = SW.squeezed_widgets(page)
    print(f"{tag}: 内容块={int(body.minimumSizeHint().width())} 闸读={cw} 需求={need} "
          f"侧栏={side} 视口={vp} 裁而未告={len(c)} 越界={len(o)} 压扁={len(q)}")
    for line in (c + o)[:3]:
        print(f"    · {line}")


def main() -> int:
    app = TU.QApplication.instance() or TU.QApplication(sys.argv[:1])
    app.setStyleSheet(TU.theme.qss())
    cfg = TU.make_cfg()
    win = TU.build_window(cfg)
    win.show()
    TU.pump(0.6)
    win.sidebar.setVisible(True)
    TU.pump(0.3)
    page = win.sidebar_stack.widget(2)
    win._sidebar_page(2)
    TU.pump(0.5)

    orig = form_rows.content_width
    naive = lambda p: int(p.minimumSizeHint().width())    # noqa: E731
    back_w = int(win.sidebar_width)
    kids = _kids(page)
    back_font = {k: k.font() for k in kids}
    for scale in (1.0, 1.5, 1.8):
        for k in kids:
            f = QFont(back_font[k])
            px = f.pixelSize() if f.pixelSize() > 0 else max(1, int(f.pointSize() * 96.0 / 72.0))
            f.setPixelSize(int(px * scale))
            k.setFont(f)
        TU.pump(0.4)
        win.sidebar_width = INT_RANGE["sidebar_width"][0]
        for tag, fn in (("装闸", orig), ("摘闸", naive)):
            form_rows.content_width = fn
            w = int(win._fit_sidebar())
            TU.pump(0.35)
            cap = INT_RANGE["sidebar_width"][1]
            report(app, win, page, f"字号×{scale} {tag} 钉{INT_RANGE['sidebar_width'][0]}（侧栏={w}，"
                                   f"{'顶到上限' if w >= cap else '未到上限'}）")
        form_rows.content_width = orig
    for k, f in back_font.items():
        k.setFont(f)
    win.sidebar_width = back_w
    win._fit_sidebar()
    TU.pump(0.4)
    report(app, win, page, "复原")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
