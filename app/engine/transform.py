"""画面变换与画质增强的单一事实表（几何走属性，增强走滤镜链）。

为什么再开一张表，而不是往 `tweaks.py` 里塞：那边每一项都是"发一个 set_property 就完事"，
这一族里有两类东西的**下发形状根本不同**——
  - 几何类（比例 / 旋转 / 充满 / 平移）是 GPU 侧属性，一次一条 set_property，零成本；
  - 增强类（镜像 / 裁剪 / 锐化 / 降噪 / 去带）不是属性，是**一条滤镜链**，
    必须整条拼好后用一次 `change-list vf set` 替换，界面每个滑轨只改链上的一小截。
把它们混进一张表，就会有两处代码各自决定"这项怎么发"，那正是"界面动了、画面没动"的产地。

**全部取值以本机这版内核实测为准**（v0.41.0-1101-g5d85ba5fb，hwdec=auto-safe→d3d11va；
测法＝set 之后立刻 get 回来比对，并在同一帧位置各截一张逐像素比。
证据留在 `tests/out/probe-transform.log`、`probe-transform2.log`、`probe-transform4.log`、
`probe-transform5.log`、`probe-transform6.log`、`probe-transform7-crop.log`、
`probe-transform11.log`（越界值到底是夹还是拒，从已知旧值逐项重测那一份）、
`probe-transform12.log`（`change-list vf append` 到底什么形状会留残留，带原始 error 应答那一份），
接线级对照（对着真内核把这一族每项走一遍）在 `probe-transform-engine.log`。

1. 越界值是**整条拒收**，不是夹紧——所以夹紧是外壳的承重件。这一条原来写错过一次，
   2026-10-10 用 `probe-transform11.py` 从**已知旧值**逐项重测（旧值 0.0/0.5/1.0 各发一遍）
   才把机制钉对：**本机这版内核对越界值一次都没夹过**，日志里连一条 "Clamped" 都没有，
   回报永远是发之前的旧值。
   `video-rotate` 只收 0..359（发 360、-90 都停在旧值，不管旧值是 0 还是 90）→ 外壳取模归一；
   `panscan` 两侧都拒（-1.0 与 2.0 都停在旧值，连 -0.0001 都拒）→ [0,1] 由外壳夹。
   **旧写法"发 -1.0 也夹到 1.0"是把上一档量到的旧值 1.0 当成了夹紧结果**：结论（外壳必须夹）
   没错，机制说反了——内核不是"夹到最大"而是"根本不动"，所以指望它得到的不是
   "画面糊到最大"而是"界面动了、画面没动"，这个差别决定了出错时该往哪查。
   `video-pan-x/y` 没有边界（±5.0 照单全收，实测回报就是 5.0），我们只开放 ±1.0（再大画面整个飞走）；
   `video-scale-x` 也没有上界（发 3.0、发 0.0 都照收；0.0＝画面直接没了），负值才拒——
   这一族干脆不开放它，范围不是内核给的，就别指望内核兜。
   `video-aspect-override` 收 "16:9" 也收浮点、回报浮点，但 **"auto" 被拒**——
   自动档要发 `no`（回报 -2.0）。**发 -1 会吃一条 deprecation 警告**（实测每次启动都打
   "Setting video-aspect-override to -1 is deprecated"），-1 是老版 sentinel、-2 是这版
   的"不覆盖"；所以下发用 `no`，回读比对时 -1 与 -2 都算自动档，别只认一个数；
   订阅缓存还没填上时 `prop()` 回的是配置里那串 `"no"`，所以比对还要认第三种写法
   （否则每按一次"下一档比例"都红一条"内核没收下"）。
2. 滤镜链最大的坑不是"有没有这个滤镜"，而是**它能不能初始化**：
   本机硬解给的是 d3d11 的 nv12 帧。实测 `hflip`、`vflip`、`unsharp`、`cas`、`hqdn3d`、
   `rotate` 单独发下去，内核回话 "Impossible to convert between the formats…" 紧跟着
   "Disabling filter xxx"——可 `vf` 列表里名字**照样在**、`enabled` 照样 True，
   同一帧逐像素差 0.00。也就是说"读列表"证明不了任何事。
   加上 `format=yuv420p` 打头才真的生效：锐化边缘能量 1.400→2.184；
   水平镜像与"基线左右翻转"的差 0.070、与基线本身的差 18.202。
   `crop` 是唯一不用前缀的（ffmpeg 的 crop 直接吃 nv12），实测 1280×720 裁出 1280×404。
   所以每个滤镜带一个 `needs_sw`：链里只要有它，就自动补 `format=yuv420p`；
   一个滤镜都不用时**不补**——实测裸 `format=yuv420p` 也让像素动了 0.01，
   那是硬解帧往返 CPU 的代价，白付不好。
3. 滤镜名与参数名照抄 `mpv --vf=<name>=help` 与 `ffmpeg -h filter=<name>`：
   `deband` 的参数是 `1thr/2thr/3thr/range`，**不是** mpv 自带去带选项那套
   `iterations/threshold`——实测发 `deband=iterations=4` 内核回 "AVOption 'iterations'
   not found"，整条创建失败。`unsharp` 的 `luma_amount` 内核允许 -2..5（负的是模糊），
   本表只开放 0..2：带噪片段上实测 amount=2.0 已把高频从 13.7 抬到 31.3（噪点一起放大）。
   `nlmeans` 在本机实测像素一点没动（0.00），所以不开放，降噪走 `hqdn3d`。
4. 内核自带的 GPU 侧去带（`--deband`，在 `perf.py` 的"高质量"预设里）**不能热切**：
   实测 `change-list file-local-options deband yes` 之后 `options/deband` 仍回报 False、
   像素零变化。所以这里另做一档滤镜去带（热切、立刻看得见）。两者不是同一个开关，
   界面措辞和 docs 都要写清楚，别让人以为设置页那一档没生效。
5. `vf` 是属性，换片不丢（实测重新 loadfile 之后仍是 [format,unsharp]），
   所以不需要在 file-loaded 时补发。
6. `crop` 用 `iw/ih` 表达式（实测 `crop=iw:ih*9/16:0:(ih-ih*9/16)/2` 生效），
   因此裁剪能直接进启动参数、不必先知道片子尺寸。表达式里不能带 ':' 和空格
   （mpv 与 ffmpeg 都按 ':' 切参数）。注意裁剪后 `video-out-params` 的 w/h **仍回报原尺寸**
   （实测 1280×720），界面别拿它当"裁完多大"——唯一能证的是截图尺寸。
7. **`min()`／`max()`／`if()` 这类多参函数不能出现在 `--vf` 串里**：它们的参数分隔符 `,`
   正是 mpv 切滤镜的分隔符。实测 `crop=trunc(min(iw,ih*235/100)/2)*2:…` 内核回
   `Option vf-set: 'ih' isn't supported.`、`vf` 列表直接变空、截图仍是 1280×720；
   把逗号转义成反斜杠逗号、或把整段表达式用单引号包起来，**两种写法也一样被拒**
   （本机这版没有可用的转义出路）。
   所以"取较小者"用恒等式 `min(a,b) = (a+b-abs(a-b))/2` 写——`abs()` 只有一个参数，
   不带逗号。实测五种比例（2.35:1→1280×544、4:3→960×720、1:1→720×720、
   16:9→1280×720、21:9→1280×548）全部按截图尺寸对上，且与 `format=yuv420p`、`hflip`、
   `unsharp` 同链时仍成立。证据：`tests/out/probe-transform7.log`。
8. 布尔项的**串**写法不能交给 `bool()`：实测 `bool("0") is True`、`bool("false") is True`。
   人手把配置里的 `"mirror_h"` 改成 `"0"` 想关掉镜像，走 `bool()` 会得到"checkbox 显示关着、
   画面上 hflip 还挂着"——比"界面动了画面没动"更难查，因为配置看起来确实是关的。
   所以 `as_flag()` 对字符串只认白名单 `("1","true","yes","on")`，认不出的串一律算关
   （一个说不清的值不该往链上加一档滤镜）。JSON 里写真布尔当然没问题，这一条兜的是手改、
   非 JSON 来源，以及 `cycle_transform` 那类"把当前缓存值原样喂回来"的路径。
9. **替换整条链只有 `change-list vf set` 这一条路**，`append`/`remove` 都不能当"改一档"用
   （2026-10-10 用 `probe_transform12.py` 逐个形状量的，每次都抓 IPC 应答里的原始 error）：
   - append 的值**带逗号就整条拒收**：`append "format=yuv420p,hflip"` 应答
     `error running command`，日志原文 `Option vf-append: -append takes only 1 item (no ',').`，
     链表纹丝不动（还是 `[]`）。也就是说"逐档 append 一条链"这种写法在内核那儿根本走不通。
   - append 的值是**单档**时照挂，而且**同档挂两次就是两条**：`append hflip` 两次 →
     `['hflip', 'hflip']`；`set unsharp` 之后再 `append unsharp` → `['unsharp', 'unsharp']`。
     这才是"残留"的真形状：每次改配置往链上补一档，改一轮就攒出一串重复滤镜。
   - `remove` 一次只删一条同名的（`['hflip','hflip']` 删一次还剩一条），所以"关掉一档"
     走 remove 也删不干净；`set ""` 才是清空（实测回报 `[]`）。
   - 对照：同一条带逗号的链走 `set "format=yuv420p,hflip"` 是能挂上的（`['format','hflip']`），
     所以 append 那次的空列表是内核拒收，不是命令没送到。
   顺带又撞见一次"列表≠事实"：在硬解帧上 `append hflip`（没前缀）之后日志回
   `Disabling filter hflip.00 because it has failed.`，而列表照显示两条、与基线逐像素差 0.000。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .tweaks import Choice, Flag, Num      # 复用同一组控件描述，别再抄一份 clamp/text

MPV_MEASURED_ON = "v0.41.0-1101-g5d85ba5fb"

# 硬解帧转软件帧的那一步：只有链里真有吃不下 nv12 的滤镜时才补，且必须在最前。
SW_PREFIX = "format=yuv420p"
# 自动档在**配置里**长这样，在**内核里**也长这样（`no` → 回报 -2.0）。
# 内部比对用的"自动"哨兵是负数（ratio_float 给 -1.0），和线上值不是一回事，别混用。
ASPECT_AUTO = "no"


# ==================== 几何类（属性，一次一条 set_property，GPU 侧零成本） ====================

ASPECT = Choice("video-aspect-override", "aspect_override", ASPECT_AUTO, "画面比例",
                ((ASPECT_AUTO, "自动（按片子本身）"), ("16:9", "16:9"), ("4:3", "4:3"),
                 ("21:9", "21:9"), ("2.35:1", "2.35:1"), ("1:1", "1:1")))
ROTATE = Choice("video-rotate", "rotate", "0", "旋转",
                (("0", "0°"), ("90", "90°"), ("180", "180°"), ("270", "270°")))
KEEPASPECT = Flag("keepaspect", "keep_aspect", True, "保持比例（不拉伸）")
PANSCAN = Num("panscan", "panscan", 0.0, 1.0, 0.01, 0.0, "充满画面", "", "%.2f")
PAN_X = Num("video-pan-x", "pan_x", -1.0, 1.0, 0.05, 0.0, "左右平移", "", "%.2f")
PAN_Y = Num("video-pan-y", "pan_y", -1.0, 1.0, 0.05, 0.0, "上下平移", "", "%.2f")

PROPS: tuple[Any, ...] = (ASPECT, ROTATE, KEEPASPECT, PANSCAN, PAN_X, PAN_Y)


# ==================== 增强类（滤镜链，一次整条替换） ====================

@dataclass(frozen=True)
class Filter:
    """链上的一档。`expr` 把配置值翻成发往内核的那一截；返回空串＝这一档不上链。

    `spec` 同时是这张表的一行（Num/Flag/Choice），负责取值范围、默认值与界面文字；
    `spec.prop` 就是 ffmpeg 的滤镜名，`spec.cfg` 就是配置键。
    needs_sw 是实测结论的落点（模块 docstring 第 2 条）：True 的滤镜在没有
    `format=yuv420p` 的链上会被内核**静默禁用**，界面却什么都看不出来。
    """
    key: str
    label: str
    needs_sw: bool
    spec: Any

    def expr(self, v: Any) -> str:
        return ""


class _Strength(Filter):
    """按强度值算参数的基类（0 一律＝不开，链里干脆不留这一节）。"""

    def expr(self, v: Any) -> str:
        s = self.spec.clamp(v)
        return "" if s <= 0 else self.params(s)

    def params(self, s: float) -> str:
        return self.key


class _Sharpen(_Strength):
    def params(self, s: float) -> str:
        # 位置参数＝luma 尺寸x:y:强度 + chroma 尺寸x:y:强度；色度不动（实测只会糊边）
        return f"unsharp=5:5:{s:.2f}:5:5:0.0"


class _Denoise(_Strength):
    def params(self, s: float) -> str:
        # 0..10 档 → hqdn3d 的四个参数；上限对齐实测有效档（4.0 时高频 13.7→13.0）
        q = (s / 10.0) * 4.0
        return f"hqdn3d={q:.2f}:{q * 0.75:.2f}:{q * 1.5:.2f}:{q * 1.125:.2f}"


class _Deband(_Strength):
    def params(self, s: float) -> str:
        # 1thr 内核范围 3e-05..0.5（照抄 ffmpeg -h filter=deband）；10 档＝0.040，实测有效
        thr = 0.004 * s
        return (f"deband=1thr={thr:.4f}:2thr={thr * 0.75:.4f}"
                f":3thr={thr * 0.75:.4f}:range={int(6 + s * 1.8)}")


class _Crop(Filter):
    def expr(self, v: Any) -> str:
        wh = _ratio_wh(str(v or ""))
        if not wh:
            return ""
        n, d = wh
        # 居中裁到目标比例：宽取"装得下的最大宽"、高同理；trunc/2*2 是为了 yuv420p 的偶数要求。
        # 这里的"取较小者"**不能**写成 min(a,b)：',' 是 mpv 切滤镜的分隔符，实测整条链会被
        # 内核拒收（vf 变空、画面不动）。用恒等式 min(a,b)=(a+b-abs(a-b))/2 绕开，
        # abs() 单参、不带逗号。分母 4 是 min 的 /2 与偶数化的 /2 合成一件事。
        w = f"trunc((iw+ih*{n}/{d}-abs(iw-ih*{n}/{d}))/4)*2"
        h = f"trunc((ih+iw*{d}/{n}-abs(ih-iw*{d}/{n}))/4)*2"
        return f"crop={w}:{h}:(iw-{w})/2:(ih-{h})/2"


class _FlagFilter(Filter):
    def expr(self, v: Any) -> str:
        return self.key if as_flag(v) else ""


SHARPEN = _Sharpen("unsharp", "锐化", True,
                   Num("unsharp", "sharpen", 0.0, 2.0, 0.05, 0.0, "锐化", "×", "%.2f"))
DENOISE = _Denoise("hqdn3d", "降噪", True,
                   Num("hqdn3d", "denoise", 0, 10, 1, 0, "降噪", "档", "%.0f"))
DEBAND = _Deband("deband", "去带（滤镜）", True,
                 Num("deband", "deband_vf", 0, 10, 1, 0, "去带", "档", "%.0f"))
CROP = _Crop("crop", "按比例裁剪", False,
             Choice("crop", "crop_ratio", "none", "按比例裁剪",
                    (("none", "不裁剪"), ("16:9", "16:9"), ("4:3", "4:3"),
                     ("21:9", "21:9"), ("2.35:1", "2.35:1"), ("1:1", "1:1"))))
MIRROR_H = _FlagFilter("hflip", "左右镜像", True,
                       Flag("hflip", "mirror_h", False, "左右镜像"))
MIRROR_V = _FlagFilter("vflip", "上下镜像", True,
                       Flag("vflip", "mirror_v", False, "上下镜像"))

# 链上顺序是**契约**：先去带、再降噪、最后锐化（顺序反了会把刚压下去的噪点又抬起来），
# 几何类（裁剪、镜像）放末尾。测试整串按这个顺序比对。
FILTERS: tuple[Filter, ...] = (DEBAND, DENOISE, SHARPEN, CROP, MIRROR_H, MIRROR_V)

SPECS: tuple[Any, ...] = PROPS + tuple(f.spec for f in FILTERS)
_BY_CFG: dict[str, Any] = {s.cfg: s for s in SPECS}
# **配置键和属性名都能查到同一行**：引擎侧习惯按 mpv 属性名说话（set_tweak 那套），
# 界面与配置按 cfg 键说话。只认一种写法，另一种就会静默查不到——查不到时
# normalize/mpv_value 是原样放行的，那就成了"界面动了、内核收到一个没人核对过的值"。
_BY_KEY: dict[str, Any] = {}
for _s in SPECS:
    _BY_KEY[_s.cfg] = _s
    _BY_KEY[_s.prop] = _s
FILTER_BY_NAME: dict[str, Filter] = {f.key: f for f in FILTERS}
ALL_KEYS: tuple[str, ...] = tuple(_BY_CFG)


def _spec(key: str) -> Any:
    return _BY_KEY.get(key)


def spec_for_cfg(key: str) -> Any:
    """配置键（或属性名）→ 表项（Num/Flag/Choice）；不在表里返回 None，调用方据此拒绝下发。"""
    return _spec(key)


def filter_for_cfg(key: str) -> Filter | None:
    for f in FILTERS:
        if f.spec.cfg == key or f.spec.prop == key:
            return f
    return None


def prop_of(key: str) -> str:
    """配置键 → mpv 属性名；滤镜类返回空串（它们走链，没有属性可设）。"""
    s = _spec(key)
    return s.prop if any(s is p for p in PROPS) else ""


def defaults() -> dict[str, Any]:
    """配置默认值（键 → Python 值）。config.DEFAULTS 从这里取，避免两处各写一份。"""
    return {s.cfg: s.default for s in SPECS}


def reset_values() -> dict[str, Any]:
    """"一键复位"要写回配置的值：走一遍归一，免得默认值本身写歪了也被原样发出去。"""
    return {k: normalize(k, v) for k, v in defaults().items()}


# ==================== 取值归一与下发形状 ====================

def _ratio_wh(s: str) -> tuple[int, int] | None:
    """"16:9" / "16/9" / "2.35：1" / "1.7777" → (分子, 分母，已约到最简)；坏值 → None。

    分隔符三种写法都得认：界面下拉给 "16:9"，人手改配置会写 "16/9"，
    中文输入法打出来是 "16：9"。不认就静默变"不裁剪"——那又是一次"配置动了、画面没动"。

    用 `Fraction` 而不是"乘 100 取整"：后者会把小数**量化成两位**——实测 "1.7777" 因此
    变成 1.78，离 16/9 差到 2.2e-3，比 21:9/2.35:1 的档间距（0.0167）还经得起一档的容差
    一算就认不回 16:9 了。Fraction 吃十进制串是精确的（2.35 → 47/20、1.7777 → 17777/10000），
    顺带自动约分，ffmpeg 表达式要的"整数比"直接就有。
    """
    t = str(s or "").strip().replace("：", ":").replace("/", ":")
    if not t or t in ("none", "auto", "no"):
        return None
    a, _, b = t.partition(":")
    try:
        r = Fraction(a) / Fraction(b) if b else Fraction(a)
    except (ValueError, ZeroDivisionError):
        return None
    if r <= 0:
        return None
    # 小数位太多（1.0000001:1 这类）就不再是"一个比例"而是噪声：约不出干净的整数比，
    # 拼进表达式也只会得到一条谁也看不懂的串。按"认不出"处理，让调用方落回默认档。
    # 上限 1e7 是量出来的：表里最"脏"的合法写法 2.35:1 → 47/20，手打的 1.7777 →
    # 17777/10000（分母 1e4）都要能过，否则"1.7777 认不回 16:9"这条容差白写。
    if r.numerator > 10_000_000 or r.denominator > 10_000_000:
        return None
    return (r.numerator, r.denominator)


def ratio_float(s: Any) -> float:
    """"16:9" → 16/9；"1.7777" → 1.7777；auto/none/坏值 → -1（内部的"自动"哨兵，不是线上值）。"""
    wh = _ratio_wh(str(s or ""))
    return -1.0 if not wh else wh[0] / wh[1]


# 布尔项**串**写法的白名单：只有这几串算"开"。
_ON_STRINGS = ("1", "true", "yes", "on")


def as_flag(v: Any) -> bool:
    """布尔项的取值：真布尔/数字照 `bool()`，字符串只认白名单，别的一律"关"。

    为什么不能直接 `bool(v)`：实测 `bool("0") is True`、`bool("false") is True`——
    人手在配置里写 `"mirror_h": "0"` 想关掉镜像，结果是 checkbox 显示关着、画面上
    `hflip` 照挂（"配置写着关、画面其实开着"，和"界面动了画面没动"是同一种病的两面）。
    走 JSON 的话 `false` 是真布尔，这一条兜的是手改配置与非 JSON 来源；认不出的串
    落回"关"而不是"开"，是因为一个说不清的值不该往链上加一档滤镜。
    """
    if isinstance(v, str):
        return v.strip().lower() in _ON_STRINGS
    return bool(v)


def normalize(key: str, v: Any) -> Any:
    """配置里该存的 Python 值：数字夹紧、旋转取模、枚举留在表内、比例收成表里的串。

    为什么夹紧必须在这里：内核**不会**替我们夹（越界值整条拒收、回报停在旧值），
    而 `video-pan-x` 又相反——内核照收 ±2.0，但我们不想开放那么大。范围只有在这张表里
    说过一次，界面、引擎、启动参数才不会各说一遍。
    """
    s = _spec(key)
    if s is None:
        return v
    if isinstance(s, Flag):
        return as_flag(v)
    if isinstance(s, Num):
        return s.clamp(v)
    if isinstance(s, Choice):
        vals = [o[0] for o in s.options]
        cand = str(v).strip()
        if s is ROTATE:
            try:
                return str(int(float(cand)) % 360)      # 实测 360/-90 会被整条拒收
            except (TypeError, ValueError):
                return s.default
        # 比例类（画面比例、裁剪）按**数值**认档，不是按字符串相等。
        # 这里比较的是 `CROP.spec` 而不是 `CROP`：CROP 是 Filter 实例，表里存的是它带的
        # Choice，写成 `s is CROP` 永远为假——实测"16/9"会被静默降成"不裁剪"。
        if s is ASPECT or s is CROP.spec:
            # 比例写法不统一（"16/9"、"abc"、"2.35"）时按数值认，认不出就退回该键的默认档。
            # 容差 5e-4 是量出来的：表里最接近的两档（21:9=2.3333 与 2.35:1）差 0.0167，
            # 再大就会把 2.35 认成 21:9；再小则手打的 "1.7777" 认不回 16:9（差 7.8e-5）。
            for allow in vals:
                if allow == cand or abs(ratio_float(allow) - ratio_float(cand)) < 5e-4:
                    return allow
            return s.default
        return cand if cand in vals else s.default
    return v


def mpv_value(key: str, v: Any) -> Any:
    """配置值 → 发往内核的形式（只有几何类有"发往内核"这件事）。"""
    s = _spec(key)
    if isinstance(s, Flag):
        return "yes" if as_flag(v) else "no"
    if isinstance(s, Num):
        return s.clamp(v)
    if s is ROTATE:
        return int(normalize(key, v))
    if s is ASPECT:
        r = ratio_float(v)
        # 自动档发 "no"（回报 -2.0）。发 -1 内核照收但每次打一条 deprecation 警告，
        # 而这条警告会盖掉"日志里有没有真问题"这个判据——自检看的是同一个文件。
        return ASPECT_AUTO if r < 0 else _tidy(r)
    return v


def _tidy(x: float) -> Any:
    """浮点写成内核回报的样子：整值不带小数点，比例最多 6 位（16/9→1.777778）。"""
    r = round(float(x), 6)
    return int(r) if r == int(r) else r


def same_value(key: str, want: Any, got: Any) -> bool:
    """内核回报与请求是不是同一件事（浮点带容差、布尔看 yes/true、旋转按取模比）。

    布尔那条必须加括号：`a in (t) == b` 在 Python 里是**链式比较**
    （`(a in t) and (t == b)`），右边永远为假——实测漏一次括号，
    keepaspect 改成功了也会被报成"内核没收下"。
    """
    s = _spec(key)
    if isinstance(s, Flag):
        return (str(got).strip().lower() in ("true", "yes", "1")) == as_flag(want)
    if s is ASPECT:
        # 自动档有两个合法形状：下发写 "no"，内核回报 -2.0（老版写 -1）。
        # 所以这里比的是"是不是都落在自动侧"，而不是比数值——直接按 -1 比 -2 会永远判假，
        # 于是"复位画面比例"每次都要红一次。
        # 回报侧还要认 `"no"` 这一串：`MpvPlayer.prop()` 在订阅缓存还没填上时回的是
        # **配置默认**，也就是我们发出去的那串 "no"（`cycle_transform` 走的就是这条路）。
        # 只认负数会把"其实已经是自动档"读成"内核没收下"，于是每按一次快捷键都红一条。
        want_auto = ratio_float(want) < 0
        if want_auto and isinstance(got, str) and got.strip().lower() in ("no", "none", "auto"):
            return True
        try:
            g = float(got)
        except (TypeError, ValueError):
            return False
        return g < 0 if want_auto else abs(g - ratio_float(want)) < 1e-4
    if s is ROTATE:
        try:
            return int(float(got)) % 360 == int(float(want)) % 360
        except (TypeError, ValueError):
            return False
    if isinstance(s, Num):
        try:
            return abs(float(got) - float(want)) < 1e-4
        except (TypeError, ValueError):
            return False
    return str(got) == str(want)


# ==================== 滤镜链 ====================

def active_filters(cfg) -> list[Filter]:
    """链上现在该有哪几档（按契约顺序，取值 0/关/none 的不算）。"""
    out = []
    for f in FILTERS:
        v = cfg.get(f.spec.cfg, f.spec.default) if hasattr(cfg, "get") else f.spec.default
        if f.expr(v):
            out.append(f)
    return out


def chain_parts(cfg) -> list[str]:
    out = []
    for f in FILTERS:
        v = cfg.get(f.spec.cfg, f.spec.default) if hasattr(cfg, "get") else f.spec.default
        e = f.expr(v)
        if e:
            out.append(e)
    return out


def build_chain(cfg) -> str:
    """整条 `vf` 的值；一个滤镜都没有时返回空串（内核侧＝清空链，实测 set '' → []）。

    `format=yuv420p` 只在链里真有 needs_sw 的滤镜时才补，且必须在最前——
    实测放后面（`unsharp,format=…`）内核照样 "Impossible to convert"。
    """
    parts = chain_parts(cfg)
    if not parts:
        return ""
    if any(f.needs_sw for f in active_filters(cfg)):
        return ",".join([SW_PREFIX] + parts)
    return ",".join(parts)


def chain_names(cfg) -> list[str]:
    return [f.key for f in active_filters(cfg)]


# 内核日志里"这个滤镜其实没跑起来"的原话（本机实测到的写法，逐条来自探针）
DISABLE_MARKS = ("Disabling filter", "failed to configure", "Failed to configure",
                 "Impossible to convert", "Creating filter", "AVOption", "No such filter",
                 # 链串被 ',' 劈开时内核说的是这句（实测 crop=…min(iw,ih… 触发）
                 "isn't supported", "is not supported", "Option vf-set",
                 # 整条带逗号的链走 append 时的原话（probe-transform12.log），
                 # 有人把下发路径改回 append 就得在这里漏出来，别只靠源码形状判据。
                 "Option vf-append", "-append takes only 1 item")


def filter_failures(log_text: str) -> list[str]:
    """从内核日志里挑出"滤镜被禁用／创建失败"的行。

    为什么必须有这一条：`vf` 列表里名字照在、`enabled` 照是 True，画面却一动不动
    （实测 unsharp 单独发下去就是这样）。外壳不看日志就只能报"已开启锐化"，
    而真相是内核把那一条扔了。这是本模块对"列表≠事实"唯一的防线。
    """
    out: list[str] = []
    for line in str(log_text or "").splitlines():
        if any(m in line for m in DISABLE_MARKS):
            t = line.strip()
            if "[e]" in t:                    # 只留人话那截，日志前缀对界面没用
                t = t.split("]", 2)[-1].strip()
            if t not in out:
                out.append(t)
    return out


def startup_args(cfg) -> list[str]:
    """启动参数：几何全部下发，链里真有滤镜时才带 --vf。

    为什么启动也要走这张表：只靠运行时补发的话，第一次播放前那几秒画面是"没变换"的样子，
    而且退出后配置里的值与实际永远差一截（和 tweaks 那族同一个理由）。
    """
    out: list[str] = []
    for s in PROPS:
        out.append(f"--{s.prop}={mpv_value(s.cfg, cfg.get(s.cfg, s.default))}")
    chain = build_chain(cfg)
    if chain:
        out.append(f"--vf={chain}")
    return out


# ==================== 界面要的小工具 ====================

NUMS: tuple[Num, ...] = tuple(s for s in PROPS if isinstance(s, Num))
CHOICES: tuple[Choice, ...] = tuple(s for s in PROPS if isinstance(s, Choice))
FLAGS: tuple[Flag, ...] = tuple(s for s in PROPS if isinstance(s, Flag))
FILTER_NUMS: tuple[Num, ...] = tuple(f.spec for f in FILTERS if isinstance(f.spec, Num))
FILTER_FLAGS: tuple[Flag, ...] = tuple(f.spec for f in FILTERS if isinstance(f.spec, Flag))
FILTER_CHOICES: tuple[Choice, ...] = tuple(f.spec for f in FILTERS if isinstance(f.spec, Choice))

# panscan 内核是 0..1，给人看的是百分比——写在这儿而不是 Num.fmt，
# 因为 fmt 拿到的还是 0..1，乘 100 得在单位换算这一层做。
_PCT_KEYS = {"panscan"}


def label(key: str) -> str:
    s = _spec(key)
    return getattr(s, "label", str(key))


def value_text(key: str, v: Any) -> str:
    """OSD / 数值标签用的一行字（枚举给中文，布尔给开关，比例给百分数）。"""
    s = _spec(key)
    if key in _PCT_KEYS and isinstance(s, Num):
        try:
            return f"{float(v) * 100:.0f}%"
        except (TypeError, ValueError):
            return s.text(s.default)
    if isinstance(s, Num):
        return s.text(v)
    if isinstance(s, Flag):
        return "开" if as_flag(v) else "关"
    if isinstance(s, Choice):
        for val, text in s.options:
            if val == normalize(key, v):
                return text
        return str(v)
    return str(v)


def cycle(key: str, cur: Any, forward: bool = True) -> Any:
    """枚举循环一格 / 数值走一步 / 布尔翻转（快捷键都走这里，别再各抄一份步进）。"""
    s = _spec(key)
    if isinstance(s, Choice):
        vals = [o[0] for o in s.options]
        try:
            i = vals.index(normalize(key, cur))
        except ValueError:
            i = vals.index(s.default) if s.default in vals else 0
        return vals[(i + (1 if forward else -1)) % len(vals)]
    if isinstance(s, Num):
        step = s.step or 1
        return s.clamp(s.clamp(cur) + (step if forward else -step))
    if isinstance(s, Flag):
        return not as_flag(cur)
    return cur


def is_known(key: str) -> bool:
    """这一项在不在表里——配置键与 mpv 属性名**都算**（别名查不到就会静默放行）。"""
    return key in _BY_KEY
