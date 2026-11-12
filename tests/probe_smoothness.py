"""无头探针：平滑度四件（video-sync / interpolation / tscale / deinterlace）的内核口径。

要回答两件事，都别猜：
  ① 这几档在启动参数里收不收（写错档位的后果是内核静默退回默认，界面看不出来）；
  ② 能不能运行期改（`set_property` / `set_property options/…`）。能热切就不该让用户重启，
     不能就老实归入「启动期」那一族、在设置页标出要重启。

全程 --vo=null --ao=null，无窗口；d3d11 那套真渲染不在本探针的范围里。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from app.engine.ipc import MpvIpc  # noqa: E402

MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")

CASES = [
    ("video-sync", "display-resample"),
    ("video-sync", "display-tempo"),
    ("interpolation", "yes"),
    ("tscale", "mitchell"),
    ("tscale", "lanczos"),
    ("deinterlace", "auto"),
]


def launch(pipe: str, extra: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
         "--vo=null", "--ao=null", "--no-audio", "--idle=yes", "--keep-open=yes",
         "--pause=yes", "--volume=0", "--really-quiet", *extra],
        cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main() -> int:
    stamp = int(time.time() * 1000)
    pipe = rf"\\.\pipe\gelu-probe-smooth-{stamp}"
    args = [f"--{k}={v}" for k, v in CASES]
    p = launch(pipe, args)
    ipc = MpvIpc(pipe)
    if not ipc.connect(timeout=10.0, proc=p):
        print("[probe] 连不上 IPC", flush=True)
        p.kill()
        return 2
    try:
        print("[probe] ①启动参数收下了：", flush=True)
        for key, val in CASES:
            got = ipc.get(f"options/{key}", "<unreadable>")
            plain = ipc.get(key, "<unreadable>")
            print(f"    --{key}={val:16s} → options/{key}={got!r} 裸属性={plain!r}", flush=True)
        print("[probe] ①b 订阅（界面画真值靠它）：", flush=True)
        for n, (key, _val) in enumerate(dict.fromkeys(CASES)):
            try:
                ipc.request(["observe_property", 800 + n, key], timeout=2.0)
                print(f"    observe {key:14s} → 收下", flush=True)
            except Exception as exc:
                print(f"    observe {key:14s} → 拒：{exc}", flush=True)
        print("[probe] ②运行期改：", flush=True)
        for key, val in CASES:
            for prop in (key, f"options/{key}"):
                try:
                    ipc.request(["set_property", prop, val], timeout=2.0)
                    now = ipc.get(f"options/{key}", "?")
                    print(f"    set {prop}={val:16s} → 现在 options/{key}={now!r}", flush=True)
                except Exception as exc:
                    print(f"    set {prop}={val:16s} → 拒：{exc}", flush=True)
        # 真加载一支片，看这套参数下内核还起不起得来
        ipc.fire(["loadfile", SAMPLE, "replace"])
        time.sleep(1.2)
        print(f"[probe] ③装片后 idle-active={ipc.get('idle-active')!r} "
              f"video-format={ipc.get('video-format')!r}", flush=True)
    finally:
        try:
            ipc.fire(["quit"])
        except Exception:
            pass
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
