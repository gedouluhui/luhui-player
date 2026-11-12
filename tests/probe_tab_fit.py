"""一次性探针（可删）：侧栏页签到底谁放不下、画面页那一屏内容有多高。

为什么要测而不是看着改：`播放列表` 在 sweep 图里左右都被切掉半个字，
说明布局给这个按钮的宽度**小于它的文字宽＋QSS padding**。而"该给多宽"是三件事的乘积
（字号的水平推进、QSS 的 padding、按钮之间的 spacing 与容器 margin），
凭眼睛挑一个"看起来够"的 padding，换一台 125%/175% 缩放的机器就又是另一回事。
所以这里把三个量都打出来：文字宽、按钮 sizeHint、布局实际分到的宽度。

跑法：python tests/probe_tab_fit.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("GELU_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                ".selftest-data"))

from PyQt5.QtCore import Qt                       # noqa: E402
from PyQt5.QtGui import QFontMetrics             # noqa: E402
from PyQt5.QtWidgets import (QApplication, QScrollArea, QToolButton, QWidget)  # noqa: E402

from app import config as cfgmod                  # noqa: E402
from app.ui import theme                          # noqa: E402

# 页签名单不在这里抄一份：下面读的是 `win.tab_btns` 与按钮自己的 `text()`，
# 抄来的名单在第六页（声音均衡）加进来那天就会变成一份"看起来还在用"的旧地名。


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(theme.qss())
    cfg = cfgmod.Config()
    w = int(cfg["sidebar_width"])
    print(f"sidebar_width(配置默认)={w}")

    host = QWidget(None)
    host.setWindowFlags(Qt.Window)
    host.resize(1280, 720)
    host.show()
    for _ in range(6):
        app.processEvents()

    # 复刻 main_window 里那条 tab 栏：同样的 objectName、同样的 margin/spacing、同样的 stretch 尾巴
    from app.ui.main_window import MainWindow
    win = MainWindow(cfg)
    win.resize(1280, 720)
    win.show()
    for _ in range(10):
        app.processEvents()
    win._set_sidebar(True)
    for _ in range(10):
        app.processEvents()

    tabs = win.tab_btns
    bar = tabs[0].parentWidget()
    lay = bar.layout()
    print(f"tab 栏容器宽={bar.width()} margin={lay.contentsMargins()} spacing={lay.spacing()}")
    # 页签这一排的"能不能放下"现在是个能算的数（产品侧 `_layout_tabs` 钉死了每个按钮的宽度），
    # 所以这里量的就是三件事：文字宽、按钮实得宽、这一排需要的总宽 ≤ 侧栏宽。
    total = 0
    for b in tabs:
        b.ensurePolished()        # QSS 的 font-size 要落到 QFontMetrics 上
    texts = [QFontMetrics(b.font()).horizontalAdvance(b.text()) for b in tabs]
    print(f"文字宽（已 polish，QSS 的 12px 生效）={texts}")
    for side_w in (240, 245, 250, 260, 300, 360, 519, 520):
        need = win._layout_tabs(side_w)
        win.sidebar.setFixedWidth(side_w)
        for _ in range(8):
            app.processEvents()
        got = [b.width() for b in tabs]
        real = [b.contentsRect().width() for b in tabs]
        hints = [b.sizeHint().width() for b in tabs]
        ok = all(t <= g and t <= r for t, g, r in zip(texts, got, real))
        ok_hint = all(h <= g for h, g in zip(hints, got))
        pad = (got[0] - texts[0]) // 2
        print(f"  侧栏 {side_w}：留白档≈{pad} 缝={lay.spacing()} 这一排需要={need} 实得宽={got} "
              f"contentsRect={real} sizeHint={hints} "
              f"⇒ {'每个页签都放得下' if ok and need <= side_w else '有页签放不下'}"
              f"／{'样式要的宽也都给到了' if ok_hint else '有页签被压到 sizeHint 以下'}")
    win.sidebar.setFixedWidth(int(cfgmod.Config()["sidebar_width"]))
    win._layout_tabs(int(cfgmod.Config()["sidebar_width"]))
    for _ in range(8):
        app.processEvents()

    # 画面页：内容高度 vs 视口高度（底部那行快捷键提示会不会被切一半）
    # **必须先把那一页切成当前页**：QStackedWidget 里没显示的那页不参与布局，
    # 它的 viewport 停在没人管的小值上（第一版探针就是这么量出"视口=30"的，那是探针的坑不是产品的）。
    for page, name, panel in ((2, "调节", win.tweak_panel), (3, "画面", win.transform_panel),
                              (4, "统计", win.stats_panel)):
        win._sidebar_page(page)
        for _ in range(12):
            app.processEvents()
        area = panel.findChild(QScrollArea)
        inner = area.widget() if area else None
        if inner is None:
            print(f"{name}页：没有 QScrollArea（{panel.__class__.__name__}）")
            continue
        print(f"{name}页 视口={area.viewport().height()} 内容={inner.sizeHint().height()}"
              f"/{inner.height()} 超出={inner.sizeHint().height() - area.viewport().height()} "
              f"滚动条可见={area.verticalScrollBar().isVisible()} "
              f"widgetPolicy={area.widgetResizable()}")
        # 最后一行（提示/按钮）此刻在视口里露出多少
        last = inner.findChildren(QWidget)[-1]
        print(f"     末行 {last.__class__.__name__} y={last.y()} 高={last.height()} "
              f"视口底={area.viewport().height()}")
    # 新判据 tabs_fit（test_ui 里那条"全量程都放得下"）现在就能离线跑：它只量几何、不抓像素，
    # 所以屏幕暗着也作数。把它的原样 + 两种注入一起过一遍，确认三条子判据各自归各自的家，
    # 而不是"合起来红成一团、分不清是哪条抓到的"。
    import test_ui as TU
    win._set_sidebar(True, 0)
    for _ in range(8):
        app.processEvents()
    trials = (
        ("原样（240..520 每 20px 一档）", lambda: TU.tabs_fit(win, tuple(range(240, 521, 20)))),
        ("钉成文字+2（旧写法：压破样式但还没切字）", lambda: TU.tabs_fit(win, (300,), squeeze=2)),
        ("钉成文字−6（比文字还窄：真的切字）", lambda: TU.tabs_fit(win, (300,), squeeze=-6)),
        ("钉成文字+40（每格够宽、need 没变，只有布局压 x 会重叠）",
         lambda: TU.tabs_fit(win, (300,), squeeze=40)),
        ("摘掉加宽那一步再喂 200（应同时报排不开＋重叠）",
         lambda: TU.tabs_fit(win, (200,), no_grow=True)),
        ("越界配置 99999/100 走产品两道闸（应放行）", lambda: TU.tabs_fit(win, (99999, 100))),
        ("复原之后再量一次", lambda: TU.tabs_fit(win, (300, 240))),
    )
    for tag, run in trials:
        ok, detail = run()
        print(f"tabs_fit {tag}：{'放行' if ok else '报红'} —— {detail}")
    print("收口值：" + " ".join(f"{v!r}→{win._clamped_sidebar_width(v)}"
                               for v in (100, 239, 240, "280", 520, 521, 99999, None, "abc")))
    # U50 的另一半：把两道闸整个摘掉（照配置原样钉宽度），⑤必须真的红——
    # "期望它红"不算，得当场量到侧栏超过窗口一半、视频区被挤没。
    _orig = int(win.sidebar_width)
    print(f"摘闸前：侧栏={win.sidebar.width()} 视频区宽={win.video.width()} 窗口={win.width()}")
    win.sidebar_width = 99999
    win.sidebar.setFixedWidth(99999)
    for _ in range(8):
        app.processEvents()
    print(f"摘闸：侧栏={win.sidebar.width()} 窗口={win.width()} 视频区宽={win.video.width()} "
          f"⇒ 超过窗口一半={'是' if win.sidebar.width() * 2 > win.width() else '否'}")
    win.sidebar_width = _orig
    win._fit_sidebar()
    for _ in range(8):
        app.processEvents()
    print(f"装闸复原：侧栏={win.sidebar.width()} 视频区宽={win.video.width()}")
    win.player.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
