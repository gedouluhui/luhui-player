"""快捷键一览页：把生效表排版成**两列表格**（纯函数）＋一个只读对话框。

为什么不是 `QPlainTextEdit` + `.ljust()`：那是旧版的做法，左边键名列按**字符数**补空格，
而控件用的是比例字体——中文和字母的宽度差得很远，于是每一行的动作描述起始位置都不一样，
整页看上去像没对齐的草稿。表格由排版引擎按列宽摆放，不依赖"空格正好等于几个像素"这个
根本不成立的假设；顺带还保住了选中复制与 Cmd/Ctrl+滚轮缩放。

分组是给人找键用的：66 项动作平铺成一张长表，用户按 F1 之后还得自己扫三遍。分组表
`GROUPS` 必须覆盖 `keymap.DEFAULT` 的每一个动作（tests/test_help_page.py 用集合对账：
少一项落进"其他"就判红），新增动作忘了归组会被测试当场点出来，而不是悄悄挤进"其他"。
"""
from __future__ import annotations

import html

from . import theme

# 分类只按**动作名**分，不按说明文字（说明会随改键/措辞变化，动作名是稳定主键）。
# 顺序就是页面上的顺序：先"看片时最常按的"，后"设置与文件"。
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("播放与跳转", ("toggle_play", "back5", "fwd5", "step_back", "step_fwd",
                    "prev", "next", "prev_chapter", "next_chapter",
                    "speed_down", "speed_up", "speed_reset", "shuffle", "loop",
                    "ab_a", "ab_b", "ab_clear")),
    ("跳到进度", tuple(f"jump_{n}" for n in range(0, 100, 10))),
    ("音量与声音", ("vol_up", "vol_down", "mute", "tone_cycle", "eq_reset",
                    "menu_audio", "menu_device")),
    ("字幕", ("sub_delay_down", "sub_delay_up", "sub_size_down", "sub_size_up",
              "menu_sub")),
    ("画面与变换", ("rotate_cycle", "aspect_cycle", "crop_cycle", "keepaspect_toggle",
                    "panscan_toggle", "mirror_h_toggle", "mirror_v_toggle",
                    "transform_reset")),
    ("截图与导出", ("screenshot", "burst_shot", "export_gif")),
    ("窗口与面板", ("fullscreen", "exit_fullscreen", "toggle_playlist",
                    "toggle_library", "toggle_tweak", "toggle_transform",
                    "toggle_stats", "always_on_top", "quit")),
    ("文件与工具", ("open_files", "open_dir", "media_info", "keymap_edit",
                    "sleep_timer", "settings", "help_keys")),
)
OTHER = "其他"


def _esc(s) -> str:
    """唯一的转义入口：测试能把这一句换成恒等函数，证明"不转义会被抓住"。"""
    return html.escape(str(s))

# 动作名 → 分类名。同一动作在 GROUPS 里写两遍会被这里覆盖成后一个；测试对账
# "每个动作恰在一组里"时会把重复组也抓出来，所以这里不必自己再查重。
_CATEGORY: dict[str, str] = {act: name for name, acts in GROUPS for act in acts}


def merge_rows(rows) -> list[tuple[str, tuple[str, ...], str]]:
    """`(键, 动作, 说明)` 原表 → 一项动作一行：多把键并进同一条，顺序按首次出现的顺序。

    动作 + 说明一起做键（不是只按动作名）：说明是全表唯一的语义标签，如果导入的旧表
    里同一动作带着两种措辞，硬合并会让其中一句凭空消失——两行都留着比"猜哪句对"诚实。
    """
    order: list[tuple[str, str]] = []
    keys: dict[tuple[str, str], list[str]] = {}
    for seq, act, desc in rows:
        gk = (str(act), str(desc))
        if gk not in keys:
            keys[gk] = []
            order.append(gk)
        s = str(seq)
        if s not in keys[gk]:
            keys[gk].append(s)
    return [(act, tuple(keys[(act, desc)]), desc) for act, desc in order]


def group_items(items) -> list[tuple[str, list[tuple[tuple[str, ...], str]]]]:
    """压好的条目按 GROUPS 分桶；桶空就不画，认不出动作的落进"其他"（放最后）。"""
    buckets: dict[str, list[tuple[tuple[str, ...], str]]] = {
        name: [] for name, _ in GROUPS}
    buckets[OTHER] = []
    for act, seqs, desc in items:
        buckets[_CATEGORY.get(act, OTHER)].append((seqs, desc))
    return [(name, buckets[name]) for name in [n for n, _ in GROUPS] + [OTHER]
            if buckets[name]]


def group_rows(rows) -> list[tuple[str, list[tuple[tuple[str, ...], str]]]]:
    """压行后再分桶（给不关心筛选的调用方留的直路）。"""
    return group_items(merge_rows(rows))


def _hay(act: str, seqs: tuple[str, ...], desc: str) -> str:
    """搜索用的整行文本：说明、分组名、每一把键。

    **不含动作名**（toggle_play / back5 这些内部标识）：它们不出现在界面上，
    算进来会让搜 "K" 命中 "back5"——用户看到一条跟 K 毫无关系的"后退 5 秒"。
    """
    return " ".join((desc, _CATEGORY.get(act, OTHER), *seqs)).casefold()


def filter_items(items, query: str):
    """按关键词筛**压行后的条目**，不是先筛键再压行。

    这个顺序是刻意的：一条动作挂着 Space 与 K 两把键，搜 "K" 时那一行仍然要显示
    "Space 或 K"——只留下命中的那把键会让人以为另一把不生效。空查询原样返回。
    """
    q = str(query or "").strip().casefold()
    if not q:
        return list(items)
    q_flat = q.replace(" ", "")
    out = []
    for act, seqs, desc in items:
        hay = _hay(act, seqs, desc)
        if q in hay or (q_flat and q_flat in hay.replace(" ", "")):
            out.append((act, seqs, desc))
    return out


def _key_html(seqs: tuple[str, ...]) -> str:
    """一把或多把键。多把用暗一档的"或"隔开，免得把一串键读成一个组合。"""
    sep = f'<span style="color:{theme.TEXT_FAINT}"> 或 </span>'
    return sep.join(_esc(s) for s in seqs)


def _no_match_html(query: str) -> str:
    """搜不到时说的是"没搜到"，不是画一张空表让人以为页面坏了。"""
    return (f'<p style="margin:12px 0 0 0; color:{theme.TEXT_DIM}">'
            f"没有匹配「{_esc(query)}」的动作或按键。"
            f"可以换个词：试试「截图」「Ctrl」或「字幕」。</p>")


def build_help_html(rows, edit_keys: str = "", query: str = "") -> str:
    """整页 HTML。所有来自配置/键表的文字都过 `_esc`：

    键名和说明是**用户可改/可导入**的，把 `<`、`&` 直接拼进富文本轻则吃掉半个词，
    重则让 QTextDocument 从那里开始把后面整段当标签解析——快捷键页不该是注入面。

    `query` 非空时表头改成"筛出 N / 共 M 项"，**分母永远是整张表**：只写"共 N 项"
    会让用户以为自己的键表被筛掉了，而事实是这一页暂时只画匹配项。
    """
    all_items = merge_rows(rows)
    items = filter_items(all_items, query)
    n_act = len(items)
    n_bind = sum(len(seqs) for _a, seqs, _d in items)
    edit = (f'（按 {_esc(edit_keys)} 可改）'
            if edit_keys else "")
    if str(query or "").strip():
        head_text = (f"筛出 {n_act} / {len(all_items)} 项动作、{n_bind} 条绑定{edit}")
    else:
        head_text = f"共 {n_act} 项动作、{n_bind} 条绑定{edit}"
    parts = [f'<p style="margin:0 0 4px 0; color:{theme.TEXT_DIM}">{head_text}</p>']
    if str(query or "").strip() and not items:
        parts.append(_no_match_html(query))
        return "".join(parts)
    for name, bucket in group_items(items):
        parts.append(f'<p style="margin:14px 0 4px 0; color:{theme.ACCENT_TEXT};'
                     f' font-weight:600">{_esc(name)}</p>')
        # width="100%" + 固定比例的键列：两个 td 的宽度都由表格定，行与行天然对齐。
        # 键格**不许 nowrap**：全屏那条挂着四把键（Return 或 Enter 或 F 或 Alt+Return），
        # 它的最小内容宽比 44% 还宽，nowrap 会把整张表的键列撑到 80% 宽，说明列被推到
        # 屏幕右边——同一页里只有这一组的说明不和别人对齐（实测 x=428 vs 221）。
        # 键名内部没有空格，换行只会发生在"或"两侧，不会把 Ctrl+PgDown 拆开。
        # 隔行底色用 bgcolor（Qt 富文本对 tr 的 CSS background 支持不稳，属性更保险）。
        cells = []
        for i, (seqs, desc) in enumerate(bucket):
            bg = theme.BG_RAISE if i % 2 else theme.BG
            cells.append(
                f'<tr bgcolor="{bg}">'
                f'<td width="44%" style="color:{theme.TEXT};'
                f' font-family:Consolas,Cascadia Mono,monospace">{_key_html(seqs)}</td>'
                f'<td style="color:{theme.TEXT_DIM}">{_esc(desc)}</td></tr>')
        parts.append('<table width="100%" cellspacing="0" cellpadding="5">'
                     + "".join(cells) + "</table>")
    return "".join(parts)


def help_dialog_class():
    """延迟导入 QtWidgets：这个模块的纯函数部分要被不建 QApplication 的测试直接用。"""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton,
                                 QTextBrowser, QVBoxLayout)

    from . import scroll_feel
    from .dialog_shell import AppDialog

    class HelpDialog(AppDialog):
        def __init__(self, rows, edit_keys: str = "", parent=None):
            super().__init__(parent)
            self.setWindowTitle("快捷键一览")
            self.resize(560, 720)
            self._rows = list(rows)
            self._edit_keys = edit_keys
            lay = QVBoxLayout(self)
            lay.setContentsMargins(20, 16, 20, 14)
            lay.setSpacing(10)

            head = QVBoxLayout()
            head.setSpacing(2)
            title = QLabel("快捷键一览", self)
            title.setObjectName("DialogTitle")
            hint = QLabel("按动作或按键搜索；表里的键名可以整段选中复制。", self)
            hint.setObjectName("DialogHint")
            head.addWidget(title)
            head.addWidget(hint)
            lay.addLayout(head)

            self.search = QLineEdit(self)
            self.search.setPlaceholderText("查找动作或按键…（例如：字幕 / Ctrl / 截图）")
            self.search.setClearButtonEnabled(True)
            self.search.textChanged.connect(self._rerender)
            lay.addWidget(self.search)

            self.view = QTextBrowser(self)
            self.view.setObjectName("HelpView")
            self.view.setFrameShape(QTextBrowser.NoFrame)
            self.view.setOpenLinks(False)
            self.view.document().setDocumentMargin(12)
            lay.addWidget(self.view, 1)
            self.scroll_feel = scroll_feel.attach(self.view)

            foot = QHBoxLayout()
            foot.addStretch(1)
            self.btn_close = QPushButton("关闭", self)
            self.btn_close.setFocusPolicy(Qt.NoFocus)
            self.btn_close.clicked.connect(self.accept)
            foot.addWidget(self.btn_close)
            lay.addLayout(foot)

            self._rerender("")
            self.search.setFocus()

        def _rerender(self, query: str) -> None:
            self.view.setHtml(build_help_html(self._rows, self._edit_keys, query))
            self.view.verticalScrollBar().setValue(0)

    return HelpDialog
