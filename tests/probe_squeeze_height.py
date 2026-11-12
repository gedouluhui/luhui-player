"""一次性探针（任务 #41）：压扁尺的**高度**那一档到底能不能信。

跑法：PYTHONIOENCODING=utf-8 GELU_DATA=tests/.selftest-data python tests/probe_squeeze_height.py

capture_sweep 加了"实得高 < sizeHint 高"之后，keymap 页与截图工坊各报了两句（「按下组合键…」
实得高 14 < 28、「70 条绑定…」16 < 32、「字幕：gelu-sample」16 < 32），但我目检
tests/out/sweep/keymap.png：那几句**整句都在**。所以要么目检错，要么这把高度尺在某种常见
形状下恒报警。这里把被点名的标签的机制量清楚：wordWrap 开没开、sizeHint 的高折了几行、
minimumSizeHint 折几行、布局实际给了几行、整句一行放不放得下。
量完再决定"留这一档 / 只对非换行标签留 / 整档删掉"。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import (QApplication, QLabel)  # noqa: E402

import test_ui as TU                                # noqa: E402
from app import config as cfgmod                    # noqa: E402
from app.ui.gif_dialog import GifDialog             # noqa: E402
from app.ui.keymap_dialog import KeymapDialog       # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


def dump(w: QLabel, tag: str) -> None:
    fm = w.fontMetrics()
    line = max(fm.height(), 1)
    text_w = fm.horizontalAdvance(w.text())
    print(f"[{tag}] {w.text()[:22]!r}")
    print(f"    wordWrap={w.wordWrap()} 实得={w.width()}×{w.height()} "
          f"sizeHint={w.sizeHint().width()}×{w.sizeHint().height()} "
          f"minSizeHint={w.minimumSizeHint().width()}×{w.minimumSizeHint().height()}")
    print(f"    单行高={line} ⇒ sizeHint 折 {w.sizeHint().height() / line:.2f} 行，"
          f"minSizeHint 折 {w.minimumSizeHint().height() / line:.2f} 行，"
          f"实得折 {w.height() / line:.2f} 行")
    print(f"    整句宽={text_w} 实得宽={w.width()} ⇒ "
          f"{'一行放得下（那 sizeHint 的两行高就是虚报）' if text_w <= w.width() else '一行放不下，该换行'}")
    # 候选替代尺：按**实得宽**算需要多高。开了换行的标签 sizeHint 不知道最终宽度（上面三句
    # 的 sizeHint 宽都是 153~180，而实得 944），所以它报的行数是猜的；heightForWidth 才是
    # "给我这个宽度，我要几行"的真答。这条量出来若与实得行数一致，高度档就改用它。
    hfw = w.hasHeightForWidth()
    need = w.heightForWidth(w.width()) if hfw else w.sizeHint().height()
    print(f"    hasHeightForWidth={hfw} heightForWidth(实得宽)={need} "
          f"⇒ 折 {need / line:.2f} 行  {'✔ 与实得一致' if abs(need - w.height()) <= 3 else '✘ 与实得差 ' + str(need - w.height())}")


cfg = cfgmod.Config()

dlg = KeymapDialog(cfg)
dlg.resize(980, 1000)
dlg.show()
TU.pump(0.25)
for lab in dlg.findChildren(QLabel):
    t = lab.text()
    if "按下组合键" in t or "条绑定" in t:
        dump(lab, "keymap")
dlg.close()

gif = GifDialog(cfg, lambda: {})
gif.show()
TU.pump(0.25)
for lab in gif.findChildren(QLabel):
    if "字幕" in lab.text() or "音轨" in lab.text():
        dump(lab, "gif")
gif.close()

# 对照组：一句**真的**被竖向切掉的标签（固定高度只给半行），高度尺该报。
probe = QLabel("这一句的高度被布局压成半行，字应当看不见下半截")
probe.setFixedHeight(probe.fontMetrics().height() // 2)
probe.show()
TU.pump(0.15)
dump(probe, "对照:真压扁")
probe.close()

# 第二条对照：开了换行、宽度只放得下一半、高度只给一行 —— 这种是真被切（后面两行根本没画），
# 上面那三条是"整句一行放得下、sizeHint 却按窄宽猜了三行"的虚报。新尺必须把这两类分开。
tight = QLabel("这一句开了换行，宽度只够两行，而高度只给了一行，第二行根本没画出来，"
               "属于真被竖向切掉的那一类")
tight.setWordWrap(True)
tight.setFixedWidth(220)
tight.setFixedHeight(tight.fontMetrics().height())
tight.show()
TU.pump(0.15)
dump(tight, "对照:换行且真切")
tight.close()
