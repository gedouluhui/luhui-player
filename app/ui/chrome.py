"""无边框窗口外壳：保留原生阴影/圆角/贴边快照，同时把非客户区并进客户区。

做法（Qt 里做 Fluent 无边框的常规路子，本文件把它的坑都写清楚）：
  1. Qt 侧用 FramelessWindowHint，再用 win32 把 WS_CAPTION|WS_THICKFRAME 加回去——
     只有带着这两个样式位，DWM 才会给窗口阴影、圆角，任务栏/贴边的系统快照才认它。
  2. 处理 WM_NCCALCSIZE：把客户区扩到整个窗口矩形，标题带就真的没了。
     代价是那条 8px 缩放边框压在内容里（贴边 8px 内是"缩放"而不是"点击"），
     控制条与时间轴本来就内缩，够用；换来的是原生材质，值。
  3. 处理 WM_NCHITTEST：左右下边缘给 HTLEFT/…（缩放仍走 DefWindowProc），顶边只让出
     HTCAPTION_EDGE 像素，其余（含标题栏）一律 HTCLIENT。标题栏**不给 HTCAPTION**：
     WS_POPUP 无边框窗上 DefWindowProc 的隐式标题栏拖动根本不启动（"命中正确却拖不动"
     的根因），给了也拖不动。移动改由 TitleBar 收到客户区鼠标事件后调 startSystemMove
     进系统移动循环（见 title_bar.py / win32.begin_system_move），贴边吸附/跨屏/双击最大化都还在。
     应用级原生过滤器收不到输入消息（Qt 用 !isInputMessage 挡掉 WM_NCLBUTTONDOWN），
     PyQt5 又没有窗口级原生过滤器，所以拦消息这条路走不通，只能从客户区鼠标事件入手。
  4. 处理 WM_GETMINMAXINFO：最大化矩形 = 工作区 + 一个边框宽，配合第 2 条的内缩，
     客户区正好等于工作区（不内缩就会盖住任务栏/顶出屏幕）。
  5. 全屏交给 Qt 的 showFullScreen()：它会自己摘框架样式，跨 DPI/多屏也由它算；
     进出全屏后由 MainWindow 调 refresh_frame() 补样式与圆角开关。

注意 ctypes 细节：wt.MSG 的 wParam/lParam 是 32 位 c_long，64 位下会把指针截断，
所以这里自定义 _MSG，两个参数都用 c_ssize_t。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

from PyQt5.QtCore import QAbstractNativeEventFilter, QCoreApplication, Qt, QTimer
from PyQt5.QtWidgets import QWidget

from .. import win32

HTCAPTION_EDGE = 4          # 顶边只让出 4px 给缩放，其余归标题拖动


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("pad", wt.UINT),
                ("wParam", ctypes.c_ssize_t), ("lParam", ctypes.c_ssize_t),
                ("time", wt.DWORD), ("pt", wt.POINT)]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wt.POINT), ("ptMaxSize", wt.POINT), ("ptMaxPosition", wt.POINT),
                ("ptMinTrackSize", wt.POINT), ("ptMaxTrackSize", wt.POINT)]


class Chrome(QAbstractNativeEventFilter):
    """装在一个顶层 QWidget 上的原生消息管家。"""

    def __init__(self, window: QWidget):
        super().__init__()
        self.w = window
        self._installed = False
        self._hwnd: int | None = None         # 安装时记下的 HWND：消息比对用它，不再回身调 self.w
        self.last_error: str | None = None    # nativeEvent 里兜住的异常（自检页会读）
        self.seen: dict[int, int] = {}        # 每条本管消息的计数：为 0 就说明过滤器根本没跑
        # WM_DPICHANGED 之后要校准 mpv 原生子窗口（Qt 不管它）。回调由 MainWindow 接：
        # 0ms 一拍先按新物理尺寸铺一次，80ms 再兜一拍——跨屏拖动时系统会在窗口落定
        # 前后各发一次，布局的最终几何不一定在第一拍就绪。
        self.on_dpi_changed = None

    # ---------- 安装 ----------
    def install(self) -> None:
        """在窗口已经建好 HWND（show 过或调用过 winId()）之后装。

        装在 QApplication（应用级）上。注意它**收不到输入消息**：Qt 的 qwindowscontext
        用 `!isInputMessage(msg)` 把 WM_NCLBUTTONDOWN 这类挡在应用级过滤器之外，所以
        "按住标题栏拖动"不能靠在这里拦 WM_NCLBUTTONDOWN 实现（PyQt5 又没有窗口级原生
        过滤器可装）。那条路改由 TitleBar 的鼠标事件调 startSystemMove 走（见 title_bar.py）；
        本过滤器只管非输入消息：WM_NCCALCSIZE / WM_NCHITTEST / WM_GETMINMAXINFO。
        """
        self.refresh_frame()
        if not self._installed:
            app = QCoreApplication.instance()
            if app is not None:
                self._hwnd = int(self.w.winId())
                app.installNativeEventFilter(self)
                self._installed = True

    def uninstall(self) -> None:
        """摘掉过滤器。**必须**在窗口关闭时调用：installNativeEventFilter 只存裸指针、
        不接管所有权，Chrome 一旦被 GC 而清单里还留着它的地址，后续每条原生消息都是
        use-after-free——实测形态是进程在退出阶段访问违例，faulthandler 只能给出
        "<no Python frame>"（崩在 C++ 里，Python 栈早没了），比崩溃更难查的是它偶发。
        """
        if self._installed:
            app = QCoreApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self)
            self._installed = False
        self._hwnd = None

    # ---------- 全屏与样式 ----------
    def set_fullscreen(self, on: bool) -> None:
        # 状态相同就别再叫 showNormal()：设置页改"全屏时浮层控制条"等选项时也会走到
        # 这里，而无条件 showNormal() 会把用户已经最大化的窗口打回普通态（屏幕上表现为
        # 窗口突然缩小、位置也跳了）。全屏→全屏的重复调用同理，不再抖一次边框。
        if on:
            if not self.w.isFullScreen():
                self.w.showFullScreen()
        elif self.w.isFullScreen():
            self.w.showNormal()
        self.refresh_frame()

    def refresh_frame(self) -> None:
        hwnd = int(self.w.winId())
        fs = bool(self.w.windowState() & Qt.WindowFullScreen)
        win32.set_corner_rounded(hwnd, not fs)
        if not fs:
            # Qt 在状态变化时会按它认为的 flags 重写样式位，把我们加的框架位抹掉，
            # 所以进出全屏/最小化之后都要补一次，否则阴影、圆角、贴边快照一起丢。
            win32.add_frame_styles(hwnd)

    # ---------- 原生消息 ----------
    def nativeEventFilter(self, eventType, message):
        # PyQt5 把 C++ 的 nativeEvent 暴露成 **nativeEventFilter**：方法名写成 nativeEvent
        # 时基类会认为虚函数没实现，每条消息都抛 NotImplementedError
        # （"abstract and must be overridden"），外壳静默失效，还可能把异常带进原生回调。
        # 这里也绝不能让 Python 异常逃出去：nativeEventFilter 是被 Windows 在发消息时
        # 直接调进 Python 的，异常穿过原生栈时 PyQt5 会 abort 整个进程（实测：一次
        # AttributeError 让窗口在 show() 中途无声消失，日志里连栈都留不下）。全量兜住。
        try:
            return self._dispatch(eventType, message)
        except Exception as exc:  # 宁可外壳退化，也不能带崩播放器
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False, 0

    def _dispatch(self, eventType, message):
        if eventType != b"windows_generic_MSG":
            return False, 0
        try:
            msg = _MSG.from_address(int(message))
        except (ValueError, OSError):
            return False, 0
        # 用安装时记下的 HWND 比对，不回身调 self.w.winId()：窗口先被销毁、过滤器后
        # 拆的话，那一句就是在已删除的 C++ 对象上发消息（RuntimeError 至少还能兜住，
        # 但访问违例兜不住）。卸载后 _hwnd 是 None，这里直接不认任何消息。
        if self._hwnd is None or not msg.hwnd or int(msg.hwnd) != int(self._hwnd):
            return False, 0
        self.seen[msg.message] = self.seen.get(msg.message, 0) + 1
        if msg.message == win32.WM_DPICHANGED:
            # 放行（False）：新位置/尺寸 Qt 自己会用建议矩形落地。回调只负责把 mpv
            # 的原生子窗口按**新**客户区物理宽高再铺一次；`--wid` 的子窗口不跟随
            # 父窗缩放，不补这一下就是"跨屏之后画面缩在左上角/大出一块"。
            cb = self.on_dpi_changed
            if cb is not None:
                QTimer.singleShot(0, cb)
                QTimer.singleShot(80, cb)
            return False, 0
        if msg.message == win32.WM_NCCALCSIZE:
            return self._on_nccalcsize(msg)
        if msg.message == win32.WM_NCHITTEST:
            return self._on_nchittest(msg)
        if msg.message == win32.WM_GETMINMAXINFO:
            return self._on_getminmaxinfo(msg)
        return False, 0

    # ---- 各消息 ----
    def _on_nccalcsize(self, msg):
        if not msg.wParam:
            return False, 0
        p = win32.NCCALCSIZE_PARAMS.from_address(msg.lParam)
        hwnd = int(msg.hwnd)
        if win32.is_maximized(hwnd):
            b = win32.border_width(hwnd)
            r = p.rgrc[0]
            r.left += b
            r.top += b
            r.right -= b
            r.bottom -= b
        return True, 0     # 客户区 = 整个窗口矩形（最大化时按边框内缩）

    def _on_nchittest(self, msg):
        hwnd = int(msg.hwnd)
        # WM_NCHITTEST 的 lParam 是两个 16 位**有符号**短整数拼的屏幕坐标（负坐标屏会在左/上）。
        # 注意 ctypes.wintypes 里没有 c_short，得用 ctypes 本体的。
        x = ctypes.c_short(msg.lParam & 0xFFFF).value
        y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
        fs = bool(self.w.windowState() & Qt.WindowFullScreen)
        left, top, right, bottom = win32.window_rect(hwnd)
        b = win32.border_width(hwnd)

        # 缩放边：这一段用的是物理像素（GetWindowRect/WM_NCHITTEST 都是），
        # 别混进 Qt 的逻辑坐标；全屏与最大化时不给缩放边。
        if not fs and not win32.is_maximized(hwnd):
            near_l, near_r = x <= left + b, x >= right - b
            near_t, near_b = y <= top + HTCAPTION_EDGE, y >= bottom - b
            if near_t and near_l:
                return True, win32.HTTOPLEFT
            if near_t and near_r:
                return True, win32.HTTOPRIGHT
            if near_b and near_l:
                return True, win32.HTBOTTOMLEFT
            if near_b and near_r:
                return True, win32.HTBOTTOMRIGHT
            if near_l:
                return True, win32.HTLEFT
            if near_r:
                return True, win32.HTRIGHT
            if near_t:
                return True, win32.HTTOP
            if near_b:
                return True, win32.HTBOTTOM

        # 客户区（含标题栏）一律 HTCLIENT，标题栏不在这里给 HTCAPTION：WS_POPUP 无边框窗上
        # DefWindowProc 的隐式 HTCAPTION 拖动根本不启动（"命中正确却拖不动"的根因）。
        # 拖动改由 TitleBar 收到客户区鼠标事件后调 startSystemMove 进系统移动循环（title_bar.py），
        # 贴边吸附/跨屏/双击最大化都还在。视频区是 mpv 的原生子窗口，点击到不了本 HWND，天然不拖。
        return True, win32.HTCLIENT

    def _on_getminmaxinfo(self, msg):
        hwnd = int(msg.hwnd)
        mi = _MINMAXINFO.from_address(msg.lParam)
        # 跟命中测试用同一把尺：Qt 的逻辑最小尺寸 × Qt 的 DPR。用 Win32 DPI 会在
        # 150% 取整到 2.0 的机器上把下限报小，拖动缩放时先缩过头、再被 Qt 弹回来。
        # 下限读 live 值而不是常量：小屏上 _restore_geometry 会把最小宽收到屏幕宽，
        # 让窗口能整个放下；这里若仍报 880，用户一拽边框就会被顶回屏外。
        dpr = self.w.devicePixelRatioF() or 1.0
        mw = max(1, self.w.minimumWidth())
        mh = max(1, self.w.minimumHeight())
        mi.ptMinTrackSize.x = int(mw * dpr)
        mi.ptMinTrackSize.y = int(mh * dpr)
        if not win32.is_maximized(hwnd):
            wl, wtop, wr, wb = win32.monitor_workarea(hwnd)
            ml, mt, _mr, _mb = win32.monitor_fullarea(hwnd)
            b = win32.border_width(hwnd)
            mi.ptMaxPosition.x = wl - ml - b
            mi.ptMaxPosition.y = wtop - mt - b
            mi.ptMaxSize.x = (wr - wl) + 2 * b
            mi.ptMaxSize.y = (wb - wtop) + 2 * b
        return False, 0
