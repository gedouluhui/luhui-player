"""标题栏：品牌 + 当前片名 + 三个窗口按钮（最小化/最大化/关闭）。

拖动由**本控件自己**处理：按住标题栏（品牌字、片名、空白处都算）走过拖动阈值后调
`win32.begin_system_move()` 进系统原生移动循环（ReleaseCapture + SC_DRAGMOVE，与 Qt
startSystemMove 同口径），双击进最大化/还原。命中测试把标题栏判成 HTCLIENT 就是为了
让这些鼠标事件到得了这里——WS_POPUP 无边框窗上 DefWindowProc 的隐式 HTCAPTION 拖动
根本不启动，只能自己发起。窗钮各自吃事件，不会触发拖动。
"""
from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QSizePolicy,
                             QToolButton, QWidget)

from .. import config as cfgmod
from .. import win32
from . import form_rows, icons, material, theme

M = theme.M


class _HoverSwapButton(QToolButton):
    """悬停时换一张同形状、不同颜色的图标。

    QSS 的 `#CloseBtn:hover { color: #FFF }` 改的是**文字**，改不到 `QIcon` 的像素；
    关闭钮悬停时底色变成红，图标却还是原来那档灰，深红底上那枚 X 几乎看不见。
    QIcon 的 Active 档在 QToolButton 只悬停（未按下）时也不保证会被取用，所以这里
    用 enter/leave 显式换图，不把可读性押在样式引擎的实现细节上。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._swap: tuple[QIcon, QIcon] | None = None

    def set_hover_icons(self, normal: QIcon, hot: QIcon) -> None:
        self._swap = (normal, hot)
        self.setIcon(normal)

    def enterEvent(self, ev):
        if self._swap is not None:
            self.setIcon(self._swap[1])
        super().enterEvent(ev)

    def leaveEvent(self, ev):
        if self._swap is not None:
            self.setIcon(self._swap[0])
        super().leaveEvent(ev)


class TitleBar(QWidget):
    def __init__(self, parent=None, on_min=None, on_max=None, on_close=None, cfg=None):
        # `cfg` 是键位表所在的那份配置（关闭那颗的悬停要说"这把键现在是什么"）。
        # 可以不给：不给就按默认表算（`effective(None)`），因为**没有 cfg 就没有用户的表**，
        # 而绝不能退回抄死的那句「（Ctrl+W）」——抄死的那句在用户改键之后就是谎话。
        super().__init__(parent)
        self._cfg = cfg if cfg is not None else {}
        self._on_max = on_max
        # 拖动状态：按下先记原点，移动超过系统阈值才真正进系统移动循环——
        # 否则一次单击就被当成拖动，双击最大化永远等不到第二下。
        self._drag_armed = False
        self._drag_origin = None
        self.setObjectName("TitleBar")
        # QWidget 子类默认不画 QSS 的背景/边框（WA_StyledBackground 关着），于是
        # #TitleBar 那条 border-bottom 一直没上过屏。材质面要在它上面画，先让样式底图成立。
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(M.title_h)     # 先给标准档，末尾 `_fit_height()` 再照实际字号收口

        row = QHBoxLayout(self)
        row.setContentsMargins(18, 0, 6, 0)
        row.setSpacing(10)

        self.logo = QLabel(self)
        self.logo.setPixmap(icons.pixmap("logo", 20, theme.ACCENT))
        row.addWidget(self.logo, 0, Qt.AlignVCenter)

        brand = QLabel(cfgmod.APP_TITLE, self)
        brand.setObjectName("BrandTitle")
        row.addWidget(brand, 0, Qt.AlignVCenter)

        # 一条竖向发丝线：品牌与片名是两个信息，不加这条会读成一个长句。
        self.sep = QFrame(self)
        self.sep.setObjectName("BrandSep")
        self.sep.setFixedSize(1, 14)
        row.addWidget(self.sep, 0, Qt.AlignVCenter)

        # 拖动区铺满中间整条：标题文本在它下面，点哪里都能拖
        self.drag = QWidget(self)
        self.drag.setObjectName("DragRegion")
        self.drag.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        inner = QHBoxLayout(self.drag)
        inner.setContentsMargins(2, 0, 12, 0)
        self.lbl_doc = QLabel("", self.drag)
        self.lbl_doc.setObjectName("NowTitle")
        inner.addWidget(self.lbl_doc, 0, Qt.AlignVCenter | Qt.AlignLeft)
        # 整句自己存着：标签上放的是"按当前宽度裁过的版本"，见 set_document/_elide
        self._doc = ""
        row.addWidget(self.drag, 1)

        # 三颗窗钮收在自己的盒里：它们之间是 2px，和左边的品牌组隔着一整段弹性空白。
        caps = QWidget(self)
        cl = QHBoxLayout(caps)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(2)
        cl.addWidget(self._cap("minimize", "最小化", on_min, "CaptionBtn"))
        self.btn_max = self._cap("maximize", "最大化 / 还原", on_max, "CaptionBtn")
        cl.addWidget(self.btn_max)
        cl.addWidget(self._cap("close", "关闭", on_close, "CaptionBtn CloseBtn",
                                actions=("quit",)))
        row.addWidget(caps, 0)
        self.set_document("")       # 首帧没有片名：只留品牌，不留一条空竖线
        self._fit_height()
        theme.on_accent(self.refresh_accent)

    def refresh_accent(self) -> None:
        self.logo.setPixmap(icons.pixmap("logo", 20, theme.ACCENT))

    def paintEvent(self, _ev):          # noqa: N802 — Qt 命名
        # QSS 先铺了一遍 BG_DEEP，这里在它上面加厚度（渐变/噪点/受光/底影）。
        material.paint_surface(self, "title")

    def _fit_height(self) -> None:
        """高度照里面**文字自己要多高**给，不钉死 `M.title_h`。

        整页字号放到 3.0 档时「LuHui播放器」的 sizeHint 高会超过固定值，标题与片名
        会被上下切掉一截。标准字号下这条规则算出来仍是 54，视觉不动；上下各留 12px
        是让文字别贴边。
        """
        need = 0
        for w in self.findChildren(QLabel) + self.findChildren(QToolButton):
            w.ensurePolished()        # QSS 的 font-size 先落到 QFontMetrics 上，否则量的是默认字号
            need = max(need, int(w.sizeHint().height()))
        self.setFixedHeight(max(M.title_h, need + 12))

    def _cap(self, icon: str, label: str, slot, name: str,
             actions: tuple = ()) -> QToolButton:
        """颗子按钮。悬停里的键名由 `form_rows.bind_key_tip` 按生效表现算，不在这儿抄。"""
        close = "CloseBtn" in name.split()
        b = _HoverSwapButton(self) if close else QToolButton(self)
        b.setObjectName(name)
        b.setAutoRaise(True)
        normal = icons.icon(icon, 14, theme.TEXT_DIM)
        b.setIcon(normal)
        b.setIconSize(icons.size_hint(14))
        b.setFixedSize(46, 32)
        if isinstance(b, _HoverSwapButton):
            # 悬停底色变成 DANGER 红，而 QSS 只改得了文字、改不到图标像素；不换图的话
            # 那枚 14px 的灰 X 落在深红上基本看不见。最小化/最大化不换：它们的图标由
            # set_maximized 单点改写，挂一对 swap 会在还原时把"热点图"留在屏幕上。
            b.set_hover_icons(normal, icons.icon(icon, 14, theme.WHITE))
        form_rows.bind_key_tip(b, self._cfg, label, actions)
        b.setFocusPolicy(Qt.NoFocus)   # 标题栏按钮不该吃走空格/回车
        if slot:
            b.clicked.connect(slot)
        return b

    def set_document(self, text: str) -> None:
        self._doc = str(text or "")
        # 没在放东西时，标题栏只留品牌：一条悬空的竖线和一个空标签会像"加载失败"。
        has = bool(self._doc)
        self.sep.setVisible(has)
        self.lbl_doc.setVisible(has)
        self._elide()

    def _elide(self) -> None:
        """片名放不下的时候用省略号，不用"断在半截"。

        QLabel 不会自己裁字：给它 90px 它就画前几个像素，剩下的直接消失——
        一个 40 个字的片名在窄窗口里会显示成"C:\\Users\\name\\Desk"这种断句，
        既不完整也不像被裁过。所以这里自己按可用宽度取省略版（中间省略，保留头尾），
        整句留在 tooltip 里。
        """
        avail = max(0, self.drag.width() - 26)     # 26 = DragRegion 左右内容边距
        t = self._doc
        if t and self.lbl_doc.fontMetrics().horizontalAdvance(t) > avail:
            t = self.lbl_doc.fontMetrics().elidedText(self._doc, Qt.ElideMiddle, avail)
        if self.lbl_doc.text() != t:
            self.lbl_doc.setText(t)
        self.lbl_doc.setToolTip(self._doc)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._elide()

    def set_maximized(self, on: bool) -> None:
        self.btn_max.setIcon(icons.icon("restore" if on else "maximize", 14, theme.TEXT_DIM))

    def drag_region(self) -> QWidget:
        return self.drag

    # ---------- 拖动 / 双击最大化 ----------
    def mousePressEvent(self, ev):
        # 落在窗钮上的按击被 QToolButton 自己吃掉，到不了这里；品牌字/片名/空白处
        # 的子控件默认忽略鼠标事件，会一路上浮到本控件，于是整条标题栏都能拖。
        if ev.button() == Qt.LeftButton:
            self._drag_armed = True
            self._drag_origin = ev.globalPos()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag_armed and (ev.buttons() & Qt.LeftButton) and self._drag_origin is not None:
            if (ev.globalPos() - self._drag_origin).manhattanLength() >= QApplication.startDragDistance():
                # 越过系统拖动阈值：这一下是"拖"不是"点"。交棒给系统移动循环后本控件
                # 不再追鼠标（循环自己抓），松手时循环退出；方向/贴边吸附/跨屏都由系统算。
                self._drag_armed = False
                self._drag_origin = None
                win32.begin_system_move(int(self.window().winId()))
                ev.accept()
                return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        self._drag_armed = False
        self._drag_origin = None
        super().mouseReleaseEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._on_max is not None:
            self._on_max()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)
