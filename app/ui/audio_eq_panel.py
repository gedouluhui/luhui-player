"""均衡器页：十段 + 前置增益（侧栏第五页，统计让到第六页）。

这一页和第三页（`tweak_panel`）、第四页（`transform_panel`）长得很像——都是滑轨加状态行，
但**下发的形状与坏值的下场**都不同，所以第三份事实表与第三块面板各走各的：
  - 第三页那五项是 GPU 侧 `eq` 属性，一次一条 `set_property`，越界内核自己夹；
  - 第四页是一条 `vf` 滤镜链，坏值的下场是"画面不动，而列表照显示"；
  - 这一页是一条 `af` 滤镜链，坏值的下场是**整个进程一声不出**（本机实测越界增益、
    不存在的滤镜名、空图 `lavfi=[]` 三档的产物字节数都是 `-1`，内核原话
    `Audio filter initialized failed!`）。所以这一页的滑轨量程（±12 dB）由
    `engine/equalizer.py` 那张表夹死，界面上出现不了 999。

取值来源为什么只能"问一次、反解"：`af` 是列表属性，内核推回来的不是逐档数字，而是
`[{'name':'lavfi','params':{'graph':'equalizer=f=31:t=h:w=13:g=12'}}]` 这样一串
（本机 ⑥ 那一腿读回的原始形状）。面板上那十个数因此全部来自 `equalizer.parse_entries()`
的反解结果，而不是"我发出去的那串"。

四条写在界面上的实测结论（`tests/out/eq-probe-r15b.log`，腿号按那份日志的编号）：
  1. **归零＝不挂滤镜**（③/⑥）：全 0 时链是空串，内核读回 `[]`，声音就是原片；
     面板不会写着"0 dB"却偷偷挂十一条滤镜。
  2. **热切真的能听见**（④a，按实时节奏放那条管线）：播到中段挂 bass+12，低频带
     48.4→59.2 dB（+10.8）；十段全 +12 时九个测量带各 +7.7…+11.7。所以拖动松手就下发
     是有效的，不需要"重开片子才生效"那种话。
  3. **抬太高会削顶**（⑦）：十段全 +12 不带前置时产物 `peak=1.000 clip=657`；
     同一串后面加 `volume=-9dB` 变成 `clip=0`。所以预设自带负前置，而用户自己拖的前置
     只在状态行上提醒"净抬升"，不替他改（替他改就是替他做决定）。
  4. **前置必须带 `dB`**（第 6 条）：`volume=-12`（没后缀）是线性乘 12，实测 +17.9 dB 并爆音。
     拼串与反解两处都按这一条办事，反解遇到不带后缀的条目时算"读不懂"并原话报出来。

和第三、四页同样的两条纪律：拖动途中不 confirm（否则每个 valueChanged 都补一次同步往返，
UI 线程等 mpv 应答，手感变成一格一顿）；控件一律 NoFocus（方向键是窗口级快捷键）。

内核没起来时**照样能调**：这些值全部进 `equalizer.startup_args`，播放时一次带上，
所以这里报"已记入设置，播放后生效"，而不是"播放内核还没启动"。
"""
from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QPushButton, QScrollArea,
                             QVBoxLayout, QWidget)

from ..engine import equalizer
from ..engine.player import MpvPlayer
from . import scroll_feel, theme
from .form_rows import (NumRow, align_num_values, bind_key_tip, mk_hint,
                        mk_label as _label, paint_choice)

# 状态行按事实换色，色号不在这儿抄第二份（同 `transform_panel` 那段理由）：
# 本模块的事实只有"哪种状态用哪一档"，"那一档是什么色"归 theme 的令牌表。
OK_TINT = theme.OK
WARN_TINT = theme.DANGER_TEXT
DIM_TINT = theme.TEXT_DIM

# 下拉里那一条"自定义"是**界面自己造的一档**，不在 `equalizer.PRESETS` 里：
# 它不代表一串值，只代表"当前这十档不等于任何一条预设"。写成常量是因为
# `_on_preset` 要认出它并**不当成一次动作**（点到它就是看着自己拖出来的结果）。
CUSTOM = "custom"


def _f(v: Any) -> float:
    """任意值 → float，认不出来回 0。

    这里的取值有三处出处都可能塞进字符串：内核读回反解的 dict、config.json（用户可以拿
    编辑器改）、表里的默认值。而每个调用点都在 Qt 槽的调用链上——槽里抛 ValueError
    不是 traceback，是进程当场没了（本机实测 rc=127、stderr 全空）。收口必须发生在取值那一道。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x and x not in (float("inf"), float("-inf")) else 0.0


class AudioEqPanel(QWidget):
    sig_status = pyqtSignal(str)

    def __init__(self, player: MpvPlayer, cfg, parent=None):
        super().__init__(parent)
        self.p = player
        self.cfg = cfg
        self.setObjectName("Panel")
        self.setMinimumWidth(240)
        # 按**配置键**索引（十段的 prop 与 cfg 同名，前置也是）：面板只管"这一档是谁"，
        # 拼串、夹紧、反解全在 engine/equalizer.py 那一份事实里。
        self._rows: dict[str, NumRow] = {}
        # 上一次内核读回的东西（逐档 dB / 读不懂之处 / 失败行）。三份一起存，
        # 因为"列表好看但没声音"这件事只有紧邻那一段日志能证（见 `_paint_chain`）。
        self._vals: dict[str, float] = {}
        self._problems: list[str] = []
        self._fails: list[str] = []
        self._asked = False       # 这一页问过 `af` 没有（问过之后才敢拿缓存当内核的话）

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 10, 10)
        outer.setSpacing(6)
        outer.addWidget(_label("声音均衡", self, "PanelTitle"))

        # 侧栏最窄 260 上下，而这一页有十二根滑轨＋预设＋复位＋状态行：不装滚动区，
        # 前置增益和状态行会被挤出可视范围，看起来像"功能没做"（同调节页那条理由）。
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("AudioEqScroll")
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

        self._build_preset()
        self._build_bands()
        align_num_values(self._rows.values())
        self._build_actions()
        self._form.addWidget(mk_hint(
            "十段全 0＝不挂滤镜，声音就是原片。量程 ±12 dB 是外壳的口味线（内核允许 ±900），"
            "抬太高会削顶：预设会自己往下让前置，手动把净抬升拖过 +3 dB 时状态行会提醒。",
            self._body))
        self._form.addStretch(1)

        self.p.sig_log.connect(self._on_engine_log)
        self.repaint_from_state()

    # ================= 构建 =================
    def _section(self, title: str) -> QVBoxLayout:
        self._form.addWidget(_label(title, self._body, "SectionTitle"))
        box = QVBoxLayout()
        box.setSpacing(6)
        self._form.addLayout(box)
        return box

    def _build_preset(self) -> None:
        box = self._section("预设")
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(_label("音色", self._body))
        row.addStretch(1)
        self.cb_preset = QComboBox(self._body)
        # 字数档而不是像素档（同调节页设备那一行）：钉像素宽在字号 1.3 档会把整行顶出视口。
        self.cb_preset.setMinimumContentsLength(10)
        self.cb_preset.setFocusPolicy(Qt.NoFocus)
        for pr in equalizer.PRESETS:
            self.cb_preset.addItem(pr.label, pr.key)
        self.cb_preset.addItem("自定义", CUSTOM)
        # `activated` 而不是 `currentIndexChanged`（form_rows 那条规矩 1）：
        # 重画下拉时后者也会回调回来，等于界面自己给自己下命令。
        self.cb_preset.activated.connect(self._on_preset)
        row.addWidget(self.cb_preset)
        box.addLayout(row)
        # 选中那一条的说明（也是这一页的换行说明行之一）
        self.lbl_preset = mk_hint("", self._body)
        box.addWidget(self.lbl_preset)

    def _build_bands(self) -> None:
        box = self._section("十段")
        for b in equalizer.BANDS:
            self._add_num(box, b.spec)
        self._add_num(box, equalizer.PREAMP)

    def _add_num(self, box: QVBoxLayout, spec) -> NumRow:
        # 数值胶囊写 "+3 dB / 0 dB / -3 dB"（`equalizer.value_text`），不写 "+3.00"：
        # 这一族的单位只有 dB，而"零点是不是 0"要看一眼就知道。
        row = NumRow(spec, self._body, fmt=lambda v, k=spec.cfg: equalizer.value_text(k, v))
        row.sig_change.connect(self._on_num_change)
        box.addWidget(row)
        self._rows[spec.cfg] = row
        return row

    def _build_actions(self) -> None:
        box = self._section("动作")
        self.btn_reset = QPushButton("全部归零", self._body)
        self.btn_reset.setFocusPolicy(Qt.NoFocus)
        # 键名不在这里抄：这一页没有专属快捷键（动作组给空表，`tip_note` 归成空串），
        # 而将来给它绑键时只该改键位表，不该回来改这句提示。
        bind_key_tip(self.btn_reset, self.cfg, "十段与前置全部回 0（＝摘掉滤镜）", ())
        self.btn_reset.clicked.connect(self.reset_all)
        box.addWidget(self.btn_reset)
        self.lbl_chain = _label("", self._body, "Status")
        self.lbl_chain.setWordWrap(True)
        box.addWidget(self.lbl_chain)

    # ================= 值来源 =================
    def _current(self, cfg_key: str) -> float:
        """这一档现在该画什么：内核起来且问过 `af` 时用反解回来的值，否则只有配置。

        为什么这里要"问过"才算：`af` 没有订阅推送，`_vals` 只可能来自 `page_shown` 那一次
        读回或某次动作后的读回。没问过就把空 dict 当"内核说全是 0"画，会在"启动参数带了
        一条链、而这一页从没打开过"那种情形下画出一页假的 0。
        """
        if self.p.running and self._asked:
            return _f(self._vals.get(cfg_key, 0.0))
        return equalizer.gain_of(self.cfg, cfg_key)

    def _sync_from_cache(self) -> None:
        """把引擎那一层的读回缓存收到面板自己身上（不发命令、不问内核）。

        为什么必须收：改一根滑轨走的是 `set_audio_eq`，它内部整链重发并读过一次 `af`，
        而那份结果留在引擎里。面板不收的话，`_paint_preset` 拿的还是**上一次**的十档，
        于是"拖完 31 Hz 之后下拉还指着人声"——那是界面在撒谎，而它撒的正是自己刚做的动作。
        """
        st = self.p.audio_eq_state()
        self._vals = {k: _f(v) for k, v in st.items() if k in equalizer.ORDER}
        self._problems = list(st.get("problems") or [])
        self._fails = list(st.get("failed") or [])
        self._asked = True

    def repaint_from_state(self) -> None:
        for cfg_key, row in self._rows.items():
            if not row.slider.isSliderDown():
                row.show(self._current(cfg_key))
        self._paint_preset()
        self._paint_chain()

    def _paint_preset(self) -> None:
        """下拉画"当前这十档正好等于哪一条预设"，谁都不等于就画"自定义"。

        为什么不记"用户上次选了哪一条"：改任意一根滑轨之后链就变了，下拉继续指着
        "人声"就是一句假话；而配置里只有十档数字，那才是事实，预设只是数字的别名。
        """
        now = {k: self._current(k) for k in equalizer.ORDER}
        hit = None
        for pr in equalizer.PRESETS:
            if all(abs(now[k] - pr.vals.get(k, 0.0)) < 1e-4 for k in equalizer.ORDER):
                hit = pr
                break
        paint_choice(self.cb_preset, (hit.key if hit is not None else CUSTOM), norm=str)
        self.lbl_preset.setText(f"{hit.label}：{hit.hint}" if hit is not None
                                else "自定义：手动拖过的十档，不属于任何一条预设。")

    def page_shown(self) -> None:
        """主窗口切到这一页时调用：内核在则**问一次** `af` 并按反解重画（不发命令、不改值）。

        为什么必须问：这一族的值是整条链，内核没有可订阅的属性可推。启动参数按配置带上去
        （`equalizer.startup_args`），于是"用户从没点过这一页、但链其实一直在跑"是常态；
        不问就照配置画，配置与内核不一致时（例如上一次被表夹过）界面画的是一个假的现在。
        问一次之后读缓存：`_asked` 就是这个记号。这里和画面页相反（那一页只读缓存不发同步
        请求），差别在**这一族没有订阅**，不问就永远拿不到真值；一次 `get af` 换一整页的
        十一段加前置，是这一页唯一划得来的同步往返。
        """
        if self.p.running:
            _chain, vals, problems, fails = self.p.read_audio_eq()
            self._vals, self._problems, self._fails = dict(vals), list(problems), list(fails)
            self._asked = True
        else:
            self._vals, self._problems, self._fails, self._asked = {}, [], [], False
        self.repaint_from_state()

    def _paint_warn(self, why: list[str], prefix: str) -> None:
        """警告档状态行的唯一落笔处（两句前缀只在这里写一遍）。

        为什么单独成一条方法而不是就地 `setText`：`_on_engine_log` 也要落同一句话，
        两处各抄一遍前缀，迟早有一处改口径，屏幕上就会出现两种"其实是一件事"的红字。
        """
        self.lbl_chain.setText(prefix + "；".join(why[:2]))
        self.lbl_chain.setStyleSheet(f"color: {WARN_TINT};")

    def _paint_chain(self) -> None:
        """状态行：内核说链上有什么、有没有读不懂的条目、有没有整条起不来。

        顺序按"这句话现在是谁的事实"排，**第一条是 `not live`**：内核没在放的时候，
        `_fails` / `_problems` 里那份是上一次会话留下的旧话（或者干脆是一次没人应答的
        "读不到 af"），拿它涂警告色等于把"现在没播放"报成"现在链坏了"。这一刻的真话只有一句：
        这些取值已记入设置，播放时一次带上。内核活着时才轮到那两档，顺序按"坏得最狠"排：
        整条没起来（一声不出）> 有读不懂的条目（界面画的不是内核挂的）> 空链（中性事实）> 有链。
        只把前两档染警告色——"空链"不是坏消息，用户就是要原片，
        给它涂红等于把一次正常操作报成事故。
        """
        live = self.p.running
        parts = self._active_parts()
        if not live:
            self.lbl_chain.setText("内核没在放：这些取值已记入设置，播放时一次带上。")
            self.lbl_chain.setStyleSheet(f"color: {DIM_TINT};")
        elif self._fails:
            self._paint_warn(self._fails, "内核没把链挂起来（会没声音）：")
        elif self._problems:
            self._paint_warn(self._problems, "内核的链有读不懂的条目：")
        elif not parts:
            self.lbl_chain.setText("均衡器未启用（不挂滤镜，声音就是原片）")
            self.lbl_chain.setStyleSheet(f"color: {DIM_TINT};")
        else:
            net = self._net_boost()
            warn = f"　净抬升 {net:+.0f} dB，可能削顶" if net > equalizer.CLIP_NET_MAX else ""
            heads = "、".join(p.split("=")[0] for p in parts[:3])
            self.lbl_chain.setText(f"均衡器：{len(parts)} 条　{heads}"
                                   + ("…" if len(parts) > 3 else "") + warn)
            self.lbl_chain.setStyleSheet(f"color: {OK_TINT};")

    def _active_parts(self) -> list[str]:
        """当前**该上链**的那几节（按面板认定的那十一个数拼，不另开一份判据）。"""
        return equalizer.chain_parts({k: self._current(k) for k in equalizer.ORDER})

    def _net_boost(self) -> float:
        """最大抬升 + 前置（"会不会削顶"那把尺的口径，见模块 docstring 第 3 条）。"""
        top = max([self._current(b.cfg) for b in equalizer.BANDS] + [0.0])
        return top + self._current(equalizer.PREAMP.cfg)

    # ================= 事件：控件 → 内核 =================
    def _status(self, cfg_key: str, shown: float, want: float, ok: bool, live: bool) -> str:
        """一次动作说出来的那句话。四种形状分开写，别共用一条模板：
        没起内核（值已记入设置）、内核点头了、内核整条链没起来（会没声音）、
        内核挂的是另一个数（读回对不上）。

        最后一句要同时写"内核是几"和"你要的是几"：早先那版只把 `shown` 念一遍，
        于是屏幕上出现"内核那一档不是 +2 dB"——而 +2 dB 正是内核挂着的值，
        那句话把自己的证据当成了被否的那个，用户读完只会以为两处有一处在瞎说。
        """
        text = equalizer.value_text(cfg_key, shown)
        if not live:
            return f"{equalizer.label(cfg_key)} {text}（已记入设置，播放后生效）"
        if ok:
            return f"{equalizer.label(cfg_key)} {text}"
        bad = self._fails or self._problems
        if bad:
            return f"{equalizer.label(cfg_key)} {text}：{bad[0][:56]}"
        return (f"{equalizer.label(cfg_key)}：内核是 {text}，"
                f"你要的是 {equalizer.value_text(cfg_key, want)}")

    def _on_num_change(self, cfg_key: str, value: float, commit: bool) -> None:
        spec = equalizer.spec_for(cfg_key)
        if spec is None:
            # 表外项不下发（下发就是"没人核对过的值进 af 链"，而这一族坏值的下场是没声音）。
            # 滑轨只可能从这张表建出来，走到这里说明有人改了表没改连接，留一句而不静悄悄。
            self.sig_status.emit(f"这一档不在均衡器表里：{cfg_key}")
            return
        live = self.p.running
        got, ok = self.p.set_audio_eq(cfg_key, value, confirm=commit and live)
        if not commit:
            return                          # 拖动途中：只写值，不等内核
        row = self._rows.get(cfg_key)
        if row is not None and not row.slider.isSliderDown():
            row.show(got)                   # 用内核反解回来的那一档重画，弹回它真在的位置
        self._sync_from_cache()
        self._paint_preset()
        self.sig_status.emit(self._status(cfg_key, _f(got), _f(value), ok, live))
        self._paint_chain()

    def _on_preset(self, idx: int) -> None:
        key = str(self.cb_preset.itemData(idx))
        if key == CUSTOM:
            return            # 那条是界面自己挂上的读数，不是一次动作
        live = self.p.running
        if not live:
            self._write_preset_quietly(key)
            self.repaint_from_state()
            self.sig_status.emit(
                f"预设「{equalizer.PRESET_BY_KEY[key].label}」已记入设置，播放后生效")
            return
        self.p.apply_audio_eq_preset(key)
        self._sync_from_cache()
        self.repaint_from_state()
        pr = equalizer.PRESET_BY_KEY[key]
        bad = self._fails or self._problems
        self.sig_status.emit(f"预设 {pr.label}" + ("" if not bad else f"：{bad[0][:56]}"))

    def _write_preset_quietly(self, key: str) -> None:
        """内核没起来时整档写进配置，走的是 `set_audio_eq(confirm=False)` 那一个出口。

        为什么不在这里自己摸 `self.cfg[...]`：持久化的口径（夹紧、键名、"表外项不发"）
        在引擎那一层已经写了一遍，抄第二遍就会有两份（同 `TransformPanel.reset_all`）。
        十次 `_fire` 在 `_ipc is None` 时全部空转，一条命令也不会发出去。
        """
        for k, v in (equalizer.preset_values(key) or {}).items():
            self.p.set_audio_eq(k, v, confirm=False)

    def reset_all(self) -> None:
        """一键归零。内核没起来时不发链、也不问 `af`：那会换来一串"读不到 af"的原话，
        把 OSD 刷成噪声（`TransformPanel.reset_all` 是同一条教训）。"""
        if not self.p.running:
            self._write_preset_quietly(equalizer.FLAT.key)
            self._vals, self._problems, self._fails, self._asked = {}, [], [], False
            self.repaint_from_state()
            self.sig_status.emit("均衡器已归零（已记入设置，播放后生效）")
            return
        self.p.reset_audio_eq()
        self._sync_from_cache()
        self.repaint_from_state()
        bad = self._fails or self._problems
        self.sig_status.emit("均衡器已归零（链已摘掉，声音就是原片）" if not bad
                             else f"均衡器已归零，但{bad[0][:48]}")

    # ================= 事件：内核日志 =================
    def _on_engine_log(self, text: str) -> None:
        """启动期与下发期的"这一族没成立"都从日志里认，原话进状态行。

        为什么面板要自己收 `sig_log`：`--af` 是启动参数，内核挂不上链时**没有任何一次动作**
        会让面板走下发那条路，而用户此刻正需要知道"为什么没声音"。
        判据用 `equalizer.EQ_FAIL_MARKS` 那张表，不靠在这里猜关键字。
        """
        t = str(text or "")
        bad = equalizer.failures(t)
        if not bad:
            return
        self._fails = list(self._fails) + bad
        # 直接落笔、不走 `_paint_chain`：内核因为 `--af` 挂不上而**启动致命**时 `running` 是 False，
        # 而 `_paint_chain` 的第一条就是"内核没在放"——那句话本身没错，但它会把"为什么没声音"
        # 的唯一见证盖掉。这一刻用户需要的正是这句原话，所以这里越过 live 闸门一次。
        self._paint_warn(self._fails, "内核没把链挂起来（会没声音）：")

    # ================= 快捷键入口（将来绑键时与点控件同一段代码） =================
    def bump(self, cfg_key: str, delta: float) -> None:
        """一根滑轨加减一档，和拖滑轨走同一个出口（一份逻辑才不会出现
        "按钮改了内核没改"或"键盘的 OSD 文案和面板对不上"）。"""
        spec = equalizer.spec_for(cfg_key)
        if spec is None:
            self.sig_status.emit(f"这一档不在均衡器表里：{cfg_key}")
            return
        want = spec.clamp(_f(self._current(cfg_key)) + _f(delta))
        live = self.p.running
        got, ok = self.p.set_audio_eq(cfg_key, want, confirm=live)
        row = self._rows.get(cfg_key)
        if row is not None and not row.slider.isSliderDown():
            row.show(got)
        self._sync_from_cache()
        self._paint_preset()
        self.sig_status.emit(self._status(cfg_key, _f(got), want, ok, live))
        self._paint_chain()
