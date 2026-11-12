# -*- coding: utf-8 -*-
"""退出收口自检：#109 那两类"关掉窗口人就没了／整扇窗冻住"的守卫，一次锁死。

用法：
    python tests/test_shutdown_quiet.py            # 正常自检：全部 PASS/ENV 才算过
    python tests/test_shutdown_quiet.py --falsify  # 证伪：喂坏形状，判据必须翻红

十三件事，四族（每一件都对应一条**量出来的**坏形状，不是"看起来该防"）：

A **结构闸门**（AST，不吃时间，把"每家自己记得用 poolkit"从口头约定变成机器不变式）
  A1 池子不许由 store 造：扫到的四家引擎 store + 统计页里，出现 `QThreadPool(...)`
     或 `QThreadPool.globalInstance` 就红——前者是"拆 store ⇒ ~QThreadPool 攥着 GIL 等
     线程收"的实测死锁（`tests/_probe_job_lifetime.py`，六份 45s 栈一模一样），
     后者是绕过登记，退出收口看不见这张池子。
  A2 工作线程里的发射不许裸写 `sig.done.emit(...)`：crash.log 2026-10-19 22:56:34 那一桩
     就是这句打在已被删掉的壳上，而兜底分支里同一句又抛一次 ⇒ 两道墙塌在同一块砖上。
  A3 关窗两条路都得接上收口：`MainWindow.closeEvent` + `app/main.py` 的 `aboutToQuit`。
     只挂前者的话，跳过 closeEvent 的那几条退出路（会话注销、`quit()`）没人收。

B **锁的可重入性**：2026-10-20 04:42 加 `stats()` 时当场把量具冻住 120s——`stats()` 持着
   `Lock` 又调同样要锁的 `pools()` ⇒ 同线程二次 acquire 永久阻塞，而且是**持着锁**阻塞，
   `shutdown()` 的立旗/清排队/杀孩子全跟着排队。窗口关不掉比崩更难查。

C **发射的兜底**（同进程、确定性）：C1 打到已删的壳上必须"吞掉＋计数＋留原文＋不抛"；
   C2 `pin` 是壳在世的**唯一**原因——同一形状不 pin 就必须死（配对，缺一半都算瞎）。

D/E **真退进程**（要 ffmpeg + 4K 样片；缺料记 ENV 并点名，不记产品红、不静默跳）：
   D1 在途收口再退 ⇒ rc=0／不挂／吞 0；D2 确定性阳性对照：`sip.delete` 后**裸**发射每次
      都是 `0xC0000409`；D3 同一场面走 `poolkit.emit` ⇒ rc=0 **且**真的吞到东西；
   D4 作业在跑时拆 store 必须自己走出来（历史上是主线程死锁）；
   E3 收口杀掉的 ffmpeg pid 不许还活着（实测过关窗后它还在往盘上写到 5 MB）。

关于"8/15 → 0/8"那句话，这里必须留一句硬话（**量出来的**，见
`tests/out/probe-exit-crash-postfix-0448.log` 末尾那段判决）：2026-10-20 04:48 复跑五组，
D（收口再退）8/8 干净、收口 0.03–0.05s、killed=1、吞 0；E（同形状摘掉收口）3/3 **也**干净，
只是退出要 6.44/6.64/6.64s；G（把 pin＋emit 整项摘回修前形状）0/6 崩；F（只摘 pin）0/3 崩
⇒ **这一台今天复现不了那个 race，race 当不了判据的阳性对照**，那条分布只能说明它此刻跑赢了
race，不能说明"修好了"。所以：
* 崩不崩这条由 **D2/D3 那对确定性读数**背书——同一场面（工作线程里 `sip.delete(壳)` 之后发射），
  裸写每次 `0xC0000409`、走 `poolkit.emit` 每次 rc=0＋吞到 2 条，只差那一层包裹；
* 收口值不值那条由 **E-vs-D 的退出耗时**背书（6.4–6.6s → 0.81s，且不留还在写盘的 ffmpeg）；
* "池子不归 store 拆"那条规则由 **A1 + F1 的活变异**背书（D4 只是把它在真机上再走一遍）。
"""
from __future__ import annotations

import ast
import ctypes
import gc
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QRunnable, Qt  # noqa: E402  （QThreadPool 故意不导入：A1 不许这层有用它的地方）

from app import config as cfgmod  # noqa: E402
from app.engine import poolkit, thumbs  # noqa: E402

FAILFAST = 0xC0000409          # __failfast：Popen.returncode 里是 -1073740791，折无符号就是这个
SAMPLES = os.path.join(HERE, "samples")
# 在途腿要的是"退出那一刻 ffmpeg 还在抽"，0.4s 就跑完的样片会把判据读成假绿，所以优先 4K
# （`_probe_job_lifetime.py` 实测宽 1920 那一趟约 2s，够动手）。两份都缺 ⇒ 整组记 ENV。
BIG = next((p for p in (os.path.join(SAMPLES, "uhd", "h264-4k.mp4"),
                        os.path.join(SAMPLES, "gelu-sample-long.mp4")) if os.path.exists(p)), "")
ENGINE_STORES = [os.path.join(ROOT, "app", "engine", n) for n in
                 ("thumbs.py", "posters.py", "library.py", "gif.py")]
UI_WORKERS = [os.path.join(ROOT, "app", "ui", "stats_panel.py")]
MAIN_WINDOW = os.path.join(ROOT, "app", "ui", "main_window.py")
MAIN_APP = os.path.join(ROOT, "app", "main.py")

results: list[tuple[str, str, str]] = []          # (名, PASS/FAIL/ENV, 明细)
falsified: list[tuple[str, str, str]] = []        # (名, CAUGHT/MISSED/NOP, 明细)


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, "PASS" if ok else "FAIL", detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def env_skip(name: str, why: str) -> None:
    """环境缺料记 ENV，不记产品红——但**不许不响**：汇总里点名，看门狗日志里看得见。"""
    results.append((name, "ENV", f"ENV 跳过：{why}"))
    print(f"[ENV ] {name}  —— 跳过：{why}", flush=True)


def read_sources(paths: list[str]) -> dict[str, str]:
    out = {}
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            out[p] = fh.read()
    return out


# ---------------------------------------------------------------- 判据（纯函数）
# 每条判据都写成"吃读数、吐真假"的纯函数：--falsify 才能拿已知坏样本喂它，
# 而不必去改产品文件（就地改写产品文件那一路在别的轮里出过投毒事故）。

def j_pool_construction(sources: dict[str, str]) -> list[str]:
    """A1：池子只许 poolkit 造。返回违规点名表（空表＝合规）。"""
    bad: list[str] = []
    for path, src in sources.items():
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            bad.append(f"{os.path.basename(path)}: 解析不了（{exc.msg}）")
            continue
        for node in ast.walk(tree):
            t = ast.unparse(node)
            if isinstance(node, ast.Call) and t.startswith("QThreadPool("):
                arg = ast.unparse(node.args[0]) if node.args else "无参"
                bad.append(f"{os.path.basename(path)}:{node.lineno} new QThreadPool({arg})"
                           f"——该走 poolkit.new_pool")
            elif isinstance(node, ast.Attribute) and t == "QThreadPool.globalInstance":
                bad.append(f"{os.path.basename(path)}:{node.lineno} 直接摸 QThreadPool.globalInstance"
                           f"——该走 poolkit.adopt_global_pool(name)，否则收口看不见这张池子")
    return bad


def j_emit_calls(sources: dict[str, str]) -> list[str]:
    """A2：`run()` 里的发射一律走 poolkit.emit；裸 `xxx.emit(...)` 就是回到崩的那条路。

    只查 `run()`（工作线程那一侧）。store 主线程侧的 `self.sig_done.emit(...)` 是合法的，
    把它一起禁掉会让判据"红得没有道理"，下一轮就有人来放宽它。
    """
    bad: list[str] = []
    for path, src in sources.items():
        tree = ast.parse(src)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for fn in [m for m in cls.body if isinstance(m, ast.FunctionDef) and m.name == "run"]:
                for node in ast.walk(fn):
                    if not isinstance(node, ast.Call):
                        continue
                    callee = ast.unparse(node.func)
                    if not callee.endswith(".emit"):
                        continue
                    if callee.startswith("poolkit.emit"):
                        continue
                    if callee.startswith("self.sig.") or callee.startswith("sig.") or \
                            ".sig.done.emit" in callee or ".sig.prog.emit" in callee:
                        bad.append(f"{os.path.basename(path)}:{node.lineno} "
                                   f"`{cls.name}.run()` 里裸发射 {callee}()——该走 poolkit.emit")
    return bad


def j_shutdown_wired(main_src: str, app_src: str) -> list[str]:
    """A3：closeEvent 与 aboutToQuit 两条路都必须真的调到 poolkit.shutdown。"""
    bad: list[str] = []
    tree = ast.parse(main_src)
    hits = [ast.unparse(c) for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "closeEvent"
            for c in ast.walk(n) if isinstance(c, ast.Call)]
    if not any(h.startswith("poolkit.shutdown(") for h in hits):
        bad.append("MainWindow.closeEvent 里没有 poolkit.shutdown(...)：在途作业会带着整扇窗一起没")
    t2 = ast.parse(app_src)
    wires = [n for n in ast.walk(t2) if isinstance(n, ast.Call)
             and ast.unparse(n.func).endswith(".aboutToQuit.connect")]
    # 挂的是 lambda：把 lambda 的 body 也摊进来才算"这条兜底真的调了收口"
    bodies = [ast.unparse(x) for w in wires for x in ast.walk(w)]
    if not any("poolkit.shutdown" in b for b in bodies):
        bad.append("app/main.py 没把 poolkit.shutdown 挂到 aboutToQuit："
                   "跳过 closeEvent 的那几条退出路没人收")
    return bad


def j_reentrant(spent: float) -> bool:
    """B1：持锁再调一把也要锁的公开函数，必须在 3s 内返回（不可重入的 Lock 会永久阻塞）。"""
    return spent < 3.0


def j_swallow(ret: object, delta: int, texts: list[str], raised: str) -> tuple[bool, str]:
    """C1：发射到已死的壳上——必须返回 False、计数 +1、留下点得出对象名的原文，而且不抛。"""
    named = any("has been deleted" in t and "JobSignals" in t for t in texts)
    ok = ret is False and delta == 1 and named and not raised
    return ok, f"返回={ret!r} 计数+{delta} 点名={named} 抛={raised or '无'}"


def j_pinned_after_drop(alive_pinned: bool, alive_unpinned: bool, delivered: int) -> tuple[bool, str]:
    """C2：pin 必须是壳活着的**唯一**原因——pin 的那只活、不 pin 的那只死，且发射真落地。"""
    ok = alive_pinned and (not alive_unpinned) and delivered == 1
    return ok, (f"pin 着的那只还活={alive_pinned} 不 pin 的那只已死={not alive_unpinned} "
                f"送达={delivered}")


def j_drain_exit(rc: int, hung: bool, swallowed: int, inflight: int,
                 active: int, killed: int) -> tuple[bool, str]:
    """D1：带着活收口再退——前提要真（有活在跑），结论要 rc=0／不挂／吞 0。"""
    if inflight < 1 or active < 1:
        return False, (f"动手那刻 提交={inflight} 在跑={active}：没在『真有活』时收口，"
                       f"这条读数不作数（假绿光的门）")
    if hung:
        return False, "收口后没退出来（挂住）"
    if rc != 0:
        return False, f"rc=0x{rc:08X}"
    if swallowed != 0:
        return False, f"吞掉了 {swallowed} 条回报：收口没排干净，靠兜底硬扛"
    if killed < 1:
        return False, "killed=0：登记册里那个 ffmpeg 没被杀，等待只是干等"
    return True, "前提真／rc=0／不挂／吞 0／杀到孩子"


def j_raw_crash(rc: int) -> bool:
    """D2（确定性阳性对照）：裸发射已删的壳 ⇒ 必须是致命码。它要是回了 0，说明这把尺瞎了。"""
    return rc == FAILFAST


def j_safe_emit_exit(rc: int, swallowed: int) -> bool:
    """D3：同一场面换成 poolkit.emit ⇒ 干净退出**并且**真的吞掉了东西（不是没发生）。"""
    return rc == 0 and swallowed >= 1


def j_del_store(rc: int, hung: bool, spent: float, tore: bool, active: int) -> tuple[bool, str]:
    """D4：作业还在跑时拆 store ⇒ 必须自己走出来（历史上这里是主线程死锁）。

    `tore` 是前提：Qt 的连接可能把 store 钉住，`del` 没真拆成 ⇒ 这一趟的场面没发生，
    不许拿它当"修好了"的读数（记 ENV 并点名，规则由 A1 + F1 变异背书）。
    """
    if active < 1:
        return False, f"动手那刻在跑={active}：作业没在跑就拆，这条读数不作数"
    if not tore:
        return False, "store 没被 del 掉（weakref 还活着）：场面没发生，读数不作数"
    if hung:
        return False, "拆完没退出来（死锁回归）"
    if rc != 0 or spent > 20.0:
        return False, f"rc=0x{rc:08X} 用时 {spent}s"
    return True, "前提真／拆得掉／自己走出来"


def j_kill_child(alive: list[int], tracked: int, alive_at_tear: int) -> tuple[bool, str]:
    """E3：收口之后名册里那个 pid 不许还活着。只查名册里的那一个，绝不按镜像名批处理。"""
    if tracked < 1 or alive_at_tear < 1:
        return False, f"登记={tracked} 动手那刻还活着={alive_at_tear}：没杀到真孩子，读数不作数"
    if alive:
        return False, f"收口后 {alive} 还活着：关窗后它还在往盘上写"
    return True, f"登记 {tracked} 个、动手时 {alive_at_tear} 个在跑，收口后全没了"


def j_idempotent(first: dict, second: dict, stuck_delta: int, flag_up: bool,
                 shutdowns: int) -> tuple[bool, str]:
    """E1：连调两次 shutdown——两次都要有读数，第二次不许新增 stuck，旗子仍立，计数各加一次。"""
    if not first or not second:
        return False, f"读数有空的：第一次={bool(first)} 第二次={bool(second)}"
    if second.get("grace_ms") != first.get("grace_ms"):
        return False, f"两次档位串了：{first.get('grace_ms')} vs {second.get('grace_ms')}"
    if stuck_delta != 0:
        return False, f"第二次多出 {stuck_delta} 条 stuck：重复计数"
    if shutdowns != 2:
        return False, f"shutdowns={shutdowns}（应当各算一次＝2）"
    if not flag_up:
        return False, "两次收口之后旗子反倒躺下了"
    return True, f"waited {first.get('waited_ms')}ms/{second.get('waited_ms')}ms"


def j_closing_rejects(rows: list[tuple[str, bool, str, int]]) -> list[str]:
    """E2：立旗之后四家都不许往池里塞活；会报原因的那两家必须报得出来。

    rows 是 (store 名, 起没起新作业, 它自己报的话, 起的孩子数)。
    thumbs/posters 按设计只回 key（不报原因，源码注释里有账），所以原因判据只压
    library/gif——把四家一刀切会让判据红得没道理，下一轮就有人来放宽它。
    """
    bad: list[str] = []
    if len(rows) < 4:
        bad.append(f"只问了 {len(rows)} 家（四家都得问）")
    for name, started, said, kids in rows:
        if started:
            bad.append(f"{name} 旗子立着还接了活（孩子={kids}）")
        if name in ("library", "gif") and "关闭" not in said:
            bad.append(f"{name} 没把『为什么没起』说出来：{said!r}")
    return bad


# ---------------------------------------------------------------- 子进程真跑

CHILD = r'''
import ctypes, gc, os, sys, time, weakref
HERE, mode, cache, big, dur = sys.argv[1:6]
DUR = float(dur or 0.0)
EXE = sys.argv[6]
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
os.environ["GELU_DATA"] = os.path.join(HERE, ".selftest-data")
import faulthandler
faulthandler.dump_traceback_later(25, repeat=True, exit=False)
from PyQt5.QtCore import QRunnable, QThreadPool
from PyQt5.QtWidgets import QApplication
APP = QApplication.instance() or QApplication(sys.argv[:1])
from app.engine import poolkit, thumbs


def out(tag, txt):
    print("SQ %s %s" % (tag, txt), flush=True)


def alive(pid):
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except ImportError:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        import ctypes as _c
        code = _c.c_ulong()
        ok = k32.GetExitCodeProcess(h, _c.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259


if mode in ("raw-crash", "safe-crash"):
    try:
        import sip
    except ImportError:
        from PyQt5 import sip                    # type: ignore

    class Boom(QRunnable):
        """确定性地复刻"C++ 壳先没了"：工作线程里 sip.delete(壳) 之后发射。

        raw=修前形状（裸 emit）⇒ 异常从 run() 逃进 QThreadPool 工作线程，PyQt 交给
        excepthook 再 abort ⇒ 每次都是 0xC0000409。raw=False 同一场面走 poolkit.emit
        ⇒ 当场被兜住、计数 +1、进程干净退出。两条只差那一层包裹。
        """
        def __init__(self, raw):
            super().__init__()
            self.autoDelete = False
            self.raw = raw
            self.sig = poolkit.JobSignals()

        def run(self):
            sip.delete(self.sig)
            if self.raw:
                self.sig.done.emit("k", True, "x")
            else:
                poolkit.emit(self.sig, "done", "k", True, "x")
                out("STATS", "swallowed=%s" % poolkit.stats().get("swallowed"))

    pool = QThreadPool.globalInstance()
    pool.start(Boom(raw=(mode == "raw-crash")))
    pool.start(Boom(raw=(mode == "raw-crash")))
    pool.waitForDone(15000)
    out("EXIT", "REACHED")

elif mode in ("drain", "kill-child", "del-store"):
    store = thumbs.ThumbStore(EXE, os.path.join(cache, "th"), max_jobs=1)
    key = store.request(big, DUR, 1920)
    t0 = time.monotonic()
    while store._pool.activeThreadCount() == 0 and time.monotonic() - t0 < 6.0:
        time.sleep(0.02)
    act0 = store._pool.activeThreadCount()
    # 再等"孩子真登记上、真在跑"那一刻：工作线程从 submit 到 spawn 之间有几毫秒，
    # 在那几毫秒里采样会读到 tracked=0 ⇒ 判据的"没杀到孩子"到底是产品漏登记还是采样太早，
    # 就分不开了。实测第一版就是这么红的（killed=0 / tracked=0 / 腿只用 2.2s）。
    t1 = time.monotonic()
    tracked = []
    while time.monotonic() - t1 < 4.0:
        tracked = poolkit.child_pids()
        if tracked and any(alive(p) for p in tracked):
            break
        time.sleep(0.02)
    out("PRE", "inflight=%d active=%d tracked=%d alive=%d pinned=%d wait=%.2f"
        % (len(store._active), act0, len(tracked),
           sum(1 for p in tracked if alive(p)), poolkit.pinned_count(),
           time.monotonic() - t0))
    if mode == "del-store":
        sref = weakref.ref(store)
        store.setParent(None)
        del store                                  # 历史上这一步把主线程冻死
        gc.collect()
        APP.processEvents()
        APP.processEvents()
        out("TEAR", "store_gone=%d pinned=%d" % (sref() is None, poolkit.pinned_count()))
        poolkit.shutdown(1500)                     # 产品路径：closeEvent 收了才走
        out("EXIT", "REACHED")
    else:
        t2 = time.monotonic()
        r = poolkit.shutdown(1500)
        out("READ", "secs=%.2f killed=%s swallowed=%s pids=%s active_left=%s pinned_left=%s"
            % (time.monotonic() - t2, r.get("killed"), r.get("swallowed"),
               tracked, r.get("active_left"), r.get("pinned_left")))
        out("EXIT", "REACHED")
else:
    out("BAD", mode)
'''


def spawn(mode: str, secs: float) -> dict:
    """跑一条子进程腿，带回 rc／是否挂住／墙钟／输出／它读到的那几个数。"""
    cache = tempfile.mkdtemp(prefix="gelu-sq-")
    so, se = os.path.join(cache, "o.txt"), os.path.join(cache, "e.txt")
    env = dict(os.environ, GELU_FFMPEG=cfgmod.FFMPEG_EXE,
               PYTHONIOENCODING="utf-8")
    t0 = time.monotonic()
    try:
        with open(so, "wb") as fo, open(se, "wb") as fe:
            p = subprocess.Popen([sys.executable, "-u", "-c", CHILD, HERE, mode, cache, BIG,
                                  str(DUR_BIG), cfgmod.FFMPEG_EXE],
                                 stdout=fo, stderr=fe, cwd=ROOT, env=env,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            hung = False
            try:
                p.wait(secs)
            except subprocess.TimeoutExpired:
                hung = True
                p.kill()
                p.wait(10)
        rc = p.returncode & 0xFFFFFFFF
        with open(so, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
        with open(se, encoding="utf-8", errors="replace") as fh:
            txt += "\n" + fh.read()
        # 子进程的读数原样回显到本脚本的日志里：留档只有"父进程汇总"的话，
        # 下次再红（killed=0 这一类）就得重跑一遍才能知道当时到底量到了什么。
        for ln in txt.splitlines():
            if ln.startswith("SQ "):
                print(f"       [{mode}] {ln[3:]}", flush=True)
        num = lambda pat, d=-1: (int(m.group(1)) if (m := re.search(pat, txt)) else d)
        # 两条工作线程几乎同时打 swallowed=，行会串行（实测 "swallowed=1SQ ... swallowed=2"）
        # ⇒ 取**最大值**。两个来源分开读：READ 里那句是 shutdown 的最终读数（取最后一条），
        # STATS 里那句是工作线程自己打的（取最大），混着读会把"兜了两条"当成"兜了一条"。
        sw_read = [int(x) for x in re.findall(r"READ secs=[\d.]+ killed=\d+ swallowed=(\d+)", txt)]
        sw_stats = [int(x) for x in re.findall(r"STATS swallowed=(\d+)", txt)]
        swallowed = sw_read[-1] if sw_read else (max(sw_stats) if sw_stats else 0)
        return {"rc": rc, "hung": hung, "s": round(time.monotonic() - t0, 2), "txt": txt,
                "swallowed": swallowed,
                "inflight": num(r"inflight=(\d+)"),
                "active": num(r"\bactive=(\d+)"),
                "tracked": num(r"tracked=(\d+)", 0),
                "alive_tear": num(r"alive=(\d+)", 0),
                "killed": num(r"killed=(\d+)", 0),
                "store_gone": num(r"store_gone=(\d+)", -1),
                "pids": _child_pids_from(txt)}
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def _child_pids_from(txt: str) -> list[int]:
    m = re.search(r"pids=\[([^\]]*)\]", txt)
    if not m:
        return []
    return [int(x) for x in re.findall(r"\d+", m.group(1))]


def _pid_alive(pid: int) -> bool:
    """问这个 pid 现在还在不在。**只查名册里那一个**——绝不按镜像名批处理进程。"""
    try:
        import psutil
        p = psutil.Process(int(pid))
        return p.is_running() and p.status() != psutil.ZOMBIE
    except (ImportError, Exception):               # noqa: B014  （进程已没了就走 Win32 那条）
        pass
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259          # STILL_ACTIVE


DUR_BIG = 0.0   # main() 里现量，不手抄


# ---------------------------------------------------------------- 正常自检
def main() -> int:
    t0 = time.perf_counter()
    global DUR_BIG
    srcs = read_sources(ENGINE_STORES + UI_WORKERS)

    bad = j_pool_construction(srcs)
    check("A1 每张线程池都由 poolkit 造（扫到的工作线程里没有 QThreadPool／globalInstance）",
          not bad, "；".join(bad) or f"扫了 {len(srcs)} 个文件")
    bad = j_emit_calls(srcs)
    check("A2 工作线程 run() 里的发射全走 poolkit.emit", not bad,
          "；".join(bad) or "裸发射 0 处")
    pair = read_sources([MAIN_WINDOW, MAIN_APP])
    bad = j_shutdown_wired(pair[MAIN_WINDOW], pair[MAIN_APP])
    check("A3 关窗两条路都接了收口（closeEvent + aboutToQuit）", not bad, "；".join(bad))

    # ---- B 锁必须可重入（04:42 那条 120s 死锁的回归）
    box: dict[str, float] = {}

    def nested() -> None:
        s = time.perf_counter()
        with poolkit._LOCK:                    # 自己持着
            poolkit.stats()                    # 这里面又要 pools() → 拿同一把锁
        box["spent"] = time.perf_counter() - s

    th = threading.Thread(target=nested, daemon=True)
    th.start()
    th.join(6.0)
    spent = box["spent"] if (not th.is_alive() and "spent" in box) else 99.0
    check("B1 poolkit 的锁可重入：持锁调 stats() 在 3s 内返回（不许演 04:42 那次 120s 冻死）",
          j_reentrant(spent), f"用时 {spent:.2f}s，线程还活着={th.is_alive()}")

    # ---- C1 发射的兜底（同进程、确定性）
    poolkit.reset()
    try:
        import sip as _sip
    except ImportError:
        from PyQt5 import sip as _sip                # type: ignore
    before = int(poolkit.stats()["swallowed"])
    sig = poolkit.JobSignals()
    _sip.delete(sig)
    ret: object = None
    raised = ""
    try:
        ret = poolkit.emit(sig, "done", "k", True, "x")
    except BaseException as exc:                     # 兜底自己不许抛——抛了就是回到崩的那条路
        raised = f"{type(exc).__name__}: {exc}"
    st = poolkit.stats()
    ok, why = j_swallow(ret, int(st["swallowed"]) - before, list(st["swallowed_text"]), raised)
    check("C1 发射到已删的壳上：返回 False、计数 +1、留下点名原文，且不抛", ok, why)

    # ---- C2 pin 到底做了什么（配对的"不 pin 就死"）
    class _J(QRunnable):
        def __init__(self):
            super().__init__()
            self.sig = poolkit.JobSignals()

    hits: list[str] = []
    pinned_job = _J()
    pinned_job.sig.done.connect(lambda k, ok2, d: hits.append(k), Qt.DirectConnection)
    w_pin = weakref.ref(pinned_job.sig)
    key_pin = id(pinned_job.sig)
    poolkit.pin(pinned_job)
    del pinned_job
    gc.collect()
    alive_pinned = w_pin() is not None
    holder = poolkit._PINNED.get(key_pin) if alive_pinned else None
    poolkit.emit(holder, "done", "k", True, "x") if holder else None
    unpinned = _J()
    w_free = weakref.ref(unpinned.sig)
    del unpinned
    gc.collect()
    alive_unpinned = w_free() is not None
    ok, why = j_pinned_after_drop(alive_pinned, alive_unpinned, len(hits))
    check("C2 pin 是信号壳活着的唯一原因：pin 着的还在、不 pin 的已死，且发射真送达", ok,
          f"{why}｜在册={poolkit.pinned_count()}")
    poolkit.reset()

    # ---- D 子进程真退（要 ffmpeg + 一份够慢的样片）
    if not os.path.exists(cfgmod.FFMPEG_EXE) or not BIG:
        why0 = f"缺 ffmpeg（{cfgmod.FFMPEG_EXE}）或大样片（BIG={BIG or '无'}）"
        for nm in ("D1 在途收口后再退：rc=0／不挂／吞 0",
                   "D2 裸发射已删的壳必须每次带走进程（确定性阳性对照）",
                   "D3 同一场面走 poolkit.emit 必须干净退出且真的吞掉",
                   "D4 作业还在跑时拆 store 必须自己走出来（死锁回归）",
                   "E3 收口杀掉的名册 pid 不许还活着（不留还在写盘的 ffmpeg）"):
            env_skip(nm, why0)
    else:
        from app.engine.library import probe_duration
        DUR_BIG = probe_duration(cfgmod.FFMPEG_EXE, BIG)
        r = spawn("drain", 60.0)
        ok, why = j_drain_exit(r["rc"], r["hung"], r["swallowed"], r["inflight"],
                               r["active"], r["killed"])
        check("D1 在途收口后再退：前提真／rc=0／不挂／吞 0／杀到孩子", ok,
              f"{why}｜时长 {r['s']}s 样片 {os.path.basename(BIG)} {DUR_BIG:.0f}s")
        r = spawn("raw-crash", 45.0)
        check("D2 裸发射已删的壳必须每次带走进程（D 族的阳性对照，它红了就是尺瞎了）",
              j_raw_crash(r["rc"]),
              f"rc=0x{r['rc']:08X}（要 0x{FAILFAST:08X}）｜尾巴={r['txt'].strip()[-90:]!r}")
        r = spawn("safe-crash", 45.0)
        check("D3 同一场面走 poolkit.emit 必须干净退出且真的吞掉",
              j_safe_emit_exit(r["rc"], r["swallowed"]),
              f"rc=0x{r['rc']:08X} 吞={r['swallowed']}｜用时 {r['s']}s")
        r = spawn("del-store", 45.0)
        if r["store_gone"] == 0:
            env_skip("D4 作业还在跑时拆 store 必须自己走出来（死锁回归）",
                     f"Qt 的连接把 store 钉住了，del 没真拆掉（在跑={r['active']}）"
                     f"⇒ 场面没发生；这条规则由 A1+F1 变异背书")
        else:
            ok, why = j_del_store(r["rc"], r["hung"], r["s"], r["store_gone"] == 1, r["active"])
            check("D4 作业还在跑时拆 store 必须自己走出来（死锁回归）", ok,
                  f"{why}｜rc=0x{r['rc']:08X} 用时 {r['s']}s")
        r = spawn("kill-child", 60.0)
        alive_now = [pid for pid in r["pids"] if _pid_alive(pid)]
        if r["hung"]:
            alive_now = [pid for pid in r["pids"] if _pid_alive(pid)]
        ok, why = j_kill_child(alive_now, r["tracked"], r["alive_tear"])
        check("E3 收口杀掉的名册 pid 不许还活着（不留还在写盘的 ffmpeg）", ok,
              f"{why}｜pid={r['pids']}")

    # ---- E1/E2 收口的行为（同进程）
    poolkit.reset()
    pool = poolkit.new_pool("shutdown-quiet", 1)
    first = poolkit.shutdown(120)
    st_mid = poolkit.stats()
    second = poolkit.shutdown(120)
    st_end = poolkit.stats()
    ok, why = j_idempotent(first, second, int(st_end["stuck"]) - int(st_mid["stuck"]),
                           poolkit.is_closing(), int(st_end["shutdowns"]))
    check("E1 shutdown() 幂等：两次都有读数、第二次不新增 stuck、旗子仍立、计数各一次", ok,
          f"{why}｜在册池子={st_end['pools']}")
    rows = _closing_refusals()
    bad = j_closing_rejects(rows)
    check("E2 立旗之后 thumbs/posters/library/gif 都不再接新活（会报原因的那两家必须报）",
          not bad, "；".join(bad) or
          "；".join(f"{n}={'接了' if s else '拒了'}" + (f"（{d}）" if d else "")
                    for n, s, d, _k in rows))
    poolkit.reset()
    del pool
    return report(t0)


def _closing_refusals() -> list[tuple[str, bool, str, int]]:
    """四家逐个问一遍"旗子立着的时候你还接不接活"——用真实入口，不用假想。

    读数取的是** store 自己的账**（`_active` / 返回的 jid / refresh 的返回值），不是"看起来没起"；
    `poolkit.child_pids()` 再核一遍"确实一个 ffmpeg 都没生"。
    """
    from app.engine import gif, library, posters
    cache = tempfile.mkdtemp(prefix="gelu-sq-close-")
    rows: list[tuple[str, bool, str, int]] = []
    vid = os.path.join(SAMPLES, "gelu-sample-1.mp4")
    try:
        poolkit.reset()
        poolkit.set_closing(True)
        ts = thumbs.ThumbStore(cfgmod.FFMPEG_EXE, os.path.join(cache, "th"), max_jobs=1)
        ts.request(vid, 12.0, 160)
        rows.append(("thumbs", bool(ts._active), "", len(poolkit.child_pids())))

        ps = posters.PosterStore(cfgmod.FFMPEG_EXE, os.path.join(cache, "po"), max_jobs=1)
        ps.request(vid, 12.0)
        rows.append(("posters", bool(ps._active), "", len(poolkit.child_pids())))

        lib = library.Library(cfgmod.FFMPEG_EXE, os.path.join(cache, "lib.json"), roots=[SAMPLES])
        started = lib.refresh()
        rows.append(("library", bool(started), lib.last_status, len(poolkit.child_pids())))

        gs = gif.GifStore(cfgmod.FFMPEG_EXE)
        jid, reason = gs.start(vid, 0.0, 1.0, cache)
        rows.append(("gif", bool(jid), reason, len(poolkit.child_pids())))
    finally:
        poolkit.set_closing(False)
        shutil.rmtree(cache, ignore_errors=True)
    return rows


def report(t_start: float) -> int:
    n = len(results)
    p = sum(1 for _nm, s, _d in results if s == "PASS")
    e = sum(1 for _nm, s, _d in results if s == "ENV")
    fails = [nm for nm, s, _d in results if s == "FAIL"]
    # 汇总行的**形状**是台账对账尺（`_r17_doc_check.py` 的 SUMMARY_RE）认的口径：
    # `---- 汇总：A/B 通过；无失败 ----`。第一版在这里塞了"（ENV 跳过 N 条…）"，
    # 于是文档如实抄的 `13/13 通过` 在那本日志里"找不到"——判据红的是量具的排版，不是产品。
    # ENV 的账单起一行，两条都不丢。
    print(f"\n---- 汇总：{p}/{n} 通过；"
          + (f"失败项：{fails}" if fails else "无失败") + " ----", flush=True)
    print(f"---- 其中 ENV 跳过 {e} 条（三个数都由本脚本现数），"
          f"用时 {time.perf_counter() - t_start:.1f}s ----", flush=True)
    print(f"[FINAL] rc={0 if not fails else 1}  样片={os.path.basename(BIG) or '无'}"
          f" ffmpeg={os.path.exists(cfgmod.FFMPEG_EXE)}", flush=True)
    return 0 if not fails else 1


# ---------------------------------------------------------------- 证伪
def _mut(src: str, old: str, new: str) -> tuple[str, str]:
    """文本变异：锚点不在原文里／换了个等于没换 ⇒ 记 NOP（变异失效），不许记漏网。"""
    if old not in src:
        return src, f"NOP：锚点在原文里找不到（{old[:44]}）"
    out = src.replace(old, new)
    if out == src:
        return src, "NOP：替换后文本没变"
    return out, ""


def falsify() -> int:
    """喂已知坏样本：判据必须翻红；同时喂已知好样本：必须绿（否则"红"是它自己在瞎叫）。"""
    good = read_sources(ENGINE_STORES + UI_WORKERS)
    pair = read_sources([MAIN_WINDOW, MAIN_APP])
    mw_src, ap_src = pair[MAIN_WINDOW], pair[MAIN_APP]

    def add(name: str, caught: bool, note: str = "", detail: str = "") -> None:
        falsified.append((name, "NOP" if note else ("CAUGHT" if caught else "MISSED"),
                          (note + ("｜" if note and detail else "")) + detail))

    # F1 池子换回"挂在 store 名下" ⇒ A1 必须点名
    k1 = next(k for k in good if k.endswith("thumbs.py"))
    m1, n1 = _mut(good[k1], 'poolkit.new_pool("thumbs", max_jobs)', "QThreadPool(self)")
    hit1 = j_pool_construction({**good, k1: m1})
    add("F1 池子换回 QThreadPool(self) 必须被 A1 点名",
        bool(n1) is False and any("thumbs.py" in h for h in hit1), n1, f"{hit1[:1]}")
    add("F1 正对照：没注坏时 A1 判没问题", not j_pool_construction(good), "", "")

    # F2 绕开登记直接摸 globalInstance ⇒ A1 必须点名
    k2 = next(k for k in good if k.endswith("gif.py"))
    m2, n2 = _mut(good[k2], "self._pool = poolkit.adopt_global_pool(\"gif\")",
                  "self._pool = QThreadPool.globalInstance()")
    hit2 = j_pool_construction({**good, k2: m2})
    add("F2 直接摸 QThreadPool.globalInstance 必须被 A1 点名",
        bool(n2) is False and any("globalInstance" in h for h in hit2), n2, f"{hit2[:1]}")

    # F3 一处发射改回裸 emit ⇒ A2 必须点名
    k3 = next(k for k in good if k.endswith("posters.py"))
    m3, n3 = _mut(good[k3], 'poolkit.emit(sig, "done", self.key, ok,', 'sig.done.emit(self.key, ok,')
    hit3 = j_emit_calls({**good, k3: m3})
    add("F3 裸发射注回 PosterJob.run 必须被 A2 点名",
        bool(n3) is False and any("posters.py" in h for h in hit3), n3, f"{hit3[:1]}")
    add("F3 正对照：没注坏时 A2 判没问题", not j_emit_calls(good), "", "")

    # F4 摘掉 closeEvent 的收口 ⇒ A3 必须点名
    m4, n4 = _mut(mw_src, "readings = poolkit.shutdown(SHUTDOWN_GRACE_MS)", "readings = {}")
    hit4 = j_shutdown_wired(m4, ap_src)
    add("F4 closeEvent 摘掉 poolkit.shutdown 必须被 A3 点名",
        bool(n4) is False and bool(hit4), n4, f"{hit4[:1]}")
    # F5 摘掉 aboutToQuit 那条兜底（closeEvent 那条还在）⇒ A3 必须仍点名
    m5, n5 = _mut(ap_src, "app.aboutToQuit.connect(lambda: poolkit.shutdown(SHUTDOWN_GRACE_MS))",
                  "_unused = lambda: None")
    hit5 = j_shutdown_wired(mw_src, m5)
    add("F5 aboutToQuit 那条兜底摘掉必须被 A3 点名（另一条还挂着，不许因此放行）",
        bool(n5) is False and any("aboutToQuit" in h for h in hit5), n5, f"{hit5[:1]}")
    add("F5 正对照：两条都挂着时 A3 判没问题", not j_shutdown_wired(mw_src, ap_src), "", "")

    # F6 锁换成不可重入的 threading.Lock ⇒ B1 必须翻红（判据不许恒真）
    orig = poolkit._LOCK
    poolkit._LOCK = threading.Lock()
    box: dict[str, float] = {}

    def nested() -> None:
        s = time.perf_counter()
        with poolkit._LOCK:
            poolkit.stats()
        box["spent"] = time.perf_counter() - s

    th = threading.Thread(target=nested, daemon=True)
    th.start()
    th.join(4.0)
    spent = 99.0 if th.is_alive() else box.get("spent", 99.0)
    poolkit._LOCK = orig                        # 立刻交回，别把这只瞎掉的锁留给后面的腿
    add("F6 锁换成 threading.Lock 时 B1 必须翻红", j_reentrant(spent) is False, "",
        f"用时={spent:.2f}s 线程还活着={th.is_alive()}")
    add("F6 正对照：可重入锁下的实测用时 B1 判通过", j_reentrant(0.001) is True)
    add("F6 正对照：摘回原锁后 B1 复跑必须绿（证明刚才那条红是变异造成的，不是判据坏了）",
        _live_reentrant_check() < 3.0)

    # F7 摘掉 emit 的兜底 ⇒ C1 的那条坏形状必须判红（而且它真的会抛）
    def raw_emit(s, name, *a):
        getattr(s, name).emit(*a)
        return True

    poolkit.reset()
    try:
        import sip as _sip
    except ImportError:
        from PyQt5 import sip as _sip                # type: ignore
    s7 = poolkit.JobSignals()
    _sip.delete(s7)
    raised = ""
    ret7 = None
    try:
        ret7 = raw_emit(s7, "done", "k", True, "x")
    except BaseException as exc:
        raised = type(exc).__name__
    ok7, why7 = j_swallow(ret7 if not raised else None, 0, [], raised)
    add("F7 摘掉兜底后那条坏形状必须让 C1 判红",
        bool(raised) and ok7 is False, "", f"抛={raised or '没抛'}｜{why7}")
    # 正对照必须喂**同一形状**：同样打在已删的壳上，只是走真的 poolkit.emit ⇒ C1 要判绿。
    # （喂"发射成功了、一条没吞"那种读数进这条判据是我第一版的错：那条判据管的是
    #  "吞得干不干净"，不是"发没发出去"，拿好形状喂它会得到一个假漏网。）
    poolkit.reset()
    s7b = poolkit.JobSignals()
    _sip.delete(s7b)
    b_before = int(poolkit.stats()["swallowed"])
    ret7b = poolkit.emit(s7b, "done", "k", True, "x")
    st7b = poolkit.stats()
    ok7b, why7b = j_swallow(ret7b, int(st7b["swallowed"]) - b_before,
                            list(st7b["swallowed_text"]), "")
    add("F7 正对照：同一形状走真 poolkit.emit ⇒ C1 判绿", ok7b is True, "", why7b)

    # F8 C2 的配对：只喂"pin 与不 pin 都活着"这种读数必须判红
    add("F8 C2 对『不 pin 也活着』（pin 其实没起作用）必须翻红",
        j_pinned_after_drop(True, True, 1)[0] is False, "", "")
    add("F8 C2 对『pin 着的反倒死了』必须翻红",
        j_pinned_after_drop(False, False, 1)[0] is False, "", "")
    add("F8 C2 对『一条都没送达』必须翻红",
        j_pinned_after_drop(True, False, 0)[0] is False, "", "")
    add("F8 正对照：pin 活／不 pin 死／送达 1 判绿", j_pinned_after_drop(True, False, 1)[0] is True)

    # F9 读数层的坏样本：每条判据都要有"会拒绝的读数"，否则它是恒真桩
    add("F9 D1 对『退出时其实没活』必须翻红（假绿光的门）",
        j_drain_exit(0, False, 0, 0, 0, 1)[0] is False, "", "")
    add("F9 D1 对『没杀到孩子、只是干等』必须翻红",
        j_drain_exit(0, False, 0, 1, 1, 0)[0] is False, "", "")
    add("F9 D1 对『靠吞兜底硬扛』必须翻红",
        j_drain_exit(0, False, 3, 1, 1, 1)[0] is False, "", "")
    add("F9 D1 对『rc 是致命码』必须翻红",
        j_drain_exit(FAILFAST, False, 0, 1, 1, 1)[0] is False, "", "")
    add("F9 D1 的正对照：前提真／rc=0／不挂／吞 0／杀到 ⇒ 判绿",
        j_drain_exit(0, False, 0, 1, 1, 1)[0] is True)
    add("F9 D2 对『rc=0』必须翻红（对照失效要响，不能静默）", j_raw_crash(0) is False)
    add("F9 D2 的正对照：0xC0000409 判绿", j_raw_crash(FAILFAST) is True)
    add("F9 D3 对『rc=0 但一条都没吞到』必须翻红", j_safe_emit_exit(0, 0) is False)
    add("F9 D4 对『挂住没退』必须翻红", j_del_store(0, True, 30.0, True, 1)[0] is False)
    add("F9 D4 对『没拆掉（场面没发生）』必须翻红，不许当绿",
        j_del_store(0, False, 1.0, False, 1)[0] is False, "", "")
    add("F9 D4 的正对照：拆掉了且自己走出来 ⇒ 判绿", j_del_store(0, False, 1.5, True, 1)[0] is True)
    add("F9 E3 对『pid 还活着』必须翻红", j_kill_child([4242], 1, 1)[0] is False)
    add("F9 E3 对『一个都没登记过』不许判绿（空名册不算杀干净）",
        j_kill_child([], 0, 0)[0] is False, "", "")
    add("F9 E3 的正对照：登记 1 个、动手时活着、收口后没了 ⇒ 判绿",
        j_kill_child([], 1, 1)[0] is True)
    add("F9 E1 对『第二次多一条 stuck』必须翻红",
        j_idempotent({"grace_ms": 1}, {"grace_ms": 1}, 1, True, 2)[0] is False)
    add("F9 E1 对『只调了一次』必须翻红（读数是空的）",
        j_idempotent({"grace_ms": 1}, {}, 0, True, 2)[0] is False)
    add("F9 E1 对『shutdowns 记成 3（重复计数）』必须翻红",
        j_idempotent({"grace_ms": 1}, {"grace_ms": 1}, 0, True, 3)[0] is False)
    add("F9 E1 的正对照：两次各一次、旗子仍立 ⇒ 判绿",
        j_idempotent({"grace_ms": 1}, {"grace_ms": 1}, 0, True, 2)[0] is True)
    add("F9 E2 对『只问了三家』必须翻红",
        bool(j_closing_rejects([("thumbs", False, "", 0)] * 3)), "", "")
    add("F9 E2 对『旗子立着还接了活』必须翻红",
        bool(j_closing_rejects([("thumbs", True, "", 1), ("posters", False, "", 0),
                                ("library", False, "窗口在关闭", 0), ("gif", False, "关闭", 0)]))
        , "", "")
    add("F9 E2 对『library 没报原因』必须翻红",
        bool(j_closing_rejects([("thumbs", False, "", 0), ("posters", False, "", 0),
                                ("library", False, "", 0), ("gif", False, "关闭", 0)])), "", "")
    add("F9 E2 的正对照：四家都拒、两家报了原因 ⇒ 判没问题",
        not j_closing_rejects([("thumbs", False, "", 0), ("posters", False, "", 0),
                               ("library", False, "窗口在关闭，扫描没起", 0),
                               ("gif", False, "窗口正在关闭，导出不启动", 0)]))
    poolkit.reset()
    return report_falsify(t_start=time.perf_counter())


def _live_reentrant_check() -> float:
    """用当前那把锁真跑一遍嵌套，返回用时（F6 的"摘回原锁必须复绿"对照）。"""
    box: dict[str, float] = {}

    def go() -> None:
        s = time.perf_counter()
        with poolkit._LOCK:
            poolkit.stats()
        box["spent"] = time.perf_counter() - s

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(4.0)
    return 99.0 if th.is_alive() else box.get("spent", 99.0)


def report_falsify(t_start: float) -> int:
    print("==== 证伪（坏形状必须被否掉；正对照必须不被否掉）====", flush=True)
    for name, state, detail in falsified:
        print(f"[{state}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    c = sum(1 for _n, s, _d in falsified if s == "CAUGHT")
    m = [n for n, s, _d in falsified if s == "MISSED"]
    np = [n for n, s, _d in falsified if s == "NOP"]
    print(f"---- 证伪收口：{c}/{len(falsified)} 抓到；漏网 {len(m)}；变异失效(NOP) {len(np)}"
          f"（三个数由本脚本现数） ----", flush=True)
    if m:
        print("!! 以下坏样本没被否掉，这份自检不可信：" + ", ".join(m), flush=True)
    if np:
        print("!! 以下变异是空操作，它没测到任何东西（判据不许为此放宽）：" + ", ".join(np),
              flush=True)
    print(f"[FINAL] rc={0 if not (m or np) else 1}  用时 {time.perf_counter() - t_start:.1f}s",
          flush=True)
    return 0 if not (m or np) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(falsify() if "--falsify" in sys.argv else main())
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        traceback.print_exc()
        print(f"[FINAL] 自检自己崩了：{type(exc).__name__}: {exc} ⇒ rc=1", flush=True)
        raise SystemExit(1)
