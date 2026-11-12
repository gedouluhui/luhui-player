"""「双击视频 → 播放器开出来却没加载文件」这条链的验收：参数、转交、留痕、可见解释。

用户原话（2026-10-16）：「双击视频确实打开了播放器，但是视频根本没有加载进来，只是单纯的
播放器界面，显示打开文件夹那些……应该是点开就能直接播放的了啊。」

这份量具盯的是**文件在半路被谁吃掉**，四处嫌疑逐条要有牙：
1. **参数形状**（`collect_paths`）：多余一层引号、被空格劈成几段、相对路径、注册表里没展开的
   `%1`、真不存在——旧写法是 `os.path.exists` 一筛就丢，只往 stderr 印一句，`pythonw` 下没人看见；
2. **单实例转交**（`forward_to_running` / `Handoff`）：旧写法发完字节就当"交出去了"然后自己退，
   而接收方那句 `if win is not None` 在窗口还没装配完时把路径**直接扔掉**——窗口被顶到前面、
   文件没了，正是用户看到的那一幕。现在要求 `OK <nonce>` 回执，收不到就自己开新窗口；
3. **留痕**（`log_launch`）：每次启动一行——argv 原文、收下几条、丢了哪几条为什么丢、走的
   冷启动还是转交。下次再出这种事不必猜；
4. **可见解释**（`_EmptyPage.set_notice` / `notify_external_drop` / `_osd` 转写）：`Osd` 挂在
   视频容器上，而空状态时那个容器是**隐藏**的（`VideoSurface.set_empty`），所以"没有找到可播放
   的文件"这类话以前压根不会出现。

跑法：
    python tests/test_instance.py                 # 验收，全绿退出 0
    python tests/test_instance.py --falsify       # 证伪：把实现换成常见错误写法，对应判据必须翻红

三条老规矩（本仓验收脚本共用）：判据不许恒真（每条由 --falsify 证伪）、判据自己不许崩
（包在 try/except 里，崩了单记 FAIL，不许顺手放宽）、环境不给记 ENV 并退 2（不许冒充产品红）。

本机 Qt/Windows 实测过两条管道脾气，判据形状照着实测写、不照着文档想（2026-10-16 spike）：
· 命名管道的写要等**服务端把字节读走**才完成，而服务端那个槽靠事件循环跑 —— 所以等待必须
  "分片 + 每片之间泵一次循环"；`pump=APP.processEvents` 就是为这个（见 forward 的签名注释）。
· 泵过循环之后 `waitForReadyRead`/`waitForBytesWritten` **照样可能返回 False**（字节已经被事件
  循环收进缓冲、或已无字节可写），所以"收到没有"只能读缓冲，不能信那两个布尔返回值。
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BS = chr(92)                       # 路径分隔符不写成字面量：见 check_openwith.py 第 1 条
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
MAIN_PY = os.path.join(ROOT, "app", "main.py")

TMP = tempfile.mkdtemp(prefix="gelu-instance-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")        # 必须在 import config 之前
os.environ["QT_QPA_PLATFORM"] = "offscreen"                # 不占用户桌面，也不依赖屏幕醒着
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QByteArray, QTimer                  # noqa: E402
from PyQt5.QtNetwork import QLocalServer, QLocalSocket       # noqa: E402
from PyQt5.QtWidgets import QApplication                     # noqa: E402

from app import main as M                                    # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv[:1])
OUT_PATH = ""
RESULTS: list[list[str]] = []
SERVERS: list = []                 # 服务端 C++ 对象必须有人持有，否则判据自己失灵


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


def kp(*parts: str) -> str:
    return BS.join(parts)


def new_key() -> str:
    return "gelu-it-" + uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- 真样本（盘上真有这些文件）
SAMPLE_DIR = kp(TMP, "sample dir")
os.makedirs(SAMPLE_DIR, exist_ok=True)
VID = kp(SAMPLE_DIR, "美国队长 4K.mkv")           # 带空格 + 中文，两种坑一起占
VID2 = kp(TMP, "rel.mkv")                          # 给相对路径那条用
GONE = kp(TMP, "盘上并没有这个.mkv")
NOEXT = kp(TMP, "readme.txt")                      # 存在但不可播：给"话要看得见"那两条用
for _p in (VID, VID2, NOEXT):
    with open(_p, "wb") as _fh:
        _fh.write(b"not really media; existence is all we need")


class FakeWin:
    """只记录"窗口被叫到做了什么"的替身：判据要的是顺序与内容，不是真像素。"""

    def __init__(self):
        self.calls: list[str] = []
        self.added: list[list[str]] = []
        self.dropped: list[list] = []

    def showNormal(self):
        self.calls.append("showNormal")

    def activateWindow(self):
        self.calls.append("activateWindow")

    def add_paths(self, paths, replace=True):
        self.calls.append("add_paths")
        self.added.append(list(paths))

    def notify_external_drop(self, dropped):
        self.calls.append("notify_external_drop")
        self.dropped.append(list(dropped))


def pump(ms: int = 30) -> None:
    t = time.time()
    while time.time() - t < ms / 1000.0:
        APP.processEvents()
        time.sleep(0.002)


# ---------------------------------------------------------------- 服务端夹具
class HandoffServer:
    """就用产品那套 `M.listen`，只把管道名换成一次性钥匙，不去碰真实例的入口。"""

    def __init__(self, handoff, key=None):
        self.key = key or new_key()
        self.handoff = handoff
        self.srv = M.listen(handoff, self.key)
        SERVERS.append(self)

    @property
    def listening(self) -> bool:
        return bool(self.srv is not None and self.srv.isListening())

    def close(self):
        try:
            if self.srv is not None:
                self.srv.close()
        except BaseException:
            pass
        if self in SERVERS:
            SERVERS.remove(self)


class SilentServer:
    """只会 accept、爱回不回执的回声壁——用来把发送方那句谎话逼出来。

    两种脾气分开占（`hold`）：
    · `hold=False`：读完就挂断——"对面那个实例正在退出"，发送方**该**立刻判否，别白等窗口；
    · `hold=True`：读完不挂断也不回执——"对面活着但没把文件落地"，发送方**必须**等满回执
      窗口才许判否。这两种在真实世界里结局一样（都没落地），但产品的等待策略相反，
      所以判据也得相反；合成一条就会有一半行为没人量。
    """

    def __init__(self, key=None, ack: bytes = b"", read: bool = True, hold: bool = False):
        self.key = key or new_key()
        self.ack = ack
        self.read = read
        self.hold = hold
        self.got: list[bytes] = []
        self.keep: list = []
        self.srv = QLocalServer()
        self.srv.newConnection.connect(self.on_new)
        self.srv.listen(self.key)
        SERVERS.append(self)

    def on_new(self):
        c = self.srv.nextPendingConnection()
        if c is None:
            return
        c.setParent(self.srv)                        # 同一条教训：不挂父随时可能被回收
        self.keep.append(c)

        def d():
            data = bytes(c.readAll().data())
            if data:
                self.got.append(data)
            if self.hold:
                return                                # 活着、收下了、就是不回话
            if c.atEnd():
                if self.ack:
                    c.write(QByteArray(self.ack))    # QByteArray 只吃 bytes，喂 str 会抛→整进程 abort
                    c.flush()
                    pump(20)
                c.close()

        if self.read:
            c.readyRead.connect(d)
            c.disconnected.connect(d)

    def close(self):
        try:
            self.srv.close()
        except BaseException:
            pass
        if self in SERVERS:
            SERVERS.remove(self)


def probe_forward(ack: bytes = b"", key: str | None = None, hold: bool = False) -> bool:
    """对着一台给定脾气的服务端投一次，只回"发送方说成不成功"——给证伪当探针用。"""
    srv = SilentServer(key=key, ack=ack, hold=hold)
    try:
        return M.forward_to_running([VID], key=srv.key, ack_ms=500, pump=APP.processEvents)
    finally:
        srv.close()


# ---------------------------------------------------------------- 判据
def j_plain() -> tuple[bool, str]:
    got, dropped, ignored = M.collect_paths([VID])
    return (got == [os.path.normpath(VID)] and not dropped and not ignored,
            f"kept={got} dropped={dropped} ignored={ignored}")


def j_clean_quoted() -> tuple[bool, str]:
    got, dropped, _ig = M.collect_paths(['"' + VID + '"'])
    return (got == [os.path.normpath(VID)] and not dropped,
            f"kept={got} dropped={dropped}（多套一层引号还须认得是同一个文件）")


def j_placeholder() -> tuple[bool, str]:
    got, dropped, _ig = M.collect_paths(["%1"])
    a = not got and len(dropped) == 1 and "%1" in dropped[0][0] and "展开" in dropped[0][1]
    got2, dropped2, _ig2 = M.collect_paths(["%~1"])
    b = not got2 and len(dropped2) == 1 and "%~1" in dropped2[0][0]
    return (a and b, f"%1 → {dropped}；%~1 → {dropped2}（不点名就等于静默吃掉）")


def j_fragments() -> tuple[bool, str]:
    toks = VID.split(" ")
    got, dropped, _ig = M.collect_paths(toks)
    return (len(toks) >= 2 and got == [os.path.normpath(VID)] and not dropped,
            f"拆成 {len(toks)} 段 {toks} → kept={got} dropped={dropped}")


def j_relative() -> tuple[bool, str]:
    old = os.getcwd()
    try:
        os.chdir(TMP)
        got, dropped, _ig = M.collect_paths([os.path.basename(VID2)])
    finally:
        os.chdir(old)
    ok = got == [os.path.normpath(VID2)] and not dropped and got and os.path.isabs(got[0])
    return (ok, f"kept={got} dropped={dropped}（交到内核那边必须已经是绝对路径）")


def j_missing() -> tuple[bool, str]:
    got, dropped, _ig = M.collect_paths([GONE])
    ok = not got and len(dropped) == 1 and dropped[0][0] == GONE and bool(dropped[0][1])
    return (ok, f"kept={got} dropped={dropped}")


def j_order_mixed() -> tuple[bool, str]:
    got, dropped, _ig = M.collect_paths([VID, GONE, VID2])
    want = [os.path.normpath(VID), os.path.normpath(VID2)]
    return (got == want and len(dropped) == 1,
            f"kept={got}（顺序要跟着用户点的那个来）dropped={dropped}")


def j_options() -> tuple[bool, str]:
    got, dropped, ignored = M.collect_paths(["--hwdec", "-v", VID])
    return (got == [os.path.normpath(VID)] and not dropped and len(ignored) == 2,
            f"kept={len(got)} dropped={dropped} ignored={ignored}（选项不是路径，也不算丢文件）")


def j_no_args() -> tuple[bool, str]:
    got, dropped, ignored = M.collect_paths([])
    return (not got and not dropped and not ignored,
            f"kept={got} dropped={dropped} ignored={ignored}（反向哨兵：空启动不许报警）")


def j_wire() -> tuple[bool, str]:
    """载荷形状自己也得有账：发出去的字节要能被原样读回来（含丢弃原因）。"""
    kept = [os.path.normpath(VID)]
    dropped = [(GONE, "盘上没有这个文件")]
    raw = M.build_handoff(kept, dropped, "abcd1234ef567890")
    n2, k2, d2 = M.parse_handoff(raw.decode("utf-8"))
    legacy = "\n".join(kept)                        # 旧客户端：光秃秃一行路径，没有版本行
    n3, k3, d3 = M.parse_handoff(legacy)
    ok = (n2 == "abcd1234ef567890" and k2 == kept and d2 == dropped
          and n3 == "" and k3 == kept and d3 == [])
    return (ok, f"新形状={n2!r},{k2},{d2} 旧形状={n3!r},{k3},{d3}")


def j_no_server() -> tuple[bool, str]:
    t0 = time.time()
    ok = M.forward_to_running([VID], key=new_key(), ack_ms=1500, pump=APP.processEvents)
    dt = time.time() - t0
    return (ok is False and dt < 3.0, f"返回={ok} 耗时={dt:.2f}s（没人听时必须快速判否）")


def j_accept_without_ack() -> tuple[bool, str]:
    """对面把连接挂断又不回执：必须判否，而且**别白等**——立刻自己开窗口。"""
    srv = SilentServer(ack=b"", hold=False)
    try:
        t0 = time.time()
        ok = M.forward_to_running([VID], key=srv.key, ack_ms=1500, pump=APP.processEvents)
        dt = time.time() - t0
    finally:
        srv.close()
    return (ok is False and dt < 1.0,
            f"对面挂断不回回执 → 返回={ok} 耗时={dt:.2f}s"
            f"（返回 True 就是把「没落地」报成「已送达」；等满 1.5s 是把用户多按一次双击的时间吃掉）")


def j_hold_without_ack() -> tuple[bool, str]:
    """对面活着、字节也收走了，就是不回执：这是"那句谎"最像真的时刻，必须等满窗口再判否。

    和上一条是两种脾气（挂断 vs 沉默），产品的等待策略相反，所以两条都要有——
    合成一条就会有一半行为没人量（实测：只留挂断那条时，把等待改成"状态一变就没回执"
    的写法仍然全绿）。
    """
    srv = SilentServer(ack=b"", hold=True)
    try:
        t0 = time.time()
        ok = M.forward_to_running([VID], key=srv.key, ack_ms=500, pump=APP.processEvents)
        dt = time.time() - t0
        arrived = len(srv.got) >= 1
    finally:
        srv.close()
    return (ok is False and arrived and dt >= 0.3,
            f"沉默但没挂断 → 返回={ok} 收到字节={arrived} 耗时={dt:.2f}s"
            f"（等不够 0.3s 就是没真等回执；返回 True 就是旧写法那句谎）")


def j_frame_state_split() -> tuple[bool, str]:
    """四种状态各归各位——尤其是 `wait`（还差字节）和 `legacy`（判不了长度）不能混成一锅。

    这两家在挂断时的结局**相反**：legacy 必须收（否则旧版本客户端的文件全丢），
    wait 必须丢（否则会把半截字节里的残行当路径递进内核）。合成一个分支就会有一半错。
    """
    nonce = "b" * 16
    full = M.build_handoff([VID, VID2], [(GONE, "盘上没有这个文件")], nonce)
    head = full[:full.index(b"\n") + 1]
    body = full[len(head):]
    want = {"整帧": (full, "ready"),
            "只有头一行": (head, "wait"),
            "正文缺一块": (head + body[:-5], "wait"),
            "半个头（连换行都没有）": (head[:6], "headless"),
            "旧形状（没有长度行）": ((VID + "\n" + VID2 + "\n").encode("utf-8"), "legacy"),
            "空载荷（正文 0 字节）": (M.build_handoff([], [], nonce), "ready")}
    bad = [f"{k}→{M.frame_state(v)!r}（应 {e}）" for k, (v, e) in want.items()
           if M.frame_state(v) != e]
    return (not bad, "错位：" + "、".join(bad) if bad else "六档全对：" +
            "、".join(f"{k}={M.frame_state(v)}" for k, (v, _e) in want.items()))


def j_fragmented_frame() -> tuple[bool, str]:
    """`frame_ready` 那道闸的正面与反面：到齐说"是"，四种没到齐都说"不是"。

    起因是本机实测的 Qt/Windows 脾气（留档 gelu-atend-out.txt：state=3 连接还开着时
    atEnd 已经是 True）。旧写法拿 atEnd 当"到齐"，第一个分片一到就 parse，多出来的路径行
    一个都不认——这正好是"双击了却没加载"的另一半。真管道那条腿在 `j_frame_e2e`。
    """
    nonce = "f" * 16
    full = M.build_handoff([VID, VID2], [(GONE, "盘上没有这个文件")], nonce)
    head = full[:full.index(b"\n") + 1]
    body = full[len(head):]
    cases = [("整帧", M.frame_ready(full), True),
             ("只有头一行", M.frame_ready(head), False),
             ("正文缺一截", M.frame_ready(head + body[:-5]), False),
             ("半个头", M.frame_ready(head[:6]), False),
             ("旧形状（无长度行，等挂断）", M.frame_ready((VID + "\n").encode("utf-8")), False)]
    bad = [f"{k}（答={got!r} 应={exp}）" for k, got, exp in cases if bool(got) is not exp]
    n, kept, dropped = M.parse_handoff(full.decode("utf-8"))
    shape = (n == nonce and kept == [os.path.normpath(VID), os.path.normpath(VID2)]
             and dropped == [(GONE, "盘上没有这个文件")])
    return (not bad and shape, ("判错：" + "、".join(bad) if bad else "五档全对")
            + f"；长度行没把 parse_handoff 带歪={shape}")


def j_frame_e2e() -> tuple[bool, str]:
    """真管道、真分片：发送侧每次只写 7 个字节，接收端（产品那套 `listen`）要等齐一帧再动。

    这条不靠"碰巧一次读完"，也不是纯函数——`readyRead` 被叫了很多次之后，窗口拿到的到底是
    完整的两条路径还是一地碎片，只有真管道量得出来。
    """
    nonce = "c" * 16
    h = M.Handoff()
    w = FakeWin()
    h.attach(w)
    srv = HandoffServer(h)
    sock = QLocalSocket()
    try:
        sock.connectToServer(srv.key)
        if not sock.waitForConnected(800):
            return (False, "客户端连不上（夹具自己失灵，这条不作数）")
        payload = bytes(M.build_handoff([VID, VID2], [(GONE, "盘上没有这个文件")], nonce))
        ack = bytearray()

        def drain():
            ack.extend(bytes(sock.readAll().data()))

        for i in range(0, len(payload), 7):
            sock.write(QByteArray(payload[i:i + 7]))
            sock.flush()
            pump(30)
            drain()
        t0 = time.time()
        while not M.ack_matches(bytes(ack).decode("utf-8", "ignore"), nonce) \
                and time.time() - t0 < 3.0:
            pump(30)
            drain()
        got_ack = M.ack_matches(bytes(ack).decode("utf-8", "ignore"), nonce)
        flat = [p for one in w.added for p in one]
        ok = (got_ack and flat == [os.path.normpath(VID), os.path.normpath(VID2)]
              and len(w.added) == 1 and len(w.dropped) == 1)
        return (ok, f"回执={got_ack} 投了 {len(payload)} 字节（每片 7）→ 窗口收到={w.added} "
                    f"丢档={w.dropped}")
    finally:
        try:
            sock.abort()
        except BaseException:
            pass
        srv.close()


def j_truncated_discard() -> tuple[bool, str]:
    """两向都判：新客户端半截就挂断 ⇒ 整帧作废；旧形状（没长度行）挂断 ⇒ 照旧收下。

    为什么半截要作废：正文是在字节中间断的，最后一行很可能是 `C:\\x\\a.m` 这种看着像路径的
    东西，递进去要么报一句假错误导用户，要么万一真有个同名残文件就播错了。
    为什么旧形状必须照收：兼容的是"已经开在用户机器上的老版本"，砍掉这条等于把老客户的
    双击全丢。两个方向都得有牙——只判一边，实现就会往省事那侧倒。
    """
    nonce = "d" * 16
    h = M.Handoff()
    w = FakeWin()
    h.attach(w)
    h2 = M.Handoff()
    w2 = FakeWin()
    h2.attach(w2)
    srv_new = HandoffServer(h)          # 收半截新帧的（产品那套 listen）
    srv_old = HandoffServer(h2)         # 收旧形状的（同一套代码，另一把钥匙）
    socks: list[QLocalSocket] = []
    try:
        payload = bytes(M.build_handoff([VID, VID2], [], nonce))
        s = QLocalSocket()
        socks.append(s)
        s.connectToServer(srv_new.key)
        if not s.waitForConnected(800):
            return (False, "半截帧那条连不上（夹具自己失灵，这条不作数）")
        s.write(QByteArray(payload[:payload.index(b"\n") + 9]))    # 头一行 + 正文 8 字节
        s.flush()
        pump(60)
        s.disconnectFromServer()
        pump(150)
        new_refused = not w.added and not w.dropped and h.delivered == 0

        s2 = QLocalSocket()
        socks.append(s2)
        s2.connectToServer(srv_old.key)
        if not s2.waitForConnected(800):
            return (False, "旧形状那条连不上（夹具自己失灵，这条不作数）")
        legacy = (os.path.normpath(VID) + "\n").encode("utf-8")     # 老版：光秃秃一行路径
        s2.write(QByteArray(legacy))
        s2.flush()
        pump(60)
        s2.disconnectFromServer()
        pump(150)
        legacy_ok = w2.added == [[os.path.normpath(VID)]]
        return (new_refused and legacy_ok,
                f"半截新帧作废={new_refused}（窗口动作={w.added or w.dropped or '无'}、"
                f"送达计数={h.delivered}）；旧形状挂断仍收到={legacy_ok}（{w2.added}）")
    finally:
        for one in socks:
            try:
                one.abort()
            except BaseException:
                pass
        srv_new.close()
        srv_old.close()




def j_wrong_nonce() -> tuple[bool, str]:
    """回执里的 nonce 不是这一次的：可能是上一次留下的半截管道，不能算数。"""
    srv = SilentServer(ack=b"OK 0000000000000000\n")
    try:
        ok = M.forward_to_running([VID], key=srv.key, nonce="aabbccddeeff0011",
                                  ack_ms=500, pump=APP.processEvents)
    finally:
        srv.close()
    return (ok is False, f"回的是别人的 nonce → 返回={ok}")


def j_right_nonce() -> tuple[bool, str]:
    h = M.Handoff()
    w = FakeWin()
    h.attach(w)
    srv = HandoffServer(h)
    try:
        t0 = time.time()
        ok = M.forward_to_running([VID], key=srv.key, ack_ms=2500, pump=APP.processEvents)
        dt = time.time() - t0
    finally:
        srv.close()
    return (ok is True and w.added == [[os.path.normpath(VID)]] and dt < 2.0,
            f"返回={ok} 耗时={dt:.2f}s 窗口收到={w.added}")


def j_queued_then_attach() -> tuple[bool, str]:
    """这一条就是用户那一幕：窗口还没装配完时投递进来，必须排队，attach 之后一条都不能少，
    而且**回执要等真落地才发**——不然发送方退了、文件还是没了。"""
    h = M.Handoff()
    srv = HandoffServer(h)                        # 此刻 h 手里没有窗口（旧写法：直接丢）
    w = FakeWin()
    QTimer.singleShot(250, lambda: h.attach(w))   # 模拟"装配完"就发生在这之后
    try:
        ok = M.forward_to_running([VID, VID2], key=srv.key, ack_ms=6000,
                                  pump=APP.processEvents)
    finally:
        srv.close()
    flat = [p for one in w.added for p in one]
    want = [os.path.normpath(VID), os.path.normpath(VID2)]
    front = w.calls[:2]
    return (ok is True and flat == want and front == ["showNormal", "activateWindow"],
            f"返回={ok} 排队={h.pending_count} → attach 后送达={w.added} 动作顺序={w.calls}")


def j_dropped_travel() -> tuple[bool, str]:
    """转交成功时，"这一趟丢掉的参数"也要跟着过去，由已经开着的窗口说给人听。"""
    h = M.Handoff()
    w = FakeWin()
    h.attach(w)
    srv = HandoffServer(h)
    try:
        ok = M.forward_to_running([VID], key=srv.key, dropped=[(GONE, "盘上没有这个文件")],
                                  ack_ms=2500, pump=APP.processEvents)
    finally:
        srv.close()
    flat = [d for one in w.dropped for d in one]
    return (ok and flat and "盘上没有" in str(flat), f"返回={ok} 窗口收到的丢档={w.dropped}")


def j_nonce_once() -> tuple[bool, str]:
    """同一 nonce 连投两次（回执丢过一次就会重投）：窗口只能收到一次，不能把同一个文件播两遍。"""
    h = M.Handoff()
    w = FakeWin()
    h.attach(w)
    srv = HandoffServer(h)
    try:
        n = "1234567890abcdef"
        ok1 = M.forward_to_running([VID], key=srv.key, nonce=n, ack_ms=2500,
                                   pump=APP.processEvents)
        ok2 = M.forward_to_running([VID], key=srv.key, nonce=n, ack_ms=2500,
                                   pump=APP.processEvents)
        ok3 = M.forward_to_running([VID2], key=srv.key, nonce="fedcba0987654321",
                                   ack_ms=2500, pump=APP.processEvents)
        APP.processEvents()
    finally:
        srv.close()
    return (ok1 and ok2 and ok3 and len(w.added) == 2,
            f"三次投递（第 2 次同 nonce）→ 窗口实际收到 {len(w.added)} 次 {w.added}")


def j_socket_held() -> tuple[bool, str]:
    """接收端的连接 socket 必须挂在 server 上：没有父的 QObject 随时可能被回收，
    半路断掉就成了"发的人以为发了、收的人没收到"。行为 + 源码（带留底对照）两条腿一起。"""
    h = M.Handoff()
    w = FakeWin()
    h.attach(w)
    srv = HandoffServer(h)
    got = []
    try:
        for _i in range(6):                       # 连投六条，少一条就是被 GC 咬了
            got.append(M.forward_to_running([VID], key=srv.key, ack_ms=2500,
                                             pump=APP.processEvents))
        APP.processEvents()
    finally:
        srv.close()
    src = "setParent" in open(MAIN_PY, encoding="utf-8").read()
    bak = newest_backup()
    old = ("setParent" in open(bak, encoding="utf-8").read()) if bak else None
    return (all(got) and len(w.added) == 6 and src and old is False,
            f"六投六达={all(got)} 收到 {len(w.added)} 次；源码 setParent 现版={src} 留底版={old}")



def newest_backup() -> str:
    """改前留底（`tests/out/pre-instance-*-main.py`）：源码形状那两条要有"旧写法必须不合格"的对照。"""
    d = os.path.join(ROOT, "tests", "out")
    cands = sorted((x for x in os.listdir(d) if x.startswith("pre-instance-")
                    and x.endswith("-main.py")), key=lambda x: os.path.getmtime(os.path.join(d, x)))
    return os.path.join(d, cands[-1]) if cands else ""


def order_ok(src: str) -> bool:
    a, b, c = src.find("listen(handoff"), src.find("MainWindow(cfg"), src.find("handoff.attach(")
    return -1 not in (a, b, c) and a < b < c


def j_listen_before_window() -> tuple[bool, str]:
    """装配顺序：先开门（listen）再装窗口，中间那段装配期由 Handoff 排队顶着。
    读源码的判据不配 monkeypatch 证伪（补丁改不了盘上文本），所以这里自带对照：
    同一条谓词跑**改前留底**那份，必须判它不合格——红不出来就是这条尺没牙。"""
    src = open(MAIN_PY, encoding="utf-8").read()
    now = order_ok(src)
    bak = newest_backup()
    if not bak:
        return False, "没有 pre-instance-*-main.py 留底，这条对照跑不了（不许拿「没对照」当通过）"
    old = order_ok(open(bak, encoding="utf-8").read())
    return (now and not old,
            f"现版合格={now}／留底版（{os.path.basename(bak)}）合格={old}——"
            f"旧写法是 listen 排在 MainWindow 之后且没有 attach，投递窗口期=整个装配期")



def j_launch_log() -> tuple[bool, str]:
    p = M.log_launch("forward", ["%1", VID], [os.path.normpath(VID)], [(GONE, "盘上没有")],
                     ["--hwdec"])
    if not p or not os.path.isfile(p):
        return False, f"log_launch 没落盘（返回={p!r}）"
    raw = open(p, "rb").read()
    lines = [x for x in raw.decode("utf-8", "ignore").splitlines() if x.strip()]
    last = lines[-1] if lines else ""
    ok = ("%1" in last and os.path.basename(VID) in last and "盘上没有" in last
          and "forward" in last and b"\r" not in raw)
    return (ok, f"{p} 共 {len(lines)} 行，最后一行：{last[:220]}")


def j_log_rotation() -> tuple[bool, str]:
    p = ""
    keep = int(M.LAUNCH_LOG_KEEP)
    for i in range(keep + 80):
        p = M.log_launch("cold", [f"probe-{i}"], [], [], [])
    lines = [x for x in open(p, encoding="utf-8", errors="ignore").read().splitlines() if x.strip()]
    return (len(lines) <= keep, f"写了 {keep + 80} 次之后盘上剩 {len(lines)} 行（上限 {keep}）")


def j_notice_points() -> tuple[bool, str]:
    """空状态页要能把"这次为什么没放起来"写在脸上——OSD 挂在隐藏的视频容器上，看不见。"""
    from app.ui.main_window import MainWindow
    win = MainWindow(M.cfgmod.Config())
    win.show()
    APP.processEvents()
    try:
        win.notify_external_drop([(GONE, "盘上没有这个文件")])
        APP.processEvents()
        shown = win.empty.notice.isVisible() and "盘上没有" in win.empty.notice.text()
        base = os.path.basename(GONE) in win.empty.notice.text()
        win.notify_external_drop([])
        APP.processEvents()
        hidden = not win.empty.notice.isVisible()
    finally:
        win.close()
        APP.processEvents()
    return (shown and base and hidden,
            f"点名可见={shown} 带文件名={base} 空名单时收起={hidden}")


def j_add_paths_visible() -> tuple[bool, str]:
    """`add_paths` 走到"没有找到可播放的文件"那一支时，用户必须看得见这句话。"""
    from app.ui.main_window import MainWindow
    win = MainWindow(M.cfgmod.Config())
    win.show()
    APP.processEvents()
    try:
        win.add_paths([NOEXT])                    # 存在，但不是媒体
        APP.processEvents()
        txt = win.empty.notice.text()
        ok = win.empty.isVisible() and bool(txt) and ("没有" in txt or "不能" in txt)
    finally:
        win.close()
        APP.processEvents()
    return (ok, f"空状态页文字={txt!r}（以前这句只进了隐藏的 OSD）")


def j_osd_routes() -> tuple[bool, str]:
    """通用那条：视频容器还藏着的时候，`_osd` 的话要抄一份到能看见的地方；播放中不许抄。"""
    from app.ui.main_window import MainWindow
    win = MainWindow(M.cfgmod.Config())
    win.show()
    APP.processEvents()
    try:
        win._osd("没有找到可播放的文件")
        APP.processEvents()
        routed = "没有找到" in win.empty.notice.text() and win.empty.notice.isVisible()
        win.video.set_empty(False)
        win.empty.setVisible(False)
        APP.processEvents()
        win._osd("已载入 1 个文件")
        APP.processEvents()
        quiet = not win.empty.notice.isVisible()
    finally:
        win.close()
        APP.processEvents()
    return (routed and quiet, f"抄到空状态页={routed} 播放中不打扰={quiet}")


def j_remove_server_facts() -> tuple[bool, str]:
    """本机 Qt 实测：`removeServer` 到底拆不拆得动正在听的 server。
    这条不判产品对错，只把"那句无条件调用有没有害"记成事实——拆不动就是无害的多余动作，
    不许把它写成用户那次故障的根因（机制假设没量过就不进账）。"""
    h = M.Handoff()
    w = FakeWin()
    h.attach(w)
    srv = HandoffServer(h)
    try:
        before = srv.listening
        QLocalServer.removeServer(srv.key)
        pump(300)
        still = srv.listening
        ok = M.forward_to_running([VID], key=srv.key, ack_ms=2500, pump=APP.processEvents)
    finally:
        srv.close()
    return (before and ok is True,
            f"拆之前 isListening={before} 之后={still} 还能收到={ok}"
            f"（本机是 Windows 命名管道那一路；跨进程未测，所以只记「那句无害」）")


ITEMS = [
    ("参数形状", "正常一条路径：收下、不丢、不误报", j_plain),
    ("参数形状", "多余一层引号的路径还认得是同一个文件", j_clean_quoted),
    ("参数形状", "注册表里没展开的 %1 / %~1 要点名，不许静默丢", j_placeholder),
    ("参数形状", "被空格劈成几段的路径拼回真文件", j_fragments),
    ("参数形状", "相对路径补成绝对再交出去", j_relative),
    ("参数形状", "真不存在的绝对路径：丢，并给出原因", j_missing),
    ("参数形状", "好坏混着来时顺序不变、坏的逐条点名", j_order_mixed),
    ("参数形状", "选项类参数不进路径、也不算丢文件", j_options),
    ("参数形状", "反向哨兵：0 参数冷启动一个警都不报", j_no_args),
    ("转交协议", "载荷发出去能原样读回来（含丢弃原因与旧形状）", j_wire),
    ("转交协议", "没人听时快速判否", j_no_server),
    ("转交协议", "对方只 accept 不回执 → 必须判否（旧写法在此说谎）", j_accept_without_ack),
    ("转交协议", "对面活着但不回执 → 等满回执窗口才判否", j_hold_without_ack),
    ("转交协议", "帧到齐只认首行声明的字节数（正反两向）", j_fragmented_frame),
    ("转交协议", "四种帧状态各归各位：wait 与 legacy 不许混（六档）", j_frame_state_split),
    ("转交协议", "载荷分片递进来时接收端要等齐再动（真管道）", j_frame_e2e),
    ("转交协议", "半截新帧作废、旧形状挂断仍收（两向）", j_truncated_discard),
    ("转交协议", "回执 nonce 对不上 → 判否", j_wrong_nonce),
    ("转交协议", "回执 nonce 对得上 → 判真，且窗口真收到", j_right_nonce),
    ("转交协议", "窗口没装配完时排队，attach 后一条不少，回执等真落地", j_queued_then_attach),
    ("转交协议", "转交时丢档原因跟着过去，由已开的窗口说", j_dropped_travel),
    ("转交协议", "同一 nonce 重投只生效一次", j_nonce_once),
    ("转交协议", "接收端连接 socket 挂父（六投六达 + 源码）", j_socket_held),
    ("装配顺序", "listen 在 MainWindow 之前、attach 在之后（源码死判据）", j_listen_before_window),
    ("留痕", "launch.log 一行讲清收到什么、丢什么、走哪条路", j_launch_log),
    ("留痕", "launch.log 有上限，不会越长越大", j_log_rotation),
    ("可见解释", "空状态页能点名这次为什么没放起来", j_notice_points),
    ("可见解释", "add_paths 走到空列表那一支时话要看得见", j_add_paths_visible),
    ("可见解释", "_osd 在视频容器藏着时抄一份到能看见的地方", j_osd_routes),
    ("本机事实", "removeServer 拆不拆得动正在听的 server（实测记档）", j_remove_server_facts),
]
JUDGES = {name: fn for _g, name, fn in ITEMS}


# ---------------------------------------------------------------- 证伪
def patch(obj, attr, value):
    old = getattr(obj, attr)
    setattr(obj, attr, value)
    return (obj, attr, old)


def unpatch(rec) -> None:
    obj, attr, old = rec
    setattr(obj, attr, old)


def old_style_forward(paths, key=None, nonce=None, connect_ms=300, write_ms=500,
                      ack_ms=1500, pump=None, dropped=None) -> bool:
    """用户遇到的那一版：写完字节就宣布"交出去了"，不看回执、不等落地。"""
    sock = QLocalSocket()
    sock.connectToServer(key or M.INSTANCE_KEY)
    if not sock.waitForConnected(connect_ms):
        return False
    sock.write(QByteArray(M.build_handoff(list(paths), [], nonce or "x")))
    sock.waitForBytesWritten(write_ms)
    sock.disconnectFromServer()
    return True


def old_receive(self, nonce, kept, dropped, sock=None) -> str:
    """旧接收方：没有窗口就当这批东西不存在。"""
    if self.win is None:
        return "dropped"
    self.win.showNormal()
    self.win.activateWindow()
    if kept:
        self.win.add_paths(kept)
    return "delivered"


def no_dedup_receive(self, nonce, kept, dropped, sock=None) -> str:
    """去掉 nonce 去重：同一批路径投两次就播两遍。"""
    if self.win is None:
        self.pending.append((nonce, list(kept), list(dropped or []), sock))
        self.seen.append(nonce)
        return "queued"
    self.seen.append(nonce)
    self._deliver(list(kept), list(dropped or []))
    return "delivered"


def probe_receive_no_window() -> str:
    return M.Handoff().receive("n1", [VID], [], None)


def probe_nonce_twice() -> int:
    h = M.Handoff()
    w = FakeWin()
    h.attach(w)
    h.receive("same", [VID], [], None)
    h.receive("same", [VID], [], None)
    return len(w.added)


def probe_log() -> int:
    p = M.log_launch("falsify-probe", ["x"], [], [], [])
    return 1 if p and os.path.isfile(p) else 0


def probe_notice() -> str:
    """在真窗口上跑一句 `notify_external_drop`，回 notice 的文字（补丁没落上一眼看得穿）。"""
    from app.ui.main_window import MainWindow
    win = MainWindow(M.cfgmod.Config())
    win.show()
    APP.processEvents()
    try:
        win.notify_external_drop([("某个路径", "某个原因")])
        APP.processEvents()
        return win.empty.notice.text()
    finally:
        win.close()
        APP.processEvents()


def probe_osd_route() -> str:
    from app.ui.main_window import MainWindow
    win = MainWindow(M.cfgmod.Config())
    win.show()
    APP.processEvents()
    try:
        win._osd("没有找到可播放的文件")
        APP.processEvents()
        return win.empty.notice.text()
    finally:
        win.close()
        APP.processEvents()


def probe_collect_empty() -> list:
    return [list(x) for x in M.collect_paths([])]


def probe_frame() -> list:
    """帧判据的两个方向各取一次：整帧该说"到齐"，只有头/缺尾巴该说"还没"。"""
    full = M.build_handoff([VID, VID2], [(GONE, "盘上没有")], "e" * 16)
    head = full[:full.index(b"\n") + 1]
    return [M.frame_ready(full), M.frame_ready(head), M.frame_ready(full[:-5])]


MUTATIONS: list[tuple] = []


def build_mutations() -> list[tuple]:
    """延迟到 --falsify 时再造这张表：表里要引用 `M.Handoff` 这些**产品侧对象**，
    在 import 期就取名字的话，量具会先于判据崩掉（实发：改前那版 main.py 没有 Handoff，
    整份验收连一条都没跑就 AttributeError）。"""
    return [
    ("剥引号摘掉（照字面当路径）", M, "clean_arg", lambda s: s,
     lambda: M.clean_arg('"a.mkv"'), ["多余一层引号的路径还认得是同一个文件"]),
    ("空格劈开的路径不拼回", M, "join_fragments", lambda toks, i, limit=4: None,
     lambda: M.join_fragments(VID.split(" "), 0),
     ["被空格劈成几段的路径拼回真文件"]),
    ("%1 不当成没展开的占位符", M, "PCT_TOKENS", (),
     lambda: M.collect_paths(["%1"])[1],
     ["注册表里没展开的 %1 / %~1 要点名，不许静默丢"]),
    ("缓冲一有空就当整帧到齐（atEnd 语义）", M, "frame_state", lambda buf: "ready",
     probe_frame, ["帧到齐只认首行声明的字节数（正反两向）",
                   "四种帧状态各归各位：wait 与 legacy 不许混（六档）",
                   "载荷分片递进来时接收端要等齐再动（真管道）",
                   "半截新帧作废、旧形状挂断仍收（两向）"]),
    ("帧永远判不到齐（只在挂断后处理，回执等到天荒地老）", M, "frame_state",
     lambda buf: "wait", probe_frame,
     ["帧到齐只认首行声明的字节数（正反两向）",
      "四种帧状态各归各位：wait 与 legacy 不许混（六档）",
      "载荷分片递进来时接收端要等齐再动（真管道）",
      "回执 nonce 对得上 → 判真，且窗口真收到"]),
    ("半截帧也照 parse（有多少算多少）", M, "frame_state",
     lambda buf: "legacy" if b"\n" in buf else "headless", probe_frame,
     ["半截新帧作废、旧形状挂断仍收（两向）",
      "四种帧状态各归各位：wait 与 legacy 不许混（六档）"]),
    ("不看回执就宣布已送达（旧写法）", M, "forward_to_running", old_style_forward,
     lambda: probe_forward(ack=b"", hold=True),
     ["对方只 accept 不回执 → 必须判否（旧写法在此说谎）",
      "对面活着但不回执 → 等满回执窗口才判否",
      "回执 nonce 对不上 → 判否",
      "窗口没装配完时排队，attach 后一条不少，回执等真落地"]),
    ("窗口没装配完就把路径丢掉（旧写法）", M.Handoff, "receive", old_receive,
     probe_receive_no_window,
     ["窗口没装配完时排队，attach 后一条不少，回执等真落地"]),
    ("nonce 不去重（同一批播两遍）", M.Handoff, "receive", no_dedup_receive,
     probe_nonce_twice, ["同一 nonce 重投只生效一次"]),
    ("留痕换成空函数", M, "log_launch", lambda *a, **k: "",
     probe_log, ["launch.log 一行讲清收到什么、丢什么、走哪条路",
                 "launch.log 有上限，不会越长越大"]),
    ("点名只进隐藏的 OSD（旧写法）", None, "notify", None,
     probe_notice,
     ["空状态页能点名这次为什么没放起来"]),
    ("_osd 不抄到空状态页（旧写法）", None, "osd", None,
     probe_osd_route,
     ["_osd 在视频容器藏着时抄一份到能看见的地方",
      "add_paths 走到空列表那一支时话要看得见"]),
    ("空启动也瞎报警", M, "collect_paths",
     lambda argv: (list(argv), [("(空)", "一律当丢的文件")], []),
     probe_collect_empty,
     ["反向哨兵：0 参数冷启动一个警都不报", "选项类参数不进路径、也不算丢文件"]),
]


def falsify() -> int:
    print("\n== 证伪：把实现换成常见错误写法，对应判据必须翻红 ==")
    from app.ui.main_window import MainWindow
    tally = {"CAUGHT": 0, "MISSED": 0, "NOP": 0}
    # 后两条要在真窗口类上动手，先单独登记补丁位置（方法名写死在代码里，改名就必须红）
    extra = {
        "notify": (MainWindow, "notify_external_drop", lambda self, dropped: None),
        "osd": (MainWindow, "_osd",
                lambda self, text: self.osd.show_text(text) if text else None),
    }
    table = build_mutations()
    for name, obj, attr, value, probe, want in table:
        if obj is None:
            obj, attr, value = extra[attr]
        try:
            base = probe()
        except BaseException as exc:
            say("NOP", f"注入「{name}」", f"探针在改之前就崩了，这条不作数：{type(exc).__name__}")
            tally["NOP"] += 1
            continue
        rec = patch(obj, attr, value)
        try:
            mutated = probe()
        except BaseException:
            mutated = "<崩了>"
        if mutated == base:
            unpatch(rec)
            say("NOP", f"注入「{name}」", f"补丁没落上：改前改后一模一样（{base!r}）")
            tally["NOP"] += 1
            continue
        bad = []
        try:
            for k in want:
                fn = JUDGES.get(k)
                if fn is None:
                    bad.append(f"{k}（判据名对不上）")
                    continue
                try:
                    ok, why = fn()
                except BaseException as exc:
                    ok, why = False, f"崩了 {type(exc).__name__}: {exc}"
                if ok:
                    bad.append(f"{k}：{why[:90]}")
        finally:
            unpatch(rec)
            APP.processEvents()
        if bad:
            tally["MISSED"] += 1
            say("MISSED", f"注入「{name}」之后仍绿：{len(bad)}/{len(want)} 条", "；".join(bad))
        else:
            tally["CAUGHT"] += 1
            say("CAUGHT", f"注入「{name}」→ {len(want)} 条全部翻红",
                "、".join(w[:24] for w in want))
    total = sum(tally.values())
    print(f"\n[FINAL] 证伪 {total} 条 —— CAUGHT {tally['CAUGHT']}／MISSED {tally['MISSED']}／"
          f"NOP {tally['NOP']}", flush=True)
    print("DONE", flush=True)
    return 1 if tally["MISSED"] or tally["NOP"] else 0


# ---------------------------------------------------------------- 入口
def main() -> int:
    global OUT_PATH
    out_dir = os.path.join(ROOT, "tests", "out")
    os.makedirs(out_dir, exist_ok=True)
    OUT_PATH = os.path.join(out_dir, "test-instance-"
                            + datetime.datetime.now().strftime("%m%d-%H%M%S") + ".log")
    say("NOTE", "日志落这里", OUT_PATH)
    say("NOTE", "样本", VID)
    groups: dict[str, list] = {}
    for g, name, fn in ITEMS:
        groups.setdefault(g, []).append((name, fn))
    for g, items in groups.items():
        print(f"\n== {g}（{len(items)} 条）==", flush=True)
        for name, fn in items:
            try:
                ok, why = fn()
            except BaseException as exc:
                ok, why = False, f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            say("PASS" if ok else "FAIL", name, why)
    pump(50)
    rc = 1 if count("FAIL") else (2 if count("ENV") else 0)
    say("FINAL", f"共 {len(ITEMS)} 条判据：PASS {count('PASS')}／FAIL {count('FAIL')}／"
        f"ENV {count('ENV')}／NOTE {count('NOTE')}", f"rc={rc}")
    print("DONE", flush=True)
    try:
        with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("DONE\n")
    except OSError:
        pass
    return rc


if __name__ == "__main__":
    try:
        sys.exit(falsify() if "--falsify" in sys.argv else main())
    except SystemExit:
        raise
    except BaseException:
        traceback.print_exc()
        print("FINAL 量具自己崩了 —— 这一趟不作数 rc=2", flush=True)
        sys.exit(2)
