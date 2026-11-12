"""#86 前置的小探测：**空列表上追加到底会不会自己播起来**（决定"添加"那条怎么修）。

为什么要单独量这一条：修"播放列表上的添加＝整表替换"时，改法有两条路——
  (a) 把接线改成 `replace=False`（`loadfile … append`）就完事；
  (b) 追加之外还得管"列表本来是空的"那一档，否则空列表上点"添加"会变成"文件进列表了但不动"。
走哪条不取决于文档怎么写，取决于**这台机器上这个内核**在 `--idle=yes` 下
收到 `append` / `append-play` 会不会把它变成当前条目。

第一版这趟翻过一次车，值得记下来：为了"不出画面不出声"加了 `--video=no --no-audio`，
结果 `append`/`replace` 之后 `playlist-pos` 恒为 −1、`idle-active` 恒为 True，
看起来像"追加不会自己播"——其实那条片子压根没被解码（**没画面可播，位置当然不动**）。
判据的前提自己没立住。所以这一版改回**能真播**的形状：留视频、只断声音
（`--no-audio`），窗口会亮几秒，这是量的代价。
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

from player_harness import ask_soft, kill_pids, tell  # noqa: E402

MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
S1 = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
S2 = os.path.join(HERE, "samples", "gelu-sample-2.mp4")
PROPS = ["idle-active", "playlist-count", "playlist-pos", "path", "pause",
         "playback-time", "duration", "eof-reached", "video-format"]


def read(pipe: str, tag: str) -> dict:
    got, miss = ask_soft(pipe, PROPS, wait=4.0)
    print(f"[probe] {tag}：{got}" + (f" 读不到={miss}" if miss else ""), flush=True)
    return got


def main() -> int:
    pipe = rf"\\.\pipe\gelu-probe-append-{int(time.time())}"
    cmd = [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
           "--idle=yes", "--force-window=yes", "--keep-open=yes",
           "--no-audio", "--mute=yes", "--volume=0", "--really-quiet"]
    p = subprocess.Popen(cmd, cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"[probe] 空载内核 pid={p.pid} pipe={pipe}（有画面、无声音）", flush=True)
    try:
        a = read(pipe, "起步（没喂文件）")
        if not a:
            return 2
        print(f"[probe] 前提检查：idle-active={a.get('idle-active')!r} "
              f"count={a.get('playlist-count')!r} pos={a.get('playlist-pos')!r}", flush=True)

        tell(pipe, ["loadfile", S1, "append"])
        time.sleep(2.5)
        b = read(pipe, "空列表 loadfile … append")
        t1 = b.get("playback-time")
        time.sleep(2.0)
        b2 = read(pipe, "  ↑ 2 秒后再看位置")
        append_plays = (b.get("playlist-pos") == 0 and isinstance(b2.get("playback-time"), (int, float))
                        and isinstance(t1, (int, float)) and b2["playback-time"] > t1)
        print(f"[probe] → 空列表 append **会**自己播吗：{append_plays}", flush=True)

        # 清干净再来一遍，测 append-play
        tell(pipe, ["playlist-clear"])
        time.sleep(1.0)
        c0 = read(pipe, "playlist-clear 之后")
        tell(pipe, ["loadfile", S2, "append-play"])
        time.sleep(2.5)
        c = read(pipe, "空列表 loadfile … append-play")
        t2 = c.get("playback-time")
        time.sleep(2.0)
        c2 = read(pipe, "  ↑ 2 秒后再看位置")
        ap_plays = (c.get("playlist-pos") == 0 and isinstance(c2.get("playback-time"), (int, float))
                    and isinstance(t2, (int, float)) and c2["playback-time"] > t2)
        print(f"[probe] → 空列表 append-play **会**自己播吗：{ap_plays}（clear 后 {c0}）", flush=True)

        # 已经在播了再追加一条：当前条目不许被抢走
        tell(pipe, ["loadfile", S1, "append-play"])
        time.sleep(2.0)
        d = read(pipe, "播放中再 append-play 第二条")
        print("\n[probe] —— 结论 ——", flush=True)
        print(f"  空列表 append：pos={b.get('playlist-pos')!r} 动没动="
              f"{'动了' if append_plays else '没动'}", flush=True)
        print(f"  空列表 append-play：pos={c.get('playlist-pos')!r} 动没动="
              f"{'动了' if ap_plays else '没动'}", flush=True)
        print(f"  播放中追加：pos={d.get('playlist-pos')!r} count={d.get('playlist-count')!r} "
              f"当前={os.path.basename(str(d.get('path')))}", flush=True)
        print(f"  产品侧该怎么写：{'append-play 一个词就够' if ap_plays else '追加时要自己判空列表并补 playlist-pos'}",
              flush=True)
        return 0
    finally:
        tell(pipe, ["quit"])
        time.sleep(1.0)
        kill_pids([p.pid])
        print(f"[probe] 收尾：pid={p.pid} 退出码={p.poll()!r}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
