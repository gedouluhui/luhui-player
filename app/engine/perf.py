"""启动期性能档位（硬解 / GPU API / 缩放预设 / 音量上限）的单一事实表。

为什么和 `tweaks.py` 分成两张表：那边那族能热切（`set_property` 立刻生效，界面画的是
内核回报的值）；**这一族不能**。本机 v0.41.0-1101 实测（docs/design.md §3 第 7 条）：
  - `get_property hw-decode` / `current-decoder` / `current-hwdec` → property not found，
    这三个常被引用的属性在这版内核里根本不存在；
  - `set_option hwdec d3d11va` → invalid parameter；
  - `loadfile … replace` 之后 `options/hwdec` 仍是启动那次的值 —— 换片不会重新解析。
所以这一族只有两个出口：拼进启动参数，以及在设置页里选完由用户重启。界面上要显示
"眼下真正在用什么"，读的是 `hwdec-current` / `current-gpu-context`（内核回报的真值），
而不是配置里那个"我想要的"。

白名单是**承重件**，不是装饰，这条也是实测来的：
  `--hwdec=bogus-value` 内核照样启动（`options/hwdec` 原样留着这个串），
  但 `hwdec-current` 变成 `no` —— 也就是**静默退回软件解码**。用户看不出任何异常，
  只会发现"风扇怎么变响了、4K 怎么卡了"。所以配置里的值必须先过这张表，
  过不了就回退到默认档并把这件事报出来，而不是原样递给内核。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MPV_MEASURED_ON = "v0.41.0-1101-g5d85ba5fb"

# 本机 `mpv --hwdec=help` 的原样输出（25 档，含 copy 变体）
HWDEC_ALL: tuple[str, ...] = (
    "amf", "amf-copy", "auto", "auto-copy", "auto-copy-safe", "auto-copy-unsafe", "auto-safe",
    "auto-unsafe", "cuda", "cuda-copy", "d3d11va", "d3d11va-copy", "d3d12va", "d3d12va-copy",
    "dxva2", "dxva2-copy", "no", "nvdec", "nvdec-copy", "qsv", "qsv-copy", "vaapi",
    "vaapi-copy", "vulkan", "vulkan-copy",
)

# 界面只放这台机器（RTX 3070 Ti + Windows）用得上的档：全列 25 条的话，
# "amf" 和 "amf-copy" 的差别没人看得懂，多出来的选项只是多一个选错的机会。
# 软解那一档留着：硬解花屏时"能一条命令切到软解"是唯一的现场排查手段。
HWDEC_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto-safe", "自动安全档（推荐）"),
    ("d3d11va", "D3D11 零拷贝硬解"),
    ("d3d11va-copy", "D3D11 硬解（经内存拷贝）"),
    ("nvdec", "NVDEC 硬解"),
    ("nvdec-copy", "NVDEC 硬解（经内存拷贝）"),
    ("dxva2", "DXVA2 硬解（老驱动兼容档）"),
    ("d3d12va", "D3D12 硬解"),
    ("cuda", "CUDA 硬解"),
    ("vulkan", "Vulkan 硬解"),
    ("no", "强制软件解码（排查花屏用）"),
)

# 本机 `mpv --gpu-api=help`：auto / d3d11 / vulkan / opengl，
# 对应 context 为 auto / d3d11 / winvk / angle（实测设 opengl 后
# current-gpu-context 就从 d3d11 变成 angle）。
# 注意 d3d11 这条路才接得上 d3d11va 的零拷贝：换 API 等于换输出通路。
GPU_API_CHOICES: tuple[tuple[str, str], ...] = (
    ("d3d11", "D3D11（默认，硬解零拷贝）"),
    ("auto", "自动"),
    ("opengl", "OpenGL（ANGLE）"),
    ("vulkan", "Vulkan（WinVulkan）"),
)


@dataclass(frozen=True)
class Preset:
    """缩放/去带的一组搭配。三项都是启动参数（profile 这一版设了读不回，实测）。"""
    pid: str
    label: str
    scale: str
    dscale: str
    deband: bool

    def args(self) -> list[str]:
        return [f"--scale={self.scale}", f"--dscale={self.dscale}",
                f"--deband={'yes' if self.deband else 'no'}"]


QUALITY_PRESETS: tuple[Preset, ...] = (
    Preset("fast", "省资源（核显/电池时用）", "bilinear", "nearest", False),
    Preset("balanced", "均衡（默认）", "lanczos", "mitchell", False),
    Preset("high", "高质量缩放 + 去带", "ewa_lanczossharp", "catmull_rom", True),
)

# 音量上限：内核 `--volume-max` 收的是整数。默认 200 是第二轮定的口径，
# 界面滑块量程必须跟着它走（写死 200 的话，用户改成 110 后滑块还能拖到 200，
# 拖过去就是"设了没生效"）。
VOLUME_MAX_CHOICES: tuple[tuple[int, str], ...] = (
    (100, "100%（不放大，最保真）"),
    (150, "150%"),
    (200, "200%（默认）"),
    (300, "300%（易爆音，谨慎）"),
)

DEFAULTS: dict[str, Any] = {
    "hwdec": "auto-safe",
    "gpu_api": "d3d11",
    "quality_preset": "balanced",
    "volume_max": 200,
}

# 配置键 → 白名单。设置页按这张表生成下拉，校验也走它，别在 UI 里另抄一份。
_TABLES: dict[str, tuple[tuple[Any, str], ...]] = {
    "hwdec": HWDEC_CHOICES,
    "gpu_api": GPU_API_CHOICES,
    "quality_preset": tuple((p.pid, p.label) for p in QUALITY_PRESETS),
    "volume_max": VOLUME_MAX_CHOICES,
}
_DEFAULTS_FULL: dict[str, tuple[str, ...]] = {
    "hwdec": HWDEC_ALL,
    "gpu_api": tuple(v for v, _ in GPU_API_CHOICES),
    "quality_preset": tuple(p.pid for p in QUALITY_PRESETS),
    "volume_max": tuple(str(v) for v, _ in VOLUME_MAX_CHOICES),
}


def options_for(key: str) -> tuple[tuple[Any, str], ...]:
    """设置页用：某一项的可选值与界面文字。"""
    return _TABLES.get(key, ())


def label(key: str, value: Any) -> str:
    for v, text in options_for(key):
        if _same(v, value):
            return text
    return str(value)


def preset(pid: str) -> Preset:
    for p in QUALITY_PRESETS:
        if p.pid == str(pid):
            return p
    return QUALITY_PRESETS[1]


def valid(key: str, value: Any) -> bool:
    """值能不能交给内核。以**全量白名单**为准（不是界面上那几档）：
    手填配置里写 d3d12va 是合法诉求，界面没列它不代表该拒。"""
    allowed = _DEFAULTS_FULL.get(key)
    if allowed is None:
        return False
    return any(_same(a, value) for a in allowed)


def _same(a: Any, b: Any) -> bool:
    if str(a) == str(b):
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def resolve(cfg) -> dict[str, Any]:
    """四项的**校验后**取值；非法值回退默认档（不原样递给内核去静默降级）。"""
    out: dict[str, Any] = {}
    for key, dflt in DEFAULTS.items():
        v = cfg.get(key, dflt) if hasattr(cfg, "get") else dflt
        out[key] = v if valid(key, v) else dflt
    out["volume_max"] = int(out["volume_max"])
    return out


def warnings(cfg) -> list[str]:
    """配置里哪些值是"回退过的"，给引擎报出去。

    措辞统一走"不支持的取值"而不是"没有这一档"：volume_max=999 不是内核缺档，是越过了
    我们给的量程，用户看到"内核没有这一档"会去翻 mpv 文档，白翻。
    """
    notes = []
    for key, dflt in DEFAULTS.items():
        v = cfg.get(key, dflt) if hasattr(cfg, "get") else dflt
        if not valid(key, v):
            notes.append(f"{key}={v!r} 不是本机内核支持的取值，已按 {dflt} 启动")
    return notes


def startup_args(cfg) -> tuple[list[str], list[str]]:
    """返回 (启动参数, 回退说明)。"""
    r = resolve(cfg)
    args = [f"--hwdec={r['hwdec']}", f"--gpu-api={r['gpu_api']}",
            f"--volume-max={r['volume_max']}", *preset(r["quality_preset"]).args()]
    return args, warnings(cfg)


def hwdec_state(raw: Any) -> tuple[bool, str]:
    """`hwdec-current` 的回报 → (是否硬件解码, 给人看的一行)。

    实测三种形状都见过：`'d3d11va'`（在用）、`'no'`（软件解码，含非法档位被
    内核静默降级后的样子）、订阅还没来时是 None。所以判"在不在硬解"必须排除
    no/none/空串，而不是 `if raw:` —— `'no'` 是真值字符串，会误报成"正在硬解"。
    """
    t = str(raw or "").strip()
    if not t or t.lower() in ("no", "none"):
        return False, "软件解码"
    return True, t

