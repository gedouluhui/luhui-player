"""滚动手感：惯性滚轮与滚动条淡出。

Qt 默认的一次滚轮＝一次瞬移（value 直接加 N×singleStep），连续滚动时每格之间是
断开的，看起来像翻页；滚动条则永远亮着，在一屏安静的内容边上多一道常驻灰线。
这里把每一格换成一段目标位移，用 16ms 定时器指数逼近，连续滚动把增量叠到同一个
目标上——松手之后画面还在收尾，这就是"惯性"的来源；滚动条滚动时出现，停手约
0.9s 后淡出。

三条必须遵守的规则：
  1. 每次 tick 都把目标重新夹进滚动条**当前**量程：媒体库扫描回填会让 range 变小，
     旧目标会把滚动条顶出去再被 Qt 夹回来，表现是"滚过头又弹一下"。
  2. 外部 setValue（拖动、键盘、scrollToItem）不是我们的动画。用 `_applying` 区分；
     不区分的话下一次 tick 会把用户刚拖到的位置拽回旧目标。
  3. 滚动条淡出的特效**延迟到第一次滚动/悬停才挂**：没滚动过的界面渲染结果与改动前
     逐像素一致，截图回归不因这个模块变红；淡出只发生在用户确实碰过滚动之后。

`GELU_NO_MOTION=1`，或 Windows 关掉"动画效果"（SPI_GETCLIENTAREAANIMATION=0）时，
`attach()` 返回 None，整个模块退回 Qt 原生滚动——无障碍设置优先于手感。
"""
from __future__ import annotations

import math
import os
import sys
import time

from PyQt5.QtCore import (QEasingCurve, QEvent, QObject, QPropertyAnimation, Qt,
                          QTimer)
from PyQt5.QtWidgets import (QAbstractItemView, QAbstractScrollArea, QApplication,
                             QGraphicsOpacityEffect, QWidget)

FRAME_MS = 16            # 60fps 的节拍；指数逼近按真实 dt 算，掉帧也不会变慢
TAU_S = 0.055            # 时间常数：约 150ms 收完 95%，介于"跟手"与"有惯性"之间
SNAP_PX = 0.6            # 离目标不到半像素直接落地，避免定时器为了 1px 空转
FADE_MS = 180            # 滚动条淡入/淡出的时长
FADE_IDLE_MS = 900       # 停手多久之后淡出
LINE_PX = 28.0           # 系统"每次滚 N 行"里一行的估值（逻辑像素）
STEP_MIN, STEP_MAX = 60.0, 220.0   # 一次滚轮位移的夹口：太小像没动，太大像翻页
SPI_GETCLIENTAREAANIMATION = 0x1042

_ATTR = "_gelu_scroll_feel"       # attach 过的记号：重复调用不叠第二层


def reduced_motion() -> bool:
    """系统是否要求减少动画：环境变量优先，其次 Windows 的"动画效果"开关。"""
    env = (os.environ.get("GELU_NO_MOTION") or "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        enabled = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0)
        return bool(ok) and not enabled.value
    except Exception:
        return False


class ScrollbarFade(QObject):
    """滚动条的空闲淡出。特效对象懒建，初始外观与没有这个类时完全一样。"""

    def __init__(self, bar, parent=None):
        super().__init__(parent or bar)
        self.bar = bar
        self._effect: QGraphicsOpacityEffect | None = None
        self._anim: QPropertyAnimation | None = None
        self._hover = False
        self._dragging = False
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(FADE_IDLE_MS)
        self._idle.timeout.connect(self._fade_out)
        bar.installEventFilter(self)

    # ---------- 对外 ----------
    def poke(self) -> None:
        """有滚动发生：立刻可见，并重新开始空闲计时。"""
        self._idle.stop()
        self._to(1.0)
        if not (self._hover or self._dragging):
            self._idle.start()

    @property
    def opacity(self) -> float:
        return 1.0 if self._effect is None else float(self._effect.opacity())

    # ---------- 事件 ----------
    def eventFilter(self, obj, ev):        # noqa: N802 — Qt 命名
        if obj is not self.bar:
            return False
        t = ev.type()
        if t == QEvent.Enter:
            self._hover = True
            self._idle.stop()
            self._to(1.0)
        elif t == QEvent.Leave:
            self._hover = False
            if not self._dragging:
                self._idle.start()
        elif t == QEvent.MouseButtonPress:
            self._dragging = True
            self._idle.stop()
            self._to(1.0)
        elif t == QEvent.MouseButtonRelease:
            self._dragging = False
            if not self._hover:
                self._idle.start()
        return False

    # ---------- 内部 ----------
    def _ensure(self) -> None:
        if self._effect is not None:
            return
        self._effect = QGraphicsOpacityEffect(self.bar)
        self._effect.setOpacity(1.0)
        self.bar.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def _to(self, value: float) -> None:
        self._ensure()
        assert self._effect is not None and self._anim is not None
        if abs(self._effect.opacity() - value) < 0.01:
            self._effect.setOpacity(value)
            return
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(value)
        self._anim.start()

    def _fade_out(self) -> None:
        if self._hover or self._dragging:
            return
        if self._effect is not None:
            self._to(0.0)


class SmoothScroll(QObject):
    """给一个 QAbstractScrollArea 接管滚轮：目标位移 + 指数逼近。"""

    def __init__(self, area: QAbstractScrollArea):
        super().__init__(area)
        self.area = area
        self.bar = area.verticalScrollBar()
        self._viewport = area.viewport()
        self._target = float(self.bar.value())
        self._current = float(self.bar.value())
        self._applying = False      # 区分"我们在 setValue"与"外部在改值"
        self._last = 0.0
        self.steps = 0              # 真正推动过画面的 tick 数（测试读它）
        self.fade = ScrollbarFade(self.bar, self)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._viewport.installEventFilter(self)
        # 滑轨、下拉会自己吞掉 Wheel（QSlider/QComboBox 默认就改值）：只挂在视口上，
        # 鼠标停在它们身上时滚轮就变成"改设置"而不是"滚页面"。所以滚轮在应用级截，
        # 只认落在这个视口内部的控件——菜单、弹出下拉、控制条上的滑轨都不受影响。
        # 普通事件过滤器在 QObject 析构时会自动摘除，和原生消息过滤器那种裸指针不同。
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.bar.valueChanged.connect(self._on_value)

    def uninstall(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._timer.stop()

    # ---------- 对外 ----------
    @property
    def animating(self) -> bool:
        return self._timer.isActive()

    @property
    def target(self) -> float:
        return self._target

    @property
    def current(self) -> float:
        return self._current

    def cancel(self) -> None:
        """立刻停在当前真值上。用户按下/拖动/键盘接管时调用。"""
        self._timer.stop()
        self._current = self._target = float(self.bar.value())

    # ---------- 事件 ----------
    def eventFilter(self, obj, ev):        # noqa: N802 — Qt 命名
        if ev.type() == QEvent.Wheel and isinstance(obj, QWidget):
            # 应用级过滤器先于控件自己的 wheelEvent 到达：停在滑轨/下拉上的滚轮
            # 也应该滚页面，而不是把「字幕大小」改掉。
            if obj is self._viewport or self._viewport.isAncestorOf(obj):
                return self._on_wheel(ev)
            return False
        if obj is self._viewport and ev.type() == QEvent.MouseButtonPress:
            # 惯性还在跑时按下：先停住再让点击落地，否则条目会在
            # press 与 release 之间移开，点到的不是看见的那一格。
            self.cancel()
        return False

    def _on_wheel(self, ev) -> bool:
        # 带修饰键的滚轮留给控件自己：帮助页的 Ctrl+滚轮是缩放，Shift+滚轮是横向。
        # 这里一律不抢，否则"平滑滚动"会把那两个已有功能静默吃掉。
        if ev.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier
                             | Qt.MetaModifier):
            return False
        dy = self._wheel_dy(ev)
        if dy == 0.0:
            return False
        self.fade.poke()
        if not self._timer.isActive():
            self._current = float(self.bar.value())
            self._target = self._current
        self._target += dy
        self._clamp()
        if abs(self._target - self._current) >= SNAP_PX:
            self._last = time.perf_counter()
            self._timer.start()
        else:
            self._set(self._target)
        ev.accept()
        return True

    def _wheel_dy(self, ev) -> float:
        """一次滚轮的位移（逻辑像素，正数=向下）。"""
        pixels = ev.pixelDelta().y()
        if pixels:
            # 触控板：delta 本身就是像素，再乘倍率会飞出去。
            return -float(pixels)
        angle = ev.angleDelta().y()
        if not angle:
            return 0.0
        try:
            lines = int(QApplication.wheelScrollLines())
        except Exception:
            lines = 3
        if lines <= 0:          # 系统设成"一次滚一页"
            step = max(120.0, self._viewport.height() * 0.9)
        else:
            step = min(STEP_MAX, max(STEP_MIN, lines * LINE_PX))
        return -(angle / 120.0) * step

    # ---------- 动画 ----------
    def _tick(self, dt: float | None = None) -> None:
        now = time.perf_counter()
        if dt is None:
            dt = min(0.05, max(0.004, now - self._last))
        self._last = now
        self._clamp()
        diff = self._target - self._current
        if abs(diff) <= SNAP_PX:
            self._set(self._target)
            self._timer.stop()
            return
        self._current += diff * (1.0 - math.exp(-dt / TAU_S))
        self._set(self._current)
        self.steps += 1

    def _clamp(self) -> None:
        lo = float(self.bar.minimum())
        hi = float(self.bar.maximum())
        self._target = min(max(self._target, lo), hi)
        self._current = min(max(self._current, lo), hi)

    def _set(self, value: float) -> None:
        iv = int(round(value))
        if iv == self.bar.value():
            return
        self._applying = True
        try:
            self.bar.setValue(iv)
        finally:
            self._applying = False

    def _on_value(self, value: int) -> None:
        if self._applying:
            return
        # 外部改值：用户拖动/键盘/scrollToItem，或者量程变化把值夹了一下。
        self._current = self._target = float(value)
        self._timer.stop()

    # ---------- 测试 ----------
    def settle(self, step_s: float = FRAME_MS / 1000.0, limit: int = 240) -> int:
        """把动画跑完（测试用：不依赖真实时钟）。返回 tick 次数。"""
        n = 0
        while self._timer.isActive() and n < limit:
            self._tick(step_s)
            n += 1
        return n


def attach(area: QAbstractScrollArea):
    """给滚动区装上平滑滚动与滚动条淡出，返回 SmoothScroll（不可动时返回 None）。

    重复调用返回同一个对象。减少动画时什么都不装，保持 Qt 原生行为。
    """
    if area is None:
        return None
    old = getattr(area, _ATTR, None)
    if old is not None:
        return old
    if reduced_motion():
        return None
    if isinstance(area, QAbstractItemView):
        # 平滑滚轮按像素算位移；条目模式下一次 setValue 是一整行，动画会变成跳行。
        area.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    feel = SmoothScroll(area)
    setattr(area, _ATTR, feel)
    return feel
