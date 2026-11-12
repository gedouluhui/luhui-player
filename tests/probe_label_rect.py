"""一次性探针（可删）：QSS 的 padding 到底能不能**量**出来，而不是靠 sizeHint 猜。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_label_rect.py

为什么要再量一遍（capture_sweep 里那把"实得宽 < sizeHint 宽"的压扁尺有已知误报类）：
tests/probe_tab_fit.py 实测——侧栏 240 时五个页签各拿 56/44/32/32/32px，而它们的 sizeHint 是
59/47/35/35/35。差的那 3px 是 **Qt 默认样式给 ToolButton 留的 11px 松弛**（sizeHint = 文字 + 11，
**不含** QSS 的 padding；QSS 写了 padding:0 之后真正给字的是整格宽）。也就是说那三个页签
一个字都没被切，压扁尺却会报——判据带着一个"以后有人把侧栏拖到 240 就集体假红"的坑。

而原始故障（页签 54px 的格、QSS 左右各 10px、文字 48px）要的恰恰是"扣掉 QSS padding 之后
还剩多少给字"。所以这里把三种候选量具摆在一起对：
  A. contentsRect()            —— 已知**不含** QSS padding（第一版栽在这儿）
  B. sizeHint()                —— 含默认松弛、不含 QSS padding（现在这把，会假红）
  C. subElementRect(CE_…)      —— 走 QStyleSheetStyle，理论上 padding 算进去了
在"该报"（54px 格 + 2×10 padding + 48px 字）与"不该报"（56px 格 + 0 padding + 48px 字）
两个形状上各量一次：谁能把这两例分开，判据就换谁。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import (QApplication, QStyle, QStyleOptionToolButton, QToolButton, QWidget)  # noqa: E402

TEXT = "播放列表"


def measure(tb: QToolButton) -> dict:
    tb.ensurePolished()
    fm = tb.fontMetrics()
    # 类型必须对上：initStyleOption 的重载按参数类型挑，塞 QStyleOptionButton 是 TypeError
    # （本机实测；这类"枚举/重载没被重导出"的坑和 tests/audit_qt_attrs.py 扫的是同一族）。
    opt = QStyleOptionToolButton()
    tb.initStyleOption(opt)
    st = tb.style()
    out = {"文字": fm.horizontalAdvance(tb.text()), "实得": tb.width(),
           "contentsRect": tb.contentsRect().width(), "sizeHint": tb.sizeHint().width(),
           "minimumSizeHint": tb.minimumSizeHint().width()}
    # subElementRect 要的是 SubElement 枚举，而 Qt5 **没有** SE_ToolButtonLabel
    # （`CE_ToolButtonLabel` 是 ControlElement，塞进去是 TypeError——本机实测）。
    # 能拿到的最接近"标签矩形"的是 subControlRect(SC_ToolButton)，它给整颗按钮，不含 padding 扣减。
    try:
        out["SC_ToolButton"] = st.subControlRect(QStyle.CC_ToolButton, opt, tb,
                                                 QStyle.SC_ToolButton).width()
    except Exception as exc:                     # noqa: BLE001
        out["SC_ToolButton"] = f"抛异常 {type(exc).__name__}: {exc}"
    return out


def build(pad: str, width: int) -> tuple[QWidget, QToolButton]:
    host = QWidget(None)
    host.setStyleSheet(f"QToolButton {{ font-size: 12px; padding: {pad}; }}")
    host.resize(300, 60)
    tb = QToolButton(host)
    tb.setText(TEXT)
    tb.setFixedWidth(width)
    tb.move(4, 4)
    host.show()
    for _ in range(10):
        QApplication.processEvents()
    return host, tb


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    cases = [("该报：格 54 / padding 2×10 / 字 48", "4px 10px", 54),
             ("不该报：格 56 / padding 0 / 字 48（侧栏 240 的真实形状）", "4px 0px", 56),
             ("该报：格 60 / padding 2×10 / 字 48", "4px 10px", 60),
             ("不该报：格 68 / padding 2×10 / 字 48（侧栏 300 的真实形状）", "4px 10px", 68)]
    for name, pad, w in cases:
        host, tb = build(pad, w)
        m = measure(tb)
        need = m["文字"]
        # 四把候选尺，各自"报"的条件写成一行：
        #   ①② 量"格放不放得下字"（现有两把的算法）；③④ 量"布局有没有把样式自己要的宽压掉"
        flags = {"实得<文字": m["实得"] < need,
                 "contentsRect<文字": m["contentsRect"] < need,
                 "实得<sizeHint": m["实得"] < m["sizeHint"],
                 "实得<minSizeHint": m["实得"] < m["minimumSizeHint"]}
        want = name.startswith("该报")
        marks = "  ".join(f"{k}{'报' if v else '静'}{'✔' if v == want else '✘'}"
                          for k, v in flags.items())
        print(f"{name}（该报={want}）\n    {m}\n    {marks}")
        host.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
