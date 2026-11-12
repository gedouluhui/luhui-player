"""后台作业的四件公共件：发射打死不了宿主、在途壳钉住、池子不归 store 拆、退出前收干净。

为什么要有这个文件（**都是量出来的，不是推出来的**，2026-10-20）：

1. `data/logs/crash.log` 2026-10-19 22:56:34 那一桩：线程池作业发射回报时
   `RuntimeError: wrapped C/C++ object of type _JobSignals has been deleted`，而且**兜底的
   `except Exception` 里那句"把失败发出去"自己又抛了同一个错**——两道墙塌在同一块砖上，
   异常一个都跑不进钩子，PyQt 直接 failfast。探针 `tests/_probe_exit_crash.py` 在真机上
   量到的分布：作业还在途就退进程 ⇒ 15 趟里 8 趟 `0xC0000409`（-1073740791），
   先排空再退 ⇒ 3 趟全干净；钩子阳性对照抓到 2 条，同一条 RuntimeError 原文在 6 趟里出现 5 趟。
   所以"用户双击打开、放了一会儿、关掉窗口、进程没了、日志里只有 rc=127"这一类事是**统计性**
   的，不是"低概率所以不算问题"。
2. `tests/_probe_job_lifetime.py` 另量到一条独立的：作业还在跑时把 store 拆掉，
   **主线程死锁**——六份 45s 的 faulthandler 栈一模一样：主线程停在 `del store`，
   工作线程停在 `thumbs.py:78 os.makedirs` 等 GIL。机制是 `QThreadPool(self)` 把池子挂在
   store 名下，拆 store 会调 `~QThreadPool`，它内部等线程收；这一等是**攥着 GIL 的 C++ 调用**，
   而那个线程要 GIL 才能走完 `os.makedirs`。于是"池子的爹是谁"不是风格问题，是死锁问题。
3. 量具自己又撞到一条**本文件的**死锁（2026-10-20 04:42，`tests/_probe_exit_crash.py` D 组
   两趟各挂满 120s 没退，faulthandler 的栈每次一模一样）：
   `poolkit.py:304 in stats` → `poolkit.py:246 in pools`——`stats()` **持着 `_LOCK`** 又调了
   同样要这把锁的 `pools()`。`threading.Lock` 不可重入：同一条线程第二次 `acquire()` 就是
   永久阻塞，而且是**持着锁**阻塞，于是后面任何要这把锁的调用（`shutdown()` 的立旗、清排队、
   杀孩子全在等它）一起冻住。界面线程走到这里就是"窗口关不掉"，比 #109 原来那条崩更难查——
   崩至少进程没了，卡住是整扇窗跟着死。所以这把锁是 `RLock`。为什么不改成"`stats` 里别调
   `pools`"：那种写法靠"以后没人再嵌套"维持，而嵌套是这类登记表最自然的写法（下次再加一个
   `children_left` 就会再撞一次）；可重入把这**整类**坑一次封掉，代价只是同线程内多几次计数。

对应四条件（每家 store 用同一套，别再各写一份、各漏一处）：

* `emit(sig, name, *args)`：发射只该"送到就没",绝不该把宿主带走；吞掉的都计数、留原文，
  验收脚本要能问出"这趟到底吞没吞"。
* `pin/unpin`：把信号壳的 Python 强引用寄在模块级名册里，`run()` 结束才解寄。C++ 壳是被
  sip 随着 store 拆除／解释器收尾拆掉的，多这一道强引用就拆不到它头上。
* `new_pool`：**池子不归 store 所有**（模块级强引用名册养着）。这一条同时解决两件事：
  拆 store 不再触发 `~QThreadPool` 的攥-GIL 等待；退出时能按名字点名每一张池子还活动着几条线程。
* `shutdown(grace_ms)`：立旗（不再接新活）→ 清排队 → 杀掉登记过的 ffmpeg 孩子 → **用 Python
  侧 `time.sleep` 轮询**等收（sleep 让出 GIL，工作线程才走得动）。这里绝不写长的
  `waitForDone(ms)`：那是"攥着 GIL 等一个需要 GIL 的线程"，正是上面第 2 条的机制本体。

`QThreadPool` 在本机 PyQt 5.15.2 上没有 `setExpirable`（实测 `dir()` 只有 `setExpiryTimeout`），
也没有 `waitForFinished`（只有 `waitForDone`）。写之前先 `dir()`，别抄 Qt6 的名字——
`thumbs.wait()` 那一条就为这个每次调用都抛 AttributeError，只是没人真去调过。
"""
from __future__ import annotations

import subprocess
import threading
import time
from typing import Any

from PyQt5.QtCore import QCoreApplication, QObject, QThreadPool, pyqtSignal

# 回报载荷各家形状不同（thumb/poster 是 str key，gif 是 int id + 4 个参数，scan 是 list+dict），
# 所以名册只养"壳"，不假设它的签名；子类按需再挂别的信号。
class JobSignals(QObject):
    """QRunnable 不是 QObject，信号只能挂在这样一个小壳上往外转。

    这个小壳就是 crash.log 里被删掉的那个对象：它没有 parent，C++ 侧的生命周期跟着
    Python 包装器走，而包装器会在 store 拆除／解释器收尾时被 decref 到零。
    """
    done = pyqtSignal(str, bool, str)          # key, 成功?, 详情


# 可重入是**必须的**，不是保险：见模块开头第 3 条实测（stats 持锁调 pools，Lock 版直接把
# 调用线程永久冻住，D 组两趟各挂 120s）。登记表里的函数互相调用是常态，不许靠"别嵌套"维持。
_LOCK = threading.RLock()
_PINNED: dict[int, Any] = {}          # id(信号壳) -> 壳（在途作业强引用）
_CHILDREN: dict[int, Any] = {}        # id(job) -> subprocess.Popen（我们生的孩子，退出时要杀）
_POOLS: dict[int, tuple[QThreadPool, str]] = {}     # id(pool) -> (池子, 名字)
_SWALLOWED: list[str] = []            # 最近几条"吞掉的发射"原文（判据要能点名，不能只给计数）
_COUNTS = {"swallowed": 0, "pinned": 0, "killed": 0, "shutdowns": 0, "stuck": 0}
_CLOSING = False
LAST_READINGS: dict[str, Any] = {}


def set_closing(flag: bool) -> None:
    """"不再接新活"的旗。产品里只由 `shutdown()` 立；验收脚本可以放下来复用同一批对象。"""
    global _CLOSING
    _CLOSING = bool(flag)


def is_closing() -> bool:
    return _CLOSING


def reset() -> None:
    """把名册与计数清干净（**只给验收脚本用**：一个进程里连跑多趟判据时，
    上一趟留下的计数会把这一趟的"应当为 0"读成红，那是一条假红，不是产品坏）。"""
    global _CLOSING
    with _LOCK:
        _PINNED.clear()
        _CHILDREN.clear()
        _SWALLOWED.clear()
        for k in _COUNTS:
            _COUNTS[k] = 0
        LAST_READINGS.clear()
    _CLOSING = False


# ---------------- 发射 ----------------

def emit(sig: Any, name: str, *args) -> bool:
    """发一帧回报。送到了返回 True；壳没了／Qt 对象已死／别的任何意外一律吞掉返回 False。

    这一句必须**在所有 except 分支里也用**：crash.log 那一桩就是兜底分支自己发射时再抛，
    于是"给失败留证据"这条路反而成了压死宿主的最后一根。宽到 `BaseException` 是有意的——
    工作线程里没有键盘中断要接，任何从这层跑出去的异常都只会有一个去处：把进程带走。
    """
    try:
        getattr(sig, name).emit(*args)
        return True
    except BaseException as exc:
        txt = f"{type(sig).__name__}.{name} -> {type(exc).__name__}: {exc}"
        with _LOCK:
            _COUNTS["swallowed"] += 1
            _SWALLOWED.append(txt)
            del _SWALLOWED[:-8]
        return False


# ---------------- 在途名册 ----------------

def pin(job: Any) -> None:
    """把作业的信号壳寄进模块级名册（`run()` 开头调，`finally` 里 `unpin`）。"""
    sig = getattr(job, "sig", None)
    if sig is None:
        return
    with _LOCK:
        _PINNED[id(sig)] = sig
        _COUNTS["pinned"] += 1


def unpin(job: Any) -> None:
    sig = getattr(job, "sig", None)
    if sig is None:
        return
    with _LOCK:
        _PINNED.pop(id(sig), None)


def pinned_count() -> int:
    with _LOCK:
        return len(_PINNED)


# ---------------- 子进程 ----------------

def spawn(job: Any, cmd: list[str], **kw) -> subprocess.Popen:
    """起孩子并登记。登记不是为了管它，是为了**退出时能杀它**：
    实测过（`tests/test_gif_real.py --residue`）关掉窗口后 ffmpeg 的 pid 还在，
    产物从 0 字节一路写到 5 MB——用户以为"取消了"，其实"关掉了个还在后台写盘的导出"。"""
    kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc = subprocess.Popen(cmd, **kw)
    with _LOCK:
        _CHILDREN[id(job)] = proc
    return proc


def drop_child(job: Any) -> None:
    with _LOCK:
        _CHILDREN.pop(id(job), None)


def run(job: Any, cmd: list[str], timeout: float | None = None, **kw) -> subprocess.CompletedProcess:
    """`subprocess.run` 的替身：argv 与返回形状照旧（`.returncode/.stdout/.stderr`），
    多出来的一件事是这个孩子被登记过，`shutdown()` 杀得到它。

    超时行为与 `subprocess.run` 保持一致：杀掉、收干净、把 `TimeoutExpired` 抛回给调用方
    （调用方的 `except Exception` 会把它写成一次失败回报）。这里不兜成 rc——
    "超时"和"ffmpeg 自己失败"在日志里是两回事，合并就把线索洗掉了。
    """
    text = bool(kw.get("text")) or ("encoding" in kw)
    # capture_output 是 subprocess.run 的糖，Popen 没有这个参数；调用方沿用旧写法时
    # 在这里换回显式的 stdout/stderr，别把 TypeError 留给退出那一瞬去发现。
    kw = dict(kw)
    kw.pop("capture_output", None)
    proc = spawn(job, cmd, stdin=subprocess.DEVNULL,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        return subprocess.CompletedProcess(cmd, proc.returncode,
                                           out if text else (out or b""),
                                           err if text else (err or b""))
    finally:
        drop_child(job)


def kill_children() -> int:
    """杀掉所有登记过、还活着的孩子。只动这张名册里的 pid——绝不按镜像名批处理。"""
    with _LOCK:
        procs = list(_CHILDREN.values())
    n = 0
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
                n += 1
        except (OSError, ValueError):
            pass
    with _LOCK:
        _COUNTS["killed"] += n
        _CHILDREN.clear()
    return n


def child_pids() -> list[int]:
    with _LOCK:
        return [int(p.pid) for p in _CHILDREN.values()]


# ---------------- 池子 ----------------

def new_pool(name: str, max_threads: int) -> QThreadPool:
    """造一张**不归 store 所有**的线程池，并登记名字。

    为什么不写 `QThreadPool(self)`：那是把池子的生死交给 store，而 `~QThreadPool` 会等线程收，
    这一等攥着 GIL（见模块开头第 2 条实测死锁）。挂到 `QCoreApplication` 名下也只是让它活到
    进程结束，真正的强引用在 `_POOLS` 里——名字是给 `shutdown()` 的读数用的，出问题时能点名
    "是哪张池子还挂着几条线程"，而不是只印一句"没排空"。
    """
    app = QCoreApplication.instance()
    pool = QThreadPool(app) if app is not None else QThreadPool()
    pool.setMaxThreadCount(max(1, int(max_threads)))
    with _LOCK:
        _POOLS[id(pool)] = (pool, name)
    return pool


def adopt_global_pool(name: str = "global") -> QThreadPool:
    """把 `QThreadPool.globalInstance()` 也登记进来（gif 与统计页采样用的是它）。

    不登记的话 `shutdown()` 看不见它，退出收口就漏掉"正在导 GIF／正在采样"这两条——
    而那正是关掉窗口之后还在写盘的那两条。

    两张 store 用的是**同一个对象**，所以名字要合并而不是覆盖：后登记的一方把前一个名字
    顶掉的话，读数里就只剩"stats"，看日志的人会以为 gif 那条根本没在 poolkit 眼里——
    名册少一个名字不要紧，**装作少了一个**才要紧。
    """
    pool = QThreadPool.globalInstance()
    with _LOCK:
        cur = _POOLS.get(id(pool))
        names = []
        if cur is not None:
            names = [n for n in cur[1].split("+") if n]
        if name not in names:
            names.append(name)
        _POOLS[id(pool)] = (pool, "+".join(names))
    return pool


def pools() -> list[tuple[str, QThreadPool]]:
    with _LOCK:
        return [(n, p) for p, n in _POOLS.values()]


def active_threads() -> int:
    return sum(p.activeThreadCount() for _n, p in pools())


# ---------------- 退出收口 ----------------

def shutdown(grace_ms: int = 1500) -> dict[str, Any]:
    """退出前把在途作业收干净；返回这一趟的读数（验收脚本原样印进日志）。

    四步的顺序是有原因的，不能换：
      ① 立旗 —— 先断"再来新活"的路，否则边收边有 `request()` 递新作业，永远收不完；
      ② `clear()` —— 只扔**还没开跑**的排队项（已开跑的杀不掉，只能等它自己走完）；
      ③ 杀孩子 —— ffmpeg 那类子进程一杀，卡在 `communicate()` 里的工作线程当场就能走，
         这是"grace 之内真能排空"的主要来源（不杀的话 1.5s 只是干等一个 1800s 的超时）；
      ④ Python 侧轮询等收 —— 每一步都让出 GIL。

    幂等：关第二次窗口、`closeEvent` 与 `aboutToQuit` 各调一次都不该重复计数或卡住。
    到 grace 还没收干净的那部分记进 `stuck` 并点名，**不放宽、也不假装绿**：那些作业靠
    `emit()` 与 `pin()` 兜住，最坏是一条被吞掉的回报，不再是整条进程。
    """
    t0 = time.time()
    set_closing(True)
    for _name, pool in pools():
        try:
            pool.clear()
        except (RuntimeError, AttributeError):
            pass
    killed = kill_children()
    deadline = t0 + max(0.0, grace_ms / 1000.0)
    while time.time() < deadline and active_threads() > 0:
        time.sleep(0.02)          # 让出 GIL：工作线程是 Python 代码，攥着 GIL 等它＝死锁
    left = {n: p.activeThreadCount() for n, p in pools() if p.activeThreadCount() > 0}
    readings = {
        "grace_ms": int(grace_ms),
        "killed": killed,
        "pinned_left": pinned_count(),
        "active_left": sum(left.values()),
        "per_pool": left,
        "waited_ms": int((time.time() - t0) * 1000),
        "swallowed": int(_COUNTS["swallowed"]),
    }
    with _LOCK:
        _COUNTS["shutdowns"] += 1
        _COUNTS["stuck"] += 1 if left else 0
        LAST_READINGS.update(readings)
    return readings


def stats() -> dict[str, Any]:
    with _LOCK:
        out = dict(_COUNTS)
        out["pinned"] = len(_PINNED)
        out["children"] = len(_CHILDREN)
        out["swallowed_text"] = list(_SWALLOWED)
        out["pools"] = sorted(n for n, _p in pools())
        out["closing"] = _CLOSING
        out["readings"] = dict(LAST_READINGS)
    return out
