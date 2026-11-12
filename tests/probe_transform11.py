# 一次性测量（结论进 engine/transform.py 的 docstring 与 tests/test_transform.py 的已知答案）：
# 几何项的**越界值到底是"夹紧"还是"整条拒收"**。
# 为什么要重测：表里原来写"panscan 发 -1.0 也夹到 1.0（不是 0.0）"，据此把判据写成
# `got == 1.0`。真机验收第一轮量到的是 **0.0**——两种解释差别很大：
#   夹紧 ⇒ 内核把 -1 夹到区间下界 0.0（外壳夹不夹都一样，界面语义正确）；
#   拒收 ⇒ 内核停在旧值，而旧值恰好是 0.0（那"夹到 1.0"那句就是把上一次量到的
#          旧值当成了夹的结果——旧探针发 -1 之前链上还挂着 panscan=1.0 的上一档）。
# 所以要**从已知的旧值出发**逐个方向量：旧值 0.0 / 0.5 / 1.0，发 -1.0 与 2.0，看回掉什么。
# 顺带把 video-rotate 的 360（从 0 与从 90 两种旧值）与 video-pan-x 的 ±5 一起量掉：
# 这三条是"外壳夹紧是承重件"这个结论的全部依据，不能靠一句转述。
# 跑法：python tests/probe_transform11.py   （> tests/out/probe-transform11.log）
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

    def probe(prop, from_val, send_val):
        p.set_prop(prop, from_val)
        pump(0.35)
        cur = p.get(prop)
        p.set_prop(prop, send_val)
        pump(0.35)
        got = p.get(prop)
        verdict = ("夹到边界/新值" if str(got) != str(cur) and got != send_val else
                   ("原样收下" if got == send_val else "停在旧值（整条拒收）"))
        print(f"  {prop:16s} 旧={cur!r:>8} 发={send_val!r:>6} -> 回报={got!r:>8}  {verdict}")
        return cur, send_val, got

    print("\n1) panscan（内核区间 0..1）：负值与超上界，分别从 0.0 / 0.5 / 1.0 三种旧值发")
    for old in (0.0, 0.5, 1.0):
        probe("panscan", old, -1.0)
    for old in (0.0, 0.5, 0.2):
        probe("panscan", old, 2.0)
    probe("panscan", 0.0, 0.35)
    probe("panscan", 0.35, -0.0001)
    p.set_prop("panscan", 0.0)

    print("\n2) video-rotate（内核只收 0..359）：360 与 -90，分别从 0 与 90 发")
    for old in (0, 90):
        probe("video-rotate", old, 360)
    for old in (0, 90):
        probe("video-rotate", old, -90)
    probe("video-rotate", 0, 270)
    p.set_prop("video-rotate", 0)

    print("\n3) video-pan-x（内核照收 ±2.0，我们只开放 ±1.0）：发 ±5")
    for old, send in ((0.0, 5.0), (0.4, 5.0), (0.0, -5.0), (0.4, -5.0)):
        probe("video-pan-x", old, send)
    p.set_prop("video-pan-x", 0.0)

    print("\n4) 走产品路径（set_transform 会先归一）：同一批越界值")
    for key, send in (("panscan", -1.0), ("panscan", 2.0), ("rotate", "360"),
                      ("rotate", "-90"), ("pan_x", 5.0), ("pan_x", -5.0),
                      ("aspect_override", "-1"), ("aspect_override", "auto")):
        actual, ok = p.set_transform(key, send)
        pump(0.3)
        from app.engine import transform
        prop = transform.prop_of(key)
        print(f"  {key:16s} 发={send!r:>6} -> 归一后配置={actual!r:>8} 内核回报"
              f"={p.get(prop)!r:>9} 判一致={ok}")
    print(f"  复位后 options 侧：video-aspect-override={p.get('video-aspect-override')!r}")
    p.reset_transforms()
    pump(0.6)

    print('\n5) video-scale-x（表里原话是"发 -1.0 夹到 0.0"，跟上面几条同源，本轮一起重测）')
    for old, send in ((1.0, -1.0), (0.5, -1.0), (1.0, 3.0), (1.0, 0.0), (1.0, 1.5)):
        probe("video-scale-x", old, send)
    p.set_prop("video-scale-x", 1.0)
    pump(0.3)

    print("\n6) 内核有没有替我们做过任何一次夹紧（scale 也发过一遍之后看日志）")
    try:
        with open(p.log_path, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError as exc:
        print("  读不到日志:", exc)
        txt = ""
    keys = ("deprecated", "Clamped", "clamped", "out of range", "invalid")
    lines = [l.strip()[-110:] for l in txt.splitlines()
             if any(k in l for k in keys)]
    print(f"  命中 {len(lines)} 行（去重后 {len(set(lines))}）：")
    for l in list(dict.fromkeys(lines))[:14]:
        print("   ", l)
    p.quit()
    pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
