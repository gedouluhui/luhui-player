"""R3-5c 全面体检：把每一张界面都画出来、量一遍、留档成 PNG，供人眼逐张目检。

为什么和 test_ui.py 分开：test_ui 问的是"功能对不对"（属性、状态、接线），
这一份问的是"好不好看、有没有画坏"——它需要的是**每张界面各存一份像素**，
配五把机械尺（有没有墨、字有没有被截、被祖先切掉却没留线索、两块文字有没有叠在一起、
整块掉在横向裁剪框外）＋一张跨主题对比表，这样"改了主题色之后紫底白字看不见"这类问题
下次不会再靠人眼偶然发现。

屏幕休眠也能跑：纯 Qt 画的表面走 `render_to()`（离屏渲染到一张先铺好底色的图，实测过：
屏幕不出像素时它照样画出真东西），只有 mpv 的画面像素、DWM 阴影、抓屏遮挡闸门需要亮着的屏——
那部分走 `--screen`，屏幕不给像素就整段 rc=3（环境问题，不是产品失败）。

`--font-scale` 补的是这一把量具自己的档位盲区：这几把尺从来只在**产品默认字号**上跑过，
而"格子放不放得下这行字"是个随字号变的事实——同一张界面，13px 全绿、16px 可能整排被裁。
所以换档跑不是"多做一遍重复劳动"，是把结论的适用半径写清楚：以后要真给用户提供界面字号
（§5 外观那一条），这一档就是它的验收；不换档跑过，就不许说"任何字号都不裁字"。
本轮实测的适用半径（一档一个进程；2026-10-11 在页签换行改造**之后**的代码上重跑，各档日志 tests/logs/capture_sweep-*.log）：
**1.0 / 1.5 / 3.0 / 3.5 全绿**（标准档再加 --screen 是 50 条），页签条在高字号档自动排成两排，所以改造前那笔 23×72px 的真重叠不再出现；
4.0 起红的是**压扁尺**（媒体库说明实得高 78 < 需要 122、统计页 172 < 183），5.0 起加上按钮截字（「采样 3 秒」需 272 > 可用 246），6.0 侧栏五页全红（当时侧栏五页）——
所以 1.0–3.0 是契约档，4.0/6.0 是探边档，红了不算出厂缺陷，
但必须能点名是谁越界（这几档确实红了，见 `[FAIL] 侧栏·…`；要修的是每列给几行高，不是字号设置）。

退出码：
  0 每张表面都画得出来，五把尺与对比表全过
  1 任何一条判据红（包括非表面级的那几条：样片载入、对比表、六色换色），不只是"某张表面没过尺"
  2 量具自己跑不了：缺样片，或者"换主色"那把尺缺 numpy／两张基准尺寸对不上
    （环境问题，绝不冒充产品失败；但会点名，不静默跳过）
  3 `--screen` 这条路此刻没有像素可量（前提：前面没有产品红，有红按 1 报）

换主色那把尺（`accent_switch_px`）逐像素数面积，需要 numpy；没有它这两条走 rc=2。

用法：
  python tests/capture_sweep.py                 # 全部表面，离屏渲染
  python tests/capture_sweep.py --only bar      # 只跑名字里含 bar 的（调试用）
  python tests/capture_sweep.py --screen        # 再加抓屏那几条（要屏幕亮着）
  python tests/capture_sweep.py --falsify       # 五把尺的已知答案对照：坏样本必须被抓住
  python tests/capture_sweep.py --font-scale 1.3 # 整张界面字号放大三成再走一遍（存图另开目录）
"""
from __future__ import annotations

import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 这份量具在 show()/render_to() 之后**立刻**抓像素，而换页纱会在头 170ms 把画面压暗、
# 悬停过渡也会让底色带着半截透明度——像素尺会把"正处在过渡里"的中间帧当成产品颜色。
# 全部关掉动效（与系统"减少动画"走同一条路）：这里的结论只该关于**稳定态**。
os.environ.setdefault("GELU_NO_MOTION", "1")

from PyQt5.QtCore import QPoint, Qt                        # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter, QPen, QPixmap  # noqa: E402
from PyQt5.QtWidgets import (QAbstractButton, QAbstractScrollArea, QApplication, QDialog,   # noqa: E402
                             QComboBox, QHBoxLayout, QLabel, QMenu, QScrollArea, QStyle,
                             QToolButton, QVBoxLayout, QWidget)

import test_ui as TU                                      # noqa: E402
from app import config as cfgmod                          # noqa: E402
from app.ui import menus, theme                           # noqa: E402
from app.ui.main_window import SPEEDS, MainWindow          # noqa: E402

try:
    import numpy as np                                    # noqa: E402
except ImportError:
    np = None

OUT = os.path.join(TU.OUT, "sweep")
# 浮层实际压在**视频**上，不是压在窗口底上：离屏渲染时那张底色必须是画面的近黑，
# 用 theme.BG 会把"气泡压在亮画面上"这层关系量成"气泡压在深色面板上"。
VIDEO_BLACK = "#0B0D10"
results: list[tuple[str, bool, str]] = []
# 量具自己没量成的那几条（缺 numpy、基准图尺寸对不上）：不算产品红，但也不能当没发生。
unmeasured: list[str] = []
LUM_OK = 4.5          # 正文字（12~13px）按 WCAG-AA 的 4.5:1 夹
LUM_BIG = 3.0         # 大字/图形件按 3:1
# 换主色那条尺的面积下限。**出处**：2026-10-10 逐档字号实跑，各档 log 里这把尺自己的读数
# （每档取五个非默认色中最小那一读，单位 px²）：
#   1.0 档 5298（sweep-font1.0-20261010-035003.log）、1.15 档 5395、1.3 档 5567、1.5 档 5726、
#   1.8 档 6083、2.0 档 6291、2.5 档 7063、3.0 档 7776（对应 sweep-font<档>-20261010-034*.log）。
# 读数随字号单调上涨，是因为字号越大那些图标/胶囊/描边本身也越大——这把尺量的是绝对面积，
# 所以每一档都得单独看，不能拿标准档的数去夹放大档（反过来也一样：标准档不能借放大档的余量）。
# 下限取最低读数 5298 的约 38%：主色真落到像素上时它占的是"图标＋选中态填充＋进度条＋
# 若干描边"这一整组，若漏掉一半用途（theme.py 记的第 3 条派生坑）读数会掉到 2000 以下；
# 而同一份代码连跑两次的抖动只有 ±1%（上表里 1.0 档五读 5298~5329），用不到这半个量级的余量。
# `tests/probe_accent_area.py` 是这条下限的定标探针（读留档 PNG 而不是读日志，两路能对上才敢用）。
# 以前这条闸门是"采样点 ≥ 60"，那是**抽样口径**下的手感数——同一个口径在 2.5 档只捞到 48 点就红了五条
#（产品什么都没变，见 `accent_switch_px` 的说明），所以整个口径换成逐像素、下限按上面的实测重定。
ACCENT_AREA_MIN = 2000


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def exit_code(rs: list[tuple[str, bool, str]], nas: list[str]) -> int:
    """这轮该以什么退出码收场：**看全部判决**，不看"表面级的那几条"。

    以前这段逻辑内联在 `run()` 结尾，而且只 `if fails: return 1`——`fails` 装的是 SURFACES
    那张表里的表面，而"样片真载进来了""主题令牌对比表全过""六色真的改动到主色"这几条
    只进 `results` 不进 `fails`。于是它们红了也 rc=0：外层任何脚本化闸门（doctor、看门狗、
    以后 CI）都会把"有红"读成"全绿"，这比红本身坏得多。抽成函数还有一层用处：
    退出码的优先级（产品红 > 量具不作数 > 全绿）能在 `--falsify` 里拿已知答案对照，
    内联的那段没法对照。
    """
    if any(not ok for _nm, ok, _d in rs):
        return 1
    if nas:
        return 2
    return 0


def measure_unavailable(name: str, detail: str) -> None:
    """量具自己此刻量不了（没有 numpy、两张基准尺寸对不上）：点名登记，退出码走 rc=2。

    为什么单开一条道：这类情况以前是**当成产品 FAIL** 报的（`--only accent` 那一轮红过五条
    「两张尺寸不一样」），而红的原因是基准图根本没画全，跟"主色有没有落到像素上"没关系。
    把环境问题算到产品头上，下次真红就混在假红里看不见了；但也不能不吭声——
    所以这里既不打 PASS 也不打 FAIL，打 `[NA]` 并记账，`run()` 见到账上有名字就直接 rc=2。
    """
    unmeasured.append(f"{name} —— {detail}")
    print(f"[NA] {name}  —— {detail}（量具不作数，不是产品判负）", flush=True)


# ---------------------------------------------------------------- 五把尺
def _stride(img: QImage, cap: int = 220_000) -> int:
    """采样步长：整窗 2400×1500 逐像素要 360 万次 pixelColor（Python 里要跑十几秒），
    限制在约 cap 个采样点上，20 万像素足够把"有没有墨"量准，误差不影响判据方向。"""
    n = img.width() * img.height()
    return max(1, int((n / cap) ** 0.5))


def panel_ink(img: QImage) -> tuple[float, str]:
    """墨迹尺：**和这张图的众数色不一样**的像素占比。

    为什么不用 test_ui 里的 has_ink / has_picture：那两把尺量的是 24×12 网格上的
    "明暗跨度/标准差/颜色数"，用来判"这块区域是不是真有一帧画面"很好，用来判
    "这张整屏界面有没有画坏"会系统性误杀——深色整窗里绝大多数像素都是同一块底，
    标准差天然小，而文字只占百分之几。这里换成"底 vs 墨"的占比：
    众数色当底（界面 90% 以上都是底），凡是离它够远的都算墨。
    """
    # 收口在入口：grab() 给的是 QPixmap，只有抓屏那条给 QImage，
    # 两路都要能量（写死一条的话另一路是 AttributeError，而 AttributeError 会伪装成"这张画不出来"）
    if hasattr(img, "toImage"):
        img = img.toImage()
    if img.isNull() or img.width() < 8 or img.height() < 8:
        return 0.0, "空图（<8×8）"
    img = img.convertToFormat(QImage.Format_ARGB32)
    step = _stride(img)
    buckets: dict[tuple[int, int, int], int] = {}
    pts: list[tuple[int, int, int, int]] = []
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            c = img.pixelColor(x, y)
            pts.append((c.red(), c.green(), c.blue(), c.alpha()))
            k = (c.red() >> 4, c.green() >> 4, c.blue() >> 4)
            buckets[k] = buckets.get(k, 0) + 1
    if not buckets:
        return 0.0, "采不到点"
    # 众数桶的代表色取桶里第一个真实像素，不用桶中心（(7,7,7)*16 会和实际底色差出一截）
    bg = max(buckets, key=buckets.get)
    base = next((r, g, b, a) for (r, g, b, a) in pts
                if (r >> 4, g >> 4, b >> 4) == bg)
    ink = 0
    for (r, g, b, a) in pts:
        # 全透明不算墨：分层窗口（OSD/预览）圆角外沿是 alpha=0，
        # 那里的 RGB 是未定义垃圾值，当成"有墨"会把一张空浮层判成画满了东西。
        if a < 24:
            continue
        if abs(r - base[0]) + abs(g - base[1]) + abs(b - base[2]) > 60:
            ink += 1
    frac = ink / len(pts)
    return frac, (f"墨迹占比 {frac * 100:.2f}%（采样 {len(pts)} 点，"
                  f"底色 rgb{base[:3]} 占 {buckets[bg] * 100 // len(pts)}%）")


def _pixels(img: QImage):
    """把 QImage 摊成 (H, W, 4) 的 uint8（内存序 BGRA），逐像素、不抽样。

    两处必须小心：
      · `bytesPerLine` 可能有行尾填充，直接 reshape 成 (H,W,4) 会错位（宽不是 4 的倍数时整张图斜掉）；
      · `constBits()` 给的指针**不持有**那张图，QImage 一被回收就是悬空内存——所以先 `bytes()` 拷出来
        （几 MB 的 memcpy，换来的是"量到一半图被释放"这种只会偶发、又只会伪装成乱值的坑）。
    """
    img = img.convertToFormat(QImage.Format_ARGB32)
    bpl, h, w = int(img.bytesPerLine()), img.height(), img.width()
    buf = img.constBits()
    buf.setsize(bpl * h)
    data = bytes(buf)
    rows = np.frombuffer(data, dtype=np.uint8).reshape(h, bpl)
    return np.ascontiguousarray(rows[:, :w * 4].reshape(h, w, 4))


def accent_switch_px(base, cur, tokens: tuple[str, ...]) -> tuple[int, str]:
    """换主色**真的改动到的像素面积**：同一坐标两张不一样，且改动后那份颜色≈主色的三种用途之一。

    为什么这条比"墨迹占比"合适用在这里：控制条＋侧栏这张图 98% 是平的窗口底，
    墨迹尺量到 0.8% 是实话（界面本来就该这么安静），拿 2% 当下限等于把"设计得克制"判成坏。
    真正要问的是"**换了主题色之后，主色到底落到像素上没有**"——所以两张相减再验色相：
      · 若 set_accent 只改令牌不重发样式表（theme.py 开头记的第 2 条坑），两张一模一样 → 0；
      · 若主色只喂给了图标、填充那一条规则漏了（第 3 条派生坑），改动面积会掉到远低于下限。
    容差按 L1≤96：抗锯齿会把主色和底混出过渡边，只取精确值会漏掉整条描边。

    为什么是**逐像素**而不是抽样（这一条改过两次，第二次才是对的）：
      第 1 版报"采样点个数"，`_stride` 把点数封顶在 ~22 万，于是步长随图大小涨——主色那几处
      都是 1~2px 的描边和小图标，同一个界面在 1.0 档量到 314 点、2.5 档只剩 48 点，
      红了五档"换色失败"（`logs/sweep-font2.5-*.log` 之前那轮），可产品什么都没变。
      第 2 版改成"点数 × 步长²"想折算回面积：实测 1.0 档 5024px²、2.5 档 1200px²，
      依然不随字号守恒（细线在隔步采样下是"每 step 命中一次"，折算系数本身就带 step，
      不是面积而是"面积 × 步长"的一个近似）。抽样把方向都判反了，说明这把尺在这条判据上
      就不该抽样——逐像素数出来的就是像素面积，`--font-scale` 各档才互可比。
    成本：整张 300 万~800 万像素，numpy 向量化后每对 ~0.2s，六种主题色五对 ≈ 1s。
    numpy 不在场时这条**不作数**（见 `run()` 里的缺件闸门），不许悄悄退回抽样把红算到产品头上。
    """
    if hasattr(base, "toImage"):
        base = base.toImage()
    if hasattr(cur, "toImage"):
        cur = cur.toImage()
    if base.size() != cur.size():
        return -1, f"两张尺寸不一样（{base.width()}×{base.height()} vs {cur.width()}×{cur.height()}）"
    if np is None:
        return -1, "没有 numpy：这把尺量不了（量具缺件，不是产品问题）"
    a, b = _pixels(base).astype(np.int16), _pixels(cur).astype(np.int16)
    diff = np.abs(a - b)
    changed = diff[..., :3].sum(axis=2) > 24
    vis = np.minimum(a[..., 3], b[..., 3]) >= 24       # 任一边透明的像素没画出来，不算改动
    near = np.zeros(a.shape[:2], dtype=bool)
    keep = np.empty_like(diff[..., :3])                # 复用缓冲：三档令牌各算一次，不各留一份大的
    for c in tokens:
        w = QColor(c)
        np.abs(b[..., :3] - np.array([w.blue(), w.green(), w.red()], dtype=np.int16), out=keep)
        near |= keep.sum(axis=2) <= 96
    n = int((changed & near & vis).sum())
    tot_px = a.shape[0] * a.shape[1]
    return n, (f"改动到主色的像素 {n}px²（占这张图 {n * 100.0 / max(1, tot_px):.3f}%，"
               f"图 {a.shape[1]}×{a.shape[0]}，逐像素不抽样）")


def chrome_strips(img: QImage, rect: tuple[int, int, int, int]) -> QImage:
    """整窗图里把**视频区那块**裁掉，只留外壳四边（上/下/左/右三条带拼起来）。

    为什么不直接量整窗：视频区在离屏渲染里必然是一整块底色（原生子窗口不归 Qt 画），
    它占整窗 80% 以上面积，于是"整窗墨迹 1.1%"这种数字量的其实是"视频区有多大"，
    而不是"外壳画没画出来"。裁掉之后这张尺才回到它想问的问题上。
    """
    x, y, w_, h_ = rect
    W, H = img.width(), img.height()
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w_), min(H, y + h_)
    parts = [img.copy(0, 0, W, y0), img.copy(0, y1, W, max(0, H - y1))]
    if x1 > x0 and y1 > y0:
        parts += [img.copy(0, y0, x0, y1 - y0), img.copy(x1, y0, max(0, W - x1), y1 - y0)]
    parts = [p for p in parts if p.width() >= 2 and p.height() >= 2]
    gap = 4
    total_h = sum(p.height() for p in parts) + gap * max(0, len(parts) - 1)
    out = QImage(max([p.width() for p in parts] or [1]), max(1, total_h), QImage.Format_ARGB32)
    out.fill(QColor("#000000"))
    p = QPainter(out)
    yy = 0
    for part in parts:
        p.drawImage(0, yy, part)
        yy += part.height() + gap
    p.end()
    return out


def render_to(w: QWidget, backdrop: str) -> QImage:
    """把这块控件连同子孙画进一张**先铺好底色**的图里（离屏渲染，不经过屏幕）。

    为什么不用 `widget.grab()`：QSS 里那条通用 `QWidget { background: transparent; }`
    让 grab() 把"没画到的地方"留成纯黑，而屏幕上那里是窗口底（侧栏卡片之间、
    对话框整块背板、浮层圆角外沿）。目检要看的是用户那一版，所以目标先铺成
    它实际压在下面的那块颜色，再 render 进去。
    已知量不到的：mpv 的画面像素（原生子窗口不在 Qt 的绘制体系里，这里必然是一整块底色），
    所以"有没有真帧"仍然只有 `--screen` 那条路能证（以及 test_ui 的抓屏判据）。
    """
    dpr = w.devicePixelRatioF() or 1.0
    ww = max(1, int(round(w.width() * dpr)))
    hh = max(1, int(round(w.height() * dpr)))
    img = QImage(ww, hh, QImage.Format_ARGB32)
    img.setDevicePixelRatio(dpr)
    img.fill(QColor(backdrop))
    p = QPainter(img)
    # flags 一律**不点名**：本机 PyQt5 的 QWidget.RenderFlag 是个没有成员的空壳枚举
    # （dir() 里只有 int 的那些方法），写 QWidget.RenderDrawChildren 还是
    # QWidget.RenderFlag.RenderDrawChildren 都是 AttributeError——和当年 QLocalServer.NoError
    # 同一类坑（枚举成员没被重导出，tests/audit_qt_attrs.py 就是为扫这一类写的）。
    # render() 的默认 flags 本来就是"连子控件一起画"，少写这一个参数既是对的也不给坑留门。
    # 只给 painter：剩下三个默认值正是我们要的（原点 (0,0)、整块、默认 flags 里
    # 已经含 DrawWindowBackground 与 DrawChildren）。
    # 为什么不点名 flags——本机 PyQt5 的枚举成员名是 DrawChildren 而不是 Qt 文档里的
    # RenderDrawChildren，而 QWidget.RenderFlag 这个空壳上又查不到它（dir() 只有 int 的
    # 那些方法）；写哪一种都是 AttributeError 或 TypeError。同一类坑还有当年的
    # QLocalServer.NoError：枚举成员没被重导出（tests/audit_qt_attrs.py 扫的就是这类）。
    w.render(p)
    p.end()
    return img


def clipped_labels(root: QWidget) -> list[str]:
    """截字尺：控件的文字宽度需求 > 它实际能用的内容宽度。

    量的是 QLabel 与带文字的按钮。跳过三类不该报的：开了自动换行的（本来就靠换行放得下）、
    没显示的（宽度是 0，量了是自欺）、以及带图标的按钮（要先把图标那一格扣掉）。
    """
    bad: list[str] = []
    cands: list[QWidget] = list(root.findChildren(QLabel))
    cands += [w for w in root.findChildren(QAbstractButton) if w.text()]
    if isinstance(root, QLabel):
        cands.append(root)
    for w in cands:
        if not w.isVisible() or isinstance(w, QMenu):
            continue
        txt = w.text()
        if not txt:
            continue
        # wordWrap 只有 QLabel 有（按钮没有这个属性，点名会 AttributeError）；
        # 开了换行的标签靠换行放得下，本来就不该算截字。
        if isinstance(w, QLabel) and w.wordWrap():
            continue
        fm = w.fontMetrics()
        need = fm.horizontalAdvance(txt)
        cr = w.contentsRect()
        avail = cr.width()
        if isinstance(w, QAbstractButton) and not w.icon().isNull():
            # 图标那一格＋Qt 给图标留的间距都要扣掉，否则带图标的按钮会集体"被截字"
            avail -= w.iconSize().width() + w.style().pixelMetric(
                QStyle.PM_LayoutHorizontalSpacing)
        avail -= 2 * w.style().pixelMetric(QStyle.PM_DefaultFrameWidth) // 3
        if avail > 0 and need > avail + 1:
            bad.append(f"{type(w).__name__}「{txt[:14]}」需 {need}px > 可用 {avail}px")
    return bad


def clipped_without_hint(root: QWidget) -> list[str]:
    """裁而未告尺：文字被切掉了，却既没有省略号、也没有 tooltip 兜底。

    这把尺补的是 clipped_labels 的一个结构性盲区（统计页的数值格就是从这儿漏过去的）：
    那一格的宽度**等于**它需要的宽度（271px 的字放 271px 的格），按"格放不下"量是零告警；
    真正把右端切掉的是**祖先**——整块表单被数值格的最小宽度顶到 354px，而视口只有 268px，
    横向滚动条又常关，于是屏幕上只剩「H.264 / AVC / MPEG-4 AVC / MP」。
    所以这里量的可用宽度取两个来源的**较小值**：控件自己的内容宽，和 clipped_rect()
    算上滚动裁剪之后真正露出来的宽。

    放过的三种：开了换行的（靠第二行放得下）、文字里已经带省略号的（切是切了，但留了
    "这里还有字"的线索）、有 tooltip 的（整句在悬停里）。剩下那种"悄悄切掉"才是产品缺陷。
    """
    bad: list[str] = []
    cands: list[QWidget] = list(root.findChildren(QLabel))
    cands += [w for w in root.findChildren(QAbstractButton) if w.text()]
    if isinstance(root, QLabel):
        cands.append(root)
    for w in cands:
        if not w.isVisible() or isinstance(w, QMenu):
            continue
        txt = w.text()
        if not txt or (isinstance(w, QLabel) and w.wordWrap()):
            continue
        need = w.fontMetrics().horizontalAdvance(txt)
        own = w.contentsRect().width()
        vis = clipped_rect(w, w.window()).width()
        avail = min(own, vis)
        if avail > 0 and need > avail + 1 and "…" not in txt and not str(w.toolTip()).strip():
            bad.append(f"{type(w).__name__}「{txt[:14]}」需 {need}px，只露出 {avail}px"
                       f"（自身 {own}px）且无省略号/tooltip")
    return bad


def squeezed_widgets(root: QWidget) -> list[str]:
    """压扁尺：带文字的控件**实得尺寸小于样式自己要的尺寸**（宽用 sizeHint，高用按实得宽算的真需求）。

    为什么还要第三把（前两把在这个 bug 上是结构性失明的）：那两把量的都是"这个格子放不放得下
    这行字"，而侧栏页签的故障形状是**布局把格子压小了**（这条发现于五页签时代）。`contentsRect()` 不含 QSS 的
    padding（本机实测：文字 48px、格子 54px、真正留给字的只有 34px），于是"48 放 54"两把尺
    都判"放得下"；`clipped_without_hint` 又按设计放过挂着 tooltip 的控件，而页签个个都有 tooltip。
    `sizeHint()` 是样式把 padding、边距、最小宽都算进去之后自己报的"我要多宽"，
    布局给不到就是在压一个压不动的东西——压出来的结果就是字被切一半。

    高度那一档**不能**照抄 sizeHint（tests/probe_squeeze_height.py 实测）：开了 wordWrap 的
    标签，sizeHint 是在**不知道最终宽度**的情况下猜的行数——快捷键页那句提示实得宽 944、
    一行就放得下，sizeHint 却报 180×60（3.3 行），照抄就是把每一页的说明文字都判成压扁。
    真需求是 `heightForWidth(实得宽)`：同一批标签它报 18/21/18，与实得逐项相等；而"宽度只够
    两行、高度只给一行"那种真切掉的，它报 81 vs 实得 18，照样抓得住。没开换行的没有
    heightForWidth（探针里 hasHeightForWidth=False），退回 sizeHint 高，半行高的对照件仍被抓。

    放过的四类：没显示的（量了是自欺）、文字里已经带省略号的（切是切了但留了线索）、
    没有文字的纯图标按钮（压扁它不产生截字）、以及**换行标签的宽度**（靠换行放得下是设计，
    它的真实代价由上面那档高度来管）。

    这把尺的**已知宽报半径**（tests/probe_label_rect.py 实测，用之前得先知道）：
    `sizeHint` 在"文字 + 2×QSS padding"之外还带一份 Qt 给 ToolButton 的通用松弛——
    本机 12px 字号下 `border:none` 的页签是 +11px，没写 border 的对照件是 +17px。
    所以"实得宽正好等于文字+padding"的格子也会被它报一句。这不是缺陷而是分工：
    它报的是**布局压破了控件自己报的最小需求**这一类（这一档也许还没切字，但字号一大、
    换台 175% 缩放的机器就切），"字此刻到底少没少"由前两把尺管。产品侧因此把页签钉在
    「文字+2×留白」与 sizeHint 的**大值**上（app/ui/main_window.py 的 `_layout_tabs`），
    两边对得上，sweep 才既不漏抓也不假红。
    """
    bad: list[str] = []
    cands: list[QWidget] = list(root.findChildren(QLabel))
    cands += [w for w in root.findChildren(QAbstractButton) if w.text()]
    if isinstance(root, (QLabel, QAbstractButton)) and root.text():
        cands.append(root)
    for w in cands:
        if not w.isVisible() or isinstance(w, QMenu):
            continue
        txt = w.text()
        if not txt or "…" in txt:
            continue
        wraps = isinstance(w, QLabel) and w.wordWrap()
        hint = w.sizeHint()
        cw, chh = w.width(), w.height()
        if not wraps and cw > 0 and hint.width() > 0 and cw + 1 < hint.width():
            bad.append(f"{type(w).__name__}「{txt[:14]}」被压扁：实得宽 {cw}px < sizeHint "
                       f"{hint.width()}px（文字本身 {w.fontMetrics().horizontalAdvance(txt)}px）")
            continue
        need_h = w.heightForWidth(cw) if (wraps and w.hasHeightForWidth() and cw > 0) else hint.height()
        if chh > 0 and need_h > 0 and chh + 1 < need_h:
            bad.append(f"{type(w).__name__}「{txt[:14]}」被压扁：实得高 {chh}px < 需要 "
                       f"{need_h}px（{'按实得宽 ' + str(cw) + 'px 算的行高' if need_h != hint.height() else 'sizeHint 高'}）")
    return bad


def clipped_rect(w: QWidget, top: QWidget):
    """控件真正**被画出来**的那块矩形（换算到 top 坐标系），父容器与滚动裁剪都算进去。

    为什么不能直接用 mapTo 的矩形：QScrollArea 里的内容整块都比视口高（统计页二十多行），
    藏在视口下面的那几行 mapTo 出来的 y 会落到滚动区**外面**——正好压在视口下方的按钮行上。
    矩形说"压在一起"，像素上根本没画出来，这是一类典型的"rect 与像素不同源"误报：
    上一版就是靠这条把统计页判成重叠（「IPC 往返」×「采样 3 秒」@(12,551)）。

    循环写成"先判这一层、再决定停不停"：`top` 自己也可能就是那个滚动区（裁而未告尺的
    对照件正是这么摆的——一个顶层 QScrollArea 同时当 root 和 top）。旧写法
    `while a is not top` 会连 top 自己的视口都不算，于是产品没坏、对照先漏。

    为什么每一层普通父容器也要求交（2026-10-10 补）：Qt 把子控件裁到父控件矩形内，这件事
    **不需要**它是滚动区。旧写法只对 `QAbstractScrollArea` 求交，于是"侧栏那一排比侧栏宽"
    这种形状里，最后一格整块被裁掉、点不到也看不见，而尺子报"露出 320px"（--falsify 里
    那条正样本当场量到的就是这个数）。`top` 自己那层照旧算（循环是"先求交、再判停"），
    所以 `top` 是顶层窗口时那圈边界也裁得到——第一版补丁在这里加了个"跳过 isWindow 的层"，
    正好把对照件要测的那种"顶层小窗裁掉宽子控件"跳没了，改回来并留这条注释。
    """
    r = QRectOf(w, top)
    a = w.parentWidget()
    while a is not None:
        r = r.intersected(QRectOf(a.viewport() if isinstance(a, QAbstractScrollArea) else a, top))
        if a is top:
            break
        a = a.parentWidget()
    return r


def _scroll_owner(a: QWidget):
    """这一层父控件属于哪个滚动区：滚动区自己，或它的 `viewport()`（内容块挂在 viewport 下）。

    为什么要认 viewport：Qt 把内容块（`setWidget()` 那块）的父级设成 `qt_scrollarea_viewport`，
    而 viewport 是个普通 QWidget。祖先链从里往外走时**先到 viewport、后到滚动区**，
    只在 `isinstance(a, QAbstractScrollArea)` 那一支才看横向滚动策略的写法，会在 viewport
    这一层就把区间交空并提前返回——"能滚所以不算丢"那档永远轮不到被读（--falsify 里
    那条对照当场 MISSED，量到的裁剪来源写着 `QWidget「qt_scrollarea_viewport」宽 108`）。
    """
    if isinstance(a, QAbstractScrollArea):
        return a
    p = a.parentWidget()
    if isinstance(p, QAbstractScrollArea) and a is p.viewport():
        return p
    return None


def x_spans(w: QWidget, top: QWidget) -> tuple[tuple[int, int], tuple[int, int], str]:
    """**只按横向**算两件事：控件自己的 x 区间、以及横向真正露出来的 x 区间（top 坐标系）。

    为什么不能拿 `clipped_rect()` 的宽当判据（本机实测，PyQt5 5.15.10）：
    `QRect(150,10,100,20).intersected(视口).width()` 是 0，
    而 `QRect(10,500,80,20)`（横向完全在视口内、纵向滚到视口下方）intersected 出来**也是 0**——
    Qt 的 intersected 只要任一轴不相交就把整个矩形判 null，两个轴混在一个数里。
    拿它当"掉出右沿"会把每一页滚出视口的那十几行全判成缺陷（竖向滚出是滚动区的本职，用户能滚到）。
    所以这里自己走祖先链，只交 x 区间，纵向一律不看。

    滚动区那一层分两种：横向**能**滚（策略不是 AlwaysOff 且 range>0）时这层根本不参与裁剪——
    内容只是暂时在视口外，用户拖滚动条找得回，报了就是把"设计如此"判成缺陷；
    横向关了（侧栏那三页正是这一档）才是"掉出去找不回"。

    第三个返回值是"最后管着这条横向区间的是谁"，给报错用，两种滚动区说得清清楚楚。
    """
    p = w.mapTo(top, QPoint(0, 0))
    own = (int(p.x()), int(p.x()) + max(0, int(w.width())) - 1)
    vis = own
    who = f"{type(top).__name__}"
    a = w.parentWidget()
    while a is not None:
        area = _scroll_owner(a)
        if area is not None:
            can_h = (area.horizontalScrollBarPolicy() != Qt.ScrollBarAlwaysOff
                     and area.horizontalScrollBar().maximum() > 0)
            who = (f"{type(area).__name__}视口 横向滚动"
                   + ("能滚（藏得住但找得回）" if can_h else "＝关（掉出去找不回）"))
            clip = None if can_h else area.viewport()
        else:
            clip = a
            who = f"{type(a).__name__}「{a.objectName() or ''}」宽 {a.width()}"
        if clip is not None:
            cp = clip.mapTo(top, QPoint(0, 0))
            span = (int(cp.x()), int(cp.x()) + max(0, int(clip.width())) - 1)
            vis = (max(vis[0], span[0]), min(vis[1], span[1]))
            if vis[0] > vis[1]:
                return own, vis, who
        if a is top:
            break
        a = a.parentWidget()
    return own, vis, who


def _label_of(w: QWidget) -> str:
    """这个控件自己写出来的字（QComboBox 没有 text()，只有 currentText()）。"""
    for attr in ("text", "currentText"):
        f = getattr(w, attr, None)
        if callable(f):
            try:
                s = str(f())
            except Exception:
                continue
            if s:
                return s
    return ""


def _out_of_view(w: QWidget) -> bool:
    """这一层自己是否整块横向在裁剪框外（宽 ≤0 的层不算——那是没摆过，不是掉出去）。"""
    if w.width() <= 0:
        return False
    _own, vis, _who = x_spans(w, w.window())
    return vis[0] > vis[1]


def beyond_viewport(root: QWidget) -> list[str]:
    """掉出视口尺：控件**整块横向**落在裁剪框外——看不见也点不到，而前四把尺一条都不报。

    这把尺补的是 2026-10-10 才看见的一类盲区。前三把（截字/裁而未告/压扁）比的都是
    "这一格放不放得下这一行字"，而 `QScrollArea` + `setWidgetResizable(True)` + 横向滚动条关闭
    的坏形状是**另一件事**：内容块整体比视口宽，右边那些控件**自己一点没被压**（格是宽的、
    字是全的），只是落在了裁剪框外——裁而未告那把会看见它"需要 103px、露出 488px"于是放行
    （那 488 是它自己那一整格跟视口交出来的左半段，字正好排在左半段里），
    而同一行末尾的数值胶囊整块在视口右沿以外，**它没被压、只是看不见**。

    真树上的读数（`--font-scale` 一档一个进程，`tests/logs/probe-fifth-headroom-*.log`）：
    1.0/1.3/1.8/2.0/2.5/3.0/3.5 越界 **0 个**（侧栏靠 `_fit_sidebar` 长到 300→520 一路兜住）；
    4.0 起有控件**露出一点但被切**（画面 16 个、统计 7 个，归裁而未告那把管）；
    6.0 起才是这一把的形状：调节 5 个、画面 5 个、统计 1 个整块在外面
    （`sweep-font6.0-*.log` 里那句 `QLabel「0」整块在裁剪框外（自己 x=1188..1284，横向露出 0px，
    裁它的是 QScrollArea视口 横向滚动＝关）`）。
    换句话说这把尺今天管的是**未来那档"界面字号"设置**和"窗口被拖窄 + 字号调大"的组合，
    不是当前出厂档的缺陷——当初它报的"2.5 档 50 个、3.0 档 66 个"是量具自己算错的
    （`_scale_qss` 换档时拿上一档产物再乘一遍，见 `_scale_qss` 的说明），已订正。

    只管横向，纵向交给滚动区（滚出视口的行用户滚得到，报了就是噪声）。
    只报"整块在外面"的：露出哪怕 1px 的那类仍由裁而未告尺按"字全不全"管，两边不重复点名。
    横向能滚的滚动区里不报（找得回）。
    去重到最外一层：一整行掉出去时报**那一行**并写清里面有几个人看得见的东西，
    不报行里每个子控件——一行报三条，读的人数不出到底坏了几个（这一档最初也是 MISSED，
    因为"最外那层"通常是个不带文字的容器，只在文字候选里去重是去不掉的）。
    """
    cands: list[QWidget] = list(root.findChildren(QLabel))
    cands += [w for w in root.findChildren(QAbstractButton) if w.text()]
    cands += [w for w in root.findChildren(QComboBox) if w.currentText()]
    if isinstance(root, (QLabel, QAbstractButton, QComboBox)):
        cands.append(root)
    groups: dict[QWidget, list[str]] = {}
    for w in cands:
        if not w.isVisible() or isinstance(w, QMenu):
            continue
        label = _label_of(w)
        if not label:
            continue
        own, vis, _who = x_spans(w, w.window())
        if vis[0] <= vis[1]:
            continue
        # 往上找最外那一层"整块也在外面"的容器（容器自己不报文字，所以要去重必须问容器）
        key, a = w, w.parentWidget()
        while a is not None and a is not root and a is not w.window() and _out_of_view(a):
            key, a = a, a.parentWidget()
        groups.setdefault(key, []).append(f"{type(w).__name__}「{label[:12]}」")
    bad: list[str] = []
    for key, labels in groups.items():
        own, _vis, who = x_spans(key, key.window())
        # 分支只看"这一层自己带不带字"：带字就是它本人掉出去（报它的文字，报错才认得出是谁）；
        # 不带字说明它是容器，报容器 + 里面有哪几个带字的（`key in labels` 那种写法拿对象比字符串
        # 列表，恒 False，一整行会全走容器那支、把「尾巴」这类单个控件也报成无名容器——
        # 就是这条尺最初的 MISSED 之一）。
        if _label_of(key):
            bad.append(f"{type(key).__name__}「{_label_of(key)[:14]}」整块在裁剪框外"
                       f"（自己 x={own[0]}..{own[1]}，横向露出 0px，裁它的是 {who}）")
        else:
            bad.append(f"{type(key).__name__}「{key.objectName() or ''}」整块在裁剪框外"
                       f"（x={own[0]}..{own[1]}，裁它的是 {who}；里面有 {len(labels)} 个带文字的控件："
                       f"{'、'.join(labels[:3])}）")
    return bad


def colliding_text(root: QWidget) -> list[str]:
    """重叠尺：两块会画文字的叶子控件，几何上真的压在一起。

    只比"没有子控件的文字控件"，否则父面板（本来就框住子控件）会和每个子控件都算重叠。
    坐标一律取 clipped_rect()：被滚动区裁掉的面积不算"画在屏幕上"。    已知不管的：刻意分层摆的浮层（OSD/预览在另一些顶层窗口里，不在这棵树内）。
    """
    leaves: list[tuple[str, object]] = []
    for w in list(root.findChildren(QLabel)) + list(root.findChildren(QAbstractButton)):
        # 每一项都得**当场决定去留**。上一版这里写成"不满足条件时进内层再看文字"，
        # 结果 isVisible() 为假但有文字的标签一条都没被跳过——侧栏里没显示的那几页
        # 全被当成"重叠"报了（--falsify 里现在有一条专门盯这个）。
        if isinstance(w, QMenu) or not w.text() or w.children():
            continue
        if not w.isVisible():
            continue
        r = clipped_rect(w, root)
        if r.isValid() and r.width() > 1 and r.height() > 1:
            leaves.append((f"{type(w).__name__}「{w.text()[:12]}」", r))
    out: list[str] = []
    for i in range(len(leaves)):
        for j in range(i + 1, len(leaves)):
            a, b = leaves[i][1], leaves[j][1]
            m = a.intersected(b)
            if m.width() > 1 and m.height() > 1:
                out.append(f"{leaves[i][0]} 与 {leaves[j][0]} 压在一起 "
                           f"{m.width()}×{m.height()}px @({m.x()},{m.y()})")
    return out


def QRectOf(w: QWidget, top: QWidget):
    """把子控件矩形换算到 top 自己的坐标系（QRect 从这里也顺手拿到）。"""
    from PyQt5.QtCore import QRect
    p = w.mapTo(top, QPoint(0, 0))
    return QRect(p.x(), p.y(), w.width(), w.height())


def contrast(fg: str, bg: str) -> float:
    """WCAG 对比度。写在这里而不是借外部库：判据要跟主题令牌一起改，多一个依赖不如多 12 行。"""
    def lin(hexs: str) -> float:
        c = QColor(hexs)
        ch = []
        for v in (c.redF(), c.greenF(), c.blueF()):
            ch.append(v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4)
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    a, b = lin(fg), lin(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# 前景/背景配对表：每一行都是 qss() 里真存在的一条规则（末尾那条闸门会反过来查表有没有漏）。
# 需要值是"这行字给用户读正文用"还是"只是图形"：正文按 4.5，图形/大字按 3.0。
def contrast_rows() -> list[tuple[str, str, str, float]]:
    A, AF, AT = theme.ACCENT, theme.ACCENT_FILL, theme.ACCENT_TEXT
    return [
        ("正文 on 窗口底", theme.TEXT, theme.BG, LUM_OK),
        ("正文 on 面板", theme.TEXT, theme.BG_RAISE, LUM_OK),
        ("次要文字 on 窗口底", theme.TEXT_DIM, theme.BG, LUM_OK),
        ("次要文字 on 面板", theme.TEXT_DIM, theme.BG_RAISE, LUM_OK),
        ("次要文字 on 悬停底（chip）", theme.TEXT_DIM, theme.BG_HOVER, LUM_OK),
        ("选中页签文字 on 选中底", AT, theme.BG_ACTIVE, LUM_OK),
        ("当前条目主色文字 on 选中底（播放列表那一行）", AT, theme.BG_ACTIVE, LUM_OK),
        ("主色文字 on 窗口底", AT, theme.BG, LUM_OK),
        ("主按钮白字 on 主色底", "#FFFFFF", AF, LUM_OK),
        ("勾选框描边与面板底的区分", AT, theme.BG_RAISE, LUM_BIG),
        ("进度条主色 on 面板", A, theme.BG_HOVER, LUM_BIG),
        # 危险色有**两份用途**，各按各的口径判（见 theme 里 DANGER/DANGER_TEXT 那段）：
        # 身份色 DANGER 只当铺底（关闭键 hover），白字压上去是图形件口径 3.0；
        # 文字档 DANGER_TEXT 才是那行 12~13px 的红字，落在面板上按正文 4.5 判。
        # 以前这里写的是"危险色文字 + 身份色 + 3.0"——三样凑在一起，
        # 于是 4.46:1 的红字一直"达标"，而它差的正是那 0.04。
        ("危险色文字 on 面板（冲突红字、滤镜被内核扔掉）", theme.DANGER_TEXT, theme.BG_RAISE, LUM_OK),
        ("白字 on 危险底（关闭键 hover 那整块）", "#FFFFFF", theme.DANGER, LUM_BIG),
        ("成功色文字 on 面板", theme.OK, theme.BG_RAISE, LUM_OK),
        # 弱化色只出现在 :disabled 与输入框占位符上（读不到不影响操作），按图形件口径；
        # 这一条要是哪天被用到正文上，就得挪到 LUM_OK 那一组去。
        ("弱化文字 on 窗口底", theme.TEXT_FAINT, theme.BG, LUM_BIG),
    ]


def contrast_fail(rows: list[tuple[str, str, str, float]]) -> list[str]:
    return [f"{name} {contrast(fg, bg):.2f}:1（需 ≥{need:.1f}）{fg} on {bg}"
            for name, fg, bg, need in rows if contrast(fg, bg) < need - 1e-9]


def qss_foregrounds(sheet: str) -> set[str]:
    """样式表里出现过的**前景色**（color: 那侧），用来查对比表有没有漏掉一条规则。

    必须用否定前后瞻的正则：`border-color`、`selection-background-color` 里也含
    "color:"，按字符串切会把它们的主色当成前景色，闸门于是报出一堆假孤儿（自己踩过一次）。
    """
    import re
    out: set[str] = set()
    for m in re.finditer(r"(?<![-\w])color:\s*([^;{}\s]+)", sheet):
        c = QColor(m.group(1))
        if c.isValid():
            out.add(c.name().upper())
    return out


# ---------------------------------------------------------------- 表面清单
def synth_frame(w: int = 320, h: int = 180) -> QPixmap:
    """给悬停预览造一张"有内容的帧"：抽帧回来的图是 mpv/ffmpeg 的产物，
    屏幕休眠时拿不到真像素，这里用渐变＋色块合成。**只有渲染器被量**，
    所以报告里这张必须写明"输入是合成图"，不能写成"真封面/真帧"。"""
    from PyQt5.QtGui import QLinearGradient, QRadialGradient
    pm = QPixmap(w, h)
    g = QLinearGradient(0, 0, 0, h)
    g.setColorAt(0.0, QColor("#1B2A4A"))
    g.setColorAt(0.55, QColor("#C2683A"))
    g.setColorAt(1.0, QColor("#0B1017"))
    p = QPainter(pm)
    p.fillRect(pm.rect(), g)
    r = QRadialGradient(w * 0.7, h * 0.35, h * 0.35)
    r.setColorAt(0.0, QColor("#F6E7B0"))
    r.setColorAt(1.0, QColor(0, 0, 0, 0))
    p.setBrush(r)
    p.setPen(Qt.NoPen)
    p.drawEllipse(int(w * 0.7 - h * 0.3), int(h * 0.35 - h * 0.3), int(h * 0.6), int(h * 0.6))
    p.setPen(QPen(QColor("#0B1017"), 3))
    for i in range(6):
        x = int(w * (0.08 + 0.15 * i))
        p.drawLine(x, h - 8, x + 20, 8)
    p.end()
    return pm


class Sweep:
    """每一张表面的"怎么画出来"。构造时就拍走空状态那张。

    为什么是构造时拍：`window`（空状态）与 `window_loaded` 是同一个控件的两种状态，
    而载入样片之后 empty 页就藏起来了、当场再"退回空状态"要靠停内核（停完标题栏、
    时间码也一起变了，拍到的不是用户打开播放器看到的第一眼）。所以在按载入键之前先存。
    """

    def __init__(self, cfg, win: MainWindow):
        self.cfg = cfg
        self.win = win
        self.notes: dict[str, str] = {}
        TU.pump(0.2)
        self.empty_shot = render_to(win, theme.BG)
        # 占位页也必须在这之前拍：样片一进来它就 setVisible(False)，
        # 而隐藏后的控件"子控件全算不可见"，事后 render 出来是一张没有按钮的白板——
        # 那不是产品坏了，是量法错了，所以拍到的是它还在台上的那一帧。
        self.empty_page_shot = render_to(win.empty, theme.BG)

    # ---------- 各种"把这张画出来"的入口 ----------
    def s_window(self, _w):
        return self.empty_shot

    def s_window_loaded(self, _w):
        return render_to(self.win, theme.BG)

    def s_empty_page(self, _w):
        return self.empty_page_shot

    def s_bar(self, _w):
        return render_to(self.win.bar, theme.BG)

    def s_bar_states(self, _w):
        """非默认态：倍速不是 1、静音、三个侧栏按钮亮着——这些态各有各的配色，
        默认态全绿不代表它们没画错（选中色、禁用色、勾上色是三套规则）。"""
        b = self.win.bar
        prev = (b.lbl_speed.text(), b.btn_loop.isChecked(), b.btn_shuffle.isChecked())
        b._on_speed(1.75)
        b._paint_loop("one")
        b.btn_shuffle.setChecked(True)
        pm = render_to(b, theme.BG)
        b._on_speed(1.0)
        b._paint_loop("no")
        b.btn_shuffle.setChecked(prev[2])
        self.notes["bar-states"] = f"改前={prev} 量的是：1.75x＋单曲循环＋乱序 同时亮"
        return pm

    def s_title(self, _w):
        return render_to(self.win.title, theme.BG)

    def s_osd(self, _w):
        self.win.osd.show_text("倍速 1.75x", 8000)
        TU.pump(0.1)
        return render_to(self.win.osd, VIDEO_BLACK)

    def s_preview(self, _w):
        pm = synth_frame()
        tl = self.win.bar.scrub.mapToGlobal(QPoint(0, 0))
        self.win.preview.show_at(pm, "00:04 / 00:10", tl.x() + 320, tl.y() - 8)
        TU.pump(0.1)
        self.notes["preview"] = "输入是合成帧（见 synth_frame 的说明），量的是浮层渲染器"
        return render_to(self.win.preview, VIDEO_BLACK)

    def _sidebar(self, page: int):
        self.win._set_sidebar(True, page)
        TU.pump(0.25)
        return render_to(self.win.sidebar, theme.BG)

    def s_sidebar_playlist(self, _w):
        return self._sidebar(0)

    def s_sidebar_library(self, _w):
        img = self._sidebar(1)
        n = len(self.win.library.items())
        self.notes["sidebar-library"] = f"库里 {n} 条（扫描根＝tests/samples）"
        return img

    def s_sidebar_tweak(self, _w):
        return self._sidebar(2)

    def s_sidebar_transform(self, _w):
        img = self._sidebar(3)
        self.notes["sidebar-transform"] = (
            "画面页：几何 6 项＋增强 6 项＋链状态行。抓的是默认状态，所以链应是（空）；"
            f"实际状态行={self.win.transform_panel.lbl_chain.text()!r}")
        return img

    def s_sidebar_audio_eq(self, _w):
        img = self._sidebar(4)
        p = self.win.audio_eq_panel
        self.notes["sidebar-audio-eq"] = (
            "声音均衡页：预设下拉＋十段＋前置＋归零＋链状态行。抓的是默认状态（全 0＝不挂滤镜）；"
            f"滑轨 {len(p._rows)} 根、状态行={p.lbl_chain.text()!r}")
        return img

    def s_sidebar_stats(self, _w):
        img = self._sidebar(5)
        self.notes["sidebar-stats"] = "统计页在载入样片后问到的实时读数"
        return img

    def s_settings(self, _w):
        self.win._open_settings()
        TU.pump(0.2)
        return render_to(self.win.settings_dlg, theme.BG)

    def s_keymap(self, _w):
        self.win._open_keymap()
        TU.pump(0.2)
        return render_to(self.win.keymap_dlg, theme.BG)

    def s_gif(self, _w):
        self.win._open_gif()
        TU.pump(0.25)
        d = self.win.gif_dlg
        # 这一面的**状态指纹**（2026-10-13 r15 学到的，别再让下一个人重做我这次的像素定位）：
        # 工坊那句「还剩 X 秒可导」按「片长 − 起点」现算，而抓档链跑到这一面时样片播到第几秒
        # 是随机的。r14→r15 同形状（采样 722400 点、底色 75%）墨迹从 6.62% 涨到 6.96%，
        # 逐像素一比，5304 个改动点全落在提示行那一带（r15 那句是
        # 「起点离片尾只剩 0.0 秒，导不出这一段」）。没有这一行指纹，跨轮比数的人只能
        # 像这次一样自己把 PNG 切开才知道"漂"的是哪一行、是不是产品改的。
        self.notes["gif"] = (f"起点={float(d.spin_start.value()):.2f} "
                             f"提示行={d.lbl_range.text()!r}（这句随播放进度变，跨轮比先比它）")
        return render_to(d, theme.BG)

    def s_sleep(self, _w):
        from app.ui.sleep_timer import SleepDialog
        d = SleepDialog(self.win.sleep, self.win)
        d.resize(420, 300)
        d.show()
        TU.pump(0.15)
        img = render_to(d, theme.BG)
        d.hide()
        d.deleteLater()
        return img

    def s_help(self, _w):
        return grab_exec(lambda: self.win._show_help())

    def s_menu_speed(self, _w):
        return render_to(menus.speed_menu(self.win.bar, 1.0, SPEEDS), theme.BG_RAISE)

    def _track(self, kind: str):
        m = menus.track_menu(self.win.player, kind, self.win.bar)
        m.adjustSize()
        return render_to(m, theme.BG_RAISE)

    def s_menu_audio(self, _w):
        return self._track("audio")

    def s_menu_video(self, _w):
        return self._track("video")

    def s_menu_sub(self, _w):
        return self._track("sub")

    def s_menu_device(self, _w):
        m = menus.device_menu(self.win.player, self.win.bar)
        m.adjustSize()
        return render_to(m, theme.BG_RAISE)

    def s_menu_loop(self, _w):
        m = menus.loop_menu(self.win.player, self.win.bar)
        m.adjustSize()
        return render_to(m, theme.BG_RAISE)

    def s_menu_more(self, _w):
        from app.ui.main_window import CONTEXT_LABELS, MORE_ACTIONS
        extra = [(CONTEXT_LABELS[a], ("do", a)) for a in MORE_ACTIONS]
        m = menus.more_menu(self.cfg, self.win.bar, extra=extra)
        m.adjustSize()
        return render_to(m, theme.BG_RAISE)

    def s_menu_context(self, _w):
        return grab_exec(lambda: self.win._popup_context(QPoint(200, 200)),
                         holder="menu")

    def s_fullscreen(self, _w):
        self.win.toggle_fullscreen(True)
        TU.pump(0.3)
        img = render_to(self.win, theme.BG)
        self.win.toggle_fullscreen(False)
        TU.pump(0.3)
        self.notes["fullscreen"] = "全屏态：标题条与控制条都在（自动收起要等鼠标不动，这里不等）"
        return img

    def s_accents(self, _w):
        """六种主题色各画一张控制条＋侧栏，并逐色问两句：
        ① 这张画得出来吗（墨迹尺，下限只卡"别是空白"）；
        ② 换成这一色之后，主色真的改动到像素了吗（accent_switch_px，拿默认蓝当基准）。
        换色最容易只在三条用途里的一条上翻车（紫底白字看不见、选中页签文字没跟上就是这类）。
        """
        shots: list[tuple[str, str, QImage, tuple[str, str, str]]] = []
        old = theme.ACCENT
        # 画第一张之前先让侧栏**真的排过一次版**。以前这一节是从"侧栏还藏着"的状态直接 render 的，
        # 于是基准那张（默认蓝）量到的是侧栏没布局时的 3028×152 短图，而装完第一支主题色、
        # 走过一轮事件循环之后侧栏才长出 1256px 的真身——后面五张全跟一张短图比，
        # `--only accent`（只跑这一节，前面没有别的表面把它显形）就红成五条「两张尺寸不一样」。
        # 判据不该依赖"前面那几节跑没跑过"，所以这里自己显形一次；脏图在
        # tests/out/sweep/font-1.5、font-3 里留过档，重跑这两档会被覆盖成对的。
        self.win._set_sidebar(True, 0)
        TU.pump(0.2)
        for hexv, name in theme.ACCENTS:
            theme.install(QApplication.instance(), hexv)
            TU.pump(0.15)
            one = render_to(self.win.bar, theme.BG)
            two = render_to(self.win.sidebar, theme.BG)
            canvas = QImage(one.width() + two.width() + 12, max(one.height(), two.height()),
                            QImage.Format_ARGB32)
            canvas.fill(QColor(theme.BG))
            p = QPainter(canvas)
            p.drawImage(0, 0, one)
            p.drawImage(int(one.width() + 12), 0, two)
            p.end()
            path = os.path.join(OUT, f"50-accent-{name}-{hexv.lstrip('#')}.png")
            canvas.save(path)
            shots.append((name, hexv, canvas,
                          (theme.ACCENT, theme.ACCENT_FILL, theme.ACCENT_TEXT)))
            rows = contrast_rows()
            bad = contrast_fail(rows)
            check(f"主题色「{name}」的对比表全过", not bad, "；".join(bad[:4]))
        base_name, _bh, base_img, _bt = shots[0]
        inks: list[str] = []
        for name, hexv, canvas, toks in shots:
            frac, d = panel_ink(canvas)
            inks.append(f"{name}:{frac * 100:.1f}%")
            # 下限 0.4%：这张图本来就 98% 是平底（见 accent_switch_px 的说明），
            # 这条只否"整块空白/样式全丢"，"主色有没有真的换过来"交给下面那条量
            check(f"主题色「{name}」的控制条＋侧栏画得出来（墨迹尺）",
                  frac >= 0.004, f"{d} 主色={hexv}")
            if name == base_name:
                continue
            n, detail = accent_switch_px(base_img, canvas, toks)
            if n < 0:
                measure_unavailable(f"主题色「{name}」相对「{base_name}」真的改动到主色像素", detail)
                continue
            check(f"主题色「{name}」相对「{base_name}」真的改动到主色像素（面积 ≥ {ACCENT_AREA_MIN}px²）",
                  n >= ACCENT_AREA_MIN, f"{detail} 三用途={'/'.join(toks)}")
        # 基准自己不能既是"改动点"又是"没改动"——同色相减必须是 0，
        # 否则上面那条的下限是噪声给的（--falsify 里有一条专门盯这个）
        n_same, d_same = accent_switch_px(base_img, base_img, shots[0][3])
        if n_same < 0:
            measure_unavailable("自己和自己相减必须一个改动点都没有（证明这条量的是变化，不是噪声）",
                                d_same)
        else:
            check("自己和自己相减必须一个改动点都没有（证明这条量的是变化，不是噪声）",
                  n_same == 0, f"{d_same}")
        theme.install(QApplication.instance(), old)
        TU.pump(0.15)
        self.notes["accents"] = " ".join(inks)
        return QImage()      # 已经各自存过文件了


def grab_exec(fn, holder: str = "dlg"):
    """把 exec() 换成"显示→grab→收起"，让模态页走的还是**产品自己的构造路径**。

    界面本身是真的：同一个类、同一套装配、同一张 QSS；换掉的只有"卡在模态循环里等人点"
    这一段。抓完返回值给 None，产品那边 `if chosen:` 的分支就不会执行任何动作。
    """
    grabbed: list[QImage] = []

    def d_exec(self, *a):
        self.show()
        TU.pump(0.1)
        grabbed.append(render_to(self, theme.BG))
        self.hide()
        return 0

    def m_exec(self, *a):
        self.adjustSize()
        grabbed.append(render_to(self, theme.BG_RAISE))
        return None

    patchers = {"dlg": (QDialog, "exec", d_exec), "menu": (QMenu, "exec", m_exec)}
    cls, attr, repl = patchers[holder]
    orig = getattr(cls, attr)
    setattr(cls, attr, repl)
    try:
        fn()
    finally:
        setattr(cls, attr, orig)
    return grabbed[0] if grabbed else QImage()


# name -> (floor, fn)；floor 是墨迹尺的下限（第一次跑会把实测值打在报告里，之后当回归闸门）
SURFACES: list[tuple[str, float, str, bool]] = [
    # 整窗两张的下限比面板低：窗口里视频区/空状态页天生就是一大块纯色底，
    # 墨全集中在控制条与标题条那两排——实测 1.85%，闸门压到 1.2% 才既不误杀
    # "整窗画崩成一张底"也放过这种"本来就空"的设计。
    ("window", 0.030, "整窗外壳（空状态）", False),
    ("window-loaded", 0.030, "整窗外壳（载入样片后）", False),
    ("bar", 0.030, "控制条", False),
    ("bar-states", 0.030, "控制条非默认态", False),
    # 标题条实测 1.30%：它只有"应用名＋文档名＋三个按钮图标"，天生就这么空。
    # 下限从首测的 2% 压到 0.8%，是为了让"整条画崩成一块底"还能红、而"设计得干净"不再红——
    # 这条闸门的用途是回归报警，不是审美评分（第一版把下限凭手感写成 2%，就是拿尺子判审美）。
    # tree=True：片名标签是这张表面唯一会随内容变长的东西，截字/压扁/重叠这几把尺得看着它。
    ("title", 0.008, "标题条", True),
    ("osd", 0.100, "OSD 气泡", False),
    ("preview", 0.150, "时间轴悬停预览", False),
    # 占位页 1.00%：一整块舞台中央只有图标＋三行字＋两个按钮，剩下全是底。
    # 和标题条同一类"天生就空"，下限按实测留一半余量。
    ("empty-page", 0.006, "空状态占位页（打开播放器的第一眼）", True),
    ("sidebar-playlist", 0.020, "侧栏·播放列表", True),
    ("sidebar-library", 0.020, "侧栏·媒体库", True),
    ("sidebar-tweak", 0.020, "侧栏·调节", True),
    ("sidebar-transform", 0.020, "侧栏·画面", True),
    ("sidebar-audio-eq", 0.020, "侧栏·声音均衡", True),
    ("sidebar-stats", 0.020, "侧栏·统计", True),
    ("settings", 0.020, "设置页", True),
    ("keymap", 0.020, "快捷键编辑页", True),
    ("gif", 0.020, "截图工坊", True),
    ("sleep", 0.020, "睡眠定时", True),
    ("help", 0.020, "快捷键一览", True),
    ("menu-speed", 0.020, "菜单·倍速", False),
    ("menu-audio", 0.020, "菜单·音轨", False),
    ("menu-video", 0.020, "菜单·视频轨", False),
    ("menu-sub", 0.020, "菜单·字幕轨", False),
    ("menu-device", 0.020, "菜单·输出设备", False),
    ("menu-loop", 0.020, "菜单·循环", False),
    ("menu-more", 0.020, "菜单·更多", False),
    ("menu-context", 0.020, "右键菜单", False),
    ("fullscreen", 0.030, "全屏态整窗外壳", False),
    ("accents", 1.000, "六种主题色对照（各自另存 PNG）", False),
]


def widget_of(win: MainWindow, name: str) -> QWidget | None:
    """走树的五把尺（截字/裁而未告/压扁/重叠/掉出视口）要有个"根"：整窗用窗口，浮层与菜单用它们自己。"""
    table = {
        "window": win, "window-loaded": win, "fullscreen": win,
        "bar": win.bar, "bar-states": win.bar, "title": win.title,
        "osd": win.osd, "preview": win.preview, "empty-page": win.empty,
        "sidebar-playlist": win.sidebar, "sidebar-library": win.sidebar,
        "sidebar-tweak": win.sidebar, "sidebar-transform": win.sidebar,
        "sidebar-audio-eq": win.sidebar,
        "sidebar-stats": win.sidebar,
        "settings": win.settings_dlg, "keymap": win.keymap_dlg, "gif": win.gif_dlg,
    }
    return table.get(name)


_QSS_UNSCALED = theme.qss        # 模块导入那一刻抓住的"从没被任何档换过"的那一份


def _rescale_qss(text: str, scale: float) -> str:
    """纯换算：把 QSS 文本里的 `font-size: Npx` 全按档位放大/缩小（纯函数，好单独证伪）。"""
    return re.sub(r"font-size:\s*(\d+)px",
                  lambda m: f"font-size: {max(6, round(int(m.group(1)) * scale))}px",
                  text)


def _scale_qss(scale: float) -> None:
    """把 QSS 里所有 `font-size: Npx` 按档位放大/缩小——"用户能调界面字号"这件事的替身。

    为什么不写 `app.setFont(...)`：theme.qss() 里 `*` 与 `QWidget` 两条规则显式写了 font-size，
    Qt 的样式表优先级压过 setFont，setFont 打进去会被静默盖掉——那样量出来的"字号没变"
    是量法没生效，不是产品扛住了。
    为什么补在 `theme.qss` 这个**函数**上、不是补在第一份返回值上：产品里有三条路各自调它
    （`install()`、`set_accent()`、设置页换主色时窗口级那张样式表），只改一份返回值等于
    只放大半张界面，剩下的那半会替产品撒谎说"没裁字"。

    为什么起算点必须是 `_QSS_UNSCALED` 而不是"当时的 `theme.qss`"（2026-10-10 自己坑出来的）：
    上一版写成 `orig = theme.qss`，于是**第二次调用是把上一档的产物再乘一遍**——同一个进程里
    连着跑 1.8→2.5→3.0，第三档量到的字号其实是 1.8×2.5×3.0＝13.5 倍，屏幕上那个"内容宽 1199、
    50 个控件越过右沿"是这个乘积给的，不是 3.0 档给的。同一形状还骗出过 probe_page_need 的 973。
    三份探针（viewport_clip / page_need / min_width）都在循环里换档，读回来的每一个数都因此偏高，
    而报告只是"数字很大"，看不出量法在乘自己。⇒ 换档永远从原始那份起算，
    判据在 --falsify：连调两档之后读回来的必须等于只调后一档，而不是两档之积。
    """
    def scaled() -> str:
        return _rescale_qss(_QSS_UNSCALED(), scale)
    theme.qss = scaled


def main(argv: list[str]) -> int:
    global OUT
    # 跑法闸门：这一份量具每一条"墨迹占比下限"都是按**产品装配**标的（Windows 平台 + 150% 缩放，dpr=1.5）。
    # 图省事套上 QT_QPA_PLATFORM=offscreen 批量跑，同一张标题条会从 2.39% 掉到 0.35%（离屏平台的 dpr
    # 与抗锯齿都不同），七张表面集体贴地板——红的是跑法，不是产品。所以这里直接拒跑并点名原因
    # （退 2＝量具的前提立不住），别让下一位照着七条红去改本来没坏的渲染代码。
    if os.environ.get("QT_QPA_PLATFORM", "").strip().lower() == "offscreen":
        print("[ENV] 这份量具不能在 offscreen 平台上跑：墨迹下限是按产品装配（Windows 平台 + 150% 缩放）"
              "标的，离屏那套 dpr=1.0 会把同一片文字的墨迹占比读到约 1/7（实测标题条 2.39% → 0.35%）。"
              "摘掉 QT_QPA_PLATFORM 再跑——它量的是控件像素，不需要屏幕亮着。", flush=True)
        return 2
    only = ""
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]
    do_screen = "--screen" in argv
    scale = 1.0
    if "--font-scale" in argv:
        scale = float(argv[argv.index("--font-scale") + 1])
    if scale != 1.0:
        # 换档必须换目录：这一轮的 PNG 是"那一档字号的证据"，写回原名会把标准档的
        # 留档覆盖掉（仓里没有 VCS，覆盖就是丢证据）。
        OUT = os.path.join(TU.OUT, "sweep", f"font-{scale:g}")
        _scale_qss(scale)
    os.makedirs(OUT, exist_ok=True)
    print(f"---- 本轮字号档 = {scale:g}（1 为产品默认；其余是给界面字号压的档，不是产品设置）"
          f"　存图目录：{OUT} ----", flush=True)

    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    TU.install_crash_log()
    cfg = TU.make_cfg()
    # 媒体库要有东西可画：扫描根指向 tests/samples（跑完夹回原值——仓里没有 VCS，
    # 测试状态留在用户那份 config.json 里就是污染）。
    old_roots = list(cfg.get("library_roots") or [])
    cfg["library_roots"] = [TU.SAMPLES]
    cfg.save()
    theme.install(app, str(cfg["theme_accent"]))
    try:
        return run(app, cfg, only, do_screen)
    finally:
        # 关窗要发生在 Python 拆对象之前：留着活窗口到解释器退出，Qt 析构顺序随机，
        # 实测会刷一屏"[ipc] 回调异常: wrapped C/C++ object of type MpvPlayer has been deleted"
        # ——那是外壳的收尾，不是内核在报错，但它会把真正的失败信息淹掉。
        for w in [x for x in QApplication.topLevelWidgets() if isinstance(x, MainWindow)]:
            w.close()
        TU.pump(0.6)
        cfg["library_roots"] = old_roots
        cfg.save()


def grab_precondition(win: QWidget, w: QWidget) -> str:
    """抓屏判据的前提：主窗和目标都必须是可见态。返回"不许量"的原因，空串＝可以量。

    为什么单独拎出来：这条判据测的是"屏幕上的像素"，而像素只在窗口开着的时候存在。
    上一版把 `win.close()` 写在抓屏之前，遮挡闸门只能报"这块区域没露在最上层"，
    于是 rc=1 把量具自己的顺序错算成了产品坏（logs/capture_sweep-screen-10100630.log:50-51）。
    前提由量具自己守住，塌了就点名叫"不作数"（走 rc=2），绝不冒充产品判负。
    """
    if not win.isVisible():
        return "主窗在抓屏前已是不可见态（谁把 close()/hide() 挪到了判据前面）"
    if not w.isVisible():
        return f"目标 {type(w).__name__} 在抓屏前已是不可见态（同上）"
    return ""


def visible_others(app: QApplication, win: QWidget) -> list[QWidget]:
    """场上除主窗外还"可见"的顶层窗（按类型点名，不做任何判断）。"""
    return [w for w in app.topLevelWidgets() if w is not win and w.isVisible()]


def dismiss_dialogs(app: QApplication, win: QWidget) -> tuple[list[str], list[str]]:
    """把体检叫开、却没收拾的自家**对话框**关掉；返回（关掉的类名，没动但仍在场的类名）。

    为什么抓屏前必须收拾（本机 2026-10-11 实测）：`_open_keymap` / `_open_gif` 走的是
    产品自己的常驻实例——只 `show()`，不负责关。体检跑到那两张就把它们留在主窗上面，
    抓屏判据于是量到两个自家句柄（logs/capture-sweep-screen-005220.log:50-51 里
    `0xe120b8/(701,185,1923,1521)` 与 `0x4aa1bf2/(881,395,1743,1311)`，实测对应
    KeymapDialog 768×686 与 GifDialog 665×476，见 logs/probe-leftover-windows-*），
    判出来的"红"是 Z 序被自己的弹窗压住，而不是抓屏通路坏了。

    只关 QDialog：其余顶层窗（Osd 是产品自己带定时收起的分层浮窗）一律不动，
    只把类名报出去——万一场上出现一张本不该开着的窗，日志里看得见，
    而不是被量具悄悄清场清掉。
    """
    closed, left = [], []
    for w in visible_others(app, win):
        if isinstance(w, QDialog):
            w.hide()
            closed.append(type(w).__name__)
        else:
            left.append(type(w).__name__)
    if closed:
        TU.pump(0.25)          # 收起要过一轮事件循环，否则遮挡表里还留着它
    return closed, left


def run(app, cfg, only: str, do_screen: bool) -> int:
    win = TU.build_window(cfg)
    sw = Sweep(cfg, win)        # 先拍空状态，再递播放请求（见 Sweep 的说明）
    win.add_paths(TU.FILES)
    loaded = TU.wait_for(
        lambda: float(win.player.prop("duration", 0) or 0) > 0
        and not win.video.isHidden(), 12.0)
    check("样片真载进来了（时长已从内核报上来；离屏 grab 量的是外壳，画面像素另说）",
          loaded, f"duration={win.player.prop('duration')} running={win.player.running}")

    seen: dict[str, float] = {}
    fails: list[str] = []
    for name, floor, label, tree in SURFACES:
        if only and only not in name:
            continue
        fn = getattr(sw, "s_" + name.replace("-", "_"))
        img = QImage()
        err = ""
        # 渲染之前先让事件循环走一轮：setText 之后 Qt 要过一轮才重算布局，
        # 不 pump 就 render 会拍到"旧几何 + 新文字"——标题条就这么被量出过一个孤零零的
        # "g"（片名已经写进标签，标签宽度还是空文字那会的 8px），看着像产品坏了，其实是量法错了。
        TU.pump(0.06)
        before = len(results)
        try:
            img = fn(None)
            if name in ("window", "window-loaded", "fullscreen"):
                dpr = win.devicePixelRatioF() or 1.0
                vr = win.video.mapTo(win, QPoint(0, 0))
                rect = (int(vr.x() * dpr), int(vr.y() * dpr),
                        int(win.video.width() * dpr), int(win.video.height() * dpr))
                img = chrome_strips(img, rect)
                sw.notes[name] = (sw.notes.get(name, "") +
                                  f"｜已裁掉视频区 {rect[2]}×{rect[3]}（那块归抓屏判据）").strip("｜")
        except BaseException as exc:
            err = f"{type(exc).__name__}: {exc}"
            import traceback
            traceback.print_exc(limit=6)
        if name == "accents":
            # 这一节的判决是在 s_accents **内部**各自 check 的（六色各两条），所以上面那张
            # 表面级的 `ok` 对它不适用。但"整节崩了"以前是悄悄跳过的：err 记在局部变量里、
            # 又因为 continue 谁也不看，于是六条判据一条没落也照样往下走——那是 HOLE，
            # 不是"这条不适用"。现在要求它至少留下过一条判决，否则当场点名。
            if err or len(results) == before:
                check("主题色这一节跑出了判决（整节异常或一条都没记＝量具塌了，不许当没发生）",
                      False, err or "s_accents 一条 check 都没落，且没抛异常")
            continue
        path = os.path.join(OUT, f"{name}.png")
        saved = bool(img and not img.isNull()) and img.save(path)
        frac, d = panel_ink(img)
        seen[name] = frac
        ok = saved and not err and frac >= floor
        extra = []
        if tree and isinstance(img, QImage):
            root = widget_of(win, name)
            if root is not None:
                cl = clipped_labels(root)
                ch = clipped_without_hint(root)
                co = colliding_text(root)
                sq = squeezed_widgets(root)
                bv = beyond_viewport(root)
                if cl:
                    extra.append("截字：" + "；".join(cl[:3]))
                if ch:
                    extra.append("裁而未告：" + "；".join(ch[:3]))
                if co:
                    extra.append("重叠：" + "；".join(co[:3]))
                if sq:
                    extra.append("压扁：" + "；".join(sq[:3]))
                if bv:
                    extra.append(f"掉出视口（{len(bv)} 个）：" + "；".join(bv[:3]))
                ok = ok and not cl and not ch and not co and not sq and not bv
        note = sw.notes.get(name, "")
        check(f"{label}：画得出来且过五把尺（下限 {floor * 100:.1f}%）", ok,
              (f"{d} 存图={saved} " + ("；".join(extra) + " " if extra else "")
               + (f"｜{note}" if note else "") + (f"｜异常 {err}" if err else "")).strip())
        if not ok:
            fails.append(name)

    # 对比表（当前主色）
    bad = contrast_fail(contrast_rows())
    check("主题令牌的对比表全过（正文 ≥4.5:1，图形/大字 ≥3:1）", not bad,
          "；".join(bad[:6]) or f"accent={theme.ACCENT} fill={theme.ACCENT_FILL} text={theme.ACCENT_TEXT}")
    # 表有没有漏掉样式表里的前景色（漏了就等于新加了一条规则没人检查）
    toks = {QColor(x).name().upper() for _, f, b, _ in contrast_rows() for x in (f, b)} \
        | {QColor("#FFFFFF").name().upper()}
    orphan = sorted(qss_foregrounds(theme.qss()) - toks)
    check("样式表里每个前景色都在对比表里有名有姓（新增规则不许逃过检查）",
          not orphan, f"没登记的：{orphan[:6]}")

    # 抓屏那两条**必须在窗口还开着的时候跑**（顺序陷阱，本机 2026-10-11 实测踩过）：
    # 旧写法先 `win.close()` 再抓屏，自家窗口已经是"隐藏"态，遮挡闸门只能报"这块区域
    # 没露在最上层"——于是 rc=1 说产品红，其实红的是量具自己把判据的前提拆了
    # （logs/capture_sweep-screen-10100630.log:50-51，两条红都写着"我们要的是 …/隐藏/…"）。
    # 判据的前提（窗口可见且在最上层）由量具自己维持，才轮得到"屏不给像素/外来窗口压着"
    # 那两种环境情形去走 rc=3 的分支。
    if do_screen:
        ok_env, d_env = TU.capture_health()
        if not ok_env:
            # 屏幕不出像素只让**抓屏那两条**不作数，前面已经量出来的产品红不能被它盖掉：
            # 以前这里是无条件 return 3，于是"界面坏了＋屏黑了"同时发生时退出码说"环境问题"，
            # 看门狗会把它当成"等屏亮了再跑一次就好"，而真正该修的那条红被吞了。
            reds_now = [nm for nm, okk, _ in results if not okk]
            if reds_now:
                print(f"---- 屏幕不出像素（抓屏两条不作数：{d_env}），"
                      f"但前面已有 {len(reds_now)} 条产品判据红 ----")
                for nm in reds_now[:8]:
                    print(f"  红：{nm}")
                return 1
            print(f"---- 屏幕此刻不出像素，抓屏那几条不作数（rc=3）：{d_env} ----")
            return 3
        closed, left = dismiss_dialogs(app, win)
        if closed:
            print(f"---- 抓屏前收拾体检自己叫开的弹窗：{closed}"
                  f"（没动但仍在场：{left}）----", flush=True)
        for label, w in (("整窗抓屏", win), ("控制条抓屏", win.bar)):
            why = grab_precondition(win, w)
            if why:
                # 量具自己的前提塌了（顺序被谁改回去了），不是产品的坏：
                # 记进 `unmeasured` 走 rc=2 并点名原因，绝不写成 rc=1 的产品红。
                unmeasured.append(f"{label}：{why}（量具前提，不是产品判负）")
                continue
            img, occl = TU.capture(win, w, label)
            frac, d = panel_ink(img)
            note = (f"｜抓屏前已收掉 {closed}" if closed else "") + \
                   (f"｜在场未动 {left}" if left else "")
            check(f"{label}：抓屏通路也画得出来", (not occl) and frac >= 0.02,
                  f"{d} {occl}{note}")

    win.close()
    TU.pump(0.5)

    print("\n---- 实测墨迹占比（留作下次回归的下限参考）----")
    for k, v in sorted(seen.items(), key=lambda kv: kv[1]):
        print(f"  {v * 100:6.2f}%  {k}")
    print(f"---- 存图目录：{OUT} ----")
    if fails:
        print(f"---- {len(fails)} 张表面没过尺：{fails} ----")
    # 退出码以前只看 `fails`（SURFACES 那张表里的表面级判决），而**非表面**的那几条
    # ——样片载入、当前主色对比表、样式表漏登记、六色换色——只进 `results` 不进 `fails`，
    # 所以红了也照样 rc=0：脚本化的闸门（watchdog、doctor）会把"有红"读成"全绿"。
    # 现在按全部判决定退出码，产品红优先于量具不作数。
    reds = [nm for nm, okk, _ in results if not okk]
    if reds:
        print(f"---- {len(reds)} 条判据红（含表面级与非表面级，全量口径）----")
        for nm in reds[:10]:
            print(f"  红：{nm}")
        return 1
    if unmeasured:
        print(f"---- 量具此刻不作数的 {len(unmeasured)} 条（缺 numpy／两张对不上，不是产品判负）----")
        for u in unmeasured:
            print(f"  NA：{u}")
        return 2
    return 0


# ---------------------------------------------------------------- 证伪
def falsify() -> int:
    """五把尺各自拿"已知答案"两头夹：坏样本必须红、好样本必须不报。

    为什么必须跑这一趟：这些尺是我自己写的，"看着能过"和"真能抓住坏"是两件事。
    上一轮踩过的那类坑（判据拿同一个函数算两遍再跟自己点头）在这里同样成立——
    所以每条负向样本都要**手工造出一份确定的坏**，而不是从真界面里挑一张"看起来怪"的。
    """
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    cfg = TU.make_cfg()
    theme.install(app, str(cfg["theme_accent"]))
    misses: list[str] = []

    def fcase(name: str, hit: bool, detail: str = "") -> None:
        # detail 是给"没抓到"看的：一条对照红了却不说出量到什么，等于只报了个死活
        print(f"[{'CAUGHT' if hit else 'MISSED'}] {name}"
              + (f"  —— {detail}" if detail else ""), flush=True)
        if not hit:
            misses.append(name)

    solid = QImage(400, 240, QImage.Format_ARGB32)
    solid.fill(QColor(theme.BG))
    frac, _ = panel_ink(solid)
    fcase("纯色空图必须判「没墨」（否则整窗画崩成一张底也照样绿）", frac < 0.005)

    clear = QImage(400, 240, QImage.Format_ARGB32)
    clear.fill(Qt.transparent)
    frac, _ = panel_ink(clear)
    fcase("全透明图必须判「没墨」（分层窗口圆角外沿的垃圾 RGB 不算墨）", frac < 0.005)

    lab = QLabel("音量输出设备选择器测试", None)
    lab.resize(200, 30)
    lab.show()
    img = lab.grab().toImage()
    frac, _ = panel_ink(img)
    fcase("真有字的小图必须判「有墨」（下限太高的话所有面板都会被误杀）", frac >= 0.02)

    hair = QImage(400, 400, QImage.Format_ARGB32)
    hair.fill(QColor(theme.BG))
    p = QPainter(hair)
    p.setPen(QPen(QColor(theme.TEXT), 1))
    p.drawLine(0, 200, 400, 200)
    p.end()
    frac, _ = panel_ink(hair)
    fcase(f"一根 1px 细线也要被看见（实测 {frac * 100:.2f}%：这就是下限为什么压到 2%）",
          frac > 0.0005)

    tight = QWidget()
    tight.resize(220, 40)
    a = QLabel("这段文字明显放不下，长到需要三百多像素", tight)
    a.setFixedWidth(60)
    a.move(4, 8)
    b = QLabel("短", tight)
    b.move(120, 8)
    tight.show()
    TU.pump(0.1)
    bad = clipped_labels(tight)
    fcase("窄框里的长句必须报截字", any("放不下" in x for x in bad))
    fcase("同一棵树里放得下的短句不许误报", not any(x.startswith("QLabel「短」") for x in bad))

    wrap = QWidget()
    wrap.resize(160, 60)
    c = QLabel("这段也很长但它开了自动换行", wrap)
    c.setWordWrap(True)
    c.setFixedWidth(60)
    c.move(4, 4)
    wrap.show()
    TU.pump(0.1)
    fcase("开自动换行的标签不该算截字（这是尺的边界，不是产品的错）",
          not any("自动换行" in x for x in clipped_labels(wrap)))

    # 裁而未告尺的已知答案对照。正样本刻意做成统计页当时的形状：标签自己的宽度==它需要的
    # 宽度（clipped_labels 量不到），切它的是**祖先视口**——这把尺存在的唯一理由。
    host = QScrollArea()
    host.setWidgetResizable(True)
    host.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    host.resize(120, 70)
    inner = QWidget()
    lay = QHBoxLayout(inner)
    lay.setContentsMargins(0, 0, 0, 0)
    clab = QLabel("这一串比视口宽，右端被切掉且没有任何线索", inner)
    lay.addWidget(clab)
    host.setWidget(inner)
    host.show()
    TU.pump(0.12)
    hits = clipped_without_hint(host)
    fcase("被祖先视口切掉、又没留线索的标签必须报（旧尺量不到的那一类）",
          any("只露出" in x for x in hits),
          f"可用宽={clab.width()} 需要宽={clab.fontMetrics().horizontalAdvance(clab.text())}px "
          f"露出={clipped_rect(clab, host.window()).width()}px 报={hits[:1]}")
    clab.setToolTip("整句在这里")
    _tip = clipped_without_hint(host)
    fcase("同一格挂上 tooltip（整句在悬停里）就不该再报", not _tip, f"报={_tip[:1]}")
    clab.setToolTip("")
    # 省略号那一档要**仍然比可见宽度长**才算数：不然它是"放得下所以不报"，
    # 这条对照就成了恒真，测不出尺子认不认省略号。
    full = "这一串比视口宽，右端被切掉且没有任何线索"
    clab.setText(full[:6] + "…" + full[-6:])
    _ell = clipped_without_hint(host)
    fcase("已经带省略号的算留了线索，不该报（裁完仍超可见宽，才测得出这条）",
          not _ell and clab.fontMetrics().horizontalAdvance(clab.text())
          > clipped_rect(clab, host.window()).width(),
          f"文本={clab.text()!r} 需要={clab.fontMetrics().horizontalAdvance(clab.text())}px "
          f"露出={clipped_rect(clab, host.window()).width()}px 报={_ell[:1]}")
    host.close()

    # 裁而未告尺的第二类盲区：**普通父容器**（不是滚动区）把子控件裁掉。
    # 形状是"字号放大之后侧栏那一排需要 245px、侧栏只有 240px"这类事——最后一格整个被
    # sidebar 裁掉（看不见也点不到），而旧写法只对 QAbstractScrollArea 的视口求交，
    # 对普通 QWidget 父容器根本不裁。正样本刻意让**格子自己够宽**（320px 的格放 260px 的字），
    # 这样"截字"那把也闭嘴，测到的才是这一把独有的盲区。
    pbox = QWidget()
    pbox.resize(120, 40)
    pinner = QWidget(pbox)
    pinner.setGeometry(0, 0, 320, 40)
    plab = QLabel("这一串超出父容器右端被裁掉且没线索", pinner)
    plab.setGeometry(0, 0, 320, 30)
    pbox.show()
    TU.pump(0.12)
    _need = plab.fontMetrics().horizontalAdvance(plab.text())
    phits = clipped_without_hint(pbox)
    fcase("被普通父容器（非滚动区）裁掉、又没留线索的标签必须报（格子自己够宽，旧写法量不到）",
          any("只露出" in x for x in phits),
          f"文字需 {_need}px 格子 {plab.width()}px 父容器 {pbox.width()}px "
          f"露出={clipped_rect(plab, pbox.window()).width()}px 报={phits[:1]}")
    # 前提当场钉住：这一格必须真的"格子够宽、只有父容器不够"——否则它落在旧尺看得见的位置，
    # 报红测的是通用能力，不是这块盲区。
    fcase("上面那条的前提成立：格子放得下字（截字尺不报）但父容器放不下",
          _need <= plab.width() and _need > pbox.width() and not clipped_labels(pbox),
          f"文字={_need}px 格子={plab.width()}px 父={pbox.width()}px 截字尺={clipped_labels(pbox)[:1]}")
    pbox.resize(340, 40)          # 父容器给够：同一份内容不该再报（否则这把是恒红）
    TU.pump(0.12)
    _pback = clipped_without_hint(pbox)
    fcase("把父容器加宽到够放之后同一句不该再报（否则裁而未告尺是恒红）",
          not _pback and plab.fontMetrics().horizontalAdvance(plab.text())
          <= clipped_rect(plab, pbox.window()).width(),
          f"露出={clipped_rect(plab, pbox.window()).width()}px 报={_pback[:1]}")
    pbox.close()

    # 字号档换算尺的已知答案（2026-10-10：这把尺曾经"乘自己"，三份探针因此报过乘积的数）。
    # 这条同时钉两件事：① 换档函数的行为（连调两档 ⇒ 读回来是最后一档）；
    # ② 判据自己有牙——把旧那条路（第二次拿第一次的产物再乘）算出来的数摆在旁边，
    #    两者必须**不相等**，否则 ① 就成了恒真（两种写法给同一个数，谁也咬不住）。
    def _fs(text: str) -> list[str]:
        return re.findall(r"font-size:\s*(\d+)px", text)[:4]

    _pristine = _QSS_UNSCALED
    _c2, _c3 = _rescale_qss(_pristine(), 2.0), _rescale_qss(_rescale_qss(_pristine(), 2.0), 3.0)
    _scale_qss(2.0)
    _scale_qss(3.0)
    _t2 = theme.qss()
    theme.qss = _pristine                      # 证伪跑完要把字号档交回去，别影响后面的判据
    _want = _rescale_qss(_pristine(), 3.0)
    fcase("连换两档只算最后那一档（旧写法第二次拿上一档产物再乘 ⇒ 探针报的字号是乘积不是档位）",
          _t2 == _want and _c3 != _want and _c3 != _c2,
          f"换档后前四处={_fs(_t2)} 单档3.0={_fs(_want)} 两档相乘={_fs(_c3)}"
          f"（后两组必须不同，否则这条判据恒真）")

    # 掉出视口尺的已知答案（2026-10-10 补，它管的是前四把结构性失明的那一类）。
    # 正样本刻意让**格子自己够宽、字也排得下**：这样截字/裁而未告/压扁三条都闭嘴，
    # 报出来的只可能是这一条——顺便把"这把尺独有"钉成机器判据，而不是注释里的一句话。
    # 内容块不用布局（用 `setGeometry` 直接摆）：布局会按视口宽重排，那样"越界"这个形状
    # 得靠碰运气摆出来，对照件就不知道自己到底在测什么。
    bh = QScrollArea()
    bh.setWidgetResizable(True)
    bh.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)      # 产品侧栏就是这一档：掉出去找不回
    bh.resize(120, 70)
    bbody = QWidget()
    bbody.setMinimumSize(320, 260)               # 内容比视口宽——正是调节页/画面页那个形状
    blab = QLabel("短", bbody)
    blab.setGeometry(4, 0, 90, 20)               # 横向在框内
    brow = QWidget(bbody)
    brow.setGeometry(200, 24, 110, 24)           # 整行横向掉出右沿（自己 110 宽，字都放得下）
    rlay = QHBoxLayout(brow)
    rlay.setContentsMargins(0, 0, 0, 0)
    rlay.addWidget(QLabel("甲", brow))
    rlay.addWidget(QLabel("乙", brow))
    boff = QLabel("尾巴", bbody)
    boff.setGeometry(210, 52, 100, 20)           # 单个控件掉出右沿（模拟行末的数值胶囊）
    bbelow = QLabel("下面", bbody)
    bbelow.setGeometry(4, 200, 90, 20)           # 横向在框内、纵向在视口下方（滚动区本职，不许报）
    bh.setWidget(bbody)
    bh.show()
    TU.pump(0.15)
    _own, _vis, _who = x_spans(boff, bh.window())
    _bhits = beyond_viewport(bh)
    fcase("整块掉在裁剪框右边的控件必须报（格子够宽、字也放得下，前四把量不到这一类）",
          any("尾巴" in x for x in _bhits),
          f"自己 x={_own[0]}..{_own[1]} 露出 x={_vis[0]}..{_vis[1]} 裁剪来自 {_who} 报={_bhits[:3]}")
    fcase("前提成立：同一个样本上截字/裁而未告/压扁三条都沉默（证明报它的是新那把）",
          not clipped_labels(bh) and not clipped_without_hint(bh) and not squeezed_widgets(bh),
          f"截字={clipped_labels(bh)[:1]} 裁而未告={clipped_without_hint(bh)[:1]} "
          f"压扁={squeezed_widgets(bh)[:1]}")
    # 去重的判据只认**主语**（"整块在裁剪框外"前面那半句）：容器那条本来就要在括号里列出
    # 行里的子控件，拿整条字符串找「甲」会把"已经去重了"报成"还在重复报"。
    _subj = [x.split("整块在裁剪框外")[0] for x in _bhits]
    fcase("一整行掉出去只点名最外那一层，行里的子控件算重复（否则一格报三条，读的人数不出坏了几个）",
          any(s.startswith("QWidget") for s in _subj)
          and not any("甲」" in s or "乙」" in s for s in _subj)
          and any("尾巴」" in s for s in _subj),
          f"{len(_bhits)} 条，主语={_subj}")
    # 竖向滚出视口是滚动区的本职，不许报（报了每一页下面那十几行都成"缺陷"，噪声会淹掉真坏）。
    _vb = [x for x in beyond_viewport(bh) if "下面" in x]
    fcase("纵向在视口下方、横向完全在框内不该报——这把尺只判横向",
          not _vb and x_spans(bbelow, bh.window())[0][1] < 120,
          f"自己 x={x_spans(bbelow, bh.window())[0]} y={bbelow.y()} 报={_vb[:1]}")
    # 容器给够 ⇒ 不报（否则这把是恒红，任何界面只要有滚动区就全灭）
    bh.resize(340, 70)
    TU.pump(0.15)
    _b2 = beyond_viewport(bh)
    _o2, _v2, _w2 = x_spans(boff, bh.window())
    fcase("把视口加宽到够放之后同一份内容不该再报（否则掉出视口尺是恒红）",
          not _b2 and _v2[0] <= _v2[1],
          f"自己 x={_o2} 露出 x={_v2}（裁剪来自 {_w2}）报={_b2[:1]}")
    bh.resize(120, 70)
    TU.pump(0.15)
    # 横向**能**滚的那一档：内容只是藏起来了，用户拖滚动条找得回 ⇒ 不算丢（另一类真坏）。
    bh.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
    TU.pump(0.2)
    _canh = beyond_viewport(bh)
    _mx = int(bh.horizontalScrollBar().maximum())
    fcase("滚动区横向能滚（能拖回来）时不该报——掉出视口只判找不回的那种",
          _mx > 0 and not _canh,
          f"横向 range 上限={_mx}（=0 说明这份摆法根本滚不动，那条对照就不成立）报={_canh[:1]}")
    bh.close()

    # 压扁尺的已知答案。正样本刻意复刻侧栏页签当时的形状：**格子比文字宽**（所以"截字"那把
    # 按"格放得下字"放过它），QSS 的左右 padding 又不在 contentsRect 里（所以可用宽被高估），
    # tooltip 还让"裁而未告"那把按设计闭嘴——三把旧尺一条都不报，而屏幕上字真的没了半截。
    # 这条对照测的就是"新尺补的到底是哪个盲区"：如果哪天它变得能被旧尺抓到，这把尺的前提就
    # 不在了，要按 §6 第 3 条重审。
    box = QWidget()
    box.setStyleSheet("QToolButton { font-size: 12px; padding: 4px 10px; }")
    box.resize(300, 60)
    row = QHBoxLayout(box)
    row.setContentsMargins(0, 0, 0, 0)
    tb = QToolButton(box)
    tb.setText("播放列表页签")
    tb.setToolTip("整句在悬停里")          # 让"裁而未告"那把按设计放过它
    # 宽度取"比文字宽、比 sizeHint 窄"：这正是当时那个页签的形状（48px 的字放 54px 的格，
    # 格子里真正给字的只有 54-2×10=34px）。旧尺看不见那 2×padding，所以必须放它过关。
    tb.setFixedWidth(tb.fontMetrics().horizontalAdvance(tb.text()) + 18)
    row.addWidget(tb)
    row.addStretch(1)
    box.show()
    TU.pump(0.12)
    _narrow = tb.width()
    sq_hit = squeezed_widgets(box)
    old_hit = clipped_labels(box) + clipped_without_hint(box)
    # 前提当场钉住：这一格必须**真的**落在旧尺的盲区里（比文字宽、又比样式要的窄），
    # 否则"旧尺不报"测的是运气，不是盲区。
    fcase("格子比样式要的窄、但比文字宽，而且挂着 tooltip：只有压扁尺要报（旧尺量不到的那一类）",
          bool(sq_hit) and not old_hit
          and tb.width() > tb.fontMetrics().horizontalAdvance(tb.text())
          and tb.width() + 1 < tb.sizeHint().width(),
          f"文字={tb.fontMetrics().horizontalAdvance(tb.text())}px 实得宽={tb.width()}px "
          f"sizeHint={tb.sizeHint().width()}px contentsRect={tb.contentsRect().width()}px "
          f"压扁尺={sq_hit[:1]} 旧尺={old_hit[:1]}")
    tb.setFixedWidth(max(tb.sizeHint().width(), tb.width()))
    TU.pump(0.12)
    _wide = squeezed_widgets(box)
    fcase("同一颗按钮把宽度还给 sizeHint 之后不该再报（否则这把尺是恒红，不是抓到了 bug）",
          not _wide and tb.width() >= tb.sizeHint().width(),
          f"实得宽={tb.width()} sizeHint={tb.sizeHint().width()} 报={_wide[:1]}")
    tb.setFixedWidth(_narrow)
    tb.setText("播放列表页签被切…")          # 带省略号＝留了线索，压扁也不该报
    TU.pump(0.12)
    _ell2 = squeezed_widgets(box)
    # 前提要当场钉住：换成这句之后 sizeHint **仍然**比实得宽，否则这条测的是运气不是省略号。
    fcase("文字里已经带省略号的页签不该被压扁尺报（切是切了，但留了线索）",
          not _ell2 and tb.width() + 1 < tb.sizeHint().width(),
          f"实得宽={tb.width()} sizeHint={tb.sizeHint().width()} 报={_ell2[:1]}")
    box.close()

    # 高度那一档的两条已知答案（探针 tests/probe_squeeze_height.py 量出来的形状）：
    # 换行标签的 sizeHint 高是"不知道最终宽度"时猜的，照抄会把每一页的说明文字全判成压扁；
    # 换成按实得宽算的 heightForWidth 之后，"一行放得下"与"两行只给一行高"才分得开。
    vbox = QWidget()
    vbox.resize(900, 200)
    vlay = QVBoxLayout(vbox)
    vlay.setContentsMargins(0, 0, 0, 0)
    # 三颗标签的高度全部**钉死**：不钉的话布局会把没设固定高的那颗撑到 170px，
    # 那测的就不是"一行放得下只给一行高"，而是布局今天心情如何（上一版栽在这儿）。
    # 但第一颗**不能钉宽度**：它的虚报正来自"宽度由布局给、sizeHint 只能按窄宽猜行数"
    # （探针实测：实得宽 944 时 sizeHint 报 180×60）；一钉宽度 sizeHint 就跟着变准，
    # 这条对照会退化成恒真。
    ok_lab = QLabel("这一句开了换行，但整句一行就放得下，sizeHint 却按窄宽猜了三行高")
    ok_lab.setWordWrap(True)
    cut_lab = QLabel("这一句开了换行，宽度只够两行，高度只给一行，第二行根本没画出来，"
                     "属于真被竖向切掉的那一类")
    cut_lab.setWordWrap(True)
    half_lab = QLabel("没开换行、高度被压成半行的标签，靠 sizeHint 那一档照样要报")
    # 高度钉成"按布局会给的 900 宽算，一行究竟要多高"。上一版钉的是 fontMetrics().height()
    # （当场量到 12），而 QLabel 的换行排版要 17 —— 差的那 5px 不是虚报，是真的少给了一行，
    # 于是这条对照测的是我的算术而不是尺子。heightForWidth 才是"这个宽度要这么多高"的真答。
    # 而量它之前必须先 ensurePolished()：全局 QSS 的字号是 polish 时才落到字体上的，
    # 没 polish 就量（这里就量到 12）等于拿另一个字号的度量钉高度，钉子自己短了一行——
    # 和 tests/probe_tab_fit.py 里"未 polish 的 fontMetrics 全是 16px"是同一个坑。
    for x in (ok_lab, cut_lab, half_lab):
        x.ensurePolished()
    _one_line = ok_lab.heightForWidth(900)
    ok_lab.setFixedHeight(_one_line)
    cut_lab.setFixedHeight(_one_line)     # 只给"宽行一行"的高
    cut_lab.setFixedWidth(220)            # 而这个宽度要三行才放得下
    half_lab.setFixedHeight(half_lab.sizeHint().height() // 2)
    for x in (ok_lab, cut_lab, half_lab):
        vlay.addWidget(x)
    vlay.addStretch(1)
    vbox.show()
    TU.pump(0.15)
    _v = squeezed_widgets(vbox)
    fcase("开了换行、整句一行放得下的说明文字不该被高度档误报（否则每一页的说明都成产品缺陷）",
          not any("这一句开了换行，但整句" in s for s in _v)
          and ok_lab.height() + 1 < ok_lab.sizeHint().height()
          and ok_lab.heightForWidth(ok_lab.width()) <= ok_lab.height(),
          f"实得高={ok_lab.height()} sizeHint 高={ok_lab.sizeHint().height()} "
          f"heightForWidth={ok_lab.heightForWidth(ok_lab.width())} 报={_v[:1]}")
    fcase("开了换行、宽度只够两行而高度只给一行必须报（真切掉的那一类不能被放过）",
          any("宽度只够两行" in s for s in _v)
          and cut_lab.heightForWidth(cut_lab.width()) > cut_lab.height() + 1,
          f"实得高={cut_lab.height()} 需要={cut_lab.heightForWidth(cut_lab.width())} 报={_v[:2]}")
    _v2 = squeezed_widgets(vbox)
    fcase("没开换行、高度只给半行也要报（换行那档退回 sizeHint 高的路没断）",
          any("没开换行" in s for s in _v2), f"实得高={half_lab.height()} 报={_v2[:3]}")
    vbox.close()
    overlap = QWidget()
    overlap.resize(240, 60)
    o1 = QLabel("左边这一块", overlap)
    o1.move(10, 10)
    o1.adjustSize()
    o2 = QLabel("压上来的一块", overlap)
    o2.move(20, 16)
    o2.adjustSize()
    overlap.show()
    TU.pump(0.1)
    fcase("两块文字真的叠在一起必须报", bool(colliding_text(overlap)))

    apart = QWidget()
    apart.resize(300, 40)
    q1 = QLabel("一", apart)
    q1.move(4, 8)
    q1.adjustSize()
    q2 = QLabel("二", apart)
    q2.move(120, 8)
    q2.adjustSize()
    apart.show()
    TU.pump(0.1)
    fcase("隔开的两块文字不许误报重叠", not colliding_text(apart))

    hidden = QWidget()
    hidden.resize(240, 60)
    hv = QLabel("看得见的一块", hidden)
    hv.move(10, 10)
    hv.adjustSize()
    hz = QLabel("藏在背后的一块", hidden)
    hz.move(12, 12)
    hz.adjustSize()
    hz.hide()                       # 和上面那块完全重叠，但它没显示
    hidden.show()
    TU.pump(0.1)
    fcase("没显示的标签不许算重叠（侧栏六页里没在显示的那五页就靠这条不被误报；颗数会变，措辞按当下）",
          not any("藏在背后" in x for x in colliding_text(hidden)))

    # 滚动裁剪：藏在视口外面的那一行，mapTo 之后正好落在视口下方的控件上。
    # 上一版就是被这一类骗到——统计页「IPC 往返」×「采样 3 秒」@(12,551) 报重叠，
    # 而那张 PNG 里那行根本没画出来（像素上没重叠，矩形上重叠）。两条对照一起摆：
    # 视口外的不许报（否则每张带滚动区的页面都白报），视口内的必须还报（否则修成了摆设）。
    scrolly = QWidget()
    scrolly.resize(260, 200)
    sc = QScrollArea(scrolly)
    sc.setGeometry(0, 0, 260, 100)        # 视口只到 ~97（QScrollArea 自带边框）
    sc.setWidgetResizable(True)
    sbar = QWidget()
    sbar.setMinimumHeight(900)
    out_row = QLabel("在视口外的一行", sbar)
    out_row.move(6, 115)            # 折到 root 坐标正好压在下面那块上（视口在 100 处收口）
    out_row.adjustSize()
    in_a = QLabel("在视口内的一行", sbar)
    in_a.move(6, 20)
    in_a.adjustSize()
    in_b = QLabel("在视口内的另一行", sbar)
    in_b.move(12, 26)               # 故意和上一行叠在一起
    in_b.adjustSize()
    sc.setWidget(sbar)
    below = QLabel("滚动区外面的按钮位", scrolly)
    below.move(6, 110)
    below.adjustSize()
    scrolly.show()
    TU.pump(0.15)
    _hits = colliding_text(scrolly)
    fcase("滚动区视口外那行不许算重叠（统计页曾被这么误报）",
          not any("视口外" in x for x in _hits), f"实际报出：{_hits[:2]}")
    fcase("视口内真的叠着的两个标签仍必须报（裁剪逻辑修过头就成了摆设）",
          any("视口内" in x for x in _hits), f"实际报出：{_hits[:2]}")

    # 换主色那条尺：改动点必须**落在主色上**才算数
    tok = (theme.ACCENT, theme.ACCENT_FILL, theme.ACCENT_TEXT)
    flat = QImage(200, 60, QImage.Format_ARGB32)
    flat.fill(QColor(theme.BG))
    painted = QImage(200, 60, QImage.Format_ARGB32)
    painted.fill(QColor(theme.ACCENT_FILL))
    n_up, d_up = accent_switch_px(flat, painted, tok)
    fcase("底图换成主色底必须报出改动点（这条量的就是「主色落到像素上没有」）",
          n_up > 0, d_up)
    n_flat, _ = accent_switch_px(flat, flat, tok)
    fcase("同一张图自己相减必须是 0（否则换色那条的下限是噪声给的）", n_flat == 0)
    offhue = QImage(200, 60, QImage.Format_ARGB32)
    offhue.fill(QColor(theme.TEXT))       # changed 了，但改的不是主色
    n_off, d_off = accent_switch_px(flat, offhue, tok)
    fcase("改动到非主色的像素不许算「换色成功」（否则这条尺恒真）", n_off == 0, d_off)

    # 逐像素口径的已知答案：贴一块**面积我说了算**的主色方块，读回来的必须正好是那个数。
    # 为什么这条是这一版才配有的：原来是隔步抽样，同样这块 40×25=1000px² 的方块，
    # 按抽样口径会读成"点数 × 步长²"，细描边那类每 step 才命中一次，折算系数本身就带 step
    # ——1.0 档与 2.5 档因此差出 4 倍，而真实面积是同一个数。这条对照把"量的是像素面积"
    # 这件事钉成可证伪的：谁哪天把它改回抽样、或者在 BGRA/行填充上算错位，读回来的数就不等于 1000。
    known = QImage(200, 60, QImage.Format_ARGB32)
    known.fill(QColor(theme.BG))
    pp = QPainter(known)
    pp.fillRect(20, 10, 40, 25, QColor(theme.ACCENT))
    pp.end()
    n_known, d_known = accent_switch_px(flat, known, tok)
    fcase("贴一块 40×25＝1000px² 的主色方块，逐像素读回来的面积必须正好 1000",
          n_known == 1000, d_known)
    # 同一块方块搬个位置、面积不变 → 读数必须不变（防"行填充/坐标映射"那类错位：
    # 错位时平移会让读数变，正确实现下它不该动）。
    known2 = QImage(200, 60, QImage.Format_ARGB32)
    known2.fill(QColor(theme.BG))
    pp = QPainter(known2)
    pp.fillRect(120, 30, 40, 25, QColor(theme.ACCENT))
    pp.end()
    n_move, d_move = accent_switch_px(flat, known2, tok)
    fcase("同一块 1000px² 的方块挪个位置，读数必须还是 1000（行填充错位会在这儿露馅）",
          n_move == 1000, d_move)

    # numpy 不在场时这把尺**不许返回 0**：0 在判据里就是"主色没落到像素上"，
    # 那是把量具缺件判成产品坏。负数才是"量不了"，调用方（s_accents）据此走 [NA]＋rc=2。
    if np is not None:
        _save_np = np
        try:
            globals()["np"] = None
            n_no, d_no = accent_switch_px(flat, painted, tok)
        finally:
            globals()["np"] = _save_np
        fcase("摘掉 numpy 后这条尺返回负数（走量具不作数），不许返回 0 冒充「主色没落地」的产品红",
              n_no < 0, f"{d_no} 读数={n_no}")

    fcase("紫底白字这类配对必须被对比表抓住",
          contrast_fail([("造一个坏配对", "#FFFFFF", "#A6ABB8", LUM_OK)]) != [])
    fcase("正文 on 窗口底这种好配对不许误报",
          contrast_fail([("正文", theme.TEXT, theme.BG, LUM_OK)]) == [])
    fcase("对比表里没有的坏前景色必须被「漏登记」闸门抓住",
          bool(qss_foregrounds("QLabel { color: #123456; }")))

    # 主题色派生令牌：三个派生值必须**真的**满足对比要求（不满足就是 set_accent 写漏了）
    for hexv, name in theme.ACCENTS:
        theme.set_accent(hexv)
        bad = contrast_fail(contrast_rows())
        fcase(f"主题色「{name}」派生的 fill/text 全过对比表（{theme.ACCENT_FILL}/{theme.ACCENT_TEXT}）",
              not bad)
    theme.set_accent(str(cfg["theme_accent"]))

    # ---- 抓屏判据的两把前置尺：前提守卫 + 收拾自家弹窗（R6-b）----
    # 为什么要进这一份对照：这两把尺决定的是"抓屏那两条算不算数"。它们一旦恒真或恒假，
    # 红就会在"产品坏"和"量具没准备好"之间乱窜——上一轮正好两个坑都踩过：
    # 先 `win.close()` 再抓屏（报成产品红），以及体检自己叫开的 KeymapDialog/GifDialog
    # 没收拾（也报成产品红，logs/capture-sweep-screen-005220.log:50-51）。
    host = QWidget(None)
    host.resize(320, 200)
    host.setWindowTitle("gelu-falsify-host")
    bar = QWidget(host)
    bar.setGeometry(0, 160, 320, 40)
    host.show()
    TU.pump(0.1)
    fcase("主窗和目标都可见时前提必须放行（守卫恒真＝每条抓屏都不作数，也是瞎）",
          grab_precondition(host, bar) == "", grab_precondition(host, bar))

    pop = QDialog(host)
    pop.resize(200, 120)
    pop.move(60, 40)
    pop.show()
    TU.pump(0.1)
    _closed, _left = dismiss_dialogs(app, host)
    fcase("体检叫开的 QDialog 必须被收掉（留着它抓屏＝遮挡闸门点自家句柄＝假产品红）",
          "QDialog" in _closed and not pop.isVisible(),
          f"收掉={_closed} 仍在场={_left} pop可见={pop.isVisible()}")
    fcase("收弹窗不许把主窗和目标一起关掉（那就退化成上一版「先关窗再抓屏」）",
          host.isVisible() and bar.isVisible(),
          f"主窗可见={host.isVisible()} 目标可见={bar.isVisible()}")

    pop.show()
    TU.pump(0.1)
    _inert = [type(w).__name__ for w in visible_others(app, host)]
    fcase("对照：不做任何收拾时那张弹窗必须还看得见（证明上一条「收掉」不是恒真）",
          "QDialog" in _inert, f"场上={_inert}")
    pop.hide()

    floater = QWidget(None)            # 顶层但**不是** QDialog：只许点名，不许清场
    floater.setWindowTitle("gelu-falsify-floater")
    floater.resize(60, 60)
    floater.move(400, 400)
    floater.show()
    TU.pump(0.1)
    _c2, _l2 = dismiss_dialogs(app, host)
    fcase("非 QDialog 的自家浮窗只许点名不许关（否则「产品真画了张不该开的窗」会被量具抹掉）",
          floater.isVisible() and "QWidget" in _l2 and "QWidget" not in _c2,
          f"关={_c2} 点名={_l2}")
    floater.close()

    host.hide()
    TU.pump(0.1)
    _why = grab_precondition(host, bar)
    fcase("主窗被关掉后前提必须点名「主窗不可见」（这条分流就是 rc=2 的入口）",
          "主窗" in _why, _why or "空串＝照样去量")
    pop.close()
    host.close()

    print(f"\n---- 证伪：{len(misses)} 条漏网 ----")
    for m in misses:
        print(f"  漏网：{m}")
    return 0 if not misses else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--falsify" in args:
        sys.exit(falsify())
    sys.exit(main(args))
