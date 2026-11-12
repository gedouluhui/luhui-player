# -*- coding: utf-8 -*-
"""闸门④的共用度量器：此刻有没有**别人的**进程在抢前台（看门狗用它决定什么时候开跑）。

为什么要有这个文件（2026-10-20）：本仓两位看门狗（`watch_ui_r17.py`／`watch_fullscreen_r20.py`）
都靠 `tests/out/chain-r17.lock` 躲开彼此的趟，可那把锁是**同仓约定**——隔壁另一个仓
（`salt-player`）那一串不知道它有这把锁，也就不会去拿。当天实发：我这边刚要起前台趟，那边
`verify_all.py --with-mutants --with-device`／`test_audioplay.py`／`probe_mini_window.py` 正在跑。
两条都会弹真窗口的趟并跑，读到的就不是被测者（本仓对"外来窗口压在上面"历来记 ENV、从不记
产品红——这里做的是它的**前置**版本：能提前问，就别等量到一本废账）。

判据为什么是**窗口**而不是命令行名字：第一版就是按名字扫（`test_[a-z_]+\\.py` 那类），扫出 6 条，
其中 `_ref_out/watch_probe.py` 那种纯网络轮询根本不碰屏幕——按名字批"抢前台"会把不相关的趟
算进来，白等一晚上。"会不会互相抢"这件事的真实载体是"谁此刻有一扇看得见的窗"。

"是不是本仓的"为什么要把命令行里的脚本路径**解析成绝对路径**再比：看门狗自己就是
`python tests/watch_ui_r17.py` 这样起的，命令行里根本没有 `lumen-player` 这五个字。
第一版只搜那个子串，于是本仓自己的窗被判成外来——`--selfcheck` 的②当场翻红（红得对，是尺子的错，
不是产品的错），当场把它钉成"路径落进本仓目录算自己人"。

两半各管一件事（都只决定**什么时候开跑**，不参与任何判决）：
  A 窗口半：python/pythonw 进程持有 ≥ MIN_W×MIN_H 的可见顶层窗，且**不是本仓的**；
  B 命令行半：别的仓的命令行带前台链特征（`--with-device`／`verify_all`／`unlock_watcher`），
     或是一个**会建窗**的播放器实例（`*_player.py`；`smoke_launch` 那条故意不建窗的对照腿不算）。
B 查不到时（powershell 起不来／超时／rc≠0）**不挡路**：身份改由窗口标题第二通道判，
并把"无法核对身份"印在回执里。理由：这条闸门只管打扰，问不到就饿死整趟等于把尾巴交回给用户。

**R21 的口径修正（2026-10-20 当晚，挂看门狗要起跑时发现整条链被按住）**：上面两半原来
"命中即挡路"，而挂上真桌面之后挡住它的是用户自己开着放歌的椒盐音乐——一扇 895×591 的
**背景**窗＋一条 `salt_player.py`，没有任何自动化趟在驱动它。这把尺子最初的理由是
"两趟会弹真窗口的自动化并跑，读到的不是被测者"，不是"桌面上有别人的窗"：被测腿本来就要
把自家窗顶到最前，而且量到遮挡时自己记 ENV。所以 `classify` 把命中分成两档——
  挡路：外来窗正是**当前前台**／同 pid 命令行带自动化趟形状／命令行问不到（保守）／
        对不上任何形状的可见大窗（保守，豁免只给指名那一档）；
  只报不挡：**背景里的空闲播放器实例**（有窗但不在前台、命令行只有 `*_player.py` 那一形状，
        或连窗都查不到）。
降级只此一种输入，其余四档一个字没动；每一条都有反向哨兵（自检⑥⑦⑧⑨⑩⑪⑫）——
"任何放宽都要配对向哨兵，否则分不出『口径变准』与『守卫被松』"（记忆
[[feedback-self-written-detectors-need-controls]]）。"只报不挡"也照样进回执（`why_text`
那条纯函数判据⑬盯着）：看不见背景里有什么，下一次口径坏了就没人发现。

跑法：
    python tests/desk_busy.py                       # 现在问一次，把人话打到 stdout
    python tests/desk_busy.py --selfcheck           # 校准这把尺（会短暂显三扇自己造的小窗，不抢前台）
    python tests/desk_busy.py --selfcheck-mutate     # 证伪：注坏尺子，逐条要求自检翻红
退出码：0 没被挡／问不到；1 自检或证伪有错；3 此刻确实有别家在抢前台。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import re
import shlex
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

IMGS = ("python.exe", "pythonw.exe")
OURS_CMD = r"lumen[\-\\]player"                       # 命令行里**写了绝对路径**的本仓＝自己人
SCRIPT_EXTS = (".py", ".pyw", ".bat", ".cmd", ".ps1", ".vbs")
OURS_TITLE = r"LuHui|GeluPlayer|luhui|gelu|截图工坊|快捷键|睡眠定时|设置"   # 第二身份通道（含旧名兼容）
APP_CMD = r"[\w\-]*player\.py"                        # 会建窗的播放器实例（别仓的）
NON_WINDOW = r"smoke_launch"                          # 名字像播放器但**不建窗**的对照腿
FOREGROUND_TOK = r"--with\-device|verify_all|unlock_watcher"
MIN_W, MIN_H = 120, 60                                # 再小的算托盘／隐形工具窗，不认为是"抢前台"
_STILL_ACTIVE = 259
_QUERY_LIMITED = 0x1000
_SYNC = 0x0010
EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def proc_image(pid: int) -> tuple[bool, str]:
    """这个 PID 还在不在、它的可执行文件叫什么（拿不到名字给空串）。"""
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    h = k32.OpenProcess(_QUERY_LIMITED | _SYNC, False, pid)
    if not h or ctypes.cast(h, ctypes.c_void_p).value == ctypes.c_void_p(-1).value:
        return False, ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        k32.QueryFullProcessImageNameW.restype = wt.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_wchar_p,
                                                   ctypes.POINTER(wt.DWORD)]
        got = bool(k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)))
        code = wt.DWORD()
        k32.GetExitCodeProcess.restype = wt.BOOL
        k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        alive = bool(k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == _STILL_ACTIVE)
        return alive, (buf.value if got else "")
    finally:
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle(h)


def command_lines(timeout: float = 60.0) -> tuple[dict[int, str] | None, str]:
    """pid → 命令行。**按命令行认自己人，绝不按镜像名批判**（记忆
    [[feedback-windows-edge-automation-safety]]：按镜像名批处理把用户自己的进程算进去过）。
    查不到就交回 (None, 原因)，由调用方决定降级口径——这里不猜。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | "
          'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }')
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except BaseException as exc:                       # noqa: BLE001
        return None, f"命令行查不到（{type(exc).__name__}）"
    if r.returncode != 0:
        return None, f"命令行查不到（powershell rc={r.returncode}）"
    out: dict[int, str] = {}
    for ln in (r.stdout or "").splitlines():
        pid_s, _, cmd = ln.partition("\t")
        if pid_s.strip().isdigit():
            out[int(pid_s.strip())] = cmd.strip()
    if not out:
        # 空表是"查成了但没有 python 进程"，与"根本没查成"必须分开：前者是真干净，后者不许当干净。
        return {}, "命令行表为空（这台机器上没有 python 进程在跑）"
    return out, ""


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(p)))


def cmd_paths(cmd: str) -> list[str]:
    """命令行里所有**看着像脚本文件**的 token，逐个试解析成绝对路径。

    先用 shlex 按引号切（`"F:\某 片.py"` 这种带空格的路径要是按空格切就废了），再剥引号。
    相对路径按两个基准各试一次：本仓目录（看门狗／探针一律是从仓根起的，这是主渠道）
    和当前 cwd（万一有人从别处起）。解析不了的（只写了 `python -c` 那种）就不算路径，
    交给绝对路径子串那一档兜。
    """
    try:
        toks = shlex.split(cmd or "", posix=False)
    except BaseException:                              # noqa: BLE001
        toks = (cmd or "").split()
    out: list[str] = []
    for raw in toks:
        tok = raw.strip('"').strip("'")
        if not tok.lower().endswith(SCRIPT_EXTS):
            continue
        for base in (ROOT, os.getcwd()):
            cand = tok if os.path.isabs(tok) else os.path.join(base, tok)
            try:
                out.append(_norm(cand))
            except BaseException:                      # noqa: BLE001
                pass
    return out


def in_repo(cmd: str | None) -> bool:
    """这一条命令行属不属于本仓（两个渠道：绝对路径子串 / 脚本 token 解析后落在本仓目录里）。"""
    if not cmd:
        return False
    if re.search(OURS_CMD, cmd):
        return True
    root = _norm(ROOT)
    return any(p == root or p.startswith(root + os.sep) for p in cmd_paths(cmd))


def is_ours(cmd: str | None, title: str, pid: int = 0) -> bool:
    """这扇窗／这个进程是不是**本仓**的。三个身份通道，从硬到软：
    自己这个 PID（扫描者从不把自己算成外人，否则自检／看门狗一开窗就把自己挡到时钟上限）、
    命令行（主）、窗口标题（只在命令行问不到时才用——它是窗口自己带的名字，不是我们去猜的）。"""
    if pid and pid == os.getpid():
        return True
    if cmd is not None:
        return in_repo(cmd)
    return bool(re.search(OURS_TITLE, title or ""))


def visible_windows(cmds: dict[int, str] | None,
                    pretend_foreign: bool = False) -> tuple[list[dict], list[str]]:
    """A 半：枚举顶层窗，挑出"python 系 + 可见 + 够大 + 不是本仓"的。
    `pretend_foreign=True` 只给自检用：把归属判据摘掉，专门验这把尺**看得见窗**
    （看不见的尺永远报"桌面干净"，那是最舒服的假绿光）。"""
    u = ctypes.windll.user32
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    u.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
    u.IsWindowVisible.restype = wt.BOOL
    u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    rows: list[dict] = []
    notes: list[str] = []
    img_cache: dict[int, str] = {}
    pids: set[int] = set()

    def image(pid: int) -> str:
        if pid not in img_cache:
            _alive, img = proc_image(pid)
            img_cache[pid] = os.path.basename(img).lower()
        return img_cache[pid]

    def cb(hwnd: int, _lp: int) -> bool:
        pid = wt.DWORD(0)
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        p = pid.value
        img = image(p)
        if not img or not any(img.startswith(x) for x in IMGS):
            return True
        rect = wt.RECT()
        if not (u.IsWindowVisible(hwnd) and u.GetWindowRect(hwnd, ctypes.byref(rect))):
            return True
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < MIN_W or h < MIN_H:
            return True
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, buf, 256)
        title = buf.value or ""
        cmd = (cmds or {}).get(p) if cmds is not None else None
        if not pretend_foreign and is_ours(cmd, title, p):
            return True
        pids.add(p)
        rows.append({"pid": p, "image": img, "title": title, "w": w, "h": h, "hwnd": hwnd})
        return True

    u.EnumWindows.argtypes = [EnumProc, wt.LPARAM]
    u.EnumWindows(EnumProc(cb), 0)
    if cmds is None:
        notes.append("命令行半查不到⇒归属只按窗口标题判，认不出的窗一律当外来（保守）")
    return rows, notes


def foreground_pid() -> int:
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = wt.HWND
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wt.DWORD(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def foreign_command_hits(cmds: dict[int, str], seen_pids: set[int] | None = None
                         ) -> list[tuple[int, str, str]]:
    """B 半：别仓命令行里"下一拍就要弹窗"的形状。返回 (pid, 命令行, 种类) 三元组，
    种类只有 `"auto"`（自动化趟）与 `"app"`（会建窗的播放器实例）两家——**归哪一档由
    `classify` 定，这里不再自己决定挡不挡路**（R21 之前是这里直接吐字符串进 hits，
    于是"空闲实例"与"正在被驱动的实例"读起来一模一样，见文件头的口径修正）。
    `seen_pids` 用来避开"A 半已经按窗点过名"的那几个 pid（同一件事不许报两遍）。
    """
    hits: list[tuple[int, str, str]] = []
    for pid, cmd in (cmds or {}).items():
        if in_repo(cmd) or (seen_pids and pid in seen_pids):
            continue
        tok = re.search(FOREGROUND_TOK, cmd)
        app = re.search(APP_CMD, cmd) and not re.search(NON_WINDOW, cmd)
        if tok:
            hits.append((pid, cmd, "auto"))
        elif app:
            hits.append((pid, cmd, "app"))
    return hits


# ------------------------------------------------------------------ 挡路口径（纯函数）
# 五个旋钮＝这张表的全部自由度。每个旋钮都有一条已知答案（⑤–⑭）盯着，也各有一条注入
# （D7–D11）负责"把它调错时当场翻红"。**新增一档而不放宽**：调错的后果要么白等一晚
# （IDLE_APP_SOFT 调紧＝今晚那一档又变回挡路），要么撞车变回实发那种（其余调松）。
# 两条 fail-closed 的边界（跑证伪时才被发现，写在这里免得下一轮误读成"哨兵没牙"）：
#   DEGRADED_HARD 与 AUTO_HARD 被关掉时，那一档**落回默认保守支**，还是挡路，只是归因换了话术；
#   所以 D8/D9 那两条注入的红是**归因漂移**（⑦⑧要求点名是哪一档），而不是"红变绿"。
#   这是故意的：调错旋钮最多写错原因，绝不允许把闸门顺手变成免检金牌。
FG_HARD = True          # 外来窗**正是当前前台窗** ⇒ 挡路：主人正在用它
DEGRADED_HARD = True    # 命令行问不到 ⇒ 保守挡路（宁可白等，不许白跑一趟废账）
AUTO_HARD = True        # 同 pid 的命令行带自动化趟形状 ⇒ 挡路（哪怕它此刻在背景）
IDLE_APP_SOFT = True    # 背景里的空闲播放器实例 ⇒ 只报不挡（R21 新档，口径见文件头）
DEFAULT_HARD = True     # 对不上任何形状的可见大窗 ⇒ 保守挡路（豁免只给指名那一档）


def _win_line(r: dict) -> str:
    return (f"窗 pid={r['pid']} {r['image']}「{(r['title'] or '')[:40]}」"
            f"{r['w']}×{r['h']}")


def classify(rows: list[dict], cmds: dict[int, str] | None,
             fgp: int) -> tuple[list[str], list[str]]:
    """把"别仓的东西"分成（挡路, 只报不挡）两家。纯函数：不 EnumWindows、不起 powershell，
    所以自检能喂合成输入（真桌面上造不出"当前前台是隔壁的窗"这种可控前提）。

    为什么要有"只报不挡"这一家（2026-10-20，R21 挂看门狗当晚）：A/B 两半第一版都是
    "看见就算挡"，于是**用户自己开着放歌的椒盐音乐**（一扇 895×591 的背景窗＋一条
    `salt_player.py` 命令行，没有任何自动化趟在驱动它）把这条闸门永久按住——
    整条链要等到天亮也起不来，那等于把"你回来自己跑这几条命令"又写回纸上。
    闸门存在的理由是**两趟会弹真窗口的自动化并跑**（实发过的那种），不是"桌面上有别人的窗"：
    被测腿本来就要把自家窗顶到最前、并且量到遮挡时自己记 ENV。所以这里只降级**一种**输入
    （空闲播放器实例），其余四档一个字没动，并且每一档都有反向哨兵（⑥⑦⑧⑨⑩⑪⑫）。
    """
    hard: list[str] = []
    soft: list[str] = []
    for r in rows:
        pid = r["pid"]
        line = _win_line(r)
        cmd = None if cmds is None else cmds.get(pid)
        if FG_HARD and fgp and pid == fgp:
            hard.append(line + "（它此刻就是当前前台窗⇒主人正在用它）")
            continue
        if DEGRADED_HARD and (cmds is None or cmd is None):
            hard.append(line + "（命令行问不到／这一 pid 不在表里⇒对不上"
                               "「空闲实例」那一档，保守挡路）")
            continue
        tok = re.search(FOREGROUND_TOK, cmd or "")
        app = bool(re.search(APP_CMD, cmd or "")) and not re.search(NON_WINDOW, cmd or "")
        if tok and AUTO_HARD:
            hard.append(line + f"（同 pid 命令行带自动化趟形状 {tok.group(0)}）")
        elif app and IDLE_APP_SOFT:
            soft.append(line + "（背景里的空闲播放器实例：不是当前前台、也没有自动化趟在驱动它"
                               "⇒只报不挡；它一被驱动，B 半那条形状会当场把它升回挡路）")
        elif not DEFAULT_HARD:
            soft.append(line + "（对不上任何形状，但 DEFAULT_HARD 被调松⇒只报不挡）")
        else:
            hard.append(line + "（背景大窗，命令行既不是播放器实例也不是自动化趟⇒保守挡路）")
    seen = {r["pid"] for r in rows}
    for pid, cmd, kind in foreign_command_hits(cmds, seen):
        line = f"pid={pid}（{'自动化趟' if kind == 'auto' else '播放器实例'}）{cmd[:120]}"
        if kind == "auto" and AUTO_HARD:
            hard.append(line)
        elif kind == "app" and IDLE_APP_SOFT:
            soft.append(line + "（没查到它的窗⇒只报不挡）")
        elif pid and fgp and pid == fgp and FG_HARD:
            hard.append(line + "（它的进程就是当前前台）")
        else:
            hard.append(line + "（豁免没生效⇒保守挡路）")
    return hard, soft


def scan() -> dict:
    """一趟完整扫描。返回 {"rows","fg","cmds","b_err","notes","hard","soft","hits","b_hits"}。"""
    cmds, b_err = command_lines()
    rows, notes = visible_windows(cmds)
    fgp = foreground_pid()
    hard, soft = classify(rows, cmds, fgp)
    fg = ""
    if fgp and cmds is not None and fgp not in {r["pid"] for r in rows}:
        cmd = cmds.get(fgp)
        alive, img = proc_image(fgp)
        nm = os.path.basename(img).lower()
        if any(nm.startswith(x) for x in IMGS) and not is_ours(cmd, "", fgp):
            fg = f"前台窗 pid={fgp} {nm}"
            hard.append(fg + "（它没有上榜的大窗，但前台这一格照样挡路）")
    return {"rows": rows, "fg": fg, "cmds": cmds, "b_err": b_err, "notes": notes,
            "hard": hard, "soft": soft, "hits": hard}


def why_text(hard: list[str], soft: list[str], degraded: bool, b_err: str = "") -> str:
    """把人话组在一处（纯函数）：**挡路那一档不许把"只报不挡"盖掉**——放宽了口径之后，
    回执里必须还看得见"今晚到底有什么在背景"，否则下一次口径坏了没人发现。
    """
    if hard:
        out = f"别仓有 {len(hard)} 项在抢前台，等它收：" + " ｜ ".join(hard[:5])
        if soft:
            out += f"｜ 另有 {len(soft)} 项只报不挡：" + " ｜ ".join(soft[:3])
    else:
        out = f"挡路 0 项、只报不挡 {len(soft)} 项"
        if soft:
            out += "：" + " ｜ ".join(soft[:3])
    if degraded:
        out += f"；{b_err or '命令行查不到'}⇒身份只按窗口那一半判（降级读数）"
    elif b_err:
        out += f"；{b_err}"
    return out


def busy() -> tuple[bool, str, bool]:
    """看门狗要的那三样：（挡不挡路, 人话, 这一拍是不是"降级读数"）。

    `degraded=True` 表示归属身份这一半没能核对上（命令行查不到）。**它是结构化字段而不是
    话术**：调用方以前靠 `if "查不到" in why` 这种子串判据接它，两边措辞一分叉就静默不响
    （本仓真发过一次：④说"问不到"、闸门 match"查不到"，那条存疑回执整晚没印过）。
    """
    s = scan()
    blocked = bool(s["hard"])
    return blocked, why_text(s["hard"], s["soft"], s["cmds"] is None, s["b_err"]), \
        s["cmds"] is None


# ------------------------------------------------------------------ 已知答案：挡路口径（纯函数）
# ⑤–⑭ 喂的是合成输入，不碰真桌面："当前前台正是隔壁那扇窗"这种前提在真桌面上造不出来
# （要造就得去抢用户的前台），而口径的每一格都必须有已知答案盯着（记忆
# [[feedback-mutation-falsification-for-selftests]]：fixture 不许预写答案——这里的期望
# 是"这一格该落到哪一档"，档位本身由被测的 `classify` 现场算）。
_SALT = (r"C:\tools\pythonw.exe "
         r"C:\work\salt-player\salt_player.py")
_SALT_AUTO = (r"C:\tools\python.exe "
              r"C:\work\salt-player\tests\verify_all.py --with-device")
_TOOL = (r"C:\tools\python.exe "
         r"C:\work\tools\watch_probe.py --slug x")
_SMOKE = (r"C:\tools\python.exe "
          r"C:\work\salt-player\tests\smoke_launch_player.py")
_FPID = 4242


def _row(pid: int = _FPID) -> dict:
    """一扇**隔壁仓**的可见大窗（今晚椒盐音乐那个尺寸）。归属这一层由 `cmds` 决定，不在这里猜。"""
    return {"pid": pid, "image": "pythonw.exe",
            "title": "错过的烟火 · 周杰伦 — 椒盐音乐 - Salt Player Pro",
            "w": 895, "h": 591, "hwnd": 0x1234}


def pure_legs(verbose: bool = True) -> list[int]:
    """跑 ⑤–⑭ 那几条已知答案，返回（翻红的编号, 总条数）。"""
    res: list[tuple[int, bool, str, str]] = []

    h, s = classify([_row()], {_FPID: _SALT}, 0)
    res.append((5, (not h) and len(s) == 1 and f"pid={_FPID}" in s[0] and "只报不挡" in s[0],
                 "⑤背景里的空闲播放器实例（有窗、不是当前前台、命令行只有 `*_player.py` 那一形状）"
                 "⇒ 必须**只报不挡**、而且只报**一遍**（A 半点过名的 pid 不许在 B 半再算一次）："
                 "这就是今晚把整条链按住的那一档，算成挡路＝等到天亮也起不来",
                 f"挡路={h} 只报={s}"))

    h, s = classify([_row()], {_FPID: _SALT}, _FPID)
    res.append((6, len(h) == 1 and "当前前台窗" in h[0] and not s,
                 "⑥**反向哨兵**：同一扇窗、同一份命令行，只要它成了当前前台窗就必须升回挡路"
                 "（主人在用它）——这条不响，就分不清『口径变准』与『守卫被松』",
                 f"挡路={h} 只报={s}"))

    h, s = classify([_row()], {_FPID: _SALT_AUTO}, 0)
    res.append((7, len(h) == 1 and "自动化趟形状" in h[0] and not s,
                 "⑦**反向哨兵**：同一 pid 换成带 `verify_all --with-device` 的命令行 ⇒ 必须挡路，"
                 "并且归因要点名是「自动化趟形状」（只红不许是撞对的：文案换了因就说明档位算错了）",
                 f"挡路={h} 只报={s}"))

    h, s = classify([_row()], None, 0)
    res.append((8, len(h) == 1 and "命令行问不到" in h[0] and not s,
                 "⑧**反向哨兵**：命令行问不到（cmds=None）时那一档必须**保守挡路**——"
                 "对不上『空闲实例』就降级放行，等于把身份核查变成免检金牌",
                 f"挡路={h} 只报={s}"))

    h, s = classify([_row()], {_FPID: _TOOL}, 0)
    res.append((9, len(h) == 1 and "保守挡路" in h[0] and not s,
                 "⑨**反向哨兵**：既不是播放器实例也不是自动化趟的一扇大窗 ⇒ 仍按老口径挡路"
                 "（豁免只给指名那一档，不许写成『认不出就算背景』）",
                 f"挡路={h} 只报={s}"))

    h, s = classify([_row()], {_FPID: _SMOKE}, 0)
    res.append((10, len(h) == 1 and "保守挡路" in h[0] and not s,
                 "⑩**反向哨兵**：`smoke_launch_player.py` 那条**故意不建窗**的对照腿不许被"
                 "`*_player.py` 那档豁免——它落在默认保守挡路，才算出豁免名单真的有边界",
                 f"挡路={h} 只报={s}"))

    h, s = classify([], {_FPID: _SALT}, 0)
    res.append((11, not h and len(s) == 1 and "没查到它的窗" in s[0],
                 "⑪只有命令行、查不到窗的空闲播放器实例（＝最小化在托盘那种）⇒ 也只报不挡："
                 "它一旦弹窗，A 半当场把它点名；被驱动时 B 半的自动化形状当场升回挡路",
                 f"挡路={h} 只报={s}"))

    h, s = classify([], {999: _SALT_AUTO}, 0)
    res.append((12, len(h) == 1 and "自动化趟" in h[0] and not s,
                 "⑫**反向哨兵**：没有窗的**自动化趟**必须挡路（它下一拍就要弹窗）——"
                 "⑪与这条是一对反向哨兵，谁把 B 半整档调松，⑫当场红",
                 f"挡路={h} 只报={s}"))

    t = why_text(["窗 pid=1（在抢）"], ["窗 pid=2（只报不挡）"], False)
    res.append((13, "在抢" in t and "只报不挡" in t and "另有 1 项" in t,
                 "⑬回执合成：**挡路那一档不许把『只报不挡』盖掉**——放宽口径之后如果看不见"
                 "背景里有什么，下一次口径坏了没人发现（顺带钉住『项数由脚本自己数』：那句"
                 "「另有 N 项」里的 N 必须等于 len(soft)）",
                 t))
    t2 = why_text([], ["窗 pid=2（只报不挡）"], True, "powershell 超时")
    res.append((14, "挡路 0 项" in t2 and "只报不挡 1 项" in t2 and "降级读数" in t2,
                 "⑭没被挡时也要把『只报不挡』**有几项**印出来（那句「1 项」必须由 len(soft) 算出来，"
                 "写死『只报不挡』四个字的话，D13 那种吞掉整段的坏法照样绿），"
                 "且 degraded=True 时那句「降级读数」必须跟着出现（这是给看门狗哨兵的那半，"
                 "不能只活在返回值里）",
                 t2))

    red: list[int] = []
    for no, ok, txt, got in res:
        if verbose:
            print(f"  [{'PASS' if ok else 'FAIL'}] {txt}\n        量到 {got}")
        if not ok:
            red.append(no)
    return red, len(res)          # 条数交给调用方现数，不写进任何一句话术里


# ------------------------------------------------------------------ 自检
def run_legs(verbose: bool = True) -> list[int]:
    """跑已知答案，返回**翻红的编号**（空表＝全按声明的期望站住）。

    ①–④ 要真窗口（证明这把尺**看得见**窗），⑤–⑭ 是纯函数（证明这把尺**分得清档**）。
    窗口那四条不占前台：造的窗都带 `WA_ShowWithoutActivating`，不 raise、不激活，只借这一拍被枚举到。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication, QWidget

    def say(s: str) -> None:
        if verbose:
            print(s)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    tag = f"deskbusy-selftest-{int(time.time() % 100000):05d}"

    def mk(w: int, h: int, title: str) -> QWidget:
        p = QWidget()
        p.setAttribute(Qt.WA_ShowWithoutActivating, True)
        p.setWindowTitle(title)
        p.setFixedSize(w, h)
        p.move(20, 20)
        p.show()
        return p

    def pump() -> None:
        for _ in range(12):
            app.processEvents()
            time.sleep(0.05)

    oks: list[bool] = []        # ①–④ 的成败按顺序记，编号由下标算（红在哪个编号＝第几条不站）
    mine = os.getpid()
    cmds, b_err = command_lines()

    def listed(pretend: bool = True) -> bool:
        return mine in {r["pid"] for r in visible_windows(cmds, pretend_foreign=pretend)[0]}

    big = mk(240, 120, tag)
    pump()
    premise = big.isVisible()
    ok1 = listed(True)
    # ② 三件事都要成立才算绿（少了任何一件，"归属规则有牙"这句话就只是好看）：
    #    a) 自己造的这扇窗在正常判据下不上榜；
    #    b) **命令行这一档单独**能认出自己人、且认不出隔壁仓——少了 b，pid 那条捷径会把
    #       命令行渠道完全遮住（本进程永远豁免，命令行坏了也照样绿），别的仓的本进程兄弟
    #       （看门狗起的探针）就会被当外人白挡；
    #    c) pid 渠道独立成立（问不到命令行时，扫描者至少不许挡自己）。
    mine_cmd = (cmds or {}).get(mine) or " ".join([sys.executable, *sys.argv])
    foreign_cmd = (r"C:\x\anaconda3\python.exe "
                   r"C:\work\salt-player\tests\verify_all.py --with-device")
    ch_pos = in_repo(mine_cmd)
    ch_neg = not in_repo(foreign_cmd)
    ch_pid = is_ours(None, "", mine)
    ok2 = (not listed(False)) and ch_pos and ch_neg and ch_pid
    how = (f"（命令行渠道：自己「{mine_cmd[:56]}」→ 落点 {cmd_paths(mine_cmd)[:1]}＝{ch_pos}；"
           f"隔壁仓那条＝{ch_neg}；pid 渠道＝{ch_pid}）")
    say(f"  [{'PASS' if premise and ok1 else 'FAIL'}] ①摘掉归属判据后，自己造的 240×120 窗必须被看见"
        f"（前提：窗真的显形={premise}）⇒ 本进程 pid={mine} 在名单里={ok1}"
        "（不在＝这把尺瞎了，此后每一个「桌面干净」都是假绿光）")
    say(f"  [{'PASS' if premise and ok2 else 'FAIL'}] ②正常判据下自己的窗不许上榜，且**命令行渠道单独**"
        f"要认得出自己人／认不出隔壁仓⇒ 上榜={listed(False)} 自己={ch_pos} 隔壁={ch_neg} pid={ch_pid}"
        f"{how}" + (f"；注：{b_err}⇒命令行没查到，窗口那一半只按标题判" if cmds is None else ""))
    oks.append(bool(premise and ok1))
    oks.append(bool(premise and ok2))

    big.close()                                  # 顺序是有讲究的：小窗那一条必须在大窗关掉之后才问，
    pump()                                       # 否则名单里那个 pid 是大窗撑着的，小窗进没进根本看不出来
    small = mk(40, 30, tag + "-small")
    pump()
    shown = small.isVisible()
    got3 = listed(True)
    ok3 = bool(shown) and not got3
    say(f"  [{'PASS' if ok3 else 'FAIL'}] ③大窗关掉、只剩 40×30 小窗（显形={shown}）时"
        f"必须**不**上榜（上榜={got3}＝阈值没牙，任何托盘件都会被算成抢前台；"
        "小窗没显形＝这条腿压根没量到，按失灵记）")
    oks.append(bool(ok3))

    small.close()
    pump()
    second = mk(300, 200, tag + "-again")
    pump()
    up = bool(second.isVisible()) and listed(True)
    second.close()
    pump()
    ok4 = up and not listed(True)
    say(f"  [{'PASS' if ok4 else 'FAIL'}] ④再造一扇 300×200：显形时上榜={up}、关掉后必须消失"
        "（不消失＝上一拍的窗会替这一拍永久挡路）")
    oks.append(bool(ok4))

    for w in (big, small, second):
        try:
            w.close()
        except BaseException:                          # noqa: BLE001
            pass
    pump()
    red = [i + 1 for i, ok in enumerate(oks) if not ok]
    pr, pt = pure_legs(verbose)
    # 条数由脚本自己数（`len(oks)+pt`）：写成字面量就得记着回来改，上一轮加 ⑤–⑭ 时正是这种数最容易漂。
    return red + pr, len(oks) + pt


def selfcheck() -> int:
    """这把尺自己先证明"看得见窗"且"分得清档"。窗口那四条（少任何一条，"桌面干净"这个读数就不作数）：
      ① 摘掉归属判据后，**自己刚造的那扇 240×120 的窗必须被看见**（阳性对照：EnumWindows 看不见、
         `IsWindowVisible` 在锁屏／息屏态恒假这类失灵，都会伪装成"没人抢前台"，然后整晚白等或整晚撞车）；
      ② 同一扇窗在**正常判据**下必须被认成自己人，并且**命令行渠道单独**要成立（正向认得出本仓的
         相对路径命令行、反向认不出隔壁仓的）：只比自己这扇窗是不够的——扫描者对自己有 pid 捷径，
         命令行渠道坏了也照样绿，那本仓的兄弟进程（看门狗起的探针）就会被当外人白挡；
      ③ 大窗关掉、只剩 40×30 的小窗时**不**许上榜（阈值有牙）。顺序是承重的：小窗那一条必须在
         大窗关掉之后才问，否则名单里那个 pid 是大窗撑着的，"小窗进没进"根本看不出来；
      ④ 另造一扇 300×200，显形时上榜、关掉后必须消失（读数不粘：上一拍的窗不许替这一拍永久挡路）。
    加上 ⑤–⑭ 那批纯函数的档位判据（本体见 `pure_legs`：正向档＋反向哨兵若干＋回执合成两条；
    条数一律由 `[FINAL]` 那行现数，话术里不抄数）。
    """
    red, total = run_legs()
    rc = 1 if red else 0
    print(f"[FINAL] desk_busy 自检：共 {total} 条已知答案，错 {len(red)} 条"
          + (f"（红在 {red}）" if red else "") + f" ⇒ rc={rc}")
    return rc


# 注入的坏尺子：每条只摘一项能力，并声明"哪几条必须翻红"（子集判：额外翻红允许，漏判不许）。
def _sticky(m) -> None:
    """把 `visible_windows` 换成"同一口径第一次的结果永久复用"——这就是"上一拍的窗替这一拍挡路"的形状。

    按 `pretend_foreign` 分槽缓存：不然这条注入会顺带把归属判据也摘了，②跟着红，
    那就分不清"红在粘"还是"红在瞎"。
    """
    real = m.visible_windows
    acc: dict[bool, list] = {}

    def fake(cmds, pretend_foreign=False):
        if pretend_foreign not in acc:
            acc[pretend_foreign] = list(real(cmds, pretend_foreign)[0])
        return acc[pretend_foreign], []

    m.visible_windows = fake


def _no_dedup(m) -> None:
    """把 B 半的"这一 pid 已被 A 半点过名"那一步摘掉⇒同一扇窗被报两遍（⑤的"只报一遍"红）。"""
    real = m.foreign_command_hits
    m.foreign_command_hits = lambda cmds, seen_pids=None: real(cmds, set())


def _drop_soft_line(m) -> None:
    """`why_text` 只印挡路那一档、把"只报不挡"整段吞掉⇒⑬⑭红（放宽之后必须还看得见现场）。"""
    real = m.why_text
    m.why_text = lambda hard, soft, degraded, b_err="": real(hard, [], degraded, b_err)


MUTANTS = (
    ("D1 归属判据恒 False",
     "②必须翻红：这一条挡的就是「本仓自己的窗把看门狗挡到时钟上限」，摘掉后自己的窗会被当外人",
     lambda m: setattr(m, "is_ours", lambda cmd, title, pid=0: False),
     [2]),
    ("D5 命令行归属恒 True（宽过头）",
     "②必须翻红：把隔壁仓也算自己人＝这道闸门永远不会挡路，撞车会变回实发那种",
     lambda m: setattr(m, "in_repo", lambda cmd: True),
     [2]),
    ("D6 命令行归属恒 False（只按子串、不解析相对路径）",
     "②必须翻红：这正是第一版栽的形状——`python tests/x.py` 里没有 lumen-player，本仓的兄弟进程会被白挡",
     lambda m: setattr(m, "in_repo", lambda cmd: False),
     [2]),
    ("D2 大小阈值收到 0×0",
     "③必须翻红：阈值没牙之后任何托盘件／隐形工具窗都会被算成抢前台",
     lambda m: (setattr(m, "MIN_W", 0), setattr(m, "MIN_H", 0)),
     [3]),
    ("D3 枚举恒交空表（这把尺瞎了）",
     "①必须翻红（④跟着红：看不见窗就不可能「显形时上榜」）——瞎尺子最舒服的表现就是「桌面干净」",
     lambda m: setattr(m, "visible_windows", lambda cmds, pretend_foreign=False: ([], ["瞎了"])),
     [1, 4]),
    ("D4 读数粘住（第一拍的名单一直留着）",
     "④必须翻红：关掉窗后本进程必须从名单消失，粘住＝上一拍替这一拍永久挡路",
     _sticky,
     [4]),
    # ---- R21 新口径的五个旋钮：每个都要求"调错时当场有已知答案翻红"（正向放宽也要有牙）----
    ("D7 前台那一档松掉（FG_HARD=False）",
     "⑥必须翻红：主人正在用的那扇外来窗不再挡路＝闸门形同虚设。今晚那次放宽动的**不是**这一档，"
     "把它一起松掉就是「守卫被松」而不是「口径变准」",
     lambda m: setattr(m, "FG_HARD", False),
     [6]),
    ("D8 命令行问不到时改判放行（DEGRADED_HARD=False）",
     "⑧必须翻红，红在**归因**（同 D9：那一档落回默认保守支，仍挡路但不再点名「命令行问不到」）——"
     "问不到身份这一格是本文件里最容易被顺手改成放行的，所以它既要有已知答案也要有注入",
     lambda m: setattr(m, "DEGRADED_HARD", False),
     [8]),
    ("D9 自动化趟那一档松掉（AUTO_HARD=False）",
     "⑦必须翻红，而且红在**归因**：旋钮关掉后那一档落回默认保守支，还是挡路，只是不再点名"
     "是「自动化趟形状」（fail-closed：调错旋钮最多写错原因，不许把闸门变成免检）。"
     "⑫不红是设计使然，不是哨兵漏了——所以这条注的期望只写 ⑦，跑出来 [7] 才对得上；"
     "真要验『自动化趟不再挡路』得同时关掉 AUTO_HARD 与 DEFAULT_HARD，那是两条注、各自有账",
     lambda m: setattr(m, "AUTO_HARD", False),
     [7]),
    ("D10 默认那档也降级（DEFAULT_HARD=False）",
     "⑨⑩必须翻红：认不出形状的窗也算背景⇒豁免从『指名的一档』变成『认不出就算』",
     lambda m: setattr(m, "DEFAULT_HARD", False),
     [9, 10]),
    ("D11 空闲实例那档调紧（IDLE_APP_SOFT=False）＝今晚那种白等",
     "⑤⑪必须翻红：背景里放歌的椒盐音乐又变成挡路⇒整条链等到天亮也起不来（这是放宽的**反方向**，"
     "所以它也要有哨兵：只许朝一个方向坏的尺子等于没尺子）",
     lambda m: setattr(m, "IDLE_APP_SOFT", False),
     [5, 11]),
    ("D12 B 半不再去重（同一扇窗报两遍）",
     "⑤必须翻红：A 半点过名的 pid 不许在 B 半再算一次——数字虚高会让人以为现场比实际更挤，"
     "进而想把它放宽",
     _no_dedup,
     [5]),
    ("D13 回执吞掉『只报不挡』（why_text 只印挡路）",
     "⑬⑭必须翻红：口径放宽之后如果看不见背景里有什么，下一次口径坏了没人发现",
     _drop_soft_line,
     [13, 14]),
)


def selfcheck_mutate() -> int:
    """证伪：逐条把坏尺子注进本模块，要求 `run_legs` 按声明的期望翻红。

    恒等注入（真尺子原样交回去）是正向对照，期望是**不翻红**——它证明「翻红」是坏尺子带来的，
    不是「重新装配一次」这个动作带来的。注入只改内存里的模块属性，不碰盘上文件
    （记忆 [[feedback-no-concurrent-file-mutating-sweeps]]：会改文件的扫不许并跑，这里根本没改）。
    """
    base = sys.modules[__name__]
    keys = ("is_ours", "in_repo", "visible_windows", "MIN_W", "MIN_H",
            "FG_HARD", "DEGRADED_HARD", "AUTO_HARD", "IDLE_APP_SOFT", "DEFAULT_HARD",
            "foreign_command_hits", "why_text")
    red_base, total = run_legs(verbose=False)
    print(f"  基线（真尺子）：共 {total} 条，错 {len(red_base)} 条 rc={1 if red_base else 0}"
          "（基线不绿的话，下面的『翻红』就没有对照，先修基线）"
          + (f" 红在 {red_base}" if red_base else ""))
    bad = 0
    for name, expect_txt, inject, must in MUTANTS:
        saved = {k: getattr(base, k) for k in keys}
        t0 = time.monotonic()
        verdict = "注了"
        red: list[int] = []
        try:
            before = {k: getattr(base, k) for k in keys}
            inject(base)
            after = {k: getattr(base, k) for k in keys}
            if after == before:                        # 注了个空操作：不许冒充「抓到」
                verdict = "NOP（注入后模块属性一字未变，这条腿不作数）"
            else:
                red, _t = run_legs(verbose=False)
        except BaseException as exc:                   # noqa: BLE001
            verdict = f"CRASH({type(exc).__name__}:{exc})"
        finally:
            for k, v in saved.items():
                setattr(base, k, v)
        covered = verdict.startswith("注了") and all(x in red for x in must)
        if not covered:
            bad += 1
        print(f"  [{'CAUGHT' if covered else 'MISSED'}] {name}⇒ 期望 {expect_txt}"
              f"；结局＝翻红 {red or '无'}（{time.monotonic() - t0:.2f}s，{verdict}）")
    # 正向对照：恒等注入必须**不**翻红。
    red_id, _t = run_legs(verbose=False)
    if red_id:
        bad += 1
    print(f"  [{'PASS' if not red_id else 'MISSED'}] D0 恒等注入（真尺子原样交回去）⇒ 上面这些条必须全绿"
          "（判成翻红就说明红是「重新装配」带来的，不是坏尺子带来的）；"
          f"结局＝{red_id or '全绿'}")
    rc = 1 if bad else 0
    print(f"[FINAL] desk_busy 证伪：共 {len(MUTANTS)} 条坏尺子 + 1 条恒等对照，"
          f"判不到的 {bad} 条 ⇒ rc={rc}")
    return rc


def claim_notes() -> list[str]:
    """只读看一眼隔壁仓自己的「桌面凭据」（`../<仓>/tools/.desk_claim`）。

    **不参与判决**，纯参考：那边也有一条"human／agent／unknown，读不到就按 human 拦"的
    fail-closed 契约（`salt-player/tools/desk_claim.py`），它的链会不会来抢前台，那格子里
    写得比我们隔着进程猜要准。读不到就一条不印——这条是加分信息，不是前提。
    """
    out: list[str] = []
    parent = os.path.dirname(ROOT)
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return out
    for nm in names:
        f = os.path.join(parent, nm, "tools", ".desk_claim")
        if nm == os.path.basename(ROOT) or not os.path.isfile(f):
            continue
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                first = (fh.readline() or "").strip()
        except OSError as exc:
            out.append(f"（参考）{nm} 的桌面凭据读不到：{type(exc).__name__}")
            continue
        out.append(f"（参考，不参与判决）{nm} 的桌面凭据＝{first[:60] or '空文件'}")
    return out


def main() -> int:
    if "--selfcheck-mutate" in sys.argv:               # sys.argv 是列表， membership 判，不会被子串误命中
        return selfcheck_mutate()
    if "--selfcheck" in sys.argv:
        return selfcheck()
    ok, why, degraded = busy()
    print(f"{'[挡路]' if ok else '[能开跑]'} {why}")
    for line in claim_notes():
        print(line)
    return 3 if ok else 0


if __name__ == "__main__":
    raise SystemExit(main())
