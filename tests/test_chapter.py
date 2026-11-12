"""章节跳转 + 副字幕 id 判读的离线验收与证伪（不建主窗口、不用内核）。

用法：
    python tests/test_chapter.py            # 全部判据必须 PASS
    python tests/test_chapter.py --falsify  # 已知坏实现必须被抓到（MISSED/NOP 即失败）

覆盖：
  · `chapter_target()` 的边界（首章/末章/无章节/无当前章）；
  · `MpvPlayer.chapter_step()` 发什么命令、连按不丢步、到边界不发命令；
  · `set_chapter()` 越界/无章节不发命令；
  · 菜单：章节子菜单的条目、当前章勾选、没有章节时一条不可点的说明；
  · 主窗口 `_chapter_step` / `_on_menu_action("chapter")` 的接线与 OSD；
  · `secondary_sub_id()`：内核回字符串 "no" 时不许当成"开着"（旧 bug）；
  · 快捷键表与 dispatch 的双向对账（新动作必须两处都在）。

证伪方式：逐条把承重件换成已知坏实现，同一批判据必须翻红；函数对象必须真的换了
（`is not`），否则记 NOP——"打桩没打上"不能被当成"抓到了"。
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GELU_DATA", os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".selftest-data-chapter")))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt5.QtWidgets import QAction, QApplication, QMenu  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine import player as player_mod  # noqa: E402
from app.engine.player import MpvPlayer, chapter_target  # noqa: E402
from app.ui import keymap, menus  # noqa: E402
from app.ui import main_window as mw  # noqa: E402

CHECKED: list[tuple[str, bool, str]] = []
FALSIFIED: list[tuple[str, bool, str]] = []
APP = None

ROWS = [{"title": "片头", "time": 0.0},
        {"title": "正片 一", "time": 3.0},
        {"title": "正片 二", "time": 6.0}]


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKED.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


class OfflinePlayer(MpvPlayer):
    """离线替身：不启动内核，但 `running` 要照真机"已连上"那一档报。

    菜单的"还没开播"与"这支片子没有章节"就靠这个属性分开；没有这条，
    章节表非空的判据会被当成没内核，测的就不是原来那件事了。
    """
    running = True


def make_player(rows=ROWS, cur=0) -> MpvPlayer:
    """不启动内核的 MpvPlayer：属性直接放进缓存，命令只记账不发送。"""
    p = OfflinePlayer(cfgmod.Config())
    p._props["chapter-list"] = [dict(r) for r in rows]
    p._props["chapter"] = cur
    sent: list[tuple[str, object]] = []
    p.sent = sent
    p.set_prop = lambda name, value: sent.append((name, value))
    return p


def judge_target(fn) -> tuple[bool, str]:
    """纯函数判据：返回 (是否全部通过, 第一个不符的点)。"""
    cases = [
        ("无章节", (0, 0, 1), (None, "")),
        ("无章节倒着按", (0, 0, -1), (None, "")),
        ("无当前章按下一章", (3, -1, 1), (0, "")),
        ("无当前章按上一章", (3, -1, -1), (2, "")),
        ("首章按上一章", (3, 0, -1), (0, "first")),
        ("末章按下一章", (3, 2, 1), (2, "last")),
        ("中间前进", (3, 0, 1), (1, "")),
        ("中间后退", (3, 2, -1), (1, "")),
        ("只有一章", (1, 0, 1), (0, "last")),
        ("大步越界也夹住", (3, 1, 9), (2, "last")),
    ]
    for label, args, want in cases:
        try:
            got = fn(*args)
        except Exception as exc:      # noqa: BLE001 - 判据自己不许崩
            return False, f"{label}: 抛了 {type(exc).__name__}: {exc}"
        if got != want:
            return False, f"{label}: 得 {got!r}，要 {want!r}"
    return True, ""


def judge_step(p: MpvPlayer) -> tuple[bool, str]:
    """chapter_step 的行为判据：目标/命令/边界/标题/连按。"""
    r = p.chapter_step(1)
    if r["index"] != 1 or r["moved"] is not True or p.sent != [("chapter", 1)]:
        return False, f"前进一章：index={r['index']} moved={r['moved']} sent={p.sent}"
    if r["title"] != "正片 一" or abs(r["time"] - 3.0) > 1e-6:
        return False, f"标题/时间没取目标行：{r}"
    # 缓存还没翻面（模拟内核尚未推送）：第二下必须从期望值续走，不能原地踏步
    r2 = p.chapter_step(1)
    if r2["index"] != 2 or p.sent != [("chapter", 1), ("chapter", 2)]:
        return False, f"连按第二下丢步：index={r2['index']} sent={p.sent}"
    # 内核推来真值后，期望值必须让位给内核
    p._apply("chapter", 2)
    if p._chapter_expect is not None:
        return False, "内核已推来 chapter=2，期望值却没销账"
    r3 = p.chapter_step(1)
    if r3["edge"] != "last" or r3["moved"] is not False or len(p.sent) != 2:
        return False, f"末章还发命令：edge={r3['edge']} moved={r3['moved']} sent={p.sent}"
    return True, ""


def judge_edges(p: MpvPlayer) -> tuple[bool, str]:
    p._props["chapter"] = 0
    p.sent.clear()
    r = p.chapter_step(-1)
    if r["edge"] != "first" or r["moved"] is not False or p.sent:
        return False, f"首章按上一章：{r} sent={p.sent}"
    p._props["chapter"] = -1
    r = p.chapter_step(-1)
    if r["index"] != 2 or not p.sent:
        return False, f"无当前章按上一章：{r} sent={p.sent}"
    return True, ""


def judge_set_chapter(p: MpvPlayer) -> tuple[bool, str]:
    p.sent.clear()
    r = p.set_chapter(2)
    if r["index"] != 2 or p.sent != [("chapter", 2)] or r["title"] != "正片 二":
        return False, f"跳到第 3 章：{r} sent={p.sent}"
    p.sent.clear()
    r = p.set_chapter(9)
    if r["index"] is not None or p.sent:
        return False, f"越界下标发了命令：{r} sent={p.sent}"
    q = make_player(rows=[], cur=-1)
    r = q.set_chapter(0)
    if r["total"] != 0 or q.sent:
        return False, f"没有章节还发命令：{r} sent={q.sent}"
    # 菜单跳转要接过"期望值"这一棒：先按一下造出旧期望（1, 基线 0），再从菜单跳到
    # 末章——内核还没推送时紧接着按"下一章"，必须从 2 算（到边界停住），不能从
    # 旧缓存 0 算出 1 把用户往回带。只是把旧期望清掉也会在这里现形。
    c = make_player(ROWS, 0)
    c.chapter_step(1)
    c.sent.clear()
    c.set_chapter(2)
    r = c.chapter_step(1)
    if (r["index"] != 2 or r["edge"] != "last" or r["moved"] is not False
            or c.sent != [("chapter", 2)]):
        return False, f"菜单跳到末章后快速再按丢了落点：{r} sent={c.sent}"
    return True, ""


def judge_notice(fn) -> tuple[bool, str]:
    cases = [
        ({}, "这支片子没有章节"),
        ({"total": 0, "index": None}, "这支片子没有章节"),
        ({"total": 1, "index": 0, "title": "全片"}, "这支片子只有 1 章"),
        ({"total": 3, "index": 0, "edge": "first"}, "已是第一章 · 共 3 章"),
        ({"total": 3, "index": 2, "edge": "last"}, "已是最后一章 · 共 3 章"),
        ({"total": 3, "index": 0, "title": "片头", "time": 0.0}, "第 1/3 章 · 片头"),
        ({"total": 3, "index": 1, "title": "正片 一", "time": 3.0},
         "第 2/3 章 · 00:03 · 正片 一"),
        ({"total": 8, "index": 7, "title": "  ", "time": 65.0}, "第 8/8 章 · 01:05"),
    ]
    for info, want in cases:
        try:
            got = fn(info)
        except Exception as exc:      # noqa: BLE001
            return False, f"{info}: 抛了 {type(exc).__name__}: {exc}"
        if got != want:
            return False, f"{info}: 得 {got!r}，要 {want!r}"
    return True, ""


def judge_notice_edge_ignored(fn) -> bool:
    """把边界文案换成普通章节文案的坏实现，必须被判据否掉。"""
    ok, _ = judge_notice(fn)
    return not ok


def judge_secondary(fn) -> tuple[bool, str]:
    p = MpvPlayer(cfgmod.Config())
    for raw, want in ((None, None), ("no", None), ("NO", None), ("", None), (False, None),
                      (3, 3), ("4", "4")):
        p._props["secondary-sid"] = raw
        got = fn(p)
        if got != want:
            return False, f"secondary-sid={raw!r} 得 {got!r}，要 {want!r}"
    return True, ""


def find_chapter_actions(menu) -> list[QAction]:
    return [a for a in menu.actions() if isinstance(a.data(), tuple) and a.data()[0] == "chapter"]


def judge_menu(player) -> tuple[bool, str]:
    m = menus.chapter_menu(player, None)
    acts = m.actions()
    labels = [a.text() for a in acts if not a.isSeparator()]
    if "上一章" not in labels or "下一章" not in labels:
        return False, f"缺上一章/下一章：{labels}"
    nav = [a for a in acts if a.text() in ("上一章", "下一章")]
    if [a.data() for a in nav] != [("do", "prev_chapter"), ("do", "next_chapter")]:
        return False, f"导航条目 data 不对：{[a.data() for a in nav]}"
    ch = find_chapter_actions(m)
    if len(ch) != 3:
        return False, f"章节条目数 {len(ch)} ≠ 3"
    checked = [i for i, a in enumerate(ch) if a.isChecked()]
    if checked != [1]:
        return False, f"当前章（下标 1）没勾对：checked={checked}"
    if not any("00:03" in a.text() and "正片 一" in a.text() for a in ch):
        return False, f"章节条目没带时间/标题：{[a.text() for a in ch]}"
    empty = menus.chapter_menu(make_player(rows=[], cur=-1), None)
    ea = [a for a in empty.actions() if not a.isSeparator()]
    if len(ea) != 1 or ea[0].isEnabled() or "没有章节" not in ea[0].text():
        return False, f"空章节菜单形状不对：{[a.text() for a in ea]}"
    more = menus.more_menu({}, None, submenus=[("章节", menus.chapter_menu(player, None))])
    subs = [a.menu() for a in more.actions() if a.menu() is not None]
    if len(subs) != 1 or subs[0].objectName() != "ChapterMenu":
        return False, f"更多菜单里没挂章节子菜单：{[s.text() for s in subs]}"
    return True, ""


def judge_menu_cold(player) -> tuple[bool, str]:
    """没内核时菜单要说"还没启动"——哪怕缓存里残留着上一支片子的章节表。"""
    m = menus.chapter_menu(player, None)
    acts = [a for a in m.actions() if not a.isSeparator()]
    if len(acts) != 1 or acts[0].isEnabled() or "还没启动" not in acts[0].text():
        return False, f"无内核菜单形状不对：{[a.text() for a in acts]}"
    return True, ""


def judge_dispatch_source(src: str) -> list[str]:
    """dispatch 源码里缺分支的动作名；静态判据，也给证伪用。"""
    names = set(re.findall(r'action == "([a-z0-9_]+)"', src))
    return [a for a in ("prev_chapter", "next_chapter") if a not in names]


def judge_wiring() -> tuple[bool, str]:
    win = mw.MainWindow.__new__(mw.MainWindow)
    win.player = make_player(ROWS, 0)
    said: list[str] = []
    win._osd = lambda s: said.append(s)
    mw.MainWindow._chapter_step(win, 1)
    if not said or "第 2/3 章" not in said[-1] or "正片 一" not in said[-1]:
        return False, f"按键那条路 OSD 不对：{said}"
    act = QAction()
    act.setData(("chapter", 2))
    mw.MainWindow._on_menu_action(win, act)
    if "第 3/3 章" not in said[-1] or win.player.sent[-1] != ("chapter", 2):
        return False, f"菜单那条路没跳到第 3 章：said={said} sent={win.player.sent}"
    return True, ""


def assert_mutated(base, mutated, label: str) -> None:
    if base is mutated:
        FALSIFIED.append((f"{label}（打桩没生效＝NOP）", False, "函数对象没变"))
    else:
        FALSIFIED.append((label, True, ""))


def falsify() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])   # 菜单构造前必须先有 QApplication
    orig_target = player_mod.chapter_target
    orig_step = MpvPlayer.chapter_step
    orig_notice = mw.chapter_notice
    orig_menu = menus.chapter_menu
    orig_secondary = MpvPlayer.secondary_sub_id
    good_target, good_step, good_notice, good_menu, good_secondary = (
        orig_target, orig_step, orig_notice, orig_menu, orig_secondary)

    # M1：边界不夹住
    def bad_clamp(count, current, delta):
        return current + delta, ""

    ok, _ = judge_target(bad_clamp)
    FALSIFIED.append(("M1 边界不夹住必须被 chapter_target 判据抓到", not ok, ""))
    assert_mutated(orig_target, bad_clamp, "M1b 打桩检查")

    # M2：没有章节时也回目标 0
    def bad_empty(count, current, delta):
        if count <= 0:
            return 0, ""
        return orig_target(count, current, delta)

    ok, _ = judge_target(bad_empty)
    FALSIFIED.append(("M2 没有章节却给出下标必须被抓住", not ok, ""))

    # M3：连按不续期望值（每一步都读同一份旧缓存）
    def bad_pos(self):
        try:
            return int(self.prop("chapter", -1) or -1)
        except (TypeError, ValueError):
            return -1

    p = make_player(ROWS, 0)
    orig_pos = MpvPlayer._chapter_pos
    MpvPlayer._chapter_pos = bad_pos
    try:
        ok, _ = judge_step(p)
    finally:
        MpvPlayer._chapter_pos = orig_pos
    FALSIFIED.append(("M3 连按第二下必须从期望值续走（否则原地踏步）", not ok, ""))
    assert_mutated(orig_pos, bad_pos, "M3b 打桩检查")

    # M4：到边界还发命令
    def bad_step(self, delta):
        rows = self.chapters()
        cur = self._chapter_pos()
        target, edge = orig_target(len(rows), cur, int(delta))
        out = {"index": target, "total": len(rows), "title": "", "time": 0.0,
               "edge": edge, "moved": False}
        if target is not None:
            self.set_prop("chapter", target)     # 坏：target == cur 也发
            out["moved"] = True
            row = rows[target]
            out["title"] = str(row.get("title") or "")
            out["time"] = float(row.get("time") or 0.0)
        return out

    p = make_player(ROWS, 1)
    MpvPlayer.chapter_step = bad_step
    try:
        ok, _ = judge_edges(p)
    finally:
        MpvPlayer.chapter_step = orig_step
    FALSIFIED.append(("M4 首/末章仍发命令必须被抓到", not ok, ""))
    assert_mutated(orig_step, bad_step, "M4b 打桩检查")

    # M5：每条跳转都发命令（越界也发）
    def bad_set(self, idx):
        out = {"index": None, "total": len(self.chapters()), "title": "", "time": 0.0,
               "edge": "", "moved": False}
        self.set_prop("chapter", int(idx))
        return out

    p = make_player(ROWS, 0)
    orig_set = MpvPlayer.set_chapter
    MpvPlayer.set_chapter = bad_set
    try:
        ok, _ = judge_set_chapter(p)
    finally:
        MpvPlayer.set_chapter = orig_set
    FALSIFIED.append(("M5 越界章节下标仍发命令必须被抓到", not ok, ""))
    assert_mutated(orig_set, bad_set, "M5b 打桩检查")

    # M6：OSD 不看边界
    def bad_notice(info):
        return f"第 {int(info.get('index') or 0) + 1}/{info.get('total') or 1} 章"

    ok, _ = judge_notice(bad_notice)
    FALSIFIED.append(("M6 边界文案被吞必须被 chapter_notice 判据抓到", not ok, ""))
    assert_mutated(orig_notice, bad_notice, "M6b 打桩检查")

    # M7：菜单不标当前章
    def bad_menu(player, parent):
        m = orig_menu(player, parent)
        for a in m.actions():
            if isinstance(a.data(), tuple) and a.data()[0] == "chapter":
                a.setChecked(False)
        return m

    p = make_player(ROWS, 1)
    menus.chapter_menu = bad_menu
    try:
        ok, _ = judge_menu(p)
    finally:
        menus.chapter_menu = orig_menu
    FALSIFIED.append(("M7 菜单不勾当前章必须被抓到", not ok, ""))
    assert_mutated(orig_menu, bad_menu, "M7b 打桩检查")

    # M8：副字幕关闭态当成开着（旧写法 bool("no")）
    def bad_secondary(self):
        v = self.prop("secondary-sid")
        return v if bool(v) else None

    ok, _ = judge_secondary(bad_secondary)
    FALSIFIED.append(("M8 把 'no' 当成已开副字幕必须被抓到", not ok, ""))
    assert_mutated(orig_secondary, bad_secondary, "M8b 打桩检查")

    # M9：dispatch 分支检测器本身要能被一段缺分支的源码骗……不，必须报缺
    bad_src = 'def dispatch(self, action):\n    if action == "next":\n        pass\n'
    missing = judge_dispatch_source(bad_src)
    FALSIFIED.append(("M9 缺分支的 dispatch 源码必须被对账器点名",
                      missing == ["prev_chapter", "next_chapter"], f"得到 {missing}"))
    good_src = 'def dispatch(self, action):\n    if action == "prev_chapter": pass\n' \
               '    elif action == "next_chapter": pass\n'
    FALSIFIED.append(("M9b 正对照：两条都在时不许误报",
                      judge_dispatch_source(good_src) == [], ""))

    # M10：菜单跳转只把旧期望清掉、不接过新落点（旧缓存会把快速再按带回上一章）
    def bad_set_stale(self, idx):
        out = orig_set(self, idx)
        self._chapter_expect = None
        return out

    p = make_player(ROWS, 0)
    MpvPlayer.set_chapter = bad_set_stale
    try:
        ok, _ = judge_set_chapter(p)
    finally:
        MpvPlayer.set_chapter = orig_set
    FALSIFIED.append(("M10 菜单跳转后不留新期望（快速再按按旧缓存回退）必须被抓到", not ok, ""))
    assert_mutated(orig_set, bad_set_stale, "M10b 打桩检查")

    # M11：没内核时把"还没启动"说成"这支片子没有章节"
    def bad_cold(player, parent):
        if not player.running:
            m = QMenu("章节", parent)
            a = QAction("这支片子没有章节", m)
            a.setEnabled(False)
            m.addAction(a)
            return m
        return orig_menu(player, parent)

    cold = MpvPlayer(cfgmod.Config())
    cold._props["chapter-list"] = [dict(r) for r in ROWS]
    menus.chapter_menu = bad_cold
    try:
        ok, _ = judge_menu_cold(cold)
    finally:
        menus.chapter_menu = orig_menu
    FALSIFIED.append(("M11 无内核却说成『没有章节』必须被抓到", not ok, ""))
    assert_mutated(orig_menu, bad_cold, "M11b 打桩检查")

    if not FALSIFIED:
        return 1
    missed = [n for n, ok, _ in FALSIFIED if not ok]
    print(f"\n---- 证伪收口：抓到 {len(FALSIFIED) - len(missed)}/{len(FALSIFIED)}"
          f"（漏网 {len(missed)}） ----")
    for n, ok, d in FALSIFIED:
        print(f"[{'CAUGHT' if ok else 'MISSED'}] {n}" + (f"  —— {d}" if d else ""))
    return 0 if not missed else 1


def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    ok, why = judge_target(chapter_target)
    check("chapter_target：首/末/无章节/无当前章/大步越界全部按约定落点", ok, why)

    p = make_player(ROWS, 0)
    ok, why = judge_step(p)
    check("chapter_step：发绝对下标、标题取目标行、连按不丢步、到边界不发命令", ok, why)

    p = make_player(ROWS, 0)
    ok, why = judge_edges(p)
    check("chapter_step：首章按上一章 / 无当前章按上一章（落最后一章）", ok, why)

    p = make_player(ROWS, 0)
    ok, why = judge_set_chapter(p)
    check("set_chapter：有效下标发命令；越界/无章节一条都不发", ok, why)

    ok, why = judge_notice(mw.chapter_notice)
    check("chapter_notice：没有章节/只有一章/首末章/带标题时间的话术", ok, why)

    p = make_player(ROWS, 1)
    ok, why = judge_menu(p)
    check("章节菜单：上/下一章 + 章节表 + 当前章勾选 + 空章节说明 + 挂在更多菜单", ok, why)

    cold = MpvPlayer(cfgmod.Config())
    cold._props["chapter-list"] = [dict(r) for r in ROWS]   # 上一次会话的残留表
    ok, why = judge_menu_cold(cold)
    check("章节菜单：内核没起时说『还没启动』，不把残留章节表当成这支片子的", ok, why)

    ok, why = judge_wiring()
    check("接线：dispatch 的 _chapter_step 与菜单 chapter 分支都真跳到目标章", ok, why)

    ok, why = judge_secondary(MpvPlayer.secondary_sub_id)
    check("secondary_sub_id：'no'/空/None/False 都是关闭，数字 id 原样回", ok, why)

    rows = keymap.effective({})
    acts = {a for _s, a, _d in rows}
    pair = [s for s, a, _d in rows if a in ("prev_chapter", "next_chapter")]
    check("快捷键表：Ctrl+PgUp / Ctrl+PgDown 都有效、不撞键、动作名可绑",
          len(pair) == 2 and all(keymap.valid(s) for s in pair)
          and not [c for c in keymap.conflicts(rows) if any("chapter" in x for x in c[1])]
          and {"prev_chapter", "next_chapter"} <= acts,
          f"绑定={pair} 冲突={keymap.conflicts(rows)}")
    check("KERNEL_ONLY 收下两个章节动作（没内核时回一句话而不是静默）",
          {"prev_chapter", "next_chapter"} <= set(mw.KERNEL_ONLY), "")
    missing = judge_dispatch_source(inspect.getsource(mw.MainWindow.dispatch))
    check("dispatch 源码里真有两条章节分支（快捷键与菜单同一段派发）", not missing, f"缺={missing}")
    src = inspect.getsource(MpvPlayer.chapter_step) + inspect.getsource(MpvPlayer._chapter_pos)
    check("章节路径只读缓存、不发同步请求（UI 线程不许被内核往返拖住）",
          "self.get(" not in src and "_req(" not in src, "")

    passed = sum(1 for _n, ok, _d in CHECKED if ok)
    fails = [n for n, ok, _d in CHECKED if not ok]
    print(f"\n[FINAL] 章节：{passed}/{len(CHECKED)} 通过；红 {len(fails)} 条"
          + (f"：{fails}" if fails else ""))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(falsify() if "--falsify" in sys.argv else main())
