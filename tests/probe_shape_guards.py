"""一次性探针：五处"形状靠程序算"的地方，此刻各自量到多少？

要写验收判据（test_ui 的 U52–U56）之前，得先知道这五处的真实读数长什么形状，
否则判据只能凭猜测写阈值——猜错的判据要么恒真（产品坏了也绿），要么恒红（好产品被冤枉）：
  ① `MainWindow._pages_need()` / `form_rows.content_width()`：侧栏最宽那一页要多宽才不裁字；
  ② `form_rows.mk_hint()`：说明行是否真的靠换行而不是把列顶宽；
  ③ `TitleBar._fit_height()`：标题条高度跟着字走还是钉死 44；
  ④ `setMinimumContentsLength(12)`：下拉合起来时能不能容得下 12 个字；
  ⑤ `SettingsDialog._fit_width()`：设置窗宽与内容需求、屏幕上限三者的关系。

本文件不产生判据，只打印事实；用完留档即可丢弃。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from PyQt5.QtCore import QPoint                        # noqa: E402
from PyQt5.QtGui import QFontMetrics                   # noqa: E402
from PyQt5.QtWidgets import (QApplication, QComboBox, QLabel,   # noqa: E402,F001
                             QScrollArea, QStyle, QVBoxLayout, QWidget)
import test_ui as TU                                    # noqa: E402
import capture_sweep as SW                              # noqa: E402
from app import config as cfgmod                        # noqa: E402
from app.ui import form_rows, theme                     # noqa: E402
from app.ui import tweak_panel as TP                    # noqa: E402
from app.ui.settings_dialog import SettingsDialog        # noqa: E402

def page_name(win, i: int) -> str:
    """页名从 `win.tab_defs` 现取，不在模块里抄名单（同 `probe_scrollbar_allow.py` 那条理由：
    抄的名单在加页那天不会报错，只会让输出里那一行指向别的一页）。"""
    d = getattr(win, "tab_defs", ())
    return str(d[i][0]) if i < len(d) else str(i)


def _old_rule(pg) -> int:
    """订正前那条 bar 取法（排过版才问实际差值、否则兜底 sizeHint）照抄一遍，用来看差多少。"""
    lay = pg.layout()
    if lay is None:
        return int(pg.minimumSizeHint().width())
    m = lay.contentsMargins()
    chrome = int(m.left()) + int(m.right())
    best = 0
    for sc in pg.findChildren(TU.QScrollArea):
        body = sc.widget()
        if body is None:
            continue
        bar = (int(sc.width()) - int(sc.viewport().width())
               if int(sc.width()) > 0 else int(sc.verticalScrollBar().sizeHint().width()))
        best = max(best, int(body.minimumSizeHint().width()) + max(0, bar)
                   + 2 * int(sc.frameWidth()))
    return (best + chrome) if best else int(lay.minimumSize().width()) + chrome


def pages(win, cfg):
    print("\n---- ① 侧栏各页（名单按 tab_defs 取）的内容宽需求 ----")
    print(f"  侧栏宽={win.sidebar.width()} tab_row_need={win.tab_row_need} "
          f"sidebar_page_need={getattr(win, 'sidebar_page_need', None)} "
          f"_pages_need()={win._pages_need()}")
    stack = win.sidebar_stack
    for i in range(stack.count()):
        pg = stack.widget(i)
        vis0 = pg.isVisible()
        cw_hidden, old_hidden = form_rows.content_width(pg), _old_rule(pg)
        stack.setCurrentIndex(i)
        TU.pump(0.15)
        naive = int(pg.minimumSizeHint().width())
        cw = form_rows.content_width(pg)
        old = _old_rule(pg)
        parts = []
        for sc in pg.findChildren(TU.QScrollArea):
            body = sc.widget()
            parts.append(f"body={int(body.minimumSizeHint().width()) if body else '无'}"
                         f"/视口={int(sc.viewport().width())}/条={int(sc.width()) - int(sc.viewport().width())}")
        overs = SW.beyond_viewport(pg)
        rows = []
        for sc in pg.findChildren(TU.QScrollArea):
            body = sc.widget()
            if body is None:
                continue
            vp = sc.viewport().width()
            right = 0
            who = ""
            for c in body.findChildren(QLabel) + body.findChildren(QComboBox):
                if c.width() <= 0:
                    continue
                r = c.mapTo(body, QPoint(0, 0)).x() + c.width()
                if r > right:
                    right, who = r, f"{type(c).__name__}「{c.text()[:10]}」"
            rows.append(f"body宽={int(body.width())}/视口宽={int(vp)}/最右块右沿={right}({who})")
        print(f"  [{page_name(win, i)}] 开页前可见={vis0} "
              f"new(隐藏={cw_hidden}/显示={cw}) old_rule(隐藏={old_hidden}/显示={old}) "
              f"minimumSizeHint={naive} {' '.join(parts)} | {' '.join(rows)}")
        for sc in pg.findChildren(QScrollArea):
            print(f"      滚动条三来源：真实差值={int(sc.width()) - int(sc.viewport().width())} "
                  f"vbar.sizeHint={int(sc.verticalScrollBar().sizeHint().width())} "
                  f"PM_ScrollBarExtent={int(sc.style().pixelMetric(QStyle.PM_ScrollBarExtent))} "
                  f"sc宽={int(sc.width())}")
        print(f"      掉出视口={overs[:3]}({len(overs)}) 截字={SW.clipped_labels(pg)[:2]} "
              f"裁而未告={SW.clipped_without_hint(pg)[:2]} 压扁={SW.squeezed_widgets(pg)[:2]} "
              f"重叠={SW.colliding_text(pg)[:2]}")
    stack.setCurrentIndex(0)
    TU.pump(0.12)
    print(f"  逐页量完之后再问一次：_pages_need()={win._pages_need()} "
          f"sidebar_page_need={getattr(win, 'sidebar_page_need', None)} "
          f"侧栏宽={win.sidebar.width()} 配置说={int(cfg['sidebar_width'])}")
    print(f"  现在重排一次 _fit_sidebar()={win._fit_sidebar()} "
          f"→ 侧栏宽={win.sidebar.width()} sidebar_page_need={win.sidebar_page_need}")


def hint(win):
    print("\n---- ② mk_hint：窄格子里换行还是裁字 ----")
    host = QWidget(None)
    lay = QVBoxLayout(host)
    lay.setContentsMargins(8, 8, 8, 8)
    text = "把文件或文件夹拖进来即可播放，也可以 Ctrl+O 从菜单里挑"
    lab = form_rows.mk_hint(text, host)
    lay.addWidget(lab)
    host.show()
    TU.pump(0.2)
    fm = QFontMetrics(lab.font())
    one = fm.horizontalAdvance(text)
    wide = lab.sizeHint().width()
    print(f"  wordWrap={lab.wordWrap()} hasHeightForWidth={lab.hasHeightForWidth()} "
          f"单行需求={one} sizeHint={wide}×{lab.height()} 行数代理={lab.height() // max(1, fm.height())}")
    for frac in (0.6, 0.45):
        want = int(one * frac)
        host.setFixedWidth(want)
        TU.pump(0.2)
        held = lab.width()
        print(f"  给 {want}px：实得宽={held} 高={lab.height()} heightForWidth={lab.heightForWidth(held)} "
              f"host.minimumSizeHint宽={int(host.minimumSizeHint().width())} "
              f"裁而未告={SW.clipped_without_hint(host)} 截字={SW.clipped_labels(host)}")
    host.deleteLater()
    wrapped = []
    for tag, node in (("plist", win.panel), ("lib", win.lib_panel), ("tweak", win.tweak_panel),
                      ("transform", win.transform_panel), ("stats", win.stats_panel)):
        for t in node.findChildren(QLabel):
            if t.wordWrap():
                wrapped.append(f"{tag}:{t.text()[:10]}")
    plain = [f"{type(o).__name__}:{t.text()[:10]}" for o, w in
             (("plist", win.panel), ("lib", win.lib_panel), ("tweak", win.tweak_panel),
              ("transform", win.transform_panel), ("stats", win.stats_panel))
             for t in w.findChildren(QLabel)
             if not t.wordWrap() and len(t.text()) > 22]
    print(f"  界面里 wordWrap=True 的说明行={wrapped}")
    print(f"  界面里长文本却没开 wordWrap 的 QLabel（候选漏网）={plain}")


def title(win):
    print("\n---- ③ 标题条高度 ----")
    need = 0
    who = ""
    for w in win.title.findChildren(QLabel) + win.title.findChildren(TU.QToolButton):
        w.ensurePolished()
        h = int(w.sizeHint().height())
        if h > need:
            need, who = h, type(w).__name__ + ":" + (w.text()[:10] or w.objectName())
    print(f"  title.height()={win.title.height()} M.title_h={theme.M.title_h} "
          f"子件最高 sizeHint 高={need}（{who}） 规则算出来={max(theme.M.title_h, need + 12)} "
          f"压扁={SW.squeezed_widgets(win.title)}")


def combos(win):
    print("\n---- ④ 下拉的最小内容宽 ----")
    tb = win.tweak_panel
    d = getattr(tb, "cb_device", None)
    if isinstance(d, QComboBox):
        fm = QFontMetrics(d.font())
        print(f"  cb_device minimumContentsLength={d.minimumContentsLength()} "
              f"sizeHint宽={int(d.sizeHint().width())} 12个0要={fm.horizontalAdvance('0' * 12)} "
              f"当前条目数={d.count()} 最长条目宽={max((fm.horizontalAdvance(d.itemText(i)) for i in range(d.count())), default=0)} "
              f"宽={d.width()}")
    rows = []
    for w in (win.tweak_panel, win.transform_panel):
        for cb in w.findChildren(QComboBox):
            rows.append(f"{type(w).__name__}.{cb.objectName() or 'cb'}={cb.minimumContentsLength()}")
    print(f"  所有下拉的 minimumContentsLength：{rows}")


def settings(win, cfg):
    print("\n---- ⑤ 设置窗宽 ----")
    scr = TU.APP.primaryScreen()
    avail = int(scr.availableGeometry().width()) if scr else 0
    d = SettingsDialog(cfg, win)
    d.show()
    TU.pump(0.3)
    print(f"  width={d.width()} content_need={d.content_need} 屏幕可用宽={avail} 上限={avail - 80} "
          f"掉出视口={SW.beyond_viewport(d)[:3]} 截字={SW.clipped_labels(d)[:3]}")
    d.hide()
    return d


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    TU.APP = app
    cfg = TU.make_cfg()
    theme.install(app, str(cfg["theme_accent"]))    # 与 app/main.py、test_ui、capture_sweep 同一份装配
    win = TU.build_window(cfg)
    win.add_paths(TU.FILES[:1])
    TU.wait_for(lambda: float(win.player.prop("duration", 0) or 0) > 0, 12.0)
    TU.pump(0.4)
    win._set_sidebar(True, 0)
    TU.pump(0.3)
    print(f"字号档：theme.scale={getattr(theme, 'SCALE', None)} 字体={win.font().pointSizeF() or win.font().pixelSize()}")
    pages(win, cfg)
    hint(win)
    title(win)
    combos(win)
    d = settings(win, cfg)
    del d
    win.close()
    TU.pump(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
