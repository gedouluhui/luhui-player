"""一次性探针（可删）：动效与滚动手感的无头回归 + 3000 卡片的滚动帧成本。

跑法（离屏，不开前台窗口）：
  QT_QPA_PLATFORM=offscreen GELU_DATA=tests/.selftest-data python tests/probe_feel_perf.py

判据两部分：
  ① 手感链要真的动：一格滚轮 → 目标一段位移 → 停下时值等于目标；外部 setValue 能
     立刻夺回控制；滚动条闲置后淡出、再滚又亮；悬停按帧推进；换页纱能收干净。
  ② 帧成本：3000 张卡片的网格里，滚轮动画的每一帧（事件循环 + 视口重绘）和一次
     大跳后的首帧都不许 >50ms。50ms 是"人能看见的一次顿"的下限，不是 16.7ms 的
     理想帧预算——离屏渲染比上屏少了合成一步，这里只抓"有没有整窗级的卡"。
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt                    # noqa: E402
from PyQt5.QtGui import QImage, QMouseEvent, QWheelEvent                # noqa: E402
from PyQt5.QtWidgets import QApplication, QSlider                       # noqa: E402

import test_ui as TU                                                   # noqa: E402
from app import config as cfgmod                                       # noqa: E402
from app.ui import scroll_feel, theme                                  # noqa: E402

OK = True
N_CARDS = 3000
FRAME_BUDGET_MS = 50.0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        OK = False


def send_wheel(obj, pos, notch=120, pixel=False) -> None:
    pd = QPoint(0, notch) if pixel else QPoint(0, 0)
    ad = QPoint(0, 0) if pixel else QPoint(0, notch)
    ev = QWheelEvent(QPointF(pos), QPointF(obj.mapToGlobal(pos)), pd, ad,
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(obj, ev)


def send_mouse(obj, typ, pos, button=Qt.NoButton, buttons=Qt.NoButton) -> None:
    QApplication.sendEvent(obj, QMouseEvent(typ, QPointF(pos), button, buttons,
                                            Qt.NoModifier))


def render_ms(view, img) -> float:
    t0 = time.perf_counter()
    view.viewport().render(img)
    return (time.perf_counter() - t0) * 1000.0


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
    try:
        win.library.sig_items.disconnect()
    except (TypeError, RuntimeError):
        pass
    win._set_sidebar(True, 1)
    for _ in range(20):
        if not win.library.running():
            break
        TU.pump(0.1)
    TU.pump(0.2)

    # 3000 条：路径故意不存在——PosterStore.request 见不到文件就直接返回键，
    # 不会为探针起 3000 个 ffmpeg。
    t0 = time.perf_counter()
    panel.set_items([{"name": f"第{i}部影片.mkv", "path": f"X:/none/{i:05d}.mkv",
                      "size": 1.2e9, "dur": 5400.0, "mtime": 1700000000 + i}
                     for i in range(N_CARDS)])
    build_ms = (time.perf_counter() - t0) * 1000.0
    panel._fill_timer.stop()
    panel._fill_rows.clear()
    TU.pump(0.6)
    grid = panel.grid
    bar = grid.verticalScrollBar()
    check("3000 张卡片都进了模型", grid.count() == N_CARDS, f"count={grid.count()} build={build_ms:.0f}ms")
    check("网格有滚动量程", bar.maximum() > bar.minimum() + 2000,
          f"range={bar.minimum()}..{bar.maximum()}")
    check("大库仍开着滚动手感", panel.scroll_feel is not None)
    sf = panel.scroll_feel
    if sf is None:
        return 1

    img = QImage(max(1, grid.viewport().width()), max(1, grid.viewport().height()),
                 QImage.Format_ARGB32)

    # ① 一格滚轮 → 一段有收尾的位移
    start = bar.value()
    send_wheel(grid.viewport(), grid.viewport().rect().center(), -120)
    TU.pump(0.016)
    check("滚轮后动画在跑", sf.animating and sf.target > start,
          f"start={start} target={sf.target:.0f}")
    costs, totals = [], []
    deadline = time.perf_counter() + 3.0
    while sf.animating and time.perf_counter() < deadline:
        t0 = time.perf_counter()
        TU.pump(0.008)
        pump_ms = (time.perf_counter() - t0) * 1000.0
        cost = render_ms(grid, img)
        costs.append(cost)
        totals.append(pump_ms + cost)
    check("滚动自己会停", not sf.animating)
    check("停下时值落在目标上", abs(bar.value() - round(sf.target)) <= 1,
          f"value={bar.value()} target={sf.target:.1f}")
    moved = bar.value() - start
    check("一格滚轮滚了一段（不是一格一像素）", 40 <= moved <= 400, f"moved={moved}px")
    worst = max(totals) if totals else -1.0
    worst_render = max(costs) if costs else -1.0
    check(f"滚动每帧（事件循环+重绘）< {FRAME_BUDGET_MS:.0f}ms",
          worst >= 0 and worst < FRAME_BUDGET_MS,
          f"frames={len(costs)} worst={worst:.1f}ms render_worst={worst_render:.1f}ms")
    if costs:
        mid_render = sorted(costs)[len(costs) // 2]
        print(f"      重绘成本：中位 {mid_render:.1f}ms / 最差 {worst_render:.1f}ms；"
              f"整帧（含 8ms 事件等待）：最差 {worst:.1f}ms")

    # ② 外部改值立刻夺回控制；大跳的首帧也要在预算内
    send_wheel(grid.viewport(), grid.viewport().rect().center(), -120)
    TU.pump(0.016)
    bar.setValue(0)
    TU.pump(0.05)
    check("外部 setValue 取消动画", not sf.animating and bar.value() == 0,
          f"value={bar.value()} target={sf.target:.0f}")
    t0 = time.perf_counter()
    bar.setValue(bar.maximum())
    TU.pump(0.016)
    jump = render_ms(grid, img)
    jump_all = (time.perf_counter() - t0) * 1000.0
    check(f"跳到底首帧 < {FRAME_BUDGET_MS:.0f}ms", jump < FRAME_BUDGET_MS,
          f"render={jump:.1f}ms 整体={jump_all:.1f}ms")

    # ③ 滚动条淡出 / 再滚亮起
    bar.setValue(0)
    sf.fade.poke()
    TU.pump(0.2)
    check("刚滚过时滚动条可见", sf.fade.opacity > 0.95, f"opacity={sf.fade.opacity:.2f}")
    TU.pump(scroll_feel.FADE_IDLE_MS / 1000.0 + scroll_feel.FADE_MS / 1000.0 + 0.25)
    check("闲置后滚动条淡出", sf.fade.opacity < 0.05, f"opacity={sf.fade.opacity:.2f}")
    send_wheel(grid.viewport(), grid.viewport().rect().center(), -120)
    TU.pump(0.2)
    check("再滚又亮起来", sf.fade.opacity > 0.95, f"opacity={sf.fade.opacity:.2f}")
    sf.cancel()
    bar.setValue(0)
    TU.pump(0.1)

    # ④ 3000 项下悬停仍是按帧推进，且离开会退干净
    hv = panel.hover_feel
    idx = grid.model().index(0, 0)
    rect = grid.visualRect(idx)
    check("首屏有卡片可悬停", rect.isValid() and rect.width() > 20, f"rect={rect}")
    send_mouse(grid.viewport(), QEvent.MouseMove, rect.center())
    TU.pump(0.016)
    mid = hv.progress(0) if hv is not None else 0.0
    TU.pump(0.3)
    done = hv.progress(0) if hv is not None else 0.0
    hover_ok = hv is not None and 0.0 < mid < done and done > 0.99
    check("悬停有过渡且会收满", hover_ok, f"mid={mid:.2f} done={done:.2f}")
    send_mouse(grid.viewport(), QEvent.Leave, rect.center())
    TU.pump(0.4)
    check("离开后退回 0", hv is None or hv.progress(0) < 0.01,
          f"p={hv.progress(0) if hv is not None else 0:.2f}")

    # ⑤ 滑条上的滚轮滚页面，不改数值
    win._set_sidebar(True, 2)
    TU.pump(0.5)
    sliders = [s for s in win.tweak_panel.findChildren(QSlider) if s.isVisible()]
    check("调节页找得到可见滑条", bool(sliders), f"n={len(sliders)}")
    if sliders:
        s = sliders[0]
        before_s, before_scroll = s.value(), win.tweak_panel.scroll.verticalScrollBar().value()
        send_wheel(s, s.rect().center(), -120)
        TU.pump(0.4)
        after_scroll = win.tweak_panel.scroll.verticalScrollBar().value()
        check("滚轮悬在滑条上：滑条数值不动", s.value() == before_s,
              f"{before_s} -> {s.value()}")
        check("滚轮悬在滑条上：页面照滚", after_scroll > before_scroll,
              f"{before_scroll} -> {after_scroll}")

    # ⑥ 换页纱收干净，且收完不再压暗
    win._set_sidebar(True, 3)
    check("换页纱开跑", win.page_fade.running)
    TU.pump(0.35)
    check("换页纱收干净", not win.page_fade.running and not win.page_fade._veil.isVisible())

    win.close()
    TU.pump(0.2)
    print("RESULT:", "PASS" if OK else "FAIL")
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
