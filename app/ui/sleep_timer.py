"""睡眠定时器：到点做一件事（暂停播放 / 停止播放 / 退出播放器），期间可随时取消。

三条不是顺手写成的选择：
  - **绝对截止时刻**（`time.monotonic()` 起点 + 秒数）而不是"每秒减一"的假倒计时。
    假倒计时在事件循环被长任务占住时会走样（少算几秒），余量也就骗人；这里余量
    永远由 `deadline - now` 现算，QTimer 只负责"到点叫一次"和"最后一分钟提醒一次"。
  - **到点动作交回主窗口执行**（sig_expire → dispatch）。关窗要存进度、要卸 chrome
    子类、要收内核，这些都写在 main_window.closeEvent 里；这里自己 `close()` 之外的
    野路子（比如直接 player.quit()）就会留下一个没人认领的 mpv 进程。
  - **取消要能撤销已经在途的定时器**：用可 stop 的 QTimer 实例而不是
    `QTimer.singleShot`——后者发出去就收不回，取消之后照样到点是最难查的那类 bug。
"""
from __future__ import annotations

import time

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton, QSpinBox,
                             QVBoxLayout, QWidget)

from .dialog_shell import AppDialog
from .scrubber import fmt_time

MINUTES_MIN = 1
MINUTES_MAX = 720
PRESETS = (15, 30, 45, 60, 90)

ACTIONS: tuple[tuple[str, str], ...] = (
    ("pause", "暂停播放"),
    ("stop", "停止播放"),
    ("quit", "退出播放器"),
)
WARN_BEFORE = 60.0      # 最后一分钟提醒一次，不做每秒冒泡


def action_text(action: str) -> str:
    for k, text in ACTIONS:
        if k == action:
            return text
    return str(action)


class SleepTimer(QObject):
    """一个"到点 + 到点做什么"的定时器。界面只读 state()/remaining()，不自己数秒。"""

    sig_state = pyqtSignal(str)          # 给人看的一句话（OSD）
    sig_expire = pyqtSignal(str)         # 到点：把动作名交回主窗口执行

    def __init__(self, parent=None):
        super().__init__(parent)
        self._deadline = 0.0
        self._secs = 0.0
        self._action = "pause"
        self._fire_timer: QTimer | None = None
        self._warn_timer: QTimer | None = None

    # ---------- 状态 ----------
    @property
    def armed(self) -> bool:
        return self._deadline > 0.0

    def remaining(self) -> float:
        if not self.armed:
            return 0.0
        return max(0.0, self._deadline - time.monotonic())

    def action(self) -> str:
        return self._action

    def state_text(self) -> str:
        if not self.armed:
            return "睡眠定时：未设置"
        return f"睡眠定时：还剩 {fmt_time(self.remaining())} · {action_text(self._action)}"

    # ---------- 操作 ----------
    def arm(self, minutes: int, action: str) -> bool:
        try:
            m = int(minutes)
        except (TypeError, ValueError):
            return False
        if m < MINUTES_MIN or m > MINUTES_MAX:
            self.sig_state.emit(f"睡眠定时：只接受 {MINUTES_MIN}~{MINUTES_MAX} 分钟")
            return False
        known = {k for k, _ in ACTIONS}
        if action not in known:
            # 不认识的动作为什么不回落成"暂停"：那样用户以为设的是退出、实际只暂停，
            # 机器整晚醒着都没人知道。宁可当场不受理。
            self.sig_state.emit("睡眠定时：不认的动作 " + str(action))
            return False
        self._stop_timers()
        self._secs = float(m) * 60.0
        self._deadline = time.monotonic() + self._secs
        self._action = action
        self._fire_timer = QTimer(self)
        self._fire_timer.setSingleShot(True)
        self._fire_timer.timeout.connect(self._fire)
        # 上限：QTimer 的毫秒数是 int32，720 分钟（4320 万 ms）还没超，但保险起见夹一下
        self._fire_timer.start(min(int(self._secs * 1000), 2 ** 31 - 1))
        if self._secs > WARN_BEFORE:
            self._warn_timer = QTimer(self)
            self._warn_timer.setSingleShot(True)
            self._warn_timer.timeout.connect(
                lambda: self.sig_state.emit("睡眠定时：还剩 1 分钟") if self.armed else None)
            self._warn_timer.start(int((self._secs - WARN_BEFORE) * 1000))
        self.sig_state.emit(f"已设定：{m} 分钟后{action_text(action)}")
        return True

    def cancel(self) -> None:
        was = self.armed
        self._stop_timers()
        self._deadline = 0.0
        self._secs = 0.0
        if was:
            self.sig_state.emit("已取消睡眠定时")

    def _stop_timers(self) -> None:
        for t in (self._fire_timer, self._warn_timer):
            if t is not None:
                t.stop()
        self._fire_timer = self._warn_timer = None

    def _fire(self) -> None:
        if not self.armed:
            return
        action = self._action
        self._stop_timers()
        self._deadline = 0.0
        self._secs = 0.0
        self.sig_state.emit(f"睡眠定时到点：{action_text(action)}")
        self.sig_expire.emit(action)


class SleepDialog(AppDialog):
    """设置睡眠定时的对话框：已有定时时显示余量并提供"取消"。"""

    def __init__(self, timer: SleepTimer, parent: QWidget | None = None):
        super().__init__(parent)
        self.t = timer
        self.setWindowTitle("睡眠定时")
        self.setObjectName("SettingsDialog")
        self.resize(380, 236)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)

        self.lbl_state = QLabel(timer.state_text(), self)
        self.lbl_state.setObjectName("DocTitle")
        lay.addWidget(self.lbl_state)

        # 状态那行只有结果（未设置 / 还剩多少），"谁来数、到点干什么"得有一句自己的话。
        # 少了它，下面这几个控件只能靠猜：尤其"启用"看上去像在问"要不要开始播放"。
        hint = QLabel("倒计时由播放器自己数；到点自动执行所选动作。", self)
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        mins = QHBoxLayout()
        mins.setSpacing(8)
        lab = QLabel("时长", self)
        lab.setObjectName("RowLabel")
        mins.addWidget(lab)
        self.spin = self._minutes()
        mins.addWidget(self.spin, 1)
        lay.addLayout(mins)

        quick = QHBoxLayout()
        quick.setSpacing(6)
        for m in PRESETS:
            b = QPushButton(str(m), self)
            b.setObjectName("PageAction")
            b.setFixedWidth(38)
            b.setFocusPolicy(Qt.NoFocus)
            b.setToolTip(f"{m} 分钟")
            b.clicked.connect(lambda _c=False, v=m: self.spin.setValue(v))
            quick.addWidget(b)
        quick.addStretch(1)
        lay.addLayout(quick)

        act = QHBoxLayout()
        act.setSpacing(8)
        lab = QLabel("到点", self)
        lab.setObjectName("RowLabel")
        act.addWidget(lab)
        self.cb_action = QComboBox(self)
        for k, text in ACTIONS:
            self.cb_action.addItem(text, k)
        self.cb_action.setCurrentIndex(max(0, [k for k, _ in ACTIONS].index(timer.action())))
        self.cb_action.setFocusPolicy(Qt.NoFocus)
        act.addWidget(self.cb_action, 1)
        lay.addLayout(act)

        lay.addStretch(1)
        row = QHBoxLayout()
        self.btn_off = QPushButton("取消定时", self)
        self.btn_off.setFocusPolicy(Qt.NoFocus)
        self.btn_off.clicked.connect(self._cancel)
        self.btn_on = QPushButton("启用", self)
        self.btn_on.setObjectName("Primary")
        self.btn_on.setFocusPolicy(Qt.NoFocus)
        self.btn_on.clicked.connect(self._apply)
        # 「取消定时」和「启用」分居两端：它们是一对相反动作，挨着放很容易点反。
        row.addWidget(self.btn_off)
        row.addStretch(1)
        row.addWidget(self.btn_on)
        lay.addLayout(row)

        # 余量每秒现算一次：只在对话框开着时数，关掉窗口不该留下一个常驻定时器
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._repaint_state)
        self._tick.start()
        self._repaint_state()

    def _minutes(self) -> QSpinBox:
        s = QSpinBox(self)
        s.setRange(MINUTES_MIN, MINUTES_MAX)
        s.setValue(30)
        s.setSuffix(" 分钟")
        s.setFocusPolicy(Qt.NoFocus)
        return s

    def _repaint_state(self) -> None:
        self.lbl_state.setText(self.t.state_text())
        self.btn_off.setEnabled(self.t.armed)

    def _apply(self) -> None:
        self.t.arm(int(self.spin.value()), str(self.cb_action.currentData()))
        self._repaint_state()

    def _cancel(self) -> None:
        self.t.cancel()
        self._repaint_state()

    def closeEvent(self, ev):
        self._tick.stop()
        super().closeEvent(ev)
