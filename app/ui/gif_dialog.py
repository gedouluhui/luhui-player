"""截图工坊：GIF 片段导出 + 连拍。一个只管"把界面里的数收成一个 dict 发出去"的对话框。

纪律有三条，都是前面几轮踩出来的：

1. **不碰引擎**。当前播放位置、时长、A-B、外挂字幕文件这些"事实"由外部以一个
   `state_of()` 回调给进来（主窗口那边是 player 的订阅缓存），对话框既不发 IPC 请求，
   也不持有 player。这样 tests/test_gif_dialog.py 拿一个假字典就能把整个对话框跑完，
   不用起内核、不用真片子。
2. **夹住在界面里做**。时长上限、起点不能越过片尾，都在 `spec()` 里用 engine/gif.py 的
   `clamp_range()` 收口——同一个函数，界面上看到的区间和真发出去的区间不可能对不上。
3. **槽里绝不许抛**。PyQt5 的槽里一个 TypeError 不是 traceback，是进程当场没了
   （rc=127、stderr 全空）。所以进度/结果两个槽都先兜类型，再动控件。
"""
from __future__ import annotations

import os
from typing import Any, Callable

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QGridLayout, QHBoxLayout,
                             QLabel, QProgressBar, QPushButton, QShortcut, QSpinBox,
                             QVBoxLayout, QWidget)

from ..engine import gif
from . import keymap, theme
from .dialog_shell import AppDialog

DUR_PRESETS = (2, 4, 6, 10)
BURST_COUNTS = (4, 8, 16, 24)


class GifDialog(AppDialog):
    sig_export = pyqtSignal(dict)        # {video,start,dur,width,fps,sub}
    sig_cancel = pyqtSignal()            # 取消当前那趟导出
    sig_burst = pyqtSignal(int, int)     # 张数, 间隔毫秒
    sig_reveal = pyqtSignal(str)         # 打开这个文件所在目录

    def __init__(self, cfg: Any, state_of: Callable[[], dict], parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.state_of = state_of
        self.job_id = 0
        self.last_path = ""
        self._owner_shortcuts: list = []   # 见 install_owner_keys：主窗键位的本窗镜像
        self.setWindowTitle("截图工坊")
        # 不能叫 #Panel：那个 id 在侧栏里是"透明、无边框"的连续表面。
        # 顶层对话框用同一个 id 时，QSS 的 id 选择器会压过 QDialog 的背板，
        # 整扇窗只剩控件、没有底（离屏截图里就是一圈透明像素）。
        self.setObjectName("GifDialog")
        self.setMinimumWidth(440)
        self._build()
        self.refresh()
        self.adjustSize()

    # ================= 搭界面 =================
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(10)

        head = QVBoxLayout()
        head.setSpacing(2)
        title = QLabel("导出 GIF 片段", self)
        title.setObjectName("DialogTitle")
        sub = QLabel(f"从当前片的播放位置截一段导出（最长 {gif.MAX_DURATION:g} 秒），"
                     "或按间隔连拍 PNG。", self)
        sub.setObjectName("DialogHint")
        sub.setWordWrap(True)
        head.addWidget(title)
        head.addWidget(sub)
        lay.addLayout(head)

        # 三行两列：标签一列、控件一列；行尾的按钮和预设各归各的，不再挤在一条
        # 由控件宽度决定位置的横排里（旧版「用 A-B 段」会随起点框的宽度左右漂）。
        form = QGridLayout()
        form.setContentsMargins(0, 4, 0, 0)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(9)
        form.setColumnMinimumWidth(0, 44)
        form.setColumnStretch(1, 1)

        label = QLabel("起点", self)
        label.setObjectName("RowLabel")
        form.addWidget(label, 0, 0)
        self.spin_start = QDoubleSpinBox(self)
        self.spin_start.setRange(0.0, 359_999.0)
        self.spin_start.setDecimals(2)
        self.spin_start.setSuffix(" 秒")
        self.spin_start.setFocusPolicy(Qt.NoFocus)
        form.addWidget(self.spin_start, 0, 1)
        acts = QHBoxLayout()
        acts.setSpacing(6)
        self.btn_here = QPushButton("用当前位置", self)
        self.btn_here.setFocusPolicy(Qt.NoFocus)
        self.btn_here.clicked.connect(self._use_pos)
        acts.addWidget(self.btn_here)
        self.btn_ab = QPushButton("A-B 段", self)
        self.btn_ab.setFocusPolicy(Qt.NoFocus)
        self.btn_ab.setToolTip("用时间轴上的 A-B 循环区间作为导出区间")
        self.btn_ab.clicked.connect(self._use_ab)
        acts.addWidget(self.btn_ab)
        form.addLayout(acts, 0, 2)

        label = QLabel("时长", self)
        label.setObjectName("RowLabel")
        form.addWidget(label, 1, 0)
        dur_box = QWidget(self)
        dur = QHBoxLayout(dur_box)
        dur.setContentsMargins(0, 0, 0, 0)
        dur.setSpacing(6)
        self.spin_dur = QDoubleSpinBox(dur_box)
        self.spin_dur.setRange(gif.MIN_DURATION, gif.MAX_DURATION)
        self.spin_dur.setDecimals(2)
        self.spin_dur.setSingleStep(0.5)
        self.spin_dur.setSuffix(" 秒")
        self.spin_dur.setValue(float(self.cfg.get("gif_duration", gif.DEFAULT_DURATION)))
        self.spin_dur.setFocusPolicy(Qt.NoFocus)
        self.spin_dur.valueChanged.connect(lambda _v: self._repaint_note())
        dur.addWidget(self.spin_dur, 1)
        for s in DUR_PRESETS:
            b = QPushButton(str(s), dur_box)
            b.setObjectName("PageAction")
            b.setFocusPolicy(Qt.NoFocus)
            b.setFixedWidth(38)
            b.setToolTip(f"{s} 秒")
            b.clicked.connect(lambda _c=False, v=s: self.spin_dur.setValue(float(v)))
            dur.addWidget(b)
        form.addWidget(dur_box, 1, 1, 1, 2)

        label = QLabel("画面", self)
        label.setObjectName("RowLabel")
        form.addWidget(label, 2, 0)
        pic = QWidget(self)
        pich = QHBoxLayout(pic)
        pich.setContentsMargins(0, 0, 0, 0)
        pich.setSpacing(8)
        self.combo_w = QComboBox(pic)
        for w in gif.WIDTHS:
            self.combo_w.addItem(f"{w} px", w)
        self.combo_w.setCurrentIndex(max(0, list(gif.WIDTHS).index(
            int(self.cfg.get("gif_width", gif.DEFAULT_WIDTH)))
            if int(self.cfg.get("gif_width", gif.DEFAULT_WIDTH)) in gif.WIDTHS else 1))
        self.combo_w.setFocusPolicy(Qt.NoFocus)
        pich.addWidget(self.combo_w, 1)
        fps = QLabel("帧率", pic)
        fps.setObjectName("InlineLabel")
        pich.addWidget(fps)
        self.combo_f = QComboBox(pic)
        for f in gif.FPS_LADDER:
            self.combo_f.addItem(f"{f} fps", f)
        self.combo_f.setCurrentIndex(max(0, list(gif.FPS_LADDER).index(
            int(self.cfg.get("gif_fps", gif.DEFAULT_FPS)))
            if int(self.cfg.get("gif_fps", gif.DEFAULT_FPS)) in gif.FPS_LADDER else 1))
        self.combo_f.setFocusPolicy(Qt.NoFocus)
        pich.addWidget(self.combo_f, 1)
        form.addWidget(pic, 2, 1, 1, 2)
        lay.addLayout(form)

        self.chk_sub = QCheckBox("烧进字幕（仅外挂字幕文件）", self)
        self.chk_sub.setFocusPolicy(Qt.NoFocus)
        self.chk_sub.toggled.connect(lambda _v: self._repaint_note())
        lay.addWidget(self.chk_sub)

        self.lbl_range = QLabel("", self)
        self.lbl_range.setObjectName("TimeCode")
        self.lbl_range.setWordWrap(True)
        lay.addWidget(self.lbl_range)
        self.lbl_sub = QLabel("", self)
        self.lbl_sub.setObjectName("DialogHint")
        self.lbl_sub.setWordWrap(True)
        lay.addWidget(self.lbl_sub)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_open = QPushButton("打开目录", self)
        self.btn_open.setObjectName("PageAction")
        self.btn_open.setFocusPolicy(Qt.NoFocus)
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._reveal)
        self.btn_stop = QPushButton("取消", self)
        self.btn_stop.setFocusPolicy(Qt.NoFocus)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._cancel)
        self.btn_go = QPushButton("导出 GIF", self)
        self.btn_go.setObjectName("Primary")
        self.btn_go.setFocusPolicy(Qt.NoFocus)
        self.btn_go.clicked.connect(self._export)
        # 主出口靠右，和设置页/快捷键页同一个方向；"打开目录"是结果出来后才用得到的
        # 次动作，压成小号描边，不和"取消/导出"争同一档视觉重量。
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_stop)
        row.addStretch(1)
        row.addWidget(self.btn_go)
        lay.addLayout(row)

        self.bar = QProgressBar(self)
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(3)
        # 空闲时它只是一条躺在本该留白的轨道；没有任务就不占那一行。
        self.bar.setVisible(False)
        lay.addWidget(self.bar)

        self.lbl_state = QLabel("", self)
        self.lbl_state.setObjectName("TimeCode")
        self.lbl_state.setWordWrap(True)
        # 空闲时这一行是空的，却仍然占着一条字高 + 上下间距。藏起来，等真有
        # "正在导出/已导出/失败"要说时再出现（_set_state 一处收口）。
        self.lbl_state.setVisible(False)
        lay.addWidget(self.lbl_state)

        burst = QLabel("连拍", self)
        burst.setObjectName("SectionTitle")
        lay.addWidget(burst)

        b1 = QHBoxLayout()
        b1.setSpacing(8)
        n_lab = QLabel("张数", self)
        n_lab.setObjectName("RowLabel")
        b1.addWidget(n_lab)
        self.spin_n = QSpinBox(self)
        self.spin_n.setRange(2, 60)
        self.spin_n.setValue(int(self.cfg.get("burst_count", 8)))
        self.spin_n.setSuffix(" 张")
        self.spin_n.setFocusPolicy(Qt.NoFocus)
        b1.addWidget(self.spin_n)
        gap_lab = QLabel("间隔", self)
        gap_lab.setObjectName("RowLabel")
        b1.addWidget(gap_lab)
        self.spin_gap = QSpinBox(self)
        self.spin_gap.setRange(50, 3000)
        self.spin_gap.setSingleStep(50)
        self.spin_gap.setValue(int(self.cfg.get("burst_gap_ms", 250)))
        self.spin_gap.setSuffix(" 毫秒")
        self.spin_gap.setFocusPolicy(Qt.NoFocus)
        b1.addWidget(self.spin_gap)
        b1.addStretch(1)
        self.btn_burst = QPushButton("开始连拍", self)
        self.btn_burst.setFocusPolicy(Qt.NoFocus)
        self.btn_burst.clicked.connect(self._burst)
        b1.addWidget(self.btn_burst)
        lay.addLayout(b1)

        self.lbl_burst = QLabel("按当前速度连续截帧，每图存一张 PNG", self)
        self.lbl_burst.setObjectName("DialogHint")
        lay.addWidget(self.lbl_burst)

    def install_owner_keys(self, rows, sink) -> int:
        """把播放器的键位镜像到**本窗口**上，返回装了几条。

        为什么需要这一份镜像：`QShortcut(context=WindowShortcut)` 只认当前活动窗口，
        所以这个不模态的面板一成为活动窗，主窗那 60 多条键就整体收不到。真机实测
        （tests/probe_gif3.py B1–B4）就是这个形状：工坊没开时按 B 命中 `['burst_shot']`，
        工坊开着时同一把键命中 `[]`，把主窗抢回活动才又响。而用户最自然的用法恰恰是
        **开着工坊放片子**——按空格暂停、按 B 连拍，都不该先点一下播放器。

        为什么是「调用方把行递进来 + 命中后交回它的 sink」而不是本对话框自己去读键位表：
        本文件的第一条纪律是不持有引擎、不解释动作含义。键位事实只有主窗那一份
        （`self.key_rows`），筛掉哪几把由 `keymap.mirrorable()` 定（Esc/Return/Enter 留给
        这个窗口自己：关掉它、触发默认按钮），这里只负责把它们挂到本窗口上再原样交回
        `sink(action)`。装两次不是叠两份——旧的一组先摘干净，一次按键只响一次。
        """
        for sc in getattr(self, "_owner_shortcuts", None) or []:
            sc.setEnabled(False)
            sc.setParent(None)
            sc.deleteLater()
        self._owner_shortcuts = []
        if not callable(sink):
            return 0                      # 没有落点的镜像＝按下去就在槽里抛，那是 rc=127 的形状
        for seq, act, _desc in keymap.mirrorable(rows):
            sc = QShortcut(QKeySequence(str(seq)), self, context=Qt.WindowShortcut)
            sc.activated.connect(lambda checked=False, a=str(act): sink(a))
            self._owner_shortcuts.append(sc)
        return len(self._owner_shortcuts)

    # ================= 状态 → 界面 =================
    def refresh(self) -> None:
        """每次打开/每次片子换了都要重刷：起点、A-B 可不可用、字幕能不能烧。"""
        st = self._state()
        self.spin_start.setValue(float(st.get("pos") or 0.0))
        a, b = st.get("ab") or (None, None)
        self.btn_ab.setEnabled(bool(a is not None and b is not None))
        sub = str(st.get("sub") or "")
        self.chk_sub.setEnabled(bool(sub))
        if sub:
            # 上次烧过字幕就接着烧（这个键由 _persist 写，是"真被读回来"的键，不是摆设）
            self.chk_sub.setChecked(bool(self.cfg.get("gif_include_sub")))
        else:
            self.chk_sub.setChecked(False)
        self._repaint_note()

    def _state(self) -> dict:
        """外部回调给什么，这里就收成"界面敢用的形状"，再把异常降级成一条错误文本。

        为什么要在门口收类型：这个字典的另一头是主窗口的 `gif_state()`（内核缓存），
        今天它给的是 float/str/tuple，明天换内核或多一条早退分支就可能给 None、"abc"、
        一个字符串的 ab。而**槽里一个 ValueError 不是 traceback，是进程当场没了**
        （PyQt5 实测：rc=127、stderr 全空）。所以宁可在这里多写十行，也不让界面去猜。
        """
        try:
            st = self.state_of()
        except Exception as exc:                 # 取状态失败只该让界面停在旧值上，不该带走进程
            return {"error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(st, dict):
            return {}
        out: dict[str, Any] = {}
        for k in ("video", "sub"):
            out[k] = str(st.get(k) or "")
        for k in ("pos", "duration"):
            try:
                out[k] = float(st.get(k) or 0.0)
            except (TypeError, ValueError):
                out[k] = 0.0
        ab = st.get("ab")
        try:
            a, b = (list(ab)[:2] + [None, None])[:2] if isinstance(ab, (list, tuple)) \
                else (None, None)
            out["ab"] = (float(a), float(b))
        except (TypeError, ValueError):
            out["ab"] = (None, None)
        if "error" in st:
            out["error"] = st["error"]
        return out

    def _repaint_note(self) -> None:
        """两行说明各管各的：一行讲字幕，一行讲区间。导出状态写在下面的 lbl_state，
        这里绝不往它身上盖字——盖掉之后用户就看不见上一条结果是成是败了。"""
        st = self._state()
        sub = str(st.get("sub") or "")
        if sub:
            self.lbl_sub.setText(f"字幕：{os.path.basename(sub)}")
        elif not self.chk_sub.isEnabled():
            self.lbl_sub.setText("当前没有外挂字幕文件；内嵌字幕轨烧不进去（要先解成文件才行）")
        else:
            self.lbl_sub.setText("")
        dur = float(st.get("duration") or 0.0)
        start = float(self.spin_start.value())
        if dur > 0:
            self.spin_dur.setMaximum(max(gif.MIN_DURATION,
                                         min(gif.MAX_DURATION, dur - start)))
            avail = dur - start
            self.lbl_range.setText(
                (f"起点离片尾只剩 {avail:.1f} 秒，导不出这一段" if avail < gif.MIN_DURATION
                 else f"还剩 {avail:.1f} 秒可导"))
        else:
            # 时长未知（流、或内核还没报上来）：不猜上界，只说清楚没夹
            self.spin_dur.setMaximum(gif.MAX_DURATION)
            self.lbl_range.setText("时长未知：区间按界面填写值导出")
            if "error" in st:
                self.lbl_range.setText(f"读当前播放状态失败：{st['error']}")

    # ================= 收集 =================
    def spec(self) -> dict:
        """界面上的数 → 一份导出参数。区间已经用 gif.clamp_range 夹过，
        所以这里给出的 start/dur 与真发出去的完全一致。"""
        st = self._state()
        dur_total = float(st.get("duration") or 0.0)
        w = self.combo_w.currentData()
        f = self.combo_f.currentData()
        start, dur = gif.clamp_range(float(self.spin_start.value()),
                                     float(self.spin_dur.value()), dur_total)
        return {"video": str(st.get("video") or ""), "start": start, "dur": dur,
                "width": int(w) if w else gif.DEFAULT_WIDTH,
                "fps": int(f) if f else gif.DEFAULT_FPS,
                "sub": str(st.get("sub") or "") if self.chk_sub.isChecked() else ""}

    # ================= 动作 =================
    def _use_pos(self) -> None:
        st = self._state()
        self.spin_start.setValue(float(st.get("pos") or 0.0))
        self._repaint_note()

    def _set_state(self, text: str) -> None:
        text = str(text or "")
        self.lbl_state.setText(text)
        self.lbl_state.setVisible(bool(text.strip()))

    def _use_ab(self) -> None:
        st = self._state()
        a, b = (st.get("ab") or (None, None))[:2]
        if a is None or b is None:
            return
        lo, hi = sorted((float(a), float(b)))
        self.spin_start.setValue(lo)
        span = max(gif.MIN_DURATION, min(gif.MAX_DURATION, hi - lo))
        self._repaint_note()
        self.spin_dur.setValue(span)

    def _export(self) -> None:
        sp = self.spec()
        if not sp["video"]:
            self._set_state("没有正在播放的文件，导不出片段")
            return
        self._persist(sp)
        self.job_id += 1
        self.sig_export.emit(dict(sp))
        self.bar.setValue(0)
        self.bar.setVisible(True)
        self.btn_stop.setEnabled(True)
        self.btn_go.setEnabled(False)
        self._set_state("正在导出…")

    def _cancel(self) -> None:
        self.sig_cancel.emit()

    def _burst(self) -> None:
        n, gap = int(self.spin_n.value()), int(self.spin_gap.value())
        self.cfg["burst_count"] = n
        self.cfg["burst_gap_ms"] = gap
        self.cfg.save()
        self.sig_burst.emit(n, gap)

    def _reveal(self) -> None:
        if self.last_path:
            self.sig_reveal.emit(self.last_path)

    def _persist(self, sp: dict) -> None:
        """界面上调好的规格就是下次的默认值：存了才谈得上"上次那个宽度"。
        存完要 save，不然关掉播放器这些数又回到出厂值。

        键名以 spec() 发出去的那份为准（width/fps，不是 gif_width/gif_fps）：这里曾经
        按配置键名去 sp 里取值，结果每次点"导出 GIF"都是 KeyError——而它发生在 Qt 槽里，
        表现不是 traceback 是**进程当场没了**（rc=127、stderr 全空）。写"界面值→配置键"
        这类搬运时，两头都从同一个字典取键名，别凭记忆拼。
        """
        self.cfg["gif_width"] = sp["width"]
        self.cfg["gif_fps"] = sp["fps"]
        self.cfg["gif_duration"] = sp["dur"]
        self.cfg["gif_include_sub"] = bool(sp["sub"])
        self.cfg.save()

    # ================= 回流（进度/结果）=================
    def on_progress(self, _jid: int, sec: float, total: float) -> None:
        """槽里不抛：类型兜住再算，除零兜住再除。"""
        try:
            frac = 0.0 if float(total) <= 0 else max(0.0, min(1.0, float(sec) / float(total)))
            self.bar.setValue(int(frac * 1000))
            self._set_state(f"正在导出… {sec:.1f} / {total:.1f} 秒")
        except (TypeError, ValueError):
            pass

    def on_done(self, _jid: int, ok: bool, detail: str, path: str) -> None:
        self.btn_go.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.bar.setValue(1000 if ok else 0)
        self.last_path = str(path or "")
        self.btn_open.setEnabled(bool(self.last_path))
        self._set_state(("已导出：" + os.path.basename(self.last_path) + " —— "
                                if ok else "导出失败：") + str(detail or ""))
