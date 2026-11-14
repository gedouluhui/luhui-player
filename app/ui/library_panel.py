"""媒体库面板：扫描出来的片按卡片排，双击就播。

和播放列表面板的分工：列表是"这一场要放什么"（真相在内核 playlist），媒体库是
"这台机器上有什么"（真相在磁盘扫描）。所以这里的数据流是单向的：Library 扫完把整表
推给本面板，本面板只做排序/过滤/画卡片，从不自己改条目内容——要变的是磁盘或索引。

封面从 PosterStore 拿：请求→就绪→换图标。没就绪的格子画占位色块，绝不画假封面。
"""
from __future__ import annotations

import os
import re

from PyQt5.QtCore import QEvent, QRectF, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QMenu, QPushButton, QStyle,
                             QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget)

from ..engine.library import Library
from ..engine.posters import PosterStore
from . import form_rows, icons, motion, scroll_feel, theme
from .scrubber import fmt_time

M = theme.M

# (键, 显示名)。顺序就是下拉框顺序；默认第 0 项。
SORTS = (("mtime", "最近加入"), ("name", "名称"), ("dur", "时长"), ("size", "大小"))
# 两个范围：全部＝磁盘扫描出来的整库；最近＝真的播过的那几部（配置里的 `recent`，
# 由 `Config.push_recent` 维护）。最近这一档**不是**一种排序：它是一次过滤，
# 顺序固定为"最近播的在前"，所以进这一档时排序下拉置灰——留着它可选是在撒谎。
SCOPE_ALL = "all"
SCOPE_RECENT = "recent"
# 旧固定档：只在"还没拿到视口宽度"的第一帧与测试兜底里用。真正的卡片尺寸由
# `card_metrics()` 按视口宽算——固定 192 在 300 宽的侧栏里只剩一列还空掉 86px，
# 拉到 520 时两列又空 120px；同一块面板不能只会一种宽度。
ICON_W, ICON_H = 176, 99          # 16:9 封面格（兜底）
GRID_W, GRID_H = 192, 158
CARD_GAP = 8                      # IconMode 的 setSpacing(4) 在相邻格之间留 8
CARD_MIN, CARD_MAX = 140, 260     # 单格总宽（含左右各 8 的内边距）
CARD_PAD = 16                     # 格宽 - 封面宽：左右各 8
CARD_TEXT_H = 50                  # 封面下方的两行文字区（标题 17 + 元信息 14 + 留白）


def card_metrics(avail: int) -> tuple[int, int, int, int, int]:
    """视口宽 → (格宽, 封面宽, 封面高, 格高, 列数)。

    列数按"每格至少 CARD_MIN"取整，再均分；单格封顶 CARD_MAX，免得 520 宽的
    侧栏里出现一张 498px 的巨幅卡片。返回的格子**不超视口**（除极窄的不可达档）。
    """
    avail = max(120, int(avail))
    cols = max(1, (avail + CARD_GAP) // (CARD_MIN + CARD_GAP))
    while cols > 1:
        cell = (avail - CARD_GAP * (cols - 1)) // cols
        if cell >= CARD_MIN:
            break
        cols -= 1
    cell = (avail - CARD_GAP * (cols - 1)) // cols
    cell = max(120, min(CARD_MAX, cell))
    icon_w = max(1, cell - CARD_PAD)
    icon_h = max(1, round(icon_w * 9 / 16))
    return cell, icon_w, icon_h, icon_h + CARD_TEXT_H, cols


ROLE_TITLE = Qt.UserRole + 3
ROLE_META = Qt.UserRole + 4
ROLE_PROGRESS = Qt.UserRole + 5
# (次选, 兜底)：窄卡上整串「时长 · 体积 · 看到 34%」放不下时，按这个次序退，
# 先丢体积、再丢「看到」二字——进度是这一格最要紧的信息，不能总被省略号吃掉。
ROLE_META_ALT = Qt.UserRole + 6


class _CardDelegate(QStyledItemDelegate):
    """媒体库卡片：圆角封面 + 两行字 + 观看进度。

    为什么不用默认 delegate 的"图标居中、文字堆在下面"：那条路的结果是同一张
    .mkv 占位块上悬着一串深灰文字，卡片之间没有边界，整页像文件列表的截图。
    这里把封面当一张海报来裱：圆角、悬停亮边、底部一条进度发丝线。
    """

    def paint(self, p: QPainter, opt, idx):
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        cell = QRectF(opt.rect)
        fade, pressed = motion.row_state(opt, idx)
        selected = bool(opt.state & QStyle.State_Selected)
        # 悬停只让底色淡入（约 110ms），选中与"正在播放"仍即时：它们回答"现在在哪"，
        # 慢半拍就成了跟不上手的界面。按下直接给选中色，松手即回。
        if selected or pressed:
            bg = QColor(theme.BG_ACTIVE)
        elif fade > 0.004:
            bg = QColor(theme.BG_HOVER)
            bg.setAlphaF(min(1.0, fade))
        else:
            bg = None
        if bg is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(cell.adjusted(1, 1, -1, -1), 10, 10)

        icon_sz = opt.widget.iconSize() if opt.widget is not None else QSize(ICON_W, ICON_H)
        pw = max(1, min(icon_sz.width(), int(cell.width()) - 16))
        ph = max(1, round(pw * icon_sz.height() / max(1, icon_sz.width())))
        poster = QRectF(cell.center().x() - pw / 2, cell.top() + 8, pw, ph)
        radius = 9.0

        icon = idx.data(Qt.DecorationRole)
        clip = QPainterPath()
        clip.addRoundedRect(poster, radius, radius)
        p.save()
        p.setClipPath(clip)
        if isinstance(icon, QIcon):
            dpr = max(1.0, float(opt.widget.devicePixelRatioF() if opt.widget else 1.0))
            # PyQt5 的 QIcon.pixmap 没有 devicePixelRatio 那个重载（只有 size/mode/state）；
            # 按物理像素要图再自己钉 DPR，150% 缩放机上封面才不糊。
            pm = icon.pixmap(QSize(max(1, int(poster.width() * dpr)),
                                   max(1, int(poster.height() * dpr))))
            if not pm.isNull():
                pm.setDevicePixelRatio(dpr)
                p.drawPixmap(poster.toRect(), pm)
        p.restore()

        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(theme.ACCENT) if selected else
                      motion.mix_color(theme.BORDER_SOFT, theme.BORDER, fade),
                      1.0 if selected else 0.8))
        p.drawRoundedRect(poster.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        prog = idx.data(ROLE_PROGRESS)
        try:
            frac = float(prog or 0.0)
        except (TypeError, ValueError):
            frac = 0.0
        if 0.0 < frac <= 1.0:
            track = QRectF(poster.left() + 8, poster.bottom() - 7,
                           max(1.0, poster.width() - 16), 2.5)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 110))
            p.drawRoundedRect(track, 1.25, 1.25)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(QRectF(track.left(), track.top(),
                                     max(1.0, track.width() * min(1.0, frac)), track.height()),
                              1.25, 1.25)

        title = str(idx.data(ROLE_TITLE) or "") or \
            str(idx.data(Qt.DisplayRole) or "").split("\n")[0]
        meta = str(idx.data(ROLE_META) or "")
        if not meta:
            parts = str(idx.data(Qt.DisplayRole) or "").split("\n")
            meta = parts[1] if len(parts) > 1 else ""
        alt = idx.data(ROLE_META_ALT)
        if isinstance(alt, (tuple, list)) and len(alt) == 2:
            mid, short = str(alt[0]), str(alt[1])
        else:
            mid = short = meta

        tf = QFont(theme.font_ui(12, 600))
        p.setFont(tf)
        p.setPen(QColor(theme.TEXT))
        fm = QFontMetrics(tf)
        tw = fm.elidedText(title, Qt.ElideRight, max(10, int(poster.width())))
        ty = poster.bottom() + 7
        p.drawText(QRectF(poster.left(), ty, poster.width(), 17),
                   Qt.AlignVCenter | Qt.AlignLeft, tw)

        mf = QFont(theme.font_mono(10))
        p.setFont(mf)
        p.setPen(QColor(theme.TEXT_FAINT))
        mfm = QFontMetrics(mf)
        avail = max(10, int(poster.width()))
        # 三档按"放得下"挑，而不是一律省略：卡片只有 135–142px 时，
        # 「1:30:23 · 8.3GB · 看到 34%」的省略号恰好落在进度上。
        chosen = meta
        for cand in (meta, mid, short):
            if mfm.horizontalAdvance(cand) <= avail:
                chosen = cand
                break
        else:
            chosen = short
        mm = mfm.elidedText(chosen, Qt.ElideRight, avail)
        p.drawText(QRectF(poster.left(), ty + 17, poster.width(), 14),
                   Qt.AlignVCenter | Qt.AlignLeft, mm)
        p.restore()

# 封面补图的分批大小：一片 = 事件循环每空闲一次最多解码几张封面。
# 为什么不整表一次解完：本机实测一张 320×180 的封面 jpg 解码约 1.0ms，而 PosterStore
# 的内存缓存只留最近 60 张（那是刻意的，不让大库吃内存），于是"重建 3000 项的库"=
# 3000 次解码 ≈ 3 秒**整窗冻住**，这期间点什么都没反应。一片 12 张 ≈ 12ms，压在
# 一帧预算（16.7ms）下面：界面照常回话，卡片按从上往下的顺序逐批长出来。
# 读数出处：tests/probe_rebuild_cost.py（一次性量具，2026-10-11）。
FILL_BATCH = 12

# 搜索框防抖：打字途中每次击键都整表重建一遍，而那次结果下一秒就被下一个字作废。
# 停 220ms 再扫，等于"打完了才量一次"；排序下拉与扫描回包仍走即时（各只一次事件）。
SEARCH_DEBOUNCE_MS = 220


def fmt_size(n: float) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.0f}{unit}" if unit == "B" else f"{v:.1f}{unit}"
        v /= 1024.0
    return f"{v:.1f}TB"


class LibraryPanel(QWidget):
    sig_play_path = pyqtSignal(str)         # 双击/回车：只放这一个
    sig_play_list = pyqtSignal(list)        # 右键"从这项开始播全部"：整表路径
    sig_refresh = pyqtSignal()
    sig_add_root = pyqtSignal(str)
    sig_remove_root = pyqtSignal(str)
    sig_forget = pyqtSignal(str)

    def __init__(self, lib: Library, posters: PosterStore, cfg, parent=None):
        super().__init__(parent)
        self.lib = lib
        self.posters = posters
        self.cfg = cfg
        self.setObjectName("Panel")
        self.setAcceptDrops(True)
        self.setMinimumWidth(240)

        self._items: list[dict] = []
        self._shown: list[dict] = []
        self.scope = SCOPE_ALL
        self._scope_n = 0                 # 当前范围里的条数（_sync_empty 读它，省一次重算）
        self._recent_dirty = False
        self.last_error: str | None = None

        # ---- 计量（判据数这些，不掐表；见 tests/test_responsiveness.py 的说明）----
        self.rebuilds = 0            # 真整表重建的次数
        self.keystrokes = 0          # 搜索框被敲过几下
        self.sync_decodes = 0        # 本轮重建里**同步**解码了几张封面（封顶应是 FILL_BATCH）
        self.fill_batches = 0        # 补图分了几个空闲片跑完
        self.placeholders_made = 0   # 占位块画了几次（同扩展名×尺寸档只该画一次）
        self.poster_scans = 0        # _on_poster 为找到那一行摸过几张卡（命中就该停）
        # 封面键 → 卡片行：封面按 key 回来，线性扫 grid 是 O(行数)；库里 3000 项成批
        # 回图时这就是几百万次 item()。字典在重建时清空、补图时登记，命中即 O(1)。
        self._key_items: dict[str, QListWidgetItem] = {}
        self.card_layouts = 0        # 真正改过几次卡片尺寸（同尺寸的 resize 不该抖）
        self.card_w = GRID_W         # 当前格宽/列数（判据与预览读它们）
        self.card_cols = 1
        self._card_sig: tuple[int, int] | None = None
        self._need_flow = False          # 网格刚从隐藏里回来：下一次排布要整表重来
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.setInterval(60)      # 拖窗时一帧改一次尺寸会抖；停手再排
        self._layout_timer.timeout.connect(self._apply_card_layout)
        self._fill_rows: list[int] = []
        self._fill_gen = 0           # 补图队列的世代号：重建一次就作废上一轮
        self._ph: dict[tuple, QIcon] = {}   # (扩展名, 逻辑宽, 逻辑高, DPR) → 占位块
        self._fill_timer = QTimer(self)
        self._fill_timer.setSingleShot(True)
        self._fill_timer.setInterval(0)         # 0＝一闲下来就补一片，不自己排节拍
        self._fill_timer.timeout.connect(self._fill_batch)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._rebuild)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 10, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(4)
        root.addLayout(head)
        head.addWidget(_label("媒体库", self, "PanelTitle"))
        self.lbl_count = _label("0", self, "PanelCount")
        head.addWidget(self.lbl_count)
        head.addStretch(1)
        self.btn_add = self._head_btn(head, "plus", "添加文件夹到媒体库", self._pick_root)
        self.btn_refresh = self._head_btn(head, "repeat", "重新扫描（重探所有文件）",
                                          lambda: self.sig_refresh.emit())
        self.btn_more = self._head_btn(head, "more", "更多", self._more_menu)

        # 搜索栏是**过滤器**不是"填表"：行首放大镜、胶囊形、行尾清空都在 SearchField 里，
        # 播放列表那栏用同一个件——两处各拼一遍，形状迟早会分头漂。
        self.search = form_rows.SearchField(self, "搜索片名…")
        self.search.textChanged.connect(self._on_typed)
        root.addWidget(self.search)

        row = QHBoxLayout()
        row.setSpacing(6)
        root.addLayout(row)
        self.sort = QComboBox(self)
        for _k, label in SORTS:
            self.sort.addItem(label)
        self.sort.currentIndexChanged.connect(self._rebuild)
        row.addWidget(self.sort)
        # 范围开关：时钟图标 + 勾选态。不放文字是因为这一行在 240 宽的侧栏里已经
        # 排着排序下拉和状态读数；图标按钮只在勾选时亮成主色，名实由 tooltip 与
        # 状态行那四个字（「最近播放」）一起说。
        self.btn_scope = QToolButton(self)
        self.btn_scope.setObjectName("ToolBtn")
        self.btn_scope.setCheckable(True)
        self.btn_scope.setAutoRaise(True)
        self.btn_scope.setIcon(icons.icon("history", 16, theme.TEXT_DIM, theme.TEXT))
        self.btn_scope.setIconSize(icons.size_hint(16))
        self.btn_scope.setFixedSize(M.btn_s, M.btn_s)
        self.btn_scope.setToolTip("只看最近播放过的片（再点一次回到全部）")
        self.btn_scope.setAccessibleName("最近播放")
        self.btn_scope.setFocusPolicy(Qt.NoFocus)
        self.btn_scope.toggled.connect(self._on_scope_toggled)
        theme.on_accent(self.refresh_accent)
        row.addWidget(self.btn_scope)
        row.addStretch(1)
        self.lbl_status = _label("未扫描", self, "AppTitle")
        row.addWidget(self.lbl_status)

        self.grid = QListWidget(self)
        self.grid.setObjectName("LibraryGrid")
        self.grid.setViewMode(QListWidget.IconMode)
        self.grid.setResizeMode(QListWidget.Adjust)
        self.grid.setMovement(QListWidget.Static)
        self.grid.setItemDelegate(_CardDelegate(self.grid))
        self.grid.setMouseTracking(True)
        self.grid.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.grid.setIconSize(QSize(ICON_W, ICON_H))
        self.grid.setGridSize(QSize(GRID_W, GRID_H))
        self.grid.setSpacing(4)
        self.grid.setWordWrap(True)
        self.grid.setTextElideMode(Qt.ElideRight)
        self.grid.setSelectionMode(QListWidget.SingleSelection)
        self.grid.setUniformItemSizes(True)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._context_menu)
        self.grid.itemDoubleClicked.connect(lambda it: self._play(it))
        self.grid.itemActivated.connect(lambda it: self._play(it))
        root.addWidget(self.grid, 1)
        # 视口本身也要盯着：滚动条出现/消失时视口宽会变（487↔498），而面板组件本身的
        # 宽度没变，resizeEvent 不会来。只听面板宽度就会一直按"带滚动条"的窄口径排卡片。
        self.grid.viewport().installEventFilter(self)
        # 滚动：一格滚轮换成一段有收尾的位移，滚动条闲置后淡出（见 ui/scroll_feel.py）。
        self.scroll_feel = scroll_feel.attach(self.grid)
        # 悬停/按下：委托按每行的进度画，这里只管推进（见 ui/motion.py）。
        self.hover_feel = motion.attach_hover(self.grid)

        # 空状态页：库里一部片都没有、或搜索词把当前表筛空了，都不该只留一块黑。
        # 两句话分开写——"还没有片"要教下一步（加文件夹/拖进来），"没搜到"要留住
        # 用户刚打的词并给退路（换个词），拿同一句"媒体库为空"糊过去等于答非所问。
        self.empty_page = QWidget(self)
        el = QVBoxLayout(self.empty_page)
        el.setContentsMargins(20, 20, 20, 20)
        el.setSpacing(10)
        el.addStretch(1)
        art = QLabel(self.empty_page)
        art.setPixmap(icons.pixmap("folder", 44, theme.TEXT_FAINT))
        art.setAlignment(Qt.AlignCenter)
        el.addWidget(art)
        self.empty_art = art
        self._empty_icon = "folder"
        self.empty_title = QLabel("媒体库还是空的", self.empty_page)
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignCenter)
        self.empty_title.setWordWrap(True)
        el.addWidget(self.empty_title)
        self.empty_hint = QLabel("", self.empty_page)
        self.empty_hint.setObjectName("HeroHint")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        el.addWidget(self.empty_hint)
        row_e = QHBoxLayout()
        row_e.setSpacing(8)
        row_e.addStretch(1)
        self.empty_add = QPushButton("添加文件夹…", self.empty_page)
        self.empty_add.setObjectName("Primary")
        self.empty_add.setMinimumSize(120, 36)
        self.empty_add.setCursor(Qt.PointingHandCursor)
        self.empty_add.setFocusPolicy(Qt.NoFocus)
        self.empty_add.clicked.connect(self._pick_root)
        row_e.addWidget(self.empty_add)
        row_e.addStretch(1)
        el.addSpacing(6)
        el.addLayout(row_e)
        el.addStretch(2)
        self.empty_page.hide()
        root.addWidget(self.empty_page, 1)

        # 这句是"说明"不是"读数"，所以走共用的 `form_rows.mk_hint`（允许换行）而不是单行 `_label`：
        # 侧栏加宽的上限是 520，而这一句的宽度只随字号涨——3.5 档字号实测一行要 543px，
        # 单行写法它既不被布局压缩也无处可去，于是超出那 45px 从右边掉出去
        # （`logs/sweep-font3.5-*.log` 里那条"截字/裁而未告/压扁"三连就是这一句）。
        # 换成两行只是长一点；上面的卡片网格拿的是剩余高度的 `stretch=1`，多出一行高不会挤掉它。
        # 同页的「0」「未扫描」是读数，要一眼扫到，刻意**不**换成行（理由见 mk_hint 的说明）。
        self.foot_hint = form_rows.mk_hint("双击卡片播放 · 右键更多操作", self)
        root.addWidget(self.foot_hint)
        self._sync_empty("", 0)             # 首帧还没扫描：先教"怎么加片"

        self.lib.sig_items.connect(self.set_items)
        self.lib.sig_status.connect(self._set_status)
        self.posters.sig_done.connect(self._on_poster)
        self._layout_timer.start()          # 首次 show/resize 也会起；这一发兜住"直接可见"的情形

    # ---------- 卡片尺寸随侧栏宽走 ----------
    def resizeEvent(self, ev):              # noqa: N802 — Qt 命名
        super().resizeEvent(ev)
        self._layout_timer.start()

    def showEvent(self, ev):                # noqa: N802
        super().showEvent(ev)
        # 整块面板被别的页换下去期间，网格也跟着不可见；回到可见时 item 可能已经
        # 按"藏起来时的视口"排过一版，只补一次排布请求不足以改回列数（见 _apply_card_layout）。
        self._need_flow = True
        self._layout_timer.start()

    def eventFilter(self, obj, ev):         # noqa: N802 — Qt 命名
        """网格视口换宽重启排版：滚动条来去只改视口，不改面板几何。"""
        if obj is self.grid.viewport() and ev.type() == QEvent.Resize:
            self._layout_timer.start()
        return super().eventFilter(obj, ev)

    def _apply_card_layout(self) -> None:
        """按网格视口宽排卡片。同一尺寸不重设（setGridSize 会触发整表重排）。"""
        vw = self.grid.viewport().width()
        if vw < 120:                        # 布局还没落定；show/resize 会再叫一次
            return
        cell, icon_w, icon_h, grid_h, cols = card_metrics(vw)
        self.card_w, self.card_cols = cell, cols
        sig = (cell, icon_h)
        if sig == self._card_sig:
            if self._need_flow:
                # 网格刚从隐藏里回来：item 是在藏着的时候排的（那一趟的视口宽可能是 0
                # 或上一档），而 show() 之后 Qt 不会自动重算列数。setGridSize 同值会
                # 早退，只有先归零再设回，才真的按当前视口把整表重排一遍。
                self._need_flow = False
                self.grid.setGridSize(QSize(0, 0))
                self.grid.setGridSize(QSize(cell, grid_h))
            else:
                # 尺寸档没变不代表几何是对的：重建可能发生在网格藏着的时候（空态页 /
                # 侧栏别的页），补一次 item 排布，不做 setGridSize——那个才会触发整表重排。
                self.grid.scheduleDelayedItemsLayout()
            return
        self._need_flow = False
        self._card_sig = sig
        self.grid.setIconSize(QSize(icon_w, icon_h))
        self.grid.setGridSize(QSize(cell, grid_h))
        self._refresh_placeholders()            # 旧尺寸的占位块不清掉就会糊（见 _placeholder_icon）
        self.card_layouts += 1

    # ---------- 状态行 ----------
    # 括号里的探测计数和结尾的耗时是"排查用"的，不是"扫一眼用"的：整句按 12px 量到 254px，
    # 而侧栏这一格只有 178px，于是画出来断成「4 个媒体文件（探测 0 /」半截。
    # 所以显示短句、整句进 tooltip——判据（tests/test_library.py）读的是 Library 那份
    # last_status 原文，不是这个标签，缩短显示不会把"探测/复用到底几个"那条账弄丢。
    _DIAG = re.compile(r"（[^）]*）|，用时\s*[\d.]+s")

    def _set_status(self, text: str) -> None:
        full = str(text or "")
        if self.scope == SCOPE_RECENT:
            # 这一档的状态位只服务一件事：让"为什么只剩这几部"有一个字面答案。
            # 扫描诊断原文仍进 tooltip，不丢账。
            self.lbl_status.setText("最近播放")
            self.lbl_status.setToolTip(full)
            return
        short = self._DIAG.sub("", full).strip(" ，,·") or full
        self.lbl_status.setText(short)
        self.lbl_status.setToolTip("" if short == full else full)

    # ---------- 范围：全部 / 最近播放 ----------
    def _on_scope_toggled(self, on: bool) -> None:
        self.scope = SCOPE_RECENT if on else SCOPE_ALL
        self.sort.setEnabled(not on)      # 最近这一档的顺序是"最近播的在前"，排序不适用
        self.search.setPlaceholderText("搜索最近播放…" if on else "搜索片名…")
        self._paint_scope_icon()
        self._rebuild()

    def _paint_scope_icon(self) -> None:
        on = self.btn_scope.isChecked()
        self.btn_scope.setIcon(icons.icon("history", 16,
                                          theme.ACCENT_TEXT if on else theme.TEXT_DIM,
                                          theme.ACCENT if on else theme.TEXT))

    def refresh_accent(self) -> None:
        # 勾选态的图标颜色是主色派生值，换主题后旧位图还挂在缓存键的另一侧。
        self._paint_scope_icon()

    @staticmethod
    def _path_key(p: str) -> str:
        try:
            return os.path.normcase(os.path.normpath(p)) if p else ""
        except (OSError, ValueError):
            return str(p or "")

    def _synthetic(self, path: str) -> dict | None:
        """最近播过、但不在媒体库索引里的文件（从命令行/文件对话框直接打开的）。

        只收**盘上还在**的：一条点开只会弹"文件不存在"的卡片比没有这条更糟。
        时长从观看记录里拿（有就显示，没有就 --:--），不为了画一行字去跑 ffprobe。
        """
        try:
            if not os.path.isfile(path):
                return None
            st = os.stat(path)
        except OSError:
            return None
        name = os.path.basename(path)
        if not name:
            return None
        dur = 0.0
        try:
            dur = float(self.cfg.watch_state(path).get("dur") or 0.0)
        except (TypeError, ValueError, AttributeError):
            dur = 0.0
        return {"path": path, "name": name, "ext": os.path.splitext(name)[1],
                "dur": dur, "size": st.st_size, "mtime": st.st_mtime}

    def _recent_items(self) -> list[dict]:
        """`recent` 列表 → 卡片条目表（按最近播放顺序）。

        先按规范化路径去索引里认领；认不到且盘上还在的，现造一条。
        去重也用同一种键：Windows 上大小写与斜杠写法不改变"这是同一部片"。
        """
        try:
            recents = [str(p) for p in (self.cfg.get("recent") or []) if p]
        except (TypeError, AttributeError):
            recents = []
        if not recents:
            return []
        by_key: dict[str, dict] = {}
        for e in self._items:
            by_key.setdefault(self._path_key(str(e.get("path") or "")), e)
        out: list[dict] = []
        seen: set[str] = set()
        for p in recents:
            k = self._path_key(p)
            if not k or k in seen:
                continue
            seen.add(k)
            hit = by_key.get(k)
            if hit is not None:
                out.append(hit)
                continue
            ent = self._synthetic(p)
            if ent is not None:
                out.append(ent)
        return out

    def note_played(self, _path: str = "") -> None:
        """播放内核换了一支片：最近这一档的排序/进度可能已经变了。

        可见就当场重画（≤24 条，代价是毫秒级）；不可见只记一笔，等 `page_shown` 补。
        """
        if self.scope != SCOPE_RECENT:
            return
        if self.isVisible():
            self._rebuild()
        else:
            self._recent_dirty = True

    def page_shown(self) -> None:
        """切到媒体库页时补一次：播放进度可能在这期间又落过盘。"""
        if self.scope == SCOPE_RECENT:
            self._recent_dirty = False
            self._rebuild()

    # ---------- 头部小按钮 ----------
    def _head_btn(self, row: QHBoxLayout, icon: str, tip: str, slot) -> QToolButton:
        b = QToolButton(self)
        b.setObjectName("ToolBtn")
        b.setAutoRaise(True)
        b.setIcon(icons.icon(icon, 16, theme.TEXT_DIM, theme.TEXT))
        b.setIconSize(icons.size_hint(16))
        b.setFixedSize(M.btn_s, M.btn_s)
        b.setToolTip(tip)
        b.setFocusPolicy(Qt.NoFocus)
        b.clicked.connect(slot)
        row.addWidget(b)
        return b

    def _pick_root(self) -> None:
        from PyQt5.QtWidgets import QFileDialog
        d = QFileDialog.getExistingDirectory(self, "添加到媒体库", os.path.expanduser("~"))
        if d:
            self.sig_add_root.emit(d)

    def _more_menu(self) -> None:
        m = QMenu(self)
        a_refresh = m.addAction("重新扫描")
        a_add = m.addAction("添加文件夹…")
        m.addSeparator()
        roots = m.addMenu("已加入的文件夹")
        for r in self.lib.roots:
            act = roots.addAction(r)
            act.setData(r)
        if not self.lib.roots:
            roots.setEnabled(False)
        chosen = m.exec(self.btn_more.mapToGlobal(self.btn_more.rect().bottomLeft()))
        if chosen is a_refresh:
            self.sig_refresh.emit()
        elif chosen is a_add:
            self._pick_root()
        elif chosen is not None and chosen.data():
            self.sig_remove_root.emit(str(chosen.data()))

    # ---------- 数据 ----------
    def set_items(self, items: list) -> None:
        self._items = list(items or [])
        self._rebuild()          # 扫描回包不是击键：结果要立刻看见，不过防抖

    def _sync_empty(self, query: str, shown: int) -> None:
        """在"卡片网格 / 空状态页"之间翻面（两页顶同一个 stretch 位）。

        `shown` 是**筛完之后**的条数：搜索命中 0 时网格里本来就没东西，留着它只会
        给一块空黑；而"库里 0 部片"和"词没命中"要对用户说的话不是同一句。
        """
        if shown:
            was_hidden = not self.grid.isVisible()
            self.empty_page.hide()
            self.grid.show()
            if was_hidden:
                self._need_flow = True
            # 网格在空态页期间是藏着的：这期间重建的 item 会按"藏起来时的视口"排成
            # 一列，而 `_apply_card_layout` 见尺寸档没变会早退，于是一列一直留到下次
            # 改尺寸。显式排一次——只排可见项，比每次 setGridSize 触发的整表重排便宜。
            self.grid.scheduleDelayedItemsLayout()
            self.foot_hint.show()
            # 网格刚从隐藏里回来时布局还没落定（viewport 还是 0/旧宽），
            # 这一发让 60ms 后的排布按新宽度重算一次，否则卡片会挂着上一档尺寸。
            self._layout_timer.start()
            return
        q = str(query or "").strip()
        icon = "search"
        if self.scope == SCOPE_RECENT:
            if self._scope_n == 0:
                icon = "history"
                self.empty_title.setText("还没有播放记录")
                self.empty_hint.setText("双击媒体库里的卡片开始播放\n播过的片会按时间排在这里")
            else:
                self.empty_title.setText("最近播放里没有找到匹配的片名")
                self.empty_hint.setText(
                    f"最近 {self._scope_n} 部片里没有「{q}」\n换个词试试" if q
                    else "换个筛选词试试")
            self.empty_add.hide()           # "添加文件夹"在这条路上答非所问
        elif not self._items:
            icon = "folder"
            self.empty_title.setText("媒体库还是空的")
            self.empty_hint.setText("点右上角 + 添加文件夹\n或把文件夹拖进来")
            self.empty_add.show()
        else:
            self.empty_title.setText("没有找到匹配的片名")
            self.empty_hint.setText(
                f"在 {len(self._items)} 部片里没有命中「{q}」\n换个词试试"
                if q else "换个筛选词试试")
            self.empty_add.hide()
        # 图标跟着"为什么空"走：库空是文件夹，搜不到是放大镜，最近为空是历史钟。
        # 三种空态共用一张静态图时，看过一次就知道图标在说别的事。
        if icon != self._empty_icon:
            self._empty_icon = icon
            self.empty_art.setPixmap(icons.pixmap(icon, 44, theme.TEXT_FAINT))
        self.grid.hide()
        self.foot_hint.hide()               # "双击卡片"在无卡片时是一句错话
        self.empty_page.show()

    def _on_typed(self, *_a) -> None:
        """搜索框每改一次记一笔，但**不**马上整表重建（防抖在这里，不在 `_rebuild` 里）。

        为什么这么分：`_rebuild` 有好几个来路（扫描回包、排序下拉、清空、程序自己调用），
        它们都该即时；只有"人在打字"这一路该等。把等待挂在信号入口而不是挂在出口，
        就不会出现"别的来路也被拖了 220ms"，也不会因为漏连一个入口而把打字那路丢掉。
        """
        self.keystrokes += 1
        self._debounce.start()

    def _rebuild(self, *_a) -> None:
        """按搜索词 + 排序重建网格：先铺格子（文字＋占位块），封面交给 `_fill_batch` 逐批补。

        整表重建（而不是增量 diff）这一点没变——换来的是"界面和 `_items` 一定一致"。
        变的是**封面什么时候解出来**：以前每张卡片在重建这一趟里同步解码，
        3000 项就是一趟 3 秒的整窗冻住（解码读数见文件头 FILL_BATCH 的说明）；
        现在这一趟只造 item（微秒级），封面按 12 张一片排在事件循环的空隙里补，
        界面在补图期间照常回话。
        """
        self._debounce.stop()                   # 手动来的重建（扫描回包/排序）不该再触发那次防抖
        raw = str(self.search.text() or "").strip()
        word = raw.lower()
        k = self._keyf()
        source = self._items if self.scope == SCOPE_ALL else self._recent_items()
        self._scope_n = len(source)
        items = [e for e in source if not word or word in str(e.get("name", "")).lower()]
        if self.scope == SCOPE_ALL:
            # 最近这一档的顺序就是"最近播的在前"，是数据本身的含义，不该被排序键打散。
            try:
                items.sort(key=self._sort_key(k))
            except Exception as exc:
                self.last_error = f"排序失败：{type(exc).__name__}: {exc}"
        self._shown = items

        self.grid.blockSignals(True)
        self.grid.clear()
        self._key_items.clear()                 # 旧表的行随 grid.clear() 作废，定位表同步清
        self.sync_decodes = 0                   # 只统计"重建这一趟里同步解码了几张"
        # 一次 stat 换整张进度表：逐卡调 cfg.watch_state 会为"外部有没有改过"各 stat 一次，
        # 240 张就是 240 次。同一趟重建里这份表不会变（同线程，重建期间没有写入口）。
        watch = self.cfg.watch_snapshot()
        for e in items:
            path = str(e.get("path") or "")
            title, meta, frac, alt = self._parts(e, watch)
            it = QListWidgetItem(f"{title}\n{meta}")
            it.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            it.setData(Qt.UserRole, path)
            it.setData(ROLE_TITLE, title)
            it.setData(ROLE_META, meta)
            it.setData(ROLE_META_ALT, alt)
            it.setData(ROLE_PROGRESS, frac)
            it.setToolTip(path)
            # key 先留空：封面是"补"出来的，不是"建"出来的。`_on_poster` 按这个键认卡片，
            # 补图那片填上之后，ffmpeg 回来的那一张才有地方换图标。
            it.setData(Qt.UserRole + 1, "")
            it.setIcon(self._placeholder_icon(e))
            self.grid.addItem(it)
        self.grid.blockSignals(False)
        # 上一代的余量就此作废：`_fill_rows` 整个换成新表的行号，正在排队的 timeout
        # 只会照着**新**的行号补，不会把旧列表的图标画到新列表上。
        self._fill_rows = list(range(len(items)))
        self._fill_gen += 1
        self.rebuilds += 1
        self.lbl_count.setText(str(len(items)))
        if self.scope == SCOPE_RECENT:
            self._set_status(f"最近播放 · {len(items)}/{self._scope_n}")
        else:
            self._set_status(self.lib.last_status or ("共 %d 项" % len(items)))
        self._sync_empty(raw, len(items))
        if self._fill_rows:
            self._fill_timer.start()

    def _fill_batch(self) -> None:
        """一片封面：最多 `FILL_BATCH` 张，跑完还有剩就再排一片（还是 interval 0）。"""
        rows = self._fill_rows[:FILL_BATCH]
        del self._fill_rows[:FILL_BATCH]
        for r in rows:
            it = self.grid.item(r)
            if it is None or r >= len(self._shown):    # 表比队列短＝中途重建过，这一片作废
                continue
            e = self._shown[r]
            path = str(e.get("path") or "")
            key = self.posters.request(path, float(e.get("dur") or 0))
            it.setData(Qt.UserRole + 1, key)
            if key:
                self._key_items[key] = it       # 封面回来时按这个键直查，不扫全表
            pm = self.posters.image(key)
            if pm is not None:
                self.sync_decodes += 1
                it.setIcon(QIcon(pm))
        self.fill_batches += 1
        if self._fill_rows:
            self._fill_timer.start()

    def _placeholder_icon(self, e: dict) -> QIcon:
        """占位块按 (扩展名, 逻辑尺寸, DPR) 缓存：同一档卡片里 .mkv 只画一次。

        尺寸进 key 是承重的：卡片格会随侧栏宽度换列（150–260px），旧图标只有 176×99，
        放大到 244 宽时扩展名那行字会糊、缩小到 141 时又会糊成同样比例的一团。
        换一档就重画一张，缓存上限仍按"扩展名有限×尺寸档有限"来封顶。
        """
        sz = self.grid.iconSize()
        w, h = max(1, sz.width()), max(1, sz.height())
        dpr = max(1.0, float(self.devicePixelRatioF() or 1.0))
        key = (str(e.get("ext") or "").lstrip(".").upper()[:4], w, h, round(dpr, 2))
        hit = self._ph.get(key)
        if hit is not None:
            return hit
        self.placeholders_made += 1
        ic = _placeholder(e, w, h, dpr)
        if len(self._ph) > 32:                  # 扩展名×尺寸档是有限集；真超了就从头上清
            self._ph.clear()                    # （宁可重画几张，也不让一个字典跟着库长）
        self._ph[key] = ic
        return ic

    def _refresh_placeholders(self) -> None:
        """卡片档位变了：只给还没拿到封面的格子换占位块，真封面一张都不动。

        为什么不是等下一次整表重建：换列/换格只在拖侧栏时发生，而用户拖完就要看见
        新排布；等下次扫描回包可能已经过了很久，中间一直挂着上一档尺寸的糊图。
        """
        for i in range(self.grid.count()):
            it = self.grid.item(i)
            if it is None or it.data(Qt.UserRole + 1):
                continue                    # 封面已就绪（或正在补）：这一格不归占位块管
            if i < len(self._shown):
                it.setIcon(self._placeholder_icon(self._shown[i]))

    def _keyf(self) -> str:
        i = self.sort.currentIndex()
        return SORTS[max(0, min(i, len(SORTS) - 1))][0]

    def _sort_key(self, k: str):
        if k == "name":
            return lambda e: str(e.get("name", "")).lower()
        if k == "dur":
            return lambda e: -float(e.get("dur") or 0)
        if k == "size":
            return lambda e: -float(e.get("size") or 0)
        return lambda e: -float(e.get("mtime") or 0)

    def _parts(self, e: dict, watch: dict | None = None
               ) -> tuple[str, str, float, tuple[str, str]]:
        """卡片的事实：标题、整串元信息、观看进度（0..1）、两档更短的元信息。

        `watch` 是整趟重建共用的一份进度表（`Config.watch_snapshot()`）；
        没传就按这一条现问——保留给单条调用，重建路径必须传表，否则 N 张卡 N 次 stat。
        短档只服务显示：整串仍进 item 文本（测试与复制读的是那一份原文）。
        """
        name = str(e.get("name") or "")
        stem, _ext = os.path.splitext(name)
        dur = float(e.get("dur") or 0)
        dur_text = fmt_time(dur) if dur > 0 else "--:--"
        base = f"{dur_text} · {fmt_size(e.get('size') or 0)}"
        w = (watch if watch is not None
             else self.cfg.watch_state(str(e.get("path") or ""))).get(str(e.get("path") or ""))
        w = w if isinstance(w, dict) else {}
        total = float(w.get("dur") or 0)
        pos = float(w.get("pos") or 0)
        if total > 0 and pos > 0:
            frac = max(0.0, min(1.0, pos / total))
            pct = int(frac * 100)
            return (stem, f"{base} · 看到 {pct}%", frac,
                    (f"{dur_text} · 看到 {pct}%", f"{dur_text} · {pct}%"))
        if w.get("done"):
            return (stem, f"{base} · 已看完", 1.0,
                    (f"{dur_text} · 已看完", f"{dur_text} · 已看完"))
        return stem, base, 0.0, (base, base)

    def _label(self, e: dict, watch: dict | None = None) -> str:
        title, meta, _frac, _alt = self._parts(e, watch)
        return f"{title}\n{meta}"

    def _on_poster(self, key: str, ok: bool, _detail: str) -> None:
        """封面回来了：只换那一张的图标，不重建整表（重建会把用户的选中态弄丢）。

        `_key_items` 直查：一张封面只属于一行（key 由路径+大小+修改时间算出来），
        旧写法逐行摸 `grid.item()` 是 O(行数)，库里 3000 项、封面成批回来时这一笔
        就是几百万次无用调用（2026-10-20 审查 B5；`poster_scans` 是给判据读的读数，
        命中一张仍记 1）。
        """
        if not ok:
            return
        it = self._key_items.get(key)
        if it is None:
            return
        pm = self.posters.image(key)
        if pm is None:
            return
        self.poster_scans += 1
        it.setIcon(QIcon(pm))

    # ---------- 动作 ----------
    def _play(self, it: QListWidgetItem | None) -> None:
        path = str(it.data(Qt.UserRole) or "") if it is not None else ""
        if path:
            self.sig_play_path.emit(path)

    def _context_menu(self, pos) -> None:
        it = self.grid.itemAt(pos)
        if it is None:
            return
        path = str(it.data(Qt.UserRole) or "")
        m = QMenu(self)
        a_play = m.addAction("播放")
        a_all = m.addAction("从这项开始播放全部")
        m.addSeparator()
        a_dir = m.addAction("打开所在文件夹")
        a_forget = m.addAction("从媒体库移除")
        chosen = m.exec(self.grid.mapToGlobal(pos))
        if chosen is a_play:
            self._play(it)
        elif chosen is a_all:
            i = self.grid.row(it)
            paths = [str(self.grid.item(j).data(Qt.UserRole) or "") for j in range(i, self.grid.count())]
            paths += [str(self.grid.item(j).data(Qt.UserRole) or "") for j in range(0, i)]
            self.sig_play_list.emit([p for p in paths if p])
        elif chosen is a_dir and path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
        elif chosen is a_forget and path:
            self.sig_forget.emit(path)

    # ---------- 拖放：拖文件夹进来＝加进媒体库 ----------
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for u in ev.mimeData().urls():
            p = u.toLocalFile()
            if p and os.path.isdir(p):
                self.sig_add_root.emit(p)
            elif p:
                self.sig_play_path.emit(p)
        ev.acceptProposedAction()


def _label(text: str, parent: QWidget, obj: str) -> QLabel:
    lb = QLabel(text, parent)
    lb.setObjectName(obj)
    return lb


def _placeholder(e: dict, w: int = ICON_W, h: int = ICON_H, dpr: float = 1.0) -> QIcon:
    """没封面时的占位块：哑光底 + 中央淡播放记号 + 右下角扩展名，一眼看出"还没抽到图"。

    三件都是按**当前卡片尺寸**画的，不是固定 176×99 再让 Qt 拉：
      · 卡片格会随侧栏宽度在 150–260px 之间换档，固定尺寸放大后扩展名会糊；
      · 底比面板亮一档（BG_HOVER + BORDER_SOFT 细边）——旧版用面板同色 BG_RAISE，
        在 300px 侧栏里整块和背景融成一片，只剩一个浮在空处的"MP4"；
      · 按 devicePixelRatio 出图：本机 200% 缩放，1x 位图会被拉成双倍糊边。
    播放记号只占高度的 ~34%，缩到 141×79 也不挤；没有高光、没有渐变，是哑面。

    返回 QIcon 不是 QPixmap：`QListWidgetItem.setIcon()` 只收 QIcon，
    给它 QPixmap 会在**信号槽回调里**抛 TypeError —— 而这个回调是 C++ 侧的事件投递调进来的，
    异常穿不过去，进程直接 abort（rc=127、一行 traceback 都留不下）。
    类型收口在函数签名上，比在每个调用点记得包一层可靠。
    """
    w, h = max(1, int(w)), max(1, int(h))
    dpr = max(1.0, float(dpr or 1.0))
    pm = QPixmap(int(round(w * dpr)), int(round(h * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    body = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
    radius = min(8.0, h / 6.0)
    p.setPen(QPen(QColor(theme.BORDER_SOFT), 1.0))
    p.setBrush(QColor(theme.BG_HOVER))
    p.drawRoundedRect(body, radius, radius)

    # 中央：描边圆 + 实心播放三角。只跟高度挂钩，宽卡片里也不会横着摊平。
    d = max(14.0, min(h * 0.34, w * 0.22))
    cx, cy = w / 2.0, h / 2.0
    ink = QColor(theme.TEXT_FAINT)
    ink.setAlpha(170)
    p.setPen(QPen(ink, max(1.0, d * 0.055)))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(cx - d / 2.0, cy - d / 2.0, d, d))
    tri = d * 0.38
    tri_path = QPainterPath()
    tri_path.moveTo(cx - tri * 0.42, cy - tri * 0.60)
    tri_path.lineTo(cx + tri * 0.80, cy)
    tri_path.lineTo(cx - tri * 0.42, cy + tri * 0.60)
    tri_path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(ink)
    p.drawPath(tri_path)

    ext = str(e.get("ext") or "").lstrip(".").upper()[:4]
    if ext:
        f = QFont(p.font())
        f.setPixelSize(max(9, min(12, int(round(h * 0.09)))))
        p.setFont(f)
        p.setPen(QColor(theme.TEXT_FAINT))
        p.drawText(body.adjusted(0, 0, -8, -6), Qt.AlignRight | Qt.AlignBottom, ext)
    p.end()
    return QIcon(pm)
