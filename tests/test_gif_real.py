"""R3-2 截图工坊真机验收：真窗口、真 mpv、真 ffmpeg、真产物、真像素。

分工（前三份都绿了才轮到这份，而三份全绿**不等于**用户那边能用）：
  test_gif.py         命令拼得对 + 真 ffmpeg 的产物读得回来（没有窗口）
  test_gif_dialog.py  对话框收数与回显（有 Qt，没有主窗口）
  test_gif_wiring.py  主窗口把两边接起来（替身对话框 + 真接口对账）
  本份                用户真按 G 与 B：工坊真的出现在屏幕上、盘上真的多出一个**会动**的
                      GIF、字幕真的烧进了产物、连拍真的截出 N 张、导出中途关窗真的没有
                      ffmpeg 残留

为什么"会动"与"字幕烧进去了"必须在这儿判：ffmpeg 退出码 0 + 文件存在 + 帧数够，三种好
事实同时成立时产物仍可能是**单帧静止图**或者**一版没字的**（test_gif 顶部第 4 条记的就是
这个）。这两条只有把产物逐帧解出来量像素才判得了，而替身给不出真产物。

用法：
    python tests/test_gif_real.py            # 真机验收
    python tests/test_gif_real.py --falsify  # 证伪：已知坏样本必须让对应判据翻红
    python tests/test_gif_real.py --residue  # 只判"导出中途关窗留不留 ffmpeg"（不看像素，锁屏也能跑）

退出码：0 全绿；1 有判据没过；2 缺 mpv/ffmpeg/样片（环境，不算产品失败）；
3 环境此刻给不了判据要的前提（屏幕抠不出像素 / 工坊做不成活动窗），相应判据不作数。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# 自己的数据目录：连拍与产物都落在这里，开跑前整个清一遍，"新增了几张"才算得准。
os.environ["GELU_DATA"] = os.path.abspath(os.path.join(HERE, ".selftest-data-gifreal"))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtCore import Qt                                            # noqa: E402
from PyQt5.QtGui import QImage, QPainter                                # noqa: E402
from PyQt5.QtTest import QTest                                         # noqa: E402
from PyQt5.QtWidgets import QApplication, QShortcut                     # noqa: E402

from app import config as cfgmod                                       # noqa: E402
from app.engine import gif as G                                        # noqa: E402
from app.ui import keymap as KM                                        # noqa: E402
from app.main import crash_log_path, install_crash_log                 # noqa: E402
import probe_gif_sub as PGS                                            # noqa: E402
import smoke_launch as SL                                              # noqa: E402
import test_ui as TU                                                   # noqa: E402

SAMPLE = os.path.join(TU.SAMPLES, "gelu-sample-1.mp4")
SRT = os.path.join(TU.SAMPLES, "gelu-sample-1.srt")
# 导出这一段：4~6 秒。样片 A 的同名外挂字幕第 3 条正好盖住 4.000~5.950，也就是**整段都有字**，
# "有字幕版近白像素成片、无字幕版几乎为零"才判得稳（不靠某一刻刚好有字）。
START, DUR, WIDTH, FPS = 4.0, 2.0, 320, 8
BurstA = (3, 250)          # 跑完那一轮：3 张、间隔 250ms
BurstB = (20, 500)         # 取消那一轮：故意给足 10 秒，好在中途按停
FF = cfgmod.FFMPEG_EXE

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


# ---------------------------------------------------------------- 进程枚举
_SNAP = 0x2
_QUERY = 0x1000


def pro_named(exe_name: str, under: str) -> list[int]:
    """列出**可执行文件全路径**落在 under 下面、且文件名叫 exe_name 的进程 pid。

    为什么不按镜像名一刀切（本机踩过：按名字杀把用户自己的 Edge 带走了）：这里只**读**，
    而且必须对上路才认，别人的同名进程不算我们的残留。
    """
    out: list[int] = []
    k = ctypes.windll.kernel32

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    hSnap = k.CreateToolhelp32Snapshot(_SNAP, 0)
    pe = PE32()
    pe.dwSize = ctypes.sizeof(PE32)
    ok = k.Process32FirstW(hSnap, ctypes.byref(pe))
    under = under.lower()
    while ok:
        if pe.szExeFile.lower() == exe_name:
            h = k.OpenProcess(_QUERY, False, pe.th32ProcessID)
            if h:
                size = wt.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) \
                        and buf.value.lower().startswith(under):
                    out.append(int(pe.th32ProcessID))
                k.CloseHandle(h)
        ok = k.Process32NextW(hSnap, ctypes.byref(pe))
    k.CloseHandle(hSnap)
    return out


def ffmpeg_here() -> list[int]:
    return pro_named("ffmpeg.exe", os.path.join(ROOT, "runtime").lower())


# ---------------------------------------------------------------- 像素尺
def gimg(rows: list[list[int]]) -> QImage:
    """按给定灰度值造一张 Grayscale8 图（证伪用的合成样本，不需要屏幕也不需要解码）。"""
    w, h = len(rows[0]), len(rows)
    buf = bytearray(w * h)
    for y, row in enumerate(rows):
        for x, v in enumerate(row):
            buf[y * w + x] = v
    return QImage(bytes(buf), w, h, w, QImage.Format_Grayscale8).copy()


def _gray(img: QImage) -> tuple[bytes, int, int]:
    """回 (整幅字节, 宽, 跨行字节数)。Grayscale8 上 pixel(x,y) 回的是调色板索引串（本机实测），
    所以只能按字节读——这一点沿用 probe_gif_sub.measure 的结论。"""
    im = img.convertToFormat(QImage.Format_Grayscale8)
    return (im.constBits().asstring(im.byteCount()), im.width(), im.bytesPerLine())


def median_of(img: QImage) -> int:
    data, w, stride = _gray(img)
    vals = sorted(data[y * stride + x] for y in range(img.height()) for x in range(0, w, 2))
    return vals[len(vals) // 2] if vals else 0


def band_near_white(img: QImage, top_frac: float = 0.72, delta: int = 200) -> int:
    """下部字幕带里"接近白"的像素数：比本帧中位数亮 delta 以上才算。

    为什么不用 probe_gif_sub 那套 med+60：那份探针的样片是**纯亮度斜坡**，底下没有别的东西；
    本仓的合成样片在底部 28% 里有 61 根会动的灰刻度（灰度 150~180）和一个蓝方块（≈135），
    med+60 会把它们一起数进来，"有没有字幕"就量不出来了。白字是 245 以上，刻度最亮 180，
    中间隔着一条没人踩的线 → delta 取 200（med≈18 时起判 218）。
    """
    data, w, stride = _gray(img)
    h = img.height()
    if not w or h < 4:
        return 0
    med = median_of(img)
    lo = int(h * top_frac)
    n = 0
    for y in range(lo, h):
        row = data[y * stride: y * stride + w]
        if len(row) < w:
            break
        for x in range(w):
            if row[x] > med + delta:
                n += 1
    return n


def frame_bytes(img: QImage) -> bytes:
    data, _w, _s = _gray(img)
    return data


# ---------------------------------------------------------------- 判据（纯函数，证伪就喂这些）
def v_state(st: dict, want_file: str, kpos: float, kdur: float, want_sub: str) -> tuple[bool, str]:
    """工坊里那五个数必须是**内核此刻的真值**，不是替身喂的常数，也不是 media-title。"""
    v = str(st.get("video") or "")
    pos, dur = float(st.get("pos") or 0.0), float(st.get("duration") or 0.0)
    sub = str(st.get("sub") or "")
    same_v = bool(v) and os.path.normcase(os.path.abspath(v)) == os.path.normcase(os.path.abspath(want_file))
    d_pos = abs(pos - kpos)
    d_dur = abs(dur - kdur)
    ok = (same_v and d_pos < 0.8 and d_dur < 0.3 and dur > 5.0
          and os.path.normcase(sub) == os.path.normcase(os.path.abspath(want_sub)))
    return ok, (f"video={os.path.basename(v) or '(空)'} 与内核同文件={same_v} pos={pos:.2f} "
                f"内核={kpos:.2f} 差={d_pos:.2f} dur={dur:.2f} 内核={kdur:.2f} 差={d_dur:.2f} "
                f"sub={os.path.basename(sub) or '(空)'}")


def v_product(info: dict, want_w: int, want_h: int, expect_fr: int) -> tuple[bool, str]:
    """产物本身过关：解得出、帧数够、宽高等于所选规格、不是个空壳。"""
    fr, w, by = int(info.get("frames") or 0), int(info.get("w") or 0), int(info.get("bytes") or 0)
    h = int(info.get("h") or 0)
    ok = bool(info.get("ok")) and w == want_w and h == want_h and fr >= expect_fr and by > 2048
    return ok, (f"ok={info.get('ok')} 帧={fr}（该 ≥{expect_fr}） {w}×{h}（该 {want_w}×{want_h}）"
                f" 字节={by} err={info.get('err') or ''}")


def v_motion(frames: list[QImage]) -> tuple[bool, str]:
    """逐帧比第一帧：至少有一帧不一样，才算"会动"。单帧静止图在这里必红。"""
    if len(frames) < 2:
        return False, f"只解出 {len(frames)} 帧，谈不上动没动"
    base = frame_bytes(frames[0])
    diff = sum(1 for im in frames[1:] if frame_bytes(im) != base)
    return diff >= 1, f"{len(frames)} 帧里 {diff} 帧与首帧不同"


def v_progress(rows: list[tuple[float, float]], want_total: float) -> tuple[bool, str]:
    """进度真从 ffmpeg 流到了外壳：至少一条、不回退、末值对得上这一段长度。

    不许要求"必须有中间档"：两秒片段编码很快，ffmpeg 完全可能只把最后一档报上来，
    把这种实现细节写成产品判据就会造出随机红。
    """
    if not rows:
        return False, "一条进度都没收到（-progress pipe:1 没读上来）"
    mono = all(rows[i][0] <= rows[i + 1][0] + 1e-6 for i in range(len(rows) - 1))
    last = rows[-1][0]
    tot_ok = all(abs(float(t) - want_total) < 0.6 for _s, t in rows)
    ok = mono and last >= 0.5 * want_total and tot_ok
    return ok, f"{len(rows)} 档 末值={last:.2f}（总 {want_total:.2f}）单调={mono} 总时长对={tot_ok}"


def v_sub_band(with_frames: list[QImage], without_frames: list[QImage]) -> tuple[bool, str]:
    """字幕真烧进产物：有字版下部带成片出白字，无字幕版几乎为零。

    两趟同区间同规格各导一次再比，而不是只看有字那一趟的绝对值——样片底部本来就有一排
    会动的灰刻度，绝对值判据会把它们算成"字幕"。
    """
    a = [band_near_white(im) for im in with_frames]
    b = [band_near_white(im) for im in without_frames]
    if not a or not b:
        return False, f"有字 {len(a)} 帧 / 无字 {len(b)} 帧，解不出帧就比不了"
    fa = sum(1 for x in a if x >= 8) / len(a)
    fb = sum(1 for x in b if x >= 8) / len(b)
    ok = fa >= 0.6 and fb <= 0.10
    return ok, (f"有字版 {sum(1 for x in a if x >= 8)}/{len(a)} 帧出白字（{fa:.0%}，该 ≥60%）"
               f" 无字版 {sum(1 for x in b if x >= 8)}/{len(b)}（{fb:.0%}，该 ≤10%）"
               f" 中位白字 {sorted(a)[len(a) // 2]}/{sorted(b)[len(b) // 2]}")


def v_burst(saved: list[str], want: int, shot_dir: str) -> tuple[bool, str]:
    """连拍真的截出 want 张：每张都在盘上、都落在配置指定的截图目录里。"""
    exist = [p for p in saved if os.path.exists(p) and os.path.getsize(p) > 1024]
    same_dir = {os.path.normcase(os.path.dirname(p)) for p in exist}
    ok = len(exist) == want and same_dir == {os.path.normcase(os.path.abspath(shot_dir))}
    return ok, (f"新增 {len(exist)}/{want} 张 目录={sorted(os.path.basename(d) for d in same_dir)} "
                f"该={os.path.basename(os.path.abspath(shot_dir))}")


def v_cancel(start: int, after: int, want: int) -> tuple[bool, str]:
    """再按一次 B 是取消：等过两个间隔之后张数不再涨，而且**取消前真的在截**。

    为什么要加"至少截到一张"：旧的写法只比 `after == start`，于是 0 张→0 张也报绿
    （按键压根没命中、或连拍没起），那是恒真的绿。start 还得 < want：跑满了再按停
    就没有"中途取消"这回事，那种绿同样不作数。
    """
    started = 1 <= start < want
    ok = started and after == start
    return ok, (f"取消那一下已截 {start} 张（该 ≥1 且 <{want}，中途取消成立={started}）"
                f" → 又等 1.5 秒 {after} 张 不再涨={after == start}")


def v_cleanup(pids: list[int], crash_delta: int) -> tuple[bool, str]:
    """关窗以后不许留下还在写盘的 ffmpeg，也不许留下新的崩溃记录。"""
    return (not pids and crash_delta == 0), f"残留 ffmpeg={sorted(pids)} crash.log 新增={crash_delta} 字节"


def v_ink(img: QImage) -> tuple[bool, str]:
    ok, d = TU.has_ink(img)
    return ok, f"工坊面板 {d}"


# ---------------------------------------------------------------- 按键（真事件，但 dispatch 照常跑）
HITS: list[str] = []


def wire_hits(win) -> None:
    real = win.dispatch

    def dispatch(action):
        HITS.append(str(action))
        return real(action)

    win.dispatch = dispatch


def press(win, seq: str, wait: float = 0.08) -> list[str]:
    key, mods = TU.key_event_parts(seq)
    if not key:
        raise ValueError(f"{seq!r} 解析不出键码，没有可递的事件")
    HITS.clear()
    QTest.keyClick(win, Qt.Key(int(key)), Qt.KeyboardModifier(int(mods)))
    TU.pump(wait)
    return list(HITS)


def pick_combo(cb, value: int) -> bool:
    for i in range(cb.count()):
        if cb.itemData(i) == value:
            cb.setCurrentIndex(i)
            return True
    return False


STAMP = time.strftime("%Y%m%d-%H%M%S")


def save_shot(w, name: str) -> str:
    """留档：纯 Qt 面板用 grab_qt（不受遮挡影响），文件名带本轮时间戳，别覆盖上一轮的。"""
    p = os.path.join(TU.OUT, f"gif-real-{name}-{STAMP}.png")
    img = TU.grab_qt(w)
    img.save(p)
    print(f"[SHOT] {name} → {os.path.relpath(p, ROOT)}  {img.width()}×{img.height()}", flush=True)
    return p


def run_real() -> int:
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(120, repeat=True, exit=False)   # 卡住也要留下栈
    missing = [p for p in (SAMPLE, SRT) if not os.path.exists(p)]
    if missing:
        print(f"缺少样片/字幕：{missing}，先跑 python tests/gen_sample.py")
        return 2
    if not os.path.exists(FF):
        print(f"[ENV] 没有 ffmpeg：{FF} —— 产物类判据不作数，这不算产品失败。")
        return 2
    if not os.path.exists(SL.MPV_MARK):
        print(f"[ENV] 没有 mpv 内核：{SL.MPV_MARK}")
        return 2

    os.makedirs(TU.OUT, exist_ok=True)
    shot_dir = os.path.join(cfgmod.DATA_DIR, "shots")
    if os.path.isdir(shot_dir):
        for f in os.listdir(shot_dir):
            try:
                os.remove(os.path.join(shot_dir, f))
            except OSError:
                pass
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)   # 末尾那趟"导出中途关窗"是步骤，不是退出信号
    TU.APP = app
    install_crash_log()
    crash_before = os.path.getsize(crash_log_path()) if os.path.exists(crash_log_path()) else 0

    # 屏幕闸门放在建窗**之前**：显示器休眠/锁屏时整屏抠出来是全黑（本机实测：亮度 0、颜色 1），
    # 而这份验收有一半判据要看像素。若先建窗再判屏，日志里会留下一串"看着像产品同时坏了几件事"
    # 的红——那是环境不给屏幕，写成产品失败就是伪造故障，所以这里先单独判一次、不给就直接回 3。
    health_ok, d_health = TU.capture_health()
    print(f"[{'PASS' if health_ok else 'ENV'}] 抓屏通路可用  —— {d_health}")
    if not health_ok:
        return 3

    cfg = TU.make_cfg()
    cfg["screenshot_dir"] = shot_dir
    cfg["burst_count"], cfg["burst_gap_ms"] = BurstA
    cfg["resume"] = False
    cfg.save()
    TU.theme.install(app, str(cfg["theme_accent"]))

    win = TU.build_window(cfg)
    wire_hits(win)
    # 内核是**懒启动**的：建窗时不拉 mpv，第一条播放请求才 ensure_engine()（这是产品契约，
    # 不是缺陷）。上一轮这里写的是"内核随窗口启动成功"，问的是错的问题，所以它必然红——
    # 那种红写得再响也不是产品失败。锚点挪到递上播放请求之后。
    win.add_paths([SAMPLE])
    loaded = TU.wait_for(lambda: "gelu-sample-1" in str(win.player.prop("path", ""))
                         and not win.video.isHidden()
                         and float(win.player.prop("duration", 0) or 0) > 0, 12.0)
    check("递上第一条播放请求后内核真起来了（懒启动的正确锚点）",
          bool(win.player.running), f"running={win.player.running} version={win.player.version}")
    # duration 必须进闸门：上一轮 duration=None 也报了"真播起来了"，而工坊那五个数
    # 全部来自内核此刻的 state，时长没落定时的绿是抢跑出来的。
    check("样片真播起来了（路径不隐藏、时长已从内核报上来）", loaded,
          f"path={os.path.basename(str(win.player.prop('path', '')))} "
          f"duration={win.player.prop('duration')} hidden={win.video.isHidden()}")
    win.player.add_subtitle(SRT)
    got_sub = TU.wait_for(lambda: bool(G.sub_file_of(win.player.prop("track-list", []) or [])), 6.0)
    check("同名外挂字幕挂上后被认成可烧的文件（external）", got_sub,
          f"track-list 里选中的字幕={G.sub_file_of(win.player.prop('track-list', []) or []) or '(空)'}")

    # ---------- G 键开工坊 ----------
    hit = press(win, "G")
    dlg = win.gif_dlg
    shown = bool(dlg is not None and dlg.isVisible())
    check("按 G 真开出截图工坊（按键→动作→建窗→显示一条链）", "export_gif" in hit and shown,
          f"命中的动作={hit} 对话框可见={shown}")
    if not shown:
        print("\n---- 工坊没开出来，后面的判据都做不了 ----")
        win.close()
        return 1
    img = TU.grab_qt(dlg)
    ok_ink, d_ink = v_ink(img)
    check("工坊面板真画出东西（不是空白壳）", ok_ink, d_ink)
    img2, err2 = TU.capture(dlg, dlg, "gif-dialog")
    check("工坊面板在屏幕上也真看得见（抓屏命中链是自己的窗口）", not err2 and TU.has_picture(img2),
          err2 or f"抓屏 {img2.width()}×{img2.height()}")
    save_shot(dlg, "gif-dialog-open")
    st = win.gif_state()
    kpos = float(win.player.prop("time-pos", 0) or 0.0)
    kdur = float(win.player.prop("duration", 0) or 0.0)
    ok_s, d_s = v_state(st, SAMPLE, kpos, kdur, SRT)
    check("工坊报的五个数就是内核此刻的真值（video 用 path，不是 media-title）", ok_s, d_s)

    # ---------- 导出：带字幕那一趟 ----------
    progs: list[tuple[float, float]] = []
    dones: list[tuple[int, bool, str, str]] = []
    win.gif.sig_progress.connect(lambda j, s, t: progs.append((float(s), float(t))))
    win.gif.sig_done.connect(lambda j, o, dt, p: dones.append((int(j), bool(o), str(dt), str(p))))

    def set_form(sub: bool) -> str:
        dlg.spin_start.setValue(START)
        dlg.spin_dur.setValue(DUR)
        pick_combo(dlg.combo_w, WIDTH)
        pick_combo(dlg.combo_f, FPS)
        dlg.chk_sub.setChecked(sub)
        return dlg.spec()["video"]

    spec_v = set_form(True)
    check("界面收好的导出参数指向正在播的文件", spec_v == st["video"], f"spec.video={os.path.basename(spec_v)}")
    dlg.btn_go.click()
    done = TU.wait_for(lambda: dones, 60.0)
    check("真 ffmpeg 跑完并回报了结果（没挂着也没静默吞掉）", done,
          f"结果条数={len(dones)} 进度档数={len(progs)}")
    if not done:
        win.close()
        return 1
    jid, okx, detail, path = dones[-1]
    check("第一趟导出成功且产物在盘上", okx and bool(path) and os.path.exists(path),
          f"ok={okx} 产物={os.path.basename(path)} detail={detail[:160]}")
    info = G.read_gif(path) if path else {}
    ok_p, d_p = v_product(info, WIDTH, 180, G.expect_frames(DUR, FPS))
    check("产物读回来：帧数够、宽高等于所选规格、不是空壳", ok_p, d_p)
    frames = PGS.frames_of(path)
    ok_m, d_m = v_motion(frames)
    check("产物真的会动（逐帧比首帧，单帧静止图在这里必红）", ok_m, d_m)
    ok_pr, d_pr = v_progress(progs, DUR)
    check("进度从 ffmpeg 的 -progress 管道真的走到了外壳", ok_pr, d_pr)
    check("导出完成后界面上报的是同一个产物（打开目录要点它）",
          os.path.normcase(str(dlg.last_path)) == os.path.normcase(os.path.abspath(path)),
          f"dlg.last_path={os.path.basename(str(dlg.last_path))} 回报={os.path.basename(path)}")

    # ---------- 字幕对照：同区间再来一趟，这趟不带字幕 ----------
    progs.clear()
    dones.clear()
    set_form(False)
    dlg.btn_go.click()
    done2 = TU.wait_for(lambda: dones, 60.0)
    check("第二趟（无字幕）也真跑完", done2, f"结果={dones[-1][1] if dones else '没回报'}")
    path2 = dones[-1][3] if dones else ""
    ok_b, d_b = v_sub_band(frames, PGS.frames_of(path2)) if path2 else (False, "第二趟没有产物")
    check("字幕真烧进了产物（有字/无字同区间各一趟，下部带差）", ok_b, d_b)
    save_shot(dlg, "gif-dialog-two-arms")

    # ---------- 连拍：真截出 N 张 ----------
    saved: list[str] = []
    win.player.sig_saved.connect(saved.append)

    def mirror_pre() -> tuple[bool, str]:
        """按 B 之前把前提钉死：工坊是**活动窗**，而且它身上真挂着 B 那一把键。

        为什么必须钉：Qt 的 WindowShortcut 只听当前活动窗——上一轮的 FAIL 就是这个形状
        （工坊一开，主窗口整张快捷键表静默失效）。不验活动窗的话，"工坊没抢到活动窗、
        于是主窗口自己的快捷键响了"也会报绿，而那一响正是修之前就在响的东西：判据的前提
        被旧行为喂绿，刚修的缺陷就没测到。
        为什么不写成"条数 == len(mirrorable(win.key_rows))"：那是拿同一个函数算两遍再跟
        自己点头（恒真的绿）。这里改按**键值点名**：生效表里 burst_shot 绑的是哪把键，
        工坊身上就必须挂着那一把 QShortcut——两份事实来源不同（表 vs 装出来的对象）。
        """
        act = bool(dlg.isActiveWindow())
        if not act:
            dlg.activateWindow()
            TU.pump(0.25)
            act = bool(dlg.isActiveWindow())
        want_b = {KM.parsed(s) for s, a, _d in win.key_rows
                  if a == "burst_shot" and KM.parsed(s)}
        kids = dlg.findChildren(QShortcut)
        have = {int(sc.key()[0]) for sc in kids if sc.key().count() >= 1}
        ok = act and bool(want_b) and bool(want_b & have)
        return ok, (f"工坊是活动窗={act} 身上挂 {len(kids)} 条镜像键，其中含 burst_shot 那把"
                    f"={bool(want_b & have)}（键表里该键={sorted(want_b)}）"
                    f" 主窗身上 {len(win.findChildren(QShortcut))} 条")

    pre_ok, d_pre = mirror_pre()
    check("按 B 之前前提立得住：工坊是活动窗、且镜像里真有 B 那一把键", pre_ok, d_pre)
    if not pre_ok:
        # 环境不给活动窗（焦点被别的应用抢走）时这两腿只是巧合，不作数——写成产品失败就是伪造故障。
        print("---- 此刻判不了连拍：环境没给活动窗（rc=3，不是产品失败）----")
        win.gif.cancel()
        win.close()
        return 3
    cfg["burst_count"], cfg["burst_gap_ms"] = BurstA
    dlg.spin_n.setValue(BurstA[0])
    dlg.spin_gap.setValue(BurstA[1])
    before = set(saved)
    hit_b = press(win, "B")
    full = TU.wait_for(lambda: len(saved) - len(before) >= BurstA[0], 6.0)
    ok_bs, d_bs = v_burst(saved[len(before):len(before) + BurstA[0]], BurstA[0], shot_dir)
    check("工坊是活动窗时按 B 仍真截出配置里那几张（快捷键表没被面板吃掉）",
          "burst_shot" in hit_b and full and ok_bs, f"命中的动作={hit_b} {d_bs}")

    # ---------- 连拍中途再按一次 B：取消之后不再涨 ----------
    cfg["burst_count"], cfg["burst_gap_ms"] = BurstB
    dlg.spin_n.setValue(BurstB[0])
    dlg.spin_gap.setValue(BurstB[1])
    before2 = set(saved)
    press(win, "B")
    TU.pump(1.6)
    start_stop = len(saved) - len(before2)
    pre_ok2, d_pre2 = mirror_pre()          # 每一按都要重新验：活动窗会被别处抢走
    press(win, "B")
    TU.pump(1.5)
    after_stop = len(saved) - len(before2)
    ok_c, d_c = v_cancel(start_stop, after_stop, BurstB[0])
    check("再按一次 B 是取消：之后不再多截一张（且取消前真在截）",
          pre_ok2 and ok_c, f"{d_pre2}｜{d_c}")

    # ---------- 导出进行中关窗：不留活着的 ffmpeg ----------
    # 上一轮这里是"6 秒 / 640 宽 / 15 帧 + pump(0.7) 之后看一眼进程表"，本机实测那一趟
    # 0.45 秒内就自己跑完了：于是"关窗前确有 ffmpeg"必红，而"关窗后没有残留"是运气好——
    # 那种绿比红更坏。改成 --residue 的形状：整片 10 秒 / 960 宽 / 20 帧（软解 + palettegen
    # 要跑好几秒），用 wait_for 量"真跑起来了"，再单独判"关窗那一下还在跑"，两条都不靠时间点猜。
    dones.clear()
    dlg.spin_start.setValue(0.0)
    dlg._repaint_note()      # 改了起点不会自己重算时长上界（界面里只有 refresh 会算）
    dlg.spin_dur.setValue(10.0)
    pick_combo(dlg.combo_w, 960)
    pick_combo(dlg.combo_f, 20)
    dlg.chk_sub.setChecked(False)
    sp = dlg.spec()
    check("界面真收得住这一整趟（起点 0、10 秒、960 宽、20 帧）",
          abs(sp["start"]) < 0.05 and sp["dur"] >= 9.5 and sp["width"] == 960 and sp["fps"] == 20,
          f"spec={sp['start']:.2f}+{sp['dur']:.2f} 秒 {sp['width']} 宽 {sp['fps']}fps"
          f"（上界被 spin_dur 夹住就会在这儿露出来）")
    dlg.btn_go.click()
    live = TU.wait_for(lambda: bool(ffmpeg_here()), 8.0)
    check("长一趟真在跑（关窗前确有 ffmpeg 子进程）", live, f"pid={ffmpeg_here()}")
    out_gif = str(win.gif._cur.out) if win.gif._cur else ""
    size_at_close = os.path.getsize(out_gif) if out_gif and os.path.exists(out_gif) else 0
    still = ffmpeg_here()
    check("关窗那一下导出**确实还在跑**（不在跑就判不了残留，这条不算过）", bool(still),
          f"pid={still} 产物此刻={size_at_close} 字节")
    print(f"[STEP] 导出进行中关窗（产物={os.path.basename(out_gif)} "
          f"{size_at_close} 字节，此刻 ffmpeg={still}）", flush=True)
    win.close()
    died = TU.wait_for(lambda: not ffmpeg_here(), 8.0)
    alive = ffmpeg_here()
    grew = (os.path.getsize(out_gif) if out_gif and os.path.exists(out_gif) else 0) - size_at_close
    crash_after = os.path.getsize(crash_log_path()) if os.path.exists(crash_log_path()) else 0
    ok_cl, d_cl = v_cleanup(alive, crash_after - crash_before)
    check("导出中途关窗：没有留下还在写盘的 ffmpeg，也没有新的崩溃记录",
          bool(still) and died and ok_cl,
          f"{d_cl} 等它停住={died} 产物字节增量={grew}（>0 说明它还在往里写）")
    if alive:
        killed = kill_pids(alive)
        print(f"[CLEANUP] 收掉本轮自己起出来的 ffmpeg：{killed}", flush=True)
    win.gif.wait(3000)
    return 1 if [r for r in results if not r[1]] else 0


_PROCESS_TERMINATE = 0x0001


def kill_pids(pids: list[int]) -> list[int]:
    """收掉**本轮自己起出来的**进程。按镜像名一刀切本机踩过坑（把用户自己的 Edge 带走了），
    所以这里只杀 pro_named/ffmpeg_here 已经按可执行文件全路径认过的那些 pid。"""
    k = ctypes.windll.kernel32
    done = []
    for pid in pids:
        h = k.OpenProcess(_PROCESS_TERMINATE, False, pid)
        if h and k.TerminateProcess(h, 0):
            done.append(pid)
        if h:
            k.CloseHandle(h)
    return done


def run_residue() -> int:
    """只判一条：导出跑到一半关掉窗口，那个 ffmpeg 有没有跟着停。

    为什么单独开一个模式：这条量的是**进程残留**，一个像素都不看，所以显示器休眠/锁屏
    （本文件真机模式要回 rc=3 的那种时刻）照样能跑。内核此刻起不起来也不影响它——spec
    由这里直接递给真槽 `win._gif_export`（界面上的 sig_export 连的就是它；导出链本身由
    run_real 与 test_gif_wiring 判），而关窗走的是用户那条 closeEvent。
    """
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(30, repeat=True, exit=False)   # 卡住也要留下栈
    if not os.path.exists(SAMPLE):
        print(f"[ENV] 缺样片 {SAMPLE}，先跑 python tests/gen_sample.py")
        return 2
    if not os.path.exists(FF):
        print(f"[ENV] 没有 ffmpeg：{FF}")
        return 2
    TU._enable_high_dpi()
    print("[STEP] 建 QApplication", flush=True)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)   # 关窗是这一条判据的**步骤**，不许顺手把测试进程劝退
    TU.APP = app
    install_crash_log()
    crash_before = os.path.getsize(crash_log_path()) if os.path.exists(crash_log_path()) else 0
    cfg = TU.make_cfg()
    print("[STEP] 建主窗口（内核此刻起不起来都不影响这条判据）", flush=True)
    win = TU.build_window(cfg)
    # 整条样片（10 秒）× 960 宽 × 20 fps：软解 + palettegen 要跑好几秒，够在中途把窗口关掉。
    # 为什么用整片而不是 2 秒片段：本机实测 2 秒/960/20 那一趟在关窗之前就自己跑完了，
    # 于是"没有残留"是运气好而不是取消生效——那种绿比红更坏，见下面那条"关窗那一下还在跑"。
    win._gif_export({"video": SAMPLE, "start": 0.0, "dur": 10.0,
                     "width": 960, "fps": 20, "sub": ""})
    live = TU.wait_for(lambda: bool(ffmpeg_here()), 8.0)
    pids_live = ffmpeg_here()
    check("长一趟真在跑（关窗前确有 ffmpeg 子进程）", live, f"pid={pids_live}")
    if not live:
        win.gif.cancel()
        print("[STEP] 关窗", flush=True)
        win.close()
        return 1
    out_gif = str(win.gif._cur.out) if win.gif._cur else ""
    size_at_close = os.path.getsize(out_gif) if out_gif and os.path.exists(out_gif) else 0
    still = ffmpeg_here()
    # 这条闸门专门拦"运气好"：关窗时这趟已经自己跑完了，那就没有残留可判，判据不作数。
    check("关窗那一下导出**确实还在跑**（不在跑就判不了残留，这条不算过）", bool(still),
          f"pid={still} 产物此刻={size_at_close} 字节")
    print(f"[STEP] 导出进行中关窗（产物={os.path.basename(out_gif)} "
          f"{size_at_close} 字节，此刻 ffmpeg={still}）", flush=True)
    win.close()
    died = TU.wait_for(lambda: not ffmpeg_here(), 8.0)
    alive = ffmpeg_here()
    grew = (os.path.getsize(out_gif) if out_gif and os.path.exists(out_gif) else 0) - size_at_close
    crash_after = os.path.getsize(crash_log_path()) if os.path.exists(crash_log_path()) else 0
    ok_cl, d_cl = v_cleanup(alive, crash_after - crash_before)
    check("导出中途关窗：没有留下还在写盘的 ffmpeg，也没有新的崩溃记录", ok_cl and died,
          f"{d_cl} 等它停住={died} 产物字节增量={grew}（>0 说明它还在往里写）")
    left = os.path.exists(out_gif) if out_gif else False
    check("取消顺带收掉半截产物（关窗不该在用户目录里留一个没人要的 GIF）", not left,
          f"残留={os.path.basename(out_gif) if left else '无'}"
          f"{f' {os.path.getsize(out_gif)} 字节' if left else ''}")
    if alive:
        killed = kill_pids(alive)
        print(f"[CLEANUP] 收掉本轮自己起出来的 ffmpeg：{killed}", flush=True)
    win.gif.wait(3000)
    return 1 if [r for r in results if not r[1]] else 0


# ---------------------------------------------------------------- 证伪
def _bad_frames(n: int = 6, still: bool = False) -> list[QImage]:
    """造 n 帧：still=True 就每帧一模一样（单帧静止图那种坏产物）。"""
    rows = [[18] * 40 for _ in range(20)]
    out = [gimg(rows)]
    for i in range(1, n):
        r2 = [row[:] for row in rows]
        if not still:
            r2[15][10 + i] = 200
        out.append(gimg(r2))
    return out


def _frame(white_in_band: int, band_top_row: int = 15) -> QImage:
    rows = [[18] * 40 for _ in range(20)]
    x = 0
    while x < white_in_band:
        rows[band_top_row][x % 40] = 250
        if x % 40 == 39:
            rows[band_top_row + 1][x % 40] = 250
        x += 1
    return gimg(rows)


FALSIFY = [
    ("状态取的是替身常数（video 用标题、pos 恒 0、dur 恒 0、sub 丢）", v_state,
     ({"video": SAMPLE, "pos": 3.2, "duration": 10.0, "ab": None, "sub": SRT}, SAMPLE, 3.2, 10.0, SRT),
     [({"video": "样片 A · 1280x720 · 30fps", "pos": 3.2, "duration": 10.0, "sub": SRT}, SAMPLE, 3.2, 10.0, SRT),
      ({"video": SAMPLE, "pos": 0.0, "duration": 10.0, "sub": SRT}, SAMPLE, 3.2, 10.0, SRT),
      ({"video": SAMPLE, "pos": 3.2, "duration": 0.0, "sub": SRT}, SAMPLE, 3.2, 10.0, SRT),
      ({"video": SAMPLE, "pos": 3.2, "duration": 10.0, "sub": ""}, SAMPLE, 3.2, 10.0, SRT)]),
    ("产物坏相：单帧／宽度不对／空壳／读失败", v_product,
     ({"ok": True, "frames": 16, "w": 320, "h": 180, "bytes": 30000, "err": ""}, 320, 180, 16),
     [({"ok": True, "frames": 1, "w": 320, "h": 180, "bytes": 30000, "err": ""}, 320, 180, 16),
      ({"ok": True, "frames": 16, "w": 640, "h": 360, "bytes": 30000, "err": ""}, 320, 180, 16),
      ({"ok": True, "frames": 16, "w": 320, "h": 180, "bytes": 200, "err": ""}, 320, 180, 16),
      ({"ok": False, "frames": 0, "w": 0, "h": 0, "bytes": 0, "err": "解不出"}, 320, 180, 16)]),
    ("静止产物：所有帧一模一样", v_motion, (_bad_frames(6, False),),
     [(_bad_frames(6, True),), ([( _frame(0))],)]),
    ("进度坏相：一条没有／回退／末值只有 1 成／总时长报错", v_progress,
     ([(0.5, 2.0), (2.0, 2.0)], 2.0),
     [([], 2.0), ([(2.0, 2.0), (0.5, 2.0)], 2.0), ([(0.2, 2.0), (0.3, 2.0)], 2.0),
      ([(2.0, 9.0)], 2.0)]),
    ("字幕带坏相：两版都没字／硬编码都有字／无字版也满", v_sub_band,
     ([_frame(30) for _ in range(10)], [_frame(0) for _ in range(10)]),
     [([_frame(0) for _ in range(10)], [_frame(0) for _ in range(10)]),
      ([_frame(30) for _ in range(10)], [_frame(30) for _ in range(10)]),
      ([_frame(30) for _ in range(3)], [_frame(30) for _ in range(3)])]),
    # v_burst 不在表里：它判的是"盘上真有几个文件"，喂假路径的好样本会自己判红。
    # 好/坏四组都在 run_falsify 下面用临时目录现造现删（见 cases）。
    ("取消坏相：取消后又涨／一张没截也报「没再涨」／跑满了才按", v_cancel, (4, 4, 20),
     [(4, 7, 20), (0, 0, 20), (20, 20, 20), (2, 2, 2)]),
    ("残留与崩溃：有活着的 ffmpeg／crash.log 变长", v_cleanup, ([], 0),
     [([1234], 0), ([], 88), ([1234], 88)]),
    ("面板空白：一整块同色必须判「没画出内容」（防 has_ink 恒真）", v_ink,
     (gimg([[240 if x < 20 else 20 for x in range(40)] for _ in range(20)]),),
     [(gimg([[18] * 40 for _ in range(20)]),), (gimg([[0] * 40 for _ in range(20)]),)]),
]


def _call(fn, args: tuple) -> tuple[bool, str]:
    """判据自己崩了 ≠ 判据判红：这里把异常吃下来、单独标 CRASH，免得一条坏样本
    用 traceback 冒充"抓到"（本机验收规矩：断言崩与断言失败必须在回执里分开写）。"""
    try:
        return fn(*args)
    except BaseException as exc:  # noqa: BLE001
        return False, f"CRASH {type(exc).__name__}: {exc}"


def run_falsify() -> int:
    miss = 0
    for name, fn, good, bads in FALSIFY:
        ok_g, d_g = _call(fn, good)
        got, crashed = [], []
        for b in bads:
            ok_b, d_b = _call(fn, b)
            got.append(not ok_b)
            if d_b.startswith("CRASH"):
                crashed.append(d_b[:90])
        caught = all(got) and ok_g and not crashed and bool(bads)
        miss += 0 if caught else 1
        tag = "CAUGHT" if caught else "MISSED"
        extra = f"｜崩了={crashed}" if crashed else ""
        print(f"[{tag}] {name}  —— 好样本 ok={ok_g}｜坏样本判红={got}｜{d_g[:120]}{extra}")
    # v_burst 依赖真盘上的文件，坏样本只能在这里现造：少一张 / 目录不符 / 名字不在盘上
    tmp = os.path.join(HERE, ".falsify-burst")
    os.makedirs(tmp, exist_ok=True)
    real = [os.path.join(tmp, f"s{i}.png") for i in range(3)]
    stray = os.path.join(tmp, "z.png")        # 真在盘上、但不在配置的那个目录里
    for p in real + [stray]:
        open(p, "wb").write(b"0" * 4096)
    cases = [
        ("少一张", real[:2], 3, tmp, False),
        ("文件在盘上、但落的目录不是配置那一个", [stray], 1, os.path.join(tmp, "other"), False),
        ("其实不在盘上", [os.path.join(tmp, "gone.png")], 1, tmp, False),
        ("三张都对", real, 3, tmp, True),
    ]
    for name, saved, want, d, want_ok in cases:
        ok, det = v_burst(saved, want, d)
        hit = (ok == want_ok)
        miss += 0 if hit else 1
        print(f"[{'CAUGHT' if hit else 'MISSED'}] 连拍：{name}  —— ok={ok} 该={want_ok}｜{det}")
    for p in real + [stray]:
        try:
            os.remove(p)
        except OSError:
            pass
    print(f"\n证伪 {len(FALSIFY) + len(cases)} 组，漏网 {miss} 组")
    return 1 if miss else 0


if __name__ == "__main__":
    if "--falsify" in sys.argv:
        raise SystemExit(run_falsify())
    if "--residue" in sys.argv:
        raise SystemExit(run_residue())
    raise SystemExit(run_real())
