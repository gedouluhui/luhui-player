"""端到端：按「打开方式」里那条命令原样执行一遍，证明选了我们真能把文件播起来。

它证明的是什么（用户原话：右键视频 → 打开方式里根本没这个播放器；选不了、也就播不了）：
1. HKCU 里我们那条 ProgID 的 `shell 下的 open 下的 command` **确实存在、且能按系统的 argv 规则拆成
   一行可执行命令**——资源管理器在你点列表中那一项时，执行的就是这一行；
2. 把 `%1` 换成一个真视频的绝对路径后，**这个命令行真能把播放器拉起来**：新起
   pythonw(open_file.py) → 新起 mpv.exe → 顶层窗口标题带上那个文件名；
3. 那块窗口区域**真的在出画面像素**（不是黑屏空壳）；
4. 完事只按显式 PID 收拾自己起的进程，别人家一个不碰；
5. 而且**这一趟不碰用户真正的 `data\\`**：起进程前把 `GELU_DATA` 指到临时目录并预置
   `mute=true`＋`volume=0`，跑完再拿"用户那本 recent／watch-state／留痕／缩略图缓存"的
   开跑前后指纹对账。这一条是 2026-10-17 自查补的：上一版没隔离，实测把测试样本写进了
   用户的「最近播放」与观看进度、缩略图缓存多出三格、留痕里多了两行测试记录，
   而样本带 aac 音轨、用户真配置 volume=200 —— 那等于在用户的声卡上放了一趟声。
   静音不靠"配置里写了"，靠从**被测实例自己的内核**读回 `mute`/`volume` 才算数。

它**不**做的事：不点对话框、不写注册表、不动默认程序。"列表里看得见这一项"由
`tests/e2e_openwith_dialog.ps1`（UIA 把列表项的名字逐个抄出来）负责；两路合起来
才是"选得了 + 播得起"。

跑法：
    python tests/e2e_openwith.py [样本绝对路径]
    python tests/e2e_openwith.py --expect-red 不存在的路径   # 反向对照：这本账必须翻红

退出码：0 全绿；1 至少一条真失败；2 环境不给（屏不出像素 / 前置条件不成立 / 读不到注册表）。
环境红一律记 ENV，不许冒充产品红，也不许因为"环境不给"就顺手放宽判据。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import datetime
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time

BS = chr(92)                                   # 路径分隔符不写成字面量：见 check_openwith.py 第 1 条
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
GEN = os.path.join(ROOT, "packaging", "make_launchers.py")

psutil = None
PSUTIL_ERR = ""
try:
    import psutil                              # type: ignore
except BaseException as _exc:
    PSUTIL_ERR = f"{type(_exc).__name__}: {_exc}"

winreg = None
WINREG_ERR = ""
try:
    import winreg                               # type: ignore
except BaseException as _exc:
    WINREG_ERR = f"{type(_exc).__name__}: {_exc}"

# 用户真正的数据目录落在哪儿，只认 app.config 那一个真源（自己拼 ROOT\data 的话，
# 哪天 DATA_DIR 换一个地方，"这一趟没写脏用户数据"那条判据就会去量一个没人在用的目录）。
cfgmod = None
APP_ERR = ""
try:
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from app import config as cfgmod            # type: ignore
except BaseException as _exc:                   # noqa: BLE001
    APP_ERR = f"{type(_exc).__name__}: {_exc}"

# 起实例那套把手（临时数据目录＋静音预置＋问内核）共用 player_harness 那一份：
# 抄第二遍的时候坑会跟着抄，判据却会分叉（e2e_instance_handoff / e2e_gpu 用的是同一份）。
ph = None
PH_ERR = ""
try:
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import player_harness as ph                 # type: ignore
except BaseException as _exc:                   # noqa: BLE001
    PH_ERR = f"{type(_exc).__name__}: {_exc}"

ml = None
GEN_ERR = ""
try:
    # 按**文件路径**加载生成器：`packaging` 在本机 site-packages 里是个真包（anaconda 装了
    # packaging 库），`from packaging import ...` 会绑到那边去。
    _spec = importlib.util.spec_from_file_location("gelu_make_launchers_for_e2e", GEN)
    ml = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(ml)               # type: ignore
except BaseException as _exc:
    GEN_ERR = f"{type(_exc).__name__}: {_exc}"

_S32 = ctypes.windll.shell32
_S32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
_S32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_U32 = ctypes.windll.user32
_U32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
_U32.GetWindowTextW.restype = ctypes.c_int
_U32.GetWindowTextLengthW.argtypes = [wt.HWND]
_U32.GetWindowTextLengthW.restype = ctypes.c_int
_U32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_U32.GetWindowRect.restype = wt.BOOL
_U32.IsWindowVisible.argtypes = [wt.HWND]
_U32.IsWindowVisible.restype = wt.BOOL
_U32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_U32.GetWindowThreadProcessId.restype = wt.DWORD
_U32.WindowFromPoint.restype = wt.HWND
_U32.WindowFromPoint.argtypes = [wt.POINT]
_U32.GetAncestor.restype = wt.HWND
_U32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
_ENUM_CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.LPARAM, wt.LPARAM)
# 必须在取任何窗口矩形之前声明 DPI 感知：本机 150% 缩放，非感知进程拿到的 GetWindowRect
# 是被虚拟化过的坐标，直接拿去 cut 屏幕（屏幕抓的是设备像素）就会错开一整块——
# 实测第一版就裁歪了，图里左上一角是别人的桌面。
_U32.SetProcessDPIAware.restype = wt.BOOL
_DPI_AWARE = bool(_U32.SetProcessDPIAware())

RESULTS: list[list[str]] = []                  # [tag, 判据, 证据]，tag ∈ PASS/FAIL/ENV/NOTE/FINAL
OUT_PATH = ""


def say(tag: str, what: str, why: str = "") -> None:
    RESULTS.append([tag, what, why])
    line = f"[{tag}] {what}" + (f" —— {why}" if why else "")
    print(line, flush=True)
    if OUT_PATH:
        try:
            with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def count(tag: str) -> int:
    return sum(1 for r in RESULTS if r[0] == tag)


def _append(line: str) -> None:
    """往日志文件再补一行（反向对照那条分支用；正常分支自己内联写了 DONE）。"""
    if not OUT_PATH:
        return
    try:
        with open(OUT_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + chr(10))
    except OSError:
        pass


def kp(*parts: str) -> str:
    return BS.join(parts)


def reg_open_command() -> tuple[str | None, str]:
    """读 HKCU 里我们那条 ProgID 的 shell 下的 open 下的 command 默认值（只读，不写）。"""
    if winreg is None or ml is None:
        return None, (WINREG_ERR or GEN_ERR or "读不了")
    sub = kp("Software", "Classes", ml.PROGID, "shell", "open", "command")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as k:
            v, _t = winreg.QueryValueEx(k, "")
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return (v if isinstance(v, str) else None), sub


def where_label() -> str:
    return "HKCU" + BS + kp("Software", "Classes", getattr(ml, "PROGID", "?"),
                            "shell", "open", "command")


def argv_of(cmdline: str) -> tuple[list[str], str]:
    """用系统自己的 CommandLineToArgvW 拆这行——拆法必须和资源管理器一致，不自写引号规则。"""
    argc = ctypes.c_int(0)
    arr = _S32.CommandLineToArgvW(cmdline, ctypes.byref(argc))
    if not arr:
        return [], f"CommandLineToArgvW 返空（GetLastError={ctypes.GetLastError()}）"
    try:
        return [arr[i] for i in range(argc.value) if arr[i] is not None], ""
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(arr, ctypes.c_void_p))


PY_NAMES = ("pythonw.exe", "python.exe")   # 只有这两种壳才可能是"播放器实例"


def cmd_pids(needle: str, names: tuple[str, ...] = ()) -> list[tuple[int, str]]:
    """命令行里含 needle 的进程（含命令行），本进程一律排除。

    `names` 不是可选的修饰：这台机器上跑一趟本文件，套在外面的 bash 包装器命令行里
    就带着 "open_file.py" 这串字（实测 pid 15448/27752/32224 三条 bash 一起命中）。
    不按可执行文件名筛，前置条件那条会把"别人的壳"读成"播放器已经在跑"，直接假 ENV 拒跑。
    """
    out: list[tuple[int, str]] = []
    me = os.getpid()
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = int(p.info["pid"])
            if pid == me:
                continue
            nm = (p.info.get("name") or "").lower()
            if names and nm not in names:
                continue
            cl = " ".join(p.info.get("cmdline") or [])
            if needle.lower() in cl.lower():
                out.append((pid, cl))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def hwnds_of(pid: int) -> list[int]:
    """那个进程的可见、有标题的顶层窗口句柄（EnumWindows，只读）。"""
    found: list[int] = []
    box = wt.DWORD(0)

    def cb(h, _l):
        try:
            box.value = 0
            _U32.GetWindowThreadProcessId(h, ctypes.byref(box))
            if int(box.value) == pid and _U32.IsWindowVisible(h) and _U32.GetWindowTextLengthW(h) > 0:
                found.append(int(h))
        except BaseException:
            pass
        return True                              # 继续枚举

    _U32.EnumWindows(_ENUM_CB(cb), 0)
    return found


def win_text_and_rect(h: int) -> tuple[str, tuple[int, int, int, int]]:
    n = _U32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 2)
    _U32.GetWindowTextW(h, buf, n + 1)
    r = wt.RECT()
    _U32.GetWindowRect(h, ctypes.byref(r))
    return buf.value, (r.left, r.top, r.right, r.bottom)


def sample_pixels(img, step: int = 7) -> dict:
    mn, mx, buckets, total, n = 255, 0, set(), 0, 0
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            c = img.pixelColor(x, y)
            v = (c.red() + c.green() + c.blue()) // 3
            mn, mx = min(mn, v), max(mx, v)
            buckets.add((c.red() // 24, c.green() // 24, c.blue() // 24))
            total += v
            n += 1
    return {"min": mn, "max": mx, "distinct": len(buckets),
            "mean": (total / n) if n else 0.0, "w": img.width(), "h": img.height()}


EXPECT_RED = "--expect-red" in sys.argv        # 反向对照：故意喂坏样本，这本账要求"必须红"


def real_data_state() -> dict:
    """用户真正那个 `data\\` 的指纹：两份配置 md5、留痕逐行、缩略图缓存格数。

    为什么正好数这四样：本趟起的是**真产品**，而它历史上写脏过的就是这四样——
    2026-10-17 自查时实测到：`recent` 里多一条测试样本、`watch-state.json` 多一条、
    `cache/thumbs` 多三格、`logs/launch.log` 多两行（那一趟还在用户 volume=200 的真配置上
    放了 8 秒带 aac 音轨的样本）。这四样对账，"测试没冒充用户"就不是一句承诺而是一个数。
    """
    d = cfgmod.DATA_DIR if cfgmod else os.path.join(ROOT, "data")
    out: dict = {"dir": d, "files": {}, "launch_rows": [], "thumb_dirs": -1}
    for name in ("config.json", "watch-state.json"):
        p = os.path.join(d, name)
        try:
            with open(p, "rb") as fh:
                out["files"][name] = hashlib.md5(fh.read()).hexdigest()
        except BaseException as exc:            # noqa: BLE001 - 读不到就记原因，不冒充"没变"
            out["files"][name] = f"读不到:{type(exc).__name__}"
    p = os.path.join(d, "logs", "launch.log")
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            out["launch_rows"] = [l.rstrip() for l in fh if l.strip()]
    except BaseException:                       # noqa: BLE001
        out["launch_rows"] = []                 # 还没有这本账＝ 0 行，不是"读不到"
    t = os.path.join(d, "cache", "thumbs")
    try:
        out["thumb_dirs"] = len(os.listdir(t))
    except BaseException:                       # noqa: BLE001
        out["thumb_dirs"] = 0
    return out


def run() -> int:
    global OUT_PATH
    rest = [a for a in sys.argv[1:] if a != "--expect-red"]
    sample = rest[0] if rest else os.path.join(
        ROOT, "tests", "samples", "gelu-sample-3.mkv")
    OUT_PATH = os.path.join(ROOT, "tests", "out",
                            "e2e-openwith-" + datetime.datetime.now().strftime("%m%d-%H%M%S") + ".log")
    say("NOTE", "日志落这里", OUT_PATH)

    # ---- 环境闸门：缺哪样就记 ENV，直接退，不许拿"没测"冒充"测过" -------------------------
    if ml is None:
        say("ENV", "生成器加载（取 PROGID 用）", GEN_ERR)
        return finish(2)
    if psutil is None:
        say("ENV", "进程核对能力（psutil）", PSUTIL_ERR)
        return finish(2)
    if winreg is None:
        say("ENV", "读注册表的能力（winreg）", WINREG_ERR)
        return finish(2)
    if not os.path.isfile(sample) and not EXPECT_RED:
        say("ENV", "样本文件在不在", sample)
        return finish(2)
    if EXPECT_RED:
        say("NOTE", "反向对照这一趟", f"故意喂不存在的路径 {sample!r}，这本账要求至少红一条")
    sample = os.path.abspath(sample)
    say("NOTE", "样本", f"{sample}（"
        f"{os.path.getsize(sample) if os.path.isfile(sample) else '盘上没有这个文件'} 字节）")

    # ---- ① 注册表里那条命令：读得出、拆得开、落点是真 exe、带 %1 --------------------------
    cmd, sub = reg_open_command()
    if not cmd:
        say("FAIL", "HKCU 里 ProgID 的 shell 下的 open 下的 command 读得出默认值", f"{where_label()}：{sub}")
        return finish(1)
    say("PASS", "HKCU 里那条 shell 下的 open 下的 command 读得出来",
        "HKCU" + BS + sub + " ＝ " + repr(cmd))

    argv, why = argv_of(cmd)
    if not argv:
        say("FAIL", "那行命令能按系统的 argv 规则拆开", why)
        return finish(1)
    exe = argv[0]
    if not os.path.isfile(exe):
        say("FAIL", "命令第一段是盘上真存在的 exe（不是 pythonw，也不是脚本自己）",
            f"第一段={exe!r}；整段={argv}")
        return finish(1)
    say("PASS", "命令第一段是盘上真存在的 exe（不是 pythonw，也不是脚本自己）",
        f"{exe}；argv={argv}")
    has_pct1 = any("%1" in a for a in argv[1:])
    say("PASS" if has_pct1 else "FAIL", "命令里带 %1（文件参数落点）", f"argv[1:]={argv[1:]}")
    if not has_pct1:
        return finish(1)

    # ---- ② 前置条件：本机没有播放器实例在跑（否则 app.main 会"转发给旧实例"再退出）--------
    already = [h[0] for h in cmd_pids("open_file.py", PY_NAMES)]
    if already:
        say("ENV", "前置条件：本机没有另一个播放器实例在跑（这轮测的是冷启动接文件）",
            f"已经有 pid {already}，那条路走的是「转发给旧实例」，这一轮不作数")
        return finish(2)
    say("PASS", "前置条件：本机没有另一个播放器实例在跑（这轮测的是冷启动接文件）",
        "命令行里含 open_file.py 的进程：0 个")

    # ---- ②.5 数据目录隔离：这一趟起的是**真产品**，绝不能写进用户真正的 data\ -------------
    #   实测过的后果（2026-10-17 自查）：上一版没设 GELU_DATA，于是测试样本被塞进用户的
    #   recent 与 watch-state、缩略图缓存多出三格、launch.log 里留下测试行；更要紧的是
    #   样本带 aac 音轨、用户真配置是 volume=200 且没静音——等于凌晨在用户的声卡上放了一趟声。
    #   现在把 GELU_DATA 指到临时目录并预置 mute=true＋volume=0（配方取自 player_harness，
    #   与 e2e_instance_handoff 用的是同一份），稍后再从**被测实例自己的内核**把这两个值读回来：
    #   配置写着静音 ≠ 内核真的静音，中间隔着界面初始化那一截。
    if ph is None or cfgmod is None:
        say("ENV", "隔离数据目录的前提（player_harness 与 app.config 起得来）",
            (PH_ERR or APP_ERR) or "未知原因")
        return finish(2)
    data_tmp = os.path.join(tempfile.gettempdir(), f"gelu-e2e-openwith-{os.getpid()}")
    child_env = ph.make_env(data_tmp, {"mute": True, "volume": 0})
    base_state = real_data_state()
    say("NOTE", "这一趟的数据落点（不是用户真目录）",
        f"{data_tmp}；用户真目录 {base_state['dir']} 开跑前指纹："
        f"config={base_state['files'].get('config.json','')[:8]} "
        f"watch={base_state['files'].get('watch-state.json','')[:8]} "
        f"留痕 {len(base_state['launch_rows'])} 行 缓存 {base_state['thumb_dirs']} 格")

    # ---- ③ 就按资源管理器那一行执行 ------------------------------------------------------
    run_argv = [exe] + [a.replace("%1", sample) for a in argv[1:]]
    base_procs = {h[0] for h in cmd_pids("open_file.py", PY_NAMES)}
    t0 = time.time()
    try:
        sp = subprocess.Popen(run_argv, cwd=ROOT, env=child_env,
                              creationflags=0x00000008)  # DETACHED_PROCESS
    except BaseException as exc:
        say("FAIL", "按注册表那行命令起进程", f"{type(exc).__name__}: {exc}")
        return finish(1)
    say("NOTE", "已把启动器丢出去（它自己只负责转手，立刻退）",
        f"launcher pid={sp.pid} 命令行={run_argv}")

    app_pid, app_cmd = 0, ""
    while time.time() < t0 + 90.0:
        fresh = [hit for hit in cmd_pids("open_file.py", PY_NAMES) if hit[0] not in base_procs]
        if fresh:
            app_pid, app_cmd = fresh[0][0], fresh[0][1]
            break
        time.sleep(0.5)
    if not app_pid:
        say("FAIL", "启动器真的把 pythonw(open_file.py) 拉起来了（进程层）",
            f"等了 {time.time() - t0:.1f}s，新进程 0 个；基线={sorted(base_procs)}")
        return finish(1)
    say("PASS", "启动器真的把 pythonw(open_file.py) 拉起来了（进程层）",
        f"pid={app_pid}：{app_cmd}")

    # ---- ④ 窗口标题带上那个文件名 = 文件真的交给了播放器 ---------------------------------
    base = os.path.basename(sample)
    hwnd, title, rect = 0, "", (0, 0, 0, 0)
    deadline = time.time() + 90.0
    while time.time() < deadline:
        for h in hwnds_of(app_pid):
            t, rc = win_text_and_rect(h)
            if base in t:
                hwnd, title, rect = h, t, rc
                break
        if hwnd:
            break
        time.sleep(0.5)
    if not hwnd:
        for h in hwnds_of(app_pid):              # 退一步：把看到的都摊出来好诊断
            t, rc = win_text_and_rect(h)
            if not hwnd:
                hwnd, title, rect = h, t, rc
        say("FAIL", "顶层窗口标题带上那个文件名（说明文件真的交给了播放器）",
            f"hwnd={hex(hwnd) if hwnd else '无'} 标题={title!r} 要找={base!r} 矩形={rect}")
    else:
        say("PASS", "顶层窗口标题带上那个文件名（说明文件真的交给了播放器）",
            f"hwnd={hex(hwnd)} 标题={title!r} 矩形={rect}")

    # ---- ⑤ mpv 内核真的起来了，而且**真的在往前播那个文件** -------------------------------
    mpv: list[tuple[int, str, str]] = []
    try:
        for c in psutil.Process(app_pid).children(recursive=True):
            nm = (c.name() or "")
            cl = " ".join(c.cmdline())
            if nm.lower() == "mpv.exe" or "--wid" in cl:
                mpv.append((int(c.pid), nm, cl))
    except BaseException as exc:
        say("NOTE", "列子进程", f"{type(exc).__name__}: {exc}")
    say("PASS" if mpv else "FAIL", "mpv 内核实起来了（子进程层）",
        f"mpv={[(p, n) for p, n, _c in mpv]}")

    # 这一条是整趟最硬的一条：绕过界面，直接问内核"你手上放的是哪个文件、走到几秒了"。
    # 界面标题能被缓存骗，进程能被空壳骗，只有内核自己报的 time-pos 在往前走，
    # 才说明"从打开方式接过来的那个文件"真的在解码播放。
    pipe = ""
    for _p, _n, cl in mpv:
        for tok in cl.split():
            if tok.startswith("--input-ipc-server="):
                pipe = tok.split("=", 1)[1]
    if not pipe:
        say("NOTE", "问不到内核（mpv 没起来＝也不会出声），这一条腿改由像素兜",
            f"mpv={[(p, n) for p, n, _c in mpv]}")
    else:
        try:
            if ROOT not in sys.path:
                sys.path.insert(0, ROOT)
            from app.engine.ipc import MpvIpc      # 用项目自己的 IPC 客户端，不再写第二套协议
            cli = MpvIpc(pipe)
            cli.connect(timeout=10.0)
            p1 = cli.request(["get_property", "playback-time"], timeout=4.0)
            path = cli.request(["get_property", "path"], timeout=4.0)
            paused = cli.request(["get_property", "pause"], timeout=4.0)
            time.sleep(2.5)
            p2 = cli.request(["get_property", "playback-time"], timeout=4.0)
            vid = cli.request(["get_property", "vid"], timeout=4.0)
            mute = cli.request(["get_property", "mute"], timeout=4.0)
            vol = cli.request(["get_property", "volume"], timeout=4.0)
            cli.close()
            same = bool(path) and os.path.normcase(str(path)) == os.path.normcase(sample)
            say("PASS" if same else "FAIL",
                "内核自己报的 path 就是那个样本（不是别的文件、不是空列表）",
                f"path={path!r} 要找={sample!r}")
            moving = (isinstance(p1, (int, float)) and isinstance(p2, (int, float))
                      and p2 > p1 and paused is False and vid not in (None, False))
            say("PASS" if moving else "FAIL",
                "内核报的播放位置在往前走、pause 为假、vid 轨道已选中（真在播，不是停在封面）",
                f"playback-time {p1!r} → {p2!r}（差 "
                f"{(p2 - p1) if isinstance(p1, (int, float)) and isinstance(p2, (int, float)) else '量不出'}）"
                f" pause={paused!r} vid={vid!r}")
            silent = ph.silent_now({"mute": mute, "volume": vol})
            say("PASS" if silent else "FAIL",
                "这一趟不会出声：mute/volume 从**被测实例自己的内核**读回（不是读那份配置）",
                f"mute={mute!r} volume={vol!r}；隔离目录里预置的是 mute=true＋volume=0，"
                f"真配置那边是 volume=200——读回来的必须是前者")
            gate_bad = ph.silent_gate_selftest()
            say("PASS" if not gate_bad else "FAIL",
                f"静音判据自带 {len(ph.SILENT_CASES)} 档已知好坏对照（不许恒真/恒假）",
                "；".join(gate_bad) or "每档都判对")
        except BaseException as exc:
            say("ENV", "问内核要属性这条腿跑不动（连不上管道 / mpv 不接第二个客户端）",
                f"{type(exc).__name__}: {exc}；pipe={pipe!r}")

    # ---- ⑥ 画面真的在出像素（外加"没被别人盖住"这道闸门）--------------------------------
    env_no_pixels = False
    try:
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        say("NOTE", "量具自己的 DPI 感知声明（不声明的话 150% 缩放下窗口矩形会被虚拟化，"
            "裁出来的就是别人桌面的左上角——第一版就是这么歪的）",
            f"SetProcessDPIAware 返回 {int(_DPI_AWARE)}")
        full = app.primaryScreen().grabWindow(0).toImage()
        st_all = sample_pixels(full, 37)
        if not (st_all["distinct"] >= 2 and st_all["max"] > 8):
            say("ENV", "整屏在出像素（后面那条像素判据的前提）",
                f"整屏 {st_all['w']}×{st_all['h']} 亮度{st_all['min']}~{st_all['max']} "
                f"色桶{st_all['distinct']} —— 屏不给像素，像素判据不作数")
            env_no_pixels = True
        else:
            say("PASS", "整屏在出像素（像素判据的前提成立）",
                f"整屏 {st_all['w']}×{st_all['h']} 亮度{st_all['min']}~{st_all['max']} 色桶{st_all['distinct']}")
            if hwnd and rect[2] > rect[0] and rect[3] > rect[1]:
                wx, wy = rect[0], rect[1]
                ww, wh = rect[2] - rect[0], rect[3] - rect[1]
                # 只抠画面带：避开顶部标题条与底部控制条，横向偏左（侧栏是从右边盖出来的，
                # 开着的时候右半不是画面）。
                x = wx + int(ww * 0.06)
                y = wy + int(wh * 0.13)
                cw = int(ww * 0.42)
                ch = int(wh * 0.45)
                hit = _U32.WindowFromPoint(wt.POINT(int(wx + ww // 2), int(wy + wh // 2)))
                chain = []
                hcur = int(hit or 0)
                while hcur and len(chain) < 12:
                    chain.append(hcur)
                    hcur = int(_U32.GetAncestor(wt.HWND(hcur), 1) or 0)   # GA_PARENT
                top_ok = hwnd in chain
                if not top_ok:
                    say("ENV", "那块窗口区域里有画面（像素这条腿）",
                        f"播放器窗口中心点最上面是 {hex(int(hit or 0))}，不在自家窗口链 "
                        f"{[hex(h) for h in chain]} 里 —— 别人的窗口压上来了，这是环境不是产品")
                    env_no_pixels = True
                else:
                    crop = app.primaryScreen().grabWindow(0, x, y, cw, ch).toImage()
                    st = sample_pixels(crop)
                    good = st["distinct"] >= 6 and st["max"] > 8 and st["w"] >= 32 and st["h"] >= 32
                    say("PASS" if good else "FAIL",
                        f"画面带（窗口内 {cw}×{ch} 设备像素，已避开标题条/控制条/右侧栏）里有画面",
                        f"色块数={st['distinct']} 亮度{st['min']}~{st['max']} 均值{st['mean']:.0f} "
                        f"窗口矩形={rect} 遮挡闸门=自家在最上面")
                    png = OUT_PATH[:-4] + "-window.png"
                    try:
                        crop.save(png)
                        say("NOTE", "像素留档", png)
                    except BaseException as exc:
                        say("NOTE", "像素留档没写成", f"{type(exc).__name__}: {exc}")
    except BaseException as exc:
        say("ENV", "像素这条腿跑不动（Qt 起不来之类）", f"{type(exc).__name__}: {exc}")
        env_no_pixels = True

    # ---- ⑦ 只按显式 PID 收拾自己起的那两三个进程 ------------------------------------------
    targets = [app_pid] + [p for p, _n, _c in mpv]
    killed: list[int] = []
    for pid in targets:
        try:
            p = psutil.Process(pid)
            cl = " ".join(p.cmdline())
            if ("open_file.py" not in cl) and ((p.name() or "").lower() != "mpv.exe"):
                say("NOTE", f"跳过 pid={pid}：命令行里认不出自家，不动它", cl[:140])
                continue
            p.terminate()
            killed.append(pid)
        except psutil.NoSuchProcess:
            pass
        except BaseException as exc:
            say("NOTE", f"收拾 pid={pid} 时报", f"{type(exc).__name__}: {exc}")
    time.sleep(3.0)
    for pid in [p for p in killed if psutil.pid_exists(p)]:
        try:
            psutil.Process(pid).kill()
        except BaseException:
            pass
    time.sleep(1.0)
    still = [p for p in killed if psutil.pid_exists(p)]
    say("PASS" if not still else "FAIL",
        "收尾只按显式 PID 收拾自己起的进程，而且收拾干净了",
        f"terminate={killed} 仍在={still}（别人家的进程一个没碰；本趟开始前在跑的 pid 不在名单里）")

    # ---- ⑧ 用户真正的 data\ 一个字节都不许多出来 ------------------------------------------
    #   这是本条链自己的账，不是产品的功能：产品写自己的目录天经地义，测试写用户的就不行。
    #   判红之前先分清"是谁写的"——新长出来的行里带着本趟那个样本才认是我们自己脏的，
    #   否则这一分钟里可能是用户本人也双击了文件，那要记 ENV 并把原话摊出来，不许记产品红。
    after_state = real_data_state()
    diffs: list[str] = []
    for k in after_state["files"]:
        if after_state["files"][k] != base_state["files"].get(k):
            diffs.append(f"{k} md5 {str(base_state['files'].get(k))[:8]}→"
                         f"{str(after_state['files'][k])[:8]}")
    if len(after_state["launch_rows"]) != len(base_state["launch_rows"]):
        new = after_state["launch_rows"][len(base_state["launch_rows"]):]
        diffs.append(f"launch.log 多 {len(new)} 行：" + " ‖ ".join(l[:90] for l in new))
    if after_state["thumb_dirs"] != base_state["thumb_dirs"]:
        diffs.append(f"缩略图缓存 {base_state['thumb_dirs']}→{after_state['thumb_dirs']} 格")
    ours = any(sample.lower() in d.lower() or "gelu-sample" in d.lower() for d in diffs)
    if not diffs:
        say("PASS", "这一趟没往用户真正的 data\\ 写任何东西"
            "（recent／watch-state／留痕／缩略图四样开跑前后对账）",
            f"config={after_state['files'].get('config.json','')[:8]} "
            f"watch={after_state['files'].get('watch-state.json','')[:8]} "
            f"留痕 {len(after_state['launch_rows'])} 行 缓存 {after_state['thumb_dirs']} 格"
            "，与开跑前逐字相同")
    else:
        say("FAIL" if ours else "ENV", "这一趟没往用户真正的 data\\ 写任何东西"
            "（recent／watch-state／留痕／缩略图四样开跑前后对账）",
            ("是我们自己写脏的：" if ours else "看不出是本趟写的（这一分钟里可能用户本人也打开了文件）：")
            + "；".join(diffs)[:400])

    # 留痕那一本账必须落在**隔离目录**里：这样"上次外部打开收到了什么"才是用户自己的历史，
    # 而不是被测试反复覆写的流水（doctor 那条自检读的就是这一本）。
    tmp_rows: list[str] = []
    try:
        with open(os.path.join(data_tmp, "logs", "launch.log"),
                  encoding="utf-8", errors="replace") as fh:
            tmp_rows = [l.rstrip() for l in fh if l.strip()]
    except BaseException as exc:                # noqa: BLE001
        tmp_rows = []
        say("NOTE", "隔离目录里那本留痕读不到", f"{type(exc).__name__}")
    ok_tmp = bool(tmp_rows) and any(sample.lower() in l.lower() or "argv" in l for l in tmp_rows)
    say("PASS" if ok_tmp else ("ENV" if EXPECT_RED else "FAIL"),
        "启动留痕写到了隔离目录那一本（用户那本才是他自己的历史）",
        f"{len(tmp_rows)} 行：" + " ‖ ".join(l[:110] for l in tmp_rows[-2:]))
    shutil.rmtree(data_tmp, ignore_errors=True)
    say("NOTE", "隔离目录已回收", f"{data_tmp} 还在不在＝{os.path.exists(data_tmp)}")

    rc = 1 if count("FAIL") else (2 if env_no_pixels or count("ENV") else 0)
    return finish(rc, t0)


def finish(rc: int, t0: float | None = None) -> int:
    if EXPECT_RED:                               # 反向对照：判据必须翻红，全绿才是这本账的失败
        ok = count("FAIL") >= 1
        say("FINAL", f"反向对照：{'成立（判据真的会红，不是恒绿）' if ok else '失败（喂了坏样本还全绿＝量具是瞎的）'}",
            f"FAIL {count('FAIL')} 条／ENV {count('ENV')} 条 rc={0 if ok else 1}")
        print("DONE", flush=True)
        _append("DONE")
        return 0 if ok else 1
    tag = "全绿" if rc == 0 else ("有真失败" if rc == 1 else "环境不给")
    dur = f"耗时 {time.time() - t0:.1f}s" if t0 else "没走到起进程那步"
    say("FINAL", f"端到端：{tag} —— PASS {count('PASS')}／FAIL {count('FAIL')}／"
        f"ENV {count('ENV')}／NOTE {count('NOTE')}", f"{dur} rc={rc}")
    print("DONE", flush=True)
    if OUT_PATH:
        try:
            with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write("DONE\n")
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    try:
        sys.exit(run())
    except SystemExit:                           # sys.exit() 不是崩溃：别把它记成"量具自己崩了"
        raise
    except BaseException:
        import traceback
        print("TRACEBACK（这一趟不作数）:", flush=True)
        traceback.print_exc()
        say("FINAL", "量具自己崩了 —— 端到端不作数", "rc=2")
        print("DONE", flush=True)
        sys.exit(2)
