"""侧栏共用的表单行：一根「标题 + 当前值 + 滑轨」。

为什么单独开一个文件而不是让 `tweak_panel` 自己留着：调节页（字幕/均衡/HDR/声音）和
画面页（比例/旋转/充满/平移/锐化/降噪/去带）用的是**同一种控件**，而它对量纲的处理
并不显然——QSlider 只有整数，表里是 float + step，`round` 与 `int` 的差别会直接变成
用户手上的"拖两下只动了 0.1"。两处各抄一份，将来某一份改了取整方式，症状就是
"同一个滑轨在两个页面上手感不一样"，而没人会想到去比对两份代码。

控件的事实来源仍然是 `engine/tweaks.py` / `engine/transform.py` 那两张表：
这里只负责把 `Num` 画出来，范围、步长、默认值、单位文字都从 spec 取，
`fmt` 只是**显示层**的换算（同一张表里 panscan 的 0..1 给人看成百分比），
不参与取值，所以夹紧、下发、比对都还是表说了算。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit,
                             QScrollArea, QSlider, QToolButton, QVBoxLayout, QWidget)

from ..engine import tweaks
from . import icons, keymap, theme


def content_width(panel: QWidget) -> int:
    """这一页「一个字都不被切」至少要多宽（给侧栏的加宽规则与设置窗口的最小宽用）。

    为什么不能直接问 `panel.minimumSizeHint()`：这几页里装的是 `QScrollArea`，
    而滚动区报的最小宽是"我能缩到很小"——它把内容的顶宽藏到横向滚动条后面去了。
    侧栏这几页又把横向滚动条关掉（300px 的侧栏里让人横向拖着读一行字是更坏的方案），
    于是内容超出视口的那几十像素**既不滚动也不压缩，直接从右边掉出去**，
    而所有量具都看不见——Qt 的布局仍然"排开了"，只是排在了裁剪框外面。

    实测字号 1.3 档：侧栏 300 → 视口 268，而调节页的内容要 304（顶宽的是
    「输出设备 + 下拉(156) + 刷新(64)」那一行），右边 36px 掉出去，
    正好把每一行末尾的数值胶囊「1.00x」切掉一截。

    所以这里绕开滚动区，直接问它肚子里那块 `widget()` 的 `minimumSizeHint()`，
    再把滚动条宽、边框、面板自己的左右外边距加回来。
    没有滚动区的面板（播放列表/媒体库）退回布局自己的最小宽——
    它们的长文件名是**设计成省略号**的，不该把侧栏顶宽。
    """
    lay = panel.layout()
    if lay is None:
        return int(panel.minimumSizeHint().width())
    margins = lay.contentsMargins()
    chrome = int(margins.left()) + int(margins.right())
    best = 0
    for sc in panel.findChildren(QScrollArea):
        body = sc.widget()
        if body is None:
            continue            # 空的滚动区（还没塞内容）不顶宽
        # 滚动条占的那一条：取「已排过版的真实差值」与「滚动条自己的 sizeHint」两者最大。
        # 宁可多算 10px 也不少算，少算的结果就是内容悄悄掉到裁剪框外。
        # 为什么不能只信"问实际差值"那一条（2026-10-11 实测订正）：没排过版的滚动区 `width()`
        # 不是 0 而是 Qt 默认的 100（视口 98），于是"实际差值"给 2px、兜底那一条轮不到，
        # 调节页因此少算一整根滚动条（真实 26px），`_pages_need()` 读 378 而排过版之后同一页读
        # 402，差的正是行尾数值胶囊「1.00x」那 5px（裁而未告尺点名：需 45px、只露 40px）。
        # 为什么**不**把 `QStyle.PM_ScrollBarExtent` 也放进 max：它不认样式表。本机出厂形状
        # （`theme.install` 之后）量到真实差值 10、sizeHint 10、样式那把 26——拿 26 就是凭空
        # 给侧栏多要 16px。sizeHint 与真实差值两把都跟着 QSS 走，取它们的 max 就够兜住上面那一族。
        bar = max(int(sc.width()) - int(sc.viewport().width()),
                  int(sc.verticalScrollBar().sizeHint().width()))
        best = max(best, int(body.minimumSizeHint().width()) + max(0, bar)
                   + 2 * int(sc.frameWidth()))
    if best:
        return best + chrome
    return int(lay.minimumSize().width()) + chrome


def fit_to_content(win: QWidget, cap: Callable[[int], int] | None = None) -> int:
    """把窗口加宽到"内容一个字都不被切"，返回落到的宽度；只加宽，绝不缩。

    为什么不直接 `win.setMinimumWidth(content_width(win))` 就完事：设置窗口的最小宽一旦顶到
    屏幕外，用户就再也拖不回来（窗口比屏幕宽，底部的"保存/关闭"在屏幕外）——
    所以调用方要给一个上限，越界时**宁可留一条"最后一格被切"的红**让尺子去点，
    也不造一个关不掉的窗口。这一条与侧栏的 520 上限是同一个道理（见 `MainWindow._fit_sidebar`）。

    量具对账用：调用方把 `content_width` 的读数存起来，判据问的是
    "窗口宽 ≥ 内容宽 或 已经顶到上限"，两样都不成立就是产品该修。
    """
    need = content_width(win)
    lay = win.layout()
    chrome = 0
    if lay is not None:
        m = lay.contentsMargins()
        chrome = int(m.left()) + int(m.right())
    target = need + chrome
    if cap is not None:
        target = min(target, int(cap(need)))
    w = max(int(win.width()), int(target))
    if w != int(win.width()):
        win.resize(w, int(win.height()))
    return w


def mk_hint(text: str, parent: QWidget) -> QLabel:
    """一行按键/用法提示：允许换行，因为它是"说明"而不是"读数"。

    为什么不照单行排：这类句子的长度只随字号涨，而它待的那一列宽度是有限额的
    （侧栏最宽 520，实测 3.0 档字号下「把文件或文件夹拖进来即可播放」一行要 508px）。
    单行写法有两种坏结局，而且都静默：要么把整列顶宽（挤掉视频区），要么超出部分掉到
    裁剪框外（字少半截，布局却"排开了"）。换成两行只是长一点，两者都比那两个好。
    读数类（「A-B：未设置」这种）不放进来——那些是要一眼扫到的，换行反而读不顺。
    布局这边是安全的：外层是 QVBoxLayout，`QLabel` 带 wordWrap 时 `hasHeightForWidth()` 成立，
    布局会按实际折行给到第二行的位子（量它的高度要用 `heightForWidth(实得宽)`，
    用 `sizeHint().height()` 量到的是单行高——`capture_sweep` 的尺子就是这么修的）。
    """
    lab = mk_label(text, parent, "AppTitle")
    _hint_style(lab)
    return lab


def _hint_style(lab: QLabel) -> None:
    """提示行共用的那两件事：说明档的字色字号 + 允许换行。

    为什么单独抽出来：`KeyHintLabel` 是 `QLabel` 的子类，不能直接调 `mk_hint`（那是造另一个实例），
    而它必须和 `mk_hint` 长得一模一样——"提示"和"提示"在同一页里出现两种字号是第二种不一致。
    两边都走这里，改一处就都改了。
    """
    lab.setObjectName("AppTitle")
    lab.setWordWrap(True)


class KeyHintLabel(QLabel):
    """一行按键提示，但**键名是从生效键位表算出来的**，不是面板里手抄的一句话。

    为什么要它（而 `mk_hint("R 旋转 · Shift+R 比例 · H 镜像", self)` 那种写法不行）：
    那句话里有的是**表的事实**（哪把键管什么），而表是用户可以改的（Ctrl+K 改键位）。
    手抄的版本在用户把"旋转"改成 X 之后仍然写着"按 R"——界面在它最该可信的那一行上撒谎，
    因为用户正是在记不住按键时才来看它。这里改成每次按键提示都问一次
    `keymap.effective(cfg["keymap"])`，改键、加键、删键都跟着动。
    说法（"旋转""比例"）留在调用方：那是这一页自己的措辞，表里那句"画面旋转 90°"
    在 300px 的侧栏放不下（见 `mk_hint` 那条换行理由）。

    一段都没键可说时（用户把这几把键全删了）整行藏起来而不是留一行空字：
    空白行在侧栏里占一整行的位子，看着像页面坏了。
    """

    def __init__(self, cfg, groups, parent: QWidget, sep: str = " · "):
        super().__init__(parent)
        _hint_style(self)
        self._cfg = cfg
        # `groups` 挂在**公开**属性上，不是私有：判据要拿它独立重算这一行"该写什么"，
        # 摸私有字段的话，将来类内部改个名，判据就悄悄瞎了（读不到 ⇒ 空 ⇒ 过）。
        self.groups = [(tuple(a), lab) for a, lab in groups]
        self._sep = sep
        self.refresh()

    def refresh(self) -> str:
        """按当前生效表重画这一行，返回画出来的那句（判据读返回值，不必再摸私有字段）。"""
        rows = keymap.effective(self._cfg.get("keymap"))
        text = keymap.key_hint(rows, self.groups, self._sep)
        self.setText(text)
        self.setVisible(bool(text))
        return text


def refresh_key_hints(root: QWidget) -> int:
    """把 `root` 子树里所有按键提示重画一遍，返回重画了几行。

    为什么在窗口层收口而不是让每个面板自己订阅：改键位之后要重画的是**整窗**的提示行
    （画面页、调节页，将来还会有别的页），逐页接线就会漏页；而这一族控件有一个共同的类型，
    按类型找一遍不会漏，也不要求调用方知道有哪几页。返回值给判据核"确实找到了东西"，
    免得将来某次改名把这条刷新变成空转（空转＝不报错的过期提示）。
    """
    n = 0
    for lab in root.findChildren(KeyHintLabel):
        lab.refresh()
        n += 1
    return n + refresh_key_tips(root)


# 控件上挂登记用的属性名。**公开名字**（不带前导下划线）：判据要按这个名字在盘上的控件树里
# 找东西，挂成私有字段的话将来类内部改个名，判据就悄悄瞎了（读不到 ⇒ 空 ⇒ 过）。
TIP_ATTR = "gelu_key_tip"


def bind_key_tip(widget: QWidget, cfg, label, actions: Iterable[str] = ()) -> str:
    """给控件绑一句「说法（键名）」的悬停提示，并登记下来好让改键位之后能重算。

    为什么要登记而不是当场算完就撒手（V6 之前就是这个样子）：那句话里的**键**是键位表的事实，
    而表用户可以改（Ctrl+K）。抄死的版本有两种坏法——① 改键之后继续说旧键；② 出厂文案本身
    就已经和表不一致（2026-10-12 静态扫出来三条：「下一个（PgDn）」表里写的是 PgDown、
    「停止（Shift+CS）」这把键表里根本没有、「静音（Z / M）」实际绑的是 Z 和 Shift+,）。
    登在控件自己身上（而不是面板里的一张表），因为要重画的是整窗：逐页接线一定会漏页。

    `label` 可以是字符串，也可以是**无参函数**——控制条上「播放/暂停」「静音/取消静音」
    「倍速 1.25x」「循环：单曲」这几颗的说法本来就跟着状态变，写成字符串就会在状态变了之后
    露出旧文案；函数则在每次重画（状态变了、或键位改了）时现取。
    """
    return bind_key_tip_parts(widget, cfg, ((label, actions),))


def bind_key_tip_parts(widget: QWidget, cfg, parts: Iterable[Iterable[Any]]) -> str:
    """一句提示要**分几段说**时用这个（「全屏（Enter/F）& 退出（Esc）」就是两段）。

    为什么不把两段合成一个字符串传进去：合成一句就得在这儿再 parse 一次"哪几个字是键名"，
    而这件事正是本轮要消灭的写法。分段传，键名那一段永远由 `keymap.tip_note` 现算。
    """
    widget.__dict__[TIP_ATTR] = (cfg, tuple((w, tuple(a)) for w, a in parts))
    return paint_key_tip(widget)


def paint_key_tip(widget: QWidget) -> str:
    """按当前生效表把这句提示重画到控件上，返回画出去的那句（判据读返回值，不摸私有字段）。"""
    cfg, parts = widget.__dict__[TIP_ATTR]
    rows = keymap.effective(cfg.get("keymap"))
    text = "".join((w() if callable(w) else w) + keymap.tip_note(rows, a) for w, a in parts)
    widget.setToolTip(text)
    return text


def refresh_key_tips(root: QWidget) -> int:
    """重画 `root` 子树里所有登记过的悬停提示，返回重画了几颗。

    和 `refresh_key_hints` 同一个收口理由：改一次键位要跟着动的是整窗的按钮，
    一处处接线就会漏。一颗都没登记时返回 0——调用方（判据）要把这个数当证据看，
    不许把它当成"没有东西要刷，通过"。
    """
    n = 0
    for w in root.findChildren(QWidget):
        if TIP_ATTR in w.__dict__:
            paint_key_tip(w)
            n += 1
    return n


def key_tips(root: QWidget) -> list[tuple[QWidget, tuple[tuple[Any, tuple[str, ...]], ...]]]:
    """窗口里登记过的悬停提示（控件 → 它的「说法 / 动作组」分段）；判据拿它逐颗核对。

    判据不该自己去猜"哪颗按钮该说什么"：登记本身就是产品对"这颗按钮归哪几把键管"的表态，
    而**键名**那一句由判据独立重算（见 `tests/test_ui.py` 的 `tip_expect`），两边各算各的。
    """
    return [(w, w.__dict__[TIP_ATTR][1]) for w in root.findChildren(QWidget)
            if TIP_ATTR in w.__dict__]


def mk_label(text: str, parent: QWidget, obj: str = "RowLabel") -> QLabel:
    """造一个标签。**默认是"表单行的标题"那一档（正文色 13px），不是暗灰的说明档。**

    为什么默认给正文档：这个工厂绝大多数调用点造的就是行标题（滑轨行、下拉行、开关行），
    而它们原先默认成 `#AppTitle`（12px 暗灰）——和标题栏那行品牌字、和行尾的数值胶囊
    共用同一档，一整页没有主次。说明行（`mk_hint`）与状态读数（滤镜链、A-B）是少数，
    所以让它们显式点名自己的那一档（说明行 `#AppTitle`、状态读数 `#Status`），而不是让所有行标题去点名正文档。
    """
    lab = QLabel(text, parent)
    lab.setObjectName(obj)
    return lab


class SearchField(QLineEdit):
    """媒体库与播放列表共用的搜索框：胶囊形、行首放大镜、行尾清空叉。

    `setClearButtonEnabled(True)` 的清空件由 Qt 内部那颗 QToolButton 画，图标是系统
    标准图上那枚"浅色圆底 + 深色叉"——压在深色搜索框上，它成了整栏最亮的一点，
    比用户写的字还先被看见。图标只能在按钮被创建之后（首次 show）换掉；
    认它靠"落在右半边"，不靠 objectName（这版 PyQt 里那颗按钮的名字是空的）。
    """

    def __init__(self, parent: QWidget, placeholder: str, accessible_name: str = ""):
        super().__init__(parent)
        self.setObjectName("SearchField")
        self.setPlaceholderText(placeholder)
        self.setClearButtonEnabled(True)
        self.addAction(icons.icon("search", 14, theme.TEXT_DIM), QLineEdit.LeadingPosition)
        if accessible_name:
            self.setAccessibleName(accessible_name)

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        for b in self.findChildren(QToolButton):
            if b.x() + b.width() // 2 > self.width() // 2:
                b.setIcon(icons.icon("close", 12, theme.TEXT_FAINT, theme.TEXT))
                b.setIconSize(QSize(12, 12))
                break


class NumRow(QWidget):
    """一根滑轨：标题 + 当前值 + 滑块。值与滑块之间用"格数"换算。

    QSlider 只有整数，而表里是 float + step（字幕大小 0.05 一格、平移 0.05 一格）。
    这里把量纲收在类里：`idx = round((v-lo)/step)`，反向同理。为什么要 round 而不是
    int()：0.3 + 0.05*2 在二进制里是 0.39999999999999997，截断会少一格，
    表现为"拖两下只动了 0.1"。

    `fmt` 只改**读出来的那行字**（panscan 的 0..1 写成百分数），不改发出去的值：
    传进来的是 `transform.value_text` 那一类"给人看的换算"，
    而 `spec.text` 是默认那一档（带单位、按 spec.fmt 格式化）。
    """

    sig_change = pyqtSignal(str, float, bool)      # (属性名, 值, 是否该向内核确认)

    def __init__(self, spec: tweaks.Num, parent: QWidget,
                 fmt: Callable[[Any], str] | None = None):
        super().__init__(parent)
        self.spec = spec
        self._fmt = fmt or spec.text
        self._steps = max(1, int(round((spec.hi - spec.lo) / spec.step)))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.lbl = mk_label(spec.label, self)
        self.val = QLabel(self._fmt(spec.default), self)
        self.val.setObjectName("Chip")
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # 58 只是单行兜底；一页里真正的列宽由 `align_num_values` 按最宽读数统一给。
        self.val.setMinimumWidth(58)
        top.addWidget(self.lbl)
        top.addStretch(1)
        top.addWidget(self.val)
        lay.addLayout(top)
        self.slider = QSlider(Qt.Horizontal, self)
        # 零点落在中间的那些项（亮度/对比度/灰度/饱和度/色相、字幕与声音延迟、左右/上下平移）
        # 换一根中性滑轨：默认样式"从最左铺主色到滑块"，于是值为 0（＝没动）时也有半根彩色的轨，
        # 看着像已经调过。判据用区间而不是 label 名单——将来表里加一项双极的，UI 不用跟着改。
        if spec.lo < 0 < spec.hi:
            self.slider.setObjectName("BipSlider")
        self.slider.setRange(0, self._steps)
        # 高度下限＝把手直径。QSlider 自己按 Fusion 的默认厚度要位子（真窗口实测这一页最矮的
        # 滑轨控件只有 11 高），而 Qt 把控件矩形**外面**的像素裁掉：14 的圆压在一个 11 高的控件上，
        # 上下各被削掉 1.5px，圆变成鼓形（探针 tests/probe_handle_shape.py 的 C 段量过这一刀：
        # 同一份 QSS，控件高 10 时 bbox 就是 14×10，控件高 ≥14 才出满 14×14）。
        # 这一句不是"好看一点"，是把 theme.SLIDER_HANDLE 那条规则要画的东西留给它画出来。
        self.slider.setMinimumHeight(theme.SLIDER_HANDLE)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.slider.setFocusPolicy(Qt.NoFocus)
        self.slider.valueChanged.connect(self._on_slide)
        lay.addWidget(self.slider)

    def value_of(self, idx: int) -> float:
        return round(self.spec.lo + int(idx) * self.spec.step, 6)

    def index_of(self, v: Any) -> int:
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = float(self.spec.default)
        return max(0, min(self._steps, int(round((x - self.spec.lo) / self.spec.step))))

    def show(self, v: Any) -> None:
        """按内核/配置的值重画，期间不发信号（否则内核→滑块→内核回环）。"""
        idx = self.index_of(v)
        self.slider.blockSignals(True)
        self.slider.setValue(idx)
        self.slider.blockSignals(False)
        self.val.setText(self._fmt(self.value_of(idx)))

    def _on_slide(self, idx: int) -> None:
        v = self.value_of(idx)
        self.val.setText(self._fmt(v))
        # 手指还在滑块上＝拖动途中，不发同步确认；滚轮/方向键没有 down 状态，算一次动作
        self.sig_change.emit(self.spec.prop, v, not self.slider.isSliderDown())

    def value_width(self) -> int:
        """这一行读数在所有档位下需要的最大宽（含样式表的边距/描边）。"""
        saved = self.val.text()
        texts = {saved}
        for idx in (0, self._steps, self.index_of(self.spec.default)):
            texts.add(self._fmt(self.value_of(idx)))
        widest = 0
        for t in texts:
            self.val.setText(t)
            self.val.ensurePolished()
            widest = max(widest, int(self.val.sizeHint().width()))
        self.val.setText(saved)
        return widest


def align_num_values(rows) -> int:
    """把一页里所有滑轨行的读数锁成同一列宽，避免 58/60 这种肉眼可见的参差。

    宽度取各行**量程两端与默认值**里最宽的那串：拖动过程中读数只会在这三档之间变短，
    所以列宽不会随手指移动而重排（`setFixedWidth` 而不是只抬 `minimumWidth`——
    后者遇到比基准宽的值时那一行仍会自己长出去）。
    """
    rows = list(rows)
    widest = max((r.value_width() for r in rows), default=0)
    if widest:
        for r in rows:
            r.val.setFixedWidth(widest)
    return widest


# ==================== 一行下拉 / 一个勾选 ====================
# 这两行看着谁都会写，但里面各埋着一条实测出来的规矩，所以也只留一份：
#   1. 连接用 `activated`（只有用户操作才发）而不是 `currentIndexChanged`——后者在
#      "按内核值重画下拉"时也会回调回来，等于界面自己给自己下命令（回环）；
#   2. 重画时值不在预设里就**插一条"当前 X"并选中**，绝不"顺手"落到第 0 项——
#      那是替用户改了设置（老配置留的 6 位颜色、将来新增的档位都走这条路）；
#   3. 控件一律 NoFocus：主窗口的方向键是窗口级快捷键，焦点落在下拉/勾选上时
#      会"按一下跳 5 秒，同时这一项也动了"。
# `norm` 是调用方的取值归一（调节页拿它翻颜色串），界面侧不知道内核会回报什么形状。

def add_choice(box: QVBoxLayout, item: Any, parent: QWidget,
               on_change: Callable[[QComboBox], None]) -> QComboBox:
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(mk_label(item.label, parent))
    row.addStretch(1)
    cb = QComboBox(parent)
    # 下拉的最短宽度按**字数**要，不按像素钉死：`setMinimumWidth(156)` 那种写法在标准字号
    # 下刚好排得开，字号到 1.3 档就变成"这一行最少要 274px"，而侧栏视口只有 268px——
    # 多出来的那几像素既不滚动也不压缩，直接从右边掉出去（实测切掉行尾的数值胶囊）。
    # 字数档是随字号长的，所以这一行在任何字号档下都要得开同样的字。
    # 真正撑开下拉的是最长那一条选项（sizeHint 自己会算），这里只管"被压到时不许短成这样"。
    cb.setMinimumContentsLength(12)
    cb.setFocusPolicy(Qt.NoFocus)
    for v, text in item.options:
        cb.addItem(text, v)
    cb.activated.connect(lambda _i=0, c=cb: on_change(c))
    row.addWidget(cb)
    box.addLayout(row)
    return cb


def paint_choice(cb: QComboBox | None, val: Any,
                 norm: Callable[[Any], str] | None = None) -> None:
    if cb is None or val is None or (isinstance(val, str) and not val):
        return            # 空值不是"第 0 项"：内核还没推来过就别动控件
    want = norm(val) if norm else str(val)
    if getattr(cb, "_gelu_custom", False):
        cb.removeItem(0)
        cb._gelu_custom = False
    idx = cb.findData(want)
    if idx < 0:
        cb.insertItem(0, f"当前 {want}", want)
        cb._gelu_custom = True
        idx = 0
    cb.blockSignals(True)
    cb.setCurrentIndex(idx)
    cb.blockSignals(False)


class _RowLabel(QLabel):
    """开关行的标题，但整块可点：点标题等于点这一行的开关。

    为什么标题要接住点击：开关本体只有 40×24，让人瞄着那一小块点是苛刻的，
    而标题写的正是这一项的名字——点它当然该拨这一项（iOS 的整行可点落到这里就是"标题＋开关"这两块）。

    转发用 `animateClick()` 而不是 `setChecked()`：前者发 `clicked`（走的是用户那条路，
    界面→内核），后者只发 `toggled`，而 `add_check` 接的刻意是 `clicked`
    ——上面那条规矩 1（防"按内核值重画时自己给自己下命令"的回环）不能从标题这条捷径绕过去。
    指针形状也给成手型，否则"能点"这件事没有任何视觉交代。
    """

    def __init__(self, text: str, parent: QWidget, target: QCheckBox,
                 obj: str = "RowLabel"):
        super().__init__(text, parent)
        self.setObjectName(obj)
        self._target = target
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._target.isEnabled():
            self._target.animateClick()
            ev.accept()
            return
        super().mousePressEvent(ev)


def add_check(box: QVBoxLayout, item: Any, parent: QWidget,
              on_toggle: Callable[[bool], None], tip: str = "") -> QCheckBox:
    """一行布尔项：标题贴左、开关贴右，和同一页的滑轨行、下拉行是同一种排法。

    为什么不用 `QCheckBox(item.label)`：那样指示器永远画在文字**左边**（Qt 的勾选框
    就是这个顺序，把文字搬不到行尾），于是这一页只有它一行是"[控件] 标题 ……"，
    而其余十几行都是"标题 …… [控件]"。所以标题交给同级 QLabel，开关自己不留文字。

    不留文字不等于不留名字：`setAccessibleName(item.label)` 把这一项叫什么挂在控件自己身上，
    读屏念得出，判据也读得到（`controls_cover_table` 比的就是这一份，标签仍来自事实表，
    "写着锐化、动的是降噪"照样翻红）。

    `tip` 要同时给标题和开关：改了排法之后能悬停的两块是分开的，只给开关那 40×24 的话，
    用户读着名字想知道"这是干什么的"时恰好悬在没有提示的那块上。
    """
    row = QHBoxLayout()
    row.setSpacing(6)
    c = QCheckBox(parent)
    c.setFocusPolicy(Qt.NoFocus)
    c.setAccessibleName(item.label)
    c.clicked.connect(lambda _c=False, w=c: on_toggle(w.isChecked()))
    lab = _RowLabel(item.label, parent, c)
    # 标题是同级控件，不是勾选框自己的一部分——所以"屏幕上写的那行字"和"这一项叫什么"
    # 现在是两份东西。留一条回链，让判据能核"两份是不是同一份"（`controls_cover_table`
    # 里那条"开关行没有可见标题"）。挂属性而不是让判据去摸布局：同级 QLabel 在 Qt 里
    # 没有稳定的"找哥哥"接口，摸布局的写法会跟着 addStretch 的增删漂。
    c._row_label = lab
    for w in (lab, c):
        if tip:
            w.setToolTip(tip)
    row.addWidget(lab)
    row.addStretch(1)
    row.addWidget(c)
    box.addLayout(row)
    return c


def paint_check(c: QCheckBox | None, val: Any) -> None:
    if c is None or val is None:
        return            # 空值不是"关"：照 None 画会把用户刚勾上的抹掉
    on = val if isinstance(val, bool) else str(val).lower() in ("true", "yes")
    c.blockSignals(True)
    c.setChecked(bool(on))
    c.blockSignals(False)
