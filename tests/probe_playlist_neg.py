"""量一件小事：播放列表右键菜单在空白处读出 row=-1 之后，把 -1 发给内核会怎样。

为什么单独量：`QListWidget.row(None)` 在本机 PyQt5 5.15.2 上**不抛**，回 -1
（先跑过一版证伪，旧写法的"崩"没成立）。那空白处的「播放/从列表移除」到底
坏在哪，只能问内核：`playlist-pos -1` 是跳到哪、`playlist-remove -1` 会不会
误删末条。菜单的可否点击就按这条读数来写文案。

全程 --vo=null --ao=null，无窗口无声。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from player_harness import ask_soft, tell  # noqa: E402

MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
S1 = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
S2 = os.path.join(HERE, "samples", "gelu-sample-2.mp4")
PROPS = ["playlist-count", "playlist-pos", "idle-active", "path"]


def read(pipe: str, tag: str) -> dict:
    got, miss = ask_soft(pipe, PROPS, wait=5.0)
    print(f"[probe] {tag}：count={got.get('playlist-count')} pos={got.get('playlist-pos')} "
          f"idle={got.get('idle-active')} path={os.path.basename(str(got.get('path') or ''))}"
          + (f" 读不到={miss}" if miss else ""), flush=True)
    return got


def main() -> int:
    pipe = rf"\\.\pipe\gelu-probe-plneg-{int(time.time())}"
    cmd = [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
           "--idle=yes", "--vo=null", "--ao=null", "--really-quiet"]
    p = subprocess.Popen(cmd, cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"[probe] pid={p.pid} pipe={pipe}", flush=True)
    try:
        a = read(pipe, "起步")
        if not a:
            return 2
        for f in (S1, S2):
            print(f"[probe] loadfile append {os.path.basename(f)} → {tell(pipe, ['loadfile', f, 'append'])}")
        time.sleep(1.2)
        b = read(pipe, "加了两条之后")
        print(f"[probe] set playlist-pos -1 → {tell(pipe, ['set_property', 'playlist-pos', -1])}")
        time.sleep(0.6)
        c = read(pipe, "  ↑ 之后")
        print(f"[probe] playlist-remove -1 → {tell(pipe, ['playlist-remove', -1])}")
        time.sleep(0.8)
        d = read(pipe, "  ↑ 之后")
        print(f"[probe] 对照：playlist-remove 99 → {tell(pipe, ['playlist-remove', 99])}")
        time.sleep(0.8)
        e = read(pipe, "  ↑ 之后（对照）")
        print("\n[probe] 判读：")
        print(f"  pos -1 是否把当前条目弄没/跳走：{b.get('playlist-pos')} → {c.get('playlist-pos')}"
              f"（条数 {b.get('playlist-count')} → {c.get('playlist-count')}）")
        print(f"  remove -1 是否真删了东西：条数 {c.get('playlist-count')} → {d.get('playlist-count')}")
        print(f"  越界 99 的对照：条数 {d.get('playlist-count')} → {e.get('playlist-count')}")
        return 0
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        print(f"[probe] 已按显式 PID {p.pid} 收掉", flush=True)


if __name__ == "__main__":
    sys.exit(main())
