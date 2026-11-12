"""点桌面那个 .lnk（真实双击入口），确认它开得出「LuHui播放器」窗口。

和 smoke_launch 的区别：那边直接拉起 pythonw -m app.main，这边中间多了一层快捷方式，
所以验的是"文件里存的目标/参数/工作目录在改名之后仍然对得上"这一条链路。
判"窗口是我们的"用窗标题==APP_TITLE，认进程用可执行文件绝对路径，绝不按镜像名动手。
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import APP_TITLE, APP_NAME  # noqa: E402
from tests.smoke_launch import (  # noqa: E402
    WM_CLOSE, enum_toplevels, our_mpv, window_pid, window_title)

shell32 = ctypes.windll.shell32
user32 = ctypes.windll.user32
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")
LNK = os.path.join(DESKTOP, f"{APP_TITLE}.lnk")

rc = 0
res: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global rc
    res.append((name, ok, detail))
    if not ok:
        rc = 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  —— {detail}")


check("桌面有这一名字的快捷方式", os.path.exists(LNK), LNK)
if not os.path.exists(LNK):
    print("---- 后面的检查做不了，先跑 python packaging/make_shortcut.py ----")
    raise SystemExit(1)

before_mpv = set(our_mpv())
h = shell32.ShellExecuteW(None, "open", LNK, None, None, 1)      # 1 = SW_SHOWNORMAL
check("ShellExecuteW 接受了这个 .lnk", int(h) > 32, f"rc={int(h)}（>32 才算成功）")

hwnd, t0 = 0, time.time()
while time.time() - t0 < 25.0:
    cands = [w for w in enum_toplevels() if window_title(w) == APP_TITLE]
    if cands:
        hwnd = cands[0]
        break
    time.sleep(0.25)
check(f"出现标题为「{APP_TITLE}」的窗口", hwnd != 0,
      f"hwnd={hwnd} 用时 {time.time() - t0:.1f}s")

if hwnd:
    pid = window_pid(hwnd)
    exe = ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    hproc = kernel_open = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if hproc:
        buf = ctypes.create_unicode_buffer(4096)
        size = ctypes.c_ulong(4096)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
            exe = buf.value
        ctypes.windll.kernel32.CloseHandle(hproc)
    check("开窗的进程解释器就是本项目 .lnk 里写的那个 pythonw",
          exe.lower().endswith("pythonw.exe") and "anaconda3" in exe.lower(),
          f"pid={pid} exe={exe}")
    ok_close = user32.PostMessageW(hwnd, WM_CLOSE, 0, 1)
    check("点了 ×", bool(ok_close), f"PostMessageW={ok_close}")
    t1 = time.time()
    while time.time() - t1 < 15.0:
        if not user32.IsWindow(hwnd) or window_title(hwnd) != APP_TITLE:
            break
        time.sleep(0.25)
    check("窗口随之消失", not user32.IsWindow(hwnd) or window_title(hwnd) != APP_TITLE,
          f"存活 {time.time() - t1:.1f}s")

deadline = time.time() + 15.0
left = set(our_mpv()) - before_mpv
while left and time.time() < deadline:
    time.sleep(0.3)
    left = set(our_mpv()) - before_mpv
check("本项目的 mpv 内核没有残留", not left, f"残留 pid={sorted(left)}")
print(f"\n---- .lnk 验收：{sum(1 for _, o, _ in res if o)}/{len(res)} 通过 "
      f"（品牌={APP_TITLE} / {APP_NAME}）----")
raise SystemExit(rc)
