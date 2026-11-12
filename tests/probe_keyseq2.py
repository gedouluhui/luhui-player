"""探针 2：把 QKeySequence 的"解析值 / 显示串 / 真按键命中"三件事分开量。

探针 1 发现 `QKeySequence("PgDn").toString()` 是空串，而 "PgUp" 不是——空串不能当
合法性判据（否则默认表自己四条全红）。到底是解析失败还是显示失败，得看解析出来的
键值本身。再一件：真实按键事件会不会把 "J" 和 "Shift+J" 混着触发，这决定了重绑以后
用户按下的是不是他以为的那条。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from PyQt5.QtCore import Qt                      # noqa: E402
from PyQt5.QtGui import QKeySequence             # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402
from PyQt5.QtTest import QTest                   # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)


def show(qs: QKeySequence) -> str:
    parts = []
    for i in range(qs.count()):
        k = int(qs[i])
        parts.append(f"{k:#010x}(k={k & ~0x4ffffff & 0xffffff:#06x},"
                     f"mods={k & 0x4ffffff0000000:#x})")
    return f"count={qs.count()} isEmpty={qs.isEmpty()} toString={qs.toString()!r} keys=[" + \
        " ".join(parts) + "]"


print("--- a) 解析：这些串各自变成什么键值 ---")
for s in ("PgUp", "PgDn", "PageUp", "Page Down", "Minus", "-", "Equals", "=",
          "Backslash", "\\", "Escape", "Esc", "NotAKey", "   ", "Ctrl+", ";;",
          "中文", "A B", "Shift", "Ctrl++", "+", "MediaPlay", "Media Play",
          "Ctrl+Alt+Shift+Meta+X", "Volume_Mute", "AudioMute", "Audio Volume Up"):
    print(f"  {s!r:<28} {show(QKeySequence(s))}")

print("\n--- b) 从键值构造（捕获路径走这里）：toString 能不能显示、再解析回不回得来 ---")
cases = [("PgUp", Qt.Key_PageUp), ("PgDown", Qt.Key_PageDown), ("Home", Qt.Key_Home),
         ("End", Qt.Key_End), ("Minus", Qt.Key_Minus), ("Equal", Qt.Key_Equal),
         ("Backslash", Qt.Key_Backslash), ("Escape", Qt.Key_Escape),
         ("Space", Qt.Key_Space), ("Comma", Qt.Key_Comma), ("Period", Qt.Key_Period),
         ("Enter", Qt.Key_Enter), ("Return", Qt.Key_Return),
         ("K", Qt.Key_K), ("Ctrl+K", Qt.Key_K | Qt.ControlModifier),
         ("Shift+K", Qt.Key_K | Qt.ShiftModifier),
         ("Ctrl+Shift+K", Qt.Key_K | Qt.ControlModifier | Qt.ShiftModifier),
         ("Up", Qt.Key_Up), ("Ctrl+Up", Qt.Key_Up | Qt.ControlModifier),
         ("F1", Qt.Key_F1), ("Ctrl+F1", Qt.Key_F1 | Qt.ControlModifier),
         ("Alt+Return", Qt.Key_Return | Qt.AltModifier)]
for name, kv in cases:
    qs = QKeySequence(int(kv))
    txt = qs.toString()
    rt = QKeySequence(txt)
    print(f"  {name:<16} {show(qs)}  往返{'ok' if rt == qs else 'DIFF -> ' + show(rt)}")

print("\n--- c) 真按键命中：装 QShortcut 后 QTest 按下，看哪条动作被触发 ---")
TESTS = [
    ("J", Qt.Key_J, Qt.NoModifier, "期望只触发 J"),
    ("J+Shift", Qt.Key_J, Qt.ShiftModifier, "期望触发 Shift+J 还是 J？"),
    ("K", Qt.Key_K, Qt.NoModifier, "默认表写的是大写 K，实际按小写 k 算不算"),
    ("Comma", Qt.Key_Comma, Qt.NoModifier, ""),
    ("Lt", Qt.Key_Less, Qt.ShiftModifier, "表里写的是 '<'"),
    ("BackslashKey", Qt.Key_Backslash, Qt.NoModifier, "表里写的是 'Backslash'"),
    ("Minus", Qt.Key_Minus, Qt.NoModifier, "表里写的是 'Minus'"),
    ("Equals", Qt.Key_Equal, Qt.NoModifier, "表里写的是 'Equals'"),
    ("PgDn", Qt.Key_PageDown, Qt.NoModifier, "表里写的是 'PgDn'"),
    ("Space", Qt.Key_Space, Qt.NoModifier, ""),
    ("Enter", Qt.Key_Return, Qt.NoModifier, "表里写的是 'Enter'"),
    ("AltReturn", Qt.Key_Return, Qt.AltModifier, ""),
    ("CtrlComma", Qt.Key_Comma, Qt.ControlModifier, ""),
    ("Digit0", Qt.Key_0, Qt.NoModifier, ""),
]
TABLE = {"J": "sub_delay_down", "Shift+J": "sub_delay_up", "K": "toggle_play",
         ",": "step_back", "<": "mute", "Backslash": "speed_reset",
         "Minus": "sub_size_down", "Equals": "sub_size_up", "PgDn": "next",
         "Space": "toggle_play", "Enter": "fullscreen", "Alt+Return": "fullscreen",
         "Ctrl+,": "settings", "0": "jump_0"}
hit: list[str] = []
w = QWidget()
w.resize(300, 200)
w.show()
QTest.qWaitForWindowExposed(w)
QTest.qWait(120)
QTest.qWaitForWindowActive(w)
scs = []
for seq, act in TABLE.items():
    from PyQt5.QtWidgets import QShortcut
    sc = QShortcut(QKeySequence(seq), w, context=Qt.WindowShortcut)
    sc.activated.connect(lambda checked=False, a=act: hit.append(a))
    scs.append(sc)
for name, key, mods, note in TESTS:
    hit.clear()
    QTest.keyClick(w, key, mods)
    APP.processEvents()
    QTest.qWait(60)
    APP.processEvents()
    print(f"  {name:<14} key={key:#06x} mods={mods:#x} -> 触发 {hit or '（无）'}   {note}")

print("\n--- d) 修饰键单独按下会不会误触发（只装 Shift 的非法行）---")
hit.clear()
QTest.keyClick(w, Qt.Key_Shift, Qt.ShiftModifier)
APP.processEvents()
print(f"  Shift alone -> {hit or '（无）'}")
print("\nDONE")
