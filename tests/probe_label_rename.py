"""一次性探针：把设置页那一项的标题从「侧栏宽度」改成「侧栏宽度下限」之后，
设置窗自身的三道形状闸还立不立得住（需求 == content_width、加宽后两把尺清场）。

只打印事实，不产生判据；用完即可丢弃。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import test_ui as TU                                 # noqa: E402
import capture_sweep as SW                           # noqa: E402
from app.ui import form_rows                         # noqa: E402
from app.ui.settings_dialog import ITEMS, SettingsDialog  # noqa: E402


def main() -> int:
    app = TU.QApplication.instance() or TU.QApplication(sys.argv[:1])
    TU.APP = app
    cfg = TU.make_cfg()
    TU.theme.install(app, str(cfg["theme_accent"]))
    win = TU.build_window(cfg)
    win.show()
    TU.pump(0.6)
    print("标题：", [i.label for i in ITEMS if i.cfg == "sidebar_width"],
          " hint：", [i.hint for i in ITEMS if i.cfg == "sidebar_width"])
    dlg = SettingsDialog(cfg, win)
    dlg.show()
    TU.pump(0.5)
    need = int(dlg.content_need)
    cw = form_rows.content_width(dlg)
    w0 = int(dlg.width())
    dlg._fit_width()
    TU.pump(0.4)
    w1 = int(dlg.width())
    red = SW.clipped_without_hint(dlg) + SW.beyond_viewport(dlg)
    print(f"content_need={need} 现算 content_width={cw} 自然宽={w0} 加宽后={w1} 红={len(red)}条 {red[:2]}")
    print("一致：", need == cw, " 够宽：", w1 >= need, " 清场：", not red)
    win.close()
    TU.pump(0.4)
    return 0 if (need == cw and w1 >= need and not red) else 1


if __name__ == "__main__":
    raise SystemExit(main())
