"""脱离作业对象起看门狗：起一条就活一条，别把我的终端退了它就跟着没了。

为什么要有这个文件（2026-10-20 实发）：`tests/watch_fullscreen_r20.py` 那一趟是在后台 Bash
调用里直接 `python tests/watch_fullscreen_r20.py` 起的，探针第 2 趟量到『复现』之后正要跑
UI 两腿，日志就停在 03:40:08 那条"闸门④挡路"上——**没有 [FINAL]、没有哨兵、没有异常行**。
这不是看门狗跑崩，是它的父作业（那个 shell）先没了：Windows 的作业对象会把里头的进程一起
带走，所以"我起了一条后台看门狗"这句话的真实含义是"它活到我这一轮结束为止"。留尾巴的规矩
是"不许把活儿交回给用户"，而一条随时会被终端带死的看门狗等于更糟：用户以为它在等，其实它
早没了，盘上只有一本不再增长的日志。

所以这里只做一件事：用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB`
起，然后**当场验活**——验的方式是不看退出码、只看盘上两样：进程表里那个 pid 的**命令行**里
有没有这条脚本，以及日志文件在几秒内有没有长大。按命令行识别而不是按镜像名批处理：这台机器
上 python.exe 是用户自己的，绝不能按名字杀（记忆 [[feedback-windows-edge-automation-safety]]）。

三条判据（`--selftest` 离线跑，不需要真起进程）：
  ① 已经有活的同脚本实例 ⇒ 拒起（返回 3），并点名那个 pid。会改文件的趟（`test_ui --falsify`
     就地改产品文件）并跑两趟＝第二趟把第一趟的变异态当基线，那是投毒不是并发；
  ② 查不到进程表（WMI 自己失败）⇒ **不当"没有活的"**，记 3 存疑：宁可让人来看一眼，
     也不能因为问不到就放行起第二趟（这条与①同源，判据的前提不能是"我猜没事"）；
  ③ 起完验活：pid 命令行不认／日志不长 ⇒ 报"起了但没活"，返回 2，绝不印"已起，等它跑"。

跑法：  python tests/launch_watch.py tests/watch_fullscreen_r20.py
        python tests/launch_watch.py --selftest
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT_DIR = os.path.join(HERE, "out")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# DETACHED_PROCESS 0x8 | CREATE_NEW_PROCESS_GROUP 0x200 | CREATE_BREAKAWAY_FROM_JOB 0x01000000
# 后一个是关键：只前两个的话，进程还挂在父作业对象里，父作业关闭时 WMI 查不到也照样被带走。
FLAGS = 0x8 | 0x200 | 0x01000000

RC_OK, RC_NO_LIVE, RC_SPAWN_DEAD, RC_QUERY_FAIL, RC_BAD_ARG = 0, 1, 2, 3, 4


def live_pids(script: str) -> tuple[bool, list[int]]:
    """按**命令行**数在跑的实例。返回 (查到了没有, pid 列表)。

    查询走 WMI（`Get-CimInstance Win32_Process`），形状与 `tests/desk_busy.py` 一致。
    这一句查不到时返回 (False, [])——调用方必须把它当"无法确认"，不许当"没有活的"。
    两处实发的坑都在这里处理掉了：
      · **不许把查询自己算进去**：本启动器的命令行里就写着 `tests/watch_xxx.py`，
        直接按子串匹配会数出"已经有一位在跑"，于是永远起不了趟（记忆里踩过同一形状）；
        所以摘掉 `os.getpid()`。
      · **零命中≠查不到**：`Get-CimInstance | Where | ConvertTo-Json` 在没有匹配时输出**空串**，
        和 powershell 自己失败的形状一样。所以让它无论如何都印一行 `N=<count>;<json>`：
        有 `N=` 才叫"问到了"，回执是几就是几；没有就记"无法确认"。
    """
    stem = os.path.basename(script).lower()
    cmd = ("$rows = @(Get-CimInstance Win32_Process | "
           "Where-Object { $_.ProcessId -ne $PID -and $_.Name -match '^python(\\.exe|w\\.exe)$' } | "
           "Select-Object ProcessId,CommandLine); "
           "\"N=\" + $rows.Count + \";\" + ($rows | ConvertTo-Json -Compress)")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=40,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return False, []
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out.startswith("N="):
        return False, []
    body = out.split(";", 1)[1] if ";" in out else ""
    rows = []
    if body.strip():
        import json
        try:
            rows = json.loads(body.strip())
        except ValueError:
            return False, []
        if isinstance(rows, dict):
            rows = [rows]
    hits = []
    for row in rows or []:
        cl = str(row.get("CommandLine") or "")
        # 认"这一条脚本"，不认"python 这个镜像"：绝不按镜像名批处理别人的进程（这台机器上
        # python.exe 是用户自己的）。同目录同名（tests/watch_x.py 与 out/watch_x.py）撞上的
        # 概率不大，这里再要求命令行里出现这个 stem 才算数。
        if stem in cl.lower():
            pid = int(row.get("ProcessId") or 0)
            if pid != os.getpid():          # 双保险：WMI 里 $PID 是 powershell 自己的，不是我们的
                hits.append(pid)
    return True, hits


def spawn(script: str, log_name: str = "") -> tuple[int, str]:
    """起一条脱离作业对象的看门狗，返回 (pid|0, 日志绝对路径)。"""
    path = script if os.path.isabs(script) else os.path.join(ROOT, script)
    if not os.path.exists(path):
        return 0, f"（脚本不在：{path}）"
    os.makedirs(OUT_DIR, exist_ok=True)
    # 日志由脚本自己写（每行 flush）；这里只兜住"脚本自己 print／traceback 没重定向"那部分，
    # 否则那条最重要的"看门狗自身异常"会跟着 DETACHED_PROCESS 一起消失进空气里。
    lg = log_name or (os.path.splitext(os.path.basename(path))[0] + "-launcher.log")
    logpath = lg if os.path.isabs(lg) else os.path.join(OUT_DIR, lg)
    try:
        with open(logpath, "a", encoding="utf-8", errors="replace") as fh:
            fh.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} launcher 起趟："
                     f"{os.path.basename(path)} ====\n")
            fh.flush()
            os.fsync(fh.fileno())
            p = subprocess.Popen([sys.executable, "-u", path], cwd=ROOT,
                                 stdout=fh, stderr=subprocess.STDOUT,
                                 creationflags=FLAGS, close_fds=True)
    except OSError as exc:
        return 0, f"起趟失败：{type(exc).__name__}: {exc}"
    return p.pid, logpath


def verify(pid: int, script: str, logpath: str, secs: float = 12.0) -> tuple[bool, str]:
    """验活＝两样都要：进程表里那个 pid 的命令行认这条脚本 ＋ 日志在这段里长大。

    只看第一样会漏"起来了但第一行就崩"（崩了的孩子还在 pid 表里的窗口极短，
    或者脚本自己 fork 完就退）；只看第二样会漏"上一趟留下的日志刚好在这一秒被别的东西写"。
    """
    stem = os.path.basename(script).lower()
    size0 = os.path.getsize(logpath) if os.path.exists(logpath) else -1
    deadline = time.time() + secs
    seen_pid = ""
    grew = ""
    while time.time() < deadline:
        ok_query, pids = live_pids(script)
        if ok_query and pid in pids:
            seen_pid = f"pid={pid} 在进程表里认这条脚本"
        if os.path.exists(logpath) and os.path.getsize(logpath) > size0:
            grew = f"日志长大 {os.path.getsize(logpath) - size0}B"
            break
        time.sleep(1.0)
    if seen_pid and grew:
        return True, f"{seen_pid}；{grew}"
    if not seen_pid and not grew:
        return False, "验活两头都空：进程表里没认到这条脚本，日志也没长（这就是『起了但没活』）"
    if not grew:
        return False, (f"{seen_pid}，但 {secs:.0f}s 内日志没长＝起步就卡住／崩在第一行之前；"
                       f"看 {os.path.basename(logpath)} 末尾")
    return False, f"日志长了但进程表认不到 pid={pid}（可能它自己 exec 成了别的命令行）；存疑，人工看一眼"


# ---------------- 离线自检：已知答案对照 + 变异 ----------------

def decide(queryable: bool, hits: list[int]) -> tuple[str, int]:
    """①②那两条闸的纯函数：(档, rc)。有活的→拒起；查不到→存疑；干净→放行。"""
    if hits:
        return "已有活的实例", RC_NO_LIVE
    if not queryable:
        return "无法确认（WMI 查不到，不当『没有活的』）", RC_QUERY_FAIL
    return "可以起", RC_OK


def selftest() -> int:
    cases = [
        ("干净盘上无实例 → 放行", True, [], ("可以起", RC_OK)),
        ("有一位在跑 → 拒起（会改文件的趟不许并跑）", True, [4321], ("已有活的实例", RC_NO_LIVE)),
        ("查不到进程表 → 存疑，绝不放行", False, [], ("无法确认（WMI 查不到，不当『没有活的』）", RC_QUERY_FAIL)),
        ("查不到＋有 id → 仍按『有活的』拒（保守取严的一边）", False, [9], ("已有活的实例", RC_NO_LIVE)),
    ]
    bad = 0
    for name, q, h, want in cases:
        got = decide(q, h)
        tag = "PASS" if got == want else "FAIL"
        if got != want:
            bad += 1
        print(f"[{tag}] {name}：期望 {want} ／ 实得 {got}", flush=True)
    # 变异一：把"查不到"兜成"没有活的"（最常见的坏法：`return [], False` 写成 `if not q: return True, []`）
    def mutated_query_swallows(q, h):
        return decide(True, []) if not q else decide(q, h)

    m1 = mutated_query_swallows(False, [])
    ok1 = (m1 != cases[2][3])
    print(f"[{'PASS' if ok1 else 'FAIL'}] 变异①（查不到→当没有活的）必须翻红：实得 {m1} "
          f"基线 {cases[2][3]} ⇒ {'翻了' if ok1 else '没翻＝这条判据是摆设'}", flush=True)
    # 变异二：无视已存在的实例硬起（并跑投毒的来路）
    def mutated_ignore_alive(q, h):
        return decide(q, [])

    m2 = mutated_ignore_alive(True, [4321])
    ok2 = (m2 != cases[1][3])
    print(f"[{'PASS' if ok2 else 'FAIL'}] 变异②（有活的仍放行）必须翻红：实得 {m2} "
          f"基线 {cases[1][3]} ⇒ {'翻了' if ok2 else '没翻'}", flush=True)
    # 常量本身也要有牙：少了 CREATE_BREAKAWAY_FROM_JOB 就等于没修这个问题
    ok3 = (FLAGS & 0x01000000) != 0 and (FLAGS & 0x8) != 0
    print(f"[{'PASS' if ok3 else 'FAIL'}] 旗子含 BREAKAWAY+DETACHED（不然父作业一关它就没了）"
          f"：FLAGS={hex(FLAGS)}", flush=True)
    total = bad + (0 if ok1 else 1) + (0 if ok2 else 1) + (0 if ok3 else 1)
    print(f"---- [FINAL] 启动器自检：{len(cases) + 3} 档已知答案／变异，错 {total} ⇒ "
          f"rc={1 if total else 0}", flush=True)
    return 1 if total else 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    script = argv[0] if argv else ""
    if not script:
        print("用法：python tests/launch_watch.py <tests/watch_xxx.py> [日志名]；或 --selftest",
              flush=True)
        return RC_BAD_ARG
    log_name = argv[1] if len(argv) > 1 else ""
    q, hits = live_pids(script)
    tier, rc = decide(q, hits)
    if rc != RC_OK:
        print(f"[{tier}] 不起趟：{('活着的 pid=' + ','.join(map(str, hits))) if hits else '无'}"
              f"（并跑＝两把尺互踩／会改文件的趟把对方变异态当基线）", flush=True)
        return rc
    pid, logpath = spawn(script, log_name)
    if not pid:
        print(f"[起趟失败] {logpath}", flush=True)
        return RC_SPAWN_DEAD
    ok, why = verify(pid, script, logpath)
    print(f"[{'起了且活着' if ok else '起了但没活'}] pid={pid} 日志={logpath}：{why}", flush=True)
    return RC_OK if ok else RC_SPAWN_DEAD


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"[FINAL] 启动器自身异常：{type(exc).__name__}: {exc}", flush=True)
        raise SystemExit(RC_QUERY_FAIL)
