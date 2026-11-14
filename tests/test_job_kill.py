# -*- coding: utf-8 -*-
"""子进程树收口自检：Job Object 内核级 kill-on-close + 启动清扫缓存残骸。

要守的四件事（全是"主进程没能正常退出"那一档）：

  J1 `win32.assign_to_kill_job(pid)` 真把进程放进一个带
     `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job 里（IsProcessInJob 验，不听返回值）；
  J2 内核级收口：中间进程被 `taskkill /F` 强杀（等价于主程序 Qt 崩溃/被强杀），
     它名下的孙进程必须由内核杀掉——不依赖我们任何 Python 收尾代码；
  J3 `poolkit.spawn()` 出的孩子自动进 Job（ffmpeg 那些探针/导出/封面都走它）；
  J4 `player.begin_start()` 的 Popen 之后立刻有 assign 调用（源码结构闸门）；
  J5 启动清扫会把缓存里上次被强杀留下的 `*.part.jpg` / `*.tmp` 清掉，
     成品与无关文件原样保留，且 `app/main.py` 的 `main()` 真的调用了它。

注意 J2 用的是一个**独立中间进程**：它自己建 Job、起孙进程、打一行 pid 后睡死。
taskkill /F 中间进程 ⇒ Job 句柄随进程消失 ⇒ 孙进程被内核终止。
本测试进程自己的 Job 不影响它。
"""
from __future__ import annotations

import ast
import ctypes
import ctypes.wintypes as wt
import os
import shutil
import subprocess
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from app import win32  # noqa: E402

_k32 = ctypes.windll.kernel32
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wt.BOOL)]
_k32.IsProcessInJob.restype = wt.BOOL
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wt.DWORD]
_k32.WaitForSingleObject.restype = wt.DWORD

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 258
WAIT_OBJECT_0 = 0


def _open(pid: int, access: int):
    return _k32.OpenProcess(access, False, int(pid))


def in_job(pid: int) -> bool:
    h = _open(pid, PROCESS_QUERY_LIMITED_INFORMATION)
    if not h:
        return False
    try:
        res = wt.BOOL(False)
        return bool(_k32.IsProcessInJob(h, None, ctypes.byref(res))) and bool(res.value)
    finally:
        _k32.CloseHandle(h)


def alive(pid: int) -> bool:
    h = _open(pid, SYNCHRONIZE)
    if not h:
        return False
    try:
        return _k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        _k32.CloseHandle(h)


def wait_dead(pid: int, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def j1_assign() -> tuple[bool, str]:
    if not hasattr(win32, "assign_to_kill_job"):
        return False, "win32 里没有 assign_to_kill_job"
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ok = win32.assign_to_kill_job(p.pid)
        if not ok:
            return False, "assign_to_kill_job 返回 False"
        if not in_job(p.pid):
            return False, "IsProcessInJob=False（返回值骗人）"
        return True, ""
    finally:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


def j2_kill_on_close() -> tuple[bool, str]:
    helper = (
        "import subprocess,sys,time;"
        f"sys.path.insert(0, {ROOT!r});"
        "from app import win32;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
        "assert win32.assign_to_kill_job(p.pid) is True;"
        "print(p.pid, flush=True);"
        "time.sleep(120)"
    )
    mid = subprocess.Popen([sys.executable, "-c", helper],
                           stdout=subprocess.PIPE, text=True)
    try:
        line = mid.stdout.readline().strip()
        if not line:
            return False, "中间进程没报出孙进程 pid（assign 阶段就失败了）"
        gpid = int(line)
        if not alive(gpid):
            return False, "孙进程没起来"
        subprocess.run(["taskkill", "/F", "/PID", str(mid.pid)],
                       capture_output=True, timeout=15)
        try:
            mid.wait(timeout=10)
        except subprocess.TimeoutExpired:
            return False, "中间进程没被 taskkill 杀掉（测试前提不成立）"
        if not wait_dead(gpid, 5.0):
            return False, f"中间进程已死 5s，孙进程 {gpid} 还活着（内核没有收口）"
        return True, ""
    finally:
        try:
            mid.kill()
        except Exception:
            pass


def j3_poolkit_spawn() -> tuple[bool, str]:
    from app.engine import poolkit
    if not hasattr(win32, "assign_to_kill_job"):
        return False, "win32 里没有 assign_to_kill_job"
    owner = object()
    p = poolkit.spawn(owner, [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        if not in_job(p.pid):
            return False, "poolkit.spawn 的孩子不在 Job 里"
        return True, ""
    finally:
        poolkit.drop_child(owner)
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass


def _calls_assign(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name == "assign_to_kill_job":
                return True
    return False


def j4_player_wiring() -> tuple[bool, str]:
    path = os.path.join(ROOT, "app", "engine", "player.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "begin_start":
            return (_calls_assign(node), "" if _calls_assign(node)
                    else "begin_start 里没有 assign_to_kill_job 调用")
    return False, "player.py 里找不到 begin_start"


def j5_cache_sweep() -> tuple[bool, str]:
    from app import config as cfgmod
    if not hasattr(cfgmod, "sweep_cache_temp"):
        return False, "config.py 里没有 sweep_cache_temp"
    tmp = tempfile.mkdtemp(prefix="sweep-", dir=HERE)
    try:
        os.makedirs(os.path.join(tmp, "posters"))
        os.makedirs(os.path.join(tmp, "thumbs"))
        dead = [os.path.join(tmp, "posters", "abc.part.jpg"),
                os.path.join(tmp, "thumbs", "xyz.tmp")]
        live = [os.path.join(tmp, "posters", "abc.jpg"),
                os.path.join(tmp, "thumbs", "keep.bin"),
                os.path.join(tmp, "notes.txt")]
        for p in dead + live:
            with open(p, "wb") as fh:
                fh.write(b"x" * 32)
        n = cfgmod.sweep_cache_temp(tmp)
        if n != 2:
            return False, f"扫掉 {n} 个，应为 2"
        left = [p for p in dead if os.path.exists(p)]
        if left:
            return False, f"半成品还在：{left}"
        missing = [p for p in live if not os.path.exists(p)]
        if missing:
            return False, f"误删了成品/无关文件：{missing}"
        # 接线闸门：main() 必须真的调它（函数存在但没人调 = 残骸永远不清）
        tree = ast.parse(open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read())
        wired = any(isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "sweep_cache_temp"
                    for node in ast.walk(tree))
        if not wired:
            return False, "app/main.py 没有调用 sweep_cache_temp"
        return True, ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    rows = [("J1 assign_to_kill_job 真入 Job", j1_assign),
            ("J2 强杀中间进程 ⇒ 内核收口孙进程", j2_kill_on_close),
            ("J3 poolkit.spawn 自动入 Job", j3_poolkit_spawn),
            ("J4 player.begin_start 有 assign 接线", j4_player_wiring),
            ("J5 启动清扫缓存残骸 + main() 接线", j5_cache_sweep)]
    red = 0
    for name, fn in rows:
        try:
            ok, detail = fn()
        except Exception as exc:              # noqa: BLE001 - 自检脚本要给出全貌
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" ｜ {detail}" if detail else ""))
        red += 0 if ok else 1
    print(f"[FINAL] 子进程收口：{len(rows) - red}/{len(rows)} 通过；红 {red} 条")
    return 1 if red else 0


if __name__ == "__main__":
    raise SystemExit(main())
