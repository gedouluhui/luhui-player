# 探针 v5（一次性，不算判据）：这台 24H2 上"打开方式"那个列表的宿主是
#   C:WindowsSystem32OpenWith.exe（旁边还有 Windows.Internal.OpenWithHost_WinUI3.dll）
# 前两扇门（InvokeVerb("openas")、rundll32 OpenAs_RunDLL）实测都不弹窗口，
# SHOpenWithDialog 按名字拿到但一调就访问违例（见 probe-openwith-api 的输出）。
# 这一趟只问：直接把 OpenWith.exe 拿起来，会不会弹出一扇能读到列表的窗口。
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

BS = chr(92)
U32 = ctypes.windll.user32
U32.SetProcessDPIAware()
try:
    import psutil
except BaseException as exc:
    print("no psutil:", exc)
    psutil = None

TARGET = (sys.argv[1] if len(sys.argv) > 1 else
          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "tests", "samples", "gelu-sample-3.mkv"))
OW = os.path.join(os.environ.get("SystemRoot", "C:" + BS + "Windows"), "System32", "OpenWith.exe")


def wins():
    out = []
    keep = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, l):
        keep.append(h)
        return True

    U32.EnumWindows(cb, 0)
    for h in keep:
        try:
            if not U32.IsWindowVisible(h):
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
            out.append((int(h), buf.value, cls.value, int(pid.value),
                        (r.left, r.top, r.right, r.bottom)))
        except BaseException:
            continue
    return out


def procname(pid):
    if psutil is None:
        return "?"
    try:
        return psutil.Process(pid).name()
    except BaseException:
        return "?"


def dump_children(h):
    keep = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(c, l):
        keep.append(c)
        return True

    try:
        U32.EnumChildWindows(wt.HWND(h), cb, 0)
    except BaseException as exc:
        print("     EnumChildWindows 报:", exc)
    for c in keep:
        cls = ctypes.create_unicode_buffer(256)
        U32.GetClassNameW(c, cls, 255)
        n = U32.GetWindowTextLengthW(c)
        b = ctypes.create_unicode_buffer(n + 2)
        U32.GetWindowTextW(c, b, n + 1)
        print("     子窗 类=%s 文=%r" % (cls.value, b.value[:60]))


def main() -> int:
    print("target:", TARGET)
    print("OpenWith.exe 在不在:", os.path.isfile(OW), OW)
    if not os.path.isfile(OW):
        return 2
    before = {w[0] for w in wins()}
    pids_before = {p.info["pid"] for p in psutil.process_iter(["pid"]) if p.info["pid"]} if psutil else set()
    for args in ([TARGET], [ '"' + TARGET + '"' ]):
        print("---- 试 OpenWith.exe", args, "----")
        try:
            pr = subprocess.Popen([OW] + args)
        except BaseException as exc:
            print("   起不来:", type(exc).__name__, exc)
            continue
        got = None
        for i in range(10):
            time.sleep(1.0)
            fresh = [w for w in wins() if w[0] not in before]
            print("   第 %d 秒：新窗口 %d 扇" % (i + 1, len(fresh)))
            for h, nm, cls, pid, rc in fresh:
                print("     hwnd=%s 名=%r 类=%s 进程=%s(%d) 矩形=%s"
                      % (hex(h), nm[:50], cls, procname(pid), pid, rc))
            if fresh:
                got = fresh
                break
        try:
            pr.terminate()
        except BaseException:
            pass
        if got:
            for h, nm, cls, pid, rc in got:
                dump_children(h)
            print("命中：这扇门能用。留档完就关。")
            return 0
        print("   这组参数没弹窗口，进程状态:", pr.poll())
    print("两组参数都没弹窗口")
    return 1


if __name__ == "__main__":
    sys.exit(main())
