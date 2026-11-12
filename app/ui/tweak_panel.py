"""调节面板：字幕样式 / 画面均衡 / HDR / 声音（侧栏第三页）。

一条铁律：这里每个控件画的是**内核回报的值**，不是"我发出去的值"。
差别只在两种情况下显形：越界（滑块能拖到 130，但命令在发出前就被 tweaks 表夹成 100
——实测直接 set 130 时内核是整条拒收、回报停在旧值，指望内核替我们夹会换来"界面动了、
画面没动"）和不支持的枚举（这一版 mpv 不认 butterlight）。界面若照自己发出的值画，
用户看到的是"设了但没生效"，而我们排查时一点线索都拿不到。所以每次"一次动作"
（松手、勾选、选下拉、按按钮）都走 `player.set_tweak(..., confirm=True)`，
拿返回值重画那一个控件。

拖动途中不 confirm：那会给每次 valueChanged 都补一次同步往返，UI 线程等 mpv 应答，
滑块手感变成一格一顿。所以只有 `isSliderDown()` 为假时才确认。

控件一律 `setFocusPolicy(Qt.NoFocus)`：主窗口的方向键是窗口级快捷键（←/→ 进退 5 秒），
焦点落在滑块或下拉上时那两条会同时生效——"按一下跳 5 秒，同时字幕延迟 +0.1"。
音量条早就这么处理过，这里同理。

滑轨那一行（标题+数值+QSlider）在 `form_rows.NumRow`：画面页（`transform_panel`）
的充满/平移/锐化/降噪/去带用的是同一根轨，量纲换算（float+step ↔ 整数格）抄两份
就会漂，所以只有那一份。（本节标题叫"画面"指的是均衡器那五项；几何与增强在下一页，
两页的措辞分开是为了不让用户以为一个按钮管着两件事。）
"""
from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ..engine import tweaks
from ..engine.player import MpvPlayer
from . import scroll_feel
from .form_rows import (KeyHintLabel, NumRow, add_check, add_choice, align_num_values,
                        bind_key_tip, mk_hint, mk_label as _label, paint_check, paint_choice)
from .scrubber import fmt_time

DEVICE_HINT = "（播放内核未启动）"
DESC_MAX = 46          # 设备描述里那句 "Automatically selected audio API..." 会很长


class TweakPanel(QWidget):
    sig_status = pyqtSignal(str)

    def __init__(self, player: MpvPlayer, cfg, parent=None):
        super().__init__(parent)
        self.p = player
        self.cfg = cfg
        self.setObjectName("Panel")
        self.setMinimumWidth(240)
        self._rows: dict[str, NumRow] = {}
        self._choices: dict[str, QComboBox] = {}
        self._flags: dict[str, QCheckBox] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 10, 10)
        outer.setSpacing(6)
        outer.addWidget(_label("调节", self, "PanelTitle"))

        # 侧栏只有 300px 宽，而这一页有二十多行：不装进滚动区，下面的 A-B 与声音
        # 设备就会被挤出可视范围，看起来像"功能没做"。
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("TweakScroll")
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self._body = QWidget(self)
        self._form = QVBoxLayout(self._body)
        self._form.setContentsMargins(0, 0, 6, 0)
        self._form.setSpacing(8)
        self.scroll.setWidget(self._body)
        outer.addWidget(self.scroll, 1)
        self.scroll_feel = scroll_feel.attach(self.scroll)

        self._build_sub()
        self._build_video()
        self._build_motion()
        self._build_hdr()
        self._build_audio()
        align_num_values(self._rows.values())
        self._form.addStretch(1)

        # 侧栏只有 300px 宽：这一行长了会被右边裁掉（实测第一版就被裁成"画面复"），
        # 每节自己的快捷键提示放在各节里，这里只留最常用的两组。
        # 键名**不在这里抄**（`KeyHintLabel`）：那两把键是键位表的事实，而表用户可以改（Ctrl+K），
        # 抄死的写法在改键之后还写着旧键，等于在最该可信的那一行上撒谎。
        outer.addWidget(KeyHintLabel(self.cfg, (
            (("sub_delay_down", "sub_delay_up"), "字幕延迟"),
            (("sub_size_down", "sub_size_up"), "大小"),
        ), self))

        self.p.sig_info.connect(self._on_info)
        self.repaint_from_kernel()

    # ================= 构建 =================
    def _section(self, title: str) -> QVBoxLayout:
        self._form.addWidget(_label(title, self._body, "SectionTitle"))
        box = QVBoxLayout()
        box.setSpacing(6)
        self._form.addLayout(box)
        return box

    def _add_num(self, box: QVBoxLayout, spec: tweaks.Num) -> NumRow:
        row = NumRow(spec, self._body)
        row.sig_change.connect(self._on_num_change)
        box.addWidget(row)
        self._rows[spec.prop] = row
        return row

    def _add_choice(self, box: QVBoxLayout, item: tweaks.Choice) -> QComboBox:
        # 行的搭法与"重画时值不在预设里怎么办"都在 form_rows：画面页的下拉是同一套规矩，
        # 两份代码就会有两份"activated 还是 currentIndexChanged"的选择。
        cb = add_choice(box, item, self._body, lambda c, it=item: self._on_choice(it, c))
        self._choices[item.prop] = cb
        return cb

    def _add_flag(self, box: QVBoxLayout, item: tweaks.Flag) -> QCheckBox:
        # 三个勾选各占一行：横向挤在 300px 侧栏里会截字，而"粗体/可见/避开边框"
        # 是三个独立开关，缩成一行反而看不出哪个是哪个。
        c = add_check(box, item, self._body, lambda on, it=item: self._on_flag(it, on))
        self._flags[item.prop] = c
        return c

    def _build_sub(self) -> None:
        box = self._section("字幕")
        for spec in tweaks.SUB_NUM:
            self._add_num(box, spec)
        # 三个勾选各占一行：横向挤在 300px 侧栏里会截字，而"粗体/可见/避开边框"
        # 是三个独立开关，缩成一行反而看不出哪个是哪个。
        for item in tweaks.SUB_FLAG:
            self._add_flag(box, item)
        self._add_choice(box, tweaks.SUB_ALIGN_X)
        self._add_choice(box, tweaks.SUB_ALIGN_Y)
        self._add_choice(box, tweaks.SUB_COLOR)
        self._add_choice(box, tweaks.SUB_BACK_COLOR)

    def _build_video(self) -> None:
        # 这一节叫"画面均衡"（亮度/对比度/…五项），不叫"画面"：下一页（transform_panel）
        # 整页就叫「画面」，管的是比例/旋转/充满/平移与增强。两页各留一个"画面"标题，
        # 用户在侧栏里看到的就是两个都像"调画面"的地方，而它们下发的东西完全不同
        # （那边是 GPU 属性，这边是 eq 滤镜）。
        box = self._section("画面均衡")
        for spec in tweaks.EQ:
            self._add_num(box, spec)
        self.btn_reset = QPushButton("画面复位", self._body)
        self.btn_reset.setFocusPolicy(Qt.NoFocus)
        # 键名那半句从生效表算，不在这里抄：抄死的「（Shift+G）」在用户把这把键改掉之后
        # 会继续教人按一把不响的键，而悬停提示正是"忘了按哪个键"的人才去读的地方。
        bind_key_tip(self.btn_reset, self.cfg,
                     "亮度/对比度/灰度/饱和度/色相全部回 0", ("eq_reset",))
        self.btn_reset.clicked.connect(self._on_reset_eq)
        box.addWidget(self.btn_reset)

    def _build_motion(self) -> None:
        # 四个控件都是**热切**属性（本机实测 set_property 立刻生效、subscribe 也有回报），
        # 所以归这一页、不归设置页那张"重启后生效"的表。插帧与同步方式的耦合在这里
        # 各管一个方向：开插帧把同步从 audio 抬到 display-resample；把同步改回 audio
        # 则关掉插帧——组合失效时不声不响，等于让用户对着一个"开着却不生效"的开关。
        box = self._section("画面平滑")
        self._add_choice(box, tweaks.MOTION_SYNC)
        self._add_flag(box, tweaks.INTERPOLATION)
        self._add_choice(box, tweaks.TSCALE)
        self._add_choice(box, tweaks.DEINTERLACE)
        box.addWidget(mk_hint("插帧把 24fps 片补到显示器刷新率，要配合「跟随显示器」同步才有效。"
                              "开销中高：风扇变响或画面发飘时先关它。", self._body))

    def _build_hdr(self) -> None:
        box = self._section("HDR / 色调")
        self._add_choice(box, tweaks.TONE_MAPPING)
        self._add_choice(box, tweaks.GAMUT_MAPPING)
        self._add_choice(box, tweaks.TARGET_PEAK)
        box.addWidget(KeyHintLabel(self.cfg, ((("tone_cycle",), "换色调映射档位"),), self._body))

    def _build_audio(self) -> None:
        box = self._section("声音")
        self._add_num(box, tweaks.AUDIO_NUM[0])
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(_label("输出设备", self._body))
        self.cb_device = QComboBox(self._body)
        # 见 `form_rows.add_choice`：字数档而不是像素档。这一行是「标签+下拉+刷新」三件，
        # 原来钉 156px 时整行的最小需求是 274px，正好比侧栏视口宽 6px 掉出去。
        self.cb_device.setMinimumContentsLength(12)
        self.cb_device.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.cb_device.setFocusPolicy(Qt.NoFocus)
        self.cb_device.activated.connect(self._on_device)
        row.addWidget(self.cb_device, 1)
        self.btn_dev = QPushButton("刷新", self._body)
        self.btn_dev.setFocusPolicy(Qt.NoFocus)
        self.btn_dev.setToolTip("重新向内核询问设备列表（插了耳机/切了输出后点一下）")
        self.btn_dev.clicked.connect(self.refresh_devices)
        row.addWidget(self.btn_dev)
        box.addLayout(row)

        ab = QHBoxLayout()
        ab.setSpacing(6)
        self.btn_ab_a = QPushButton("设 A", self._body)
        self.btn_ab_b = QPushButton("设 B", self._body)
        self.btn_ab_c = QPushButton("清除", self._body)
        for b in (self.btn_ab_a, self.btn_ab_b, self.btn_ab_c):
            b.setFocusPolicy(Qt.NoFocus)
            ab.addWidget(b)
        box.addLayout(ab)
        self.btn_ab_a.clicked.connect(lambda: self._on_ab("a"))
        self.btn_ab_b.clicked.connect(lambda: self._on_ab("b"))
        self.btn_ab_c.clicked.connect(lambda: self._on_ab("clear"))
        # 「A-B：未设置」是状态读数不是行标题（同上：行标题走正文色，读数留在暗灰档）
        self.lbl_ab = _label("A-B：未设置", self._body, "Status")
        box.addWidget(self.lbl_ab)
        box.addWidget(KeyHintLabel(self.cfg, (
            (("ab_a", "ab_b"), "设点"),
            (("ab_clear",), "清除"),
        ), self._body))

    # ================= 值来源 =================
    def _current(self, item: Any) -> Any:
        """当前该显示什么：优先内核订阅缓存，没起内核时退回配置。

        为什么优先内核：缓存是内核推来的真值。配置文件可能是上一次退出时的快照，
        老版本里还留过 6 位颜色这种内核不认的写法——照配置画就等于把脏值再发回去。
        """
        v = self.p.prop(item.prop, None)
        if v is None or (isinstance(v, str) and v == ""):
            v = self.cfg.get(item.cfg, item.default)
        return v

    def repaint_from_kernel(self) -> None:
        for prop, row in self._rows.items():
            row.show(self._current(row.spec))
        for prop, cb in self._choices.items():
            self._paint_choice(prop, self._current(tweaks.spec(prop)))
        for prop, c in self._flags.items():
            self._paint_flag(prop, self._current(tweaks.spec(prop)))
        self._sync_motion_rows()
        self._paint_ab()

    def page_shown(self) -> None:
        """主窗口切到这一页时调用：补齐需要内核的东西（设备列表）并按真值重画。"""
        self.refresh_devices()
        self.repaint_from_kernel()
        live = self.p.running
        for b in (self.btn_ab_a, self.btn_ab_b):
            b.setEnabled(live)
        self.btn_ab_c.setEnabled(live)

    # ================= 事件：控件 → 内核 =================
    def _on_num_change(self, prop: str, value: float, commit: bool) -> None:
        spec = tweaks.spec(prop)
        if spec is None:
            return
        live = self.p.running
        actual, ok = self.p.set_tweak(prop, value, confirm=commit and live)
        if not commit:
            return                                  # 拖动途中：只写值，不等内核
        row = self._rows.get(prop)
        # 用返回值重画：越界时它是被内核（或表）夹过的值，滑块会自己弹回能到的位置
        if row is not None and actual is not None and not row.slider.isSliderDown():
            row.show(actual)
        if not live:
            self.sig_status.emit(f"{spec.label} {spec.text(actual)}（已记入设置，播放后生效）")
        elif ok:
            self.sig_status.emit(f"{spec.label} {spec.text(actual)}")
        else:
            self.sig_status.emit(f"{spec.label}：内核只接受 {spec.text(actual)}")

    def _sync_motion_rows(self) -> None:
        """插帧关着时把"插帧混合"置灰：那一档只在两帧之间补帧时才有意义，
        留着可点等于让用户以为自己在调一个对画面无效的参数。"""
        on = bool(self._current(tweaks.INTERPOLATION))
        cb = self._choices.get("tscale")
        if cb is not None:
            cb.setEnabled(on)
            cb.setToolTip("" if on else "先打开上面的「插帧」；关着时这一档不影响画面")

    def _on_choice(self, item: tweaks.Choice, cb: QComboBox) -> None:
        val = cb.currentData()
        live = self.p.running
        actual, ok = self.p.set_tweak(item.prop, val, confirm=live)
        shown = actual if actual is not None else val
        self._paint_choice(item.prop, shown)
        extra = ""
        if item is tweaks.MOTION_SYNC and str(shown) == "audio" \
                and bool(self._current(tweaks.INTERPOLATION)):
            # 跟着音频时钟时内核不会在帧间补帧：插帧开着只是一枚假开关。
            self.p.set_tweak(tweaks.INTERPOLATION.prop, False, confirm=live)
            self._paint_flag(tweaks.INTERPOLATION.prop, False)
            self._sync_motion_rows()
            extra = "；跟随音频时插帧无效，已关掉插帧"
        if not live:
            self.sig_status.emit(f"{item.label} {tweaks.option_text(item, shown)}（播放后生效）{extra}")
        elif ok:
            self.sig_status.emit(f"{item.label} {tweaks.option_text(item, shown)}{extra}")
        else:
            self.sig_status.emit(f"{item.label}：内核不支持 {val}")

    def _on_flag(self, item: tweaks.Flag, on: bool) -> None:
        live = self.p.running
        actual, _ok = self.p.set_tweak(item.prop, on, confirm=live)
        shown = on if actual is None else bool(actual)
        c = self._flags.get(item.prop)
        if c is not None:
            c.blockSignals(True)
            c.setChecked(shown)
            c.blockSignals(False)
        extra = ""
        if item is tweaks.INTERPOLATION:
            if shown and str(self._current(tweaks.MOTION_SYNC)) == "audio":
                new, _ok2 = self.p.set_tweak(tweaks.MOTION_SYNC.prop, "display-resample",
                                             confirm=live)
                sync_shown = new if new is not None else "display-resample"
                self._paint_choice(tweaks.MOTION_SYNC.prop, sync_shown)
                extra = "；同步方式已切到跟随显示器"
            self._sync_motion_rows()
        self.sig_status.emit(f"{item.label} {'开' if shown else '关'}{extra}"
                             + ("" if live else "（播放后生效）"))

    def _on_reset_eq(self) -> None:
        if not self.p.running:
            # 不起内核时不能走 reset_video_eq：它每项都要向内核确认，而那会连着
            # 产生五条"请求 X 但内核回报 None"的日志，OSD 会被噪声刷满。
            # 持久化仍交给 set_tweak(confirm=False)，别在这里自己摸 cfg 的键。
            for spec in tweaks.EQ:
                self.p.set_tweak(spec.prop, spec.default, confirm=False)
                self._rows[spec.prop].show(spec.default)
            self.sig_status.emit("画面已复位（播放后生效）")
            return
        out = self.p.reset_video_eq()
        for prop, v in out.items():
            row = self._rows.get(prop)
            if row is not None:
                row.show(v)
        self.sig_status.emit("画面已复位")

    def _on_device(self, idx: int) -> None:
        name = self.cb_device.itemData(idx)
        if not name:
            return
        actual = self.p.set_audio_device(str(name))
        self._paint_device(actual)
        self.sig_status.emit("输出设备 " + (self.cb_device.currentText() or str(actual)))

    def _on_ab(self, which: str) -> None:
        if not self.p.running:
            self.sig_status.emit("播放内核还没启动")
            return
        if which == "clear":
            self.p.clear_ab()
            self._paint_ab()
            self.sig_status.emit("已清除 A-B")
            return
        # 先记下"当前在哪"：set_ab 用的就是缓存里这个位置，而缓存要到内核推回来才更新，
        # 设完立刻读 ab_range 拿到的还是旧值——拿它当回报会写出"A 点已设（0:00→0:00）"。
        pos = float(self.p.prop("time-pos", 0.0) or 0.0)
        self.p.set_ab(which)
        rng = self.p.ab_range()
        if rng and which == "b":
            self.sig_status.emit(f"A-B 循环 {fmt_time(rng[0])} → {fmt_time(rng[1])}")
        else:
            self.sig_status.emit(f"{'A 点' if which == 'a' else 'B 点'} = {fmt_time(pos)}")
        self._paint_ab()

    # ================= 事件：内核 → 控件 =================
    def _on_info(self, info: dict) -> None:
        """订阅缓存里任何一项变了就只重画那一项（不发请求、不整页重排）。"""
        for key, val in (info or {}).items():
            row = self._rows.get(key)
            if row is not None:
                if row.slider.isSliderDown() or val is None:
                    continue          # 用户正拖着这根，别把他的手抢走；空值也不当 0 画
                row.show(val)
                continue
            if key in self._choices:
                self._paint_choice(key, val)
                continue
            if key in self._flags:
                self._paint_flag(key, val)
                if key == tweaks.INTERPOLATION.prop:
                    self._sync_motion_rows()
                continue
            if key in ("ab-loop-a", "ab-loop-b"):
                self._paint_ab()
            elif key == "audio-device":
                self._paint_device(val)

    def _paint_flag(self, prop: str, val: Any) -> None:
        # 空值不当"关"、画之前先 blockSignals 这两条都在 form_rows.paint_check：
        # 画面页的镜像/保持比例走的是同一套规矩，两份代码就会有两份"照 None 画"的写法。
        paint_check(self._flags.get(prop), val)

    def _paint_choice(self, prop: str, val: Any) -> None:
        cb = self._choices.get(prop)
        if cb is None:
            return
        # 普通枚举也要归一：内核把 deinterlace 的 no/yes 回报成 JSON 布尔，
        # 直接 str(False) 会让下拉多出一项「当前 False」。choice_value 把布尔折回
        # yes/no，字符串原样返回，所以这一个是所有枚举的通用 norm。
        norm = tweaks.norm_color if "color" in prop else tweaks.choice_value
        paint_choice(cb, val, norm)

    def _paint_device(self, val: Any) -> None:
        name = str(val or "auto")
        idx = self.cb_device.findData(name)
        if idx < 0:
            self.cb_device.addItem(name, name)
            idx = self.cb_device.count() - 1
        self.cb_device.blockSignals(True)
        self.cb_device.setCurrentIndex(idx)
        self.cb_device.blockSignals(False)

    def _paint_ab(self) -> None:
        rng = self.p.ab_range()
        self.lbl_ab.setText("A-B：未设置" if rng is None
                           else f"A-B：{fmt_time(rng[0])} → {fmt_time(rng[1])}")

    # ================= 快捷键入口 =================
    # 主窗口的快捷键全部转发到这里，和点控件走同一段代码：一份逻辑才不会出现
    # "按钮改了内核没改"或"键盘的 OSD 文案和面板对不上"。
    def nudge(self, prop: str, delta: float) -> None:
        spec = tweaks.spec(prop)
        if not isinstance(spec, tweaks.Num):
            return
        actual, ok = self.p.nudge_tweak(prop, delta)
        if actual is None:
            self.sig_status.emit(f"{spec.label}：内核没回报值")
            return
        row = self._rows.get(prop)
        if row is not None:
            row.show(actual)
        self.sig_status.emit(f"{spec.label} {spec.text(actual)}" if ok
                             else f"{spec.label}：内核只接受 {spec.text(actual)}")

    def step(self, prop: str, forward: bool = True) -> None:
        """按表里定义的步长走一格（快捷键用，别在调用方写死 0.05）。"""
        spec = tweaks.spec(prop)
        if not isinstance(spec, tweaks.Num):
            return
        self.nudge(prop, spec.step if forward else -spec.step)

    def cycle_choice(self, prop: str) -> None:
        item = tweaks.spec(prop)
        if not isinstance(item, tweaks.Choice):
            return
        actual, ok = self.p.cycle_choice(prop)
        shown = actual if actual is not None else item.default
        self._paint_choice(prop, shown)
        label = f"{item.label} {tweaks.option_text(item, shown)}"
        self.sig_status.emit(label if ok else f"{item.label}：内核回报 {shown}")

    def reset_eq(self) -> None:
        self._on_reset_eq()

    def mark_ab(self, which: str) -> None:
        self._on_ab(which)

    def mark_device(self, name: str) -> None:
        """设备菜单选完之后，让面板上的下拉和内核说的一致。"""
        self._paint_device(str(name or "auto"))

    def refresh_devices(self) -> None:
        """向内核问一次设备清单（同步请求，只在开页/点刷新时做，不放订阅里）。

        为什么值得这一次往返：`audio-device-list` 不是订阅属性，插了耳机之后没有事件
        推上来；而它变动频率极低，为它开轮询比在 UI 线程上等一次应答贵得多。
        """
        cur = str(self.p.prop("audio-device", self.cfg["audio_device"]) or "auto")
        self.cb_device.blockSignals(True)
        self.cb_device.clear()
        if not self.p.running:
            self.cb_device.addItem(DEVICE_HINT, None)
            self.cb_device.setEnabled(False)
            self.cb_device.blockSignals(False)
            return
        self.cb_device.setEnabled(True)
        try:
            rows = self.p.audio_devices()
        except Exception:
            rows = []
        for r in rows or []:
            name = str(r.get("name") or "")
            if not name:
                continue
            desc = str(r.get("description") or name)
            self.cb_device.addItem((desc[:DESC_MAX] + "…") if len(desc) > DESC_MAX else desc,
                                   name)
        idx = self.cb_device.findData(cur)
        if idx < 0:
            self.cb_device.addItem(f"当前 {cur}", cur)
            idx = self.cb_device.count() - 1
        self.cb_device.setCurrentIndex(idx)
        self.cb_device.blockSignals(False)
