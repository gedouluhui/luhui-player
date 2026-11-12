"""端到端：**真产品进程**里的 GPU 加速实测（用户问的"GPU 加速到底有没有给我加上"）。

这一趟补的是 docs/design.md §3 第 7 条自己留下的缺口：那批 hwdec 读数是**裸起 mpv** 量的，
而产品起的是 `--wid=<hwnd> --vo=gpu --gpu-api=d3d11` 的嵌入实例——渲染通路不是同一条。
所以这里只走真产品：`packaging/open_file.py` 起 Qt 外壳，外壳自己拉起 mpv，
再从内核自己的命名管道问它"你现在到底在用什么解码"。

三趟装配（**串行**：上一趟收拾干净、单实例那根管道空出来了才起下一趟——
两个实例同时开着的话，后起的那个会把文件交给前一个然后自己退掉，
那已经不是这一趟要测的形状了）：
  ① `hwdec=auto-safe`（出厂默认）——要看的是"默认档真的在硬解、4K 真的实时"；
  ② `hwdec=no`（强制软解）——**①那条判据的牙**：如果判据在②也报"硬解在跑"，
     那它就是恒真的废尺，①的绿一钱不值；
  ③ `hwdec=bogus-value`——perf.py 那张白名单是不是承重件：内核收到的 `options/hwdec`
     必须是回退后的 `auto-safe`，而不是原样递进去的坏串（实测原样递的话内核照样启动、
     只是静默退到软解，用户只会发现"变卡了"）。

四把尺，各管一件事（每把都先量"它有没有鉴别力"，量不出差别的尺**只入档、不当判据**）：
  • `hwdec-current`：内核回报真正在用的解码通路。`hwdec`／`options/hwdec` 是**命令行回显**
    （bogus 也照原样回显），所以它注定不能当证据——③那趟就是它的反面教材；
  • `nvidia-smi utilization.decoder`：NVDEC 硬件真在动没有（`usage.SMI_FIELDS` 里**没有**
    这一项，所以这里单独查一次，不借那张表的位置——借了会把 `memory.used` 读成解码器占用）；
  • 进度推进率＝`time-pos` 增量 ÷ 墙钟增量，加 `frame-drop-count` 增量：4K 有没有卡；
  • 外壳与内核的 CPU%：软解该由 CPU 买单（psutil 的 cpu_percent 必须 priming 两次取差，
    一次调用只会得到 0）。

本机已实测的属性名清单在 tests/out/probe-gpu-chain.txt：`hw-decode`／`current-decoder`／
`current-hwdec`／`decoder-desc`／`d3d11-adapters` 在这版内核里**不存在**（property not found），
所以这里一条都不问——一条不存在的答案会把 `ask()` 那一整批读数一起作废，
症状是"全部读数 None＋一条也没取到"，而内核其实活得好好的（这趟真翻过车）。

跑法：
    python tests/e2e_gpu.py
    python tests/e2e_gpu.py --expect-red   # 反向对照：强制软解那一趟改口要求"硬解在跑"，必须翻红

退出码：0 全绿（或反向对照成立）；1 至少一条真失败／反向对照没翻红；2 环境不给
（管道已被占／没内核／没 4K 样片／静音前提不成立）。
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from player_harness import (APP_ERR, PSUTIL_ERR, ask, cfgmod, cpu_window,  # noqa: E402
                            kill_pids, make_env, pipe_listening, pipe_name, pipe_of,
                            player_pids, psutil, silent_gate_selftest, silent_now, spawn,
                            tell, wait_child, wait_pid, wait_props)

from app.engine import perf  # noqa: E402
import app.engine.usage as U  # noqa: E402  只用它的 nvidia-smi 路径与两个小解析器

UHD = os.path.join(HERE, "samples", "uhd")
S_HEVC = os.path.join(UHD, "hevc-4k10.mp4")      # 4K60 HEVC Main10
S_H264 = os.path.join(UHD, "h264-4k.mp4")        # 4K60 H.264 Constrained Baseline，~62 Mbps
S_AV1 = os.path.join(UHD, "av1-4k.mp4")          # 4K60 AV1 Main
BOOT_WAIT = 150.0
FREE_WAIT = 45.0        # 上一趟的锁管道要空出来才允许起下一趟
MEASURE = 6.0           # 每趟的采样窗口（秒）
RT_MIN = 0.90           # 进度推进率下限（1.0＝正好实时）
DROP_MAX = 30           # 窗口内允许的画面掉帧数（6s×60fps＝360 帧的约 8%）

RESULTS: list[list[str]] = []
OUT_PATH = ""
EXPECT_RED = "--expect-red" in sys.argv

LOAD_PROPS = ["path", "mute", "volume", "pause", "idle-active", "playlist-count",
              "playlist-pos", "width", "height", "video-format", "video-codec"]
HW_PROPS = ["hwdec-current", "current-gpu-context", "current-vo", "options/hwdec",
            "hwdec", "time-pos", "frame-drop-count", "decoder-frame-drop-count",
            "estimated-vf-fps", "pause", "path"]
T_PROPS = ["time-pos", "frame-drop-count", "decoder-frame-drop-count"]


def say(tag: str, what: str, why: str = "") -> None:
    RESULTS.append([tag, what, why])
    line = f"[{tag}] {what}" + (f" —— {why}" if why else "")
    print(line, flush=True)
    if OUT_PATH:
        try:
            with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def count(tag: str) -> int:
    return sum(1 for r in RESULTS if r[0] == tag)


def red(tag: str, needle: str) -> bool:
    """反向对照的成不成，只看**那一条哨兵**有没有红（不是"随便红一条"）。
    拿 count('FAIL')>=1 当成立，另一条判据偶然翻红也会冒充"量具有牙"。"""
    return any(r[0] == tag and needle in r[1] for r in RESULTS)


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


# ---------------------------------------------------------------- NVDEC 占用
def smi_decoder() -> tuple[float | None, str]:
    """问驱动要"解码器占用"。取不到回 (None, 原因)——**不许拿 0 冒充"取到 0"**。"""
    exe = U.nvidia_smi_path()
    if not exe:
        return None, "找不到 nvidia-smi（这台机器可能没有 N 卡，或驱动没带 CLI）"
    try:
        r = subprocess.run([exe, "--query-gpu=utilization.gpu,utilization.decoder",
                            "--format=csv,noheader,nounits"], capture_output=True,
                           timeout=5.0, stdin=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except BaseException as exc:                 # noqa: BLE001
        return None, f"nvidia-smi 问不动：{type(exc).__name__}: {exc}"
    if r.returncode != 0:
        return None, f"nvidia-smi rc={r.returncode}"
    txt = U._decode(r.stdout)
    line = next((l for l in txt.splitlines() if l.strip()), "")
    cols = [c.strip() for c in line.split(",")]
    if len(cols) < 2:
        return None, f"读数形状不认识：{txt.strip()[:120]!r}"
    dec = U._num(cols[1])
    if dec is None:
        return None, f"这一版驱动不给 decoder 字段（原样 {cols[1]!r}）"
    return float(dec), ""


def gpu_name() -> str:
    exe = U.nvidia_smi_path()
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, timeout=5.0,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return U._decode(r.stdout).splitlines()[0].strip() if r.returncode == 0 else ""
    except BaseException:                        # noqa: BLE001
        return ""


def adapter_from_log(data_dir: str) -> tuple[str, str]:
    """从内核自己的日志里取"渲染落在哪块卡"：`Device Name:` 那一行。

    为什么读日志而不读属性：本机 v0.41.0-1101 没有 `d3d11-adapters`（实测 property
    not found），适配器名只有 vo/gpu/d3d11 的 v 级日志给——那是内核自己说的话，
    比"我们猜它是独显"硬。
    """
    path = os.path.join(data_dir, "logs", "mpv.log")
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for ln in fh:
                if "Device Name:" in ln:
                    return ln.split("Device Name:", 1)[1].strip(), path
    except OSError as exc:
        return "", f"{path}（读不动：{type(exc).__name__}）"
    return "", path + "（没有 Device Name 行）"


# ---------------------------------------------------------------- 采样
def sample_window(pipe: str, pids: dict, secs: float) -> dict:
    """在 secs 秒里同时量：内核进度/掉帧/硬解通路＋NVDEC 占用＋两个进程的 CPU%。

    CPU 必须与进度**同窗**：分开量出来的是两段时间的读数，拿去比"软解 CPU 更高"
    就是拿两次不相干的观察互相作证。
    """
    t0 = time.time()
    a, why_a = ask(pipe, T_PROPS, wait=4.0)
    decs = [d for d in (smi_decoder()[0],) if d is not None]
    cpu, cpu_why = cpu_window({"外壳": pids.get("shell"), "内核": pids.get("mpv")},
                              max(1.0, secs))
    for _ in range(3):
        d, _w = smi_decoder()
        if d is not None:
            decs.append(d)
        time.sleep(0.25)
    wall = time.time() - t0
    b, why_b = ask(pipe, HW_PROPS, wait=4.0)
    return {"start": a or {}, "end": b or {}, "wall": wall,
            "why": [w for w in (why_a, why_b) if w], "dec": decs,
            "cpu": cpu, "cpu_why": cpu_why}


def ratio_of(w: dict) -> str:
    a, b = w["start"].get("time-pos"), w["end"].get("time-pos")
    if num(a) and num(b) and w.get("wall", 0) > 0:
        return f"{(b - a) / w['wall']:.3f}"
    return "没量到"


def ratio_val(w: dict) -> float | None:
    a, b = w["start"].get("time-pos"), w["end"].get("time-pos")
    if num(a) and num(b) and w.get("wall", 0) > 0:
        return (b - a) / w["wall"]
    return None


def delta(w: dict, key: str) -> int | None:
    a, b = w["start"].get(key), w["end"].get(key)
    return int(b - a) if num(a) and num(b) else None


# ---------------------------------------------------------------- 装配
def wait_pipe_free(why: str) -> bool:
    t0 = time.time()
    while time.time() < t0 + FREE_WAIT:
        if pipe_listening(pipe_name()) is False:
            return True
        time.sleep(0.5)
    say("ENV", "上一趟的单实例锁没在窗口期里放开（串行装配的前提）",
        f"{pipe_name()} 仍有人听，等了 {time.time() - t0:.1f}s（{why}）")
    return False


def boot(tag: str, cfg_extra: dict, sample: str) -> dict | None:
    """按"双击这个文件"的形状起**一个**真产品实例；回 None＝环境不给。

    起不来／静音不成立一律记 ENV，**不许**把"没跑成"记成产品的 FAIL。
    """
    if not wait_pipe_free(tag):
        return None
    data = os.path.join(os.environ.get("TEMP", "C:" + chr(92) + "temp"),
                        f"gelu-gpu-{tag}-{os.getpid()}")
    cfg = {"mute": True, "volume": 0, "resume": False}
    cfg.update(cfg_extra)
    env = make_env(data, cfg)
    # 显卡适配器名只有 v 级日志才印（产品默认 all=warn）：为取证临时提级
    env["GELU_MPV_VERBOSE"] = "all=v"
    # 基线必须在**丢出进程之前**取：三趟都开同一支 S_HEVC，上一趟的外壳如果还在收尾的路上，
    # 事后取基线就把它认成"这一趟新起的那个"——于是量的是别人的内核、收拾的是别人的 PID。
    base = {h[0] for h in player_pids()}
    sp = spawn(sample, env)
    pid, cl = wait_pid(sample, base, tag, BOOT_WAIT)
    if not pid:
        say("ENV", f"{tag}：实例进程没起来", cl)
        kill_pids([sp.pid])
        return None
    mpid, mname, mcmd = wait_child(pid, BOOT_WAIT)
    if not mpid:
        say("ENV", f"{tag}：内核（mpv 子进程）没起来", f"pid={pid} 名下没有 mpv")
        kill_pids([pid, sp.pid])
        return None
    pipe = pipe_of(mcmd)
    if not pipe:
        say("ENV", f"{tag}：读不出内核的 IPC 管道名", mcmd[:200])
        kill_pids([mpid, pid, sp.pid])
        return None
    got, why = ask(pipe, LOAD_PROPS, wait=25.0)
    if not got:
        say("ENV", f"{tag}：问内核要属性这条腿跑不动（连不上管道／内核不接客户端）",
            f"{why}；pipe={pipe!r}")
        kill_pids([mpid, pid, sp.pid])
        return None
    if not silent_now(got):
        say("ENV", f"{tag}：静音前提不成立——这一趟不许按下播放",
            f"从被测实例读回 mute={got.get('mute')!r} volume={got.get('volume')!r}；临时目录={data}")
        kill_pids([mpid, pid, sp.pid])
        return None
    say("NOTE", f"{tag}：装配起来了（静音已从被测实例读回，不是进程内垫片）",
        f"外壳 pid={pid} 内核 pid={mpid}({mname}) 管道={pipe} 配置={cfg_extra or '（出厂默认）'}")
    tell(pipe, ["set_property", "pause", False])
    time.sleep(1.0)
    return {"tag": tag, "pid": pid, "mpid": mpid, "pipe": pipe, "data": data,
            "pids": {"shell": pid, "mpv": mpid}, "load": got, "mcmd": mcmd, "dead": False}


def teardown(inst: dict) -> None:
    if inst.get("dead"):
        return
    inst["dead"] = True
    gone, still, skipped = kill_pids([inst["mpid"], inst["pid"]])
    say("PASS" if not still else "FAIL",
        f"{inst['tag']}：收尾只按显式 PID 收拾自己起的外壳与内核",
        f"terminate={gone} 仍在={still} 跳过={skipped}")
    import shutil
    shutil.rmtree(inst["data"], ignore_errors=True)


# ---------------------------------------------------------------- 主流程
def run() -> int:
    global OUT_PATH
    OUT_PATH = os.path.join(ROOT, "tests", "out", "e2e-gpu-"
                            + datetime.datetime.now().strftime("%m%d-%H%M%S") + ".log")
    say("NOTE", "日志落这里", OUT_PATH)
    t_all = time.time()

    # ---------------- 量具先自查 ----------------
    bad = silent_gate_selftest()
    say("PASS" if not bad else "FAIL",
        "静音闸门自带 8 档已知好坏对照（判据不许恒真/恒假）",
        "八档全对" if not bad else "漏网：" + " ‖ ".join(bad))
    if bad:
        return finish(1)

    # ---------------- 环境闸门 ----------------
    if sys.platform != "win32":
        say("ENV", "只在 Windows 上跑（进程树＋命名管道＋nvidia-smi 是这一路的主角）", sys.platform)
        return finish(2)
    if psutil is None:
        say("ENV", "进程核对能力（psutil）", PSUTIL_ERR)
        return finish(2)
    if cfgmod is None or APP_ERR:
        say("ENV", "app.config / app.engine.ipc 起不来", APP_ERR)
        return finish(2)
    if not os.path.exists(cfgmod.MPV_EXE):
        say("ENV", "播放内核在不在（这一趟要真起三次内核）", cfgmod.MPV_EXE)
        return finish(2)
    for p in (S_HEVC, S_H264, S_AV1):
        if not os.path.isfile(p):
            say("ENV", "4K 样片在不在（用 runtime/ffmpeg 现合成，见 tests/out/mk4k.log）", p)
            return finish(2)
    if pipe_listening(pipe_name()):
        say("ENV", "前置条件：本机没有别的实例在听这根管道",
            f"{pipe_name()} 已经有人在听——双击会交给**别人**的播放器，这一轮不作数")
        return finish(2)
    say("PASS", "前置条件：管道没人听（这一趟起的外壳是这台机器上仅有的播放器）", pipe_name())
    n, why = smi_decoder()
    say("PASS" if n is not None else "ENV", "NVDEC 这把尺问得到吗",
        f"当前 utilization.decoder={n!r}（{U.nvidia_smi_path()}）" if n is not None
        else f"{why} —— 解码器占用那条只能记『未测』")
    if n is None:
        return finish(2)
    say("NOTE", "样片与内核",
        f"{os.path.basename(S_HEVC)}／{os.path.basename(S_H264)}／{os.path.basename(S_AV1)} "
        f"都是 3840×2160@60（ffmpeg 现合成）；内核={os.path.relpath(cfgmod.MPV_EXE, ROOT)}；"
        f"实测属性名清单=tests/out/probe-gpu-chain.txt")

    pending: list[dict] = []
    try:
        # ================= ① 出厂默认档 =================
        dflt = boot("①默认档", {"hwdec": perf.DEFAULTS["hwdec"],
                                "gpu_api": perf.DEFAULTS["gpu_api"]}, S_HEVC)
        if dflt is None:
            return finish(2)
        pending.append(dflt)
        L = dflt["load"]
        ok_load = (os.path.normcase(str(L.get("path") or "")) == os.path.normcase(S_HEVC)
                   and L.get("width") == 3840 and L.get("height") == 2160)
        say("PASS" if ok_load else "FAIL",
            "①：双击 4K 片之后内核报的就是这支片、分辨率真的是 3840×2160",
            f"path={os.path.basename(str(L.get('path')))!r} {L.get('width')}×{L.get('height')} "
            f"{L.get('video-format')} pos={L.get('playlist-pos')!r} 列表={L.get('playlist-count')!r}")

        w1 = sample_window(dflt["pipe"], dflt["pids"], MEASURE)
        r1 = ratio_val(w1)
        d1 = delta(w1, "frame-drop-count")
        say("PASS" if r1 is not None and r1 >= RT_MIN else "FAIL",
            f"①：4K60 实时（窗口内进度推进率 ≥ {RT_MIN:.2f}×墙钟）",
            f"time-pos {w1['start'].get('time-pos')!r}→{w1['end'].get('time-pos')!r}／墙钟 "
            f"{w1['wall']:.2f}s → 推进率={ratio_of(w1)} vf-fps="
            f"{w1['end'].get('estimated-vf-fps')!r} 问不动={w1['why'] or '（没）'}")
        say("PASS" if d1 is not None and d1 <= DROP_MAX else "FAIL",
            f"①：窗口内画面掉帧增量 ≤ {DROP_MAX}（360 帧的约 8%）",
            f"增量={d1} 解码器丢帧增量={delta(w1, 'decoder-frame-drop-count')}")

        raw1 = w1["end"].get("hwdec-current")
        hw1, txt1 = perf.hwdec_state(raw1)
        say("PASS" if hw1 else "FAIL",
            "①：默认档真的在**硬件解码**（读内核回报的 hwdec-current，不读配置里那个愿望）",
            f"hwdec-current={raw1!r} → {txt1}｜请求值 hwdec={w1['end'].get('hwdec')!r}"
            f" options/hwdec={w1['end'].get('options/hwdec')!r}（都是回显，不作证据）｜"
            f"解释函数=app.engine.perf.hwdec_state（与产品同源）")
        ctx = w1["end"].get("current-gpu-context")
        say("PASS" if ctx == perf.DEFAULTS["gpu_api"] else "FAIL",
            f"①：GPU 通路真的落在配置的 {perf.DEFAULTS['gpu_api']}（d3d11va 零拷贝那条）",
            f"current-gpu-context={ctx!r} current-vo={w1['end'].get('current-vo')!r}")
        say("PASS" if w1["dec"] and max(w1["dec"]) > 0 else "FAIL",
            "①：NVDEC 硬件真在动（问驱动要 utilization.decoder，窗口内峰值 > 0）",
            f"样本={w1['dec']} 峰值={max(w1['dec']) if w1['dec'] else '未采'}"
            "｜这条的牙在②：软解那趟必须量到 0")
        say("NOTE", "①：同窗 CPU 实测（外壳＋内核，单核基准 %）",
            f"{w1['cpu']} 窗口={w1['wall']:.2f}s {w1['cpu_why'] or ''}")

        ad, ad_path = adapter_from_log(dflt["data"])
        gn = gpu_name()
        say("PASS" if ad else "NOTE",
            "①：『跑在哪块卡上』有实测答案（内核自己日志里的适配器名）",
            f"日志适配器={ad!r}｜nvidia-smi 首卡={gn!r}｜出处={os.path.relpath(ad_path, ROOT)}")
        if ad and gn:
            same = ad.lower() in gn.lower() or gn.lower() in ad.lower()
            say("PASS" if same else "NOTE",
                "①：渲染与硬解落在**独显**（日志里的适配器与驱动报的那张同名）",
                f"{ad!r} vs {gn!r}"
                + ("" if same else " —— 落点不是独显：那是这台机器的接线，不是缺陷"))

        # =============== ①补：同一趟里换片，启动档位不许掉 ===============
        for s2 in (S_H264, S_AV1):
            nm = os.path.basename(s2)
            tell(dflt["pipe"], ["loadfile", s2, "replace"])
            g2, why2, rounds = wait_props(
                dflt["pipe"], ["path", "width", "video-format", "hwdec-current"], 30, 0.6,
                lambda g, want=s2: os.path.normcase(str(g.get("path") or "")) == os.path.normcase(want))
            raw2 = (g2 or {}).get("hwdec-current")
            hw2, txt2 = perf.hwdec_state(raw2)
            say("PASS" if hw2 else "NOTE",
                f"①补：换成 {nm} 之后硬解通路还在（换片不重新解析启动参数）",
                f"hwdec-current={raw2!r} → {txt2}｜{g2.get('video-format')!r} "
                f"{g2.get('width')}×｜等了 {rounds} 轮｜问不动={why2 or '（没）'}")
            wx = sample_window(dflt["pipe"], dflt["pids"], 4.0)
            say("NOTE", f"①补：{nm} 同窗读数",
                f"hwdec-current={wx['end'].get('hwdec-current')!r} 推进率={ratio_of(wx)} "
                f"掉帧增量={delta(wx, 'frame-drop-count')} decoder 峰值="
                f"{max(wx['dec']) if wx['dec'] else '未采'} cpu={wx['cpu']}")

        teardown(dflt)

        # ================= ② 强制软解：①那批判据的牙 =================
        soft_cfg = {"hwdec": "no", "gpu_api": perf.DEFAULTS["gpu_api"]}
        soft = boot("②强制软解对照", soft_cfg, S_HEVC)
        if soft is None:
            return finish(2)
        pending.append(soft)
        w2 = sample_window(soft["pipe"], soft["pids"], MEASURE)
        raw_s = w2["end"].get("hwdec-current")
        hw_s, txt_s = perf.hwdec_state(raw_s)
        if EXPECT_RED:
            # 反向对照：装配就是"强制软解"，**期望**换成①那句"默认档真的在硬件解码"。
            # 配置写着 hwdec=no，判据却要求硬解 → 这条必须红；它绿就说明①那条是恒真废尺。
            say("PASS" if hw_s else "FAIL",
                "①（挪到软解装配上）：默认档真的在**硬件解码**——反向对照里这条**必须红**",
                f"这趟配置={soft_cfg}｜hwdec-current={raw_s!r} → {txt_s}")
        say("PASS" if not hw_s else "FAIL",
            "②：配置 hwdec=no 时内核回报的是软件解码（证明①那条判据不是恒真）",
            f"hwdec-current={raw_s!r} → {txt_s}｜请求值 hwdec={w2['end'].get('hwdec')!r} "
            f"options/hwdec={w2['end'].get('options/hwdec')!r}")
        teeth = bool(w1["dec"]) and bool(w2["dec"]) and max(w1["dec"]) > 0 and max(w2["dec"]) == 0
        if w1["dec"] and w2["dec"]:
            say("PASS" if teeth else "NOTE",
                "②：软解那趟 NVDEC 占用归零——这把尺**有**鉴别力（①那条绿不是白给的）",
                f"①峰值={max(w1['dec'])} ②峰值={max(w2['dec'])}"
                + ("" if teeth else " —— 两档拉不开：①那条降级为佐证，不能当证据"))
        else:
            say("NOTE", "②：NVDEC 这把尺这趟没采到数", f"①={w1['dec']} ②={w2['dec']}（只入档）")
        c1, c2 = w1["cpu"].get("内核"), w2["cpu"].get("内核")
        if c1 is not None and c2 is not None:
            say("PASS" if c2 > c1 else "NOTE",
                "②：软解的内核 CPU 高于硬解（同一支 4K、同一种量法）",
                f"①硬解={c1:.1f}% ②软解={c2:.1f}% 差={c2 - c1:+.1f} 个百分点"
                + ("" if c2 > c1 else " —— 方向不对：这支片子软解也扛得住，CPU 差不足以作证（如实记下）"))
        else:
            say("NOTE", "②：CPU 对照没量成（psutil 那一侧）",
                f"①={w1['cpu']} ②={w2['cpu']}｜{w2['cpu_why']}")
        say("NOTE", "②：软解那趟的实时性与掉帧（4K60 卡不卡）",
            f"推进率={ratio_of(w2)} 掉帧增量={delta(w2, 'frame-drop-count')} "
            f"vf-fps={w2['end'].get('estimated-vf-fps')!r} 外壳 CPU={w2['cpu'].get('外壳')}")
        teardown(soft)

        # ================= ③ 坏档位：白名单是不是承重件 =================
        bogus = boot("③坏档位", {"hwdec": "bogus-value",
                                 "gpu_api": perf.DEFAULTS["gpu_api"]}, S_HEVC)
        if bogus is None:
            return finish(2)
        pending.append(bogus)
        w3 = sample_window(bogus["pipe"], bogus["pids"], 4.0)
        opt = w3["end"].get("options/hwdec")
        opt_s = opt[0] if isinstance(opt, list) and opt else opt
        raw3 = w3["end"].get("hwdec-current")
        hw3, txt3 = perf.hwdec_state(raw3)
        say("PASS" if opt_s == perf.DEFAULTS["hwdec"] else "FAIL",
            "③：配置里的坏档位没被原样递给内核（白名单拦下并回退默认档）",
            f"内核收到的 options/hwdec={opt!r} 期望={perf.DEFAULTS['hwdec']!r}"
            "｜拦不住的话内核照样启动、只是静默退软解，用户只知道『变卡了』")
        say("PASS" if hw3 else "FAIL",
            "③：回退之后硬解仍在跑（钉死『白名单是装饰』那种写法）",
            f"hwdec-current={raw3!r} → {txt3}｜推进率={ratio_of(w3)}｜decoder 峰值="
            f"{max(w3['dec']) if w3['dec'] else '未采'}")
        teardown(bogus)

        rc = 1 if count("FAIL") else (2 if count("ENV") else 0)
        return finish(rc, t_all)
    finally:
        for inst in pending:
            try:
                teardown(inst)
            except BaseException as exc:         # noqa: BLE001 - 收尾失败不许把整趟崩掉
                say("NOTE", f"{inst['tag']}：收尾报", f"{type(exc).__name__}: {exc}")
        left = player_pids()
        say("PASS" if not left else "NOTE",
            "这台机器上没有留下本趟起的外壳进程（别人家的一律不动）",
            "零残留" if not left else f"残留={[(p, c[:70]) for p, c in left]}")


def finish(rc: int, t0: float | None = None) -> int:
    print("DONE", flush=True)
    _tail("DONE")
    if EXPECT_RED:
        ok = red("FAIL", "反向对照")
        say("FINAL", f"反向对照：{'成立（判据真的会红）' if ok else '失败（喂了 hwdec=no 还全绿＝那把尺是瞎的）'}",
            f"哨兵那条={'红了' if ok else '没红'}／FAIL {count('FAIL')} 条／ENV {count('ENV')} 条 "
            f"rc={0 if ok else 1}")
        return 0 if ok else 1
    tag = "全绿" if rc == 0 else ("有真失败" if rc == 1 else "环境不给")
    dur = f"耗时 {time.time() - t0:.1f}s" if t0 else "没起进程"
    say("FINAL", f"GPU 加速端到端：{tag} —— PASS {count('PASS')}／FAIL {count('FAIL')}／"
        f"ENV {count('ENV')}／NOTE {count('NOTE')}", f"{dur} rc={rc}")
    return rc


def _tail(line: str) -> None:
    if not OUT_PATH:
        return
    try:
        with open(OUT_PATH, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        print("TRACEBACK（这一趟不作数）:", flush=True)
        traceback.print_exc()
        say("FINAL", "量具自己崩了 —— GPU 实测不作数", "rc=2")
        print("DONE", flush=True)
        _tail("DONE")
        sys.exit(2)
