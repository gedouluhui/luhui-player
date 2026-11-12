"""探针：QKeySequence 对本机 Qt5 的解析行为，逐条量清楚再决定快捷键表怎么写。

要回答的三个问题（都直接决定 app/ui/keymap.py 的形状）：
  1. 大写字母（"K"）与 "Shift+K" 解析出来是同一个序列吗？—— 若是，默认表里
     ("J", ...) 与 ("Shift+J", ...) 就是同一把键，谁生效看 Qt 的心情。
  2. 每条默认序列能否"解析 → toString() → 再解析"原样回来？—— 不能回的不许进配置。
  3. 非法串（"NotAKey"、""、"Ctrl++"）解析出什么？isEmpty 能不能当判据？
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from PyQt5.QtCore import Qt                    # noqa: E402
from PyQt5.QtGui import QKeySequence           # noqa: E402
from PyQt5.QtWidgets import QApplication       # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)  # QtGui 要用 QKeySequence 必须有 app 吗？实测

sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
from app.ui.main_window import KEYMAP          # noqa: E402


def keys(qs: QKeySequence) -> list[str]:
    out = []
    for i in range(qs.count()):
        k = qs[i]
        out.append(f"{k:#010x}")
    return out


print(f"Qt = {__import__('PyQt5.QtCore', fromlist=['QT_VERSION_STR']).QT_VERSION_STR}")
print("\n--- 1) 大小写字母是不是同一把键 ---")
for a, b in (("K", "Shift+K"), ("J", "Shift+J"), ("k", "K"), ("Z", "Shift+Z"),
             ("V", "Shift+V"), ("U", "Shift+U"), ("S", "Shift+S"), ("L", "Shift+L"),
             ("E", "Shift+E"), ("I", "Shift+I"), ("F", "Shift+F"), ("A", "Shift+A"),
             ("C", "Shift+C"), ("G", "Shift+G"), ("T", "Shift+T"), ("B", "Shift+B")):
    qa, qb = QKeySequence(a), QKeySequence(b)
    print(f"  {a!r:<14}={keys(qa)}  {b!r:<14}={keys(qb)}  相等={qa == qb}")

print("\n--- 2) 默认表逐条：isEmpty / toString 往返 / 冲突 ---")
seen: dict[str, list[str]] = {}
bad = []
for seq, act, _d in KEYMAP:
    qs = QKeySequence(seq)
    canon = qs.toString()
    rt = QKeySequence(canon)
    ok_rt = (rt == qs)
    key = canon
    seen.setdefault(key, []).append(act)
    flag = "" if (not qs.isEmpty() and ok_rt) else "  <<< 有问题"
    if flag:
        bad.append(seq)
    print(f"  {seq:<16} isEmpty={qs.isEmpty()} count={qs.count()} canon={canon!r:<18}"
          f" 往返={'ok' if ok_rt else 'DIFF'}{flag}")

print("\n--- 3) 冲突（同一 canon 绑多个动作）---")
for k, acts in seen.items():
    if len(set(acts)) > 1:
        print(f"  {k!r} -> {acts}")
dups = [k for k, acts in seen.items() if len(acts) > 1]
print(f"  合计 {len(dups)} 处")

print("\n--- 4) 非法串长什么样 ---")
for s in ("", "   ", "NotAKey", "Ctrl++", "Ctrl+", "+", ";;", "F13", "MediaPlay",
          "Ctrl+Alt+Shift+Meta+X", "Space", "  Space  ", "A B", "Ctrl+Shift+Alt+Del",
          "中文", "0", "-", "=", "[", "]", "\\", ",", "<", ">", "PgUp", "PgDn",
          "Alt+Return", "Enter", "Escape", "Media Stop", "Ctrl+Compose"):
    try:
        qs = QKeySequence(s)
        print(f"  {s!r:<28} isEmpty={qs.isEmpty()} count={qs.count()} canon={qs.toString()!r}"
              f" 往返={'ok' if QKeySequence(qs.toString()) == qs else 'DIFF'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {s!r:<28} 抛 {type(exc).__name__}: {exc}")

print("\n--- 5) 多键序列（Qt 允许 4 段）会不会静默变成永远按不出的键 ---")
for s in ("A B", "Ctrl+A Ctrl+B"):
    qs = QKeySequence(s)
    print(f"  {s!r}: count={qs.count()} isEmpty={qs.isEmpty()}")

print("\n--- 6) 只有修饰键的行能不能按出来 ---")
for s in ("Shift", "Ctrl+Shift", "Shift+Control"):
    qs = QKeySequence(s)
    print(f"  {s!r}: isEmpty={qs.isEmpty()} canon={qs.toString()!r}")
