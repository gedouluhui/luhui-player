"""R4-1 引擎接线的一次性探针（不是交付测试，交付测试是 tests/test_transform.py）。

目的只有一个：在往界面上画控件之前，先确认 set_transform / apply_filter_chain /
reset_transforms 这一套对着**真内核**是通的——哪一项请求下去内核没收、哪一项
回报和请求对不上，这里先暴露出来，免得界面建在一条断的管线上。
"""
from __future__ import annotations

import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("GELU_DATA", os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".selftest-data")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from PyQt5.QtCore import QCoreApplication  # noqa: E402
from PyQt5.QtGui import QImage  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine import transform  # noqa: E402
from app.engine.player import MpvPlayer  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples", "gelu-sample-1.mp4")
NOISY = os.path.join(os.path.dirname(__file__), "out", "tmp-noisy.mp4")

fails: list[str] = []


def show(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  —— {detail}")
    if not ok:
        fails.append(name)


def main() -> int:
    app = QCoreApplication.instance() or QCoreApplication([])
    cfg = cfgmod.Config()
    cfg["resume"] = False
    cfg["screenshot_dir"] = os.path.join(cfgmod.DATA_DIR, "shots")
    for k, v in transform.reset_values().items():
        cfg[k] = v          # 从干净档起步，别把上一轮的脏配置带进启动参数
    cfg.save()

    p = MpvPlayer(cfg)
    logs: list[str] = []
    p.sig_log.connect(lambda s: logs.append(s))
    if not p.start(hwnd=0):
        print("内核起不来：", logs[-5:])
        return 2
    p.open_files([SAMPLE], remember=False)
    time.sleep(1.2)

    # ---- 几何族：逐项下发、回读、看内核认不认 ----
    for key, want in (("aspect_override", "16:9"), ("rotate", "90"),
                      ("panscan", 0.35), ("pan_x", 0.2), ("pan_y", -0.2),
                      ("keep_aspect", False)):
        actual, ok = p.set_transform(key, want)
        prop = transform.prop_of(key)
        show(f"几何 {key}→{want}", ok,
             f"{prop} 回报={actual!r} 缓存={p.prop(prop)!r}")
    show("旋转越界先取模再发（450→90）",
         p.set_transform("rotate", 450)[1] and float(p.get("video-rotate", -1)) == 90,
         f"video-rotate={p.get('video-rotate')!r}")
    show("panscan 越界夹到 1（实测内核会把 -1 夹成 1，必须由表来管）",
         p.set_transform("panscan", 9.0)[0] == 1.0 and p.set_transform("panscan", -9.0)[0] == 0.0,
         f"9→{p.cfg['panscan']!r}")
    show("属性名也能查（video-rotate 而不是只有 rotate）",
         transform.is_known("video-rotate") and p.set_transform("video-rotate", "180")[1],
         f"回报={p.get('video-rotate')!r}")

    # ---- 滤镜族：整条链替换 + 日志判据 ----
    for key, want in (("sharpen", 1.0), ("denoise", 4), ("deband_vf", 6),
                      ("mirror_h", True), ("mirror_v", True)):
        p.set_transform(key, want, persist=True, confirm=False)
        chain, names, lf = p.apply_filter_chain()
        show(f"滤镜 {key}→{want} 上链", key and True,
             f"链={chain} 列表={names} 日志禁用行={lf[:1]}")

    p.set_transform("crop_ratio", "16:9")
    chain, names, lf = p.apply_filter_chain()
    show("裁剪上链且内核没禁用", "crop" in names and not lf,
         f"链={chain} 列表={names} 禁用={lf[:1]}")
    # 判据必须是**截图尺寸**：实测裁剪后 video-out-params 的 w/h 照样回报 1280×720，
    # 拿它当"裁完多大"会永远看不出裁剪有没有生效。
    shots = cfgmod.SHOT_DIR
    os.makedirs(shots, exist_ok=True)

    def frame_size(tag: str):
        path = os.path.join(shots, f"crop-{tag}.png").replace("\\", "/")
        if os.path.exists(path):
            os.remove(path)
        p._req(["screenshot-to-file", path, "video"], timeout=8)
        for _ in range(80):
            if os.path.exists(path):
                break
            time.sleep(0.1)
        if not os.path.exists(path):
            return None
        im = QImage(path)
        r = (im.width(), im.height())
        os.remove(path)
        return r

    for ratio, exp in (("2.35:1", (1280, 544)), ("4:3", (960, 720)), ("1:1", (720, 720))):
        p.set_transform("crop_ratio", ratio)
        p.apply_filter_chain()
        time.sleep(0.7)
        got = frame_size(ratio)
        show(f"裁剪 {ratio} 真的改了出图像素（{exp[0]}×{exp[1]}）", got == exp, f"截图={got}")

    # 内核不吃 ',' 转义：min() 写法会被整条拒收。判据＝日志抓到 + 列表停在**上一条**链
    # （实测被拒时内核不改写 vf，所以"列表没变"就是"新链没生效"，两种写法都得防）。
    off = p._log_offset()
    before = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
    p._req(["change-list", "vf", "set",
            "crop=trunc(min(iw,ih*235/100)/2)*2:trunc(min(ih,iw*100/235)/2)*2:0:0"], timeout=3.0)
    time.sleep(0.6)
    listed = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
    caught = transform.filter_failures(p._log_since(off))
    show("表里的写法确实在绕开 min()（内核把带逗号的表达式整条拒收、列表停在上一条）",
         bool(caught) and listed == before,
         f"上一条={before} 现在={listed} 抓到={caught[:1]}")
    p.set_transform("crop_ratio", "none")
    p.apply_filter_chain()

    # 真判据：去掉 format 前缀以后，硬解帧上这些滤镜必须被日志抓到
    cfg["mirror_h"] = True
    bad = transform.SW_PREFIX + ",hflip"
    off = p._log_offset()
    p._req(["change-list", "vf", "set", "hflip"], timeout=3.0)
    time.sleep(0.4)
    listed = [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)]
    caught = transform.filter_failures(p._log_since(off))
    show("裸 hflip（不带 format 前缀）列表照有、日志必须抓到",
         "hflip" in listed and bool(caught),
         f"列表={listed} 抓到={caught[:1]}")
    print(f"   （对照：带前缀的写法本该是 {bad!r}）")

    # ---- 复位与状态 ----
    st = p.reset_transforms()
    bad_geom = [s.cfg for s in transform.PROPS
                if not transform.same_value(s.cfg, s.default, st.get(s.prop))]
    show("复位把几何拉回默认", not bad_geom,
         f"未回默认={bad_geom} 回报={ {s.prop: st.get(s.prop) for s in transform.PROPS} }")
    show("复位清空滤镜链", st.get("vf") == [] and st.get("vf_failed") == [],
         f"vf={st.get('vf')} failed={st.get('vf_failed')[:1]}")

    state = p.transform_state()
    show("transform_state 的键就是配置键（界面按它重画）",
         set(state) == {s.cfg for s in transform.PROPS} | {"vf"},
         f"{sorted(state)}")

    # ---- 表外的键不发命令，并且报出来 ----
    n0 = len(logs)
    actual, ok = p.set_transform("no-such-thing", 1)
    show("表外的键拒发并上报", (not ok) and actual is None and len(logs) > n0,
         f"logs={logs[n0:]}")

    # ---- 循环 ----
    before = p.cfg["rotate"]
    val, ok = p.cycle_transform("rotate")
    show("cycle_transform 按表走一格",
         ok and val != before, f"{before} → {val}")

    # 换片以后链子还在（实测契约），复位过就该是空
    p.set_transform("sharpen", 1.5)
    p.apply_filter_chain()
    p.open_files([NOISY if os.path.exists(NOISY) else SAMPLE], remember=False)
    time.sleep(1.2)
    show("换片不清链（实测：vf 跨 loadfile 保留）",
         "unsharp" in [str(x.get("name")) for x in (p.get("vf", []) or []) if isinstance(x, dict)],
         f"vf={p.get('vf')}")

    p.quit()
    print(f"\n---- 探针汇总：{len(fails)} 项不符 ----")
    for f in fails:
        print("   ", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
