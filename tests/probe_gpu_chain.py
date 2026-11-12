"""#86 的前置探测：真产品路径（--wid 嵌入）下，硬解到底落在哪个属性、哪块卡上。

为什么不沿用 docs/design.md §3 第 7 条那批读数：那批是**裸起 mpv** 量的，
而产品起的是 `--wid=<hwnd> --vo=gpu --gpu-api=d3d11` 的嵌入实例，两者不是同一条
渲染通路（docs 里也自己写了这个缺口）。这趟就是把缺口填上：属性名、回报形状、
日志里选中的显卡适配器，全部从**真产品进程**里取，取到了才写进 e2e 的判据。

只探测、不判决（打印到 stdout，GBK 控制台下由调用方重定向到文件再读）。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from player_harness import (APP_ERR, PSUTIL_ERR, ask, cfgmod, kill_pids,  # noqa: E402
                            make_env, mpv_child, pipe_of, player_pids, psutil, spawn,
                            tell, wait_pid, silent_now)

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "samples", "gelu-sample-1.mp4")
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "probe-gpu-data")

# 候选属性：perf.py 断言"存在"的两条 + 裸起时想当然会写的几条 + 统计页在用的几条
CANDIDATES = [
    "hwdec-current", "current-gpu-context", "hw-decode", "current-decoder",
    "current-hwdec", "hwdec", "options/hwdec", "decoder-desc", "current-vo",
    "video-codec", "video-format", "width", "height", "video-out-params/decoder-desc",
    "estimated-vf-fps", "demuxer-cache-state", "frame-drop-count",
    "decoder-frame-drop-count", "vo-timing", "container-fps", "path", "pause",
    "time-pos", "playback-time", "duration", "eof-replaced", "idle-active",
    "playlist-count", "mute", "volume", "d3d11-adapters", "gpu-context",
]


def ask_one(pipe: str, prop: str) -> str:
    """单条问：回值就印值，报错就印"✗ 原因"。逐条问是因为 ask() 一片里有一条不存在
    就会整片失败——那正是"属性名猜错"与"内核没连上"分不开的原因。"""
    got, why = ask(pipe, [prop], wait=2.0)
    if got:
        v = got.get(prop)
        s = repr(v)
        return f"OK  {s[:300]}"
    return f"✗   {(why or '')[:160]}"


def run() -> int:
    print(f"[probe] psutil={PSUTIL_ERR or 'ok'}  app={APP_ERR or 'ok'}")
    if psutil is None or cfgmod is None:
        print("[probe] 依赖不齐，这趟不作数")
        return 2
    base = {p for p, _ in player_pids()}
    if base:
        print(f"[probe] 已有实例在跑 {sorted(base)}：先按显式 PID 收拾干净再探测")
        kill_pids(sorted(base))

    env = make_env(DATA, {"mute": True, "volume": 0, "hwdec": "auto-safe",
                          "gpu_api": "d3d11", "quality_preset": "balanced",
                          "volume_max": 200, "resume": False})
    # 探测期把内核日志提到 v：显卡适配器名只有 v 级才印（产品默认 all=warn）
    env["GELU_MPV_VERBOSE"] = "all=v"
    proc = spawn(SAMPLE, env)
    print(f"[probe] 起了产品实例 pid={proc.pid}")
    pid, cl = wait_pid(SAMPLE, set(), "探测", 40.0)
    if not pid:
        print(f"[probe] {cl}")
        kill_pids([proc.pid])
        return 2
    mpv_pid, mpv_name, mpv_cl = wait_child_or_none(pid)
    if not mpv_pid:
        print("[probe] 没找到 mpv 子进程")
        kill_pids([pid, proc.pid])
        return 2
    pipe = pipe_of(mpv_cl)
    print(f"[probe] 实例 pid={pid} 内核 pid={mpv_pid}({mpv_name}) 管道={pipe}")
    print(f"[probe] 内核命令行={mpv_cl[:600]}")

    props = ["path", "pause", "idle-active", "mute", "volume", "playlist-count"]
    got, why = ask(pipe, props, wait=20.0)
    print(f"[probe] 起步读数 {got} 原因={why}")
    if not got or got.get("idle-active") is not False:
        print("[probe] 文件没真的装载（idle-active 还是 True），后面读数都不作数")
        kill_pids([mpv_pid, pid, proc.pid])
        return 2
    # 静音必须从**这个实例**自己重读（跨进程/跨重启的"上次已静音"不作数）：
    # 这趟会真的按下播放键，没静音就是扰民。
    if not silent_now(got):
        print(f"[probe] 这个实例没静音（{got}）：不按下播放，整趟判 ENV")
        kill_pids([mpv_pid, pid, proc.pid])
        return 2
    tell(pipe, ["set_property", "pause", False])
    time.sleep(3.0)

    print("[probe] —— 属性逐个问 ——")
    for p in CANDIDATES:
        print(f"  {p:38s} {ask_one(pipe, p)}")

    print("[probe] —— 内核日志里跟显卡/解码有关的行 ——")
    log = os.path.join(DATA, "logs", "mpv.log")
    if os.path.exists(log):
        with open(log, "r", encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        keys = ("adapter", "Adapter", "d3d11", "D3D11", "hwdec", "Hardware",
                "decoder", "nvdec", "cuda", "gpu-context", "VO: [gpu]", "libplacebo",
                "device", "NVIDIA", "Intel")
        hits = [ln.strip() for ln in text.splitlines()
                if any(k in ln for k in keys)]
        print(f"[probe] 日志 {len(text)} 字节，命中 {len(hits)} 行：")
        for ln in hits[:80]:
            print("   | " + ln[:220])
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "out",
                               "probe-gpu-mpv.log"), "w", encoding="utf-8",
                  newline="\n") as fh:
            fh.write(text)
    else:
        print(f"[probe] 没有日志文件 {log}")

    kill_pids([mpv_pid, pid, proc.pid])
    print("[probe] 已按显式 PID 收拾")
    return 0


def wait_child_or_none(pid: int):
    t0 = time.time()
    while time.time() < t0 + 30.0:
        hit = mpv_child(pid)
        if hit[0]:
            return hit
        time.sleep(0.4)
    return (0, "", "")


if __name__ == "__main__":
    sys.exit(run())
