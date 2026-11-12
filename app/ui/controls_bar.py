"""控制条：时间轴 + Transport 按钮 + 音量 + 状态标签。

它是"遥控面板"：按钮直接调 player，快捷键也统一走这里的方法（一份逻辑，
按钮与键盘表现必然一致）。需要实时数据才能填的菜单（音轨/字幕/更多）不在这里造，
只发 sig_menu 让主窗口去弹——菜单要读 track-list，而 track-list 的真相在内核。
"""
from __future__ import annotations

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QLayout, QSizePolicy, QSlider,
                             QSpacerItem, QToolButton, QVBoxLayout, QWidget)

from ..engine.player import MpvPlayer
from . import form_rows, icons, theme
from .scrubber import Scrubber, fmt_time

M = theme.M
SPEEDS = [0.25, 0.5, 0.75, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0]
LOOP_MODES = ["no", "inf", "one"]      # 列表循环 → 单曲循环 → 不循环
LOOP_ICONS = {"no": "loop_off", "inf": "repeat", "one": "repeat_one"}
LOOP_TIPS = {"no": "不循环", "inf": "列表循环", "one": "单曲循环"}


class ControlsBar(QWidget):
    sig_menu = pyqtSignal(str, QWidget)      # (kind, 锚点按钮)
    sig_status = pyqtSignal(str)            # 需要 OSD 冒泡的话
    sig_toggle_playlist = pyqtSignal()
    sig_toggle_library = pyqtSignal()
    sig_toggle_tweak = pyqtSignal()
    sig_toggle_fullscreen = pyqtSignal()

    def __init__(self, player: MpvPlayer, cfg, parent=None):
        super().__init__(parent)
        self.p = player
        self.cfg = cfg
        self.setObjectName("ControlsBar")
        self._fs = False
        self._dragging = False
        self._loop_idx = 0
        self._mute_synced = False
        # 悬停提示里"跟着状态变的那半句"（说法）：**内核推下来之前是 None**，
        # 那几个按钮这时说中性话（"播放/暂停""静音""循环模式""倍速 1x"），
        # 不猜一个状态出来。键名那半句不在这里，它由 `form_rows.bind_key_tip` 现算。
        self._paused: bool | None = None
        # 还没有任何媒体时也是"闲"的：内核此时根本没启动，不会推 idle 事件，
        # 所以初值必须是 True——否则空状态那颗主按钮画的是暂停（点下去什么也不会发生）。
        self._idle = True
        self._muted: bool | None = None
        self._speed: float = 1.0
        self._loop_state: str | None = None
        # 窄窗紧凑档（见 `minimumSizeHint` / `_apply_compact`）：标准档实测最小宽
        # 约 1100px（18 颗按钮 + 组距），而外壳答应能缩到 880px。中间那段不能靠布局硬压
        # 固定按钮（压不动就裁剪），只能主动把次级件收小一档。所有按钮都还在，只是更紧。
        self._compact = False
        self._tool_buttons: list[QToolButton] = []
        self._icon_name: dict[QToolButton, str] = {}
        self._icon_base: dict[QToolButton, int] = {}
        self._gaps: list[QSpacerItem] = []
        self._gap_w = 12          # 组与组之间的呼吸：紧凑档收到 0（见 _apply_compact）

        root = QVBoxLayout(self)
        self._root = root
        root.setContentsMargins(M.gutter, 4, M.gutter, 6)
        root.setSpacing(4)
        # 不让布局把自己的 1033px 当成**父窗口**的硬下限：外壳的最小宽是 880，而标准档
        # 的按钮和是 1033。硬下限在布局里时，窗口拖到 1033 就停住，`resizeEvent` 里那套
        # 紧凑档永远不会被触发（实测：请求 880，实际停在 1033）。松掉约束之后，
        # `minimumSizeHint` 给的 880 才作数，紧凑档由 resizeEvent 落地。
        root.setSizeConstraint(QLayout.SetNoConstraint)

        self.scrub = Scrubber(self)
        self.scrub.sig_seek.connect(self._on_seek)
        self.scrub.sig_drag_state.connect(self._on_drag_state)
        root.addWidget(self.scrub)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self._row = row
        root.addLayout(row)

        # --- 左簇：transport ---
        # 括号里那半句（键名）不在这些字面量里：它们由 `form_rows.bind_key_tip` 按**生效键位表**
        # 现算。原先这里抄了 17 句，2026-10-12 静态扫出来三句已经和表不一致（「PgDn」表里写的是
        # PgDown、「Shift+CS」这把键表里根本没有、「Z / M」实际绑的是 Z 和 Shift+,），
        # 而第四种坏法（用户改了键、悬停还在说旧键）扫都扫不出来。
        self.btn_play = self._tool(row, "pause", self._play_saying, self.toggle_play,
                                   ("toggle_play",), name="BigPlay", px=22)
        self.btn_prev = self._tool(row, "prev", "上一个", self.prev, ("prev",))
        self.btn_next = self._tool(row, "next", "下一个", self.next, ("next",))
        self._gap(row)
        # 停止没有快捷键（表里就没这一行），所以括号那半句是空的——写「（Shift+CS）」
        # 不是"给它标个键"，是让用户按一把什么都不干的键。
        self.btn_stop = self._tool(row, "stop", "停止", self.stop)
        self._gap(row)
        self.btn_back = self._tool(row, "step_back", "上一帧", lambda: self.step(True),
                                   ("step_back",))
        self.btn_fwd = self._tool(row, "step_fwd", "下一帧", lambda: self.step(False),
                                  ("step_fwd",))
        self.btn_speed = self._tool(row, "speed", self._speed_saying,
                                    lambda: self.sig_menu.emit("speed", self.btn_speed),
                                    ("speed_down", "speed_up"))
        self.lbl_speed = QLabel("1x", self)
        self.lbl_speed.setObjectName("Chip")
        # 对齐标记不是装饰：不加 Qt.AlignVCenter 时布局会把标签**拉满整行高度**，
        # 于是这个只有两个字的小药丸画成一块 40px 高的深灰方块（药丸变成了砖）。
        row.addWidget(self.lbl_speed, 0, Qt.AlignVCenter)
        # 倍速药丸和时间码之间必须留出一段：这一行的行距是 2px（按钮要紧凑成"一簇"），
        # 但药丸自己有 8px 内边距，2px 行距会让"1x"的右边直接顶到"00:00"的数字上——
        # 两个不同用途的东西贴在一起，读的人得分不清倍速是时间的一部分。
        # 紧凑档收到 4px：880 宽时整行刚好差这 8px，而时间码自己一个字都不能少。
        self._speed_time_gap = QSpacerItem(12, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
        row.addItem(self._speed_time_gap)
        self.lbl_time = QLabel("00:00 / 00:00", self)
        self.lbl_time.setObjectName("TimeCode")
        # 这一格是**读数**，不是可压缩的弹性件：紧凑档把组距压到 0 之后仍要留出
        # 小时级时间码（`0:00:00 / 0:00:00`）的宽，否则布局会悄悄把后半截裁掉。
        # 但要封顶 140：真机 12px 下小时级只要 126px（880 宽时这一格实际能拿到约 164px），
        # 而 offscreen 的回退字体量出 211px——把它写进布局下限，整条控制条的最小宽
        # 会被顶到 927px，窗口就下不到承诺的 880 了（tests/test_ui_polish.py 量过）。
        _cur = self.lbl_time.text()
        self.lbl_time.setText("0:00:00 / 0:00:00")
        self.lbl_time.ensurePolished()
        self.lbl_time.setMinimumWidth(min(self.lbl_time.sizeHint().width(), 140))
        self.lbl_time.setText(_cur)
        row.addWidget(self.lbl_time)

        row.addStretch(1)

        # --- 右簇：音量、轨道、模式、视图 ---
        self.btn_volume = self._tool(row, "volume_high", self._mute_saying,
                                     self.toggle_mute, ("mute",))
        self.vol = QSlider(Qt.Horizontal, self)
        # 量程起点按配置，真值由内核的 volume-max 订阅来收（_on_info）：
        # 设置页可以把上限改成 110/300，写死 200 就会出现"拖得动、内核不收"的那段。
        self.vol.setRange(0, max(0, int(cfg["volume_max"])))
        self.vol.setFixedWidth(96)
        # 和 form_rows.NumRow 同一个下限：把手是一颗 14 的圆，控件比它矮就会被上下裁平。
        self.vol.setMinimumHeight(theme.SLIDER_HANDLE)
        self.vol.setFocusPolicy(Qt.NoFocus)   # 同上：焦点在音量条上时方向键会双份生效
        self.vol.valueChanged.connect(self._on_volume_slider)
        row.addWidget(self.vol)
        self._gap(row)

        self.btn_sub = self._tool(row, "subs", "字幕轨",
                                  lambda: self.sig_menu.emit("sub", self.btn_sub),
                                  ("menu_sub",))
        self.btn_audio = self._tool(row, "audio", "音轨",
                                    lambda: self.sig_menu.emit("audio", self.btn_audio),
                                    ("menu_audio",))
        self._gap(row)
        self.btn_loop = self._tool(row, "loop_off", self._loop_saying, self.cycle_loop,
                                   ("loop",), checkable=True)
        self.btn_shuffle = self._tool(row, "shuffle", "乱序", self.toggle_shuffle,
                                      ("shuffle",), checkable=True)
        self._gap(row)
        self.btn_shot = self._tool(row, "camera", "截图存帧", self.screenshot,
                                   ("screenshot",))
        self.btn_tweak = self._tool(row, "sliders", "调节：字幕/画面/HDR/声音",
                                    lambda: self.sig_toggle_tweak.emit(),
                                    ("toggle_tweak",), checkable=True)
        self.btn_library = self._tool(row, "folder", "媒体库",
                                      lambda: self.sig_toggle_library.emit(),
                                      ("toggle_library",), checkable=True)
        self.btn_list = self._tool(row, "playlist", "播放列表",
                                   lambda: self.sig_toggle_playlist.emit(),
                                   ("toggle_playlist",), checkable=True)
        self._gap(row)
        # 一颗按钮说**两件事**（进去一把键、出来另一把），所以分成两段各配各的动作：
        # 合成一句就得在这儿再 parse 一次"哪几个字是键名"，而那正是本轮要消灭的写法。
        self.btn_fs = self._tool(row, "fullscreen", None,
                                 lambda: self.sig_toggle_fullscreen.emit(),
                                 parts=(("全屏", ("fullscreen",)),
                                        ("& 退出", ("exit_fullscreen",))))
        self.btn_more = self._tool(row, "more", "更多",
                                   lambda: self.sig_menu.emit("more", self.btn_more))

        # 内核状态 → 界面：单向订阅，界面不去"猜"内核状态
        self.p.sig_time.connect(self._on_time)
        self.p.sig_duration.connect(self._on_duration)
        self.p.sig_buffered.connect(self.scrub.set_buffered)
        self.p.sig_chapters.connect(self.scrub.set_chapters)
        self.p.sig_pause.connect(self._on_pause)
        self.p.sig_volume.connect(self._on_volume)
        self.p.sig_mute.connect(self._on_mute)
        self.p.sig_speed.connect(self._on_speed)
        self.p.sig_info.connect(self._on_info)
        self.p.sig_idle.connect(self._on_idle)
        # 内核还没启动时不会有任何 paused/idle 事件，初态得自己画一次（否则是 ⏸）。
        self._paint_play_icon()
        theme.on_accent(self._refresh_accent)

    def _refresh_accent(self) -> None:
        """主色换了：勾选态图标与倍速胶囊的颜色是主色派生值，得当场重画。

        图标缓存按颜色串分键，旧配色的位图不会自己失效；不重画的话，
        换了主题色，循环/列表/乱序那几颗仍是上一套绿。
        """
        for b in self._tool_buttons:
            self._repaint_icon(b)
        self._on_speed(self._speed)

    # ---------- 构建辅助 ----------
    def _gap(self, row: QHBoxLayout) -> QSpacerItem:
        """组间呼吸。紧凑档里必须能收成 0——它占的是"可以省的宽度"，不是固定装饰。"""
        sp = QSpacerItem(self._gap_w, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
        self._gaps.append(sp)
        row.addItem(sp)
        return sp

    def _icon_color(self, btn: QToolButton) -> str:
        # 构造期 `self.btn_play` 还没接住返回值，所以认 objectName 而不是认对象。
        if btn.objectName() == "BigPlay":
            return theme.PRIMARY_INK
        if btn.isCheckable() and btn.isChecked():
            return theme.ACCENT_TEXT
        return theme.TEXT_DIM

    def _hover_color(self, btn: QToolButton) -> str | None:
        return None if btn.objectName() == "BigPlay" else theme.TEXT

    def _repaint_icon(self, btn: QToolButton) -> None:
        name = self._icon_name.get(btn, "play")
        size = self._icon_size(btn)
        btn.setIcon(icons.icon(name, size, self._icon_color(btn),
                               self._hover_color(btn)))
        btn.setIconSize(icons.size_hint(size))

    def _tool(self, row: QHBoxLayout, icon: str, label, slot, actions: tuple = (),
              name="ToolBtn", px=20, checkable: bool = False,
              parts: tuple | None = None) -> QToolButton:
        """造一颗控制条按钮。提示语分两半：**说法在这儿，键名不在**。

        `label` 可以是字符串，也可以是**无参函数**（播放/静音/倍速/循环那四颗跟着状态变）。
        `actions` 是这颗按钮归哪几把键管，交给 `form_rows.bind_key_tip` 去生效表里查键名；
        一句提示要分几段说时用 `parts`（这时 `label` 给 None，别给空串——空串看着像"忘了写"）。
        """
        b = QToolButton(self)
        b.setObjectName(name)
        b.setAutoRaise(True)
        # 表示"开关态"的按钮必须显式 checkable：QToolButton 默认不可勾选，
        # 而不可勾选时 setChecked(True) 是**静默无效**的（isChecked 一直 False，
        # QSS 的 :checked 规则也永远不生效）。循环/乱序/列表三个按钮踩过这条。
        b.setCheckable(checkable)
        self._tool_buttons.append(b)
        self._icon_name[b] = str(icon)
        self._icon_base[b] = int(px)
        side = M.big_play if name == "BigPlay" else M.btn
        b.setFixedSize(side, side)
        if checkable:
            b.toggled.connect(lambda _on, btn=b: self._repaint_icon(btn))
        self._repaint_icon(b)
        if parts is None:
            if label is None:
                # 这里宁可炸在构造期，也不把它兜成"提示语是空串"：那颗按钮从此只说键名，
                # 而 Qt 的悬停提示是**看不见的地方**，验收和眼睛都不容易发现它变哑了。
                raise ValueError("_tool 的 label 不能为 None（要分几段说请用 parts）")
            form_rows.bind_key_tip(b, self.cfg, label, actions)
        else:
            form_rows.bind_key_tip_parts(b, self.cfg, parts)
        b.setCursor(Qt.PointingHandCursor)
        # 不抢键盘焦点：焦点停在播放按钮上时，空格会同时触发按钮和 QShortcut，
        # 于是"暂停→立即恢复"看起来像按了没反应。按钮只认鼠标，键盘走快捷键表。
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(slot)
        row.addWidget(b)
        return b

    def _set_icon(self, btn: QToolButton, name: str, px: int = 20) -> None:
        """换图标时记住**名字与标准档尺寸**：紧凑档切回来时按名字重画，不靠调用方再喊一次。"""
        self._icon_name[btn] = str(name)
        self._icon_base[btn] = int(px)
        self._repaint_icon(btn)

    def _icon_size(self, btn: QToolButton) -> int:
        base = self._icon_base.get(btn, 20)
        # BigPlay 的圆形底不跟着窗口缩，里面的三角/双竖条也不缩——它是一行里唯一的主操作，
        # 20px 的三角落在 48px 的白圆里会显得比次级图标还弱。
        if self._compact and btn is not self.btn_play:
            return max(16, base - 2)
        return base

    def _paint_play_icon(self) -> None:
        """主按钮画**下一步能做什么**：闲/停/暂停都是 ▶，只有正在播才是 ⏸。

        不能只听 `_on_pause` 那一头：空窗口里内核还没启动，一条 paused 都不会来，
        而按钮从构造起就挂着 ⏸；等第一支片子载入后内核才推 paused=False，中间这段时间
        屏幕上就对用户说了谎。idle 与 paused 两个事实合起来才够判这一颗。
        """
        playing = (not self._idle) and self._paused is not True
        self._set_icon(self.btn_play, "pause" if playing else "play", 22)

    def _apply_compact(self, on: bool) -> None:
        """窄窗：次级按钮 36→30、图标 −2px、音量条 96→72、组距 12→0、倍速-时间码间距 12→4。

        为什么不是简单藏几颗：藏掉的按钮在 880 宽的窗口里就**没有入口**了（快捷键
        还在，但鼠标用户找不到）。收小一档能保住全部功能，且 30px 仍在可点范围内。
        BigPlay 不缩：它是这一行唯一的主操作，视觉层级不该随窗口宽度变。
        """
        on = bool(on)
        if on == self._compact:
            return
        if getattr(self, "vol", None) is None or getattr(self, "btn_play", None) is None:
            return          # 构造期的 resize 事件：控件还没造完，等下一拍
        self._compact = on
        for b in self._tool_buttons:
            if b is self.btn_play:
                continue
            side = M.btn_s if on else M.btn
            b.setFixedSize(side, side)
            self._repaint_icon(b)
        gap = 0 if on else self._gap_w
        for sp in self._gaps:
            sp.changeSize(gap, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
        self._row.invalidate()
        self.vol.setFixedWidth(72 if on else 96)
        self._row.setSpacing(0 if on else 2)
        self._speed_time_gap.changeSize(4 if on else 12, 0, QSizePolicy.Fixed, QSizePolicy.Minimum)
        m = 14 if on else M.gutter
        self._root.setContentsMargins(m, 4, m, 6)
        # `QSpacerItem.changeSize` 只改 hint，不会自己把已经量好的几何作废；少了这一拍，
        # 紧凑档的组距仍按 12px 占位，时间码被挤成「00:00 / 0(」（960 宽实测 50px vs 97px）。
        self._row.activate()
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:
        """把布局自报的标准档宽度收到外壳允许的最小宽以内。

        外壳（chrome/main_window）承诺最小 880；布局那边是 18 颗固定按钮 + 组距的和，
        真实最小宽约 1100。两个数不接上的后果不是"布局自己会缩"，而是**窗口根本拖不到
        880**，或者在外力改几何时右边一截被裁掉。这里给 880 的松值，真正怎么排由
        `resizeEvent` 切紧凑档；紧凑档实测最小宽约 860，仍在 880 以内。
        高度照布局原样给——紧凑档只动横向，不动这条 84px 的视觉高度。
        """
        native = super().minimumSizeHint()
        return QSize(min(int(native.width()), 880), int(native.height()))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # 40px 回差：标准档约 1100、紧凑档≈860，在切线上来回 1px 会让用户拖动窗口时
        # 按钮尺寸反复跳。进紧凑要早于"真的放不下"，退要等宽度真回来。
        if not self._compact and ev.size().width() < 1140:
            self._apply_compact(True)
        elif self._compact and ev.size().width() > 1180:
            self._apply_compact(False)

    # ---------- 动作（快捷键也调这些） ----------
    def toggle_play(self) -> None:
        self.p.toggle_pause()

    def play(self) -> None:
        self.p.play()

    def stop(self) -> None:
        self.p.stop()
        self.scrub.set_state(0, self.scrub._dur)

    def next(self) -> None:
        self.p.next()

    def prev(self) -> None:
        self.p.prev()

    def step(self, backward: bool = False) -> None:
        self.p.frame_step(backward)
        self.sig_status.emit("上一帧" if backward else "下一帧")

    def cycle_speed(self, forward: bool = True) -> None:
        nxt = self.p.cycle_speed(SPEEDS, forward)
        if nxt is not None:
            self.sig_status.emit(f"倍速 {nxt:g}x")

    def screenshot(self) -> None:
        self.p.snapshot(bool(self.cfg["screenshot_include_sub"]))

    def set_volume(self, v: int) -> None:
        self.p.set_volume(v)
        self.cfg.save()

    def adjust_volume(self, delta: int) -> None:
        self.p.adjust_volume(delta)
        nxt = max(0, min(self.p.volume_max(), int(self.p.prop("volume", 100)) + delta))
        self.sig_status.emit(f"音量 {nxt}%")

    def toggle_mute(self) -> None:
        self.p.toggle_mute()   # OSD 文案交给 _on_mute：状态以内核回报为准，别自己猜

    def cycle_loop(self) -> None:
        mode = LOOP_MODES[(self._loop_idx + 1) % 3]
        if mode == "no":
            self.p.set_loop("no")
            self.p.set_loop_file("no")
        elif mode == "inf":
            self.p.set_loop("inf")
            self.p.set_loop_file("no")
        else:
            self.p.set_loop("no")
            self.p.set_loop_file("inf")
        # 先按意图画：属性是"发出去—内核回推"两段式，立刻读缓存读到的还是旧值；
        # 内核随后推来的 loop-playlist/loop-file 会在 _on_info 里把这里再对齐一次
        # （菜单和快捷键走的是另一条入口，靠那次对齐保证按钮不会和内核各说各话）。
        self._paint_loop(mode)
        self.sig_status.emit(LOOP_TIPS[mode])

    def _paint_loop(self, mode: str) -> None:
        self._loop_idx = LOOP_MODES.index(mode)
        self._set_icon(self.btn_loop, LOOP_ICONS[mode])
        self.btn_loop.setChecked(mode != "no")
        # 只改状态再重画：那句「循环：单曲循环（L）」里，说法在这（`_loop_saying`），
        # 键名由 `form_rows` 现取——原来这里抄了一遍「（L）」，改键位之后就说旧键。
        self._loop_state = mode
        form_rows.paint_key_tip(self.btn_loop)

    # ---------- 悬停提示的"说法"那一半（键名那半句不在这里） ----------
    def _play_saying(self) -> str:
        if self._paused is None:
            return "播放/暂停"           # 内核还没推 paused，不猜一个状态出来
        # **动作口径**，不是状态口径：按下去会干什么，悬停就说什么。三处同族都是这个口径——
        # 图标 `"play" if paused else "pause"`（见 `_on_pause`：暂停时给的是▶那颗，
        # 也就是"点我开播"）、`_mute_saying`（静音时给「取消静音」）、以及右键菜单里
        # 那句 `("暂停" if not paused else "播放", "toggle_play")`（`main_window`）。
        # 2026-10-13 之前这一行写反了（暂停时说「暂停」），而它的自测尺 `U58 ③` 的金牌
        # 就是 `paint_key_tip` 本身——用同一个反了的映射去验反了的映射，验不出方向；
        # 真正把它照出来的是验收里那句"暂停之后提示该说播放"。
        return "播放" if self._paused else "暂停"

    def _mute_saying(self) -> str:
        return "取消静音" if self._muted else "静音"

    def _loop_saying(self) -> str:
        if self._loop_state is None:
            return "循环模式"
        return f"循环：{LOOP_TIPS[self._loop_state]}"

    def _speed_saying(self) -> str:
        return f"倍速 {self._speed:g}x"

    def _loop_from_kernel(self) -> str:
        """内核那两个属性 → 档位。单曲优先判，因为"单曲循环"时 loop-playlist 也是 no。"""
        if str(self.p.prop("loop-file", "no")) == "inf":
            return "one"
        if str(self.p.prop("loop-playlist", "no")) in ("inf", "yes"):
            return "inf"
        return "no"

    def toggle_shuffle(self) -> None:
        on = not bool(self.p.prop("shuffle", False))
        self.p.set_shuffle(on)
        self.sig_status.emit("乱序播放" if on else "顺序播放")

    def set_fullscreen(self, on: bool) -> None:
        self._fs = on
        self._set_icon(self.btn_fs, "fullscreen_exit" if on else "fullscreen")

    # ---------- 内核 → 界面 ----------
    def _on_time(self, t: float) -> None:
        if not self._dragging:
            self.scrub.set_state(t, self.scrub._dur)
            self.lbl_time.setText(f"{fmt_time(t)} / {fmt_time(self.scrub._dur)}")

    def _on_duration(self, d: float) -> None:
        self.scrub.set_state(self.scrub._pos, d)
        self.lbl_time.setText(f"{fmt_time(self.scrub._pos)} / {fmt_time(d)}")
        # 换片之后 A-B 两点可能还挂在缓存里（内核跨文件保留它们，见 E19），
        # 而新的时长可能比旧的 B 还短——时间轴按新时长重画一次，别让 B 跑到轨道外。
        self.refresh_ab_marks()

    def _on_drag_state(self, dragging: bool) -> None:
        self._dragging = dragging

    def _on_seek(self, sec: float, exact: bool) -> None:
        if exact:
            self.p.seek_abs(sec)
        else:
            self.p.seek_abs(sec)      # 拖动中同样用绝对定位：相对 seek 会累积成飘
        self.lbl_time.setText(f"{fmt_time(sec)} / {fmt_time(self.scrub._dur)}")

    def _on_pause(self, paused: bool) -> None:
        self._paused = bool(paused)
        self._paint_play_icon()
        form_rows.paint_key_tip(self.btn_play)

    def _on_volume(self, v: int) -> None:
        if self.vol.value() != v:
            self.vol.blockSignals(True)
            self.vol.setValue(v)
            self.vol.blockSignals(False)
        self._repaint_volume_icon()

    def _on_mute(self, muted: bool) -> None:
        self._repaint_volume_icon()
        self._muted = bool(muted)
        form_rows.paint_key_tip(self.btn_volume)
        # 订阅建立时内核会把当前值推下来一次，那次不是用户动作，别冒 OSD
        if self._mute_synced:
            self.sig_status.emit("静音" if muted else "取消静音")
        self._mute_synced = True

    def _repaint_volume_icon(self) -> None:
        v = int(self.p.prop("volume", 100) or 0)
        name = "volume_mute" if (bool(self.p.prop("mute", False)) or v == 0) else \
            ("volume_low" if v < 55 else "volume_high")
        self._set_icon(self.btn_volume, name)

    def _on_speed(self, s: float) -> None:
        self._speed = float(s)
        form_rows.paint_key_tip(self.btn_speed)
        self.lbl_speed.setText(f"{self._speed:g}x")
        # ACCENT_TEXT 而不是 ACCENT：这颗 chip 是**字**，字要落在 BG_HOVER 上读，
        # 六档主色里有三档（蓝/紫/橙）原色铺在这个底上不够对比（体检那一轮量出来的）。
        self.lbl_speed.setStyleSheet(
            f"color: {theme.ACCENT_TEXT if abs(self._speed - 1.0) > 1e-3 else theme.TEXT_DIM};")

    def _on_info(self, info: dict) -> None:
        if "shuffle" in info:
            self.btn_shuffle.setChecked(bool(info["shuffle"]))
        if "volume-max" in info:
            # 上限以内核回报为准：启动参数写着 200，但万一这版内核自己夹得更低，
            # 滑块就该停在内核真能到的位置（player.volume_max() 同一条口径）。
            mx = self.p.volume_max()
            if mx > 0 and self.vol.maximum() != mx:
                self.vol.setRange(0, mx)
        if "loop-playlist" in info or "loop-file" in info:
            mode = self._loop_from_kernel()
            if LOOP_MODES[self._loop_idx] != mode:
                self._paint_loop(mode)
        if "ab-loop-a" in info or "ab-loop-b" in info:
            self.refresh_ab_marks()

    def refresh_ab_marks(self) -> None:
        """把内核里的 A/B 两点交给时间轴（未设的一端是 None，不是 0）。"""
        a, b = self.p.ab_marks()
        self.scrub.set_loop_range(a, b)

    def _on_idle(self, idle: bool) -> None:
        self._idle = bool(idle)
        self._paint_play_icon()
        en = not idle
        # 空闲（没载片）时只有"对这一条片子动手"的入口该灰：传输、逐帧、截图、
        # 倍速、音轨、字幕。音量/循环/乱序/视图开关仍可点——它们调节的是播放器
        # 本身，空窗口里也该能先设好。
        for b in (self.btn_play, self.btn_stop, self.btn_back, self.btn_fwd, self.btn_shot,
                  self.btn_speed, self.btn_sub, self.btn_audio, self.lbl_speed):
            b.setEnabled(en)

    def _on_volume_slider(self, v: int) -> None:
        self.p.set_volume(v)
        self.cfg["volume"] = v
        self._repaint_volume_icon()
