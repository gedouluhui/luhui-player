"""视频容器：一个原生 HWND，把 mpv 的渲染窗口嵌进来，并负责输入与全屏几何。

为什么要点轮询：mpv 的子窗口永远盖在 Qt 表面之上（airspace），鼠标点落在它的窗口上，
Qt 的 mousePressEvent 根本收不到；又不能装全局钩子（见 docs/design.md §7），
所以用 30ms 轮询 GetCursorPos + GetAsyncKeyState 读"光标在哪、按键边沿"，
只读状态、不截获消息。滚轮留给 mpv（input.conf），这里故意不管。
"""
from __future__ import annotations

import time

from PyQt5.QtCore import QPoint, QTimer, Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget

from .. import win32

_DOUBLE_GAP = 0.42   # 两次抬起间隔小于它就判双击（比系统 GetDoubleClickSeconds 宽松些，够用）
_CLICK_SLOP = 12     # 按下与抬起之间移动超过它就不算"点击"（是拖选/滑动）


class VideoSurface(QWidget):
    """承载 mpv 画面的原生子窗口；对外只发语义事件，不认识播放状态。"""

    sig_click = pyqtSignal(int, QPoint)   # 1=左单击 2=左双击 3=右键；QPoint 是全局物理像素
    sig_hover = pyqtSignal(QPoint)        # 光标在画面内移动（局部逻辑坐标），用于唤出控制条
    sig_left_down = pyqtSignal()
    sig_left_up = pyqtSignal()

    def __init__(self, parent=None, poll_ms: int = 30):
        super().__init__(parent)
        self.setObjectName("VideoSurface")
        # 原生子窗口：winId() 会真的建一个 HWND，mpv 要的就是它
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setMouseTracking(True)
        self.setMinimumSize(160, 90)

        self._last_l = False
        self._last_r = False
        self._last_raw = QPoint()
        self._press_t = 0.0
        self._press_raw = QPoint()
        self._pending_single = False   # 已抬起一次，正在等第二个按下沿
        self._skip_next_up = False     # 双击的第二下：抬起不再算单击候选
        self._empty = True
        self._children = -1
        self.last_error: str | None = None
        # 四把计数留给验收（不是给人看的）：判"新加的两条路有没有**真跑起来**"。
        # 只看"没报错"是不够的——WindowFromPoint 万一在这台机器上恒给 0，代码会一路走
        # 矩形兜底，浮层那颗按钮照旧被多认成一次画面点击，而屏幕上什么异常都看不出来。
        self.poll_ticks = 0            # 真跑过几拍轮询（藏起来/最小化时不该再加——B1 的账）
        self.poll_fires = 0            # 定时器真响了几次（表没停也算——B1 的账就记在这一格）
        self.point_hits = 0            # 问到 HWND 的次数（按 HWND 认路那条跑了多久）
        self.point_fallbacks = 0       # 没问到、退回矩形判的次数（持续 >0 ＝那条路是瞎的）
        self.point_over_foreign = 0    # 光标压在**别人**的窗口上（浮层/遮挡）的拍数
        # 铺子窗口的账（B6）：同一次尺寸下重复 refit 只该真铺一遍；窗口还没起孩子时
        # （_fit_n=0）必须允许重试，否则内核刚起来的第一次铺窗会被"几何没变"吞掉。
        self.fit_calls = 0
        self.fit_runs = 0
        self.fit_skips = 0
        self._fit_key: tuple | None = None
        self._fit_n = 0
        self._fit_child = 0            # 上一次真铺时记下的孩子句柄（跳过前拿它验"还铺满吗"）

        self._poll = QTimer(self)
        self._poll.setInterval(poll_ms)
        self._poll.timeout.connect(self._tick)
        # 构造期**不**起表：这块容器出厂是隐藏的（空状态），而空状态里每一拍都是白跑
        # （两次 GetAsyncKeyState＋一圈比较，30ms 一次＝每秒三十几次纯浪费）。
        # 起表交给自己 showEvent，见 `_start_poll`。
        # （上一版就在这里：窗口还没显示、什么文件都没有，表已经在跑。）

    # ---------- 对外 ----------
    @property
    def hwnd(self) -> int:
        return int(self.winId())

    @property
    def is_empty(self) -> bool:
        return self._empty

    def set_empty(self, empty: bool) -> None:
        """没有文件时整块隐藏：父窗口不可见 ⇒ mpv 的子窗口自然跟着消失。"""
        self._empty = bool(empty)
        self.setVisible(not empty)

    def refit(self, force: bool = False) -> int:
        """把 mpv 建的子窗口铺满客户区（--wid 不会随父窗口自动缩放，实测必须手动铺）。

        同一几何下的重复调用直接跳过（B6）：隐藏/显式刷新那几路经常连着来好几声
        `refit()`（resizeEvent 自己还有一声），而每一次都要 EnumChildWindows 一圈。
        跳过是安全的——子窗口的 (位置,尺寸) 只由客户区决定；`_fit_n=0`（还没有孩子，
        内核刚起）时永远重试，`force=True` 给"明知几何没变也必须再铺一次"的调用方。

        2026-10-22 补：这个"跳过是安全的"少了一半——**孩子能被外力搬走/缩小**，而我们的
        几何键不会变（U60 抓到的形状：容器没动、孩子被缩到一半，走产品 refit 复原不了，
        画面永久缺一块）。所以跳过之前再问一句 `child_fills_client`：只认"上次铺的那个
        孩子确实正好盖住客户区"；问不到或已对不上就落回真铺一遍。`_fit_child=0`
        （离屏替身/还没记下句柄）保持旧行为直接跳过。
        """
        if self._empty or not self.isVisible():
            self._fit_key = None       # 藏起来期间几何变了，回来第一声必须真铺
            return 0
        self.fit_calls += 1
        key = (int(self.winId()), self.width(), self.height(),
               round(self.devicePixelRatioF() or 1.0, 3))
        if not force and key == self._fit_key and self._fit_n > 0:
            if not self._fit_child or win32.child_fills_client(self.hwnd, self._fit_child):
                self.fit_skips += 1
                return self._fit_n
        n = win32.fit_child_windows(self.hwnd)
        self._children = n
        self._fit_n = n
        self._fit_key = key
        # 同一时刻枚举顺序固定：`fit_child_windows` 铺的第一个就是我们留作锚点的那个。
        self._fit_child = win32.first_child(self.hwnd) if n else 0
        self.fit_runs += 1
        return n

    # ---------- Qt 事件 ----------
    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.refit()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.refit(force=True)
        self.sync_poll()

    def hideEvent(self, ev):
        super().hideEvent(ev)
        self._stop_poll()

    # ---------- 轮询的开关 ----------
    def _should_poll(self) -> bool:
        """画面不可见、还是空状态、或者**主窗最小化了**，都不该每 30ms 去问系统。

        B1 的账：最小化时 QWidget 仍算 visible（Qt 只在隐藏时发 hideEvent），旧代码
        的 `isVisible()` 闸门挡不住它 ⇒ 缩到任务栏之后每秒还在 GetCursorPos ×30。
        """
        if self._empty or not self.isVisible():
            return False
        try:
            w = self.window()
            return not (w is not None and w.isMinimized())
        except RuntimeError:           # C++ 侧已经拆了：当作"不该问"
            return False

    def sync_poll(self) -> None:
        """按当前可见性/最小化状态起停轮询。主窗的 changeEvent 会调这一下（接线见
        app/ui/main_window.py）；showEvent/hideEvent 也走同一条判定。"""
        if self._should_poll():
            self._start_poll()
        else:
            self._stop_poll()

    def _start_poll(self) -> None:
        """显示出来才开始问。两头都 `_sync_edges()`：藏起来之前/醒来之前的按键边沿都是过期的。

        不起表的形态是"画面点不动"，起太早的形态是"什么都没放也在每秒三十几次问系统"——
        两头都要，所以开关跟着可见性走，而不是跟着构造走。
        """
        self._sync_edges()
        if not self._poll.isActive():
            self._poll.start()

    def _stop_poll(self) -> None:
        self._sync_edges()
        self._poll.stop()

    # ---------- 轮询 ----------
    def _local_of(self, gx: int, gy: int) -> QPoint:
        """GetCursorPos 是物理像素，Qt 坐标是逻辑像素：不除 dpr 在 150% 缩放机上会偏一位。"""
        dpr = self.devicePixelRatioF() or 1.0
        return self.mapFromGlobal(QPoint(int(gx / dpr), int(gy / dpr)))

    def _tick(self) -> None:
        # 30ms 一次的槽里绝不能抛：异常穿过 Qt 的定时器回调会让 PyQt5 直接 abort 进程，
        # 而且窗口刚关闭的那一瞬 HWND 已失效，是最容易出错的时候。出错就停轮询。
        self.poll_fires += 1           # 表真响了一拍（哪怕下面立刻退回——读数要能分开这两件事）
        try:
            self._poll_tick()
        except Exception as exc:
            self._poll.stop()
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _inside_now(self, raw: QPoint) -> bool:
        """光标那一点到底是不是"这块画面"——**不能只按矩形判**（2026-10-20 全屏审查 §5①）。

        全屏的控制条是压在画面矩形**里面**的另一个顶层窗（`floaters.ChromeHost`）。只按矩形判，
        "点播放键"会被额外认成"点了一下画面"，而画面单击绑的正是暂停/播放——
        形态是"按一下没反应、按两下才像一下"。改成问 Win32"这点上最深的窗口是谁"，
        再沿 owner/parent 链往上走：画面里的链一定经过本控件的 HWND（mpv 的孩子窗口挂在我们下面），
        浮层的链走的是主人窗口（顶层窗的 owner），**不经过**本控件 ⇒ 不算画面内。

        查不到 HWND 时退回矩形判：宁可按旧口径多算一次点击，也别让整块画面点不动。
        三把计数（`point_hits`/`point_fallbacks`/`point_over_foreign`）就是给验收读的——
        只看"没报错"不够：WindowFromPoint 万一在这台机器上恒给 0，代码会一路走矩形兜底，
        浮层那颗按钮照旧被多认成一次画面点击，而屏幕上什么异常都看不出来。
        """
        h = win32.hwnd_at_point(raw.x(), raw.y())
        if h:
            self.point_hits += 1
            if h == self.hwnd or self.hwnd in win32.ancestor_chain(h):
                return True
            self.point_over_foreign += 1   # 浮层／别人的窗口压在画面上
            return False
        self.point_fallbacks += 1
        return self.rect().contains(self._local_of(raw.x(), raw.y()))

    def _poll_tick(self) -> None:
        # 闸门放在计数之前：藏起来/最小化的那几拍不是"轮询"，只是定时器空响（poll_fires
        # 记那一半）。旧写法先 +1 再判可见性，读数会把"表没停"说成"真跑了"。
        if not self._should_poll():
            self._sync_edges()
            return
        self.poll_ticks += 1

        raw = QPoint(*win32.cursor_pos())
        inside = self._inside_now(raw)
        l, r = win32.lbutton_down(), win32.rbutton_down()

        self._guard_focus(inside)

        if inside and raw != self._last_raw:
            self.sig_hover.emit(self._local_of(raw.x(), raw.y()))

        # 左键：按下/抬起都通知外壳（拖动条要按下沿）。双击在"第二次按下"就判定，
        # 比等第二次抬起更跟手；因此要把那一次抬起产生的点击候选丢掉。
        if l and not self._last_l:
            if inside:
                self.sig_left_down.emit()
                if self._pending_single and time.monotonic() - self._press_t < _DOUBLE_GAP:
                    self._pending_single = False
                    self._skip_next_up = True
                    self.sig_click.emit(2, raw)
                self._press_t = time.monotonic()
                self._press_raw = raw
            else:
                self._press_t = 0.0      # 按下点不在画面里：别拿上一趟的起点凑单击
        elif self._last_l and not l:
            self.sig_left_up.emit()
            if self._skip_next_up:
                self._skip_next_up = False
            elif inside and self._press_t and (raw - self._press_raw).manhattanLength() <= _CLICK_SLOP:
                self._pending_single = True
                QTimer.singleShot(int(_DOUBLE_GAP * 1000), self._flush_single)

        # 画面区不移动窗口：窗口只认标题栏/边框那一条（见 chrome 的 HTCAPTION）。
        # 按住画面移动超过点击容差就不算单击，避免拖选字幕/手势时误触暂停。
        self._last_l = l

        # 右键按 Windows 习惯在抬起时触发（按下就弹菜单会挡住拖动选菜单）
        if self._last_r and not r and inside:
            self.sig_click.emit(3, raw)
        self._last_r = r
        self._last_raw = raw

    def _flush_single(self) -> None:
        """双击窗口到期还没等到第二下 → 补发单击。"""
        if self._pending_single:
            self._pending_single = False
            self.sig_click.emit(1, self._last_raw)

    def _guard_focus(self, inside: bool) -> None:
        """按键落进 mpv 子窗口时 Qt 就收不到快捷键，发现就把焦点抢回顶层窗口。"""
        if not inside:
            return
        f = win32.focus_hwnd()
        if not f:
            return
        top = int(self.window().winId())
        if f != top and f in win32.direct_children(self.hwnd):
            win32.focus_window(top)

    def _sync_edges(self) -> None:
        self._last_l = win32.lbutton_down()
        self._last_r = win32.rbutton_down()
        self._pending_single = False
