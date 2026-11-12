# 一次性测量（结论进 app/engine/transform.py 的 docstring 与 tests/test_transform.py 的判据）：
# `change-list vf append` 到底**在什么写法下**会留下残留。
# 为什么要重测：验收判据原来写成"append 两次 `format=yuv420p,hflip` ⇒ 链上挂两条"，
# 真机第一轮量到的是 **列表=[]**（一次都没挂上）。两种解释差别很大：
#   A) 带逗号的整条链根本不能 append（内核拒收）⇒ "append 留残留"这句实测结论的适用范围
#      只在单档写法上，判据必须换成单档 append；
#   B) 是这条判据前置状态没清干净/命令超时 ⇒ 那"列表=[]"是测量废了，不是内核行为。
# 所以逐个形状量：单档 append 两次、整条链 append 两次、set 一条再 append 同一条、
# remove 能不能删干净，并且每次都抓 **原始应答里的 error 字段**（`_req` 会把错吞成日志）。
# 另外补一条像素级后果：hflip 挂两次 = 镜像两次 = 回到原图，而列表显示 2 条——
# 这条才是"列表不是事实"在 append 形状上的活证据。
# 跑法：python tests/probe_transform12.py   （> tests/out/probe-transform12.log）
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtCore import QCoreApplication  # noqa: E402

from app import config as cfgmod          # noqa: E402
from app.engine.ipc import IpcError       # noqa: E402
from app.engine.player import MpvPlayer   # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
APP = QCoreApplication([])


def pump(s=0.1):
    end = time.monotonic() + s
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.02)


def main():
    cfg = cfgmod.Config()
    cfg["resume"] = False
    cfg.save()
    p = MpvPlayer(cfg)
    if not p.start(hwnd=0):
        return 1
    print(f"# {p.version}")
    p.open_files([SAMPLE], remember=False)
    pump(1.8)
    p.set_prop("pause", True)
    pump(0.5)

    def raw(cmd):
        """发一条命令，抓 (data, error, 当前 vf 名字列表)。error 是 _req 会吞掉的那一栏。"""
        try:
            data = p._ipc.request(cmd, timeout=3.0)
            err = None
        except IpcError as exc:
            data, err = None, str(exc)
        pump(0.25)
        listed = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
        return data, err, listed

    def clear():
        raw(["change-list", "vf", "set", ""])

    print("\n1) 单档 append：hflip 发两次，中间各问一次列表")
    clear()
    for i in (1, 2):
        _d, e, l = raw(["change-list", "vf", "append", "hflip"])
        print(f"  第{i}次 append 'hflip'      error={e!r} 列表={l}")
    clear()

    print("\n2) 整条链 append（值里带逗号）：format=yuv420p,hflip 发两次")
    for i in (1, 2):
        _d, e, l = raw(["change-list", "vf", "append", "format=yuv420p,hflip"])
        print(f"  第{i}次 append 带逗号链      error={e!r} 列表={l}")
    print(f"  现状（上一轮判据量到的形状）：列表为空就是内核没挂上，看下面 3) 能不能挂")
    clear()

    print("\n3) 先 set 一条单档，再 append 同一档（产品路径的'改一档'形状）")
    raw(["change-list", "vf", "set", "unsharp"])
    print("  set 'unsharp' 后            列表=" +
          str([x.get('name') for x in (p.get('vf', []) or []) if isinstance(x, dict)]))
    _d, e, l = raw(["change-list", "vf", "append", "unsharp"])
    print(f"  append 'unsharp'            error={e!r} 列表={l}")
    _d, e, l = raw(["change-list", "vf", "append", "format=rgb24"])
    print(f"  append 'format=rgb24'       error={e!r} 列表={l}")
    clear()

    print("\n4) remove 能不能删干净（表里原话：set '' 才是清空）")
    raw(["change-list", "vf", "set", "hflip,vflip"])
    _d, e, l = raw(["change-list", "vf", "remove", "hflip"])
    print(f"  remove 'hflip'              error={e!r} 列表={l}")
    _d, e, l = raw(["change-list", "vf", "remove", "vflip"])
    print(f"  remove 'vflip'              error={e!r} 列表={l}")
    _d, e, l = raw(["change-list", "vf", "remove", "vflip"])
    print(f"  再 remove 一次（空链上删）    error={e!r} 列表={l}")
    clear()

    print("\n5) append 的像素后果：hflip 挂两条 = 镜像两次 = 看起来像没开")
    base = None
    try:
        import numpy as np  # noqa: F401
        have_np = True
    except ImportError:
        have_np = False

    def shot(tag):
        path = os.path.join(HERE, "out", "tf-frames", f"probe12-{tag}.png")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            got = p._ipc.request(["screenshot-to-file", path, "video"], timeout=5.0)
        except IpcError as exc:
            print(f"  截图废了：{exc}")
            return None
        pump(0.6)
        real = (got or {}).get("filename") if isinstance(got, dict) else None
        src = real or path
        if not have_np or not os.path.exists(src):
            return None
        from PIL import Image
        return np.asarray(Image.open(src).convert("RGB"), dtype=float)

    clear()
    a0 = shot("baseline")
    raw(["change-list", "vf", "append", "hflip"])
    a1 = shot("once")
    raw(["change-list", "vf", "append", "hflip"])
    a2 = shot("twice")
    if a0 is not None and a1 is not None and a2 is not None:
        d1 = abs(a1 - a0).mean()
        d2 = abs(a2 - a0).mean()
        n = [x.get('name') for x in (p.get('vf', []) or []) if isinstance(x, dict)]
        print(f"  列表={n}")
        print(f"  挂1条与基线差={d1:.3f}（该大）  挂2条与基线差={d2:.3f}（该≈0：镜像两次回到原图）")
    else:
        print("  缺 numpy/PIL 或截图没出帧，这一节不作数")
    clear()

    print("\n6) 内核日志里 append 相关的行（看有没有被拒的原文）")
    try:
        with open(p.log_path, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError as exc:
        print("  读不到日志:", exc)
        txt = ""
    keys = ("append", "change-list", "Invalid", "invalid", "Removing", "Disabling")
    lines = [l.strip()[-120:] for l in txt.splitlines() if any(k in l for k in keys)]
    print(f"  命中 {len(lines)} 行（去重 {len(set(lines))}）：")
    for l in list(dict.fromkeys(lines))[:20]:
        print("   ", l)
    p.quit()
    pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
