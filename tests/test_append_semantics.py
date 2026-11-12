"""「添加」到底做了什么——播放列表追加语义的验收与证伪（2026-10-17 那两笔修复的账）。

起因（用户原话）："双击视频确实打开了播放器，但视频根本没加载进来……你也再看看还有没有
其他问题，都给我改好"。顺着"打开方式／双击"这条线往外查，在**播放列表那一页**撞见四笔：

1. 侧栏"添加媒体文件"与"把文件拖到列表上"发的是 `sig_add_paths`，而它当年直连
   `MainWindow.add_paths`——那里的 `replace` 默认 True。按钮写着"添加"，做的是"整表换成
   新挑的那几个"：一支十几条的列表，点一次"添加"就没了。
2. 把接线改成追加之后，空列表上点"添加"变成"文件进了列表却一动不动"。这不是猜的，是
   `tests/probe_append_idle.py` 在**本机同一版内核**（v0.41.0-1101-g5d85ba5fb）上量出来的：
   空列表 `loadfile x append` 之后 `playlist-pos` 仍是 −1、`idle-active` 仍 True；
   改用 `append-play` 又会在**已经在播**时跳到新条目（pos 0→1）——那是"改播这一条"，
   不是"加到队尾"。两头都不对，所以"当前有没有条目"只能我们自己判。
3. 修 1／2 时在同一段里抓到 `if play_index:`——第 0 条永远是假值，"重启后恢复到上次那条"
   如果上次就是第 0 条，这一句会静默什么都不做。
4. 1／2／3 都修好之后，真机（屏幕亮了的 08:41 那一趟）仍然四条红，症状和第 1 笔一模一样。
   根因在**缓存**那一层：`_on_start_file` 当年把 `playlist-pos` 列进 `_FILE_SCOPED`
   "换文件时抹掉防陈旧"，可它是"列表里第几条"、不是"这支文件的多长时间"——抹掉之后
   内核在"播放中往队尾追加"这一档**不会**再推第二发（值没变），于是缓存空着，
   `open_files` 读成 −1＝"以为没在播"＝第一条发成 replace。前面 1／2／3 是接线与分支写错，
   这一笔是**分支的输入被人抹了**，所以 1 修完还会复发。E18 钉的就是这条依赖。
   同一张表里另外三格（`loop-file`／`ab-loop-a`／`ab-loop-b`）经真内核量过是同一形状——
   换文件时值不变、内核也不推第二发（`tests/out/probe-globals-across-file.txt` ③④），
   所以一起从那张表里拿掉了，E19 钉它们；ab-loop 那两格的后果是界面级的：
   换一支片子后调节页会写「A-B：未设置」，而内核其实还在按上一支那两个点循环。

这一趟量的是**我们发出去的命令与接线**：引擎侧把 `Player._fire` 换成记录器（不发 IPC、
不起内核，前面的判断一行都不动）；接线侧读的是 `MainWindow` 自己的源码（AST），
它只能证明"信号连到哪个槽、槽里传的什么参数"，真按一次按钮那一腿在 `tests/test_ui.py`。
内核收到这些命令之后 behave 成什么样，由 `tests/out/probe-append-idle.txt` 那份实测负责；
本文件把那份实测结论当作**推演器的规则**复用一次，用来判"空列表点添加之后，用户看到的
是'在播'还是'停在封面'"。真内核那一腿另有 e2e，各管各的账。

跑法：
    python tests/test_append_semantics.py
    python tests/test_append_semantics.py --falsify   # 每种坏写法必须各自把对应判据顶红

退出码：0 全绿（--falsify 时＝变异表全部被抓）；1 至少一条真失败；2 环境不给。
"""
from __future__ import annotations

import ast
import datetime
import inspect
import io
import os
import sys
import textwrap

if hasattr(sys.stdout, "reconfigure"):     # 本机控制台默认 GBK：判据里一个"−"（U+2212）就能把
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 整趟劈死在 print 上，
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")   # 还被兜成"这一趟不作数"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# GELU_DATA 必须在 import app.config **之前**指好：DATA_DIR 是 import 时求值的模块级量，
# 它还决定 watch-state 落在哪儿——C 组要写进度，不许碰用户真正的观看进度表。
TMP = os.path.join(os.environ.get("TEMP", "C:" + chr(92) + "temp"), f"gelu-append-{os.getpid()}")
os.makedirs(os.path.join(TMP, "logs"), exist_ok=True)
os.environ["GELU_DATA"] = TMP
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RESULTS: list[list[str]] = []
OUT_PATH = ""
_SEQ = 0                                    # 每个 Run 一个独立数据目录的编号（见 Runner 的说明）
FALSIFY = "--falsify" in sys.argv

SAMPLES = os.path.join(HERE, "samples")
A = os.path.join(SAMPLES, "gelu-sample-1.mp4")
B = os.path.join(SAMPLES, "gelu-sample-2.mp4")
C = os.path.join(SAMPLES, "gelu-sample-3.mkv")
GONE = os.path.join(SAMPLES, "这文件盘上根本没有.mp4")

ENGINE_ERR = ""
UI_ERR = ""
try:
    from app import config as cfgmod
    import app.engine.player as P
    from app.engine.player import MpvPlayer
except BaseException as _exc:                       # noqa: BLE001 - 起不来要记 ENV，不许崩
    cfgmod = None
    P = None
    MpvPlayer = None
    ENGINE_ERR = f"{type(_exc).__name__}: {_exc}"
try:
    import app.ui.main_window as MW
    from app.ui.main_window import MainWindow
except BaseException as _exc:                       # noqa: BLE001
    MW = None
    MainWindow = None
    UI_ERR = f"{type(_exc).__name__}: {_exc}"


def say(tag: str, what: str, why: str = "") -> None:
    RESULTS.append([tag, what, why])
    line = f"[{tag}] {what}" + (f" —— {why}" if why else "")
    print(line, flush=True)
    if OUT_PATH:
        try:
            with io.open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def count(tag: str) -> int:
    return sum(1 for r in RESULTS if r[0] == tag)


# ============================================================ 引擎侧：真对象＋假传输
class Runner:
    """`Runner(func=None)`＝用产品现在的 `open_files`；给了 func＝用被改坏的那一份。

    只摘掉**最后一步发送**（`_fire` 换成记录器）。不换成整类桩：那样连
    "读订阅缓存判当前条目""按配置决定续播"这些判断都会变成我自己写的，
    判据就只是在给我的桩盖章。

    每个 Run 占一个**自己的**数据目录：`watch-state.json` 与 `config.json` 的最近文件都是
    跨调用累积的，而 E11–E16 要量的正是"有没有进度""recent 记没记"。第一版让它们共用一份
    目录，结果 E11 写进去的 30 秒被 E13 当成"已有进度"读了出来、E5 推进 recent 的 B 被
    E16 当成"remember=False 也记了"——两条红都是我的量具自己造的现场，不是产品。
    `Config.watch_state_path` 每次调用都现取 `cfgmod.DATA_DIR`，所以在构造前换目录就够了。
    """

    def __init__(self, func=None, pos=None, resume=True, remembered=None, start_file=None):
        global _SEQ
        _SEQ += 1
        d = os.path.join(TMP, f"run-{_SEQ}")
        os.makedirs(d, exist_ok=True)
        cfgmod.DATA_DIR = d
        cfg = cfgmod.Config(os.path.join(d, "config.json"))
        cfg["resume"] = resume
        pl = MpvPlayer(cfg)                          # 构造只填字段，不起内核
        pl._props = {} if pos is None else {"playlist-pos": pos}
        log: list[list] = []
        pl._fire = lambda cmd: log.append(cmd)
        if start_file is not None:                   # 只换"换文件时清哪几格缓存"这一步
            pl._on_start_file = start_file.__get__(pl, MpvPlayer)
        if remembered:                               # 进度走产品自己的写盘口，不手搓表
            for p, (at, dur) in remembered.items():
                cfg.remember_position(p, at, dur)
        self.player = pl
        self.cfg = cfg
        self.log = log
        self.func = func
        self.dir = d
        self.res: dict = {}          # open_files 那句返回值（界面拿它决定去等哪一种回执）

    def go(self, paths, **kw):
        f = self.func.__get__(self.player, MpvPlayer) if self.func else self.player.open_files
        self.res = f(paths, **kw) or {}
        return self


def has(r: Runner, *prefix) -> bool:
    return any(c[:len(prefix)] == list(prefix) for c in r.log)


def lf(r: Runner) -> list[list]:
    return [c for c in r.log if c[:1] == ["loadfile"]]


def mode_of(c: list) -> str:
    return str(c[2]) if len(c) > 2 else ""


def start_of(c: list) -> str | None:
    """这条 loadfile 里的 options 串——只认**内核收得下的那一档**（裸字符串）。

    为什么不能像旧版那样数"`c[3][0]` 里的 start= 字样"：2026-10-19 那次双击，E11 是**绿的**，
    内核却把整条命令退回去了（`Command loadfile: argument index has incompatible type.`）——
    旧尺读的就是那个被拒的列表形态。文本匹配全绿、消费它的程序不认，这一族盲区必须靠
    `start_shape_ok` 那把尺补：它按这台内核的位置参数表逐位核（`<url> <flags> <index> <options>`）。
    """
    if len(c) < 5 or not isinstance(c[4], str):
        return None
    return c[4] if c[4].startswith("start=") else None


def start_shape_ok(c: list) -> tuple[bool, str]:
    """带续播位置的那一条，形状必须是真内核量出来的那一条（E21 的谓词，纯函数）。

    判决出处：`tests/probe_loadfile_index.py`，留档
    `tests/out/probe-loadfile-index-v3-1019-205812.txt`（内核 v0.41.0-1101）：
    · `["loadfile", url, "replace", ["start=3.000"]]` ＝ 当年产品写法 → **拒**；
    · `["loadfile", url, "replace", -1, ["start=3.000"]]` → 拒（列表还是落在 index 那一位）；
    · `["loadfile", url, "replace", -1, "start=3.000"]` → **收下**，位置从 3.0 起；
    · `{"start": …}`／`{"name":…,"args":…}` 命名形态 → 同样拒。
    不带位置的那一条（`[url, mode]`）本来就可用，直接放行。
    """
    if c[:1] != ["loadfile"]:
        return False, f"这条根本不是 loadfile：{c}"
    if len(c) == 3:
        return True, "不带选项（没有续播位置时就该是这个形状）"
    if len(c) != 5:
        return False, f"位置数={len(c)}（要么 3 段、要么 5 段，多一段都会落进 index）：{c}"
    if not isinstance(c[3], int):
        return False, f"第 4 位（index）不是整数 −1 而是 {c[3]!r}：内核就是在这判 incompatible"
    if c[3] != -1:
        return False, f"index={c[3]!r}（要 −1＝不动当前条目）"
    if not isinstance(c[4], str):
        return False, f"options 必须是裸字符串，这里是 {type(c[4]).__name__}：{c[4]!r}"
    if not c[4].startswith("start="):
        return False, f"options 内容不像定位项：{c[4]!r}"
    return True, "形状＝<url> <flags> −1 \"start=…\"（量具里唯一被这台内核收下的一档）"


# ------------------------------------------------------------ 内核行为推演器
# 规则全部来自本机实测留档 tests/out/probe-append-idle.txt（同一版内核 v0.41.0-1101）：
#   空列表 `append` → 条目进列表、`playlist-pos` 仍 −1（不播）
#   空列表 `replace`／`append-play` → `playlist-pos` 0（播）
#   播放中 `append` → 排队尾、pos 不动
#   播放中 `append-play` → 跳到新条目
# 它是**推演**不是真内核：这一趟只回答"我们发出去这串命令会落到哪个 pos"，
# 真内核那一腿在 tests/e2e_gpu.py 与 tests/e2e_instance_handoff.py。
def sim_pos(r: Runner) -> int:
    pos = r.player.prop("playlist-pos", -1)
    pos = pos if isinstance(pos, int) else -1
    n = 0
    for c in r.log:
        if c[:1] != ["loadfile"]:
            continue
        m = mode_of(c)
        if m == "replace":
            pos = 0
            n = 1
        elif m == "append-play":
            pos = 0 if n == 0 else n
            n += 1
        elif m == "append":
            n += 1
    for c in r.log:
        if c[:2] == ["set_property", "playlist-pos"]:
            pos = c[2]
    return pos


# ============================================================ 接线侧：读产品自己的源码
def _tree(src: str) -> ast.Module:
    return ast.parse(textwrap.dedent(src))


def connect_target(init_src: str, sig: str = "sig_add_paths") -> str:
    """`self.panel.sig_add_paths.connect(X)` 里那个 X 的原话（找不到回空串）。

    走 AST 而不是 `in` 一句文本：`connect(self.add_paths)` 与注释里提到 `add_paths`
    的两种形状，文本尺一视同仁，AST 只认"这个信号的 connect 调用参数"。
    """
    want = f".{sig}.connect"
    for node in ast.walk(_tree(init_src)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "connect" \
                and isinstance(node.func.value, ast.Attribute) \
                and node.func.value.attr == sig:
            if node.args:
                return ast.unparse(node.args[0])
    return ""


def kw_in_func(fname: str, key: str, src: str | None = None):
    """某个方法体里 `self.<fname 之外的调用>` 中名为 key 的关键字取值（取不到回哨兵 "〈无〉"）。"""
    body = src if src is not None else inspect.getsource(getattr(MainWindow, fname))
    for node in ast.walk(_tree(body)):
        if isinstance(node, ast.Call):
            for kb in node.keywords:
                if kb.arg == key:
                    try:
                        return ast.literal_eval(kb.value)
                    except (ValueError, TypeError):
                        return ast.unparse(kb.value)
    return "〈无〉"


# ============================================================ 判据表
def c_empty_two(w):
    r = w.mk(pos=None)
    r.go([A, B], append=True)
    want = [["loadfile", A, "replace"], ["loadfile", B, "append"]]
    return lf(r) == want, f"发出={lf(r)} 应为={want}"


def c_live_no_steal(w):
    r = w.mk(pos=0)
    r.go([A, B], append=True)
    want = [["loadfile", A, "append"], ["loadfile", B, "append"]]
    steal = has(r, "set_property", "playlist-pos")
    return lf(r) == want and not steal, f"发出={lf(r)} 应为={want} 还发了抢播命令={steal}"


def c_live_pos_stays(w):
    r = w.mk(pos=0)
    r.go([B], append=True)
    return sim_pos(r) == 0, f"按实测语义推演 pos={sim_pos(r)}（加到队尾＝当前条目不许变）"


def c_empty_one(w):
    r = w.mk(pos=None)
    r.go([A], append=True)
    ok = lf(r) == [["loadfile", A, "replace"]] and sim_pos(r) >= 0
    return ok, f"发出={lf(r)} 推演 pos={sim_pos(r)}（空列表点一次添加必须 ≥0，否则就是停在封面）"


def c_replace(w):
    r = w.mk(pos=0)
    r.go([A, B], append=False)
    want = [["loadfile", A, "replace"], ["loadfile", B, "append"]]
    return lf(r) == want, f"发出={lf(r)} 应为={want}（双击／窗口拖放走这条：整表换成新挑的那几个）"


def c_index_zero(w):
    r = w.mk(pos=None)
    r.go([A, B], append=False, play_index=0)
    ok = has(r, "set_property", "playlist-pos", 0)
    return ok, f"命令={r.log} 里{'有' if ok else '没有'} playlist-pos=0（`if play_index:` 把 0 当没传）"


def c_index_none(w):
    r = w.mk(pos=None)
    r.go([A, B], append=False)
    return not has(r, "set_property", "playlist-pos"), f"命令={r.log}（没给 play_index 就不许多发）"


def c_index_two(w):
    r = w.mk(pos=None)
    r.go([A, B, C], append=False, play_index=2)
    return has(r, "set_property", "playlist-pos", 2), f"末条命令={r.log[-1:]}（对照组：0 那条不是靠少发蒙过去的）"


def c_missing(w):
    r = w.mk(pos=0)
    r.go([GONE], append=True)
    return not lf(r) and r.player.last_files == [], f"发出={r.log} last_files={r.player.last_files}"


def c_relative(w):
    r = w.mk(pos=0)
    rel = os.path.relpath(A, ROOT)
    cwd = os.getcwd()
    os.chdir(ROOT)                                # 相对路径过得了我们这边的 exists()
    try:
        r.go([rel], append=False, remember=False)
    finally:
        os.chdir(cwd)
    got = lf(r)[0][1] if lf(r) else "（没发）"
    return got == os.path.abspath(A), f"送出去={got!r} 应为={os.path.abspath(A)!r}（原样递相对路径＝到内核那边不存在）"


def c_resume_hit(w):
    r = w.mk(pos=None, remembered={A: (30.0, 120.0)})
    if r.cfg.recall_position(A) != 30.0:                       # 前提当场断言
        return False, f"前提立不住：写进去的进度读回来是 {r.cfg.recall_position(A)}（要 30.0），{r.dir}"
    r.go([A], append=False)
    s = start_of(lf(r)[0]) if lf(r) else None
    return s == "start=30.000", f"第一条={lf(r)[0] if lf(r) else '（没发）'} 期望带 start=30.000"


def c_resume_off(w):
    r = w.mk(pos=None, resume=False, remembered={A: (30.0, 120.0)})
    if r.cfg["resume"] is not False or r.cfg.recall_position(A) != 30.0:
        return False, f"前提立不住：resume={r.cfg['resume']!r} 进度={r.cfg.recall_position(A)}" \
                     f"（这一条要的是「关了续播但确实有进度」）"
    r.go([A], append=False)
    return start_of(lf(r)[0]) is None if lf(r) else False, \
        f"第一条={lf(r)[0] if lf(r) else '（没发）'}（关了续播不许还带 start）"


def c_no_history(w):
    r = w.mk(pos=None)
    if r.cfg.recall_position(A) != 0.0:                        # 前提：这一条量的就是"没有进度"
        return False, f"前提立不住：新数据目录里 {os.path.basename(A)} 已带进度 " \
                     f"{r.cfg.recall_position(A)}（{r.dir}）——那量的不是「没进度」那一档"
    r.go([A], append=False)
    return start_of(lf(r)[0]) is None if lf(r) else False, \
        f"第一条={lf(r)[0] if lf(r) else '（没发）'}（没进度就别塞 start=0.000）"


def c_append_first_resumes(w):
    r = w.mk(pos=None, remembered={A: (45.0, 120.0)})
    if r.cfg.recall_position(A) != 45.0:
        return False, f"前提立不住：进度读回来是 {r.cfg.recall_position(A)}（要 45.0），{r.dir}"
    r.go([A], append=True)
    s = start_of(lf(r)[0]) if lf(r) else None
    return s == "start=45.000", f"第一条={lf(r)[0] if lf(r) else '（没发）'} 期望带 start=45.000（新那条路也要能续播）"


def c_last_files(w):
    r = w.mk(pos=0)
    r.go([GONE, B], append=True)
    return r.player.last_files == [B], f"last_files={r.player.last_files} 应为=[{os.path.basename(B)}]（崩溃重启拿它恢复）"


def c_recent(w):
    r = w.mk(pos=0)
    r.go([A], append=True, remember=True)
    r2 = w.mk(pos=0)
    if B in r2.cfg["recent"]:                        # 前提：r2 那一份里 B 本来不该已经在
        return False, f"前提立不住：r2 的新数据目录里 recent 已含 B（{r2.dir}）——" \
                     f"那「记没记」就不是这一次调用说了算的"
    r2.go([B], append=True, remember=False)
    ok = bool(r.cfg["recent"]) and r.cfg["recent"][0] == A and B not in r2.cfg["recent"]
    return ok, f"remember=True→recent[0]={os.path.basename(str((r.cfg['recent'] or ['（空）'])[0]))} " \
               f"remember=False→recent 含 B={B in r2.cfg['recent']}"


def c_three_append(w):
    r = w.mk(pos=0)
    r.go([A, B, C], append=True)
    modes = [mode_of(x) for x in lf(r)]
    return modes == ["append", "append", "append"], f"模式列={modes} 发出={lf(r)}"


def c_pos_survives_start_file(w):
    """换文件那一下，不许让引擎忘记"当前是列表里第几条"（真机 leg1 的根因，2026-10-17）。

    E1–E17 全部是**直接给缓存塞一个 pos**（`Runner(pos=…)`），所以这一路从来没经过
    `_on_start_file`——而真机上"正播着点添加"恰恰一定刚经过它：内核推过 start-file，
    当年的 `_FILE_SCOPED` 里带着 `playlist-pos` 就把刚塞进来的那格抹掉了，之后
    "播放中往队尾 append"这一档 pos 不变、内核不再推第二发 ⇒ 缓存空着 ⇒
    `prop("playlist-pos", -1)` 读成 −1 ⇒ 第一条发成 `replace`＝整表替换。
    四条真机红里前三条都是这一条的下游（留档 tests/out/probe-playlist-pos-wipe.txt
    ④⑤⑥：抹完 prop()=None／默认 −1、首条 loadfile=replace）。
    这一条把"缓存 ⇒ 追加决策"这条依赖钉在**假传输＋真 open_files** 上，
    不需要屏幕、不需要真内核，锁屏也能跑；真窗口那一腿在 tests/test_ui.py 6.6–6.9。
    """
    r = w.mk(pos=1)
    r.player._on_start_file({"event": "start-file"})
    still = r.player.prop("playlist-pos", -1)
    r.go([C], append=True)
    got = mode_of(lf(r)[0]) if lf(r) else "（没发）"
    ok = still == 1 and got == "append"
    return ok, (f"start-file 之后 prop('playlist-pos', -1)={still!r}（要 1；读成 −1 就是"
                f"「以为没在播」）首条 loadfile={got!r}（要 append，发成 replace 就是整表替换）")


def c_globals_survive_start_file(w):
    """E18 同族的另外三格：`loop-file`／`ab-loop-a`／`ab-loop-b` 也不许被 start-file 抹掉。

    出处不是推理，是这台机器上真内核的读数（`tests/out/probe-globals-across-file.txt`）：
    换文件那一下③ `get_property` 回来仍是 `inf`／1.0／2.0（真值从上一支带过来），
    ④ 内核只推了 `playlist-pos` 那一格，这三格**一条 property-change 都没推**。
    两件事合起来才决定"抹掉"是不是缺陷：值不变＋不推 ⇒ 抹了就永远空着。
    后果按消费方分：`ab_range()` 读空缓存回 None ⇒ 换一支片子后调节页写「A-B：未设置」
    而内核其实还在按上一支那两个点循环（`_paint_ab` 在建页与设点两处都调）；
    `_loop_from_kernel()` 读空缓存回 "no" ⇒ 口径写着"以内核回报为准"，量的却是"我们记不记得"。

    这三格当时是**手挑的**（因为事故就长在这一族上）；同一趟只量四格就说"其余无害"是猜，
    所以后来把整张 `_FILE_SCOPED` 逐格量了一遍（E20 那段讲的就是那一趟的第四格）。
    """
    r = w.mk(pos=1)
    for k, v in (("loop-file", "inf"), ("ab-loop-a", 30.0), ("ab-loop-b", 42.0)):
        r.player._apply(k, v)                     # 走产品自己的入缓存口，不手搓 _props
    r.player._on_start_file({"event": "start-file"})
    got = {k: r.player.prop(k, "（缓存空着）") for k in ("loop-file", "ab-loop-a", "ab-loop-b")}
    rng = r.player.ab_range()
    ok = got == {"loop-file": "inf", "ab-loop-a": 30.0, "ab-loop-b": 42.0} and rng == (30.0, 42.0)
    return ok, (f"start-file 之后读到={got} ab_range()={rng!r}"
                "（要 inf/30.0/42.0 与 (30.0, 42.0)；空着就是「界面忘了内核此刻是什么状态」）")


def c_vo_survives_start_file(w):
    """E18/E19 那一族的**第四格**：`current-vo`（输出驱动）也不许被 start-file 抹掉。

    这一格不是手挑的，是整张 `_FILE_SCOPED` 逐格量出来的（`tests/probe_file_scoped_table.py`，
    留档 `tests/out/probe-file-scoped-table-v4-092438.txt`）：表内 21 格 → 19 格有实测依据
    留在表里、1 格「内核查无此格」（`container-format`，property not found）、
    1 格判"该摘"＝`current-vo`。它的读数与 ab-loop 那三格同一个形状：
    **两条腿都 `gpu-next`→`gpu-next`（值没变）且换文件那一下内核不推第二发**。

    消费方（留档里那一列是脚本现扫 `app/` 得到的，不是抄的）：统计页「输出驱动」那一行
    （`app/ui/stats_panel.py`）与媒体信息那句"输出 gpu-next"（`app/ui/main_window.py`
    的 `_media_info`）。抹掉的后果＝换一支片子之后那一行变空、那半句消失——
    用户看到的还是"界面忘了内核此刻是什么状态"。
    """
    r = w.mk(pos=1)
    r.player._apply("current-vo", "gpu-next")          # 走产品自己的入缓存口
    r.player._on_start_file({"event": "start-file"})
    got = r.player.prop("current-vo", "（缓存空着）")
    ok = got == "gpu-next"
    return ok, (f"start-file 之后 current-vo={got!r}（要 'gpu-next'；空着就是统计页那行变空）")


def c_start_shape(w):
    """E21 内核到底收不收这一条命令（#97，2026-10-19 双击《美国队长3》的那一笔真凶）。

    E11/E14 那次是**绿的**，用户那边却是"提示说加载了、画面黑的"——因为那两条只问
    "命令串里有没有 start= 字样"，而当年那个字样是包在列表里的，落在内核的 index 位上，
    整条命令被退回。这一条把问法换成"按这台内核的位置参数表逐位核对"，
    谓词 `start_shape_ok` 的判决全部出自 `tests/probe_loadfile_index.py` 的留档。
    三条真形状（旧凶形状／缺 −1／字典形态）当场喂给谓词要求判假，少了这一步
    "谓词恒真"也能把这条跑绿（同 U58 那条纪律：观察不证明自己**有**牙）。
    """
    r = w.mk(pos=None, remembered={A: (30.0, 120.0)})
    if r.cfg.recall_position(A) != 30.0:
        return False, f"前提立不住：进度读回来是 {r.cfg.recall_position(A)}（要 30.0），{r.dir}"
    r.go([A], append=False)
    head = lf(r)[0] if lf(r) else []
    ok, why = start_shape_ok(head)
    bad = [["loadfile", A, "replace", ["start=30.000"]],   # 2026-10-19 产品里的写法（被拒）
           ["loadfile", A, "replace", "start=30.000"],     # 少了 index 那一档
           ["loadfile", A, "replace", -1, {"start": 30.0}],  # 字典形态（这一构建同样拒）
           ["loadfile", A, "replace", 0, "start=30.000"]]  # index 给了 0＝抢当前条目
    rejected = [b for b in bad if start_shape_ok(b)[0]]
    ok_plain = start_shape_ok(["loadfile", A, "append"])[0]
    return (ok and not rejected and ok_plain), (
        f"发出的第一条={head} → {why}；坏形状漏网={rejected or '（都判假，对）'}；"
        f"不带选项那条放行={ok_plain}")


def c_receipt_replace(w):
    """E22 界面那句"已载入"改由内核回执撑着：`open_files` 必须把"这一趟等什么"告诉界面。"""
    r = w.mk(pos=0)
    r.go([A, B], append=False)
    want = {"count": 2, "plays": True, "head": os.path.abspath(A)}
    return r.res == want, f"返回值={r.res} 应为={want}（plays=True ⇒ 界面去等 file-loaded）"


def c_receipt_append(w):
    """E23 播放中往队尾追加：这条路上**永远不会有** file-loaded，返回值必须说 plays=False。

    错了会怎样：界面去等一支不会来的回执，20 秒后把"追加成功"报成"内核没收下"——
    把好结果说成坏，和把坏结果说成好是同一种病。
    """
    r = w.mk(pos=1)
    r.go([B, C], append=True)
    ok = r.res.get("plays") is False and r.res.get("count") == 2
    return ok, f"返回值={r.res}（要 plays=False／count=2；发出={lf(r)}）"


def c_receipt_empty(w):
    """E24 全是盘上没有的路径：count 必须是 0，界面据此当场说话，不许静默。"""
    r = w.mk(pos=0)
    r.go([GONE], append=False)
    ok = r.res.get("count") == 0 and not lf(r)
    return ok, f"返回值={r.res} 发出={lf(r)}（应为 count=0 且一条都不发）"


def c_wiring_signal(w):
    tgt = connect_target(w.init_src())
    return tgt == "self.add_to_playlist", f"sig_add_paths.connect({tgt or '（没找到）'}) 应为 self.add_to_playlist"


def c_wiring_slot(w):
    val = kw_in_func("add_to_playlist", "replace", src=w.slot_src())
    slot = connect_target(w.init_src())
    return val is False, f"槽 {slot or '（没找到）'} 里 add_paths(…, replace={val!r}) 应为 replace=False"


def c_drop_still_replaces(w):
    """窗口的拖放（不是列表那一页）保持"换成这批"——两页语义分家，这条钉住没被动过的那一半。"""
    src = inspect.getsource(MainWindow.dropEvent)
    called = [ast.unparse(n.func) for n in ast.walk(_tree(src)) if isinstance(n, ast.Call)]
    direct = [c for c in called if c.endswith("add_paths")]
    no_replace_kw = "replace=False" not in src
    return bool(direct) and no_replace_kw, f"dropEvent 调了 {direct}（默认 replace=True 那一路）"


CHECKS = [
    ("E1 空列表连加两支：第一条 replace、第二条 append", c_empty_two),
    ("E2 播放中连加两支：两条都 append，且不许多发抢播命令", c_live_no_steal),
    ("E3 播放中加一条：按实测语义推演，当前条目还是原来那条", c_live_pos_stays),
    ("E4 空列表只加一条：也要真的播起来（推演 pos≥0）", c_empty_one),
    ("E5 替换语义（双击／窗口拖放）：第一条 replace、其余 append", c_replace),
    ("E6 play_index=0 必须真的下发（旧写法 `if play_index:` 在这静默吞掉）", c_index_zero),
    ("E7 没给 play_index 就不许多发 playlist-pos", c_index_none),
    ("E8 play_index=2 正常下发（对照：0 那条不是靠少发蒙过去的）", c_index_two),
    ("E9 盘上没有的路径一条都不递给内核", c_missing),
    ("E10 相对路径收口成绝对再递给内核", c_relative),
    ("E11 有进度且开着续播：第一条带 start=30.000", c_resume_hit),
    ("E12 关了续播：有进度也不带 start", c_resume_off),
    ("E13 没有进度：不许塞 start=0.000", c_no_history),
    ("E14 空列表点添加看到一半的文件：新那条路也享受续播（start=45.000）", c_append_first_resumes),
    ("E15 last_files 记的是过滤＋绝对化之后的那份", c_last_files),
    ("E16 recent 只在 remember=True 时记（静默腿不许污染用户的最近文件）", c_recent),
    ("E17 播放中一次加三支：三条全 append，队尾不插队", c_three_append),
    ("E18 换文件不许把「当前是第几条」从缓存里抹掉（真机 leg1 的根因）", c_pos_survives_start_file),
    ("E19 换文件不许把 loop-file／ab-loop-a／ab-loop-b 抹掉（内核不推第二发那三格）",
     c_globals_survive_start_file),
    ("E20 换文件不许把 current-vo 抹掉（整表普查量出来的第四格）",
     c_vo_survives_start_file),
    ("E21 带续播位置的那条 loadfile 必须是真内核收得下的形状（E11 绿着被拒的那笔账）",
     c_start_shape),
    ("E22 open_files 告诉界面该等哪一种回执：replace＝plays=True、条数与首条绝对路径都对",
     c_receipt_replace),
    ("E23 播放中追加＝plays=False（这条路上永远没有 file-loaded，界面只能去等列表变长）",
     c_receipt_append),
    ("E24 全被过滤掉时 count=0：界面当场说话，不许静默", c_receipt_empty),
    ("W1 侧栏添加信号的落点＝add_to_playlist（不是整表替换那个槽）", c_wiring_signal),
    ("W2 add_to_playlist 里传的确实是 replace=False", c_wiring_slot),
    ("W3 窗口拖放仍然是「换成这批」（另一半语义不许被顺手改掉）", c_drop_still_replaces),
]


# ============================================================ 变异（证伪）
class World:
    """判据看到的"世界"：默认＝产品现状；变异跑＝替换掉某一份源码／某个函数。"""

    def __init__(self, func=None, init_src=None, slot_src=None, start_file=None):
        self._func = func
        self._init_src = init_src
        self._slot_src = slot_src
        self._start_file = start_file

    def mk(self, **kw):
        kw = dict(kw)
        if self._start_file is not None:
            kw["start_file"] = self._start_file
        return Runner(func=self._func, **kw)

    def init_src(self):
        return self._init_src if self._init_src is not None else inspect.getsource(MainWindow.__init__)

    def slot_src(self):
        return self._slot_src if self._slot_src is not None else World.BASE_SLOT

    BASE_INIT = None
    BASE_SLOT = None


def mutate(kind: str, old: str, new: str):
    """把一处产品源码改坏。回 (给 World 的覆盖项, 变异后源码, 原文源码)。

    三条硬规矩（都是本项目实发过的坑）：
    • `count(old)==1` 先钉死"只有这一处可改"，否则改到别处、红得没有指向；
    • `str.replace(a, b, 0)` 是**一处都不换**——必须显式 count=1；
    • 变异后文本必须≠原文（NOP 闸门）：注成空操作却把它记成"漏网"，
      是拿判据的额度去赔量具的错。
    """
    if kind == "open_files":
        base = inspect.getsource(MpvPlayer.open_files)
        ns = dict(vars(P))
    elif kind == "start_file":
        base = inspect.getsource(MpvPlayer._on_start_file)
        ns = dict(vars(P))
    elif kind == "init":
        base = World.BASE_INIT
        ns = None
    elif kind == "slot":
        base = World.BASE_SLOT
        ns = None
    else:
        raise ValueError(kind)
    if base.count(old) != 1:
        raise AssertionError(f"锚点出现 {base.count(old)} 次（要的是恰好 1 次）：{old!r}")
    mut = base.replace(old, new, 1)
    if mut == base:
        raise AssertionError("变异后源码与原文相同（空操作）")
    if kind == "open_files":
        exec(compile("from __future__ import annotations\n" + textwrap.dedent(mut),
                     "<mutated open_files>", "exec"), ns)   # noqa: S102 - 变异的是自家源码
        return {"func": ns["open_files"]}, mut, base
    if kind == "start_file":
        exec(compile("from __future__ import annotations\n" + textwrap.dedent(mut),
                     "<mutated _on_start_file>", "exec"), ns)   # noqa: S102
        return {"start_file": ns["_on_start_file"]}, mut, base
    if kind == "init":
        return {"init_src": mut}, mut, base
    return {"slot_src": mut}, mut, base


MUTATIONS = [
    ("追加时永远把第一条发成 replace（＝回到「抢当前条目」）",
     "open_files",
     "append_first = bool(append) and isinstance(live, int) and live >= 0",
     "append_first = False",
     "E2 播放中连加两支：两条都 append，且不许多发抢播命令"),
    ("追加时永远当成长尾追加（＝空列表点添加不播，正是用户报的那个形状）",
     "open_files",
     "append_first = bool(append) and isinstance(live, int) and live >= 0",
     "append_first = True",
     "E4 空列表只加一条：也要真的播起来（推演 pos≥0）"),
    ("play_index 用真值判断（0 被当成「没传」）",
     "open_files",
     "if play_index is not None and play_index >= 0:",
     "if play_index:",
     "E6 play_index=0 必须真的下发（旧写法 `if play_index:` 在这静默吞掉）"),
    ("摘掉存在性过滤（坏路径直接递给内核）",
     "open_files",
     "paths = [mpv_path(p) for p in paths if os.path.exists(p)]",
     "paths = [mpv_path(p) for p in paths]",
     "E9 盘上没有的路径一条都不递给内核"),
    ("摘掉绝对化（相对路径在我们这儿过 exists()，在内核那儿不存在）",
     "open_files",
     "paths = [mpv_path(p) for p in paths if os.path.exists(p)]",
     "paths = [p for p in paths if os.path.exists(p)]",
     "E10 相对路径收口成绝对再递给内核"),
    ("没有进度时也下发 start（`pos > 1` 那道门槛拿掉）",
     "open_files",
     "if pos > 1:",
     "if True:",
     "E13 没有进度：不许塞 start=0.000"),
    ("续播开关不看配置（关了续播还接着播上次位置）",
     "open_files",
     'pos = (self.cfg.recall_position(p) if self.cfg["resume"] else 0.0) or 0.0',
     "pos = self.cfg.recall_position(p) or 0.0",
     "E12 关了续播：有进度也不带 start"),
    ("信号接回整表替换那个槽（＝回到「点添加、列表没了」）",
     "init",
     "self.panel.sig_add_paths.connect(self.add_to_playlist)",
     "self.panel.sig_add_paths.connect(self.add_paths)",
     "W1 侧栏添加信号的落点＝add_to_playlist（不是整表替换那个槽）"),
    ("槽里的 replace=False 改成 True（接线对了、参数又写错）",
     "slot",
     "self.add_paths(paths, replace=False)",
     "self.add_paths(paths, replace=True)",
     "W2 add_to_playlist 里传的确实是 replace=False"),
    ("把当年那条真机 bug 注回去：start-file 顺手抹掉 playlist-pos",
     "start_file",
     "for k in self._FILE_SCOPED:",
     'for k in list(self._FILE_SCOPED) + ["playlist-pos"]:',
     "E18 换文件不许把「当前是第几条」从缓存里抹掉（真机 leg1 的根因）"),
    ("同族注回去：start-file 顺手抹掉那三格「内核不推第二发」的全局项",
     "start_file",
     "for k in self._FILE_SCOPED:",
     'for k in list(self._FILE_SCOPED) + ["loop-file", "ab-loop-a", "ab-loop-b"]:',
     "E19 换文件不许把 loop-file／ab-loop-a／ab-loop-b 抹掉（内核不推第二发那三格）"),
    ("整表普查那一格注回去：start-file 顺手抹掉 current-vo（值不变又不推，统计页那行会空）",
     "start_file",
     "for k in self._FILE_SCOPED:",
     'for k in list(self._FILE_SCOPED) + ["current-vo"]:',
     "E20 换文件不许把 current-vo 抹掉（整表普查量出来的第四格）"),
    ("续播形状注回 2026-10-19 那天的凶形状：options 包成列表（内核把它当 index 拒收）",
     "open_files",
     'cmd += [-1, f"start={pos:.3f}"]',
     'cmd += [[f"start={pos:.3f}"]]',
     "E21 带续播位置的那条 loadfile 必须是真内核收得下的形状（E11 绿着被拒的那笔账）"),
    ("续播形状少了 index 那一档（选项直接落在 index 位上，同样是这条报错）",
     "open_files",
     'cmd += [-1, f"start={pos:.3f}"]',
     'cmd += [f"start={pos:.3f}"]',
     "E21 带续播位置的那条 loadfile 必须是真内核收得下的形状（E11 绿着被拒的那笔账）"),
    ("续播形状改成字典形态（这一构建只认裸字符串，量具里 N2/Q4 都判拒）",
     "open_files",
     'cmd += [-1, f"start={pos:.3f}"]',
     'cmd += [-1, {"start": pos}]',
     "E21 带续播位置的那条 loadfile 必须是真内核收得下的形状（E11 绿着被拒的那笔账）"),
    ("index 给成 0（不是「不动当前条目」，而是明确要播这一条——追加那一档的语义就变了）",
     "open_files",
     'cmd += [-1, f"start={pos:.3f}"]',
     'cmd += [0, f"start={pos:.3f}"]',
     "E21 带续播位置的那条 loadfile 必须是真内核收得下的形状（E11 绿着被拒的那笔账）"),
    ("回执话术里 replace 那一路谎报 plays（追加也去等 file-loaded，好结果会被报成坏）",
     "open_files",
     '"plays": not append_first',
     '"plays": True',
     "E23 播放中追加＝plays=False（这条路上永远没有 file-loaded，界面只能去等列表变长）"),
    ("回执里不给首条绝对路径（界面到点就没法分「命令被拒」还是「收了但解不开」）",
     "open_files",
     '"head": paths[0]}',
     '"head": ""}',
     "E22 open_files 告诉界面该等哪一种回执：replace＝plays=True、条数与首条绝对路径都对"),
    ("全被过滤掉那条分支谎报 count=1（界面会去等一条根本发不出去的回执）",
     "open_files",
     'return {"count": 0, "plays": False, "head": ""}',
     'return {"count": 1, "plays": False, "head": ""}',
     "E24 全被过滤掉时 count=0：界面当场说话，不许静默"),
]


def run_falsify() -> int:
    for name, kind, old, new, target in MUTATIONS:
        try:
            over, mut_src, base_src = mutate(kind, old, new)
        except BaseException as exc:               # noqa: BLE001 - 锚点没钉住是量具的错，点名
            say("FAIL", f"证伪锚点：{name}", f"{type(exc).__name__}: {exc}")
            continue
        base_w, mut_w = World(), World(**over)
        flipped: list[str] = []
        missed: list[str] = []
        diffs = 0
        for chk_name, fn in CHECKS:
            try:
                ok_b, d_b = fn(base_w)
                ok_m, d_m = fn(mut_w)
            except BaseException as exc:           # noqa: BLE001 - 变异把判据跑崩也算被抓
                ok_b, ok_m = True, False
                d_m = f"变异后判据跑崩：{type(exc).__name__}: {exc}"
            if d_b != d_m:
                diffs += 1
            if ok_b and not ok_m:
                flipped.append(chk_name)
            if chk_name == target and not (ok_b and not ok_m):
                missed.append(f"{chk_name}（现状={ok_b} 变异后={ok_m}：{d_m}）")
        hit = not missed and bool(flipped) and mut_src != base_src
        say("PASS" if hit else "FAIL",
            f"证伪：{name} → 必须把「{target}」顶红",
            f"源码确有改动={mut_src != base_src} 读数变化的判据={diffs} 条 实际顶红={flipped or '（没有）'}"
            + (f" 没顶到位={missed}" if missed else ""))
    return 1 if count("FAIL") else 0


# ============================================================ 主流程
def run() -> int:
    global OUT_PATH
    OUT_PATH = os.path.join(ROOT, "tests", "out", "append-semantics-"
                            + datetime.datetime.now().strftime("%m%d-%H%M%S") + ".log")
    say("NOTE", "日志落这里", OUT_PATH)
    if ENGINE_ERR:
        say("ENV", "app.config / app.engine.player 起不来", ENGINE_ERR)
        return finish(2)
    if UI_ERR:
        say("ENV", "app.ui.main_window 起不来（W 组判据与两条接线变异都跑不动）", UI_ERR)
        return finish(2)
    for p in (A, B, C):
        if not os.path.isfile(p):
            say("ENV", "样片在不在（判据要用盘上真存在的文件）", p)
            return finish(2)
    if os.path.exists(GONE):
        say("ENV", "哨兵文件本该在盘上没有", GONE)
        return finish(2)
    World.BASE_INIT = inspect.getsource(MainWindow.__init__)
    World.BASE_SLOT = inspect.getsource(MainWindow.add_to_playlist)
    say("NOTE", "量具形状",
        f"引擎侧＝真 MpvPlayer＋假 `_fire` 记录器（不发 IPC、不起内核，判断一行不动）；"
        f"接线侧＝AST 读 MainWindow 自己的源码；内核行为按 tests/out/probe-append-idle.txt "
        f"的实测语义推演（不是真内核）；GELU_DATA={TMP}")

    if FALSIFY:
        return finish(run_falsify())
    for name, fn in CHECKS:
        try:
            ok, detail = fn(World())
        except BaseException as exc:               # noqa: BLE001 - 判据自己崩了要点名，不许静默
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        say("PASS" if ok else "FAIL", name, detail)
    return finish(1 if count("FAIL") else 0)


def finish(rc: int) -> int:
    print("DONE", flush=True)
    _tail("DONE")
    say("FINAL", f"播放列表追加语义：{'全绿' if rc == 0 else ('有真失败' if rc == 1 else '环境不给')}"
        f" —— PASS {count('PASS')}／FAIL {count('FAIL')}／ENV {count('ENV')}／NOTE {count('NOTE')}",
        f"判据表 {len(CHECKS)} 条／变异表 {len(MUTATIONS)} 种 rc={rc}")
    return rc


def _tail(line: str) -> None:
    if not OUT_PATH:
        return
    try:
        with io.open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        print("TRACEBACK（这一趟不作数）:", flush=True)
        traceback.print_exc()
        say("FINAL", "量具自己崩了 —— 追加语义不作数", "rc=2")
        print("DONE", flush=True)
        _tail("DONE")
        sys.exit(2)
