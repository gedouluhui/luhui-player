"""快捷键表自检：不建窗口、不抓屏，纯逻辑＋Qt 键序列解析的真值。

用法：
    python tests/test_keymap.py            # 正常自检：全部 PASS 才算过
    python tests/test_keymap.py --falsify  # 证伪：把某个检查函数换成"常见错误写法"，
                                           # 对应那条自检**必须**变红；不变红说明那条判据
                                           # 是恒真的，等于没测

为什么这份值得单独存在：`QKeySequence(串)` 对写错的键名**不报错**，只解析成
Qt::Key_UNKNOWN(0x01ffffff)，于是那条快捷键装上后永远不响、也没有任何提示。第一轮到现在
的默认表里就有 5 条这种死键（PgDn / Backslash / Minus / Equals / Enter），另有 1 条是
"键值合法但物理上按不出来"（裸写的「<」：Qt 解析成不带 Shift 的 0x3c，而真实键盘给出的是
0x2c+Shift）。键值实测见 tests/probe_keyseq*.py——那几个探针是本文件数值的地基，
判据与实测对不上时先重跑探针。

两条老规矩：
  · 判据不许恒真 —— 每条都由 `--falsify` 里对应的桩函数证伪一遍；
  · 判据不许建立在 `toString()` 上 —— 它对「PgDn」回空串、对「Page Down」回「PgDown」，
    拿显示串当合法性判据会把默认表自己判红（F6 专门测这一点）。
"""
from __future__ import annotations

import inspect
import json
import os
import shutil
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QKeySequence  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.ui import keymap as KM  # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)    # QKeySequence 要有 app 才可靠
results: list[tuple[str, bool, str]] = []

# 探针 1/2/3 实测出来的三组已知答案：
#   DEAD      —— 解析成 Key_UNKNOWN，装上永不响，必须全部判死
#   ALIVE     —— 解析成活键（其中「<」虽然活着但物理按不出，见 UNREACHABLE）
#   UNREACHABLE—— 键值合法、真实键盘上却按不出来的那一类
DEAD = ["PgDn", "Backslash", "Minus", "Equals", "NotAKey", "   ", "",
        "Ctrl+", ";;", "中文", "A B", "Shift", "Ctrl+Shift", "MediaPlay",
        "AudioMute", "Volume_Mute", "Numpad Enter"]
ALIVE = ["Page Down", "PgDown", "PgUp", "-", "=", "\\", "Return", "Enter", "Esc",
         "Escape", "Space", ",", ".", "[", "]", "Shift+,", "Shift+-", "Ctrl+-",
         "F1", "F13", "Media Stop", "Volume Mute", "Ctrl+Alt+Shift+Meta+X", "K", "Shift+K",
         "  Space  "]        # 带空白的写法：normalize 会 strip 掉，所以它算活键而不是死串
UNREACHABLE = ["<", ">", "_", ":", "?", "{", "}", "|", "!", "@", "~", "^"]

# 上一版默认表里那 6 条按不响的写法 → 本轮换成实测能按的写法，并记着"当初为什么不行"
OLD_WAYS = {"PgDn": ("PgDown", "死串"), "Backslash": ("\\", "死串"), "Minus": ("-", "死串"),
            "Equals": ("=", "死串"), "<": ("Shift+,", "按不出"),
            "Enter": ("Return", "只绑了小键盘 Enter")}


def pairs(rows) -> set:
    return {(s, a) for s, a, _d in rows}


# ============================================================ 判据（一条函数一项）
# 每个函数回 (是否通过, 详情)。正常自检拿去打 PASS/FAIL，证伪拿去喂坏桩——两边共用同一份
# 实现，才不会出现"证伪测的是另一个东西"。
def p_default_alive() -> tuple[bool, str]:
    bad = [(s, a) for s, a, _d in KM.DEFAULT if KM.dead(s)]
    return not bad, f"死行={bad}（共 {len(KM.DEFAULT)} 行）"


def p_default_reachable() -> tuple[bool, str]:
    bad = [(s, a, KM.needs_shift(s)) for s, a, _d in KM.DEFAULT if KM.needs_shift(s)]
    return not bad, f"物理按不出的行={bad}"


def p_default_canonical() -> tuple[bool, str]:
    bad = [(s, KM.canonical(s)) for s, _a, _d in KM.DEFAULT if KM.canonical(s) != s]
    # 别名写法与显示写法不一致时退回原串，这样"按不响的键名"永远不会因为显示而成形
    fb = [(s, KM.canonical(s)) for s in ("PgDn", "NotAKey") if KM.canonical(s) != s]
    # 生效表里的串必须已经是 strip 过的：装快捷键用的是这个串本身，带空白就解析成死键
    raw = [(s, repr(s)) for s, _a, _d in KM.effective({"on": [["  F6  ", "mute"]]})
           if s != s.strip()]
    dirty = [(s, repr(s)) for s, _a, _d in KM.effective(
        {"off": ["  K  "], "on": [["Ctrl+J ", "mute"]]}) if s != s.strip()]
    return not bad and not fb and not raw and not dirty, \
        f"显示不一样的默认行={bad} 回退失败={fb} 带空白的生效串={raw + dirty}"


def p_default_single_key() -> tuple[bool, str]:
    dup = sorted({s for s, _a, _d in KM.DEFAULT
                  if sum(1 for t, _b, _c in KM.DEFAULT if KM.same_key(s, t)) > 1})
    conf = KM.conflicts(KM.effective(None))
    return not dup and not conf, f"重复的键={dup} 冲突={conf}"


def p_known_answers() -> tuple[bool, str]:
    miss_dead = [s for s in DEAD if not KM.dead(s)]
    miss_alive = [s for s in ALIVE if KM.dead(s)]
    miss_un = [s for s in UNREACHABLE if not KM.needs_shift(s)]
    wrong_un = [s for s in ("+", "*", "Shift+,", "Shift+-", "Ctrl+Shift+Z") if KM.needs_shift(s)]
    return not miss_dead and not miss_alive and not miss_un and not wrong_un, (
        f"该判死却判活={miss_dead} 该判活却判死={miss_alive}"
        f" 该报按不出却没报={miss_un} 误报按不出={wrong_un}"
        f"（对照 {len(DEAD)} 死 / {len(ALIVE)} 活 / {len(UNREACHABLE)} 按不出）")


def p_reason_text() -> tuple[bool, str]:
    no_reason = [s for s in DEAD + UNREACHABLE if not KM.reason(s)]
    usable = [s for s in ALIVE if s not in UNREACHABLE]
    false_alarm = [s for s in usable if KM.reason(s)]
    return not no_reason and not false_alarm, \
        f"没理由的坏串={no_reason} 冤枉了好串={false_alarm}"


def p_same_key_mods() -> tuple[bool, str]:
    ok = (not KM.same_key("K", "Shift+K") and KM.same_key("PgDown", "Page Down")
          and not KM.same_key("", "K") and not KM.same_key("K", "")
          and KM.parsed("K") != KM.parsed("Shift+K"))
    return ok, (f"K={KM.parsed('K'):#010x} Shift+K={KM.parsed('Shift+K'):#010x} "
                f"同键={KM.same_key('K', 'Shift+K')}")


def p_shift_pairs() -> tuple[bool, str]:
    """点名之外还要给对出路：提示里必须写出"该按哪把物理键"。"""
    want = {"<": "Shift+,", ">": "Shift+.", "_": "Shift+-", ":": "Shift+;",
            "?": "Shift+/", "!": "Shift+1", "@": "Shift+2", "{": "Shift+["}
    wrong = []
    for s, fix in want.items():
        note = KM.needs_shift(s)
        if fix not in note:
            wrong.append((s, fix, note))
    return not wrong, f"提示里没给出路（或给错）的={wrong}"


def p_diff_roundtrip() -> tuple[bool, str]:
    """四种编辑都要 diff → 生效表 往返等价，且**折算出来的 diff 展开以后不许留同键值残行**。

    后半句是给 `diff_of` 的"成对折算"上闸门。只按"键在不在"算 off 的那种写法（改绑 K
    时 off 是空的，旧那行全凭装表时的 `dedupe()` 兜住）在今天这台机器上**看不出错**——
    生效表照样对，屏幕上照样响。所以只看往返等价的话这条判据是摆设：必须另查"展开后
    还有没有撞键"，否则存储语义烂掉了也没人报警，而对话框拿的正是展开后那张表
    （它会显示两条同一把键，并把其中一条涂红）。
    """
    base = KM.effective(None)
    cases = {
        "原样": base,
        "删一行": [r for r in base if r[0] != "E"],
        "改绑（K 从播/停换到下一个）":
            [("K", "next", KM.desc_of("next")) if s == "K" else (s, a, d) for s, a, d in base],
        "加一条新键": list(base) + [("Ctrl+J", "mute", "静音切换")],
        "删光某个动作": [r for r in base if r[1] != "mute"],
    }
    bad = []
    for name, rows in cases.items():
        d = KM.diff_of(rows)
        got = pairs(KM.effective(d))
        if got != pairs(rows):
            bad.append((name, "往返不等", sorted(got ^ pairs(rows))[:4]))
        leftover = KM.shadowed(KM.joined(d))
        if leftover:
            bad.append((name, "展开后仍有同键值残行", [(l[0:2], w[0:2]) for l, w in leftover]))
    return not bad, f"不等的={bad}"


def p_diff_minimal() -> tuple[bool, str]:
    d = KM.diff_of(KM.effective(None))
    return d == {"off": [], "on": []}, f"{d}"


def p_incremental_upgrade() -> tuple[bool, str]:
    """整张表存进配置的做法在这一条上必挂——这就是用 diff 存的全部理由。"""
    saved = KM.DEFAULT
    try:
        KM.DEFAULT = saved + (("N", "next", "下一个（新版本新增）"),)
        rows = KM.effective({"off": ["K"], "on": [["Ctrl+J", "mute"]]})
        seqs = {s for s, _a, _d in rows}
        return ("N" in seqs and "K" not in seqs and ("Ctrl+J", "mute") in pairs(rows)), \
            f"新默认行在={'N' in seqs} 用户的删仍生效={'K' not in seqs} 用户的加仍生效={('Ctrl+J','mute') in pairs(rows)}"
    finally:
        KM.DEFAULT = saved


def p_normalize_dirty() -> tuple[bool, str]:
    d = KM.normalize({"off": ["K", "K", 123, "", None],
                      "on": [["Ctrl+J", "mute"], ["PgDn", "next"], ["X", "nope"], ["坏"],
                             "整条是字符串", {"seq": "Ctrl+B", "action": "loop"},
                             ["Ctrl+J", "mute"], ["Shift+,", "mute"]]})
    ok = d["off"] == ["K"] and [x for x in d["on"]] == [["Ctrl+J", "mute"],
                                                        ["Ctrl+B", "loop"], ["Shift+,", "mute"]]
    junk = [None, {}, [], "字符串", 0, {"off": "不是列表", "on": None}, {"on": [[None, None]]}]
    junk_bad = [x for x in junk if KM.normalize(x) != {"off": [], "on": []}]
    return ok and not junk_bad, f"收口结果={d} 没兜住的脏顶层={junk_bad}"


CAPTURE = [(ord("j"), int(Qt.ShiftModifier), "Shift+J"),
           (ord("J"), int(Qt.NoModifier), "J"),
           (ord("k"), int(Qt.NoModifier), "K"),
           (Qt.Key_Left, int(Qt.ControlModifier), "Ctrl+Left"),
           (Qt.Key_Shift, int(Qt.ShiftModifier), ""),
           (Qt.Key_Control, int(Qt.ControlModifier), ""),
           (Qt.Key_Alt, int(Qt.AltModifier), ""),
           (Qt.Key_Meta, int(Qt.MetaModifier), ""),
           (Qt.Key_AltGr, int(Qt.AltModifier), ""),
           (Qt.Key_CapsLock, int(Qt.NoModifier), ""),
           (Qt.Key_NumLock, int(Qt.NoModifier), ""),
           (Qt.Key_PageDown, int(Qt.NoModifier), "PgDown"),
           (Qt.Key_Comma, int(Qt.ShiftModifier), "Shift+,"),
           (Qt.Key_Enter, int(Qt.KeypadModifier), "Enter"),
           (Qt.Key_Space, int(Qt.NoModifier), "Space"),
           (Qt.Key_Escape, int(Qt.NoModifier), "Esc")]


def p_capture() -> tuple[bool, str]:
    bad = []
    for key, mods, want in CAPTURE:
        got = KM.from_event(key, mods)
        if got != want:
            bad.append((hex(key), hex(mods), got, want))
        elif got and (KM.dead(got) or KM.needs_shift(got) or KM.canonical(got) != got):
            bad.append((hex(key), "捕获结果不是可用的规范串", got, want))
    return not bad, f"{bad}"


def p_conflicts() -> tuple[bool, str]:
    rows = list(KM.effective(None)) + [("Space", "next", "下一个"), ("PgDn", "next", "下一个")]
    cf = KM.conflicts(rows)
    ok = any(t == "Space" and "next" in acts for t, acts in cf)
    # 同键同动作不算冲突：默认表本来就有多把键做同一件事
    same = KM.conflicts(list(KM.effective(None)) + [("Space", "toggle_play", "播/停")])
    return ok and not same, f"冲突={cf} 同键同动作也被判冲突={same}"


def p_problems() -> tuple[bool, str]:
    rows = list(KM.effective(None)) + [("PgDn", "next", "x"), ("<", "mute", "x"),
                                       ("Ctrl+U", "no_such_action", "x"),
                                       ("Space", "toggle_play", "y")]
    pb = KM.problems(rows)
    return len(pb) == 4, f"{len(pb)} 条：{pb}"


def p_bad_pairs() -> tuple[bool, str]:
    """对话框靠这个集合涂红，所以它必须"冲突的两边都圈"而不是只圈一边。

    只圈一边是这类函数最典型的写法（遍历到第二条才发现第一条、或者只认精确串），
    后果是用户在表里看到一条红的、一条白的，删掉红的那条以后仍然只有一条会响。
    """
    base = KM.effective(None)
    clean = KM.bad_pairs(base)
    conf = KM.bad_pairs(base + [("Space", "next", "坏样本")])
    want_conf = {("Space", "toggle_play"), ("Space", "next")}
    alias = KM.bad_pairs(base + [("Page Up", "vol_up", "坏样本")])   # 别名写法撞了 PgUp
    want_alias = {("PgUp", "prev"), ("Page Up", "vol_up")}
    others = {
        "死串": KM.bad_pairs(base + [("PgDn", "next", "坏样本")]),
        "按不出": KM.bad_pairs(base + [("<", "mute", "坏样本")]),
        "表外动作": KM.bad_pairs(base + [("Ctrl+U", "no_such", "坏样本")]),
        "重复行": KM.bad_pairs(base + [("Space", "toggle_play", "重复")]),
    }
    missed = {k: v for k, v in others.items() if not v}
    dup = ("Space", "toggle_play") in others["重复行"]
    return (not clean and want_conf <= conf and want_alias <= alias and not missed and dup), (
        f"干净表被判红的={sorted(clean)} 一键双绑圈到={sorted(conf)} 别名撞键圈到={sorted(alias)}"
        f" 四类毛病漏判={missed} 重复行没圈={not dup}")


def p_shadow_dedupe() -> tuple[bool, str]:
    """同一把键的两行（写法不同也算）必须 ①被点名 ②装表时折叠成一条。

    依据是探针 4 的实测（tests/probe_keypress_abort.py 步骤 E/F）：同一窗口同 context
    下两条解析成同一键值的 QShortcut，按一次那把键**两条都不响**。所以"用户加一条
    Page Down → 静音"在没有去重的世界里坏的不是那一行，而是连带把默认的
    PgDown → 下一个 一起弄哑——屏幕上看不出来、也不报错，正是最贵的那种坏。
    """
    base = KM.effective(None)
    # ① 出厂表自证零撞键：去重对它是**恒等**的。这一条不成立的话后面每一条"61 行逐条
    #    真按"都会静默少一行，而那看起来像"键位没问题"。
    shipped_ok = len(base) == len(KM.DEFAULT) and not KM.shadowed(base)
    # ② 别名撞键：joined 里两条都在，effective 折叠成一条且留下的是用户那条
    raw = {"off": [], "on": [["Page Down", "mute"]]}
    j, ded = KM.joined(raw), KM.effective(raw)
    folded = len(j) == len(KM.DEFAULT) + 1 and len(ded) == len(KM.DEFAULT)
    keeps_user = ("Page Down", "mute") in pairs(ded) and ("PgDown", "next") not in pairs(ded)
    sh = KM.shadowed(j)
    named = any("同一把键" in p for p in KM.problems(j))
    # 标红这一半用**同键值同动作**的别名对：conflicts() 碰不到它（动作没撞），
    # 只有撞键规则会标——不然这个断言其实是在重复证明冲突规则。
    same_act = KM.joined({"off": [], "on": [["Page Down", "next"]]})
    red_loser = ("PgDown", "next") in KM.bad_pairs(same_act)
    # ③ 三行撞同一把键：只留最后一条，前两行都进点名台账
    triple = [("PgDown", "next", "x"), ("Page Down", "vol_up", "y"), ("PgDown", "mute", "z")]
    t_ded, t_shadow = KM.dedupe(triple), KM.shadowed(triple)
    tri_ok = [r[0:2] for r in t_ded] == [("PgDown", "mute")] and len(t_shadow) == 2
    # ④ 死写法不偷别人的键（探针 F：单装 PgDn 谁都不响，但它不抢 PageDown 的匹配）：
    #    这类行原样留在表里交给 problems() 点名，不能被去重悄悄吞掉——吞掉以后对话框
    #    里看不见那一行，用户连"哪条白装了"都无从下手。
    with_dead = KM.dedupe([("PgDown", "next", "x"), ("PgDn", "mute", "y"),
                           ("PageDown", "vol_up", "z")])
    dead_kept = len(with_dead) == 3 and len(KM.shadowed(with_dead)) == 0
    bad = [n for n, ok in (("出厂表零撞键", shipped_ok), ("别名撞键折叠成一条", folded),
                           ("留下的是用户那条", keeps_user), ("台账点名被顶掉的行", bool(sh)),
                           ("体检里有这句话", named), ("标红圈到被顶掉那行", red_loser),
                           ("三行撞键只留最后一条", tri_ok), ("死串不参与去重", dead_kept)) if not ok]
    return not bad, (f"没做到的={bad} 折叠={len(j)}→{len(ded)} 台账={sh} "
                     f"三行折叠={[r[0:2] for r in t_ded]}")


MIRROR_IN = [("Space", "toggle_play", "播放/暂停"), ("B", "burst_shot", "连拍"),
             ("Page Down", "next", "下一个"), ("F11", "fullscreen", "全屏"),
             ("Shift+K", "speed_up", "加速"), (",", "step_back", "上一帧"),
             ("Ctrl+Return", "menu_sub", "带修饰的 Return 是另一把键")]
MIRROR_OUT = [("Esc", "close_panel", "关闭面板"), ("Escape", "close_alias", "关闭的别名写法"),
              ("Return", "confirm", "确认"), ("Enter", "confirm2", "小键盘确认"),
              ("", "noop", "空串装不上"), ("   ", "noop2", "只有空白")]
MIRROR_JUNK = [("A",), ["B"], "Space", 7, None]


def p_mirrorable() -> tuple[bool, str]:
    """工具窗镜像：留给面板自己的那三把键**按键值**摘掉，其余原样原序；脏行不进镜像。

    这条判据的存在理由是真机实测（tests/probe_gif3.py B1–B4）：工坊一成为活动窗，
    主窗那 60 多条 `QShortcut(context=WindowShortcut)` 整体收不到键（按 B 命中的动作
    是 `[]`）。修法是在工坊自己那份上重挂一遍，而"挂哪些"就得有个唯一事实来源。
    四组已知答案，每组各自挡一种常见坏写法：
      · 「Escape」这条是**别名**——按字面串筛的写法会把它镜像过去，等于把"关掉面板"
        抢给播放器，用户就真的出不去那个窗（这正是不许用 `in KEEP_FOR_WINDOWS` 比串的理由）；
      · 「Ctrl+Return」必须**留下**：剔掉修饰位（MOD_BITS）的写法会把它当成 Return 摘掉，
        而实测这是两把不同的键；
      · 空串与纯空白要摘：`QKeySequence("")` 是零长序列，挂上就是一条永不响的白装键；
      · 形状不对的行（不是三元组）整条丢：`keymap` 的表可能来自用户手改过的 JSON，
        而 `install_owner_keys` 那边是 `for seq, act, _desc in ...` 解包——解包抛出坐在
        Qt 槽里，表现是进程当场没了（rc=127），不是 traceback。
    """
    rows = MIRROR_IN + MIRROR_OUT + MIRROR_JUNK
    got = KM.mirrorable(rows)
    shaped = [r for r in got if isinstance(r, (tuple, list)) and len(r) >= 3]
    got_pairs = {(str(r[0]), str(r[1])) for r in shaped}
    want = {(s, a) for s, a, _d in MIRROR_IN}
    bad_shape = [r for r in got if r not in shaped]
    missing, extra = sorted(want - got_pairs), sorted(got_pairs - want)
    # 出厂生效表这一腿：摘掉的正好是那三行，且**同一批对象、同一顺序**留回来
    table = KM.effective(None)
    mir = KM.mirrorable(table)
    dropped = [r for r in table if not any(r is m for m in mir)]
    only_three = sorted(str(r[0]) for r in dropped) == ["Enter", "Esc", "Return"]
    in_order = [r for r in table if r not in dropped] == mir
    tuples = all(isinstance(r, tuple) and len(r) == 3 for r in mir)
    safe = (KM.mirrorable([]) == [] and KM.mirrorable(None) == []
            and KM.mirrorable(iter(MIRROR_IN)) == list(MIRROR_IN))
    bad = [n for n, ok in (("该镜像的一把不少", not missing), ("该摘的没漏进来", not extra),
                           ("脏行不进镜像", not bad_shape),
                           (f"出厂表只摘三把（实际摘了 {[str(r[0]) for r in dropped]}）", only_three),
                           ("剩下的原序原对象", in_order), ("行还是三元组", tuples),
                           ("空表/None/迭代器都不抛", safe)) if not ok]
    return not bad, (f"没做到的={bad} 收到={len(got)} 该有 {len(want)}+0 脏行漏进={bad_shape} "
                     f"漏={missing} 多={extra} 出厂表 {len(table)} 行→镜像 {len(mir)} 行")


BAD_FILES = {
    "没有 kind": {"bindings": [["Space", "toggle_play"]]},
    "kind 是别的程序": {"kind": "mpv-keys", "bindings": [["Space", "toggle_play"]]},
    "bindings 缺失": {"kind": KM.KIND, "version": 1},
    "bindings 空（收了就等于把全部快捷键删光）": {"kind": KM.KIND, "version": 1, "bindings": []},
    "条目只有一项": {"kind": KM.KIND, "version": 1, "bindings": [["Space"]]},
    "动作名不存在": {"kind": KM.KIND, "version": 1, "bindings": [["Space", "toggle_super_stats"]]},
    "死串键名": {"kind": KM.KIND, "version": 1, "bindings": [["PgDn", "next"]]},
    "物理按不出": {"kind": KM.KIND, "version": 1, "bindings": [["<", "mute"]]},
    "同键绑两个动作": {"kind": KM.KIND, "version": 1,
                      "bindings": [["Space", "toggle_play"], ["Space", "next"]]},
    "顶层是列表": [{"seq": "Space"}],
}


def write_json(path: str, body) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False)


def p_export_import(tmp: str) -> tuple[bool, str]:
    rows = KM.effective({"off": ["K"], "on": [["Ctrl+J", "mute"]]})
    p = os.path.join(tmp, "sub", "keys.json")
    ok_e, d_e = KM.export(rows, p)
    back, err = KM.read_file(p)
    got = pairs([(s, a, "") for s, a in back])
    # 文件形状这一段不能假设文件真存在：导出谎报成功时这里要是崩了，traceback 会
    # 冒充"抓到了"，而抓到的其实是判据自己。缺文件就当形状不对，照样红，且红得有名。
    shape_ok = False
    if os.path.isfile(p):
        try:
            blob = json.load(open(p, encoding="utf-8"))
            shape_ok = (blob.get("kind") == KM.KIND and blob.get("version") == KM.VERSION
                        and isinstance(blob.get("bindings"), list)
                        and bool(blob.get("bindings"))
                        and all(isinstance(x, (list, tuple)) and len(x) == 2
                                for x in blob["bindings"]))
        except Exception as exc:  # noqa: BLE001
            d_e += f"；文件读不成 {type(exc).__name__}"
    imp, err_i = KM.import_diff(p)
    rt = pairs(KM.effective(imp)) == pairs(rows)
    return (ok_e and not err and got == pairs(rows) and shape_ok and not err_i and rt), \
        f"导出={d_e} 读回 err={err} 条数={len(back)} 文件形状={shape_ok} 折算 diff err={err_i}"


def p_alias_and_bad_files(tmp: str) -> tuple[bool, str]:
    p = os.path.join(tmp, "alias.json")
    write_json(p, {"kind": KM.KIND, "version": 1,
                   "bindings": [["Page Down", "next"], ["Esc", "exit_fullscreen"],
                                ["K", "toggle_play"], ["Shift+K", "toggle_play"]]})
    got, err_a = KM.read_file(p)
    alias_ok = not err_a and len(got) == 4 and bool(got) and got[0] == ("Page Down", "next")
    leaked = []
    for name, body in BAD_FILES.items():
        bp = os.path.join(tmp, "bad.json")
        write_json(bp, body)
        rows_in, msg = KM.read_file(bp)
        if not msg or rows_in:
            leaked.append(f"{name}→{msg or '(没收口)'}/{rows_in}")
    _, msg_m = KM.read_file(os.path.join(tmp, "根本不存在.json"))
    hp = os.path.join(tmp, "half.json")
    with open(hp, "w", encoding="utf-8") as fh:
        fh.write('{"kind": "luhui-keymap", "binding')
    _, msg_j = KM.read_file(hp)
    return alias_ok and not leaked and bool(msg_m) and bool(msg_j), \
        f"别名={got} err={err_a} 漏过的坏文件={leaked} 不存在={msg_m} 截断={msg_j}"


def p_export_fail(tmp: str) -> tuple[bool, str]:
    """写不进去的路径要回 (False, 原因)，不许把对话框带崩。

    挑的不是"目录不存在"那种——那种会被 makedirs 顺手建出来（真机器上实测过）；要挑
    **名字已经被一个普通文件占着**的路径，makedirs 必挂，而且挂法在各系统都一样。
    """
    blocker = os.path.join(tmp, "blocker")
    with open(blocker, "w", encoding="utf-8") as fh:
        fh.write("x")
    bad_path = os.path.join(blocker, "子", "keys.json")
    try:
        ok, msg = KM.export(KM.effective(None), bad_path)
    except Exception as exc:  # noqa: BLE001 - 把异常带出对话框同样判失败，但要留名字
        return False, f"导出直接抛 {type(exc).__name__}: {exc}（对话框会被带崩）"
    return (not ok) and "写不进去" in str(msg) and bool(msg), f"ok={ok} msg={msg}"


def p_dispatch_covered() -> tuple[bool, str]:
    from app.ui.main_window import MainWindow
    from test_ui import keymap_covered
    return keymap_covered(KM.known_actions(), inspect.getsource(MainWindow.dispatch))


def p_action_list() -> tuple[bool, str]:
    ok = (KM.known_actions() == {a for _s, a, _d in KM.DEFAULT}
          and KM.desc_of("nope") == "nope" and KM.desc_of("mute") == "静音切换")
    return ok, f"{len(KM.known_actions())} 个动作，desc_of 兜底={'对' if KM.desc_of('nope') == 'nope' else '错'}"


def p_old_ways_still_dead() -> tuple[bool, str]:
    """当初判它不行的理由现在还得成立——否则要么判据松了，要么 Qt 换了行为。"""
    back = []
    for old, (new, kind) in OLD_WAYS.items():
        if kind == "死串" and not KM.dead(old):
            back.append((old, kind, "现在解析成活键了"))
        elif kind == "按不出" and not KM.needs_shift(old):
            back.append((old, kind, "现在不再报按不出"))
        elif kind.startswith("只绑") and KM.same_key(old, new):
            back.append((old, kind, f"与 {new} 成了同一把键"))
    return not back, f"旧写法反倒判活了={back}（说明判据松了或 Qt 换了行为）"


def p_old_ways_fixed() -> tuple[bool, str]:
    table = {s for s, _a, _d in KM.DEFAULT}
    not_in = {o: n for o, (n, _k) in OLD_WAYS.items() if n not in table}
    still_bad = {n: KM.reason(n) for n, k in OLD_WAYS.values()
                 if k != "只绑了小键盘 Enter" and KM.reason(n)}
    # 「Enter」这条换的是"再加一把 Return"，两条都留在表里：小键盘 Enter 也该能全屏
    both = {n for n in ("Enter", "Return") if n in table}
    return not not_in and not still_bad and both == {"Enter", "Return"}, \
        f"没进表的={not_in} 换进来仍按不出={still_bad} 两种 Enter 都在表里={sorted(both)}"


CHECKS = [
    ("默认表里没有一条死键（Qt 解析成 Key_UNKNOWN 的写法全数挡在表外）", p_default_alive),
    ("默认表里没有一条物理按不出来的移位标点（如裸写的「<」）", p_default_reachable),
    ("默认表的写法就是 Qt 的显示写法，且生效表里的串都是去掉空白的（表/help/存储三处同一个串）",
     p_default_canonical),
    ("默认表同一把键只出现一次，也没把一把键绑给两个动作", p_default_single_key),
    (f"已知答案对照：{len(DEAD)} 条死串全判死、{len(ALIVE)} 条活串全判活、"
     f"{len(UNREACHABLE)} 条移位标点全报按不出", p_known_answers),
    ("每条坏串都给一句人话理由，且不冤枉好串", p_reason_text),
    ("same_key 认得出修饰键差别（K 与 Shift+K 两把键；PgDown 与 Page Down 一把）", p_same_key_mods),
    ("提示里写清了该按哪把物理键（Shift+, 而不是只说「不行」）", p_shift_pairs),
    ("生效表 → diff → 生效表 集合等价（删/改/加/删光某个动作四种编辑都往返），"
     "且折算出的 diff 展开后不留同键值残行", p_diff_roundtrip),
    (f"默认不动时 diff 是空的（存的是改动，不是整张 {len(KM.DEFAULT)} 行快照）", p_diff_minimal),
    ("新版本默认表加新行：用户那份 diff 不会把它吃掉", p_incremental_upgrade),
    ("手改坏的配置逐条丢弃脏条目而不是整份作废；顶层根本不是 dict 也不抛", p_normalize_dirty),
    ("按键捕获收口成规范串（字母归大写、修饰键与锁定键回空、小键盘 Enter 不带 Keypad 位）", p_capture),
    ("同键不同动作被点名，同键同动作不算冲突", p_conflicts),
    ("体检把死串、按不出、表外动作、重复行四种毛病全列出来", p_problems),
    ("标红用的 (键,动作) 集合把冲突行的每一条都圈进去（不只圈一边）", p_bad_pairs),
    ("同一把键的两行（写法不同也算）被点名并在装表时折叠成一条", p_shadow_dedupe),
    ("工具窗镜像：按**键值**摘掉 Esc/Return/Enter（别名也算、带修饰的不算），空串与脏行不进镜像",
     p_mirrorable),
    ("导出→读回是同一套键位，文件给人看得懂，导入折算成 diff", p_export_import),
    (f"别名写法收得下；{len(BAD_FILES)} 份坏文件逐份拒收；文件不存在/JSON 截断回人话",
     p_alias_and_bad_files),
    ("导出目标写不进去时回 (False, 原因) 而不是把对话框带崩", p_export_fail),
    ("默认表里每个动作名在 dispatch 都真有分支", p_dispatch_covered),
    ("可绑动作清单来自默认表本身（不维护第二份清单）", p_action_list),
    (f"上一版那 {len(OLD_WAYS)} 条按不响的写法，当初的理由今天仍成立", p_old_ways_still_dead),
    (f"{len(OLD_WAYS)} 条替代写法逐条进了默认表且自己都能按得响", p_old_ways_fixed),
]


# ============================================================ 正常自检
def run(tmp: str) -> int:
    bad = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn(tmp) if fn.__code__.co_argcount else fn()
        except Exception as exc:  # noqa: BLE001 - 判据自己不许崩，崩了也要留下是哪条
            ok, detail = False, f"判据自身抛 {type(exc).__name__}: {exc}"
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""),
              flush=True)
        if not ok:
            bad.append(name)
    print(f"\n合计 {len(CHECKS)} 条，失败 {len(bad)} 条" + (f"：{bad}" if bad else ""))
    return 1 if bad else 0


# ============================================================ 证伪
def falsify(tmp: str) -> int:
    """把某个实现换成"看起来对"的写法，对应那条自检必须翻红。翻不红＝那条判据是摆设。

    找判据靠**函数身份**而不是名字串：判据文案里带着 `len(DEAD)`、`len(BAD_FILES)`
    这类动态数字，改动一处措辞就会让"按名字找"静默找不到（或者更坏：找到另一条同名
    判据，测的还是另一个东西）。身份匹配找不到就当场抛，不会假装测过了。
    """
    print("---- 证伪：削弱实现以后，对应判据必须变红 ----")
    saved = {k: getattr(KM, k) for k in ("dead", "needs_shift", "effective", "normalize",
                                         "conflicts", "canonical", "from_event", "diff_of",
                                         "read_file", "same_key", "DEFAULT", "export",
                                         "reason", "problems", "bad_pairs", "known_actions",
                                         "joined", "shadowed", "dedupe", "parsed",
                                         "mirrorable", "KEEP_FOR_WINDOWS",
                                         "_SHIFTED", "_MOD_KEYS")}

    def restore():
        for k, v in saved.items():
            setattr(KM, k, v)

    def target(fn) -> tuple[bool, str]:
        for _n, f in CHECKS:
            if f is fn:
                try:
                    return fn(tmp) if fn.__code__.co_argcount else fn()
                except Exception as exc:  # noqa: BLE001
                    return False, f"判据崩了 {type(exc).__name__}: {exc}"
        raise AssertionError(f"证伪表指向的函数不在 CHECKS 里：{fn.__name__}")

    hits: list[tuple[str, bool, str]] = []
    covered: set = set()

    def add(label, fn, stub):
        """装上坏桩 → 跑**同一条**判据 → 它必须红。跑不红就说明那条判据是摆设。"""
        covered.add(fn)
        restore()
        stub()
        try:
            ok, detail = target(fn)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"判据崩了 {type(exc).__name__}: {exc}"
        hits.append((label, not ok, f"削弱后该条判据的结果 ok={ok} —— {detail}"))
        restore()

    # F1 最自然的写法：拿 isEmpty 当合法性判据
    def f1():
        KM.dead = lambda s: QKeySequence(str(s or "")).isEmpty()
    add("F1 isEmpty 当合法性判据 → 死串对照必须漏", p_known_answers, f1)

    # F2 不做移位标点检查
    def f2():
        KM.needs_shift = lambda s: ""
    add("F2 摘掉移位标点检查 → 提示该按哪把键那条必须漏", p_shift_pairs, f2)
    def f2b():
        KM.needs_shift = lambda s: ""
    add("F2b 摘掉移位标点检查 → 已知答案对照（12 条按不出）也必须漏", p_known_answers, f2b)
    def f2c():
        # 常见的"抄全 US 布局移位表"写法：把 + 和 * 也当成按不出，于是误报小键盘上真有的键
        KM._SHIFTED = dict(KM._SHIFTED)
        KM._SHIFTED.update({0x2B: 0x3D, 0x2A: 0x38})
    add("F2c 移位表把「+」「*」也算进去 → 不误报小键盘键那半边必须红", p_known_answers, f2c)

    # F3 effective 忽略 diff（等于"整张表快照"那种存法）
    def f3():
        KM.effective = lambda raw: list(KM.DEFAULT)
    add("F3 effective 忽略 diff → 往返那条必须红", p_diff_roundtrip, f3)
    def f3b():
        KM.effective = lambda raw: list(KM.DEFAULT)
    add("F3b effective 忽略 diff → 增量升级那条必须红", p_incremental_upgrade, f3b)

    # F4 normalize 原样放行脏条目
    def f4():
        def naive(raw):
            r = raw if isinstance(raw, dict) else {}
            return {"off": list(r.get("off") or []), "on": list(r.get("on") or [])}
        KM.normalize = naive
    add("F4 脏条目不丢 → 收口那条必须红", p_normalize_dirty, f4)

    # F5 冲突按 (键, 动作) 分组 → 一键双绑放行
    def f5():
        def naive(rows):
            g = {}
            for s, a, _d in rows:
                g[(KM.parsed(s), a)] = g.get((KM.parsed(s), a), 0) + 1
            return [(s, [a]) for (k, a), n in g.items() if n > 1]
        KM.conflicts = naive
    add("F5 冲突按 (键,动作) 分组 → 一键双绑那条必须红", p_conflicts, f5)

    # F6 canonical 直接用 toString，不回退原串
    def f6():
        KM.canonical = lambda s: QKeySequence(str(s or "")).toString()
    add("F6 显示串不回退原串 → 写法自洽那条必须红", p_default_canonical, f6)

    # F7 same_key 丢掉修饰位
    def f7():
        KM.same_key = lambda a, b: KM.parsed(a) != 0 and \
            (KM.parsed(a) & 0x00FFFFFF) == (KM.parsed(b) & 0x00FFFFFF)
    add("F7 same_key 丢修饰位 → 修饰键差别那条必须红", p_same_key_mods, f7)

    # F8 捕获器不剔纯修饰键、字母不归一
    def f8():
        KM.from_event = lambda key, mods: QKeySequence(int(key) | (int(mods) & KM.MOD_BITS)).toString()
    add("F8 捕获不剔修饰键 → 捕获那条必须红", p_capture, f8)

    # F9 diff_of 的 off 只按"键在不在"算 → 改绑会留下同键两个动作
    def f9():
        def naive(rows):
            want = {s for s, _a, _d in rows}
            dflt = {(s, a) for s, a, _d in KM.DEFAULT}
            return {"off": [s for s, _a, _d in KM.DEFAULT if s not in want],
                    "on": [[s, a] for s, a, _d in sorted(set(rows))
                           if (s, a) not in dflt]}
        KM.diff_of = naive
    add("F9 off 只按键算 → 改绑往返那条必须红", p_diff_roundtrip, f9)

    # F10 导入只保证 JSON 能读
    def f10():
        def naive(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except Exception:  # noqa: BLE001
                return [], "读不了"
            if not isinstance(raw, dict):
                return [], "顶层不是对象"
            out = []
            for i in (raw.get("bindings") if isinstance(raw.get("bindings"), list) else []):
                if isinstance(i, (list, tuple)) and len(i) > 1:
                    out.append((str(i[0]), str(i[1])))
            return out, ""
        KM.read_file = naive
    add("F10 导入只看不判 → 坏文件闸门那条必须红", p_alias_and_bad_files, f10)

    # F11 把老死键塞回默认表
    def f11():
        KM.DEFAULT = saved["DEFAULT"] + (("PgDn", "next", "坏样本"),)
    add("F11 默认表塞回一条死键 → 表级判据必须红", p_default_alive, f11)

    # F12 把按不出的「<」塞回默认表
    def f12():
        KM.DEFAULT = saved["DEFAULT"] + (("<", "mute", "坏样本"),)
    add("F12 默认表塞回裸写的「<」 → 可达性判据必须红", p_default_reachable, f12)

    # F13 默认表里塞一条重复键
    def f13():
        KM.DEFAULT = saved["DEFAULT"] + (("Space", "next", "坏样本"),)
    add("F13 默认表塞回一把重复的键 → 唯一性判据必须红", p_default_single_key, f13)

    # F14 dispatch 里根本没有的动作名混进默认表
    def f14():
        KM.DEFAULT = saved["DEFAULT"] + (("Ctrl+U", "toggle_super_stats", "坏样本"),)
    add("F14 默认表塞一个 dispatch 不认的动作名 → 对账那条必须红", p_dispatch_covered, f14)

    # F15 导出什么都不写（"看起来成功了"）
    def f15():
        KM.export = lambda rows, path: (True, "写了个空壳")
    add("F15 导出什么都不写 → 导出/读回那条必须红", p_export_import, f15)

    # F16 reason 只报死串、不提按不出 → 坏串必须每条都有话说
    def f16():
        KM.reason = lambda s: "Qt 认不出这个键名" if KM.dead(s) else ""
    add("F16 理由只覆盖死串 → 每条坏串都有理由那条必须红", p_reason_text, f16)

    # F17 diff_of 永远返回整表快照 → "默认不动时 diff 该是空的"必须红
    def f17():
        KM.diff_of = lambda rows: {"off": [], "on": [[s, a] for s, a, _d in rows]}
    add("F17 存整表快照 → diff 为空那条必须红", p_diff_minimal, f17)

    # F18 problems 只查死串 → 四种毛病全列出来那条必须红
    def f18():
        KM.problems = lambda rows: [f"{s} 是死键" for s, _a, _d in rows if KM.dead(s)]
    add("F18 体检只查死串 → 四种毛病那条必须红", p_problems, f18)

    # F19 bad_pairs 只圈冲突的一边（只认精确串相同）→ 标红集合那条必须红
    def f19():
        def naive(rows):
            out = set()
            for s, a, _d in rows:
                if KM.reason(s) or a not in KM.known_actions():
                    out.add((s, a))
            seen = set()
            for s, a, _d in rows:
                if (s, a) in seen:
                    out.add((s, a))          # 只把"后来那条"标红，第一条留在白里
                seen.add((s, a))
            return out
        KM.bad_pairs = naive
    add("F19 标红只圈一边 → (键,动作) 集合那条必须红", p_bad_pairs, f19)

    # F20 动作清单另立一份（漏一个）→ "清单来自默认表本身"那条必须红
    def f20():
        KM.known_actions = lambda: {a for _s, a, _d in KM.DEFAULT if a != "mute"}
    add("F20 动作清单少一个 → 清单来源那条必须红", p_action_list, f20)

    # F21 判据松了（死串不再判死）→ "旧写法的理由今天仍成立"那条必须红
    def f21():
        KM.dead = lambda s: False
    add("F21 死串判据放松 → 旧写法对照那条必须红", p_old_ways_still_dead, f21)

    # F22 修好的写法有一条没进表 → "替代写法逐条进表"那条必须红
    def f22():
        KM.DEFAULT = tuple(r for r in saved["DEFAULT"] if r[0] != "\\")
    add("F22 替代写法没进默认表 → 逐条进表那条必须红", p_old_ways_fixed, f22)

    # F23 生效表把用户新加的串原样放进去（不 strip）→ "三处同一个串"必须红
    def f23():
        def naive(raw):
            r = raw if isinstance(raw, dict) else {}
            d = KM.normalize({"off": r.get("off")})
            rows = [x for x in KM.DEFAULT if x[0] not in set(d["off"])]
            for item in (r.get("on") or []):
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    act = str(item[1])
                    rows.append((str(item[0]), act, KM.desc_of(act)))
            return rows
        KM.effective = naive
    add("F23 生效串不去空白（带空白的写法装上就是死键） → 串自洽那条必须红", p_default_canonical, f23)

    # F24 导出把写失败吞掉照样回"成功" → 对话框会显示"已导出"而磁盘上什么都没有
    def f24():
        def naive(rows, path):
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"kind": KM.KIND, "version": KM.VERSION,
                               "bindings": [[KM.canonical(s), a] for s, a, _d in rows]},
                              fh, ensure_ascii=False)
            except Exception:      # noqa: BLE001 - 就是这个"反正不会崩"的写法
                pass
            return True, "已导出"
        KM.export = naive
    add("F24 导出吞掉写失败谎报成功 → 写不进去那条必须红", p_export_fail, f24)

    # F25 同键值不折叠（最自然的写法：joined 直接当 effective 用）→ 折叠/三行必须红
    def f25():
        KM.dedupe = lambda rows: list(rows)
    add("F25 同键值两行原样装上去（Qt 会让两条全哑） → 撞键折叠那条必须红", p_shadow_dedupe, f25)

    # F26 折叠留了第一条（默认行顶掉用户行）→ "留下的是用户那条"必须红
    def f26():
        def naive(rows):
            seen, out = set(), []
            for r in rows:
                c = KM.parsed(r[0])
                if c in (0, KM.UNKNOWN_KEY) or c not in seen:
                    seen.add(c)
                    out.append(r)
            return out
        KM.dedupe = naive
    add("F26 折叠留下的是默认行而不是用户刚加的那行 → 撞键折叠那条必须红", p_shadow_dedupe, f26)

    # F27 去重连修饰位一起吞：Shift+K 会把 K 顶掉，出厂表少一行
    #     （屏幕上看不出来，而"61 行逐条真按"会静默缺一行——那是最贵的漏）
    def f27():
        def naive(rows):
            seen, out = set(), []
            for r in rows:
                c = KM.parsed(r[0]) & 0x01FFFFFF
                if c in (0, KM.UNKNOWN_KEY) or c not in seen:
                    seen.add(c)
                    out.append(r)
            return out
        KM.dedupe = naive
    add("F27 去重把修饰位也算同一把键 → 出厂表零撞键那半边必须红", p_shadow_dedupe, f27)

    # F28 体检不点名撞键 → "体检里有这句话"必须红
    def f28():
        real = saved["problems"]
        KM.problems = lambda rows: [p for p in real(rows) if "同一把键" not in p]
    add("F28 体检漏掉撞键这句话 → 撞键折叠那条必须红", p_shadow_dedupe, f28)

    # F29 标红不圈被顶掉那行 → "标红圈到被顶掉那行"必须红
    #     （样本特意用**同动作**的别名对：那条只可能由撞键规则圈到，冲突规则碰不到它）
    def f29():
        real = saved["bad_pairs"]
        KM.bad_pairs = lambda rows: real(rows) - {tuple(r[:2]) for r, _w in KM.shadowed(rows)}
    add("F29 标红不圈被顶掉那行（用户看不出哪条白装了） → 撞键折叠那条必须红",
        p_shadow_dedupe, f29)

    # F30 台账把死串也算进撞键：PgDn 那一类行会被 dedupe 悄悄吞掉
    def f30():
        def naive(rows):
            rows = list(rows)
            last: dict[int, int] = {}
            for i, r in enumerate(rows):
                last[KM.parsed(r[0])] = i
            return [(r, rows[last[KM.parsed(r[0])]]) for i, r in enumerate(rows)
                    if last[KM.parsed(r[0])] != i]

        def nodedupe(rows):
            rows = list(rows)
            last: dict[int, int] = {}
            for i, r in enumerate(rows):
                last[KM.parsed(r[0])] = i
            return [r for i, r in enumerate(rows) if last[KM.parsed(r[0])] == i]
        KM.shadowed = naive
        KM.dedupe = nodedupe
    add("F30 死串也参与去重（被顶掉的是永不响那种行） → 死串不参与去重那半边必须红",
        p_shadow_dedupe, f30)

    # F31 镜像不筛（整表原样挂到工具窗上）→ 面板自己的 Esc/Return/Enter 必须红：
    #     用户打不开就关不掉那个窗，比"按不响"更糟
    def f31():
        KM.mirrorable = lambda rows: list(rows or ())
    add("F31 一把不筛 → Esc 留给面板那半边必须红", p_mirrorable, f31)

    # F32 按**字面串**筛而不是按键值：用户写成「Escape」的那行照样被镜像走
    def f32():
        def naive(rows):
            return [r for r in rows or ()
                    if isinstance(r, (tuple, list)) and len(r) >= 3
                    and KM.parsed(str(r[0])) != 0 and str(r[0]) not in KM.KEEP_FOR_WINDOWS]
        KM.mirrorable = naive
    add("F32 按串筛（别名 Escape 漏进来） → 按键值筛那条必须红", p_mirrorable, f32)

    # F33 筛的时候把修饰位抹掉：Ctrl+Return 会被当成 Return 摘掉，而那实测是另一把键
    def f33():
        def naive(rows):
            keep = {KM.parsed(k) & 0x01FFFFFF for k in KM.KEEP_FOR_WINDOWS}
            return [r for r in rows or ()
                    if isinstance(r, (tuple, list)) and len(r) >= 3
                    and (KM.parsed(str(r[0])) & 0x01FFFFFF) not in (0,)
                    and (KM.parsed(str(r[0])) & 0x01FFFFFF) not in keep]
        KM.mirrorable = naive
    add("F33 抹掉修饰位（Ctrl+Return 被当成 Return） → 带修饰的那把必须留下", p_mirrorable, f33)

    # F34 空串也挂：QKeySequence("") 是零长序列，白装一条永不响的键
    def f34():
        def naive(rows):
            keep = {KM.parsed(k) for k in KM.KEEP_FOR_WINDOWS}
            return [r for r in rows or ()
                    if isinstance(r, (tuple, list)) and len(r) >= 3
                    and str(r[0]) not in KM.KEEP_FOR_WINDOWS
                    and KM.parsed(str(r[0])) not in keep]
        KM.mirrorable = naive
    add("F34 空串/纯空白也进镜像 → 零长序列那半边必须红", p_mirrorable, f34)

    # F35 不查行形状：install_owner_keys 那边三元组解包，抛出坐在 Qt 槽里＝进程没了
    def f35():
        def naive(rows):
            keep = {KM.parsed(k) for k in KM.KEEP_FOR_WINDOWS}
            out = []
            for r in rows or ():
                if not isinstance(r, (tuple, list)) or len(r) < 3:
                    out.append(r)
                    continue
                c = KM.parsed(str(r[0]))
                if c and c not in keep:
                    out.append(r)
            return out
        KM.mirrorable = naive
    add("F35 脏行也收下 → 形状收口那半边必须红", p_mirrorable, f35)

    bad = 0
    for label, hit, detail in hits:
        if not hit:
            bad += 1
        print(f"[{'CAUGHT' if hit else 'MISSED'}] {label}  —— {detail}")
    # 没有坏桩指向的判据＝从没被证伪过，和"恒真的判据"是同一种摆设，这里也算漏网。
    holes = [n for n, fn in CHECKS if fn not in covered]
    for n in holes:
        print(f"[HOLE ] 这条判据没有任何坏桩指向它：{n}")
    print(f"\n证伪 {len(hits)} 组，漏网 {bad} 组，没人证的判据 {len(holes)} 条")
    return 1 if (bad or holes) else 0


if __name__ == "__main__":
    work = tempfile.mkdtemp(prefix="gelu-keymap-")
    try:
        if "--falsify" in sys.argv:
            sys.exit(falsify(work))
        sys.exit(run(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
