"""设置页：性能档位（硬解 / GPU API / 缩放预设 / 音量上限）+ 播放与界面项。

每一项都带一个**生效时机**标注，这不是文案装饰：
  - "重启后生效"是内核行为，不是我们偷懒 —— 本机实测 `set_option hwdec` 报
    invalid parameter，`loadfile … replace` 之后 `options/hwdec` 仍是启动那次的值，
    也就是说换片不会重解析。不写明，用户改完看不到任何变化就会以为"设置没存住"。
  - "下次载入生效"是另一类：值要等 `open_files` 拼命令时才用到（断点续播）。
  - 只有"立即生效"的项才当场把界面/内核改到位（主题色、侧栏宽度下限、截图含字幕）。

可选值一律从事实表里取（`engine/perf.py` 与 `ui/theme.ACCENTS`），这里不抄第二份清单：
写死一份"看起来对"的档位列表，正是设置页最典型的失效方式——内核换了版本、界面还在
提供上一版的选项，点了静默降级。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                             QSpinBox, QVBoxLayout, QWidget)

from .. import config as cfgmod
from ..engine import perf
from . import form_rows, scroll_feel, theme
from .dialog_shell import AppDialog

EFFECT_NOW = "立即生效"
EFFECT_NEXT = "下次载入生效"
EFFECT_RESTART = "重启后生效"

INT_RANGE = {"sidebar_width": (240, 520)}


@dataclass(frozen=True)
class Item:
    cfg: str
    label: str
    kind: str               # choice | bool | int | dir
    effect: str
    table: str = ""         # kind=choice 时取值的表名
    hint: str = ""

    def options(self) -> tuple[tuple[object, str], ...]:
        if self.kind != "choice":
            return ()
        if self.table == "theme_accent":
            return theme.ACCENTS
        return tuple(perf.options_for(self.table or self.cfg))


ITEMS: tuple[Item, ...] = (
    Item("hwdec", "硬件解码", "choice", EFFECT_RESTART, "hwdec",
         "档位名来自本机 mpv --hwdec=help。填进去的非法值不会被拒绝，而是静默退回软解，"
         "所以这一项只能从列表里选"),
    Item("gpu_api", "GPU API", "choice", EFFECT_RESTART, "gpu_api",
         "D3D11 才接得上 d3d11va 的零拷贝通路；换 API 等于换输出上下文"),
    Item("quality_preset", "缩放与去带", "choice", EFFECT_RESTART, "quality_preset"),
    Item("volume_max", "音量上限", "choice", EFFECT_RESTART, "volume_max",
         "控制条滑块量程按这个值画，超过内核上限的那一段拖不动是对的"),
    Item("resume", "断点续播", "bool", EFFECT_NEXT),
    Item("screenshot_include_sub", "截图含字幕", "bool", EFFECT_NOW),
    Item("screenshot_dir", "截图目录", "dir", EFFECT_RESTART,
         hint="这个目录作为启动参数交给内核，改完要重启才换"),
    Item("theme_accent", "主题色", "choice", EFFECT_NOW, "theme_accent"),
    Item("sidebar_width", "侧栏宽度下限", "int", EFFECT_NOW,
         hint="这一栏排不开时会自动加宽（最多加到 520），所以你设的是下限而不是定值"),
    Item("float_controls_in_fs", "全屏控制条浮在画面上", "bool", EFFECT_NOW,
         hint="关掉它，控制条回到布局里占一行：画面会被压矮一条、左右各让出一条黑边，"
              "唤醒和收起时还会跟着抖一次尺寸（旧行为，留作浮层在个别机器上不弹菜单时的退路）"),
)
_BY_CFG = {i.cfg: i for i in ITEMS}
_RESTARTABLE = tuple(i.cfg for i in ITEMS if i.effect == EFFECT_RESTART)

# 两节的标题。写成常量而不是就地字符串，是因为 `_row_effect` 的第一条规矩要靠它判：
# "整节同一种生效时机 ⇒ 行尾不再重复"只有在**标题真的写了这一句**时才成立，
# 所以标题文案必须把 `EFFECT_*` 的字面串拼进去（判据核的是屏幕上 QLabel 的 text，不是这里）。
# 第二节的"没标注的立即生效"是把"省略"这件事本身说出来——否则省略会被读成"漏了"。
TITLE_RESTART = f"性能（都是启动期参数 · {EFFECT_RESTART}）"
TITLE_UI = f"播放与界面（没标注的{EFFECT_NOW}）"


def _title(text: str, parent: QWidget) -> QLabel:
    # 这一页没有「页标题 → 分节」两级：两行节标题用 PanelTitle 的 15px 600，
    # 和页头的粗体一样重，扫一眼像两句并列的标题，而不是"页面下面有两组设置"。
    # 分节走 SectionTitle（11px、字距 1.5 的说明行），真正的页面层级交给页头。
    lab = QLabel(text, parent)
    lab.setObjectName("SectionTitle")
    return lab


def _row_effect(it: Item, section: tuple[Item, ...], title: str) -> str:
    """这一行的行尾要不要写生效时机、写什么。回空串＝这一行不写（话在别处说过了）。

    两条"话在别处"的规矩，缺一不可，而且都必须能被判据复核（见 `tests/test_ui.py` 第 3 节）：

    1. **整节同一种时机 ⇒ 由节标题说一次。** 性能那一节五项全是"重启后生效"，
       旧写法给每一行挂一枚实心胶囊，于是同一句话在屏幕上出现五遍，
       而且它的视觉重量和下拉、按钮是一样的——用户扫一眼以为那是五个不同的状态。
       前提是"标题里就有这一句"，所以判据拿**屏幕上那行标题的字面**来核（`title` 传的是
       `QLabel.text()`，不是这里的常量）：把标题改成别的说法、行尾又没标注，立刻红。
       标题文案因此必须把 `EFFECT_RESTART` 这个字面串原样写进去（见 `TITLE_RESTART`）。
    2. **"立即生效"是默认档 ⇒ 不标。** 剩下那一节是混合的（一次载入 + 三个立即），
       混合节里只标例外：三个"立即生效"本来就是用户对设置的预期，写出来是噪音；
       而"下次载入生效"是**会骗到人的那一类**（改完以为马上变了），必须留。
    """
    if it.effect == EFFECT_NOW:
        return ""
    if len({i.effect for i in section}) == 1 and it.effect in title:
        return ""
    return it.effect


class SettingsDialog(AppDialog):
    """读写 cfg 的设置页。保存后通过 sig_applied 告诉主窗口哪些项需要它动手。"""

    sig_applied = pyqtSignal(dict, list)     # (立即生效的键值, 需要重启才生效的键名)

    def __init__(self, cfg, parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(f"设置 - {cfgmod.APP_TITLE}")
        self.setObjectName("SettingsDialog")
        # 十行设置 + 页头 + 页脚在 560 高的窗口里差 53px，最后一项要滚动才看得见。
        # 这种一次能看全的短页面不该靠滚动：620 把全部行放下；720p 这类矮屏按可用
        # 高度收，滚动条仍是退路（不是裁剪）。
        _h = 620
        _scr = self.screen() or QApplication.primaryScreen()
        if _scr is not None:
            _h = min(_h, int(_scr.availableGeometry().height()) - 80)
        self.resize(520, _h)
        self._widgets: dict[str, QWidget] = {}
        # 每一项的**整行**容器（不是行里那个控件）：判据要问的是"这一行的行尾写了什么"，
        # 而从 `self._widgets[cfg].parentWidget()` 往上摸到的是行里的盒子或父级——
        # 一旦某一行按规矩**不该有**胶囊（整节同档、或默认档），往上摸就会摸到邻行那枚，
        # 于是"该省的行"全都读成写着「下次载入生效」（2026-10-12 实测就这么红的）。
        # 留一份行容器，判据就只能在**这一行**里数胶囊，摸不到别人家。
        self._rows: dict[str, QWidget] = {}
        self._row_labels: list[QLabel] = []
        self._sections: list[tuple[QLabel, tuple[Item, ...]]] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 页头：系统标题栏只报窗口名，客户区里要有一句"这是哪一页、这页在管什么"。
        # 少了它，第一行就是分节说明，整页像从半截开始的表单。
        head = QWidget(self)
        hl = QVBoxLayout(head)
        hl.setContentsMargins(20, 14, 20, 0)
        hl.setSpacing(2)
        lab_title = QLabel("设置", head)
        lab_title.setObjectName("DialogTitle")
        lab_sub = QLabel("性能档位、播放行为与界面偏好。每项都写着什么时候生效。", head)
        lab_sub.setObjectName("DialogHint")
        lab_sub.setWordWrap(True)
        hl.addWidget(lab_title)
        hl.addWidget(lab_sub)
        outer.addWidget(head)

        scroll = QScrollArea(self)
        scroll.setObjectName("StatsScroll")
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setFocusPolicy(Qt.NoFocus)
        body = QWidget(scroll)
        form = QVBoxLayout(body)
        form.setContentsMargins(20, 2, 14, 10)
        form.setSpacing(8)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.scroll_feel = scroll_feel.attach(scroll)

        self._section(body, form, TITLE_RESTART,
                      [i for i in ITEMS if i.effect == EFFECT_RESTART])
        self._section(body, form, TITLE_UI,
                      [i for i in ITEMS if i.effect != EFFECT_RESTART])
        form.addStretch(1)

        foot = QWidget(self)
        flow = QVBoxLayout(foot)
        flow.setContentsMargins(18, 8, 18, 12)
        flow.setSpacing(8)
        self.lbl_note = QLabel("", foot)
        self.lbl_note.setObjectName("AppTitle")
        self.lbl_note.setWordWrap(True)
        flow.addWidget(self.lbl_note)
        row = QHBoxLayout()
        btn_reset = QPushButton("恢复默认档位", foot)
        btn_reset.setFocusPolicy(Qt.NoFocus)
        btn_reset.setToolTip("只把性能四项回默认档，其余设置不动")
        btn_reset.clicked.connect(self._reset_perf)
        btn_close = QPushButton("关闭", foot)
        btn_close.setFocusPolicy(Qt.NoFocus)
        btn_close.clicked.connect(self.reject)
        btn_save = QPushButton("保存", foot)
        btn_save.setObjectName("Primary")
        btn_save.setFocusPolicy(Qt.NoFocus)
        btn_save.clicked.connect(self._save)
        # 「恢复默认」离「保存」远一点：它不是这一页的出口，误点的代价却是把四项改掉。
        row.addWidget(btn_reset)
        row.addStretch(1)
        row.addWidget(btn_close)
        row.addWidget(btn_save)
        flow.addLayout(row)
        outer.addWidget(foot)

        self._align_labels()
        self._load_values()
        self._fit_width()

    # ================= 构建 =================
    def _section(self, body: QWidget, form: QVBoxLayout, title: str, items: list[Item]) -> None:
        lab = _title(title, body)
        form.addWidget(lab)
        section = tuple(items)
        # 记账给判据用：它要核的是"屏幕上这行标题的字面"，所以留 QLabel 本体而不是这里的字符串。
        self._sections.append((lab, section))
        for it in items:
            form.addWidget(self._row(body, it, lab, section))

    def _row(self, body: QWidget, it: Item, title_lab: QLabel,
             section: tuple[Item, ...]) -> QWidget:
        w = QWidget(body)
        self._rows[it.cfg] = w         # 见 `__init__` 里那条：判据按行取胶囊，不往上摸
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        lab = QLabel(it.label, w)
        lab.setObjectName("RowLabel")   # 行标题走正文档，理由见 theme 里 #RowLabel 那条注释
        # 列宽不在这里定：各行分别按自己的字宽开会把「全屏控制条浮在画面上」那行的开关
        # 顶到右边，整页控件左缘参差。先只保证不压字（96 是「硬件解码」的下限），
        # 建完所有行后再统一取最长的那一条（_align_labels）。
        # `ensurePolished()` 是量之前那一步：QSS 的 font-size 不落 QFontMetrics，量的是默认字号。
        lab.ensurePolished()
        lab.setMinimumWidth(max(96, int(lab.sizeHint().width())))
        self._row_labels.append(lab)
        row.addWidget(lab)
        ctl = self._control(w, it)
        row.addWidget(ctl, 1)
        note = _row_effect(it, section, title_lab.text())
        if note:
            # 只标"例外"：整节同一时机时由节标题说一次，"立即生效"是默认档不标。
            # 判据（`_row_effect` 的那两条规矩）核的是"这一项的时机在界面上到底有没有被说出来"，
            # 所以这里省掉的那一行，标题必须正好写着它——两边同用一个函数，不会各说各的。
            tag = QLabel(note, w)
            tag.setObjectName("Chip")
            tag.ensurePolished()
            tag.setMinimumWidth(int(tag.sizeHint().width()))      # 生效时机那枚标签同理
            row.addWidget(tag)
        return w

    def _control(self, parent: QWidget, it: Item) -> QWidget:
        if it.kind == "choice":
            cb = QComboBox(parent)
            cb.setFocusPolicy(Qt.NoFocus)
            for value, text in it.options():
                cb.addItem(text, value)
            self._widgets[it.cfg] = cb
            return cb
        if it.kind == "bool":
            # 开关自己不带文字：这一项的名字写在行首那一列（和滑轨、下拉同一排法），
            # 旧写法那句"开"是给 Win32 方框补上下文的，换成靠位置表态的开关之后它变成多余的第三份。
            c = QCheckBox(parent)
            c.setAccessibleName(it.label)
            c.setFocusPolicy(Qt.NoFocus)
            self._widgets[it.cfg] = c
            return c
        if it.kind == "int":
            s = QSpinBox(parent)
            lo, hi = INT_RANGE.get(it.cfg, (0, 1000))
            s.setRange(lo, hi)
            s.setFocusPolicy(Qt.NoFocus)
            self._widgets[it.cfg] = s
            return s
        box = QWidget(parent)
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        edit = QLineEdit(box)
        edit.setObjectName("PathEdit")
        edit.setFocusPolicy(Qt.ClickFocus)
        # 路径比这一格长时，QLineEdit 是"横向滚动"而不是省略号：光标一动，开头的盘符就退出视野，
        # 看着像目录被改坏了，其实只是看不见。悬停给全文（textChanged 的签名正好是 str→setToolTip）。
        edit.textChanged.connect(edit.setToolTip)
        browse = QPushButton("选目录", box)
        browse.setFocusPolicy(Qt.NoFocus)
        browse.clicked.connect(lambda: self._browse(edit))
        h.addWidget(edit, 1)
        h.addWidget(browse)
        self._widgets[it.cfg] = edit
        return box

    def _align_labels(self) -> None:
        """所有行标题共用一个列宽（取最长那条），让每一行的控件从同一条竖线开始。"""
        width = max((lab.sizeHint().width() for lab in self._row_labels), default=96)
        for lab in self._row_labels:
            lab.setFixedWidth(max(96, int(width)))

    def _fit_width(self) -> int:
        """把窗口至少加宽到"这一页一个字都不被切"，上限是屏幕可用宽。

        为什么需要：这一页装的是 QScrollArea，而滚动区报的最小宽是"我能缩到很小"，
        内容顶宽掉到视口外既不滚动也不压缩（`form_rows.content_width` 里记着同一族形状，
        侧栏 1.3 档字号实测切掉行尾数值胶囊就是它）。整页字号放到 1.5 档时实测内容要 563px，
        而窗口默认 520px，右侧三行「重启后生效」的标签各被切掉 21px。
        加宽是有上限的：窗口比屏幕还宽的话，底部的"保存/关闭"就在屏幕外，
        那比切两个字坏得多——越界时留下"最后一格被切"这一条红，由 `capture_sweep` 点名。
        """
        screen = self.screen() or QApplication.primaryScreen()
        avail = int(screen.availableGeometry().width()) if screen is not None else 0
        self.content_need = form_rows.content_width(self)
        return form_rows.fit_to_content(self, cap=(lambda _n: avail - 80) if avail else None)

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        # 排过版再量一次：构造期滚动区还是 0×0，`content_width` 只能照样式表里的滚动条宽预留；
        # 到这一步视口是真的，多要的那几像素（若有）才是内容真的要。
        self._fit_width()

    def _browse(self, edit: QLineEdit) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择目录", edit.text() or os.path.expanduser("~"))
        if d:
            edit.setText(d)
            edit.setCursorPosition(0)

    # ================= 取值 / 写回 =================
    def _load_values(self) -> None:
        for it in ITEMS:
            w = self._widgets.get(it.cfg)
            if w is None:
                continue
            cur = self.cfg.get(it.cfg)
            if it.kind == "choice":
                idx = w.findData(cur)
                if idx < 0:
                    # 配置里的值不在当前列表里（手改过、或换过内核版本）：
                    # 显式列出来并选中，而不是悄悄跳到第 0 项——那是替用户改设置。
                    w.insertItem(0, f"当前 {cur}", cur)
                    w._gelu_custom = True
                    idx = 0
                w.setCurrentIndex(idx)
            elif it.kind == "bool":
                w.setChecked(bool(cur))
            elif it.kind == "int":
                w.setValue(int(cur or 0))
            else:
                w.setText(str(cur or ""))
                # QLineEdit 放不下长路径时默认把光标留在末尾，静止时显示的是路径尾部
                # （"…\lumen-player\截图"），开头那个盘符反而消失。光标归零，先露开头。
                w.setCursorPosition(0)

    def _value(self, it: Item):
        w = self._widgets.get(it.cfg)
        if w is None:
            return None, f"{it.label}：控件不在"
        if it.kind == "choice":
            v = w.currentData()
            if it.table in perf.DEFAULTS and not perf.valid(it.table, v):
                return None, f"{it.label}：{v!r} 不是本机内核认的档位"
            if it.table == "theme_accent" and not theme.is_accent(v):
                return None, f"{it.label}：颜色 {v!r} 不合法"
            return v, ""
        if it.kind == "bool":
            return bool(w.isChecked()), ""
        if it.kind == "int":
            return int(w.value()), ""
        text = str(w.text() or "").strip().strip('"')
        if not text:
            return None, f"{it.label}：目录不能空着"
        return text, ""

    def reload_from_cfg(self) -> None:
        """按当前配置重画一遍（主窗口复用同一个对话框实例时调用）。

        为什么需要它而不是每次开新的：设置页可能一边改一边看效果（主题色、侧栏宽度），
        对话框留着才连贯；留着就要在每次 show 前把值同步回来，否则屏幕上是上一轮的
        控件状态、磁盘上是这一轮的配置，点"保存"会把旧值写回去。
        """
        self.lbl_note.setText("")
        self._load_values()
        self._fit_width()     # 重画会往下拉里插"当前 X"那条，内容顶宽可能比上一次大

    def _reset_perf(self) -> None:
        for key, dflt in perf.DEFAULTS.items():
            w = self._widgets.get(key)
            if w is not None and w.count():
                idx = w.findData(dflt)
                if idx >= 0:
                    w.setCurrentIndex(idx)

    def _save(self) -> None:
        changed_now: dict[str, object] = {}
        needs_restart: list[str] = []
        notes: list[str] = []
        for it in ITEMS:
            v, err = self._value(it)
            if err:
                notes.append(err)
                continue
            old = self.cfg.get(it.cfg)
            if str(old) == str(v) or (isinstance(old, (int, float)) and isinstance(v, int)
                                      and float(old) == float(v)):
                continue
            self.cfg[it.cfg] = v
            if it.effect == EFFECT_NOW:
                changed_now[it.cfg] = v
            if it.effect == EFFECT_RESTART:
                needs_restart.append(it.cfg)
        if notes:
            self.lbl_note.setText("；".join(notes))
            return
        self.cfg.save()
        if needs_restart:
            tail = "已保存。这些要重启播放器才生效：" + "、".join(_name(k) for k in needs_restart)
        else:
            tail = "已保存。"
        self.lbl_note.setText(tail)
        self.sig_applied.emit(changed_now, needs_restart)

    def closeEvent(self, ev):
        for it in ITEMS:              # 清掉 _load_values 可能插进去的"当前 X"那一项
            w = self._widgets.get(it.cfg)
            if isinstance(w, QComboBox) and getattr(w, "_gelu_custom", False):
                w.removeItem(0)
                w._gelu_custom = False
        super().closeEvent(ev)


_NAMES = {c.cfg: c.label for c in ITEMS}
_NAMES.update({"hwdec": "硬件解码", "gpu_api": "GPU API",
               "quality_preset": "缩放与去带", "volume_max": "音量上限"})


def _name(cfg_key: str) -> str:
    return _NAMES.get(cfg_key, cfg_key)
