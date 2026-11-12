# -*- coding: utf-8 -*-
"""探针（R20）：全屏那一瞬到底是谁的尺寸没跟上——把"画面只在屏幕中间一块、四周留黑"的几种形状一次量开。

用户实测（2026-10-19）：双击开片能正常播了，但点全屏之后**窗口**铺满整屏，**画面**却只在
屏幕中间一块，四周留一圈黑边。剩下能造成这个形状的只有三类，盘上都看不出来，只能量：

  A 尺寸没跟上：mpv 建在我们容器里的那个子窗口还停在全屏前的尺寸。
     ⇒ 判据是原生矩形：子窗口 ≠ 容器客户区（`mpv_fills` 那把尺就是量这个的）。
     旁证已经写在代码里：`_toggle_max()` 除了当场铺一次还写了 `QTimer.singleShot(80, refit)`
     （注释原话"最大化动画落定后再铺一次 mpv 子窗口"），而 `toggle_fullscreen()` 只有当场那一次。
     两条路写法不一样本身就是线索。A 还要再分两种（修法不同）："过渡之后根本没人再铺"
     与"铺了，但那一刻量到的客户区还是旧尺寸"——所以这一趟给 `video.refit` 装了记录仪，
     把每次调用的时刻与当时量到的尺寸摊开。
  B 子窗口铺满了，内核却把画面画小：letterbox / panscan / zoom 那一族。
     ⇒ 原生矩形相等，而像素图上是"中心有内容、外圈近黑"。
  C 子窗口铺满了而画面根本没画出来（中心也黑）——那是另一件事，单独一档，别混进 A/B 的判决。

开跑之前已经用**不占前台的三条离线量**把三类候选否掉了（记在这里，免得下一位重新猜）：
  - 用户那份 `data/config.json` 现读：panscan=0.0、pan_x/pan_y=0.0、aspect_override=no、
    keep_aspect=True、crop_ratio=none，全是一档都不动的中性值 ⇒ "面板留了脏值"不解释 B；
  - 内核实测（`--vo=null --force-window=no --ao=null`，无窗无声，2026-10-19，同一个原盘）：
    `vid=1`、`width/height=3840×2160`、`video-out-params/aspect=1.777778`、`panscan=0.0`、
    `video-zoom=0.0`、`keepaspect=True`，两条 `image=True` 的封面轨 selected=False
    ⇒ "内核把 254×254 封面当视频放了"当场否掉；
  - 片子自己的黑边（ffmpeg 抽三帧逐像素量包围盒）：内容 2.400:1、上下各 13%、**左右 0 px**
    ⇒ 全屏时**本应只有上下黑边**；左右也黑就不是片子带的。

第 1 趟实测（2026-10-19 23:33，用户原盘，看门狗挑的空闲窗口，probe-fullscreen-fit-2333.log）：
  - 原生矩形**每一拍都严丝合缝**：容器 3840×2160、子窗口 3840×2160、尺寸差 0×0；
    进全屏后 refit 被调用 10 次，其中 t=+2.641s 那次量到的就是落定尺寸 ⇒ **A 当场否掉**；
  - 7 拍里只有最后一拍（+6.0s）屏上有帧，形状=「上下留黑」，内缩 左0 上224 右0 下273
    （占高 10.4%/12.7%，与 ffmpeg 量的 13% 对得上）⇒ 那一拍是正常遮幅，不是"中间一块"；
  - 前面几拍全是"整块没内容"，判决被喂成 C——**那是量具的错，不是产品的**：这一趟在
    开片后约 1s 就进全屏，而这台盘上这份 4K REMUX 冷启动到第一帧要 6~9s，
    于是全屏那几拍量的是"还没出帧的黑"。用户那一幕恰恰相反：他是**看着画面**点的全屏，
    此刻内核的交换链已经按**窗口态尺寸**建好——真正可疑的形状只可能出现在"有帧之后"。
    ⇒ 第 1 趟的"没复现/C"**都不算数**，它没量到用户所在的那个状态。
  - 由此给量具补了三道：①**等帧闸**（窗口态量不到帧就不进全屏，seek 一次再等，还不行 rc=3）；
    ②PHASES 补一站 12.0s（分得开"瞬态"与"停在那儿了"）；③**二次进全屏**也量两拍
    （第一次是刚建交换链、第二次是拿旧的按新尺寸重铺，两者修法不同）。
  - 屏尺寸订正：这台是 **3840×2160**（第 1 趟 qt_block 现量：Qt 3840×2160@(0,0)，
    物理/逻辑比 1.000），不是先前写在注释里猜的 1920×1080——两者都是 16:9，
    所以"左右 0 px"那条离线结论不受影响。

第 2 趟（2026-10-20 03:35，用户原盘，probe-fullscreen-fit-0335.log）：原生矩形每一拍仍严丝合缝
（容器/子窗口 3840×2160），12 张抓图里唯一一条红是「二次+8.00s」读出右内缩 64px＝「不规则留黑」。
2026-10-21 离线复量把这条红归给**旧尺的缩样**：同一张 r20-re2-fs-8.00s.png，逐像素读
(0,274,0,274)＝上下留黑，旧缩样读 (0,273,64,273)＝不规则留黑——右缘最后 30 列每列只有 1~3 个
像素过阈（最右列只有一个 34 的像素），240 宽采样每格只取一个源像素，整格漏掉。
⇒ 那一趟的 [FINAL]「档位=复现」**不作数**（红在量具，不在产品）；换尺之后要重跑真窗口那一趟
才谈得上结论。证据与机器输出：tests/out/r20-re2-ruler-artifact-20261021.md。

**这一趟会占前台**（全屏本来就压满整块屏，抓像素还要把窗口抬到最上面）。所以：
  - 开跑前两闸门：链锁 `tests/out/chain-r17.lock`（会起真窗口的两趟并跑，读到的就不是被测者）
    ——现在是**先等到点**而不是直接走人（见下面"跑法"）；屏必须真的在出像素
    （`test_ui.capture_health`，与产品同一把尺）。屏不给就 rc=3 走人——不抢前台，也不冒充验过。
  - 第三道闸门是本量具自己：开跑前在同进程跑一遍像素尺自检，没过就 rc=1，不拿唯一一次机会
    去量一把没校准的尺。
  - 由 `tests/watch_fullscreen_r20.py` 在"人离开机器"之后才起这一趟。
  - 只读不写产品档案：数据目录走 test_ui 自带的隔离档案 `tests/.selftest-data`，
    用户那份 `data/config.json` 与 `watch-state.json` 一个字节都不动
    （开跑前后各记一次"尺寸＋mtime"来核，收尾不相同就当场报红）。
  - 内核全程静音：`cfg["mute"]=True` 从**启动参数**进去，并且起窗之后从这台实例
    回读 `mute`/`volume`（静音闸门是进程内的，换个实例就作废）。

跑法：    python tests/probe_fullscreen_fit.py                     # 放合成样片（默认）
          python tests/probe_fullscreen_fit.py --file "<路径>"     # 放用户那个原盘
          python tests/probe_fullscreen_fit.py --selfcheck         # 只校准像素尺，不占前台
          python tests/probe_fullscreen_fit.py --selfcheck-mutate  # 给自检本身证伪，也不占前台
          python tests/probe_fullscreen_fit.py --recheck "tests/out/r20-*.png"
                                                                    # 换尺子之后复量已落盘的抓图（只读）
真窗口那一趟**开跑前先在自己进程里跑一遍 --selfcheck**：尺子没校准就不去抢前台（rc=1 走人）；
链锁 `chain-r17.lock` 还开着时先等最多 15 分钟（看门狗挑的就是"人离开"那一刻，别因为并跑白等），
等满仍没收就照跑并把"可能与别的趟并跑"写进留档——读数按存疑读。
输出：    stdout ＋ tests/out/probe-fullscreen-fit-<时分>.log（逐拍一块，最后 [FINAL] 判决；
          --selfcheck／--selfcheck-mutate／--recheck 各带自己的前缀，免得同一分钟把真那一趟的留档盖掉）
退出码：  0 量到了（A/B/C/D 有其一成立，或明确量到"没复现"）；
          1 量具自身出错，或 --selfcheck 没把尺校准（含 --selfcheck-mutate 有判不到的注入）；
          3 环境不给（屏不出像素／文件不在／内核没起来／窗口态等不到任何一帧）。
判决档位的**机器可读形状**不在本文件里手写：`档位=／档位自洽=` 这一段由 `tests/r20_bucket.emit()`
打印，看门狗 `tests/watch_fullscreen_r20.py` 用同一个文件的 `parse()` 读。为什么这么分：
上一版看门狗拿话术判分流（`"没能复现" not in final`），而本量具的判决文字写作「没复现：…」，
一个「能」字之差就把好趟读成缺陷、把用户那片 4K 的第二次测量整趟跳过——两头的 rc 都还是 0。
字段与形状只此一处，`python tests/r20_bucket.py --selfcheck-mutate` 里 D7–D10 四条就是专门
抓"探针又写回字面量／看门狗又改回话术"的（静态腿，读源码文本，不写盘）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import io
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT_DIR = os.path.join(HERE, "out")
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SELFCHK = "--selfcheck" in sys.argv
MUTATE = "--selfcheck-mutate" in sys.argv
RECHECK = "--recheck" in sys.argv
if SELFCHK or MUTATE or RECHECK:
    # 校准那半截不许占前台，也不许挑日子：锁屏时也得能把尺子验一遍。
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

LOCK = os.path.join(OUT_DIR, "chain-r17.lock")
LOCK_TIMEOUT = 900.0           # 等链锁最多这么久（别的常跑链可能正持着它），到点也照跑并报出来

# 导入次序是承重的：test_ui 在**自己 import 的那一刻**才把 GELU_DATA 设成隔离档案
# （tests/.selftest-data），而 app.config 的 DATA_DIR 是 import 时算死的。顺序反过来，
# 这一趟就会去读写用户真正的 data/config.json——探针不许碰那个。
import test_ui as TU                                    # noqa: E402  必须第一个
from PyQt5.QtCore import QPoint, QRect, Qt                 # noqa: E402
from PyQt5.QtGui import QColor, QImage, QPainter       # noqa: E402
from app import win32 as WIN32                          # noqa: E402
import r20_bucket                                       # noqa: E402  判决档位的唯一打印形状（emit）

# 每一拍的取样时刻（秒，相对 toggle_fullscreen(True) 返回那一刻）。
# 3.2 那一站必须越过 HIDE_AFTER_MS（main_window.py:47 = 2600ms）：控制条收起时
# `_maybe_autohide()` 自己还会再 refit 一次，那是"落定"之前的最后一笔，得量在它之后。
# 6.0 那一站管"自动收起**没**发生"那一支：收起是一次性的（_hide_timer 为 singleShot），
# 光标压在控制条上时整支都不响，而那恰好是"刚点完全屏"的状态——A 就在这支里复现。
# 12.0 是 2026-10-19 补的：第 1 趟量到"前 5 拍全黑、第 6 拍才出帧"，说明这台盘上第一帧
# 落屏要 6~9s，那些"黑"是没出帧而不是画小了；出帧之后再站一站，才分得开"瞬态"与"停在那儿了"
# （交换链尺寸不跟的 B 是**持续**的，不会自己好；会自己好的是解码/重绘的瞬态）。
PHASES = (0.0, 0.15, 0.6, 1.5, 3.2, 6.0, 12.0)
# 二次进全屏的取样时刻：用户那句"点完全屏画面只在中间"也可能只在**退出来再进去**那一趟出现
# （第一次是内核刚建交换链、第二次是拿旧的按新尺寸重铺），所以进去之后停 3s 量一次。
REENTRY = (3.0, 8.0)
# 「瞬态」与「停在那儿了」的分界：2026-10-19 三趟量下来，进全屏后 0~0.6s 那几拍的内缩
# 都会自己好（解码/重绘的过渡），交换链尺寸不跟的那种 B 是**持续**的、不会自己好。
# 所以下面 B 只收 t ≥ SETTLE_S 的拍次；早期拍次单列一行报，既不许它冒充复现，也不许悄悄删掉。
SETTLE_S = 1.5

_rows: list[str] = []       # 同时进 stdout 与日志，日志那份就是留档


def say(msg: str = "") -> None:
    print(msg, flush=True)
    _rows.append(msg)


# ---------------------------------------------------------------- 原生侧的小尺子
_U = ctypes.WinDLL("user32")
_U.GetClassNameW.restype = ctypes.c_int
_U.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_U.IsWindowVisible.restype = wt.BOOL
_U.IsWindowVisible.argtypes = [ctypes.c_void_p]
_U.GetWindowThreadProcessId.restype = ctypes.c_uint
_U.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wt.DWORD)]


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(128)
    n = _U.GetClassNameW(ctypes.c_void_p(int(hwnd)), buf, 128)
    return buf.value if n else "?"


def visible(hwnd: int) -> bool:
    return bool(_U.IsWindowVisible(ctypes.c_void_p(int(hwnd))))


def mine(hwnd: int) -> bool:
    """这个句柄属不属于本进程。读不出 pid 时按"不属于"：宁可少算自己，也别把别人的窗当成我们的。"""
    pid = wt.DWORD(0)
    _U.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
    return pid.value == os.getpid()


def tree(hwnd: int, depth: int = 0, acc: None = None) -> list[tuple[int, int]]:
    """递归展开后代窗口：(hwnd, 层深)。direct_children 只给一层，孙窗口要自己走。

    为什么要走到孙：`fit_child_windows()` 只铺**直接**子窗口，万一 mpv 的渲染面其实在孙一层，
    那"铺了父、没铺孙"就是 A 的第三种形态，只量一层看不见。
    """
    acc = [] if acc is None else acc
    for k in WIN32.direct_children(hwnd):
        acc.append((k, depth))
        tree(k, depth + 1, acc)
    return acc


# ---------------------------------------------------------------- 逐拍取样
def native_block(video) -> dict:
    """原生几何那一摊：容器客户区 vs 每个后代矩形的差。"""
    cw, ch = WIN32.client_size(video.hwnd)
    sl, st, sr, sb = WIN32.window_rect(video.hwnd)
    kids = WIN32.direct_children(video.hwnd)
    say(f"  容器 HWND={video.hwnd:#x} 客户区={cw}×{ch} 屏幕矩形=({sl},{st})-({sr},{sb}) "
        f"直接子窗口 {len(kids)} 个")
    for h, d in tree(video.hwnd):
        r = WIN32.window_rect(h)
        say(f"    {'  ' * d}#{h:#x} '{class_name(h)}' vis={visible(h)} mine={mine(h)} "
            f"rect=({r[0]},{r[1]}) {r[2] - r[0]}×{r[3] - r[1]} "
            f"相对容器=({r[0] - sl},{r[1] - st}) 尺寸差={r[2] - r[0] - cw}×{r[3] - r[1] - ch}")
    fills, detail = TU.mpv_fills(video)
    say(f"  mpv_fills（kids[0] 与客户区差 ≤2 物理像素）= {fills} —— {detail}")
    return {"client": (cw, ch), "kids": len(kids), "fills": fills, "detail": detail}


def kernel_block(win) -> dict:
    """内核侧那一摊：它以为的显示尺寸与几何类取值。get 是同步的，读不到就报读不到。

    `osd-dimensions` 是 2026-10-21 补的**独立旁证**：它直接报 mpv 这一拍认为的渲染矩形与
    四边留白（ml/mr/mt/mb）。像素尺量的是"屏上墨迹贴不贴边"，这条量的是"内核自己把画面
    摆在哪"——两者都指向同一形状才算把 B（内核把画面画小）钉死；一个 dict 而已，读不到
    就按 <读失败> 留在纸上，不影响判决。
    """
    names = ("dwidth", "dheight", "video-out-params/dw", "video-out-params/dh",
             "video-out-params/aspect", "width", "height", "panscan", "video-zoom",
             "keepaspect", "video-align-x", "video-align-y", "video-aspect-override",
             "osd-dimensions", "current-vo", "hwdec-current", "vid")
    got: dict[str, object] = {}
    parts = []
    for n in names:
        try:
            v = win.player.get(n, "<无>")
        except Exception as exc:                       # 属性不存在／管道断了，都要留在纸上
            v = f"<读失败 {type(exc).__name__}>"
        got[n] = v
        parts.append(f"{n}={v}")
    say("  内核：" + " ".join(parts))
    return got


def qt_block(win, video) -> dict:
    host = getattr(win, "host", None)
    hosted = bool(host.is_hosting()) if host is not None else False
    hg = host.geometry() if (host is not None and hosted) else None
    say(f"  Qt：顶层 {win.width()}×{win.height()} state={int(win.windowState())} "
        f"video(逻辑)={video.width()}×{video.height()} dpr={video.devicePixelRatioF():.2f} "
        f"stage={win.stage.width()}×{win.stage.height()} "
        f"bar vis={win.bar.isVisible()}({win.bar.height()}) 光标在条上={win.bar.underMouse()} "
        f"住={'浮层' if hosted else '布局'} 浮层矩形={None if hg is None else (hg.x(), hg.y(), hg.width(), hg.height())} "
        f"title vis={win.title.isVisible()} sidebar vis={win.sidebar.isVisible()}"
        f"({win.sidebar.width()}) empty vis={win.empty.isVisible()}")
    scr = win.screen().geometry()
    ml, mt, mr, mb = WIN32.monitor_fullarea(int(win.winId()))
    say(f"  屏：Qt {scr.width()}×{scr.height()}@({scr.x()},{scr.y()}) / "
        f"原生全屏区=({ml},{mt})-({mr},{mb}) 即 {mr - ml}×{mb - mt}  "
        f"物理/逻辑比={max(1, mr - ml) / max(1, scr.width()):.3f}")
    # 顺手把这一拍的 Qt 形状交出去：`悬停唤醒`那一拍要拿它算"标题栏有没有偷走高度"，
    # 而那个数只能从**同一拍**的 Qt 读数里来，不许事后另开一趟去量（布局会变）。
    # R21 补的四个键是给几何尺分模式用的：`bar_hosted` 决定"控制条该不该算进让行"，
    # `sidebar_vis/w` 决定"该给的宽度是不是整块屏"，`float_wanted` 用来抓"设置里开着浮层、
    # 条却还占着布局行"这一档（＝ChromeHost 没接手）。`host_seen` 单独记"这只眼看没看见宿主"——
    # 看不见时不许把"没看见"当成"产品没做"，那条判据（overlay_off）当场失效而不是翻红。
    return {"title_vis": bool(win.title.isVisible()), "bar_vis": bool(win.bar.isVisible()),
            "bar_h": int(win.bar.height()), "title_h": int(win.title.height()),
            "video": (int(video.width()), int(video.height())),
            "screen_qt": (int(scr.width()), int(scr.height())),
            "screen_phys": (mr - ml, mb - mt), "dpr": float(video.devicePixelRatioF()),
            "bar_hosted": hosted, "host_seen": host is not None,
            "float_wanted": bool(win.cfg["float_controls_in_fs"]),
            "sidebar_vis": bool(win.sidebar.isVisible()),
            "sidebar_w": int(win.sidebar.width()),
            "host_geo": (None if hg is None else
                         (int(hg.x()), int(hg.y()), int(hg.width()), int(hg.height())))}


def hover_wake(win) -> dict:
    """把**真光标**移进画面里晃一下，走"轮询 → sig_hover → _wake_ui"这条真链。

    为什么非要动真光标：`VideoSurface` 是 30ms 轮询 `GetCursorPos` 的（airspace，见
    video_surface.py 的模块注释），`QApplication.sendEvent` 塞一条 MouseMove 它根本不看。
    而用户那一句"点完全屏画面只在中间一块"就是在这一步按下的——他刚点完控制条上的全屏，
    手一抖光标划过画面，`_wake_ui()` 把标题栏也一起唤回来，舞台当场矮一截。
    量完把光标放回原处（探针不许给用户留下"鼠标被搬走了"这种副作用）。
    """
    old = _cursor_pos()
    g = win.video.mapToGlobal(QPoint(0, 0))
    dpr = win.video.devicePixelRatioF() or 1.0
    cx = int(g.x() * dpr) + win.video.width() * dpr // 2
    cy = int(g.y() * dpr) + win.video.height() * dpr // 2
    _set_cursor(cx, cy)
    TU.pump(0.25)
    _set_cursor(cx + 9, cy + 7)        # 位置必须**变过**：_tick 里 raw != _last_raw 才发信号
    TU.pump(0.35)
    say(f"  （光标从 {old} 移到画面中心 ({cx},{cy}) 再挪 9px，走真轮询那条链）")
    out: dict = {"cursor_restored": False}
    q = qt_block(win, win.video)
    nat = native_block(win.video)
    px = pixels_of(win, win.video, "视频区·悬停唤醒", save=png("08-wake"))
    _set_cursor(old[0], old[1])
    # "放回原处"不许自说自话：读完再量一次，量不回来就把它记成 False（这一趟对用户手的欠账）。
    out["cursor_restored"] = _cursor_pos() == (int(old[0]), int(old[1]))
    if not out["cursor_restored"]:
        say(f"  （光标没回到 {old}，现在在 {_cursor_pos()}：探针给用户留了副作用，点名）")
    out.update({"qt": q, "client": nat["client"], "fills": nat["fills"],
                "detail": nat["detail"], "shape": px["shape"], "box": px["box"],
                "usable": px["usable"]})
    # 这一拍的账：屏高被谁分了。标题栏一显，舞台就矮 title_h，画面按高适配 ⇒ 左右各让出黑边。
    # **全程用 Qt 逻辑像素做减法**：`video.height()` 是逻辑的，而 `monitor_fullarea` 是物理的，
    # 在 dpr=1.5 那台实例上混着减会凭空多出半条标题栏（这一趟要量的就是"高度被谁吃了"）。
    ch = chrome_of(q, fs=True)
    out["chrome"] = ch
    out["screen_h"] = q["screen_qt"][1]
    out["lost_h"] = ch["rows_lost"]
    out["title_stole"] = ch["d"]
    say(f"  唤醒结果：标题栏={q['title_vis']} 控制条={q['bar_vis']}"
        f"（住在{'浮层' if ch['bar_hosted'] else '布局'}）侧栏={q['sidebar_vis']}({q['sidebar_w']}px) "
        f"舞台={q['video'][0]}×{q['video'][1]} "
        f"（屏高 {q['screen_qt'][1]} 让掉 {ch['rows_lost']}px：条 {ch['by_bar']} + 标题栏 {ch['by_title']}；"
        f"横向让掉 {q['screen_qt'][0] - q['video'][0]}px，其中侧栏 {ch['by_sidebar']}px）"
        f"子窗口铺满={nat['fills']}；形状={px['shape']}")
    say(f"  ⇒ 悬停把标题栏唤回来了（R20 那一改的复现判据）={ch['d']}"
        f"；舞台比该给的还矮={ch['h_short']} 窄={ch['w_short']}；"
        f"舞台＝整块屏={ch['full_stage']}（浮层生效时这一条才成立）")
    return out



def ink_map(img: QImage, nx: int = 10, ny: int = 6, quiet: bool = False) -> tuple[bool, bool]:
    """把抓到的那块划成 nx×ny 格，逐格问"这里有内容吗"，打出一张字符图。

    返回 (外圈是否整环近黑, 中心是否有内容)。

    为什么用格子而不是一个"整块有没有画面"的数：要分的正是**内容落在哪儿**——
    中心有、外圈黑 = 画面画小了（B）；外圈也亮 = 铺满了；中心也黑 = 根本没画出来（C）。
    一个总数把这三种压成同一个答案。
    为什么 B 必须同时要求"中心有内容"：只判外圈黑会把"整块全黑"（C／内核没出帧）
    误报成"画面画小了"，而这两件的修法完全不同——这一条是 ink_map 自己自检里的 ③。
    """
    if img.isNull():
        if not quiet:
            say("  像素：<无图>")
        return False, False
    w, h = img.width(), img.height()
    grid: list[list[float]] = []
    inky: list[list[bool]] = []
    for j in range(ny):
        row: list[float] = []
        irow: list[bool] = []
        for i in range(nx):
            x0, y0 = int(i * w / nx), int(j * h / ny)
            x1, y1 = int((i + 1) * w / nx), int((j + 1) * h / ny)
            cell = img.copy(QRect(x0, y0, max(1, x1 - x0), max(1, y1 - y0)))
            st = TU.sample_stats(cell, nx=4, ny=4)
            row.append(st["mean"])
            irow.append(bool(st["distinct"] >= 4 or st["max"] > 40))
        grid.append(row)
        inky.append(irow)
    ring = [inky[0] + inky[-1]] + [[c[0], c[-1]] for c in inky[1:-1]]
    ring_ink = sum(1 for cells in ring for v in cells if v)
    ring_total = sum(len(cells) for cells in ring)
    center = inky[ny // 2][nx // 2]
    if not quiet:
        say(f"  像素图（#＝那一格有内容，.＝近黑）外圈亮格={ring_ink}/{ring_total} 中心有内容={center}")
        for j in range(ny):
            say("    " + "".join("#" if v else "." for v in inky[j]) + "  均亮度 "
                + " ".join(f"{v:5.0f}" for v in grid[j]))
    return ring_ink == 0, center


INK_TARGET_W = 240     # `shape_of` 容差的量子（4 格＝原宽/240×4 px）；内缩尺本身**不再**缩样
INK_DELTA = 32         # 「这个像素与外圈底色差多少才算内容」的下限（对色差的宽容度，不是对亮度）
# 「贴边」容差＝量子步长的 **4 格**（不是原图宽度的百分比）。为什么不是旧的 1%：
# 这把尺问的是**内容**到边的距离，而测量现场有两类"内容"不是画面本身：浮层控制条的图标
# （`grab_widget` 抓的是整屏再裁，条醒着时底/左/右各量到 26px 的图标）、样片自己贴边的
# 色块。2026-10-20 两趟真机各抓到一次读数在 (0,48,0,32)／(32,48,0,32)／(0,48,0,96) 之间翻
# （4K 那档一格＝16 物理像素）。三拍的留档图（r20-02/03/04-fs-*.png）逐张看过，画面铺满
# 整块屏、没有任何一处不同；翻的是**旧尺**的读数——它把 3828 宽的图缩到 240 宽、每格只取
# 一个源像素，边缘那几列暗而稀的内容被整格漏掉（`--recheck` 那只脚本与 2026-10-21 的
# 逐像素复量：同一批图现在一致读 (0,274,0,274)）。剩下的 26px 是浮层图标，所以容差仍留四格。
# 四格仍远小于这一把尺要抓的东西——真·B（内核把画面画小）与"舞台被压扁"在 4K 那档是
# 几百像素的内缩（窗口态那一拍左右各 68px 也比它大一截）。两端各有一张已知答案的图夹着：
# ⑧（两格抖动**不许**读成缺陷）与 ⑨（10% 内缩**必须**读成居中偏小），配 M10/M11 两条
# 反向注入——"任何放宽都要配对向哨兵"（记忆 [[feedback-self-written-detectors-need-controls]]）。
EDGE_TOL_CELLS = 4


def _planes(img: QImage):
    """QImage → (r, g, b) 三个 uint8 平面（整图一次转换，逐像素 `pixelColor` 是八百万次 ctypes）。

    为什么必须整图一次拿：全屏那块是 3828×2148＝八百二十万个像素，`pixelColor` 逐点问要
    几分钟，会把取样拍次拖晚（节拍失真）。而"内容贴不贴边"恰恰只能逐像素问——缩到 240 宽
    之后每格只剩一个采样点，真机 re2 右缘那种每列只有 1~3 个像素过阈的暗内容会被整格漏掉
    （2026-10-20 03:35 那一趟唯一的红就是这么来的：读 64px 右内缩，实际 0）。
    """
    if img.format() != QImage.Format_RGB32:
        img = img.convertToFormat(QImage.Format_RGB32)
    w, h = img.width(), img.height()
    bpl = img.bytesPerLine()
    raw = img.constBits().asstring(bpl * h)
    a = np.frombuffer(raw, dtype=np.uint8).reshape(h, bpl)[:, :w * 4].reshape(h, w, 4)
    return a[..., 2], a[..., 1], a[..., 0]      # 小端 Format_RGB32 的内存序是 B,G,R,A


def _bg_of(r, g, b) -> tuple[int, int, int]:
    """外圈底色＝最外一圈像素的**上中位数**（与旧实现同一个量，不是均值、也不是 np.median）。

    为什么不用 np.median：偶数个样本时它取中间两数的平均，而旧实现取 `sorted(v)[len//2]`
    （偏上的那一个）。差一档就会让"底色 28 vs 阈值 32"这类边界案例换答案，而 ⑦ 验收的
    正是这一档；换实现可以，换口径不行。
    为什么按像素而不是按格子取中位：没铺到的那圈**到底是谁画的**没定数——内核画的黑边是
    纯黑，Qt 容器自己露出来的是主题底 #17181C（最大通道 28）。写死 thresh=24 的话 28 刚好
    越过线，"留了一圈黑"会被读成"铺满了"（自检 ⑦ 钉的就是它）。
    """
    h, w = r.shape
    band = max(1, min(w, h) // 25)
    m = np.zeros((h, w), bool)
    m[:band, :] = True
    m[-band:, :] = True
    m[:, :band] = True
    m[:, -band:] = True
    out = []
    for plane in (r, g, b):
        s = np.sort(plane[m])
        out.append(int(s[len(s) // 2]))
    return tuple(out)


def _diff_mask(r, g, b, bg):
    """默认谓词：任一通道与外圈底色差超过 INK_DELTA（向量化版，语义与旧的逐点 test 相同）。"""
    br, bg_, bb = bg
    return ((np.abs(r.astype(np.int16) - br) > INK_DELTA)
            | (np.abs(g.astype(np.int16) - bg_) > INK_DELTA)
            | (np.abs(b.astype(np.int16) - bb) > INK_DELTA))


def _bbox_by_test(img: QImage, flat_bright: int,
                  mask_of) -> tuple[int, int, int, int] | None:
    """内缩尺的骨架：整图**逐像素**问「这个像素算不算内容」→ 取第一/最后一列有内容的行/列。

    `mask_of(r, g, b, bg)` 返回逐像素布尔数组，由调用方给——这把尺的**全部**判断力都在
    这个谓词里，所以它有两种写法时必须各有已知答案盯着（自检的 ⑦ 与证伪的 M7 就是这一对）。
    为什么不缩样：旧版缩到 240 宽、每格只取一个源像素（Qt FastTransformation 实测采样列
    ＝2+5i），真机 re2 的右缘每列只有 1~3 个像素过阈、且都不在采样行上 ⇒ 右缘那几列被
    整格漏掉，读数凭空多出 64px 内缩，「上下留黑」被读成「不规则留黑」。⑩ 是那张已知答案，
    M12 把旧缩样注回来逼它翻红；两边各一条，缺一条这一改就不作数。
    代价：3828×2148 一张约 0.05s（numpy 整图），比缩样那条慢一个量级；十拍合计仍在秒级。
    """
    if img.isNull():
        return None
    r, g, b = _planes(img)
    h, w = r.shape
    bg = _bg_of(r, g, b)
    mask = mask_of(r, g, b, bg)
    rows = mask.any(axis=1)
    cols = mask.any(axis=0)
    # 整块几乎一个色（纯色帧／片头／一整屏黑）＝没有边界可定位。这一支**必须排在**
    # "取第一格"之前：一格都没过时 `argmax` 会把空行报成 0（第一版把顺序写反，
    # ① 那种"整块都亮"的已知答案被读成了"没画"）。按底色分两档：
    # 亮 ⇒ 报"铺满"（至少在画，只是无从定位）；暗 ⇒ 报 None（交给 C 那一档，留档图人过目）。
    inked = int(rows.sum()) * int(cols.sum())
    if inked <= 0.01 * w * h:
        return (0, 0, 0, 0) if max(bg) > flat_bright else None
    first_r = int(np.argmax(rows))
    first_c = int(np.argmax(cols))
    last_r = h - 1 - int(np.argmax(rows[::-1]))
    last_c = w - 1 - int(np.argmax(cols[::-1]))
    # 内缩＝"从这条边往里数多少像素还没有内容"，所以右下两边取的是最后一个有内容的像素**之后**。
    return (first_c, first_r, w - 1 - last_c, h - 1 - last_r)


def _bbox_sampled(img: QImage, flat_bright: int, mask_of,
                  target_w: int = INK_TARGET_W) -> tuple[int, int, int, int] | None:
    """**旧**内缩尺（2026-10-20 03:35 之前）：缩到 240 宽、每格只取一个源像素。

    留在这里只有一个用途：M12 把它注回去，要求 ⑩ 当场翻红——证明"逐像素"这件事不是白改的。
    连旧版"逐列时只看每隔一行的格子"（`mask[::2]`）也照抄：证伪要注的是**那一版**，不是
    一个更讲道理的近似版；否则翻红就不能归因到"缩样"这一条。
    """
    if img.isNull():
        return None
    w, h = img.width(), img.height()
    tw = max(8, min(w, target_w))
    th = max(8, int(h * tw / w))
    small = img.scaled(tw, th, Qt.IgnoreAspectRatio, Qt.FastTransformation)
    r, g, b = _planes(small)
    sh, sw = r.shape
    bg = _bg_of(r, g, b)
    mask = mask_of(r, g, b, bg)
    rows = mask.any(axis=1)
    cols = mask[::2, :].any(axis=0)
    inked = int(rows.sum()) * int(cols.sum())
    if inked <= 0.01 * sw * sh:
        return (0, 0, 0, 0) if max(bg) > flat_bright else None
    first_r = int(np.argmax(rows))
    first_c = int(np.argmax(cols))
    last_r = sh - 1 - int(np.argmax(rows[::-1]))
    last_c = sw - 1 - int(np.argmax(cols[::-1]))
    sx, sy = w / sw, h / sh
    return (int(round(first_c * sx)), int(round(first_r * sy)),
            max(0, w - int(round((last_c + 1) * sx))),
            max(0, h - int(round((last_r + 1) * sy))))


def ink_bbox(img: QImage) -> tuple[int, int, int, int] | None:
    """内容到这块图四边的**像素内缩**：(左, 上, 右, 下)。整块都没内容返回 None。

    为什么在 10×6 的格子之外还要这一把：格子说"有没有"，内缩说"黑边到底在哪几边、各多宽"。
    用户那幕的关键分歧正在形状上——
      只有上下内缩 = 片子自己的遮幅（本片 2.40:1，ffmpeg 已量过＝正常）；
      左右也内缩一大截 = 画面真的比窗口小（B）；
      内缩全贴着一边（另三边≈0）= 子窗口停在旧尺寸没跟着铺（A：WS_CHILD 的坐标是**父客户区**
      相对的，所以旧尺寸必然落在容器左上角，不会居中——这一条能当场把"居中"这个说法证掉）。
    为什么逐像素（而不是旧版缩到 240 宽）：见 `_bbox_by_test`——re2 右缘那几列稀薄的内容
    会被缩样整格漏掉，把正常遮幅读成缺陷形状。逐像素之后**没有量化误差**，
    `shape_of` 的"贴边"容差仍按四格取（见 `EDGE_TOL_CELLS`：那 26px 是浮层图标，不是量化）。
    判"有内容"用的是**与外圈底色的色差**（>INK_DELTA），不是绝对亮度：见 `_bbox_by_test`。
    整块一个色时按底色分流——亮（>64）报"铺满"（无从定位，但至少证明在画），
    暗报 None（＝没画出来／内核没出帧，交给 C 那一档，留档图由人过目）。
    """
    return _bbox_by_test(img, 64, _diff_mask)



def shape_of(box: tuple | None, w: int, h: int, eps: int | None = None) -> str:
    """把量到的内缩归成一个**形状名**（纯函数：selfcheck 用已知答案夹它，主程序用它下判决）。

    为什么要有这一层：A 与 B 的修法完全相反，而它们在"有没有黑边"上长得一样——
    片子的遮幅也留黑、子窗口停在旧尺寸也留黑。分得开它们的不是"有没有黑"，是**黑在哪几边**：
      铺满    四边都贴着（容差见下）
      贴左上  左边与上边贴住、右与下空一大截 ⇒ A（WS_CHILD 的坐标是父客户区相对的，
              旧尺寸必然落在容器左上角——这一档同时把"画面居中"这个口头说法变成可核的数）
      居中偏小 四边都空 ⇒ B（内核把画面画小了）
      上下留黑 左右贴边、只空上下 ⇒ 片子自己的遮幅（本片 2.40:1 就该是这一档，正常）
      左右留黑 上下贴边、只空左右 ⇒ 同上，方向相反（也正常）
      无内容   整块没过亮度阈 ⇒ C（根本没画出来）
    `eps` 取**量化步长的四格**（`EDGE_TOL_CELLS`，下限 2 px——`grab_widget` 抠图时四周各让开
    6 px，贴边的读数天生带几像素）：一格＝原宽/`INK_TARGET_W` 像素，读数天生按格跳，
    而"贴不贴边"问的是**内容**到边的距离——两格以内的抖动是样片自己的色块
    与浮层图标在跳，不是画面在跳（这一档的来龙去脉与真机三拍的读数都写在 `EDGE_TOL_CELLS`）。
    留这个口子给证伪用：`eps=` 显式传坏值（M10 传旧的 1%、M11 传 25%），分类逻辑仍只有这一份。
    """
    if box is None:
        return "无内容"
    l, t, r, b = box
    eps = max(2, int(round(EDGE_TOL_CELLS * w / INK_TARGET_W))) if eps is None else eps
    edge = [v < eps for v in (l, t, r, b)]      # True = 这一边贴住了（贴边＝没留黑）
    if all(edge):
        return "铺满"
    if not any(edge):
        return "居中偏小"
    if edge[0] and edge[1] and not edge[2] and not edge[3]:
        return "贴左上"
    if edge[2] and edge[3] and not edge[0] and not edge[1]:
        return "贴右下"
    if edge[0] and edge[2]:
        return "上下留黑"      # 左右贴边＝横向铺满了，只是纵向有带（本片 2.40:1 该是这个）
    if edge[1] and edge[3]:
        return "左右留黑"      # 上下贴边＝纵向铺满了（把 16:9 塞进更宽的窗口才会这样）
    return "不规则留黑"


def chrome_of(q: dict, fs: bool = True, tol: int = 2) -> dict:
    """把一拍的 Qt 读数算成「全屏时那块舞台被谁压扁了」——纯函数，自检用已知答案夹它。

    为什么必须另开这把尺、光看像素不够（2026-10-19 第 4 趟的教训）：用户那一句
    "周围一圈都是空出来的黑边"在像素尺上叫**居中偏小**，跟真·B（内核把画面画小了）同分——
    因为 mpv 自己的信箱黑边和容器外面那圈黑是同一种黑，分不出来。分得出它们的只有几何：
      全屏时标题栏不该在场（`title_up`）；舞台宽该等于"屏宽减去当场开着的侧栏"（`w_short`）；
      舞台高该等于屏高减去**当场真的醒了、而且真的占了布局行**的那几条（`h_short`）。
    `rows_lost` 报的是屏高被吃掉多少、`by_bar`/`by_title`/`by_sidebar` 报被谁吃的，
    这样"控制条占一行"与"标题栏回来了"（R20 那一改）与"侧栏挤宽"（R21 补的另外半边）分得开。

    R21 为什么要改口径（全屏审查 §3 点名这处结构失明）：老写法 `want_h = sh - by_bar - by_title`
    **无条件**把"条醒着"当成合法的让行，于是"全屏时控制条吃掉一行 ⇒ 左右各一圈黑"这一档
    永远量不出来——尺子把要抓的东西当成天经地义。现在浮层模式（`bar_hosted`）下条不住在布局里，
    它一分高度都不该扣；扣了就是 bug，`h_short` 会响。
    新键各自钉的事：`full_stage`＝舞台就是整块屏（浮层模式落定后**必须**成立，也是像素尺
    能判内核的唯一前提）；`by_sidebar`＝侧栏挤掉的宽度；`overlay_off`＝设置开着浮层、
    条却还占着布局行（宿主没接手；`host_seen` 为假时不作数——看不见宿主是量具的瞎，不是产品的坏）。
    `tol` 不是给用户调的旋钮，是给证伪留的那道口子（C2 把它注成 0，要求 ⑦⑧⑨ 那三档翻红）；
    判据一律走默认值 2。
    """
    sw, sh = q["screen_qt"]
    stage_w, stage_h = q["video"]
    hosted = bool(q.get("bar_hosted", False))
    by_bar = 0 if hosted else (q["bar_h"] if q["bar_vis"] else 0)
    by_title = q["title_h"] if q["title_vis"] else 0
    by_sidebar = int(q.get("sidebar_w", 0)) if q.get("sidebar_vis", False) else 0
    want_h = sh - by_bar - by_title
    want_w = sw - by_sidebar
    return {"fs": bool(fs),
            "title_up": bool(q["title_vis"]),
            "bar_up": bool(q["bar_vis"]),
            "bar_hosted": hosted,
            "rows_lost": sh - stage_h,
            "by_bar": by_bar, "by_title": by_title, "by_sidebar": by_sidebar,
            "w_short": stage_w < want_w - tol,
            "h_short": stage_h < want_h - tol,
            "full_stage": abs(stage_w - sw) <= tol and abs(stage_h - sh) <= tol,
            "overlay_off": bool(fs and q.get("float_wanted", False)
                                and q.get("host_seen", True)
                                and not hosted and q.get("bar_vis", False)),
            # R20 的复现判据：全屏、标题栏在场（它一显，16:9 的画面就只剩"中间一块"）。
            "d": bool(fs and q["title_vis"]),
            # D 在场时像素尺给 B 的那一档（居中偏小）是被几何解释掉的，不许再记进 B 的账。
            # **只要舞台不是整块屏**就算解释掉——浮层没生效时舞台矮一条、侧栏开着时舞台窄一栏，
            # 那种盒子塞 16:9 一样要留黑，同一档同样判不了内核，只是它另案（谁占的行要说得清）。
            "explains_small": bool(fs and (stage_w < sw - 2 or stage_h < sh - 2))}


# ---------------------------------------------------------------- 真光标的挪与放
_CUR_HOME: tuple[int, int] | None = None


def _cursor_pos() -> tuple[int, int]:
    """真光标现在在哪（物理像素）。**只许走 `app.win32.cursor_pos()` 那一份**：它在 import 时
    就把 `user32.GetCursorPos.argtypes` 钉成 `[POINTER(wintypes.POINT)]`，而
    `ctypes.windll.user32` 是**进程内单例**——探针自己另立一个 `class PT(ctypes.Structure)`
    就是拿另一种结构体去撞那份声明。2026-10-20 冒烟趟当场崩在这里
    （`ArgumentError: expected LP_POINT instance instead of pointer to PT`），整趟量具失灵：
    探针 import 了 `test_ui`，`test_ui` 又 import 了 `app.win32`，所以这份 argtypes
    在探针动手之前就已经挂上了，本机必崩（不是环境偶发）。
    """
    return WIN32.cursor_pos()


def _set_cursor(x: int, y: int) -> None:
    """把真光标放到**物理像素**坐标上。argtypes/restype 当场声明：`app/win32.py` 只钉了
    `GetCursorPos` 那一头，`SetCursorPos` 没人管过——不设 restype 时 ctypes 按 int 截返回值，
    而这条链的收尾要把光标还给用户，返回值要能判（0＝没搬动，得报出来）。"""
    u32 = ctypes.windll.user32
    u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u32.SetCursorPos.restype = wt.BOOL
    if not u32.SetCursorPos(int(x), int(y)):
        say(f"  （SetCursorPos({x},{y}) 返回 0：光标没搬动，这一拍的『条醒着/收起』不作数）")


def park_cursor(win) -> None:
    """进全屏那一刻把**真光标**挪到舞台正中，之后不再动（收尾放回原处）。

    为什么这一趟非要动真光标：`_maybe_autohide()` 的条件是"光标不在条上、也不在标题栏上"。
    探针是程序化调 `toggle_fullscreen()` 的，光标就停在用户上一次点的地方——压在最底下那条
    控制条上是常事，那样整支自动收起都不响，每一拍都成了"条醒着"那一档，**落定之后的形状**
    就量不到了（那一档才是 R20 的判决所在）。挪到舞台正中是把"手离开鼠标"这个前提做出来，
    不是放宽判据：量不到的东西不许写进结论，那就让它量得到。
    """
    global _CUR_HOME
    _CUR_HOME = _cursor_pos()
    g = win.video.mapToGlobal(QPoint(0, 0))
    dpr = win.video.devicePixelRatioF() or 1.0
    cx = int(g.x() * dpr) + int(win.video.width() * dpr) // 2
    cy = int(g.y() * dpr) + int(win.video.height() * dpr) // 2
    _set_cursor(cx, cy)
    say(f"  （真光标从 {_CUR_HOME} 挪到舞台正中 ({cx},{cy})：让自动收起那一支真的能响；收尾放回）")


def restore_cursor() -> None:
    """把 park_cursor 记下的那个位置还给系统——幂等，没挪过就什么都不做。
    放回之后**再量一次**：给用户留了副作用却没说，比留了副作用更糟。"""
    global _CUR_HOME
    if _CUR_HOME is None:
        return
    _set_cursor(_CUR_HOME[0], _CUR_HOME[1])
    now = _cursor_pos()
    say(f"  （真光标放回 {_CUR_HOME}"
        + ("，量回来一致）" if now == tuple(_CUR_HOME) else f" 失败：现在在 {now}，请手动归位）"))
    _CUR_HOME = None


def pixels_of(win, video, name: str, save: str = "", quiet: bool = False) -> dict:
    """抓"视频容器那块"的像素，一次跑三把尺（格子／内缩／形状名）。

    `quiet=True` 只关打印、不关量：等第一帧那种轮询要抓十几次，全印出来会把判决埋在噪声里。
    """
    img, err = TU.capture(win, video, name)
    if err or img.isNull():
        if not quiet:
            say(f"  像素：{err or '空图'}（这一拍只记几何，像素不作数）")
        return {"usable": False, "ring": False, "center": False, "box": None,
                "wh": (0, 0), "shape": "无图", "err": err or "空图"}
    ring_black, center_ink = ink_map(img)
    box = ink_bbox(img)
    w, h = img.width(), img.height()
    sh = shape_of(box, w, h)
    if not quiet:
        if box is None:
            say(f"  像素尺②内容内缩：整块都没过亮度阈（黑／没出帧）（图 {w}×{h}）")
        else:
            say(f"  像素尺②内容内缩：左{box[0]} 上{box[1]} 右{box[2]} 下{box[3]} 物理像素"
                f"（占宽 {box[0] / w:.2f}/{box[2] / w:.2f}、占高 {box[1] / h:.2f}/{box[3] / h:.2f}）"
                f"⇒ 内容 {w - box[0] - box[2]}×{h - box[1] - box[3]}，图 {w}×{h}")
        say(f"  像素尺③形状名：{sh}")
    if save:
        done = bool(img.save(save))
        sz = os.path.getsize(save) if done and os.path.exists(save) else 0
        say(f"  留档图：{save} 写盘={done} 字节={sz}")
    return {"usable": True, "ring": ring_black, "center": center_ink, "box": box,
            "wh": (w, h), "shape": sh, "err": ""}


def png(tag: str) -> str:
    """每一拍的留档图（ASCII 名，免得中文路径在别的工具里再遭一次编码罪）。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    return os.path.join(OUT_DIR, f"r20-{tag}.png")


def wait_frame(win, budget: float, name: str) -> tuple[dict, float]:
    """等视频容器那块**真的画出一帧**（形状不在「无内容／无图」里），每 1.5s 探一次。

    为什么这是量具的必需前置（2026-10-19 第 1 趟的教训，probe-fullscreen-fit-2333.log）：
    那一趟在开片后约 1s 就进全屏，于是全屏 7 拍里 5 拍量到"整块没内容"，判决被喂成
    「C：根本没画出来」。那不是产品坏，是第一帧还没落屏（这台盘上 4K REMUX 冷启动到
    第一帧要 6~9s）。更要命的是**方向**：用户那一幕是"看着画面点了全屏"，此刻 mpv 的
    交换链已经按**窗口态尺寸**建好——真正可疑的形状（矩形铺满了而画面还是旧尺寸那一小块）
    只可能出现在"有帧之后"。所以没出帧之前，这一趟一把尺都不该开口。

    超时不算失败，交回调用方决定（可以 seek 之后再等一轮）；返回（最后一次读数, 等了多久）。
    """
    t0 = time.monotonic()
    px = {"usable": False, "shape": "无图", "box": None, "ring": False, "center": False,
          "wh": (0, 0), "err": "还没抓"}
    while True:
        px = pixels_of(win, win.video, name, quiet=True)
        if px["usable"] and px["shape"] not in ("无内容", "无图"):
            return px, time.monotonic() - t0
        if time.monotonic() - t0 > budget:
            return px, time.monotonic() - t0
        TU.pump(1.5)


# ---------------------------------------------------------------- 记录 refit 的调用
def arm_refit_recorder(video) -> list[dict]:
    """包一层：每次 refit 都记下"当时客户区多大、铺了几个"。

    这一条是给 A 分诊用的：A 的两种修法（"没人再铺"与"铺的时候尺寸还旧"）
    光看最后一拍的原生矩形分不出来，看调用序列才分得出。
    """
    log: list[dict] = []
    orig = video.refit

    def wrapped():
        cw, ch = WIN32.client_size(video.hwnd)
        n = orig()
        log.append({"t": time.monotonic(), "client": (cw, ch), "n": n})
        return n

    video.refit = wrapped                                # type: ignore[assignment]
    return log


# ---------------------------------------------------------------- 前置闸门与留痕
def fingerprint(path: str) -> str:
    """一个文件的尺寸与改动时刻——用来证明这一趟没碰用户那份档案。"""
    try:
        st = os.stat(path)
        return (f"{os.path.basename(path)} size={st.st_size} "
                f"mtime={time.strftime('%H:%M:%S', time.localtime(st.st_mtime))}")
    except OSError as exc:
        return f"{path} 读不到：{type(exc).__name__}: {exc}"


def argv_file() -> str:
    for i, a in enumerate(sys.argv):
        if a == "--file" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


# ---------------------------------------------------------------- 尺子自己的校准（不占前台）
# 构造图与期望内缩**共用同一组比例**：改这里，自检的"答案"跟着变，不会出现"构造动了、
# 答案还是手抄的旧数"那种假绿／假红。
BOX_FRAC = 0.35        # ② 中间那块：四边各让开 35%
ANCH_W, ANCH_H = 0.65, 0.67   # ⑤ 贴左上那块
BAR_FRAC = 0.13        # ⑥ 遮幅带：与 ffmpeg 量的本片黑边同档
NUDGE_CELLS = 2        # ⑧ 内容左边让开的**格**数＝真机 +0.60s 那一拍量到的读数抖动（不是像素）
SMALL_FRAC = 0.10      # ⑨ 四边各让开一成：容差放宽之后仍必须抓到的那一档黑框


def fill_img(w: int, h: int, rgb: tuple[int, int, int]) -> QImage:
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor(*rgb))
    return img


def box_in_black(w: int, h: int, rgb: tuple[int, int, int]) -> QImage:
    """黑底中间一块——B 的形状。`QImage` 只有整图 `fill()`，局部矩形要走 QPainter。"""
    img = fill_img(w, h, (0, 0, 0))
    bx, by = int(w * BOX_FRAC), int(h * BOX_FRAC)
    assert w - 2 * bx > 0 and h - 2 * by > 0, "比例常量把已知答案画成空矩形了，这张图不作数"
    painter = QPainter(img)
    painter.fillRect(QRect(bx, by, w - 2 * bx, h - 2 * by), QColor(*rgb))
    painter.end()
    return img


def anchored_top_left(w: int, h: int, rgb: tuple[int, int, int]) -> QImage:
    """贴左上的一块（旧尺寸留在原位）——A 该有的形状，用来证明尺分得开「贴左上」与「居中」。"""
    img = fill_img(w, h, (0, 0, 0))
    aw, ah = int(w * ANCH_W), int(h * ANCH_H)
    assert 0 < aw < w and 0 < ah < h, "比例常量把已知答案画成铺满了，这张图不作数"
    painter = QPainter(img)
    painter.fillRect(QRect(0, 0, aw, ah), QColor(*rgb))
    painter.end()
    return img


def letterbox(w: int, h: int, rgb: tuple[int, int, int]) -> QImage:
    """上下留一条黑带、左右铺满——**本片 2.40:1 在全屏下本来就该长这样**（ffmpeg 量的 13%）。
    拿它当已知答案是为了钉死一件事：形状尺不许把「片子自己的遮幅」报成 B。
    上下带同宽（画的是 `y=band … h-band`），所以答案里的内缩是 `(0, band, 0, band)`。"""
    img = fill_img(w, h, (0, 0, 0))
    band = int(h * BAR_FRAC)
    assert 0 < band and h - 2 * band > 0, "遮幅带把内容吃掉了一半以上，这张图不作数"
    painter = QPainter(img)
    painter.fillRect(QRect(0, band, w, h - 2 * band), QColor(*rgb))
    painter.end()
    return img


def box_on_bg(w: int, h: int, rgb: tuple[int, int, int], bg: tuple[int, int, int]) -> QImage:
    """**主题底色**（不是纯黑）上一块内容——⑦，专门钉那处瞎：
    没铺到的那圈到底谁画的没有定数，Qt 容器露出来的是 `theme.BG=#17181C`（最大通道 28）。
    拿绝对亮度阈（旧写法 thresh=24）量它，28 刚好越线 ⇒ "四周留黑"会被读成"铺满了"，
    而这正是本趟要判的那一档。已知答案里必须有这张，尺子才算被夹住。"""
    img = fill_img(w, h, bg)
    bx, by = int(w * BOX_FRAC), int(h * BOX_FRAC)
    assert w - 2 * bx > 0 and h - 2 * by > 0, "比例常量把已知答案画成空矩形了，这张图不作数"
    painter = QPainter(img)
    painter.fillRect(QRect(bx, by, w - 2 * bx, h - 2 * by), QColor(*rgb))
    painter.end()
    return img


def letterbox_nudge(w: int, h: int, rgb: tuple[int, int, int], cells: int = NUDGE_CELLS) -> QImage:
    """⑧：上下留遮幅带，内容**左边再让开两格**——真机量到的那一档抖动长这样。
    （4K 那趟同一版产品、同一支样片：+0.15s 内缩 (0,48,0,32)、+0.60s (32,48,0,32)、
    +1.50s (0,48,0,96)，一格＝16 物理像素，三拍之间翻的只有"左边算不算贴住"。）
    已知答案：**形状仍须「上下留黑」**。旧口径（1%＝这张图上的 7px）会把它读成「不规则留黑」
    ＝把样片色块的读数抖动写成画面的缺陷形状；M10 就是逼这张图证明新容差真的在管这件事。"""
    img = fill_img(w, h, (0, 0, 0))
    band = int(h * BAR_FRAC)
    left = int(round(cells * w / INK_TARGET_W))
    assert 0 < band and h - 2 * band > 0 and 0 < left < w // 8, "已知答案画歪了，这张图不作数"
    painter = QPainter(img)
    painter.fillRect(QRect(left, band, w - left, h - 2 * band), QColor(*rgb))
    painter.end()
    return img


def letterbox_faint_edge(w: int, h: int, rgb: tuple[int, int, int], band: int | None = None,
                         faint_w: int = 24) -> QImage:
    """⑩：遮幅正常、内容铺满全宽，但**右缘那一小带又暗又稀**——真机 re2 的形状。

    真机那次（2026-10-20 24:04 探针第 6/6 拍）：右缘 30 列每列只有 1~3 个像素比底色亮 32
    以上（列均值 <0.5），而旧的 240 宽缩样每格只取一个源像素（实测采样列 = 2+5i），
    这几列被整格漏掉，右内缩凭空多出 64px ⇒「上下留黑」被读成「不规则留黑」。
    这张图把它做成已知答案：内容铺满 0..w-1，右缘 `faint_w` 像素压暗到 9（低于 INK_DELTA），
    再画一条 60 灰的细线一直伸到最后一列、行号落在旧采样行 2+5j **之外**——
      · 逐像素：右缘确有内容 ⇒ 右内缩 0，形状「上下留黑」；
      · 旧缩样：细线整条落在采样行之间，最后几个采样列又都在暗带里 ⇒ 右内缩被读大。
    M12 把旧缩样注回来，就是逼这张图当场翻红（没有它，这一改只是"看起来更细"）。
    """
    band = int(h * BAR_FRAC) if band is None else band
    assert w == 1200 and h == 700, \
        f"这张已知答案的采样行是实测的（1200×700→240×140 时源行=2+5j），换了尺寸得重新量：{w}×{h}"
    assert 0 < faint_w and faint_w > EDGE_TOL_CELLS * w / INK_TARGET_W, \
        f"右缘暗带（{faint_w}px）不够宽：旧尺可能也漏不掉它，这张图就夹不住 M12"
    assert band % 5 != 0, f"细线行 {band + 2}（＝band+2）落在旧采样行 2+5j 上，这张图夹不住旧尺"
    img = fill_img(w, h, (0, 0, 0))
    p = QPainter(img)
    p.fillRect(QRect(0, band, w, h - 2 * band), QColor(*rgb))
    # 右缘压暗（低于 INK_DELTA ⇒ 旧尺看见的是"没内容"），再补一条细线伸到最后一列。
    p.fillRect(QRect(w - faint_w, band, faint_w, h - 2 * band), QColor(9, 9, 9))
    p.fillRect(QRect(w - faint_w, band + 2, faint_w, 1), QColor(60, 60, 60))
    p.end()
    return img


def box_frac(w: int, h: int, rgb: tuple[int, int, int], frac: float = SMALL_FRAC) -> QImage:
    """⑨：四边各让开 `frac`（一成）的一块——**容差放宽之后仍必须抓到**的那一档。
    这张给容差划下界：它只许盖住读数抖动（两格），不许盖住用户一眼看得见的黑框
    （一成宽在 4K 上是左右各 383 物理像素＝真·B「内核把画面画小了」）。
    M11 把容差撑到 25%，就是逼这张图当场翻红。"""
    img = fill_img(w, h, (0, 0, 0))
    bx, by = int(w * frac), int(h * frac)
    assert w - 2 * bx > 0 and h - 2 * by > 0, "比例常量把已知答案画成空矩形了，这张图不作数"
    painter = QPainter(img)
    painter.fillRect(QRect(bx, by, w - 2 * bx, h - 2 * by), QColor(*rgb))
    painter.end()
    return img


def selfcheck(fns: tuple | None = None, say_it: bool = True) -> tuple[int, list[bool]]:
    """给三把像素尺喂**已知答案**的图，量具先证明自己分得开，再谈它的读数。

    为什么这一半可以在锁屏／后台跑：它只需要 QImage，不需要真窗口；
    `QT_QPA_PLATFORM=offscreen` 在 import Qt 之前就设上了。
    各张图挡的误报：② 是 B 的形状；③ 挡「整块全黑被当成画面画小」；④ 挡「空图时跟着猜」；
    ⑤ 是 A 的形状——**这一张是这一趟最要紧的对照**：② 与 ⑤ 在「有没有黑边」上同分，
    只有形状名把它们分开，而这两件的修法相反；⑥ 挡「把片子自己的遮幅报成 B」；
    ⑦ 挡「留的那圈不是纯黑而是主题底色」——旧实现用绝对亮度阈 thresh=24，而 `theme.BG=#17181C`
    的最大通道是 28，刚好越线，那一圈会被当成内容（M7 就是逼这张图证明它有效）；
    ⑧⑨ 是**一对**：⑧ 挡"把两格的读数抖动当成缺陷形状"（真机 +0.60s 那一拍），
    ⑨ 挡"为了 ⑧ 把容差一路放松、连一成宽的黑框都不响"。两张各配一条反向注入（M10/M11），
    缺一张、或两条注入有一条不翻红，这一对就不成立（记忆 [[feedback-self-written-detectors-need-controls]]：
    任何放宽都要配对向哨兵，否则分不出「口径变准」与「守卫被松」）。
    ⑩ 挡"右缘暗而稀的内容被缩样整格漏掉"：真机 re2 每列只有 1~3 个像素过阈，旧尺读 64px
    右内缩、逐像素读 0——同一张图两种答案，而其中一个是缺陷形状。M12 把旧缩样注回来，
    ⑩ 必须翻红；没有这一对，改"逐像素"就只是换了个实现，证明不了它救了哪一档。

    `fns` 是给证伪趟换尺子用的（默认用真的那三把）；返回值带逐条 PASS/FAIL，
    好让证伪那边比一比"换了坏尺子之后读数确实变了"——没变就是这条注入是空操作，
    不许拿"自检还是绿的"当"注入抓到了"。
    """
    W, H = 1200, 700
    # 槽位数先钉死：注错位置（把坏形状尺插进格子尺那一槽）不该以 TypeError 侥幸收工。
    assert fns is None or (isinstance(fns, tuple) and len(fns) == 3), \
        f"fns 必须是（格子尺, 内缩尺, 形状尺）三槽，拿到 {None if fns is None else len(fns)} 项"
    im, ib, so = fns or (ink_map, ink_bbox, shape_of)    # 容差＝尺子自己的量化步长（宽缩到 240 格，一格 ≈ W/240 像素；两边都可能错一格）。
    TOL = max(8, int(W / INK_TARGET_W) * 2) + 2   # 合成图的画边余量（两格＋2）；逐像素尺本身没有量化误差
    bx, by = int(W * BOX_FRAC), int(H * BOX_FRAC)     # ② 的那一块：四边内缩都是这个数
    aw, ah = int(W * ANCH_W), int(H * ANCH_H)         # ⑤ 贴左上那块
    band = int(H * BAR_FRAC)                          # ⑥ 的遮幅带

    cases = [
        ("①整块都有内容 ⇒ 外圈不该黑、中心该有、形状该是「铺满」",
         fill_img(W, H, (200, 120, 60)), False, True, "铺满", (0, 0, 0, 0)),
        ("②只有中间一块 ⇒ 外圈该整环黑、中心该有、形状该是「居中偏小」（这就是 B）",
         box_in_black(W, H, (255, 0, 0)), True, True, "居中偏小", (bx, by, bx, by)),
        ("③整块全黑 ⇒ 外圈黑**而且**中心也没内容（这是 C，不许被当成 B）",
         fill_img(W, H, (0, 0, 0)), True, False, "无内容", None),
        ("④空图 ⇒ 两个数都按「没有证据」报，不许跟着猜", QImage(), False, False, "无图", None),
        ("⑤贴左上的一块 ⇒ 形状该是「贴左上」（A 的形状；与 ② 同样「有黑边」但修法相反）",
         anchored_top_left(W, H, (0, 200, 120)), False, True, "贴左上",
         (0, 0, W - aw, H - ah)),
        ("⑥上下留黑带、左右铺满 ⇒ 形状该是「上下留黑」（片子自己的遮幅，不是 B）",
         letterbox(W, H, (60, 120, 200)), False, True, "上下留黑",
         # 带宽 band ⇒ 内容从 y=band 到 y=h-band，下边让开的也是 band（不是 h−2·band：
         # 那是内容的**高度**，第一版把它当成下边内缩写进答案，⑥ 就一直在要求一个几何上不成立的数）。
         (0, band, 0, band)),
        ("⑦主题底色（#17181C，最大通道 28）上中间一块 ⇒ 仍须读成「居中偏小」。"
         "这张钉的是绝对亮度阈：28 会越过旧 thresh=24，留黑圈就被当成铺满了",
         box_on_bg(W, H, (200, 60, 40), (23, 24, 28)), True, True, "居中偏小", (bx, by, bx, by)),
        ("⑧上下遮幅＋内容左边让开**两格**（＝真机 +0.60s 那一拍的内缩形状）⇒ 仍须「上下留黑」："
         "两格是这把尺自己的读数抖动（样片那行色块最左一格这一刻是不是暗色、浮层图标盖掉的那一带"
         "还剩多宽），不是画面在跳。旧容差（1%＝这张图上的 7px）夹在两格与三格中间，"
         "把这一拍读成「不规则留黑」＝量具造出来的假案（M10 逼这张图证明新容差在管事）",
         letterbox_nudge(W, H, (250, 200, 40)), False, True, "上下留黑",
         (int(round(NUDGE_CELLS * W / INK_TARGET_W)), band, 0, band)),
        ("⑨四边各让开一成 ⇒ 必须仍是「居中偏小」：容差放宽只许盖住读数抖动，"
         "不许盖住用户看得见的一成黑框（4K 那档＝左右各 383 物理像素，正是真·B）（M11 钉它）",
         box_frac(W, H, (30, 200, 250)), False, True, "居中偏小",
         (int(W * SMALL_FRAC), int(H * SMALL_FRAC), int(W * SMALL_FRAC), int(H * SMALL_FRAC))),
        ("⑩遮幅正常、右缘一窄带**暗而稀**的内容（真机 re2：每列 1~3 个过阈像素）⇒ 仍须"
         "「上下留黑」、右内缩 0：旧缩样每格只取一个源像素，这几列会被整格漏掉（M12 钉它）",
         letterbox_faint_edge(W, H, (60, 120, 200)), False, True, "上下留黑",
         (0, band, 0, band)),
    ]
    bad = 0
    results: list[bool] = []
    if say_it:
        say(f"---- 像素尺自检（已知答案的图：格子尺＋内缩尺＋形状名；内缩容差 ±{TOL}px"
            f"＝合成图的画边余量，逐像素尺本身是精确的）----")
    for name, img, want_ring, want_center, want_shape, want_box_ in cases:
        ring_black, center_ink = im(img, quiet=True)
        box = ib(img)
        sh = so(box, img.width(), img.height()) if not img.isNull() else "无图"
        box_ok = (box == want_box_) if want_box_ is None or box is None else all(
            abs(a - b) <= TOL for a, b in zip(box, want_box_))
        ok = (ring_black == want_ring and center_ink == want_center
              and sh == want_shape and box_ok)
        bad += 0 if ok else 1
        results.append(ok)
        if say_it:
            say(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            say(f"        量到 外圈整环黑={ring_black}（该 {want_ring}）中心有内容={center_ink}（该 {want_center}）"
                f"内缩={box}（该 {want_box_}±{TOL}）形状={sh}（该 {want_shape}）")
    if say_it:
        say(f"[FINAL] R20 像素尺自检：共 {len(cases)} 张已知答案，错 {bad} 张 ⇒ rc={1 if bad else 0}")
    return (1 if bad else 0), results


# ---------------------------------------------------------------- 几何尺（D）自己的校准
def _chrome_q(sw, sh, vw, vh, bar_vis, bar_h, title_vis, title_h,
              hosted: bool = False, float_wanted: bool = False,
              sb_vis: bool = False, sb_w: int = 0, host_seen: bool = True) -> dict:
    """按 qt_block 交出来的那把形状造一份已知答案的读数（别手抄别的字段）。

    后四个默认值就是"R20 那一版产品"的形状：条住在布局里（hosted=False）、设置里没有浮层这个键、
    侧栏关着。老那 8 档因此一个字都不必改输入——尺子换了口径，已知答案得跟着换，
    但**输入**不该被硬拖着改（那会把"哪一档是老的、哪一档是新的"这条线索抹掉）。
    """
    return {"screen_qt": (sw, sh), "video": (vw, vh),
            "bar_vis": bar_vis, "bar_h": bar_h,
            "title_vis": title_vis, "title_h": title_h,
            "screen_phys": (sw, sh), "dpr": 1.0,
            "bar_hosted": hosted, "float_wanted": float_wanted,
            "sidebar_vis": sb_vis, "sidebar_w": sb_w, "host_seen": host_seen,
            "host_geo": None}


CHROME_KEYS = ("d", "explains_small", "w_short", "h_short", "title_up", "rows_lost",
               "by_bar", "by_title", "bar_hosted", "by_sidebar", "full_stage", "overlay_off")


def selfcheck_chrome(fn=chrome_of, say_it: bool = True) -> tuple[int, list[bool]]:
    """几何尺的已知答案：每档都是"屏 2560×1440、条 101、标题栏 59"（＝这台机 dpr≈1.5 的逻辑尺寸）。

    为什么这把新尺也必须先自证：D 是这一趟**唯一**能把「居中偏小」判成"不是 B"的谓词，
    它瞎了就等于把用户的锅记到内核头上（修法相反），它过敏了就等于把好好的全屏报成坏。
    ⑦⑧⑨ 钉的是容差与归因：Qt 布局给的高度常差 1px（边距取整）、侧栏开着时"该给的宽"本来就窄一栏，
    那都不该响。⑤ 钉的是"别抢戏"：窗口态本来就不是屏大小，D／explains_small 都不许响。
    ⑩⑪ 是 R21 那一改的正反两面：浮层生效时舞台**必须**等于整块屏（⑩），
    浮层还开着而舞台仍被压矮＝有别的东西占着行（⑪，老尺子会把这条当成天经地义，所以必须新立）。
    ⑫⑬ 钉"设置要浮层、条却还占行"这一档抓得住（⑫），而**量具看不见宿主时不许把产品判坏**（⑬）。
    """
    W, H, BAR, TITLE = 2560, 1440, 101, 59
    cases = [
        ("①全屏、控制条醒着且住在布局里、标题栏藏着 ⇒ 舞台 2560×1339 是**该给的**：D 与两条 short 都不许响；"
         "但 explains_small 该响——舞台不是整块屏时像素尺就判不了内核，哪怕这一条是合法的",
         True, _chrome_q(W, H, W, H - BAR, True, BAR, False, TITLE),
         {"d": False, "explains_small": True, "w_short": False, "h_short": False,
          "title_up": False, "rows_lost": BAR, "by_bar": BAR, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
        ("②R20 的真形状：悬停把标题栏唤回来 ⇒ 舞台 2560×1280，D 响且它解释掉「居中偏小」",
         True, _chrome_q(W, H, W, H - BAR - TITLE, True, BAR, True, TITLE),
         {"d": True, "explains_small": True, "w_short": False, "h_short": False,
          "title_up": True, "rows_lost": BAR + TITLE, "by_bar": BAR, "by_title": TITLE,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
        ("③全屏且控制条也收起了 ⇒ 舞台＝整块屏，什么都不许响（full_stage 是这一档唯一的正向证据）",
         True, _chrome_q(W, H, W, H, False, BAR, False, TITLE),
         {"d": False, "explains_small": False, "w_short": False, "h_short": False,
          "title_up": False, "rows_lost": 0, "by_bar": 0, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": True, "overlay_off": False}),
        ("④侧栏把舞台挤窄（2300×1339）⇒ 只有 w_short／explains_small 该响，D 不抢这一档",
         True, _chrome_q(W, H, W - 260, H - BAR, True, BAR, False, TITLE),
         {"d": False, "explains_small": True, "w_short": True, "h_short": False,
          "title_up": False, "rows_lost": BAR, "by_bar": BAR, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
        ("⑤窗口态（fs=False）⇒ D／explains_small 都不许响（它压根不在全屏这一幕里），"
         "但 w_short、h_short 照实报：窗口本来就不是屏大小",
         False, _chrome_q(W, H, 1200, 600, True, BAR, True, TITLE),
         {"d": False, "explains_small": False, "w_short": True, "h_short": True,
          "title_up": True, "rows_lost": H - 600, "by_bar": BAR, "by_title": TITLE,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
        ("⑥舞台比「屏高−醒着的条」还矮 39px（有别的东西占行）⇒ h_short 该响、D 不该响",
         True, _chrome_q(W, H, W, H - BAR - 39, True, BAR, False, TITLE),
         {"d": False, "explains_small": True, "w_short": False, "h_short": True,
          "title_up": False, "rows_lost": BAR + 39, "by_bar": BAR, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
        ("⑦舞台 2560×1338（只差 1px 取整）⇒ h_short 不许响：容差 2px 之内是量化噪声",
         True, _chrome_q(W, H, W, H - BAR - 1, True, BAR, False, TITLE),
         {"d": False, "explains_small": True, "w_short": False, "h_short": False,
          "title_up": False, "rows_lost": BAR + 1, "by_bar": BAR, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
        ("⑧舞台 2559×1339（只差 1px）⇒ w_short 不许响：同上",
         True, _chrome_q(W, H, W - 1, H - BAR, True, BAR, False, TITLE),
         {"d": False, "explains_small": True, "w_short": False, "h_short": False,
          "title_up": False, "rows_lost": BAR, "by_bar": BAR, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
        ("⑨侧栏开着 260 且舞台就是 2300×1339 ⇒ w_short 不许响：那一栏是侧栏**该挤**的宽，"
         "归因走 by_sidebar（R21 补的横向半边：全屏里不收侧栏就是上下留黑）",
         True, _chrome_q(W, H, W - 260, H - BAR, True, BAR, False, TITLE,
                         sb_vis=True, sb_w=260),
         {"d": False, "explains_small": True, "w_short": False, "h_short": False,
          "title_up": False, "rows_lost": BAR, "by_bar": BAR, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 260, "full_stage": False, "overlay_off": False}),
        ("⑩修完之后的浮层稳态：条浮在画面上、舞台＝整块屏 ⇒ 什么都不许响，"
         "且 explains_small 必须**不响**（舞台就是整块屏时像素尺才第一次有权判内核）",
         True, _chrome_q(W, H, W, H, True, BAR, False, TITLE,
                         hosted=True, float_wanted=True),
         {"d": False, "explains_small": False, "w_short": False, "h_short": False,
          "title_up": False, "rows_lost": 0, "by_bar": 0, "by_title": 0,
          "bar_hosted": True, "by_sidebar": 0, "full_stage": True, "overlay_off": False}),
        ("⑪浮层开着（条不住在布局里）舞台却还是 2560×1339 ⇒ h_short 必须响："
         "老尺子在这里把「醒着的条」当合法让行、于是永远量不到这一档（2026-10-20 审查点名的结构失明）",
         True, _chrome_q(W, H, W, H - BAR, True, BAR, False, TITLE,
                         hosted=True, float_wanted=True),
         {"d": False, "explains_small": True, "w_short": False, "h_short": True,
          "title_up": False, "rows_lost": BAR, "by_bar": 0, "by_title": 0,
          "bar_hosted": True, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
        ("⑫设置里开着浮层、条却还占着布局行（＝ChromeHost 没接手）⇒ overlay_off 该响；"
         "几何上它和 ① 一模一样（by_bar 照扣、h_short 不响），所以这一档只能由 overlay_off 抓",
         True, _chrome_q(W, H, W, H - BAR, True, BAR, False, TITLE, float_wanted=True),
         {"d": False, "explains_small": True, "w_short": False, "h_short": False,
          "title_up": False, "rows_lost": BAR, "by_bar": BAR, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": True}),
        ("⑬同一形状但**量具没看见宿主**（host_seen=False）⇒ overlay_off 不许响："
         "看不见宿主是量具的瞎，不许写成产品的坏；这一档降级回旧口径（按条占行算，h_short 不响）",
         True, _chrome_q(W, H, W, H - BAR, True, BAR, False, TITLE,
                         float_wanted=True, host_seen=False),
         {"d": False, "explains_small": True, "w_short": False, "h_short": False,
          "title_up": False, "rows_lost": BAR, "by_bar": BAR, "by_title": 0,
          "bar_hosted": False, "by_sidebar": 0, "full_stage": False, "overlay_off": False}),
    ]
    bad = 0
    results: list[bool] = []
    if say_it:
        say(f"---- 几何尺自检（已知答案的读数：{len(cases)} 档，逐键比 {CHROME_KEYS}）----")
    for name, fs, q, want in cases:
        got = fn(q, fs)
        miss = [f"{k}＝{got[k]}（该 {want[k]}）" for k in CHROME_KEYS if got[k] != want[k]]
        ok = not miss
        bad += 0 if ok else 1
        results.append(ok)
        if say_it:
            say(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            say(f"        量到 " + " ".join(f"{k}={got[k]}" for k in CHROME_KEYS)
                + ("｜ 差在：" + "；".join(miss) if miss else ""))
    if say_it:
        say(f"[FINAL] R20 几何尺自检：共 {len(cases)} 档已知答案，错 {bad} 档 ⇒ rc={1 if bad else 0}")
    return (1 if bad else 0), results


_CHROME_MUTATIONS = (
    ("C1 几何尺瞎在标题栏上（d 恒 False）⇒ 只有 ② 该翻红（不然 R20 的复现判据就是编的）",
     lambda q, fs: {**chrome_of(q, fs), "d": False}),
    ("C2 容差从 2px 收到 0px ⇒ ⑦⑧ 那两档 1px 量化噪声会被当成坏（判据过敏；"
     "这一条只动 tol 这一个数，动的是尺子而不是数据。注：⑨ 不归它管——那一档响不响取决于"
     "「该给的宽度认不认侧栏」，那是 C8 的账，不是容差的账）",
     lambda q, fs: chrome_of(q, fs, tol=0)),
    ("C3 explains_small 恒 False ⇒ 凡「舞台不是整块屏」的那些档（①②④⑥⑦⑧⑨⑪⑫⑬）全翻红："
     "D 不再替画面挡 B 的账，舞台被 chrome 压着时居中偏小也会被记给内核",
     lambda q, fs: {**chrome_of(q, fs), "explains_small": False}),
    ("C4 rows_lost 拿宽当高算 ⇒ 除 ③⑩（舞台就是整块屏、本来一行都没让）外其余各档的 rows_lost 全站不住",
     lambda q, fs: {**chrome_of(q, fs), "rows_lost": q["screen_qt"][0] - q["video"][0]}),
    ("C5 恒等注入（真尺子原样交回去）⇒ 读数必须逐档相同（＝判成 NOP 而不是假称抓到）；"
     "它是证伪器自己的正向对照：哪天 case 表不再消费这些键，它会当场自曝",
     lambda q, fs: chrome_of(q, fs)),
    ("C6 浮层模式当不存在（照 R20 那版无条件把醒着的条算进该给的高度）⇒ ⑪ 那条"
     "「条不住在布局里、舞台却仍被压矮」会被尺子自己解释掉（＝审查点名的结构失明复活），"
     "⑩⑪ 的 by_bar 也跟着翻面",
     lambda q, fs: {**chrome_of(q, fs),
                    "by_bar": (q["bar_h"] if q["bar_vis"] else 0),
                    "h_short": (q["video"][1] < q["screen_qt"][1]
                                 - (q["bar_h"] if q["bar_vis"] else 0)
                                 - (q["title_h"] if q["title_vis"] else 0) - 2)}),
    ("C7 full_stage 恒 False ⇒ ③⑩ 翻红：「舞台＝整块屏」是浮层修完之后唯一那条正向证据，"
     "它一瞎，修好与没修好读起来一模一样",
     lambda q, fs: {**chrome_of(q, fs), "full_stage": False}),
    ("C8 侧栏挤宽当不存在（by_sidebar 恒 0、该给的宽度不按侧栏收）⇒ 只有 ⑨ 翻红："
     "上下留黑那一半边的账没人记了",
     lambda q, fs: {**chrome_of(q, fs), "by_sidebar": 0,
                    "w_short": q["video"][0] < q["screen_qt"][0] - 2}),
    ("C9 overlay_off 恒 False ⇒ 只有 ⑫ 翻红：设置里明明开着浮层、条却还占着布局行"
     "（ChromeHost 没接手）这一档就抓不到了",
     lambda q, fs: {**chrome_of(q, fs), "overlay_off": False}),
)


def selfcheck_chrome_mutate() -> int:
    """几何尺的证伪：逐条注坏，要求②⑦⑧这类档点名翻红；恒等那条必须判 NOP。

    结局四档与像素尺那边同一口径：CAUGHT／MISSED／NOP／CRASH，任何非期望的都算 rc=1。
    C5 是**反向哨兵**——如果哪天 case 表被改成不看这些键了，恒等注入也会"抓到"，那立刻暴露。
    """
    base_rc, base_res = selfcheck_chrome(say_it=False)
    say(f"---- 几何尺的证伪：{len(_CHROME_MUTATIONS)} 条，逐条按期望要求自检翻红 ----")
    say(f"  基线（真尺子）：错 {base_res.count(False)} 档 rc={base_rc}")
    if base_rc != 0:
        say("[FINAL] R20 几何尺证伪：基线就没绿，证伪不作数 ⇒ rc=1")
        return 1
    bad = 0
    for name, make in _CHROME_MUTATIONS:
        expect_nop = name.startswith("C5")
        crashed, res = "", base_res
        try:
            _, res = selfcheck_chrome(make, say_it=False)
        except BaseException as exc:
            crashed = f"{type(exc).__name__}: {str(exc)[:150]}"
        if crashed:
            bad += 1
            say(f"  [CRASH] {name}\n        结局＝自检崩了：{crashed}——崩不算抓到")
            continue
        if res == base_res:
            if expect_nop:
                say(f"  [对照 PASS] {name}\n        结局＝恒等注入读数逐档相同（这正是它该做的："
                    "它不翻红，才说明翻红是坏尺子带来的、不是注入这个动作带来的）")
            else:
                bad += 1
                say(f"  [NOP] {name}\n        结局＝读数与基线逐档相同 ⇒ 这条注入是空操作，尺子不消费它")
            continue
        if expect_nop:
            bad += 1
            say(f"  [MISSED] {name}\n        结局＝恒等注入却改了读数 ⇒ 这把尺不纯（同样的进该有同样的出），"
                f"红在 {[i + 1 for i, ok in enumerate(res) if not ok]} 档")
            continue
        if all(base_res) and any(not r for r in res):
            say(f"  [CAUGHT] {name}\n        结局＝翻红 {[i + 1 for i, ok in enumerate(res) if not ok]} 档")
        else:
            bad += 1
            say(f"  [MISSED] {name}\n        结局＝读数变了但没按声明翻红（红 {[i + 1 for i, ok in enumerate(res) if not ok]}）")
    say(f"[FINAL] R20 几何尺证伪：共 {len(_CHROME_MUTATIONS)} 条，判不到的 {bad} 条 ⇒ rc={1 if bad else 0}")
    return 1 if bad else 0


# ---------------------------------------------------------------- 自检的证伪（注入坏尺子）
# 一把从没被证明"会翻红"的自检，绿的读数不值钱：下面每条把一个静默 bug 注进尺子里，
# 要求自检当场点名。判据是**逐条 PASS/FAIL 列表与基线不同**（＝这条注入真起了作用），
# 不是"退出码非零"——编码崩、断言崩都能造非零，却证明不了哪条抓到了。
def _bad_shape_full(box, w, h):        # 形状尺失灵：不管量到什么都说"铺满"
    return "铺满"


def _bad_shape_eps0(box, w, h):        # 贴边阈值调到 0：**只要不是四边严丝合缝就报「不规则留黑」**
    # 这一条故意不走 `shape_of` 的 eps 口子：那道口子只能改容差，改不出"除了铺满什么都不认"这一坏形。
    # （试过写成 `shape_of(box, w, h, eps=1)`：那样它只翻 ⑧，与 M10 撞成一类，M2 声明的
    # 「正常档 ⑥ 会被读成不规则留黑」也就不成立了——注入坏的东西必须和它声明的那句话对得上。）
    if box is None:
        return "无内容"
    l, t, r, b = box
    edge = [v < 1 for v in (l, t, r, b)]
    if all(edge):
        return "铺满"
    return "不规则留黑"


def _bad_shape_eps_old(box, w, h):     # 容差退回**旧的 1%**：真机两格的读数抖动会翻成「不规则留黑」
    return shape_of(box, w, h, eps=max(2, int(0.01 * min(w, h))))


def _bad_shape_eps_loose(box, w, h):   # 容差一路松到 25% 宽：一成宽的黑框也被判成「铺满」
    return shape_of(box, w, h, eps=int(0.25 * w))


def _bad_bbox_none(img, **kw):         # 内缩尺失灵：一律「量不到」
    return None


def _bad_bbox_transposed(img, **kw):   # 行列扫反了：把 (左,上,右,下) 报成 (上,左,下,右)
    box = ink_bbox(img)
    return None if box is None else (box[1], box[0], box[3], box[2])


def _bad_shape_merge_ac(box, w, h) -> str:   # A 的形状被并进 B：贴左上 ⇒ 报成居中偏小
    sh = shape_of(box, w, h)
    return "居中偏小" if sh == "贴左上" else sh


def _bad_bbox_abs(img, **kw):          # 退回旧的绝对亮度阈：主题底色 28 会越过 thresh=24
    return _bbox_by_test(img, 64, lambda r, g, b, bg: np.maximum(np.maximum(r, g), b) > 24)


def _bad_bbox_sampled(img, **kw):      # 退回 2026-10-20 03:35 之前那版：缩到 240 宽、每格一个采样点
    return _bbox_sampled(img, 64, _diff_mask)


def _bad_inkmap_const(img, quiet=True):   # 格子尺失灵：恒报「外圈不黑、中心有内容」
    return False, True


def _bad_inkmap_ring_only(img, quiet=True):  # 只量外圈，中心那半截瞎了 ⇒ C 会被当成 B
    ring, _ = ink_map(img, quiet=True)
    return ring, True


# 注：`fns` 的槽位顺序与 `selfcheck` 里那行解包**必须是**（格子尺, 内缩尺, 形状尺）；
# 第一版把坏形状尺插进了第 0 槽，结果是注入靠 TypeError 侥幸"抓到"——那不算抓到，
# 所以 selfcheck 现在先断言槽位数，注错位置当场自曝。
MUTATIONS = (
    ("M1 形状尺恒报「铺满」⇒ ②③⑤⑥⑦ 全该翻红（不然 A/B/C 的分诊就是它编的）",
     lambda fns: (fns[0], fns[1], _bad_shape_full), "red"),
    ("M2 形状尺的贴边阈值从 1% 收到 <1px ⇒ 正常档（⑥ 上下留黑）会被读成不规则留黑",
     lambda fns: (fns[0], fns[1], _bad_shape_eps0), "red"),
    ("M3 内缩尺恒返 None ⇒ 带内缩期望的 ①②⑤⑥⑦ 该翻红",
     lambda fns: (fns[0], _bad_bbox_none, fns[2]), "red"),
    ("M4 格子尺恒报「外圈不黑／中心有内容」⇒ ②③ 该翻红（这一条挡的就是「什么都量不到也算绿」）",
     lambda fns: (_bad_inkmap_const, fns[1], fns[2]), "red"),
    ("M5 格子尺只量外圈、中心那半瞎 ⇒ ③（C）会被当成 B，该翻红",
     lambda fns: (_bad_inkmap_ring_only, fns[1], fns[2]), "red"),
    ("M6 内缩尺行列扫反（上/左 互换、下/右 互换）⇒ ②⑤⑥⑦ 的内缩数值与形状都该站不住",
     lambda fns: (fns[0], _bad_bbox_transposed, fns[2]), "red"),
    ("M7 内缩尺退回旧的绝对亮度阈（thresh=24）⇒ ⑦（主题底色 #17181C，最大通道 28）必须翻红——"
     "这把尺曾经就瞎在这一档：留一圈「不是纯黑的黑」会被读成铺满",
     lambda fns: (fns[0], _bad_bbox_abs, fns[2]), "red"),
    ("M8 形状尺把「贴左上」并进「居中偏小」⇒ ⑤ 该翻红（这一条最贴本趟的风险：A 被当成 B 就是修反）",
     lambda fns: (fns[0], fns[1], _bad_shape_merge_ac), "red"),
    ("M9 槽位注错（把坏形状尺插进格子尺那一槽）⇒ 期望 selfcheck 的槽位断言当场自曝，"
     "而不是靠 TypeError 侥幸收工",
     lambda fns: (_bad_shape_full, fns[1]), "assert"),
    ("M10 贴边容差退回旧的 1%（＝2026-10-20 之前那一版）⇒ ⑧ 必须翻红："
     "这一条钉的是『口径变准』这件事本身——新容差不是白放宽的，它救回了真机那一拍",
     lambda fns: (fns[0], fns[1], _bad_shape_eps_old), "red"),
    ("M11 贴边容差一路松到 25% 宽 ⇒ ⑨（一成黑框＝真·B 的大小）必须翻红："
     "这一条是 M10 的反向哨兵，挡的是『借着 ⑧ 把守卫拆了』",
     lambda fns: (fns[0], fns[1], _bad_shape_eps_loose), "red"),
    ("M12 内缩尺退回 240 宽单像素缩样（2026-10-20 03:35 之前那一版）⇒ ⑩ 必须翻红："
     "这一条钉的是『逐像素问』这件事本身——re2 右缘那几列稀薄的内容会被整格漏掉，"
     "正常的「上下留黑」又会被读成「不规则留黑」",
     lambda fns: (fns[0], _bad_bbox_sampled, fns[2]), "red"),
)


def selfcheck_mutate() -> int:
    """把上面的坏尺子逐条注进去，要求自检翻红**并且**读数与基线不同。

    四类结局分开记：CAUGHT（按这条声明的期望翻红）／MISSED（读数变了但自检仍全绿＝注入白做）／
    NOP（读数与基线逐张相同＝尺子根本不消费这条，判据是假的）／
    CRASH（自检抛了——真尺子的注入**不该**抛，抛了就说明这条注入打坏的是已知答案本身，
    它证明不了判据；只有 expect="assert" 那条（专门测槽位断言）允许以 AssertionError 收工）。
    任何一条 MISSED／NOP／CRASH（非期望）⇒ rc=1。

    有一条注入试过之后**从这份清单里删掉了**，记在这儿免得下一位再试一遍：
    「内缩尺去掉缩样（逐像素问原图）」在 1200×700 上跑了 2.08s，读数与基线**逐张相同**——
    它只改耗时不改正确性，自检判不到它，所以它不是判据能管的事（耗时由主程序的拍次自己负责）。
    """
    base_rc, base_res = selfcheck(say_it=False)
    say(f"---- 自检的证伪：{len(MUTATIONS)} 条坏尺子，逐条按声明的期望要求自检翻红 ----")
    say(f"  基线（真尺子）：错 {base_res.count(False)} 张 rc={base_rc}"
        f"（基线本身不绿的话，下面的『翻红』就没有对照，先修基线）")
    if base_rc != 0:
        say("[FINAL] R20 自检证伪：基线就没绿，证伪不作数 ⇒ rc=1")
        return 1
    bad = 0
    for name, make, expect in MUTATIONS:
        fns = (ink_map, ink_bbox, shape_of)
        patched = make(fns)
        t0 = time.monotonic()
        crashed: str = ""
        rc = 0
        res = base_res
        try:
            rc, res = selfcheck(patched, say_it=False)
        except BaseException as exc:
            crashed = f"{type(exc).__name__}: {str(exc)[:150]}"
        secs = time.monotonic() - t0
        if expect == "assert":
            if crashed.startswith("AssertionError") and "三槽" in crashed:
                say(f"  [CAUGHT] {name}")
                say(f"        结局＝槽位断言当场自曝（{secs:.2f}s）：{crashed}")
            else:
                bad += 1
                say(f"  [MISSED] {name}")
                say(f"        结局＝期望 AssertionError 点名「三槽」，实际拿到 "
                    f"{'崩：' + crashed if crashed else f'rc={rc} 红 {res.count(False)} 张'}（{secs:.2f}s）")
            continue
        if crashed:
            bad += 1
            say(f"  [CRASH] {name}")
            say(f"        结局＝自检崩了（{secs:.2f}s）：{crashed}"
                f"——崩不算抓到：这条注入坏的是判据要读的东西，不是尺子读数")
            continue
        if res == base_res:
            bad += 1
            say(f"  [NOP] {name}")
            say(f"        结局＝读数与基线逐张相同（{secs:.2f}s）⇒ 这条注入是空操作，尺子不消费它")
        elif rc == 0:
            bad += 1
            say(f"  [MISSED] {name}")
            say(f"        结局＝读数变了但自检仍全绿（{secs:.2f}s）⇒ 坏尺子能瞒过自检")
        else:
            say(f"  [CAUGHT] {name}")
            say(f"        结局＝翻红 {res.count(False)}/{len(res)} 张，红在 "
                f"{[i + 1 for i, ok in enumerate(res) if not ok]}（{secs:.2f}s）")
    say(f"[FINAL] R20 自检证伪：共 {len(MUTATIONS)} 条，判不到的（MISSED/NOP/CRASH）{bad} 条 ⇒ rc={1 if bad else 0}")
    return 1 if bad else 0



# ---------------------------------------------------------------- 正题
def _qapp():
    """按**产品的装配**起 QApplication，不是按测试自己的习惯。

    这条是 2026-10-20 补的，因为它一度让整本读数的量纲是错的：产品在 `app/main.py:74
    _enable_high_dpi()` 里开了 `AA_EnableHighDpiScaling`（150% 缩放机上 dpr=1.5），
    而这把尺原来直接 `QApplication(...)`，dpr 恒 1.00——于是量到的"控制条让行 76px／
    标题栏+条让行 120px"全是用户真实值（114／180）的三分之二，`test_ui.py` 那两行
    （2654、4715）却是调了 `_enable_high_dpi()` 的。**同一件事在两处各写一遍、其中一处
    忘了跟上**，就是这个分裂的机制。现在探针直接调产品那个函数：将来产品改了缩放策略，
    这把尺跟着变，不用有人记得回来改这里。
    属性必须在构造**之前**设；已存在实例（同进程里别的趟先起过）时它是空操作，
    所以下面把实际 dpr 印出来——不印的话"装配不合"就只会变成一串读不懂的数。
    """
    app = TU.QApplication.instance()
    if app is None:
        from app.main import _enable_high_dpi
        _enable_high_dpi()
        app = TU.QApplication(sys.argv[:1])
    TU.APP = app
    say(f"[装配] dpr={app.primaryScreen().devicePixelRatio():.2f} "
        f"逻辑屏={app.primaryScreen().geometry().width()}×{app.primaryScreen().geometry().height()} "
        f"（产品 `_enable_high_dpi()` 已生效；这里若是 1.00 而用户机是 150%，那这本账的量纲就不对）")
    return app


def recheck() -> int:
    """复量已经落盘的抓图（只读 PNG，不占前台）：换尺子之后把旧读数逐张重算一遍。

    为什么要这一支：2026-10-20 03:35 那一趟唯一的红（re2 右内缩 64px ⇒「不规则留黑」）
    是**旧尺**读出来的。改成逐像素之后，同一张 PNG 必须当场改判成「上下留黑」，否则
    "修了尺子"就只是空口。只认命令行给的通配符，不默认扫 tests/out——那里还混着窗口态、
    杠杆拍与自检图，默认扫会把不相干的读数也端上来。
    """
    import glob
    pats = [a for a in sys.argv[1:] if not a.startswith("--")]
    files = [f for pat in pats for f in sorted(glob.glob(pat))]
    if not files:
        say("[FINAL] 复量：没给 PNG（用法：--recheck \"tests/out/r20-*.png\"）⇒ rc=1")
        return 1
    say(f"---- 复量留档图（逐像素内缩尺；只读 PNG）{len(files)} 张 ----")
    bad = 0
    for f in files:
        img = QImage(f)
        if img.isNull():
            say(f"  [SKIP] {os.path.basename(f)}：读不出来")
            bad += 1
            continue
        box = ink_bbox(img)
        sh = shape_of(box, img.width(), img.height())
        say(f"  {os.path.basename(f):32s} {img.width()}×{img.height()} 内缩={box} 形状={sh}")
    say(f"[FINAL] 复量：{len(files)} 张，读不出 {bad} 张 ⇒ rc={1 if bad else 0}")
    return 1 if bad else 0


def main() -> int:
    if RECHECK:
        return recheck()
    if MUTATE:
        _qapp()
        a = selfcheck_mutate()
        say("")
        b = selfcheck_chrome_mutate()
        return max(a, b)
    if SELFCHK:
        _qapp()
        rc1, _ = selfcheck()
        rc2, _ = selfcheck_chrome()
        return max(rc1, rc2)

    # 本仓库没有 git，"这一拍量的是哪一版产品"只能靠盘上字节指纹说话（量具自检见
    # tests/build_stamp.py --selfcheck / --selfcheck-mutate）。印在最前面：看门狗是排队起趟的，
    # 等的这段时间我还在改产品代码——不印的话，事后连"该不该重跑"都无从判。
    import build_stamp
    say(build_stamp.line())

    # 链锁：别的常跑链（test_ui 那一串）持锁时并跑，读到的就不是被测者。
    # 这一趟由看门狗挑好人离开的时刻才起，所以这里等一会儿而不是直接走人——
    # 等到点还没收就照样跑，但把"并跑风险"写进留档，读数按存疑读（占前台的机会不多，别浪费）。
    waited = 0.0
    while os.path.exists(LOCK) and waited < LOCK_TIMEOUT:
        say(f"[等待] 链锁还在（{os.path.basename(LOCK)}）：再等 60s（已等 {waited:.0f}s / 上限 {LOCK_TIMEOUT:.0f}s）")
        time.sleep(60.0)
        waited += 60.0
    if os.path.exists(LOCK):
        say(f"[风险] 等满 {LOCK_TIMEOUT:.0f}s 链锁仍没收（{LOCK}）——照跑，但本趟读数按"
            "「可能与别的趟并跑」存疑读，别拿它单独下结论。")

    # 抢前台之前先让尺子自证：这一趟只有一次机会（用户就在旁边），
    # 一把校准都没过的像素尺量出来的 A/B/C 不值得拿人去换。
    app = _qapp()
    from app.ui import theme
    app.setStyleSheet(theme.qss())
    sc_rc, _ = selfcheck(say_it=True)
    if sc_rc != 0:
        say("[SKIP] 像素尺自检没过：这把尺量不到 A/B/C，不去占前台。")
        say("[FINAL] R20 全屏几何探针：没跑——量具自检翻红 ⇒ rc=1")
        return 1
    ch_rc, _ = selfcheck_chrome(say_it=True)
    if ch_rc != 0:
        say("[SKIP] 几何尺自检没过：这把尺量不到 D（舞台被谁压扁），不去占前台。")
        say("[FINAL] R20 全屏几何探针：没跑——几何尺自检翻红 ⇒ rc=1")
        return 1
    files = [os.path.join(ROOT, "data", n) for n in ("config.json", "watch-state.json")]
    snap0 = [fingerprint(f) for f in files]
    say("[闸门] 用户档案（开跑前）：" + " ／ ".join(snap0))

    ok, why = TU.capture_health()
    if not ok:
        say(f"[SKIP] 屏不出像素，抢前台也没意义：{why}")
        say("[FINAL] R20 全屏几何探针：没跑——环境不给屏幕 ⇒ rc=3")
        return 3


    path = argv_file() or TU.FILES[0]
    if not os.path.exists(path):
        say(f"[SKIP] 要放的片不在盘上：{path}")
        say("[FINAL] R20 全屏几何探针：没跑——片不在 ⇒ rc=3")
        return 3
    say(f"[片] {path}")

    cfg = TU.make_cfg()
    cfg["mute"] = True                       # 启动参数里就静音（--mute=yes）
    win = TU.build_window(cfg)
    win.add_paths([path])
    if not TU.wait_for(lambda: float(win.player.prop("duration", 0) or 0) > 0, 30.0):
        say("[SKIP] 内核没起来或这条片没读进时长，量不到东西。")
        say("[FINAL] R20 全屏几何探针：没跑——内核没起来 ⇒ rc=3")
        win.close()
        return 3
    TU.pump(0.8)
    say(f"[静音回读] mute={win.player.get('mute', '<无>')} "
        f"volume={win.player.get('volume', '<无>')}（从这一台实例现读，不是配置里的期望值）")
    # 侧栏**保持出厂那一档（关）**：用户这一幕就是"双击开片→点全屏"，中间没开过面板。
    # 开着侧栏量出来的"画面比屏幕窄"是侧栏占的宽度，不是这一趟要分诊的东西——
    # 它的读数写在 qt_block 的 `sidebar vis=…` 里，需要时另开一趟单独判。
    say(f"[前置] 侧栏={win.sidebar.isVisible()} 播放={win.player.running}")

    # 前置闸：**先等窗口态真的画出一帧**（为什么必须有这一道见 wait_frame 的注释——第 1 趟
    # 就是缺它，把"还没出帧的黑"量成了 C）。等不到就 seek 到 60s 再等一轮：片头整段是黑的
    # 片子确实存在，而 seek 之后仍什么都量不到，那就是内核没在放或这块没在画——
    # 像素尺没有对象，这一趟不作数（rc=3），既不叫"复现"也不叫"没复现"。
    frame, wf = wait_frame(win, 60.0, "视频区·等第一帧")
    say(f"[等帧] 窗口态第一帧等了 {wf:.1f}s ⇒ 形状={frame['shape']} 内缩={frame['box']}")
    if frame["shape"] in ("无内容", "无图"):
        say("[等帧] 60s 没出帧 ⇒ seek 到 60s 再等一轮（片头可能整段是黑的）")
        win.player.seek_abs(60.0)
        frame, wf_seek = wait_frame(win, 60.0, "视频区·seek 后等帧")
        say(f"[等帧] seek 后又等了 {wf_seek:.1f}s ⇒ 形状={frame['shape']} 内缩={frame['box']}")
    if frame["shape"] in ("无内容", "无图"):
        say("[SKIP] 窗口态量不到任何一帧：像素尺没有对象，进全屏也只会量到一片黑。")
        say("[FINAL] R20 全屏几何探针：没跑——窗口态没出帧 ⇒ rc=3")
        win.close()
        return 3

    refit_log = arm_refit_recorder(win.video)

    say("\n---- 拍 0：全屏前（窗口态，作为后面每一拍的对照）----")
    base_q = qt_block(win, win.video)
    base = native_block(win.video)
    base_kernel = kernel_block(win)
    base_pix = pixels_of(win, win.video, "视频区·窗口态", save=png("00-windowed"))

    say("\n---- 进全屏 ----")
    t_fs = time.monotonic()
    win.toggle_fullscreen(True)
    TU.pump(0.2)                       # mapToGlobal 要等布局落定一次才算得准
    park_cursor(win)
    t0 = time.monotonic()
    phases: list[dict] = []
    for k, dt in enumerate(PHASES, start=1):
        wait = t0 + dt - time.monotonic()
        if wait > 0:
            TU.pump(wait)
        say(f"\n---- 拍 {k}：全屏后 +{dt:.2f}s ----")
        q = qt_block(win, win.video)
        ph = native_block(win.video)
        ph["t"] = dt
        ph["label"] = f"+{dt:.2f}s"
        ph["qt"] = q
        ph["chrome"] = chrome_of(q, fs=True)
        ph["kernel"] = kernel_block(win)
        px = pixels_of(win, win.video, f"视频区·+{dt}s", save=png(f"{k:02d}-fs-{dt:.2f}s"))
        ph.update(px)
        phases.append(ph)

    say("\n---- 悬停唤醒：光标划过画面（用户按下「全屏」之后紧接着做的第一件事）----")
    wake = hover_wake(win)

    say("\n---- 杠杆：手动再 refit 一次，看画面会不会当场铺满 ----")
    n = win.video.refit()
    TU.pump(0.8)
    lever = native_block(win.video)
    lx = pixels_of(win, win.video, "视频区·杠杆后", save=png("99-lever"))
    say(f"  杠杆 refit() 铺了 {n} 个子窗口；铺满={lever['fills']}；形状={lx['shape']}"
        f"（像素可用={lx['usable']}）")

    say("\n---- 退出全屏 ----")
    win.toggle_fullscreen(False)
    TU.pump(1.0)
    back = native_block(win.video)
    back_fills = back["fills"]

    # 二次进全屏：用户那句"点完全屏，画面只在屏幕中间一块"也可能只在**退出来再进去**那一趟
    # 才现形——第一次是内核刚按新尺寸建交换链，第二次是拿旧的交换链按新尺寸重铺，两者修法不同。
    # 占前台的机会不多，能一趟量完的别留给下一趟（多花约 20s）。
    say("\n---- 二次进全屏（第一趟落定之后退出来、再进去）----")
    frame2, wf2 = wait_frame(win, 20.0, "视频区·二次前窗口态")
    say(f"  退出后窗口态重新出帧：等了 {wf2:.1f}s 形状={frame2['shape']} 内缩={frame2['box']}"
        f"；矩形「{back['detail']}」")
    again: list[dict] = []
    t_fs2 = time.monotonic()
    win.toggle_fullscreen(True)
    t2 = time.monotonic()
    for k, dt in enumerate(REENTRY, start=1):
        wait = t2 + dt - time.monotonic()
        if wait > 0:
            TU.pump(wait)
        say(f"\n---- 二次拍 {k}：再进全屏后 +{dt:.2f}s ----")
        q2 = qt_block(win, win.video)
        ph = native_block(win.video)
        ph["t"] = dt
        ph["label"] = f"二次+{dt:.2f}s"
        ph["qt"] = q2
        ph["chrome"] = chrome_of(q2, fs=True)
        ph["kernel"] = kernel_block(win)
        px = pixels_of(win, win.video, f"视频区·二次+{dt}s", save=png(f"re{k}-fs-{dt:.2f}s"))
        ph.update(px)
        again.append(ph)
    win.toggle_fullscreen(False)
    TU.pump(0.8)

    last = phases[-1]
    say("\n---- refit 的调用序列（t 相对进全屏那一刻；落定客户区="
        f"{last['client'][0]}×{last['client'][1]}）----")
    for r in refit_log:
        dtc = (r["client"][0] - last["client"][0], r["client"][1] - last["client"][1])
        say(f"  t={r['t'] - t_fs:+.3f}s{'（二次那一趟）' if r['t'] >= t_fs2 else ''} "
            f"当时客户区={r['client'][0]}×{r['client'][1]} "
            f"与落定差={dtc[0]}×{dtc[1]} 铺了 {r['n']} 个")
    # 只算**第一次**进全屏那一段的调用：二次进全屏的 refit 发生在同一本日志里，
    # 混进来会让"每一次都旧"这类整段判据被第二趟的落定尺寸稀释（那判的是另一件事）。
    after = [r for r in refit_log if t_fs <= r["t"] < t_fs2]
    # A 的两种形态分开报：**一次都没铺**（修法＝补一次落定后的 refit）与
    # **铺了但那一刻原生客户区还是旧尺寸**（同一修法能救，但说明 resizeEvent 的时机不可信，
    # 该把"尺寸对不上就重铺"做成常态检查而不是补一次定时）。
    a_no_call = not after
    a_stale_size = bool(after) and all(
        abs(r["client"][0] - last["client"][0]) > 4 or abs(r["client"][1] - last["client"][1]) > 4
        for r in after)
    say(f"  进全屏之后的 refit 调用={len(after)} 次；一次都没有={a_no_call}；"
        f"每一次采到的客户区都比落定值旧（差>4px）={a_stale_size}")

    # 判决：只按盘上量到的数说话。三类各要各的证据，允许同时不给结论。
    # 两趟全屏（第一次进、退出来再进）的拍次一起进账，条目用 `label` 点名而不是秒数——
    # 两趟的 +3.0s 是两回事，拿秒数当键会把"二次那一趟"并进第一次的账里。
    shots = phases + again
    a_stale = [p["label"] for p in shots if p["kids"] and not p["fills"]]
    no_kids = [p["label"] for p in shots if not p["kids"]]
    # D（全屏时 chrome 占了布局行）走**几何尺**，不走像素尺：mpv 自己的信箱黑边和
    # 容器外那圈黑是同一种黑，像素尺把它们分不开（这把尺的已知答案在 selfcheck_chrome 里）。
    d_title = [p["label"] for p in shots if p["chrome"]["d"]]
    d_w = [p["label"] for p in shots if p["chrome"]["w_short"]]
    d_h = [p["label"] for p in shots if p["chrome"]["h_short"]]
    d_off = [p["label"] for p in shots if p["chrome"]["overlay_off"]]
    d_side = [p["label"] for p in shots if p["chrome"]["by_sidebar"] > 2]
    # 几何这一摊**只按落定之后**（t ≥ SETTLE_S）的拍子进账：+0/+0.15 那两拍布局还在动，
    # 拿它们下判决会把"过渡"读成"坏"（B 那边早就这么分了家，D 这边 R20 漏了——同一趟里两把
    # 尺分两种口径，回头必然互相打脸）。过渡拍照样打印，只是不进判决。
    settled = [p for p in shots if p["t"] >= SETTLE_S]
    d_geo = sorted({p["label"] for p in settled
                    if p["chrome"]["h_short"] or p["chrome"]["w_short"] or p["chrome"]["overlay_off"]})
    d_side_settled = sorted({p["label"] for p in settled if p["chrome"]["by_sidebar"] > 2})
    full_shots = sorted({p["label"] for p in settled if p["chrome"]["full_stage"]})
    hosted_shots = sorted({p["label"] for p in settled if p["chrome"]["bar_hosted"]})
    d_any = d_title + (["悬停唤醒"] if wake["chrome"]["d"] else [])
    BAD_SHAPE = ("居中偏小", "贴左上", "贴右下", "不规则留黑")
    # B 的账要分三摊再下判决（2026-10-19 第 4 趟：三趟都在 +0/+0.15/+0.6 报 B，
    # 而那一档自己会好——把过渡态和"被几何解释掉的"当成内核画小了，判据就在骗人）：
    #   过渡（t < SETTLE_S）自己会好 ⇒ 只报不闸；
    #   落定但舞台不是整块屏（D／条醒着）⇒ 像素尺判不了内核，记 D 或"另案"，不许记给 B；
    #   落定且舞台就是整块屏 ⇒ 才是真·B。
    b_trans: list[str] = []
    b_explained: list[str] = []
    b_small: list[str] = []
    for p in shots:
        if not (p["usable"] and p["kids"] and p["fills"] and p["shape"] in BAD_SHAPE):
            continue
        if p["t"] < SETTLE_S:
            b_trans.append(p["label"])
        elif p["chrome"]["explains_small"]:
            b_explained.append(p["label"])
        else:
            b_small.append(p["label"])
    b_all = b_trans + b_explained + b_small
    c_dark = [p["label"] for p in shots if p["usable"] and p["shape"] in ("无内容",)]
    ok_shape = [p["label"] for p in shots
                if p["usable"] and p["shape"] in ("铺满", "上下留黑", "左右留黑")]
    lever_fixes = bool(lever["fills"]) and lx["shape"] in ("铺满", "上下留黑", "左右留黑")
    say("\n---- 读数汇总 ----")
    say(f"  窗口态容器客户区={base['client'][0]}×{base['client'][1]} 形状={base_pix['shape']}；"
        f"最后一拍={last['client'][0]}×{last['client'][1]} 形状={last['shape']}（{last['detail']}）；"
        f"二次进之前窗口态形状={frame2['shape']}")
    say(f"  每拍形状（两趟全屏一起进账，标签带「二次」的是退出来再进去那一趟）="
        f"{[(p['label'], p['shape']) for p in shots]}")
    say(f"  A 子窗口未铺满的拍次={a_stale}")
    say(f"  容器里一个子窗口都没有的拍次={no_kids}（只报不闸：这一档出现时 A/B 的矩形尺与像素尺都无对象可量）")
    wc = wake["chrome"]
    say(f"  D 全屏时标题栏在场（＝R20 的复现判据）的拍次={d_title}；悬停唤醒那一拍="
        f"{wc['d']}（唤醒后舞台={wake['qt']['video'][0]}×{wake['qt']['video'][1]} "
        f"屏={wake['qt']['screen_qt'][0]}×{wake['qt']['screen_qt'][1]} dpr={wake['qt']['dpr']:.2f} "
        f"标题栏高={wake['qt']['title_h']} 条高={wake['qt']['bar_h']} 形状={wake['shape']}）")
    say(f"  D 舞台横向比屏窄的拍次={d_w} 纵向比「屏高减掉当场醒着的那几条」还矮的拍次={d_h}"
        f"（这两档出现时画面按高适配 ⇒ 左右各让出黑边＝用户说的「四周都是空的」）")
    sb_px = sorted({p["chrome"]["by_sidebar"] for p in shots if p["chrome"]["by_sidebar"] > 2})
    say(f"  D2 浮层没生效的拍次={d_off}（设置开着 `float_controls_in_fs`，条却还占着布局行）")
    say(f"  D3 全屏里侧栏还开着、把舞台挤窄一栏的拍次={d_side}（挤掉的宽度读数={sb_px}px）"
        f"⇒ 画面按**宽**适配 ⇒ 上下各让出一条黑边（＝左右留黑的镜像，R21 之前这把尺没记这一笔）")
    say(f"  落定（t≥{SETTLE_S}s）之后几何仍不干净的拍次={d_geo}／共 {len(settled)} 拍"
        f"（条住浮层的拍次={hosted_shots}；舞台＝整块屏的拍次={full_shots}）")
    say(f"  B 子窗口铺满、画面没铺满，且**落定之后几何解释不掉**的拍次={b_small}"
        f"（过渡 t<{SETTLE_S}s 的={b_trans} 只报不闸；落定但已被 D 解释掉的={b_explained} 记 D 不重复记 B）")
    say(f"  C 整块没内容的拍次={c_dark}（这一档也可能是片子自己黑场的那几秒——"
        f"窗口态那一拍有帧而它没帧才可疑，分家看留档图 r20-*.png，别只信这一行）")
    say(f"  形状正常（铺满／单边遮幅）的拍次={ok_shape}")
    say(f"  杠杆（手动补一次 refit）：铺满={lever['fills']} 形状={lx['shape']} ⇒ 补铺能修={lever_fixes}")
    say(f"  内核（窗口态→最后一拍）：dwidth {base_kernel.get('dwidth')}→{last['kernel'].get('dwidth')} "
        f"dheight {base_kernel.get('dheight')}→{last['kernel'].get('dheight')} "
        f"vo-dw {base_kernel.get('video-out-params/dw')}→{last['kernel'].get('video-out-params/dw')} "
        f"aspect {base_kernel.get('video-out-params/aspect')}→{last['kernel'].get('video-out-params/aspect')} "
        f"panscan {base_kernel.get('panscan')}→{last['kernel'].get('panscan')} "
        f"zoom {base_kernel.get('video-zoom')}→{last['kernel'].get('video-zoom')}")
    say(f"  退出全屏后再铺满={back_fills}（这条不成立就是另一个 bug，别混进上面的判决）")
    say("  注：本片自己的黑边已由 ffmpeg 量过（内容 2.40:1、上下各 13%、左右 0 px）——"
        "全屏时**本来就该有**上下黑边，所以形状尺把「上下留黑／左右留黑／铺满」都算正常，"
        "只有「居中偏小／贴左上／贴右下／不规则留黑」才进 B 的候选。"
        "但候选不等于账：舞台被 chrome 压扁时（D）画面也只能按高适配、四边都留黑，"
        "像素尺与真·B 同分，那一档的账记给 D（见 chrome_of 的注释）。")

    # 判决分成三档，并且**以字段的形式印进 [FINAL]**（`档位=复现／没复现／量不到`）。
    # 为什么非要一个字段：看门狗拿这一句话决定"要不要再占用户那片 11 分钟"，而它上一版是
    # 拿**话术**判的（`"没能复现" not in final`）。话术一改分流就反过来——本行的判决文字
    # 写的是「没复现：…」，不带「能」字，那一版看门狗会把好趟读成"量到缺陷"、直接跳过
    # 用户原盘那一趟。这是 [[feedback-self-written-detectors-need-controls]] 里
    # "文本匹配全绿但消费它的程序不认"那一类，同一个坑第二次不许再踩：判据要落在**字段**上。
    bucket = "未分类"                 # 兜底值：将来新加一档而忘了分类，这里会印「未分类」，
    # 看门狗对不认识的档位**拒收**（记成量具回执失灵），而不是拿话术猜一个方向——
    # 宁可这一趟不作数，也不许分流悄悄反了方向（那正是上一版的错法）。
    if a_stale:
        bucket = "复现"
        flavor = ("进全屏之后一次都没铺" if a_no_call else
                  ("铺了 " + str(len(after)) + " 次，但每一次采到的客户区都还是旧尺寸"
                   if a_stale_size else "铺过并且有一次量到了落定尺寸"))
        verdict = f"A：子窗口没跟着铺满（{flavor}）"
    elif d_any:
        bucket = "复现"
        verdict = ("D：全屏时标题栏回到布局里（拍次=" + "、".join(d_any) +
                   "）⇒ 舞台比整块屏矮一截，16:9 的画面只能按高适配、左右各让出黑边"
                   "＝用户那句「画面只在屏幕中间一块、四周都是空的」。"
                   "子窗口每次都跟容器严丝合缝，锅不在 refit，在占布局的那一行 chrome")
    elif d_off:
        bucket = "复现"
        verdict = ("D2：设置里开着 `float_controls_in_fs`，控制条却还是占着布局的一行（拍次=" +
                   "、".join(d_off) + "）⇒ ChromeHost 没接手。这一档**不是**用户选的退路"
                   "（退路是把键关掉，那时 overlay_off 不响），是浮层装配没生效")
    elif d_geo:
        bucket = "复现"
        det = "；".join(f"{p['label']}：让行 {p['chrome']['rows_lost']}px"
                        f"（条 {p['chrome']['by_bar']}／标题栏 {p['chrome']['by_title']}"
                        f"／侧栏 {p['chrome']['by_sidebar']}，条住在"
                        f"{'浮层' if p['chrome']['bar_hosted'] else '布局'}）"
                        for p in settled if p["label"] in d_geo)
        verdict = ("D2：落定之后舞台仍比「该给的」小（" + det +
                   "）⇒ 让掉的那几行没有一条是当场醒着的 chrome 该得的，"
                   "画面因此按高（或按宽）适配、另一边各让出黑边")
    elif d_side_settled:
        bucket = "复现"
        verdict = ("D3：全屏里侧栏没收起，把舞台挤窄一整栏（拍次=" + "、".join(d_side_settled) +
                   f"，宽度读数={sb_px}px）⇒ 画面按宽适配、上下各让出一条黑边。"
                   "探针自己从没开过侧栏；若用户在这之前按过 Ctrl+L/M/T 那是他显式开的，"
                   "看留档图分家，别直接判产品坏")
    elif b_small:
        bucket = "复现"
        verdict = (f"B：舞台已是整块屏、子窗口也铺满，落定后画面仍没铺满"
                   f"（形状={sorted({p['shape'] for p in shots if p['label'] in b_small})}）")
    elif c_dark:
        bucket = "复现"
        verdict = ("C：整块没内容——窗口态那一拍明明有帧（等帧闸过了才开跑），进全屏后这块却空了"
                   "；也可能是片子自己黑场，分家看留档图，另案")
    elif len(no_kids) == len(shots):
        bucket = "量不到"
        verdict = ("量不到：全屏每一拍容器里都没有子窗口——矩形尺与像素尺都没对象可量，"
                   "这不叫「没复现」，也不叫产品坏")
    elif not any(p["usable"] for p in shots):
        bucket = "量不到"
        verdict = ("量不到：全屏每一拍的像素都不可用（抓图失败/被遮挡），"
                   "形状尺等于没上过场——这不叫「没复现」")
    else:
        bucket = "没复现"
        verdict = ("没复现：两趟全屏每一拍加上悬停唤醒那一拍，标题栏都藏着、侧栏没挤宽、"
                   f"落定的 {len(settled)} 拍里舞台要么等于整块屏（浮层生效，这些拍={full_shots}）、"
                   "要么只让出当场醒着的那条控制条（退路模式），并且每一拍形状都在正常档 "
                   "⇒ 屏上量不到的东西不写进结论")
    snap1 = [fingerprint(f) for f in files]
    same = all(a == b for a, b in zip(snap0, snap1))
    # 字段与判决话术必须自洽：A/B/C/D* 这四族是"量到了缺陷"，判决行不许以「量不到：」「没复现：」
    # 开头；后两族反过来。这一条是给**将来**改文案的人设的闸——把某档的 bucket 改错、或把话术
    # 换个开头，这里会当场印 `档位自洽=False`，看门狗见 False 就拒收（不猜方向）。
    # 用字段印出来而不是 assert：assert 崩了就没有 [FINAL] 行，"没回执"和"回执说不自洽"是两回事，
    # 后者才知道该查哪一行。
    bucket_ok = (bucket in ("复现", "没复现", "量不到")
                 and (bucket == "复现") == (not verdict.startswith(("量不到：", "没复现："))))
    say("[闸门] 用户档案（收尾）：" + " ／ ".join(snap1) + f" —— 与开跑前相同={same}")
    say(f"[FINAL] R20 全屏几何探针：{r20_bucket.emit(bucket, bucket_ok)}{verdict}；"
        f"拍次={len(shots)} 最后一拍像素可用={last['usable']} "
        f"形状={last['shape']} 最后一拍舞台={last['qt']['video']}／屏={last['qt']['screen_qt']} "
        f"标题栏在场={last['qt']['title_vis']} 条住浮层={last['qt']['bar_hosted']} "
        f"浮层矩形={last['qt']['host_geo']} 侧栏={last['qt']['sidebar_vis']}({last['qt']['sidebar_w']}px) "
        f"悬停唤醒把标题栏唤回来={wake['chrome']['d']} 落定后几何仍不干净的拍次={d_geo} "
        f"浮层没生效={d_off} 侧栏挤宽={d_side_settled} 舞台=整块屏的拍次={full_shots} "
        f"落定后的 B={b_small} 杠杆补铺能修={lever_fixes} 退出全屏再铺满={back_fills} "
        f"用户档案未改动={same} ⇒ rc=0")
    win.close()
    TU.pump(0.6)
    return 0


if __name__ == "__main__":
    code = 1
    log_path = os.path.join(OUT_DIR,
                            "probe-fullscreen-fit-"
                            + ("selfcheck-" if SELFCHK else "mutate-" if MUTATE
                               else "recheck-" if RECHECK else "")
                            + time.strftime("%H%M") + ".log")
    try:
        code = main()
    except BaseException as exc:
        say(f"[ERROR] 量具自身异常：{type(exc).__name__}: {exc}")
        import traceback
        say(traceback.format_exc())
        code = 1
    finally:
        try:
            restore_cursor()                     # 探针动过真光标就必须还回去，崩了也要还
        except BaseException as exc:
            say(f"[WARN] 光标没能放回：{type(exc).__name__}: {exc}")
        os.makedirs(OUT_DIR, exist_ok=True)
        with io.open(log_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(_rows) + "\n")
        print(f"留档：{log_path}", flush=True)
    raise SystemExit(code)
