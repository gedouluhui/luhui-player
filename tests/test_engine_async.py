# -*- coding: utf-8 -*-
"""内核启动不再卡住 GUI：排队、轮询、失败告知、关窗作废（`QT_QPA_PLATFORM=offscreen`）。

这道判据守的是用户第一眼看到的那 0.4 秒（最坏 10 秒）：双击一支片子，窗口必须立刻
能重画、能收拖放，而不是整块灰掉等内核握手。产品侧把 `player.start()` 拆成了
`begin_start()`（只 Popen）＋ `poll_start()`（一拍一次试管道），界面用 40ms 定时器驱动；
文件请求在握手完成前**原样排队**，成功后才按顺序交给内核。

为什么单开一条（不塞进 test_engine.py）：那一条要真内核，跑一趟要起 mpv；这里一个
假内核就能把**时序**逐拍摆出来——pending 阶段有没有提前下发、两批的替换/追加语义有没有
被排队冲掉、失败时队列有没有收干净、关窗那一瞬间还在途的请求会不会去碰一扇拆掉的窗。
真内核的启动与看门狗仍归 test_engine.py。

跑法：
    python tests/test_engine_async.py            # 验收：全部 PASS 才算过
    python tests/test_engine_async.py --falsify  # 证伪：注入旧同步实现/坏收口，判据必须翻红
"""
from __future__ import annotations

import ast
import atexit
import inspect
import os
import shutil
import sys
import tempfile
import textwrap
import time

# 用户还在用电脑：前台不许长窗口。必须在导入 Qt / app 之前设。
os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = tempfile.mkdtemp(prefix="engine-async-", dir=HERE)
os.environ["GELU_DATA"] = DATA
atexit.register(lambda: shutil.rmtree(DATA, ignore_errors=True))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, ROOT)

from PyQt5.QtCore import QTimer                             # noqa: E402
from PyQt5.QtWidgets import QApplication                     # noqa: E402

from app import config as cfgmod                            # noqa: E402
from app.engine.player import MpvPlayer                      # noqa: E402
from app.ui import main_window as mw                         # noqa: E402

APP = None
results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []

VID = os.path.abspath(os.path.join(HERE, "samples", "gelu-sample-1.mp4"))
VID2 = os.path.abspath(os.path.join(HERE, "samples", "gelu-sample-2.mp4"))


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


# ---------------------------------------------------------------- 假内核
class FakeEngine:
    """只回答这条链会问的东西：running / expand / begin_start / poll_start / open_files。

    `poll_start` 的每一拍从 `states` 里取一个结果（取完回 "ok"），于是"第几拍才连上"
    由判据自己摆；`open_files` 同时记下**当时 running 是什么**，用来证明下发发生在
    握手成功之后，而不是"队列碰巧空了"。
    """

    def __init__(self):
        self.running = False
        self.begin_result = True
        self.states: list[str] = []
        self.begin_calls = 0
        self.poll_calls = 0
        self.poll_times: list[float] = []
        self.opens: list[tuple[list[str], bool, bool, float, bool]] = []
        self.subs: list[str] = []
        self.quit_calls = 0
        self.expand_calls = 0
        self.last_expand_truncated = False
        self.last_expand_count = 0

    def expand(self, path: str) -> list[str]:
        return MpvPlayer.expand(self, path)

    def begin_start(self, hwnd: int, wait: float = 10.0) -> bool:
        self.begin_calls += 1
        return self.begin_result

    def poll_start(self) -> str:
        self.poll_calls += 1
        self.poll_times.append(time.monotonic())
        state = self.states.pop(0) if self.states else "ok"
        if state == "ok":
            self.running = True
        elif state == "fail":
            self.running = False
        return state

    def open_files(self, files, append: bool = False, remember: bool = True) -> dict:
        files = list(files)
        self.opens.append((files, bool(append), bool(remember), time.monotonic(),
                           bool(self.running)))
        return {"count": len(files), "plays": not bool(append),
                "head": files[0] if files else ""}

    def add_subtitle(self, path: str) -> None:
        self.subs.append(path)

    def prop(self, name: str, default=None):
        return {"path": "", "duration": 0, "pause": True,
                "volume": 100, "mute": False, "speed": 1.0,
                "shuffle": False, "loop-file": "no",
                "loop-playlist": "no"}.get(name, default)

    def quit(self) -> None:
        self.quit_calls += 1


_seq = [0]


def fresh():
    """一扇真窗口（offscreen）＋一个假内核；窗口从没 show 过，不碰桌面。"""
    _seq[0] += 1
    cfg = cfgmod.Config(path=os.path.join(DATA, f"cfg-{_seq[0]}.json"))
    cfg["resume"] = False
    w = mw.MainWindow(cfg, files=[])
    fake = FakeEngine()
    w.player = fake
    w._engine_timer.stop()
    return w, fake


def drop(w, fake) -> None:
    try:
        w._cancel_pending_open()
        w._engine_timer.stop()
        w._open_timer.stop()
        w._hide_timer.stop()
        w.empty._notice_timer.stop()
        try:
            w.osd._hide.stop()
            w.osd.hide()
        except Exception:
            pass
        w.hide()
        fake.quit()
    finally:
        w.deleteLater()
        APP.processEvents()


def pump_until(pred, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(0.004)
    APP.processEvents()
    try:
        return bool(pred())
    except Exception:
        return False


# ---------------------------------------------------------------- 场景（证伪复用）
def run_queue_case(mutate=None) -> dict:
    """单批：启动 → pending 若干拍 → ok。把每一步的时点都带回来。"""
    w, fake = fresh()
    proof = None
    try:
        if mutate is not None:
            proof = mutate(w, fake)
        fake.states = ["pending"] * 4 + ["ok"]
        beats: list[tuple[float, int]] = []
        QTimer.singleShot(90, lambda: beats.append((time.monotonic(), len(fake.opens))))
        t0 = time.monotonic()
        exc = ""
        try:
            w.add_paths([VID])
        except BaseException as e:                 # noqa: BLE001 - 量具自己崩了也要留证据
            exc = f"{type(e).__name__}: {e}"
        add_s = time.monotonic() - t0
        snap = {
            "exc": exc,
            "add_s": add_s,
            "queue": len(w._pending_open),
            "timer": bool(w._engine_timer.isActive()),
            "opens_at_return": list(fake.opens),
            "begin_calls": fake.begin_calls,
            "poll_calls_pre": fake.poll_calls,
        }
        if not exc:
            pump_until(lambda: bool(fake.opens) or not w._engine_timer.isActive())
            # 判据要比较"心跳那一刻 vs 首次下发"。坏实现会在心跳之前就下发并让上一行
            # 立刻返回，所以这里必须再等心跳落地，否则读到的只是"还没到 90ms"。
            pump_until(lambda: bool(beats), 0.6)
        snap.update({
            "beats": list(beats),
            "opens": list(fake.opens),
            "queue_after": len(w._pending_open),
            "timer_after": bool(w._engine_timer.isActive()),
            "poll_calls": fake.poll_calls,
            "poll_times": list(fake.poll_times),
            "notice": w.empty.notice.text(),
            "notice_sticky": bool(getattr(w.empty, "_notice_sticky", False)),
            "empty_hidden": bool(w.empty.isHidden()),
            "osd": str(getattr(w.osd, "_text", "")),
            "mut": proof,
        })
        return snap
    finally:
        drop(w, fake)


def run_two_batch_case(mutate=None) -> dict:
    """连拖两批（替换 + 追加）：两批都必须排队，ok 后按顺序、按各自语义下发。"""
    w, fake = fresh()
    proof = None
    try:
        if mutate is not None:
            proof = mutate(w, fake)
        fake.states = ["pending"] * 3 + ["ok"]
        exc = ""
        try:
            w.add_paths([VID], replace=True)
            w.add_paths([VID2], replace=False)
        except BaseException as e:                 # noqa: BLE001
            exc = f"{type(e).__name__}: {e}"
        snap = {
            "exc": exc,
            "queue": len(w._pending_open),
            "timer": bool(w._engine_timer.isActive()),
            "opens_at_return": list(fake.opens),
        }
        if not exc:
            pump_until(lambda: len(fake.opens) >= 2 or not w._engine_timer.isActive())
        snap.update({
            "opens": list(fake.opens),
            "queue_after": len(w._pending_open),
            "timer_after": bool(w._engine_timer.isActive()),
            "mut": proof,
        })
        return snap
    finally:
        drop(w, fake)


def run_direct_case(mutate=None) -> dict:
    """内核已经在跑：不许再发起启动、不许排队，当场下发。"""
    w, fake = fresh()
    proof = None
    try:
        fake.running = True
        if mutate is not None:
            proof = mutate(w, fake)
        exc = ""
        try:
            w.add_paths([VID], replace=False)
        except BaseException as e:                 # noqa: BLE001
            exc = f"{type(e).__name__}: {e}"
        snap = {
            "exc": exc,
            "opens": list(fake.opens),
            "queue": len(w._pending_open),
            "timer": bool(w._engine_timer.isActive()),
            "begin_calls": fake.begin_calls,
            "mut": proof,
        }
        return snap
    finally:
        drop(w, fake)


def run_fail_case(begin_result: bool = True, mutate=None) -> dict:
    """握手失败（begin_result=True）或进程都拉不起来（False）的收口。"""
    w, fake = fresh()
    proof = None
    try:
        fake.begin_result = begin_result
        ws_before = len(w._pending_open)
        if mutate is not None:
            proof = mutate(w, fake)
        fake.states = ["pending", "fail"] if begin_result else []
        exc = ""
        try:
            w.add_paths([VID])
        except BaseException as e:                 # noqa: BLE001
            exc = f"{type(e).__name__}: {e}"
        if begin_result and not exc:
            pump_until(lambda: not w._engine_timer.isActive())
        snap = {
            "exc": exc,
            "queue": len(w._pending_open),
            "timer": bool(w._engine_timer.isActive()),
            "opens": list(fake.opens),
            "begin_calls": fake.begin_calls,
            "notice": w.empty.notice.text(),
            "notice_sticky": bool(getattr(w.empty, "_notice_sticky", False)),
            "empty_hidden": bool(w.empty.isHidden()),
            "osd": str(getattr(w.osd, "_text", "")),
            "queue_at_start": ws_before,
            "mut": proof,
        }
        return snap
    finally:
        drop(w, fake)


def run_cancel_case(mutate=None) -> dict:
    """握手还在途时关窗/取消：定时器停、队列空，之后绝不能再下发。"""
    w, fake = fresh()
    proof = None
    try:
        if mutate is not None:
            proof = mutate(w, fake)
        fake.states = ["pending"] * 20
        exc = ""
        try:
            w.add_paths([VID])
            w._cancel_pending_open()
            pump_until(lambda: False, 0.25)        # 空跑 0.25s：若定时器没停，这里会下发
        except BaseException as e:                 # noqa: BLE001
            exc = f"{type(e).__name__}: {e}"
        return {
            "exc": exc,
            "queue": len(w._pending_open),
            "timer": bool(w._engine_timer.isActive()),
            "opens": list(fake.opens),
            "mut": proof,
        }
    finally:
        drop(w, fake)


# ---------------------------------------------------------------- 判据
def judge_queue(s: dict) -> list[str]:
    bad: list[str] = []
    if s["exc"]:
        bad.append(f"add_paths 抛了：{s['exc']}")
    if s["opens_at_return"]:
        bad.append(f"还没握手就把文件下发了：{s['opens_at_return']}")
    if s["queue"] != 1:
        bad.append(f"排队数={s['queue']}（要 1）")
    if not s["timer"]:
        bad.append("40ms 握手定时器没跑起来")
    if s["begin_calls"] != 1:
        bad.append(f"发起启动次数={s['begin_calls']}（要 1）")
    if s["add_s"] > 0.2:
        bad.append(f"add_paths 堵了 {s['add_s'] * 1000:.0f}ms（同步等握手的老毛病）")
    return bad


def judge_loop_alive(s: dict) -> list[str]:
    bad: list[str] = []
    if not s["beats"]:
        bad.append("90ms 心跳没跑：事件循环被堵住了")
    else:
        when, opened_then = s["beats"][0]
        if opened_then:
            bad.append(f"心跳那一刻已经下发了 {opened_then} 批")
        if s["opens"] and when >= s["opens"][0][3]:
            bad.append("心跳晚于首次下发：等待不是在事件循环里做的")
    if s["poll_calls"] < 3:
        bad.append(f"只轮询了 {s['poll_calls']} 次（握手没有按拍推进）")
    if len(s["poll_times"]) >= 2:
        span = s["poll_times"][-1] - s["poll_times"][0]
        if span < 0.08:
            bad.append(f"轮询首末只差 {span * 1000:.0f}ms：像忙等，不像定时器")
    return bad


def judge_delivered(s: dict) -> list[str]:
    bad: list[str] = []
    got = [(o[0], o[1], o[2]) for o in s["opens"]]
    want = [([VID], False, True)]
    if got != want:
        bad.append(f"下发内容={got}（要 {want}）")
    if s["queue_after"]:
        bad.append(f"下发后队列还剩 {s['queue_after']} 项")
    if s["timer_after"]:
        bad.append("ok 之后握手定时器还在跑")
    if s["opens"] and not all(o[4] for o in s["opens"]):
        bad.append("有文件是在 running=False 时下发的")
    return bad


def judge_two_queued(s: dict) -> list[str]:
    bad: list[str] = []
    if s["exc"]:
        bad.append(f"连拖两批抛了：{s['exc']}")
    if s["opens_at_return"]:
        bad.append("握手前就下发了")
    if s["queue"] != 2:
        bad.append(f"排队数={s['queue']}（要 2：第二批不能把第一批挤掉）")
    if not s["timer"]:
        bad.append("握手定时器没跑")
    return bad


def judge_two_order(s: dict) -> list[str]:
    bad: list[str] = []
    got = [(o[0], o[1], o[2]) for o in s["opens"]]
    want = [([VID], False, True), ([VID2], True, True)]
    if got != want:
        bad.append(f"下发顺序/语义={got}（要 {want}）")
    # 不拿时间戳判先后：两批是在**同一拍**里接着下发的，monotonic 的分辨率下完全可以相等
    # （实测就撞到过）。顺序的证据是 opens 列表本身的次序，不是给它们硬排一个时间。
    if s["opens"] and not all(o[4] for o in s["opens"]):
        bad.append("有文件是在 running=False 时下发的")
    if s["queue_after"]:
        bad.append(f"下发后队列还剩 {s['queue_after']} 项")
    if s["timer_after"]:
        bad.append("ok 之后握手定时器还在跑")
    return bad


def judge_direct(s: dict) -> list[str]:
    bad: list[str] = []
    got = [(o[0], o[1], o[2]) for o in s["opens"]]
    want = [([VID], True, True)]
    if s["exc"]:
        bad.append(f"add_paths 抛了：{s['exc']}")
    if got != want:
        bad.append(f"直接下发={got}（要 {want}）")
    if s["queue"]:
        bad.append(f"内核已在跑却还排队 {s['queue']} 项")
    if s["timer"]:
        bad.append("内核已在跑还启动了握手定时器")
    if s["begin_calls"]:
        bad.append(f"内核已在跑却又发起 {s['begin_calls']} 次启动")
    return bad


def judge_fail_delivery(s: dict) -> list[str]:
    bad: list[str] = []
    if s["exc"]:
        bad.append(f"失败路径抛了：{s['exc']}")
    if s["opens"]:
        bad.append(f"握手失败仍然下发了：{s['opens']}")
    if s["queue"]:
        bad.append(f"失败后队列还剩 {s['queue']} 项")
    if s["timer"]:
        bad.append("失败后握手定时器还在跑")
    return bad


def judge_fail_visible(s: dict) -> list[str]:
    bad: list[str] = []
    if not s["notice"].strip():
        bad.append("占位页没有留下原因（用户只看到黑屏）")
    elif not s["notice_sticky"]:
        bad.append("原因不是 sticky：用户还没读完就自己消失了")
    if "播放内核" not in s["notice"]:
        bad.append(f"原因没点名内核：{s['notice']!r}")
    if s["empty_hidden"]:
        bad.append("占位页被藏起来了，原因写在一块看不见的布上")
    if "启动失败" not in s["osd"]:
        bad.append(f"OSD 没说失败：{s['osd']!r}")
    return bad


def judge_begin_fail(s: dict) -> list[str]:
    bad = judge_fail_delivery(s)
    if s["begin_calls"] != 1:
        bad.append(f"发起启动次数={s['begin_calls']}（要 1）")
    bad += judge_fail_visible(s)
    return bad


def judge_cancel_live(s: dict) -> list[str]:
    bad: list[str] = []
    if s["exc"]:
        bad.append(f"取消路径抛了：{s['exc']}")
    if s["timer"]:
        bad.append("取消后握手定时器还在跑")
    if s["queue"]:
        bad.append(f"取消后队列还剩 {s['queue']} 项")
    if s["opens"]:
        bad.append(f"取消之后仍然下发了：{s['opens']}")
    return bad


def judge_close_source(src: str) -> list[str]:
    """closeEvent 必须调 `_cancel_pending_open`，而且排在 `player.quit()` 之前。

    为什么查源码而不是只查函数行为：真正危险的是**别人以后把那一行删掉**；函数本身
    永远能正确清队列，删掉接线却仍全绿。AST 读的是产品文件里那一段，不手抄方法名。
    """
    bad: list[str] = []
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError as exc:
        return [f"closeEvent 源码解析失败：{exc}"]
    fn = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
    if fn is None:
        return ["源码里找不出 closeEvent 函数体"]
    calls: dict[str, list[int]] = {"_cancel_pending_open": [], "quit": []}
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name in calls:
                calls[name].append(node.lineno)
    if not calls["_cancel_pending_open"]:
        bad.append("closeEvent 没有调用 _cancel_pending_open：关窗时排队请求会去碰已拆的窗")
    if not calls["quit"]:
        bad.append("closeEvent 里找不到 player.quit()（判据前提不成立）")
    if calls["_cancel_pending_open"] and calls["quit"]:
        if min(calls["_cancel_pending_open"]) > min(calls["quit"]):
            bad.append("_cancel_pending_open 排在 quit 之后：内核先没了，排队路径还在")
    return bad


# ---------------------------------------------------------------- 证伪：注入坏实现
def mut_old_sync(w, fake):
    """旧写法：在 add_paths 里同步等到 ok，再当场下发（不排队）。"""
    base = w._begin_engine

    def mutant():
        ok = base()
        if not ok:
            return False
        while w.player.poll_start() == "pending":
            time.sleep(0.01)
        w._engine_timer.stop()
        queue, w._pending_open = w._pending_open, []
        for files, subs, replace in queue:
            w._open_files_now(files, subs, replace)
        return True

    w._begin_engine = mutant
    return {"applied": w._begin_engine is not base}


def mut_open_while_pending(w, fake):
    """握手还在 pending 就把队列交出去（"连上"和"还在等"外观一样，最像的坏法）。"""
    base = w._poll_engine_start

    def mutant():
        w.player.poll_start()
        w._engine_timer.stop()
        queue, w._pending_open = w._pending_open, []
        for files, subs, replace in queue:
            w._open_files_now(files, subs, replace)

    w._poll_engine_start = mutant
    # 定时器在 __init__ 里连的是**当时那个绑定方法**；只改实例属性，Qt 还叫旧的那一个。
    w._engine_timer.timeout.disconnect()
    w._engine_timer.timeout.connect(mutant)
    return {"applied": w._poll_engine_start is not base}


def mut_drop_replace(w, fake):
    """排队时把"替换还是追加"丢了：第一批替换，第二批本该追加。"""
    base = w._open_files_now

    def mutant(files, subs, replace):
        return base(files, subs, True)

    w._open_files_now = mutant
    return {"applied": w._open_files_now is not base}


def mut_fail_is_ok(w, fake):
    """把 fail 当成 ok：内核没起来也照样下发。"""
    base = w._poll_engine_start

    def mutant():
        w.player.poll_start()
        w._engine_timer.stop()
        queue, w._pending_open = w._pending_open, []
        for files, subs, replace in queue:
            w._open_files_now(files, subs, replace)

    w._poll_engine_start = mutant
    w._engine_timer.timeout.disconnect()
    w._engine_timer.timeout.connect(mutant)
    return {"applied": w._poll_engine_start is not base}


def mut_silent_fail(w, fake):
    """失败只清队列、不告诉用户（黑屏 + 一句会消失的"正在启动"）。"""
    base = w._engine_failed

    def mutant(why: str) -> None:
        w._pending_open.clear()
        w._engine_timer.stop()
        w.video.set_empty(True)
        w.empty.setVisible(True)

    w._engine_failed = mutant
    return {"applied": w._engine_failed is not base}


def mut_no_cancel(w, fake):
    """关窗不取消排队（等价于 closeEvent 里那一行被删掉）。"""
    base = w._cancel_pending_open

    def mutant() -> None:
        pass

    w._cancel_pending_open = mutant
    return {"applied": w._cancel_pending_open is not base}


def falsify() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])

    def record(label: str, base_bad, mut_bad, applied: bool) -> None:
        """记账是一段纯逻辑，自检直接喂它——不靠再造一个"恒红判据"去过基线。

        判 CAUGHT 的条件是三个一起：干净基线绿（否则判据恒红，抓谁都不算数）、
        变异红、变异真的落到了对象上（NOP 不许混成一等功）。
        """
        if not applied:
            falsified.append((label, False, "变异没换掉方法对象（NOP），这一条不作数"))
            return
        ok = (not base_bad) and bool(mut_bad)
        falsified.append((label, ok,
                           ("基线绿、变异红：" + "；".join(mut_bad[:3])) if ok
                           else f"基线{'红' if base_bad else '绿'}、变异{'红' if mut_bad else '绿'}"
                                + (f"；基线问题={'；'.join(base_bad[:2])}" if base_bad else "")))

    def caught(label: str, judge, run) -> None:
        """先跑干净基线（判据必须绿），再跑变异（判据必须红）。"""
        base = run()
        base_bad = judge(base)
        mutated = run(mutate=fake_mut)
        mut_bad = judge(mutated)
        applied = bool((mutated.get("mut") or {}).get("applied"))
        record(label, base_bad, mut_bad, applied)

    fake_mut = None

    fake_mut = mut_old_sync
    caught("M1 旧同步等待（add_paths 里等完再下发）必须被'立刻返回/心跳'抓住",
           judge_queue, run_queue_case)
    caught("M1b 旧同步等待也必须让事件循环判据翻红", judge_loop_alive, run_queue_case)

    fake_mut = mut_open_while_pending
    caught("M2 pending 就下发必须被抓到（心跳那一刻还不该有文件）",
           judge_loop_alive, run_queue_case)

    fake_mut = mut_drop_replace
    caught("M3 排队丢掉'替换/追加'语义必须被两批顺序判据抓住",
           judge_two_order, run_two_batch_case)

    fake_mut = mut_fail_is_ok
    caught("M4 把 fail 当 ok 下发必须被失败收口判据抓住",
           judge_fail_delivery, run_fail_case)

    fake_mut = mut_silent_fail
    caught("M5 失败不告知（黑屏）必须被可见性判据抓住",
           judge_fail_visible, run_fail_case)

    fake_mut = mut_no_cancel
    caught("M6 取消不清队列必须被'取消后绝不下发'抓住",
           judge_cancel_live, run_cancel_case)

    # M7 关窗那一行的接线：不是跑函数，而是读产品源码。基线/变异都过同一个判据。
    real_src = inspect.getsource(mw.MainWindow.closeEvent)
    mutant_src = real_src.replace("self._cancel_pending_open()",
                                  "pass  # 变异：把取消那一行删掉")
    src_applied = mutant_src != real_src
    base_bad = judge_close_source(real_src)
    mut_bad = judge_close_source(mutant_src)
    falsified.append((
        "M7 closeEvent 漏掉/后置 _cancel_pending_open 必须被源码判据抓住",
        src_applied and not base_bad and bool(mut_bad),
        (f"源码变异未生效（找不到那一行），不作数" if not src_applied else
         "基线绿、变异红：" + "；".join(mut_bad[:2]) if (not base_bad and mut_bad) else
         f"基线={base_bad} 变异={mut_bad}")))

    # 记账函数自己会不会把 MISSED 也写成 CAUGHT？三组已知答案直接喂 record()。
    #   这里不再造"恒红判据"去跑基线：那玩意儿的基线当然红，测的是记账凭什么信。
    n0 = len(falsified)
    record("_自检：基线绿＋变异红 → CAUGHT", [], ["假红"], True)
    self_red = len(falsified) == n0 + 1 and falsified[-1][1] is True
    falsified.pop()
    record("_自检：基线绿＋变异绿 → MISSED", [], [], True)
    self_green = len(falsified) == n0 + 1 and falsified[-1][1] is False
    falsified.pop()
    record("_自检：变异没落地（NOP）→ MISSED", [], ["假红"], False)
    self_nop = len(falsified) == n0 + 1 and falsified[-1][1] is False
    falsified.pop()
    falsified.append(("记账函数会把红记 CAUGHT、绿记 MISSED、NOP 不作数（判据不恒真）",
                      self_red and self_green and self_nop, ""))
    return report()


# ---------------------------------------------------------------- 汇总
def report() -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n---- 汇总：{passed}/{len(results)} 通过"
          + (f"；失败项：{fails}" if fails else "；无失败") + " ----", flush=True)
    if falsified:
        print("==== 证伪（坏样本必须被抓出来）====")
        for name, ok, detail in falsified:
            print(f"[{'CAUGHT' if ok else 'MISSED'}] {name}"
                  + (f"  —— {detail}" if detail else ""), flush=True)
        missed = [n for n, ok, _ in falsified if not ok]
        print(f"---- 证伪收口：抓到 {len(falsified) - len(missed)}/{len(falsified)}"
              f"（漏网 {len(missed)}） ----", flush=True)
        if missed:
            return 1
    return 0 if passed == len(results) else 1


def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    if "--falsify" in sys.argv:
        return falsify()

    q = run_queue_case()
    check("握手期间：请求原样排队、add_paths 立刻返回、只发起一次启动",
          not judge_queue(q), "；".join(judge_queue(q)[:4]))
    check("握手期间事件循环照常转（心跳先于下发，轮询按 40ms 拍走）",
          not judge_loop_alive(q), "；".join(judge_loop_alive(q)[:4]))
    check("握手 ok 后才下发；一批只下一次，队列与定时器收干净",
          not judge_delivered(q), "；".join(judge_delivered(q)[:4]))

    two = run_two_batch_case()
    check("连拖两批都排队（第二批不挤掉第一批、不提前下发）",
          not judge_two_queued(two), "；".join(judge_two_queued(two)[:3]))
    check("两批按顺序下发，替换/追加语义各自保留",
          not judge_two_order(two), "；".join(judge_two_order(two)[:4]))

    direct = run_direct_case()
    check("内核已在跑：直接下发，不再发起启动/不再排队",
          not judge_direct(direct), "；".join(judge_direct(direct)[:4]))

    fail = run_fail_case(True)
    check("握手失败：一批都不下发，队列与定时器收干净",
          not judge_fail_delivery(fail), "；".join(judge_fail_delivery(fail)[:4]))
    check("握手失败把原因留在占位页（sticky，且点名内核）",
          not judge_fail_visible(fail), "；".join(judge_fail_visible(fail)[:4]))

    begin_fail = run_fail_case(False)
    check("进程都拉不起来：同样不残留、同样正面告知",
          not judge_begin_fail(begin_fail), "；".join(judge_begin_fail(begin_fail)[:4]))

    cancel = run_cancel_case()
    check("握手在途时取消：定时器停、队列空，之后绝不下发",
          not judge_cancel_live(cancel), "；".join(judge_cancel_live(cancel)[:4]))

    src_bad = judge_close_source(inspect.getsource(mw.MainWindow.closeEvent))
    check("closeEvent 接了 _cancel_pending_open，且排在 player.quit() 之前",
          not src_bad, "；".join(src_bad[:3]))
    return report()


if __name__ == "__main__":
    sys.exit(main())
