"""快捷键编辑页：改键 / 删键 / 加键 / 恢复默认 / 导入 / 导出。

三条设计决定，都是被这份项目里已有的坑逼出来的：

1. **按键捕获而不是手打键名**。`QKeySequence("PgDn")` 解析成 Qt::Key_UNKNOWN，装上不报错
   也永不响（实测见 tests/probe_keyseq.py），而它看起来完全像个合法写法。让人按下组合键、
   由 `keymap.from_event` 从真实事件构造，这类死键名根本进不来；手打的串还要靠
   `keymap.dead()` 兜，兜得住也说不清"为什么你写的 PgDn 不响而 PgDown 响"。
2. **有毛病就不许保存**。同键双绑、按不出、表外动作名三种都会静默失效（按一次响两条或
   一条都不响）。这里不"提醒一下还是让你存"：存进去的那一刻起，症状就变成"用户的电脑坏了"。
3. **保存的是 diff，不是整张表**（`keymap.diff_of`）。存整表的话，将来版本新增一条默认
   快捷键会被用户这份旧快照静默吃掉——他从来没删过它。

删掉的默认键位在这里看不见（表里只列生效的），要恢复用"恢复默认"或在"新增"里把那把键
加回来。这一条写进底部说明，不假装它是个完美设计。
"""
from __future__ import annotations

import os

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout,
                             QHeaderView, QLabel, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout, QWidget)

from .. import config as cfgmod
from . import keymap, theme
from .dialog_shell import AppDialog

COLUMNS = ("按键", "动作", "状态")
NEW = "__new__"           # 捕获态的两种目标：某一行，还是"给新动作加一条"


class _CaptureTarget(QWidget):
    """捕获期间的焦点落脚点。

    为什么单独一个控件而不是拦 QDialog.keyPressEvent：表格此刻还带着焦点的话，按键先被
    它吃掉（方向键翻选中行），对话框根本看不到事件。焦点挪到这个空控件上，事件路径只有一条。
    """

    def __init__(self, dlg: "KeymapDialog"):
        super().__init__(dlg)
        self._dlg = dlg
        self.setFixedSize(1, 1)
        self.setFocusPolicy(Qt.StrongFocus)

    def keyPressEvent(self, ev):
        if self._dlg.feed_key(int(ev.key()), int(ev.modifiers())) != "none":
            return
        super().keyPressEvent(ev)


class KeymapDialog(AppDialog):
    """读写 cfg["keymap"] 的编辑页；保存后把生效表交回主窗口重装快捷键。"""

    sig_saved = pyqtSignal(list)        # list[(键, 动作, 说明)]

    def __init__(self, cfg, parent: QWidget | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle(f"快捷键设置 - {cfgmod.APP_TITLE}")
        self.setObjectName("KeymapDialog")
        self.resize(620, 640)

        self._rows: list[tuple[str, str, str]] = []
        self._target = None             # None / 行号 / NEW
        self._default_pairs = {(s, a) for s, a, _d in keymap.DEFAULT}
        self._default_seqs = {s for s, _a, _d in keymap.DEFAULT}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(10)

        # 标题说这一页是什么，副标题说这一页怎么用。合并成一行 15px 粗体的时代，
        # 用户扫过来只看到一句很长的命令。 
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = QLabel("快捷键", self)
        title.setObjectName("DialogTitle")
        hint = QLabel("按下组合键即可改键；有冲突或未生效的键位时不能保存", self)
        hint.setObjectName("DialogHint")
        hint.setWordWrap(True)
        titles.addWidget(title)
        titles.addWidget(hint)
        lay.addLayout(titles)

        self.table = QTableWidget(self)
        self.table.setColumnCount(len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        hdr = self.table.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hdr.setHighlightSections(False)
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 168)
        self.table.setMinimumHeight(240)
        lay.addWidget(self.table, 1)

        self._cap = _CaptureTarget(self)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("新增给：", self))
        self.combo = QComboBox(self)
        for act in sorted(keymap.known_actions()):
            self.combo.addItem(f"{keymap.desc_of(act)}（{act}）", act)
        self.combo.setFocusPolicy(Qt.ClickFocus)
        row.addWidget(self.combo, 1)
        self.btn_add = QPushButton("新增一条", self)
        self.btn_add.setFocusPolicy(Qt.NoFocus)
        self.btn_add.clicked.connect(self._start_new)
        row.addWidget(self.btn_add)
        lay.addLayout(row)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        self.btn_change = QPushButton("改键", self)
        self.btn_del = QPushButton("删除这条", self)
        self.btn_reset = QPushButton("恢复默认", self)
        self.btn_imp = QPushButton("导入…", self)
        self.btn_exp = QPushButton("导出…", self)
        for b in (self.btn_change, self.btn_del, self.btn_reset, self.btn_imp, self.btn_exp):
            b.setFocusPolicy(Qt.NoFocus)
        tools.addWidget(self.btn_change)
        tools.addWidget(self.btn_del)
        tools.addWidget(self.btn_reset)
        tools.addStretch(1)
        # 导入/导出动的是整张表，和上面三把改单行的按钮隔开一段，别让手指顺着排。
        tools.addWidget(self.btn_imp)
        tools.addWidget(self.btn_exp)
        lay.addLayout(tools)

        self.note = QLabel("", self)
        self.note.setObjectName("AppTitle")
        self.note.setWordWrap(True)
        self.note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.note)

        foot = QHBoxLayout()
        foot.addStretch(1)
        self.btn_close = QPushButton("关闭", self)
        self.btn_close.setFocusPolicy(Qt.NoFocus)
        self.btn_close.clicked.connect(self.reject)
        self.btn_save = QPushButton("保存", self)
        self.btn_save.setObjectName("Primary")
        self.btn_save.setFocusPolicy(Qt.NoFocus)
        self.btn_save.clicked.connect(self._save)
        foot.addWidget(self.btn_close)
        foot.addWidget(self.btn_save)
        lay.addLayout(foot)

        self.btn_change.clicked.connect(self._start_change)
        self.btn_del.clicked.connect(self._delete_current)
        self.btn_reset.clicked.connect(self._reset_default)
        self.btn_imp.clicked.connect(self._import)
        self.btn_exp.clicked.connect(self._export)
        # 选中行变化必须刷新工具按钮。少了这一句，"改键/删除"只在表格重建时才亮：打开时
        # `_rebuild` 看到 currentRow=-1 就把它俩灰掉了，用户点一行想改键，按钮还是灰的，
        # 只能靠"先新增一条触发 rebuild"歪打正着。tests/test_keymap_dialog.py 的
        # c_tools_disabled_while_capturing 直接按"选一行 → 该可用"这条真实用户路径判。
        self.table.itemSelectionChanged.connect(self._update_tools)

        self.reload_from_cfg()

    # ================= 表 <-> 数据 =================
    def reload_from_cfg(self) -> None:
        """按当前配置重画（主窗口复用同一个实例时调用，同设置页那条理由）。"""
        self._stop_capture()
        self._rows = list(keymap.effective(self.cfg.get("keymap")))
        self._rebuild(keep=None)
        self._validate("")

    def rows(self) -> list[tuple[str, str, str]]:
        return list(self._rows)

    def _source_of(self, seq: str, act: str) -> str:
        if (seq, act) in self._default_pairs:
            return "默认"
        if seq in self._default_seqs:
            return "已改（原键位让给了别的动作）"
        return "自定义"

    def _rebuild(self, keep) -> None:
        """重画表格。`keep` 是行号，画完把选中与焦点挪回那一行。"""
        sel = self.table.currentRow() if keep is None else keep
        bad = keymap.bad_pairs(self._rows)
        self.table.setRowCount(len(self._rows))
        for i, (seq, act, desc) in enumerate(self._rows):
            wrong = (seq, act) in bad
            for col, text in enumerate((keymap.canonical(seq), desc, self._source_of(seq, act))):
                it = QTableWidgetItem(text)
                it.setToolTip(text)          # 状态列会被列宽截断，全文至少还能悬停读到
                if col == 0:
                    it.setForeground(QColor(theme.DANGER_TEXT if wrong else theme.TEXT))
                elif col == 2:
                    it.setForeground(QColor(theme.TEXT_FAINT))
                else:
                    it.setForeground(QColor(theme.TEXT_DIM))
                self.table.setItem(i, col, it)
        if 0 <= sel < len(self._rows):
            self.table.selectRow(sel)
        self._update_tools()

    def _update_tools(self) -> None:
        has = self._selected() is not None
        self.btn_change.setEnabled(has and self._target is None)
        self.btn_del.setEnabled(has and self._target is None)

    def _selected(self) -> int | None:
        r = self.table.currentRow()
        return r if 0 <= r < len(self._rows) else None

    # ================= 捕获 =================
    def _start_change(self) -> None:
        r = self._selected()
        if r is None:
            return
        self._target = r
        self._update_tools()        # 进捕获态立刻把改键/删除灰掉，否则再点一次会把
        self._cap.setFocus()        # _target 换到别的行，而人以为还在改原来那行
        self._set_note(f"请按下要绑给「{self._rows[r][2]}」的键（Esc 取消）", ok=True)

    def _start_new(self) -> None:
        act = self.combo.currentData()
        if not act:
            return
        self._target = NEW
        self._update_tools()        # 同 _start_change：捕获期间这两把按钮不该再点
        self._cap.setFocus()
        self._set_note(f"请按下要绑给「{keymap.desc_of(act)}」的键（Esc 取消）", ok=True)

    def _stop_capture(self) -> None:
        self._target = None
        self.table.clearFocus()
        # 必须在这里把工具按钮状态刷回来。少了这一行，Esc 取消捕获以后"改键/删除"还停在
        # 捕获期间被禁用的样子，要等下一次 _rebuild 才恢复——而取消路径上没有人 rebuild，
        # 于是用户看到"我按了 Esc，按钮全灰了"。tests/test_keymap_dialog.py 逐条盯这个。
        self._update_tools()

    def feed_key(self, key: int, mods: int) -> str:
        """捕获态的入口，也是测试的入口（不靠真弹窗就能验捕获逻辑）。

        返回值是给测试断言用的状态名，给人看的那句走 `_set_note`：
            none   —— 不在捕获态，事件该给谁就给谁
            wait   —— 只按了修饰键，还没到主键，继续等
            cancel —— Esc 取消，回到浏览态
            set:…  —— 绑上了，返回新键串
            bad:…  —— 键不行（按不出/表外动作），留在捕获态让人重按
        """
        if self._target is None:
            return "none"
        if int(key) in (0x01000000, 0x01000001):      # Esc / Cancel：取消，不当键位
            self._stop_capture()
            self._set_note("已取消")
            return "cancel"
        seq = keymap.from_event(key, mods)
        if not seq:
            return "wait"
        why = keymap.reason(seq)
        if why:
            self._set_note(f"{seq} 不能用：{why}", bad=True)
            return f"bad:{why}"
        act = self._rows[self._target][1] if self._target != NEW else str(self.combo.currentData())
        row = (seq, act, keymap.desc_of(act))
        if self._target == NEW:
            self._rows.append(row)
            keep = len(self._rows) - 1
        else:
            self._rows[self._target] = row
            keep = int(self._target)
        self._stop_capture()
        self._rebuild(keep=keep)
        self._validate(f"已绑好：{keymap.canonical(seq)} → {keymap.desc_of(act)}。记得点保存")
        return f"set:{seq}"

    # ================= 编辑 =================
    def _delete_current(self) -> None:
        r = self._selected()
        if r is None:
            return
        seq, act, _d = self._rows.pop(r)
        self._rebuild(keep=min(r, len(self._rows) - 1))
        self._validate(f"删掉了 {keymap.canonical(seq)} → {keymap.desc_of(act)}。记得点保存")

    def _reset_default(self) -> None:
        self._rows = list(keymap.DEFAULT)
        self._rebuild(keep=None)
        self._validate(f"已回到默认 {len(self._rows)} 条。记得点保存")

    # ================= 导入 / 导出 =================
    def _export(self) -> None:
        p, _f = QFileDialog.getSaveFileName(self, "导出快捷键",
                                            os.path.join(cfgmod.DATA_DIR, "快捷键.json"),
                                            "快捷键 (*.json)")
        if not p:
            return
        ok, msg = keymap.export(self._rows, p)
        self._set_note(msg, bad=not ok)

    def _import(self) -> None:
        p, _f = QFileDialog.getOpenFileName(self, "导入快捷键", cfgmod.DATA_DIR, "快捷键 (*.json)")
        if not p:
            return
        rows, err = keymap.read_file(p)
        if err:
            self._set_note(f"没收这个文件：{err}", bad=True)
            return
        self._rows = [(s, a, keymap.desc_of(a)) for s, a in rows]
        self._rebuild(keep=None)
        self._validate(f"读了 {len(self._rows)} 条。确认没问题再点保存")

    # ================= 校验 / 保存 =================
    def _validate(self, msg: str = "") -> None:
        probs = keymap.problems(self._rows)
        self.btn_save.setEnabled(not probs)
        if probs:
            self._set_note("先解决这些才能保存：" + "；".join(probs), bad=True)
        elif msg:
            self._set_note(msg, ok=True)
        else:
            removed = len(self._default_seqs) - len({s for s, _a, _d in self._rows})
            tail = f"（另有 {removed} 条默认键位被删掉，可用新增加回）" if removed else ""
            self._set_note(f"{len(self._rows)} 条绑定，没有冲突{tail}")

    def _set_note(self, text: str, bad: bool = False, ok: bool = False) -> None:
        self.note.setText(text)
        self.note.setStyleSheet(f"color: {theme.DANGER_TEXT if bad else theme.OK if ok else theme.TEXT_DIM};")

    def _save(self) -> None:
        probs = keymap.problems(self._rows)
        if probs:
            self._validate()
            return
        self.cfg["keymap"] = keymap.diff_of(self._rows)
        self.cfg.save()
        self._validate(f"已保存：{len(self._rows)} 条绑定，立即生效")
        self.sig_saved.emit(list(self._rows))
