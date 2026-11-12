"""一次性探针（可删）：悬停/按下过渡与换页淡入是否真的接到了绘制上。

跑法（离屏，不碰前台屏幕）：
  QT_QPA_PLATFORM=offscreen GELU_DATA=tests/.selftest-data python tests/probe_motion.py
  QT_QPA_PLATFORM=offscreen GELU_DATA=tests/.selftest-data python tests/probe_motion.py --reduced

问三件事：
  ① 委托画出来的底色是不是**跟着动画进度**走（不是 Qt 的即时 MouseOver）——
     合成 MouseMove 之后立刻抓图，应当还是原底色；等收满再抓，才是 BG_HOVER。
  ② 按下是否即时给确认色，松手是否立刻回来。
  ③ 换页纱是否只在换页时出现、170ms 内收干净、并且不吃鼠标。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

REDUCED = "--reduced" in sys.argv
if REDUCED:
    os.environ["GELU_NO_MOTION"] = "1"

from PyQt5.QtCore import QEvent, QPointF, Qt                    # noqa: E402
from PyQt5.QtGui import QColor, QMouseEvent                    # noqa: E402
from PyQt5.QtWidgets import QApplication, QStyleOptionViewItem   # noqa: E402

import test_ui as TU                                           # noqa: E402
from app import config as cfgmod                               # noqa: E402
from app.ui import motion, theme                               # noqa: E402

OK = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        OK = False


def send(obj, typ, pos, button=Qt.NoButton, buttons=Qt.NoButton) -> None:
    ev = QMouseEvent(typ, QPointF(pos), button, buttons, Qt.NoModifier)
    QApplication.sendEvent(obj, ev)


def sample(view, x: float, y: float) -> QColor:
    pm = view.viewport().grab()
    dpr = pm.devicePixelRatio() or 1.0
    return pm.toImage().pixelColor(round(x * dpr), round(y * dpr))


def close_enough(c: QColor, hex_color: str, tol: int = 6) -> bool:
    b = QColor(hex_color)
    return (abs(c.red() - b.red()) <= tol and abs(c.green() - b.green()) <= tol
            and abs(c.blue() - b.blue()) <= tol)


def main() -> int:
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    app.setStyle("Fusion")
    cfg = cfgmod.Config()
    theme.install(app, str(cfg["theme_accent"]))
    app.setStyleSheet(theme.qss())
    win = TU.build_window(cfg)
    TU.pump(0.3)
    panel = win.lib_panel
    # 探针只测悬停这条链：让后台扫描回包别再整表重建（它会在过渡中途正确地清掉
    # 进度，但那是扫描与重建自己的验收，不是这里要量的东西）。
    try:
        win.library.sig_items.disconnect()
    except (TypeError, RuntimeError):
        pass
    win._set_sidebar(True, 1)
    # 媒体库自己的扫描回包会在 ~1s 内整表重建一次；等它落地再铺固定样本。否则重建会
    # （正确地）清掉正在过渡的悬停进度，读数就成了"被扫描打断"，不是动效本身的事。
    for _ in range(40):
        if not win.library.running():
            break
        TU.pump(0.1)
    TU.pump(0.2)
    panel.set_items([{"name": f"片{i}.mkv", "path": f"X:/x/片{i}.mkv",
                      "size": 1e9, "dur": 120.0, "mtime": 1000 + i} for i in range(10)])
    TU.pump(0.4)

    grid = panel.grid
    check("建窗时不跑换页淡入", not win.page_fade.running)
    check("换页纱不吃鼠标",
          win.page_fade._veil.testAttribute(Qt.WA_TransparentForMouseEvents))

    if REDUCED:
        check("减少动画：滚动手感也不装",
              panel.scroll_feel is None and win.panel.scroll_feel is None)
        check("减少动画：网格不装悬停", panel.hover_feel is None)
        check("减少动画：列表不装悬停", win.panel.hover_feel is None)
        win._set_sidebar(True, 2)
        check("减少动画：换页不淡入、不露纱",
              not win.page_fade.running and not win.page_fade._veil.isVisible())
        win.close()
        TU.pump(0.2)
        return 0 if OK else 1

    hv = panel.hover_feel
    check("媒体库网格装了滚动手感", panel.scroll_feel is not None)
    check("播放列表装了滚动手感", win.panel.scroll_feel is not None)
    check("网格装了悬停动画", hv is not None)
    check("列表装了悬停动画", win.panel.hover_feel is not None)
    if hv is None:
        return 1

    idx = grid.model().index(0, 0)
    rect = grid.visualRect(idx)
    check("第一张卡有几何", rect.isValid() and rect.width() > 20, f"rect={rect}")
    px, py = rect.center().x(), rect.top() + 3
    idle = sample(grid, px, py)
    check("未悬停时不是悬停底色", not close_enough(idle, theme.BG_HOVER), f"idle={idle.name()}")

    send(grid.viewport(), QEvent.MouseMove, rect.center())
    check("MouseMove 后进度从 0 起步", hv.progress(0) < 0.05, f"p={hv.progress(0):.3f}")
    just = sample(grid, px, py)
    check("起始帧仍是原底色（不是 Qt 的即时悬停）",
          close_enough(just, idle.name()), f"just={just.name()}")

    TU.pump(0.05)
    mid = hv.progress(0)
    check("过渡中途有中间值", 0.05 < mid < 0.99, f"p={mid:.3f}")

    TU.pump(0.35)
    check("约 110ms 后收满", hv.progress(0) > 0.99, f"p={hv.progress(0):.3f}")
    hot = sample(grid, px, py)
    check("收满后就是 BG_HOVER", close_enough(hot, theme.BG_HOVER), f"hot={hot.name()}")

    opt = QStyleOptionViewItem()
    opt.widget = grid
    fade, pressed = motion.row_state(opt, idx)
    check("row_state 从视图上读到同一进度",
          abs(fade - hv.progress(0)) < 1e-9 and not pressed, f"fade={fade:.3f}")

    send(grid.viewport(), QEvent.MouseButtonPress, rect.center(),
         Qt.LeftButton, Qt.LeftButton)
    check("按下标记落在这一行", hv.is_pressed(0))
    press = sample(grid, px, py)
    check("按下即时给选中色", close_enough(press, theme.BG_ACTIVE), f"press={press.name()}")

    send(grid.viewport(), QEvent.MouseButtonRelease, rect.center(),
         Qt.LeftButton, Qt.NoButton)
    check("松手立刻撤销按下", not hv.is_pressed(0))
    grid.clearSelection()          # 点击会选中这一行；清掉才看得到"只剩悬停"的样子
    TU.pump(0.05)
    back = sample(grid, px, py)
    check("松手清选后回到悬停底色", close_enough(back, theme.BG_HOVER), f"back={back.name()}")

    send(grid.viewport(), QEvent.Leave, rect.center())
    TU.pump(0.45)
    check("离开后进度退回 0", hv.progress(0) < 0.01, f"p={hv.progress(0):.3f}")
    gone = sample(grid, px, py)
    check("退回后回到原底色", close_enough(gone, idle.name()), f"gone={gone.name()}")

    # 换页淡入：起点、收尾、以及收尾后不再压暗内容
    win._set_sidebar(True, 2)
    check("换页纱开始跑", win.page_fade.running)
    check("纱已显示在栈上", win.page_fade._veil.isVisible())
    check("首帧浓度 = PAGE_VEIL",
          abs(win.page_fade._veil._level - motion.PAGE_VEIL) < 1e-6,
          f"level={win.page_fade._veil._level:.2f}")
    TU.pump(0.05)
    lv = win.page_fade._veil._level
    check("中途在变淡", 0.0 < lv < motion.PAGE_VEIL, f"level={lv:.2f}")
    TU.pump(0.3)
    check("170ms 内收干净",
          not win.page_fade.running and not win.page_fade._veil.isVisible(),
          f"running={win.page_fade.running} visible={win.page_fade._veil.isVisible()}")

    win.close()
    TU.pump(0.2)
    print("RESULT:", "PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
