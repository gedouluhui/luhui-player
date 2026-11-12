"""无头探针：`playlist-move` 的索引语义 + `script-message` 会不会回灌给 IPC 客户端。

两件事都是"产品要依赖、但文档没写清"的内核行为，所以先量再写：

  ① 拖拽排序要靠 `playlist-move <from> <to>`。to 是"移除之前的目标位置"还是
     "移除之后的目标位置"？把 0 移到 2 得到 [b,c,a] 还是 [b,a,c]？边界（移到末尾、
     原地、越界）各是什么结局？不先钉住，界面就会"松手之后顺序差一位"。
  ② 滚轮落在 mpv 的子窗口上（外壳收不到），想让界面冒 OSD 就得让 input.conf 里那几行
     发一条消息回来。`script-message` 到底送不送给**外部 IPC 客户端**、还是只给
     Lua 脚本？用 `keypress WHEEL_UP` 走真实的 input.conf 那条路来验，不靠猜。

全程 `--vo=null --ao=null`、无窗口、无声音；样本文件只用来占住 playlist 的条目。
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
SAMPLES = [os.path.join(HERE, "samples", "gelu-sample-1.mp4"),
           os.path.join(HERE, "samples", "gelu-sample-2.mp4"),
           os.path.join(HERE, "samples", "gelu-sample-3.mkv")]
WANT = [os.path.basename(s) for s in SAMPLES]


def names(pipe: str, ipc: MpvIpc) -> list[str]:
    pl = ipc.get("playlist") or []
    return [os.path.basename(str(e.get("filename") or "")) for e in pl]


def main() -> int:
    stamp = int(time.time() * 1000)
    pipe = rf"\\.\pipe\gelu-probe-move-{stamp}"
    conf = os.path.join(HERE, "out", f"probe-move-input-{stamp}.conf")
    os.makedirs(os.path.dirname(conf), exist_ok=True)
    with open(conf, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("WHEEL_UP   script-message luhui-wheel vol +2\n"
                 "WHEEL_DOWN script-message luhui-wheel vol -2\n"
                 "CTRL+WHEEL_UP seek 5 absolute+exact; script-message luhui-wheel seek +5\n")
    p = subprocess.Popen(
        [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
         "--vo=null", "--ao=null", "--no-audio", "--idle=yes", "--keep-open=yes",
         "--pause=yes", "--volume=0", "--really-quiet", f"--input-conf={conf}"],
        cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"[probe] 内核 pid={p.pid}（vo=null ao=null，无窗口）", flush=True)
    ipc = MpvIpc(pipe)
    messages: list[dict] = []
    ipc.on("client-message", lambda m: messages.append(dict(m)))
    try:
        if not ipc.connect(timeout=10.0, proc=p):
            print("[probe] 连不上 IPC", flush=True)
            return 2
        for i, sample in enumerate(SAMPLES):
            tell(pipe, ["loadfile", sample, "replace" if i == 0 else "append"])
        time.sleep(1.5)
        print(f"[probe] ①起点 playlist={names(pipe, ipc)}", flush=True)

        cases = [(0, 1), (0, 2), (2, 0), (1, 3), (0, 0), (0, 9)]
        for frm, to in cases:
            tele = ipc.get("playlist")
            if not tele:
                print("[probe] playlist 读不到", flush=True)
                return 3
            n = len(tele)
            if frm >= n:
                continue
            print(f"[probe] ②playlist-move {frm} {to}（当前 n={n}）…", flush=True)
            tell(pipe, ["playlist-move", frm, to])
            time.sleep(0.35)
            print(f"[probe]    结果 {names(pipe, ipc)}", flush=True)
            # 复原成 [s1, s2, s3]，让每个 case 从同一个起点出发
            order = names(pipe, ipc)
            if order != WANT:
                for j, nm in enumerate(WANT):
                    cur = names(pipe, ipc)
                    if nm in cur and cur[j] != nm:
                        tell(pipe, ["playlist-move", cur.index(nm), j])
                        time.sleep(0.3)
                print(f"[probe]    复原 {names(pipe, ipc)}", flush=True)

        # ③ script-message 回灌：keypress 走 input.conf 的绑定，等价于滚轮落到内核窗口
        before = len(messages)
        tell(pipe, ["keypress", "WHEEL_UP"])
        time.sleep(0.5)
        tell(pipe, ["keypress", "CTRL+WHEEL_UP"])
        time.sleep(0.5)
        tell(pipe, ["script-message", "gelu-direct", "x"])
        time.sleep(0.5)
        got = messages[before:]
        print(f"[probe] ③client-message 收到 {len(got)} 条：{got}", flush=True)
        ok_keypress = any(m.get("args", [])[:2] == ["luhui-wheel", "vol"] for m in got)
        ok_ctrl = any(m.get("args", [])[:2] == ["luhui-wheel", "seek"] for m in got)
        ok_direct = any(m.get("args", [])[:1] == ["gelu-direct"] for m in got)
        print(f"[probe] 结论：input.conf 绑定回灌={'是' if ok_keypress else '否'}；"
              f"Ctrl+滚轮多命令={'是' if ok_ctrl else '否'}；"
              f"IPC 自发 script-message 回灌={'是' if ok_direct else '否'}", flush=True)
        return 0 if ok_keypress else 1
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


if __name__ == "__main__":
    raise SystemExit(main())
