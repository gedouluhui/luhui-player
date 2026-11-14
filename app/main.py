"""装配入口：QApplication、主题、单实例、命令行参数、启动自检。

用法：
    python -m app.main [文件或文件夹...]

这一层有两件事是"用户看不见的正确"，改之前先读注释：
① **命令行参数不许静默吃掉**（`collect_paths`）。以前是 `os.path.exists` 一筛就丢、
   只往 stderr 印一句 —— 而双击/右键那条路上 `pythonw` 连个控制台都没有，那句警告
   永远不会有人看见，最后就是"播放器开出来了，界面停在『打开文件夹』那一页"。
② **单实例转交要有回执**（`forward_to_running` + `Handoff`）。写完字节就宣布成功、
   接收方那句 `if win is not None` 直接丢弃，两者合起来就是"文件没了但没人报错"。
   现在的契约：回执 = 这批路径已经被一个活着的窗口收下（或已排队、装配完立刻补交）。
判据与证伪：`python tests/test_instance.py`（正跑）与 `--falsify`（常见错误写法逐条要求顶红）。
条数由脚本自己在 `[FINAL]` 行里打印——这里不抄数字：抄过的那版停在"25 条／10 条"，
而实际早就长成 30／13，落后的那份账只会让下一位误判"哪些形状还没测"。
"""
from __future__ import annotations

import datetime
import faulthandler
import os
import sys
import threading
import time
import traceback
import uuid

from PyQt5.QtCore import QByteArray, Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtNetwork import QLocalServer, QLocalSocket
from PyQt5.QtWidgets import QApplication, QMessageBox

from . import config as cfgmod
from . import win32
from .engine import poolkit
from .ui import theme
from .ui.main_window import SHUTDOWN_GRACE_MS, MainWindow

INSTANCE_KEY = f"{cfgmod.BRAND_ID}-player-v1"     # 单实例管道名：改名要连已开实例一起想（见下）
PROTO = "GELU1"                                   # 转交载荷的版本行；旧客户端没有这一行，照收
# 注册表/批处理里**没被外壳展开**的占位符。这是张平表，判据的证伪就是把它摘成空表，
# 所以别在这里加"看起来像 %N 也算"的旁路逻辑：那样一摘表就还剩一半效果，尺子量不到东西。
PCT_TOKENS = ("%1", "%2", "%3", "%4", "%5", "%0", "%*", "%~1", "%~f1", "%~nx1", "%L", "%v")
LAUNCH_LOG_KEEP = 400                             # launch.log 只留最后这么多行
SEEN_KEEP = 64                                    # nonce 去重表长度（重投保护，不是审计日志）


CRASH_LOG_MAX = 2 * 1024 * 1024      # 单份上限：2 MiB
CRASH_LOG_KEEP = 3                   # 归档保留份数（crash.log.1 .. .3）
_CRASH_FH = None                     # faulthandler 常开的句柄（进程级唯一，别关）


def crash_log_path() -> str:
    return os.path.join(cfgmod.LOG_DIR, "crash.log")


def _rotate_crash(path: str, limit: int, keep: int) -> None:
    """crash.log 超过 limit 就改名归档：.1 最新，最多 keep 份，更老的丢掉。

    无上限追加的问题不是"占点盘"，而是几个月后崩溃现场被埋进几十 MB 的旧记录里，
    真正要看的那一段反而找不到。轮转在每次写入前做，代价是一次 getsize。
    """
    try:
        if os.path.getsize(path) < limit:
            return
    except OSError:
        return                          # 文件还不存在 / 读不到：没什么可轮转的
    for i in range(keep, 0, -1):
        src = path if i == 1 else f"{path}.{i - 1}"
        if os.path.exists(src):
            try:
                os.replace(src, f"{path}.{i}")      # Windows 上 os.replace 会覆盖旧归档
            except OSError:
                return


def _append_crash(text: str) -> None:
    """往 crash.log 追加一段（带轮转 + fsync）；任何失败都吞掉——记日志不许弄死程序。"""
    path = crash_log_path()
    try:
        os.makedirs(cfgmod.LOG_DIR, exist_ok=True)
        _rotate_crash(path, CRASH_LOG_MAX, CRASH_LOG_KEEP)
        with open(path, "a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())        # 崩的时候不 fsync，断电/被收尸都可能丢最后一条
    except Exception:
        pass


def _thread_excepthook(args) -> None:
    """后台线程未处理异常落盘，再交给默认打印机（stderr 那份行为不变）。"""
    name = getattr(getattr(args, "thread", None), "name", "?")
    try:
        body = "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                  args.exc_traceback))
    except Exception:
        body = f"{args.exc_type}: {args.exc_value}\n"
    _append_crash(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} "
                  f"线程={name} ====\n{body}")
    try:
        threading.__excepthook__(args)      # stderr 那份行为不变
    except Exception:
        # 本机默认钩子只认真 ExceptHookArgs；别的形状（判据里注入的假参数）退到通用打印机。
        try:
            traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
        except Exception:
            pass


def install_crash_log() -> str:
    """未捕获异常落盘（写完 fsync），返回日志路径。

    为什么必须有：PyQt5 里凡是从 C++ 回调进 Python 的入口（paintEvent、定时器槽、
    原生事件过滤器）抛出异常，PyQt5 不会正常打印，而是直接 abort 整个进程——实测
    退出码 127、stdout/stderr 全空。用户那边只看到"播放器突然没了"，什么线索都没有。
    （真实踩过：floaters.paintEvent 里对 QRect 调 .toRect()。）落盘这一份是唯一能事后
    查证的证据，所以要在建窗口之前装好。

    三种崩溃三种入口，缺一不可：
      · 主线程 Python 异常 → `sys.excepthook`；
      · 后台线程 Python 异常（IPC 读线程、线程池里漏出来的）→ `threading.excepthook`；
      · C/C++ 层致命崩溃（Qt 访问违例、段错误）→ Python 钩子根本轮不到，
        只能靠 `faulthandler` 在信号处理器里把所有线程的栈直接写 fd。
    faulthandler 用一条常开句柄；轮转只搬名字不搬句柄，所以归档之后它写进的是 .1——
    仍在日志目录、仍受轮转封顶，换来"崩溃那一刻一定有栈"。
    """
    global _CRASH_FH
    default_hook = sys.excepthook

    def hook(etype, value, tb):
        try:
            body = "".join(traceback.format_exception(etype, value, tb))
        except Exception:
            body = f"{etype}: {value}\n"
        _append_crash(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} ====\n"
                      f"{body}")
        default_hook(etype, value, tb)

    sys.excepthook = hook
    threading.excepthook = _thread_excepthook
    if _CRASH_FH is None:
        try:
            os.makedirs(cfgmod.LOG_DIR, exist_ok=True)
            # 这条句柄要活到进程结束，必须允许轮转改名（见 win32 里的说明）。
            _CRASH_FH = win32.open_append_share_delete(crash_log_path())
        except OSError:
            _CRASH_FH = None
    if _CRASH_FH is not None:
        try:
            faulthandler.enable(file=_CRASH_FH, all_threads=True)
        except Exception:
            pass
    return crash_log_path()


def _enable_high_dpi() -> None:
    """必须在 QApplication 构造之前设，否则 150% 缩放机上全是模糊/错位。

    顺序也是契约的一部分：PerMonitorV2 要在 Qt 建窗之前落到进程上，PassThrough
    要在 QApplication 创建之前设——Qt 只读一次，晚一步就退回默认的 Round（125% /
    175% 会被折成整数倍，整个界面尺寸对不上）。PyQt5 5.15.2 有这条策略；更老的
    绑定没有就跳过，别因此起不来。
    """
    win32.enable_per_monitor_v2()
    policy = getattr(Qt, "HighDpiScaleFactorRoundingPolicy", None)
    setter = getattr(QApplication, "setHighDpiScaleFactorRoundingPolicy", None)
    if policy is not None and setter is not None:
        try:
            setter(policy.PassThrough)
        except Exception:
            pass
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)


# ==================== 命令行参数：形状兜住、丢要报名 ====================
def clean_arg(raw: str) -> str:
    """剥掉**最外面那一层**引号。

    为什么要剥：注册表里那行命令写的是 `"%1"`，而 `启动播放器.bat` 用的是 `%*`；
    只要中间再多转手一次（外壳→启动器→pythonw，或批处理里 `%~1` 与 `"%1"` 混用），
    到 `sys.argv` 就可能是 `"F:\\某片.mkv"` 这个带引号的字面量，`os.path.exists` 当场判它
    不存在——于是"双击了、播放器开了、文件没进来"。这一层剥掉，形状就回来了。
    """
    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].replace('\\"', '"')
    return s


def looks_like_option(tok: str) -> bool:
    """`--xxx` / `-x` 这类是开关，不是路径：既不能当文件交出去，也不能算"丢了一个文件"。"""
    return len(tok) > 1 and tok.startswith("-")


def resolve(tok: str) -> tuple[str | None, str]:
    """一段参数 → (盘上真存在的绝对路径 or None, 失败原因)。

    相对路径要在这里就补成绝对：`open_files()` 那边还要再查一次存在性，而内核的 cwd
    是我们的安装目录（见 engine/player.py 的 `mpv_path`），把相对的递过去就是"一声不响不放"。
    """
    if not tok:
        return None, "空参数"
    cands: list[str] = []
    if os.path.isabs(tok):
        cands.append(os.path.normpath(tok))
    else:
        cands.append(os.path.normpath(os.path.abspath(tok)))
        cands.append(os.path.normpath(os.path.join(cfgmod.ROOT, tok)))
    for p in cands:
        if os.path.exists(p):
            return p, ""
    return None, f"盘上没有这个文件（按 {len(cands)} 种拼法都找不到）"


def join_fragments(tokens: list[str], i: int, limit: int = 4):
    """把被空格劈成几段的路径拼回来。

    什么时候发生：批处理用 `%~1`（专门剥引号的那个）或命令行漏了引号，`F:\\a b\\c.mkv`
    就变成 `['F:\\\\a', 'b\\\\c.mkv']` 两段。这里只**在整段已经找不到文件时**才试拼接，
    且只向右试 `limit` 段——拼错了也只是"这条没救回来"，不会把别的真路径吃掉。
    返回 (命中的绝对路径, 最后消费掉的下标)，试不出来回 None。
    """
    for n in range(2, limit + 1):
        if i + n > len(tokens):
            break
        hit, _why = resolve(" ".join(tokens[i:i + n]))
        if hit:
            return hit, i + n - 1
    return None


def collect_paths(argv: list[str]) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    """argv → (收下并且盘上有的, 丢掉的[(原文, 原因)], 当选项忽略的)。

    三条规矩：
    · 顺序就是用户点击的顺序（第一批的第一个才是片）；
    · 丢掉的一律带原因，绝不静默（原因会写进 launch.log 并显示在空状态页上）；
    · 空 argv 什么都不要报——从快捷方式启动本来就没有参数，那不是故障。
    """
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    ignored: list[str] = []
    cleaned = [clean_arg(a) for a in (argv or [])]
    i = 0
    while i < len(cleaned):
        tok = cleaned[i]
        i += 1
        if not tok:
            continue
        if looks_like_option(tok):
            ignored.append(tok)
            continue
        if tok in PCT_TOKENS:
            dropped.append((tok, "外壳没展开的占位符（这行命令不是资源管理器递来的）"))
            continue
        hit, why = resolve(tok)
        if hit:
            kept.append(hit)
            continue
        j = join_fragments(cleaned, i - 1)
        if j:
            kept.append(j[0])
            i = j[1] + 1
            continue
        dropped.append((tok, why))
    return kept, dropped, ignored


# ==================== 启动留痕：下一次不必猜 ====================
def launch_log_path() -> str:
    return os.path.join(cfgmod.LOG_DIR, "launch.log")


def log_launch(kind: str, argv: list[str], kept: list[str],
               dropped: list[tuple[str, str]], ignored: list[str]) -> str:
    """每次启动一行：argv 原文、收下几条、丢了哪几条为什么丢、走的冷启动还是转交。

    写这一本账的理由很直接：用户报"双击没加载"时，我们这边**没有任何现场证据**——
    注册表能核对、命令能复跑，但那都是"现在"，不是"当时"。有了这行，下次一句
    `type data\\logs\\launch.log` 就能分清是"没递进来"还是"递进来被我们弄丢了"。
    记不住（磁盘只读、目录没建起来）绝不许把播放器带倒：整段包在 try 里，回 ""。
    """
    def cut(s: str, n: int = 90) -> str:
        s = str(s).replace("\n", " ").replace("\r", " ")
        return s if len(s) <= n else s[:n - 1] + "…"

    try:
        os.makedirs(cfgmod.LOG_DIR, exist_ok=True)
        line = " | ".join([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            f"pid={os.getpid()}",
            f"kind={cut(kind, 16)}",
            f"argv={len(argv)} kept={len(kept)} dropped={len(dropped)} ignored={len(ignored)}",
            "argv: " + (cut(" ; ".join(argv), 220) or "(空)"),
            "kept: " + (cut(" ; ".join(kept), 220) or "(无)"),
            "dropped: " + (cut(" ; ".join(f"{t}→{r}" for t, r in dropped), 220) or "(无)"),
            "ignored: " + (cut(" ; ".join(ignored), 80) or "(无)"),
        ])
        path = launch_log_path()
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                lines = [x for x in fh.read().splitlines() if x.strip()]
        except OSError:
            lines = []
        keep = max(1, int(LAUNCH_LOG_KEEP))
        with open(path, "w", encoding="utf-8", newline="\n") as fh:   # newline 必须给：
            fh.write("\n".join(lines[-(keep - 1):] + [line]) + "\n")   # 否则整份被转成 CRLF
        return path
    except BaseException:
        return ""


# ==================== 单实例转交：载荷、回执、排队 ====================
def make_nonce() -> str:
    return uuid.uuid4().hex[:16]


def build_handoff(paths: list[str], dropped: list[tuple[str, str]] | None,
                  nonce: str) -> bytes:
    """首行 `GELU1 <nonce> <正文字节数>`，其后一行一条路径；`!原文\\t原因` 是"这一趟丢掉的参数"。

    丢档也要过去：发送方转交成功就退了，若不把原因捎给已经开着的窗口，那句
    "你的文件我没接住"就永远没人说。

    正文长度为什么写进首行：接收端得知道"这一帧到齐了没有"。本机实测（2017-...
    见 tests/out 里的 atEnd 探针）Windows 上 `QLocalSocket.atEnd()` 在**连接还好着、
    缓冲刚被读空**的那一刻就返回 True——拿它当"到齐"信号，等于把第一个分片当整帧处理，
    多出来的路径行直接没人认。长度前缀是唯一不靠运气读完的办法。
    """
    body = [str(p) for p in (paths or [])]
    body += ["!" + str(t) + "\t" + str(r) for t, r in (dropped or [])]
    text = ("\n".join(body) + "\n").encode("utf-8") if body else b""
    return (f"{PROTO} {nonce} {len(text)}\n".encode("utf-8") + text)


def frame_state(buf: bytes) -> str:
    """这一坨字节处在帧的哪一步：`ready` / `wait` / `legacy` / `headless`。

    · `ready`    —— 首行到齐且正文够数：可以 parse。
    · `wait`     —— 首行说了要 N 个字节、现在只到了 M<N：还差，别动。
    · `legacy`   —— 首行到齐但没有长度声明（旧版本客户端）：长度判不了，只能等对方挂断封边界。
    · `headless` —— 连首行的换行都没到：什么都不知道，继续等。

    为什么不看 `atEnd()`：本机实测（2026-10-17 探针，留档 gelu-atend-out.txt）Windows 的
    `QLocalSocket.atEnd()` 在**连接还好着、缓冲刚被读空**的那一刻就返回 True
    （state=3=ConnectedState、bytesAvailable=0、atEnd=True）。拿它当"到齐"信号，等于第一个
    分片一到就 parse，多出来的路径行一个都不认——那正是"双击了视频、播放器开了、文件没了"。
    """
    head, sep, rest = (buf or b"").partition(b"\n")
    if not sep:
        return "headless"
    parts = head.decode("utf-8", "ignore").split()
    if len(parts) < 3 or parts[0] != PROTO or not parts[2].isdigit():
        return "legacy"
    return "ready" if len(rest) >= int(parts[2]) else "wait"


def frame_ready(buf: bytes) -> bool:
    """到齐了没有。只认首行声明的字节数；`legacy` 在这里一律 False，由挂断那一路收尾。"""
    return frame_state(buf) == "ready"


def parse_handoff(text: str) -> tuple[str, list[str], list[tuple[str, str]]]:
    """读载荷；没有版本行的旧形状照收（nonce 记空串，不去重、也不回执匹配）。"""
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    nonce = ""
    for n, line in enumerate((text or "").splitlines()):
        if not line:
            continue
        if n == 0 and line.startswith(PROTO):
            tail = line[len(PROTO):].split()
            nonce = tail[0] if tail else ""
            continue
        if line.startswith("!"):
            t, sep, r = line[1:].partition("\t")
            dropped.append((t, r if sep else "没带原因"))
            continue
        kept.append(line)
    return nonce, kept, dropped


def ack_line(nonce: str) -> bytes:
    return (f"OK {nonce}\n").encode("utf-8")


def ack_matches(text: str, nonce: str) -> bool:
    """回执必须**带着这一次的 nonce**才算数：空 nonce、别人的 nonce 都不算。

    为什么这么较真：管道名是固定的，上一趟转交可能留下半截连接；认出"这是回给我这一次的"
    才能让发送方放心退出，否则宁可自己开一个新窗口（多一个窗口看得见，
    文件被吃掉看不见）。
    """
    if not nonce:
        return False
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "OK" and parts[1] == nonce:
            return True
    return False


def _wait_slice(fn, total_ms: int, pump=None, slice_ms: int = 20) -> bool:
    """分片等，每片之间把事件循环让出去。

    本机实测（2026-10-16 spike，两次都踩过）：命名管道的写要等**对端把字节读走**才完成，
    而对端那个 `newConnection` 槽得靠事件循环跑；一次 `waitForBytesWritten(500)` 把自己堵在
    里面，就变成"发送方永远写不完、接收方永远没被叫醒"。同进程自测必须传 `pump`；
    跨进程（真实双击那条路）两边各有循环，传 None 即可。
    """
    if pump is None:
        return bool(fn(total_ms))
    spent = 0
    while spent < max(slice_ms, int(total_ms)):
        if fn(slice_ms):
            return True
        pump()
        spent += slice_ms
    return bool(fn(0))


def forward_to_running(paths: list[str], key: str | None = None, nonce: str | None = None,
                       dropped: list[tuple[str, str]] | None = None, connect_ms: int = 300,
                       ack_ms: int = 2500, pump=None) -> bool:
    """已有实例在跑：把路径递过去、**等它回执**，自己才退。回不到就返回 False（我们开新窗口）。

    `pump` 只给同进程自测用（见 `_wait_slice`）；生产调用不传。
    等 ack 而不是等 accept：旧写法在这里就是那句谎——它写完字节返回 True，接收方却把
    路径扔在了"窗口还没装配完"的洞里，用户看到播放器被顶到前面、列表还是空的。
    """
    key = key or INSTANCE_KEY
    nonce = nonce or make_nonce()
    sock = QLocalSocket()
    try:
        sock.connectToServer(key)
        if not _wait_slice(sock.waitForConnected, connect_ms, pump):
            return False
        sock.write(QByteArray(build_handoff(list(paths or []), dropped, nonce)))
        # flush() 是必须的那一步：没有事件循环时 write 只进 Qt 的内部缓冲，
        # 不主动刷就等于"字节从没上过管道"，对端再怎么等也等不到。
        sock.flush()
        buf = bytearray()
        deadline = time.time() + max(0.05, ack_ms / 1000.0)
        while time.time() < deadline:
            if pump is not None:
                pump()
            # 判定只读缓冲：泵过循环之后 waitForReadyRead 会照样回 False（字节已经被事件
            # 循环收进缓冲），信那个布尔会得到一条假红。
            data = bytes(sock.readAll().data())
            if data:
                buf.extend(data)
                if ack_matches(bytes(buf).decode("utf-8", "ignore"), nonce):
                    return True
            if sock.state() in (QLocalSocket.UnconnectedState, QLocalSocket.ClosingState):
                break
            sock.waitForReadyRead(20)
        return False
    except BaseException:
        return False
    finally:
        try:
            sock.abort()
        except BaseException:
            pass


class Handoff:
    """收件处：窗口还在装配时先排队，`attach` 之后立刻补交，回执等真落地才发。

    为什么需要它（而不是"窗口不在就回绝"）：从 `listen()` 成功到 `MainWindow` 装配完中间
    有几百毫秒，这段时间里双击的那一趟要是被回绝，就是"我明明点了视频，它只开了个空壳"。
    排队 + 延迟回执 = 发送方一直等到"确实落地"才退，中间任何一步崩了它都会自己开窗口。
    """

    def __init__(self):
        self.win = None
        self.pending: list[tuple[str, list[str], list[tuple[str, str]], object]] = []
        self.seen: list[str] = []
        self.delivered = 0

    @property
    def pending_count(self) -> int:
        return len(self.pending)

    @staticmethod
    def _present(w) -> None:
        """把窗口带到用户眼前。

        为什么不是无条件 `showNormal()`：窗口正全屏时那一句把它拽回普通态，而主窗的
        `_fs` 还留在 True ⇒ 标题栏与控制条都被判定"该收"，剩下的是一扇无边框、
        什么 chrome 都没有的窗口（2026-10-20 全屏审查 §3① 点名）。
        主窗提供了 `present_for_external()` 就按它自己那套带（全屏中留在全屏）；
        没有这个方法的一律退回旧两条——单实例的测试替身只认识那两条。
        """
        f = getattr(w, "present_for_external", None)
        if callable(f):
            f()
            return
        w.showNormal()
        w.activateWindow()

    def _deliver(self, kept: list[str], dropped: list[tuple[str, str]]) -> bool:
        w = self.win
        if w is None:
            return False
        try:
            self._present(w)
            if kept:
                w.add_paths(kept)
            if dropped:
                w.notify_external_drop(dropped)
        except BaseException:
            # 窗口正在销毁（Qt 那边 C++ 对象已删 → RuntimeError）：不许把接收方一起带倒，
            # 这一批就当中途没人接，交回调用方排队/不回执。
            return False
        self.delivered += 1
        return True

    @staticmethod
    def _ack(sock, nonce: str) -> None:
        try:
            if sock is None:
                return
            sock.write(QByteArray(ack_line(nonce)))
            # flush 必须在等之前：QLocalSocket 的 write 只进内部缓冲，跨进程那一头没有
            # 事件循环替我们把它推到管道上，不 flush 就会"回执写了、发送方永远等不到"。
            sock.flush()
            sock.waitForBytesWritten(200)
            sock.close()
        except BaseException:
            pass

    def receive(self, nonce: str, kept: list[str], dropped: list[tuple[str, str]],
                sock=None) -> str:
        """回 "delivered" / "queued" / "dup"。前两者由调用方决定何时回执，"dup" 立刻回执。"""
        kept = list(kept or [])
        dropped = list(dropped or [])
        if nonce and nonce in self.seen:
            return "dup"
        if nonce:
            self.seen.append(nonce)
            if len(self.seen) > SEEN_KEEP:
                del self.seen[0]
        if self._deliver(kept, dropped):
            return "delivered"
        # 没有窗口（装配中）或窗口刚销毁：排队等 attach，**sock 也要一起留着**——
        # 回执得等真落地的时刻才发，早发一句就等于回到那句谎。
        self.pending.append((nonce, kept, dropped, sock))
        return "queued"

    def attach(self, win) -> None:
        """装配完补交排队的那几批。**回执只在真落地之后发**：`_deliver` 返回失败
        （窗口刚销毁／add_paths 抛）时这批要放回队列——2026-11-12 审查 B4 的现场是
        旧写法不看返回值直接 `_ack`，第二个实例收到"成功"就退了，主窗口什么也没拿到。
        拿不到回执的发送方会自己开窗口，所以这里的正确动作是沉默并保留。
        """
        self.win = win
        due, self.pending = self.pending, []
        for nonce, kept, dropped, sock in due:
            if self._deliver(kept, dropped):
                self._ack(sock, nonce)
            else:
                self.pending.append((nonce, kept, dropped, sock))


def listen(handoff: Handoff, key: str | None = None) -> QLocalServer | None:
    """当主实例：留一个本地服务收后续实例递来的路径。"""
    key = key or INSTANCE_KEY
    srv = QLocalServer(None)
    if not srv.listen(key):
        # 只有听不上时才去清"上次异常退出留下的死锁"。`QLocalServer.removeServer` 不是纯
        # 文件操作：它会**去连同名管道并要求对面自删**。本机实测（2026-10-16）Windows
        # 命名管道那一路拆不动活着的 server，所以这句在这里是无害的；但那是环境事实、
        # 不是契约，拿它当前置条件写代码就会等着被环境咬一口。
        QLocalServer.removeServer(key)
        if not srv.listen(key):
            srv.deleteLater()
            return None
    # 判"听上了没有"用 isListening()，不用 serverError() != QLocalServer.NoError：
    # 本机 PyQt5 5.15.10 / Qt 5.15.2 **没有**把 QLocalServer 的 ServerError 枚举成员
    # 重导出成类属性（dir(QLocalServer) 里只有 errorString/fullServerName/isListening/
    # listen/removeServer/serverError/serverName，一个枚举名都没有），那句比较会在启动
    # 第一行就 AttributeError，播放器根本开不出窗口（实测 rc=1，冒烟用例抓到的）。
    # isListening 语义更直接，也不依赖枚举名。

    def on_new():
        c = srv.nextPendingConnection()
        if c is None:
            return
        # 挂父：没有父的 QObject 只被 Python 引用拿着，随时可能被回收；连接半路断掉就成了
        # "发的人以为发了、收的人没收到"。这条教训在别的验收里真金白银踩过一次。
        c.setParent(srv)
        box = {"buf": bytearray(), "done": False}

        def deliver(final: bool = False):
            """攒够一帧再动手：帧边界由首行声明的字节数说了算（`frame_state`），不看 atEnd。

            `final=True` 只来自 `disconnected`。三种收尾分开走，因为它们的产品后果不一样：
            · `ready`    —— 到齐，正常 parse、投递、回执；
            · `legacy`   —— 旧客户端没写长度，它的帧边界就是"挂断"这一动作本身，所以在 final 时收；
            · `wait`/`headless` —— 新客户端说了要 N 字节却没送完就断了（崩了／被杀）：**丢掉整帧**。
              正文是在字节中间断的，最后一行很可能是 `C:\\x\\a.m` 这种"看着像路径"的东西，
              递进去要么让用户看到一句假报错，要么万一真有个同名残文件就播错了文件。
              宁可不说话——发送方那边等不到回执会自己开窗口，用户至少能看到画面。
            """
            if box["done"]:
                return
            data = bytes(c.readAll().data())
            if data:
                box["buf"].extend(data)
            st = frame_state(bytes(box["buf"]))
            if st == "ready" or (final and st == "legacy"):
                box["done"] = True
                nonce, kept, dropped = parse_handoff(
                    bytes(box["buf"]).decode("utf-8", "ignore"))
                status = handoff.receive(nonce, kept, dropped, c)
                if status in ("delivered", "dup"):
                    handoff._ack(c, nonce)
                return
            if not final:
                return
            box["done"] = True
            log_launch("truncated", [], [], [(f"{len(bytes(box['buf']))} 字节残帧",
                                             "发送方写到一半就断了，整帧作废")], [])

        c.readyRead.connect(lambda: deliver(False))
        c.disconnected.connect(lambda: deliver(True))

    srv.newConnection.connect(on_new)
    return srv


def startup_checks() -> list[str]:
    """返回启动阻断项的说明列表（空=一切正常）。不做"应该能跑"式自检。"""
    problems = []
    if not os.path.exists(cfgmod.MPV_EXE):
        problems.append(f"找不到播放内核：{cfgmod.MPV_EXE}")
    if sys.platform != "win32":
        problems.append("本播放器依赖 Win32 窗口嵌入与 DWM，仅支持 Windows")
    return problems


def main(argv: list[str] | None = None) -> int:
    install_crash_log()          # 第一件事：后面任何一步炸了都得留下证据
    argv = list(sys.argv[1:] if argv is None else argv)
    kept, dropped, ignored = collect_paths(argv)

    _enable_high_dpi()
    # QApplication 必须先存在再去碰 QLocalSocket：以前这两步的顺序是反的（转交发生在
    # 建 app 之前），"没 app 就建 socket"在 Qt 里是未定义行为，能不能连上全看运气。
    app = QApplication.instance() or QApplication(argv or [cfgmod.APP_TITLE])
    # 无参数启动也要先问一次"是不是已经有一份在跑"：用户再点一次 exe（或从开始菜单
    # 再开一次）想要的是把已有窗口叫到前面，而不是多出第二扇空窗。没有实例时这次
    # 连接在命名管道上立刻失败（不等满超时），冷启动的代价可以忽略。
    if forward_to_running(kept, dropped=dropped):
        log_launch("forward", argv, kept, dropped, ignored)
        return 0

    handoff = Handoff()
    srv = listen(handoff)        # 先开门再装窗：中间那段投递由 Handoff 排队（见类注释）
    app.setApplicationName(cfgmod.APP_NAME)
    app.setOrganizationName(cfgmod.APP_NAME)
    # 任务栏/Alt-Tab 的图标：必须在建任何窗口之前设到 QApplication 上；单个窗口自己设
    # 只能管住自己，消息框、文件对话框仍会退回 Qt 默认图标。
    _icon = os.path.join(cfgmod.ASSETS_DIR, "app.ico")
    if os.path.isfile(_icon):
        app.setWindowIcon(QIcon(_icon))
    app.setStyle("Fusion")
    cfg = cfgmod.Config()
    # theme.install 而不是 setStyleSheet(qss())：主色要写回 theme.ACCENT 令牌
    # （图标色/进度条/选中项读的是那个全局值，见 ui/theme.py 的第 2 条规矩），
    # 调色板还要单独给一份深色的（QSS 管不到对勾、下拉箭头、禁用字色这些 Qt 自绘零件）。
    theme.install(app, str(cfg["theme_accent"]))
    cfgmod.ensure_dirs(cfg)
    # 上次若是被强杀/崩溃带走，缓存里会留半张封面（*.part.jpg）和写了一半的 *.tmp；
    # 此刻单实例已成立，不会跟任何在写的任务抢文件，正是清扫的唯一安全时机。
    cfgmod.sweep_cache_temp()
    log_launch("cold", argv, kept, dropped, ignored)
    problems = startup_checks()

    win = MainWindow(cfg, files=kept)
    app.setProperty("main_window", win)
    handoff.attach(win)
    win.show()
    if cfgmod.FROZEN:
        # 打包版没有控制台：这一行同时充当"窗口真的建出来了"与资源落点的现场记录。
        print(f"[startup] ready root={cfgmod.ROOT} bundle={cfgmod.BUNDLE_DIR} "
              f"mpv={os.path.isfile(cfgmod.MPV_EXE)}", file=sys.stderr)
    if dropped:
        win.notify_external_drop(dropped)

    if problems:
        # 弹框之前先留一行：打包版没有控制台，模态框又会让"进程还活着"看起来像正常，
        # 事后只有这一行能分清"起来了"还是"卡在一个没人能点的警告框上"。
        print("[startup] " + "；".join(problems), file=sys.stderr)
        QMessageBox.warning(win, cfgmod.APP_TITLE, "\n".join(problems) + "\n界面照常打开，但无法播放。")

    # 第二道收口闸：正常关窗走 MainWindow.closeEvent（那里能写 shutdown.log），但
    # "最后一个窗口自己没了""quit() 从别处进来"这些路不经过 closeEvent。aboutToQuit 在
    # 事件循环结束**之前**、Qt 对象还没拆的时候发，正适合再兜一次；poolkit.shutdown 是
    # 幂等的，第二次进来只是清一遍空队列，不会重复杀孩子、也不会多等一个 grace。
    app.aboutToQuit.connect(lambda: poolkit.shutdown(SHUTDOWN_GRACE_MS))

    rc = app.exec()
    if srv is not None:
        srv.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
