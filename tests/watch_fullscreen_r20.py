# -*- coding: utf-8 -*-
"""看门狗（R20）：等到"人离开机器"的那一刻，才跑那趟必须占前台的全屏几何探针。

为什么要等：`tests/probe_fullscreen_fit.py` 要把播放器真的铺满整块屏才能量到"全屏那一瞬
到底是 mpv 的子窗口没跟着铺（A），还是铺满了而画面画小（B）"。用户明确交代过：还在用这台
电脑，不许一直把窗口顶到前台；确实需要前台的测量先说一声。所以这里把"什么时候开跑"和
"量到什么"分开——时间闸门只管打扰，判决一律由探针自己出。

四条闸门（①②③借 R18 那位同一口径，不另立）：
  ① 链锁 `tests/out/chain-r17.lock` 不在（或确认是陈旧锁）：会起真窗口的两趟并跑，读到的不是被测者；
  ② 这块屏此刻真的在出像素（直接调 `test_ui.capture_health`，与产品同一把尺）；
  ③ `GetLastInputInfo` 报的空闲秒数 ≥ IDLE_MIN（这条只决定开跑时机，不参与任何判决）；
  ④ **别的项目此刻没在抢前台**（2026-10-20 加的，起因是实打实的撞车）：同一台机器上
     `另一个项目（salt-player）` 那一串也挂了自己的前台趟（`mini_unlock_watcher` / `verify_all
     --with-device` / `probe_mini_window`），它的窗口一压进来，这把尺读到的就不是被测者——
     而 `chain-r17.lock` 只管本仓的趟，管不到隔壁仓。所以这里按**命令行**（不按镜像名批杀批判，
     见记忆 [[feedback-windows-edge-automation-safety]]）扫一遍 python/pythonw，凡是命令行里
     不含 `lumen-player`、却含前台趟形状的，就等它收。查不到（powershell 自己失败）时
     **放行并把"无法确认"印进哨兵**：这条闸门只管打扰与并跑，不参与判决，宁可存疑读一趟，
     也不许因为"问不到"就饿死整趟（那等于把尾巴交回给用户）。排在①②③之后才问：前三条
     任一不通时本来就不开跑，不必每秒起一个 powershell。

两趟的顺序与分流（不许拿"再跑一遍"洗白）：
    分流吃的是探针 `[FINAL]` 里的**档位字段**（`档位=复现／没复现／量不到`，形状与读法都在
    `tests/r20_bucket.py`，它自带自检与证伪），**不吃判决那句话的措辞**。上一版这里写的是
    `"没能复现" not in final1`，而探针的措辞是「没复现：…」（没有「能」字）——好趟被读成
    "量到缺陷"、用户原盘那一趟整趟被跳过，两头的 rc 却都是 0。这一类坑（文本匹配全绿、
    消费它的程序不认）记忆里已经记过，第二次不许再踩，所以 r20_bucket 的静态腿⑫⑬专门盯这个回退。
    第 1 趟＝**冒烟**（合成样片，约 1 分钟）：新写的量具代码先在真屏幕上走一遍，
             档位=复现 就直接收（省掉用户那 11 分钟）；没复现／量不到才起第 2 趟。
    第 2 趟＝用户原盘（实测占前台 658s：那份 4K REMUX 冷启动到第一帧就要 6~9s）。
    任一趟 rc=1 ⇒ 量具自身出错／尺没校准：停，写进哨兵，绝不重跑磨绿；
             **读不出档位也归这一家**（回执不合格就是量具失灵，不猜方向、也不兜底成某一档）。
    任一趟 rc=3 ⇒ 环境不给（锁／屏／片不在／内核没起来／窗口态等不到帧）：
             等 COOLDOWN 再试，最多 MAX_TRIES 次；两趟都没复现 ⇒ 结论只写"这一台量不到"。

探针之后还有两腿（2026-10-20 接进来的尾巴，别再留成"你回来跑这两条命令"）：
    `tests/test_ui.py`（真窗口验收）＋ `tests/test_ui.py --falsify`（证伪台账）
    名字里不写"含 §10.2 与 U61"那种条目清单——那一类手抄上一轮就漏了一次（真数早已超出，
    名字还写着旧的两条）。台账里有几条、各家几条，一律由 test_ui 自己的汇总行与自打行印出来，
    看门狗从**盘上那本日志**现读。
    两腿都要前台（弹真窗口＋读像素），所以同样过四条闸门，并且**全程持 `chain-r17.lock`**：
    本仓别的看门狗见锁不起趟，隔壁那条链则由闸门④负责——锁管不到别人的仓。

跑法（后台）：  python tests/watch_fullscreen_r20.py
日志：tests/out/watch-fullscreen-r20.log（每行即时 flush）
      探针自己的读数 tests/out/probe-fullscreen-fit-watch-<序号>-<时分>.log（由探针另写一份带时分的留档）
      两腿的读数   tests/out/{accept,falsify}-ui-r20-watch-<时分>.log
哨兵：tests/out/watch-fullscreen-r20-done.flag（逐趟 rc ＋ 从该趟日志**现读**的 [FINAL] 判决行）
退出码：0 每一段都算数；1 量具失灵**或 UI 腿报真红**（两回事，处置不同，看 [FINAL] 点的是哪一名）；
        3 某一段到上限仍未拿到读数（含"别仓的趟一直没收，UI 腿没开跑"）／看门狗自身异常。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT_DIR = os.path.join(HERE, "out")
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import build_stamp     # 被测产品的盘上字节指纹（本仓库没有 git，靠它说清"这一拍量的是哪一版"）
import desk_busy      # 闸门④的共用度量器（自带自检与证伪），见 tests/desk_busy.py
import r20_bucket     # 探针判决档位的唯一读法（parse），见 tests/r20_bucket.py

POLL_SECS = 45.0
COOLDOWN = 8 * 60.0
MAX_TRIES = 6
IDLE_MIN = 180.0                  # 空闲至少 3 分钟才允许起趟。注意这只管"什么时候开跑"：
                                  # 一跑就是 1~11 分钟（合成样片约 1 分钟，用户那份 4K 原盘实测 658s），
                                  # 中途人回来会撞见全屏播放器——所以要跟用户打过招呼再挂这里。
DEADLINE_S = 9 * 3600.0
DEADLINE = time.monotonic() + DEADLINE_S   # 9 小时不是 6：这条链现在有三段（探针两趟＋UI 两腿），
# 每段都各自等闸门。上一版 6 小时是"只跑探针"时候定的——尾巴排在头里，别让它因为头里等太久
# 而被时钟掐掉（那等于把"你回来自己跑这两条命令"又写回纸上）。

LOCK = os.path.join(OUT_DIR, "chain-r17.lock")
LOG = os.path.join(OUT_DIR, "watch-fullscreen-r20.log")
FLAG = os.path.join(OUT_DIR, "watch-fullscreen-r20-done.flag")
PROBE = os.path.join(HERE, "probe_fullscreen_fit.py")
TEST_UI = os.path.join(HERE, "test_ui.py")
STILL_ACTIVE = 259
QUERY_LIMITED = 0x1000
SYNC = 0x0010
FINAL = re.compile(r"^\[FINAL\].*$", re.M)
# 探针之外那两腿：名字里不写"几条判据／哪几条编号"，那种手抄一定会漂；条数一律从盘上那本日志现读。
UI_LEGS = (("真窗口验收（含全屏浮层这几条）", (), "accept-ui-r20-watch"),
           ("真窗口验收·证伪（含对应的注入腿）", ("--falsify",), "falsify-ui-r20-watch"))
# 闸门④（"别仓此刻有没有窗在抢前台"）的判据本体在 `tests/desk_busy.py`，这里只调用。

_rows: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def note(msg: str) -> None:
    _rows.append(msg)


def _proc_image(pid: int) -> tuple[bool, str, str]:
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    h = k32.OpenProcess(QUERY_LIMITED | SYNC, False, pid)
    if not h or ctypes.cast(h, ctypes.c_void_p).value == ctypes.c_void_p(-1).value:
        return False, "", f"OpenProcess 拿不到句柄（GetLastError={ctypes.GetLastError()}）⇒ 该 PID 已不在"
    try:
        k32.GetExitCodeProcess.restype = ctypes.c_bool
        k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        code = wt.DWORD(0)
        alive = bool(k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == STILL_ACTIVE)
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        k32.QueryFullProcessImageNameW.restype = ctypes.c_bool
        k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_wchar_p,
                                                   ctypes.POINTER(wt.DWORD)]
        got = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return alive, (buf.value if got else ""), ("在" if alive else f"已退（exit={code.value}）")
    finally:
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle(h)


def lock_held() -> str | None:
    """没文件＝没锁；PID 没了或已回收给非 python＝陈旧锁（按没锁处理并点名）；读不出 PID 保守当有锁。"""
    if not os.path.exists(LOCK):
        return None
    try:
        txt = open(LOCK, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        log(f"锁文件读不动（{type(exc).__name__}: {exc}）：保守当作有锁")
        return LOCK
    m = re.search(r"pid=(\d+)", txt)
    if not m:
        log(f"锁文件里没有 pid=（内容 {txt[:60]!r}）：保守当作有锁")
        return LOCK
    if int(m.group(1)) == os.getpid():
        # 自己就是持锁人（UI 那两腿全程持锁，见 run_ui_leg）：不认这一条就会自锁死——
        # 闸门①等自己放的锁，等到时钟上限，然后交回一本"始终没等到"的账。
        return None
    alive, image, why = _proc_image(int(m.group(1)))
    if not alive:
        log(f"陈旧锁：PID={m.group(1)} 已不在（{why}），按没锁处理；锁文件不代删")
        return None
    if image and "python" not in os.path.basename(image).lower():
        log(f"陈旧锁：PID={m.group(1)} 已回收给 {image}，按没锁处理")
        return None
    return LOCK


def idle_secs() -> float:
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    user32 = ctypes.windll.user32
    user32.GetLastInputInfo.restype = wt.BOOL
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return -1.0                       # 读不到 ⇒ 当"忙"，不开跑
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def foreign_chain() -> tuple[bool, str]:
    """闸门④：**此刻有没有别人的进程在抢前台**。判据本体在 `tests/desk_busy.py`（共用度量器）。

    为什么本仓那把 `chain-r17.lock` 不够：锁是**同仓约定**，隔壁 `salt-player` 那一串不知道
    它有这把锁，也就不会去拿；而它的窗口一压进来，我这把像素尺读到的就是别人的桌面
    （本仓 R9/R18 都把"外来窗口压在上面"记 ENV，从不记产品红——这里做的是同一件事的
    **前置**版本：既然能提前问，就别等量到一本废账）。

    为什么判据是**窗口**而不是命令行名字：第一版按名字扫（`test_xxx.py` 那一类正则）扫出 6 条，
    里面 `_ref_out/watch_probe.py` 那种纯轮询笔记本的趟根本不碰屏幕——按名字批"抢前台"会把
    不相关的趟算进来，白等。"会不会互相抢"的真实载体是"谁此刻有一扇看得见的窗"。

    为什么搬到共用模块而不是留在这儿：`watch_ui_r17.py` 那条链要问同一件事，两处各写一遍迟早
    分叉（本仓已经为"同一条判据抄两份"付过账）。搬过去之后它自带 4 条已知答案的自检和
    6 条注入证伪（`python tests/desk_busy.py --selfcheck-mutate`），不再是一条没牙的 if。
    """
    return desk_busy.busy()


def screen_ready() -> tuple[bool, str]:
    """建 QApplication 之前就得起，否则 grabWindow 没有屏幕可问（同 R18）。"""
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from app.ui import theme
    app.setStyleSheet(theme.qss())
    import test_ui
    test_ui.APP = app
    return test_ui.capture_health()


def ready_to_start(tag: str) -> tuple[bool, str]:
    lk = lock_held()
    if lk:
        return False, f"抢前台的链还开着锁（{os.path.basename(lk)}），等它收"
    ok, detail = screen_ready()
    if not ok:
        return False, f"屏幕仍不出像素：{detail[:150]}"
    secs = idle_secs()
    if secs < IDLE_MIN:
        return False, f"人机交互 {secs:.0f}s 前还动过（阈值 {IDLE_MIN:.0f}s）——有人在用，不抢前台"
    # ④排在最后才问：前三条任一不通时本来就不开跑，不必每 POLL_SECS 起一个 powershell。
    blocked, why, degraded = foreign_chain()
    if blocked:
        return False, f"闸门④挡路：{why}"
    # 降级读数走**结构化字段**，不走子串：靠 `if "查不到" in why` 接判据，度量器改一次话术
    # 这条回执就静默不响（上一版真发：④说"问不到"、闸门 match"查不到"，整晚没印过一条）。
    if degraded:
        note(f"闸门④{why}（{tag}）——这一趟的读数按「可能与别仓的趟并跑」存疑读")
    return True, f"锁已收、屏出像素、机器空闲 {secs:.0f}s、{why}"


def wait_gate(tag: str) -> tuple[bool, str]:
    ok, detail = ready_to_start(tag)
    while not ok:
        if time.monotonic() >= DEADLINE:
            log(f"{tag}：等到上限仍不具备开跑条件（不是量败了）。最后一次读数见上一行")
            return False, detail
        time.sleep(POLL_SECS)
        ok, detail = ready_to_start(tag)
        log(f"等待中（{tag}）：{'可以开跑' if ok else '还不能'} —— {detail[:170]}")
    return ok, detail


def read_stamp(logfile: str) -> str:
    """从探针那本日志现读 `[被测]` 那行，压成一个短指纹（读不到就明说读不到，不猜上一版）。"""
    p = os.path.join(OUT_DIR, logfile)
    if not os.path.exists(p):
        return "被测指纹：日志不在盘上"
    rec = ""
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ln.startswith("[被测]"):
                    m = re.search(r"短=([0-9a-f]{12})", ln)
                    rec = m.group(1) if m else ""
                    break
    except OSError as exc:
        return f"被测指纹：读不动（{type(exc).__name__}）"
    if not rec:
        return "被测指纹：日志里没有 `[被测] 短=`（那版量具不带指纹，读数归属只能靠日志时刻猜）"
    now = build_stamp.stamp_short()
    return (f"探针起趟时短指纹={rec}"
            + ("＝跑完时一样（这一趟期间产品代码没被动过）" if rec == now
               else f"≠跑完时 {now}（**这之后产品代码又改过**，这份判决只属于改前的那一版）"))


def read_final(logfile: str) -> str:
    """判决行从**盘上那本日志**现读，不抄自己记住的数。"""
    p = os.path.join(OUT_DIR, logfile)
    if not os.path.exists(p):
        return "探针日志不在盘上（这趟没落盘，不作数）"
    txt = open(p, encoding="utf-8", errors="replace").read()
    m = FINAL.findall(txt)
    return " ／ ".join(x.strip()[:400] for x in m[-2:]) if m else "日志里没有 [FINAL] 行"


def run_probe(tag: str, idx: int, movie: str) -> tuple[int, str, str]:
    """一趟探针：闸门 → 跑 → 分流。判废重试，量具失灵（rc=1）立刻交回不重试。

    每一试**各落一本日志**（名字带 `t{attempt}`）：2026-10-22 实发——旧写法把 logfile 算在
    循环外、每试都用 `"w"` 打开，第 1 试那本「194/194 + 1 条 ENV」的回执被第 2 试的
    「195/195」整本覆盖，只剩 grep 摘出来的几行。判废重试恰恰是最需要留证据的时候。
    """
    logfile = ""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, PROBE]
    if movie:
        cmd += ["--file", movie]
    for attempt in range(1, MAX_TRIES + 1):
        okg, dg = wait_gate(f"{tag} 第{idx}趟·第{attempt}试")
        if not okg:
            return 3, "", f"第{attempt}试没等到开跑条件（{dg}）"
        logfile = (f"probe-fullscreen-fit-watch-{idx}-t{attempt}-"
                   + time.strftime("%H%M%S") + ".log")
        log(f"开跑 {tag} 第{idx}趟 第{attempt}试：{os.path.basename(PROBE)} --file {movie or '合成样片'} → {logfile}")
        t0 = time.monotonic()
        with open(os.path.join(OUT_DIR, logfile), "w", encoding="utf-8", newline="\n") as fh:
            rc = subprocess.call(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
        final = read_final(logfile)
        secs = time.monotonic() - t0
        st = read_stamp(logfile)
        log(f"跑完 rc={rc} 占前台约 {secs:.0f}s：{final[:300]}")
        note(f"第{idx}趟（试 {attempt} 次，{logfile}，占前台 {secs:.0f}s，rc={rc}，{st}）：{final}")
        if rc in (0, 1):
            return rc, final, logfile
        if time.monotonic() >= DEADLINE:
            log(f"{tag}：上限到了，第{idx}趟仍只拿到「没给证据」的读数")
            return rc, final, logfile
        log(f"这一趟环境没给证据（rc=3）：歇 {COOLDOWN / 60:.0f} 分钟再试，剩余 {MAX_TRIES - attempt} 次")
        time.sleep(COOLDOWN)
    log(f"{tag}：第{idx}趟试满 {MAX_TRIES} 次仍没拿到读数")
    return 3, "", logfile


def read_summary(logfile: str) -> str:
    """从**盘上那本日志**现读汇总行（`---- 汇总：…` / `---- 证伪台账自打：…`）。

    为什么不抄 test_ui 交回 stdout 的那一份：subprocess 的 stdout 与日志是同一个句柄，抄它
    和读盘等价；但"读盘"这一手让哨兵里的每个数都能被后来的人拿去复核（记忆
    [[feedback-cost-numbers-need-provenance]]：汇总数字一旦是手抄的，下一轮就滞后）。
    """
    p = os.path.join(OUT_DIR, logfile)
    if not os.path.exists(p):
        return "日志不在盘上（这腿没落盘，不作数）"
    txt = open(p, encoding="utf-8", errors="replace").read()
    rows = [ln.strip()[:400] for ln in txt.splitlines() if ln.strip().startswith("----")]
    return " ／ ".join(rows[-3:]) if rows else "日志里没有 `----` 汇总行"


def run_ui_leg(tag: str, args: tuple[str, ...], stem: str) -> tuple[int, str, str]:
    """UI 一腿：闸门 → 跑 → 分流（判废重试、真红立刻交回，与探针两趟同一口径）。

    日志名带 `t{attempt}`，理由同 `run_probe`：判废重试不许覆盖上一试的回执。
    """
    logfile = ""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, "-u", TEST_UI, *args]
    for attempt in range(1, MAX_TRIES + 1):
        okg, dg = wait_gate(f"{tag}·第{attempt}试")
        if not okg:
            return 3, "", f"第{attempt}试没等到开跑条件（{dg}）"
        logfile = f"{stem}-t{attempt}-" + time.strftime("%H%M%S") + ".log"
        log(f"开跑 {tag} 第{attempt}试：test_ui.py {' '.join(args) or '(正跑)'} → {logfile}")
        st_before = build_stamp.stamp_short()
        t0 = time.monotonic()
        with open(os.path.join(OUT_DIR, logfile), "w", encoding="utf-8", newline="\n") as fh:
            rc = subprocess.call(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
        summ = read_summary(logfile)
        secs = time.monotonic() - t0
        log(f"跑完 rc={rc} 用时 {secs:.0f}s：{summ[:300]}")
        # test_ui 自己不印指纹（它那份 AST 审计与判据台账不该被这一行扰动），所以由看门狗在
        # **起腿的那一刻**现算：腿跑的就是这一版；跑完再算一次，两值不同＝这一腿期间产品代码被
        # 动过，读数归属就不可信，点名出来（"改动发生在起腿之前还是之后"这件事没有第三个人知道）。
        note(f"{tag}（试 {attempt} 次，{logfile}，{secs:.0f}s，rc={rc}，"
             f"被测短指纹＝{st_before}→跑完时 {build_stamp.stamp_short()}）：{summ}")
        if rc in (0, 1):
            return rc, summ, logfile
        if time.monotonic() >= DEADLINE:
            log(f"{tag}：上限到了，仍只拿到「没给证据」的读数")
            return rc, summ, logfile
        log(f"这一腿环境没给证据（rc={rc}）：歇 {COOLDOWN / 60:.0f} 分钟再试，剩余 {MAX_TRIES - attempt} 次")
        time.sleep(COOLDOWN)
    log(f"{tag}：试满 {MAX_TRIES} 次仍没拿到读数")
    return 3, "", logfile


def take_lock() -> bool:
    """UI 两腿尽量全程持 `chain-r17.lock`：本仓别的看门狗见锁不起趟（隔壁仓归闸门④管）。
    拆锁排在哨兵写完之后（`main()` 的 finally），顺序错了就等于"哨兵还没落盘、别人已起趟"。

    拿不到时**不抢也不代删**（本仓对别人锁文件的既有口径见 `lock_held`），照直起腿但把
    "这一程没锁护着"印进哨兵：闸门①此刻会把活着的锁当"不能开跑"等下去，所以真能带着
    未拆锁文件开跑的只有一种情况——那份锁的持锁人已经被 `_proc_image` 判成不在（陈旧锁）。
    """
    lk = lock_held()
    if lk:
        return False                       # 活锁：闸门①会自己等，这里不该被叫到
    if os.path.exists(LOCK):
        try:
            txt = open(LOCK, encoding="utf-8", errors="replace").read().strip()[:80]
        except OSError as exc:
            txt = f"<读不动：{type(exc).__name__}>"
        note(f"起 UI 腿时盘上有一份**陈旧** chain-r17.lock（{txt}，没代删）⇒ 这一程没有锁护着，"
             "本仓别的看门狗若同时起趟会并跑；读数按存疑读")
        return False
    with open(LOCK, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"pid={os.getpid()} started_at={time.strftime('%Y-%m-%d %H:%M:%S')} "
                 "owner=watch_fullscreen_r20\n")
    log(f"锁已开（{os.path.basename(LOCK)}，pid={os.getpid()}）：UI 两腿串行期间别人不起趟")
    return True


def user_movie() -> str:
    """用户最近一次看的那个原盘——他要修的正是那一幕，所以第一趟就放他的片。
    读法照产品：`data/config.json` 的 recent[0]，盘上没有就退回合成样片（探针默认那趟）。"""
    p = os.path.join(ROOT, "data", "config.json")
    try:
        import json
        rec = json.load(open(p, encoding="utf-8")).get("recent") or []
        for r in rec:
            path = r if isinstance(r, str) else (r or {}).get("path", "")
            if path and os.path.exists(path):
                return path
    except BaseException as exc:
        log(f"读 recent 退回合成样片：{type(exc).__name__}: {exc}")
    return ""


def finish(rc: int, why: str) -> int:
    line = f"[FINAL] R20 收口链（探针两趟＋UI 两腿）：{why} —— rc={rc}；判决与汇总一律从盘上各本日志现读"
    note(line)
    log(line)
    if os.path.exists(FLAG):
        # 上一趟的哨兵是被引用过的证据（第 1 趟那份写进了对话与文档），不许就地覆盖：
        # 先按它自己的 generated_at 留一份带时戳的副本，再写新的。
        try:
            old = open(FLAG, encoding="utf-8", errors="replace").read()
            stamp = re.search(r"generated_at=(.+)", old)
            name = "watch-fullscreen-r20-done-" + (
                stamp.group(1).replace(" ", "-").replace(":", "") if stamp
                else time.strftime("mtime%Y%m%d-%H%M")) + ".flag"
            os.replace(FLAG, os.path.join(OUT_DIR, name))
            log(f"旧哨兵已留底：{name}")
        except OSError as exc:
            log(f"旧哨兵留底失败（{type(exc).__name__}: {exc}）——仍写新的，但上一份的内容就此丢失，点名在这里")
    with open(FLAG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.write("\n".join(_rows) + "\n")
    log(f"哨兵已写：{FLAG}")
    return rc


def probe_phase() -> tuple[int, str]:
    """两趟探针（先冒烟后用户原盘）→（rc, 一句人话）。rc：0 量到了／1 量具失灵／3 没拿到读数。

    分流只走 r20_bucket.parse：这一趟要不要再占用户那片 11 分钟，取决于探针报的**档位字段**
    （`复现`／`没复现`／`量不到`），不取决于判决那句话的措辞。上一版这里是 `"没能复现" not in final1`
    这样的文本匹配，而探针的措辞是「没复现：…」（没有「能」字）——结果**好趟被读成"量到缺陷"**、
    用户原盘那一趟整趟被跳过，两头的 rc 却都还是 0。这类"文本匹配全绿、但消费它的程序不认"
    在记忆里已经记过一次（[[feedback-self-written-detectors-need-controls]]），所以：字段只有一个
    真源（`tests/r20_bucket.py`），它的自检⑫⑬＋证伪 D9/D10 专门盯着"又改回话术／干脆不读字段"
    这两种回退。读不出档位＝量具回执失灵，记 rc=1 停在这里点名，**不猜方向、也不重跑磨绿**。
    """
    movie = user_movie()
    # 第 1 趟先跑**合成样片当冒烟趟**：10s 的片子，一趟约 1 分钟，作用是把量具新写的代码
    # （等帧闸、二次进全屏、几何尺、真光标唤醒站、两趟合账）先在真屏幕上走一遍。它量没量到
    # 缺陷不重要——重要的是别拿用户那 11 分钟的空闲窗口去撞一个 NameError。
    # （上一版实测：用户那份 4K REMUX 冷启动到第一帧 6~9s，整趟占前台 658s。）
    log("先起冒烟趟（合成样片）：新代码先在屏幕上走一遍，再决定要不要占用户那片的时间")
    rc1, final1, lf1 = run_probe("冒烟·合成样片", 1, "")
    if rc1 == 1:
        return 1, f"冒烟趟就量具失灵（{lf1}）——按规矩不重跑磨绿，更不拿用户那片去撞"
    if rc1 != 0:
        return 3, f"冒烟趟始终没拿到读数（{lf1}）"
    b1, w1 = r20_bucket.parse(final1)
    if not b1:
        return 1, f"冒烟趟的回执读不出档位（{w1}）：{lf1}"
    if b1 == "复现":
        return 0, f"冒烟趟就量到了缺陷形状（档位={b1}），不必再占用户那片的时间（{lf1}）"
    log(f"冒烟趟档位＝{b1}（{final1[:120]}）⇒ 起第 2 趟放用户那个片：{movie or '（读不到，退回合成样片）'}")
    rc, final, lf = run_probe("用户原盘", 2, movie)
    if rc == 1:
        return 1, f"冒烟趟档位＝{b1}（{lf1}）／第 2 趟量具失灵（{lf}）"
    if rc != 0:
        return 3, f"冒烟趟档位＝{b1}（{lf1}）／第 2 趟始终没拿到读数（{lf}）"
    b2, w2 = r20_bucket.parse(final)
    if not b2:
        return 1, f"第 2 趟的回执读不出档位（{w2}）：{lf}"
    if b2 == "复现":
        return 0, f"第 2 趟（用户原盘）量到了（档位={b2}，{lf}）"
    if b1 == b2 == "没复现":
        return 0, (f"两趟（合成样片 {lf1} ＋用户原盘 {lf}）档位都是『没复现』 —— 那结论只能是"
                   "「这一台、这一条路径量不到」，不是「没有这个 bug」")
    return 0, (f"两趟档位＝{b1}／{b2}（冒烟 {lf1} ＋原盘 {lf}）：都没量到缺陷形状，"
               "但『量不到』（工具没对象可量）与『没复现』（量到了正常）分家写，不许合并成一句绿")


def ui_phase() -> list[tuple[str, int, str, str]]:
    """探针之后的两腿：真窗口验收＋证伪。串行、全程持锁；判废重试、真红不重跑。"""
    rows: list[tuple[str, int, str, str]] = []
    holding = take_lock()
    try:
        for tag, args, stem in UI_LEGS:
            rc, summ, lf = run_ui_leg(tag, args, stem)
            rows.append((tag, rc, summ, lf))
            if rc == 1:
                log(f"{tag} 交回**真红**（rc=1）：按规矩停在这里，不重跑磨绿——"
                    f"剩下那腿（{UI_LEGS[len(rows)][0] if len(rows) < len(UI_LEGS) else '无'}）不跑，"
                    "并把没跑的那腿点名进哨兵")
                break
    finally:
        if holding:
            try:
                os.remove(LOCK)
                log(f"锁已拆：{os.path.basename(LOCK)}（别的看门狗可以起趟了）")
            except OSError as exc:
                log(f"锁没拆掉（要人工看一眼 {LOCK}）：{type(exc).__name__}: {exc}")
        if len(rows) < len(UI_LEGS):
            note(f"UI 腿有一腿**没跑**（因为上一腿真红）：{UI_LEGS[len(rows)][0]}")
    return rows


def main() -> int:
    log(f"R20 看门狗起步：闸门 空闲≥{IDLE_MIN:.0f}s／屏出像素／链锁收／别仓前台趟没在跑；"
        f"上限 {DEADLINE_S / 3600:.0f} 小时；每趟最多 {MAX_TRIES} 试，判废歇 {COOLDOWN / 60:.0f} 分钟；"
        "顺序＝探针两趟 → test_ui 正跑 → test_ui --falsify")
    note(f"起步={time.strftime('%Y-%m-%d %H:%M:%S')} 闸门=空闲≥{IDLE_MIN:.0f}s + capture_health "
         "+ 链锁 + 别仓前台趟扫描（④查不到时放行并把'无法确认'记进本哨兵）")
    prc, pwhy = probe_phase()
    rows = ui_phase()
    red = [t for t, r, _s, _l in rows if r == 1]
    na = [t for t, r, _s, _l in rows if r not in (0, 1)]
    ui_txt = " ／ ".join(f"{t}：rc={r}；汇总现读＝{s or '（没落盘）'}" for t, r, s, _l in rows) \
        or "两腿都没跑"
    if prc == 1:
        return finish(1, f"探针量具失灵（{pwhy}）；UI 腿另算：{ui_txt}")
    if red:
        return finish(1, f"{pwhy}；UI 腿出现**产品真红**，点名：{red}；另：{ui_txt}")
    if na:
        return finish(3, f"{pwhy}；UI 腿始终没拿到读数的：{na}；另：{ui_txt}")
    return finish(0, f"{pwhy}；UI 腿全绿：{ui_txt}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        try:
            finish(3, f"看门狗自身异常：{type(exc).__name__}: {exc}")
        except BaseException:
            pass
        raise
