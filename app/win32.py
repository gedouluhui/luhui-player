"""Win32 辅助层：非客户区改造、DWM 圆角/亚克力、光标与按键状态、焦点、休眠抑制。

只做 Windows 平台的事，不认识 UI 概念；坐标一律物理像素，由调用方负责换算。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import msvcrt
import os
import sys

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
kernel32 = ctypes.windll.kernel32

# ---------- 消息与命中测试常量 ----------
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_GETMINMAXINFO = 0x0024
WM_WINDOWPOSCHANGED = 0x0047
WM_DPICHANGED = 0x02E0
WM_MOUSEWHEEL = 0x020A
WM_LBUTTONDOWN = 0x0201
WM_SETFOCUS = 0x0005
WM_SYSCOMMAND = 0x0112
# Qt startSystemMove 用的就是 SC_MOVE|HTCAPTION（0xF012）：进系统原生移动循环，
# 贴边吸附、跨屏、最大化拽下还原都由系统接管，不靠 DefWindowProc 的隐式标题栏拖动。
SC_DRAGMOVE = 0xF012

HTCLIENT = 1
HTCAPTION = 2
HTSYSMENU = 3
HTMINBUTTON = 8
HTMAXBUTTON = 9
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17

# ---------- 窗口样式 ----------
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
WS_MAXIMIZE = 0x01000000
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOREDIRECTIONBITMAP = 0x00200000

SM_CXSIZEFRAME = 32
SM_CYSIZEFRAME = 33
SM_CXPADDEDBORDER = 92

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
SWP_NOACTIVATE = 0x0010

# ---------- Z 序 ----------
# SetWindowPos 的 hWndInsertAfter 用这两个**伪句柄**（-1 / -2）：注意它们不是真 HWND，
# 传成 HWND 时要显式包一层 c_void_p(-1)，别让 ctypes 按 unsigned 截成 0xFFFFFFFF。
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
WS_EX_TOPMOST = 0x00000008

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02

# ---------- DWM ----------
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_MICA_EFFECT = 102  # Win11 22H2 早期值，后备用
DWMWCP_DEFAULT = 0
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
DWMWCP_ROUNDSMALL = 3
DWMSBT_NONE = 1
DWMSBT_AUTO = 2
DWMSBT_MAINWINDOW = 3  # Mica
DWMSBT_TRANSIENTWINDOW = 4  # Acrylic
DWMSBT_TABBEDWINDOW = 5  # Tabbed


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]


class NCCALCSIZE_PARAMS(ctypes.Structure):
    _fields_ = [("rgrc", RECT * 3), ("lppos", ctypes.c_void_p)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", wt.DWORD)]


POINT = wt.POINT
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.SetFocus.argtypes = [wt.HWND]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetFocus.restype = wt.HWND
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.MoveWindow.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              wt.BOOL]
user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.MonitorFromWindow.restype = wt.HMONITOR
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
user32.ReleaseCapture.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL


def is_win11() -> bool:
    v = sys.getwindowsversion()
    return v.major >= 10 and v.build >= 22000


def get_style(hwnd: int) -> int:
    return int(user32.GetWindowLongPtrW(wt.HWND(hwnd), GWL_STYLE)) & 0xFFFFFFFF


def add_frame_styles(hwnd: int) -> None:
    """保留可缩放/可快照的原生框架样式，配合 WM_NCCALCSIZE 使用。"""
    h = wt.HWND(hwnd)
    style = int(user32.GetWindowLongPtrW(h, GWL_STYLE))
    want = style | WS_THICKFRAME | WS_CAPTION | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
    if want != style:
        # 直接传 int：ctypes.wintypes 里**没有** LONG_PTR（那是 Windows SDK 的类型名），
        # SetWindowLongPtrW 的 argtypes 已经声明为 c_void_p，ctypes 会把整数转过去。
        user32.SetWindowLongPtrW(h, GWL_STYLE, want)
        user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE)


def border_width(hwnd: int) -> int:
    """缩放边框 + 补边框的宽度（物理像素），最大化时需要据此内缩。"""
    h = wt.HWND(hwnd)
    dpi = user32.GetDpiForWindow(h) if hasattr(user32, "GetDpiForWindow") else 96
    frame = user32.GetSystemMetrics(SM_CXSIZEFRAME)
    pad = user32.GetSystemMetrics(SM_CXPADDEDBORDER)
    if dpi != 96:
        frame = int(frame * dpi / 96)
        pad = int(pad * dpi / 96)
    return frame + pad


def is_maximized(hwnd: int) -> bool:
    return bool(get_style(hwnd) & WS_MAXIMIZE)


def set_topmost(hwnd: int, on: bool) -> bool:
    """窗口置顶 / 取消，直接改 Z 序。

    为什么不用 Qt 的 `Qt.WindowStaysOnTopHint`：`setWindowFlags()` 会让 Qt **销毁并重建**
    顶层 HWND，而我们用 `--wid` 把 mpv 的渲染窗口挂在这个 HWND 下面——换句柄之后那个
    子窗口就成了孤儿（画面黑掉、怎么调都不回来）。SetWindowPos 只动 Z 序，句柄不变。

    SWP_NOACTIVATE：切换置顶不许抢焦点。用户可能正在别的窗口里打字，按下快捷键那一瞬
    把前台拽过来是最讨人厌的一种"贴心"。
    """
    if not hwnd:
        return False
    after = ctypes.c_void_p(HWND_TOPMOST if on else HWND_NOTOPMOST)
    return bool(user32.SetWindowPos(wt.HWND(hwnd), after, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE))


def is_topmost(hwnd: int) -> bool:
    """当前是不是置顶态（读扩展样式位，不靠记忆配置值）。"""
    if not hwnd:
        return False
    ex = int(user32.GetWindowLongPtrW(wt.HWND(hwnd), GWL_EXSTYLE))
    return bool(ex & WS_EX_TOPMOST)




def set_corner_rounded(hwnd: int, round_corners: bool = True) -> None:
    if not is_win11():
        return
    pref = wt.DWORD(DWMWCP_ROUND if round_corners else DWMWCP_DONOTROUND)
    dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
                                 ctypes.byref(pref), ctypes.sizeof(pref))


def _colorref(color: str) -> int:
    """#RRGGBB → Windows COLORREF（0x00BBGGRR）。Qt 的十六进制顺序正好反着。"""
    s = str(color).strip().lstrip("#")
    if len(s) != 6:
        return 0
    r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return (b << 16) | (g << 8) | r


def dark_caption(hwnd: int, caption: str, text: str, border: str) -> bool:
    """把原生标题栏染成深色。

    标准对话框（设置、快捷键、工坊）保留系统标题栏是为了拖动/贴边/无障碍都照旧；
    但系统默认按"浅色应用"画那条 caption，深色播放器里弹出来时顶上一整条白，
    会立刻变成"两套软件"。Win11 直接给 caption/border/text 三个颜色；Win10 只开
    immersive dark 开关，标题栏随系统深色主题走。

    返回是否至少有一条属性被接受：全失败也不该妨碍对话框出现，所以调用方不拿它当错误。
    """
    if not hwnd:
        return False
    h = wt.HWND(int(hwnd))
    ok = False
    if is_win11():
        for attr, color in ((DWMWA_CAPTION_COLOR, caption),
                            (DWMWA_BORDER_COLOR, border),
                            (DWMWA_TEXT_COLOR, text)):
            val = wt.DWORD(_colorref(color))
            try:
                hr = dwmapi.DwmSetWindowAttribute(h, attr, ctypes.byref(val), ctypes.sizeof(val))
            except OSError:
                continue
            ok = ok or hr == 0
    val = wt.DWORD(1)
    for attr in (DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
        try:
            hr = dwmapi.DwmSetWindowAttribute(h, attr, ctypes.byref(val), ctypes.sizeof(val))
        except OSError:
            continue
        if hr == 0:
            ok = True
            break
    set_corner_rounded(hwnd, True)
    return ok


def set_acrylic(hwnd: int, enable: bool) -> bool:
    """尽力开启亚克力背景；不支持时返回 False，调用方改用不透明底色。"""
    if not is_win11():
        return _blur_behind(hwnd, enable)
    kind = wt.DWORD(DWMSBT_TRANSIENTWINDOW if enable else DWMSBT_NONE)
    hr = dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), DWMWA_SYSTEMBACKDROP_TYPE,
                                      ctypes.byref(kind), ctypes.sizeof(kind))
    if hr != 0:  # 22H2 前用 MICA 属性试一次
        kind = wt.DWORD(DWMSBT_MAINWINDOW if enable else DWMSBT_NONE)
        hr = dwmapi.DwmSetWindowAttribute(wt.HWND(hwnd), DWMWA_MICA_EFFECT,
                                          ctypes.byref(kind), ctypes.sizeof(kind))
        return hr == 0 and enable
    extend_glass(hwnd)
    return enable


def extend_glass(hwnd: int) -> None:
    """把 DWM 合成分区扩展到整个客户区，背景材质才会露出来。"""
    m = MARGINS(-1, -1, -1, -1)
    dwmapi.DwmExtendFrameIntoClientArea(wt.HWND(hwnd), ctypes.byref(m))


class _BLURRECT(ctypes.Structure):
    _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]


class _BLURBEHIND(ctypes.Structure):
    _fields_ = [("fEnable", wt.BOOL), ("r", _BLURRECT), ("bTransitionOnMaximized", wt.BOOL)]


def _blur_behind(hwnd: int, enable: bool) -> bool:
    """Win10 的低配材质替代方案（DwmEnableBlurBehindWindow）。"""
    try:
        rect = wt.RECT()
        user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect))
        info = _BLURBEHIND()
        info.fEnable = wt.BOOL(1 if enable else 0)
        info.r = _BLURRECT(0, 0, max(1, rect.right - rect.left), max(1, rect.bottom - rect.top))
        hr = dwmapi.DwmEnableBlurBehindWindow(wt.HWND(hwnd), ctypes.byref(info))
        return hr == 0 and enable
    except Exception:
        return False


def cursor_pos() -> tuple[int, int]:
    p = POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def client_size(hwnd: int) -> tuple[int, int]:
    r = wt.RECT()
    user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(r))
    return int(r.right - r.left), int(r.bottom - r.top)


def direct_children(hwnd: int) -> list[int]:
    """只取**直接**子窗口（EnumChildWindows 是递归的，会连孙窗口一起给你）。"""
    out: list[int] = []
    h = user32.GetWindow(wt.HWND(hwnd), 5)  # GW_CHILD（GW_HWNDFIRST 是 0，别混）
    while h:
        out.append(int(h))
        h = user32.GetWindow(h, 2)  # GW_HWNDNEXT
    return out


def hwnd_at_point(x: int, y: int) -> int:
    """光标那一点上**最深的窗口**（物理像素坐标）。0＝没查到（桌面空白处或 API 不答应）。

    用在"这一下到底点的是画面还是浮层"上，见 `ancestor_chain` 那段。
    restype 必须显式设成 HWND：不设时 ctypes 按 **int** 截断返回值，64 位下句柄高位被削，
    于是同一个窗口两次问回来两个数——这类"看着能跑、比对恒假"的坑在 GDI/USER 句柄上反复出现过。
    """
    user32.WindowFromPoint.restype = wt.HWND
    user32.WindowFromPoint.argtypes = [POINT]
    return int(user32.WindowFromPoint(POINT(int(x), int(y))) or 0)


def parent_hwnd(hwnd: int) -> int:
    """上一层：子窗口给父、**被拥有的顶层窗口给主人**、顶层无主则 0。

    为什么用 GetParent 而不是 GetAncestor(GA_PARENT)：后者对没有父的顶层窗口发的是
    桌面句柄，链会一路走到 desktop 而判不出"谁是主人"；GetParent 的文档口径正好是
    "parent **or owner**"，浮层（Qt.Tool + 有主人）在这一点上行为是我们需要的：
    它的链是 [主人窗口 HWND]，**不含**视频容器的 HWND。
    """
    user32.GetParent.restype = wt.HWND
    user32.GetParent.argtypes = [wt.HWND]
    return int(user32.GetParent(wt.HWND(int(hwnd or 0))) or 0)


def ancestor_chain(hwnd: int, limit: int = 12) -> list[int]:
    """从给定窗口往上走的 owner/parent 链（不含自己）。"""
    out: list[int] = []
    cur = int(hwnd or 0)
    for _ in range(limit):
        if not cur:
            break
        p = parent_hwnd(cur)
        if not p or p == cur or p in out:
            break           # 环/到头：宁可不往下走，也别在这里死循环（30ms 一次的槽里）
        out.append(p)
        cur = p
    return out


def move_window(hwnd: int, x: int, y: int, w: int, h: int, repaint: bool = True) -> None:
    user32.MoveWindow(wt.HWND(hwnd), int(x), int(y), int(w), int(h), wt.BOOL(1 if repaint else 0))


def begin_system_move(hwnd: int) -> bool:
    """进系统原生移动循环。照 Qt `QWindow.startSystemMove` 的口径：
    ReleaseCapture + PostMessage(WM_SYSCOMMAND, SC_DRAGMOVE)。

    为什么不交给 DefWindowProc 的隐式 HTCAPTION 拖动：本窗是 WS_POPUP 无边框、又把
    WM_NCCALCSIZE 的客户区扩到整窗，那条隐式路在这套样式下实测不起作用（标题栏命中
    正确却拖不动）。自己进系统循环后，贴边吸附/跨屏/最大化拽下还原仍由系统接管。
    """
    if not hwnd:
        return False
    user32.ReleaseCapture()
    return bool(user32.PostMessageW(wt.HWND(int(hwnd)), WM_SYSCOMMAND, SC_DRAGMOVE, 0))


def fit_child_windows(hwnd: int) -> int:
    """把 mpv 建在我们容器里的子窗口铺满客户区。

    --wid 嵌入的窗口**不会**随父窗口自动改变大小（Qt 只 MoveWindow 自己的 HWND，
    不管原生子窗口），所以父容器每次 resize 都要手动铺一次。返回铺了几个。
    """
    w, h = client_size(hwnd)
    kids = direct_children(hwnd)
    for k in kids:
        move_window(k, 0, 0, max(1, w), max(1, h))
    return len(kids)


def first_child(hwnd: int) -> int:
    """第一个直接子窗口（0＝没有）。只当"上次铺过哪一个"的锚点用。"""
    h = user32.GetWindow(wt.HWND(int(hwnd or 0)), 5)
    return int(h or 0)


def child_fills_client(hwnd: int, child: int) -> bool:
    """子窗口是否**正好**盖住父客户区（原点对原点、宽高相等）。

    为什么要多问这一句：孩子可以被**外力**改小而不惊动 Qt（mpv 重建渲染窗、调试工具、
    别的嵌入器）——父容器尺寸没变 ⇒ B6 的几何键没变 ⇒ `refit()` 会跳过，画面于是永久
    缩在一角，没有任何事件能把它救回来。2026-10-22 的 U60 证伪真抓到过这个形状（容器
    1728×1256、孩子 864×628，走产品 refit 复原不了）。问不到／孩子已销毁一律算"没铺满"：
    多铺一遍的代价是一次 MoveWindow，比留一条回不来的窄画面便宜得多。

    句柄的 restype 必须显式给：默认按 int 截断，64 位下高位一削，同一个窗口两次问回来
    两个数（见 `hwnd_at_point` 那段）。
    """
    try:
        if not hwnd or not child:
            return False
        user32.IsWindow.restype = wt.BOOL
        user32.IsWindow.argtypes = [wt.HWND]
        if not user32.IsWindow(wt.HWND(int(child))):
            return False
        # 第二参用 c_void_p：本模块里 `window_rect` 传的是自定义 `RECT`、`_blur_behind`
        # 传的是 `wt.RECT`，两者布局相同但类型不同；把 argtypes 钉成其中一种，另一种就会在
        # 运行时抛 `expected LP_RECT ...`（2026-10-22 实发：这条一旦跑过，后面的 window_rect
        # 全崩）。c_void_p 两种都收。
        user32.GetWindowRect.restype = wt.BOOL
        user32.GetWindowRect.argtypes = [wt.HWND, ctypes.c_void_p]
        r = wt.RECT()
        if not user32.GetWindowRect(wt.HWND(int(child)), ctypes.byref(r)):
            return False
        user32.ClientToScreen.restype = wt.BOOL
        user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(POINT)]
        origin = POINT(0, 0)
        if not user32.ClientToScreen(wt.HWND(int(hwnd)), ctypes.byref(origin)):
            return False
        cw, ch = client_size(int(hwnd))
        return (int(r.left) == int(origin.x) and int(r.top) == int(origin.y)
                and int(r.right - r.left) == cw and int(r.bottom - r.top) == ch)
    except Exception:
        return False


GWLP_HWNDPARENT = -8


def set_owner(hwnd: int, owner_hwnd: int) -> None:
    """设拥有关系：浮层永远压在主人窗口之上，也随主人窗口一起最小化。"""
    user32.SetWindowLongPtrW(wt.HWND(hwnd), GWLP_HWNDPARENT, wt.HWND(owner_hwnd))


def screen_dpi(hwnd: int) -> int:
    if hasattr(user32, "GetDpiForWindow"):
        d = int(user32.GetDpiForWindow(wt.HWND(hwnd)))
        if d:
            return d
    return 96


DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4   # 伪句柄，不是真 HANDLE


def enable_per_monitor_v2() -> bool:
    """把本进程的 DPI 感知升到 PerMonitorV2；必须在建任何窗口之前调。

    为什么清单之外还要这一手：打包版的 exe 带 `app.manifest`（PerMonitorV2），
    但开发态走的是 pythonw.exe，Python 自带的清单里没有 dpiAware——不补这一句，
    150% 屏上 Qt 拿到的是 unaware 虚拟化尺寸，字号与原生子窗口一起发糊。
    清单已经声明过时 API 会失败（ERROR_ACCESS_DENIED）；那表示进程已经是要的
    口径，不当错误，继续尝试下一档只是兜底旧系统。
    """
    if is_per_monitor_v2():
        return True
    try:
        user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return True
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        if shcore.SetProcessDpiAwareness(2) == 0:      # PER_MONITOR_DPI_AWARE（v1）
            return True
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware.restype = wt.BOOL
        return bool(user32.SetProcessDPIAware())       # Vista 兜底：system aware
    except Exception:
        return False


def dpi_awareness() -> int:
    """当前进程档位：0=unaware 1=system 2=per-monitor；查不到按 0。"""
    try:
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.restype = wt.INT
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        ctx = user32.GetThreadDpiAwarenessContext()
        return int(user32.GetAwarenessFromDpiAwarenessContext(ctypes.c_void_p(ctx)))
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long
        shcore.GetProcessDpiAwareness.argtypes = [ctypes.c_void_p,
                                                  ctypes.POINTER(ctypes.c_int)]
        val = ctypes.c_int(-1)
        if shcore.GetProcessDpiAwareness(None, ctypes.byref(val)) == 0:
            return int(val.value)
    except Exception:
        pass
    return 0


def is_per_monitor_v2() -> bool:
    """当前线程上下文是否 PMv2（Win10 1703 以下查不到 → False）。"""
    try:
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.AreDpiAwarenessContextsEqual.restype = wt.BOOL
        user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ctx = user32.GetThreadDpiAwarenessContext()
        return bool(user32.AreDpiAwarenessContextsEqual(
            ctypes.c_void_p(ctx),
            ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))
    except Exception:
        return False


def remove_frame_styles(hwnd: int) -> None:
    """全屏时摘掉原生框架位（否则 DWM 还会给它圆角与阴影）。"""
    h = wt.HWND(hwnd)
    style = int(user32.GetWindowLongPtrW(h, GWL_STYLE))
    drop = style & ~(WS_THICKFRAME | WS_CAPTION | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
    if drop != style:
        user32.SetWindowLongPtrW(h, GWL_STYLE, drop)   # 同上：没有 wt.LONG_PTR，传 int 即可
        user32.SetWindowPos(h, 0, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED | SWP_NOACTIVATE)


def lbutton_down() -> bool:
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def rbutton_down() -> bool:
    return bool(user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000)


def focus_window(hwnd: int) -> None:
    user32.SetFocus(wt.HWND(hwnd))


def focus_hwnd() -> int:
    """当前真正持有键盘焦点的 HWND（0 表示不在本进程或没有）。"""
    return int(user32.GetFocus() or 0)


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def monitor_workarea(hwnd: int) -> tuple[int, int, int, int]:
    """当前显示器的工作区（排除任务栏），用于全屏。"""
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    mon = user32.MonitorFromWindow(wt.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcWork
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def monitor_fullarea(hwnd: int) -> tuple[int, int, int, int]:
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    mon = user32.MonitorFromWindow(wt.HWND(hwnd), 2)
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcMonitor
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


# ---------- 休眠/屏保抑制（播放期间） ----------
ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(active: bool) -> None:
    try:
        flags = ES_CONTINUOUS | (ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED if active else 0)
        kernel32.SetThreadExecutionState(wt.DWORD(flags))
    except Exception:
        pass


# ---------- 子进程生命周期：Job Object ----------
# `poolkit._CHILDREN` 只在**正常退出**那条路上杀得到孩子。主进程被 taskkill /F 或
# Qt C++ 崩溃带走时，收尾代码一行都跑不到：mpv 继续放音、ffmpeg 继续写盘，用户看到
# "窗口关了声音还在"。Job 带 JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE，内核在最后一个
# Job 句柄关闭时（= 本进程死亡，句柄被系统收走）终止 Job 内所有进程——不依赖我们
# 执行任何 Python。句柄故意做成模块级且从不关闭：它的寿命就该等于本进程的寿命。
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_INFO_CLASS_EXTENDED = 9
_PROCESS_ALL_ACCESS = 0x001F0FFF


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wt.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wt.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wt.DWORD),
        ("SchedulingClass", wt.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_JOB_HANDLE: int | None = None


def _ensure_job() -> int | None:
    global _JOB_HANDLE
    if _JOB_HANDLE:
        return _JOB_HANDLE
    if sys.platform != "win32":
        return None
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    kernel32.SetInformationJobObject.restype = wt.BOOL
    kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                 ctypes.c_void_p, wt.DWORD]
    if not kernel32.SetInformationJobObject(job, _JOB_INFO_CLASS_EXTENDED,
                                            ctypes.byref(info), ctypes.sizeof(info)):
        kernel32.CloseHandle(job)
        return None
    _JOB_HANDLE = job
    return job


def job_handle() -> int | None:
    """本进程的 Job 句柄（验收脚本问用；None = 还没建/建失败）。"""
    return _ensure_job()


def assign_to_kill_job(pid_or_handle: int, is_pid: bool = True) -> bool:
    """把一个进程放进本进程的 kill-on-close Job；失败返回 False，绝不抛。

    调用点在每次 `Popen` 返回后立刻执行——窗口只有"刚起来"那一瞬，等它跑起来
    再放就晚了。已退出/句柄拿不到都只是 False（孩子已经没了，没有要收的东西）。
    """
    if sys.platform != "win32":
        return False
    try:
        job = _ensure_job()
        if not job:
            return False
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        kernel32.AssignProcessToJobObject.restype = wt.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        h = (kernel32.OpenProcess(_PROCESS_ALL_ACCESS, False, int(pid_or_handle))
             if is_pid else pid_or_handle)
        if not h:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job, h))
        finally:
            if is_pid:
                kernel32.CloseHandle(h)
    except Exception:
        return False


# ---------- 文件句柄：允许别人改名/删除 ----------
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
GENERIC_WRITE = 0x40000000
OPEN_ALWAYS = 4
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, ctypes.c_void_p]


def open_append_share_delete(path: str):
    """以追加写打开文件，但允许别的进程/线程改名、删除它。

    为什么不用内置 `open`：Windows 上 CRT 的共享模式只有 READ|WRITE，不含
    FILE_SHARE_DELETE——句柄开着时 `os.replace` 一律 ERROR_SHARING_VIOLATION。
    crash.log 的轮转正是被 faulthandler 的常开句柄这样卡死的（实测：连写 6 次
    一次都没轮转）。这里把删除共享放出来，日志句柄才不会和轮转互相锁死。
    """
    h = kernel32.CreateFileW(path, GENERIC_WRITE,
                             FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                             None, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if not h or h == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateFileW 失败 GetLastError={kernel32.GetLastError()}")
    try:
        fd = msvcrt.open_osfhandle(h, os.O_APPEND | os.O_WRONLY)
    except Exception:
        kernel32.CloseHandle(h)
        raise
    return os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace")
