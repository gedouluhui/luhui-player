"""快捷键事实表：默认绑定、自定义的存储模型、合法性判据、冲突检测、导入导出。

这一份为什么必须存在（而不是让 main_window 那张表继续裸着）：Qt 的
`QKeySequence(字符串)` **对写错的键名不报错**。实测（tests/probe_keyseq*.py）：

    QKeySequence("PgDn")      -> 0x01ffffff  ← Qt::Key_UNKNOWN，装了永远不响
    QKeySequence("Backslash") -> 0x01ffffff
    QKeySequence("Minus")     -> 0x01ffffff
    QKeySequence("Equals")    -> 0x01ffffff
    QKeySequence("NotAKey")   -> 0x01ffffff
    QKeySequence("Page Down") -> 0x01000017   ← 活的写法长这样
    QKeySequence("PgDown")    -> 0x01000017

`isEmpty()` 一条都拦不住（它只认空串），`toString()` 也不可靠：键名不认识时它回空串，
可"PgDn"这类**合法别名没进显示表**时它同样回空串——所以判据只能落在解析出来的键值上：
`== 0x01ffffff` 就是死串。原表里 60 条有 5 条属于这一类（PgDn / Backslash / Minus /
Equals / Enter），症状是"按了没反应、也不报错"，正是最不好查的那种坏。

第二类死键更隐蔽：**键值合法，但物理上按不出来**。`QKeySequence("<")` 解析成 0x3c 且
**不带** Shift，而真实键盘上的 '<' 是 Shift+逗号、Windows 递上来的事件键仍是 0x2c+Shift
（实测见 tests/probe_keyseq3.py：按下 Shift+Less 谁都没命中，按下 Shift+逗号命中了
"Shift+,"）。所以原表的 ("<", "mute") 也是死行，得写成 "Shift+,"。

第三类坏发生在**两条一起**的时候：同一个窗口、同一种 context 下挂两条解析成同一键值的
QShortcut，按那把键**两条都不响**（探针 4 步骤 E/F 实测；单装其中任一条都响）。写法不同
也一样撞——"PgDown" 与 "Page Down" 解析成同一个 0x01000017。所以 `effective()` 必须按
**解析键值**去重（`dedupe()`），而 `shadowed()` 负责把"谁顶掉了谁"讲给对话框和测试听。

存储模型用 **diff**（`{"off": [默认表里删掉的行], "on": [[新键, 动作], ...]}`），不存整张表：
存整张表的话，以后版本新增一条默认快捷键，用户那份旧快照会把它**静默吃掉**——他从来没
删过它。diff 只记用户真正动过的部分，升级时新增的默认行照样自动出现。

`off/on` 都按 (键, 动作) **成对**算，不按键算：用户把 "K" 从"播放/暂停"改绑到"下一个"时，
要同时产生 off=["K"+toggle_play] 与 on=[["K", next]]；只按键判断的话旧那行会留在表里，
同一把键绑两个动作，谁响看 Qt 的心情。
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence

Row = tuple[str, str, str]          # (键序列, 动作, 说明)
Diff = dict[str, list]              # {"off": [seq...], "on": [[seq, act]...]}

UNKNOWN_KEY = 0x01ffffff            # C++ 里的 Qt::Key_UNKNOWN；PyQt5 没导出这个枚举值
SHIFT_BIT = 0x02000000              # Qt::ShiftModifier
MOD_BITS = 0x1E000000               # Shift|Control|Alt|Meta（小键盘位 0x20000000 不算修饰）

KIND = "luhui-keymap"
VERSION = 1

# 默认表。键的写法一律取"实测能解析成活键、且 toString 回得来"的那一种，
# 因为这张表同时是设置页的显示文本、配置里的存储值和 help 页的内容——三处必须同一个串。
DEFAULT: tuple[Row, ...] = (
    ("Space", "toggle_play", "播放 / 暂停"),
    ("K", "toggle_play", "播放 / 暂停"),
    ("Left", "back5", "后退 5 秒"),
    ("Right", "fwd5", "前进 5 秒"),
    ("Ctrl+Left", "step_back", "上一帧"),
    ("Ctrl+Right", "step_fwd", "下一帧"),
    (",", "step_back", "上一帧"),
    (".", "step_fwd", "下一帧"),
    ("PgUp", "prev", "上一个"),
    ("PgDown", "next", "下一个"),
    # 章节：与 mpv 默认的 PgUp/PgDn 同义但让位给"上一个/下一个文件"，加 Ctrl 作区分；
    # 这条命令在最后一章不会把片子顶到文件尾（见 engine/player.py chapter_step 注释）。
    ("Ctrl+PgUp", "prev_chapter", "上一章"),
    ("Ctrl+PgDown", "next_chapter", "下一章"),
    ("Up", "vol_up", "音量 +5"),
    ("Down", "vol_down", "音量 -5"),
    ("Z", "mute", "静音切换"),
    ("Shift+,", "mute", "静音切换"),
    ("[", "speed_down", "减速"),
    ("]", "speed_up", "加速"),
    ("\\", "speed_reset", "速度回到 1x"),
    ("J", "sub_delay_down", "字幕延迟 -0.1 秒"),
    ("Shift+J", "sub_delay_up", "字幕延迟 +0.1 秒"),
    ("-", "sub_size_down", "字幕缩小一档"),
    ("=", "sub_size_up", "字幕放大一档"),
    ("Shift+T", "tone_cycle", "HDR 色调映射换一档"),
    ("Shift+G", "eq_reset", "画面均衡复位（亮度/对比度/灰度/饱和度/色相）"),
    # 画面页那一族（engine/transform.py）。R / P / H 与 mpv 的默认键位同义
    # （mpv 的 input.conf 里 r 转画面、P 切 keepaspect），换过来的用户不用重新学；
    # 剩下的 O / Shift+P / Shift+R / Shift+H 是这一族自己排的，都查过与上表不撞键值。
    ("R", "rotate_cycle", "画面旋转 90°"),
    ("Shift+R", "aspect_cycle", "画面比例换一档"),
    ("O", "crop_cycle", "按比例裁剪换一档"),
    ("P", "keepaspect_toggle", "保持比例（不拉伸）"),
    ("Shift+P", "panscan_toggle", "充满画面 / 还原"),
    ("H", "mirror_h_toggle", "左右镜像"),
    ("Shift+H", "mirror_v_toggle", "上下镜像"),
    ("Ctrl+G", "transform_reset", "画面整体复位（几何＋增强）"),
    ("Shift+A", "ab_a", "A-B 循环起点"),
    ("Shift+B", "ab_b", "A-B 循环终点"),
    ("Shift+C", "ab_clear", "清除 A-B 循环"),
    ("Shift+U", "menu_device", "音频输出设备"),
    ("0", "jump_0", "跳到开头"),
    ("1", "jump_10", "跳到 10%"),
    ("2", "jump_20", "跳到 20%"),
    ("3", "jump_30", "跳到 30%"),
    ("4", "jump_40", "跳到 40%"),
    ("5", "jump_50", "跳到 50%"),
    ("6", "jump_60", "跳到 60%"),
    ("7", "jump_70", "跳到 70%"),
    ("8", "jump_80", "跳到 80%"),
    ("9", "jump_90", "跳到 90%"),
    ("V", "menu_sub", "字幕轨菜单"),
    ("U", "menu_audio", "音轨菜单"),
    ("S", "shuffle", "乱序切换"),
    ("L", "loop", "循环模式切换"),
    ("E", "screenshot", "截图存帧"),
    ("B", "burst_shot", "连拍"),
    ("G", "export_gif", "导出 GIF 片段…"),
    ("Return", "fullscreen", "全屏切换"),
    ("Enter", "fullscreen", "全屏切换"),
    ("F", "fullscreen", "全屏切换"),
    ("Alt+Return", "fullscreen", "全屏切换"),
    ("Esc", "exit_fullscreen", "退出全屏"),
    ("Shift+W", "always_on_top", "窗口置顶切换"),
    ("Ctrl+L", "toggle_playlist", "播放列表开关"),
    ("Ctrl+M", "toggle_library", "媒体库开关"),
    ("Ctrl+T", "toggle_tweak", "调节面板开关"),
    ("Ctrl+P", "toggle_transform", "画面面板开关"),
    ("I", "toggle_stats", "统计面板开关"),
    ("Ctrl+K", "keymap_edit", "快捷键设置…"),
    ("Ctrl+S", "sleep_timer", "睡眠定时…"),
    ("Ctrl+,", "settings", "设置…"),
    ("Ctrl+O", "open_files", "打开文件…"),
    ("Ctrl+Shift+O", "open_dir", "打开文件夹…"),
    ("Ctrl+I", "media_info", "媒体信息"),
    ("F1", "help_keys", "快捷键一览"),
    ("Ctrl+W", "quit", "关闭"),
)

# US 布局上"只有按 Shift 才打得出来"的字符 → 它所在的物理键。写成裸字符（不带 Shift）
# 就是上一段说的第二类死键。'+'（0x2B）与 '*'（0x2A）不列：小键盘上有独立键位，裸写按得出来。
_SHIFTED: dict[int, int] = {
    0x21: 0x31, 0x40: 0x32, 0x23: 0x33, 0x24: 0x34, 0x25: 0x35, 0x5E: 0x36,
    0x26: 0x37, 0x28: 0x39, 0x29: 0x30,          # ! @ # $ % ^ & ( )
    0x5F: 0x2D, 0x7B: 0x5B, 0x7D: 0x5D, 0x7C: 0x5C, 0x7E: 0x60,   # _ { } | ~
    0x3A: 0x3B, 0x22: 0x27, 0x3C: 0x2C, 0x3E: 0x2E, 0x3F: 0x2F,   # : " < > ?
}

# 修饰键与锁定键自己的"键码"（Qt 把它们也编成了键）：按下 Shift 不该被当成一次绑定。
# 这些值实测取自 PyQt5：Key_Shift=0x01000020、Key_Control=0x01000021、Key_Meta=0x01000022、
# Key_Alt=0x01000023、Key_AltGr=0x01000024、Key_CapsLock=0x01000028。
def _mod_keycodes() -> set[int]:
    out: set[int] = set()
    for name in ("Key_Shift", "Key_Control", "Key_Meta", "Key_Alt", "Key_AltGr",
                 "Key_CapsLock", "Key_NumLock", "Key_ScrollLock"):
        v = getattr(Qt, name, None)
        if v is not None:
            out.add(int(v))
    return out


_MOD_KEYS = _mod_keycodes()

# 字母大写归一：Qt 把 "k" 和 "K" 都解析成 0x4b（实测），而事件里可能给 0x6a，
# 存储统一用大写写法，免得同一条键在配置里出现两种拼法而被判成两条。
_LETTER_LOWER = range(0x61, 0x7B)


# ================= 单条键序列 =================
def parsed(seq: str) -> int:
    """解析成 QKeySequence 的第一段键值；解析不出东西回 0。"""
    try:
        qs = QKeySequence(str(seq or "").strip())
        if qs.count() < 1:
            return 0
        return int(qs[0])
    except Exception:      # noqa: BLE001 - 判据自己不许崩
        return 0


def dead(seq: str) -> bool:
    """这条键序列永远不会响：空串、多段序列、或 Qt 认不出的键名。"""
    code = parsed(seq)
    if code in (0, UNKNOWN_KEY):
        return True
    if not str(seq or "").strip():
        return True
    try:
        if QKeySequence(str(seq).strip()).count() != 1:
            return True       # 多段（"Ctrl+A Ctrl+B"）实测解析成 UNKNOWN，这里再兜一层
    except Exception:          # noqa: BLE001
        return True
    return False


def needs_shift(seq: str) -> str:
    """返回提示串（空=没问题）：键值合法，但物理上要按 Shift+某键才出来。"""
    code = parsed(seq)
    if code in (0, UNKNOWN_KEY):
        return ""           # 死串由 dead() 管，这里不重复报
    key = code & ~MOD_BITS
    base = _SHIFTED.get(key)
    if base is None or (code & SHIFT_BIT):
        return ""
    return (f"{chr(key)} 是 Shift+{chr(base)} 打出来的字符，裸写按不出来；"
            f"请写成 Shift+{chr(base)}")


def reason(seq: str) -> str:
    """这条键能不能用；不能用时说清楚为什么。"""
    s = str(seq or "").strip()
    if not s:
        return "空的"
    if dead(s):
        return "Qt 认不出这个键名（写了也不响）；请直接在输入框里按下组合键"
    note = needs_shift(s)
    return note


def valid(seq: str) -> bool:
    return not reason(seq)


def canonical(seq: str) -> str:
    """显示用写法。toString 认不出来的（"Page Down" 那一类别名）退回原串。"""
    s = str(seq or "").strip()
    try:
        got = QKeySequence(s).toString()
    except Exception:          # noqa: BLE001
        return s
    return got or s


def same_key(a: str, b: str) -> bool:
    """两串是不是同一把键（"PgDown"/"Page Down" 该算同一把，"K"/"Shift+K" 不算）。"""
    pa, pb = parsed(a), parsed(b)
    return pa != 0 and pa == pb


def from_event(key: int, mods: int) -> str:
    """按键捕获：QKeyEvent 的 (key, modifiers) → 存储用的串。

    三处收口，每处都对应一个实测过的坑：
      · 修饰键/锁定键本身（Key_Shift=0x01000020 那一族）回空串——否则用户按住 Shift 的
        瞬间就被存成一条"Shift+某乱码"，而 Qt 那边把它解析成 Key_UNKNOWN；
      · Keypad/GroupSwitch 位剔掉（小键盘 Enter 的事件带 Keypad 位，留着 toString 出不来）；
      · 字母归一成大写（Qt 把 "k" 与 "K" 都解析成 0x4b，实测；存储统一大写，免得同一把键
        在配置里两种拼法被判成两条）。
    """
    raw = int(key)
    if raw in _MOD_KEYS:
        return ""
    keep = int(mods) & MOD_BITS
    k = raw & 0x01FFFFFF
    if k in _MOD_KEYS or k == UNKNOWN_KEY or k < 0x20:
        return ""
    if 0x61 <= k <= 0x7A:
        k -= 0x20
    try:
        return QKeySequence(int(k) | keep).toString()
    except Exception:          # noqa: BLE001
        return ""


# ================= 动作清单 =================
def known_actions() -> set[str]:
    """可绑的动作 = 默认表里出现过的动作名。

    不搞第二份动作清单：要给一个新增动作开快捷键，先在默认表里给它一行——那一行顺带
    就提供了中文名。这样"dispatch 有没有这个分支"由 tests/test_ui.py 的对账负责。
    """
    return {a for _s, a, _d in DEFAULT}


def desc_of(action: str) -> str:
    for _s, a, d in DEFAULT:
        if a == action:
            return d
    return action


def action_label(action: str) -> str:
    """对话框里"动作"那一列：同一个动作可能有多行说明，取第一条的措辞。"""
    return desc_of(action)


# ================= diff 模型 =================
def normalize(raw: Any) -> Diff:
    """把配置里那坨收口成 `{"off": [seq…], "on": [[seq, act]…]}`。

    脏条目（非列表、缺字段、动作名不认识、键是死串）逐条丢掉而不是整份作废：
    用户手改 JSON 打错一个逗号，不该导致他把默认快捷键全丢。

    `off` 只留**当前默认表里真有其串**的那些：留垃圾没意义（删不掉任何东西），却会永远
    躺在配置里；而且 "off": "不是列表" 这种写法，`for item in "字符串"` 会把每个汉字都
    当成一条删除记录——所以先验类型，再按默认表筛。
    """
    off: list[str] = []
    on: list[list[str]] = []
    known = known_actions()
    default_seqs = {s for s, _a, _d in DEFAULT}
    if isinstance(raw, dict):
        seen_off: set[str] = set()
        raw_off = raw.get("off")
        for item in (raw_off if isinstance(raw_off, (list, tuple)) else []):
            s = str(item or "").strip()
            if s in default_seqs and s not in seen_off:
                seen_off.add(s)
                off.append(s)
        seen_on: set[str] = set()
        raw_on = raw.get("on")
        for item in (raw_on if isinstance(raw_on, (list, tuple)) else []):
            if isinstance(item, dict):
                item = [item.get("seq"), item.get("action")]
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            s, act = str(item[0] or "").strip(), str(item[1] or "").strip()
            if not s or dead(s) or act not in known:
                continue
            pair = f"{s}\x00{act}"
            if pair in seen_on:
                continue
            seen_on.add(pair)
            on.append([s, act])
    return {"off": off, "on": on}


def joined(raw: Any) -> list[Row]:
    """默认表去掉被删的行，再加上用户新增的行 —— **还没**按键值去重的那一张。

    off 按**精确串**比对而不是按 `same_key`：默认表里 "K" 与 "Shift+K" 是两把不同的键
    （实测键值 0x4b 与 0x0200004b），用串就够。反过来说，用户要是把配置里的 "PgDown"
    手打成 "Page Down"，默认那一行会删不掉，于是这张表里同一把键出现两次 ——
    那正是 `shadowed()` 与 `dedupe()` 要处理的场面。
    """
    d = normalize(raw)
    want_off = set(d["off"])
    rows: list[Row] = [r for r in DEFAULT if r[0] not in want_off]
    for seq, act in d["on"]:
        rows.append((seq, act, desc_of(act)))
    return rows


def _last_index_per_code(rows: list[Row]) -> dict[int, int]:
    """每把键（解析键值）最后出现在第几行；死串（0 与 Key_UNKNOWN）不进这张表。"""
    out: dict[int, int] = {}
    for i, (s, _a, _d) in enumerate(rows):
        c = parsed(s)
        if c not in (0, UNKNOWN_KEY):
            out[c] = i
    return out


def shadowed(rows: Iterable[Row]) -> list[tuple[Row, Row]]:
    """按解析键值找出"被同一把键的后一行顶掉"的行，回 [(被顶掉的行, 站住的行)]。

    为什么必须有这一步（探针 4 步骤 E/F 实测，tests/probe_keypress_abort.py）：同一个
    窗口、同一个 context 下挂**两条解析成同一键值**的 QShortcut，按一次那把键**两条都
    不响**——不是"前一条赢"，是全沉默。所以一张有撞键的表装上以后，坏的不止用户新加
    那一行，连默认那行也一起哑（"改一个键位把另一个键位弄坏"，比装不上隐蔽得多）。
    对照组：单装 "PgDown" 响、单装 "Page Down" 响、两条一起挂两条都不响；而
    "PageDown"/"PgDn" 这种解析成 Key_UNKNOWN 的死写法**不会**偷走别人的键（它自己
    永不匹配），所以死串不参与这里的去重，留给 dead()/problems() 单独点名。
    """
    rows = list(rows)
    last = _last_index_per_code(rows)
    out: list[tuple[Row, Row]] = []
    for i, r in enumerate(rows):
        c = parsed(r[0])
        if c in (0, UNKNOWN_KEY):
            continue
        j = last[c]
        if j != i:
            out.append((r, rows[j]))
    return out


def dedupe(rows: Iterable[Row]) -> list[Row]:
    """同一把键只留最后一条（其余位置不变）：最后一条是用户新增的行，也是真装得上的那条。

    留"最后"而不是"最先"：`joined()` 把用户的 `on` 行追加在默认行之后，用户显式加的
    绑定该赢过默认表；这也让界面上显示的行与屏幕上真的生效的行是同一行。
    """
    rows = list(rows)
    last = _last_index_per_code(rows)
    return [r for i, r in enumerate(rows)
            if parsed(r[0]) in (0, UNKNOWN_KEY) or last[parsed(r[0])] == i]


def effective(raw: Any) -> list[Row]:
    """生效表 = 默认表去掉被删的行 + 用户新增的行，再按解析键值去重。

    这里去重是**产品行为**而不是美化：main_window 拿着这张表逐条装 QShortcut，而探针
    实测"同键值两条一起挂 = 两条都不响"。少这一步，用户加一条 "Page Down → 静音"
    就会把默认表里的 "PgDown → 下一个" 一起弄哑，屏幕上看不出来、也不报错。
    """
    return dedupe(joined(raw))


def keys_of(rows: Iterable[Row], actions: Iterable[str]) -> list[str]:
    """生效表里这些动作绑的键，按表内先后排，重复写法只留第一次出现的那一条。

    为什么返回**一串**而不是第一把：一个动作可以绑多把键（播放/暂停同时是 Space 和 K），
    提示行要说全，说半句等于让用户以为另两把没得用。
    为什么按表内顺序而不是排序：表内顺序就是 F1 一览里的顺序，两处对得上用户才敢信。
    """
    want = set(actions)
    out: list[str] = []
    for seq, act, _desc in rows:
        if act in want and seq not in out:
            out.append(seq)
    return out


def key_hint(rows: Iterable[Row], groups: Iterable[Iterable[Any]], sep: str = " · ") -> str:
    """把「(动作们, 这一段的简短说法)」拼成一行按键提示，**键名从生效表取，不手抄**。

    为什么非要算出来而不是在面板里写死一行「R 旋转 · Shift+R 比例 · H 镜像」：
    那句话的**键**部分是这张表的事实，而表是用户可以改的（Ctrl+K）。写死的版本在
    用户把旋转改成 X 之后继续说"按 R"，界面就地变成一句谎话——而它撒谎的地方正好是
    "我不记得按键时来看的那一行"，是最需要可信的一处。这里改成从 `effective(cfg["keymap"])`
    算，改键、加键、删键都跟着动。
    说法（"旋转""比例"）留在调用方：那是这一页自己的措辞，不是键位表的内容，
    表里的说明列（"画面旋转 90°"）在 300px 的侧栏放不下。

    某一段的动作**一把键都没绑**（用户删掉了）时整段不出现：宁可这一行短一截，
    也不要留着一条按了没反应的键。全空时回空串，由调用方决定把这一行藏起来。
    """
    parts: list[str] = []
    for actions, label in groups:
        ks = keys_of(rows, actions)
        if ks:
            parts.append("/".join(ks) + " " + label)
    return sep.join(parts)


def tip_note(rows: Iterable[Row], actions: Iterable[str], sep: str = "/") -> str:
    """按钮提示后面那一截「（V）」「（Enter/F）」；**键名从生效表取，不在按钮旁边手抄**。

    和 `key_hint` 是同一件事的两种形状：那一把管侧栏页脚的一行字，这一把管悬停里的一颗按钮。
    为什么"一把键都没绑"时回**空串**而不是「（未绑定）」：tooltip 要说的是"这颗按钮干什么、
    怎么按"，没键的时候鼠标照样点得动，括号里写什么都是一句多余的话，还会让人以为它坏了。
    分隔符默认不带空格：控制条那一排按钮 20px 一颗，悬停里的句子越短越不容易被读成两件事。
    """
    ks = keys_of(rows, actions)
    return f"（{sep.join(ks)}）" if ks else ""


# 工具窗（现在只有截图工坊）镜像键位时**留下不镜像**的那几把：它们属于窗口自己。
# Esc 关掉工坊、Return/Enter 触发它的默认按钮——把这两类抢给播放器，用户的"逃不出这个
# 面板"就是字面意义上的逃不出去。
KEEP_FOR_WINDOWS = ("Esc", "Return", "Enter")


def mirrorable(rows: Iterable[Row]) -> list[Row]:
    """生效表里"在工具窗上也该照样能按"的那部分行。

    为什么需要这一步（而不是在主窗口装一遍就完事）：Qt 的 `QShortcut(context=WindowShortcut)`
    只认**当前活动窗口**，所以只要二级窗口抢到了活动权，主窗那 60 条键就整体失灵。
    2026-10-08 真机实测（tests/probe_gif3.py B1–B4）：工坊一开，把按键事件递给主窗按 B
    命中的动作是 `[]`；`activateWindow()` 把主窗抢回来立刻又是 `['burst_shot']`——
    也就是"打开截图工坊以后播放器所有快捷键都按不响"，而用户恰恰会开着工坊放片。

    为什么这里敢整表镜像、连 `0`–`9` 与 `,` `.` `-` `=` 都镜像：工坊里 14 个控件
    （两个数字框、两个下拉、一个复选、七个按钮、进度条等）**全部** `NoFocus`
    （gif_dialog 实测），面板上没有能被敲字的输入框，所以不存在"想打 12.5 秒却跳了进度"
    这种抢键。反过来说，哪天给工坊加了能输入的框，这一步就得改成按输入态让路。

    两件事在这里收口，不是顺手：① **按键值筛而不是按字面串**——`keep` 是
    `{parsed("Esc"), parsed("Return"), parsed("Enter")}` 三个键值，所以用户把退出那行写成
    「Escape」也一样留给窗口自己（按串比对会漏掉别名，把"关掉面板"抢给播放器）。
    ② 解析成 0 的空串行不进镜像：`QKeySequence("")` 建出来的快捷键是个零长序列，
    挂在面板上白占一条，`QShortcut(空)` 在本机还会把 activated 连上永不响的键。
    形状不对的行（不是三元组）整条丢掉——这张表可能来自用户手改过的 JSON。
    """
    keep = {parsed(k) for k in KEEP_FOR_WINDOWS}
    out: list[Row] = []
    for row in rows or ():
        if not isinstance(row, (tuple, list)) or len(row) < 3:
            continue
        code = parsed(str(row[0]))
        if code == 0 or code in keep:
            continue
        out.append(row)
    return out


def diff_of(rows: Iterable[Row]) -> Diff:
    """把界面上编辑完的整张表折算成 diff（存储用的唯一形式）。"""
    want = {(str(s).strip(), a) for s, a, _d in rows if str(s or "").strip()}
    default_pairs = {(s, a) for s, a, _d in DEFAULT}
    off: list[str] = []
    for s, a, _d in DEFAULT:
        if (s, a) not in want and s not in off:
            off.append(s)
    on: list[list[str]] = []
    for s, a in sorted(want):
        if (s, a) not in default_pairs:
            on.append([s, a])
    return {"off": off, "on": on}


# ================= 冲突与体检 =================
def conflicts(rows: Iterable[Row]) -> list[tuple[str, list[str]]]:
    """同一把键绑了不同动作 → [(显示串, [动作…]) ]。同键同动作（默认表里就有）不算冲突。"""
    buckets: dict[int, tuple[str, list[str]]] = {}
    for s, a, _d in rows:
        code = parsed(s)
        if code == 0:
            continue
        txt = canonical(s)
        if code in buckets:
            acts = buckets[code][1]
            if a not in acts:
                acts.append(a)
        else:
            buckets[code] = (txt, [a])
    return [(txt, acts) for txt, acts in buckets.values() if len(acts) > 1]


def problems(rows: Iterable[Row]) -> list[str]:
    """整张表的毛病清单，对话框和测试共用一份措辞。"""
    out: list[str] = []
    seen: set[str] = set()
    for s, a, _d in rows:
        if dead(s):
            out.append(f"{s or '(空)'} → {a}：{reason(s)}")
        elif needs_shift(s):
            out.append(f"{s} → {a}：{needs_shift(s)}")
        elif a not in known_actions():
            out.append(f"{s} → {a}：动作名不在表里")
        dup = f"{s}\x00{a}"
        if dup in seen:
            out.append(f"{s} → {a}：重复行")
        seen.add(dup)
    for txt, acts in conflicts(rows):
        out.append(f"{txt} 同时绑了 {'、'.join(acts)}：同一次按键只会响一条，删掉其中一个")
    for loser, winner in shadowed(rows):
        # 写法相同的那一撞由上面两条（重复行 / 同时绑了）负责点名，这里只补"写法不同
        # 但撞在同一把键上"那种——它是唯一一处别的检查看不见的坏（"Page Down" 与
        # "PgDown" 的 canonical 都是 "PgDown"，所以这里**不能**比 canonical）。
        if str(loser[0]).strip() != str(winner[0]).strip():
            out.append(f"{loser[0]} → {loser[1]}：和 {winner[0]} → {winner[1]} 是同一把键，"
                       f"两条一起挂会让这把键彻底不响；留下的是后面那条")
    return out


def bad_pairs(rows: Iterable[Row]) -> set[tuple[str, str]]:
    """哪几行 (键, 动作) 有问题——对话框拿它把该行涂红。

    为什么不直接从 `problems()` 的字符串里找：冲突那几条的措辞是"PgDown 同时绑了…"，
    里面没有"键 → 动作"这个形状，拿子串匹配会漏掉最需要标红的一行。这里按 (键, 动作)
    成对返回，冲突则把共用这把键的行**全部**标出来（要用户自己决定删哪一条）。
    """
    out: set[tuple[str, str]] = set()
    count: dict[tuple[str, str], int] = {}
    by_key: dict[int, list[tuple[str, str]]] = {}
    for s, a, _d in rows:
        pair = (s, a)
        count[pair] = count.get(pair, 0) + 1
        by_key.setdefault(parsed(s), []).append(pair)
        if reason(s) or a not in known_actions():
            out.add(pair)
    for pair, n in count.items():
        if n > 1:
            out.add(pair)
    for group in by_key.values():
        if len({a for _s, a in group}) > 1:
            out.update(group)
    for loser, _winner in shadowed(rows):
        # 同键值撞车：即便动作同名也要标，因为装上以后**两条都不响**（探针 4 步骤 F）。
        # 只涂被顶掉那行、不涂站住那行——站住那行是屏幕上真的生效的那条，涂红会误导。
        out.add((loser[0], loser[1]))
    return out


# ================= 导入 / 导出 =================
def export(rows: Iterable[Row], path: str) -> tuple[bool, str]:
    """导出成给人看、也能给别的机器导入的整表（不是 diff）。

    为什么导出整表而配置里存 diff：导出的用途是"我这台机器的这套键位"，跨版本要能
    原样复现；配置里存 diff 的用途是"别遮住以后新增的默认行"。两个目的不一样。
    """
    body = {"kind": KIND, "version": VERSION, "app": "LuHuiPlayer",
            "bindings": [[canonical(s), a] for s, a, _d in rows]}
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        return False, f"写不进去：{exc}"
    return True, f"导出 {len(body['bindings'])} 条 → {path}"


def read_file(path: str) -> tuple[list[tuple[str, str]], str]:
    """读导入文件；返回 ( [(键, 动作)] , 错误)。错误非空时第一条别用。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return [], "文件不存在"
    except json.JSONDecodeError as exc:
        return [], f"不是合法 JSON：{exc}"
    except OSError as exc:
        return [], f"读不了：{exc}"
    if not isinstance(raw, dict):
        return [], "顶层不是对象，不像快捷键文件"
    if str(raw.get("kind") or "") != KIND:
        return [], f"kind 应为 {KIND}，实为 {raw.get('kind')!r}（这不是本程序的快捷键文件）"
    binds = raw.get("bindings")
    if not isinstance(binds, list) or not binds:
        return [], "bindings 缺失或为空——空的键位表会把所有快捷键删光，不收"
    out: list[tuple[str, str]] = []
    for i, item in enumerate(binds):
        if isinstance(item, dict):
            item = [item.get("seq") or item.get("key"), item.get("action")]
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            return [], f"第 {i + 1} 条不是 [键, 动作] 两项"
        s, act = str(item[0] or "").strip(), str(item[1] or "").strip()
        if not s:
            return [], f"第 {i + 1} 条键是空的"
        if dead(s):
            return [], f"第 {i + 1} 条 {s!r}：{reason(s)}"
        if needs_shift(s):
            return [], f"第 {i + 1} 条 {s!r}：{needs_shift(s)}"
        if act not in known_actions():
            return [], f"第 {i + 1} 条动作 {act!r} 本程序没有"
        out.append((s, act))
    conf = conflicts([(s, a, "") for s, a in out])
    if conf:
        txt, acts = conf[0]
        return [], f"文件里 {txt} 同时绑了 {'、'.join(acts)}，收了就是一条永远不响的键"
    # 这里不按"同键值同动作"再拦一道：conflicts() 已经按**解析键值**分桶，别名撞键
    # （"PgDown" 与 "Page Down" 绑不同动作）就在上面这条里被点了名；同键值同动作的两行
    # 收进来以后由 effective()/dedupe() 折叠成一条，屏幕上这把键照样响，不必拒收。
    return out, ""


def import_diff(path: str) -> tuple[Diff, str]:
    rows, err = read_file(path)
    if err:
        return normalize({}), err
    return diff_of([(s, a, desc_of(a)) for s, a in rows]), ""
