# -*- coding: utf-8 -*-
"""内核实测探针第二趟：R4 要用的那几个"只能用一次才知道"的写法。

第一趟（probe_transform.py）量到的事实：
  - video-rotate 收 0/45/90/180/270，**360 与 -90 整条被拒**（回报停在前一个值）；
  - panscan 收到 1.0 之后自己夹，**负值整条被拒**；
  - usm / sharpen / lapsharp / reflection 在本机这版**加不进 vf**（change-list 之后 vf 是空的）；
  - deband / nlmeans / hqdn3d / hflip / vflip / crop / rotate / lavfi=[...] 加得进。
这一趟问的是把 R4 写下来必须回答的另外几件事：
  1) 整条滤镜链**一次替换**（change-list vf set）能不能用——逐步 append/remove 会有
     "中途状态"，用户连着切两个开关时界面与内核会各说各话；
  2) lettercrop（自动裁黑边）在不在；
  3) 锐化只能用 lavfi 的话，unsharp 这版 ffmpeg 收不收、参数怎么回报；
  4) video-aspect-override 能不能直接发浮点（界面读回来的是浮点，发出去的是"16:9"字符串，
     两头对不上就得先定死"以哪个为准"）；
  5) 关掉 keepaspect 之后内核回报什么（裁剪/拉伸这一档的判据要用）。

跑法：python tests/probe_transform2.py
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication        # noqa: E402

from app import config as cfgmod                 # noqa: E402
from app.engine.player import MpvPlayer          # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")


def _pump(app, secs: float) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    logs: list[str] = []
    p = MpvPlayer(cfgmod.Config())
    p.sig_log.connect(logs.append)
    if not p.start(hwnd=0):
        print("内核起不来"); [print("   ", l) for l in logs[-8:]]
        return 3
    try:
        p.open_files([SAMPLE])
        _pump(app, 2.0)
        print(f"# {p.version}")

        def vf() -> list:
            return p.get("vf", []) or []

        print("\n## 1) 整条替换 vs 逐条增删")
        p._fire(["change-list", "vf", "set", "deband,hflip"])
        _pump(app, 0.2)
        print("   set 'deband,hflip' →", [x.get("name") for x in vf()])
        p._fire(["change-list", "vf", "set", ""])
        _pump(app, 0.2)
        print("   set '' →", vf())
        p._fire(["change-list", "vf", "set", "nlmeans"])
        _pump(app, 0.2)
        p._fire(["change-list", "vf", "remove", "nlmeans"])
        _pump(app, 0.2)
        print("   先 set nlmeans 再 remove nlmeans →", vf())

        print("\n## 2) lettercrop / overscan / expand / aspect 这些滤镜在不在")
        for f in ("lettercrop", "lettercrop=limit=64:shrink_only=no", "overscan",
                  "expand=iw:ih:0:0", "aspect=16:9", "crop=0:0:0:0", "format=yuv420p"):
            p._fire(["change-list", "vf", "set", f])
            _pump(app, 0.2)
            print(f"   {f:38s} → {[x.get('name') for x in vf()]}")
        p._fire(["change-list", "vf", "set", ""])

        print("\n## 3) 锐化只能借 lavfi：unsharp 收不收")
        for g in ("lavfi=[unsharp=5:5:0.8:5:5:0.0]", "lavfi=[unsharp]",
                  "lavfi=[eq=contrast=1.05]", "lavfi=[nope]"):
            p._fire(["change-list", "vf", "set", g])
            _pump(app, 0.25)
            print(f"   {g:34s} → {vf()}")
        p._fire(["change-list", "vf", "set", ""])

        print("\n## 4) video-aspect-override：字符串与浮点各发一次，读回来是什么")
        for v in ("-1", "16:9", 1.777778, "4:3", 1.333333, "auto"):
            p.set_prop("video-aspect-override", v)
            _pump(app, 0.15)
            print(f"   发 {v!r:12} → 回 {p.get('video-aspect-override')!r}")

        print("\n## 5) keepaspect 与画面几何回报")
        for v in (True, False):
            p.set_prop("keepaspect", v)
            _pump(app, 0.15)
            ops = p.get("video-out-params", {}) or {}
            print(f"   keepaspect={v} → 回报 {p.get('keepaspect')!r}"
                  f" d_w×d_h={ops.get('dw')}×{ops.get('dh')}"
                  f" aspect={ops.get('aspect')!r} sar={ops.get('sar')!r}")
        p.set_prop("keepaspect", True)

        print("\n## 6) video-rotate 的归一化边界（0..359？90 的倍数？）")
        for v in (0, 1, 89, 90, 91, 179, 180, 269, 270, 359):
            p.set_prop("video-rotate", v)
            _pump(app, 0.12)
            print(f"   {v} → {p.get('video-rotate')!r}")
        p.set_prop("video-rotate", 0)

        print("\n## 7) panscan 与 video-scale/pan 同时给，几何怎么报")
        p.set_prop("panscan", 0.3)
        p.set_prop("video-pan-x", 0.5)
        _pump(app, 0.2)
        print("   panscan=", p.get("panscan"), " pan-x=", p.get("video-pan-x"),
              " scale-x=", p.get("video-scale-x"))
        for v in (1.0, 1.0001, 0.9999, 0.0, -0.0):
            p.set_prop("panscan", v)
            _pump(app, 0.1)
            print(f"   panscan {v!r} → {p.get('panscan')!r}")
        p.set_prop("panscan", 0.0)
        p.set_prop("video-pan-x", 0.0)

        print("\n## 内核原话（滤镜不存在/lavfi 不可用只会出现在这里）")
        for line in logs[-30:]:
            print("   ", line[:200])
        return 0
    finally:
        p.quit()


if __name__ == "__main__":
    sys.exit(main())
