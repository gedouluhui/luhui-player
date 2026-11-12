"""真机端到端冒烟：像用户那样启动 app/main.py，看屏幕上真的有没有画面。

和 test_ui.py 的分工：那份是"测试进程自己装配 MainWindow"，这份走**用户入口**
（python -m app.main 文件...），因此额外覆盖到单实例、startup_checks、配置落盘、
关窗后内核清理这些只有真启动才会跑的代码。

用法：python tests/smoke_launch.py [样片...]        # 带文件启动，走"双击视频文件"那条路
      python tests/smoke_launch.py --pythonw      # 空启动 + pythonw，走"双击桌面快捷方式"那条路
退出码 0=全过；1=有判据失败；3=屏幕此刻抠不出像素（显示器休眠/锁屏），像素那几条是
**跳过**而不是通过，把屏幕唤醒重跑才算验过。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
WM_CLOSE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# 认"这颗 mpv 是不是我们家那颗"用**这条绝对路径**，不用可执行文件名：
# 本机装着别的 mpv 也一样能认出谁是谁，而且这是按路径精确识别、绝不按镜像名全杀的那条规矩。
# （从前这里是硬写的 "lumen-player" 目录名片段，改目录名/改品牌都会让它静默失明。）
MPV_MARK = os.path.join(ROOT, "runtime", "mpv", "mpv.exe").lower()

from PyQt5.QtCore import QRect  # noqa: E402
from PyQt5.QtGui import QColor, QImage  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.config import APP_TITLE  # noqa: E402

OUT = os.path.join(HERE, "out")
DEFAULT_FILES = [os.path.join(HERE, "samples", f"gelu-sample-{i}.mp4") for i in (1, 2)]

results: list[tuple[str, bool, str]] = []
skipped: list[str] = []        # 屏幕抠不出像素时，像素类判据进这里（不算 PASS 也不算 FAIL）
BLIND = False                  # 显示器休眠/锁屏：整屏连任务栏都是 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def pix_check(name: str, ok: bool, detail: str = "") -> bool:
    """像素类判据的统一入口：环境不给屏幕时明说"跳过"，不拿一张全黑图去判产品失败。

    实测踩过：机器休眠以后整屏（含任务栏）抠出来全是 0，这三条一起 FAIL，看日志像
    "启动后窗口是黑的"。把环境算成功能，比把功能算成环境更难查。
    """
    if BLIND:
        skipped.append(name)
        print(f"[ENV] {name} 跳过 —— 屏幕此刻抠不出像素：{detail}", flush=True)
        return False
    return check(name, ok, detail)


# ---------------- Win32 小工具 ----------------
def enum_toplevels() -> list[int]:
    out: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            out.append(int(hwnd))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return out


def window_pid(hwnd: int) -> int:
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(wt.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(max(1, n + 1))
    user32.GetWindowTextW(wt.HWND(hwnd), buf, len(buf))
    return buf.value


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def mpv_paths() -> list[tuple[int, str]]:
    """按**可执行文件全路径**列出在跑的 mpv（不按镜像名猜，避免误伤别的 mpv 进程）。"""
    found: list[tuple[int, str]] = []
    TH32CS_SNAPPROCESS = 0x2
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    pe = PE32()
    pe.dwSize = ctypes.sizeof(PE32)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        if pe.szExeFile.lower() == "mpv.exe":
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pe.th32ProcessID)
            if h:
                size = wt.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    found.append((int(pe.th32ProcessID), buf.value))
                kernel32.CloseHandle(h)
        ok = kernel32.Process32NextW(snap, ctypes.byref(pe))
    kernel32.CloseHandle(snap)
    return found


def our_mpv() -> list[int]:
    return [pid for pid, path in mpv_paths() if MPV_MARK in path.lower()]


# ---------------- 像素判读 ----------------
def stats(img: QImage, nx: int = 48, ny: int = 27) -> dict:
    if img.isNull() or img.width() < 4 or img.height() < 4:
        return {"mean": 0.0, "std": 0.0, "distinct": 0, "min": 0.0, "max": 0.0}
    img = img.convertToFormat(QImage.Format_ARGB32)
    w, h = img.width(), img.height()
    buckets: set[tuple[int, int, int]] = set()
    lum: list[float] = []
    for j in range(ny):
        y = min(h - 1, int((j + 0.5) * h / ny))
        for i in range(nx):
            x = min(w - 1, int((i + 0.5) * w / nx))
            c = img.pixelColor(x, y)
            buckets.add((c.red() // 12, c.green() // 12, c.blue() // 12))
            lum.append(0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue())
    mean = sum(lum) / len(lum)
    var = sum((v - mean) ** 2 for v in lum) / len(lum)
    return {"mean": mean, "std": math.sqrt(var), "distinct": len(buckets),
            "min": min(lum), "max": max(lum)}


def main() -> int:
    global BLIND
    # --pythonw：按**快捷方式那条命令行**起（pythonw -m app.main），验的是"双击图标
    # 有没有反应、会不会弹一个黑色控制台"。不带参数=用户从图标进入的空启动路径。
    use_pythonw = "--pythonw" in sys.argv
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    launcher = [os.path.join(os.path.dirname(sys.executable), "pythonw.exe")] if use_pythonw \
        else [sys.executable]

    files = [a for a in argv if os.path.exists(a)] or ([] if use_pythonw else DEFAULT_FILES)
    os.makedirs(OUT, exist_ok=True)
    for p in files:
        if not os.path.exists(p):
            print(f"缺少样片 {p}，先跑 python tests/gen_sample.py")
            return 2

    before = set(our_mpv())
    print(f"启动前本项目的 mpv 进程：{sorted(before) or '无'}")

    # 预检：已经有一个播放器窗口在跑（上一轮跑到一半崩了、或用户自己开着）时，
    # 单实例会把这里新起的进程直接转交掉并退出，"开不出窗口"就成了假失败。
    # 只报告、不代关——那不是我的窗口。
    stale = [h for h in enum_toplevels() if window_title(h) == APP_TITLE]
    if stale:
        print(f"!! 已有一个「{APP_TITLE}」窗口在跑（hwnd={stale[0]} pid={window_pid(stale[0])}）。")
        print("   单实例会把它当成已有实例，本轮结果不可信：请先关掉那个窗口再跑。")
        return 3

    # 子进程默认按系统 ANSI（本机 GBK）写 stderr，traceback 里的中文路径到我这儿全是乱码，
    # 所以显式要 UTF-8；否则报错信息可读性为 0。
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}

    proc = subprocess.Popen(
        [*launcher, "-m", "app.main", *files], cwd=ROOT, env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    if use_pythonw:
        print(f"用 pythonw 空启动（等价于双击桌面快捷方式）：{launcher[0]}")

    hwnd = 0
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        for h in enum_toplevels():
            try:
                if window_pid(h) == proc.pid and window_title(h):
                    hwnd = h
                    break
            except Exception:
                continue
        if hwnd:
            break
        time.sleep(0.15)

    if proc.poll() is not None:
        out = proc.communicate(timeout=5)[0] or ""
        print("!! 入口进程提前退出：", out[:1500])
        check("python -m app.main 能起来并开出窗口", False,
              f"rc={proc.returncode} 输出前 200 字：{out[:200]!r}")
        return report()
    check("python -m app.main 能起来并开出窗口", bool(hwnd),
          f"hwnd={hwnd} 标题={window_title(hwnd)!r}")

    app = QApplication.instance() or QApplication(sys.argv[:1])
    scr = app.primaryScreen()
    origin = scr.geometry().topLeft()

    # 环境闸门：整屏（含任务栏那一条）抠出来只有一种颜色 = 屏幕此刻不给像素。
    # 这一步必须在任何像素判据之前，否则显示器休眠会被写成"启动后窗口是黑的"。
    full = scr.grabWindow(0).toImage()
    st_full = stats(full, 60, 34)
    BLIND = st_full["distinct"] < 2 or st_full["max"] <= 8
    print(f"[{'ENV' if BLIND else 'PASS'}] 抓屏通路可用（整屏 {full.width()}×{full.height()} "
          f"亮度{st_full['mean']:.0f} 颜色{st_full['distinct']}）"
          + ("  —— 屏幕此刻抠不出像素：显示器休眠/锁屏，像素类判据稍后全部跳过，不算产品失败"
             if BLIND else ""), flush=True)

    def grab(x: int, y: int, w: int, h: int) -> QImage:
        shot = scr.grabWindow(0)
        dpr = scr.devicePixelRatio() or 1.0
        r = QRect(int((x - origin.x()) * dpr), int((y - origin.y()) * dpr),
                  max(1, int(w * dpr)), max(1, int(h * dpr)))
        r = r.intersected(shot.rect())
        return shot.copy(r).toImage()

    # 等窗口稳定（带样片时等内核起播——懒启动，add_paths 里才拉 mpv）
    time.sleep(4.0)
    l, t, r_, b = window_rect(hwnd)
    ww, wh = r_ - l, b - t
    whole = grab(l, t, ww, wh)
    p_all = os.path.join(OUT, "smoke-window.png")
    whole.save(p_all)
    st_all = stats(whole, 60, 34)

    def has_content(s: dict) -> bool:
        """判"窗口里有内容"：有明暗起伏（不是纯色底）且有亮墨（文字/图标画出来了）。
        不用"颜色分桶数"当主判据——空状态占位页本来就几乎是纯深底（实测分桶 9），
        拿播放态的阈值去卡它会误报；真正要防的是"两层都藏着、只剩窗口底色"那种整片死黑。"""
        return s["std"] >= 6.0 and s["distinct"] >= 8 and (s["max"] - s["min"]) >= 100

    # 对照组：纯色深底必须判"无内容"。少了这一步，上面那条判据可能是恒真的。
    flat = QImage(800, 500, QImage.Format_ARGB32)
    flat.fill(QColor(12, 13, 16))
    st_flat = stats(flat, 60, 34)
    check("像素判据能否掉「整窗只剩底色」（对照组：纯色图必须判无内容）",
          not has_content(st_flat), f"纯色图 std={st_flat['std']:.1f} 分桶{st_flat['distinct']} "
                                    f"跨度{st_flat['max'] - st_flat['min']:.0f}")

    pix_check("窗口在屏幕上有内容（非单色）", has_content(st_all),
          f"{ww}×{wh} 逻辑 / 截图 {whole.width()}×{whole.height()} 物理 "
          f"颜色{st_all['distinct']} 标准差{st_all['std']:.0f} "
          f"跨度{st_all['max'] - st_all['min']:.0f} → {p_all}")

    # 视频区：窗口上部（标题 44 + 控制条约 100 逻辑像素之外的中间地带取一块）
    vx, vy = l + 40, t + 120
    vw, vh = max(60, ww - 700), max(60, wh - 300)

    def video_checks(tag: str = "") -> None:
        """画面在不在 + 时间在不在走。样片自带计时数字，定格帧会被第二条判掉。"""
        area = grab(vx, vy, vw, vh)
        p_area = os.path.join(OUT, f"smoke-video-area{tag}.png")
        area.save(p_area)
        st = stats(area)
        pix_check("视频区真的有画面（mpv 嵌进来了、容器没藏着）" + tag,
              st["distinct"] >= 6 and st["std"] >= 4.0 and st["max"] > 60,
              f"{area.width()}×{area.height()} 亮度{st['mean']:.0f} 标准差{st['std']:.0f} "
              f"颜色{st['distinct']} 最亮{st['max']:.0f} → {p_area}")
        time.sleep(2.0)
        area2 = grab(vx, vy, vw, vh)
        diff = 0
        a1 = area.convertToFormat(QImage.Format_ARGB32)
        a2 = area2.convertToFormat(QImage.Format_ARGB32)
        for j in range(0, min(a1.height(), a2.height()), 37):
            for i in range(0, min(a1.width(), a2.width()), 41):
                c1, c2 = a1.pixelColor(i, j), a2.pixelColor(i, j)
                if abs(c1.red() - c2.red()) + abs(c1.green() - c2.green()) + abs(c1.blue() - c2.blue()) > 18:
                    diff += 1
        pix_check("两次截图内容在变（时间在推进、不是定格帧）" + tag, diff >= 6,
              f"抽样点差异计数={diff}（样片自带计时数字，动不了就会是 0）")

    if files:
        video_checks()
    else:
        # 空启动（双击图标那条路）：先验占位页不是黑屏，再用单实例转交把样片喂进去
        empty_shot = grab(vx, vy, vw, vh)
        p_empty = os.path.join(OUT, "smoke-empty-state.png")
        empty_shot.save(p_empty)
        ste = stats(empty_shot)
        check("空启动显示占位页（有内容、不是黑屏也不是残留画面）",
              ste["std"] >= 3.0 and ste["distinct"] >= 4,
              f"{empty_shot.width()}×{empty_shot.height()} 标准差{ste['std']:.0f} "
              f"颜色{ste['distinct']} → {p_empty}")

    # 单实例：再跑一次同样的命令，第二个进程应当把路径递给已运行实例后立刻退出
    handoff = files or DEFAULT_FILES
    t0 = time.monotonic()
    second = subprocess.run([*launcher, "-m", "app.main", *handoff], cwd=ROOT,
                            env=child_env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            encoding="utf-8", errors="replace", timeout=30)
    check("二次启动走单实例转交并立即退出", second.returncode == 0 and time.monotonic() - t0 < 12,
          f"rc={second.returncode} 用时 {time.monotonic() - t0:.1f}s 输出={(second.stdout or '')[:120]!r}")
    time.sleep(1.0)
    check("转交后原实例窗口还在", user32.IsWindowVisible(wt.HWND(hwnd)) == 1, "")
    if not files:
        time.sleep(3.0)          # 转交过去的路径要走完 IPC 才起播
        video_checks("（经单实例转交载入）")

    # 关窗：走 WM_CLOSE（用户点 × 的同一条路），不许按名字杀进程
    user32.PostMessageW(wt.HWND(hwnd), WM_CLOSE, 0, 1)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        pass
    check("点 × 之后入口进程退出", proc.poll() is not None, f"poll={proc.poll()}")
    time.sleep(1.0)
    leftover = [p for p in our_mpv() if p not in before]
    check("关窗后本项目的 mpv 内核没有残留", not leftover,
          f"残留 pid={leftover}（识别依据＝可执行文件路径含 {MPV_MARK}）")

    tail = ""
    if proc.poll() is not None:
        try:
            tail = (proc.stdout.read() or "")[:2000]
        except Exception:
            pass
    else:
        print("!! 入口进程没退出，读不到它的输出（下面的 traceback 检查因此不可信）")
    check("整个生命周期没有未捕获异常", "Traceback" not in tail,
          f"子进程输出里 {'有' if 'Traceback' in tail else '没有'} Traceback；输出前 300 字={tail[:300]!r}")
    print(f"---- 子进程输出（前 2000 字）----\n{tail or '(无)'}")
    return report()


def report() -> int:
    bad = [n for n, ok, _ in results if not ok]
    print(f"\n---- 冒烟汇总：{len(results) - len(bad)}/{len(results)} 通过"
          + (f"；失败：{bad}" if bad else "；无失败")
          + (f"；因屏幕不给像素跳过 {len(skipped)} 条：{skipped}" if skipped else "") + " ----")
    print("注：音频是否真的出声无法用截图判定，需要人耳确认（播放时应有声、静音键能切断）。")
    if skipped:
        print("注：跳过≠通过。把显示器唤醒/解锁后重跑这一条才算验过（退出码 3 = 环境不允许）。")
    return 1 if bad else (3 if skipped else 0)


if __name__ == "__main__":
    raise SystemExit(main())
