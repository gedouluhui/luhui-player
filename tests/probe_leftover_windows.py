"""一次性探针：抓屏判据跑之前，自家进程到底有哪些顶层窗是可见的？

为什么要单独跑：capture_sweep --screen 的两条抓屏红点了两个自家句柄
（logs/capture-sweep-screen-005220.log:50-51），但遮挡闸门只给句柄和几何，
不给"那是哪个 Qt 对象"。要决定量具该收掉谁，得先按名字看清楚场上有谁，
别按猜测写白名单。

用完即可丢弃：本文件不产生判据，只打印事实。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PyQt5.QtWidgets import QApplication   # noqa: E402
import test_ui as TU                                 # noqa: E402


def dump(app, win, tag: str) -> None:
    print(f"\n---- {tag} ----")
    for w in app.topLevelWidgets():
        try:
            vis = w.isVisible()
        except BaseException as exc:            # 半个构造的对象也能在这儿崩
            print(f"  <可见性读不出 {type(w).__name__}: {exc}>")
            continue
        if not vis:
            continue
        g = w.frameGeometry()
        print(f"  {type(w).__name__:<22} win={w.isWindow()} "
              f"parent={type(w.parent()).__name__ if w.parent() else '无'} "
              f"几何={g.x()},{g.y()},{g.width()}×{g.height()} "
              f"窗口旗标=0x{int(w.windowFlags()):x}")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    cfg = TU.make_cfg()
    win = TU.build_window(cfg)
    win.add_paths(TU.FILES[:1])
    TU.wait_for(lambda: float(win.player.prop("duration", 0) or 0) > 0, 12.0)
    TU.pump(0.4)
    dump(app, win, "刚建好窗")

    win._open_keymap()
    TU.pump(0.25)
    dump(app, win, "打开快捷键页之后")

    win._open_gif()
    TU.pump(0.25)
    dump(app, win, "再打开截图工坊之后")

    for name in ("keymap_dlg", "gif_dlg"):
        d = getattr(win, name, None)
        if d is not None:
            d.hide()
    TU.pump(0.25)
    dump(app, win, "hide 这两张之后")

    win.close()
    TU.pump(0.5)
    still = [type(w).__name__ for w in app.topLevelWidgets() if w.isVisible()]
    print(f"\n---- 关窗后仍可见的顶层窗（应为空）：{still} ----")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
