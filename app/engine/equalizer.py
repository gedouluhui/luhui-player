"""声音均衡器（十段 + 前置增益）的单一事实表：af 链的下发形状全部以内核实测为准。

为什么不塞进 `transform.py`：那张表管的是"画面"，链是 `vf`、越界的下场是"画面不动"；
这一族是 `af`，**坏值的下场是整条链路起不来、一声不出**（实测 `bass=g=9999`、
不存在的滤镜名、以及空图 `lavfi=[]` 三档，产物字节数都是 `-1`：内核原话
`Audio filter initialized failed!` ⇒ `No video or audio streams selected.`）。
也就是说这张表的夹紧比画面那一族更是承重件：写歪一个数不是"这一档没生效"，
而是"整个播放器没声音"。

**全部取值以本机这版内核实测为准**（mpv v0.41.0-1101-g5d85ba5fb，FFmpeg N-127213；
测法＝把 8 s / 60 s / 90 s 粉噪经 `--ao=pcm` 落成 WAV，用同一把 FFT 带尺逐带比 dB，
并用 `--force-window=yes` 保留视频轨把管线拖到 ≈1.00× 实时以便在播放中段发 `change-list af`）。
证据：`tests/out/eq-probe-r15b.log`（下面按腿编号引用），r15 是同一趟的上一轮（形状普查那几腿相同），
一次性量具 `tests/probe_eq_volume.py`（只问 volume 的几串写法；2026-10-12 转正留档，
因为第 6 条那三个数引的就是它）→ `tests/out/eq-vol-r12.log`。

1. **`af` 的六种写法（① 那一腿，同一条 bass+12@110）**：
   `lavfi=[图]` / `lavfi=graph="图"` / 裸 `bass=…`（内核自动试桥）/ `lavfi-bass=…`（强制前缀）/
   `lavfi="图"` 五种都出真声音（逐带 +11.7/+7.1，与 ffmpeg 侧一致）；
   **`lavfi=name=k:v` 这一种是启动致命**——内核回
   `Error parsing option bass (option not found)` + `Option af: Error while parsing lavfi parameter bass (g)`，
   然后 `Setting commandline option --af=… failed.`，进程直接起不来。
   本表只发 `lavfi=[…]`：它是②④⑤⑥四腿里唯一被反复自证过、且读回形状最规整的一种。
2. **逗号同时是 mpv 切 `af` 列表的分隔符**（③/③b/⑥）。十段链两种拼法实测**频谱逐带相同**
   （RMS −9.4、九带 +6.8…+11.8），`--af` 启动期与 `change-list af set` 播放中都一样：
   - `lavfi=[a,b,c]`：内核把整串当**一条**条目，读回是一个 `params.graph` 里带逗号；
   - `lavfi=[a],lavfi=[b]`：**每条一个滤镜**，读回是 N 条结构化条目。
   本表选后者，理由是"每一条都不带逗号"这一条规则只有一处例外都不留：
   `transform.py` 第 7 条已经为 `--vf` 里的 `min(a,b)` 付过一次学费（逗号被当分隔符劈开、
   整条链变空），而 `change-list af append` 带逗号时**实测就是拒收**（⑥ 那一腿：
   `append lavfi=[eq1],lavfi=[eq2]` → `error running command`，链表纹丝不动）。
   代价量过：两种拼法在 ao=pcm 上吞吐都是 ≈13.8×（③b），十一条目不比一条图贵。
3. **换档只有 `change-list af set <整串>` 这一条路**（⑥）。实测可用的形状：
   `set` 多条目 ok、`set` 单条带逗号 ok、`set_property af <串>` 同样 ok、
   `set ""` → `[]`、`set_property af ""` → `[]`；
   **`set_property af "[]"` 被拒**（`unsupported format for accessing property`）——
   所以"关到零"发的是空串，不是 `lavfi=[]`（后者内核真去建一个空图滤镜，
   回 `Creating filter 'lavfi' failed.` + `Audio filter initialized failed!`，一声不出）。
   `remove <原样条目串>` / `toggle <原样条目串>` 也能删掉一条，但"删一档"要的是精确匹配
   整串，改一根滑轨就变成"删一条 + append 一条"，而 append 带逗号又是拒收的——
   所以本表一律整条替换，与画面那一族同一个形状，也避免"界面动了、链上还是旧的"。
4. **`af` 是列表属性、换片不丢**（⑤，按节奏放的那条管线量的）：播放中挂 bass+12，
   产物里低频 47.9 → 59.8；`loadfile` 换成第二片后又取一段，仍是 59.1（该窗口的基线是 47.9）。
   所以不需要在 file-loaded 时补发——和 `transform.py` 第 5 条同一条结论。
5. **参数名与范围照抄本机 `ffmpeg -h filter=<name>` 与内核回报**：
   `equalizer` 的参数是 `f`（Hz）、`t`（`h`＝带宽按 Hz、`q`＝按 Q）、`w`、`g`（dB，内核范围 −900..900）；
   `bass`/`treble` 是 `f`、`w`、`g`（`gain` 是规范名、`g` 是别名，两种实测**都出同样的频谱**——
   ② 那一腿特意量了 `bass=gain=12`，与 `bass=g=12` 逐带一致）；
   `superequalizer` 是 `1b..18b` 十八档**线性 0..20**（不是 dB，实测 `5b=12` 在 150-350 带 +14.8），
   本表不用它——把"线性 0..20"翻译成用户的 dB 会引入一层没人验证过的映射；
   `anequalizer` 在本机**不可用**：② 那一腿 `Could not set non-existent option 'f'` +
   `Audio filter initialized failed!`（这一版只暴露 `params`/`curves`，手册那种 `band|band` 写法
   在 ffmpeg 自己那关就报 `Option not found`）。
   本表的十段用 `equalizer`：`w = max(int(f*0.45), 8)`（≈0.65 倍频程），实测十段全开时
   九个测量带 **+6.8…+11.8 dB**、标称 +12 dB（③b/④a）——即"每一段真的在动"。
6. **前置增益（preamp）必须带 `dB` 后缀，而且只能是后缀写法**（`tests/out/eq-vol-r12.log`）：
   `volume=-9dB` 实测 −9.0 dB；`volume=0.3548` 线性写法 −9.0 dB（两把尺对得上，说明 0.3548 就是 10^(−9/20)）；
   `volume=dB(-9)` 那种"函数形式"在本机 **rc=4294967274 直接失败**（`dB(9)` 同样）；
   而 `volume=-12`（**没有**后缀）是**线性 −12**＝幅度乘 12，实测反而 **+17.9 dB 并削顶**
   （`max_volume=0.0 dB`＝顶到满刻度；第一趟终端里读到的是 +17.8，同一串写法两次独立跑差 0.1 dB，
   量级与符号一致，留档那份取重跑值）。
   所以 `preamp_graph()` 无条件拼 `…dB`，读回时 `volume=` 后面不带 `dB` 的条目算"读不懂"，
   绝不按 dB 解释（这是本模块最阴的一个坑：符号相同、量纲不同、下场是爆音）。
7. **削顶是实测出来的，不是猜的**：十段全 +12 且不带前置时产物 `peak=1.000 clip=657`
   （按节奏放那一腿 `clip=538`）；同一串后面加 `volume=-9dB` 之后 `peak=0.524 clip=0`，
   逐带变成 +0.7…+2.8（＝+12 抬升被 −9 前置压下来）。
   于是外壳定了一条口味线 `CLIP_NET_MAX = 3.0`："最大抬升 + 前置 ≤ +3 dB"这一档有实测支撑
   （+12−9=+3 时 clip=0），**但 −9 这个具体数只是那一个测点**，别的抬升幅度下的临界值是外推。
   预设（`PRESETS`）按这条线自动给负前置，面板上用户自己拖的前置不受它管。

热切这件事值得单独说一句，因为量具差点自己骗自己（④a/④b）：
`--ao=pcm` 落盘**不按实时跑**（8 s 媒体 0.58 s 抽干＝13.8×，90 s 媒体 0.6 s＝148…165×），
所以"第 1.2 s 发命令"发在一个早就 EOF 的进程上，产物里根本没有切点（④b 四种组合全部
"这腿没有证据"：要么命令没发出去就退出了，要么切点落在 90.0 s＝文件末尾之外）。
只有带视频轨 + `--force-window=yes` 那一腿被画面拖到 ≈1.00× 才真的量到东西；
而即便在那一腿，音频管线也跑在 `time-pos` 前面（发命令时 time-pos=14.92，
产物里的切点却按字节算是 15.0 s，读回的 time-pos 报 15.24）——
**切点必须按产物字节定位，不能拿 time-pos 当刻度**。

界面上的十根滑轨因此画的是**内核读回来的 `af`**（`parse_entries()` 把条目串反解成逐档 dB），
不是"我发出去的那串"：越界值在拼串前就被本表夹掉，读回对不上时面板要把那一档重画成内核的值。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .tweaks import Num

MPV_MEASURED_ON = "v0.41.0-1101-g5d85ba5fb"

# 段中心频率是 ISO 十倍频程的标准档；上限 16k 是量过的（样片 48 kHz，16k 段带宽 7.2 kHz
# 的上边缘 19.6 kHz 仍在 Nyquist 之下）。44.1 kHz 片上没单独量过（未测），但 16k＋带宽
# 仍低于 22.05 kHz。
_BAND_DEF: tuple[tuple[int, str, str], ...] = (
    (31, "31", "31 Hz"),
    (62, "62", "62 Hz"),
    (125, "125", "125 Hz"),
    (250, "250", "250 Hz"),
    (500, "500", "500 Hz"),
    (1000, "1k", "1 kHz"),
    (2000, "2k", "2 kHz"),
    (4000, "4k", "4 kHz"),
    (8000, "8k", "8 kHz"),
    (16000, "16k", "16 kHz"),
)

BAND_LO, BAND_HI = -12.0, 12.0        # 内核允许 −900..900 dB，这里按"能听且不削顶"收窄
PRE_LO, PRE_HI = -18.0, 6.0           # 前置只做"往下让"的用途；往上到 +6 就等着削顶
CLIP_NET_MAX = 3.0                    # 见模块 docstring 第 7 条（实测那一点是 +12−9=+3 ⇒ clip=0）


@dataclass(frozen=True)
class Band:
    """链上的一段。`cfg` 是配置键（`eq_1k` 这种），`hz` 是内核 `f=` 的值。"""
    hz: int
    tag: str
    cfg: str
    label: str
    spec: Num

    def graph(self, gain: Any) -> str:
        g = self.spec.clamp(gain)
        if g == 0:
            return ""
        return f"equalizer=f={self.hz}:t=h:w={band_width(self.hz)}:g={_num(g)}"


def band_width(hz: int) -> int:
    """带宽（Hz）：≈0.45 倍中心频，最低 8 Hz。这是 ③/④ 实测用的那一条形状，不改。"""
    return max(int(hz * 0.45), 8)


BANDS: tuple[Band, ...] = tuple(
    Band(hz, tag, f"eq_{tag}", label,
         Num(f"eq_{tag}", f"eq_{tag}", BAND_LO, BAND_HI, 1.0, 0.0, label, " dB", "%.0f"))
    for (hz, tag, label) in _BAND_DEF)

PREAMP = Num("eq_preamp", "eq_preamp", PRE_LO, PRE_HI, 1.0, 0.0, "前置增益", " dB", "%.0f")

BAND_BY_HZ: dict[float, Band] = {float(b.hz): b for b in BANDS}
BAND_BY_CFG: dict[str, Band] = {b.cfg: b for b in BANDS}
SPECS: tuple[Num, ...] = tuple(b.spec for b in BANDS) + (PREAMP,)
_BY_KEY: dict[str, Num] = {s.cfg: s for s in SPECS}
# 频段按"低频在前、前置最后"上链（线性滤波可交换，但顺序固定下来才有一串可比对的契约，
# 判据要能整串比对而不是"凑巧一样"）。
ORDER: tuple[str, ...] = tuple(b.cfg for b in BANDS) + (PREAMP.cfg,)


def spec_for(key: str) -> Num | None:
    """配置键（或本表自用的伪属性名）→ Num。不在表里返回 None，调用方据此拒绝下发。"""
    return _BY_KEY.get(key)


def is_known(key: str) -> bool:
    return key in _BY_KEY


def defaults() -> dict[str, Any]:
    """配置默认值（键 → 值）。`config.DEFAULTS` 从这里取，避免两处各写一份。"""
    return {s.cfg: s.default for s in SPECS}


def reset_values() -> dict[str, Any]:
    """"一键复位"写回配置的值：走一遍归一，默认值本身写歪了也不会被原样发出去。"""
    return {k: normalize(k, v) for k, v in defaults().items()}


def normalize(key: str, v: Any) -> float:
    """配置里该存的值：夹紧到本表范围（内核**不会**替我们夹，而且越界的下场是"没声音"）。

    非有限的数（NaN、±inf）不进链、也不夹到量程端点，而是退回默认值 0 dB。
    为什么不靠 `clamp` 兜：`max(lo, min(hi, nan))` 在 CPython 上是 **`hi`**（两次比较都
    "nan 不比 hi 小"，于是留下 hi），也就是说一个读不懂的数会被翻成"满增益 +12 dB"——
    那既不是安全兜底、也不是用户要的，而是一响就被听出来的意外（这一族的坏值下场是没声音，
    而 NaN 的来源恰恰是"配置被改坏了／内核没答上来"那几处，见 `audio_eq_panel._f`）。
    表外的键保持旧行为（能转数就转、转不了就 0），因为调用方拿 None 判"表外"更早发生。
    """
    try:
        x = float(v)
    except (TypeError, ValueError):
        x = float("nan")
    s = _BY_KEY.get(key)
    if s is None:
        return x if x == x and x not in (float("inf"), float("-inf")) else 0.0
    if x != x or x in (float("inf"), float("-inf")):
        return float(s.default)
    return s.clamp(x)


def _num(x: float) -> str:
    """写成内核串里最短的样子：3.0→'3'，-3.5→'-3.5'。"""
    r = round(float(x), 4)
    return f"{int(r)}" if r == int(r) else f"{r}"


def preamp_graph(v: Any) -> str:
    """前置增益 → `volume=<x>dB`；0 时返回空串（整条链为空时根本不发 `--af`）。

    `dB` 后缀是**必须**的：实测 `volume=-12`（无后缀）是线性 −12＝幅度乘 12，
    产物反而 +17.9 dB 并削顶；`volume=dB(-12)` 那种函数写法在本机 rc≠0。
    """
    x = PREAMP.clamp(v)
    return "" if x == 0 else f"volume={_num(x)}dB"


def gain_of(cfg, key: str) -> float:
    s = _BY_KEY.get(key)
    if s is None:
        return 0.0
    return s.clamp(cfg.get(key, s.default) if hasattr(cfg, "get") else s.default)


def gains(cfg) -> dict[str, float]:
    """按 `ORDER` 取当前十段＋前置的 dB 值（0＝这一档不上链）。"""
    return {k: gain_of(cfg, k) for k in ORDER}


def chain_parts(cfg) -> list[str]:
    """链上每一节**括号里**那一段（低频在前、前置在最后）。"""
    out: list[str] = []
    for b in BANDS:
        g = b.graph(gain_of(cfg, b.cfg))
        if g:
            out.append(g)
    p = preamp_graph(gain_of(cfg, PREAMP.cfg))
    if p:
        out.append(p)
    return out


def build_chain(cfg) -> str:
    """整条 `af` 的值：`lavfi=[…],lavfi=[…]`，每条一个滤镜；全零时返回空串。

    空串和 `lavfi=[]` 是两回事：实测 `set ""` → 内核回报 `[]`（干净清空），
    而 `set lavfi=[]` 会让内核去建一个空图滤镜并失败，日志
    `Audio filter initialized failed!`、产物一个字节不写（②/⑥ 两腿）。
    """
    return ",".join(f"lavfi=[{p}]" for p in chain_parts(cfg))


def startup_args(cfg) -> list[str]:
    """启动参数：链为空时**一条都不发**（见 `build_chain` 的空图下场）。"""
    chain = build_chain(cfg)
    return [f"--af={chain}"] if chain else []


def auto_preamp(vals: dict[str, float]) -> float:
    """按口味线给预设配的前置：≤0，且让"最大抬升＋前置"不超过 `CLIP_NET_MAX`。"""
    top = max([v for k, v in vals.items() if k != PREAMP.cfg] + [0.0])
    return max(PRE_LO, min(0.0, CLIP_NET_MAX - top))


# ==================== 预设 ====================

@dataclass(frozen=True)
class Preset:
    """一档预设。取值是**外壳的口味**，不是内核量出来的；唯一有实测撑的规矩是
    `clip_net() <= CLIP_NET_MAX`（见模块 docstring 第 7 条），判据按这条数。"""
    key: str
    label: str
    hint: str
    vals: dict[str, float]

    def clip_net(self) -> float:
        top = max([v for k, v in self.vals.items() if k != PREAMP.cfg] + [0.0])
        return top + self.vals.get(PREAMP.cfg, 0.0)


def _preset(key: str, label: str, hint: str, **kv: float) -> Preset:
    vals = {k: float(v) for k, v in kv.items()}
    for b in BANDS:                          # 没写到的档位补 0，预设里不许缺键
        vals.setdefault(b.cfg, 0.0)
    vals.setdefault(PREAMP.cfg, 0.0)
    return Preset(key, label, hint, vals)


FLAT = _preset("flat", "平坦", "全部归零（＝不挂滤镜）")
VOCAL = _preset("vocal", "人声", "压低频噪音、抬 1k–4k 的清晰度",
               eq_31=-3, eq_62=-2, eq_125=-1, eq_1k=4, eq_2k=4, eq_4k=2, eq_8k=1,
               eq_preamp=-2)
BASS = _preset("bass", "低音", "抬 31–125，前置往下让以免削顶",
               eq_31=6, eq_62=5, eq_125=2, eq_4k=-1, eq_8k=-2, eq_16k=-2,
               eq_preamp=-4)
BRIGHT = _preset("bright", "明亮", "抬 2k 以上，适合闷掉的片子",
                 eq_250=-1, eq_2k=3, eq_4k=4, eq_8k=5, eq_16k=4, eq_preamp=-3)
CINEMA = _preset("cinema", "影院", "微笑曲线：两头抬、中间略压",
                 eq_31=5, eq_62=4, eq_125=2, eq_500=-1, eq_2k=1, eq_4k=3,
                 eq_8k=4, eq_16k=5, eq_preamp=-3)
NIGHT = _preset("night", "夜间", "压低频与齿音，别吵到别人",
                eq_31=-5, eq_62=-4, eq_125=-2, eq_4k=-1, eq_8k=-3, eq_16k=-4)

PRESETS: tuple[Preset, ...] = (FLAT, VOCAL, BASS, BRIGHT, CINEMA, NIGHT)
PRESET_BY_KEY: dict[str, Preset] = {p.key: p for p in PRESETS}


def preset_values(key: str) -> dict[str, float] | None:
    p = PRESET_BY_KEY.get(key)
    if p is None:
        return None
    return {k: _BY_KEY[k].clamp(v) for k, v in p.vals.items()}


# ==================== 读回：把内核的 af 列表反解成逐档 dB ====================

_VOL_RE = re.compile(r"^volume=(?P<g>[+-]?[0-9]*\.?[0-9]+)\s*dB$", re.IGNORECASE)


def _params(graph: str) -> dict[str, str]:
    """`equalizer=f=31:t=h:w=13:g=12` → {'f': '31', 't': 'h', …}（冒号分隔的 key=value）。"""
    _, _, rest = graph.partition("=")
    out: dict[str, str] = {}
    for kv in rest.split(":"):
        k, sep, v = kv.partition("=")
        if sep:
            out[k.strip()] = v.strip()
    return out


def parse_entries(af: Any) -> tuple[dict[str, float] | None, list[str]]:
    """内核 `af` 列表 → ({配置键: dB 或 None＝链是空的}, 读不懂之处的原话)。

    为什么必须有这一步：面板的铁律是"画内核回报的值"。`af` 的回报不是逐档数字，
    而是 `[{'name': 'lavfi', 'enabled': True, 'params': {'graph': 'equalizer=f=31:…'}}]`
    这样一串（⑥ 那一腿读回的原始形状），所以真值得信的东西要靠这里反解。
    认不出来的条目一律进"原话"列表而不是被 `min()`/静默丢掉——那正是
    "界面看着对、其实内核挂的是别的东西"的产地。

    返回的第一个值：
      - 链为空（`[]`／没有 lavfi 条目）→ `{}`（空 dict，不是 None）：调用方据此把十档都画成 0；
      - 正常 → 每个出现的档位一条，没上链的档位**不写在里面**（调用方补 0）；
      - 整条读不出（None、串、怪形状）→ `None` + 原话，调用方不许拿"空 dict"当"都归零"。
    """
    out: dict[str, float] = {}
    problems: list[str] = []
    if af is None:
        return None, ["读不到 af（内核未启动或未应答）"]
    if isinstance(af, str):
        return None, [f"af 回报是串而不是列表：{af[:80]!r}"]
    if not isinstance(af, list):
        return None, [f"af 回报形状不认识：{type(af).__name__}"]
    for e in af:
        if not isinstance(e, dict):
            problems.append(f"条目不是对象：{str(e)[:60]}")
            continue
        name = str(e.get("name", ""))
        if not name.startswith("lavfi"):
            problems.append(f"非本表条目：{name}")
            continue
        graph = str((e.get("params") or {}).get("graph", "") or "").strip()
        if not graph:
            problems.append("lavfi 条目没有 graph（空图会让内核整条失败）")
            continue
        if "," in graph:
            problems.append(f"括号图里带逗号（本表不发这种形状）：{graph[:60]}")
            continue
        if not e.get("enabled", True):
            problems.append(f"条目被禁用：{graph[:60]}")
            continue
        head, _, _rest = graph.partition("=")
        if head == "volume":
            m = _VOL_RE.match(graph)
            if not m:
                # `volume=-12` 这种没有 dB 后缀的是**线性**增益（实测＝乘 12、+17.9 dB）。
                # 绝不能按 dB 解释，只能原话报出来。
                problems.append(f"volume 条目不带 dB 后缀（内核按线性算）：{graph[:60]}")
                continue
            try:
                out[PREAMP.cfg] = PREAMP.clamp(float(m.group("g")))
            except (TypeError, ValueError):
                problems.append(f"前置增益读不懂：{graph[:60]}")
            continue
        if head != "equalizer":
            problems.append(f"不认识的滤镜：{graph[:60]}")
            continue
        p = _params(graph)
        if p.get("t", "h").lower() != "h":
            problems.append(f"equalizer 的 t 不是 h（带宽不按 Hz 给）：{graph[:60]}")
        band = None
        try:
            band = BAND_BY_HZ.get(float(p.get("f", "")))
        except (TypeError, ValueError):
            band = None
        if band is None:
            problems.append(f"equalizer 的中心频率不在表里：{graph[:60]}")
            continue
        try:
            g = float(p.get("g", ""))
        except (TypeError, ValueError):
            problems.append(f"增益读不懂：{graph[:60]}")
            continue
        if band.cfg in out:
            problems.append(f"同一频段挂了两次：{graph[:60]}")
            continue
        out[band.cfg] = band.spec.clamp(g)
    return out, problems


def parse_matches(cfg, parsed: dict[str, float] | None) -> tuple[bool, list[str]]:
    """内核反解出来的逐档值 vs 配置里请求的值：逐档比（0＝该档不在链上，等价）。"""
    if parsed is None:
        return False, ["读不出"]
    want = gains(cfg)
    bad: list[str] = []
    for k, v in want.items():
        got = float(parsed.get(k, 0.0) or 0.0)
        if abs(got - float(v)) > 1e-4:
            bad.append(f"{k} 请求 {v:+g} 内核是 {got:+g}")
    unknown = [k for k in parsed if k not in want]
    for k in unknown:
        bad.append(f"链上多了没人请求的一档：{k}")
    return (not bad), bad


# ==================== 日志：这一族的"其实没跑起来" ====================

# 逐条来自 tests/out/eq-probe-r15b.log 的原文（②的越界/不存在滤镜/空图三档、①的启动致命档、
# 以及正常腿里出现过的写法）。`transform.DISABLE_MARKS` 那一张是画面族的，别混用：
# 画面族的下场是"Disabling filter xxx"（列表照显示、像素不动），
# 声音族的下场是整条 af 起不来、一声不出，两句原话不一样。
EQ_FAIL_MARKS = ("Audio filter initialized failed", "Creating filter 'lavfi' failed",
                 "No such filter", "Error creating filters", "Error applying option",
                 "out of range", "Could not set non-existent option",
                 "Error parsing option af", "Option af: Error", "Error parsing options",
                 "Setting commandline option --af=", "isn't supported", "is not supported",
                 "Option af-set", "Option af-append", "-append takes only 1 item",
                 "No video or audio streams selected")


def failures(log_text: str) -> list[str]:
    """从内核日志里挑出"af 这一族没成立"的行（去重、只留人话那截）。

    为什么必须有：`get af` 读回的东西在滤镜图创建失败时**照样是一条列表项**，
    而产物可以是一个字节都没写（实测三档 `产物=-1`）。外壳不看日志就只能报
    "均衡器已开启"，真相是整条链路被内核扔了、用户听到的是静音。
    """
    out: list[str] = []
    for line in str(log_text or "").splitlines():
        if any(m in line for m in EQ_FAIL_MARKS):
            t = line.strip()
            if "[e]" in t:
                t = t.split("]", 2)[-1].strip()
            if t and t not in out:
                out.append(t)
    return out


# ==================== 界面要的小工具 ====================

NUMS: tuple[Num, ...] = SPECS
# 本表里 `spec.prop` 与 `spec.cfg` 是同一串（`af` 是列表属性，内核没有"逐档属性"可订阅，
# 真值只能从 `af` 读回反解——见 `parse_entries`）。所以 `spec_for()` 一种写法就够，
# 别再像 transform 那样建两张索引：两张表指向同一件事，就多了一处会不一致的地方。


def label(key: str) -> str:
    s = _BY_KEY.get(key)
    return s.label if s is not None else str(key)


def value_text(key: str, v: Any) -> str:
    """一行字：0 写成 "0 dB"（不写 "+0"），其余带正负号。"""
    s = _BY_KEY.get(key)
    if s is None:
        return str(v)
    x = s.clamp(v)
    return "0 dB" if x == 0 else f"{x:+.0f} dB"


def is_active(cfg) -> bool:
    """链上真有没有东西（面板标题上那颗徽章、OSD 与"要不要发 --af"都看这个）。"""
    return bool(chain_parts(cfg))


def summary(cfg) -> str:
    """一行给 OSD/提示用的概览：只说动了哪几段，全零时说"未启用"。"""
    parts = [f"{BAND_BY_CFG[k].label} {v:+.0f}" for k, v in gains(cfg).items()
             if k != PREAMP.cfg and v]
    p = gain_of(cfg, PREAMP.cfg)
    if p:
        parts.append(f"前置 {p:+.0f}")
    return "、".join(parts) if parts else "未启用"
