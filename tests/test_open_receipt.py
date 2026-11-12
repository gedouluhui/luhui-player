"""界面那句「已载入 N 个文件」到底凭什么说——回执层的验收与证伪（#97 的第二半）。

起因（用户原话，2026-10-19）："只是双击视频之后会打开播放器，然后弹出来一个提示说加载了
一个视频，但是实际上并没有加载任何东西没有任何播放，只是弹出一个提示说加载了而已。"

这句话就是案发现场本身。旧写法里它是 `add_paths` 紧跟在 `open_files()` 后面**无条件**印出去
的，而 `open_files` 只做一件事：把命令塞进写队列。命令在内核那一头被整条拒掉（真凶见
`tests/test_append_semantics.py` 的 E21：`loadfile` 的 options 包成列表，落在内核的 index
位上，回 `argument index has incompatible type`）时，写队列一切正常、气泡照印成功。
更糟的是 IPC 那一层把"没有 request_id 的错误应答"直接丢了（`app/engine/ipc.py:_dispatch`），
所以**被拒这件事在客户端一侧根本不可见**——量具也看不见，除非改问法。

修法不是"把成功话术写得更谨慎"，而是把这句话的证据来源换掉：只认内核自己推回来的东西。
· 第一条发成 `replace`（会换当前条目）⇒ 等证据是内核的 `file-loaded` 事件；
· 往正在播的队尾追加（这条路上永远没有 file-loaded）⇒ 等证据是内核推的 playlist 真的变长；
· 到点没等到 ⇒ 按内核推的 `path` 与请求的 `head` 分诊，"命令被拒"和"收了但解不开"分开说。

判据分两半，缺一半都不算修好：
  R1 把 `open_receipt` 当纯函数喂**已知坏的读数**（含"该沉默"那一档）——真机观察本身不会证明
      自己有牙，判据整条写成 `check(..., True, ...)` 照样跑绿；
  R2–R8 用 AST／文本钉**接线**：`add_paths` 里不许再出现成功话、`_arm` 不许当场结算、
      三条叫醒路径（file-loaded／playlist 变长／超时）一条都不能断、claim 只在真的说话那一支
      才摘（2026-10-20 订正，见 R5：旧写法"进门先销账"正是 R20 真机「气泡=''」的根因）、
      读数必须取自内核而不是界面自己数。

跑法：
    python tests/test_open_receipt.py
    python tests/test_open_receipt.py --falsify   # 每种坏写法必须各自把对应判据顶红

退出码：0 全绿（--falsify 时＝变异表全部被抓）；1 至少一条真失败；2 环境不给。
"""
from __future__ import annotations

import ast
import datetime
import inspect
import io
import os
import re
import sys
import textwrap

if hasattr(sys.stdout, "reconfigure"):     # 本机控制台默认 GBK：文案里一个"−"（U+2212）就能
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # 把整趟劈死在 print 上，
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")   # 还被兜成"这一趟不作数"

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GELU_DATA", os.path.join(os.environ.get("TEMP", "C:" + chr(92) + "temp"),
                                                f"gelu-receipt-{os.getpid()}"))
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

RESULTS: list[list[str]] = []
OUT_PATH = ""
FALSIFY = "--falsify" in sys.argv

UI_ERR = ""
try:
    import app.ui.main_window as MW
    from app.ui.main_window import MainWindow
except BaseException as _exc:                       # noqa: BLE001 - 起不来要记 ENV，不许崩
    MW = None
    MainWindow = None
    UI_ERR = f"{type(_exc).__name__}: {_exc}"

HEAD = os.path.abspath(os.path.join(HERE, "samples", "gelu-sample-1.mp4"))
OTHER = os.path.abspath(os.path.join(HERE, "samples", "别的片子.mp4"))


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


# ============================================================ R1 的对照表（已知读数）
# 每一档都对应一件**实发过或必然会发生**的事，不是随手凑的参数组合：
#   · `loaded=True` 那两档＝内核亲口说装完了，此时才许说"已载入"；
#   · `plays=True / loaded=False / timed_out=False`＝**命令刚发出去、什么都还没回来**。
#     这一档必须沉默，它就是 2026-10-19 那句话的葬身之地：旧写法在这里印"已载入 1 个文件"；
#   · 超时两档＝用户等的到底是"没进列表"还是"进了列表没画面"，只有内核推的 `path` 分得开，
#     两档文案不同不是修辞，是把"命令被拒"和"解不开"这两条排查路给用户的不是同一条；
#   · 追加三档＝这条路上永远不会有 file-loaded，凭什么说"已追加"只能看列表长没长。
OPEN_ROWS: list[tuple[dict, tuple[bool, bool, str], str]] = [
    (dict(count=1, plays=True, head=HEAD, pl_before=0, pl_now=1, kernel_path=HEAD,
          loaded=True, timed_out=False), (True, False, "已载入 1 个文件"),
     "内核说 file-loaded 了"),
    (dict(count=3, plays=True, head=HEAD, pl_before=0, pl_now=3, kernel_path=HEAD,
          loaded=True, timed_out=False), (True, False, "已载入 3 个文件"),
     "多条也只由 file-loaded 作证"),
    (dict(count=1, plays=True, head=HEAD, pl_before=0, pl_now=0, kernel_path="",
          loaded=False, timed_out=False), (False, False, ""),
     "刚发出去、什么都没回来＝不许说话（旧写法在这句谎）"),
    (dict(count=2, plays=True, head=HEAD, pl_before=0, pl_now=0, kernel_path="",
          loaded=False, timed_out=False), (False, False, ""),
     "同上，条数换了不该改变判法（对照组）"),
    (dict(count=1, plays=True, head=HEAD, pl_before=0, pl_now=1, kernel_path=HEAD,
          loaded=False, timed_out=True), (True, True, "一直没出画面"),
     "超时＋列表里就是这一条＝收了但没画面"),
    (dict(count=1, plays=True, head=HEAD, pl_before=0, pl_now=0, kernel_path=OTHER,
          loaded=False, timed_out=True), (True, True, "没把这些文件收进播放列表"),
     "超时＋内核播的是别的＝命令根本没被收下"),
    (dict(count=2, plays=False, head=HEAD, pl_before=3, pl_now=5, kernel_path=OTHER,
          loaded=False, timed_out=False), (True, False, "已追加 2 个文件"),
     "内核推的列表从 3 长到 5＝追加成立"),
    (dict(count=2, plays=False, head=HEAD, pl_before=3, pl_now=4, kernel_path=OTHER,
          loaded=False, timed_out=True), (True, True, "只追加了 1 个"),
     "只长了一条＝剩下的命令被拒，不许报圆满"),
    (dict(count=2, plays=False, head=HEAD, pl_before=3, pl_now=3, kernel_path=OTHER,
          loaded=False, timed_out=True), (True, True, "一条都没收进去"),
     "一条没长＝别说成功"),
    (dict(count=2, plays=False, head=HEAD, pl_before=3, pl_now=3, kernel_path=OTHER,
          loaded=False, timed_out=False), (False, False, ""),
     "追加那一路也没到点＝沉默"),
    (dict(count=0, plays=True, head="", pl_before=0, pl_now=0, kernel_path="",
          loaded=False, timed_out=False), (False, False, ""),
     "count=0 不归这里说（由 _arm 当场把原因讲出来）"),
]

# 表的**非退化**闸门：判据最怕的不是红，是整张表被改成"怎么说都对"。
ROW_SHAPE = (
    ("该沉默的档", lambda e: e[0] is False, 2),
    ("该判失败的档", lambda e: e[1] is True, 2),
    ("该判成功的档", lambda e: e[0] is True and e[1] is False, 2),
)


def _tree(src: str) -> ast.Module:
    return ast.parse(textwrap.dedent(src))


def _calls(src: str) -> list[ast.Call]:
    return [n for n in ast.walk(_tree(src)) if isinstance(n, ast.Call)]


def _osd_texts(src: str) -> list[str]:
    """这个函数体里所有 `self._osd(...)` 的实参原文（判"有没有当场说话"用）。"""
    return [ast.unparse(c.args[0]) for c in _calls(src)
            if isinstance(c.func, ast.Attribute) and c.func.attr == "_osd" and c.args]


# ============================================================ 判据
def c_pure_function(w):
    """R1 `open_receipt` 逐档喂已知读数：三态（说／沉默／判坏）都必须按表落。"""
    fn = w.receipt()
    bad: list[str] = []
    for i, (kw, want, why) in enumerate(OPEN_ROWS, 1):
        try:
            got = fn(**kw)
        except BaseException as exc:               # noqa: BLE001 - 谓词崩＝这一档没判
            bad.append(f"第{i}档（{why}）跑崩：{type(exc).__name__}: {exc}")
            continue
        if (got[0], got[2]) != (want[0], want[1]):
            bad.append(f"第{i}档（{why}）该 (说={want[0]},坏={want[1]}) 实际 (说={got[0]},"
                       f"坏={got[2]})：{got[1]!r}")
        elif want[2] and want[2] not in (got[1] or ""):
            bad.append(f"第{i}档（{why}）说了，但话里没有「{want[2]}」：{got[1]!r}")
    deg = [f"{label}只有 {sum(1 for _, e, _ in OPEN_ROWS if pred(e))} 档（要 ≥{floor}）"
           for label, pred, floor in ROW_SHAPE
           if sum(1 for _, e, _ in OPEN_ROWS if pred(e)) < floor]
    detail = "；".join(bad) if bad else f"{len(OPEN_ROWS)} 档逐档核对：全按表落"
    return not bad and not deg, detail + (f" ｜表退化了：{'；'.join(deg)}" if deg else "")


def _self_calls(src: str, attr: str) -> list[ast.Call]:
    """函数体里所有 `self.<attr>(...)`（只认 self，别的接收者同名不算）。"""
    return [c for c in _calls(src)
            if isinstance(c.func, ast.Attribute) and c.func.attr == attr
            and isinstance(c.func.value, ast.Name) and c.func.value.id == "self"]


def c_add_paths_silent(w):
    """R2 成功话只许由回执说，而且 add_paths 必须真把这一趟送到回执那边。

    R22-f 之后链路是两截：内核已就绪 ⇒ add_paths 直接 `_open_files_now`（arm 在里面）；
    未就绪 ⇒ 排队 `_pending_open` ＋ `_begin_engine`，由 `_poll_engine_start` 排空到同一个
    `_open_files_now`。要钉的是**两截都通、成功话不在任何一截里**，不是那句话长在哪个函数里：
    后者会让搬家本身被误报成产品坏了（2026-10-26 实发过一次，红灯最后是判据的错）。
    """
    src = w.meth_src("add_paths")
    now = w.meth_src("_open_files_now")
    poll = w.meth_src("_poll_engine_start")
    hands = bool(_self_calls(src, "_open_files_now"))
    queues = any(isinstance(c.func, ast.Attribute) and c.func.attr == "append"
                 and isinstance(c.func.value, ast.Attribute)
                 and c.func.value.attr == "_pending_open"
                 for c in _calls(src))
    starts = bool(_self_calls(src, "_begin_engine"))
    drains = queues and "_pending_open" in poll and bool(_self_calls(poll, "_open_files_now"))
    now_arms = bool(_self_calls(now, "_arm_open_receipt"))
    routed = (hands or (queues and starts and drains)) and now_arms
    speaks = [t for t in _osd_texts(src) + _osd_texts(now)
              if "已载入" in t or "已追加" in t]
    return routed and not speaks, (
        f"直接交 _open_files_now={hands}／排队={queues}＋起内核={starts}"
        f"＋定时器排空到 _open_files_now={drains}；arm 在 _open_files_now={now_arms}；"
        f"两截里当场说成功话={speaks or '（没有，对）'}"
        "（旧写法就在 open_files 之后无条件印「已载入」）")


def c_arm_waits(w):
    """R3 `_arm_open_receipt` 只许"记下＋起表"，不许自己结算、也不许拿界面以为的条数。"""
    src = w.meth_src("_arm_open_receipt")
    settles = any(isinstance(c.func, ast.Attribute) and c.func.attr == "_settle_open_receipt"
                  for c in _calls(src))
    armed = "self._open_timer.start(OPEN_RECEIPT_MS)" in src
    before = '"before": self._pl_len' in src
    empty = "self._restore_empty(" in src
    return (armed and before and empty and not settles), (
        f"起表={armed} before 取内核推的那一份={before} 空列表当场讲原因={empty} "
        f"arm 里自己结算={settles}（要 真/真/真/假）")


def c_wakeup_paths(w):
    """R4 三条叫醒回执的路径一条都不能断：file-loaded、playlist 变长、超时。"""
    loaded = "self._settle_open_receipt(loaded=True)" in w.meth_src("_on_file_loaded")
    pl_src = w.meth_src("_on_playlist")
    append_leg = ("self._pl_len = len(pl or [])" in pl_src
                  and 'not self._open_claim["plays"]' in pl_src
                  and "self._settle_open_receipt()" in pl_src)
    timeout_leg = ("self._open_timer.timeout.connect" in w.meth_src("__init__")
                   and "timed_out=True" in w.meth_src("__init__"))
    return loaded and append_leg and timeout_leg, (
        f"file-loaded→结算={loaded} 列表变长→结算={append_leg} 超时→结算={timeout_leg}"
        "（三条全要真；断一条＝那句话要么永远不说、要么靠超时蒙）")


def c_claim_taken_first(w):
    """R5 没说成话不许销账：claim 只在**真的说话那一支**才摘（一次回执仍只许说一遍）。

    这条尺的方向在 2026-10-20 掉过头，掉头有实测依据：`_on_playlist` 是每一条 playlist
    推送都会走进来的，而内核并不只推"变长的那一条"（本机一次初始载入里 `playlist` 连推
    4 次、长度全是 2，见 `tests/out/probe-append-receipt-hidden2.log`）。旧写法第一行
    `self._open_claim = None` 把"证据还没到"那一支也一起销了账 ⇒ 真变长那条没人认 ⇒
    那句话永远不说，连 20 秒后的超时也没有（claim 没了）。R20 真机「气泡=''」就是它。
    三条同时成立才算这条尺还在管事，缺一条就红：
      ① 摘账的行号 **晚于** `open_receipt(...)` 的行号（早于＝旧 bug 还原）；
      ② `if not say:` 那一支只是 `return`，中间不许夹摘账或停表（它就是"证据没到"那条路）；
      ③ 摘账与停表还得在（说之后）——缺了就变成同一次回执被后来的推送一遍遍说。
    仍按 AST 行号比，不按 `src.find("open_receipt(")` 的字符位置：方法名
    `_settle_open_receipt(` 自己就含 "open_receipt("，按字符找会先撞上 `def` 那一行。
    """
    src = w.meth_src("_settle_open_receipt")
    tree = _tree(src)
    clear = next((n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  and isinstance(n.value, ast.Constant) and n.value.value is None
                  and any(isinstance(t, ast.Attribute) and t.attr == "_open_claim"
                          for t in n.targets)), None)
    speak = next((n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == "open_receipt"), None)
    stop = next((n.lineno for n in ast.walk(tree) if isinstance(n, ast.Expr)
                 and isinstance(n.value, ast.Call)
                 and isinstance(n.value.func, ast.Attribute)
                 and n.value.func.attr == "stop"
                 and isinstance(n.value.func.value, ast.Attribute)
                 and n.value.func.value.attr == "_open_timer"), None)
    # "证据没到"那一支只许是一句裸 `return`。按 AST 认节点，不按"从正则匹配处往后截一段文本"：
    # 截文本会把**下一条同缩进的语句**也算进去（第一版就这么把合法的 `return` 后面那行摘账
    # 读成了分支里的动作，红得毫无道理）。
    silent_leg = None
    for n in ast.walk(tree):
        t = n.test if isinstance(n, ast.If) else None
        if (isinstance(n, ast.If) and isinstance(t, ast.UnaryOp)
                and isinstance(t.op, ast.Not) and isinstance(t.operand, ast.Name)
                and t.operand.id == "say"):
            silent_leg = n
            break
    leg_clean = (silent_leg is not None and not silent_leg.orelse
                 and len(silent_leg.body) == 1
                 and isinstance(silent_leg.body[0], ast.Return)
                 and silent_leg.body[0].value is None)
    ok = (clear is not None and speak is not None and stop is not None
          and leg_clean and clear > speak and stop > speak)
    return ok, (f"摘账在第 {clear} 行、说话在第 {speak} 行、停表在第 {stop} 行"
                f"（要 摘>说 且 停表>说）；沉默分支＝一句裸 return：{leg_clean}"
                "（旧尺子钉的是 摘<说，那条把真机 bug 当成了规矩）")


def c_bad_restores_placeholder(w):
    """R6 判坏时不许只留一句气泡——占位页那句话就是说给"这一屏什么都没有"的用户听的。"""
    src = w.meth_src("_settle_open_receipt")
    m = re.search(r"if bad:\s*\n(\s+)self\._restore_empty\(text\)", src)
    return bool(m), f"bad 分支里恢复占位页={bool(m)}（没有的话：用户只看见一句 1 秒就散的气泡）"


def c_readings_from_kernel(w):
    """R7 说话用的三个读数必须全部来自内核，界面一个字都不许自己猜。

    为什么"没拿侧栏数出来的条数"也要钉：追加那一路的凭据是"内核自己说列表变长了"，
    一旦改成"侧栏里有几行"，内核拒了命令、侧栏却被我们填过了 ⇒ 判据会替谎话盖章。
    """
    src = w.meth_src("_settle_open_receipt")
    arm = w.meth_src("_arm_open_receipt")
    path_leg = 'self.player.prop("path"' in src
    len_leg = 'claim["before"], self._pl_len' in src
    no_panel_len = "self.panel" not in src and "self.panel" not in arm
    return path_leg and len_leg and no_panel_len, (
        f"内核的 path={path_leg} 内核推的列表长度={len_leg} 没拿侧栏数的条数={no_panel_len}"
        "（三条全要真；拿侧栏 row_count 当凭据＝内核拒了命令、界面还说自己赢了）")


def c_timeout_floor(w):
    """R8 等待窗口是判据的一部分：太短会把"4K REMUX 首帧慢"误报成"内核没收下"。"""
    src = w.module_src()
    m = re.search(r"^OPEN_RECEIPT_MS = (\d+)$", src, re.M)
    n = int(m.group(1)) if m else -1
    return n >= 15000, f"OPEN_RECEIPT_MS={n}（要 ≥15000，且必须是这个常量而不是散在各处的毫秒数）"


CHECKS = [
    ("R1 open_receipt 逐档喂已知读数：说／沉默／判坏三态全按表落（谓词恒真也跑不了绿）",
     c_pure_function),
    ("R2 成功话只许由回执说：add_paths →（直接／排队→定时器）→ _open_files_now → arm",
     c_add_paths_silent),
    ("R3 _arm_open_receipt 只记下＋起表，不许当场结算；before 取内核推的那一份", c_arm_waits),
    ("R4 三条叫醒路径（file-loaded／列表变长／超时）一条都不能断", c_wakeup_paths),
    ("R5 没说成话不许销账：claim 只在真的说话那一支才摘（一次回执仍只许说一遍）",
     c_claim_taken_first),
    ("R6 判坏必须恢复占位页，不许只留一句会散的气泡", c_bad_restores_placeholder),
    ("R7 回执用的读数全部来自内核（path／列表长度），界面不许自己猜", c_readings_from_kernel),
    ("R8 等待窗口有下限：太短会把慢首帧误报成命令被拒", c_timeout_floor),
]


# ============================================================ 世界与变异
class World:
    """判据看到的"世界"：默认＝产品现状；变异跑＝替换掉一份源码／一个函数。"""

    def __init__(self, receipt=None, meth=None, module_src=None):
        self._receipt = receipt
        self._meth = meth or {}
        self._module_src = module_src

    def receipt(self):
        return self._receipt if self._receipt is not None else MW.open_receipt

    def meth_src(self, name: str) -> str:
        if name in self._meth:
            return self._meth[name]
        return inspect.getsource(getattr(MainWindow, name))

    def module_src(self) -> str:
        return self._module_src if self._module_src is not None else World.BASE_MODULE

    BASE_MODULE = None


def mutate(kind: str, old: str, new: str):
    """把一处产品源码改坏。回 (给 World 的覆盖项, 变异后源码, 原文源码)。

    三条硬规矩同 test_append_semantics：锚点必须恰好 1 处、`replace` 要显式 count=1、
    变异后文本必须≠原文（NOP 闸门——注成空操作却记"漏网"是拿判据的额度赔量具的错）。
    """
    if kind == "receipt":
        base = inspect.getsource(MW.open_receipt)
        ns = dict(vars(MW))
    elif kind == "module":
        base = World.BASE_MODULE
        ns = None
    elif kind.startswith("meth:"):
        name = kind.split(":", 1)[1]
        base = inspect.getsource(getattr(MainWindow, name))
        ns = None
    else:
        raise ValueError(kind)
    if base.count(old) != 1:
        raise AssertionError(f"锚点出现 {base.count(old)} 次（要的是恰好 1 次）：{old!r}")
    mut = base.replace(old, new, 1)
    if mut == base:
        raise AssertionError("变异后源码与原文相同（空操作）")
    if kind == "receipt":
        exec(compile("from __future__ import annotations\n" + textwrap.dedent(mut),
                     "<mutated open_receipt>", "exec"), ns)   # noqa: S102 - 变异的是自家源码
        return {"receipt": ns["open_receipt"]}, mut, base
    if kind == "module":
        return {"module_src": mut}, mut, base
    _tree(mut)                                       # 注完还得能被解析，否则红得没有指向
    return {"meth": {kind.split(":", 1)[1]: mut}}, mut, base


MUTATIONS = [
    ("回到那句谎：file-loaded 没来也当场说「已载入」（＝用户报的那个形状）",
     "receipt",
     '        if loaded:\n            return True, f"已载入 {count} 个文件", False\n        if not timed_out:',
     '        if loaded:\n            return True, f"已载入 {count} 个文件", False\n        if True:',
     "R1 open_receipt 逐档喂已知读数：说／沉默／判坏三态全按表落（谓词恒真也跑不了绿）"),
    ("追加那一路不看列表长没长就报成功",
     "receipt",
     "    if pl_now >= pl_before + count:",
     "    if True:",
     "R1 open_receipt 逐档喂已知读数：说／沉默／判坏三态全按表落（谓词恒真也跑不了绿）"),
    ("超时把「没收下」和「收了没画面」糊成一句（分诊谓词恒真）",
     "receipt",
     '    same = bool(head) and (head == (kernel_path or "") or\n'
     '                           os.path.basename(head) == os.path.basename(kernel_path or ""))',
     "    same = True",
     "R1 open_receipt 逐档喂已知读数：说／沉默／判坏三态全按表落（谓词恒真也跑不了绿）"),
    ("count=0 也说成成功（空列表上按「添加」时给用户一句假圆满）",
     "receipt",
     '    if count <= 0:\n        return False, "", False',
     '    if count <= 0:\n        return True, "已载入 0 个文件", False',
     "R1 open_receipt 逐档喂已知读数：说／沉默／判坏三态全按表落（谓词恒真也跑不了绿）"),
    ("在 open_files 之后把那句成功话加回来（＝改法被后来人一键还原）",
     "meth:_open_files_now",
     '        self._arm_open_receipt(int(res.get("count", 0)), bool(res.get("plays")),',
     '        self._osd(f"已载入 {int(res.get(\'count\', 0))} 个文件")\n'
     '        self._arm_open_receipt(int(res.get("count", 0)), bool(res.get("plays")),',
     "R2 成功话只许由回执说：add_paths →（直接／排队→定时器）→ _open_files_now → arm"),
    ("arm 里当场结算（沉默改成了「立刻当成功」，换汤不换药）",
     "meth:_arm_open_receipt",
     "self._open_timer.start(OPEN_RECEIPT_MS)",
     "self._settle_open_receipt(loaded=True)",
     "R3 _arm_open_receipt 只记下＋起表，不许当场结算；before 取内核推的那一份"),
    ("before 拿界面以为的那一份（＝内核推没推都算变长了）",
     "meth:_arm_open_receipt",
     '"head": head, "before": self._pl_len}',
     '"head": head, "before": 0}',
     "R3 _arm_open_receipt 只记下＋起表，不许当场结算；before 取内核推的那一份"),
    ("file-loaded 不叫醒回执（回执到了没人接，好结果 20 秒后被报成坏）",
     "meth:_on_file_loaded",
     "self._settle_open_receipt(loaded=True)",
     "pass",
     "R4 三条叫醒路径（file-loaded／列表变长／超时）一条都不能断"),
    ("playlist 推送不接（追加那条路永远只能靠超时蒙）",
     "meth:_on_playlist",
     '        if self._open_claim and not self._open_claim["plays"]:\n'
     "            self._settle_open_receipt()",
     "        if False:\n            self._settle_open_receipt()",
     "R4 三条叫醒路径（file-loaded／列表变长／超时）一条都不能断"),
    ("超时不接（那句话永远不说＝用户以为界面卡住了）",
     "meth:__init__",
     "self._open_timer.timeout.connect(lambda: self._settle_open_receipt(timed_out=True))",
     "self._open_timer.timeout.connect(lambda: None)",
     "R4 三条叫醒路径（file-loaded／列表变长／超时）一条都不能断"),
    ("把 R20 那个真机 bug 原样注回去：进门先销账，再说有没有证据"
     "（一条「没变长」的 playlist 推送就能把回执吃掉又什么都不说）",
     "meth:_settle_open_receipt",
     "        claim = self._open_claim\n        if claim is None:",
     "        claim = self._open_claim\n        self._open_claim = None\n        if claim is None:",
     "R5 没说成话不许销账：claim 只在真的说话那一支才摘（一次回执仍只许说一遍）"),
    ("说了话也不销账（同一次回执被后来的推送一遍遍说，气泡反复弹）",
     "meth:_settle_open_receipt",
     "        self._open_claim = None          # 摘在说之后",
     "        pass                             # 变异：说完了也不摘账",
     "R5 没说成话不许销账：claim 只在真的说话那一支才摘（一次回执仍只许说一遍）"),
    ("证据没到那一支顺手把等待的表停了（销账之外的另一种「再也没有结局」）",
     "meth:_settle_open_receipt",
     "        if not say:\n            return",
     "        if not say:\n            self._open_timer.stop()\n            return",
     "R5 没说成话不许销账：claim 只在真的说话那一支才摘（一次回执仍只许说一遍）"),
    ("判坏了不恢复占位页（那句解释挂在看不见的地方）",
     "meth:_settle_open_receipt",
     "            self._restore_empty(text)",
     "            pass",
     "R6 判坏必须恢复占位页，不许只留一句会散的气泡"),
    ("内核的 path 换成界面自己猜的（分诊永远说「收了没画面」）",
     "meth:_settle_open_receipt",
     '                                      str(self.player.prop("path", "") or ""),',
     '                                      claim["head"],',
     "R7 回执用的读数全部来自内核（path／列表长度），界面不许自己猜"),
    ("列表长度改拿侧栏数出来的那一份（内核拒了命令、侧栏却已被我们填过）",
     "meth:_settle_open_receipt",
     '                                      claim["before"], self._pl_len,',
     '                                      claim["before"], self.panel.row_count(),',
     "R7 回执用的读数全部来自内核（path／列表长度），界面不许自己猜"),
    ("等待窗口砍到 2 秒（慢首帧被误报成命令被拒）",
     "module",
     "OPEN_RECEIPT_MS = 20000",
     "OPEN_RECEIPT_MS = 2000",
     "R8 等待窗口有下限：太短会把慢首帧误报成命令被拒"),
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
        for chk_name, fn in CHECKS:
            try:
                ok_b, _ = fn(base_w)
                ok_m, d_m = fn(mut_w)
            except BaseException as exc:           # noqa: BLE001 - 变异把判据跑崩也算被抓
                ok_b, ok_m, d_m = True, False, f"变异后判据跑崩：{type(exc).__name__}: {exc}"
            if ok_b and not ok_m:
                flipped.append(chk_name)
            if chk_name == target and not (ok_b and not ok_m):
                missed.append(f"{chk_name}（现状={ok_b} 变异后={ok_m}：{d_m}）")
        hit = not missed and bool(flipped) and mut_src != base_src
        say("PASS" if hit else "FAIL",
            f"证伪：{name} → 必须把「{target}」顶红",
            f"源码确有改动={mut_src != base_src} 实际顶红={flipped or '（没有）'}"
            + (f" 没顶到位={missed}" if missed else ""))
    return 1 if count("FAIL") else 0


# ============================================================ 主流程
def run() -> int:
    global OUT_PATH
    OUT_PATH = os.path.join(ROOT, "tests", "out", "open-receipt-"
                            + datetime.datetime.now().strftime("%m%d-%H%M%S") + ".log")
    say("NOTE", "日志落这里", OUT_PATH)
    if UI_ERR:
        say("ENV", f"app.ui.main_window 起不来（判据表 {len(CHECKS)} 条全在它身上）", UI_ERR)
        return finish(2)
    World.BASE_MODULE = inspect.getsource(MW)
    say("NOTE", "量具形状",
        f"R1＝直接调产品的 `open_receipt`，喂本文件 `OPEN_ROWS` 里逐档写死的已知读数"
        f"（{len(OPEN_ROWS)} 档）；R2–R8＝AST／文本读 `MainWindow` 自己的源码"
        f"（不建窗口，真按一次按钮那一腿在 e2e）。"
        "真内核那一腿的读数出处是 tests/out/probe-loadfile-index-v3-1019-205812.txt")
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
    say("FINAL", f"打开回执：{'全绿' if rc == 0 else ('有真失败' if rc == 1 else '环境不给')}"
        f" —— PASS {count('PASS')}／FAIL {count('FAIL')}／ENV {count('ENV')}／NOTE {count('NOTE')}",
        f"判据表 {len(CHECKS)} 条／已知读数 {len(OPEN_ROWS)} 档／变异表 {len(MUTATIONS)} 种 rc={rc}")
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
        say("FINAL", "量具自己崩了 —— 回执判据不作数", "rc=2")
        print("DONE", flush=True)
        _tail("DONE")
        sys.exit(2)
