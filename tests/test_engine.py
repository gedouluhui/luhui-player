"""播放内核无界面自检（不依赖 UI）。

用法：
    python tests/test_engine.py            # 正常自检：全部断言必须 PASS
    python tests/test_engine.py --falsify  # 证伪：故意喂坏输入，断言必须 FAIL 才算测试可信

测的是：IPC 往返与超时、属性订阅是否真在推、时长/seek/倍速/音量/静音、播放列表与跳转、
轨道与外挂字幕、硬解是否生效、截图产物、错误日志、内核被杀后的存活上报。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

# Windows 控制台默认 GBK，中文断言名会糊成一片；统一走 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("GELU_DATA", os.path.abspath(os.path.join(os.path.dirname(__file__), ".selftest-data")))

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt5.QtCore import QCoreApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine import perf, tweaks  # noqa: E402
from app.engine.ipc import IpcError, MpvIpc  # noqa: E402
from app.engine.player import MpvPlayer  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
FILES = [os.path.join(SAMPLES, n) for n in
         ("gelu-sample-1.mp4", "gelu-sample-2.mp4", "gelu-sample-3.mkv")]
SRT = os.path.join(SAMPLES, "gelu-sample-1.srt")

results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []
APP = None   # 跨线程 emit 到 lambda 是"队列投递"，必须有人跑事件循环才会送达


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def wait_for(pred, timeout: float, step: float = 0.05) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if APP is not None:
            APP.processEvents()
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step)
    if APP is not None:
        APP.processEvents()      # 最后再投递一次，避免"已到但没送"的假失败
    return False


def probe_value(spec) -> float:
    """挑一个和复位值不同的在范围格点：拿默认值去 set 判不出「根本没生效」。

    提到模块级是给 --falsify 复用：证伪那边要证明"每张 Num 都能拿到一个非默认格点"，
    否则 8.5 那条逐项生效就可能在靠"设了个本来就是这个值"蒙过去（恒真判据）。
    """
    for cand in (spec.default + 3 * spec.step, spec.default - 3 * spec.step,
                 spec.lo if spec.lo != spec.default else spec.hi):
        v = spec.clamp(cand)
        if abs(v - spec.default) > 1e-9:
            return v
    return float(spec.hi)


def _silent_pipe_server(pipe_path: str, stop: threading.Event) -> None:
    """只接受连接、永不回应的假内核，用来验证超时逻辑真的会触发。"""
    k32 = ctypes.windll.kernel32
    k32.CreateNamedPipeW.restype = ctypes.c_void_p
    k32.ConnectNamedPipe.restype = wt.BOOL
    h = k32.CreateNamedPipeW(pipe_path, 3, 0, 1, 4096, 4096, 0, None)  # DUPLEX|BYTE|WAIT
    if not h or h == ctypes.c_void_p(-1).value:
        return
    k32.ConnectNamedPipe(ctypes.c_void_p(h), None)
    while not stop.is_set():
        time.sleep(0.05)
    k32.DisconnectNamedPipe(ctypes.c_void_p(h))
    k32.CloseHandle(ctypes.c_void_p(h))


def check_timeout_behaviour() -> None:
    pipe = r"\\.\pipe\gelu-silent-%d" % os.getpid()
    stop = threading.Event()
    threading.Thread(target=_silent_pipe_server, args=(pipe, stop), daemon=True).start()
    time.sleep(0.25)
    sip = MpvIpc(pipe)
    try:
        sip.connect(timeout=3.0)
        t0 = time.perf_counter()
        raised = False
        try:
            sip.request(["get_property", "path"], timeout=0.4)
        except IpcError:
            raised = True
        spent = time.perf_counter() - t0
        check("无应答请求必须超时抛错而非挂死", raised and spent < 2.0, f"{spent:.2f}s 后抛出 IpcError")
    except IpcError as exc:
        check("无应答请求必须超时抛错而非挂死", False, f"连假管道都没连上：{exc}")
    finally:
        sip.close()
        stop.set()


def main() -> int:
    global APP
    import faulthandler
    faulthandler.dump_traceback_later(45, repeat=True, exit=False)  # 卡住时把栈打到 stderr
    app = QCoreApplication.instance() or QCoreApplication([])
    APP = app
    for p in FILES:
        if not os.path.exists(p):
            print(f"缺少样片 {p}，请先运行 python tests/gen_sample.py")
            return 2

    cfg = cfgmod.Config()
    cfg["screenshot_dir"] = os.path.join(cfgmod.DATA_DIR, "shots")
    cfg["resume"] = False
    # 静音起点必须自己钉死：这份 config.json 是**跨腿共享**的（同一个 GELU_DATA），
    # 而 tests/probe_fullscreen_fit.py:1323 会往里写 mute=True 并且从不还原（那条腿
    # 要的就是"内核全程静音"）。2026-10-20 实测：红过一次 `mute=False`——内核的
    # cycle mute 是对的（三组离线探针都翻得动），红的是判据把"起始没静音"当成前提。
    # 少写这一行，这条断言就得靠下面那个"翻面"判据去兜一个谁都可能改写的起点。
    cfg["mute"] = False
    cfg.save()
    os.makedirs(cfg["screenshot_dir"], exist_ok=True)

    # 0. 传输层结构体布局：hEvent 偏移错一位就会"首请求之后全超时"（实测踩过一次）
    from app.engine.ipc import _Overlapped
    check("OVERLAPPED 布局正确（32 字节，hEvent@24）",
          ctypes.sizeof(_Overlapped) == 32 and _Overlapped.hEvent.offset == 24,
          f"sizeof={ctypes.sizeof(_Overlapped)}, hEvent@{_Overlapped.hEvent.offset}")

    player = MpvPlayer(cfg)
    logs: list[str] = []
    times: list[float] = []
    player.sig_log.connect(lambda s: logs.append(s))
    player.sig_time.connect(lambda t: times.append(t))

    t_start = time.perf_counter()
    ok = player.start(hwnd=0)
    check("内核启动并连上 IPC", ok and player.running,
          f"耗时 {time.perf_counter() - t_start:.2f}s, version={player.version}")
    if not ok:
        for l in logs[-10:]:
            print("   log:", l)
        return 1

    # 1. IPC 往返延迟（测 20 次）
    rtts = []
    for _ in range(20):
        t0 = time.perf_counter()
        player.get("time-pos")
        rtts.append((time.perf_counter() - t0) * 1000)
    rtts.sort()
    check("IPC 往返延迟中位数 < 15ms", rtts[len(rtts) // 2] < 15.0,
          f"中位 {rtts[len(rtts)//2]:.1f}ms / p90 {rtts[int(len(rtts)*0.9)]:.1f}ms / max {rtts[-1]:.1f}ms")

    # 2. 加载与属性订阅
    player.open_files(FILES[:1], remember=False)
    check("订阅到 time-pos 事件", wait_for(lambda: len(times) >= 3, 4.0),
          f"4s 内收到 {len(times)} 次 time-pos 回调"
          + (f"；内核消息：{logs[:2]}" if logs else ""))
    dur = float(player.get("duration", 0) or 0)
    check("时长与样片一致(10s±0.5)", 9.5 <= dur <= 10.5, f"duration={dur:.3f}s")

    # 3. seek
    player.seek_abs(5.0)
    got = wait_for(lambda: abs(float(player.get('time-pos', 0) or 0) - 5.0) < 0.6, 3.0)
    check("精确 seek 到 5s", got, f"time-pos={player.get('time-pos')}")

    # 4. 倍速真实生效（1 秒墙钟内播放进度应推进 ≥1.5 秒）
    player.set_speed(2.0)
    time.sleep(0.3)
    a = float(player.get("time-pos", 0) or 0)
    time.sleep(1.0)
    b = float(player.get("time-pos", 0) or 0)
    check("2x 倍速下进度按 2 倍推进", (b - a) >= 1.5, f"1.0s 墙钟推进 {b - a:.2f}s，speed={player.get('speed')}")
    player.set_speed(1.0)

    # 5. 音量/静音（>100 需 volume-max 生效）
    player.set_volume(137)
    check("音量可越过 100（到 137）", wait_for(lambda: int(player.get("volume", 0) or 0) == 137, 2.0),
          f"volume={player.get('volume')}")
    # 三条各测一件事，别把它们缝成一条：
    #   起点——内核是不是按 config.mute 起的（这一条把"测试自己写了 False"变成"产品认了它"）；
    #   翻面——cycle 到底动不动得了；
    #   翻回——它不是单向开关（卡在 True 的坏毛病，只看第一条判据永远看不见）。
    # 为什么判据从"切到 True"改成"从起点翻面"：见上面 cfg["mute"] = False 那段实测。
    m0_raw = player.get("mute", None)
    m0 = bool(m0_raw)
    check("静音起点跟随 config.mute（内核未静音）", m0 is False,
          f"config.mute={cfg['mute']} 内核 mute={m0_raw!r}")
    player.toggle_mute()
    check("静音状态可切换", wait_for(lambda: bool(player.get("mute", False)) is (not m0), 2.0),
          f"起点={m0} 期望={not m0} 实得={player.get('mute')!r}")
    player.toggle_mute()
    check("再切一次能翻回起点", wait_for(lambda: bool(player.get("mute", False)) is m0, 2.0),
          f"期望={m0} 实得={player.get('mute')!r}")

    # 6. 播放列表与跳转
    player.open_files(FILES, remember=False)
    check("播放列表载入 3 项", wait_for(lambda: len(player.get("playlist", []) or []) == 3, 3.0),
          f"playlist={len(player.get('playlist', []) or [])}")
    player.goto_index(2)
    check("可跳到第 3 项", wait_for(lambda: "sample-3" in str(player.get("filename", "")), 4.0),
          f"filename={player.get('filename')}")

    # 7. 轨道与外挂字幕
    #    goto 之后 track-list 会先清空再逐项填：只读一次会拿到上一个文件的残留或空表
    #    （实测偶发 types=[]），所以判据要求"当前文件已是 sample-1 且视频/音频轨都在"。
    def tracks_now() -> list[dict]:
        return player.get("track-list", []) or []

    def has_video_audio() -> bool:
        tl = tracks_now()
        return ("sample-1" in str(player.get("path", ""))
                and any(t.get("type") == "video" for t in tl)
                and any(t.get("type") == "audio" for t in tl))

    player.goto_index(0)
    wait_for(has_video_audio, 6.0)
    wait_for(lambda: any(t.get("type") == "sub" and (t.get("external") or t.get("external-filename"))
                         for t in tracks_now()), 6.0)
    tracks = tracks_now()
    kinds = sorted({str(t.get("type")) for t in tracks})
    subs = [t for t in tracks if t.get("type") == "sub"]
    check("轨道含视频与音频", has_video_audio(), f"types={kinds}, n={len(tracks)}")
    check("外挂 srt 被自动识别", any(s.get("external") or s.get("external-filename") for s in subs),
          f"字幕轨={[(s.get('type'), s.get('external'), s.get('lang')) for s in subs]}（sub-auto=all）")
    if subs:
        player.set_track("sub", subs[0].get("id"))
        check("可切换字幕轨", wait_for(lambda: player.get("sid") == subs[0].get("id"), 2.0),
              f"sid={player.get('sid')}")

    # 8. 硬件解码是否真的在工作
    #    换文件后 hwdec-current 会先回 null、解码器起来才有值，所以必须等而不是读一次
    #    （只读一次时实测会偶发拿到 None，把好的内核误判成失败）
    player.seek_abs(1.0)
    got_hw = wait_for(lambda: bool(player.get("hwdec-current")), 5.0)
    hw = player.get("hwdec-current")
    check("硬件解码生效（hwdec-current 非空）", got_hw and bool(hw),
          f"hwdec-current={hw}, 请求={cfg['hwdec']}")

    # 8.2 启动期那四项（engine/perf.py）：非法档必须在**拼参数之前**被拦下。
    #     实测过内核面对 --hwdec=bogus 是静默降级：不报错、不退出，只在 hwdec-current
    #     上回一个 'no'。所以这张白名单是承重件——它坏了不崩，只会"用户以为开了硬解、
    #     其实全程软解"，这种账只有在这里算清才看得见。
    args_live, notes_live = perf.startup_args(cfg)
    check("正常配置：四项照表拼出启动参数，且不欠任何回退说明",
          f"--hwdec={perf.resolve(cfg)['hwdec']}" in args_live and not notes_live,
          f"args={args_live} 回退={notes_live}")
    bogus_cfg = {"hwdec": "bogus-hwdec", "gpu_api": "direct3d11",
                 "quality_preset": "ultra", "volume_max": 999}
    args_b, notes_b = perf.startup_args(bogus_cfg)
    dflt = perf.resolve(bogus_cfg)
    check("四项全非法：一项都不许原样递给内核，全部落回默认档",
          f"--hwdec={perf.DEFAULTS['hwdec']}" in args_b
          and f"--gpu-api={perf.DEFAULTS['gpu_api']}" in args_b
          and f"--scale={perf.preset(perf.DEFAULTS['quality_preset']).scale}" in args_b
          and f"--volume-max={perf.DEFAULTS['volume_max']}" in args_b,
          f"args={args_b}")
    check("每一项回退都留了一条点名说明（静默降级最坏的就是不吭声）",
          len(notes_b) == 4 and all("已按" in n for n in notes_b), f"说明={notes_b}")
    check("界面列出的每一档内核都认（下拉里不许放内核没有的写法）",
          all(perf.valid("hwdec", v) for v, _ in perf.HWDEC_CHOICES)
          and all(perf.valid("gpu_api", v) for v, _ in perf.GPU_API_CHOICES)
          and all(perf.valid("quality_preset", p.pid) for p in perf.QUALITY_PRESETS)
          and all(perf.valid("volume_max", v) for v, _ in perf.VOLUME_MAX_CHOICES),
          f"hwdec 界面 {len(perf.HWDEC_CHOICES)} 档 / 内核全量 {len(perf.HWDEC_ALL)} 档")
    check("解码通路回报认得出形状：'no'/空/None 都是软解，只有真写法才算硬解",
          perf.hwdec_state("no") == (False, "软件解码")
          and perf.hwdec_state(None) == (False, "软件解码")
          and perf.hwdec_state("  ") == (False, "软件解码")
          and perf.hwdec_state("d3d11va") == (True, "d3d11va"),
          f"no→{perf.hwdec_state('no')} 空→{perf.hwdec_state(None)} d3d11va→{perf.hwdec_state('d3d11va')}")
    check("真机对账：内核回报的 hwdec-current 是白名单里的写法",
          bool(hw) and (hw in perf.HWDEC_ALL or perf.hwdec_state(hw)[0]),
          f"请求={cfg['hwdec']} 回报={hw} 全量名单={len(perf.HWDEC_ALL)} 档")

    # 8.5 可调项全表真机往返
    #     engine/tweaks.py 那张表同时是 UI 滑块范围、启动参数、夹紧逻辑和这里期望值的
    #     唯一事实源，所以它必须逐项对得上内核：任何一档"界面里有、内核不认"（比如
    #     butterlight 这种这一版根本不接受的色调映射）都会在这里现形，而不是等用户点。
    bad_num: list[str] = []
    bad_cfg: list[str] = []
    for spec in tweaks.NUMS:
        want = probe_value(spec)
        actual, ok = player.set_tweak(spec.prop, want)
        if not (ok and tweaks.same_value(spec, want, actual)):
            bad_num.append(f"{spec.prop} 请求 {want} 回报 {actual}")
        if abs(float(cfg[spec.cfg]) - want) > 1e-6:
            bad_cfg.append(f"{spec.prop}→cfg[{spec.cfg}]={cfg[spec.cfg]}")
    check(f"数值类可调项逐项生效（{len(tweaks.NUMS)} 项，发进去=内核报回来）",
          not bad_num, f"不符：{bad_num}")
    check("同一批值确实落进了配置（下次启动的 --参数 由它拼）", not bad_cfg, f"不符：{bad_cfg}")

    bad_choice: list[str] = []
    n_opt = 0
    for item in tweaks.CHOICES:
        for v, text in item.options:
            n_opt += 1
            actual, ok = player.set_tweak(item.prop, v)
            if not ok:
                bad_choice.append(f"{item.prop}={v}（{text}）回报 {actual!r}")
    check(f"枚举类可调项每一档都被内核接受（{n_opt} 档里没有'点了没反应'）",
          not bad_choice, f"被拒：{bad_choice}")

    bad_flag: list[str] = []
    for item in tweaks.FLAGS:
        for v in (True, False):
            actual, ok = player.set_tweak(item.prop, v)
            if not ok or bool(actual) is not v:
                bad_flag.append(f"{item.prop}={v} 回报 {actual!r}")
    check("布尔类可调项双向生效（yes/no 与 True/False 不分家）", not bad_flag, f"不符：{bad_flag}")

    over, ok_over = player.set_tweak("brightness", 130)
    check("越界值在发出前就被表夹住（130 → 内核 100，且确认通过）",
          ok_over and over is not None and abs(float(over) - 100.0) < 1e-6, f"回报={over}")

    player.set_volume(250)
    check("音量 250 被夹到内核上限 200",
          wait_for(lambda: int(player.get("volume", 0) or 0) == 200, 2.0)
          and player.volume_max() == 200,
          f"volume={player.get('volume')} volume-max={player.get('volume-max')}")
    player.set_volume(100)

    n_log = len(logs)
    val, ok_unknown = player.set_tweak("no-such-prop", 1)
    check("表外的属性不发命令、并且报出来",
          (not ok_unknown) and val is None and len(logs) > n_log, f"logs={logs[-1:]}")

    player.set_tweak("brightness", 40)
    player.set_tweak("contrast", -20)
    out = player.reset_video_eq()
    # 判据里别写 `v or -1`：0.0 是假值，会把"正好回到 0"当成"没回报"（第一版就这么假红过）
    check("画面复位一次把五项拉回 0（回报的是内核真值）",
          len(out) == len(tweaks.EQ)
          and all(v is not None and abs(float(v)) < 1e-6 for v in out.values()),
          f"{out}")

    cur_tone = str(player.prop("tone-mapping", "auto"))
    nxt_tone, ok_tone = player.cycle_choice("tone-mapping")
    want_tone = tweaks.cycle_option(tweaks.TONE_MAPPING, cur_tone)
    check("枚举循环按表里的顺序走一格并回报真值",
          ok_tone and str(nxt_tone) == want_tone, f"{cur_tone} → {nxt_tone}（期望 {want_tone}）")

    player.set_tweak("sub-delay", 0.0)
    player.nudge_tweak("sub-delay", 0.1)
    _v2, ok_nudge = player.nudge_tweak("sub-delay", 0.1)
    got_delay = float(player.get("sub-delay", 0) or 0)
    check("连按两次累加到 0.2（起点读内核缓存，不是读配置）",
          ok_nudge and abs(got_delay - 0.2) < 1e-4, f"sub-delay={got_delay}")

    # 布尔翻转：起点不假设是"开"——上面那轮逐项验收最后把它设成了 False，
    # 拿"关→开"当期望就会假红。判据写成"两次翻转各回一次"才与起点无关。
    start_vis = bool(player.prop("sub-visibility", True))
    player.toggle_flag("sub-visibility")
    mid_vis = bool(player.prop("sub-visibility", start_vis))
    player.toggle_flag("sub-visibility")
    back_vis = bool(player.prop("sub-visibility", mid_vis))
    check("布尔项翻转走内核真值（连按两次回到起点）",
          mid_vis is (not start_vis) and back_vis is start_vis,
          f"{start_vis} → {mid_vis} → {back_vis}")

    restore_bad: list[str] = []
    for spec in tweaks.NUMS:
        actual, ok = player.set_tweak(spec.prop, spec.default)
        if not ok or not tweaks.same_value(spec, spec.default, actual):
            restore_bad.append(f"{spec.prop}={actual!r}")
    for item in tweaks.CHOICES:
        actual, ok = player.set_tweak(item.prop, item.default)
        if not ok:
            restore_bad.append(f"{item.prop}={actual!r}")
    for item in tweaks.FLAGS:
        actual, ok = player.set_tweak(item.prop, item.default)
        if not ok:
            restore_bad.append(f"{item.prop}={actual!r}")
    check(f"整表回到复位值（{len(tweaks.NUMS) + len(tweaks.CHOICES) + len(tweaks.FLAGS)} 项，"
          "自测档案不留脏值）", not restore_bad, f"未回到默认：{restore_bad}")

    # A-B 区间：设点用的是播放位置，判据用 ab_range()（它把内核的 "no" 译成 None）
    player.pause()
    player.seek_abs(2.0)
    wait_for(lambda: abs(player._num(player.prop("time-pos", 0)) - 2.0) < 0.6, 4.0)
    player.set_ab("a")
    wait_for(lambda: player.prop("ab-loop-a") is not None, 3.0)
    player.seek_abs(5.0)
    wait_for(lambda: abs(player._num(player.prop("time-pos", 0)) - 5.0) < 0.6, 4.0)
    player.set_ab("b")
    rng_ok = wait_for(lambda: player.ab_range() is not None, 3.0)
    rng = player.ab_range() or (0.0, 0.0)
    check("A-B 设点成对（A≈2s、B≈5s，ab_range 读出区间）",
          rng_ok and 1.2 <= rng[0] <= 2.8 and 4.2 <= rng[1] <= 5.8, f"区间={rng}")
    player.clear_ab()
    cleared = wait_for(lambda: player.ab_range() is None, 3.0)
    check("清除 A-B 后内核回报 'no' 被译成未设置", cleared, f"ab={player.prop('ab-loop-a')}")
    player.play()

    # 9. 截图产物：路径来自命令应答 data.filename（实测 0.41 不发 file-saved 事件）
    before = set(os.listdir(cfg["screenshot_dir"]))

    def shot_file():
        new = sorted(set(os.listdir(cfg["screenshot_dir"])) - before)
        if not new:
            return ""
        p = os.path.join(cfg["screenshot_dir"], new[0])
        try:
            # 落盘是异步的：文件名先出现、字节随后写完，只判存在会把半截 PNG 当成成功
            return p if os.path.getsize(p) > 5000 else ""
        except OSError:
            return ""

    saved: list[str] = []
    player.sig_saved.connect(lambda p: saved.append(p))
    player.snapshot(include_sub=True)
    wait_for(lambda: bool(shot_file()), 6.0)
    new = sorted(set(os.listdir(cfg["screenshot_dir"])) - before)
    check("截图落盘且非空", bool(shot_file()),
          f"新增={new} 大小={os.path.getsize(shot_file()) if shot_file() else '未就绪'}")
    check("截图路径经 sig_saved 上报", wait_for(lambda: bool(saved), 4.0)
          and os.path.abspath(saved[0]) == os.path.abspath(shot_file()), f"sig_saved={saved}")
    # 命名模板只在启动参数里生效（set_property 实测无效），单独一条防止回退成 mpv-shot*
    check("截图名走自定义模板（品牌前缀）", bool(new) and new[0].startswith(f"{cfgmod.BRAND_ID}-"),
          f"新增={new}")

    # 9b 发给内核的路径必须是**绝对**的。这一条不是洁癖：mpv 是 `cwd=<mpv 安装目录>` 启的
    #    （player.start），相对路径在它那边重新求值，实测后果是"screenshot 三种模式全报
    #    error running command + shots 目录一个不长 + mpv 自己的 log-file 写到 runtime/mpv/tests/…
    #    空目录"——功能坏了且现场连一句原话都没留下（本机 2026-10-11 由 tests/probe_shot_modes.py
    #    量出来：同一份代码，GELU_DATA 给相对路径 0/6 成功、给绝对路径 6/6 成功）。
    #    判据跑在 build_args 的**落盘字节**上，不跑在"我记得 abspath 了"上。
    def arg_of(args: list[str], key: str) -> str:
        hit = [a for a in args if a.startswith(key + "=")]
        return hit[0][len(key) + 1:] if hit else ""

    rel_cfg = cfgmod.Config()
    rel_cfg["screenshot_dir"] = os.path.join("tests", "relative-shot-probe")   # 故意给相对
    probe_args = MpvPlayer(rel_cfg).build_args(0)
    _shot_arg = arg_of(probe_args, "--screenshot-directory")
    _log_arg = arg_of(probe_args, "--log-file")
    _conf_arg = arg_of(probe_args, "--input-conf")
    check("配置里给相对截图目录，发下去的三条内核路径仍收口成绝对",
          all(os.path.isabs(v) for v in (_shot_arg, _log_arg, _conf_arg)),
          f"shot={_shot_arg} log={_log_arg} conf={_conf_arg}")
    check("命名管道不许被收口改坏（它不是文件路径，abspath 会把 \\\\ . \\pipe 变成盘符）",
          arg_of(probe_args, "--input-ipc-server").startswith(r"\\.\pipe"),
          f"pipe={arg_of(probe_args, '--input-ipc-server')}")
    check("DATA_DIR 无论 GELU_DATA 怎么写都是绝对路径（相对的那一路在源头就收口）",
          os.path.isabs(cfgmod.DATA_DIR) and os.path.isabs(cfgmod.LOG_DIR),
          f"GELU_DATA={os.environ.get('GELU_DATA')!r} DATA_DIR={cfgmod.DATA_DIR}")

    # 10. 错误日志
    errs = [l for l in logs if "error" in l.lower()]
    check("内核日志无 error", len(errs) == 0, f"error 行数={len(errs)}; {errs[:3]}")

    # 11. 坏输入：外壳会过滤不存在的路径，所以真正要验的是"存在但不是视频"的文件
    corrupt = os.path.join(SAMPLES, "broken.mp4")
    with open(corrupt, "wb") as fh:
        fh.write(b"FTYP this is not a real video file")
        fh.write(os.urandom(8192))
    logs.clear()  # 第 10 项已判过此前的日志，这里只看坏文件引发的新上报
    player.open_files([corrupt], remember=False)
    wait_for(lambda: bool(logs), 2.0)  # 泵事件：sig_log 是跨线程排队投递的
    check("伪 mp4 不弄死内核", player.running, f"running={player.running}, logs={logs[-3:]}")
    check("伪 mp4 有可读的错误上报", any(("error" in l.lower()) or ("失败" in l) for l in logs),
          f"log 行数={len(logs)}; {logs[:3]}")

    # 12. 超时行为：自建“只连不回话”的假内核，验证 request 会超时而不是挂死
    check_timeout_behaviour()

    # 13. 进程被杀 → 上报 sig_died
    died: list[int] = []
    player.sig_died.connect(lambda c: died.append(c))
    if player._proc:
        player._proc.kill()
    check("内核退出可被感知", wait_for(lambda: len(died) > 0, 5.0), f"exit codes={died}")
    time.sleep(3.0)  # 让看门狗重启一次
    check("崩溃后自动重启成功", player.running, f"running={player.running}, 重启后 version={player.version}")

    player.quit()
    time.sleep(0.4)
    check("退出后进程清理干净", not player.running, f"running={player.running}")

    return report(t_start)


def report(t_start: float) -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n---- 汇总：{passed}/{len(results)} 通过"
          + (f"；失败项：{fails}" if fails else "；无失败")
          + f"，用时 {time.perf_counter() - t_start:.1f}s ----", flush=True)
    if falsified:
        print("==== 证伪（坏输入必须被抓出来）====")
        for name, ok, detail in falsified:
            print(f"[{'CAUGHT' if ok else 'MISSED'}] {name}" + (f"  —— {detail}" if detail else ""))
        missed = [n for n, ok, _ in falsified if not ok]
        # 收口计数由脚本自己从同一份台账现算（不是文档里手抄的那个数）：
        # 加一条注入就不必再去文档里改"31 条"，抄的数会在加法那天悄悄落后。
        print(f"---- 证伪收口：抓到 {len(falsified) - len(missed)}/{len(falsified)}"
              f"（漏网 {len(missed)}） ----", flush=True)
        if missed:
            print("!! 以下坏样本没被抓到，自检不可信：" + ", ".join(missed))
            return 1
    return 0 if passed == len(results) else 1


def falsify() -> int:
    """故意制造错误条件，确认检测器会 FAIL；抓不到就说明测试是假的。"""
    # F1：连接不存在的管道必须失败
    ipc = MpvIpc(r"\\.\pipe\gelu-does-not-exist-12345")
    caught = False
    try:
        ipc.connect(timeout=1.0)
    except IpcError:
        caught = True
    falsified.append(("连接不存在的管道必须失败", caught, ""))

    # F1b：把"发参数前收口成绝对路径"这一步摘掉（monkeypatch 成恒等），主判据必须翻红。
    #      这条存在的理由和 9b 是一对：9b 只证明"现在是绝对的"，而绝对的来源可能是
    #      上游恰好给了绝对路径（本机默认就是绝对的），那样 9b 就是恒真的绿。摘掉承重件
    #      之后 --screenshot-directory 必须退回相对，判据立刻不成立，交回产品又成立。
    from app.engine import player as player_mod
    _orig_mpv_path = player_mod.mpv_path

    def _shot_arg_via(fn) -> str:
        player_mod.mpv_path = fn
        try:
            c2 = cfgmod.Config()
            c2["screenshot_dir"] = os.path.join("tests", "relative-shot-probe")
            args = player_mod.MpvPlayer(c2).build_args(0)
        finally:
            player_mod.mpv_path = _orig_mpv_path
        hit = [a for a in args if a.startswith("--screenshot-directory=")]
        return hit[0].split("=", 1)[1] if hit else ""

    _keep = _shot_arg_via(_orig_mpv_path)
    _dropped = _shot_arg_via(lambda p: p)
    falsified.append(("摘掉路径收口后截图目录会退回相对（9b 那条判据不是恒真）",
                      os.path.isabs(_keep) and not os.path.isabs(_dropped),
                      f"收口={_keep} 摘掉={_dropped}"))
    # 正对照：收口函数自己不许把命名管道也 abspath 掉（那样 IPC 直接连不上）
    _pipe_in = r"\\.\pipe\gelu-123"
    falsified.append(("收口函数对命名管道原样放行（正对照，防 abspath 通吃）",
                      _orig_mpv_path(_pipe_in) == _pipe_in,
                      f"得到 {_orig_mpv_path(_pipe_in)}"))

    # F2：读不存在的属性必须回落到默认值（不能凭空有值）
    cfg = cfgmod.Config()
    p = MpvPlayer(cfg)
    if p.start(hwnd=0):
        bogus = p.get("this-property-does-not-exist", "DEFAULT")
        falsified.append(("读不存在的属性必须返回默认值", bogus == "DEFAULT", f"得到 {bogus!r}"))

        # F3：把 main() 里"时长"那条判据原样搬到空闲内核上，必须为假
        #     （若这里为真，说明 10s 那条断言恒真、根本不依赖样片）
        dur = float(p.get("duration", 0) or 0)
        same_expr = 9.5 <= dur <= 10.5
        falsified.append(("未加载样片时按 10s 预期必须判 FAIL", dur == 0.0 and not same_expr,
                          f"duration={dur}"))

        # F4：空闲时 seek 不应改变时间
        p.seek_abs(5.0)
        time.sleep(0.4)
        t = float(p.get("time-pos", 0) or 0)
        falsified.append(("空闲时 seek 到 5s 不应生效", t == 0.0, f"time-pos={t}"))

        # F5：空闲时既没有播放列表也不该有当前文件
        pl = p.get("playlist", []) or []
        falsified.append(("空闲时播放列表非空=异常，按 3 项预期必须判 FAIL",
                          len(pl) != 3 and not p.get("path"), f"playlist={len(pl)} path={p.get('path')!r}"))

        # F6：非法参数必须抛错，不能静默"成功"（实测 mpv 0.41 的 screenshot 第二位是 legacy 开关）
        raised = ""
        try:
            p._ipc.request(["screenshot", "subtitles", True], timeout=2.0)
        except IpcError as exc:
            raised = str(exc)
        falsified.append(("给 screenshot 传非法 legacy 参数必须报错", bool(raised), raised))

        # F7：读线程内发同步请求必须被防线立刻拒绝（否则整条管道会自我死锁）
        seen: list[str] = []

        def hook(_m):
            if seen:
                return
            try:
                # 直接走 ipc，别用 player.get()——它会把 IpcError 咽掉，防线就测不到了
                p._ipc.request(["get_property", "mpv-version"], timeout=1.5)
                seen.append("竟然拿到了值——防线失效")
            except IpcError as exc:
                seen.append(str(exc))

        p._ipc.on_any_event(hook)
        p.set_prop("volume", 97)  # 造一次 property-change，让回调真的跑在读线程里
        end = time.monotonic() + 3.0
        while not seen and time.monotonic() < end:
            time.sleep(0.05)
        falsified.append(("读线程内同步请求必须被拒绝", bool(seen) and "禁止" in seen[0], seen[:1]))

        # F9：枚举表里混进一档内核不认的，逐项验收必须判 False。
        #     没有这一条，8.5 的"32 档全被接受"就是恒真：内核爱理不理都算过。
        #     butterlight 是这一版（v0.41.0-1101）明确不收的色调映射档，
        #     收的是 auto spline mobius hable reinhard gamma linear clip。
        bogus, ok_bogus = p.set_tweak("tone-mapping", "butterlight", persist=False)
        falsified.append(("给 tone-mapping 喂内核不认的 butterlight 必须判不一致",
                          not ok_bogus, f"回报 {bogus!r} ok={ok_bogus}"))

        # F10：F9 的正对照——表里真有的档必须判 True。
        #      只有 F9 的话，一条"永远返回 False"的确认逻辑也能骗过证伪。
        h_val, ok_h = p.set_tweak("tone-mapping", "hable", persist=False)
        falsified.append(("表里存在的 tone-mapping 档必须判通过（正对照）",
                          ok_h and str(h_val) == "hable", f"回报 {h_val!r} ok={ok_h}"))

        # F11：8.5 那条 130→100 靠的是表里的夹紧，不是内核的钳位。
        #      实测（本机 v0.41.0-1101）：直接 set_property brightness=130 时内核整条
        #      拒收、回报仍是发之前的 40——所以"内核自己会夹到 100"这句是错的假设，
        #      真出问题时是"我们没夹住 → 命令被丢 → 界面和画面都对不上"。
        p.set_tweak("brightness", 40)
        p.set_prop("brightness", 130)
        time.sleep(0.15)
        raw_b = float(p.get("brightness", 0) or 0)
        falsified.append(("绕过表直接发 130 时内核不会替我们夹到 100",
                          raw_b != 100.0, f"内核回报={raw_b}（表夹后应为 100）"))

        # F11b：反方向——内核的范围比表宽。sub-scale 我们只开放到 3.0，
        #       内核能收 50；绕过表就会看到 50 真落进内核，证明范围是表在管。
        p.set_prop("sub-scale", 50.0)
        time.sleep(0.15)
        raw_s = float(p.get("sub-scale", 0) or 0)
        falsified.append(("范围是表定的：越过表上限直接发，内核真会收下 50",
                          raw_s > 3.0 and tweaks.spec("sub-scale").clamp(50.0) == 3.0,
                          f"内核={raw_s} 表夹后={tweaks.spec('sub-scale').clamp(50.0)}"))
        p.set_tweak("sub-scale", tweaks.spec("sub-scale").default)
        p.set_tweak("brightness", 0)

        # F11c：音量同理。实测直接发 250 时内核整条拒收（回报仍是 100），
        #       所以 8.5 那条"250 → 200"证明的是 set_volume 的夹紧 + 启动参数
        #       --volume-max=200，而不是"内核自己会夹"。
        p.set_volume(100)
        p.set_prop("volume", 250)
        time.sleep(0.15)
        raw_v = float(p.get("volume", 0) or 0)
        via_api = (p.set_volume(250), p.get("volume"))
        falsified.append(("音量：越过上限直接发内核会拒收，200 是我们夹出来的",
                          raw_v != 200.0 and float(via_api[1] or 0) == 200.0,
                          f"直发={raw_v} 走 set_volume={via_api[1]}"))
        p.set_volume(100)

        # F12：连按增量到顶不能越界（快捷键一路飘出范围，UI 还显示着合法值）
        top_v, _ok_top = p.nudge_tweak("sub-border-size", 8.0)
        over_v, _ = p.nudge_tweak("sub-border-size", 0.5)
        falsified.append(("在上限再按一次增量必须停在上限、不能飘出表外",
                          over_v is not None and abs(float(over_v) - 8.0) < 1e-6,
                          f"到顶={top_v} 再按={over_v}"))
        p.set_tweak("sub-border-size", tweaks.spec("sub-border-size").default)

        # F12b：nudge 只认数值项。拿枚举项去加减必须直接判失败，
        #       否则快捷键会把 "hable" 当数字加 0.1 发给内核。
        n_val, n_ok = p.nudge_tweak("tone-mapping", 1.0)
        falsified.append(("对枚举项做增量必须拒绝", n_val is None and not n_ok,
                          f"回报={n_val!r} ok={n_ok}"))
        # F12c：静音那三条判据不许恒真。把命令发送这一腿（_fire）摘成空操作，
        #       "翻面"必须当场判不出来；接回原函数后立刻又翻得动（正对照）。
        #       为什么单独给它一条：那三条读的是同一个 mute 属性，_fire 是它们共同的
        #       承重件——少了这条注入，"内核压根不回 mute / 我们把命令拼错了"也能全绿。
        m_before = bool(p.get("mute", False))
        _orig_fire = p._fire
        p._fire = lambda command: None                  # 静默吞掉 cycle mute
        p.toggle_mute()
        stuck = wait_for(lambda: bool(p.get("mute", False)) is (not m_before), 0.6)
        p._fire = _orig_fire
        p.toggle_mute()
        revived = wait_for(lambda: bool(p.get("mute", False)) is (not m_before), 2.0)
        falsified.append(("摘掉命令发送后 mute 不再翻面（复原后立刻翻得动＝正对照）",
                          stuck is False and revived is True,
                          f"摘桩翻面={stuck}（要 False） 复原翻面={revived}（要 True） 起点={m_before}"))
        if bool(p.get("mute", False)) is not m_before:   # 别把静音状态留给下一趟
            p.toggle_mute()
            wait_for(lambda: bool(p.get("mute", False)) is m_before, 2.0)
        p.quit()
    else:
        falsified.append(("证伪需要内核可用", False, "内核启动失败，证伪无法进行"))

    # F13：8.5 的"逐项生效"不许恒真——每张 Num 都得能挑出一个非默认的格点。
    #      万一某张表把 lo/hi/step/default 写成"只有默认值可达"，那条断言就会
    #      变成"往内核塞了个它本来就是的值"，全绿但什么都没测。
    vacuous = [s.prop for s in tweaks.NUMS if abs(probe_value(s) - s.default) <= 1e-9]
    falsified.append(("每张数值表都有非默认格点可测（否则逐项生效=恒真）",
                      not vacuous, f"只会给默认值：{vacuous}"))

    # F14：复位那条判据的 0.0 陷阱（第一版就是在这里假红的）：
    #      `v or -1` 把 0.0 当假值，"正好回到 0"永远判不过；而 None 必须判不过。
    #      这里用已知答案的对照组同时钉住两头，防止判据改成"永远 True"。
    def _new_judge(d):
        return all(v is not None and abs(float(v)) < 1e-6 for v in d.values())

    def _old_judge(d):
        return all(abs(float(v or -1)) < 1e-6 for v in d.values())

    zeros = {s.prop: 0.0 for s in tweaks.EQ}
    with_none = dict(zeros, brightness=None)
    falsified.append(("复位判据：全 0 必须为真、旧写法必须为假、混 None 必须为假",
                      _new_judge(zeros) and not _old_judge(zeros) and not _new_judge(with_none),
                      f"新={_new_judge(zeros)} 旧={_old_judge(zeros)} 混None={_new_judge(with_none)}"))

    # F15：cycle_option 的三条不变式。少任何一条，Shift+T 都会变成"看运气换档"：
    #      ① 当前值不在表里（老配置留的 butterlight）要从默认档起步，不能停在未知值；
    #      ② 正向一格与反向一格互逆；③ 走完一整圈必须回到起点、且每档都走到过。
    tm = tweaks.TONE_MAPPING
    vals = [v for v, _ in tm.options]
    from_unknown = tweaks.cycle_option(tm, "butterlight")
    one_up = tweaks.cycle_option(tm, "gamma", True)
    back_home = tweaks.cycle_option(tm, one_up, False)
    cur, seen_cycle = vals[0], []
    for _ in range(len(vals)):
        cur = tweaks.cycle_option(tm, cur)
        seen_cycle.append(cur)
    falsified.append(("枚举循环：未知起点落到默认档的下一格",
                      from_unknown == vals[(vals.index(tm.default) + 1) % len(vals)],
                      f"得到 {from_unknown}（期望 {vals[(vals.index(tm.default) + 1) % len(vals)]}）"))
    falsified.append(("枚举循环：正一格反一格必须回原档", back_home == "gamma", f"回到 {back_home}"))
    falsified.append(("枚举循环：走完一圈回到起点且不漏档",
                      seen_cycle[-1] == vals[0] and set(seen_cycle) == set(vals),
                      f"一圈={seen_cycle}"))

    # F16：option_text 不许把未知值谎报成第 0 项（那是替用户改了设置），
    #      也不许把已知档原样吐回去（界面就变成英文代码，白做中文表）。
    falsified.append(("未知枚举值必须原样回显、不谎报成第一档",
                      tweaks.option_text(tm, "butterlight") == "butterlight",
                      f"得到 {tweaks.option_text(tm, 'butterlight')!r}"))
    falsified.append(("已知枚举值必须给界面文字（正对照）",
                      tweaks.option_text(tm, "clip") == "clip（直接截断）",
                      f"得到 {tweaks.option_text(tm, 'clip')!r}"))

    # F17：颜色。这一版的回报串是 #AARRGGBB（透明度在前），比 CSS 的顺序反。
    #      判据：6 位补 FF、3 位展开、8 位**不许调换**、非法值回落到全不透明白。
    falsified.append(("颜色格式：6 位补 FF、8 位不调换、非法回落",
                      tweaks.norm_color("#123456") == "#FF123456"
                      and tweaks.norm_color("#80123456") == "#80123456"
                      and tweaks.norm_color("#abc") == "#FFAABBCC"
                      and tweaks.norm_color("garbage") == "#FFFFFFFF",
                      f"6位={tweaks.norm_color('#123456')} 8位={tweaks.norm_color('#80123456')}"))
    falsified.append(("颜色比对：#000000 与 #FF000000 是同一个色（正对照）",
                      tweaks.same_value(tweaks.SUB_BACK_COLOR, "#000000", "#FF000000"), ""))

    # F18：回报比对的容差两头都要钉住：0.2 与 0.200001 算同一个值（内核回报小数位多），
    #      0.2 与 0.21 必须算不同（否则"逐项生效"形同虚设，差一格也绿）。
    sd = tweaks.spec("sub-delay")
    falsified.append(("数值比对容差：放过四舍五入噪声、抓住差一格",
                      tweaks.same_value(sd, 0.2, 0.200001) and not tweaks.same_value(sd, 0.2, 0.21),
                      f"近={tweaks.same_value(sd, 0.2, 0.200001)} 远={tweaks.same_value(sd, 0.2, 0.21)}"))

    # F19 白名单是承重件：把 perf.valid 打桩成恒真（等价于"这张表没生效"），非法档必须
    #     立刻原样拼进启动参数，同时回退说明一起消失——两头都要翻得动，才证明 8.2 那几条
    #     不是在描述一个永远为真的表达式。
    bad4 = {"hwdec": "bogus-hwdec", "gpu_api": "direct3d11", "quality_preset": "ultra",
            "volume_max": 999}
    a_clean, n_clean = perf.startup_args(bad4)
    real_valid = perf.valid
    perf.valid = lambda key, value: True
    try:
        a_mut, n_mut = perf.startup_args(bad4)
    finally:
        perf.valid = real_valid
    falsified.append(("白名单一旦失效，非法档就原样进参数且不再有任何说明（撤桩后必须复原）",
                      "--hwdec=bogus-hwdec" in a_mut and not n_mut
                      and "--hwdec=bogus-hwdec" not in a_clean and len(n_clean) == 4,
                      f"撤桩={a_clean} 打桩={a_mut} 说明(撤/打)={len(n_clean)}/{len(n_mut)}"))

    # F20 回退说明单独摘掉（只坏 warnings 这一条路）：档位仍然回退正确，但用户不会知道
    #      自己选的档被换了。8.2 里"点名说明"那条必须能否决这种半坏状态。
    real_warn = perf.warnings
    perf.warnings = lambda cfg: []
    try:
        a2, n2 = perf.startup_args(bad4)
    finally:
        perf.warnings = real_warn
    falsified.append(("只摘掉回退说明：参数仍回退、说明必须为空（判据能否决这种沉默）",
                      "--hwdec=auto-safe" in a2 and n2 == [], f"args={a2} 说明={n2}"))

    # F21 "非空即硬解"那种写法会把内核回报的 'no' 读成"正在硬解 no"。把两种写法并排放：
    #     坏写法必须在 'no' 上报真，我们那条必须报假——这才叫钉住形状，而不是凑数。
    naive = lambda raw: (bool(raw), str(raw or ""))
    falsified.append(("naive 的非空判断会把 'no' 当成硬解（我们的 hwdec_state 必须不这么判）",
                      naive("no")[0] and not perf.hwdec_state("no")[0]
                      and perf.hwdec_state("d3d11va")[0] and naive("d3d11va")[0],
                      f"naive('no')={naive('no')}  ours('no')={perf.hwdec_state('no')}"))

    # F8：检测器本身——故意喂一条假断言，必须被记成 FAIL（证明 check 不是恒真）
    n = len(results)
    recorded = check("_证伪专用：假断言必须判 FAIL", False, "故意喂进去的 False")
    kept = len(results) == n + 1 and results[-1][1] is False
    results.pop()  # 这条故意造的失败不进汇总
    falsified.append(("check() 会把假断言记成 FAIL", bool(recorded) is False and kept, ""))

    return report(time.perf_counter())


if __name__ == "__main__":
    sys.exit(falsify() if "--falsify" in sys.argv else main())
