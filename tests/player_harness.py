"""真起播放器进程的那批把手（跨进程 e2e 共用一份，不在每个脚本里各抄一遍）。

为什么要有这个文件：`e2e_instance_handoff.py` 与 `e2e_gpu.py` 都要"按双击的形状起一个真实例、
找到它的 mpv 内核、隔着命名管道问属性、完事只按显式 PID 收拾"。这些把手里每一条都踩过坑
（命令行筛进程会把套在外面的 bash 壳算进来、`volume or 1` 把 0.0 当成没读到、
收拾进程必须先在命令行里认出自家），**抄第二遍的时候坑会跟着抄，判据却会分叉**。
所以判据留在各脚本里（那是各趟的账），只有"操作与取证"的把手放这儿。

这里的东西一律**不打印、不记账**：调用方自己决定某条读数算 PASS／FAIL／ENV／NOTE。
"""
from __future__ import annotations

import ctypes
import io
import json
import os
import subprocess
import sys
import time

BS = chr(92)
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OPEN_PY = os.path.join(ROOT, "packaging", "open_file.py")
PY_NAMES = ("pythonw.exe", "python.exe")       # 只有这两种壳才可能是"播放器实例"

psutil = None
PSUTIL_ERR = ""
try:
    import psutil                              # type: ignore
except BaseException as _exc:                  # noqa: BLE001 - 缺库要变成"这趟不作数"，不是崩
    PSUTIL_ERR = f"{type(_exc).__name__}: {_exc}"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
try:
    from app import config as cfgmod           # 只取目录与管道名，不起 Qt
    from app.engine.ipc import MpvIpc          # 用项目自己的 IPC 客户端，不写第二套协议
    APP_ERR = ""
except BaseException as _exc:                  # noqa: BLE001
    cfgmod = None
    MpvIpc = None
    APP_ERR = f"{type(_exc).__name__}: {_exc}"


# ---------------------------------------------------------------- 起进程
def interpreter() -> str:
    """必须用 **pythonw**：带控制台的 python 会让实例闪一个黑框出来，
    而那恰恰是"双击视频"最讨人嫌的副作用。没有 pythonw 才退回 python。"""
    cand = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return cand if os.path.exists(cand) else sys.executable


def spawn(sample: str, env: dict[str, str]) -> subprocess.Popen:
    """按"双击"的形状起一个实例：pythonw + open_file.py + 一个绝对路径。"""
    return subprocess.Popen([interpreter(), OPEN_PY, sample], cwd=ROOT, env=env,
                            creationflags=0x00000008)      # DETACHED_PROCESS


def make_env(data_dir: str, cfg: dict) -> dict[str, str]:
    """把 `GELU_DATA` 指到临时目录并预置 config.json——既隔离用户真正的设置（测试不许改），
    又是**静音**的落点。写文件用 `newline="\\n"`：Windows 上 `io.open(...,"w")` 默认会把
    整份 LF 转成 CRLF。"""
    os.makedirs(os.path.join(data_dir, "logs"), exist_ok=True)
    with io.open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8",
                 newline="\n") as fh:
        fh.write(json.dumps(cfg, ensure_ascii=False))
    return dict(os.environ, GELU_DATA=data_dir, PYTHONIOENCODING="utf-8")


# ---------------------------------------------------------------- 找进程
def player_pids(needle: str = "") -> list[tuple[int, str]]:
    """命令行里含 open_file.py 的进程（可再要求含 needle）。**本进程排除，且按可执行文件名筛**：
    这台机器上套着测试脚本的 bash 包装器命令行里就带着 "open_file.py" 这串字，
    不按 PY_NAMES 筛会把"别人的壳"读成"播放器已经在跑"（e2e_openwith 里实测过的坑）。"""
    if psutil is None:
        return []
    me = os.getpid()
    out: list[tuple[int, str]] = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = int(p.info["pid"])
            if pid == me:
                continue
            if (p.info.get("name") or "").lower() not in PY_NAMES:
                continue
            cl = " ".join(p.info.get("cmdline") or [])
            if "open_file.py" not in cl:
                continue
            if needle and needle not in cl:
                continue
            out.append((pid, cl))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def mpv_child(pid: int) -> tuple[int, str, str]:
    """那个实例名下的 mpv 内核：回 (pid, 名字, 命令行)。找不到回 (0, "", "")。"""
    if psutil is None:
        return 0, "", ""
    try:
        for c in psutil.Process(pid).children(recursive=True):
            cl = " ".join(c.cmdline())
            if (c.name() or "").lower() == "mpv.exe" or "--input-ipc-server=" in cl:
                return int(c.pid), c.name(), cl
    except BaseException:                      # noqa: BLE001 - 找不到就是找不到，不抛
        return 0, "", ""
    return 0, "", ""


def wait_child(pid: int, timeout: float) -> tuple[int, str, str]:
    t0 = time.time()
    while time.time() < t0 + timeout:
        hit = mpv_child(pid)
        if hit[0]:
            return hit
        time.sleep(0.5)
    return 0, "", ""


def pipe_of(cmdline: str) -> str:
    for tok in (cmdline or "").split():
        if tok.startswith("--input-ipc-server="):
            return tok.split("=", 1)[1]
    return ""


def wait_pid(sample: str, base: set[int], who: str, timeout: float) -> tuple[int, str]:
    """等一个**新**实例进程出现（基线里的不算）。返回 (pid, 命令行)；超时回 (0, 原因)。"""
    t0 = time.time()
    while time.time() < t0 + timeout:
        fresh = [h for h in player_pids(sample) if h[0] not in base]
        if fresh:
            return fresh[0][0], fresh[0][1]
        time.sleep(0.5)
    return 0, f"等了 {time.time() - t0:.1f}s 还没有 {who} 的实例进程"


def pipe_name() -> str:
    """单实例那根命名管道的名字（真源在 app.config，这里只是拼法）。"""
    return f"{cfgmod.BRAND_ID}-player-v1"


def pipe_listening(name: str) -> bool | None:
    """命名管道有没有人开着。判"没人"要认三个值：本机实测 `restype=c_void_p` 会把
    INVALID_HANDLE_VALUE 交成大整数而不是 None（同 app/doctor.py 里记的那条）。"""
    if sys.platform != "win32":
        return None
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = ctypes.c_void_p
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.CreateFileW(r"\\.\pipe" + BS + name, 0xC, 0, None, 3, 0, None)
    if h is None or h == -1 or h == 0xFFFFFFFFFFFFFFFF:
        return False
    k.CloseHandle(h)
    return True


# ---------------------------------------------------------------- 问内核
def ask(pipe: str, props: list[str], wait: float = 12.0) -> tuple[dict, str]:
    """问内核要几个属性。连不上不硬等：回 ({}, 原因)，由调用方判 ENV 还是 FAIL。"""
    if MpvIpc is None or not pipe:
        return {}, "没有 IPC 管道可问"
    deadline = time.time() + wait
    last = ""
    while time.time() < deadline:
        try:
            cli = MpvIpc(pipe)
            cli.connect(timeout=8.0)
            try:
                return {p: cli.request(["get_property", p], timeout=5.0) for p in props}, ""
            finally:
                cli.close()
        except BaseException as exc:           # noqa: BLE001 - 内核还没接上时要重试到窗口期结束
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(0.6)
    return {}, last


def ask_soft(pipe: str, props: list[str], wait: float = 8.0) -> tuple[dict, list[str]]:
    """逐条问：读得到的进字典，读不到的记进"读不到"清单。

    为什么要有这一条而不用 `ask()`：`ask()` 是**判据用的**，一批里有一条问不到就整批作废
    ——这正是判据要的（不许把"问不到"当成"值为 None"）。但探测与留档要的相反：
    本机实测 `--idle=yes` 空载时 `path` 回的是 `property unavailable`（不是 None，是报错），
    于是"问一批属性"会因为这**一条正常的不存在**而全批作废，看上去像"内核没接上"。
    2026-10-17 两趟探测（probe_decoder_util / probe_append_idle）都栽过一次，
    症状都是"全部读数 None＋一条也没取到"，而内核其实活得好好的。
    """
    got: dict[str, object] = {}
    missing: list[str] = []
    for p in props:
        v, why = ask(pipe, [p], wait=wait)
        if v:
            got[p] = v[p]
        else:
            missing.append(f"{p}（{(why or '')[:90]}）")
    return got, missing


def wait_props(pipe: str, props: list[str], rounds: int, gap: float,
               keep) -> tuple[dict, str, int]:
    """反复问，直到 `keep(读数)` 成立或轮数用尽。回 (最后一次读数, 最后一次失败原因, 实际轮数)。

    `keep` 由调用方给（每个脚本要等的事情不一样），这里只负责"分片地问、每片之间歇一口气"。
    """
    got: dict = {}
    why = "（还没问过）"
    n = 0
    for n in range(1, max(1, rounds) + 1):
        got, why = ask(pipe, props)
        if got and keep(got):
            return got, why, n
        time.sleep(gap)
    return got, why, n


def tell(pipe: str, command: list) -> str:
    """给内核发**一条命令**。成不成回一句人话，绝不抛出去——
    量具自己发命令失败不是产品的失败，红要红在判据上，不能红在一次 traceback。"""
    if MpvIpc is None or not pipe:
        return "没问（IPC 不可用）"
    try:
        cli = MpvIpc(pipe)
        cli.connect(timeout=6.0)
        try:
            cli.request(command, timeout=5.0)
            return "已发出"
        finally:
            cli.close()
    except BaseException as exc:               # noqa: BLE001
        return f"发不动：{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 静音判据（唯一真源）
def silent_now(props: dict) -> bool:
    """内核自己报的 mute/volume 是不是"真的不会出声"。

    两条坑都是实头发过的，所以逐档写死对照（`SILENT_CASES`）：
    • `volume or 1` 会把 **0.0 当成"没读到"**，于是"音量确实归零"被读成"没归零"——
      第一版转交 e2e 就是这样把**已经静音好**的一趟判成 ENV、整趟没踩到转交（留档 010121）；
    • 布尔判定用 `is True`：mpv 的 mute 是布尔，读成 1/0/None 时都不许算静音。
    """
    vol = props.get("volume")
    return (props.get("mute") is True and isinstance(vol, (int, float))
            and float(vol) == 0.0)


SILENT_CASES: tuple[tuple[dict, bool, str], ...] = (
    ({"mute": True, "volume": 0.0}, True, "两道都关实了"),
    ({"mute": True, "volume": 1.0}, False, "只静音、音量还开着——别处把 mute 顶掉就回到这一档"),
    ({"mute": False, "volume": 0.0}, False, "音量归零但没静音"),
    ({"mute": False, "volume": 1.0}, False, "会出声"),
    ({"mute": True}, False, "volume 没读到：宁可这趟不作数，也不赌它没声"),
    ({"mute": None, "volume": 0.0}, False, "mute 读成 None 不算静音"),
    ({"mute": 1, "volume": 0.0}, False, "非布尔的 1 不算静音（is True 那条）"),
    ({"mute": True, "volume": None}, False, "volume 读成 None 不算归零（旧写法会在这崩）"),
)


def silent_gate_selftest() -> list[str]:
    """返回漏网档位的人话列表（空＝判据八档全对）。给脚本当成一条自己的判据来记账。"""
    return [f"{p} 该判 {e} 实际 {silent_now(p)}（{w}）"
            for p, e, w in SILENT_CASES if silent_now(p) is not e]


# ---------------------------------------------------------------- 收尾
def kill_pids(pids: list[int]) -> tuple[list[int], list[int], list[tuple[int, str]]]:
    """只按**显式 PID**收拾，而且每个都要在命令行里认出自家才动（本项目的死规矩：
    禁止按镜像名全杀）。回 (已 terminate, 收拾完仍在, 跳过没动的)。"""
    gone: list[int] = []
    skipped: list[tuple[int, str]] = []
    if psutil is None:
        return gone, [], []
    for pid in pids:
        try:
            p = psutil.Process(pid)
        except BaseException:                  # noqa: BLE001 - 已经没了就不算事
            continue
        try:
            cl = " ".join(p.cmdline())
        except BaseException:                  # noqa: BLE001
            cl = ""
        if "open_file.py" not in cl and (p.name() or "").lower() != "mpv.exe":
            skipped.append((pid, cl[:150]))
            continue
        try:
            p.terminate()
            gone.append(pid)
        except BaseException:                  # noqa: BLE001
            skipped.append((pid, f"terminate 报：{p.name()}"))
    time.sleep(3.0)
    for pid in gone:
        try:
            if psutil.pid_exists(pid):
                psutil.Process(pid).kill()
        except BaseException:                  # noqa: BLE001
            pass
    time.sleep(1.0)
    still = [p for p in gone if psutil.pid_exists(p)]
    return gone, still, skipped


# ---------------------------------------------------------------- 进程侧读数
def cpu_window(pids: dict[str, int | None], window: float) -> tuple[dict[str, float], str]:
    """在 `window` 秒里量各进程 CPU（单核基准 %，psutil 的两个时刻之差）。

    psutil 的 `cpu_percent` 必须**先 priming 再取第二次**：第一次调用只是记下起点，
    当场拿到的那个数恒为 0——写成 `p.cpu_percent(True)` 会直接报错，写成一次调用取数
    就会量出一个谁也没法解释的 0（本项目 usage.py 里记过同一族坑）。
    回 ({名字: %}, 原因)；拿不到的项不进字典。
    """
    if psutil is None:
        return {}, PSUTIL_ERR or "没装 psutil：CPU 这一项没测（不是 0，是没测）"
    if window <= 0:
        raise ValueError("窗口必须大于 0 秒：CPU 百分比是两个时刻之差，单点取不出来")
    live: dict[str, object] = {}
    for name, pid in pids.items():
        if not pid:
            continue
        try:
            p = psutil.Process(pid)
            p.cpu_percent(None)                # priming：起点
            live[name] = p
        except BaseException:                  # noqa: BLE001
            continue
    time.sleep(window)
    out: dict[str, float] = {}
    for name, p in live.items():
        try:
            out[name] = float(p.cpu_percent(None))
        except BaseException:                  # noqa: BLE001
            continue
    return out, ""
