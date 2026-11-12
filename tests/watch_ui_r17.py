"""看门狗（R17）：等这块屏真的能出像素了，把 #88 那三条**吃真窗口/真内核**的判据补跑完。

为什么不直接跑：这一段新加的判据（播放列表「添加」＝追加／拖到窗口＝整表替换／空列表添加
必须播起来，加一条还原前提）全在 `test_ui.py` 的 `main()` 里，而 `main()` 在第 4.5 节有一道
`capture_health()` 闸门——整屏抠不出像素（锁屏／显示器休眠）时它当场 `return 3` 中止，
**根本走不到我这几条**。此刻本机实测就是那个形状：整屏 3840×2160、亮度采样只有一种、最亮 0。
停在半截不等于做完了，也不等于产品坏了，它就是环境没给东西（留档见
tests/out/screen-probe-r17.txt 那一次读数）。所以这一趟不硬跑，写个看门狗等它给。

三趟，各问一件事（**串行**：上一趟收拾干净才起下一趟，两趟同时开着会抢前台、
而 test_ui 的按键与像素判据量的就是"前台此刻归谁"）：
  ① `--falsify`：U58 那条新证伪样本必须 CAUGHT（整表替换／只入列表不播／把拖放也改成
     追加／模式认错四种读数全被否决，三份好读数放行，前提立不住时点名前提）；
     同趟其余 84 行（r16 实测行数）**不许**因为屏幕亮了就少抓一条。
  ② 真窗口验收：台账 166→170（r16 是 166/166），新四条必须绿。
  ③ 再来一趟真窗口验收：问的不是"哪一边坏"，是**可复现**。这四条会改列表状态，
     收尾那条"还原到本段开始时的状态"就是为了让后面的判据前提不变——还原没做好，
     表现恰好是"第一趟绿、第二趟红"。两趟都绿才叫收口。

跑法（后台，8 小时上限）：
    python tests/watch_ui_r17.py
日志：tests/out/watch-ui-r17.log（每 60 秒记一次"此刻的屏幕读数"，超时也留可诊断的最后一行）
      各趟自己的日志 tests/out/{falsify,accept}-ui-r17.log、tests/out/accept-ui-r17b.log
哨兵：tests/out/watch-ui-r17-done.flag（三趟各自的 rc；写"跳过"= 等到上限屏幕仍不给，
      那一趟不作数）。r15 那份同名 `ui-round-done.flag` 是别的趟的账，这里不碰。

判据与产品同口径：屏幕可用性借 `test_ui.capture_health` 那**同一个**度量器
（整屏至少两种亮度且最亮 >8），这里不另写一把尺子——两把尺迟早对不上，
就会一边报"亮了"另一边报"还是黑的"。

开跑要过三道闸门（前两道是这份文件原来就有的，第三道 2026-10-20 加）：
  ① `chain-r17.lock` 没被别的链开着（同仓约定）；
  ② 屏幕真出像素；
  ③ **别仓此刻没有会抢前台的窗**（判据本体 `tests/desk_busy.py`）。
（`watch_fullscreen_r20.py` 那边还多一道"人机交互空闲 ≥180s"，这里不加：那趟会把屏占到全屏、
用户回来一定撞见；这三趟只开自己的窗，用户在场也能跑，最多是被撞——所以它只需要①②③。）
为什么要加③：①那把锁是同仓约定，隔壁 `另一个项目（salt-player）` 那一串不知道它有锁、
也就不会去拿。2026-10-20 实发：本仓刚要起前台趟，那边 `verify_all.py --with-device`／
`mini_unlock_watcher.py`／`salt_player.py` 正开着 895×591 的窗。`test_ui` 的按键通道与像素
判据量的就是"前台与屏幕此刻归谁"，两趟并跑读到的就不是被测者——那一红既不是产品坏也不是
屏幕黑，是两把尺互相踩。判据问的是**窗口**而不是命令行名字（理由与自检／证伪见 desk_busy）。
③查不到身份时**不挡路**，只把"这一趟可能与别仓并跑"记进日志——这条闸门只管打扰，
问不到就饿死整趟等于把尾巴交回给用户。
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

import desk_busy      # 闸门③（别仓有没有窗在抢前台）的共用度量器，自带自检与注入证伪

POLL_SECS = 60.0
DEADLINE = time.monotonic() + 8 * 3600.0
ROUNDS = ((("--falsify",), "falsify-ui-r17.log", "test_ui.py"),
          ((), "accept-ui-r17.log", "test_ui.py"),
          ((), "accept-ui-r17b.log", "test_ui.py"))
SENTINEL = "watch-ui-r17-done.flag"
# 别的"会自己起真窗口、还把前台抢走"的趟（e2e_openwith / e2e_instance_handoff 这类按双击
# 形状起实例的）开着这把锁时，本看门狗**不起趟**。为什么：test_ui 的按键通道与像素判据量的
# 就是"前台与屏幕此刻归谁"，另一趟同时在这台机器上弹自己的窗口，读到的就不是被测者——
# 那一红既不是产品坏也不是屏幕黑，是两把尺互相踩。锁由那条链自己建、自己拆
# （tests/chain_r17.py），本看门狗只读不写；屏幕一亮就撞上锁时它继续等，等到上限才认"跳过"。
LOCK = os.path.join(OUT_DIR, "chain-r17.lock")
# 锁文件里那条命令行的持有者是个 python.exe。查 PID 死活时要顺带认一下它的可执行文件，
# 否则 PID 被回收给别人（浏览器／资源管理器）时会误判"链还在跑"，白等八小时。
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x0010


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _proc_image(pid: int) -> tuple[bool, str, str]:
    """这个 PID 此刻还在不在、它的可执行文件是什么（拿不到名字就给空串）、外加一句原因。"""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not h or ctypes.cast(h, ctypes.c_void_p).value == ctypes.c_void_p(-1).value:
        return False, "", (f"OpenProcess 拿不到句柄（GetLastError={ctypes.GetLastError()}）"
                           "⇒ 该 PID 已不在")
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        k32.QueryFullProcessImageNameW.restype = wt.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD,
                                                   ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
        got = bool(k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)))
        code = wt.DWORD()
        k32.GetExitCodeProcess.restype = wt.BOOL
        k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        alive = bool(k32.GetExitCodeProcess(h, ctypes.byref(code))
                     and code.value == _STILL_ACTIVE)
        return alive, (buf.value if got else ""), f"{buf.value if got else '?'} exitcode={code.value}"
    finally:
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle(h)


def lock_held() -> str | None:
    """链锁在不在：在 ⇒ 返回锁文件名（本看门狗不起趟）；不在 ⇒ None。

    三种形状分开处理，别一锅端成"有锁就等"：
      ① 文件不存在 → 没锁。
      ② 文件在、里面的 PID 也还在（且它确实是个 python）→ 链正在跑，等它收哨兵再拆锁。
      ③ 文件在、PID 已经不在（或换成了别的程序）→ **陈旧锁**：上次链被掐断没走到 finally。
         这里按"没锁"处理并印一行点名——否则一条中断的链能把看门狗吊到八小时上限，
         而那八小时里其实根本没人抢前台。
    读不出 PID（文件被写坏）时**保守当作有锁**：宁可等，也不和一条可能真在跑的链并跑。
    """
    if not os.path.exists(LOCK):
        return None
    try:
        txt = open(LOCK, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        log(f"锁文件读不动（{type(exc).__name__}: {exc}）：保守当作有锁，宁等不并跑")
        return LOCK
    m = re.search(r"pid=(\d+)", txt)
    if not m:
        log(f"锁文件里没有 pid= 这行（内容 {txt[:60]!r}）：保守当作有锁，宁等不并跑")
        return LOCK
    alive, image, why = _proc_image(int(m.group(1)))
    if not alive:
        log(f"陈旧锁：PID={m.group(1)} 已经不在了（{why}），按没锁处理；"
            f"锁文件留在 {LOCK} 供事后核对，不代删")
        return None
    if image:
        # 认得出名字才敢判"PID 被回收给别人"；认不出（QueryFullProcessImageNameW 失败）
        # 就保守当作链还在——宁可多等，也不和一条真在跑的链并跑。
        if "python" not in os.path.basename(image).lower():
            log(f"陈旧锁：PID={m.group(1)} 已被回收给 {image}，不是那条链，按没锁处理")
            return None
    return LOCK


def screen_ready() -> tuple[bool, str]:
    """借用 test_ui 的那把尺子；建 QApplication 之前就得起，否则 grabWindow 没有屏幕可问。"""
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from app.ui import theme
    app.setStyleSheet(theme.qss())          # 与 test_ui.main() 同环境，免得量到裸 Qt 外观
    import test_ui
    test_ui.APP = app
    return test_ui.capture_health()


def wait_screen(tag: str) -> tuple[bool, str]:
    """等到"屏幕出像素 **且** 没有别的抢前台的趟在跑"两件事同时成立。

    返回 False 只有两种，原因都印在最后一行日志里：①到 8 小时上限屏仍不给（那一趟记"跳过"，
    不作数，也不算产品坏）；②上限到了锁还没放开——那是链那边卡住了，同样不硬起趟，
    否则 test_ui 会量到别人的窗口，交回来一本没人能解释的账。
    """
    def probe() -> tuple[bool, str]:
        lk = lock_held()
        if lk:
            return False, f"抢前台的链还开着锁（{os.path.basename(lk)}），本看门狗等它收"
        ok, detail = screen_ready()
        if not ok:
            return False, detail
        # ③排在最后：前两闸任一不通时本来就不开跑，不必每 POLL_SECS 起一个 powershell 问命令行。
        blocked, why, degraded = desk_busy.busy()
        if blocked:
            return False, f"别仓的趟正在抢前台：{why}"
        return True, detail + (f"；③{why}（身份查不到⇒这一趟按「可能与别仓的趟并跑」存疑读）"
                               if degraded else "；③别仓现在没有会抢前台的窗")
    ok, detail = probe()
    while not ok:
        if time.monotonic() >= DEADLINE:
            log(f"{tag}：等到上限仍不具备开跑条件（不是跑败了）。最后一次读数见上一行")
            return False, detail
        time.sleep(POLL_SECS)
        ok, detail = probe()
        log(f"等待中（{tag}）：{'可以开跑' if ok else '还不能'} —— {detail[:160]}")
    return ok, detail


def run_round(args: tuple[str, ...], logfile: str, script: str) -> int:
    path = os.path.join(OUT_DIR, logfile)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, os.path.join(HERE, script), *args]
    log(f"开跑：{' '.join(cmd)}  → {logfile}")
    with open(path, "w", encoding="utf-8", errors="replace") as fh:
        rc = subprocess.call(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
    tail = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [l.rstrip() for l in fh if l.strip()]
        tail = " | ".join(lines[-3:])
    except OSError:
        pass
    log(f"跑完 rc={rc}：{tail[:300]}")
    return rc


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    done_flag = os.path.join(OUT_DIR, SENTINEL)
    if os.path.exists(done_flag):
        os.remove(done_flag)               # 幂等：旧哨兵不许冒充这一趟的结论
    ok, detail = screen_ready()
    lk = lock_held()
    log(f"起步先看屏：{'能用' if ok else '不能用'} —— {detail}"
        + (f"；另：抢前台的链锁开着 {os.path.basename(lk)}" if lk else ""))
    rcs: dict[str, int | None] = {}
    if not ok or lk:
        ok, detail = wait_screen("起步")
        if not ok:
            for _a, lf, _s in ROUNDS:
                rcs[lf] = None
            with open(done_flag, "w", encoding="utf-8") as fh:
                fh.write("\n".join([f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}"]
                                   + [f"{k}: rc=跳过（屏幕不给）" for k in rcs]) + "\n")
            log(f"FINAL 三趟全跳过（屏幕 8 小时没给像素，不是跑败了）最后一次读数={detail[:160]} "
                f"哨兵={done_flag} rc=3")
            return 3
    for args, logfile, script in ROUNDS:
        ok2, d2 = wait_screen(logfile)     # 每趟开跑前复查：中途可能又暗下去
        log(f"开跑 {logfile} 之前复查屏幕：{'能用' if ok2 else '不能用'} —— {d2[:160]}")
        if not ok2:
            rcs[logfile] = None
            log(f"{logfile} 跳过（屏幕不给），不作数；屏亮后重跑本看门狗")
            continue
        rcs[logfile] = run_round(args, logfile, script)
    lines = [f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}"]
    for logfile, rc in rcs.items():
        lines.append(f"{logfile}: rc={'跳过（屏幕不给）' if rc is None else rc}")
    with open(done_flag, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    log("FINAL " + " ".join(f"{k}={v}" for k, v in rcs.items()) + f" 哨兵={done_flag}")
    real = [v for v in rcs.values() if v is not None]
    return 0 if real and all(v == 0 for v in real) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException as exc:                            # noqa: BLE001
        # 看门狗自己炸也要留下账：否则下一位只看到"没有哨兵"，分不清是没跑还是跑崩。
        log(f"看门狗自身异常：{type(exc).__name__}: {exc}")
        import traceback
        print(traceback.format_exc(), flush=True)
        sys.exit(2)
