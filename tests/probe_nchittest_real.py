"""真实 DPI 下的命中测试 + 拖动接线：窗口显示在屏幕外、不抢焦点、不碰真实鼠标。

隐藏窗口的 Qt DPR=1.0，而 GetDpiForWindow 按所在屏报 144，这个组合在正式运行时不存在
（显示后两者一致）。本探针把窗口真正 show 出来、但摆到屏幕左侧之外并禁止激活：
消息路径、DPR、窗口矩形全部是真的，用户屏幕上仍无窗口，也不接管真实鼠标。

判据（R24 起）：标题栏/品牌/窗钮/视频 = HTCLIENT（标题栏要客户区，Qt 才收得到鼠标事件）；
左/顶边 = 缩放边；按住标题栏越过阈值 → begin_system_move(本窗 HWND)；双击 → 最大化槽。
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
os.environ.setdefault("GELU_DATA", tempfile.mkdtemp(prefix="gelu-nchit-real-"))
os.environ.setdefault("GELU_NO_MOTION", "1")
os.environ.pop("QT_QPA_PLATFORM", None)          # 要真实 windows 平台，不能用 offscreen

from PyQt5.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.main import _enable_high_dpi  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

user32 = ctypes.windll.user32
WM_NCHITTEST = 0x0084
HTCLIENT, HTLEFT, HTTOP = 1, 10, 12

user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
user32.GetDpiForWindow.restype = wt.UINT


def hit(hwnd, phys_x, phys_y) -> int:
    lp = (int(phys_y) & 0xFFFF) << 16 | (int(phys_x) & 0xFFFF)
    return int(user32.SendMessageW(hwnd, WM_NCHITTEST, 0, lp))


def mouse(kind, local, screen, button, buttons) -> QMouseEvent:
    return QMouseEvent(kind, QPointF(*local), QPointF(*screen), button, buttons, Qt.NoModifier)


def main() -> int:
    _enable_high_dpi()               # 与 app/main.py 的启动顺序一致，否则 DPR 会停在 1.0
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    cfg = cfgmod.Config()
    theme.install(app, str(cfg["theme_accent"]))
    app.setStyleSheet(theme.qss())
    win = MainWindow(cfg)
    win.setWindowFlag(Qt.WindowDoesNotAcceptFocus, True)
    win.setAttribute(Qt.WA_ShowWithoutActivating, True)
    # 屏幕左侧之外：DWM 照常合成、消息链路全真，但用户屏幕上看不到。
    win.setGeometry(-4000, 160, 1200, 760)
    win.show()
    app.processEvents()
    hwnd = int(win.winId())
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    off = r.right < 0 or r.left > 10000
    dpr = win.devicePixelRatioF() or 1.0
    dpi = int(user32.GetDpiForWindow(hwnd))
    print(f"[DBG] rect=({r.left},{r.top},{r.right},{r.bottom}) dpr={dpr} dpi={dpi} "
          f"visible={win.isVisible()} active={win.isActiveWindow()}", flush=True)

    def phys(p: QPoint) -> tuple[int, int]:
        return int(p.x() * dpr), int(p.y() * dpr)

    fails = []
    total = [0]

    def check(name, ok, detail=""):
        total[0] += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
        if not ok:
            fails.append(name)

    check("窗口在屏幕外且未激活", off and not win.isActiveWindow(),
          f"off={off} active={win.isActiveWindow()}")
    # 150% 缩放下 Qt 会把 1.5 取整成 2.0，这是 Qt 的既定行为，不是错；要验的是
    # Qt 自己的逻辑尺寸 × DPR == 真实物理屏，命中换算才有唯一正确的那把尺。
    scr = app.primaryScreen()
    phys_px = int(scr.geometry().width() * dpr)
    check("Qt DPR 与屏幕逻辑尺寸自洽", abs(phys_px - int(user32.GetSystemMetrics(0))) <= 2,
          f"logical={scr.geometry().width()} dpr={dpr} → {phys_px} vs 物理 {user32.GetSystemMetrics(0)}")

    d = win.title.drag_region()
    sx, sy = phys(d.mapToGlobal(d.rect().center()))
    ht_title = hit(hwnd, sx, sy)
    check("标题拖动区 = HTCLIENT（客户区，Qt 才收得到鼠标事件）", ht_title == HTCLIENT, f"got={ht_title} at ({sx},{sy})")

    # 拖动区中心的控件必须在 TitleBar 子树里（真实点击会到标题栏处理器）
    from app.ui.title_bar import TitleBar
    lp_center = d.mapTo(win, d.rect().center())
    wchk = win.childAt(lp_center)
    chain: list[str] = []
    in_title = False
    while wchk is not None:
        chain.append(wchk.objectName() or type(wchk).__name__)
        if isinstance(wchk, TitleBar):
            in_title = True
            break
        wchk = wchk.parentWidget()
    check("拖动区中心的控件在 TitleBar 子树里", in_title, " → ".join(chain))

    from PyQt5.QtWidgets import QLabel
    brand = win.title.findChild(QLabel, "BrandTitle")
    bx, by = phys(brand.mapToGlobal(brand.rect().center()))
    check("品牌字 = HTCLIENT", hit(hwnd, bx, by) == HTCLIENT, f"got={hit(hwnd, bx, by)}")

    # 标题栏内、离左缘 40 逻辑像素（已在缩放边以内）必须仍是客户区
    lx, ly = phys(win.title.mapToGlobal(QPoint(40, win.title.height() // 2)))
    check("标题栏左侧（缩放边以内）= HTCLIENT", hit(hwnd, lx, ly) == HTCLIENT,
          f"got={hit(hwnd, lx, ly)}")

    # 窗钮必须是 HTCLIENT
    from PyQt5.QtWidgets import QToolButton
    close_btn = next(b for b in win.title.findChildren(QToolButton) if "CloseBtn" in b.objectName())
    cx, cy = phys(close_btn.mapToGlobal(close_btn.rect().center()))
    check("关闭钮 = HTCLIENT", hit(hwnd, cx, cy) == HTCLIENT, f"got={hit(hwnd, cx, cy)}")

    # 视频区 = HTCLIENT
    vx, vy = phys(win.video.mapToGlobal(win.video.rect().center()))
    check("视频区 = HTCLIENT", hit(hwnd, vx, vy) == HTCLIENT, f"got={hit(hwnd, vx, vy)}")

    mid_y = r.top + (r.bottom - r.top) // 2
    check("左边缘 = HTLEFT", hit(hwnd, r.left + 1, mid_y) == HTLEFT, f"got={hit(hwnd, r.left + 1, mid_y)}")
    check("顶边 = HTTOP", hit(hwnd, (r.left + r.right) // 2, r.top + 1) == HTTOP,
          f"got={hit(hwnd, (r.left + r.right) // 2, r.top + 1)}")

    # ---- 拖动接线（直接喂鼠标事件，不碰真实鼠标）----
    from app import win32 as _w32
    title = win.title
    calls: list[int] = []
    real_move = _w32.begin_system_move
    _w32.begin_system_move = lambda h: (calls.append(int(h)), True)[1]
    try:
        title.mousePressEvent(mouse(QEvent.MouseButtonPress, (5, 5), (500, 500), Qt.LeftButton, Qt.LeftButton))
        title.mouseMoveEvent(mouse(QEvent.MouseMove, (8, 5), (503, 500), Qt.NoButton, Qt.LeftButton))
        title.mouseReleaseEvent(mouse(QEvent.MouseButtonRelease, (8, 5), (503, 500), Qt.LeftButton, Qt.NoButton))
        check("只按下/微动（< 阈值）不启动拖动（留给双击）", calls == [], f"{calls}")

        title.mousePressEvent(mouse(QEvent.MouseButtonPress, (5, 5), (500, 500), Qt.LeftButton, Qt.LeftButton))
        title.mouseMoveEvent(mouse(QEvent.MouseMove, (40, 5), (540, 500), Qt.NoButton, Qt.LeftButton))
        title.mouseReleaseEvent(mouse(QEvent.MouseButtonRelease, (40, 5), (540, 500), Qt.LeftButton, Qt.NoButton))
        check("按住标题栏越过阈值 → begin_system_move(本窗 HWND)", calls == [hwnd], f"{calls} vs hwnd={hwnd}")
    finally:
        _w32.begin_system_move = real_move

    check("标题栏双击槽就是主窗的 _toggle_max", title._on_max == win._toggle_max, f"{title._on_max}")

    win.hide()
    win.chrome.uninstall()
    win.deleteLater()
    app.processEvents()
    print(f"\n---- 真实 DPI 命中 + 拖动接线：{total[0] - len(fails)}/{total[0]} 通过 ----", flush=True)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
