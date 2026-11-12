# -*- coding: utf-8 -*-
"""一次性诊断（**不是判据**）：线程池还在跑的时候把主人拆掉／把进程退出，
`self.sig.done.emit(...)` 那一行会不会打在一只已经被删掉的 C++ 对象上，以及会不会**挂住**。

盘上已有的真证据（不是猜出来的形状）：`data/logs/crash.log` 里 2026-10-19 22:56:34 那一栏，
两条套在一起的 traceback——`app/engine/thumbs.py:114` 的 emit 抛
`RuntimeError: wrapped C/C++ object of type _JobSignals has been deleted`，紧接着 `:117`
那句**用来兜住上一条**的 except 分支自己又抛同一句。第二抛没人接，就从 `run()` 逃进
QThreadPool 的工作线程（PyQt 把它交给 excepthook 再 abort）＝"宿主静默没了"的读法。

三档各量一件事，本趟一概不修：
  A 基线：正常跑完、事件循环照走 ⇒ 摸壳（Python 侧＋C++ 侧各摸一次）／sig 活着没／回执到没到。
    这一档"没坏"是**必须**存在的对照组：B/C 若撞到，得能说清"跟正常跑完比，差别在哪一步"。
  C 退进程：起子进程，用**真的** ThumbStore 排两趟（max_jobs=1），在第 0.4s 直接 `sys.exit`
    （不等池子）。读两样：子进程 stderr 里有没有那句 `has been deleted`；子进程**到没到得了退出**
    （到点没退＝退出挂住，那是另一种坏法，也得点名）。
  B 拆主人：ffmpeg 还在跑的时候把 store 从 Python 侧拆掉（`setParent(None)`＋`del`＋`gc`），
    看 emit 那一下打在哪。这一档在 2026-10-20 第一次跑时把探测自己挂住了（父进程 8 分钟没动、
    日志 0 字节），所以这一版给它单独设了看门狗时间：先做完 C 再做 B，且 B 全程有
    `faulthandler.dump_traceback_later` 在每个卡点留栈。

前提闸门（自己不成立就不许报判决，免得把量具失灵当成产品坏）：
  P1 ffmpeg、样片（含那份 4K）都在盘上；
  P2 工作线程**真的跑过**（sheet 落盘）——不然 C"没崩"只是"没干活"；
  P3 B/C 动手那一刻 job **确实还在跑**（读 `_pool.activeThreadCount()`），拆早拆晚都不算撞车。

跑法：python -u tests/_probe_job_lifetime.py
日志：tests/out/probe-job-lifetime-<时分秒>.log（每行即时 flush）＋同名的 -stacks.txt（卡点栈）
退出码：0 三档都拿到读数；3 前提不成立；1 探测自己崩／某档到自己设的时限还没收。
"""
from __future__ import annotations

import faulthandler
import gc
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import weakref

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

_STAMP = time.strftime("%H%M%S")
_LOG = os.path.join(HERE, "out", f"probe-job-lifetime-{_STAMP}.log")
_STACKS = os.path.join(HERE, "out", f"probe-job-lifetime-{_STAMP}-stacks.txt")
os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
_stack_fd = open(_STACKS, "w", encoding="utf-8", errors="replace")
faulthandler.enable(file=_stack_fd, all_threads=True)
# 每 45 秒把**所有线程**的栈印一次：卡住的那一趟不用猜在哪一行，盘上就有。
# 参数名要实测：本机 3.12 的 `dump_traceback_later` 签名（取自它自己的 __doc__）是
# `(timeout, repeat=False, file=sys.stderr, exit=False)`，**没有 period**——写成 period 的那一版
# 在 import 阶段就 TypeError，探测自己先崩，读数 0 字节。周期就是 repeat=True + timeout。
faulthandler.dump_traceback_later(45, repeat=True, file=_stack_fd)


def say(*a) -> None:
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def show(title: str, o: dict) -> None:
    say(f"── {title} ──")
    for k, v in o.items():
        print(f"    {k:26} = {v}", flush=True)


from PyQt5.QtWidgets import QApplication          # noqa: E402
from app import config as cfgmod                   # noqa: E402
from app.engine.thumbs import ThumbStore           # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
SLOW = os.path.join(HERE, "samples", "uhd", "h264-4k.mp4")       # 实测 ~2.0s/趟（宽 1920）
SLOW2 = os.path.join(HERE, "samples", "gelu-sample-long.mp4")    # 实测 ~0.7s/趟（宽 1920）
DUR = 10.0
DELETED = re.compile(r"wrapped C/C++ object of type (\w+) has been deleted")

# 子进程那一趟：跑真的 ThumbStore，排两活，第 0.4s 直接退出（不等池子）
CHILD = r'''
import os, sys, time
HERE = sys.argv[1]
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
from PyQt5.QtWidgets import QApplication
from app import config as cfgmod
from app.engine.thumbs import ThumbStore
app = QApplication.instance() or QApplication(sys.argv[:1])
store = ThumbStore(cfgmod.FFMPEG_EXE, sys.argv[2], max_jobs=1)
jobs = [(os.path.join(HERE, "samples", "uhd", "h264-4k.mp4"), 60.0, 1920),
        (os.path.join(HERE, "samples", "gelu-sample-long.mp4"), 32.0, 1920)]
keys = [store.request(p, d, w) for p, d, w in jobs if os.path.exists(p)]
print("CHILD 排了 %d 趟 active=%d 在册=%d" % (len(keys), store._pool.activeThreadCount(),
                                              len(store._active)), flush=True)
time.sleep(float(sys.argv[3]))
print("CHILD 退出前 active=%d max=%d 在册=%d" % (store._pool.activeThreadCount(),
                                                 store._pool.maxThreadCount(),
                                                 len(store._active)), flush=True)
sys.exit(0)                     # 不等池子：与"关掉窗口后进程自己走完退出"同一形状
'''


def pump(secs: float, app) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def touch(jobs):
    """`.running` 是纯 Python 属性（摸不到 C++），所以另摸一次 `autoDelete`（QRunnable 的真属性）：
    只摸前者会把"壳没了"读成"活"——这就是本仓记忆里那类"文本全绿但消费它的程序不认"。"""
    rows = []
    for k, j in list(jobs.items()):
        try:
            _ = j.running
            py = "活"
        except RuntimeError as exc:
            py = f"抛:{exc}"
        try:
            _ = j.autoDelete
            cpp = "活"
        except RuntimeError as exc:
            cpp = f"抛:{exc}"
        rows.append((k[:6], f"py={py} cpp={cpp}"))
    return rows


def scenario_baseline(app) -> dict:
    out = {}
    cache = tempfile.mkdtemp(prefix="gelu-jl-")
    try:
        store = ThumbStore(cfgmod.FFMPEG_EXE, cache, max_jobs=1)
        got = []
        store.sig_done.connect(lambda k, ok, d: got.append((k[:6], ok, d[:34])))
        key = store.request(SAMPLE, DUR)
        job = store._active.get(key)
        if job is None:
            return {"premise": "A：request 之后 _active 里没有 job"}
        sref = weakref.ref(job.sig)
        del job
        gc.collect()
        t0 = time.monotonic()
        store._pool.waitForDone(60000)
        out["worked_s"] = round(time.monotonic() - t0, 2)
        out["P2_products"] = sorted(os.listdir(store.dir_of(key))) if os.path.isdir(store.dir_of(key)) else []
        out["A_while_loop_still"] = touch(store._active)
        out["B_sig"] = "sig还活着" if sref() is not None else "sig已经没了"
        pump(2.0, app)
        out["C_receipts"] = got
        out["D_ready"] = store.ready(key)
        return out
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def scenario_exit(hold: float = 0.4, deadline: float = 90.0) -> dict:
    """C：真起子进程，用真 ThumbStore 排两趟，第 hold 秒直接 sys.exit。
    输出走**文件**不走管道：管道要等 EOF，孙子进程（ffmpeg）攥着句柄时 `communicate` 会越过
    timeout 继续挂——上一版就挂在这里，读数是 0 字节而不是"撞到了什么"。"""
    out = {}
    cache = tempfile.mkdtemp(prefix="gelu-jl-")
    so, se = os.path.join(cache, "out.txt"), os.path.join(cache, "err.txt")
    try:
        with open(so, "wb") as fo, open(se, "wb") as fe:
            t0 = time.monotonic()
            p = subprocess.Popen([sys.executable, "-u", "-c", CHILD, HERE, cache, str(hold)],
                                 stdout=fo, stderr=fe, cwd=ROOT,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            while p.poll() is None and time.monotonic() - t0 < deadline:
                time.sleep(0.2)
            out["elapsed_s"] = round(time.monotonic() - t0, 2)
            if p.poll() is None:
                out["rc"] = "到点没退（已终止）"
                p.kill()
                p.wait(10)
            else:
                out["rc"] = p.returncode
        txt = open(so, encoding="utf-8", errors="replace").read() + \
            "\n" + open(se, encoding="utf-8", errors="replace").read()
        out["P3_child_lines"] = " ⏐ ".join(l.strip() for l in txt.splitlines() if l.strip())[:400]
        out["C2_deleted"] = sorted(set(DELETED.findall(txt)))
        tail = [l for l in open(se, encoding="utf-8", errors="replace").read().strip().splitlines() if l.strip()]
        out["C3_stderr_tail"] = " ⏐ ".join(tail[-8:])[:500]
        return out
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def scenario_teardown(app, grace: float = 25.0) -> dict:
    """B：还在跑就把 store 从 Python 侧拆掉。这一档有可能把**探测自己**挂住（上一版实发），
    所以动手前先记一笔、动手后每 0.5s 让 faulthandler 的周期 dump 在盘上留着。"""
    out = {}
    cache = tempfile.mkdtemp(prefix="gelu-jl-")
    try:
        store = ThumbStore(cfgmod.FFMPEG_EXE, cache, max_jobs=1)
        keys = [store.request(p, d, 1920) for p, d in ((SLOW, 60.0), (SLOW2, 32.0)) if os.path.exists(p)]
        t0 = time.monotonic()
        while store._pool.activeThreadCount() == 0 and time.monotonic() - t0 < 3.0:
            time.sleep(0.02)
        out["P3_active_when_torn"] = store._pool.activeThreadCount()
        out["P3_jobs_requested"] = len(keys)
        srefs = [weakref.ref(j.sig) for j in list(store._active.values())]
        say("   B：动手拆（这一步起若挂住，栈在 -stacks.txt）")
        store.setParent(None)
        del store
        gc.collect()
        out["B_survived_tear_s"] = round(time.monotonic() - t0, 2)
        pump(grace, app)
        txt = open(_STACKS, encoding="utf-8", errors="replace").read()
        out["B2_deleted_in_stacks"] = sorted(set(DELETED.findall(txt)))
        out["B3_sig_alive_after"] = ["活" if r() is not None else "没了" for r in srefs]
        return out
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def main() -> int:
    miss = [n for n, p in (("ffmpeg", cfgmod.FFMPEG_EXE), ("样片", SAMPLE), ("4K", SLOW))
            if not os.path.exists(p)]
    if miss:
        say(f"[前提不成立] 盘上缺：{miss} ⇒ 读数不作数")
        return 3
    app = QApplication.instance() or QApplication(sys.argv[:1])
    say(f"起步：日志={os.path.basename(_LOG)} 栈={os.path.basename(_STACKS)}"
        f"（4K 宽 1920 实测 ~2s/趟，用来留撞车窗口）")

    a = scenario_baseline(app)
    show("A 基线：正常跑完", a)
    c = scenario_exit()
    show("C 退进程：子进程第 0.4s 直接 sys.exit", c)
    b = scenario_teardown(app)
    show("B 拆主人：还在跑就 del store", b)

    rows = [a, b, c]
    bad = [r["premise"] for r in rows if "premise" in r]
    if bad:
        say("[前提不成立] " + "；".join(bad) + " ⇒ 读数不作数")
        return 3

    hits = {"A": a.get("B_sig", "?"), "B": b.get("B2_deleted_in_stacks", []),
            "C": c.get("C2_deleted", [])}
    say("撞到的对象：" + " ／ ".join(f"{k}={v}" for k, v in hits.items()))
    say(f"子进程退出了吗：rc={c.get('rc')}（{c.get('elapsed_s')}s）")
    crashed = bool(hits["B"]) or bool(hits["C"])
    hung = isinstance(c.get("rc"), str)
    if crashed:
        say("⇒ 量到了：emit 打在已删除的 C++ 对象上（与 crash.log 同一句）。"
            "本趟只给读数，怎么修、修没修好都要另立判据。")
    elif hung:
        say("⇒ 没撞到 deleted，但撞到了另一种坏：退出这一步挂住（读 rc 与 -stacks.txt）")
    else:
        say("⇒ 这一台没量到：B/C 都没撞见 deleted 那句话，也没挂住。按规矩不许把"
            "『讲得通的机制』写成『已确认的根因』，#109 得降级为『这一台量不到』")
    say("[FINAL] 探测 rc=0（A/B/C 三档读数都拿到了；判决只看上面那三句）")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        traceback.print_exc()
        print(f"[FINAL] 探测自己崩：{type(exc).__name__}: {exc} ⇒ rc=1", flush=True)
        rc = 1
    finally:
        faulthandler.cancel_dump_traceback_later()
        _stack_fd.close()
    sys.exit(rc)
