# -*- coding: utf-8 -*-
"""内核实测探针：画面变换 / 画质增强这一族在本机 mpv 上到底认什么。

为什么先探再写：本仓库的事实表（engine/tweaks.py、engine/perf.py）每一条范围都是
"set 之后立刻 get 回来比对"量出来的——v0.41.0-1101 上**越界值是整条被拒、回报停在发
之前的旧值**（实测 set brightness=130 回报仍是 0）。不量就写表，得到的就是
"界面动了、画面没动"的档位，而那正是这张表要防的第一类 bug。

这一族（宽高比 / 缩放平移 / 旋转 / 裁剪 / 滤镜链）之前没量过，所以先跑这份探针，
把真值抄进表里再动 UI。

**本探针不做断言**：它的产出是给人抄的清单，断言在实现之后的验收里
（tests/test_transform.py，随 R4 一起落）。

跑法：python tests/probe_transform.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication                      # noqa: E402

from app import config as cfgmod                              # noqa: E402
from app.engine.player import MpvPlayer                       # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")

# 想试的属性 → 想试的值。**故意把明显越界的也放进去**：这一列要回答的问题不是
# "合法值能不能设"，而是"非法值谁来兜"——内核自己夹、整条拒收、还是照样收下？
# 答案决定表里的范围是不是承重件（需要外壳夹紧）。
CANDIDATES: list[tuple[str, list[object]]] = [
    ("video-aspect-override", ["-1", "16:9", "4:3", "2.35:1", "0", "abc"]),
    ("video-aspect-method", ["bitrate", "submit"]),
    ("keepaspect", [True, False, "yes"]),
    ("video-rotate", [0, 90, 180, 270, 45, 360, -90]),
    ("panscan", [0.0, 0.25, 1.0, 2.0, -1.0]),
    ("video-scale-x", [1.0, 1.5, 3.0, 0.0, -1.0]),
    ("video-scale-y", [1.0, 1.5, 0.0]),
    ("video-pan-x", [0.0, 0.5, -0.5, 2.0]),
    ("video-pan-y", [0.0, 0.5, -0.5, 2.0]),
    ("video-align-x", [0.0, 1.0, -1.0]),
    ("video-align-y", [0.0, 1.0, -1.0]),
    ("bluray-audio-ignore-fs-policy", [True]),     # 占位：不存在的项回报长什么样
]

# 滤镜链：mpv 的 `vf` 是字符串列表，官方改法是 change-list add/remove。
# 每一条都要"加进去之后 vf 里有没有它、去掉之后有没有残留"，因为"加了个不存在的滤镜"
# 在 mpv 里是**静默失败**（内核记一条 log，属性上看不出来）。
VF_TRIALS = ["deband", "usm", "sharpen", "nlmeans", "hqdn3d", "hflip", "vflip",
             "crop=640:360:0:0", "rotate=90", "lapsharp", "bilinear", "box",
             "reflection=x=1", "lavfi=[hflip]", "evictcopy"]


def _pump(app, secs: float) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def main() -> int:
    if not os.path.isfile(SAMPLE):
        print("没有样片，先跑 python tests/gen_sample.py")
        return 2
    app = QApplication.instance() or QApplication(sys.argv[:1])
    logs: list[str] = []
    p = MpvPlayer(cfgmod.Config())
    p.sig_log.connect(logs.append)
    if not p.start(hwnd=0):
        print("内核起不来，最后十条 log：")
        for line in logs[-10:]:
            print("   ", line)
        return 3
    try:
        p.open_files([SAMPLE])
        _pump(app, 2.0)
        print(f"# 内核 {p.version} ｜ 样片 {os.path.basename(SAMPLE)}"
              f" ｜ 尺寸 {p.prop('width')}×{p.prop('height')}")
        print("\n## 属性：设进去 → 立刻问回来（[不等] 表示回报与发送值对不上）")
        print("   对不上分三种，抄表的时候要分开看：夹到边界＝内核自己兜；"
              "停在旧值＝整条被拒（外壳必须夹紧，范围是承重件）；报 <问不到>＝这版没这项。")
        for name, vals in CANDIDATES:
            cells = []
            for v in vals:
                p.set_prop(name, v)
                _pump(app, 0.12)
                got = p.get(name, "<问不到>")
                same = (str(got).lower() == str(v).lower()
                        or (isinstance(v, bool) and str(got).lower() == ("true" if v else "false")))
                cells.append(f"{v!r}→{got!r}{'' if same else ' [不等]'}")
            print(f"{name:24s} " + " | ".join(cells))
        print("\n## 滤镜链：change-list vf append/remove，前后各问一次")
        for f in VF_TRIALS:
            p._fire(["change-list", "vf", "append", f])      # 探针直接走 IPC：没有公开 command()
            _pump(app, 0.15)
            after = p.get("vf", "<问不到>")
            p._fire(["change-list", "vf", "remove", f])
            _pump(app, 0.15)
            removed = p.get("vf", "<问不到>")
            print(f"  {f:22s} 加上后={after!r} 去掉后={removed!r}"
                  + ("" if not _contains(removed, f) else "  [残留]"))
        print("\n## 内核这段时间说过的话（滤镜不存在只会出现在这里）")
        for line in logs[-25:]:
            print("   ", line[:160])
        return 0
    finally:
        p.quit()


def _contains(vf: object, needle: str) -> bool:
    if isinstance(vf, list):
        return any(needle.split("=")[0] in str(x) for x in vf)
    return needle.split("=")[0] in str(vf or "")


if __name__ == "__main__":
    sys.exit(main())
