"""一次性探针（R12-V5）：侧栏两页的"数值药丸"到底齐不齐，量完就能决定要不要动产品代码。

要回答的问题只有一个：每一行右边那枚读数（`NumRow.val`，objectName=Chip）
——① 同一页里它们的宽度是不是同一个数；② 右边缘是不是对齐的；③ 拖到最大位数时会不会变宽
（变了就是"拖动途中整行重排"，也就是抖动）。

为什么不读样式表：宽度是 `setMinimumWidth(58)` + 内容 sizeHint + QSS padding 三者协商的结果，
读代码只能看出"下限是 58"，看不出这一页里最长那条读数把哪几行顶宽了。

用法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_pills.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from app.ui import theme, tweak_panel, transform_panel, form_rows  # noqa: E402


def dump(name: str, panel: QWidget) -> None:
    rows = [r for r in panel.findChildren(form_rows.NumRow)]
    print(f"\n[{name}] NumRow {len(rows)} 根")
    ws, rights = set(), set()
    for r in rows:
        r.val.ensurePolished()
        w = int(r.val.width())
        right = int(r.val.mapTo(panel, r.val.rect().bottomRight()).x())
        ws.add(w)
        rights.add(right)
        print(f"  {r.spec.label:<12} 读数={r.val.text()!r:<10} 宽={w:<4} 右缘={right:<5} "
              f"sizeHint={r.val.sizeHint().width()}")
        # 拖到量程两端，看宽度会不会变（变了＝拖动途中整行重排）
        lo, hi = r.index_of(r.spec.lo), r.index_of(r.spec.hi)
        r.show(r.spec.lo)
        w_lo = int(r.val.width())
        r.show(r.spec.hi)
        w_hi = int(r.val.width())
        r.show(r.spec.default)
        if w_lo != w or w_hi != w:
            print(f"  {'':12} ↑ 拖动途中宽度会变：最小={w_lo} 最大={w_hi} 默认={w}")
    print(f"  同页宽度集合={sorted(ws)} 右缘集合={sorted(rights)}")


def main() -> int:
    app = QApplication(sys.argv)
    theme.install(app, "#4C8DFF")
    dump("调节页", tweak_panel.TweakPanel({}))
    dump("画面页", transform_panel.TransformPanel({}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
