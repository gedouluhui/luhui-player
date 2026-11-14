# -*- coding: utf-8 -*-
"""拖动/seek 竞态自检：节流、松手精确、防回弹、状态锁、续播顺序、字幕代际。

对应用户能直接碰到的一串事：
  S1 松手必须是**精确帧**（absolute+exact）；拖动途中用关键帧档（absolute+keyframes）；
  S2 `open_files(start_pos=…)` 把续播位置带在 loadfile 命令里（看门狗恢复不再抢跑 seek）；
  S3 控制条把 `exact` 原样传给内核（旧代码两条分支一模一样）；
  S4 拖动节流 60ms、松手后 0.22s 防回弹窗口（>0.6s 的滞后旧回写丢弃）；
  S5 `_props` 有锁且并发读写不炸（IPC 读线程写、UI 线程读）；
  S6 外挂字幕带目标路径：快速 A→B 时 A 的回包不许把字幕挂到 B 上；
  S7 看门狗恢复顺序：`_start_watchdog` 里不许再在 file-loaded 之前发 seek。

S1/S2/S5 用假 IPC 直接观察命令；S3/S4 用 offscreen 真控件；S6/S7 是纯函数 + 结构闸门。
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import threading
import types

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = tempfile.mkdtemp(prefix="seek-polish-", dir=HERE)
os.environ["LUHUI_DATA"] = DATA
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PyQt5.QtGui import QMouseEvent  # noqa: E402
from PyQt5.QtWidgets import QApplication, QLabel  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine import player as PL  # noqa: E402
from app.ui import controls_bar as CB  # noqa: E402
from app.ui import scrubber as SB  # noqa: E402

app = QApplication.instance() or QApplication([])


class FakeIpc:
    def __init__(self):
        self.sent = []

    def fire(self, command):
        self.sent.append(list(command))


def make_player():
    cfg = cfgmod.Config(os.path.join(DATA, "config.json"))
    p = PL.MpvPlayer(cfg)
    p._ipc = FakeIpc()
    return p


def s1_seek_modes():
    p = make_player()
    p.seek_abs(12.5)
    p.seek_abs(12.5, exact=True)
    p.seek_abs(-3, exact=True)
    got = p._ipc.sent
    want = [["seek", 12.5, "absolute+keyframes"],
            ["seek", 12.5, "absolute+exact"],
            ["seek", 0.0, "absolute+exact"]]
    if got != want:
        return False, f"命令形状不对：{got}"
    return True, ""


def s2_open_files_start_pos():
    p = make_player()
    f1 = os.path.join(DATA, "a.mkv")
    with open(f1, "wb"):
        pass
    res = p.open_files([f1], play_index=0, remember=False, start_pos=42.5)
    got = p._ipc.sent[0]
    if got[:3] != ["loadfile", os.path.abspath(f1), "replace"]:
        return False, f"不是 replace：{got}"
    if got[3:] != [-1, "start=42.500"]:
        return False, f"续播位置没带进命令：{got[3:]}"
    if os.path.normcase(os.path.normpath(f1)) != p._resume_skip:
        return False, "_resume_skip 没记下这条路径（file-loaded 会重复 seek）"
    if res.get("plays") is not True:
        return False, f"plays 应为 True：{res}"
    # start_pos=0 时不许塞 start= 参数（mpv 会把它当成显式跳转）
    p2 = make_player()
    p2.open_files([f1], play_index=0, remember=False, start_pos=0.0)
    if p2._ipc.sent[0][3:] != []:
        return False, f"start_pos=0 仍带了参数：{p2._ipc.sent[0]}"
    return True, ""


def s3_controls_passthrough():
    class P:
        def __init__(self):
            self.calls = []

        def seek_abs(self, sec, exact=False):
            self.calls.append((sec, exact))

    bar = CB.ControlsBar.__new__(CB.ControlsBar)
    bar.p = P()
    bar.scrub = types.SimpleNamespace(_dur=100.0)
    bar.lbl_time = QLabel()
    bar._on_seek(12.0, False)
    bar._on_seek(13.0, True)
    if bar.p.calls != [(12.0, False), (13.0, True)]:
        return False, f"exact 没有原样传下去：{bar.p.calls}"
    return True, ""


def _send(widget, kind: str, x: float) -> None:
    """直接调事件处理器：offscreen 下 QTest 的 move 事件不保证送达窗口。"""
    if kind == "press":
        ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(x, 15.0),
                         Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        widget.mousePressEvent(ev)
    elif kind == "move":
        ev = QMouseEvent(QEvent.MouseMove, QPointF(x, 15.0),
                         Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
        widget.mouseMoveEvent(ev)
    else:
        ev = QMouseEvent(QEvent.MouseButtonRelease, QPointF(x, 15.0),
                         Qt.LeftButton, Qt.NoButton, Qt.NoModifier)
        widget.mouseReleaseEvent(ev)


def s4_scrubber_throttle_and_guard():
    clock = [1000.0]
    real_time = SB.time
    SB.time = types.SimpleNamespace(monotonic=lambda: clock[0])
    try:
        w = SB.Scrubber()
        w.resize(300, 30)
        w.set_state(0.0, 100.0)
        events = []
        w.sig_seek.connect(lambda s, e: events.append((s, e)))
        _send(w, "press", 30)
        if len(events) != 1 or events[0][1] is not False:
            return False, f"按下应立刻发一条 keyframes：{events}"
        _send(w, "move", 60)
        if len(events) != 1:
            return False, f"60ms 内的移动没被节流：{len(events)} 条"
        clock[0] += 0.061
        _send(w, "move", 90)
        if len(events) != 2 or events[1][1] is not False:
            return False, f"过了 60ms 应发一条：{events}"
        _send(w, "release", 120)
        if len(events) != 3 or events[2][1] is not True:
            return False, f"松手必须发且必须是精确档：{events}"
        target = events[2][0]
        if abs(target - 40.0) > 0.5:
            return False, f"松手目标秒数不对：{target}"
        # 防回弹：松手后 0.22s 内，远离目标的旧 time-pos（管道里在途的那一条）必须丢弃
        w.set_state(10.0, 100.0)
        if abs(w._pos - target) > 0.01:
            return False, f"滞后旧回写把进度拉回去了：{w._pos}（目标 {target}）"
        w.set_state(target + 0.3, 100.0)
        if abs(w._pos - (target + 0.3)) > 0.01:
            return False, "目标附近的真实位置被误丢"
        clock[0] += 0.23
        w.set_state(10.0, 100.0)
        if abs(w._pos - 10.0) > 0.01:
            return False, "守卫窗口过期后仍拒绝更新"
        return True, ""
    finally:
        SB.time = real_time


def s5_props_lock():
    p = make_player()
    if not hasattr(p, "_props_lock"):
        return False, "没有 _props_lock"
    errors = []

    def writer():
        try:
            for i in range(400):
                p._apply("time-pos", float(i))
                p._apply("volume", 50 + i)
        except Exception as exc:            # noqa: BLE001
            errors.append(repr(exc))

    ths = [threading.Thread(target=writer) for _ in range(3)]
    for t in ths:
        t.start()
    for _ in range(2000):
        p.prop("time-pos", 0.0)
        p.prop("volume", 0)
    for t in ths:
        t.join()
    if errors:
        return False, f"并发读写炸了：{errors[0]}"
    if p.prop("volume", 0) != 50 + 399:
        return False, f"最终值不对：{p.prop('volume', 0)}"
    return True, ""


def s6_pending_subs_path():
    if not hasattr(cfgmod, "same_media_path"):
        return False, "config 里没有 same_media_path"
    same = cfgmod.same_media_path
    if not same(r"D:\片\A.mkv", r"d:/片/a.mkv"):
        return False, "同一路径的不同写法没认成同一个"
    if same(r"D:\片\A.mkv", r"D:\片\B.mkv"):
        return False, "不同文件被认成同一个"
    if same("", ""):
        return False, "空串被当成同一个文件"
    tree = ast.parse(open(os.path.join(ROOT, "app", "ui", "main_window.py"),
                          encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_file_loaded":
            src = ast.dump(node)
            if "same_media_path" not in src:
                return False, "_on_file_loaded 没有校验字幕目标路径"
            return True, ""
    return False, "main_window 里找不到 _on_file_loaded"


def s7_watchdog_order():
    tree = ast.parse(open(os.path.join(ROOT, "app", "engine", "player.py"),
                          encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_start_watchdog":
            calls = []
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    f = sub.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if name in ("seek_abs", "open_files"):
                        calls.append((name, [kw.arg for kw in sub.keywords]))
            if any(n == "seek_abs" for n, _ in calls):
                return False, "看门狗仍在 file-loaded 之前 seek"
            if not any(n == "open_files" and "start_pos" in kws for n, kws in calls):
                return False, "看门狗没把位置交给 open_files(start_pos=…)"
            return True, ""
    return False, "player.py 里找不到 _start_watchdog"


def main() -> int:
    rows = [("S1 seek_abs 精确/关键帧两档", s1_seek_modes),
            ("S2 open_files(start_pos) 带续播位置", s2_open_files_start_pos),
            ("S3 控制条 exact 原样透传", s3_controls_passthrough),
            ("S4 拖动节流 + 松手精确 + 防回弹", s4_scrubber_throttle_and_guard),
            ("S5 _props 锁与并发读写", s5_props_lock),
            ("S6 外挂字幕目标路径核对", s6_pending_subs_path),
            ("S7 看门狗恢复顺序", s7_watchdog_order)]
    red = 0
    for name, fn in rows:
        try:
            ok, detail = fn()
        except Exception as exc:            # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ｜ {detail}" if detail else ""))
        red += 0 if ok else 1
    print(f"[FINAL] seek/竞态：{len(rows) - red}/{len(rows)} 通过；红 {red} 条")
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
