# -*- coding: utf-8 -*-
"""看门狗（R18）：等到"键盘与像素都归我们"的那一刻，把真窗口验收跑成一趟零判废。

为什么还要一位（R17 那位已于 08:44 收工）：本轮引擎缓存表又动过两次——
① `playlist-pos` 等四格从换文件抹掉的名单里摘掉（修"点添加＝整表替换"），
② 整张 `_FILE_SCOPED` 逐格普查之后又摘掉 `current-vo`，并为此新增一条真机判据 6.10。
台账因此从 170 长到 171。171 这个数字已经有读数（09:35／09:39 两趟 0 真红，6.10 两次都绿），
但那两趟各有一串判废：8～9 条按键判据＋0～6 条像素判据量的是"眼睛与键盘此刻归谁"，
外来窗口（Kimi Code / WPS PDF / 隔壁项目的悬浮窗）压在最上面时它们只能记 ENV（rc=3），
**既不算通过也不算产品坏**。缺的就是"一趟全绿、一条判废都没有"的那个读数。

三条闸门，缺一不开跑（都借现成的尺子，这里不另立口径）：
  ① 链锁 `tests/out/chain-r17.lock` 必须不在（或已确认是陈旧锁）——会起真窗口的两趟并跑，
     读到的就不是被测者；
  ② 这块屏此刻真的在出像素：直接调 `test_ui.capture_health`，与产品同一把尺；
  ③ 机器得像是没人用：`GetLastInputInfo` 报的空闲秒数 ≥ IDLE_MIN。这一条是**为用户加的**，
     不是为判据加的——一趟验收要把自己窗口抬到前台四五分钟，人在打字时不该被打断。
     它不参与任何判决，只决定什么时候开跑。

开跑后的分流（不许拿"再跑一遍"洗白）：
    rc=0 ⇒ 这一趟算数，接着跑第二趟（问可复现）；
    rc=1 ⇒ 真红。立刻停，把红的那几条点名进哨兵——绝不重跑磨成绿；
    rc=3 ⇒ 判废（环境没给证据）。等 COOLDOWN 再试，最多 MAX_TRIES 趟，到上限就把
            "始终没等到"写进哨兵并留最后一次读数，不冒充做完、也不冒充产品坏。

跑法（后台）：
    python tests/watch_ui_r18.py
日志：tests/out/watch-ui-r18.log（每行即时 flush；等待中每 POLL_SECS 记一次读数）
      各趟自己的日志 tests/out/accept-ui-r18-watch-<序号>-<时分>.log
哨兵：tests/out/watch-ui-r18-done.flag（逐趟 rc ＋ 从该趟日志里**现读**的汇总行 ＋ [FINAL]）
退出码：0 两趟全绿；1 出现真红；3 到上限仍未拿到零判废的读数。
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

POLL_SECS = 60.0                 # 等待期的读数节奏
COOLDOWN = 15 * 60.0             # 一趟判废之后歇多久再试
MAX_TRIES = 8                    # 每趟最多试几次（判废才重试；真红不重试）
IDLE_MIN = 180.0                 # 用户空闲至少这么多秒才允许抢前台
DEADLINE = time.monotonic() + 8 * 3600.0
LOCK = os.path.join(OUT_DIR, "chain-r17.lock")
LOG = os.path.join(OUT_DIR, "watch-ui-r18.log")
FLAG = os.path.join(OUT_DIR, "watch-ui-r18-done.flag")
STILL_ACTIVE = 259
QUERY_LIMITED = 0x1000
SYNC = 0x0010

_rows: list[str] = []            # 哨兵内容：边跑边攒，最后一次也写得出来


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def note(msg: str) -> None:
    _rows.append(msg)


def _proc_image(pid: int) -> tuple[bool, str, str]:
    """这个 PID 此刻还在不在、它的可执行文件是什么（拿不到名字给空串）、加一句原因。"""
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
    """链锁在不在。三种形状分开判（照 R17 那位同一口径）：没文件＝没锁；
    PID 活着且是 python＝链在跑，等；PID 没了或已被回收给别的程序＝陈旧锁，按没锁处理并点名。
    读不出 PID 时保守当作有锁——宁可等，也不和一条可能真在跑的链并跑。"""
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
    alive, image, why = _proc_image(int(m.group(1)))
    if not alive:
        log(f"陈旧锁：PID={m.group(1)} 已不在（{why}），按没锁处理；锁文件不代删")
        return None
    if image and "python" not in os.path.basename(image).lower():
        log(f"陈旧锁：PID={m.group(1)} 已回收给 {image}，按没锁处理")
        return None
    return LOCK


def idle_secs() -> float:
    """机器已经多久没人动键盘鼠标（GetLastInputInfo；这是"什么时候开跑不打扰人"的闸门，不进判决）。"""
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    user32 = ctypes.windll.user32
    user32.GetLastInputInfo.restype = wt.BOOL
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return -1.0                       # 读不到 ⇒ 当"忙"，不开跑（宁可等）
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def screen_ready() -> tuple[bool, str]:
    """借 test_ui 那把尺子（建 QApplication 之前就得起，否则 grabWindow 没有屏幕可问）。"""
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
        return False, (f"人机交互 {secs:.0f}s 前还动过（阈值 {IDLE_MIN:.0f}s）——"
                       "有人在用这台机器，不去抢前台")
    return True, f"锁已收、屏出像素、机器空闲 {secs:.0f}s"


def wait_gate(tag: str) -> tuple[bool, str]:
    ok, detail = ready_to_start(tag)
    while not ok:
        if time.monotonic() >= DEADLINE:
            log(f"{tag}：等到上限仍不具备开跑条件（不是跑败了）。最后一次读数见上一行")
            return False, detail
        time.sleep(POLL_SECS)
        ok, detail = ready_to_start(tag)
        log(f"等待中（{tag}）：{'可以开跑' if ok else '还不能'} —— {detail[:170]}")
    return ok, detail


SUMMARY = re.compile(r"----\s*汇总：(\d+)/(\d+) 通过；(无失败|失败项.*)")


def read_back(logfile: str) -> str:
    """从那一趟自己的日志里现读汇总行与判废数——哨兵抄的是盘上事实，不是我记住的数。"""
    p = os.path.join(OUT_DIR, logfile)
    if not os.path.exists(p):
        return "日志不在盘上（这趟没落盘，不作数）"
    txt = open(p, encoding="utf-8", errors="replace").read()
    m = SUMMARY.search(txt)
    n_env = len(re.findall(r"^\[ENV\]", txt, re.M))
    n_fail = len(re.findall(r"^\[FAIL\]", txt, re.M))
    return (f"汇总={m.group(0).strip() if m else '没打到汇总行'} 判废 ENV={n_env} 真红 FAIL={n_fail}")


def run_accept(tag: str, idx: int) -> tuple[int, str]:
    """跑一趟真窗口验收；判废（rc=3）按 COOLDOWN 重试，真红（rc=1）立刻交回来不重试。"""
    logfile = f"accept-ui-r18-watch-{idx}-" + time.strftime("%H%M") + ".log"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, os.path.join(HERE, "test_ui.py")]
    for attempt in range(1, MAX_TRIES + 1):
        okg, dg = wait_gate(f"{tag} 第{idx}趟·第{attempt}试")
        if not okg:
            return 3, f"第{attempt}试没等到开跑条件（{dg}）"
        log(f"开跑 {tag} 第{idx}趟 第{attempt}试：{' '.join(cmd)} → {logfile}")
        t0 = time.monotonic()
        with open(os.path.join(OUT_DIR, logfile), "w", encoding="utf-8", newline="\n") as fh:
            rc = subprocess.call(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
        back = read_back(logfile)
        log(f"跑完 rc={rc} 用时 {time.monotonic() - t0:.0f}s：{back}")
        note(f"第{idx}趟（试了 {attempt} 次，{logfile}）：rc={rc}；{back}")
        if rc in (0, 1):
            return rc, logfile
        if time.monotonic() >= DEADLINE:
            log(f"{tag}：上限到了，第{idx}趟仍只拿到判废读数")
            return rc, logfile
        log(f"这一趟是判废（外来窗口拿着键盘/像素）：歇 {COOLDOWN / 60:.0f} 分钟再试，"
            f"剩余次数 {MAX_TRIES - attempt}")
        time.sleep(COOLDOWN)
    log(f"{tag}：第{idx}趟试满 {MAX_TRIES} 次仍没拿到零判废的读数")
    return 3, logfile


def finish(rc: int, why: str) -> int:
    line = (f"[FINAL] R18 真窗口验收补跑：{why} —— rc={rc}；"
            "汇总/判废数由本脚本从各趟日志现读（文档别抄我，抄那两本日志）")
    note(line)
    log(line)
    with open(FLAG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        fh.write("\n".join(_rows) + "\n")
    log(f"哨兵已写：{FLAG}")
    return rc


def main() -> int:
    log(f"R18 看门狗起步：阈值 空闲≥{IDLE_MIN:.0f}s／屏出像素／链锁收；"
        f"上限 8 小时；每趟最多 {MAX_TRIES} 试，判废歇 {COOLDOWN / 60:.0f} 分钟")
    note(f"起步={time.strftime('%Y-%m-%d %H:%M:%S')} 闸门=空闲≥{IDLE_MIN:.0f}s + capture_health + 链锁")
    rc1, l1 = run_accept("验收", 1)
    if rc1 == 1:
        return finish(1, f"第 1 趟真红（{l1}）——按规矩不重跑磨绿")
    if rc1 != 0:
        return finish(3, f"第 1 趟始终判废（{l1}），第 2 趟因此没起")
    rc2, l2 = run_accept("验收（可复现第二趟）", 2)
    if rc2 == 1:
        return finish(1, f"第 1 趟绿（{l1}）／第 2 趟真红（{l2}）——第一趟绿第二趟红就是还原没做好")
    if rc2 != 0:
        return finish(3, f"第 1 趟绿（{l1}）／第 2 趟判废（{l2}）")
    return finish(0, f"两趟全绿零判废（{l1}／{l2}）")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:                       # 看门狗自己崩了也要留话，别静默消失
        try:
            finish(3, f"看门狗自身异常：{type(exc).__name__}: {exc}")
        except BaseException:
            pass
        raise
