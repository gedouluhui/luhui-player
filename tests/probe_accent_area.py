"""一次性探针（可删）：把"换主色改动到的面积"这条尺在**每一档字号**上重新量一遍，用来定下限。

跑法：PYTHONIOENCODING=utf-8 python tests/probe_accent_area.py

为什么存在：`accent_switch_px` 原来是隔步抽样，1.0 档报 314 个采样点、2.5 档只报 48 个，
于是 `--font-scale 2.5` 整批"换色失败"红了一屏，而产品什么都没改（步长随图涨，细描边每
step 才命中一次）。现在改成逐像素数面积，但**新口径的下限不能凭手感写**（那是上一版踩过的
"拿 2% 判审美"）：这里直接读各档留档的 PNG（`tests/out/sweep/**/50-accent-*.png`），
把六色相对默认蓝的面积逐档打出来，下限取"最低那一档再折一半"，并把每一档的读数写进注释当出处。

它只读盘、不开窗口，所以屏幕黑着也能跑。
"""
from __future__ import annotations

import glob
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt5.QtGui import QImage                             # noqa: E402

import capture_sweep as CS                                 # noqa: E402
from app.ui import theme                                   # noqa: E402

BASE_NAME = "蓝（默认）"


def dirs() -> list[str]:
    """标准档 + 所有换档目录，按字号从小到大。"""
    std = os.path.join(CS.TU.OUT, "sweep") if hasattr(CS, "TU") else os.path.join(HERE, "out", "sweep")
    out = [(0.0, std)]
    for d in glob.glob(os.path.join(std, "font-*")):
        # "font-1.15" 的前缀是 5 个字符（f-o-n-t-连字符），不是 6：按 6 切会把 "2.5" 切成 ".5"，
        # 于是各档按 0.5/0.15/0.3 这种假字号排序，而 "font-2" 切成空串直接 ValueError 悄悄漏档。
        try:
            out.append((float(os.path.basename(d)[5:]), d))
        except ValueError:
            print(f"（目录名里的字号读不出来，跳过：{d}）")
            continue
    return [d for _s, d in sorted(out)]


def main() -> int:
    if CS.np is None:
        print("没有 numpy，这把尺量不了（探针不作数）")
        return 2
    print(f"numpy={CS.np.__version__}  逐像素口径；读的是留档 PNG，不是重新画的")
    rows = []
    got = dirs()
    print(f"找到 {len(got)} 个留档目录：{[os.path.relpath(x, HERE) for x in got]}")
    for d in got:
        # 文件名是「50-accent-<色名>-<hex>.png」：前缀 10 个字符，再去掉 .png，
        # 最后一段是 hex，要用 rsplit 剥掉——直接切到末尾会把"橙-F76707"当色名，六张全都对不上。
        files = {os.path.basename(p)[10:-4].rsplit("-", 1)[0]: p
                 for p in glob.glob(os.path.join(d, "50-accent-*.png"))}
        if BASE_NAME not in files or len(files) < 2:
            print(f"\n（跳过 {os.path.relpath(d, HERE)}：留档不全，只有 {sorted(files)}）")
            continue
        base_img = QImage(files[BASE_NAME])
        if base_img.isNull():
            print(f"\n（跳过 {os.path.relpath(d, HERE)}：基准那张读不出来）")
            continue
        vals = []
        for hexv, name in theme.ACCENTS:
            if name == BASE_NAME:
                continue
            path = files.get(name)
            if not path:
                continue
            theme.set_accent(hexv)
            toks = (theme.ACCENT, theme.ACCENT_FILL, theme.ACCENT_TEXT)
            cur = QImage(path)
            if cur.isNull():
                vals.append((name, -1, "读不出"))
                continue
            n, det = CS.accent_switch_px(base_img, cur, toks)
            vals.append((name, n, det))
        same, _ = CS.accent_switch_px(base_img, base_img,
                                      (theme.ACCENT, theme.ACCENT_FILL, theme.ACCENT_TEXT))
        size = f"{base_img.width()}×{base_img.height()}"
        ns = [v[1] for v in vals if v[1] >= 0]
        print(f"\n=== {os.path.relpath(d, HERE)}（图 {size}）===")
        for name, n, det in vals:
            print(f"  · {name}: {n}px²  —— {det}")
        print(f"  · 自减控制（必须是 0）：{same}")
        if ns:
            rows.append((os.path.relpath(d, HERE), min(ns), max(ns), same))
    if not rows:
        print("\n没有任何一档可读——下限不定")
        return 2
    print("\n---- 汇总（每档取五色里最小的那个当该档下限候选）----")
    for label, lo, hi, same in rows:
        print(f"  {lo:8d}px² ~ {hi:8d}px²  自减={same}  {label}")
    worst = min(r[1] for r in rows)
    print(f"\n各档最小值中的最小 = {worst}px²；建议下限 = {worst // 2}px²（折一半留抖动余量）")
    bad_ctrl = [r[0] for r in rows if r[3] != 0]
    print(f"自减控制不为 0 的档（有则这把尺在测噪声，别定下限）：{bad_ctrl or '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
