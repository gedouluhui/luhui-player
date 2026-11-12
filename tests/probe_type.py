"""一次性探针（R12-V3/V5，可删）：把"字色分档"和"数值药丸齐不齐"量成数字。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_type.py
（**别加 QT_QPA_PLATFORM=offscreen**：本机离屏平台下 `QLabel.grab()` 回来的整张图
  alpha 全 0——字一个像素都没画。2026-10-12 实测：同一个脚本 windows 平台能读到
  #e8eaf0/#a6abb8 两种实心字色，离屏只读到一片全透明。所以这里读不到不透明像素时
  必须**当场认瞎**（退出码 3），不许把"亮度 0.000"当成产品读数往下判。）

V3 要回答的是：行标题（`#RowLabel`）和说明行（`#AppTitle`）在**像素上**是不是两档。
读 QSS 不算——QSS 里写了两个颜色，和它们真的被画出来是两件事（同一条规则被后面的选择器
盖掉、或者字太小被抗锯齿拉到中间值，都会让"令牌分档"在屏幕上不存在）。
所以这里量每个标签"字芯"的亮度：取该标签渲染图里不透明像素中最亮那一撮（前 2%）的均值色。

V5 要回答的是：同一页里那些行尾读数（`NumRow.val`）宽度是不是一个数、右缘齐不齐、
拖到量程两端会不会变宽（会变＝拖动途中整行重排，也就是抖动）。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("GELU_DATA", os.path.join(HERE, ".selftest-data"))

from collections import Counter                                  # noqa: E402
from PyQt5.QtGui import QImage                                   # noqa: E402
from PyQt5.QtWidgets import (QApplication, QLabel, QVBoxLayout, QWidget)  # noqa: E402

import test_ui as TU                                             # noqa: E402
from app.engine import tweaks                                    # noqa: E402
from app.ui import form_rows, theme                              # noqa: E402


def ink(img: QImage) -> tuple[str, float, int] | None:
    """标签的"字芯"颜色、亮度、像素数；**读不到实心字就回 None（=量具瞎了）**。

    口径：只认 alpha==255 的像素，取它们里最亮那一撮（前 5%）的均值色。
    为什么不是"剔掉最暗背景再看剩下"（第一版就是这么写的，当场自己骗自己）：
    单独 `grab()` 一个 QLabel 拿到的图**没有背景**——底是全透明，实心像素全是字，
    于是"最暗的实心像素"是字自己的边缘，`floor+40` 那条相对阈值把整撮字全判掉了，
    日志里就印出"没画上字"这种**结论也错、归因也错**的话（明明有 782 个不透明像素）。
    Qt 的 ARGB32 不是预乘格式，抗锯齿像素的 RGB 就是墨色本身，所以取 alpha==255
    那一撮最稳。亮度按 ITU-R 709 加权，跨色档可比。
    """
    lum = [(0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue(), c)
           for j in range(img.height()) for i in range(img.width())
           if (c := img.pixelColor(i, j)).alpha() == 255]
    if len(lum) < 30:
        return None
    lum.sort(key=lambda p: -p[0])
    top = lum[: max(1, int(len(lum) * 0.05))]
    n = len(top)
    r = round(sum(p[1].red() for p in top) / n)
    g = round(sum(p[1].green() for p in top) / n)
    b = round(sum(p[1].blue() for p in top) / n)
    return (f"#{r:02x}{g:02x}{b:02x}", round((0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0, 3), n)


def shoot(w: QWidget):
    img = w.grab().toImage().convertToFormat(QImage.Format_ARGB32)
    solid = sum(1 for j in range(img.height()) for i in range(img.width())
                if img.pixelColor(i, j).alpha() == 255)
    return img, ink(img), solid


def main() -> int:
    TU._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.install(app, theme.ACCENT_DEFAULT)
    dpr = app.primaryScreen().devicePixelRatio()
    print(f"[env] platform={app.platformName()} dpr={dpr}")

    blind = 0
    print("\n---- V3 字色分档（同一个父面板里并排画三种标签）----")
    box = QWidget()
    box.setObjectName("Panel")
    lay = QVBoxLayout(box)
    row_title = form_rows.mk_label("充满画面", box)
    hint = form_rows.mk_hint("R 旋转 · Shift+R 比例 · H 镜像", box)
    chain = QLabel("滤镜链：（空）", box)
    chain.setObjectName("AppTitle")
    chip = QLabel("1.00x", box)
    chip.setObjectName("Chip")
    for w in (row_title, hint, chain, chip):
        lay.addWidget(w)
    box.ensurePolished()
    box.adjustSize()
    box.resize(300, box.sizeHint().height())
    app.processEvents()
    tiers: dict[str, tuple[str, float]] = {}
    for name, w in (("行标题 #RowLabel", row_title), ("说明行 mk_hint", hint),
                    ("读数 #AppTitle", chain), ("数值胶囊 Chip", chip)):
        img, got, opaque = shoot(w)
        if got is None:
            blind += 1
            print(f"  {name:<16} [瞎] 实心(alpha=255)像素={opaque} 尺寸={img.width()}x{img.height()}"
                  f" —— 少于 30 个，这一档的字色读不出来，后面的分档结论不作数")
            continue
        h, l, n = got
        tiers[name] = (h, l)
        print(f"  {name:<16} 字芯={h} 亮度={l:.3f} 像素={n} 尺寸={img.width()}x{img.height()}")
    if len(tiers) == 4:
        t = tiers["行标题 #RowLabel"][1]
        d = tiers["说明行 mk_hint"][1]
        print(f"  [算] 行标题-说明行 亮度差={t - d:+.3f}（>0 才是标题比说明亮） "
              f"字芯色 {'相同→分档不存在' if tiers['行标题 #RowLabel'][0] == tiers['说明行 mk_hint'][0] else '不同'}")

    print("\n---- V5 数值药丸（同一页里的行尾读数）----")
    host = QWidget()
    host.setObjectName("Panel")
    hl = QVBoxLayout(host)
    specs = list(tweaks.NUMS)
    rows = []
    for s in specs:
        r = form_rows.NumRow(s, host)
        hl.addWidget(r)
        rows.append(r)
    host.ensurePolished()
    host.adjustSize()
    host.resize(300, host.sizeHint().height())
    host.show()                       # 不 show 的话真窗口下也没排过版，量到的是 Qt 默认 100 宽
    app.processEvents()
    ws, rights = {}, {}
    for r in rows:
        r.val.ensurePolished()
        w0 = int(r.val.width())
        r.show(r.spec.lo); app.processEvents()
        w_lo = int(r.val.width())
        r.show(r.spec.hi); app.processEvents()
        w_hi = int(r.val.width())
        r.show(r.spec.default); app.processEvents()
        ws[w0] = ws.get(w0, 0) + 1
        x = r.val.mapTo(host, TU.QPoint(0, 0)).x() / dpr
        rights[round(x + w0 / dpr, 1)] = None
        flag = "" if w_lo == w0 == w_hi else "  ← 拖动途中会变宽（抖动）"
        print(f"  {r.spec.label:<8} 读数={r.val.text()!r:<10} 宽={w0:<4} "
              f"最小值档={w_lo} 最大值档={w_hi} sizeHint={int(r.val.sizeHint().width())}"
              f" 右缘x={x + w0 / dpr:.1f} 左缘x={x:.1f}{flag}")
    print(f"  同页宽度分布={sorted(ws.items())}  最宽-最窄={max(ws) - min(ws)}"
          f"  右缘集合={sorted(rights)}（差>0.5px 就是没对齐）")
    print(f"  [注] 刻度基准：面板宽={int(host.width())} 侧栏上限=520")

    print("\n---- V3/V5 真页面（侧栏里真实的行标题 / 说明行 / 面板底 / 行尾读数，全是像素与几何）----")
    cfg = TU.make_cfg()
    win = TU.build_window(cfg)
    app.processEvents()
    for pname, panel in (("调节", win.tweak_panel), ("画面", win.transform_panel)):
        # 把这一页真的切出来再量：stacked page 没当过当前页时布局是旧的，
        # 右缘会差出一截（第一版探针就量到 123 vs 107，那 16px 是"没排过版"不是"没对齐"）。
        win.sidebar_stack.setCurrentWidget(panel)
        app.processEvents()
        labs = [w for w in panel.findChildren(QLabel)
                if w.objectName() == "RowLabel" and w.isVisibleTo(panel)]
        hints = [w for w in panel.findChildren(QLabel)
                 if w.objectName() == "AppTitle" and w.isVisibleTo(panel)]
        pimg = panel.grab().toImage().convertToFormat(QImage.Format_ARGB32)
        cnt = Counter()
        for jj in range(0, pimg.height(), 3):
            for ii in range(0, pimg.width(), 3):
                c = pimg.pixelColor(ii, jj)
                if c.alpha() == 255:
                    cnt[c.name()] += 1
        bg = cnt.most_common(1)[0][0] if cnt else "#000000"
        li = ink(labs[0].grab().toImage().convertToFormat(QImage.Format_ARGB32)) if labs else None
        hi = ink(hints[0].grab().toImage().convertToFormat(QImage.Format_ARGB32)) if hints else None
        for tag, got in (("行标题", li), ("说明行", hi)):
            if got is None:
                blind += 1
                print(f"  {pname}页 {tag} [瞎]（没读到 ≥30 个实心字像素）")
        if li and hi:
            print(f"  {pname}页 底={bg} | 行标题 {li[0]} 亮度{li[1]:.3f} 对比{theme.contrast(li[0], bg):.2f}:1 "
                  f"| 说明行 {hi[0]} 亮度{hi[1]:.3f} 对比{theme.contrast(hi[0], bg):.2f}:1 "
                  f"| 差={li[1] - hi[1]:+.3f} 行数={len(labs)}/{len(hints)}"
                  f" 首行文字={labs[0].text()!r}/{hints[0].text()[:12]!r}")
        rs = [r for r in panel.findChildren(form_rows.NumRow) if r.isVisibleTo(panel)]
        dist, edges, jitter = {}, {}, []
        for r in rs:
            w0 = int(r.val.width())
            saved = r.value_of(r.slider.value())
            dist[w0] = dist.get(w0, 0) + 1
            x = r.val.mapTo(panel, TU.QPoint(0, 0)).x() / dpr
            key = round(x + w0 / dpr, 1)
            edges[key] = edges.get(key, 0) + 1
            for probe_v in (r.spec.lo, r.spec.hi, r.spec.default):
                r.show(probe_v)
                if int(r.val.width()) != w0:
                    jitter.append(f"{r.spec.label}@{probe_v}→{int(r.val.width())}(基线{w0})")
            r.show(saved)
        print(f"  {pname}页 {len(rs)} 行：宽度分布={sorted(dist.items())} "
              f"右缘分布={sorted(edges.items())} 拖动途中变宽={jitter or '无'}")
        print("    逐行：" + " ".join(f"{r.spec.label}={r.val.text()!r}/{int(r.val.width())}"
                                     for r in rs))
    win.close()
    host.hide()
    plat = app.platformName()
    del app
    if blind:
        print(f"\n[result] 量具瞎了 {blind} 项（平台={plat}），本次读数不作数")
        return 3
    print("\n[result] 全部读到了像素")
    return 0


if __name__ == "__main__":
    sys.exit(main())
