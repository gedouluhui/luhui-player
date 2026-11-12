# 探针 v4（一次性，不算判据）：这台 24H2 上 InvokeVerb("openas") 与
# rundll32 shell32.dll,OpenAs_RunDLL 都不弹任何窗口（见 probe-openwith-uia2/3 的留档：
# 16 次轮询零新窗口，而且 FolderItem.Verbs() 的清单里压根没有"打开方式"这一条）。
# 那就换最底层那扇门：shell32 导出的 SHOpenWithDialog(SHOPENASINFO)——它才是
# "打开方式"那个列表框的宿主 API。这一趟只问三件事：
#   ① 这个导出在不在；② 调用返回什么；③ 有没有真冒出一扇能读到列表的窗口。
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

BS = chr(92)
U32 = ctypes.windll.user32
K32 = ctypes.windll.kernel32
U32.SetProcessDPIAware()
U32.EnumWindows.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
U32.GetWindowTextLengthW.restype = ctypes.c_int
U32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
U32.IsWindowVisible.restype = ctypes.c_bool
U32.GetClassNameW.argtypes = [wt.HWND, ctypes.c_wchar_p, ctypes.c_int]
U32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]


class SHOPENASINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD),
                ("pcszObject", ctypes.c_wchar_p),
                ("pcszRegKey", ctypes.c_wchar_p),
                ("oaifInFlags", wt.DWORD)]


def wins():
    out = []
    cb = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(lambda h, l: out.append(h) or True)
    U32.EnumWindows(cb, 0)
    rows = []
    for h in out:
        if not h or not U32.IsWindowVisible(h):
            continue
        n = U32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 2)
        U32.GetWindowTextW(h, buf, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        U32.GetClassNameW(h, cls, 255)
        pid = wt.DWORD(0)
        U32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        r = wt.RECT()
        U32.GetWindowRect(h, ctypes.byref(r))
        try:
            import psutil
            pname = psutil.Process(int(pid.value)).name()
        except BaseException:
            pname = "?"
        rows.append((int(h), buf.value, cls.value, pname, (r.left, r.top, r.right, r.bottom)))
    return rows


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    target = sys.argv[1] if len(sys.argv) > 1 else \
        os.path.join(here, "samples", "gelu-sample-3.mkv")
    print("target:", target)
    s32 = ctypes.windll.shell32
    try:
        fn = s32.SHOpenWithDialog
        print("① 导出 SHOpenWithDialog：按名字拿到了", fn)
    except AttributeError as exc:
        print("① 导出 SHOpenWithDialog：按名字拿不到 ->", exc)
        fn = None
    if fn is None:
        try:
            fn = s32[169]
            print("① 序数 169 拿到了", fn)
        except BaseException as exc:
            print("① 序数 169 也拿不到 ->", exc)
            return 2
    before = {w[0] for w in wins()}
    info = SHOPENASINFO()
    info.cbSize = ctypes.sizeof(SHOPENASINFO)
    info.pcszObject = target
    info.pcszRegKey = None
    info.oaifInFlags = 0x00000001          # OAIF_ALLOW_REGISTRATION（不勾"始终"就什么都不会发生）
    fn.argtypes = [wt.HWND, ctypes.POINTER(SHOPENASINFO)]
    fn.restype = ctypes.c_long

    import threading

    def call():
        hr = fn(None, ctypes.byref(info))
        print("② SHOpenWithDialog 返回 HRESULT = 0x%08x" % (hr & 0xFFFFFFFF))

    t = threading.Thread(target=call, daemon=True)     # 它是模态的，必须另开线程
    t.start()
    for i in range(12):
        time.sleep(1.0)
        fresh = [w for w in wins() if w[0] not in before]
        print("---- 第 %d 秒：新窗口 %d 扇 ----" % (i + 1, len(fresh)))
        for h, nm, cls, pn, rc in fresh:
            print("     hwnd=%s 名=%r 类=%s 进程=%s 矩形=%s" % (hex(h), nm[:60], cls, pn, rc))
        if fresh:
            # 抄一下这扇窗里有哪些子窗口（经典对话框是 #32770 + SysListView32）
            kids = []
            cb2 = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)(
                lambda h2, l: kids.append(h2) or True)
            U32.EnumChildWindows(fresh[0][0], cb2, 0)
            for h2 in kids:
                cls2 = ctypes.create_unicode_buffer(256)
                U32.GetClassNameW(h2, cls2, 255)
                n2 = U32.GetWindowTextLengthW(h2)
                b2 = ctypes.create_unicode_buffer(n2 + 2)
                U32.GetWindowTextW(h2, b2, n2 + 1)
                print("       子窗 类=%s 文=%r" % (cls2.value, b2.value[:40]))
            break
    print("（这扇窗留着不关，探针不算判据；下一步人工/脚本 ESC）")
    t.join(timeout=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
