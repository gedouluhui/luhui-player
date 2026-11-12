"""探针 4：把 test_ui 里 rc=127 静默 abort 的那一段摘出来逐层剥离。

症状是"打完上一行 PASS 就没了，stderr 全空"——PyQt 里这是 C++ 侧 abort，Python 栈留不下。
嫌疑有两个，都是"把死写法当活键用"的下游：
  A. QTest.keyClick 递一个 0x01ffffff（Key_UNKNOWN）当键码；
  B. QShortcut 装了 QKeySequence("PgDn")（解析成 Key_UNKNOWN）以后再按真 PageDown。
逐个单独跑，看哪个把进程带走。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtGui import QKeySequence  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication, QShortcut, QWidget  # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)
UNKNOWN = 0x01ffffff
BASE_SEQ = "PgDown"          # 默认表 app/ui/keymap.py 里"下一个"那一行的写法
HITS: list[str] = []

w = QWidget()
w.resize(320, 200)
w.show()
QTest.qWaitForWindowExposed(w)
QTest.qWaitForWindowActive(w)


def step(name, fn):
    print(f"-- {name} 开始", flush=True)
    fn()
    print(f"-- {name} 活着回来", flush=True)


def a():
    QTest.keyClick(w, Qt.Key(UNKNOWN), Qt.KeyboardModifier(0))
    APP.processEvents()


def drop(sc):
    sc.setEnabled(False)
    sc.setParent(None)
    sc.deleteLater()
    APP.processEvents()


def b():
    sc = QShortcut(QKeySequence("PgDn"), w, context=Qt.WindowShortcut)
    sc.activated.connect(lambda checked=False: HITS.append("from_dead_spelling"))
    print(f"   QKeySequence('PgDn')[0]={int(QKeySequence('PgDn')[0]):#010x}", flush=True)
    QTest.keyClick(w, Qt.Key_PageDown, Qt.KeyboardModifier(0))
    APP.processEvents()
    print(f"   命中={HITS or '（无）'}", flush=True)
    drop(sc)


def c():
    """活写法装上、按一次该响；摘掉以后再按不该响（摘法不灵就会污染后面的判据）。"""
    sc = QShortcut(QKeySequence("PgDown"), w, context=Qt.WindowShortcut)
    sc.activated.connect(lambda checked=False: HITS.append("alive_probe"))
    HITS.clear()
    QTest.keyClick(w, Qt.Key_PageDown, Qt.KeyboardModifier(0))
    APP.processEvents()
    hit = list(HITS)
    drop(sc)
    QTest.qWait(60)
    APP.processEvents()
    HITS.clear()
    QTest.keyClick(w, Qt.Key_PageDown, Qt.KeyboardModifier(0))
    APP.processEvents()
    print(f"   装时命中={hit} 摘后命中={HITS or '（无）'}", flush=True)


def d():
    """摘法二：只 setEnabled(False) 不摘父——看它还在不在响应（判"孤儿"到底孤不孤）。"""
    sc = QShortcut(QKeySequence("PgUp"), w, context=Qt.WindowShortcut)
    sc.activated.connect(lambda checked=False: HITS.append("pgup_probe"))
    sc.setEnabled(False)
    APP.processEvents()
    HITS.clear()
    QTest.keyClick(w, Qt.Key_PageUp, Qt.KeyboardModifier(0))
    APP.processEvents()
    print(f"   只 setEnabled(False) 后命中={HITS or '（无）'}", flush=True)
    drop(sc)


def e():
    """同一条键序列上挂**两条** QShortcut（表里一条 + 探针一条）：Qt 还响不响？

    test_ui 的校准段就是这么写的：生效表里本来就有 ("PgDown","next")，再临时挂一条
    ("PgDown", 探针) 上去。跑真机时第二次按 PageDown **两条都没响**——要在这儿分清是
    "同序列两条互斥所以全不响"还是"窗口那会儿没焦点所以谁都不响"。
    """
    base = QShortcut(QKeySequence("PgDown"), w, context=Qt.WindowShortcut)
    base.activated.connect(lambda checked=False: HITS.append("base"))
    HITS.clear()
    QTest.keyClick(w, Qt.Key_PageDown, Qt.KeyboardModifier(0))
    APP.processEvents()
    print(f"   只挂表里那条：命中={HITS or '（无）'}", flush=True)
    dup = QShortcut(QKeySequence("PgDown"), w, context=Qt.WindowShortcut)
    dup.activated.connect(lambda checked=False: HITS.append("dup"))
    HITS.clear()
    QTest.keyClick(w, Qt.Key_PageDown, Qt.KeyboardModifier(0))
    APP.processEvents()
    print(f"   同序列挂到两条：命中={HITS or '（无）'}", flush=True)
    QTest.qWait(80)
    HITS.clear()
    QTest.keyClick(w, Qt.Key_PageDown, Qt.KeyboardModifier(0))
    APP.processEvents()
    print(f"   等 80ms 再按一次：命中={HITS or '（无）'}", flush=True)
    drop(dup)
    drop(base)
    HITS.clear()
    QTest.keyClick(w, Qt.Key_PageDown, Qt.KeyboardModifier(0))
    APP.processEvents()
    print(f"   两条都摘掉后再按：命中={HITS or '（无）'}", flush=True)


def play(base_seq: str, other_seq: str | None = None) -> None:
    """装一条 base_seq，再可选装一条 other_seq，按两次 PageDown 看谁响。

    other_seq=None 表示只装一条，用来当对照组：只要单独装基准那条都不响，后面所有
    "两条互斥所以全不响"的结论都不作数（那就是焦点问题，不是抵消问题）。
    """
    names = {"base": [], "other": []}
    base = QShortcut(QKeySequence(base_seq), w, context=Qt.WindowShortcut)
    base.activated.connect(lambda checked=False: names["base"].append(base_seq))
    other = None
    if other_seq:
        other = QShortcut(QKeySequence(other_seq), w, context=Qt.WindowShortcut)
        other.activated.connect(lambda checked=False: names["other"].append(other_seq))
    for rnd in (1, 2):
        for d in names.values():
            d.clear()
        QTest.keyClick(w, Qt.Key_PageDown, Qt.KeyboardModifier(0))
        APP.processEvents()
        QTest.qWait(40)
        APP.processEvents()
        if other_seq:
            print(f"      第{rnd}轮 只 base({base_seq!r}) 响={bool(names['base'])}"
                  f" · 只 other({other_seq!r}) 响={bool(names['other'])}", flush=True)
        else:
            print(f"      第{rnd}轮 单装 {base_seq!r}：命中={names['base'] or '（无）'}",
                  flush=True)
    if other:
        drop(other)
    drop(base)


def f():
    """同一把键、几种写法（"PgDown" / "Page Down" / "PageDown" 解析成同一个键值）：
    两条一起挂会不会互相抵消？

    这决定 keymap.effective() 装表时要不要按**解析键值**去重：用户往配置里加一条
    "Page Down → 静音"，而默认表里已经有 "PgDown → 下一个"，串不同、键值相同。
    抵消的话死的就不止用户新加那行，连默认那行也一起哑——那是"改一个键位把另一个键位
    弄坏"，比装不上隐蔽得多。
    """
    for s in (BASE_SEQ, "Page Down", "PageDown", "PgDn"):
        qs = QKeySequence(s)
        k = int(qs[0]) if qs.count() else -1
        print(f"   解析 {s!r:<12} count={qs.count()} key={k:#010x} "
              f"toString={qs.toString()!r}", flush=True)
    print("   [对照] 单装表里那种写法：", flush=True)
    play(BASE_SEQ)
    print("   [对照] 单装 Page Down：", flush=True)
    play("Page Down")
    print("   表里那条 + 别名 Page Down：", flush=True)
    play(BASE_SEQ, other_seq="Page Down")
    print("   表里那条 + 别名 PageDown：", flush=True)
    play(BASE_SEQ, other_seq="PageDown")
    print("   表里那条 + 同一个串（探针 E 复现，验不是抖动）：", flush=True)
    play(BASE_SEQ, other_seq=BASE_SEQ)


steps = [("B 装死写法", b), ("C 装卸活写法", c), ("D 只禁不用摘父", d), ("E 同序列两条", e),
         ("F 别名撞键", f)]
if "--with-a" in sys.argv:      # A 会把进程带走（这正是它要证明的事），所以默认不跑，
    steps.append(("A 未知键码", a))   # 否则这份探针没法重复执行
for name, fn in steps:
    try:
        step(name, fn)
    except BaseException as exc:       # noqa: BLE001
        print(f"-- {name} 抛 {type(exc).__name__}: {exc}", flush=True)

print("DONE", flush=True)
