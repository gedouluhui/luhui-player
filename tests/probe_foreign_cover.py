"""一次性探针（不算产品件）：从**另一个进程**在屏幕上压一块不透明的置顶窗，
用来在真实条件下看第 31 条那条分流——"眼睛被外来窗口挡住"必须记 [ENV] 而不是 [FAIL]。

为什么要有它：test_ui 的 U57 那三条腿是**接线桩**（把 `_hwnd_chain_at`／`_exempt_blocker`
调出来），它证明的是"分流写对了、没接反"，证明不了"真有一扇别人的窗压上来时收尾码真的是 3"。
R7 那一轮恰好是后者（pid 18248 压住视频区），量到的却是 rc=1、汇总 153/154。
所以修法上线后要拿真窗口再看一次，而不是只看桩。

跑法（两条，同一台机同一个屏；先起遮挡、再跑验收）：
    python tests/probe_foreign_cover.py 45 &
    python tests/test_ui.py > tests/out/accept-ui-r9-covered.log 2>&1; echo rc=$?
判读：那份日志里 ①「视频区在屏幕上真的有画面」那一条必须打 [ENV] 而不是 [FAIL]；
      ②汇总必须是「失败项：[]」（真红 0 条）；③退出码必须是 3（环境不足），不是 1。
本脚本只画自己这一扇窗、只关自己这一扇窗，绝不按镜像名动任何别人的进程。
"""
from __future__ import annotations

import os
import sys
import time

from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QApplication, QWidget

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0


def main() -> int:
    app = QApplication(sys.argv[:1])
    scr = app.primaryScreen().geometry()
    w = QWidget(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    w.setAttribute(Qt.WA_ShowWithoutActivating, True)
    # 压在屏幕中间那一块（播放窗的视频区就在那儿），尺寸够盖住采样点但不挡任务栏
    w.setGeometry(int(scr.width() * 0.28), int(scr.height() * 0.22),
                  int(scr.width() * 0.44), int(scr.height() * 0.5))
    w.show()
    p = QPainter(w)
    p.fillRect(w.rect(), QColor("#c8c8c8"))
    p.end()
    print(f"遮挡窗已起：pid={os.getpid()} hwnd={int(w.winId())} "
          f"几何={w.geometry().getRect()} 存活={SECS}s", flush=True)
    t0 = time.monotonic()
    while time.monotonic() - t0 < SECS:
        w.raise_()
        app.processEvents()
        time.sleep(0.25)
    w.close()
    print(f"遮挡窗已关（{time.monotonic() - t0:.1f}s 到了），本进程退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
