"""画面页：几何变换与画质增强（侧栏第四页）。

这一页和第三页（`tweak_panel`）的区别不是"调的项不同"，而是**下发的形状不同**，
所以必须分两页、各走各的出口（详见 `engine/transform.py` 开头那段）：
  - 几何（比例 / 旋转 / 保持比例 / 充满 / 平移）是 GPU 侧属性，一次一条 `set_property`，
    零成本，而且内核会把它回报的值**订阅**推下来 → 滑轨画的是内核说的数；
  - 增强（锐化 / 降噪 / 去带 / 裁剪 / 镜像）不是属性，是**一条滤镜链**：界面每个控件
    只改链上的一小截，取值写进配置后整条重拼、一次 `change-list vf set` 替换。
    这一族内核**不回报**，配置就是外壳持有的唯一事实，所以这一半的滑轨画的是配置值，
    而"有没有真在跑"另外判（见下）。

三条只能写在界面上的实测结论（都来自 `tests/out/probe-transform*.log`）：
  1. **滤镜链里"列表里有名字"不等于"在跑"**：硬解帧上内核会把吃不下的滤镜扔掉，
     而 `vf` 列表里名字照在、`enabled` 照是 True、像素零变化。唯一的防线是每次下发时
     读那一段内核日志（`player.filter_failures()`），所以这一页的状态行必须把日志原话
     带出来，而不是只说"已开启"。
  2. **裁剪后内核不回报新尺寸**：`dwidth`、`video-out-params` 的宽高仍写原片尺寸
     （实测裁到 2.35:1 之后照样回报 1280×720）。所以这里不显示"裁完多大"，
     只写"画面按 2.35:1 裁了"——能证的只有截图尺寸。
  3. **锐化会把噪点一起抬起来**：带噪片段上实测 amount=2.0 把高频从 13.7 抬到 31.3。
     所以滑轨旁边写"先降噪再锐化"，链的顺序也按这个定（去带→降噪→锐化）。

和第三页同样的两条纪律：拖动途中不 confirm（否则每个 valueChanged 都补一次同步往返，
UI 线程等 mpv 应答，手感变成一格一顿）；控件一律 NoFocus（方向键是窗口级快捷键）。

内核没起来时**照样能调**：这些值全部进 `transform.startup_args`，播放时一次带上，
所以这里报"已记入设置，播放后生效"，而不是像快捷键那样回一句"播放内核还没启动"。
同一个动作在两个入口给出两种说法，用户就会怀疑其中一个是坏的。
"""
from __future__ import annotations

from typing import Any

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QComboBox, QFrame, QPushButton, QScrollArea, QVBoxLayout, QWidget)

from ..engine import transform
from ..engine.player import MpvPlayer
from . import scroll_feel, theme
from .form_rows import (KeyHintLabel, NumRow, add_check, add_choice, align_num_values,
                        bind_key_tip_parts, mk_label as _label, paint_check, paint_choice)

# 状态行按事实换色，色号**不在这儿抄第二份**：全部取自 theme 的令牌。
# 为什么以前是三个字面量：这一维 QSS 的选择器给不了（没有"这条是警告"），
# 但"哪个色"不是本模块的事实——本模块的事实只有"哪种状态用哪一档"。
# 原来那三份的实际后果：#E5565C 是危险色的**身份档**（铺底给白字用），
# 压在一句话的 13px 红字上、落在面板底上只有 4.46:1，差 0.04 不达正文的 4.5；
# #9A9A9A 则是整张表里唯一一处不在令牌表里的灰，屏幕上比次要色还暗一档。
# 字号也交回样式表（`#Status` 那一条），这里只改 `color`——两处写同一个字号，
# 迟早有一处改不动（11px 那份就是当年为了"长句别占两行"就地按下去的）。
OK_TINT = theme.OK
WARN_TINT = theme.DANGER_TEXT
DIM_TINT = theme.TEXT_DIM

# 每项的实测说明。写成表而不是塞在控件属性里，是因为验收要逐条核对
# "界面上说的这句话，engine/transform.py 的注释里有没有同一条实测依据"。
HINTS: dict[str, str] = {
    "aspect_override": "比例只改显示比例，不裁画面",
    "rotate": "旋转后宽高互换，字幕跟着转",
    "keep_aspect": "关掉会把画面拉伸成窗口形状",
    "panscan": "100%＝放大到填满窗口，四边会被裁掉",
    "pan_x": "平移只动画面，不动窗口",
    "pan_y": "平移只动画面，不动窗口",
    "sharpen": "锐化会把噪点一起抬起来：先降噪再锐化",
    "denoise": "降噪按 hqdn3d 的 10 档给，过高会糊",
    "deband_vf": "滤镜去带（CPU、立刻见效）。设置页性能档位里的去带是 GPU 侧另一个开关，"
                 "那一档**不能热切**（实测换了 file-local-options 内核照旧回报 False）",
    "crop_ratio": "裁剪后内核仍回报原尺寸（实测 dwidth/宽高都是原片），所以这里不写裁完多大",
    "mirror_h": "左右镜像走 CPU 侧滤镜，硬解帧上会先转一次格式",
    "mirror_v": "上下镜像走 CPU 侧滤镜，硬解帧上会先转一次格式",
}


def _f(v: Any) -> float:
    """任意值 → float，认不出来回 0。

    这一族的取值来自三处都能塞进字符串的地方：内核订阅缓存（没有媒体时推 "no"）、
    config.json（用户可以拿编辑器改）、表里的默认值。而这里每个调用点都在 Qt 槽的
    调用链上——槽里抛 ValueError 不是 traceback，是进程当场没了（rc=127、stderr 全空）。
    同一条纪律在 main_window 顶部也写着，两处不是为了重复，是因为收口必须发生在取值那一道。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x and x not in (float("inf"), float("-inf")) else 0.0


class TransformPanel(QWidget):
    sig_status = pyqtSignal(str)

    def __init__(self, player: MpvPlayer, cfg, parent=None):
        super().__init__(parent)
        self.p = player
        self.cfg = cfg
        self.setObjectName("Panel")
        self.setMinimumWidth(240)
        # 三个字典都按**配置键**索引（不是 mpv 属性名）：这一族里一半的项根本没有属性，
        # 按属性名索引就得给滤镜项编假名字，而表里 cfg 键与属性名是两套（见 transform._BY_KEY）。
        self._rows: dict[str, NumRow] = {}
        self._choices: dict[str, QComboBox] = {}
        self._flags: dict[str, Any] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 10, 10)
        outer.setSpacing(6)
        outer.addWidget(_label("画面", self, "PanelTitle"))

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("TransformScroll")
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

        self._build_geom()
        self._build_filter()
        align_num_values(self._rows.values())
        self._form.addStretch(1)

        # 侧栏只有 300px 宽：长了会被右边裁掉（第三页第一版就被裁成"画面复"），
        # 所以这里只留最常用的三把键，其余在 F1 一览里。
        # 键名**不在这里抄**（`KeyHintLabel`）：这三把键是键位表的事实，而表用户可以改（Ctrl+K）。
        # 抄死的写法在用户把旋转改成 X 之后还写着"按 R"——界面在最该可信的那一行上撒谎。
        outer.addWidget(KeyHintLabel(self.cfg, (
            (("rotate_cycle",), "旋转"),
            (("aspect_cycle",), "比例"),
            (("mirror_h_toggle",), "镜像"),
        ), self))

        self.p.sig_info.connect(self._on_info)
        self.p.sig_log.connect(self._on_engine_log)
        self.repaint_from_state()

    # ================= 构建 =================
    def _section(self, title: str) -> QVBoxLayout:
        self._form.addWidget(_label(title, self._body, "SectionTitle"))
        box = QVBoxLayout()
        box.setSpacing(6)
        self._form.addLayout(box)
        return box

    def _add_num(self, box: QVBoxLayout, spec: Any) -> NumRow:
        # fmt 只改"读出来的那行字"：panscan 的 0..1 给人看是百分数，
        # 而取值、夹紧、下发仍然按表里的 0..1 走（换算写在 transform.value_text 那一层）。
        row = NumRow(spec, self._body, fmt=lambda v, k=spec.cfg: transform.value_text(k, v))
        row.sig_change.connect(self._on_num_change)
        row.lbl.setToolTip(HINTS.get(spec.cfg, ""))
        box.addWidget(row)
        self._rows[spec.cfg] = row
        return row

    def _add_choice(self, box: QVBoxLayout, item: Any) -> QComboBox:
        cb = add_choice(box, item, self._body, lambda c, it=item: self._on_choice(it, c))
        cb.setToolTip(HINTS.get(item.cfg, ""))
        self._choices[item.cfg] = cb
        return cb

    def _add_flag(self, box: QVBoxLayout, item: Any) -> Any:
        # 提示词要同时挂在标题和开关上（`add_check` 里做）：这一行的名字和那一小块是两块地方，
        # 只挂开关的话，用户读着名字想知道"这是干什么的"时恰好悬在没有提示的那块上。
        c = add_check(box, item, self._body, lambda on, it=item: self._on_flag(it, on),
                      tip=HINTS.get(item.cfg, ""))
        self._flags[item.cfg] = c
        return c

    def _build_geom(self) -> None:
        box = self._section("几何")
        self._add_choice(box, transform.ASPECT)
        self._add_choice(box, transform.ROTATE)
        self._add_flag(box, transform.KEEPASPECT)
        self._add_num(box, transform.PANSCAN)
        self._add_num(box, transform.PAN_X)
        self._add_num(box, transform.PAN_Y)

    def _build_filter(self) -> None:
        box = self._section("增强")
        self._add_num(box, transform.SHARPEN.spec)
        self._add_num(box, transform.DENOISE.spec)
        self._add_num(box, transform.DEBAND.spec)
        self._add_choice(box, transform.CROP.spec)
        self._add_flag(box, transform.MIRROR_H.spec)
        self._add_flag(box, transform.MIRROR_V.spec)

        # 状态读数不是行标题：它报的是"内核现在挂着哪些滤镜"，和上面那些"这一项叫什么"
        # 不是一类信息，所以走 `#Status` 那一档（`mk_label` 的默认已升级成正文档，见 form_rows；
        # 调节页那条「A-B」用的是同一档，两个页面的同一角色不该两种长相）。
        self.lbl_chain = _label("滤镜链：（空）", self._body, "Status")
        self.lbl_chain.setWordWrap(True)
        box.addWidget(self.lbl_chain)

        self.btn_reset = QPushButton("画面复位", self._body)
        self.btn_reset.setFocusPolicy(Qt.NoFocus)
        # 这句要说**两把键**（本页的 Ctrl+G 与第三页的 Shift+G），所以分两段各配各的动作：
        # 合成一句就得在这儿再 parse 一次"哪几个字是键名"，而那正是本轮要消灭的写法。
        bind_key_tip_parts(self.btn_reset, self.cfg, (
            ("几何与增强整族回默认", ("transform_reset",)),
            ("；亮度那五项在第三页", ("eq_reset",))))
        self.btn_reset.clicked.connect(self.reset_all)
        box.addWidget(self.btn_reset)

    # ================= 值来源 =================
    def _current(self, spec: Any) -> Any:
        """这一项现在该画什么：几何优先内核订阅缓存，增强只有配置。

        为什么几何优先内核：缓存是内核推来的真值，配置只是上次退出时的愿望
        （越界值会被内核整条拒收，两边就可能不一致）。
        为什么增强不"优先内核"：滤镜链没有可订阅的属性，`vf` 列表又不算证据
        （被扔掉的滤镜照样在列），所以配置就是这一族唯一拿得住的事实。
        """
        prop = transform.prop_of(spec.cfg)
        if prop:
            v = self.p.prop(prop, None)
            if v is not None and not (isinstance(v, str) and v == ""):
                return v
        return self.cfg.get(spec.cfg, spec.default)

    def repaint_from_state(self) -> None:
        for cfg_key, row in self._rows.items():
            row.show(self._current(transform.spec_for_cfg(cfg_key)))
        for cfg_key, cb in self._choices.items():
            spec = transform.spec_for_cfg(cfg_key)
            # 内核把比例回报成浮点（-2 / 1.777778）、把旋转回报成整数，而下拉的 itemData
            # 是表里的串（"16:9"/"no"、"90"）。不先归一就直接 findData，永远找不到 →
            # 于是每次重画都插一条"当前 1.777778"，看着像内核给了个怪值。
            paint_choice(cb, self._current(spec),
                         norm=lambda v, k=cfg_key: str(transform.normalize(k, v)))
        for cfg_key, c in self._flags.items():
            spec = transform.spec_for_cfg(cfg_key)
            paint_check(c, transform.normalize(cfg_key, self._current(spec)))
        self._paint_chain()

    def page_shown(self) -> None:
        """主窗口切到这一页时调用：按当前状态重画一次（不改任何值、不发任何命令）。"""
        self.repaint_from_state()

    def _paint_chain(self, fails: list[str] | None = None) -> None:
        """状态行：链上有什么（内核列出的名字）＋ 内核有没有把谁扔掉（日志原话）。

        名字表来自 `player.vf_names()`，它是**缓存**：只由每次链下发与启动时刷新，
        所以这里不发同步请求。为什么不能只画名字：实测硬解帧上被内核扔掉的滤镜
        照样在列——那一行的"在跑"是假的，判据只能来自日志。
        """
        names = self.p.vf_names()
        bad = list(fails if fails is not None else self.p.filter_failures())
        if bad:
            self.lbl_chain.setText("内核没跑起来：" + "；".join(bad[:2]))
            self.lbl_chain.setStyleSheet(f"color: {WARN_TINT};")
        elif names:
            self.lbl_chain.setText("滤镜链：" + "、".join(names))
            self.lbl_chain.setStyleSheet(f"color: {OK_TINT};")
        else:
            self.lbl_chain.setText("滤镜链：（空）")
            self.lbl_chain.setStyleSheet(f"color: {DIM_TINT};")

    # ================= 事件：控件 → 内核 =================
    def _status(self, spec: Any, shown: Any, ok: bool, live: bool) -> str:
        """一次动作说出来的那句话。三种形状分开写，别共用一条模板：
        没起内核（值已记入、播放时随启动参数带上）、内核夹过（越界）、
        内核根本没收下（滤镜被扔掉）——第三种最要紧，措辞不能和前两种一样。"""
        text = transform.value_text(spec.cfg, shown)
        if not live:
            return f"{spec.label} {text}（已记入设置，播放后生效）"
        if ok:
            return f"{spec.label} {text}"
        bad = self.p.filter_failures()
        if bad:
            return f"{spec.label} {text}：内核没跑起来（{bad[0][:56]}）"
        return f"{spec.label}：内核只接受 {text}"

    def _on_num_change(self, prop: str, value: float, commit: bool) -> None:
        spec = transform.spec_for_cfg(prop)
        if spec is None:
            # 表外项不下发（下发就是"没人核对过的值进内核"）。滑轨只可能从这张表建出来，
            # 走到这里说明有人改了表没改连接，留一行日志而不是静悄悄。
            self.sig_status.emit(f"这一项不在画面表里：{prop}")
            return
        live = self.p.running
        actual, ok = self.p.set_transform(spec.cfg, value, confirm=commit and live)
        if not commit:
            return                                  # 拖动途中：只写值，不等内核
        row = self._rows.get(spec.cfg)
        # 用返回值重画：越界时它是被表夹过（或内核认下的那一个）的值，滑块自己弹回能到的位置
        if row is not None and actual is not None and not row.slider.isSliderDown():
            row.show(actual)
        self.sig_status.emit(self._status(spec, actual if actual is not None else value, ok, live))
        if not transform.prop_of(spec.cfg):
            self._paint_chain()                     # 增强项动的是整条链，状态行跟着改

    def _on_choice(self, item: Any, cb: QComboBox) -> None:
        val = cb.currentData()
        live = self.p.running
        actual, ok = self.p.set_transform(item.cfg, val, confirm=live)
        shown = actual if actual is not None else val
        paint_choice(cb, shown, norm=lambda v, k=item.cfg: str(transform.normalize(k, v)))
        self.sig_status.emit(self._status(item, shown, ok, live))
        if not transform.prop_of(item.cfg):
            self._paint_chain()

    def _on_flag(self, item: Any, on: bool) -> None:
        live = self.p.running
        actual, ok = self.p.set_transform(item.cfg, on, confirm=live)
        shown = on if actual is None else bool(actual)
        paint_check(self._flags.get(item.cfg), shown)
        self.sig_status.emit(self._status(item, shown, ok, live))
        if not transform.prop_of(item.cfg):
            self._paint_chain()

    def reset_all(self) -> None:
        """整族复位。内核没起来时不能走 `player.reset_transforms()`：它每一项都要
        向内核确认，而那会连着产生六条"请求 X 但内核回报 None"的日志，OSD 被噪声刷满。
        持久化仍交给 `set_transform(confirm=False)`，别在这里自己摸 cfg 的键。"""
        if not self.p.running:
            for k, v in transform.reset_values().items():
                self.p.set_transform(k, v, confirm=False)
            self.repaint_from_state()
            self.sig_status.emit("画面已复位（已记入设置，播放后生效）")
            return
        out = self.p.reset_transforms()
        bad = out.get("vf_failed") or []
        self.repaint_from_state()
        self._paint_chain(bad)
        self.sig_status.emit("画面已复位" if not bad else f"画面已复位，但{bad[0][:40]}")

    # ================= 事件：内核 → 控件 =================
    def _on_info(self, info: dict) -> None:
        """订阅缓存里任何一项变了就只重画那一项（不发请求、不整页重排）。
        滤镜链那半没有订阅，所以这里只会命中几何项。"""
        for key, val in (info or {}).items():
            spec = transform.spec_for_cfg(key)
            if spec is None:
                continue
            row = self._rows.get(spec.cfg)
            if row is not None:
                if row.slider.isSliderDown() or val is None:
                    continue          # 用户正拖着这根，别把他的手抢走；空值也不当 0 画
                row.show(val)
                continue
            if spec.cfg in self._choices:
                paint_choice(self._choices[spec.cfg], val,
                             norm=lambda v, k=spec.cfg: str(transform.normalize(k, v)))
            elif spec.cfg in self._flags:
                paint_check(self._flags[spec.cfg], transform.normalize(spec.cfg, val))

    def _on_engine_log(self, text: str) -> None:
        """引擎日志里凡是说"这一项没落到内核"的，原样进状态行。

        为什么面板要自己收 sig_log：链的真相**只在那一段日志里**（`vf` 列表会骗人），
        而主窗口把同一条日志送到 OSD 之后就被面板自己的状态文字覆盖了——
        用户就只剩"已开启锐化"这一句假话。判据用表里的前缀，不靠猜关键字。
        """
        t = str(text or "")
        if t.startswith("滤镜被内核禁用") or "没确认生效" in t or "不在表里" in t:
            self.lbl_chain.setText(t[:120])
            self.lbl_chain.setStyleSheet(f"color: {WARN_TINT};")

    # ================= 快捷键入口 =================
    # 和点控件走同一段代码：一份逻辑才不会出现"按钮改了内核没改"
    # 或"键盘的 OSD 文案和面板对不上"。
    def cycle(self, cfg_key: str, forward: bool = True) -> None:
        spec = transform.spec_for_cfg(cfg_key)
        if spec is None:
            self.sig_status.emit(f"这一项不在画面表里：{cfg_key}")
            return
        actual, ok = self.p.cycle_transform(cfg_key, forward, confirm=self.p.running)
        shown = actual if actual is not None else transform.normalize(cfg_key, spec.default)
        self._paint_item(spec, shown)
        self.sig_status.emit(self._status(spec, shown, ok, self.p.running))
        if not transform.prop_of(cfg_key):
            self._paint_chain()

    def toggle(self, cfg_key: str) -> None:
        """布尔项翻转（H 镜像、P 不拉伸）。当前值按 `_current` 取，和控件画的是同一个数。"""
        spec = transform.spec_for_cfg(cfg_key)
        if spec is None:
            self.sig_status.emit(f"这一项不在画面表里：{cfg_key}")
            return
        want = not bool(transform.normalize(cfg_key, self._current(spec)))
        actual, ok = self.p.set_transform(cfg_key, want, confirm=self.p.running)
        shown = bool(actual) if actual is not None else want
        self._paint_item(spec, shown)
        self.sig_status.emit(self._status(spec, shown, ok, self.p.running))
        if not transform.prop_of(cfg_key):
            self._paint_chain()

    def toggle_panscan(self) -> None:
        """快捷键用的"在还原与充满之间切一刀"，和点滑轨是同一个出口（一份逻辑）。
        判据是"现在有没有缩放"而不是"是不是 1"：停在 30% 时按一下该回 0，
        已经 100% 时按一下也回 0。"""
        cur = _f(self._current(transform.PANSCAN))
        want = 0.0 if cur > 0.0 else 1.0
        live = self.p.running
        actual, ok = self.p.set_transform("panscan", want, confirm=live)
        shown = actual if actual is not None else want
        row = self._rows.get("panscan")
        if row is not None:
            row.show(shown)
        self.sig_status.emit(self._status(transform.PANSCAN, shown, ok, live))

    def _paint_item(self, spec: Any, val: Any) -> None:
        """快捷键改完，把对应的那一个控件画回同一个数（整页重排会抢走用户的鼠标）。"""
        row = self._rows.get(spec.cfg)
        if row is not None:
            if not row.slider.isSliderDown():
                row.show(val)
            return
        if spec.cfg in self._choices:
            paint_choice(self._choices[spec.cfg], val,
                         norm=lambda v, k=spec.cfg: str(transform.normalize(k, v)))
        elif spec.cfg in self._flags:
            paint_check(self._flags[spec.cfg], transform.normalize(spec.cfg, val))
