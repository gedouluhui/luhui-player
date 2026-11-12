"""快捷键编辑对话框验收：真对话框、真配置、真导入导出文件，但不抓屏。

用法：
    python tests/test_keymap_dialog.py            # 正常验收：全部断言必须 PASS
    python tests/test_keymap_dialog.py --falsify  # 证伪：把对话框的某个方法换成"常见错误
                                                  # 写法"，对应那条判据**必须**变红

为什么这份不依赖屏幕：这里判的全是**控件属性与落盘内容**（按钮 enabled、表里第几行写了
什么、config.json 里那份 diff），不看一个像素。上一轮 test_ui 的像素段被"显示器睡着"挡了
两次，而对话框这条链本来就没人验过——所以把它摘出来单独跑，随时能跑。

分工：`tests/test_keymap.py` 证明"事实表的规则说得对"，这份证明"对话框真的按那些规则走"
（捕获→改表→禁存→存 diff→重开一致）。屏幕上按下去响不响归 `tests/test_ui.py` 的按键段。

三条老规矩（本仓所有验收脚本共用）：
  · 判据不许恒真 —— 每条都由 `--falsify` 里对应的坏桩证伪；
  · 判据自己不许崩 —— 全部包在 try/except BaseException 里，崩了也要留下是哪条；
  · 坏样本要能"点名"—— 断言里带上没做到的名字，红了去看一眼就知道缺哪半。

不碰真弹窗：`_import`/`_export` 里的 `QFileDialog.get*FileName` 换成桩（只在这个模块命名
空间里换，跑完还原），所以这份脚本不会弹任何对话框、也不需要用户点鼠标。
"""
from __future__ import annotations

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
TMP = tempfile.mkdtemp(prefix="gelu-keydlg-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")          # 必须在 import config 之前
sys.path.insert(0, ROOT)

from PyQt5.QtCore import Qt                                   # noqa: E402
from PyQt5.QtGui import QColor                                 # noqa: E402
from PyQt5.QtWidgets import (QApplication, QTableWidgetItem)   # noqa: E402

from app import config as cfgmod                              # noqa: E402
from app.ui import keymap as KM                               # noqa: E402
from app.ui import keymap_dialog as KD                        # noqa: E402
from app.ui.keymap_dialog import NEW, KeymapDialog            # noqa: E402
from app.ui import theme                                      # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)
results: list[tuple[str, bool, str]] = []
Q = int                                                              # 简写：Qt.Key_* 取整数值
SHIFT, CTRL, ALT, META = (Q(Qt.ShiftModifier), Q(Qt.ControlModifier),
                          Q(Qt.AltModifier), Q(Qt.MetaModifier))


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  —— {detail}", flush=True)


def cfg_file() -> str:
    return os.path.join(TMP, "cfg", "config.json")


def fresh_cfg(raw=None) -> cfgmod.Config:
    """每判一个新配置：既避开"上一判留下的键位"污染，也顺带验 Config 的落盘/读回。"""
    p = cfg_file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.isfile(p):
        os.remove(p)
    c = cfgmod.Config(p)
    if raw is not None:
        c["keymap"] = raw
        c.save()
    return c


def dlg_for(cfg) -> KeymapDialog:
    d = KeymapDialog(cfg)
    d.reload_from_cfg()
    return d


def row_at(d: KeymapDialog, seq: str) -> int:
    for i, (s, _a, _dd) in enumerate(d.rows()):
        if s == seq:
            return i
    raise AssertionError(f"表里没有 {seq!r} 这一行")


def rows_of(d: KeymapDialog) -> set:
    return {(s, a) for s, a, _d in d.rows()}


def press(d: KeymapDialog, key: int, mods: int = 0) -> str:
    return d.feed_key(int(key), int(mods))


def note_text(d: KeymapDialog) -> str:
    return d.note.text()


def pick_action(d: KeymapDialog, act: str) -> None:
    i = d.combo.findData(act)
    if i < 0:
        raise AssertionError(f"下拉里没有动作 {act!r}")
    d.combo.setCurrentIndex(i)


class FakeDialogs:
    """QFileDialog 的替身：把"选文件"这一步变成读一个预先写好的路径。"""

    open_path = ""
    save_path = ""

    @staticmethod
    def getOpenFileName(*_a, **_k):
        return (FakeDialogs.open_path, "快捷键 (*.json)")

    @staticmethod
    def getSaveFileName(*_a, **_k):
        return (FakeDialogs.save_path, "快捷键 (*.json)")


def write_keymap(name: str, bindings, kind: str = KM.KIND) -> str:
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"kind": kind, "version": KM.VERSION, "bindings": bindings},
                  fh, ensure_ascii=False)
    return p


# ============================================================ 判据
def c_opens_with_effective_table() -> tuple[bool, str]:
    """打开对话框摆的是**生效表**，不是出厂表也不是配置原文：删掉的看不见、加的在、撞键的折掉。

    这里刻意用一份有删有加的配置：只喂默认配置的话，"无视配置直接摆出厂表"这种写法
    也能通过（出厂表恰好等于默认配置的生效表），那条判据就是摆设。
    """
    cfg = fresh_cfg({"off": ["K"], "on": [["Ctrl+J", "mute"], ["Page Up", "prev"]]})
    d = dlg_for(cfg)
    want = KM.effective(cfg.get("keymap"))
    pairs = {(s, a) for s, a, _x in d.rows()}
    ok = (d.rows() == want and len(d.rows()) == len(KM.joined(cfg.get("keymap"))) - 1
          and ("K", "toggle_play") not in pairs and ("Ctrl+J", "mute") in pairs
          and ("Page Up", "prev") in pairs and ("PgUp", "prev") not in pairs
          and d._target is None and d.btn_save.isEnabled())
    return ok, (f"表 {len(d.rows())} 行 / 生效表 {len(want)} 行 / 出厂表 {len(KM.DEFAULT)} 行"
                f" 捕获态={d._target} 可保存={d.btn_save.isEnabled()}")


def c_feed_key_five_states() -> tuple[bool, str]:
    """五态逐个用**已知答案**的事件验，不看 UI 只看返回值与表的变化。"""
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    got = {}
    got["none"] = press(d, Q(Qt.Key_J))                       # 不在捕获态
    d.table.selectRow(row_at(d, "K"))
    d._start_change()
    got["wait"] = press(d, Q(Qt.Key_Shift), SHIFT)            # 只按了修饰键
    got["cancel"] = press(d, Q(Qt.Key_Escape))                # Esc 取消
    got["none_after"] = press(d, Q(Qt.Key_J))                 # 取消以后再按＝不相干事件
    pick_action(d, "mute")
    d._start_new()
    got["bad"] = press(d, 0x21, 0)                            # 事件给的是「!」而不带 Shift
    got["set"] = press(d, Q(Qt.Key_PageDown))                 # 同一把键换成能用的写法
    ok = (got["none"] == "none" and got["wait"] == "wait" and got["cancel"] == "cancel"
          and got["none_after"] == "none" and got["bad"].startswith("bad:")
          and got["set"] == "set:PgDown"
          and ("PgDown", "mute") in rows_of(d))
    return ok, f"五态={got} 表尾={d.rows()[-1]}"


def c_modifier_only_does_not_write() -> tuple[bool, str]:
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    before = d.rows()
    pick_action(d, "mute")
    d._start_new()
    leaked = []
    for key, mods in ((Q(Qt.Key_Shift), SHIFT), (Q(Qt.Key_Control), CTRL),
                      (Q(Qt.Key_Alt), ALT), (Q(Qt.Key_Meta), META),
                      (Q(Qt.Key_CapsLock), 0), (Q(Qt.Key_NumLock), 0)):
        r = press(d, key, mods)
        if r != "wait":
            leaked.append((hex(key), mods, r))
    return not leaked and d.rows() == before, f"漏网的修饰键/锁定键={leaked} 表变动={d.rows()!=before}"


def c_escape_restores_browsing_state() -> tuple[bool, str]:
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    d.table.selectRow(row_at(d, "K"))
    before = d.rows()
    d._start_change()
    mid = (d._target, d.btn_change.isEnabled(), d.btn_del.isEnabled())
    press(d, Q(Qt.Key_Escape))
    after = (d._target, d.btn_change.isEnabled(), d.btn_del.isEnabled())
    return (mid == (row_at(d, "K"), False, False) and after[0] is None
            and after[1] and after[2] and d.rows() == before), (f"捕获中={mid} 取消后={after}")


def c_rebind_saves_paired_diff() -> tuple[bool, str]:
    """改绑必须落成 off+on **成对**，而不是只把新那行 append 上去。"""
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    d.table.selectRow(row_at(d, "K"))
    d._start_change()                       # 不先进捕获态，feed_key 只会回 "none"
    r = press(d, Q(Qt.Key_J), CTRL)
    if not r.startswith("set:"):
        return False, f"改键没绑上：{r}"
    emitted: list[list] = []
    d.sig_saved.connect(lambda rows: emitted.append(list(rows)))
    d._save()
    stored = cfgmod.Config(cfg.path)["keymap"]
    eff = rows_of(dlg_for(cfgmod.Config(cfg.path)))
    ok = (stored["off"] == ["K"] and stored["on"] == [["Ctrl+J", "toggle_play"]]
          and ("Ctrl+J", "toggle_play") in eff and ("K", "toggle_play") not in eff
          and len(emitted) == 1 and len(emitted[0]) == len(KM.DEFAULT)
          and os.path.isfile(cfg.path))
    return ok, (f"落盘={stored} 重开生效表含 Ctrl+J={('Ctrl+J','toggle_play') in eff} "
                f"信号次数={len(emitted)}")


def c_delete_row_takes_effect() -> tuple[bool, str]:
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    d.table.selectRow(row_at(d, "E"))
    d._delete_current()
    d._save()
    back = cfgmod.Config(cfg.path)
    eff = KM.effective(back.get("keymap"))
    after = dlg_for(back)
    ok = (len(after.rows()) == len(KM.DEFAULT) - 1 and back["keymap"]["off"] == ["E"]
          and ("E", "screenshot") not in {(s, a) for s, a, _d in eff}
          and ("E", "screenshot") not in rows_of(after) and after.btn_save.isEnabled())
    return ok, f"表 {len(after.rows())} 行 off={back['keymap']['off']} 可保存={after.btn_save.isEnabled()}"


def c_new_row_uses_chosen_action() -> tuple[bool, str]:
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    pick_action(d, "media_info")
    d._start_new()
    r = press(d, Q(Qt.Key_M), CTRL | SHIFT)
    tail = d.rows()[-1]
    d._save()
    back = cfgmod.Config(cfg.path)
    ok = r == "set:Ctrl+Shift+M" and tail == ("Ctrl+Shift+M", "media_info", KM.desc_of("media_info")) \
        and ["Ctrl+Shift+M", "media_info"] in back["keymap"]["on"]
    return ok, f"返回={r} 表尾={tail} 落盘 on={back['keymap']['on']}"


def c_collision_blocks_save() -> tuple[bool, str]:
    """撞键（同一把键两个动作）必须**存不进去**：按钮灰掉，硬调 _save 也不许写盘。

    这里挑的坏法是最常见的那种"提醒一下还是让你存"：对话框里点了新增、按下 PageDown，
    与默认表的 PgDown→next 撞在同一把键上。存进去的话症状变成"用户的键盘坏了"。
    """
    cfg = fresh_cfg()
    before = dict(cfg["keymap"])
    d = dlg_for(cfg)
    emitted: list[list] = []
    d.sig_saved.connect(lambda rows: emitted.append(list(rows)))
    pick_action(d, "mute")
    d._start_new()
    r = press(d, Q(Qt.Key_PageDown))
    probs = KM.problems(d.rows())
    d._save()
    after = dict(cfg["keymap"])
    ok = (r.startswith("set:") and bool(probs) and not d.btn_save.isEnabled()
          and after == before and not emitted and "先解决这些才能保存" in note_text(d))
    return ok, f"绑上={r} 体检={probs} 按钮可用={d.btn_save.isEnabled()} 配置变了={after != before}"


def c_alias_same_action_blocks_save() -> tuple[bool, str]:
    """同键值**同动作**的别名对也要拦住：这一类 conflicts() 看不见（动作没撞）。

    走导入通道造它——文件里 "PgDown → next" 与 "Page Down → next" 是同键值同动作，
    read_file 的冲突闸门放过它（折叠以后行为是对的，收没问题），但对话框把两行都摆上桌
    以后，按下 PageDown 会两条一起沉默（探针 4 步骤 F）。
    """
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    p = write_keymap("alias.json", [["PgDown", "next"], ["Page Down", "next"]])
    FakeDialogs.open_path = p
    old_fd = KD.QFileDialog
    KD.QFileDialog = FakeDialogs
    try:
        d._import()
    finally:
        KD.QFileDialog = old_fd
    probs = KM.problems(d.rows())
    bad = KM.bad_pairs(d.rows())
    red = ("PgDown", "next") in bad or ("Page Down", "next") in bad
    ok = bool(probs) and red and not d.btn_save.isEnabled() and any("同一把键" in x or "重复行" in x
                                                                   for x in probs)
    return ok, f"体检={probs} 标红={sorted(bad)} 可保存={d.btn_save.isEnabled()}"


def c_reset_default_back_to_factory() -> tuple[bool, str]:
    """恢复默认：回到出厂那 61 行，落盘的 diff 是空的。"""
    cfg = fresh_cfg({"off": ["E", "Z"], "on": []})
    d = dlg_for(cfg)
    if len(d.rows()) >= len(KM.DEFAULT):
        return False, f"删了两条以后表还是 {len(d.rows())} 行（配置没生效）"
    d._reset_default()
    d._save()
    back = cfgmod.Config(cfg.path)
    return (d.rows() == list(KM.DEFAULT) and back["keymap"] == {"off": [], "on": []}), \
        f"恢复后 {len(d.rows())} 行，落盘 diff={back['keymap']}"


def c_reload_matches_cfg() -> tuple[bool, str]:
    """配置是唯一真相：改完存盘，同一个实例 reload 与新建实例都该看到同一张表。"""
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    d.table.selectRow(row_at(d, "K"))
    d._start_change()                       # 不先进捕获态，feed_key 只会回 "none"
    r = press(d, Q(Qt.Key_J), CTRL)
    if not r.startswith("set:"):
        return False, f"改键没绑上：{r}"
    d._save()
    d.reload_from_cfg()
    other = dlg_for(cfgmod.Config(cfg.path))
    ok = d.rows() == other.rows() and ("Ctrl+J", "toggle_play") in rows_of(d) \
        and ("K", "toggle_play") not in rows_of(d)
    return ok, f"同一个实例={len(d.rows())} 行 新实例={len(other.rows())} 行 一致={d.rows()==other.rows()}"


def c_export_real_file(tmp: str) -> tuple[bool, str]:
    cfg = fresh_cfg({"off": ["K"], "on": [["Ctrl+J", "mute"]]})
    d = dlg_for(cfg)
    p = os.path.join(tmp, "out", "keys.json")
    FakeDialogs.save_path = p
    old_fd = KD.QFileDialog
    KD.QFileDialog = FakeDialogs
    try:
        d._export()
    finally:
        KD.QFileDialog = old_fd
    read_back, err = KM.read_file(p)
    shape = False
    if os.path.isfile(p):
        blob = json.load(open(p, encoding="utf-8"))
        shape = (blob.get("kind") == KM.KIND and len(blob.get("bindings", [])) == len(d.rows())
                 and all(len(x) == 2 for x in blob["bindings"]))
    ok = os.path.isfile(p) and not err and {(s, a) for s, a in read_back} == rows_of(d) and shape
    return ok, f"文件存在={os.path.isfile(p)} 读回 err={err} 条数={len(read_back)}" \
               f" 形状={shape} note={note_text(d)[:40]}"


def c_export_blocked_path(tmp: str) -> tuple[bool, str]:
    """导出目标写不进去时要在 note 里说清楚，不许抛、也不许谎报"已导出"。"""
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    blocker = os.path.join(tmp, "blocker")
    with open(blocker, "w", encoding="utf-8") as fh:
        fh.write("x")
    FakeDialogs.save_path = os.path.join(blocker, "子", "keys.json")
    old_fd = KD.QFileDialog
    KD.QFileDialog = FakeDialogs
    try:
        d._export()
    finally:
        KD.QFileDialog = old_fd
    txt = note_text(d)
    return ("写不进去" in txt and not os.path.isfile(FakeDialogs.save_path)
            and d.note.styleSheet().find(theme.DANGER_TEXT) >= 0), f"note={txt[:60]}"


def c_import_rejects_bad_files() -> tuple[bool, str]:
    cfg = fresh_cfg()
    base = dlg_for(cfg).rows()
    leaked = []
    for name, body in (("死串", [["PgDn", "next"]]),
                       ("表外动作", [["Ctrl+J", "toggle_super_stats"]]),
                       ("按不出", [["<", "mute"]]),
                       ("空 bindings", []),
                       ("同键两动作", [["Space", "toggle_play"], ["Space", "next"]]),
                       ("kind 不符", [["Space", "toggle_play"]])):
        kind = "other" if name == "kind 不符" else KM.KIND
        p = write_keymap(f"bad-{len(leaked)}.json", body, kind)
        d = dlg_for(cfg)
        FakeDialogs.open_path = p
        old_fd = KD.QFileDialog
        KD.QFileDialog = FakeDialogs
        try:
            d._import()
        finally:
            KD.QFileDialog = old_fd
        if d.rows() != base or "没收这个文件" not in note_text(d):
            leaked.append(f"{name}→{note_text(d)[:40]}")
    # 好文件必须收得下（否则上面那半边是"永远拒绝"的假绿）
    good = write_keymap("good.json", [["Ctrl+J", "mute"], ["Esc", "exit_fullscreen"]])
    dg = dlg_for(cfg)
    FakeDialogs.open_path = good
    old_fd = KD.QFileDialog
    KD.QFileDialog = FakeDialogs
    try:
        dg._import()
    finally:
        KD.QFileDialog = old_fd
    good_ok = rows_of(dg) == {("Ctrl+J", "mute"), ("Esc", "exit_fullscreen")}
    return (not leaked and good_ok), f"漏过的坏文件={leaked} 好文件收下={good_ok}"


def c_table_marks_bad_rows_red() -> tuple[bool, str]:
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    clean = d.table.item(0, 0).foreground().color().name().upper()
    pick_action(d, "mute")
    d._start_new()
    press(d, Q(Qt.Key_PageDown))                        # 撞键：默认表里 PgDown 已经是 next
    bad = KM.bad_pairs(d.rows())
    danger = theme.DANGER_TEXT.upper()
    wrong_rows = {i for i, (s, a, _x) in enumerate(d.rows()) if (s, a) in bad}
    got_red = {i for i in range(len(d.rows()))
               if d.table.item(i, 0).foreground().color().name().upper() == danger}
    ok = (clean != danger and got_red == wrong_rows and bool(wrong_rows))
    return ok, f"干净行颜色={clean} 该红={sorted(wrong_rows)} 实际红={sorted(got_red)}"


def c_tools_disabled_while_capturing() -> tuple[bool, str]:
    """工具按钮跟着「选中行 + 捕获态」走，而且只按真实用户路径刷新（不手调 _update_tools）。

    四段各拦一种写法：点一行就该能用（少了 `itemSelectionChanged` 那条连接，改键按钮永远
    是灰的，"改键"这个功能等于没做）；进捕获态立刻禁用（少了 `_start_change` 里那次刷新，
    再点一次改键会把捕获目标悄悄换到别的行）；停止捕获要恢复；没有选中行时禁用。
    """
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    d.table.selectRow(row_at(d, "K"))                     # 用户用鼠标点一行
    selected = (d.btn_change.isEnabled(), d.btn_del.isEnabled())
    d._start_change()
    during = (d.btn_change.isEnabled(), d.btn_del.isEnabled())
    d._stop_capture()
    after = (d.btn_change.isEnabled(), d.btn_del.isEnabled())
    pick_action(d, "mute")
    d._start_new()
    during_new = (d.btn_change.isEnabled(), d.btn_del.isEnabled())
    d._stop_capture()
    no_sel = dlg_for(cfg)
    no_sel.table.clearSelection()
    no_sel.table.setCurrentCell(-1, -1)
    idle = (no_sel.btn_change.isEnabled(), no_sel.btn_del.isEnabled())
    return (selected == (True, True) and during == (False, False) and after == (True, True)
            and during_new == (False, False) and idle == (False, False)), \
        f"选中={selected} 改键捕获中={during} 停止后={after} 新增捕获中={during_new} 无选中={idle}"


def c_save_is_idempotent() -> tuple[bool, str]:
    """连点两次保存必须落同一份 diff——第二次的 off 不许越叠越长。"""
    cfg = fresh_cfg()
    d = dlg_for(cfg)
    d.table.selectRow(row_at(d, "E"))
    d._delete_current()
    d._save()
    first = json.dumps(cfgmod.Config(cfg.path)["keymap"], sort_keys=True)
    d._save()
    second = json.dumps(cfgmod.Config(cfg.path)["keymap"], sort_keys=True)
    back = dlg_for(cfgmod.Config(cfg.path))
    ok = (first == second and json.loads(first) == {"off": ["E"], "on": []}
          and len(back.rows()) == len(KM.DEFAULT) - 1)
    return ok, f"第一次={first} 第二次={second} 重开 {len(back.rows())} 行"


CHECKS = [
    ("打开对话框摆的是生效表：删的看不见、加的在、别名撞键折掉一条、无冲突可直接保存", c_opens_with_effective_table),
    ("捕获入口五态各就各位（none/wait/cancel/set/bad），且 set 存的是规范串", c_feed_key_five_states),
    ("只按修饰键或锁定键不落进表里（Shift 自己不能被存成一条键位）", c_modifier_only_does_not_write),
    ("Esc 取消回到浏览态：捕获目标清空、改键与删除重新可用、表一字未动", c_escape_restores_browsing_state),
    ("改键保存成对：off 是旧串、on 是新键旧动作，重开生效表看不到旧键", c_rebind_saves_paired_diff),
    ("删一行保存生效：表少一行、off 记下那把键、重开对话框仍看不到那行", c_delete_row_takes_effect),
    ("新增一条用的是下拉里那个动作（不是当前选中行）", c_new_row_uses_chosen_action),
    ("撞键（同一把键两个动作）禁存：按钮灰掉，硬调保存既不写盘也不发信号", c_collision_blocks_save),
    ("同键值同动作的别名对也禁存（conflicts 看不见的那一类）", c_alias_same_action_blocks_save),
    ("恢复默认回到出厂 61 行，落盘的 diff 是空的", c_reset_default_back_to_factory),
    ("配置是唯一真相：保存后 reload 与新建实例看到同一张表", c_reload_matches_cfg),
    ("导出写真文件：条数与表一致、读回来是同一套键位", c_export_real_file),
    ("导出路径写不进去时 note 说清原因并变红，不抛也不谎报成功", c_export_blocked_path),
    ("导入逐份拒收坏文件（六种），且好文件确实收得下（防永远拒绝的假绿）", c_import_rejects_bad_files),
    ("表格里该红的行才红：红行数等于 bad_pairs，干净行不是红的", c_table_marks_bad_rows_red),
    ("工具按钮跟着选中行与捕获态走：点一行要亮、进捕获要灰、取消后要亮、无选中该灰", c_tools_disabled_while_capturing),
    ("连点两次保存落同一份 diff（幂等，不越叠越长）", c_save_is_idempotent),
]


# ============================================================ 正常自检
def run(tmp: str) -> int:
    bad = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn(tmp) if fn.__code__.co_argcount else fn()
        except BaseException as exc:            # noqa: BLE001 - 判据自己崩了也要留名字
            ok, detail = False, f"判据自身抛 {type(exc).__name__}: {exc}"
        check(name, ok, detail)
        if not ok:
            bad.append(name)
    print(f"\n合计 {len(CHECKS)} 条，失败 {len(bad)} 条" + (f"：{bad}" if bad else ""))
    return 1 if bad else 0


# ============================================================ 证伪
def falsify(tmp: str) -> int:
    """把对话框某个方法换成"看起来对"的写法，对应那条判据必须翻红。翻不红＝摆设。"""
    print("---- 证伪：削弱对话框实现以后，对应判据必须变红 ----")
    saved = {n: getattr(KeymapDialog, n) for n in
             ("__init__", "feed_key", "_save", "_delete_current", "_start_new", "_start_change",
              "_validate", "_reset_default", "reload_from_cfg", "_export", "_import",
              "_rebuild", "_update_tools", "_stop_capture", "rows")}

    def restore():
        for n, v in saved.items():
            setattr(KeymapDialog, n, v)

    def target(fn):
        for _n, f in CHECKS:
            if f is fn:
                try:
                    return fn(tmp) if fn.__code__.co_argcount else fn()
                except BaseException as exc:    # noqa: BLE001
                    return False, f"判据崩了 {type(exc).__name__}: {exc}"
        raise AssertionError(f"证伪表指向的函数不在 CHECKS 里：{fn.__name__}")

    hits: list[tuple[str, bool, str]] = []
    covered: set = set()

    def add(label, fn, stub):
        covered.add(fn)
        restore()
        stub()
        try:
            ok, detail = target(fn)
        except BaseException as exc:            # noqa: BLE001
            ok, detail = False, f"判据崩了 {type(exc).__name__}: {exc}"
        hits.append((label, not ok, f"削弱后该条判据 ok={ok} —— {detail}"))
        restore()

    # D1 捕获不做合法性检查（最常见的"能拿到串就收"）
    def d1():
        def naive(self, key, mods):
            if self._target is None:
                return "none"
            if int(key) in (0x01000000, 0x01000001):
                self._stop_capture()
                self._set_note("已取消")
                return "cancel"
            seq = KM.from_event(key, mods)
            if not seq:
                return "wait"
            act = self._rows[self._target][1] if self._target != NEW else str(self.combo.currentData())
            row = (seq, act, KM.desc_of(act))
            if self._target == NEW:
                self._rows.append(row)
                keep = len(self._rows) - 1
            else:
                self._rows[self._target] = row
                keep = int(self._target)
            self._stop_capture()
            self._rebuild(keep=keep)
            self._validate(f"已绑好：{seq}")
            return f"set:{seq}"
        KeymapDialog.feed_key = naive
    add("D1 捕获不判合法性 → 五态那条必须红", c_feed_key_five_states, d1)

    # D2 捕获不看"只按了修饰键"（from_event 回空当成有效空串）
    def d2():
        def naive(self, key, mods):
            if self._target is None:
                return "none"
            seq = KM.canonical(KM.from_event(key, mods))
            act = self._rows[self._target][1] if self._target != NEW else str(self.combo.currentData())
            self._rows.append((seq, act, KM.desc_of(act)))
            self._stop_capture()
            self._rebuild(keep=len(self._rows) - 1)
            return f"set:{seq}"
        KeymapDialog.feed_key = naive
    add("D2 修饰键也被当成一次绑定 → 只按修饰键那条必须红", c_modifier_only_does_not_write, d2)

    # D3 Esc 只回话不清捕获目标
    def d3():
        real = saved["feed_key"]

        def naive(self, key, mods):
            if int(key) == 0x01000000 and self._target is not None:
                self._set_note("已取消")
                return "cancel"
            return real(self, key, mods)
        KeymapDialog.feed_key = naive
    add("D3 Esc 之后还留在捕获态 → 取消态恢复那条必须红", c_escape_restores_browsing_state, d3)

    # D4 存整张表快照（而不是 diff）
    def d4():
        def naive(self):
            if KM.problems(self._rows):
                self._validate()
                return
            self.cfg["keymap"] = {"off": [], "on": [[s, a] for s, a, _d in self._rows]}
            self.cfg.save()
            self.sig_saved.emit(list(self._rows))
        KeymapDialog._save = naive
    add("D4 存整表快照 → 改键成对落 diff 那条必须红", c_rebind_saves_paired_diff, d4)

    # D5 保存只写盘、不发信号（主窗口永远不会重装快捷键）
    def d5():
        def naive(self):
            if KM.problems(self._rows):
                self._validate()
                return
            self.cfg["keymap"] = KM.diff_of(self._rows)
            self.cfg.save()
            self._validate("已保存：无信号")      # 少了 sig_saved.emit 这一行
        KeymapDialog._save = naive
    add("D5 保存不发 sig_saved（主窗口按键位还是老的那套） → 改键那条必须红",
        c_rebind_saves_paired_diff, d5)

    # D6 删除只重画表格、不删数据
    def d6():
        def naive(self):
            r = self._selected()
            if r is None:
                return
            self._rows = self._rows          # 忘了 pop
            self._rebuild(keep=min(r, len(self._rows) - 1))
            self._validate("删掉了。记得点保存")
        KeymapDialog._delete_current = naive
    add("D6 删除没真删数据 → 删一行生效那条必须红", c_delete_row_takes_effect, d6)

    # D7 新增用的是当前选中行的动作
    def d7():
        def naive(self):
            self._target = NEW
            self._cap.setFocus()
            self._set_note("请按下要绑的键")
        real = saved["feed_key"]

        def nf(self, key, mods):
            if self._target == NEW:
                self._target = 0              # 用第 0 行的动作冒充下拉里选的那个
            return real(self, key, mods)
        KeymapDialog._start_new = naive
        KeymapDialog.feed_key = nf
    add("D7 新增拿的是选中行的动作 → 新增那条必须红", c_new_row_uses_chosen_action, d7)

    # D8 "提醒一下还是让你存"：体检不拦保存
    def d8():
        def naive(self, msg=""):
            probs = KM.problems(self._rows)
            self.btn_save.setEnabled(True)    # 就这一行写错
            if probs:
                self._set_note("注意：" + "；".join(probs), bad=True)
            elif msg:
                self._set_note(msg, ok=True)
        KeymapDialog._validate = naive
    add("D8 有冲突仍允许保存 → 撞键禁存那条必须红", c_collision_blocks_save, d8)

    # D9 保存只查 conflicts（同键值同动作的别名对看不见）
    def d9():
        def naive(self, msg=""):
            self.btn_save.setEnabled(not KM.conflicts(self._rows))
            if KM.conflicts(self._rows):
                self._set_note("先解决这些才能保存", bad=True)
        KeymapDialog._validate = naive
    add("D9 体检只查同键不同动作 → 别名同动作撞键那条必须红",
        c_alias_same_action_blocks_save, d9)

    # D10 恢复默认其实回的是"当前配置的表"
    def d10():
        def naive(self):
            self._rows = list(KM.effective(self.cfg.get("keymap")))
            self._rebuild(keep=None)
            self._validate("已恢复")
        KeymapDialog._reset_default = naive
    add("D10 恢复默认拿到的是带用户删改的表 → 恢复默认那条必须红",
        c_reset_default_back_to_factory, d10)

    # D11 reload 不重读配置（同实例复用时会顶着上一次编辑的残表）
    def d11():
        def naive(self):
            self._stop_capture()
            self._rebuild(keep=None)
            self._validate("")
        KeymapDialog.reload_from_cfg = naive
    add("D11 reload 不重读 cfg → 配置是唯一真相那条必须红", c_reload_matches_cfg, d11)

    # D12 导出吞掉写失败谎报成功
    def d12():
        def naive(self):
            p, _f = FakeDialogs.save_path, None
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump({"kind": KM.KIND, "version": KM.VERSION,
                               "bindings": [[KM.canonical(s), a] for s, a, _d in self._rows]},
                              fh, ensure_ascii=False)
            except Exception:                 # noqa: BLE001 - 就是这句"反正不会崩"
                pass
            self._set_note("已导出", ok=True)
        KeymapDialog._export = naive
    add("D12 导出谎报成功 → 写不进去要说清那条必须红", c_export_blocked_path, d12)

    # D13 导入不看错误
    def d13():
        def naive(self):
            p = FakeDialogs.open_path
            try:
                raw = json.load(open(p, encoding="utf-8"))
            except Exception:                 # noqa: BLE001
                return
            binds = raw.get("bindings") or []
            self._rows = [(str(x[0]), str(x[1]), KM.desc_of(str(x[1])))
                          for x in binds if isinstance(x, (list, tuple)) and len(x) > 1]
            self._rebuild(keep=None)
            self._validate(f"读了 {len(self._rows)} 条")
        KeymapDialog._import = naive
    add("D13 导入不校验文件内容 → 坏文件逐份拒收那条必须红", c_import_rejects_bad_files, d13)

    # D14 表格颜色一律同一个前景（红不出来）
    def d14():
        def naive(self, keep):
            sel = self.table.currentRow() if keep is None else keep
            self.table.setRowCount(len(self._rows))
            for i, (seq, act, desc) in enumerate(self._rows):
                for col, text in enumerate((KM.canonical(seq), desc, self._source_of(seq, act))):
                    it = QTableWidgetItem(text)
                    it.setForeground(QColor(theme.TEXT))     # 忘了按 bad_pairs 分色
                    self.table.setItem(i, col, it)
            if 0 <= sel < len(self._rows):
                self.table.selectRow(sel)
            self._update_tools()
        KeymapDialog._rebuild = naive
    add("D14 表格不涂红 → 该红的行才红那条必须红", c_table_marks_bad_rows_red, d14)

    # D15 工具按钮不看捕获态
    def d15():
        def naive(self):
            has = self._selected() is not None
            self.btn_change.setEnabled(has)
            self.btn_del.setEnabled(has)
        KeymapDialog._update_tools = naive
    add("D15 捕获期间按钮仍可点 → 捕获期间禁用那条必须红", c_tools_disabled_while_capturing, d15)

    # D19 把"选中行 → 刷新按钮"那条连接断开，等于 __init__ 里压根没 connect
    def d19():
        real_init = saved["__init__"]

        def naive(self, cfg, parent=None):
            real_init(self, cfg, parent)
            try:
                self.table.itemSelectionChanged.disconnect(self._update_tools)
            except TypeError:                 # 本来就是没接的（那才是我们要拦的写法）
                pass
        KeymapDialog.__init__ = naive
    add("D19 点一行以后按钮不亮（没接 itemSelectionChanged） → 选中行就该能用那半条必须红",
        c_tools_disabled_while_capturing, d19)

    # D20 进捕获态不刷新（少了 _start_change 里那次 _update_tools）
    def d20():
        def naive(self):
            r = self._selected()
            if r is None:
                return
            self._target = r
            self._cap.setFocus()                        # 忘了 self._update_tools()
            self._set_note(f"请按下要绑给「{self._rows[r][2]}」的键（Esc 取消）")
        KeymapDialog._start_change = naive
    add("D20 进捕获态按钮还亮着 → 捕获期间禁用那半条必须红",
        c_tools_disabled_while_capturing, d20)

    # D21 停止捕获不刷新（Esc 以后按钮还是灰的）
    def d21():
        def naive(self):
            self._target = None
            self.table.clearFocus()                      # 忘了 self._update_tools()
        KeymapDialog._stop_capture = naive
    add("D21 取消捕获后按钮仍灰 → 停止后要恢复那半条必须红",
        c_tools_disabled_while_capturing, d21)

    # D16 保存把新算的 off 追加在旧 diff 后面（第二次点保存 off 变成 ["E","E"]）
    def d16():
        def naive(self):
            if KM.problems(self._rows):
                self._validate()
                return
            old = self.cfg.get("keymap") or {}
            d = KM.diff_of(self._rows)
            self.cfg["keymap"] = {"off": list(old.get("off") or []) + d["off"],
                                  "on": (old.get("on") or []) + d["on"]}
            self.cfg.save()
            self._validate("已保存")
            self.sig_saved.emit(list(self._rows))
        KeymapDialog._save = naive
    add("D16 保存的 off 越叠越长 → 连点两次幂等那条必须红", c_save_is_idempotent, d16)

    # D17 打开就摆出厂表（配置里那份 diff 从头到尾没读）
    def d17():
        def naive(self):
            self._stop_capture()
            self._rows = list(KM.DEFAULT)            # 无视 off/on：删掉的还在、加的没有
            self._rebuild(keep=None)
            self._validate("")
        KeymapDialog.reload_from_cfg = naive
    add("D17 无视配置直接摆出厂表 → 打开就是生效表那条必须红",
        c_opens_with_effective_table, d17)

    # D18 导出写的是出厂表而不是屏幕上那份表（改了键的用户导出的却是默认那套）
    def d18():
        def naive(self):
            p, _f = FakeDialogs.save_path, None
            if not p:
                return
            ok, msg = KM.export(list(KM.DEFAULT), p)  # 该用 self._rows
            self._set_note(msg, bad=not ok)
        KeymapDialog._export = naive
    add("D18 导出丢了当前表的改动 → 导出写真文件那条必须红", c_export_real_file, d18)

    bad = 0
    for label, hit, detail in hits:
        if not hit:
            bad += 1
        print(f"[{'CAUGHT' if hit else 'MISSED'}] {label}  —— {detail}", flush=True)
    holes = [n for n, fn in CHECKS if fn not in covered]
    for n in holes:
        print(f"[HOLE ] 这条判据没有任何坏桩指向它：{n}")
    print(f"\n证伪 {len(hits)} 组，漏网 {bad} 组，没人证的判据 {len(holes)} 条")
    return 1 if (bad or holes) else 0


if __name__ == "__main__":
    try:
        sys.exit(falsify(TMP) if "--falsify" in sys.argv else run(TMP))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
