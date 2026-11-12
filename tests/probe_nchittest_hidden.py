"""不显示任何窗口，直接对真实 HWND 发 WM_NCHITTEST，并验证"按住标题栏拖动"的接线。

窗口只建 HWND、不 show，坐标不出屏；用户屏幕不会出现任何东西、也不碰真实鼠标。
判据（R24 起）：
  - 标题栏/品牌/窗钮/视频区一律 HTCLIENT（标题栏要 HTCLIENT，Qt 才收得到鼠标事件）；
  - 左/顶边 = 缩放边（缩放仍走 DefWindowProc）；
  - 按住标题栏移动越过系统阈值 → 调 begin_system_move(本窗 HWND)；只按下不移动 → 不调；
  - 双击标题栏 → 调最大化槽。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", tempfile.mkdtemp(prefix="gelu-nchit-"))
os.environ.setdefault("GELU_NO_MOTION", "1")

from PyQt5.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

user32 = ctypes.windll.user32

WM_NCHITTEST = 0x0084
HTCLIENT, HTLEFT, HTTOP = 1, 10, 12

user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]  # 与 win32.py 一致：两种 RECT 都收

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def make_lp(x: int, y: int) -> int:
    return (int(y) & 0xFFFF) << 16 | (int(x) & 0xFFFF)


def hit(hwnd, x, y) -> int:
    return int(user32.SendMessageW(hwnd, WM_NCHITTEST, 0, make_lp(x, y)))


def mouse(kind, local, screen, button, buttons) -> QMouseEvent:
    return QMouseEvent(kind, QPointF(*local), QPointF(*screen), button, buttons, Qt.NoModifier)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    cfg = cfgmod.Config()
    theme.install(app, str(cfg["theme_accent"]))
    app.setStyleSheet(theme.qss())
    win = MainWindow(cfg)
    win.resize(1200, 760)
    hwnd = int(win.winId())          # 只建 HWND，不 show：窗口始终不可见
    win.layout().activate()
    win.title.layout().activate()
    # 本探针的窗口没 show 过，Qt 的 DPR 还是 1.0，而 GetDpiForWindow 已按 150% 报 144；
    # 真实显示后两者一致。把 screen_dpi 钉回 96，模拟"DPR 与窗口所在屏一致"的正式条件。
    from app import win32 as _w32
    _w32.screen_dpi = lambda _h: 96
    win.chrome.install()
    win.chrome.last_error = None

    check("窗口未显示（用户屏幕无窗口）", not win.isVisible())
    check("拖动区有几何", win.title.drag_region().width() > 100 and win.title.drag_region().height() > 20,
          f"{win.title.drag_region().width()}x{win.title.drag_region().height()}")

    # 拖动区中心的控件必须在 TitleBar 子树里：真实点击到这里，鼠标事件才会浮到标题栏处理器
    from app.ui.title_bar import TitleBar
    dchk = win.title.drag_region()
    lp_center = dchk.mapTo(win, dchk.rect().center())
    wchk = win.childAt(lp_center)
    chain: list[str] = []
    in_title = False
    while wchk is not None:
        chain.append(wchk.objectName() or type(wchk).__name__)
        if isinstance(wchk, TitleBar):
            in_title = True
            break
        wchk = wchk.parentWidget()
    check("拖动区中心的控件在 TitleBar 子树里（真实点击会到标题栏）", in_title, " → ".join(chain))

    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    check("窗口矩形可用", w > 600 and h > 400, f"{w}x{h} at ({r.left},{r.top})")

    # 拖动区中心（Qt 逻辑坐标 → 屏幕物理坐标）
    d = win.title.drag_region()
    c = d.mapToGlobal(d.rect().center())
    dpr = win.devicePixelRatioF() or 1.0
    sx, sy = int(c.x() * dpr), int(c.y() * dpr)
    ht_title = hit(hwnd, sx, sy)
    check("标题拖动区 = HTCLIENT（客户区，Qt 才收得到鼠标事件）", ht_title == HTCLIENT, f"got={ht_title}")

    # 品牌字（旧版不在拖动区里）现在也该走客户区
    brand = win.title.findChild(type(win.title.lbl_doc), "BrandTitle")
    bp = brand.mapToGlobal(brand.rect().center())
    ht_brand = hit(hwnd, int(bp.x() * dpr), int(bp.y() * dpr))
    check("品牌字 = HTCLIENT", ht_brand == HTCLIENT, f"got={ht_brand}")

    # 窗钮必须仍是 HTCLIENT
    from PyQt5.QtWidgets import QToolButton
    close_btn = next(b for b in win.title.findChildren(QToolButton)
                     if "CloseBtn" in b.objectName())
    cp = close_btn.mapToGlobal(close_btn.rect().center())
    ht_close = hit(hwnd, int(cp.x() * dpr), int(cp.y() * dpr))
    check("关闭钮 = HTCLIENT", ht_close == HTCLIENT, f"got={ht_close}")

    # 视频区中心
    v = win.video
    pos = v.mapToGlobal(v.rect().center())
    ht_body = hit(hwnd, int(pos.x() * dpr), int(pos.y() * dpr))
    check("视频区 = HTCLIENT", ht_body == HTCLIENT, f"got={ht_body}")

    mid_y = r.top + h // 2
    ht_left = hit(hwnd, r.left + 1, mid_y)
    check("左边缘 = HTLEFT", ht_left == HTLEFT, f"got={ht_left}")
    ht_top = hit(hwnd, (r.left + r.right) // 2, r.top + 1)
    check("顶边 = HTTOP", ht_top == HTTOP, f"got={ht_top}")

    # ---- 拖动接线（直接喂鼠标事件给 TitleBar，不碰真实鼠标）----
    title = win.title
    calls: list[int] = []
    real_move = _w32.begin_system_move
    _w32.begin_system_move = lambda h: (calls.append(int(h)), True)[1]
    try:
        # 0) 投递链路：把按下事件发给光标下**真实子控件**（DragRegion），子控件不认鼠标事件
        #    会一路上浮到 TitleBar；据此确认正式运行时点了标题栏就一定能武装拖动。
        drag_w = title.drag_region()
        dc = drag_w.rect().center()
        dg = drag_w.mapToGlobal(dc)
        QApplication.sendEvent(drag_w, mouse(QEvent.MouseButtonPress, (dc.x(), dc.y()),
                                             (dg.x(), dg.y()), Qt.LeftButton, Qt.LeftButton))
        armed = title._drag_armed
        step = QApplication.startDragDistance() + 50
        QApplication.sendEvent(drag_w, mouse(QEvent.MouseMove, (dc.x() + step, dc.y()),
                                             (dg.x() + step, dg.y()), Qt.NoButton, Qt.LeftButton))
        ok_chain = armed and calls == [hwnd]
        seen_calls = list(calls)
        QApplication.sendEvent(drag_w, mouse(QEvent.MouseButtonRelease, (dc.x() + step, dc.y()),
                                             (dg.x() + step, dg.y()), Qt.LeftButton, Qt.NoButton))
        calls.clear()
        check("DragRegion 上的按下/拖动上浮到 TitleBar 并进系统移动", ok_chain,
              f"armed={armed} calls={seen_calls}")

        # 只按下 + 微动（< 系统拖动阈值）：不许启动拖动，否则双击最大化永远等不到第二下。
        title.mousePressEvent(mouse(QEvent.MouseButtonPress, (5, 5), (500, 500), Qt.LeftButton, Qt.LeftButton))
        title.mouseMoveEvent(mouse(QEvent.MouseMove, (8, 5), (503, 500), Qt.NoButton, Qt.LeftButton))
        title.mouseReleaseEvent(mouse(QEvent.MouseButtonRelease, (8, 5), (503, 500), Qt.LeftButton, Qt.NoButton))
        check("只按下/微动（< 阈值）不启动拖动（留给双击）", calls == [], f"{calls}")

        # 按下 + 越过阈值：应调一次 begin_system_move(本窗 HWND)。
        title.mousePressEvent(mouse(QEvent.MouseButtonPress, (5, 5), (500, 500), Qt.LeftButton, Qt.LeftButton))
        title.mouseMoveEvent(mouse(QEvent.MouseMove, (40, 5), (540, 500), Qt.NoButton, Qt.LeftButton))
        title.mouseReleaseEvent(mouse(QEvent.MouseButtonRelease, (40, 5), (540, 500), Qt.LeftButton, Qt.NoButton))
        check("按住标题栏越过阈值 → begin_system_move(本窗 HWND)", calls == [hwnd], f"{calls} vs hwnd={hwnd}")
    finally:
        _w32.begin_system_move = real_move

    # 双击 → 最大化槽
    rec: list[str] = []
    real_toggle = title._on_max
    title._on_max = lambda: rec.append("max")
    try:
        title.mouseDoubleClickEvent(mouse(QEvent.MouseButtonDblClick, (10, 10), (505, 500),
                                          Qt.LeftButton, Qt.LeftButton))
        check("双击标题栏 → 调最大化槽", rec == ["max"], f"{rec}")
    finally:
        title._on_max = real_toggle
    check("标题栏双击槽就是主窗的 _toggle_max", title._on_max == win._toggle_max,
          f"{title._on_max}")

    # begin_system_move 用的命令常量取 Qt startSystemMove 的口径：WM_SYSCOMMAND + SC_DRAGMOVE(0xF012)。
    check("SC_DRAGMOVE=0xF012 / WM_SYSCOMMAND=0x0112（照 Qt startSystemMove）",
          _w32.SC_DRAGMOVE == 0xF012 and _w32.WM_SYSCOMMAND == 0x0112,
          f"{hex(_w32.SC_DRAGMOVE)} {hex(_w32.WM_SYSCOMMAND)}")

    print(f"[INFO] chrome.last_error={win.chrome.last_error!r} seen={win.chrome.seen}", flush=True)

    win.chrome.uninstall()
    win.deleteLater()
    app.processEvents()

    bad = [x for x in results if not x[1]]
    print(f"\n---- HWND 命中 + 拖动接线：{len(results) - len(bad)}/{len(results)} 通过 ----")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
