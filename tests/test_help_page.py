# -*- coding: utf-8 -*-
"""快捷键一览页验收：两列表格对齐、分组完整、转义、条目一条不丢。**offscreen**。

用法：
    python tests/test_help_page.py            # 验收
    python tests/test_help_page.py --falsify  # 证伪：坏写法必须被同一条判据抓住

为什么要这几把尺：
1. **对齐**不是审美偏好，是"这一页到底是表还是草稿"。旧版用 `.ljust(字符数)` 在比例
   字体上补空格，动作说明的起点逐行漂移。判据不 grep 源码，而是在真 QTextBrowser 里
   用 `cursorRect()` 读每一行第二格的原点：表格排出来的 x 必须逐行相等。
2. **分组完整**：65 项动作平铺没人找得到键。`GROUPS` 少一项、同一动作塞进两组，都必须红。
3. **一条不丢**：分组只是给人看的，绝不能因为分桶把某个动作漏掉。判据把渲染后的纯文本
   和生效表逐条对账（一动作一行，不许多也不许少）。
4. **转义**：键名与说明来自用户可导入的键表，`<b>` 拼进富文本会把后面的字吃掉。
   用一行真带尖括号的样例，判"屏幕上的纯文本里还看得见尖括号"。

证伪纪律：基线绿才记 CAUGHT；变异必须真的落地（NOP 单记失败）。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="gelu-help-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")
os.environ["QT_QPA_PLATFORM"] = "offscreen"
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))
sys.path.insert(0, ROOT)

from PyQt5.QtGui import QTextTable                                       # noqa: E402
from PyQt5.QtWidgets import QApplication, QTextBrowser                   # noqa: E402

from app.ui import help_page as hp, keymap, theme                        # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv[:1])
theme.install(APP, theme.ACCENT_DEFAULT)

results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []

DEFAULT_ROWS = list(keymap.effective(None))
EDIT_KEYS = "/".join(keymap.keys_of(DEFAULT_ROWS, ("keymap_edit",)))


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""),
          flush=True)
    return bool(ok)


def caught(label: str, baseline_ok: bool, bad: list[str], landed: bool = True,
           note: str = "") -> None:
    """变异只有"基线绿 且 变异红 且 变异真落地"才算 CAUGHT。"""
    ok = bool(baseline_ok) and bool(bad) and bool(landed)
    if bad:
        why = "；".join(bad)
    elif not landed:
        why = "变异没落地（NOP）"
    elif not baseline_ok:
        why = "基线不绿"
    else:
        why = "判据没抓到（变异已落地且基线绿）"
    falsified.append((label, ok, f"{why}{('；' + note) if note else ''}"))
    print(f"[{'CAUGHT' if ok else 'MISSED'}] {label}" + (f"  —— {why}" if why else ""),
          flush=True)


def pump(ms: int = 60) -> None:
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()


# ===================== 纯函数判据 =====================
def judge_groups(groups=hp.GROUPS) -> list[str]:
    """每个默认动作恰在一组里；不许多、不许少、不许重复。"""
    bad: list[str] = []
    seen: dict[str, str] = {}
    for name, acts in groups:
        if not acts:
            bad.append(f"空组「{name}」")
        for a in acts:
            if a in seen:
                bad.append(f"动作 {a} 同时在「{seen[a]}」与「{name}」")
            seen[a] = name
    default = {a for _s, a, _d in keymap.DEFAULT}
    missing = sorted(default - set(seen))
    extra = sorted(set(seen) - default)
    if missing:
        bad.append("漏分组：" + "、".join(missing))
    if extra:
        bad.append("表里没有的动作：" + "、".join(extra))
    return bad


def judge_merge(rows) -> list[str]:
    """压行后：动作+说明一条一行；同一动作的多把键不重复；总键数=唯一(键,动作)数。"""
    bad: list[str] = []
    merged = hp.merge_rows(rows)
    want = {(a, d) for _s, a, d in rows}
    got = {(a, d) for a, _ks, d in merged}
    if got != want:
        bad.append(f"条目对不上：多 {len(got - want)} 少 {len(want - got)}")
    total = sum(len(ks) for _a, ks, _d in merged)
    uniq = len({(s, a) for s, a, _d in rows})
    if total != uniq:
        bad.append(f"键数 {total} ≠ 唯一(键,动作) {uniq}")
    for a, ks, _d in merged:
        if len(set(ks)) != len(ks):
            bad.append(f"动作 {a} 的键列里有重复：{ks}")
    return bad


def judge_header(html: str, rows) -> list[str]:
    """表头那两个数必须等于表里的真实条数（不是手抄的常量）。"""
    bad: list[str] = []
    merged = hp.merge_rows(rows)
    n_act = len(merged)
    n_bind = sum(len(ks) for _a, ks, _d in merged)
    needle = f"共 {n_act} 项动作、{n_bind} 条绑定"
    if needle not in html:
        bad.append(f"表头没有「{needle}」")
    if f"按 {EDIT_KEYS} 可改" not in html and EDIT_KEYS:
        bad.append("表头没写成键入口的当前键位")
    return bad


def judge_escape(text: str) -> list[str]:
    """渲染后的纯文本里必须看得见尖括号本身；被当标签吃掉就判红。"""
    bad: list[str] = []
    for needle in ("<b>粗体?</b>", "A&amp;B + <key>"):
        if needle not in text:
            bad.append(f"屏幕上找不到原样文字：{needle!r}")
    return bad


# ===================== 渲染判据 =====================
def make_view(html: str, width: int = 500) -> QTextBrowser:
    v = QTextBrowser()
    v.setFrameShape(QTextBrowser.NoFrame)
    v.document().setDocumentMargin(10)
    v.resize(width, 640)
    v.setHtml(html)
    v.show()
    pump(80)
    return v


def table_metrics(view: QTextBrowser) -> list[tuple[int, list[int], list[int]]]:
    """每个富文本表的(行数, 第一格 x 列表, 第二格 x 列表)，坐标来自 Qt 自己的排版。"""
    out: list[tuple[int, list[int], list[int]]] = []
    for fr in view.document().rootFrame().childFrames():
        if not isinstance(fr, QTextTable):
            continue
        xs0, xs1 = [], []
        for r in range(fr.rows()):
            xs0.append(view.cursorRect(fr.cellAt(r, 0).firstCursorPosition()).x())
            xs1.append(view.cursorRect(fr.cellAt(r, 1).firstCursorPosition()).x())
        out.append((fr.rows(), xs0, xs1))
    return out


def judge_render(view: QTextBrowser, expected, group_names: list[str]) -> list[str]:
    """表格张数=非空组数；总行数=期望条目数；每张表两列各自逐行同 x。"""
    bad: list[str] = []
    tm = table_metrics(view)
    if len(tm) != len(group_names):
        bad.append(f"表格 {len(tm)} 张 ≠ 非空组 {len(group_names)} 组")
    got_rows = sum(n for n, _a, _b in tm)
    if got_rows != len(expected):
        bad.append(f"表里共 {got_rows} 行 ≠ 期望 {len(expected)} 项")
    for i, (n, xs0, xs1) in enumerate(tm):
        if xs0 and (max(xs0) - min(xs0) > 1 or max(xs1) - min(xs1) > 1):
            bad.append(f"第 {i + 1} 张表列起点不齐：键 {sorted(set(xs0))} 说明 {sorted(set(xs1))}")
        if xs0 and xs1 and xs1[0] <= xs0[0]:
            bad.append(f"第 {i + 1} 张表说明列没有排在键列右边")
    if view.horizontalScrollBar().maximum() > 0:
        bad.append(f"出现横向滚动（{view.horizontalScrollBar().maximum()}px）")
    starts = [xs1[0] for _n, _a, xs1 in tm if xs1]
    if starts and max(starts) - min(starts) > 1:
        bad.append(f"不同分组的说明列起点不一致：{sorted(set(starts))}")
    return bad


def rendered_rows(view: QTextBrowser) -> list[tuple[str, str]]:
    """按文档顺序读每个表格行的(键格文本, 说明格文本)。"""
    out: list[tuple[str, str]] = []
    for fr in view.document().rootFrame().childFrames():
        if not isinstance(fr, QTextTable):
            continue
        for r in range(fr.rows()):
            out.append((fr.cellAt(r, 0).firstCursorPosition().block().text(),
                        fr.cellAt(r, 1).firstCursorPosition().block().text()))
    return out


def flat(s: str) -> str:
    return s.replace("\xa0", "").replace(" ", "")


def judge_complete(view: QTextBrowser, rows) -> list[str]:
    """渲染后逐行对账：说明列的顺序与内容必须和压行结果一一对应，键也一条不少。

    不拿 `toPlainText().count(desc)` 判：说明之间会互相包含（"设置…"是
    "快捷键设置…"的子串），子串计数会把两条都算成"出现 2 次"，把好页判红。
    """
    bad: list[str] = []
    rendered = rendered_rows(view)
    want = hp.merge_rows(rows)
    if len(rendered) != len(want):
        bad.append(f"行数 {len(rendered)} ≠ 期望 {len(want)}")
    from collections import Counter

    got_n, want_n = Counter(d for _k, d in rendered), Counter(d for _a, _ks, d in want)
    if got_n != want_n:
        missing = list((want_n - got_n).elements())
        extra = list((got_n - want_n).elements())
        bad.append(f"说明多 {len(extra)} 条{extra[:4]}、少 {len(missing)} 条{missing[:4]}")

    key_bad: list[str] = []
    for _a, seqs, desc in want:
        cells = [flat(k) for k, d in rendered if d == desc]
        if not cells or not any(all(flat(s) in cell for s in seqs) for cell in cells):
            key_bad.append(f"{desc}{seqs}")
    if key_bad:
        bad.append(f"{len(key_bad)} 行的键没画全：{key_bad[:4]}")
    lines = view.toPlainText().splitlines()
    for name, bucket in hp.group_rows(rows):
        if bucket and name not in lines:
            bad.append(f"分组标题「{name}」没画出来")
    return bad


def ref_filter(rows, query: str):
    """测试自己的筛选参考实现（**不许调 product 的 filter_items**）。

    第一版判据直接拿 `hp.filter_items` 算期望，于是 F10（筛选时丢掉兄弟键）永远抓不到：
    变异把期望也一起改了，两边一致——这正是"fixture 预写答案＝自证"那一类假绿。
    这里的 `_CATEGORY` 与分类表仍来自产品（那是事实表），但"怎么筛"由本函数独立写一遍。
    """
    q = str(query or "").strip().casefold()
    q_flat = q.replace(" ", "")
    out = []
    for act, seqs, desc in hp.merge_rows(rows):
        hay = " ".join((desc, hp._CATEGORY.get(act, hp.OTHER), *seqs)).casefold()
        if q and (q in hay or (q_flat and q_flat in hay.replace(" ", ""))):
            out.append((act, seqs, desc))
    return out


def _cell_keys(cell: str) -> tuple[str, ...]:
    """把键格文本拆回键元组（渲染时用 nbsp 包着"或"）。"""
    return tuple(p.strip() for p in cell.replace("\xa0", " ").split(" 或 ") if p.strip())


def judge_filter(view: QTextBrowser, rows, query: str) -> list[str]:
    """筛选只准筛"整条动作"，且分母必须是整张表、同动作的兄弟键一把不丢。"""
    from collections import Counter

    bad: list[str] = []
    all_items = hp.merge_rows(rows)
    want = ref_filter(rows, query)
    got = rendered_rows(view)
    got_n = Counter((_cell_keys(k), d) for k, d in got)
    want_n = Counter((tuple(s), d) for _a, s, d in want)
    if got_n != want_n:
        bad.append(f"筛出的(键,说明)不对：多 {list((got_n - want_n).elements())[:3]}、"
                   f"少 {list((want_n - got_n).elements())[:3]}")
    head = (f"筛出 {len(want)} / {len(all_items)} 项动作、"
            f"{sum(len(s) for _a, s, _d in want)} 条绑定")
    if head not in view.toPlainText():
        bad.append(f"表头没有「{head}」")
    return bad


def judge_no_match(view: QTextBrowser, query: str) -> list[str]:
    """搜不到就明说，不许画一张空表。"""
    bad: list[str] = []
    if table_metrics(view):
        bad.append("没有匹配却还画了表格")
    text = view.toPlainText()
    if f"没有匹配「{query}」" not in text:
        bad.append("没写『没有匹配』")
    return bad


def judge_wiring(src: str) -> list[str]:
    """主窗入口必须真的用新的生成器，而不是把旧纯文本实现留在原地。

    走 AST 而不是在源码文本里 grep：新实现的文档注释里**会提到**旧版用的 `.ljust()`
    （讲清为什么换掉），文本匹配会把那句说明当成坏代码；AST 只看见可执行的名字与属性。
    """
    import ast
    import textwrap

    bad: list[str] = []
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError as exc:
        return [f"入口源码解析不了：{exc}"]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    if "help_page" not in names or "help_dialog_class" not in attrs:
        bad.append("_show_help 没走 help_page")
    if "ljust" in attrs:
        bad.append("_show_help 还在用 .ljust 按字符数对齐")
    if "QPlainTextEdit" in names:
        bad.append("_show_help 还在用 QPlainTextEdit")
    return bad


# ===================== 验收 =====================
def main() -> int:
    rows = DEFAULT_ROWS
    html = hp.build_help_html(rows, EDIT_KEYS)
    view = make_view(html)
    names = [n for n, b in hp.group_rows(rows) if b]

    check("66 项默认动作各自恰好归进一个分组（不重不漏）",
          not judge_groups(), "；".join(judge_groups()))
    merged = hp.merge_rows(rows)
    check("多把键并进同一行动作（Space/K 合成一行），键数等于唯一(键,动作)数",
          not judge_merge(rows),
          f"{len(merged)} 项动作 / {sum(len(k) for _a, k, _d in merged)} 条绑定；"
          + "；".join(judge_merge(rows)))
    check("表头计数由表本身算出，且写出当前改键入口的键位",
          not judge_header(html, rows), "；".join(judge_header(html, rows)))
    check("分组标题全部出现在页面上，且默认表没有动作掉进『其他』",
          all(n in html for n, _b in hp.group_rows(rows)) and "其他" not in names,
          f"{len(names)} 组：" + " / ".join(names))
    bad_render = judge_render(view, hp.merge_rows(rows), names)
    check("两列表格：每张表键列/说明列逐行同 x，行数与压行后一致，无横向滚动",
          not bad_render, "；".join(bad_render))
    bad_complete = judge_complete(view, rows)
    check("渲染后的纯文本一条不丢：每个动作说明恰好出现一次", not bad_complete,
          "；".join(bad_complete))

    # 转义：键名与说明里出现尖括号时，屏幕上要原样看得见，而不是被富文本吃掉。
    tricky = [("A&amp;B + <key>", "tricky", "<b>粗体?</b>")]
    tview = make_view(hp.build_help_html(tricky, "<K>"))
    bad_esc = judge_escape(tview.toPlainText())
    check("键名/说明里的尖括号与 & 原样显示（不拼进标签）", not bad_esc, "；".join(bad_esc))

    # 长键名不得把说明列推歪（nowrap 的键列宽度由表格统一，不是各行自己撑）
    long_rows = rows + [("Ctrl+Alt+Shift+Super+F12", "jump_10", "跳到 10%")]
    lview = make_view(hp.build_help_html(long_rows, EDIT_KEYS))
    bad_long = judge_render(lview, hp.merge_rows(long_rows),
                            [n for n, b in hp.group_rows(long_rows) if b])
    check("超长键名仍在同一张表里对齐（说明列 x 不随行长漂移）", not bad_long,
          "；".join(bad_long))

    # 搜索：只筛整条动作；同一动作的兄弟键一把不丢；分母是整张表。
    fview = make_view(hp.build_help_html(rows, EDIT_KEYS, "静音"))
    bad_f = judge_filter(fview, rows, "静音")
    cells = [k for k, _d in rendered_rows(fview)]
    check("搜索『静音』只留 1 项，Z 与 Shift+, 两把键都还在，表头写 1 / 65",
          not bad_f and len(cells) == 1 and "Z" in cells[0] and "Shift+," in cells[0],
          "；".join(bad_f) + (f" 格子={cells}" if cells else ""))
    cview = make_view(hp.build_help_html(rows, EDIT_KEYS, "CTRL+k"))
    crows = rendered_rows(cview)
    check("搜索不分大小写、容许空格：『CTRL+k』命中『快捷键设置…』",
          not judge_filter(cview, rows, "CTRL+k") and len(crows) == 1
          and crows[0][1] == "快捷键设置…",
          f"{len(crows)} 行 {crows[:1]}")
    nview = make_view(hp.build_help_html(rows, EDIT_KEYS, "zzzz"))
    bad_n = judge_no_match(nview, "zzzz")
    check("搜不到时明说『没有匹配』，不画一张空表骗人", not bad_n, "；".join(bad_n))

    # 真对话框：把生成器接进 Qt 控件，并确认入口接线不是旧实现。
    dlg = hp.help_dialog_class()(rows, EDIT_KEYS)
    dlg.show()
    pump(80)
    cand = table_metrics(dlg.view)
    check("HelpDialog 真建得起来，页面里有非空表格，标题与摘要都在",
          len(cand) >= 3 and dlg.windowTitle() == "快捷键一览"
          and "项动作" in dlg.view.toPlainText(),
          f"{len(cand)} 张表 / {len(hp.group_rows(rows))} 组")
    dlg.search.setText("截图")
    pump(60)
    shot_rows = rendered_rows(dlg.view)
    dlg.search.setText("")
    pump(60)
    back_rows = rendered_rows(dlg.view)
    check("对话框搜索框真的接线：输入『截图』收成 3 行，清空回到 65 行",
          len(shot_rows) == 3 and len(back_rows) == len(merged)
          and hasattr(dlg, "btn_close") and dlg.btn_close.isEnabled(),
          f"筛后 {len(shot_rows)} 行 / 清空 {len(back_rows)} 行")
    dlg.close()

    import inspect

    from app.ui.main_window import MainWindow
    src = inspect.getsource(MainWindow._show_help)
    bad_wire = judge_wiring(src)
    check("主窗 _show_help 接到 help_page，旧的 .ljust + QPlainTextEdit 已下线",
          not bad_wire, "；".join(bad_wire))

    view.close()
    tview.close()
    lview.close()
    fview.close()
    cview.close()
    nview.close()

    ok = sum(1 for _n, good, _d in results if good)
    print(f"\n---- 验收汇总：{ok}/{len(results)} 通过"
          + ("" if ok == len(results) else "；有失败") + " ----", flush=True)
    if "--falsify" in sys.argv:
        falsify(ok == len(results))
    return 0 if ok == len(results) else 1


def falsify(base_ok: bool) -> None:
    print("\n==== 证伪（坏样本必须被抓出来）====", flush=True)
    rows = list(DEFAULT_ROWS)
    groups0 = hp.GROUPS
    html0 = hp.build_help_html(rows, EDIT_KEYS)

    # F1 漏分组：把 quit 从表里删掉 → 覆盖判据必须红
    g1 = tuple((n, tuple(a for a in acts if a != "quit")) for n, acts in groups0)
    caught("F1 GROUPS 漏掉一个动作 → 必须红", base_ok, judge_groups(g1),
           landed=(g1 != groups0))

    # F2 同一动作塞两组
    g2 = groups0 + (("重复组", ("quit",)),)
    caught("F2 同一动作归进两组 → 必须红", base_ok, judge_groups(g2),
           landed=(judge_groups(g2) != judge_groups(groups0)))

    # F3 退回"纯文本 + ljust"：没有表格，说明文字起点逐行漂移
    def bad_plain(rows_, edit_keys="", query=""):
        lines = []
        for act, seqs, desc in hp.merge_rows(rows_):
            lines.append(" 或 ".join(seqs).ljust(20) + desc)
        return '<p style="white-space:pre">' + "\n".join(hp._esc(x) for x in lines) + "</p>"

    old = hp.build_help_html
    hp.build_help_html = bad_plain
    try:
        bview = make_view(hp.build_help_html(rows, EDIT_KEYS))
        names = [n for n, b in hp.group_rows(rows) if b]
        bad3 = judge_render(bview, hp.merge_rows(rows), names)
        bview.close()
    finally:
        hp.build_help_html = old
    caught("F3 退回 .ljust 纯文本（没有表格列）→ 必须红", base_ok, bad3,
           landed=(bad_plain(rows, EDIT_KEYS) != html0))

    # F4 拆掉转义
    tricky = [("A&amp;B + <key>", "tricky", "<b>粗体?</b>")]
    esc0 = hp._esc
    hp._esc = lambda s: str(s)
    try:
        bad4 = judge_escape(make_view(hp.build_help_html(tricky, "<K>")).toPlainText())
        raw = hp.build_help_html(tricky, "<K>")
    finally:
        hp._esc = esc0
    caught("F4 去掉 HTML 转义 → 必须红", base_ok, bad4, landed=("<b>粗体?</b>" in raw))

    # F5 压行不去重：同一把键重复记进键列，表头键数跟着虚高
    rows_dup = rows + [("Space", "toggle_play", "播放 / 暂停")]
    m0 = hp.merge_rows(rows_dup)
    orig_merge = hp.merge_rows

    def dup_merge(rows_):
        order, keys = [], {}
        for seq, act, desc in rows_:
            gk = (str(act), str(desc))
            if gk not in keys:
                keys[gk] = []
                order.append(gk)
            keys[gk].append(str(seq))
        return [(a, tuple(keys[(a, d)]), d) for a, d in order]

    hp.merge_rows = dup_merge
    try:
        bad5 = judge_merge(rows_dup)
        m1 = hp.merge_rows(rows_dup)
    finally:
        hp.merge_rows = orig_merge
    caught("F5 多把键压行时不去重 → 必须红", base_ok, bad5,
           landed=(m1 != m0))

    # F6 只画第一组：内容丢失必须被"一条不丢"抓住
    orig_group = hp.group_items
    hp.group_items = lambda items: orig_group(items)[:1]
    try:
        pview = make_view(hp.build_help_html(rows, EDIT_KEYS))
        bad6 = judge_complete(pview, rows)
        n6 = len(table_metrics(pview))
        pview.close()
    finally:
        hp.group_items = orig_group
    caught("F6 分桶只返回第一组（内容丢失）→ 必须红", base_ok, bad6,
           landed=(n6 == 1 and n6 < len(orig_group(hp.merge_rows(rows)))))

    # F7 接线退回旧实现（源码文本就是变异体会被旧实现替换后的样子）
    old_src = (
        "d = QDialog(self)\n"
        "txt = QPlainTextEdit(d)\n"
        "lines = [' 或 '.join(v).ljust(width + 2) + gk[1] for gk in order]\n"
        "txt.setPlainText(head + '\\n'.join(lines))\n")
    caught("F7 _show_help 接回旧的 ljust + QPlainTextEdit → 必须红", base_ok,
           judge_wiring(old_src), landed=(old_src != "help_page.help_dialog_class"))

    # F8 搜索框不接线：query 传进来照样画整张表
    query = "静音"
    base_q = hp.build_help_html(rows, EDIT_KEYS, query)
    orig_filter = hp.filter_items
    hp.filter_items = lambda items, q: list(items)
    try:
        qview = make_view(hp.build_help_html(rows, EDIT_KEYS, query))
        bad8 = judge_filter(qview, rows, query)
        raw_q = hp.build_help_html(rows, EDIT_KEYS, query)
        qview.close()
    finally:
        hp.filter_items = orig_filter
    caught("F8 搜索被忽略（照样画整张表）→ 必须红", base_ok, bad8,
           landed=(raw_q != base_q))

    # F9 搜不到时只留表头、不写"没有匹配"
    base_none = hp.build_help_html(rows, EDIT_KEYS, "zzzz")
    orig_none = hp._no_match_html
    hp._no_match_html = lambda q: ""
    try:
        nview2 = make_view(hp.build_help_html(rows, EDIT_KEYS, "zzzz"))
        bad9 = judge_no_match(nview2, "zzzz")
        raw_none = hp.build_help_html(rows, EDIT_KEYS, "zzzz")
        nview2.close()
    finally:
        hp._no_match_html = orig_none
    caught("F9 搜不到却不说『没有匹配』→ 必须红", base_ok, bad9,
           landed=(raw_none != base_none))

    # F10 筛选按"键"而不是按"整条动作"：Space/K 搜 K 只剩 K
    def drop_keys(items, q):
        qq = str(q or "").strip().casefold()
        out = []
        for act, seqs, desc in items:
            keep = tuple(s for s in seqs if qq and qq in s.casefold())
            if keep or qq in desc.casefold() or qq in hp._CATEGORY.get(act, "").casefold():
                out.append((act, keep or seqs, desc))
        return out

    base_k = hp.build_help_html(rows, EDIT_KEYS, "K")
    hp.filter_items = drop_keys
    try:
        kview = make_view(hp.build_help_html(rows, EDIT_KEYS, "K"))
        bad10 = judge_filter(kview, rows, "K")
        raw_k = hp.build_help_html(rows, EDIT_KEYS, "K")
        kview.close()
    finally:
        hp.filter_items = orig_filter
    caught("F10 筛选把同一动作的兄弟键筛掉（Space/K 只剩 K）→ 必须红", base_ok, bad10,
           landed=(raw_k != base_k))

    # 记账自检：每一条都真的跑了判据，且没有"基线不绿还记 CAUGHT"
    tally_bad = [lbl for lbl, ok, _d in falsified if ok and not base_ok]
    falsified.append(("记账自检", not tally_bad, str(tally_bad)))
    got = sum(1 for _l, ok, _d in falsified if ok)
    print(f"---- 证伪收口：抓到 {got}/{len(falsified)}"
          f"（漏网/未落地 {len(falsified) - got}） ----", flush=True)


if __name__ == "__main__":
    sys.exit(main())
