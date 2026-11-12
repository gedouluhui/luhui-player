# -*- coding: utf-8 -*-
"""只读量具（R20 补充）：把**用户此刻开着的那扇窗**的原生几何与 DPI 档位量下来，不建窗、不抢前台、不发输入。

为什么单独做这一趟：探针那一趟是自己建窗自己全屏，量到的是"我这趟的形状"；用户实测的失败
形状（画面居中、四周留黑、比例 ≈2.398:1 而整体只有正确尺寸的 ≈8/9）在探针的相位表里没出现过。
要分「A：mpv 子窗口矩形本身就小了」与「B：矩形铺满了但内核画小」，只差**子窗口矩形 vs 容器客户区**
这一个数——而他屏幕上现在就挂着那扇窗，读它的原生矩形不需要前台，也不需要等他离开机器。

纪律（都是这个仓库立过的规矩，写在这里免得下一位重新踩）：
  - **全程只读**：EnumWindows / EnumChildWindows / Get*Rect / Get*Client / GetDpiForWindow /
    GetWindowInfo / DpiAwareness 查询，一个 Move/SetFocus/SendMessage 都没有。
  - **不落像素**：抓屏（PrintWindow/grabWindow）会把窗口抬到最上面，这一趟明确不做。
    形状判决仍由 `probe_fullscreen_fit.py` 在"人离开机器"之后出。
  - 判据一律印**原始数值＋谁给的**，不做解释性总结；解释写在跑完之后。
  - 找不到目标窗不是失败：print `NO_TARGET` 后 rc=3 走人，绝不拿"上一轮的数"冒充这一轮。

读到的两把尺（对应两类修法）：
  ① 容器客户区物理尺寸（`GetClientRect` × DPI 换算）vs mpv 子窗口屏幕矩形 ⇒ 差多少 = A 的量级；
  ② 每扇窗的 DPI awareness（`GetWindowDpiAwarenessContext` + `GetAwarenessFromDpiAwarenessContext`）
     ——父窗 per-monitor-aware、子窗 system-aware 时，同一"1920×1080 逻辑"在 3840×2160/150% 的屏上
     会缩成不一样的物理尺寸，这是"等比小一圈"最省事的成因，必须当场量掉而不是猜。

跑法：  python tests/read_live_window_r20.py            # 只读，随时可跑
        python tests/read_live_window_r20.py --pid 22468  # 只报那个 PID 的窗
退出码：0 找到并量到了；3 没找到目标窗（或找不到 mpv 子窗，只报容器）；1 量具自身出错。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys

user32 = ctypes.windll.user32
shcore = ctypes.windll.shcore
k32 = ctypes.windll.kernel32

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
EnumChildWindows = user32.EnumChildWindows
GetWindowTextLengthW = user32.GetWindowTextLengthW
GetWindowTextW = user32.GetWindowTextW
GetClassNameW = user32.GetClassNameW
GetWindowRect = user32.GetWindowRect
GetClientRect = user32.GetClientRect
ClientToScreen = user32.ClientToScreen
IsWindowVisible = user32.IsWindowVisible
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
GetWindowLongW = user32.GetWindowLongW
GetParent = user32.GetParent

for f, a, r in ((EnumWindows, [EnumWindowsProc, wt.LPARAM], ctypes.c_bool),
                (GetWindowTextW, [wt.HWND, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
                (GetClassNameW, [wt.HWND, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
                (GetWindowRect, [wt.HWND, ctypes.POINTER(wt.RECT)], ctypes.c_bool),
                (GetClientRect, [wt.HWND, ctypes.POINTER(wt.RECT)], ctypes.c_bool),
                (ClientToScreen, [wt.HWND, ctypes.POINTER(wt.POINT)], ctypes.c_bool),
                (GetWindowTextLengthW, [wt.HWND], ctypes.c_int),
                (IsWindowVisible, [wt.HWND], ctypes.c_bool),
                (GetWindowThreadProcessId, [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
                (GetWindowLongW, [wt.HWND, ctypes.c_int], ctypes.c_long),
                (GetParent, [wt.HWND], wt.HWND),
                (EnumChildWindows, [wt.HWND, EnumWindowsProc, wt.LPARAM], ctypes.c_bool)):
    f.argtypes, f.restype = a, r

GWL_STYLE = -16
WS_CHILD = 0x40000000  # 只做"是不是子窗"的标注，不参与任何判决


def text(h, maxlen=2048) -> str:
    """窗口标题。缓冲区助手在 **ctypes** 上，不在 ctypes.wintypes 上（上一版就在这儿崩）。"""
    n = GetWindowTextLengthW(h)
    if n <= 0:
        return ""
    b = ctypes.create_unicode_buffer(min(maxlen, n + 2))
    GetWindowTextW(h, b, len(b))
    return b.value


def cls(h) -> str:
    b = ctypes.create_unicode_buffer(256)
    GetClassNameW(h, b, len(b))
    return b.value


def pid_of(h) -> int:
    p = wt.DWORD(0)
    GetWindowThreadProcessId(h, ctypes.byref(p))
    return p.value


def rects(h) -> tuple[str, str]:
    r = wt.RECT()
    s = f"{r.left},{r.top},{r.right},{r.bottom} {r.right - r.left}x{r.bottom - r.top}" \
        if GetWindowRect(h, ctypes.byref(r)) else "?"
    c = wt.RECT()
    cs = f"{c.right - c.left}x{c.bottom - c.top}" if GetClientRect(h, ctypes.byref(c)) else "?"
    return s, cs


def style(h) -> str:
    st = GetWindowLongW(h, GWL_STYLE) & 0xFFFFFFFF
    return f"0x{st:08X}" + (" WS_CHILD" if st & WS_CHILD else "")


# ---- DPI 两把尺：窗口自己的 awareness 档位 + 该档下的 DPI ----
# 档位值（DPI_AWARENESS 枚举）：0=unaware 1=system 2=per-monitor 3=per-monitor v2
_AWARE = {0: "UNAWARE", 1: "SYSTEM", 2: "PER_MONITOR", 3: "PER_MONITOR_V2",
          (-1): "查询失败（这扇窗的档位未知，不许当 unaware 用）"}
try:
    GetWindowDpiAwarenessContext = user32.GetWindowDpiAwarenessContext
    GetWindowDpiAwarenessContext.argtypes = [wt.HWND]
    GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
    GetAwarenessFromDpiAwarenessContext = user32.GetAwarenessFromDpiAwarenessContext
    GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    shcore.GetDpiForWindow.argtypes = [wt.HWND]
    shcore.GetDpiForWindow.restype = ctypes.c_uint
except AttributeError as exc:  # 老 Windows 上没这几个导出：点名而不是静默跳过
    GetWindowDpiAwarenessContext = None
    print(f"[DPI] 查询函数拿不到（{exc}）⇒ 这一趟的 DPI 那把尺不作数")


def dpi_info(h) -> str:
    if GetWindowDpiAwarenessContext is None:
        return "awareness=n/a"
    ctx = GetWindowDpiAwarenessContext(h)
    if not ctx:
        return "awareness=NULL_CTX"
    try:
        aw = GetAwarenessFromDpiAwarenessContext(ctx)
    except BaseException as exc:
        return f"awareness=ERR({type(exc).__name__})"
    d = shcore.GetDpiForWindow(h)
    return f"awareness={_AWARE.get(aw, f'未知档位{aw}')} dpi={d} scale={d / 96.0:.3f}"


def walk_children(top: wt.HWND, out: list) -> None:
    def cb(h, _l):
        s, cs = rects(h)
        out.append((h, cls(h), text(h, 200), s, cs, style(h), dpi_info(h)))
        return True
    EnumChildWindows(top, EnumWindowsProc(cb), 0)


def main(argv: list[str]) -> int:
    only_pid = 0
    for i, a in enumerate(argv):
        if a == "--pid" and i + 1 < len(argv):
            only_pid = int(argv[i + 1])

    # 进程级 DPI 不设：这一趟只读别人的窗，改自己进程的档位会影响读到的换算口径吗？
    # 不会——GetDpiForWindow/GetWindowRect 报的都是**被问那扇窗**的口径，与查询者无关。
    # 保持进程默认（unaware→系统缩放后仍拿到物理像素，Win10 1607+ 的 GetWindowRect 返回物理坐标）
    # 并在下面用 GetDpiForWindow 自己换算，避免任何"改了进程才量得准"的副作用。
    tops = []

    def cb(h, _l):
        if IsWindowVisible(h):
            tops.append(h)
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    print(f"[扫描] 可见顶层窗 {len(tops)} 扇（只读 EnumWindows，未枚举隐藏窗）")

    hits = []
    for h in tops:
        p = pid_of(h)
        if only_pid and p != only_pid:
            continue
        t = text(h)
        c = cls(h)
        # 目标认法：类名带 Qt（我们的外壳）或类名是 mpv，或标题里带片名/品牌
        if c.startswith("Qt") or "芦荟" in t or "gelu" in t.lower() or \
                any(k in t for k in ("Captain", "America", "复仇者", "内战")):
            hits.append(h)

    if not hits:
        print("NO_TARGET：当前没有LuHui的可见顶层窗（用户可能已关窗/最小化到隐藏）。"
              "这一趟不作数，绝不拿上一轮读数冒充。")
        return 3

    for h in hits:
        s, cs = rects(h)
        p = pid_of(h)
        print(f"\n[顶层窗] hwnd=0x{h:X} pid={p} class={cls(h)!r} title={text(h)!r}")
        print(f"  screen_rect={s} client={cs} style={style(h)} {dpi_info(h)}")
        kids = []
        walk_children(h, kids)
        print(f"  直接/递归子窗共 {len(kids)} 扇（EnumChildWindows 是递归的）")
        for k, c2, t2, s2, cs2, st2, d2 in kids:
            tag = ""
            if c2 == "Qt5152QWindowPipeWriter" or "mpv" in c2.lower():
                tag = "  <== 疑似内核子窗"
            print(f"    hwnd=0x{k:X} class={c2!r} title={t2!r}\n"
                  f"      screen_rect={s2} client={cs2} style={st2} {d2}{tag}")
        if not kids:
            print("    （无子窗：内核窗没挂上来，或这扇不是播放器主窗）")

    print("\n[读法提醒] 上面全是原生 Win32 数值，不经 Qt；"
          "A/B 的分界 = mpv 子窗 screen_rect 尺寸 vs 容器 client×scale。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"[量具失灵] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
