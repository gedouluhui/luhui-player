"""无头探针：滚轮绑定把动作回灌给界面时，消息里的值可不可信。

背景：滚轮事件只落在 mpv 的子窗口上，外壳收不到，所以界面要知道"滚了多少"只能让
input.conf 发 `script-message` 回来。这里验证最终要落进 `_write_input_conf` 的四行：

  ① 音量消息带的是 `${volume}` 展开值。实测命令列表是**逐条执行**的，展开发生在
     第二条执行时，所以拿到的是**加完之后**的数。这很关键：`client-message` 事件先于
     `volume` 的属性推送到达（本探针同时记两条事件流来证明），照缓存画 OSD 会永远慢一格。
  ② 连发（60ms 一格，模拟快速滚轮）：每一格消息里的绝对值都等于内核当时的音量，
     最后一格等于终值——OSD 拿它直接显示，不需要自己推算。
  ③ Ctrl+滚轮的 seek 消息带方向，且内核真的跳了 5 秒。

全程 `--vo=null --ao=null`，无窗口、无声音；用 `keypress WHEEL_*` 走真实的 input.conf。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from app.engine.ipc import MpvIpc                      # noqa: E402
from player_harness import tell                        # noqa: E402

MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
N = 12
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def main() -> int:
    stamp = int(time.time() * 1000)
    pipe = rf"\\.\pipe\gelu-probe-wheel-{stamp}"
    conf = os.path.join(HERE, "out", f"probe-wheel-input-{stamp}.conf")
    os.makedirs(os.path.dirname(conf), exist_ok=True)
    # 与 app/engine/player.py 的 _write_input_conf 逐字一致（改那边要同步改这里）
    with open(conf, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("WHEEL_UP        add volume 2;  script-message luhui-wheel vol ${volume}\n"
                 "WHEEL_DOWN      add volume -2; script-message luhui-wheel vol ${volume}\n"
                 "CTRL+WHEEL_UP   seek  5 relative+exact; script-message luhui-wheel seek +5\n"
                 "CTRL+WHEEL_DOWN seek -5 relative+exact; script-message luhui-wheel seek -5\n")
    p = subprocess.Popen(
        [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
         "--vo=null", "--ao=null", "--no-audio", "--idle=yes", "--keep-open=yes",
         "--pause=yes", "--volume=40", "--volume-max=200", "--really-quiet",
         f"--input-conf={conf}"],
        cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ipc = MpvIpc(pipe)
    events: list[tuple[float, str, object]] = []
    lock = threading.Lock()
    ipc.observe("volume", lambda v: _rec(events, lock, "volume", v))
    ipc.on("client-message", lambda m: _rec(events, lock, "msg", (m.get("args") or [])))
    try:
        if not ipc.connect(timeout=10.0, proc=p):
            print("[probe] 连不上 IPC", flush=True)
            return 2
        ipc.reobserve_all()      # observe() 在连上之前只记名字，这里才真发 observe_property
        tell(pipe, ["loadfile", SAMPLE, "replace"])
        time.sleep(1.2)
        base = float(ipc.get("volume"))
        print(f"[probe] 起点 volume={base}", flush=True)

        # ① 单格：消息里是不是加完之后的绝对值；两条事件流谁先到
        with lock:
            events.clear()
        tell(pipe, ["keypress", "WHEEL_UP"])
        time.sleep(0.35)
        with lock:
            seq = list(events)
        vol = float(ipc.get("volume"))
        msgs = [v for _, k, v in seq if k == "msg"]
        kinds = [k for _, k, _ in seq]
        first = (msgs[0] if msgs else [])
        check("滚一格：消息里是 ${volume} 展开后的绝对值（42，不是旧的 40 或 '+2'）",
              first[:3] == ["luhui-wheel", "vol", "42"] and abs(vol - (base + 2)) < 1e-6,
              f"消息={first} 事件序={kinds} volume={vol}")
        check("时序事实：client-message 先于 volume 推送（所以 OSD 不能读缓存）",
              kinds[:2] == ["msg", "volume"], f"序={kinds[:2]}")

        # ② 连发 N 格：每格消息的绝对值都该与内核同步推进
        with lock:
            events.clear()
        for _ in range(N):
            tell(pipe, ["keypress", "WHEEL_UP"])
            time.sleep(0.06)
        time.sleep(0.5)
        with lock:
            seq2 = list(events)
        vol2 = float(ipc.get("volume"))
        vals = [float(v[2]) for _, k, v in seq2
                if k == "msg" and (v or [])[:2] == ["luhui-wheel", "vol"]]
        expect = base + 2 * (N + 1)
        check("连发 12 格：每格消息都是绝对音量且逐格 +2，最后一格=终值",
              len(vals) == N and vals[-1] == vol2 and abs(vol2 - expect) < 1e-6
              and all(b - a == 2 for a, b in zip(vals, vals[1:])),
              f"消息={vals} 终值={vol2} 期望={expect}")

        # ③ Ctrl+滚轮：seek 方向 + 真跳了 5 秒
        tell(pipe, ["set_property", "time-pos", 1.0])
        time.sleep(0.4)
        with lock:
            events.clear()
        t0 = float(ipc.get("time-pos") or 0)
        tell(pipe, ["keypress", "CTRL+WHEEL_UP"])
        time.sleep(0.6)
        with lock:
            seq3 = list(events)
        t1 = float(ipc.get("time-pos") or 0)
        seeks = [v for _, k, v in seq3 if k == "msg" and (v or [])[:2] == ["luhui-wheel", "seek"]]
        check("Ctrl+滚轮：消息带方向，且内核真跳了约 5 秒",
              bool(seeks) and seeks[0][2] == "+5" and 4.0 < (t1 - t0) < 6.0,
              f"消息={seeks} time-pos {t0:.2f}→{t1:.2f}")

        n_fail = sum(1 for _, ok, _ in results if not ok)
        print(f"\n---- 汇总：{len(results) - n_fail}/{len(results)} 通过"
              + (f"；失败：{[n for n, ok, _ in results if not ok]}" if n_fail else "") + " ----",
              flush=True)
        return 1 if n_fail else 0
    finally:
        try:
            tell(pipe, ["quit"])
        except Exception:
            pass
        time.sleep(0.3)
        try:
            p.kill()
        except Exception:
            pass
        ipc.close()
        try:
            os.remove(conf)
        except OSError:
            pass


def _rec(events, lock, kind, value) -> None:
    with lock:
        events.append((time.perf_counter(), kind, value))


if __name__ == "__main__":
    raise SystemExit(main())
