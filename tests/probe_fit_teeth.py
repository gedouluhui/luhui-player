"""一次性探针：两处"要不要拿真字号档才有鉴别力"的形状闸，能不能在同进程里量出来？

要回答的两个问题（都是写 U54/U55 之前必须先知道的，否则判据会写成恒真）：
  ① `TitleBar._fit_height()` 在标准字号下算出来就是 44，和旧的"钉死 44"一模一样——
     照标准档判它，摘掉闸也不会红。所以要么换字号档（换进程，`_scale_qss` 跑第二遍会二次放大），
     要么当场把子件字号调大再重排。这里先量后一条走不走得通：`setFont()` 会不会被
     样式表里的 font-size 盖掉，高度规则跟不跟着涨。
  ② `setMinimumContentsLength(12)` 到底顶住了什么：拿两个用同一份 `add_choice` 造出来的下拉，
     一个保持 12、一个调回 0，比 sizeHint 宽与"12 个 0 要多宽"。

本文件不产生判据，只打印事实。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from PyQt5.QtGui import QFont                          # noqa: E402
from PyQt5.QtWidgets import QApplication, QVBoxLayout, QWidget   # noqa: E402
import test_ui as TU                                   # noqa: E402
import capture_sweep as SW                             # noqa: E402
from app.ui import form_rows                            # noqa: E402


def title_font(win) -> None:
    print("\n---- ① 标题条：子件字号变大之后高度规则跟不跟 ----")
    tb = win.title
    kids = tb.findChildren(TU.QLabel) + tb.findChildren(TU.QToolButton)
    base_need = max(int(k.sizeHint().height()) for k in kids)
    print(f"  起点：高={tb.height()} 子件最高 sizeHint 高={base_need} "
          f"压扁={SW.squeezed_widgets(tb)}")
    saved = {k: k.font() for k in kids}
    for k in kids:
        f = QFont(k.font())
        f.setPixelSize(int(f.pixelSize() if f.pixelSize() > 0 else k.font().pointSize() * 96 / 72) * 3)
        k.setFont(f)
    TU.pump(0.2)
    after = max(int(k.sizeHint().height()) for k in kids)
    print(f"  setFont(×3) 之后：子件最高 sizeHint 高={after}（字号有没有落到 metrics："
          f"{after != base_need}）")
    tb._fit_height()
    TU.pump(0.2)
    print(f"  重排之后：高={tb.height()} 规则应给={max(TU.theme.M.title_h, after + 12)} "
          f"压扁={SW.squeezed_widgets(tb)[:2]}")
    old = tb.height()
    tb.setFixedHeight(44)                              # 注入：旧写法（钉死）
    TU.pump(0.2)
    print(f"  注入'钉死 44'之后：高={tb.height()} 压扁={SW.squeezed_widgets(tb)[:2]} "
          f"（该红：44 < 需要 {after + 12}）")
    tb._fit_height()
    TU.pump(0.2)
    print(f"  交回产品：高={tb.height()}（复原={tb.height() == old}）压扁={SW.squeezed_widgets(tb)[:2]}")
    for k, f in saved.items():
        k.setFont(f)
    tb._fit_height()
    TU.pump(0.2)
    print(f"  还原字号并重排：高={tb.height()} 压扁={SW.squeezed_widgets(tb)[:2]}")


def combos() -> None:
    print("\n---- ② minimumContentsLength(12) 顶住了什么 ----")

    class Item:
        label = "输出设备"
        options = [("auto", "自动"), ("null", "无输出")]

    host = QWidget(None)
    lay = QVBoxLayout(host)
    cb12 = form_rows.add_choice(lay, Item(), host, lambda _c: None)
    cb0 = form_rows.add_choice(lay, Item(), host, lambda _c: None)
    cb0.setMinimumContentsLength(0)
    host.show()
    TU.pump(0.2)
    for cb, tag in ((cb12, "12 档"), (cb0, "调回 0")):
        fm = cb.fontMetrics()
        need12 = fm.horizontalAdvance("0" * 12)
        print(f"  {tag}: minimumContentsLength={cb.minimumContentsLength()} "
              f"sizeHint={int(cb.sizeHint().width())}×{int(cb.sizeHint().height())} "
              f"12个0要={need12} minimumSizeHint宽={int(cb.minimumSizeHint().width())} "
              f"条目宽={[fm.horizontalAdvance(cb.itemText(i)) for i in range(cb.count())]}")
    host.deleteLater()


def settings(win, cfg) -> None:
    print("\n---- ③ 设置窗：把宽度压到需求以下再让闸自己修 ----")
    from app.ui.settings_dialog import SettingsDialog
    d = SettingsDialog(cfg, win)
    d.show()
    TU.pump(0.3)
    need = d.content_need
    print(f"  自然态：宽={d.width()} content_need={need} "
          f"掉出视口={SW.beyond_viewport(d)[:2]} 裁而未告={SW.clipped_without_hint(d)[:2]}")
    d.resize(300, d.height())
    TU.pump(0.3)
    print(f"  压到 300 之后：宽={d.width()} 掉出视口={SW.beyond_viewport(d)[:2]} "
          f"裁而未告={SW.clipped_without_hint(d)[:3]}")
    got = d._fit_width()
    TU.pump(0.3)
    print(f"  再跑一次 _fit_width()={got} 宽={d.width()} 需求={d.content_need} "
          f"掉出视口={SW.beyond_viewport(d)[:2]} 裁而未告={SW.clipped_without_hint(d)[:2]}")
    orig = d.width()
    _orig_fit = form_rows.fit_to_content
    form_rows.fit_to_content = lambda w, cap=None: int(w.width())   # 注入：闸摘掉
    d.resize(300, d.height())
    TU.pump(0.3)
    d._fit_width()
    TU.pump(0.3)
    print(f"  注入恒等版之后：宽={d.width()} 需求={d.content_need} "
          f"裁而未告={SW.clipped_without_hint(d)[:3]}（该红）")
    form_rows.fit_to_content = _orig_fit
    d.resize(orig, d.height())
    TU.pump(0.2)
    d.hide()
    del d


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    TU.APP = app
    cfg = TU.make_cfg()
    win = TU.build_window(cfg)
    win.add_paths(TU.FILES[:1])
    TU.wait_for(lambda: float(win.player.prop("duration", 0) or 0) > 0, 12.0)
    TU.pump(0.4)
    win._set_sidebar(True, 0)
    TU.pump(0.3)
    title_font(win)
    combos()
    settings(win, cfg)
    win.close()
    TU.pump(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
