"""可调项（字幕样式 / 画面均衡 / HDR / 声音）的单一事实表。

为什么单独一个模块：这些数字原本会散在四处——UI 滑块的 min/max、引擎的夹紧、
启动参数、自测的期望值。四处各写一遍的结局是必然对不上：界面上能拖到 130，
内核只收到 100，用户看到的是"滑块没到底画面就没变了"。这里把
`属性名 / 配置键 / 取值范围 / 复位值 / 标签 / 单位` 收在一张表里，
UI 从它生成控件、引擎按它夹紧与持久化、启动参数由它拼出来、自测拿它当被检清单。

**范围与枚举以本机这版 mpv 实测为准**（v0.41.0-1101-g5d85ba5fb，测法＝set 之后
立刻 get 回来比对，读不回同一个值就当不支持）：
  - brightness / contrast / gamma / saturation / hue：-100..100
    （注意：**越界值是整条被内核拒收、回报停在发之前的旧值**，实测直接 set 130
      回报仍是 0.0。所以夹紧必须由这张表来做——指望"内核自己会夹到 100"会得到
      "界面动了、画面没动"。范围比内核窄的项同理：sub-scale 我们只开放到 3.0，
      直接发 50 内核是真会收下的。）
  - tone-mapping 接受：auto clip gamma linear reinhard hable mobius spline
    （这一版**不**接受 butterlight / st2094 / bt2446a——set 之后回报停在 auto。
      少测这一步，菜单里就会有一档"点了没反应"。）
  - gamut-mapping-mode 接受：auto perceptual relative saturation（没有 preserve_hue / trim）
  - sub-align-y 接受：top center bottom（absolute-bottom 被拒）
  - sub-color / sub-back-color 的回报串是 **#AARRGGBB**（透明度在前），
    所以本模块统一把 6 位补成 FF 再发，比对时也比 RGB+AA 而不是比字符串长度。
  - volume 上限 200：靠启动参数 `--volume-max=200`。实测直接发 250 内核拒收
    （回报停在 100），走 set_volume 才会夹到 200——所以这里的范围是承重件。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MPV_MEASURED_ON = "v0.41.0-1101-g5d85ba5fb"


@dataclass(frozen=True)
class Num:
    """一个连续可调项。prop 是 mpv 属性名（也是外壳读订阅缓存的键）。"""
    prop: str
    cfg: str
    lo: float
    hi: float
    step: float
    default: float
    label: str
    unit: str = ""
    fmt: str = "%.0f"

    def clamp(self, v: Any) -> float:
        try:
            x = float(v)
        except (TypeError, ValueError):
            return float(self.default)
        return max(self.lo, min(self.hi, x))

    def text(self, v: Any) -> str:
        """给 OSD / 数值标签用的一行字：0 值写成"0"而不是"0.00x"这种噪声。"""
        try:
            x = float(v)
        except (TypeError, ValueError):
            x = float(self.default)
        s = self.fmt % x
        if s.endswith(".0"):
            s = s[:-2]
        return f"{s}{self.unit}"


@dataclass(frozen=True)
class Flag:
    """布尔项（mpv 用 yes/no，配置里存 Python bool）。"""
    prop: str
    cfg: str
    default: bool
    label: str


@dataclass(frozen=True)
class Choice:
    """枚举项。options 是 (发往 mpv 的值, 界面文字)。"""
    prop: str
    cfg: str
    default: str
    label: str
    options: tuple[tuple[str, str], ...]


# ---- 画面均衡器：五根滑轨 + 一键复位 --------------------------------------
EQ: tuple[Num, ...] = (
    Num("brightness", "brightness", -100, 100, 1, 0, "亮度", "", "%.0f"),
    Num("contrast", "contrast", -100, 100, 1, 0, "对比度", "", "%.0f"),
    Num("gamma", "gamma", -100, 100, 1, 0, "灰度", "", "%.0f"),
    Num("saturation", "saturation", -100, 100, 1, 0, "饱和度", "", "%.0f"),
    Num("hue", "hue", -100, 100, 1, 0, "色相", "", "%.0f"),
)

# ---- 字幕：数值类 --------------------------------------------------------
# 这里**没有** sub-margin-y：实测这一版 mpv 在没下发该项时按视频尺寸自己算边距
# （640×360 的样片上回报 19/34），一旦启动参数写死一个像素值，4K 片上的留白就会
# 薄得不讲理。位置交给 sub-align-x/y（上/中/下）来调，留白留给设置页按"倍率"做。
SUB_NUM: tuple[Num, ...] = (
    Num("sub-scale", "sub_scale", 0.3, 3.0, 0.05, 1.0, "字幕大小", "x", "%.2f"),
    Num("sub-delay", "sub_delay", -60.0, 60.0, 0.1, 0.0, "字幕延迟", "s", "%.1f"),
    Num("sub-border-size", "sub_border_size", 0.0, 8.0, 0.5, 3.0, "描边粗细", "px", "%.1f"),
    Num("sub-shadow-offset", "sub_shadow_offset", 0.0, 8.0, 0.5, 0.0, "阴影", "px", "%.1f"),
)

# ---- 声音：数值类（音量另说，控制条已有） --------------------------------
AUDIO_NUM: tuple[Num, ...] = (
    Num("audio-delay", "audio_delay", -60.0, 60.0, 0.1, 0.0, "声音延迟", "s", "%.1f"),
)

SUB_FLAG: tuple[Flag, ...] = (
    Flag("sub-bold", "sub_bold", True, "字幕加粗"),
    Flag("sub-visibility", "sub_visibility", True, "显示字幕"),
    Flag("sub-use-margins", "sub_use_margins", True, "避开边框"),
)

# 位置做成一个 3x3 里常用的四档：字幕几乎不会"垂直居中"盖在画面上，
# 但看翻译时确实有人要——留两档横向 + 三档纵向分开选，比 9 宫格省一半控件。
SUB_ALIGN_X = Choice("sub-align-x", "sub_align_x", "center", "水平位置",
                     (("left", "左"), ("center", "中"), ("right", "右")))
SUB_ALIGN_Y = Choice("sub-align-y", "sub_align_y", "bottom", "垂直位置",
                     (("top", "上"), ("center", "中"), ("bottom", "下")))

# 颜色：mpv 的字符串是 #AARRGGBB（透明度在前）。预设值直接按这个顺序写死，
# 界面文字里说明"半透"之类的效果，避免用户以为 FF 是"很红"。
SUB_COLOR = Choice("sub-color", "sub_color", "#FFFFFFFF", "字幕颜色",
                   (("#FFFFFFFF", "白"), ("#FFF5EBD6", "米白"), ("#FFFFF700", "柠檬黄"),
                    ("#FF00FFCC", "青"), ("#FFFF6A00", "橙")))
SUB_BACK_COLOR = Choice("sub-back-color", "sub_back_color", "#AF000000", "字幕底色",
                        (("#00000000", "无"), ("#80000000", "半黑"),
                         ("#AF000000", "黑（默认）"), ("#FF000000", "纯黑"),
                         ("#803B3B3B", "灰")))

TONE_MAPPING = Choice("tone-mapping", "tone_mapping", "auto", "HDR 色调映射",
                      (("auto", "自动"), ("spline", "spline（推荐）"), ("mobius", "mobius"),
                       ("hable", "hable"), ("reinhard", "reinhard"), ("gamma", "gamma"),
                       ("linear", "linear"), ("clip", "clip（直接截断）")))
GAMUT_MAPPING = Choice("gamut-mapping-mode", "gamut_mapping", "auto", "色域映射",
                       (("auto", "自动"), ("perceptual", "感知优先"),
                        ("relative", "相对色彩"), ("saturation", "保留饱和度")))
TARGET_PEAK = Choice("target-peak", "target_peak", "auto", "目标峰值亮度",
                     (("auto", "自动（跟随显示器）"), ("100", "100 nit"),
                      ("203", "203 nit"), ("1000", "1000 nit")))

# ---- 画面平滑（本机实测四项都是可读可写、也可订阅的普通属性：
#      `set_property video-sync …` 立刻改，`options/video-sync` 跟着变；
#      所以它们归这张**热切**表，不归 perf.py 那张"只能进启动参数"的表）----
# 同步方式决定"帧按谁的时钟出"：audio 是内核默认；插帧要有显示时钟才成立，
# 所以界面上把"开插帧"与"切到跟随显示器"绑成一次动作（见 tweak_panel._on_flag）。
MOTION_SYNC = Choice("video-sync", "video_sync", "audio", "画面同步",
                     (("audio", "跟随音频（默认，最省）"),
                      ("display-resample", "跟随显示器·重采样音频"),
                      ("display-tempo", "跟随显示器·微调速度（不变调）"),
                      ("display-vdrop", "跟随显示器·丢帧优先")))
INTERPOLATION = Flag("interpolation", "interpolation", False, "插帧（运动补偿）")
# 插帧只在两帧之间补出一帧，补帧的权重怎么取由 tscale 定：oversample 直接取最近帧
# （最省，会轻微重影），spline36/lanczos 更锐利也更能看出肥皂剧效应。
TSCALE = Choice("tscale", "tscale", "mitchell", "插帧混合",
                (("oversample", "Oversample（最省）"),
                 ("mitchell", "Mitchell（平滑）"),
                 ("spline36", "Spline36（平衡）"),
                 ("lanczos", "Lanczos（锐利）")))
# 去隔行默认 auto：只对片源里标了隔行的帧动手，逐行片源一点都不变；
# 关掉它会让老 DVD/电视转录的片出现梳齿。
DEINTERLACE = Choice("deinterlace", "deinterlace", "auto", "去隔行",
                     (("no", "关闭"), ("auto", "自动（只处理隔行片源）"),
                      ("yes", "强制去隔行")))

NUMS: tuple[Num, ...] = EQ + SUB_NUM + AUDIO_NUM
CHOICES: tuple[Choice, ...] = (SUB_ALIGN_X, SUB_ALIGN_Y, SUB_COLOR, SUB_BACK_COLOR,
                               TONE_MAPPING, GAMUT_MAPPING, TARGET_PEAK,
                               MOTION_SYNC, TSCALE, DEINTERLACE)
FLAGS: tuple[Flag, ...] = SUB_FLAG + (INTERPOLATION,)

_BY_PROP: dict[str, Any] = {}
for _n in NUMS:
    _BY_PROP[_n.prop] = _n
for _c in CHOICES:
    _BY_PROP[_c.prop] = _c
for _f in FLAGS:
    _BY_PROP[_f.prop] = _f

# 音量单独处理：它不是"可调项表"的一员（控制条已有滑块），但上限要从内核问，
# 不能拿界面上的 200 当事实——启动参数改了上限，滑块就该跟着变。
VOLUME_SPEC = Num("volume", "volume", 0, 200, 1, 100, "音量", "%", "%.0f")


def spec(prop: str):
    """按 mpv 属性名取表项；不在表里返回 None（调用方据此拒绝发命令）。"""
    return _BY_PROP.get(prop)


def cfg_keys() -> dict[str, str]:
    """属性名 → 配置键。UI 只需要这一个映射就能把控件绑到 cfg。"""
    return {p: s.cfg for p, s in _BY_PROP.items()}


def defaults() -> dict[str, Any]:
    """配置默认值（键 → 值）。config.DEFAULTS 从这里取，避免两处各写一份。"""
    out: dict[str, Any] = {}
    for n in NUMS:
        out[n.cfg] = n.default
    for c in CHOICES:
        out[c.cfg] = c.default
    for f in FLAGS:
        out[f.cfg] = f.default
    return out


def mpv_value(item: Any, v: Any) -> Any:
    """把配置值转成发往 mpv 的形式（bool→yes/no，数字夹紧，颜色补齐 AA）。"""
    if isinstance(item, Flag):
        return "yes" if bool(v) else "no"
    if isinstance(item, Num):
        return item.clamp(v)
    if isinstance(item, Choice) and "color" in item.prop:
        return norm_color(v)
    return v


def norm_color(s: Any) -> str:
    """#RGB / #RRGGBB / #AARRGGBB → #AARRGGBB（mpv 的回报形式）。

    6 位补 FF（不透明）。8 位原样保留——这一版的顺序是**透明度在前**，
    和 CSS 的 #RRGGBBAA 不一样，所以别在这里"顺手"调换。
    """
    t = str(s or "").strip().lstrip("#").upper()
    if len(t) == 3:
        t = "".join(ch * 2 for ch in t)
    if len(t) == 6:
        t = "FF" + t
    if len(t) == 8 and all(c in "0123456789ABCDEF" for c in t):
        return "#" + t
    return "#FFFFFFFF"


def same_value(item: Any, want: Any, got: Any) -> bool:
    """内核回报的值是否就是请求的那个（比"字符串相等"宽容：100 vs 100.0、颜色大小写）。"""
    if isinstance(item, Flag):
        return bool(str(got) in ("true", "True", "yes")) == bool(want)
    if isinstance(item, Num):
        try:
            return abs(float(got) - float(want)) < 1e-4
        except (TypeError, ValueError):
            return False
    if isinstance(item, Choice) and "color" in item.prop:
        return norm_color(want).upper() == norm_color(got).upper()
    if isinstance(item, Choice):
        # deinterlace 这一族内核回报的是**布尔**（no→False、yes→True），
        # 而 auto 仍是字符串；三态混着来，比对与显示都要先归一。
        return choice_value(want) == choice_value(got)
    return str(want) == str(got)


def startup_args(cfg) -> list[str]:
    """由表拼出启动参数。

    为什么启动也要走这张表：只靠"运行时 set_property"的话，第一次播放前的那几秒
    （以及任何没起内核就退出的人）配置和实际不一致；而 --sub-color 这类参数
    在 set_property 之后又被下次启动的默认值盖掉，用户会觉得"设置没存住"。
    """
    out: list[str] = []
    for n in NUMS:
        out.append(f"--{n.prop}={n.clamp(cfg.get(n.cfg, n.default))}")
    for f in FLAGS:
        out.append(f"--{f.prop}={'yes' if cfg.get(f.cfg, f.default) else 'no'}")
    for c in CHOICES:
        v = cfg.get(c.cfg, c.default)
        if "color" in c.prop:
            v = norm_color(v)
        out.append(f"--{c.prop}={v}")
    return out


def label_of(prop: str) -> str:
    s = _BY_PROP.get(prop)
    return getattr(s, "label", prop)


def choice_value(v: Any) -> str:
    """枚举值 → 可比对的字符串。内核把 no/yes 回报成 JSON 布尔（deinterlace 实测如此），
    auto 仍是字符串；这里把布尔折回 yes/no，别让 False 与 "no" 变成两个档。"""
    if v is True:
        return "yes"
    if v is False:
        return "no"
    return str(v)


def option_text(item: Choice, value: Any) -> str:
    """枚举值 → 界面文字（找不到就原样回显，别把未知值谎报成第 0 项）。"""
    want = choice_value(value)
    for v, text in item.options:
        if choice_value(v) == want:
            return text
    return want


def cycle_option(item: Choice, cur: Any, forward: bool = True) -> str:
    """在枚举里循环一格（快捷键用）。当前值不在表里时从默认档起步，
    而不是停在"未知值"上——否则连按同一个键会永远看不到画面变化。
    比对走 choice_value：deinterlace 的当前值从内核回来是布尔 False，
    用 str 比会永远认不出它已经是「关闭」，按一下反而跳回默认档。"""
    vals = [v for v, _ in item.options]
    want = choice_value(cur)
    matches = [v for v in vals if choice_value(v) == want]
    if matches:
        i = vals.index(matches[0])
    else:
        i = vals.index(item.default) if item.default in vals else 0
    return vals[(i + (1 if forward else -1)) % len(vals)]
