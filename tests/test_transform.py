"""画面变换与画质增强的真机验收 + 证伪（`app/engine/transform.py` 那张表的落地对照）。

用法：
    python tests/test_transform.py            # 验收：全部判据必须 PASS
    python tests/test_transform.py --falsify  # 证伪：喂已知坏样本／摘掉产品承重件，判据必须点名

退出码：0 全绿；1 有判据没过（或坏样本没抓到）；2 连测量对象都没有（缺内核/缺样片/缺 numpy）；
3 **本轮有判废**（内核起不来、截图这一轮不出帧）——逐条点名，其余照常记账，一趟环境抖动
不带走整张台账。优先级写在 `final_exit_code`：同轮只要有一条真红，rc 仍是 1，判废不许掩盖失败。

这一份的承重结构是"判据分三家"，各能证的东西不一样，别把它们混成一锅：

1. **离线**（不碰内核）：那张表自己的形状——夹紧、取模、认档、链顺序、`format=` 前缀规则、
   启动参数、复位覆盖。跑得快、每次都跑得起来，钉的是"写法回归"。
2. **真机·video 截图**（滤镜链的输出）：镜像／裁剪／旋转／比例／锐化／降噪／去带。
3. **真机·window 截图**（mpv 嵌进一块**固定尺寸**的窗口）：keepaspect 与 panscan 的黑边。
   为什么非要有第三家：实测内核自建窗口会跟着片子比例自动改尺寸（`probe-transform8.log`、
   `probe-transform9.log`），于是"保持比例"在那种框里永远没有可比的边框、panscan 永远没有
   该被裁的东西——`panscan=1.0` 的 window 截图均值与 0.0 一模一样（35.06→35.06）。
   换成我们给的固定 1280×720 窗口才量得出来（`probe-transform10.log`：
   keep=yes/4:3 ⇒ 整列近黑占比 1.00 的左右边条；keep=no ⇒ 0.0055；panscan=1 ⇒ 边条消失）。

两条实测教训决定了判据长什么样：
  · **`vf` 列表不能当"滤镜在跑"的证据**：硬解帧上内核会把滤镜整条扔掉（"Impossible to
    convert…"＋"Disabling filter …"），而列表里名字照在、`enabled` 照是 True、同一帧
    逐像素差 0.00。所以凡判"这一档生效"都必须同时拿到**日志没有禁用行**与**像素动了**。
  · **截图模式与窗口状态会骗人**：mpv 最小化时 window 模式直接不出帧（video 模式照常出）。
    那是环境不给，不是产品坏 ⇒ 记 ENV 而不是 FAIL（见 `env_note`）。

判据与产品同口径：验收用的 `j_*` 谓词和真机跑次里喂给它们的是同一批观测值，`--falsify`
再往同一批谓词里喂已知坏样本（表与内核不一致／append 留残留／缺前缀／复位漏项／逗号写法…），
并当场把产品的承重件摘掉一次（前缀、夹紧、取模、set→append），要求同一判据立刻变红——
"判据写好了没人用"和"检测器恒真"是这一类测试仅有的两种活法，都不许留。
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from typing import Any        # noqa: F401  （判据签名里用；有 future annotations 也不许靠它兜）

if hasattr(sys.stdout, "reconfigure"):     # Windows 控制台默认 GBK，中文判据名会糊
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("GELU_DATA", os.path.abspath(os.path.join(HERE, ".selftest-data")))
sys.path.insert(0, ROOT)

from app import config as cfgmod            # noqa: E402
from app.engine import transform            # noqa: E402
from app.engine.transform import (ASPECT, CROP, DEBAND, DENOISE, FILTERS, KEEPASPECT,  # noqa: E402
                                  MIRROR_H, MIRROR_V, PANSCAN, PAN_X, PAN_Y, ROTATE,
                                  SHARPEN, SPECS, SW_PREFIX)

OUT = os.path.join(HERE, "out", "tf-frames")
SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
NOISY = os.path.join(HERE, "out", "tmp-noisy.mp4")
ENV_SAMPLE_MISSING = "缺样片"

results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool | None, str]] = []
env_notes: list[str] = []
# --falsify 要把离线半整套判据在"被摘掉承重件的代码"上重跑一遍（见 _offline_fails）。
# 那一趟是测量器，不是台账，所以要把逐条打印关掉——不然一轮变异刷出三十行 PASS，
# 看的人分不清哪条是本轮结论。
QUIET = False

try:                                # 像素判据要的三样：numpy／PIL／Qt
    import numpy as np
    from PIL import Image
    HAVE_PIL = True
except Exception as exc:            # pragma: no cover - 缺依赖时整头判废
    HAVE_PIL = False
    np = None
    Image = None
    PIL_ERROR = repr(exc)


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    if not QUIET:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""),
              flush=True)
    return bool(ok)


def env_note(name: str, detail: str) -> None:
    """判据的**前提**立不住时记一条：既不写 PASS 也不写 FAIL。"""
    env_notes.append(f"{name} → {detail}")
    if not QUIET:
        print(f"[ENV] {name}  —— {detail}", flush=True)


def final_exit_code(fails: list[str], env: list[str]) -> int:
    """收尾分流（纯函数）：真红 ⇒ 1，只有判废 ⇒ 3，都无 ⇒ 0。判废不许掩盖失败。"""
    if fails:
        return 1
    if env:
        return 3
    return 0


def f_case(name: str, hit: bool, detail: str = "") -> None:
    falsified.append((name, bool(hit), detail))


# ==================== 判据（纯函数：观测值进、结论出，验收与证伪共用同一把尺） ====================

def j_kernel_refused(prev: object, want: object, got: object) -> bool:
    """内核**整条拒收**的形状：发之前的旧值原样回报，而且旧值本来就不等于要发的值。

    为什么不能只比"回报==请求"：越界值被拒时回报是旧值，旧值可能恰好等于请求（比如把 0
    再发一遍），那时"没变"是正常的。只有 `got==prev 且 prev!=want` 才是拒收。
    """
    try:
        same_as_prev = (str(prev) == str(got)) or (float(prev) == float(got))
        same_as_want = (str(prev) == str(want)) or (float(prev) == float(want))
    except (TypeError, ValueError):
        same_as_prev = str(prev) == str(got)
        same_as_want = str(prev) == str(want)
    return same_as_prev and not same_as_want


def j_list_vs_truth(expect_on: bool, listed_has: bool, log_fails: list[str],
                    pixel_moved: bool) -> tuple[str, str]:
    """"这一档到底开没开"的三票合议：内核列表、内核日志、像素。

    返回值是**结论**而不是布尔，是为了让 --falsify 能钉住"只看列表就报生效"这种具体错法：
      LIST_LIE  列表说在、日志/像素说没跑 ⇒ 这正是硬解帧上滤镜被扔掉的样子；
      OFF_BUT_LISTED 请求关、列表里还挂着 ⇒ 链没清干净；
      ENV  请求开、列表没有、日志也干净 ⇒ 截图/内核这一轮没给证据；
      OK   三票一致。
    """
    if expect_on:
        if log_fails or (listed_has and not pixel_moved):
            return ("LIST_LIE", f"列表={listed_has} 日志禁用行={log_fails[:1]} 像素动={pixel_moved}")
        if not listed_has:
            return ("OFF_BUT_LISTED", "请求开但内核列表里没有这一档")
        return ("OK", f"列表在、日志干净、像素动了（移动={pixel_moved}）")
    if listed_has:
        return ("OFF_BUT_LISTED", f"请求关，链上却还挂着（列表={listed_has}）")
    return ("OK", "关着，列表也空")


def j_append_residue(expect: list[str], listed: list[str]) -> list[str]:
    """`append` 下发的残留：链上多出来的、或同一档出现两次以上的名字。"""
    extra = [n for n in listed if n not in expect]
    dup = [n for n in dict.fromkeys(listed) if listed.count(n) > max(1, expect.count(n))]
    return sorted(set(extra) | set(dup))


def j_reset(defaults: dict, readback: dict) -> list[str]:
    """整族复位的漏项：`readback` 里没有这项、或值不是默认值 ⇒ 点名。"""
    out = []
    for k, dv in defaults.items():
        if k not in readback:
            out.append(f"{k}(没回报)")
            continue
        got = readback[k]
        if not transform.same_value(k, dv, got):
            out.append(f"{k}(回报 {got!r} 默认 {dv!r})")
    return out


def j_ar(w: int, h: int, target: float, tol: float = 2e-3) -> tuple[bool, float]:
    """截图的宽高比对目标比例（比例判据的统一算法：按**比值**判，不按尺寸判）。"""
    if not w or not h:
        return (False, 0.0)
    r = w / h
    return (abs(r - target) <= tol * max(1.0, target), r)


def j_mirror(diff_flipped: float, diff_plain: float) -> tuple[bool, float]:
    """镜像的不可伪造证据：与"基线左右翻转"的差必须**远小于**与基线本身的差。

    只比"像素动了"不够——链上挂个任何滤镜都会动。这一条把"动的是镜像这件事"钉死。
    """
    if diff_plain <= 0:
        return (False, 0.0)
    return (diff_flipped < diff_plain / 5 and diff_flipped < 2.0, diff_plain / max(diff_flipped, 1e-9))


def j_sharpen(edge_before: float, edge_after: float) -> tuple[bool, float]:
    return (edge_after > edge_before * 1.3, edge_after / max(edge_before, 1e-9))


def j_denoise(edges: dict[float, float]) -> tuple[bool, str]:
    """降噪在**有噪声的片段**上必须单调压高频（0→7→10 档逐级下降，各步 ≥0.10）。

    单调性用三档而不是逐档：实测档 2 与档 0 只差 0.002（在噪声片段上那点强度本来就
    看不出差别），拿它当"必须下降"会是一条随机红的判据。
    """
    steps = [(0.0, 7.0), (7.0, 10.0)]
    drops = [edges[a] - edges[b] for a, b in steps]
    return (all(d >= 0.10 for d in drops),
            " ".join(f"{a}档={edges[a]:.3f}->{b}档={edges[b]:.3f}(-{d:.3f})"
                     for (a, b), d in zip(steps, drops)))


def j_deband(edge_before: float, edge_after: float, diff: float) -> tuple[bool, str]:
    """去带档 10：像素必须动（≥1.0），且高频被压下去（在噪声片段上实测 −0.65）。

    判据证的是"这一档真的在跑、方向是压高频"，**不是**"条带观感变好"——无参考图下
    后者量不出来，别把它写进结论。
    """
    return (diff >= 1.0 and edge_after < edge_before - 0.10,
            f"逐像素差={diff:.2f} 边缘能量 {edge_before:.3f}->{edge_after:.3f}")


def j_bars(dark_keep_yes: float, dark_keep_no: float, min_bar: float = 0.15) -> tuple[bool, str]:
    """keepaspect 的像素判据：同一个"比例≠窗口比例"的画面下，保持比例必须有黑边、
    不保持必须铺满（实测 4:3 塞进 16:9 的固定窗口：0.261 vs 0.0055）。"""
    return (dark_keep_yes >= dark_keep_no + min_bar,
            f"keep=yes 近黑占比={dark_keep_yes:.3f} keep=no={dark_keep_no:.3f}")


def j_panscan(dark_at_0: float, dark_at_1: float) -> tuple[bool, str]:
    """panscan 的像素判据：同一画面下放大充满必须把边条吃掉（实测 0.261→0.008）。"""
    return (dark_at_0 >= dark_at_1 + 0.10,
            f"panscan=0 近黑={dark_at_0:.3f} panscan=1 近黑={dark_at_1:.3f}")


def j_pan(dark_at_0: float, dark_at_1: float) -> tuple[bool, str]:
    """平移的像素判据：把画面整个推出框，窗口里必须出现大片近黑（实测均值 35.06→9.12）。"""
    return (dark_at_1 >= dark_at_0 + 0.10, f"pan=0 近黑={dark_at_0:.3f} pan=满 近黑={dark_at_1:.3f}")


def j_comma_gate(chain: str, n_parts: int) -> tuple[bool, str]:
    """逗号闸门：链串里的逗号**只能**当滤镜分隔符，一次都不能落在某一档的参数内部。

    本机实测（`probe-transform7.log`）：`crop=trunc(min(iw,ih*…))` 这种多参函数写法会把
    串劈开，内核回 "Option vf-set: 'ih' isn't supported."、`vf` 直接变空、画面一动不动。
    转义成 `\\,` 与整段加引号两条出路实测同样被拒，所以这里不是风格问题而是硬约束。
    """
    if not chain:
        return (True, "空链")
    parts = chain.split(",")
    inside = [p for p in parts[1:] if not p or p[0].isspace()]
    bad = []
    for p in parts:
        for fn in ("min(", "max(", "if(", "gte(", "lt("):
            if fn in p:
                bad.append(f"{p[:28]}…含 {fn}")
                break
    return (not bad and not inside, f"段数={len(parts)} 期望={n_parts} 多参函数={bad[:2]}")


def j_startup(args: list[str], chain: str) -> tuple[bool, str]:
    """启动参数：几何 6 条一条不缺，`--vf` 只在链非空时出现（空链发 --vf= 会让硬解帧白跑 CPU）。"""
    geo = [a for a in args if a.startswith("--") and "--vf" not in a]
    vfs = [a for a in args if a.startswith("--vf=")]
    want_geo = {f"--{p.prop}=" for p in transform.PROPS}
    miss = [k for k in want_geo if not any(a.startswith(k) for a in args)]
    ok = (not miss) and (len(vfs) == (1 if chain else 0))
    if chain:
        ok = ok and vfs[0] == f"--vf={chain}"
    return (ok, f"几何 {len(geo)}/{len(want_geo)} 缺={miss} vf={vfs}")


def j_prefix(cfg: dict, chain: str) -> tuple[bool, str]:
    """`format=yuv420p` 前缀规则：链里真有 needs_sw 的滤镜才补，且必须在最前；都不用时报空串。"""
    on = [f for f in transform.FILTERS if f.expr(cfg.get(f.spec.cfg, f.spec.default))]
    wants = any(f.needs_sw for f in on)
    parts = chain.split(",") if chain else []
    has = bool(parts) and parts[0] == SW_PREFIX
    if not on:
        return (chain == "", f"都不用 ⇒ 链={chain!r}")
    if wants:
        return (has and len(parts) == len(on) + 1, f"该补前缀：首段={parts[:1]} 段数={len(parts)}")
    return ((not has) and len(parts) == len(on), f"不该补：首段={parts[:1]} 段数={len(parts)}")


def j_chain_order(cfg: dict) -> tuple[bool, str]:
    """链顺序是契约：去带→降噪→锐化→裁剪→左右镜像→上下镜像（反了会把压下去的噪点又抬起来）。"""
    names = transform.chain_names(cfg)
    want = [f.key for f in transform.FILTERS if f.expr(cfg.get(f.spec.cfg, f.spec.default))]
    return (names == want, f"链={names} 契约={want}")


def disagree_pairs(items: list[tuple[str, Any, Any]]) -> list[str]:
    """"表与内核不一致"的点名器：items 是 (配置键, 请求值, 内核回报) 三元组。

    判据不许写成"回报非空就算过"：回报非空但**值不同**正是"界面动了、画面没动"的形状
    （实测直发 360 回报 0，是个漂亮非空值）。这一把尺验收与证伪共用，所以 `--falsify`
    把 `same_value` 摘成恒真时，它必须立刻什么都点不出来 ⇒ 那条变异被抓住。
    """
    return [f"{k} 请求 {w!r} 内核回报 {g!r}" for k, w, g in items
            if not transform.same_value(k, w, g)]


def j_chain_wire(src: str) -> tuple[bool, str]:
    """下发形状：链必须整条 `set` 替换，不许出现 `append`（实测 append 留残留）。

    这一条查的是**控制流形状**而不是像素：像素判据只能证明"这一轮动对了"，证明不了
    "代码里没有另一条 append 的下发路径"。写代码的人把 set 改回 append 时，
    链照样能跑、截图照样能看，只有这条形状判据会立刻红。
    """
    t = str(src or "")
    has_set = '"change-list", "vf", "set"' in t
    has_append = '"change-list", "vf", "append"' in t or '"vf", "append"' in t
    return (has_set and not has_append,
            f"含整条 set={has_set} 含 append={has_append} 源码 {len(t)} 字")


def j_reset_wire(src: str) -> tuple[bool, str]:
    """复位的形状：既要把几何逐项走完整张 `PROPS`，又要在收尾把链清掉。

    只查其中一半的复位实现会留下"面板数字回默认了、画面上还挂着锐化"这种半复位——
    实测过 `set_transform` 与 `apply_filter_chain` 是两条路，漏任何一条都是漏项。
    """
    t = str(src or "")
    walks_props = "transform.PROPS" in t
    clears = "apply_filter_chain" in t or '"change-list", "vf", "set"' in t
    return (walks_props and clears, f"遍历 PROPS={walks_props} 收尾清链={clears}")


# ==================== 真机工具（截图通路、噪声片段、像素度量） ====================

APP = None


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.02)


def gray(path: str):
    return np.asarray(Image.open(path).convert("L"), dtype=np.int16)


def edge(a) -> float:
    return float((np.abs(np.diff(a, axis=1)).mean() + np.abs(np.diff(a, axis=0)).mean()) / 2)


def dark_frac(a) -> float:
    """近黑占比（阈值 12）：黑边判据用的就是这个，不是"整图均值"——均值会被暗场景骗。"""
    return float((a < 12).mean())


def diff_mean(a, b) -> float:
    if a is None or b is None or a.shape != b.shape:
        return float("nan")
    return float(np.abs(a - b).mean())


def mk_noisy() -> bool:
    """带噪片段：降噪/去带的判据**必须**在有噪声的片子上量（干净样片上"前后一样"是应该的）。"""
    if os.path.exists(NOISY) and os.path.getsize(NOISY) > 10000:
        return True
    ff = cfgmod.FFMPEG_EXE
    if not os.path.exists(ff) or not os.path.exists(SAMPLE):
        return False
    r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-i", SAMPLE,
                        "-vf", "noise=alls=40:allf=t+u", "-c:v", "libx264", "-preset",
                        "ultrafast", "-crf", "20", "-an", NOISY],
                       capture_output=True, timeout=300)
    if r.returncode:
        print("  噪声片段生成失败：" + r.stderr.decode("utf-8", "replace")[-200:], flush=True)
    return os.path.exists(NOISY)


class Shot:
    """一次 `screenshot-to-file` 的取帧：出不出帧是**判据前提**，立不住就记 ENV，不许算 FAIL。"""

    def __init__(self, player, host: str) -> None:
        self.p = player
        self.host = host

    def take(self, tag: str, mode: str = "video"):
        path = os.path.join(OUT, f"{tag}-{mode}.png").replace("\\", "/")
        if os.path.exists(path):
            os.remove(path)
        self.p._req(["screenshot-to-file", path, mode], timeout=8)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            pump(0.08)
            if os.path.exists(path) and os.path.getsize(path) > 2000:
                time.sleep(0.25)
                try:
                    return np.asarray(Image.open(path).convert("L"), dtype=np.int16)
                except Exception:
                    return None
        return None


def clear_chain(p) -> None:
    p._req(["change-list", "vf", "set", ""], timeout=3)
    pump(0.35)


def apply_chain(p, chain: str) -> tuple[list[str], list[str]]:
    """下发一条链，回 (内核列表里的名字, 日志里的禁用行)。**只看这两样还不够**——
    返回值刻意不含像素，像素由调用方另取一帧，防止"列表+日志都好看"就被当成生效。"""
    off = os.path.getsize(p.log_path) if os.path.exists(p.log_path) else 0
    clear_chain(p)
    if chain:
        p._req(["change-list", "vf", "set", chain], timeout=3)
    pump(0.9)
    listed = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
    try:
        with open(p.log_path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(off)
            tail = fh.read()
    except OSError:
        tail = ""
    return listed, transform.filter_failures(tail)


def set_and_read(p, prop: str, value):
    """set→get，带回"发之前的旧值"，因为 `j_kernel_refused` 需要三元组才分得开拒收与正常。"""
    prev = p.get(prop)
    p.set_prop(prop, value)
    pump(0.4)
    return prev, value, p.get(prop)


# ==================== 离线半：那张表自己的形状（不碰内核） ====================

def offline_checks() -> None:
    D = transform.defaults()
    check("表结构：几何 6 项 + 滤镜 6 档 = 12 个可调项，且没有重复的配置键",
          len(transform.PROPS) == 6 and len(transform.FILTERS) == 6 and len(SPECS) == 12
          and len(transform.ALL_KEYS) == 12 and len(set(transform.ALL_KEYS)) == 12,
          f"PROPS={len(transform.PROPS)} FILTERS={len(transform.FILTERS)} "
          f"ALL_KEYS={list(transform.ALL_KEYS)}")

    alias = [k for k in transform.ALL_KEYS
             if transform.spec_for_cfg(k) is not transform.spec_for_cfg(
                 transform.spec_for_cfg(k).prop)]
    check("双查一致：按配置键与按 mpv 属性名必须落到同一行（只认一种写法，另一种会静默放行）",
          not alias, f"对不上的={alias}")

    bad_def = [k for k, v in D.items() if transform.normalize(k, v) != v]
    check("每行的默认值自己就得过归一（默认值写歪了会被夹紧无声改掉）",
          not bad_def, f"归一后变了的={bad_def}")

    missing = [k for k in D if k not in cfgmod.DEFAULTS or cfgmod.DEFAULTS[k] != D[k]]
    check("配置默认值从这里取，两处不许各写一份", not missing,
          f"缺失或不一致的={missing}")

    ans = [("panscan", 2.0, 1.0), ("panscan", -1.0, 0.0), ("panscan", "0.4", 0.4),
           ("pan_x", 5.0, 1.0), ("pan_x", -5.0, -1.0),
           ("rotate", "360", "0"), ("rotate", "-90", "270"), ("rotate", "450", "90"),
           ("rotate", "abc", "0"), ("rotate", 90.0, "90"),
           ("video-rotate", "-90", "270"),
           ("aspect_override", "16/9", "16:9"), ("aspect_override", "16：9", "16:9"),
           ("aspect_override", "1.7777", "16:9"), ("aspect_override", "2.35", "2.35:1"),
           ("aspect_override", "21:9", "21:9"), ("aspect_override", "1.7", "no"),
           ("aspect_override", "abc", "no"),
           ("crop_ratio", "4/3", "4:3"), ("crop_ratio", "junk", "none"),
           ("crop_ratio", "none", "none"),
           ("sharpen", 99, 2.0), ("denoise", -3, 0), ("deband_vf", 100, 10),
           ("mirror_h", 1, True), ("mirror_v", "", False), ("keep_aspect", 0, False),
           ("mirror_h", "true", True), ("mirror_h", "yes", True),
           ("mirror_h", "0", False), ("mirror_v", "false", False),
           ("mirror_v", "abc", False), ("keep_aspect", "no", False)]
    wrong = [(k, v, transform.normalize(k, v), e) for k, v, e in ans
             if transform.normalize(k, v) != e]
    check("夹紧与归一有已知答案（越界整条拒收这件事只能靠外壳兜，见模块 docstring 第 1 条；"
          "布尔的**串**写法只认白名单，`bool(\"0\")` 是 True 这种默认行为不能用）",
          not wrong, f"对不上的(键,喂入,得到,期望)={wrong}")

    mv = [("aspect_override", "no", "no"), ("aspect_override", "16:9", 1.777778),
          ("aspect_override", "4:3", 1.333333), ("rotate", "90", 90),
          ("keep_aspect", True, "yes"), ("keep_aspect", False, "no"),
          ("panscan", 0.5, 0.5), ("mirror_h", 1, "yes")]
    wrong_mv = [(k, v, transform.mpv_value(k, v), e) for k, v, e in mv
                if transform.mpv_value(k, v) != e or type(transform.mpv_value(k, v)) is not type(e)]
    check("下发形状有已知答案：自动档发 `no` 而不是 -1（-1 每次启动吃一条 deprecation 警告）、"
          "枚举发串、布尔发 yes/no", not wrong_mv, f"对不上的={wrong_mv}")

    sv = [("keep_aspect", True, "yes", True), ("keep_aspect", True, True, True),
          ("keep_aspect", True, 1, True), ("keep_aspect", True, "no", False),
          ("keep_aspect", False, "no", True), ("keep_aspect", False, True, False),
          ("keep_aspect", "0", True, False), ("keep_aspect", "yes", True, True),
          ("aspect_override", "no", -2.0, True), ("aspect_override", "no", -1.0, True),
          ("aspect_override", "no", "no", True), ("aspect_override", "no", "none", True),
          ("aspect_override", "16:9", 1.777778, True), ("aspect_override", "16:9", -2.0, False),
          ("aspect_override", "16:9", "no", False), ("aspect_override", "4:3", "abc", False),
          ("rotate", "0", 360, True), ("rotate", "270", -90, True),
          ("panscan", 0.3, 0.3000001, True), ("pan_x", 0.0, "0", True)]
    wrong_sv = [(k, w, g, transform.same_value(k, w, g)) for k, w, g, e in sv
                if transform.same_value(k, w, g) is not e]
    check("回报比对有已知答案：布尔那条是链式比较的回归位（漏括号会把成功读成失败）、"
          "自动档 -1/-2/\"no\" 都算（订阅缓存没填时 `prop()` 回的就是配置默认那串）、"
          "非自动档的请求不许被字符串糊过去", not wrong_sv, f"对不上的={wrong_sv}")

    rw = [("16:9", (16, 9)), ("16/9", (16, 9)), ("16：9", (16, 9)), ("2.35", (47, 20)),
          ("1.7777", (17777, 10000)), ("none", None), ("", None), ("0:1", None),
          ("-1:1", None), ("abc", None), ("1e12", None)]
    wrong_rw = [(s, transform._ratio_wh(s), e) for s, e in rw if transform._ratio_wh(s) != e]
    check("比例串解析有已知答案：三种分隔符都认、脏值报 None、1.7777 不量化成 1.78"
          "（用 Fraction 而不是乘 100 取整）", not wrong_rw, f"对不上的={wrong_rw}")

    check("ratio_float：认得出的给数值、认不出与自动档统一给负哨兵（不是线上值）",
          abs(transform.ratio_float("16:9") - 16 / 9) < 1e-9
          and transform.ratio_float("2.35:1") == 2.35
          and transform.ratio_float("none") < 0 and transform.ratio_float("abc") < 0,
          f"16:9={transform.ratio_float('16:9')} 2.35:1={transform.ratio_float('2.35:1')} "
          f"none={transform.ratio_float('none')}")

    all_on = {"deband_vf": 10, "denoise": 10, "sharpen": 2.0, "crop_ratio": "2.35:1",
              "mirror_h": True, "mirror_v": True}
    want_chain = ["deband=1thr=0.0400:2thr=0.0300:3thr=0.0300:range=24",
                  "hqdn3d=4.00:3.00:6.00:4.50", "unsharp=5:5:2.00:5:5:0.0",
                  transform.CROP.expr("2.35:1"), "hflip", "vflip"]
    got_chain = transform.build_chain(all_on)
    check("全开时的链串逐节比对（参数名与数值都是实测来的，改一个字都要在这里先红）",
          got_chain.split(",")[1:] == want_chain,
          f"实际={got_chain.split(',')[1:]}\n     期望={want_chain}")

    ok_o, d_o = j_chain_order(all_on)
    check("链顺序契约（去带→降噪→锐化→裁剪→左右镜像→上下镜像）对全开配置成立",
          ok_o, d_o)

    zero_off = [f.key for f in FILTERS if f.expr(f.spec.default)]
    check("强度 0／关／none 的档一律不上链（链里干脆不留这一节，不是留着个 0 参数）",
          not zero_off, f"默认值却上了链的={zero_off}")

    only_crop = transform.build_chain({"crop_ratio": "4:3", "sharpen": 0, "denoise": 0,
                                       "deband_vf": 0, "mirror_h": False, "mirror_v": False})
    p_ok, p_d = j_prefix({"crop_ratio": "4:3", "sharpen": 0, "denoise": 0, "deband_vf": 0,
                          "mirror_h": False, "mirror_v": False}, only_crop)
    check("只开裁剪时不补 `format=yuv420p`（crop 直接吃 nv12，实测裸补前缀也要白跑一趟 CPU）",
          p_ok and only_crop.startswith("crop="), f"{p_d} 链={only_crop[:40]}")

    on_ok, on_d = j_prefix(all_on, got_chain)
    check("链里只要有 needs_sw 的档就必须补前缀，且补在最前（实测放后面内核照样 Impossible to convert）",
          on_ok, on_d)

    check("空配置 ⇒ 链是空串（内核侧＝清空，实测 set '' 回报 []）",
          transform.build_chain({k: transform.defaults()[k] for k in transform.ALL_KEYS}) == "",
          "")

    comma_ok, comma_d = j_comma_gate(got_chain, len(want_chain) + 1)
    crop_variants = [transform.CROP.expr(r) for r in ("16:9", "4:3", "21:9", "2.35:1", "1:1")]
    comma_all = all(j_comma_gate("format=yuv420p," + c, 2)[0] for c in crop_variants)
    check("逗号闸门：五种裁剪档拼出来的串里，逗号一次都不许落在某一档内部"
          "（min/max/if 这类多参函数在本机会被整条拒收）",
          comma_ok and comma_all, f"{comma_d} 各档={ [c[:26] for c in crop_variants] }")

    ok_a0, d_a0 = j_startup(transform.startup_args({k: 0 for k in transform.ALL_KEYS}
                                                    | {"keep_aspect": True}), "")
    ok_a1, d_a1 = j_startup(transform.startup_args(all_on), got_chain)
    check("启动参数：几何 6 条一条不缺，链空时不带 --vf、链非空时正好一条且逐字对上",
          ok_a0 and ok_a1, f"空链={d_a0} 有链={d_a1}")

    rv = transform.reset_values()
    miss_r = [k for k in transform.ALL_KEYS if k not in rv]
    check("整族复位覆盖 12 项且每项都是归一后的默认值（漏一项就是复位后画面留着上一轮的愿望）",
          len(rv) == 12 and not miss_r and all(rv[k] == transform.normalize(k, transform.defaults()[k])
                                               for k in rv),
          f"键数={len(rv)} 缺={miss_r}")

    vt = [("panscan", 0.35, "35%"), ("aspect_override", "no", "自动（按片子本身）"),
          ("aspect_override", "16:9", "16:9"), ("mirror_h", True, "开"),
          ("mirror_v", False, "关"), ("rotate", "90", "90°"), ("sharpen", 1.0, "1.00×")]
    wrong_vt = [(k, v, transform.value_text(k, v), e) for k, v, e in vt
                if transform.value_text(k, v) != e]
    check("给人看的那行字有已知答案：panscan 是百分数（内核收 0..1）、枚举给中文",
          not wrong_vt, f"对不上的={wrong_vt}")

    cyc = [("rotate", "0", True, "90"), ("rotate", "270", True, "0"),
           ("rotate", "0", False, "270"), ("aspect_override", "no", True, "16:9"),
           ("crop_ratio", "none", True, "16:9"), ("panscan", 0.0, True, 0.01),
           ("panscan", 1.0, True, 1.0), ("pan_x", -1.0, False, -1.0),
           ("mirror_h", False, True, True), ("keep_aspect", True, True, False)]
    wrong_cyc = [(k, c, f, transform.cycle(k, c, f), e) for k, c, f, e in cyc
                 if transform.cycle(k, c, f) != e]
    check("快捷键走的循环有已知答案：枚举回绕、数值步进不越界、布尔翻转",
          not wrong_cyc, f"对不上的(键,当前,方向,得到,期望)={wrong_cyc}")

    dirty = "\n".join([
        "[  12.345] [vo/gpu] [e] Impossible to convert between the formats supported by the "
        "filter 'in' and the image format nv12",
        "[  12.346] [vf] [w] Disabling filter unsharp.",
        "[   9.001] [vf] [e] AVOption 'iterations' not found in 'deband'",
        "[   9.002] [cplayer] [e] Option vf-set: 'ih' isn't supported.",
        # 真机原话（probe-transform12.log）：整条带逗号的链走 append 时内核说的是这句
        "[   3.716] [cplayer] [e] Option vf-append: -append takes only 1 item (no ',').",
        "[   9.003] [vf] [e] No such filter: 'nlmeans2'",
        "[  12.346] [vf] [w] Disabling filter unsharp."])
    hits = transform.filter_failures(dirty)
    clean = ("[   1.000] [cplayer] Starting mpv\n"
             "[   1.200] [vo/gpu] Using GPU: D3D11\n"
             "[   2.000] [cplayer] Clamped panscan to 1.000000")
    check("内核日志里「这个滤镜其实没跑起来」的行必须被挑出来（重复行只记一次），"
          "正常日志与无害警告不误报（含「命令形状错了」那句真机原话：append 带逗号）",
          len(hits) == 6 and transform.filter_failures(clean) == []
          and any("Disabling filter" in h for h in hits)
          and any("only 1 item" in h for h in hits),
          f"挑出 {len(hits)} 行={hits[:2]} 干净日志回={transform.filter_failures(clean)}")

    check("filter_failures 对空/None 输入回空表（判据不许自己先崩）",
          transform.filter_failures("") == [] and transform.filter_failures(None) == [], "")

    unknown = [k for k in ("nope", "", "volume") if transform.is_known(k)]
    check("表里有没有这一项：配置键与属性名都算，别把内核侧的 volume 这类当成自己的项",
          transform.is_known("panscan") and transform.is_known("video-rotate")
          and not unknown, f"意外在表里的={unknown}")

    bad_expr = transform.MIRROR_H.expr("0")
    check("关掉的那一档返回空串而不是 '0'（空串才是不上链，带名字会把内核骗过去；"
          "串写法的 \"0\"/\"false\" 也算关，实测 bool(\"0\") 是 True 所以不能直接用 bool）",
          bad_expr == "" and transform.MIRROR_H.expr(1) == "hflip"
          and transform.MIRROR_H.expr("yes") == "hflip" and transform.MIRROR_H.expr(False) == "",
          f"关(\"0\")={bad_expr!r} 开(1)={transform.MIRROR_H.expr(1)!r} "
          f"开(\"yes\")={transform.MIRROR_H.expr('yes')!r}")

    prop_filter = [s.prop for s in SPECS if transform.prop_of(s.cfg)]
    check("滤镜类没有属性可设（prop_of 回空串）；把它们当属性发就会打出一条没人核对过的 set_property",
          len(prop_filter) == 6 and transform.prop_of("sharpen") == ""
          and transform.prop_of("unsharp") == "", f"有属性的={prop_filter}")

    # —— 控制流形状：像素判据只能证"这一轮动对了"，证不了"代码里没有另一条下发路径" ——
    try:
        import inspect
        from app.engine.player import MpvPlayer
        src_chain = inspect.getsource(MpvPlayer.apply_filter_chain)
        src_reset = inspect.getsource(MpvPlayer.reset_transforms)
    except Exception as exc:
        env_note("下发形状判据", f"读不到引擎源码（{exc!r}）：这两条本轮没有测量对象")
    else:
        w_ok, w_d = j_chain_wire(src_chain)
        check("链的下发形状：产品走整条 `change-list vf set`，源码里不许有 append"
              "（append 实测留残留，而界面与截图都可能照样好看）", w_ok, w_d)
        r_ok, r_d = j_reset_wire(src_reset)
        check("复位的形状：几何遍历整张 PROPS + 收尾把链清掉（set_transform 与 apply_filter_chain "
              '是两条路，漏任何一条就是"半复位"）', r_ok, r_d)
        fake = src_chain.replace('"change-list", "vf", "set"', '"change-list", "vf", "append"')
        f_ok, _fd = j_chain_wire(fake)
        check("形状判据不是恒真：同一份源码只把 set 换成 append，它必须立刻翻脸",
              not f_ok and '"vf", "append"' in fake, f"改掉之后仍判过={f_ok}")


# ==================== 真机半：内核收下这些值之后，像素到底动没动 ====================

def safe_tag(s: str) -> str:
    """截图文件名里不能出现 ':' 与 '/'——实测带表达式的路径会让 screenshot-to-file 直接失败
    （`probe-transform4.log` 末尾那条"命令失败"就是这么来的）。"""
    return str(s).replace(":", "-").replace("/", "-").replace(" ", "_")[:28]


def must_shot(sh, tag: str, name: str, mode: str = "video"):
    """取一帧；取不到就记 ENV 并回 None。出不出帧是判据的**前提**，不是产品的好坏。"""
    a = sh.take(safe_tag(tag), mode)
    if a is None:
        env_note(name, "这一轮截图通路没出帧（宿主窗口被最小化／内核没在渲染），"
                       "像素判据没有测量对象；换个画面能动的时间点重跑")
    return a


def live(p, sh, cfg) -> None:
    base_name = "基线帧"
    p._req(["change-list", "vf", "set", ""], timeout=3)
    pump(0.4)
    base = must_shot(sh, "base", base_name)
    if base is None:
        return
    check(base_name + "：暂停帧的尺寸与边缘能量记进台账（后面每一条像素判据都跟它比）",
          base.shape[0] > 300 and base.shape[1] > 300,
          f"基线 {base.shape[1]}×{base.shape[0]} 边缘能量={edge(base):.3f}")

    # —— 表 ↔ 内核一致：几何 6 项走产品路径，逐项 set→get 比回来 ——
    geo_targets = [(ASPECT, "4:3"), (ROTATE, "90"), (KEEPASPECT, False),
                   (PANSCAN, 0.35), (PAN_X, 0.4), (PAN_Y, -0.25)]
    pairs, refused = [], []
    for spec, val in geo_targets:
        prev, want, got = set_and_read(p, spec.prop, transform.mpv_value(spec.cfg, val))
        if j_kernel_refused(prev, want, got):
            refused.append(f"{spec.prop}={want!r}→回报{got!r}(停在旧值{prev!r})")
            continue
        pairs.append((spec.cfg, val, got))
    disagree = disagree_pairs(pairs)
    p.reset_transforms()
    pump(0.4)
    check("表里每一行内核都认：6 个几何项 set→get 逐项比回来（范围写宽了就是"
          "「界面动了、画面没动」的产地）", not disagree and not refused,
          f"不一致={disagree} 被拒收={refused}")

    # —— 越界值是**整条拒收**，不是夹紧（`probe-transform11.log`：从已知旧值逐项发，内核一次都没
    #    夹过，日志里零条 Clamped）。这一条原来在表里写错过：把上一档量到的旧值当成了夹紧结果。
    rej = []
    for prop, old, send in (("video-rotate", 0, 360), ("video-rotate", 90, 360),
                            ("video-rotate", 0, -90), ("video-rotate", 90, -90),
                            ("panscan", 0.0, -1.0), ("panscan", 0.5, -1.0),
                            ("panscan", 0.0, 2.0), ("panscan", 0.5, 2.0),
                            ("panscan", 0.35, -0.0001)):
        prev, want, got = set_and_read(p, prop, send)
        if not j_kernel_refused(prev, want, got):
            rej.append(f"{prop} 旧={prev!r} 发={send!r}→回报 {got!r}（没停在旧值）")
    # 反向对照：**没有边界**的那两项照单全收——范围不是内核给的，就别指望它兜
    greedy = []
    for prop, send in (("video-pan-x", 5.0), ("video-pan-x", -5.0), ("video-scale-x", 3.0),
                       ("video-scale-x", 0.0)):
        prev, want, got = set_and_read(p, prop, send)
        if got != send:
            greedy.append(f"{prop} 发={send!r}→回报 {got!r}（原以为内核照收）")
    p.set_prop("video-pan-x", 0.0)
    p.set_prop("video-scale-x", 1.0)
    via_table = {}
    for key, send in (("rotate", "360"), ("rotate", "-90"), ("panscan", -1.0),
                      ("panscan", 2.0), ("pan_x", 5.0)):
        _a, okk = p.set_transform(key, send)
        pump(0.3)
        via_table[f"{key}={send}"] = (transform.mpv_value(key, transform.normalize(key, send)),
                                      p.get(transform.prop_of(key)), okk)
    p.reset_transforms()
    pump(0.5)
    check("越界行为与表一致：内核不夹（rotate 与 panscan 两侧越界都停在旧值，连 -0.0001 都拒），"
          "没边界的项照单全收（pan-x ±5、scale-x 3.0 与 0.0）；走表的每一条都落在内核可收的范围里"
          "（所以取模与夹紧是外壳的承重件，指望内核兜会得到「界面动了、画面没动」）",
          not rej and not greedy and all(a == g and ok for a, g, ok in via_table.values()),
          f"没停在旧值={rej} 没照收={greedy} 走表(下发,回报,判一致)={via_table}")

    # —— 「列表≠事实」：不带 format 前缀的链，必须靠日志与像素抓到 ——
    bare = "unsharp=5:5:1.00:5:5:0.0"
    listed, fails = apply_chain(p, bare)
    after = must_shot(sh, "bare-unsharp", "不带前缀的锐化")
    moved = (after is not None and diff_mean(base, after) > 0.05)
    verdict, why = j_list_vs_truth(True, "unsharp" in listed, fails, moved)
    check("硬解帧上「滤镜被内核扔掉但列表照样好看」这一形状，判据必须认出来"
          "（缺 format=yuv420p 前缀的链：列表在、日志有禁用行、像素一动不动）",
          verdict == "LIST_LIE" and bool(fails),
          f"结论={verdict} {why} 列表={listed}")

    prefixed = f"{SW_PREFIX},{bare}"
    listed2, fails2 = apply_chain(p, prefixed)
    after2 = must_shot(sh, "prefixed-unsharp", "带前缀的锐化")
    moved2 = (after2 is not None and diff_mean(base, after2) > 0.05)
    verdict2, why2 = j_list_vs_truth(True, "unsharp" in listed2, fails2, moved2)
    e_bare = None if after is None else edge(after)
    e_pre = None if after2 is None else edge(after2)
    ok_sh, ratio_sh = (j_sharpen(edge(base), e_pre) if e_pre is not None else (False, 0.0))
    check("同一档锐化补上 `format=yuv420p` 之后真的动像素且把高频抬起来"
          "（这条同时证明上一条的红不是测量器坏了）",
          verdict2 == "OK" and not fails2 and ok_sh,
          f"结论={verdict2} {why2} 边缘能量 {edge(base):.3f}→{None if e_pre is None else round(e_pre, 3)}"
          f"（不带前缀那帧={None if e_bare is None else round(e_bare, 3)}）比值={ratio_sh:.2f}")

    # —— 镜像：判据是「与基线翻转后的差 ≪ 与基线本身的差」，不是「像素动了」 ——
    for filt, flipper, tag in ((MIRROR_H, lambda a: a[:, ::-1], "hflip"),
                               (MIRROR_V, lambda a: a[::-1, :], "vflip")):
        cfg_key = filt.spec.cfg        # 注意是 `filt.spec.cfg`：MIRROR_H 是 Filter，配置键在它带的 Flag 上
        _r, ok_m = p.set_transform(cfg_key, True)
        pump(0.7)
        a = must_shot(sh, f"mirror-{tag}", f"{filt.label}")
        listed_m, fails_m = [str(x.get("name")) for x in (p.get("vf", []) or [])
                             if isinstance(x, dict)], p.filter_failures()
        d_flip = None if a is None else diff_mean(a, flipper(base))
        d_plain = None if a is None else diff_mean(a, base)
        ok_j = (a is not None and d_flip is not None and d_plain is not None
                and j_mirror(d_flip, d_plain)[0] and not fails_m and tag in listed_m)
        check(f"{filt.label}：翻转后的帧必须和「基线自己翻转」几乎一样，和基线本身差得远"
              f"（只比「动没动」会被任何滤镜骗过去）",
              ok_j, f"与翻转差={None if d_flip is None else round(d_flip, 3)} "
                    f"与原帧差={None if d_plain is None else round(d_plain, 3)}"
                    f"（比值要 >5）列表={listed_m} 禁用行={fails_m[:1]} 产品回报 ok={ok_m}")
        p.set_transform(cfg_key, False)
        pump(0.4)

    # —— 裁剪：唯一能证的是**截图尺寸**（实测 video-out-params 裁完仍回报原尺寸） ——
    crop_expect = {"2.35:1": 2.35, "4:3": 4 / 3, "1:1": 1.0, "21:9": 21 / 9}
    bad_crop, dims, vop_seen = [], [], {}
    for ratio, target in crop_expect.items():
        _r, ok_c = p.set_transform("crop_ratio", ratio)
        pump(0.8)
        a = must_shot(sh, f"crop-{ratio}", f"裁剪 {ratio}")
        if a is None:
            return
        h, w = a.shape[0], a.shape[1]
        vop = p.get("video-out-params", {}) or {}
        vop_seen[ratio] = (w, h, vop.get("w"), vop.get("h"), ok_c)
        dims.append(f"{ratio}→{w}×{h}")
        ok_ar, r = j_ar(w, h, target, 3e-3)
        if not ok_ar:
            bad_crop.append(f"{ratio} 截图 {w}×{h}（比 {r:.4f}）")
    check("裁剪按截图尺寸对上目标比例（2.35:1/4:3/1:1/21:9 四档，iw/ih 表达式在机器上求值）",
          not bad_crop, f"量的={dims} 不合格={bad_crop}")
    # 这一条不是判产品，是判**判据的来源**：实测裁完之后 video-out-params 的 w/h 仍然回报片子的
    # 原尺寸，谁拿它当"裁完多大"谁就永远读到 1280×720、永远"看起来没问题"。把它钉成一条，
    # 是为了让下一次有人想把判据换成 d_w/d_h 时先红在这里。
    trap = [k for k, (w, h, dw, dh, _o) in vop_seen.items()
            if (w, h) != (dw, dh) and (dw, dh) == (1280, 720)]
    check("裁完尺寸变了而 d_w/d_h 仍回报 1280×720：这条把「截图尺寸才是唯一能证的」写成判据，"
          "不许改用 video-out-params",
          len(trap) >= 3, f"截图与回报不一致的档={trap} 全部={vop_seen}")
    p.set_transform("crop_ratio", "none")
    pump(0.4)

    # —— 旋转：90/270 换宽高，180 必须等价于「基线转 180」 ——
    rot_bad, rot_dims = [], []
    for deg, swap in (("90", True), ("180", False), ("270", True)):
        p.set_prop("video-rotate", 0)
        pump(0.3)
        _r, ok_r = p.set_transform("rotate", deg)
        pump(0.8)
        a = must_shot(sh, f"rotate-{deg}", f"旋转 {deg}")
        if a is None:
            return
        h, w = a.shape[0], a.shape[1]
        rot_dims.append(f"{deg}°→{w}×{h}")
        if swap and not (w == base.shape[0] and h == base.shape[1]):
            rot_bad.append(f"{deg}° 没换宽高（{w}×{h}）")
        if not swap:
            d180 = diff_mean(a, base[::-1, ::-1])
            d0 = diff_mean(a, base)
            if not (d180 < d0 / 5):
                rot_bad.append(f"180° 与「基线转180」差 {d180:.2f}，与原帧差 {d0:.2f}（该远小于）")
    check("旋转走的是属性而不是滤镜：90/270 把截图宽高互换、180 与「基线转 180」几乎一样"
          "（把 video-rotate 当 vf 滤镜发下去，实测会被内核整条禁用）",
          not rot_bad, f"量的={rot_dims} 不合格={rot_bad}")
    p.set_transform("rotate", "0")
    pump(0.4)

    # —— 画面比例：video 模式截图的**比值**跟着走（实测 4:3→1280×960、21:9→1680×720） ——
    ar_bad, ar_dims = [], []
    for ratio, target in (("4:3", 4 / 3), ("1:1", 1.0), ("21:9", 21 / 9), ("no", 1280 / 720)):
        _r, ok_a = p.set_transform("aspect_override", ratio)
        pump(0.8)
        a = must_shot(sh, f"aspect-{ratio}", f"画面比例 {ratio}")
        if a is None:
            return
        h, w = a.shape[0], a.shape[1]
        ok_ar, r = j_ar(w, h, target)
        ar_dims.append(f"{ratio}→{w}×{h}({r:.4f})")
        if not ok_ar:
            ar_bad.append(f"{ratio} 比 {r:.4f} 对不上 {target:.4f}")
    check("画面比例真的改到出图比例：四档按比值对上，且自动档回落到片子本身的比例",
          not ar_bad, f"量的={ar_dims} 不合格={ar_bad}")

    # —— 锐化／去带在干净样片上只判「动没动 + 日志干不干净」，强度方向的判据放到噪声片段 ——
    p.set_transform("sharpen", 2.0)
    pump(0.8)
    a = must_shot(sh, "sharpen-2", "锐化 2.0")
    ok_s = a is not None and j_sharpen(edge(base), edge(a))[0]
    check("走产品路径开锐化（滑轨→配置→整条链替换）之后，边缘能量实测抬起",
          ok_s and not p.filter_failures(),
          f"边缘能量 {edge(base):.3f}→{None if a is None else round(edge(a), 3)} "
          f"链={p.vf_names()} 禁用行={p.filter_failures()[:1]}")
    p.set_transform("sharpen", 0)
    pump(0.5)
    a_off = must_shot(sh, "sharpen-off", "锐化归零")
    check("关掉之后链真的清空（set '' 实测回报 []，用 append 删不干净）",
          a_off is not None and p.vf_names() == [] and diff_mean(a_off, base) < 0.05,
          f"列表={p.vf_names()} 与基线差={None if a_off is None else round(diff_mean(a_off, base), 3)}")

    # —— append 的两种形状（本机实测见 probe-transform12.log）：「为什么必须整条 set」的活证据 ——
    #   带逗号的整条链 → 内核**整条拒收**（原话 "-append takes only 1 item (no ',')"，链表不动）；
    #   单档 → 照挂，而且同档挂两次就是两条："每改一档补一发 append"就是这么攒出残留的。
    p._req(["change-list", "vf", "set", ""], timeout=3)
    pump(0.3)
    off_ap = p._log_offset()
    p._req(["change-list", "vf", "append", f"{SW_PREFIX},hflip"], timeout=3)
    pump(0.6)
    listed_cm = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
    line_cm = [l for l in transform.filter_failures(p._log_since(off_ap)) if "only 1 item" in l]
    check("整条链（带逗号）走 append 走不通：内核整条拒收、链表纹丝不动，"
          "且日志留下原话「-append takes only 1 item (no ',')」",
          listed_cm == [] and bool(line_cm), f"列表={listed_cm} 原话={line_cm[:1]}")
    p._req(["change-list", "vf", "set", f"{SW_PREFIX},hflip"], timeout=3)
    pump(0.6)
    listed_st = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
    check("对照：同一条链改走 `set` 就挂得上（上一条的空列表因此是内核拒收，不是命令没送到）",
          listed_st == ["format", "hflip"], f"列表={listed_st}")
    p._req(["change-list", "vf", "set", ""], timeout=3)
    pump(0.3)
    for _ in (1, 2):
        p._req(["change-list", "vf", "append", "hflip"], timeout=3)
        pump(0.4)
    listed_ap = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
    residue = j_append_residue(["hflip"], listed_ap)
    check("同一档 append 两次会在链上挂两条（残留的真形状）——产品每改一档就整条替换，"
          "判据点名的就是这种写法", bool(residue), f"列表={listed_ap} 残留={residue}")
    p._req(["change-list", "vf", "set", ""], timeout=3)
    pump(0.4)

    # —— keepaspect / panscan / 平移：只有在**固定尺寸的宿主窗口**里才量得出来 ——
    p.set_transform("aspect_override", "4:3")
    pump(0.6)
    a_yes = must_shot(sh, "bars-keep-yes", "keepaspect=保持", "window")
    p.set_transform("keep_aspect", False)
    pump(0.8)
    a_no = must_shot(sh, "bars-keep-no", "keepaspect=不保持", "window")
    if a_yes is None or a_no is None:
        return
    ok_b, d_b = j_bars(dark_frac(a_yes), dark_frac(a_no))
    check("「保持比例」在固定窗口里有像素证据：4:3 塞进 16:9 的框必须有左右黑边，"
          "关掉之后铺满（实测 0.261 vs 0.0055）",
          ok_b, d_b)
    p.set_transform("keep_aspect", True)
    pump(0.6)
    a_p0 = must_shot(sh, "bars-panscan-0", "panscan=0", "window")
    p.set_transform("panscan", 1.0)
    pump(0.9)
    a_p1 = must_shot(sh, "bars-panscan-1", "panscan=1", "window")
    if a_p0 is None or a_p1 is None:
        return
    ok_ps, d_ps = j_panscan(dark_frac(a_p0), dark_frac(a_p1))
    check("panscan「充满画面」把黑边吃掉：同一画面下 0→1 近黑占比必须塌下去",
          ok_ps and not math.isnan(dark_frac(a_p0)), d_ps)
    p.set_transform("panscan", 0.0)
    p.set_transform("aspect_override", "no")
    pump(0.6)
    a_q0 = must_shot(sh, "pan-0", "平移=0", "window")
    p.set_transform("pan_x", 1.0)
    pump(0.9)
    a_q1 = must_shot(sh, "pan-max", "平移=满", "window")
    p.set_transform("pan_x", 0.0)
    pump(0.4)
    if a_q0 is None or a_q1 is None:
        return
    ok_pn, d_pn = j_pan(dark_frac(a_q0), dark_frac(a_q1))
    check("video-pan-x 把画面推出框：窗口里的近黑占比必须涨起来（这项在 video 模式截图里"
          "结构性看不见，只有 window 模式量得到）", ok_pn, d_pn)

    # —— 整族复位：12 项全回默认，链清空，日志没有禁用行 ——
    for cfg_key, val in (("sharpen", 1.5), ("crop_ratio", "1:1"), ("mirror_h", True),
                         ("panscan", 0.6), ("rotate", "270"), ("aspect_override", "21:9")):
        p.set_transform(cfg_key, val)
        pump(0.35)
    out = p.reset_transforms()
    pump(0.7)
    readback = {s.cfg: out.get(s.prop) for s in transform.PROPS}
    miss = j_reset({k: transform.defaults()[k] for k in readback}, readback)
    check("Ctrl+G 整族复位：几何逐项回到默认、链清空、且这一趟下发没有触发内核禁用行",
          not miss and out.get("vf") == [] and not out.get("vf_failed"),
          f"漏项/不一致={miss} 链={out.get('vf')} 禁用={out.get('vf_failed')} "
          f"回报={readback}")

    # —— 换片不丢链（表 docstring 第 5 条：所以不需要 file-loaded 时补发） ——
    p.set_transform("sharpen", 1.0)
    p.set_transform("mirror_v", True)
    pump(0.6)
    before = p.vf_names()
    p.open_files([os.path.join(HERE, "samples", "gelu-sample-2.mp4")], append=False,
                 remember=False)
    pump(1.8)
    after_l = p.vf_names()
    check("换一片之后滤镜链还在（vf 是属性，不需要在 file-loaded 时补发）",
          "unsharp" in after_l and "vflip" in after_l, f"换片前={before} 换片后={after_l}")
    p.set_transform("sharpen", 0)
    p.set_transform("mirror_v", False)
    pump(0.5)


def noisy_live(p, sh) -> None:
    """降噪与去带的强度判据**只能**在带噪片段上量：干净样片上「前后一样」是应该的。"""
    if not mk_noisy():
        env_note("噪声片段", f"没能造出 {NOISY}（缺 ffmpeg 或缺样片），降噪/去带的强度判据"
                             "没有测量对象——这两项本轮不作数")
        return
    p._req(["change-list", "vf", "set", ""], timeout=3)
    p.open_files([NOISY], append=False, remember=False)
    pump(2.0)
    p.set_prop("time-pos", 3.0)
    p.set_prop("pause", True)
    pump(0.9)
    base = must_shot(sh, "noisy-base", "噪声片段基线")
    if base is None:
        return
    eb = edge(base)
    check("噪声片段基线：边缘能量必须明显高于干净样片（否则「降噪压高频」根本没有东西可压）",
          eb > 5.0, f"噪声基线={eb:.3f}（样片基线量到的是 1.400 量级）")

    edges = {0.0: eb}
    for lvl in (7, 10):
        p.set_transform("denoise", lvl)
        pump(0.9)
        a = must_shot(sh, f"denoise-{lvl}", f"降噪 {lvl} 档")
        if a is None:
            return
        edges[float(lvl)] = edge(a)
    ok_d, d_d = j_denoise(edges)
    check("降噪在带噪片段上单调压高频（0→7→10 档逐级下降）", ok_d,
          f"{d_d} 列表={p.vf_names()} 禁用行={p.filter_failures()[:1]}")
    p.set_transform("denoise", 0)
    pump(0.5)

    p.set_transform("deband_vf", 10)
    pump(0.9)
    a = must_shot(sh, "deband-10", "去带 10 档")
    if a is None:
        return
    ok_db, d_db = j_deband(eb, edge(a), diff_mean(a, base))
    check("去带（滤镜这一档）真的在跑：像素动了且高频被压下去"
          "（判据证的是「这一档在动」，不是「条带观感变好」——无参考图量不出后者）",
          ok_db and not p.filter_failures(), f"{d_db} 列表={p.vf_names()}")
    p.set_transform("deband_vf", 0)
    pump(0.5)

    a2 = must_shot(sh, "noisy-base-again", "关掉之后回到基线")
    check("去带/降噪全部关掉之后回到基线帧（链里没有残留的 0 参数档）",
          a2 is not None and diff_mean(a2, base) < 0.05 and p.vf_names() == [],
          f"与基线差={None if a2 is None else round(diff_mean(a2, base), 3)} 列表={p.vf_names()}")
    p.open_files([SAMPLE], append=False, remember=False)
    pump(1.5)


# ==================== 收尾：台账、判废与退出码 ====================

def report() -> int:
    fails = [n for n, ok, _d in results if not ok]
    total = len(results)
    print(f"\n==== 画面变换与增强：{total - len(fails)}/{total} 通过，失败 {len(fails)} 条，"
          f"判废 {len(env_notes)} 处 ====", flush=True)
    for n, ok, d in results:
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
        print("   这些既不算通过也不算产品失败：修好环境（内核起得来、宿主窗口可见、画面在动）重跑。",
              flush=True)
    return rc


# ==================== 证伪轮 ====================

def _fx(label: str, hit: bool, detail: str = "") -> None:
    f_case(label, bool(hit), detail)
    print(f"[{'CAUGHT' if hit else 'MISSED'}] {label}" + (f"  —— {detail}" if detail else ""),
          flush=True)


def _offline_fails() -> list[str]:
    """把离线半整套判据在**当前**（可能被摘掉承重件的）代码上重跑一趟，带回点名的失败项。

    台账临时换出去：这一趟是测量器，不写验收结果。崩溃单独记成一档并且**不算点名**——
    "坏实现把测量器弄崩了"和"判据认出了坏实现"是两件事，前者红着也要查清楚。
    """
    global results, env_notes, QUIET
    saved = (results, env_notes, QUIET)
    results, env_notes, QUIET = [], [], True
    try:
        offline_checks()
        found = [n for n, ok, _d in results if not ok]
    except BaseException as exc:
        found = [f"离线半在坏实现下自己崩了（这不算点名，要查）：{type(exc).__name__}: {exc}"]
    finally:
        results, env_notes, QUIET = saved
    return found


def _mutate(obj: Any, name: str, value: Any, keyword: str, label: str) -> None:
    """内存级摘件：坏实现活在内存里就够，**不动磁盘**。

    为什么不动磁盘：这个仓库没有版本控制，往产品文件里注一次坏就得靠"我自己记得还原"
    兜底；而这里要证的只是"判据对坏实现有没有牙"，离线半的判据读的就是内存里那个函数。
    还原用身份比对钉住，没还原成就直接判 MISSED。
    """
    old = getattr(obj, name)
    try:
        setattr(obj, name, value)
        fails = _offline_fails()
    finally:
        setattr(obj, name, old)
    restored = getattr(obj, name) is old
    hit = [f for f in fails if keyword in f]
    _fx(f"摘掉承重件「{label}」⇒ 离线半必须点名「{keyword}」", bool(hit) and restored,
        f"点名={hit[:1] or '一条没点名'} 其它红={len(fails)} 还原={restored}")


def _append_chain_impl(self):        # 坏实现：一条条 append 上去（实测会留残留，见 live 里的活证据）
    """替 --falsify 准备的"另一种写法"：形状判据读源码，所以它必须真的存在于一个文件里。"""
    for f in transform.FILTERS:
        e = f.expr(self.cfg.get(f.spec.cfg, f.spec.default))
        if e:
            self._req(["change-list", "vf", "append", e], timeout=3.0)
    return "", [], []


def falsify() -> int:
    print("==== 证伪轮：已知坏样本 + 当场摘掉产品承重件，判据必须逐条点名 ====", flush=True)

    # —— 任务清单点名的四条：一条都不许靠"看起来没事"过关 ——
    bad = disagree_pairs([("panscan", 0.35, 0.9), ("aspect_override", "4:3", -2.0)])
    good = disagree_pairs([("panscan", 0.35, 0.35), ("aspect_override", "4:3", 1.333333)])
    _fx("坏样本①表与内核不一致：内核回了个别值，点名器必须逐条点名并且不误报一致的那些",
        len(bad) == 2 and good == [], f"点名={bad} 误报={good}")

    res = j_append_residue(["format", "hflip"], ["format", "hflip", "format", "hflip"])
    res2 = j_append_residue(["format", "hflip"], ["format", "hflip", "hqdn3d"])
    res3 = j_append_residue(["format", "hflip"], ["format", "hflip"])
    _fx("坏样本②append 留残留：同一条链挂两次、或多挂一档无关滤镜，都必须点名；"
        "整条 set 的正样本不许误报", bool(res) and bool(res2) and res3 == [],
        f"重复={res} 多挂={res2} 正样本={res3}")

    lie = j_list_vs_truth(True, True, ["[vf] [w] Disabling filter unsharp."], False)
    lie2 = j_list_vs_truth(True, True, [], False)
    okv = j_list_vs_truth(True, True, [], True)
    offv = j_list_vs_truth(False, True, [], False)
    _fx("坏样本③缺 `format=yuv420p` 前缀：列表好看、像素一动不动 ⇒ 必须判 LIST_LIE"
        "（只看列表的判据在这条上会说\"已开启\"）；关掉却还挂着判 OFF_BUT_LISTED；三票一致才判 OK",
        lie[0] == "LIST_LIE" and lie2[0] == "LIST_LIE" and okv[0] == "OK" and offv[0] == "OFF_BUT_LISTED",
        f"带日志={lie[0]} 无日志={lie2[0]} 正常={okv[0]} 关而不掉={offv[0]}")

    D = transform.defaults()
    miss = j_reset(D, {k: v for k, v in D.items() if k != "deband_vf"} | {"panscan": 0.6})
    miss0 = j_reset(D, dict(D))
    _fx("坏样本④复位漏项：少回一项或回的不是默认值，必须点名；全对的复位不许误报",
        len(miss) == 2 and miss0 == [], f"点名={miss} 正样本={miss0}")

    # —— 阈值对照：每一条像素判据都要有"刚好没过线"的样本，否则它就是恒真 ——
    _fx("镜像判据不许被\"像素动了\"骗过去：与基线差 18.2、与翻转差也是 18.2 ⇒ 判不过；"
        "实测真镜像（0.070 vs 18.202）⇒ 判过；分母为 0 ⇒ 判不过",
        not j_mirror(18.202, 18.202)[0] and j_mirror(0.070, 18.202)[0]
        and not j_mirror(0.5, 0.0)[0],
        f"假={j_mirror(18.202, 18.202)} 真={j_mirror(0.070, 18.202)} 零差={j_mirror(0.5, 0.0)}")

    _fx("锐化判据有下限：1.400→1.401（噪声级）判不过，1.400→2.184（实测）判过",
        not j_sharpen(1.400, 1.401)[0] and j_sharpen(1.400, 2.184)[0],
        f"微动={j_sharpen(1.400, 1.401)} 实测={j_sharpen(1.400, 2.184)}")

    _fx("降噪判据要单调：三档持平／三档上扬都判不过，实测 0→7→10（13.719→13.452→13.038）判过",
        not j_denoise({0.0: 13.719, 7.0: 13.719, 10.0: 13.719})[0]
        and not j_denoise({0.0: 13.0, 7.0: 13.4, 10.0: 13.9})[0]
        and j_denoise({0.0: 13.719, 7.0: 13.452, 10.0: 13.038})[0],
        "三档对照见括号前一条判据")

    _fx("去带判据不吃\"干净样片上前后一样\"：逐像素差 0.0 判不过；降幅不足 0.10 判不过；"
        "实测噪声片段档 10（差 2.23、13.719→13.065）判过",
        not j_deband(1.400, 1.400, 0.0)[0] and not j_deband(13.719, 13.665, 2.0)[0]
        and j_deband(13.719, 13.065, 2.23)[0],
        f"空转={j_deband(1.400, 1.400, 0.0)} 弱={j_deband(13.719, 13.665, 2.0)}")

    _fx("黑边判据方向不能反：keep=no 反而更多黑 ⇒ 判不过；实测 0.261 vs 0.0055 判过；"
        "边条薄到 0.10-0.0055 判不过",
        not j_bars(0.0055, 0.261)[0] and j_bars(0.261, 0.0055)[0]
        and not j_bars(0.10, 0.0055)[0], "三点对照")

    _fx("panscan 判据方向不能反：0→1 反而更黑 ⇒ 判不过；实测 0.261→0.008 判过",
        not j_panscan(0.008, 0.261)[0] and j_panscan(0.261, 0.008)[0], "两点对照")

    _fx("平移判据不许被浮点噪声抬起来：0.0001→0.0002 判不过；实测 0.0002→0.35 判过",
        not j_pan(0.0001, 0.0002)[0] and j_pan(0.0002, 0.35)[0], "两点对照")

    _fx("比例判据按比值不按尺寸：1280×720 冒充 2.35:1 判不过，1280×544 判过，0 尺寸判不过",
        not j_ar(1280, 720, 2.35, 3e-3)[0] and j_ar(1280, 544, 2.35, 3e-3)[0]
        and not j_ar(0, 0, 1.0)[0], f"720={j_ar(1280, 720, 2.35, 3e-3)} 544={j_ar(1280, 544, 2.35, 3e-3)}")

    bad_chain = ("crop=trunc(min(iw,ih*235/100)/2)*2:trunc(min(ih,iw*100/235)/2)*2"
                 ":0:(ih-trunc(min(ih,iw*100/235)/2)*2)/2")
    _fx("逗号闸门认得本机那版内核的死法：带 min() 的裁剪串判不过，表里 abs 恒等式那版判过，"
        "空链不误判",
        not j_comma_gate("format=yuv420p," + bad_chain, 2)[0]
        and j_comma_gate("format=yuv420p," + transform.CROP.expr("2.35:1"), 2)[0]
        and j_comma_gate("", 0)[0],
        f"min 版首段={bad_chain[:24]}…")

    _fx("前缀规则有反向对照：前缀挂到链尾判不过、都不用滤镜却仍发链判不过、真链判过",
        not j_prefix({"sharpen": 1.0}, "unsharp=5:5:1.00:5:5:0.0,format=yuv420p")[0]
        and not j_prefix({"crop_ratio": "4:3"}, "crop=960:720:160:0,format=yuv420p")[0]
        and j_prefix({"sharpen": 1.0}, "format=yuv420p,unsharp=5:5:1.00:5:5:0.0")[0],
        "三种写法逐条比对")

    geo_only = transform.startup_args({k: transform.defaults()[k] for k in transform.ALL_KEYS})
    _fx("启动参数判据认得两种坏法：少发一条几何项判不过、空链还硬发 --vf= 判不过、正常判过",
        not j_startup(geo_only[:-1], "")[0]
        and not j_startup(geo_only + ["--vf="], "")[0]
        and j_startup(geo_only, "")[0],
        f"少一条={j_startup(geo_only[:-1], '')} 空 vf={j_startup(geo_only + ['--vf='], '')}")

    _fx("拒收判据要三元组才成立：发 360 停在旧值 0 算拒收，把 0 再发一遍\"没变\"不算，"
        "发 90 收回 90 也不算",
        j_kernel_refused(0, 360, 0) and not j_kernel_refused(0, 0, 0)
        and not j_kernel_refused(0, 90, 90), "三个已知答案")

    _fx("形状判据认得 append 版实现：同一函数体只把 set 换成 append 判不过，源码为空也判不过",
        not j_chain_wire('self._req(["change-list", "vf", "append", e], timeout=3.0)')[0]
        and j_chain_wire('self._req(["change-list", "vf", "set", chain], timeout=3.0)')[0]
        and not j_chain_wire("")[0], "含空源码对照")

    _fx("复位形状判据认得\"半复位\"两种写法：只遍历属性不清链判不过，只清链不遍历判不过",
        not j_reset_wire("for s in transform.PROPS: self.set_transform(s.prop, v)")[0]
        and not j_reset_wire("self.apply_filter_chain()")[0]
        and j_reset_wire("for s in transform.PROPS: ...\nself.apply_filter_chain()")[0],
        "两段假源码")

    # —— 摘掉产品承重件：离线半必须立刻变红，而且当场还原 ——
    _mutate(transform, "build_chain", lambda cfg: ",".join(transform.chain_parts(cfg)),
            "补前缀", "build_chain 不再补 format=yuv420p")
    _mutate(transform, "build_chain",
            lambda cfg: ",".join(transform.chain_parts(cfg) + [transform.SW_PREFIX])
            if cfg and transform.chain_parts(cfg) else "",
            "补前缀", "前缀挂到链尾（实测放后面内核照样 Impossible to convert）")

    def _crop_with_min(v):
        if not transform._ratio_wh(str(v or "")):
            return ""
        return "crop=trunc(min(iw,ih*235/100)/2)*2:trunc(min(ih,iw*100/235)/2)*2:0:0"
    _mutate(transform.CROP, "expr", _crop_with_min, "逗号闸门",
            "裁剪表达式改用 min()（本机这版会把链串劈开）")
    _mutate(transform, "normalize", lambda key, v: v, "夹紧与归一",
            "normalize 不夹紧也不取模（内核越界是整条拒收）")
    _mutate(transform, "mpv_value",
            lambda key, v: ("-1" if key == "aspect_override" and transform.ratio_float(v) < 0
                            else transform.normalize(key, v)),
            "下发形状", "自动档发回 -1（每次启动吃一条 deprecation 警告）")
    _mutate(transform, "same_value", lambda key, want, got: True, "回报比对",
            "same_value 恒真（\"界面动了、画面没动\"就此隐形）")
    _mutate(transform, "filter_failures", lambda log_text: [], "内核日志",
            "filter_failures 恒空（硬解帧上滤镜被扔掉没人知道）")
    _mutate(transform, "DISABLE_MARKS",
            tuple(m for m in transform.DISABLE_MARKS
                  if "append" not in m and "only 1 item" not in m),
            "内核日志", "把「append 带逗号被整条拒收」那句真机原话从标记表里摘掉（命令形状错了没人报）")
    _mutate(transform, "as_flag", lambda v: bool(v), "关掉的那一档返回空串",
            '布尔项退回裸 bool(v)（配置里手写 "0" 就成了"界面显示关着、画面上滤镜还挂着"）')
    _mutate(transform, "chain_names",
            lambda cfg: [f.key for f in reversed(transform.FILTERS)
                         if f.expr(cfg.get(f.spec.cfg, f.spec.default))],
            "链顺序契约", "链顺序倒过来（降噪白做，噪点又被锐化抬起来）")
    _mutate(transform, "reset_values",
            lambda: {k: v for k, v in transform.defaults().items() if k != "panscan"},
            "整族复位", "复位值少一项（Ctrl+G 之后画面留着上一轮的缩放）")
    _keep = dict(cfgmod.DEFAULTS)
    _mutate(cfgmod, "DEFAULTS", {k: v for k, v in _keep.items() if k != "sharpen"},
            "配置默认值从这里取", "配置侧自己另写一份默认值")
    try:
        from app.engine.player import MpvPlayer
        _mutate(MpvPlayer, "apply_filter_chain", _append_chain_impl, "链的下发形状",
                "整条 set 改成逐档 append")
    except Exception as exc:
        _fx("摘掉「整条 set 改成 append」⇒ 形状判据点名", False, f"读不到引擎：{exc!r}")

    _fx("所有摘件还原之后离线半重新全绿（\"还原\"不许是嘴上说的）",
        _offline_fails() == [], f"残留红={_offline_fails()}")

    missed = [n for n, hit, _d in falsified if hit is not True]
    caught = len(falsified) - len(missed)
    print(f"\n==== 证伪：{caught}/{len(falsified)} 条点名成功，漏网 {len(missed)} 条 ====", flush=True)
    for n in missed:
        print(f"  MISSED {n}", flush=True)
    return 1 if missed else 0


# ==================== 真机轮 ====================

HOST_W, HOST_H = 1280, 720
# 启动前就把配置摆成"链非空 + 几何非默认"：这样 start() 之后读回来的东西才有得比。
STARTUP_CFG = {"sharpen": 1.0, "crop_ratio": "2.35:1", "mirror_h": True,
               "rotate": "90", "panscan": 0.2}


def main() -> int:
    global APP
    if "--falsify" in sys.argv[1:]:
        return falsify()

    missing = []
    if not HAVE_PIL:
        missing.append(f"没有 numpy/PIL（{PIL_ERROR}）：像素判据没有量具")
    if not os.path.exists(SAMPLE):
        missing.append(f"缺样片：{SAMPLE}")
    if not os.path.exists(cfgmod.MPV_EXE):
        missing.append(f"缺播放内核：{cfgmod.MPV_EXE}")
    if missing:
        print("连测量对象都没有：" + "；".join(missing) + "\n⇒ 本轮不判产品好坏，rc=2", flush=True)
        return 2

    import inspect                                    # noqa: F401  离线半的形状判据要用
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication, QWidget
    from app.engine.player import MpvPlayer

    APP = QApplication.instance() or QApplication(sys.argv[:1])
    os.makedirs(OUT, exist_ok=True)
    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = OUT
    cfg["resume"] = False
    for k, v in transform.reset_values().items():
        cfg[k] = v                    # 从默认档起测：上一轮跑歪的配置不该把这一轮带进沟里
    cfg.save()

    # 离线半先跑：内核起不起来都有结论，而且下面的启动参数判据要拿它当参照。
    offline_checks()

    host = QWidget(None)
    host.setWindowFlags(Qt.Window)
    host.resize(HOST_W, HOST_H)
    host.show()
    pump(0.7)
    hwnd = int(host.winId())

    for k, v in STARTUP_CFG.items():
        cfg[k] = v
    want_args = transform.startup_args(cfg)
    chain0 = transform.build_chain(cfg)
    p = MpvPlayer(cfg)
    sh = None
    try:
        built = p.build_args(hwnd)
        not_spliced = [a for a in want_args if a not in built]
        check("表→启动参数：build_args 真的把这张表拼了进去（缺一条＝第一次播放前那几秒画面不对，"
              "且退出后配置与内核永远差一截）", not not_spliced,
              f"没拼进去={not_spliced} 表={want_args}")
        if not p.start(hwnd=hwnd, wait=14.0):
            env_note("真机半", f"内核没起来（{cfgmod.MPV_EXE}）：这一族本轮只拿到离线半的结论")
            return report()
        sh = Shot(p, hwnd)
        check("内核版本与这张表的实测依据一致：换了 mpv 就得重跑 probe_transform*，"
              "否则\"实测\"是别人机器上的数",
              transform.MPV_MEASURED_ON in str(p.version),
              f"本机={p.version!r} 表依据={transform.MPV_MEASURED_ON}")

        p.open_files([SAMPLE], remember=False)
        pump(2.0)
        p.set_prop("time-pos", 3.0)
        p.set_prop("pause", True)
        pump(0.8)
        listed0 = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
        check("启动参数落地：--vf 那一条链在 loadfile 之后就挂在内核上（不等界面补发）",
              listed0 == ["format", "unsharp", "crop", "hflip"],
              f"链={listed0} 期望=[format, unsharp, crop, hflip] 参数={chain0[:46]}")
        rot0, ps0 = p.get("video-rotate"), p.get("panscan")
        check("启动参数落地：几何非默认值内核直接收下（rotate=90、panscan=0.2 回读一致）",
              transform.same_value("rotate", "90", rot0)
              and transform.same_value("panscan", 0.2, ps0),
              f"回报 rotate={rot0!r} panscan={ps0!r}")

        p.reset_transforms()
        pump(0.6)
        check("复位之后链是空的（默认档一个滤镜都不该上链，裸 format=yuv420p 会白跑一趟 CPU）",
              p.vf_names() == [], f"链={p.vf_names()}")

        try:
            with open(p.log_path, "r", encoding="utf-8", errors="replace") as fh:
                whole = fh.read()
        except OSError as exc:
            whole = ""
            env_note("启动参数落地：日志判据", f"读不到内核日志（{exc!r}）")
        dep = [l.strip()[-90:] for l in whole.splitlines() if "is deprecated" in l.lower()]
        if whole:
            check("日志里没有一条 deprecation 警告：自动档发的是 `no` 而不是老 sentinel -1"
                  "（这条警告每次启动都打，会把\"日志里有没有真问题\"这个判据淹掉）",
                  not dep, f"警告={dep[:2]}")

        live(p, sh, cfg)
        noisy_live(p, sh)
    finally:
        try:
            if sh is not None:
                p.reset_transforms()
        except Exception:
            pass
        for k, v in transform.reset_values().items():
            cfg[k] = v
        cfg.save()
        p.quit()
        pump(0.4)
        host.close()
    return report()


if __name__ == "__main__":
    sys.exit(main())
