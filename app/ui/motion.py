"""微动效：条目悬停/按下的短过渡 + 侧栏换页的淡入。

两件事都只做**短**过渡（140–170ms，OutCubic/exponential），因为动效在播放器里不是
装饰，是"这一下点到了"的确认：长半拍就开始挡手，再长就变成卡。

· 悬停只动**底色块的透明度**，选中态与"正在播放"仍即时——它们回答的是
  "现在在哪/选了谁"，慢半拍就成了跟不上手的界面。按下态也即时确认，松手不留残余。
· 换页不用 QGraphicsOpacityEffect：那是给整页做离屏合成，侧栏一页每个 tick 都要把
  整棵控件树重画一遍。这里用一层"侧栏底色纱"盖在内容上往下退——屏幕上同样是淡入，
  每帧只画一个矩形，代价差一个数量级。纱的初值取 0.72 而不是全遮：点下页签的
  第一帧就能看见内容，只是还没完全亮起来。

减少动画（`GELU_NO_MOTION=1` 或 Windows 关掉"动画效果"）时 `attach_hover()` 返回
None、`PageFade.run()` 什么都不做，所有控件退回原来的瞬时状态。
"""
from __future__ import annotations

import math
import time

from PyQt5.QtCore import (QEasingCurve, QEvent, QObject, QTimer, QVariantAnimation,
                          Qt)
from PyQt5.QtGui import QColor, QCursor, QPainter
from PyQt5.QtWidgets import QAbstractItemView, QStyle, QWidget

from . import theme
from .scroll_feel import reduced_motion

FRAME_MS = 16
HOVER_TAU_S = 0.038          # 约 110ms 收掉 95%：悬停要"跟手"，不能像缓动演示
SNAP = 0.01
PAGE_FADE_MS = 170
PAGE_VEIL = 0.72             # 换页第一帧的遮罩浓度（1=全遮）

_HOVER_ATTR = "_gelu_hover"


def mix_color(a: str, b: str, t: float) -> QColor:
    """两色按 t（0→1）线性混合；给悬停时"边框亮一档"这类小过渡用。"""
    t = max(0.0, min(1.0, float(t)))
    ca, cb = QColor(a), QColor(b)
    return QColor(
        int(round(ca.red() + (cb.red() - ca.red()) * t)),
        int(round(ca.green() + (cb.green() - ca.green()) * t)),
        int(round(ca.blue() + (cb.blue() - ca.blue()) * t)),
    )


class HoverAnimator(QObject):
    """给一个条目视图维护"每行的悬停进度"，按 16ms 推进并只重画变化的那几行。"""

    def __init__(self, view: QAbstractItemView):
        super().__init__(view)
        self.view = view
        self._viewport = view.viewport()
        self._progress: dict[int, float] = {}
        self._hover_row = -1
        self._pressed_row = -1
        self._last = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        # 重建收口后补挂悬停用的 0ms 单发。挂在 self 上（而不是 QTimer.singleShot）：
        # 视图先被销毁时它跟着死，不会在关窗后回调一个已删除的控件。
        self._resync_timer = QTimer(self)
        self._resync_timer.setSingleShot(True)
        self._resync_timer.setInterval(0)
        self._resync_timer.timeout.connect(self._resync_hover)
        self._viewport.setMouseTracking(True)
        self._viewport.installEventFilter(self)
        model = view.model()
        if model is not None:
            # 整表重建后行号整体作废：留着旧进度会在新内容上亮起无关的一行。
            model.modelAboutToBeReset.connect(self.reset)
            model.modelReset.connect(self._model_changed)
            model.rowsInserted.connect(self._model_changed)
            model.rowsRemoved.connect(self._model_changed)
        # 滚动时鼠标下面的条目会换人；不重新命中，高亮会留在已经滚走的那一行。
        view.verticalScrollBar().valueChanged.connect(self._resync_hover)

    # ---------- 读数（委托用） ----------
    def progress(self, row: int) -> float:
        return self._progress.get(int(row), 0.0)

    def is_pressed(self, row: int) -> bool:
        return int(row) == self._pressed_row

    # ---------- 事件 ----------
    def eventFilter(self, obj, ev):        # noqa: N802 — Qt 命名
        if obj is not self._viewport:
            return False
        t = ev.type()
        if t == QEvent.MouseMove:
            self._set_hover(self._row_at(ev.pos()))
        elif t == QEvent.Leave:
            self._set_hover(-1)
            self._set_pressed(-1)
        elif t == QEvent.MouseButtonPress:
            row = self._row_at(ev.pos())
            if row >= 0:
                self._set_pressed(row)
        elif t == QEvent.MouseButtonRelease:
            self._set_pressed(-1)
        return False

    def reset(self) -> None:
        self._timer.stop()
        self._progress.clear()
        self._hover_row = -1
        self._pressed_row = -1

    def _row_at(self, pos) -> int:
        idx = self.view.indexAt(pos)
        return int(idx.row()) if idx.isValid() else -1

    def _set_hover(self, row: int) -> None:
        if row == self._hover_row:
            return
        old = self._hover_row
        self._hover_row = row
        if old >= 0:
            self._progress.setdefault(old, 1.0)
            self._repaint_row(old)
        if row >= 0:
            self._progress.setdefault(row, 0.0)
            self._repaint_row(row)
        if self._progress and not self._timer.isActive():
            self._last = time.perf_counter()
            self._timer.start()

    def _set_pressed(self, row: int) -> None:
        if row == self._pressed_row:
            return
        old, self._pressed_row = self._pressed_row, row
        if old >= 0:
            self._repaint_row(old)
        if row >= 0:
            self._repaint_row(row)

    def _model_changed(self, *_a) -> None:
        """重建期间先清进度；等这一趟插行跑完，再按光标位置把悬停挂回新行。

        不挂回的话，扫描回包正好落在悬停中时，光标明明还停在网格上，高亮却要等到
        下一次移动才回来——看起来像"界面卡了一下"。
        """
        self.reset()
        self._resync_timer.start()

    def _resync_hover(self, _value: int = 0) -> None:
        if not self._viewport.isVisible():
            return
        if self._viewport.underMouse():
            self._set_hover(self._row_at(self._viewport.mapFromGlobal(QCursor.pos())))
        elif self._hover_row >= 0:
            self._set_hover(-1)

    # ---------- 动画 ----------
    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(0.05, max(0.004, now - self._last))
        self._last = now
        alpha = 1.0 - math.exp(-dt / HOVER_TAU_S)
        moving = False
        for row in list(self._progress):
            target = 1.0 if row == self._hover_row else 0.0
            value = self._progress[row]
            if abs(target - value) <= SNAP:
                value = target
            else:
                value += (target - value) * alpha
                moving = True
            self._progress[row] = value
            self._repaint_row(row)
            if value <= 0.0 and target == 0.0:
                del self._progress[row]
        if not moving:
            self._timer.stop()
        elif not self._progress:
            self._timer.stop()

    def _repaint_row(self, row: int) -> None:
        model = self.view.model()
        if model is None or row < 0 or row >= model.rowCount():
            return
        rect = self.view.visualRect(model.index(row, 0))
        if rect.isValid() and not rect.isEmpty():
            self._viewport.update(rect)


def attach_hover(view: QAbstractItemView):
    """给条目视图装悬停动画，返回 HoverAnimator；减少动画时返回 None。"""
    if view is None or reduced_motion():
        return None
    old = getattr(view, _HOVER_ATTR, None)
    if old is not None:
        return old
    anim = HoverAnimator(view)
    setattr(view, _HOVER_ATTR, anim)
    return anim


def row_state(opt, idx) -> tuple[float, bool]:
    """委托读一行此刻的（悬停进度, 是否按下）。

    装了动画就以动画为准；没装（减少动画 / 非条目视图）退回 Qt 的即时状态，
    这样同一个委托在两条路上都是对的。
    """
    anim = getattr(opt.widget, _HOVER_ATTR, None) if opt.widget is not None else None
    if anim is None:
        return (1.0 if opt.state & QStyle.State_MouseOver else 0.0,
                bool(opt.state & QStyle.State_Sunken))
    return anim.progress(idx.row()), anim.is_pressed(idx.row())


class _Veil(QWidget):
    """换页时盖在内容上的一层侧栏底色，`level` 从 1 退到 0。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self._level = 0.0
        self._color = QColor(theme.SIDEBAR_BG)

    def set_level(self, level: float) -> None:
        self._level = max(0.0, min(1.0, float(level)))
        self.update()

    def paintEvent(self, _ev):             # noqa: N802 — Qt 命名
        if self._level <= 0.0:
            return
        p = QPainter(self)
        c = QColor(self._color)
        c.setAlphaF(self._level)
        p.fillRect(self.rect(), c)


class PageFade(QObject):
    """侧栏换页时让新页从底色里浮出来（只画一层矩形，不做整页离屏合成）。"""

    def __init__(self, host: QWidget):
        super().__init__(host)
        self.host = host
        self._veil = _Veil(host)
        self._veil.hide()
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(PAGE_FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(lambda v: self._veil.set_level(float(v)))
        self._anim.finished.connect(self._veil.hide)
        host.installEventFilter(self)

    # 测试用读数：是否还有一页在淡入
    @property
    def running(self) -> bool:
        return self._anim.state() == QVariantAnimation.Running

    def run(self) -> None:
        if reduced_motion():
            return
        self._veil.setGeometry(self.host.rect())
        self._veil.raise_()
        self._veil.set_level(PAGE_VEIL)
        self._veil.show()
        self._anim.stop()
        self._anim.setStartValue(PAGE_VEIL)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def eventFilter(self, obj, ev):        # noqa: N802 — Qt 命名
        if obj is self.host and ev.type() == QEvent.Resize and self._veil.isVisible():
            self._veil.setGeometry(self.host.rect())
        return False
