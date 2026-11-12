"""声音均衡器（十段＋前置）的验收与证伪：`app/engine/equalizer.py` 那张表的落地对照。

用法：
    python tests/test_equalizer.py               # 验收：四家判据全跑（含真机/真声卡半）
    python tests/test_equalizer.py --no-kernel   # 只跑三家离线判据（内核/声卡不给力时仍能记账）
    python tests/test_equalizer.py --falsify     # 证伪：内存里摘掉承重件，同一批判据必须逐条点名

退出码：0 全绿；1 有判据没过（或坏样本没抓到）；2 连测量对象都没有（缺内核／缺样片）；
3 **本轮有判废**（内核起不来、样片没音轨）——逐条点名，其余照常记账。
优先级写在 `final_exit_code`：同轮只要有一条真红，rc 仍是 1，判废不许掩盖失败。

判据分四家，各能证的东西不一样，别混成一锅：

1. **离线·表**（不碰内核，也不碰 Qt）：那张表自己的形状——夹紧、拼串、括号里不许有逗号、
   前置的 `dB` 后缀、空链＝空串、启动参数、预设的净抬升线、反解器的认不认、坏原话。
   钉的是"写法回归"：跑得快、每次都跑得起来，改表的人当场看见。
2. **离线·引擎**（真 `MpvPlayer`，但**不启动内核**）：引擎那三条收口——内核没在家时
   `set_audio_eq` 不许去问 `af`、纯读不许把"没人应答"发成"链坏了"、`build_args` 里的
   `--af=` 与表同步。这一家不需要 mpv 进程：`_ipc is None` 那条路本身就是被测对象。
3. **离线·面板**（假内核）：面板与引擎之间那根线接没接对——拖动途中不发同步请求、松手发且
   只发一次、表外项一律不发、画的是内核读回的数、状态行四档措辞对得上四种事实。
   假内核不是"预写正确答案的 fixture"：它按**上一次发出去的那条命令**回一片 `af`
   （形状照 ⑥ 那一腿的实测结构），所以拼串歪了它照原样回报，红由反解那头发现；
   而它与真引擎的同构由第 P0 条 AST 判据钉着（签名＋`if` 条件集合逐方法比），
   假内核哪天悄悄宽松一点，那条就红。
4. **真机**（本机 mpv + 这台机器的真实输出通路）：启动参数真的落地、播放中热切读得回来、
   归零是干净清空、坏值**绕过外壳**直接喂内核时日志里真有原话、音量不是 100 时 `af` 里
   会不会多出这张表读不懂的条目，以及"坏消息在没有新证据的纯读里留着、在下发后清掉"这一条。

全部取值口径来自 `tests/out/eq-probe-r15b.log`（腿号 ⓪①②③③b④a④b⑤⑥⑦ 写在
`app/engine/equalizer.py` 的模块 docstring 里），本机内核 `v0.41.0-1101-g5d85ba5fb`。
**没测的东西在本文件里一律写成"未测"而不是判据**：44.1 kHz 片上 16k 段的行为、
WASAPI 独占模式、以及"用户真的听出来的差别"（频谱带尺量的是样本，不是耳朵）。

为什么这一族必须有第 2、4 家：坏值的下场不是"这一档没生效"，是**整条 af 起不来、一声不出**
（画面那一族只是画面不动）。所以凡判"生效"都必须同时拿到**读回的形状**与**日志里没有失败原话**，
缺一边就不许说"已生效"。
"""
from __future__ import annotations

import ast
import inspect
import os
import random
import sys
import textwrap
import time
from typing import Any

if hasattr(sys.stdout, "reconfigure"):     # Windows 控制台默认 GBK，中文判据名会糊
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("GELU_DATA", os.path.abspath(os.path.join(HERE, ".selftest-data")))
sys.path.insert(0, ROOT)

from app import config as cfgmod                       # noqa: E402
from app.engine import equalizer                       # noqa: E402
from app.engine.player import MpvPlayer                # noqa: E402
from PyQt5.QtCore import Qt                            # noqa: E402

# 表里的名字一律走 `equalizer.<名字>` 而不是 `from ... import <名字>`：
# 证伪轮是把**模块属性**摘掉来跑的，本地绑定抄的那一份会停在摘之前的版本上，
# 于是"注入没被抓到"其实是判据读了一副本、看错了对象。
# **例外**写在下面这批 `_EQ_*`：坏实现内部要调得回"完好那一版"才能造出可比的坏形状
# （`_silent_parse` 得先拿好实现的返回值再抹掉问题列表）。本模块没有装饰器、没有
# `__wrapped__` 可借，所以只能在导入的第一时间抄一份本地绑定——这批名字**只给坏实现用**，
# 判据一条也不许读它们，否则判据就是在验一副本、而不是验产品。
SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
HOST_W, HOST_H = 480, 270

_EQ_NORMALIZE = equalizer.normalize
_EQ_PREAMP_GRAPH = equalizer.preamp_graph
_EQ_CHAIN_PARTS = equalizer.chain_parts
_EQ_BUILD_CHAIN = equalizer.build_chain
_EQ_STARTUP_ARGS = equalizer.startup_args
_EQ_PARSE_ENTRIES = equalizer.parse_entries

results: list[tuple[str, bool, str, str]] = []
falsified: list[tuple[str, bool, str]] = []
env_notes: list[str] = []
QUIET = False


def check(name: str, ok: bool, detail: str = "", fam: str = "表") -> bool:
    results.append((name, bool(ok), detail, fam))
    if not QUIET:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f"\n       {detail}"),
              flush=True)
    return bool(ok)


def env_note(name: str, detail: str) -> None:
    env_notes.append(f"{name}：{detail}")
    if not QUIET:
        print(f"[ENV ] {name} —— {detail}", flush=True)


def final_exit_code(fails: list[str], env: list[str]) -> int:
    if fails:
        return 1
    return 3 if env else 0


def pump(seconds: float) -> None:
    app = _app()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if app is not None:
            app.processEvents()
        time.sleep(0.01)


def _app():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance()


# `main()` 建出来的 QApplication 必须被一个**活得比判据久**的名字绑住（本机实测：写成
# `QApplication.instance() or QApplication(...)` 这一句**裸表达式**时 rc=127、stderr 全空、
# faulthandler 一声不响——正是 PyQt "槽里出事＝静默 abort" 那一族，只是这次出事的槽是 sip 的
# 析构：`A or B` 的 B 造出对象后立刻变成临时值，主循环还没开跑，包装器就被引用计数回收，
# Qt 侧的单例还在、Python 侧的 ownership 已经散，之后**面板那一家第一次建 QWidget** 时
# 进程当场没了（具体是哪一块控件没测——只测到"第一条判据之前"，见下面那条位置差）。
# 判据的**位置**也跟着变：三家全绿的那一趟停在面板最后一条，而红的那趟停在第一条之前——
# 也就是说这一句要是没修，永远看不见面板那一家的任何判决，而 rc=127 又最容易被误读成
# "面板里某条判据崩了"，从而去改判据、把真凶留在原地。
_APP_REF = None                    # 唯一用途：接住 main() 建出来的那个应用对象


def _unbound_app_builds_src(src: str) -> list[str]:
    """一段源码里"建了 `QApplication` 却没绑住名字"的那些语句（返回行号＋原话）。

    要抓的形状有两种：
      - `QApplication(sys.argv[:1])` 单独成一句（`ast.Expr` 直接是 Call）；
      - `QApplication.instance() or QApplication(...)` —— 这一次事故的那一句。
        它**看着像**一条有用的语句（"有就用、没有就建"），但 `A or B` 作为表达式语句时，
        建出来的 B 是临时值：语句执行完引用计数就归零，sip 的 Python 包装器被回收，
        而 Qt 那边的单例还活着。之后第一次建 QWidget 时进程当场没了（rc=127、stderr 全空）。
    绑了名字（`_APP_REF = ...`）就变成 `ast.Assign`，这里不该报名，所以判据只认 `ast.Expr`。
    """
    out: list[str] = []

    def builds(node: ast.AST) -> bool:
        """这个表达式（含 `or` 的任一分支）里有没有 `QApplication(...)` 这一个 Call。"""
        if isinstance(node, ast.Call):
            f = node.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname == "QApplication":
                return True
        if isinstance(node, ast.BoolOp):
            return any(builds(v) for v in node.values)
        return False

    tree = ast.parse(textwrap.dedent(src))

    def scan(body: list) -> None:
        for node in body:
            if isinstance(node, ast.Expr) and builds(node.value):
                out.append(f"L{node.lineno}:{ast.unparse(node.value)[:60]}")
            # 语句可能嵌在 def / if / for / with / try 里，那一层也是"独立成句"的语境，
            # 只扫 tree.body（模块顶层）会**对函数体内的裸表达式失明**——校准红那条就是这么暴露的。
            for attr in ("body", "orelse", "finalbody"):
                inner = getattr(node, attr, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    scan(inner)
            # try 的 except -handler 与 with/for 的单表达式头不用再看：那里出现独立语句不合法。

    scan(tree.body)
    return out


def _unbound_app_builds(fn) -> list[str]:
    return _unbound_app_builds_src(inspect.getsource(fn))



# ==================== 共用小工具 ====================

def cfg_of(vals: dict[str, float]) -> dict[str, float]:
    """把一组档位值变成"表读得懂的配置形状"（缺的键由表按默认补，不在这儿猜键名）。"""
    out = dict(equalizer.defaults())
    out.update({k: float(v) for k, v in vals.items()})
    return out


def last_text(seq) -> str:
    """判据读"最后说出来的那句话"的安全取法：列表空着就取空串，不抛。

    为什么单独成一个函数：判据的**条件表达式**里写 `p.log[-1]` 时，空列表抛的是 IndexError，
    于是那一趟的结局是"断言自己崩了"，而不是"点名了一条坏实现"。这两件事在证伪轮里长得一样
    （都是没点名），但只有前者会顺手把**后面所有判据**一起带走（本机实测：摘掉面板表外守卫
    那一趟崩在上一条判据的 `p.log[-1]` 上，目标那一条根本没跑到，被记成"量具自己崩了"）。
    判据的纪律是**报红、不报崩**。
    """
    try:
        return str(seq[-1]) if len(seq) else ""
    except (TypeError, IndexError, KeyError):
        return ""


def idx_of(items, pred) -> int:
    """第一个满足 `pred` 的下标，找不到返回 -1（`[x for x in … if …][0]` 的安全版）。

    找不到的那一条判据必须自己红掉，而不是让量具崩掉——同 `last_text` 那一条理由。
    """
    for i, x in enumerate(items):
        if pred(x):
            return i
    return -1


def split_af(chain: str) -> list[str]:
    """按内核切 `af` 列表的规则切条：**方括号里不切**（③ 实测 `lavfi=[a,b,c]` 是一条）。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in chain:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            if "".join(buf).strip():
                out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def af_entries(chain: str) -> list[dict]:
    """内核视角：把一条 `af` 串变成 `get af` 的**实测回报形状**（⑥ 那一腿的原始结构）。

    这里刻意**不**替本表检查形状合不合：本表写歪了它照原样记下来（一条带逗号的图就回报
    一条带逗号的图），让 `parse_entries` 那一头去发现。假内核要是只会附和正确的拼法，
    它就是一个预写答案的 fixture，第二家与第三家都测不到东西。
    """
    out: list[dict] = []
    for item in split_af(chain):
        head, sep, rest = item.partition("=")
        graph = ""
        if sep:
            g = rest.strip()
            graph = g[1:-1] if (g.startswith("[") and g.endswith("]")) else g.strip('"')
            out.append({"name": head.strip(), "enabled": True, "params": {"graph": graph}})
        else:
            out.append({"name": item, "enabled": True, "params": {}})
    return out


# ==================== 判据（离线·表） ====================

def j_clamps(key: str, send: float) -> tuple[bool, float]:
    """越界的数进来，必须落在量程内。"""
    s = equalizer.spec_for(key)
    got = equalizer.normalize(key, send)
    return (s.lo <= got <= s.hi), got


def j_nonfinite(key: str) -> tuple[bool, float]:
    """非有限的数（NaN／±inf）退回默认 0，而不是被 `min/max` 夹成量程端点。"""
    got = equalizer.normalize(key, float("nan"))
    return got == equalizer.spec_for(key).default, got


def j_empty(cfg: dict) -> tuple[bool, str]:
    """全零＝空串＝一条都不发。空图 `lavfi=[]` 是实测会让整条 af 起不来的形状。"""
    return (equalizer.build_chain(cfg) == "" and equalizer.startup_args(cfg) == []), \
        f"链={equalizer.build_chain(cfg)!r} 参数={equalizer.startup_args(cfg)}"


def j_shape(cfg: dict) -> tuple[bool, str]:
    """每一条都是 `lavfi=[图]`，括号里不带逗号，条与条之间才用逗号。"""
    chain = equalizer.build_chain(cfg)
    if not chain:
        return True, "空链"
    parts = split_af(chain)
    bad = [p for p in parts if not (p.startswith("lavfi=[") and p.endswith("]"))]
    inner = [p for p in parts if "," in p[7:-1]]
    return (not bad and not inner), f"形状不合={bad[:2]} 括号里带逗号={inner[:2]}"


def j_order(cfg: dict) -> tuple[bool, str]:
    """低频在前、前置在最后（前置在最后是"先均衡再收音量"，与实测的链顺序一致）。"""
    parts = equalizer.chain_parts(cfg_of(cfg))
    hz: list[float] = []
    pre_at = None
    for i, p in enumerate(parts):
        if p.startswith("volume="):
            pre_at = i
        elif p.startswith("equalizer="):
            hz.append(float(p.split("f=", 1)[1].split(":")[0]))
    asc = hz == sorted(hz)
    last = (pre_at is None) or (pre_at == len(parts) - 1)
    return asc and last, f"频率序={hz} 前置位置={pre_at}/{len(parts) - 1}"


def j_preamp_spelling(vals: list[float]) -> list[str]:
    """非零前置的每一串都必须带 `dB`；0 必须给空串。不带后缀那一档内核按**线性**算
    （实测 −12 → +17.9 dB 并削顶），而空图是实测"整条 af 起不来"的那一个形状。

    大小写在这里是**两种量纲**：内核认 `dB`／`db` 都算后缀，但判据要是把大小写抖一遍
    再去比 `"dB"`，好实现也会被读成"没后缀"。所以只做小写对小时的比较。
    """
    bad = []
    for v in vals:
        g = equalizer.preamp_graph(v)
        if v == 0:
            if g != "":
                bad.append(f"0→{g!r}（0 不该上链）")
        elif not g.startswith("volume=") or not g.lower().endswith("db"):
            bad.append(f"{v}→{g!r}")
    return bad


def j_roundtrip(cfg: dict) -> tuple[bool, str]:
    """发出去什么形状，读回来就该是那几个数（表→串→回报形状→反解，逐档比）。"""
    full = cfg_of(cfg)
    want = equalizer.gains(full)
    parsed, problems = equalizer.parse_entries(af_entries(equalizer.build_chain(full)))
    if parsed is None:
        return False, f"反解整条读不出：{problems[:2]}"
    if problems:
        return False, f"有读不懂的条目：{problems[:2]}"
    diff = [f"{k} 发 {want[k]:+g} 回 {float(parsed.get(k, 0.0)):+g}" for k in equalizer.ORDER
            if abs(float(parsed.get(k, 0.0)) - want[k]) > 1e-6]
    return not diff, f"不一致={diff[:3]}"


def j_bad_shapes() -> list[tuple[str, Any, str]]:
    """认不出来的东西**清单**（每一条都该进"原话"，一条也不许被静默丢掉或当成 0）。"""
    return [
        ("内核没应答（None）", None, "读不到 af"),
        ("回报是串", "lavfi=[equalizer=f=31:t=h:w=13:g=1]", "串而不是列表"),
        ("回报是 dict", {"name": "lavfi"}, "形状不认识"),
        ("条目不是对象", ["lavfi=[x]"], "条目不是对象"),
        ("别人挂的非 lavfi 滤镜", [{"name": "volume", "enabled": True}], "非本表条目"),
        ("空图", [{"name": "lavfi", "enabled": True, "params": {}}], "没有 graph"),
        ("括号图里带逗号", [{"name": "lavfi", "enabled": True,
                            "params": {"graph": "equalizer=f=31:t=h:w=13:g=6,"
                                                 "equalizer=f=62:t=h:w=28:g=6"}}], "带逗号"),
        ("条目被禁用", [{"name": "lavfi", "enabled": False,
                         "params": {"graph": "equalizer=f=31:t=h:w=13:g=6"}}], "被禁用"),
        ("带宽不按 Hz（t=q）", [{"name": "lavfi", "enabled": True,
                                 "params": {"graph": "equalizer=f=31:t=q:w=2:g=6"}}], "t 不是 h"),
        ("中心频率不在表里", [{"name": "lavfi", "enabled": True,
                               "params": {"graph": "equalizer=f=33:t=h:w=13:g=6"}}], "不在表里"),
        ("增益不是数", [{"name": "lavfi", "enabled": True,
                         "params": {"graph": "equalizer=f=31:t=h:w=13:g=abc"}}], "读不懂"),
        ("同一频段挂两次", [{"name": "lavfi", "enabled": True,
                             "params": {"graph": "equalizer=f=31:t=h:w=13:g=6"}},
                            {"name": "lavfi", "enabled": True,
                             "params": {"graph": "equalizer=f=31:t=h:w=13:g=-6"}}], "挂了两次"),
        ("前置不带 dB（内核按线性）", [{"name": "lavfi", "enabled": True,
                                        "params": {"graph": "volume=-12"}}], "不带 dB 后缀"),
        ("不认识的滤镜", [{"name": "lavfi", "enabled": True,
                           "params": {"graph": "bass=g=12"}}], "不认识的滤镜"),
    ]


def j_empty_vs_unreadable() -> tuple[bool, str]:
    """「链是空的」（`{}`）与「整条读不懂」（None）必须分得开：把读不懂当空链，
    界面就会画一页 0，而内核挂的是别的东西。"""
    empty, p1 = equalizer.parse_entries([])
    none, p2 = equalizer.parse_entries(None)
    return (empty == {} and not p1 and none is None and bool(p2)), \
        f"空链→{empty!r}/{p1} 读不懂→{none!r}/{p2[:1]}"


def j_presets() -> tuple[bool, str]:
    """预设是**外壳的口味**：唯一有实测撑的规矩是净抬升不越过 `CLIP_NET_MAX`（⑦ 那一腿）。"""
    bad: list[str] = []
    for p in equalizer.PRESETS:
        if set(p.vals) != set(equalizer.ORDER):
            bad.append(f"{p.key} 缺键/多键")
        over = [k for k, v in p.vals.items()
                if not (equalizer.spec_for(k).lo <= v <= equalizer.spec_for(k).hi)]
        if over:
            bad.append(f"{p.key} 越界={over}")
        if p.clip_net() > equalizer.CLIP_NET_MAX + 1e-9:
            bad.append(f"{p.key} 净抬升 {p.clip_net():+.1f} > {equalizer.CLIP_NET_MAX:+.1f}")
    keys = [p.key for p in equalizer.PRESETS]
    if len(set(keys)) != len(keys):
        bad.append("预设键重复")
    return not bad, f"不合={bad}"


def j_auto_preamp() -> tuple[bool, str]:
    """口味线的算术：净抬升 = 最大抬升 + 前置 ≤ +3，且前置只做"往下让"（≤0）。"""
    cases = [({"eq_1k": 12.0}, -9.0), ({"eq_1k": 0.0}, 0.0), ({"eq_1k": 6.0}, -3.0),
             ({"eq_1k": 2.0}, 0.0), ({"eq_31": 12.0, "eq_16k": 12.0}, -9.0)]
    bad = []
    for vals, want in cases:
        got = equalizer.auto_preamp(dict(vals, **{equalizer.PREAMP.cfg: 0.0}))
        if abs(got - want) > 1e-6:
            bad.append(f"{vals}→{got}（应 {want}）")
    top = equalizer.auto_preamp({**{b.cfg: equalizer.BAND_HI for b in equalizer.BANDS},
                                 equalizer.PREAMP.cfg: 0.0})
    if top > 0 or top < equalizer.PRE_LO:
        bad.append(f"全开时前置={top} 不在 [{equalizer.PRE_LO}, 0]")
    return not bad, f"不合={bad}"


# 九条**真原话**逐字抄自 `tests/out/eq-probe-r15b.log`（腿 ①／② 的三档坏形状与②的空图档），
# 三条正常行抄自同一份日志里那些**出了声音**的腿。判据用表里的前缀，而不是在这里猜关键字。
BAD_LOG_LINES = [
    "[   0.003][e][cplayer] Option af: Error while parsing lavfi parameter bass (g)",
    "[   0.003][e][cplayer] Error parsing option af (option parameter could not be parsed)",
    "[   0.003][f][cplayer] Setting commandline option --af=lavfi=bass=g=12:f=110 failed.",
    "[   0.397][e][ffmpeg] Parsed_anequalizer_0: Could not set non-existent option 'f' to value '100'",
    "[   0.410][e][ffmpeg] Parsed_bass_0: Value 9999.000000 for parameter 'g' out of range [-900 - 900]",
    "[   0.392][e][ffmpeg] AVFilterGraph: No such filter: 'definitely_not_a_filter'",
    "[   0.392][e][ffmpeg] AVFilterGraph: Error creating filters",
    "[   0.401][e][cplayer] Audio filter initialized failed!",
    "[   0.401][f][cplayer] No video or audio streams selected.",
]
OK_LOG_LINES = [
    "[   0.403][v][af]   lavfi-bass (lavfi-bass.00)",
    "[   0.408][v][af] [lavfi-bass] 48000Hz stereo 2ch s16",
    "[   0.391][v][af] User filter list:",
]


# ==================== 离线·表：把上面的判据跑成账 ====================

def table_checks() -> None:
    fam = "表"
    ORDER = equalizer.ORDER
    SPECS = equalizer.SPECS
    BANDS = equalizer.BANDS

    # 量具自己先过一道：`main()` 建 QApplication 那一句要是写成裸表达式，面板那一家的三十条
    # 判据**一条都跑不到**，而红的形状是 rc=127、stderr 全空——长得像"面板里某条判据崩了"，
    # 于是很容易去改判据、把真凶留在原地（本机实测踩过：`_bisect` 一路 43 条 PASS 然后没）。
    # 这一条放在最前面，是为了让下一轮回归时它**先于崩溃点名**；校准两条（一红一绿）
    # 证明这把尺既不瞎也不误伤——只扫 `main` 的尺要是恒绿，写它没意义。
    leak = _unbound_app_builds(main)
    cal_red = _unbound_app_builds_src("def f():\n    QApplication.instance() or QApplication(a)\n")
    cal_green = _unbound_app_builds_src("def f():\n    x = QApplication.instance() or QApplication(a)\n")
    check("量具自证：`main()` 里建 `QApplication` 的那一句绑住了名字（`A or B` 写成裸表达式＝"
          "包装器当场被回收，之后第一次建 QWidget 就是 rc=127、stderr 全空的静默 abort），"
          "且这把尺自己校准过：同一句不加名字必须报名、加了名字必须不报名",
          not leak and bool(cal_red) and not cal_green,
          f"漏绑={leak[:1]} 校准红={cal_red[:1]} 校准绿={cal_green[:1]}", fam)

    # 1) 表的形状
    check("表恰好十一项（十段＋前置），配置键与 ORDER 一一对应、没有第二份名单",
          len(SPECS) == 11 and ORDER == tuple(s.cfg for s in SPECS)
          and len(set(ORDER)) == 11 and equalizer.NUMS == SPECS,
          f"项={len(SPECS)} 序={ORDER}", fam)
    check("十段的中心频率是 ISO 十倍频程标准档（31…16k），带名与配置键同源",
          [b.hz for b in BANDS] == [31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
          and [b.cfg for b in BANDS] == list(ORDER[:10])
          and all(equalizer.BAND_BY_CFG[c] is not None for c in ORDER[:10]),
          f"频率={[b.hz for b in BANDS]}", fam)
    check("量程按「能听且不削顶」收：段 ±12 dB、前置 −18…+6（内核允许 ±900，越界由外壳夹）",
          (equalizer.BAND_LO, equalizer.BAND_HI) == (-12.0, 12.0)
          and (equalizer.PRE_LO, equalizer.PRE_HI) == (-18.0, 6.0)
          and all(s.step == 1.0 for s in SPECS),
          f"段={equalizer.BAND_LO}..{equalizer.BAND_HI} 前置={equalizer.PRE_LO}..{equalizer.PRE_HI} "
          f"步进={sorted({s.step for s in SPECS})}", fam)
    bad = [f"{s.cfg}:{v!r}→{equalizer.normalize(s.cfg, v)}"
           for s in SPECS for v in (9999, -9999, None, "x") if not j_clamps(s.cfg, v)[0]]
    check("越界与脏值进表都会被夹回量程（这一族漏进 af 链的下场不是「这一档没生效」，是整条起不来）",
          not bad, f"漏网={bad[:4]}", fam)
    bad = [f"{s.cfg}→{j_nonfinite(s.cfg)[1]}" for s in SPECS if not j_nonfinite(s.cfg)[0]]
    check("非有限的数（NaN／±inf）退回默认 0 而不是满增益：min/max 遇到 NaN 留下上界，"
          "「读不懂」不能翻成「+12 dB 吓用户一跳」", not bad, f"不合={bad}", fam)
    check("带宽 w 随中心频率长（≈0.45 倍中心频），低频那档不塌成 0",
          all(equalizer.band_width(b.hz) == max(int(b.hz * 0.45), 8) for b in BANDS)
          and equalizer.band_width(31) >= 8,
          f"带宽={[equalizer.band_width(b.hz) for b in BANDS]}", fam)

    # 2) 拼串的形状（实测 ⑥：只有 `lavfi=[图]` 这一种在本机被反复自证过）
    z = equalizer.defaults()
    ok, d = j_empty(z)
    check("全零＝空串、且启动参数一条都不发（实测 `--af=lavfi=[]` 会让内核 "
          "`Audio filter initialized failed!`，整个进程一声不出）", ok, d, fam)
    for cfg in ({}, {"eq_31": 12}, {"eq_31": -1, "eq_preamp": -3},
                {b.cfg: 6.0 for b in BANDS},
                {**{b.cfg: 12.0 for b in BANDS}, "eq_preamp": -9}):
        s2, d2 = j_shape(cfg_of(cfg))
        check("链形状合规（每条 `lavfi=[…]`、括号里不带逗号）："
              + (f"{str(cfg)[:30]}" if not s2 else f"{len(cfg)} 档"), s2, d2, fam)
    s3, d3 = j_order({**{b.cfg: 6.0 for b in BANDS}, "eq_preamp": -6})
    check("链顺序＝低频在前、前置在最后（前置在最后是「先均衡、再收总音量」）", s3, d3, fam)
    full = equalizer.build_chain(cfg_of({**{b.cfg: 12.0 for b in BANDS}, "eq_preamp": -9}))
    check("十段全开＋前置＝十一条目、内核按 N 条结构化条目回报（⑥ 实测 `append` 带逗号整条拒收，"
          "括号里留逗号就是给内核递刀）",
          full.count("lavfi=[") == 11 and full.count(",") == 10,
          f"条目={full.count('lavfi=[')} 逗号={full.count(',')}", fam)
    bad = j_preamp_spelling([0.0, -9.0, 6.0, -18.0, -3.5, 12.0])
    check("前置永远拼成 `volume=<数>dB`、且 0 不上链（`volume=-12` 没有后缀是**线性**乘 12，"
          "实测 +17.9 dB 并削顶；`dB(-9)` 那种函数形式本机直接失败）", not bad, f"不合={bad}", fam)
    check("启动参数与链同步：链不空时恰好一条 `--af=`、链空时一条都没有",
          equalizer.startup_args(cfg_of({"eq_1k": 3.0}))
          == [f"--af={equalizer.build_chain(cfg_of({'eq_1k': 3.0}))}"]
          and equalizer.startup_args(z) == [],
          f"非空={equalizer.startup_args(cfg_of({'eq_1k': 3.0}))[:1]} 空={equalizer.startup_args(z)}",
          fam)

    # 3) 读回的反解：表→串→回报形状→逐档数
    rnd = random.Random(20261012)
    trip_bad = []
    for _i in range(40):
        cfg = {b.cfg: float(rnd.choice([0, 0, -12, -6, -1, 1, 3, 6, 12])) for b in BANDS}
        cfg[equalizer.PREAMP.cfg] = float(rnd.choice([0, -18, -9, -3, 1, 6]))
        ok, d = j_roundtrip(cfg)
        if not ok:
            trip_bad.append(d)
    check("40 组随机档位走「发出→内核视角回报→反解」一圈，逐档必须回到同一个数（面板画的就是它）",
          not trip_bad, f"不一致={trip_bad[:2]}", fam)
    ok, d = j_empty_vs_unreadable()
    check("空 dict（链是空的）与 None（整条读不懂）分得开——把读不懂当空链就是「画一页 0、"
          "内核挂着别的东西」", ok, d, fam)
    miss = []
    for name, raw, kw in j_bad_shapes():
        parsed, problems = equalizer.parse_entries(raw)
        if parsed is not None and not problems:
            miss.append(f"{name}：静默放过，画成 {parsed}")
        elif problems and not any(kw in p for p in problems):
            miss.append(f"{name}：原话里没点名「{kw}」→{problems[0][:44]}")
    check(f"{len(j_bad_shapes())} 类认不出来的形状各有原话（一条也不许被 min()／静默丢掉）",
          not miss, f"漏网={miss}", fam)
    hit = idx_of(j_bad_shapes(), lambda x: x[0].startswith("前置不带 dB"))
    one = j_bad_shapes()[hit] if hit >= 0 else None
    parsed, problems = (equalizer.parse_entries(one[1]) if one is not None else ({}, []))
    check("最阴那一条单独钉：`volume=-12` 不带后缀时绝不按 dB 解释（内核按线性＝乘 12），"
          "只能原话报出来——符号相同、量纲不同、下场是爆音",
          one is not None and equalizer.PREAMP.cfg not in parsed and bool(problems),
          f"样本={one[0] if one is not None else '这一类不在 fixture 里'} "
          f"解成={parsed} 原话={problems[:1]}", fam)
    m1, b1 = equalizer.parse_matches(
        cfg_of({"eq_1k": 3.0}),
        dict(equalizer.parse_entries(
            af_entries(equalizer.build_chain(cfg_of({"eq_1k": 3.0}))))[0]))
    m2, b2 = equalizer.parse_matches(cfg_of({"eq_1k": 3.0}), {"eq_1k": 5.0, "eq_31": 2.0})
    m3, b3 = equalizer.parse_matches(cfg_of({}), None)
    check("请求与读回比两头：一致时说一致，差一档与多一档都点名；整条读不懂不许说一致",
          m1 and not b1 and not m2 and len(b2) == 2 and not m3,
          f"一致={b1} 不一致={b2} 读不懂={b3}", fam)

    # 4) 预设与口味线
    ok, d = j_presets()
    check(f"每条预设都在量程内、键覆盖十一档、且净抬升 ≤ {equalizer.CLIP_NET_MAX:+.0f} dB"
          "（⑦ 实测 +12−9=+3 时 clip=0；这条线以上没测过，所以预设不许越线）", ok, d, fam)
    ok, d = j_auto_preamp()
    check("口味线的算术：前置 = clamp(CLIP_NET_MAX − 最大抬升) 且不往上走", ok, d, fam)
    p = equalizer.preset_values("bass")
    check("预设取值按表夹一遍、表外预设返回 None（写歪的预设数不能原样进链）",
          p is not None and all(equalizer.spec_for(k).lo <= v <= equalizer.spec_for(k).hi
                                for k, v in p.items())
          and equalizer.preset_values("nope") is None,
          f"bass={p} 表外={equalizer.preset_values('nope')}", fam)

    # 5) 日志：这一族的"其实没声音"
    blind = [ln for ln in BAD_LOG_LINES if not equalizer.failures(ln)]
    check(f"{len(BAD_LOG_LINES)} 条真原话逐行认出来（抄自 eq-probe-r15b.log 的 ①/②/⑥ 三档，"
          f"表里 {len(equalizer.EQ_FAIL_MARKS)} 个前缀）", not blind, f"没认出={blind[:2]}", fam)
    false_red = [ln for ln in OK_LOG_LINES if equalizer.failures(ln)]
    check("正常日志不许被读成失败（那三条是同一份日志里出了声音的腿；「空表」的意思才是"
          "那一次没有坏消息，而不是「没在读」）", not false_red, f"误报={false_red[:2]}", fam)

    # 6) 与配置层的接口（第二份事实表的产地就是这里）
    eq_keys = sorted(k for k in cfgmod.DEFAULTS if k.startswith("eq_"))
    check("配置默认值来自这张表本身（DEFAULTS 里恰好这十一个 eq_ 键、值与表一致——"
          "两份名单迟早有一份不改）",
          eq_keys == sorted(ORDER) and all(cfgmod.DEFAULTS[k] == equalizer.defaults()[k]
                                           for k in eq_keys),
          f"配置键={eq_keys} 表={sorted(ORDER)}", fam)
    live_cfg = cfgmod.Config()
    check("新配置读出来就带这十一个键（老配置缺键时由 DEFAULTS 补，面板不会 .get 到 None 再画 0）",
          all(live_cfg.get(k) is not None for k in ORDER),
          f"缺={[(k, live_cfg.get(k)) for k in ORDER if live_cfg.get(k) is None]}", fam)
    check("概览与「有没有上链」按同一份判据（全零说未启用，动一档就说那一档）",
          equalizer.is_active(z) is False and bool(equalizer.summary(z))
          and equalizer.is_active(cfg_of({"eq_1k": 3.0})) is True
          and "1 kHz" in equalizer.summary(cfg_of({"eq_1k": 3.0})),
          f"全零={equalizer.summary(z)!r} 动一档={equalizer.summary(cfg_of({'eq_1k': 3.0}))!r}", fam)
    check("值写法三种形状：0 写「0 dB」（不写 +0）、正数带 +、负数带 −；表外键原样退回",
          equalizer.value_text("eq_1k", 0) == "0 dB"
          and equalizer.value_text("eq_1k", 3) == "+3 dB"
          and equalizer.value_text("eq_1k", -3) == "-3 dB"
          and equalizer.value_text("nope", "x") == "x",
          f"0={equalizer.value_text('eq_1k', 0)!r} 3={equalizer.value_text('eq_1k', 3)!r}", fam)
    check("标签从表取（十段是「31 Hz…16 kHz」，前置是「前置增益」），界面不许再抄一份",
          equalizer.label("eq_31") == "31 Hz" and equalizer.label("eq_16k") == "16 kHz"
          and equalizer.label("eq_preamp") == "前置增益" and equalizer.label("nope") == "nope",
          f"={[equalizer.label(b.cfg) for b in BANDS]}", fam)
    check("这张表的实测依据钉在内核版本上（换了 mpv 就得重跑 tests/probe_eq.py，"
          "否则「实测」两个字是别人机器上的数）",
          equalizer.MPV_MEASURED_ON == "v0.41.0-1101-g5d85ba5fb",
          f"表依据={equalizer.MPV_MEASURED_ON}", fam)


# ==================== 离线·引擎：真 MpvPlayer，但不启动内核 ====================

def engine_offline_checks() -> None:
    """`_ipc is None` 那条路本身是被测对象：内核没在家时引擎必须既不说"生效"也不说"坏了"。

    这一家不需要 mpv 进程，也不需要假内核——它拿的就是产品那个类。
    """
    fam = "引擎"
    cfg = cfgmod.Config()
    for k, v in equalizer.reset_values().items():
        cfg[k] = v
    p = MpvPlayer(cfg)
    said: list[str] = []
    p.sig_log.connect(lambda t: said.append(str(t)))
    check("前提自证：没 start 过的 MpvPlayer 是 `_ipc is None` 且 `running` 为假"
          "（这一家其余几条全都挂在这个前提上，前提塌了它们会一起变成空话）",
          p._ipc is None and p.running is False,
          f"_ipc={p._ipc!r} running={p.running}", fam)
    # 这一家读的是真引擎的 `_log_since`，而那函数开的是**磁盘上那份 mpv.log**：上一轮真机播放
    # 留下的失败行会在这里冒充"本轮链坏了"，红得和产品的表态无关、只和机器上碰巧躺着什么有关。
    # 所以把日志路径挪到一个不存在的文件（`_log_offset`→0、`_log_since`→空串＝这一轮没有新证据），
    # 并当场自证这个前提——前提不成立时下面那几条"不发假警报"的判据全都不作数。
    p.log_path = os.path.join(HERE, "out", "_engine-offline-no-such-log.log")
    if os.path.exists(p.log_path):
        os.remove(p.log_path)
    check("量具自证：这一家的日志窗口指向不存在的文件，`_log_since` 读到空串"
          "（否则上一轮真机留下的坏消息会冒充本轮判决——那是环境红，不是产品红）",
          p._log_offset() == 0 and p._log_since(0) == "", f"路径={p.log_path}", fam)
    chain = equalizer.build_chain(cfg)                      # 归零后的链：必须是空的（前提）
    for k, v in (equalizer.preset_values("cinema") or {}).items():
        cfg[k] = v
    args = p.build_args(1)
    af_args = [a for a in args if a.startswith("--af=")]
    want = f"--af={equalizer.build_chain(cfg)}"
    check("表→启动参数：`--af=` 那条真的拼进了内核命令行，且只有一条（缺一条＝第一次播放前"
          "那几秒没均衡，而退出后配置与内核永远差一截）",
          chain == "" and want != "--af=" and af_args == [want],
          f"af 参数={af_args[:1]} 期望={want[:70]}", fam)
    for k, v in equalizer.reset_values().items():
        cfg[k] = v
    args0 = p.build_args(1)
    check("链为空时启动参数里不出现 `--af=`（`--af=lavfi=[]` 是实测"
          "`Audio filter initialized failed!` 那一个形状）",
          not [a for a in args0 if a.startswith("--af=")],
          f"af 参数={[a for a in args0 if a.startswith('--af=')][:1]}", fam)
    del said[:]
    got, ok = p.set_audio_eq("eq_1k", 6.0, confirm=True)
    check("内核没在家时的「松手确认」：值进配置、返回「没确认」，但**不去问 `af`**——"
          "没人应答的问句会在反解里留下一句「读不到 af」，那是问句的形状，不是链坏了的形状",
          got == 6.0 and ok is False and equalizer.gain_of(cfg, "eq_1k") == 6.0 and not said,
          f"回报={got} ok={ok} cfg={equalizer.gain_of(cfg, 'eq_1k')} 日志={said[:1]}", fam)
    del said[:]
    _c, vals, problems, fails = p.read_audio_eq(0)
    _c, vals2, problems2, fails2 = p.read_audio_eq()
    check("内核没在家时纯读：两句假警报都不发，同时「读不到 af」这句**只留在返回值里**"
          "（不外发、也不当成链的判决）",
          not said and vals == {} and vals2 == {} and not fails and not fails2
          and bool(problems) and "读不到 af" in problems[0],
          f"日志={said[:1]} 原话={problems[:1]} 失败行={fails[:1]}", fam)
    del said[:]
    got2, ok2 = p.set_audio_eq("eq_bass_boost", 3.0, confirm=True)
    # 这一句读的是 `cfg.data` 而不是 `"eq_bass_boost" not in cfg`：`Config` 只有 `__getitem__`、
    # 没有 `__contains__`/`__iter__`，而 `__getitem__` 对不认识的键**返回 None 而不抛 KeyError**，
    # 于是 `in cfg` 会按"用 0,1,2… 试到 StopIteration"那条协议**永远试下去**（本机实测：整趟
    # 卡死在这一个表达式上，45 s 栈钩子指着的就是这一行）。判据要的"这个键没被写进配置"
    # 落在真正那张 dict 上才有意义。
    check("表外档位引擎自己也不发命令、只留一句（面板那头的守卫不是唯一一道闸）",
          got2 == 0.0 and ok2 is False and len(said) == 1 and "不在表里" in said[0]
          and "eq_bass_boost" in said[0] and "eq_bass_boost" not in cfg.data,
          f"回报={got2},{ok2} 日志={said[:2]}", fam)
    for k, v in equalizer.reset_values().items():
        cfg[k] = v
    del said[:]
    got3, problems3, fails3 = p.reset_audio_eq()
    check("内核没在家时一键复位：配置照样归零、链是空的、且一句假警报不发（那会把 OSD 刷满）",
          equalizer.build_chain(cfg) == "" and not said and got3 == {},
          f"链={equalizer.build_chain(cfg)!r} 日志={said[:2]} 读回={got3}", fam)
    for k, v in cfg_of({"eq_1k": 4.0}).items():
        cfg[k] = v
    got4, pr4, fl4 = p.apply_audio_eq_preset("nope")
    check("表外预设不碰配置、并留一句可判的形状（返回 ({} , [], [原话])）",
          got4 == {} and pr4 == [] and fl4 == ["预设不在表里"]
          and equalizer.gain_of(cfg, "eq_1k") == 4.0,
          f"读回={got4} 原话={pr4} 失败={fl4} cfg_1k={equalizer.gain_of(cfg, 'eq_1k')}", fam)
    st = MpvPlayer(cfgmod.Config()).audio_eq_state()
    check("引擎的读回缓存起步是「空表 + 两个空列表」：面板据此按 0 画，"
          "而不是拿配置当内核的话", st == {"problems": [], "failed": []}, f"起步={st}", fam)


# ==================== 假内核（离线·面板用） ====================

class _Sig:
    """最小信号：面板只 connect 与 emit，不需要真的 Qt 信号语义。"""

    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, fn) -> None:
        self._slots.append(fn)

    def emit(self, *a) -> None:
        for fn in self._slots:
            fn(*a)


class FakeEngine:
    """`MpvPlayer` 那六个均衡器方法的**逐句同构**副本，只把"内核"换成一片可查的 dict。

    三条纪律，每条都有判据钉着：
      1. 方法名、参数与 `if` 条件集合和真引擎一致（P0 那条走 AST，见 `_if_tests`）——
         假内核哪天悄悄宽松一点，面板腿就会绿着骗人，所以这条必须先红给它看；
      2. `af` 的回报由**上一次发出去的那条命令**决定（`_apply`），所以面板拿到的数
         必须真走过一次拼串＋一次反解，而不是 fixture 预写的正确答案；
      3. `running=False` 时 `_req/_fire/get` 与真引擎一样空转（真引擎是 `_ipc is None`），
         计数分两套：`attempts`/`tries_get` 是"面板想问几次"（空转也记），
         `n_req/n_fire/n_get` 是"内核真收了几条"。内核没在家时判据说的是"一条都没收到、
         而且一次也没去问 `af`"，所以那一句读的是 `tries_get` 与 `n_*`，不是 `attempts`。
      4. 命令**会让内核往日志里写行**：`pending_log` 里预置的那几行在命令到达时才落进
         `log_lines`（顺序和真引擎一致——先记日志偏移、再发命令、新行在后）。测试要是提前
         把失败行塞进 `log_lines`，`_log_offset()` 会把它挡在窗口之外，红的是量具自己。
    """

    def __init__(self, cfg, live: bool = True):
        self.cfg = cfg
        self.running = live
        self.log_lines: list[str] = []
        self.pending_log: list[str] = []          # 下一条命令"让内核写出去"的那几行
        self.distort: dict[float, float] = {}     # 中心频率 → 内核真挂的 dB（P7 那条用）
        self.attempts = 0
        self.tries_get = 0
        self.n_req = self.n_fire = self.n_get = 0
        self.sig_log = _Sig()
        self._eq_vals: dict[str, float] = {}
        self._eq_problems: list[str] = []
        self._eq_failures: list[str] = []
        # 启动参数装上的那条链：内核在起跑时就已经按配置挂好了（⑤：af 跨 loadfile 留着）
        self._af = af_entries(equalizer.build_chain(cfg))

    # ---- 与真引擎同名的三条底层：空转的形状也要一样 ----
    def get(self, name: str, default: Any = None) -> Any:
        self.attempts += 1
        self.tries_get += 1
        if not self.running:
            return default
        self.n_get += 1
        if name == "af":
            return self._distort(self._af)
        return default

    def _req(self, command: list, timeout: float = 2.5) -> Any:
        self.attempts += 1
        if not self.running:
            return None
        self.n_req += 1
        self._apply(command)
        self._write_log()
        return None

    def _fire(self, command: list) -> None:
        self.attempts += 1
        if not self.running:
            return
        self.n_fire += 1
        self._apply(command)
        self._write_log()

    def _write_log(self) -> None:
        """命令落地**之后**才把 `pending_log` 写进日志：真引擎是先记偏移、再发命令、
        内核在那之后写行，所以新行必须落在偏移后面，否则 `read_audio_eq(off)` 的窗口是空的，
        判据会看见一条"链很干净"的假绿。"""
        if self.pending_log:
            self.log_lines.extend(self.pending_log)
            self.pending_log = []

    def _apply(self, command: list) -> None:
        if len(command) >= 4 and command[0] == "change-list" and command[1] == "af" \
                and command[2] == "set":
            self._af = af_entries(str(command[3]))

    def _log_offset(self) -> int:
        return len(self.log_lines)

    def _log_since(self, off: int) -> str:
        return "\n".join(self.log_lines[off:])

    def _distort(self, entries: list[dict]) -> list[dict]:
        """把内核"挂的数"改成 `distort` 里的值（形状仍是本表那一种，只换 g）。"""
        out = []
        for e in entries:
            g = str((e.get("params") or {}).get("graph", ""))
            if g.startswith("equalizer="):
                try:
                    hz = float(g.split("f=", 1)[1].split(":")[0])
                except (IndexError, ValueError):
                    hz = 0.0
                if hz in self.distort:
                    g = (f"equalizer=f={int(hz)}:t=h:w={equalizer.band_width(int(hz))}"
                         f":g={equalizer._num(self.distort[hz])}")
            out.append({**e, "params": {**(e.get("params") or {}), "graph": g}})
        return out

    # ---- 下面六个方法照 `app/engine/player.py` 逐句同构（P0 那条 AST 判据钉着） ----
    def apply_audio_eq_chain(self) -> tuple[str, dict[str, float], list[str], list[str]]:
        chain = equalizer.build_chain(self.cfg)
        off = self._log_offset()
        self._req(["change-list", "af", "set", chain])
        return self.read_audio_eq(off)

    def read_audio_eq(self, log_off: int | None = None) -> tuple[str, dict[str, float],
                                                                 list[str], list[str]]:
        chain = equalizer.build_chain(self.cfg)
        parsed, problems = equalizer.parse_entries(self.get("af", None))
        vals = parsed if parsed is not None else {}
        if log_off is None:
            fails = list(self._eq_failures)
            if problems and self.running:
                self.sig_log.emit("均衡器读回有读不懂的条目：" + "；".join(problems[:3]))
        else:
            fails = equalizer.failures(self._log_since(log_off))
            if self.running:
                if fails:
                    self.sig_log.emit("均衡器链路没成立：" + "；".join(fails[:3]))
                elif problems:
                    self.sig_log.emit("均衡器读回有读不懂的条目：" + "；".join(problems[:3]))
        self._eq_vals = vals
        self._eq_problems = list(problems)
        self._eq_failures = list(fails)
        return chain, vals, problems, fails

    def set_audio_eq(self, key: str, value: Any, persist: bool = True,
                     confirm: bool = True) -> tuple[float, bool]:
        spec = equalizer.spec_for(key)
        if spec is None:
            self.sig_log.emit(f"均衡器档位不在表里：{key}")
            return 0.0, False
        want = equalizer.normalize(key, value)
        if persist:
            self.cfg[key] = want
        if not confirm:
            self._fire(["change-list", "af", "set", equalizer.build_chain(self.cfg)])
            return want, True
        if not self.running:
            return want, False
        _chain, vals, problems, fails = self.apply_audio_eq_chain()
        got = float(vals.get(key, 0.0) or 0.0)
        ok = (not fails) and (not problems) and abs(got - want) < 1e-4
        if not ok:
            why = fails[0] if fails else (problems[0] if problems
                                          else f"内核那一档是 {got:+g}，请求的是 {want:+g}")
            self.sig_log.emit(f"{equalizer.label(key)} 没确认生效：{why}")
        return (got if not fails else want), ok

    def apply_audio_eq_preset(self, key: str) -> tuple[dict[str, float], list[str], list[str]]:
        vals = equalizer.preset_values(key)
        if vals is None:
            self.sig_log.emit(f"均衡器预设不在表里：{key}")
            return {}, [], ["预设不在表里"]
        for k, v in vals.items():
            self.cfg[k] = v
        _chain, got, problems, fails = self.apply_audio_eq_chain()
        return got, problems, fails

    def reset_audio_eq(self) -> tuple[dict[str, float], list[str], list[str]]:
        return self.apply_audio_eq_preset("flat")

    def audio_eq_state(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self._eq_vals)
        out["problems"] = list(self._eq_problems)
        out["failed"] = list(self._eq_failures)
        return out


EQ_METHODS = ("apply_audio_eq_chain", "read_audio_eq", "set_audio_eq",
              "apply_audio_eq_preset", "reset_audio_eq", "audio_eq_state")


def _if_tests(fn) -> set[str]:
    """一个函数体里所有 `if` 的条件表达式（unparse 成文本，供两边比集合）。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return {ast.unparse(node.test) for node in ast.walk(tree) if isinstance(node, ast.If)}


def _sig(fn) -> tuple:
    s = inspect.signature(fn)
    empty = inspect.Parameter.empty
    return tuple((p.name, None if p.default is empty else p.default)
                 for p in s.parameters.values())


def make_panel(live: bool = True, vals: dict[str, float] | None = None):
    from app.ui.audio_eq_panel import AudioEqPanel
    cfg = cfgmod.Config()
    for k, v in equalizer.reset_values().items():
        cfg[k] = v
    for k, v in (vals or {}).items():
        cfg[k] = equalizer.normalize(k, v)
    eng = FakeEngine(cfg, live=live)
    return AudioEqPanel(eng, cfg), eng


def panel_checks() -> None:
    fam = "面板"
    import app.ui.audio_eq_panel as AP
    ORDER = equalizer.ORDER

    # P0：假内核不许悄悄宽松
    try:
        drift_sig, drift_if = [], []
        for name in EQ_METHODS:
            real = getattr(MpvPlayer, name)
            fake = getattr(FakeEngine, name)
            if _sig(real) != _sig(fake):
                drift_sig.append(f"{name}: {list(_sig(real))} vs {list(_sig(fake))}")
            a, b = _if_tests(real), _if_tests(fake)
            if a != b:
                drift_if.append(f"{name}: 真引擎独有{sorted(a - b)} 假内核独有{sorted(b - a)}")
    except (OSError, TypeError) as exc:
        env_note("面板半", f"读不到引擎源码（{exc!r}）：同构闸门本轮没有测量对象")
        drift_sig = drift_if = ["读不到源码"]
    check("假内核与真引擎逐方法同构（签名 + `if` 条件集合走 AST 比）——"
          "假内核一旦悄悄比真引擎宽松，面板腿就会绿着骗人",
          not drift_sig and not drift_if, f"签名差={drift_sig[:2]} 条件差={drift_if[:2]}", fam)

    p, eng = make_panel()
    p.log: list[str] = []
    p.sig_status.connect(lambda t: p.log.append(str(t)))
    check("每一档都有滑轨、且没有表外的滑轨（十段＋前置＝11 根；表加了项界面漏了＝调不了）",
          sorted(p._rows) == sorted(ORDER) and len(p._rows) == len(equalizer.NUMS),
          f"滑轨={sorted(p._rows)}", fam)
    nofocus = [k for k, w in [("滑轨", r.slider) for k, r in p._rows.items()]
               + [("下拉", p.cb_preset), ("复位", p.btn_reset), ("滚动区", p.scroll)]
               if w.focusPolicy() != Qt.NoFocus]
    check("控件一律 NoFocus（方向键是窗口级快捷键，焦点落在滑轨上就是「按一下跳一格、片子也跳」）",
          not nofocus, f"有焦点={nofocus}", fam)
    check("数值胶囊写的是表的写法（0 dB / +3 dB / -3 dB），界面没抄第二份格式化",
          p._rows["eq_1k"].val.text() == "0 dB"
          and equalizer.value_text("eq_1k", 3) == "+3 dB",
          f"实际={p._rows['eq_1k'].val.text()!r}", fam)
    check(f"下拉是 {len(equalizer.PRESETS)} 条预设＋一条「自定义」（那条不代表一串值，"
          "只代表「不等于任何预设」）",
          [str(p.cb_preset.itemData(i)) for i in range(p.cb_preset.count())]
          == [x.key for x in equalizer.PRESETS] + [AP.CUSTOM],
          f"项={[(p.cb_preset.itemText(i), p.cb_preset.itemData(i)) for i in range(p.cb_preset.count())]}",
          fam)

    # 拖动途中 vs 松手：往返次数是这一家唯一硬证据
    a0, r0, f0, g0 = eng.attempts, eng.n_req, eng.n_fire, eng.n_get
    for v in (1, 2, 3, 4, 5):
        p._on_num_change("eq_1k", float(v), False)
    check("拖动途中（commit=False）零次同步往返、只发不等（否则每动一格等内核一次，手感一格一顿）",
          eng.n_req == r0 and eng.n_fire > f0 and eng.n_get == g0
          and eng.attempts - a0 == 5,
          f"同步={eng.n_req - r0} 只发={eng.n_fire - f0} 读={eng.n_get - g0}", fam)
    a1, r1, f1, g1 = eng.attempts, eng.n_req, eng.n_fire, eng.n_get
    p._on_num_change("eq_1k", 6.0, True)
    check("松手时发且只发一次同步确认，值真的进了链",
          eng.n_req - r1 == 1 and eng.n_get - g1 == 1 and eng.n_fire == f1
          and equalizer.gain_of(eng.cfg, "eq_1k") == 6.0
          and "g=6" in equalizer.build_chain(eng.cfg),
          f"同步={eng.n_req - r1} 只发={eng.n_fire - f1} 读={eng.n_get - g1} "
          f"cfg={equalizer.gain_of(eng.cfg, 'eq_1k')}", fam)
    check("滑轨画的是内核读回的那个数（不是「我发出去的那串」）",
          float(p._vals.get("eq_1k", 99.0)) == 6.0 and p._rows["eq_1k"].val.text() == "+6 dB",
          f"缓存={p._vals} 胶囊={p._rows['eq_1k'].val.text()!r}", fam)
    check("手动改一档之后下拉必须离开预设（继续指着「平坦」就是界面在撒自己刚做的那句谎）",
          str(p.cb_preset.currentData()) == AP.CUSTOM and "自定义" in p.lbl_preset.text(),
          f"下拉={p.cb_preset.currentData()!r} 说明={p.lbl_preset.text()!r}", fam)

    # 内核"挂的是另一个数"：界面必须弹回内核的那一档，并把两句话说全
    eng.distort = {1000.0: 2.0}
    del p.log[:]
    p._on_num_change("eq_1k", 6.0, True)
    check("内核挂的是别的数时：滑轨弹回内核那一档、句子同时写「内核是 +2 dB，你要的是 +6 dB」"
          "（只念一遍 `shown` 的那版会把自己刚发的值说成被否的那个）",
          p._rows["eq_1k"].val.text() == "+2 dB" and "内核是 +2 dB" in last_text(p.log)
          and "你要的是 +6 dB" in last_text(p.log),
          f"胶囊={p._rows['eq_1k'].val.text()!r} 句子={p.log[-1:]!r}", fam)
    eng.distort = {}

    # 表外项一律不发命令（这一族坏值的下场是没声音）
    a2, r2, f2 = eng.attempts, eng.n_req, eng.n_fire
    p._on_num_change("eq_bass_boost", 3.0, True)
    p.bump("eq_not_in_table", 1.0)
    check("表外档位一条命令都不发、并留一句（发出去就是「没人核对过的值进 af 链」）",
          eng.attempts == a2 and eng.n_req == r2 and eng.n_fire == f2
          and len(p.log) >= 2 and all("不在均衡器表里" in s for s in p.log[-2:]),
          f"尝试={eng.attempts - a2} 同步={eng.n_req - r2} 只发={eng.n_fire - f2} 句子={p.log[-2:]}",
          fam)

    # 预设整档一次发完
    p.reset_all()
    r3, f3 = eng.n_req, eng.n_fire
    idx = idx_of(range(p.cb_preset.count()), lambda i, c=p.cb_preset: c.itemData(i) == "cinema")
    # 找不到那一行就**不点**（`_on_preset(-1)` 会拿 None 去查 `PRESET_BY_KEY` 直接 KeyError）：
    # 下面那两条 check 因此自然变红，而不是让整块面板一家崩掉——判据要报红、不报崩。
    if idx >= 0:
        p._on_preset(idx)
    want = equalizer.preset_values("cinema")
    check("选一条预设＝一次整链替换（不是十档十次往返：那会把中间态放给用户听）",
          idx >= 0 and eng.n_req - r3 == 1 and eng.n_fire == f3
          and equalizer.gains(eng.cfg) == want,
          f"找到行={idx} 往返={eng.n_req - r3} 只发={eng.n_fire - f3} "
          f"配置={equalizer.gains(eng.cfg)} 期望={want}", fam)
    check("预设下去之后十一根滑轨一起回到那一条预设的数（从内核读回那份画）、下拉指着那一条",
          all(p._rows[k].val.text() == equalizer.value_text(k, v) for k, v in want.items())
          and str(p.cb_preset.currentData()) == "cinema",
          f"胶囊={[(k, p._rows[k].val.text()) for k in list(want)[:3]]} "
          f"下拉={p.cb_preset.currentData()!r}", fam)
    r4 = eng.n_req
    cidx = idx_of(range(p.cb_preset.count()),
                  lambda i, c=p.cb_preset: c.itemData(i) == AP.CUSTOM)
    if cidx >= 0:
        p._on_preset(cidx)
    check("点到「自定义」那一条不算一次动作（不发命令、不改值）",
          cidx >= 0 and eng.n_req == r4 and equalizer.gains(eng.cfg) == want,
          f"找到行={cidx} 额外往返={eng.n_req - r4}", fam)

    # 状态行四档措辞
    r5 = eng.n_req
    p.reset_all()
    check("归零后状态行说「不挂滤镜、声音就是原片」（而不是「已启用」这种假话），"
          "且走的是整链一次替换",
          "原片" in p.lbl_chain.text() and equalizer.build_chain(eng.cfg) == ""
          and eng.n_req - r5 == 1,
          f"状态行={p.lbl_chain.text()!r} 往返={eng.n_req - r5}", fam)
    # 失败行由**命令带出来**（`pending_log`），不是提前塞进 `log_lines`：真引擎先记日志偏移
    # 再发命令，提前塞的那一行会被偏移挡在窗口外，判据于是永远读不到坏消息。
    eng.pending_log = ["[   0.401][e][cplayer] Audio filter initialized failed!"]
    eng.apply_audio_eq_chain()
    p._sync_from_cache()
    p._paint_chain()
    check("内核把链整条扔掉时状态行必须换成「会没声音」那一档，并把内核原话带出来"
          "（同一条文字不许既说生效又说失败）",
          "没把链挂起来" in p.lbl_chain.text() and "Audio filter initialized failed"
          in p.lbl_chain.text(), f"状态行={p.lbl_chain.text()!r}", fam)
    p.page_shown()
    check("链没成立之后**开页纯读不许把红字洗成绿色**（没有日志窗口就没有新证据，"
          "沿用上一次下发的判决；实测列表在整条起不来时照样好看）",
          "没把链挂起来" in p.lbl_chain.text(), f"状态行={p.lbl_chain.text()!r}", fam)
    eng.log_lines = []
    eng.apply_audio_eq_chain()
    p._sync_from_cache()
    p._paint_chain()
    check("下一次下发带着日志窗口重走一趟，坏消息才允许被清掉（不是「读一次就好了」）",
          "没把链挂起来" not in p.lbl_chain.text(), f"状态行={p.lbl_chain.text()!r}", fam)
    eng._eq_problems = ["volume 条目不带 dB 后缀（内核按线性算）：volume=-12"]
    p._sync_from_cache()
    p._paint_chain()
    check("读回里有读不懂的条目时单独一档措辞（界面画的不是内核挂的，这比「失败」更隐蔽）",
          "读不懂" in p.lbl_chain.text(), f"状态行={p.lbl_chain.text()!r}", fam)
    eng._eq_problems = []
    p._on_num_change("eq_1k", 12.0, True)
    check("净抬升越过口味线时状态行提醒（⑦ 实测那条线：十段全 +12 不带前置时 clip=657）",
          "净抬升" in p.lbl_chain.text() and "削顶" in p.lbl_chain.text(),
          f"状态行={p.lbl_chain.text()!r}", fam)

    # 内核没起来那条路
    p2, eng2 = make_panel(live=False)
    p2.log: list[str] = []
    p2.sig_status.connect(lambda t: p2.log.append(str(t)))
    a3 = eng2.attempts
    g3 = eng2.tries_get
    eng2.log_lines.clear()
    said2: list[str] = []
    eng2.sig_log.connect(lambda t: said2.append(str(t)))
    p2._on_num_change("eq_62", 5.0, True)
    check("内核没起来时动滑轨：只记设置、一句「已记入设置」，状态行不是「已生效」也不是假警报"
          "（内核三条计数全 0：没同步、没只发落地、也**一次没去问 `af`**——问了就得到一句"
          "没人应答的「读不到 af」，那是假警报的产地）",
          equalizer.gain_of(eng2.cfg, "eq_62") == 5.0 and not said2 and eng2.n_req == 0
          and eng2.n_fire == 0 and eng2.n_get == 0 and eng2.tries_get == g3
          and "已记入设置" in last_text(p2.log) and "内核没在放" in p2.lbl_chain.text(),
          f"同步={eng2.n_req} 只发={eng2.n_fire} 收读={eng2.n_get} 问读={eng2.tries_get - g3} "
          f"引擎日志={said2[:1]} 句子={p2.log[-1:]!r} 状态行={p2.lbl_chain.text()!r}", fam)
    idx_bass = idx_of(range(p2.cb_preset.count()),
                      lambda i, c=p2.cb_preset: c.itemData(i) == "bass")
    if idx_bass >= 0:
        p2._on_preset(idx_bass)
    check("内核没起来时选预设同样只写配置：整档进 cfg、零次同步往返、一句「已记入设置」"
          "（`_write_preset_quietly` 不许变成「发一发再报错」）",
          idx_bass >= 0 and equalizer.gains(eng2.cfg) == (equalizer.preset_values("bass") or {})
          and eng2.n_req == 0 and not said2 and "已记入设置" in last_text(p2.log),
          f"找到行={idx_bass} 配置={equalizer.gains(eng2.cfg)} 同步={eng2.n_req} "
          f"引擎日志={said2[:1]} 句子={p2.log[-1:]!r}", fam)
    p2.reset_all()
    check("内核没起来时一键复位：不发链、不问 `af`（那会换来一串「读不到 af」把 OSD 刷满）",
          equalizer.build_chain(eng2.cfg) == "" and eng2.n_req == 0 and not said2
          and "已记入设置" in last_text(p2.log),
          f"链={equalizer.build_chain(eng2.cfg)!r} 同步={eng2.n_req} 引擎日志={said2[:1]}", fam)
    a4 = eng2.attempts
    p2.page_shown()
    check("内核没起来时开页一问都不问（`get af` 无人应答，问就是给自己造一句「读不到 af」）",
          eng2.attempts == a4, f"额外尝试={eng2.attempts - a4}", fam)
    # 顺序判据：`_paint_chain` 第一条是 live 闸门。上一会话的红字是真的，但它说的是**那一次**，
    # 内核停了以后再说"会没声音"就是把一次正常的空档报成事故（产品里那条重排的整条理由）。
    p4, eng4 = make_panel(live=True)
    p4.log: list[str] = []
    p4.sig_status.connect(lambda t: p4.log.append(str(t)))
    eng4.pending_log = ["[   0.401][e][cplayer] Audio filter initialized failed!"]
    eng4.apply_audio_eq_chain()
    p4._sync_from_cache()
    p4._paint_chain()
    eng4.running = False
    p4._on_num_change("eq_62", 4.0, True)
    check("上一次会话的红字不许盖过「现在没在放」：内核停了之后动滑轨，状态行说的是取值已记入设置，"
          "而不是把旧会话那句坏消息再报一遍（面板手里那份红字仍然留着，等内核回来再表态）",
          bool(p4._fails) and "内核没在放" in p4.lbl_chain.text()
          and "没把链挂起来" not in p4.lbl_chain.text(),
          f"状态行={p4.lbl_chain.text()!r} 旧红字={p4._fails[:1]}", fam)
    p4.deleteLater()
    a5 = eng2.attempts
    eng2.running = True
    eng2._af = af_entries(equalizer.build_chain(eng2.cfg))
    del said2[:]
    p2._on_engine_log("[   0.401][e][cplayer] Audio filter initialized failed!")
    check("启动期那条链没挂上时，面板从日志认出来并把原话落在状态行"
          "（`--af` 是启动参数，没有任何一次动作会走下发路径）",
          "Audio filter initialized failed" in p2.lbl_chain.text()
          and "没把链挂起来" in p2.lbl_chain.text() and not said2 and eng2.attempts == a5,
          f"状态行={p2.lbl_chain.text()!r} 引擎日志={said2[:1]} 尝试={eng2.attempts - a5}", fam)
    before = (p2.lbl_chain.text(), list(p2._fails))
    p2._on_engine_log("Nothing bad here.")
    check("正常日志一行既不追加失败项、也不改状态行（判据用表里的前缀，不是「看见日志就红」；"
          "而「不改状态行」在这里的意思是：上一句真警告原样留着）",
          p2.lbl_chain.text() == before[0] and p2._fails == before[1],
          f"改后={p2.lbl_chain.text()!r} 失败列表={p2._fails[:2]}", fam)
    p3, eng3 = make_panel(live=True)
    eng3.log_lines.clear()
    p3.reset_all()
    before3 = p3.lbl_chain.text()
    p3._on_engine_log("[   0.403][v][af]   lavfi-bass (lavfi-bass.00)")
    check("干净日志不把中性状态行涂成警告（对照组：同一块面板、同一句入口，只是日志正常）",
          p3.lbl_chain.text() == before3 and not p3._fails,
          f"改后={p3.lbl_chain.text()!r} 失败列表={p3._fails[:2]}", fam)
    p3.deleteLater()
    g6 = eng.n_get
    a6 = eng.attempts
    p.page_shown()
    check("内核在时开页恰好问一次 `af`、且一条命令都不发"
          "（这一族没有订阅，不问就永远拿不到真值；问十次就是白烧 IPC）",
          eng.n_get - g6 == 1 and eng.attempts - a6 == 1,
          f"读={eng.n_get - g6} 尝试={eng.attempts - a6}", fam)
    p.bump("eq_8k", 3.0)
    check("键盘入口（bump）与拖滑轨走同一个出口：配置、胶囊、下拉三处一起动",
          equalizer.gain_of(eng.cfg, "eq_8k") == 3.0 and p._rows["eq_8k"].val.text() == "+3 dB"
          and str(p.cb_preset.currentData()) == AP.CUSTOM,
          f"cfg={equalizer.gain_of(eng.cfg, 'eq_8k')} 胶囊={p._rows['eq_8k'].val.text()!r} "
          f"下拉={p.cb_preset.currentData()!r}", fam)
    p.deleteLater()
    p2.deleteLater()


# ==================== 真机腿 ====================

def kernel_leg(cfg) -> None:
    """本机 mpv + 这台机器的真实输出通路：`--af` 落地、热切读回、归零清空、坏值有原话。"""
    fam = "真机"
    from PyQt5.QtWidgets import QWidget
    if not os.path.exists(SAMPLE):
        env_note("真机半", f"缺样片 {SAMPLE}")
        return
    old_eq = {k: cfg.get(k) for k in equalizer.ORDER}
    old_vol = cfg.get("volume")
    for k, v in equalizer.reset_values().items():
        cfg[k] = v
    for k, v in (equalizer.preset_values("cinema") or {}).items():
        cfg[k] = v
    cfg["volume"] = 5                 # 这台机器上跑的就是真实输出通路：声音压到最低
    cfg["resume"] = False
    cfg.save()
    host = QWidget(None)
    host.setWindowFlags(Qt.Window)
    host.resize(HOST_W, HOST_H)
    host.show()
    pump(0.5)
    hwnd = int(host.winId())
    want = equalizer.preset_values("cinema") or {}
    p = MpvPlayer(cfg)
    try:
        built = p.build_args(hwnd)
        chain0 = equalizer.build_chain(cfg)
        off0 = p._log_offset()
        if not p.start(hwnd=hwnd, wait=14.0):
            env_note("真机半", f"内核没起来（{cfgmod.MPV_EXE}）：这一族本轮只拿到前三家的结论")
            return
        pump(1.0)
        check("内核版本与这张表的实测依据一致：换了 mpv 就得重跑 tests/probe_eq.py，"
              "否则「实测」是别人机器上的数",
              equalizer.MPV_MEASURED_ON in str(p.version),
              f"本机={p.version!r} 表依据={equalizer.MPV_MEASURED_ON}", fam)
        p.open_files([SAMPLE], remember=False)
        pump(2.5)
        _c, vals, problems, fails = p.read_audio_eq()
        diff = [f"{k} 期望 {want[k]:+g} 内核 {float(vals.get(k, 0.0)):+g}" for k in equalizer.ORDER
                if abs(float(vals.get(k, 0.0)) - want.get(k, 0.0)) > 1e-6]
        check("启动参数落地：`--af=` 那 N 条目在 loadfile 之后就挂在内核上，"
              "读回反解的逐档数与请求逐档相同（不等界面补发）",
              any(a == f"--af={chain0}" for a in built) and not diff
              and not problems and not fails,
              f"参数={[a for a in built if a.startswith('--af=')][:1]} 不一致={diff[:3]} "
              f"原话={problems[:2]} 失败={fails[:2]}", fam)
        t0 = p.get("time-pos", None)
        pump(2.0)
        t1 = p.get("time-pos", None)
        try:
            moved = (float(t1 or 0) - float(t0 or 0)) > 0.5
        except (TypeError, ValueError):
            moved = False
        whole = p._log_since(off0)
        check("真实输出通路在放：time-pos 往前走、整段日志里没有这一族的失败原话——"
              "这两句一起才敢说「链挂在真声音上」（`ao=pcm` 那类文件输出证的不是这条）",
              moved and not equalizer.failures(whole),
              f"time {t0}→{t1} 日志坏行={equalizer.failures(whole)[:2]}", fam)
        ac = p.get("audio-params", None) or p.get("audio-codec-name", None)
        check("这一路确实有音频流在管线里（audio-params/audio-codec-name 有东西，"
              "而不是「整片没声音所以链也没挂」）", bool(ac), f"音频参数={ac}", fam)

        # 音量普查：内核自己会不会往 `af` 里塞一条（我们的反解器对它是瞎的）
        p.set_prop("volume", 60)
        pump(0.6)
        raw60 = p.get("af", None)
        _c, v60, pr60, f60 = p.read_audio_eq()
        check("音量不是 100 时 `af` 读回里不许多出这张表读不懂的条目（真出现就得把那条原话"
              "收进解析或白名单，而不是让用户看见一句假警告）",
              not pr60, f"volume=60 的回报={str(raw60)[:120]} 原话={pr60[:2]}", fam)
        p.set_prop("volume", 5)
        pump(0.4)

        # 热切（④a 那条结论：播放中段换档听得见）
        off1 = p._log_offset()
        got, ok = p.set_audio_eq("eq_1k", 9.0)
        pump(0.5)
        bad1 = equalizer.failures(p._log_since(off1))
        check("播放中热切一档：返回内核读回的那个数、判一致，且紧邻那段日志没有失败原话"
              "（④a 实测切点前 +0.2 dB、切后 +7.7 dB，所以「能切」这件事本身是量过的）",
              ok and abs(float(got) - 9.0) < 1e-6 and not bad1,
              f"回报={got} ok={ok} 日志={bad1[:2]}", fam)
        _c, vals2, problems2, fails2 = p.read_audio_eq()
        check("热切之后整条链还完整（改一档＝整链重发，其余档不许被丢掉）",
              all(abs(float(vals2.get(k, 0.0)) - want.get(k, 0.0)) < 1e-6
                  for k in equalizer.ORDER if k != "eq_1k")
              and abs(float(vals2.get("eq_1k", 0)) - 9.0) < 1e-6
              and not problems2 and not fails2,
              f"读回={vals2} 原话={problems2[:2]} 失败={fails2[:2]}", fam)

        # 坏值绕过外壳直接喂内核：证明"日志是唯一防线"这件事在产品路径上成立
        off2 = p._log_offset()
        p._req(["change-list", "af", "set", "lavfi=[equalizer=f=1000:t=h:w=450:g=9999]"],
               timeout=3.0)
        pump(0.8)
        _c, _v2, _pr2, bad_log = p.read_audio_eq(off2)
        listed = p.get("af", []) or []
        check("越界增益绕过外壳直接喂内核：日志必须留下原话（这就是「外壳夹紧是承重件」的证据，"
              "也证明判据认得内核的真说法），而列表照样可能好看——只信列表就会报一句假平安",
              bool(bad_log), f"日志={bad_log[:2]} 列表={str(listed)[:100]}", fam)
        _c, _v3, _pr3, still = p.read_audio_eq()
        check("坏消息在**没有日志窗口的纯读**里留着（沿用上一次下发的判决）：「这一次没读到坏行」"
              "不等于「链好了」", bool(still), f"纯读带回来的失败行={still[:2]}", fam)
        off3 = p._log_offset()
        p._req(["change-list", "af", "set", "lavfi=[nonexist=1]"], timeout=3.0)
        pump(0.8)
        bad3 = equalizer.failures(p._log_since(off3))
        check("不存在的滤镜名同样在原话里（这一族的坏值不是「这一档没生效」，是整条起不来）",
              bool(bad3), f"日志={bad3[:2]}", fam)
        _c, vals3, pr3, f3 = p.apply_audio_eq_chain()
        pump(0.4)
        check("坏值之后走表的出口重发一次就能恢复、且那次下发把旧的坏消息清掉"
              "（表里那串永远可收：这就是外壳夹紧的意义）",
              not f3 and not pr3 and abs(float(vals3.get("eq_1k", 0)) - 9.0) < 1e-6,
              f"读回={vals3} 原话={pr3[:2]} 失败={f3[:2]}", fam)

        # 面板接真引擎
        from app.ui.audio_eq_panel import AudioEqPanel
        panel = AudioEqPanel(p, cfg)
        panel.page_shown()
        pump(0.3)
        check("面板开页读的是内核真值（eq_1k 那根停在热切后的 9，而不是配置里预设的 3）",
              panel._rows["eq_1k"].val.text() == "+9 dB",
              f"胶囊={panel._rows['eq_1k'].val.text()!r} 内核={vals3.get('eq_1k')}", fam)
        panel._on_num_change("eq_250", -4.0, True)
        pump(0.5)
        _c, vals4, pr4, f4 = p.read_audio_eq()
        check("面板拖一根→内核真挂了那一档（读回反解相等、日志干净）",
              abs(float(vals4.get("eq_250", 0)) + 4.0) < 1e-6 and not pr4 and not f4,
              f"读回={vals4} 原话={pr4[:2]} 失败={f4[:2]}", fam)
        panel.reset_all()
        pump(0.5)
        check("面板一键复位＝内核链清空（`set \"\"` → 读回 `[]`，实测只有空串是干净清空）",
              (p.get("af", []) or []) == [] and panel._vals == {}
              and "原片" in panel.lbl_chain.text(),
              f"af={p.get('af')} 状态行={panel.lbl_chain.text()!r}", fam)
        panel.deleteLater()
    finally:
        for k, v in old_eq.items():
            if v is not None:
                cfg[k] = v
        cfg["volume"] = 100 if old_vol is None else old_vol
        cfg.save()
        try:
            p.quit()
        except Exception:
            pass
        pump(0.5)
        host.close()
        pump(0.3)


# ==================== 台账 ====================

def report(title: str = "声音均衡器") -> int:
    fails = [n for n, ok, _d, _f in results if not ok]
    total = len(results)
    by_fam: dict[str, int] = {}
    for _n, _ok, _d, f in results:
        by_fam[f] = by_fam.get(f, 0) + 1
    print(f"\n==== {title}：{total - len(fails)}/{total} 通过，失败 {len(fails)} 条，"
          f"判废 {len(env_notes)} 处 ====", flush=True)
    print("     分家计数（条数由本脚本现数，不是手抄）：" +
          "、".join(f"{k} {v}" for k, v in sorted(by_fam.items())), flush=True)
    for n, ok, d, _f in results:
        if not ok:
            print(f"  FAIL {n}\n       {d}", flush=True)
    if env_notes:
        print(f"~~ 本轮不作数 {len(env_notes)} 处（判据的前提没拿到）：", flush=True)
        for n in env_notes:
            print("   ", n, flush=True)
    rc = final_exit_code(fails, env_notes)
    if env_notes and fails:
        print("   判废不许掩盖失败：本轮另有真实失败 ⇒ rc=1。", flush=True)
    elif env_notes:
        print("   这些既不算通过也不算产品失败：修好环境（内核起得来、有声轨的样片在）重跑。",
              flush=True)
    return rc


# ==================== 证伪轮 ====================

def _fx(label: str, hit: bool, detail: str = "") -> None:
    falsified.append((label, bool(hit), detail))
    print(f"[{'CAUGHT' if hit else 'MISSED'}] {label}" + (f"  —— {detail}" if detail else ""),
          flush=True)


def _rerun() -> list[str]:
    """把三家离线判据在**当前**（可能被摘掉承重件的）代码上重跑一遍，带回点名的失败项。

    崩溃单独记一档并且**不吞掉已经跑出来的失败**："坏实现把测量器弄崩了"和"判据认出了坏实现"
    是两件事，但崩之前那几条红是实的。
    """
    global results, env_notes, QUIET
    saved = (results, env_notes, QUIET)
    results, env_notes, QUIET = [], [], True
    try:
        table_checks()
        engine_offline_checks()
        panel_checks()
        found = [n for n, ok, _d, _f in results if not ok]
    except BaseException as exc:
        found = [n for n, ok, _d, _f in results if not ok]
        found.append(f"离线三家在坏实现下自己崩了（这不算点名，要查）："
                     f"{type(exc).__name__}: {exc}")
    finally:
        results, env_notes, QUIET = saved
    return found


def _mutate(obj: Any, name: str, value: Any, keyword: str, label: str) -> None:
    """内存级摘件：坏实现活在内存里就够，**不动磁盘**（这个仓库没有版本控制）。"""
    old = getattr(obj, name, None)
    try:
        setattr(obj, name, value)
        fails = _rerun()
    finally:
        if old is None:
            try:
                delattr(obj, name)
            except AttributeError:
                pass
        else:
            setattr(obj, name, old)
    restored = getattr(obj, name, None) is old
    hit = [f for f in fails if keyword in f]
    _fx(f"摘掉承重件「{label}」⇒ 必须点名「{keyword}」", bool(hit) and restored,
        (f"点名={hit[:1] or '一条没点名'} 其它红={len(fails)} 还原={restored}"
         if not hit or not restored else f"点名={hit[0][:34]}"))


def _bad_normalize(key: str, v: Any) -> float:
    """坏实现：不夹，原样放行（内核范围 ±900 之外照样进链）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if x == x else 0.0


def _bad_normalize_nan(key: str, v: Any) -> float:
    """坏实现：NaN 交给 min/max 去夹（实测留下量程上界 ⇒「读不懂」变成「满增益 +12」）。"""
    s = equalizer.spec_for(key)
    try:
        x = float(v)
    except (TypeError, ValueError):
        return float(s.default)
    return max(s.lo, min(s.hi, x))


def _lenient_parse(af):
    """坏实现：`volume=-12` 也按 dB 解释（实测那是线性乘 12，界面画 −12 dB、耳朵听着 +17.9）。"""
    out, problems = _EQ_PARSE_ENTRIES(af)
    if isinstance(af, list):
        for e in af:
            g = str((e.get("params") or {}).get("graph", "")) if isinstance(e, dict) else ""
            if g.startswith("volume=") and not g.lower().endswith("dB"):
                try:
                    out = dict(out or {})
                    out[equalizer.PREAMP.cfg] = float(g.split("=", 1)[1])
                except (TypeError, ValueError):
                    pass
                problems = [x for x in problems if "不带 dB" not in x]
    return out, problems


def _silent_parse(af):
    """坏实现：认不出来的条目静默丢掉（界面看着满、内核挂的是别的）。"""
    out, _problems = _EQ_PARSE_ENTRIES(af)
    return (out if out is not None else {}), []


def _no_none_parse(af):
    """坏实现：空链与读不懂混成一档（整条读不懂也说「那是没挂滤镜」）。"""
    out, problems = _EQ_PARSE_ENTRIES(af)
    return ({} if out is None else out), problems


def _loud_preset(p):
    from dataclasses import replace
    return replace(p, vals={**p.vals, equalizer.PREAMP.cfg: 6.0})


def _drag_confirms(self, cfg_key, value, commit):
    """坏实现：拖动途中也 confirm（每一格都等内核一次）。"""
    spec = equalizer.spec_for(cfg_key)
    if spec is None:
        return
    got, ok = self.p.set_audio_eq(cfg_key, value, confirm=True)
    if commit:
        row = self._rows.get(cfg_key)
        if row is not None:
            row.show(got)
        self._sync_from_cache()
        self._paint_preset()
        self._paint_chain()


def _no_table_guard(self, cfg_key, value, commit):
    """坏实现：**只**摘掉表外守卫那三行，其余与产品逐句一致。

    早先这一版顺手把 `sig_status.emit` 也丢了，于是下一条判据读 `p.log[-1]` 先 IndexError，
    崩在目标判据**之前**——注入被记成"量具自己崩了"而不是"判据点名"（本机实测：那一条 MISSED
    就是这么来的）。摘件要摘得准：**多摘一项，测的就不是那一项了**。
    """
    from app.ui.audio_eq_panel import _f       # 连"脏值收口"都照产品用同一个，别在坏实现里换掉
    live = self.p.running
    got, ok = self.p.set_audio_eq(cfg_key, value, confirm=commit and live)
    if not commit:
        return
    row = self._rows.get(cfg_key)
    if row is not None and not row.slider.isSliderDown():
        row.show(got)
    self._sync_from_cache()
    self._paint_preset()
    self.sig_status.emit(self._status(cfg_key, _f(got), _f(value), ok, live))
    self._paint_chain()


def _paint_chain_old(self):
    """坏实现：live 闸门挪回最后（内核没在家时把旧会话的话、或一句「没人应答」报成链坏了）。"""
    live = self.p.running
    parts = self._active_parts()
    if self._fails:
        self._paint_warn(self._fails, "内核没把链挂起来（会没声音）：")
    elif self._problems:
        self._paint_warn(self._problems, "内核的链有读不懂的条目：")
    elif not live:
        self.lbl_chain.setText("内核没在放：这些取值已记入设置，播放时一次带上。")
    elif not parts:
        self.lbl_chain.setText("均衡器未启用（不挂滤镜，声音就是原片）")
    else:
        self.lbl_chain.setText(f"均衡器：{len(parts)} 条")


def falsify() -> int:
    import app.engine.equalizer as EQ
    import app.ui.audio_eq_panel as AP

    _app() or None
    if "--no-kernel" in sys.argv[1:]:
        env_note("证伪轮", "真机腿的注入本来就不在这一轮（这一轮摘的是三家离线判据的承重件）")

    # —— 三家离线判据在完好代码上必须先全绿，否则下面的"翻红"没有对照 ——
    table_checks()
    engine_offline_checks()
    panel_checks()
    green = [n for n, ok, _d, _f in results if not ok]
    check("证伪轮的开始条件：完好代码上三家离线判据一条不红（红着谈「抓到坏样本」是空的）",
          not green, f"起手就红={green[:3]}", "证伪")

    _mutate(EQ, "normalize", _bad_normalize, "夹回量程", "夹紧（normalize 原样放行）")
    _mutate(EQ, "normalize", _bad_normalize_nan, "非有限", "NaN 被夹成满增益（默认档失守）")
    _mutate(EQ, "preamp_graph", lambda v: ("" if equalizer.PREAMP.clamp(v) == 0
                                           else "volume=" + equalizer._num(
                                               equalizer.PREAMP.clamp(v))),
            "前置永远拼成", "前置的 dB 后缀（拼成线性写法）")
    _mutate(EQ, "chain_parts",
            lambda cfg: (lambda ps: (ps[-1:] + ps[:-1] if ps and
                                     ps[-1].startswith("volume=") else ps))(_EQ_CHAIN_PARTS(cfg)),
            "链顺序", "链顺序（前置挪到最前）")
    _mutate(EQ, "build_chain",
            lambda cfg: (lambda ps: f"lavfi=[{','.join(ps)}]" if ps else "")(_EQ_CHAIN_PARTS(cfg)),
            "链形状合规", "每条一个滤镜（改回一条带逗号的括号图）")
    _mutate(EQ, "build_chain", lambda cfg: _EQ_BUILD_CHAIN(cfg) or "lavfi=[]",
            "全零＝空串", "空串换成空图 lavfi=[]")
    _mutate(EQ, "startup_args",
            lambda cfg: [f"--af={_EQ_BUILD_CHAIN(cfg) or 'lavfi=[]'}"],
            "启动参数与链同步", "链为空也发 --af")
    _mutate(EQ, "parse_entries", _lenient_parse, "绝不按 dB 解释",
            "反解把 volume=-12 当 dB 解释")
    _mutate(EQ, "parse_entries", _silent_parse, "各有原话", "反解静默丢掉认不出的条目")
    _mutate(EQ, "parse_entries", _no_none_parse, "分得开", "空链与读不懂混成一档")
    last = equalizer.PRESETS[-1]
    _mutate(EQ, "PRESETS", tuple(list(equalizer.PRESETS[:-1]) + [_loud_preset(last)]),
            "每条预设都在量程内", "把最后一条预设的前置抬到 +6（越过实测那条线）")
    _mutate(EQ, "auto_preamp",
            lambda vals: max(equalizer.PRE_LO, equalizer.CLIP_NET_MAX
                             - max([v for k, v in vals.items()
                                    if k != equalizer.PREAMP.cfg] + [0.0])),
            "且不往上走", "前置允许往上抬（口味线只管往下让这件事丢了）")
    _mutate(EQ, "EQ_FAIL_MARKS",
            tuple(m for m in equalizer.EQ_FAIL_MARKS if "initialized" not in m),
            "条真原话逐行认出来", "失败前缀表里漏掉「链路没成立」那句")
    _mutate(EQ, "value_text",
            lambda key, v: (lambda s: str(v) if s is None else
                            f"{s.clamp(v):+.0f} dB")(equalizer.spec_for(key)),
            "值写法三种形状", "0 也写成 +0 dB")
    _mutate(EQ, "label", lambda key: str(key), "标签从表取", "界面拿配置键当标签")

    _mutate(AP.AudioEqPanel, "_sync_from_cache", lambda self: None,
            "内核读回的那个数", "面板不收读回缓存（滑轨停在面板自己发的那串）")
    _mutate(AP.AudioEqPanel, "_paint_preset", lambda self: None,
            "下拉必须离开预设", "面板不重算下拉")
    _mutate(AP.AudioEqPanel, "_on_num_change", _drag_confirms,
            "拖动途中（commit=False）零次同步往返", "拖动途中也 confirm（每格等内核一次）")
    _mutate(AP.AudioEqPanel, "_on_num_change", _no_table_guard,
            "表外档位一条命令都不发", "表外项照发（没人核对过的值进链）")
    _mutate(AP.AudioEqPanel, "_paint_chain", _paint_chain_old,
            "上一次会话的红字不许盖过", "live 闸门挪回最后（空档报成链坏了）")
    # 假内核自己也要被摘一次：同构闸门必须认得出"fixture 悄悄宽松"
    _mutate(FakeEngine, "set_audio_eq", _loose_fake_set,
            "假内核与真引擎逐方法同构", "假内核去掉 not running 早退（fixture 变宽松）")
    # 真引擎的两条闸门：把 running 摘成恒真 ⇒ 内核没在家时也会去问 af、也会发假警报
    _mutate(MpvPlayer, "running", property(lambda self: True),
            "内核没在家时的「松手确认」", "引擎的 not-running 早退（把没人在家当有人应答）")

    missed = [n for n, hit, _d in falsified if not hit]
    print("\n==== 证伪轮（三家离线判据的注入）====", flush=True)
    print(f"注入 {len(falsified)} 处，抓到 {len(falsified) - len(missed)} 处，"
          f"漏网 {len(missed)} 处", flush=True)
    for n in missed:
        print(f"  MISSED {n}", flush=True)
    fails = [n for n, ok, _d, _f in results if not ok]
    rc = final_exit_code(fails + missed, env_notes)
    if rc == 0:
        print("对照：完好代码上那批判据是绿的（起手检查已跑过），摘件之后每条都点名——"
              "这两句一起才敢说判据有牙。", flush=True)
    return rc


def _loose_fake_set(self, key, value, persist=True, confirm=True):
    """坏假内核：没有 `not running` 那一步（其余照抄）。"""
    spec = equalizer.spec_for(key)
    if spec is None:
        self.sig_log.emit(f"均衡器档位不在表里：{key}")
        return 0.0, False
    want = equalizer.normalize(key, value)
    if persist:
        self.cfg[key] = want
    if not confirm:
        self._fire(["change-list", "af", "set", equalizer.build_chain(self.cfg)])
        return want, True
    _chain, vals, problems, fails = self.apply_audio_eq_chain()
    got = float(vals.get(key, 0.0) or 0.0)
    ok = (not fails) and (not problems) and abs(got - want) < 1e-4
    if not ok:
        self.sig_log.emit(f"{equalizer.label(key)} 没确认生效：{problems[:1]}")
    return (got if not fails else want), ok


# ==================== 入口 ====================

def main() -> int:
    missing = []
    if not os.path.exists(cfgmod.MPV_EXE):
        missing.append(f"缺播放内核：{cfgmod.MPV_EXE}")
    if not os.path.exists(SAMPLE):
        missing.append(f"缺样片：{SAMPLE}")
    global _APP_REF                 # 见下方 `_APP_REF` 那条注释：这一句必须**绑住**，不许写成裸表达式
    from PyQt5.QtWidgets import QApplication
    _APP_REF = QApplication.instance() or QApplication(sys.argv[:1])
    if "--falsify" in sys.argv[1:]:
        return falsify()
    if missing and "--no-kernel" not in sys.argv[1:]:
        print("连测量对象都没有：" + "；".join(missing) + "\n⇒ 本轮不判产品好坏，rc=2", flush=True)
        return 2

    cfg = cfgmod.Config()
    table_checks()
    engine_offline_checks()
    panel_checks()
    if "--no-kernel" in sys.argv[1:]:
        env_note("真机半", "点名 --no-kernel：启动参数落地、热切、坏值原话、音量普查这几条本轮没量")
    else:
        kernel_leg(cfg)
    return report()


if __name__ == "__main__":
    sys.exit(main())
