"""播放列表面板：直接反映内核的 playlist 属性，不自建第二份列表。

为什么不自己维护一份：内核才是播放列表的真相（loadfile/append/playlist-remove/乱序
都在它那边动）。自己存一份必然出现"界面有、内核没有"的错位。所以 playlist 属性一变
就整表重建（不增量改），但**合并着重建**：见 `COALESCE_MS`。

合并的理由是量出来的，不是审美：mpv 每 `loadfile` 一次就推一次 playlist，
"拖一个文件夹进来"= N 次推送 = N 趟"清表＋重造 N 行"，总代价 O(N²)
（1000 个文件 ≈ 50 万次 item 造销，这段时间窗口完全不回话）。按 60ms 的窗合并后，
一阵连续的推送只在窗外各画一次：列表最多滞后 60ms，却永远不会停在中间态——
因为每次真画都画的是"窗内最后那份"，而窗外再来推送会另起一窗。

拖动排序同样走内核：`playlist-move` 的语义是"把第 src 项挪到**原表**第 slot 个坑位"
（本机 v0.41.0-1101 实测，见 tests/probe_playlist_move.py）。界面只把用户松开鼠标时
那个坑位报上去，真正的顺序仍以随后的 playlist 推送为准——界面不替内核记顺序。
"""
from __future__ import annotations

import json
import os

from PyQt5.QtCore import QEvent, QMimeData, QPoint, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QDrag, QFont, QFontMetrics, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QAbstractItemView, QFrame, QHBoxLayout, QLabel,
                             QListWidget, QListWidgetItem, QMenu, QStyle,
                             QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget)

from ..engine.player import MpvPlayer
from . import icons, motion, scroll_feel, theme
from .form_rows import SearchField, bind_key_tip, mk_hint

M = theme.M

COALESCE_MS = 60        # 合并窗：窗内的 playlist 推送只画最后一份
ROW_MIME = "application/x-luhui-playlist-rows"
ROLE_CURRENT = Qt.UserRole + 2


class _PlaylistDelegate(QStyledItemDelegate):
    """播放列表的一行：序号、片名、当前播放的均衡器小柱。

    为什么不用 QSS 的 ::item：圆角选中块、序号列、当前行的呼吸条是三处不同几何，
    QSS 只给一整块矩形，画出来永远像"系统列表"。
    """

    ROW_H = 38

    def sizeHint(self, opt, idx):
        return QSize(0, self.ROW_H)

    def paint(self, p: QPainter, opt, idx):
        p.save()
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(opt.rect.adjusted(0, 1, 0, -1))
        fade, pressed = motion.row_state(opt, idx)
        selected = bool(opt.state & QStyle.State_Selected)
        current = bool(idx.data(ROLE_CURRENT))

        if selected or pressed:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.BG_ACTIVE))
            p.drawRoundedRect(r, 8, 8)
        elif fade > 0.004:
            c = QColor(theme.BG_HOVER)
            c.setAlphaF(min(1.0, fade))
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawRoundedRect(r, 8, 8)

        if current:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(theme.ACCENT))
            p.drawRoundedRect(QRectF(r.left() + 1, r.center().y() - 8, 2.5, 16), 1.25, 1.25)

        # 序号：等宽、低对比，滚一遍能对上是第几首，但不跟片名抢。
        f = QFont(theme.font_mono(10))
        p.setFont(f)
        p.setPen(QColor(theme.TEXT_FAINT))
        num = str(idx.row() + 1)
        p.drawText(QRectF(r.left() + 12, r.top(), 24, r.height()),
                   Qt.AlignVCenter | Qt.AlignRight, num)

        # 当前行的三根小柱：它不是装饰，是"内核真在放这一行"的唯一直接证据。
        right_pad = 34 if current else 14
        if current:
            base = r.right() - 20
            mid = r.center().y()
            for i, h in enumerate((7.0, 12.0, 9.0)):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(theme.ACCENT))
                x = base + i * 4.5
                p.drawRoundedRect(QRectF(x, mid - h / 2, 2.5, h), 1.25, 1.25)

        title = str(idx.data(Qt.DisplayRole) or "")
        tf = QFont(theme.font_ui(13, 600 if current else 400))
        p.setFont(tf)
        p.setPen(QColor(theme.ACCENT_TEXT if current else theme.TEXT))
        avail = r.width() - 12 - 24 - 8 - right_pad
        fm = QFontMetrics(tf)
        text = fm.elidedText(title, Qt.ElideMiddle, max(10, int(avail)))
        p.drawText(QRectF(r.left() + 44, r.top(), max(10, avail), r.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, text)
        p.restore()


class _PlaylistList(QListWidget):
    """列表本体：多选、Delete 移除、把拖动翻译成"源行 + 插入坑位"。

    拖拽不做 Qt 的 InternalMove：那套会在模型里自己搬行，而模型的行是**上一份**
    内核 playlist 的倒影——搬完界面与内核就对不上，下一次推送又把顺序弹回去。
    这里只发信号，顺序仍由内核的 `playlist-move` 改、由推送回填。
    """

    sig_add_paths = pyqtSignal(list)
    sig_move_rows = pyqtSignal(list, int)
    sig_delete_rows = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.MoveAction)
        # 插入线由自己画（QListView 的 drop indicator 是给它自己的 InternalMove 用的，
        # 自定义 mime 下多半不出现；靠它就等于"拖了看不到落点"）。
        self.setDropIndicatorShown(False)
        self.reorder_allowed = lambda: True

        self._drop_slot: int | None = None
        self._drop_line = QFrame(self.viewport())
        self._drop_line.setFixedHeight(2)
        self._drop_line.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._drop_line.setStyleSheet(
            f"background: {theme.ACCENT}; border: none; border-radius: 1px;")
        self._drop_line.hide()

    # ---------- 键盘 ----------
    def keyPressEvent(self, ev):
        mods = ev.modifiers()
        if ev.key() in (Qt.Key_Delete, Qt.Key_Backspace) and \
                not (mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
            rows = self._visible_selected_rows()
            if rows:
                self.sig_delete_rows.emit(rows)
                ev.accept()
                return
        super().keyPressEvent(ev)

    def _visible_selected_rows(self) -> list[int]:
        """只算看得见的行：筛选藏起来的条目不该被 Delete 顺手带走。"""
        return sorted({self.row(it) for it in self.selectedItems() if not it.isHidden()})

    # ---------- 拖动源 ----------
    def startDrag(self, actions):
        rows = self._visible_selected_rows()
        if not rows:
            return
        mime = QMimeData()
        mime.setData(ROW_MIME, json.dumps(rows).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pm = self._drag_pixmap(rows)
        if not pm.isNull():
            drag.setPixmap(pm)
            drag.setHotSpot(QPoint(pm.width() // 2, pm.height() // 2))
        drag.exec_(Qt.MoveAction)

    def _drag_pixmap(self, rows: list[int]) -> QPixmap:
        pm = QPixmap(176, 30)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(theme.BORDER_SOFT), 1))
        p.setBrush(QColor(theme.BG_HOVER))
        p.drawRoundedRect(0.5, 0.5, pm.width() - 1, pm.height() - 1, 8, 8)
        f = QFont(self.font())
        f.setPixelSize(12)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(theme.TEXT))
        first = self.item(rows[0]).text() if rows and self.item(rows[0]) else ""
        text = f"移动 {len(rows)} 项" if len(rows) > 1 else first
        text = p.fontMetrics().elidedText(text, Qt.ElideMiddle, 152)
        p.drawText(pm.rect().adjusted(12, 0, -12, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
        p.end()
        return pm

    # ---------- 落点 ----------
    @staticmethod
    def _mime_has_rows(mime) -> bool:
        return mime.hasFormat(ROW_MIME)

    def _slot_at(self, pos) -> int:
        """鼠标位置 → 插入坑位（0..count）。落在一行的上半＝它前面，下半＝它后面。"""
        item = self.itemAt(pos)
        if item is None:
            return self.count()
        r = self.visualItemRect(item)
        return self.row(item) + (1 if pos.y() > r.center().y() else 0)

    def _show_drop_at(self, slot: int) -> None:
        self._drop_slot = slot
        n = self.count()
        if n == 0:
            y = 3
        elif slot >= n:
            y = min(self.viewport().height() - 2, self.visualItemRect(self.item(n - 1)).bottom() + 1)
        else:
            y = max(1, self.visualItemRect(self.item(slot)).top())
        self._drop_line.setGeometry(6, y - 1, max(1, self.viewport().width() - 12), 2)
        self._drop_line.show()
        self._drop_line.raise_()

    def _hide_drop(self) -> None:
        self._drop_slot = None
        self._drop_line.hide()

    def _auto_scroll(self, pos) -> None:
        bar = self.verticalScrollBar()
        edge = 26
        if pos.y() < edge:
            bar.setValue(bar.value() - 24)
        elif pos.y() > self.viewport().height() - edge:
            bar.setValue(bar.value() + 24)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls() or self._mime_has_rows(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev):
        md = ev.mimeData()
        if md.hasUrls():
            ev.acceptProposedAction()
            return
        if not self._mime_has_rows(md):
            ev.ignore()
            return
        if not self.reorder_allowed():
            ev.ignore()
            return
        self._auto_scroll(ev.pos())
        self._show_drop_at(self._slot_at(ev.pos()))
        ev.acceptProposedAction()

    def dragLeaveEvent(self, ev):
        self._hide_drop()
        super().dragLeaveEvent(ev)

    def dropEvent(self, ev):
        md = ev.mimeData()
        self._hide_drop()
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.toLocalFile()]
            if paths:
                self.sig_add_paths.emit(paths)
                ev.acceptProposedAction()
            return
        if not self._mime_has_rows(md) or not self.reorder_allowed():
            ev.ignore()
            return
        try:
            rows = [int(r) for r in json.loads(bytes(md.data(ROW_MIME)).decode("ascii"))]
        except (ValueError, TypeError, UnicodeDecodeError):
            ev.ignore()
            return
        slot = self._slot_at(ev.pos())
        self.sig_move_rows.emit(rows, slot)
        ev.acceptProposedAction()


class PlaylistPanel(QWidget):
    sig_play_index = pyqtSignal(int)
    sig_remove_index = pyqtSignal(int)
    sig_remove_rows = pyqtSignal(list)
    sig_move_steps = pyqtSignal(list)      # [(源行, 插入坑位), …]：主窗口逐条递给内核
    sig_clear = pyqtSignal()
    sig_add_paths = pyqtSignal(list)
    sig_shuffle_toggle = pyqtSignal()
    sig_close = pyqtSignal()

    def __init__(self, player: MpvPlayer, parent=None, cfg=None):
        # `cfg` 只为悬停提示里的**键名**而存在（乱序那颗）。不给就按默认表算，
        # 而不是抄一句「（S）」——抄的那句在用户改键之后就是谎话，而改键的人才会去看悬停。
        super().__init__(parent)
        self.p = player
        self._cfg = cfg if cfg is not None else {}
        self.setObjectName("Panel")
        self.setAcceptDrops(True)
        self.setMinimumWidth(240)

        self._items: list[dict] = []
        self._cur = -1                   # 上一次画成"当前条目"的行号（只为少改几行样式）
        # ---- 计量（判据数这些，不掐表）----
        self.pushes = 0                  # 收到多少次 playlist 推送
        self.rebuilds = 0                # 真整表重建了几次
        self._pending: list | None = None
        self._coalesce = QTimer(self)
        self._coalesce.setSingleShot(True)
        self._coalesce.setInterval(COALESCE_MS)
        self._coalesce.timeout.connect(self._rebuild_now)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 10, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(4)
        root.addLayout(head)
        title = QLabel("播放列表", self)
        title.setObjectName("PanelTitle")
        head.addWidget(title)
        self.lbl_count = QLabel("0", self)
        self.lbl_count.setObjectName("PanelCount")
        head.addWidget(self.lbl_count)
        head.addStretch(1)
        self.btn_shuffle = self._head_btn(head, "shuffle", "乱序",
                                          lambda: self.sig_shuffle_toggle.emit(),
                                          actions=("shuffle",))
        self.btn_add = self._head_btn(head, "plus", "添加文件/文件夹", self._pick_files)
        self.btn_clear = self._head_btn(head, "close", "清空列表", lambda: self.sig_clear.emit())

        self.filter = SearchField(self, "筛选片名…", "筛选播放列表")
        self.filter.textChanged.connect(self._apply_filter)
        root.addWidget(self.filter)

        self.list = _PlaylistList(self)
        self.list.setObjectName("PlaylistList")
        self.list.setItemDelegate(_PlaylistDelegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.reorder_allowed = lambda: not self._filtering()
        # 横向滚动条必须关掉：条目是长绝对路径时它会冒出来，而样式表把滚动条槽设成
        # 透明，透明槽在截屏里露出一片抖动噪点（真机截图目检发现的）。列表项一律靠
        # 省略号截断，用户看的是文件名，不是路径。
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.ElideMiddle)
        self.list.setWordWrap(False)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.itemDoubleClicked.connect(lambda _i: self._play_current_row())
        self.list.itemActivated.connect(lambda _i: self._play_current_row())
        self.list.sig_add_paths.connect(self.sig_add_paths)
        self.list.sig_move_rows.connect(self._on_move_rows)
        self.list.sig_delete_rows.connect(self.sig_remove_rows)
        root.addWidget(self.list, 1)

        # 空列表的正脸：不摆一行灰字在角落，而是"这块地方正等着东西"。
        # 作为 viewport 的孩子铺满并让鼠标穿透——拖放、右键、滚轮仍然落在列表上。
        self._empty = QWidget(self.list.viewport())
        self._empty.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        ev = QVBoxLayout(self._empty)
        ev.setContentsMargins(18, 18, 18, 18)
        ev.setSpacing(8)
        ev.addStretch(1)
        art = QLabel(self._empty)
        art.setPixmap(icons.pixmap("playlist", 30, theme.TEXT_FAINT))
        art.setAlignment(Qt.AlignCenter)
        ev.addWidget(art)
        title = QLabel("列表还空着", self._empty)
        title.setObjectName("EmptyTitle")
        title.setAlignment(Qt.AlignCenter)
        ev.addWidget(title)
        sub = QLabel("把文件拖进来，或点右上角的 +", self._empty)
        sub.setObjectName("HeroHint")
        sub.setAlignment(Qt.AlignCenter)
        ev.addWidget(sub)
        self._empty_title, self._empty_sub = title, sub
        ev.addStretch(1)
        self._empty.hide()
        self.list.viewport().installEventFilter(self)
        self.scroll_feel = scroll_feel.attach(self.list)
        self.hover_feel = motion.attach_hover(self.list)

        tip = QWidget(self)
        tl = QHBoxLayout(tip)
        tl.setContentsMargins(0, 0, 0, 0)
        self.hint = mk_hint("把文件或文件夹拖进来即可播放", tip)
        tl.addWidget(self.hint)
        tl.addStretch(1)
        root.addWidget(tip)

        self.p.sig_playlist.connect(self.rebuild)
        self.p.sig_pos.connect(self._highlight)

    # ---------- 构建 ----------
    def _head_btn(self, row: QHBoxLayout, icon: str, tip: str, slot,
                  actions: tuple = ()) -> QToolButton:
        b = QToolButton(self)
        b.setObjectName("ToolBtn")
        b.setAutoRaise(True)
        b.setIcon(icons.icon(icon, 16, theme.TEXT_DIM, theme.TEXT))
        b.setIconSize(icons.size_hint(16))
        b.setFixedSize(M.btn_s, M.btn_s)
        bind_key_tip(b, self._cfg, tip, actions)
        b.setFocusPolicy(Qt.NoFocus)   # 头部按钮不抢焦点，Space/Enter 留给播放控制与列表
        b.clicked.connect(slot)
        row.addWidget(b)
        return b

    def _pick_files(self) -> None:
        from PyQt5.QtWidgets import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(
            self, "添加媒体文件", os.path.expanduser("~"),
            "媒体文件 (*.mp4 *.mkv *.mov *.avi *.wmv *.flv *.webm *.m4v *.ts *.mpg "
            "*.mp3 *.flac *.wav *.m4a *.aac *.ogg *.opus *.wma *.ape *.aif *.aiff *.dts "
            "*.rm *.rmvb *.vob *.m3u8 *.m3u);;所有文件 (*)")
        if paths:
            self.sig_add_paths.emit(paths)

    # ---------- 内核 → 界面 ----------
    def rebuild(self, playlist: list) -> None:
        """信号入口：高频、可合并。只把"最后一份"记下来并排一次窗，真活儿在 `_rebuild_now`。

        分成两个方法而不是在同一个方法里判断，是为了让判据能分别数：
        推送次数（`pushes`）与真重建次数（`rebuilds`）——合并生效的证据就是前者远大于后者，
        而把合并拆掉（rebuild 直连 _rebuild_now）时两者会一模一样。
        """
        self.pushes += 1
        self._pending = list(playlist or [])
        if not self._coalesce.isActive():
            self._coalesce.start()

    @staticmethod
    def _entry_path(it: dict) -> str:
        return str(it.get("filename") or it.get("playlist_path") or "")

    def _rebuild_now(self) -> None:
        self.rebuilds += 1
        playlist = self._pending or []
        # 重建会清空选择：把"选的是哪几部片"按路径记下，画完再按同样的路径选回来。
        sel_paths = {self._entry_path(self._items[self.list.row(it)])
                     for it in self.list.selectedItems()
                     if 0 <= self.list.row(it) < len(self._items)}
        self._items = list(playlist or [])
        self.list.blockSignals(True)
        self.list.clear()
        for i, it in enumerate(self._items):
            # mpv 的 playlist[].filename 是**完整路径/URL**，直接当标题会让侧栏变成
            # 一列 C:\Users\... 的截断串（真机截图目检发现）。成熟播放器一律
            # "元数据标题优先，没有就取文件名"，完整路径放 tooltip。
            path = self._entry_path(it)
            name = str(it.get("title") or "").strip() or os.path.basename(path.rstrip("/"))
            if not name:
                name = str(it.get("id") or "") or "未命名条目"
            item = QListWidgetItem(name)
            item.setToolTip(path)
            item.setData(Qt.UserRole, path)
            cur = bool(it.get("playing") or it.get("current"))
            item.setData(ROLE_CURRENT, cur)
            # ACCENT_TEXT：当前条目这一行同时是"选中行"（底=BG_ACTIVE），
            # 主色原色压在这块底上只有 3.7:1，六档里有一档（紫）连 3:1 都勉强。
            item.setForeground(QColor(theme.ACCENT_TEXT if cur else theme.TEXT))
            if cur:
                item.setFont(_bold(item.font()))
            self.list.addItem(item)
            if path and path in sel_paths:
                item.setSelected(True)
        self.list.blockSignals(False)
        self._cur = -1
        self._apply_filter()
        self._highlight(self._current_index())

    def _current_index(self) -> int:
        for i, it in enumerate(self._items):
            if it.get("playing") or it.get("current"):
                return i
        return -1

    def _style_row(self, i: int, cur: bool) -> None:
        it = self.list.item(i)
        if it is None:
            return
        it.setData(ROLE_CURRENT, bool(cur))
        it.setForeground(QColor(theme.ACCENT_TEXT if cur else theme.TEXT))
        it.setFont(_bold(it.font()) if cur else _plain(it.font()))
        self.list.viewport().update()

    def _highlight(self, idx: int) -> None:
        """把内核报的"正在播第几行"画出来。

        `sig_pos` 是高频信号（每几百毫秒一拍）：同一行不重复改样式，也不把视图拽回去
        ——用户正滚到别处挑片时，每一拍再 scrollToItem 一次等于列表自己往上弹。
        多选进行中（≥2 行）也不改当前行：否则用户刚框住五部片，下一拍位置更新就把
        选择打回一部。
        """
        if idx < 0 or idx >= self.list.count():
            return
        if len(self.list.selectedItems()) <= 1:
            self.list.blockSignals(True)
            self.list.setCurrentRow(idx)
            self.list.blockSignals(False)
        if idx == self._cur:
            return
        old, self._cur = self._cur, idx
        if 0 <= old < self.list.count():
            self._style_row(old, False)
        self._style_row(idx, True)
        it = self.list.item(idx)
        if it is not None and not it.isHidden():
            self.list.scrollToItem(it)

    # ---------- 空列表 ----------
    def eventFilter(self, obj, ev):
        if obj is self.list.viewport() and ev.type() == QEvent.Resize:
            self._empty.setGeometry(self.list.viewport().rect())
        return super().eventFilter(obj, ev)

    def _sync_empty(self, total: int, shown: int, filtering: bool) -> None:
        empty = total <= 0 or (filtering and shown <= 0)
        if empty:
            if total <= 0:
                self._empty_title.setText("列表还空着")
                self._empty_sub.setText("把文件拖进来，或点右上角的 +")
            else:
                self._empty_title.setText("没有匹配的条目")
                self._empty_sub.setText("换个词试试")
            self._empty.setGeometry(self.list.viewport().rect())
        self._empty.setVisible(empty)
        if empty:
            self._empty.raise_()

    # ---------- 筛选 ----------
    def _filtering(self) -> bool:
        return bool(self.filter.text().strip())

    def _apply_filter(self, *_a) -> None:
        word = self.filter.text().strip().lower()
        shown = 0
        for i in range(self.list.count()):
            it = self.list.item(i)
            hit = not word or word in it.text().lower() or word in it.toolTip().lower()
            it.setHidden(bool(word) and not hit)
            shown += 0 if it.isHidden() else 1
        total = self.list.count()
        self.lbl_count.setText(f"{shown}/{total}" if word else f"{total}")
        self._sync_empty(total, shown, bool(word))
        if word:
            self.hint.setText(f"筛选命中 {shown}/{total}" if shown
                              else "没有匹配的条目，换个词试试")
        elif total:
            self.hint.setText("拖动排序 · 双击播放 · Delete 移除")
        else:
            self.hint.setText("把文件或文件夹拖进来即可播放")
        self.hint.setToolTip("筛选生效时不能拖动排序：隐藏的行会让落点没有唯一含义"
                             if word and total else "")

    # ---------- 界面 → 内核 ----------
    def _play_current_row(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.sig_play_index.emit(row)

    def _context_menu(self, pos) -> None:
        # 空白处右键：itemAt 回 None。本机 PyQt5 5.15.2 上 `row(None)` 其实回 −1、
        # **不抛**（tests/probe_playlist_neg.py 量过），所以这里不是"防崩"，是防
        # "弹出两个点得动、点下去什么也不发生"的菜单：`row=-1` 会一路走到
        # `set_property playlist-pos -1`／`playlist-remove -1`，内核两条都拒
        # （unsupported format for accessing property／invalid parameter，同一趟量过）。
        # 空白处的正确语义是"只给清空列表"，两条针对行的动作必须置灰。
        item = self.list.itemAt(pos)
        row = self.list.row(item) if item is not None else -1
        chosen_rows: list[int] = []
        if row >= 0:
            selected = {self.list.row(it) for it in self.list.selectedItems() if not it.isHidden()}
            if row not in selected:
                # 右键点在没选中的行上：先把选择落到这一行，再给菜单——否则"从列表移除"
                # 移除的是另一行，菜单看的是这行、动的是那行。
                self.list.blockSignals(True)
                self.list.setCurrentRow(row)
                self.list.blockSignals(False)
                selected = {row}
            chosen_rows = sorted(selected)
        m = QMenu(self)
        act_play = m.addAction("播放")
        act_remove = m.addAction("从列表移除")
        act_play.setEnabled(row >= 0)
        act_remove.setEnabled(row >= 0)
        act_many = None
        if len(chosen_rows) > 1:
            act_many = m.addAction(f"移除所选 {len(chosen_rows)} 项")
            act_many.setEnabled(True)
        m.addSeparator()
        act_clear = m.addAction("清空列表")
        chosen = m.exec(self.list.mapToGlobal(pos))
        if chosen is act_play and row >= 0:
            self.sig_play_index.emit(row)
        elif chosen is act_remove and row >= 0:
            self.sig_remove_index.emit(row)
        elif act_many is not None and chosen is act_many and chosen_rows:
            self.sig_remove_rows.emit(chosen_rows)
        elif chosen is act_clear:
            self.sig_clear.emit()

    def _on_move_rows(self, rows: list, slot: int) -> None:
        """拖动落点 → 一串 `playlist-move` 步骤（界面只算"怎么挪"，顺序仍由内核回填）。

        多重选择按"整块插入"处理：先把选中块按原顺序摘出来，再插到落点对应的空位。
        步骤用模拟算：每一步把"下一个该在的位置"上缺的那一项从后面搬到前面。
        只产生"从后往前搬"（j > t），因为这版内核 `playlist-move` 的落点语义在
        j > t 时最直白（最终停在 t；j < t 时会落成 t−1，见 probe_playlist_move）。
        """
        n = len(self._items)
        rows = sorted({int(r) for r in (rows or []) if 0 <= int(r) < n})
        if not rows or self._filtering():
            return
        slot = max(0, min(n, int(slot)))
        sel = set(rows)
        paths = [self._entry_path(it) for it in self._items]
        rest = [i for i in range(n) if i not in sel]
        # 落点在"去掉选中项之后"的坐标：选中项里排在落点之前的那些会被一起摘走。
        p = slot - sum(1 for r in rows if r < slot)
        desired = [paths[i] for i in rest[:p]] + [paths[r] for r in rows] + \
                  [paths[i] for i in rest[p:]]
        if desired == paths:
            return
        cur = list(paths)
        steps: list[list[int]] = []
        for t in range(n):
            if cur[t] == desired[t]:
                continue
            j = cur.index(desired[t], t + 1)
            steps.append([j, t])
            cur.insert(t, cur.pop(j))
        if steps:
            self.sig_move_steps.emit(steps)

    # ---------- 拖放（列表之外的区域）----------
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.toLocalFile()]
        if paths:
            self.sig_add_paths.emit(paths)
            ev.acceptProposedAction()


def _bold(f):
    f.setBold(True)
    return f


def _plain(f):
    f.setBold(False)
    return f
