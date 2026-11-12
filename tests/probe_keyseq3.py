"""探针 3：移位标点（< > + _ : 这类"要按 Shift 才出来的符号）到底能不能绑、怎么绑。

探针 2 里 `QKeySequence("<")` 解析成 0x3c 且**不带** Shift 修饰，而真实键盘上 '<' 是
Shift+逗号，Windows 递上来的事件键多半还是 0x2c——所以那一行"装了但永远按不出来"。
默认表里 ("<", "mute") 就是这么一条死键。这里要问清两件事：
  1. 事件里到底给的是 0x2c+Shift 还是 0x3c？（决定 "<" 这种写法有没有救）
  2. "Shift+," 这个串解析成什么、真实按下能不能命中？（决定修法）
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from PyQt5.QtCore import Qt                      # noqa: E402
from PyQt5.QtGui import QKeyEvent, QKeySequence  # noqa: E402
from PyQt5.QtWidgets import QApplication, QShortcut, QWidget  # noqa: E402
from PyQt5.QtTest import QTest                   # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)
log: list[int] = []


class Watch(QWidget):
    def keyPressEvent(self, ev):
        log.append((int(ev.key()), int(ev.modifiers()), str(ev.text())))
        super().keyPressEvent(ev)


print("--- 解析串 → 键值（UNKNOWN=0x01ffffff 是死串的标志）---")
UNKNOWN = 0x01ffffff   # C++ 里的 Qt::Key_UNKNOWN；PyQt5 没把这个枚举值导出来，写死并说明
for s in ("<", "Shift+,", "Ctrl+Shift+,", ">", "Shift+.", "+", "Shift+=",
          "Ctrl++", "_", "Shift+-", ":", "Shift+;", "Return", "Enter", "Numpad Enter",
          "Page Down", "PgDn", "Media Stop", "Volume Mute", "Audio Mute", " mute"):
    qs = QKeySequence(s)
    k = int(qs[0]) if qs.count() else -1
    print(f"  {s!r:<18} count={qs.count()} key={k:#010x} dead={k == UNKNOWN} "
          f"toString={qs.toString()!r}")

w = Watch()
w.resize(320, 200)
w.show()
QTest.qWaitForWindowExposed(w)
QTest.qWait(120)
QTest.qWaitForWindowActive(w)

hits: list[str] = []
TABLE = {"<": "lt", ",": "comma", "Shift+,": "shiftcomma", "Z": "z",
         "Ctrl+Shift+,": "ctrlshiftcomma", "Return": "return", "Enter": "enter",
         "Shift+.": "shiftdot", ".": "dot", ">": "gt",
         "Page Down": "pagedown", "-": "minus", "Ctrl+-": "ctrlminus"}
for seq, tag in TABLE.items():
    sc = QShortcut(QKeySequence(seq), w, context=Qt.WindowShortcut)
    sc.activated.connect(lambda checked=False, t=tag: hits.append(t))
    getattr(hits, "append")  # noqa: B018
    sc._tag = tag

print("\n--- 真实事件：QTest 按下 → 事件里是什么键，命中了哪条 ---")


def press(name, key, mods):
    """QTest 直接造事件（绕开操作系统映射），问的就是"事件长这样时哪条快捷键认账"。"""
    log.clear()
    hits.clear()
    QTest.keyClick(w, key, mods)
    APP.processEvents()
    QTest.qWait(50)
    APP.processEvents()
    print(f"  {name:<18} 事件={[(f'{k:#06x}', f'{m:#x}', t) for k, m, t in log]}"
          f" 命中={hits or '（无）'}")


press("逗号", Qt.Key_Comma, Qt.NoModifier)
press("Shift+逗号", Qt.Key_Comma, Qt.ShiftModifier)
press("Less 键无修饰", Qt.Key_Less, Qt.NoModifier)
press("Shift+Less", Qt.Key_Less, Qt.ShiftModifier)
press("句号", Qt.Key_Period, Qt.NoModifier)
press("Shift+句号", Qt.Key_Period, Qt.ShiftModifier)
press("Return", Qt.Key_Return, Qt.NoModifier)
press("Enter(小键盘)", Qt.Key_Enter, Qt.NoModifier)
press("减号", Qt.Key_Minus, Qt.NoModifier)
press("Ctrl+减号", Qt.Key_Minus, Qt.ControlModifier)
print("\nDONE")
