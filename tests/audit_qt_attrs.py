"""静态排查：代码里写的 `Qt类名.成员`、`这个变量（=Qt类实例）.成员`、`self.<属性>（=Qt类实例）.成员`
在本机 PyQt5 上真的存在吗。

动机：app/main.py 曾用 `QLocalServer.NoError`，而本机 PyQt5 5.15.10 根本没把
QLocalServer 的 ServerError 枚举成员重导出成类属性（dir() 里一个枚举名都没有）——
这种坑只有跑到那一行才炸，而"跑到那一行"意味着用户压根打不开播放器。
这里不拉起界面，只用 AST 收集 app/ 下所有 `从 PyQt5 导入的类 . 成员` 写法，
逐条 hasattr 验证。

第二遍（实例级）是后来补的，因为同一类坑换了个形状又中了一次：`sc.clearShortcut()`。
C++ 的 QShortcut 有 clearShortcut，PyQt5 5.15.2 的 SIP 没包出来（hasattr 实测 False），
而那行写在"重装快捷键"的循环里 —— 只有用户按第二次"保存"才走到，且它是 Qt 槽里的
AttributeError：不是 traceback，是进程直接没了。类级审计看不见它，因为接收者是小写变量。

第三遍（调用点参数表）同样是被闪退逼出来的：keymap_dialog 的 `_validate(self, msg="")`
定义里只有一个参数，而"恢复默认"和"保存"两个按钮写成 `self._validate("…", ok=True)`。
前两遍都放行——接收者是 self，`_validate` 这个名字也确实存在，只有参数表不匹配。它炸在
Qt 槽里就是 TypeError，症状是"点保存就闪退"，跟属性缺失同一个死法。这一遍不查 PyQt5，
查的是 app/ 里"同一文件同一个类里 self.方法(…)"的调用点与定义的签名对不对得上。

第四遍（实例属性成员）补的是第二遍的形状缺口：第二遍只认"本作用域里赋成唯一一个 Qt
构造结果的小写变量"，而 app/ 里 333 处 Qt 成员调用的接收者是 `self.<属性>`（`self._pool.`、
`self.vol.`、`self.lbl_time.` 这种，属性在本类的 `__init__` 里赋值、在别的方法里使用）——
第二遍一条都看不见。这一类里最典型的就是 `self._pool.waitForFinished(...)`：本机 PyQt5
5.15.10 的 QThreadPool 只有 waitForDone，waitForFinished 是 Qt6 才有的名字（hasattr 实测
False），写成这样就是"缩略图跑完那一刻线程池回调里 AttributeError"。第四遍按"同一个类体里
这个属性只被赋成过唯一一个 Qt 构造结果"推断类型，规则与第二遍同样的保守（不作废就宁可漏判）。
今天 333 处全对得上，所以这一遍现在报 0 条——它是给"下一次换个形状的中招"预备的网，不是
用来证明现在漏了东西。

用法：
    python tests/audit_qt_attrs.py             # 扫 app/（退出码 0=全部对得上）
    python tests/audit_qt_attrs.py --selftest  # 跑审计自己的已知答案对照（含它看不见的那类）
"""
from __future__ import annotations

import ast
import importlib
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, ROOT)


def imported_modules(tree: ast.AST) -> dict[str, str]:
    """本文件里的 名字 -> 来源模块（只关心 PyQt5.*）。"""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                out[a.asname or a.name] = node.module
        elif isinstance(node, ast.Import):
            for a in node.names:
                out[(a.asname or a.name).split(".")[0]] = a.name
    return out


def collect() -> set[tuple[str, str, str, str]]:
    """返回 (模块, 类名, 成员名, 出处) 四元组集合。"""
    refs = set()
    for rel, src in sources():
        names = imported_modules(ast.parse(src, rel))
        for node in ast.walk(ast.parse(src, rel)):
            if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                continue
            cls, attr = node.value.id, node.attr
            mod = names.get(cls, "")
            if not mod.startswith("PyQt5") or not cls[:1].isupper():
                continue
            refs.add((mod, cls, attr, f"{rel}:{node.lineno}"))
    return refs


SYNTHETIC: list[tuple[str, str]] | None = None     # --selftest 用它替换扫描范围


def sources():
    if SYNTHETIC is not None:
        yield from SYNTHETIC
        return
    base = os.path.join(ROOT, "app")
    for dirpath, _dirs, files in os.walk(base):
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, ROOT).replace("\\", "/")
            yield rel, open(p, encoding="utf-8").read()


def _py_side(attr: str) -> bool:
    """这个成员名是不是"Python 侧挂在 Qt 对象上的属性"，而不是 Qt 的成员。

    为什么第二遍与第四遍都要放过它：Qt 的公开成员**没有一个以下划线开头**（SIP 包出来的
    名字全部照 C++ 原名），而"往控件上挂一个自己的字段"是 PyQt 的常规写法，本仓库早就在用
    （`paint_choice` 里的 `cb._gelu_custom`、`add_check` 里的 `c._row_label`）。
    不放过的话，判据会把这类字段报成"QCheckBox 没有 _row_label"——那是一条**真红假故障**：
    字段本来就不该在 Qt 上有，读它的是我们自己的代码。

    放过之后谁接班：这类字段拼错了不会静默，它会在**用它的那条判据**里当场炸
    （`controls_cover_table` 用 `getattr(c, "_row_label", None) is None` 作前提，
    读不到就直接判红），而运行时真读一个不存在的字段是 AttributeError，不是"默默没生效"。
    少的那一点覆盖是"下划线开头的 Qt 私有槽被手滑调错"——本仓库没有一处手调 Qt 私有槽。
    """
    return attr.startswith("_")


def collect_instances() -> set[tuple[str, str, str, str]]:
    """实例成员引用：`sc = QShortcut(...)` 之后的 `sc.clearShortcut()`。

    上面那份类成员审计看不见这一类——接收者是小写变量名，不是类名。而本机正是这样中的：
    PyQt5 5.15.2 的 QShortcut **没有** clearShortcut（C++ 里有、SIP 没包）。这种写法不会
    在导入期报错，只在"用户保存了一次快捷键"那一路径上炸，而它炸在 Qt 槽里 —— 症状不是
    traceback，是进程当场没了（rc=127、stderr 全空）。

    收法保守：只认"同一作用域里这个变量只被赋成过这一个 Qt 类的构造结果"，别的信息
    一律作废（赋值过任何非 Qt 构造就打上哨兵，宁可漏判不误判——误判会让人学会忽略这份报告）。
    """
    refs = set()
    for rel, src in sources():
        tree = ast.parse(src, rel)
        names = imported_modules(tree)
        scopes = [("<module>", tree)]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append((node.name, node))
        for _scope, body in scopes:
            types: dict[str, set[str]] = {}

            def bind(var: str, token: str) -> None:
                types.setdefault(var, set()).add(token)

            for node in ast.walk(body):
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if not isinstance(t, ast.Name):
                            continue
                        v = node.value
                        qt_cls = (v.func.id if isinstance(v, ast.Call)
                                  and isinstance(v.func, ast.Name)
                                  and names.get(v.func.id, "").startswith("PyQt5")
                                  and v.func.id[:1].isupper() else "?")
                        bind(t.id, qt_cls)
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    if isinstance(node.target, ast.Name):
                        bind(node.target.id, "?")
                elif isinstance(node, ast.For):
                    for t in _names_of(node.target):
                        bind(t, "?")            # 循环变量的类型由被迭代的集合决定，猜不得
                elif isinstance(node, ast.comprehension):
                    for t in _names_of(node.target):
                        bind(t, "?")            # 推导式里的 c / row 会和函数里同名的 Qt 对象撞车
                elif isinstance(node, ast.NamedExpr):     # 海象 :=
                    if isinstance(node.target, ast.Name):
                        bind(node.target.id, "?")
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    bind(node.name, "?")
                elif isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
                    bind(node.optional_vars.id, "?")
                elif isinstance(node, ast.arg):
                    bind(node.arg, "?")        # 形参可以是任何东西
            for node in ast.walk(body):
                if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
                    continue
                got = types.get(node.value.id)
                if not got or len(got) != 1 or "?" in got:
                    continue
                if _py_side(node.attr):
                    continue                     # Python 侧挂在控件上的字段，不是 Qt 成员
                cls = next(iter(got))
                refs.add((names[cls], cls, node.attr, f"{rel}:{node.lineno}"))
    return refs


def _names_of(target: ast.AST) -> list[str]:
    """`for c, _ in …` 这种元组目标里的每个变量名。"""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for el in target.elts:
            out += _names_of(el)
        return out
    return []


def _self_attr_of(target: ast.AST) -> list[str]:
    """`self.vol = …` / `for self.item in …` / `self.a, self.b = …` 目标里的属性名。

    只认挂在 `self` 上的那一跳；`obj.a` 这种别的接收者不算（那是别人的对象，类型由别人定）。
    """
    if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
        return [target.attr]
    if isinstance(target, (ast.Tuple, ast.List)):
        out = []
        for el in target.elts:
            out += _self_attr_of(el)
        return out
    return []


def collect_attrs() -> set[tuple[str, str, str, str]]:
    """第四遍：接收者是 `self.<属性>` 的 Qt 成员引用（属性在本类里赋成唯一一个 Qt 构造结果）。

    第二遍只能认"本作用域里赋成唯一一个 Qt 构造的小写变量"，`self.` 打头的那种一律不认。
    而 app/ 里的 Qt 成员调用有 333 处接收者是 `self.<属性>` ——`sc.clearShortcut()` 换了
    `self.sc.clearShortcut()` 这个形状，第二遍就放行了，症状还是那条最坏的：AttributeError
    在 Qt 槽里 = rc=127、stderr 全空、进程直接没了。真名字是现成的：`self._pool.waitForFinished()`
    （本机 QThreadPool 只有 waitForDone，waitForFinished 是 Qt6 才有的）。

    推断规则与第二遍一样的保守，判得准比判得多重要：
      * 只认"同一个类体里这个属性**只**被赋成过一个 Qt 类的构造结果"；赋过工厂函数返回值
        (`self.sc = make_shortcut()`)、赋过 None、赋成两个不同 Qt 类、`+=`、`del`、当循环目标
        —— 一律打哨兵作废，这个属性的所有引用都不判。
      * 属性的赋值必须在**本类体**里出现；本类没赋过（基类赋的、别处 `mw.vol = …` 赋的）看不见。
      * 三级链 `self.vol.valueChanged.connect(…)` 只判 `self.vol` 那一跳（valueChanged 是不是
        QSlider 真有的名字），最外层那一跳（connect）不判——它的类型是信号对象，不在这一遍的账上。
      * 内嵌类的 `self.x` 与本类的 `self.x` 不算同一个（先把嵌套类的节点摘出去）。
    """
    refs = set()
    for rel, src in sources():
        tree = ast.parse(src, rel)
        names = imported_modules(tree)

        def qt_ctor(v: ast.expr) -> str:
            """这个赋值右边的表达式能不能确定是"某个 PyQt5 类的构造结果"。不能就回 '?'。"""
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
                c = v.func.id
                if names.get(c, "").startswith("PyQt5") and c[:1].isupper():
                    return c
            return "?"

        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            skip: set[int] = set()               # 嵌套类里的节点不记到外层类头上
            for n in ast.walk(cls):
                if isinstance(n, ast.ClassDef) and n is not cls:
                    skip |= {id(x) for x in ast.walk(n)}
            types: dict[str, set[str]] = {}

            def bind(attr: str, token: str) -> None:
                types.setdefault(attr, set()).add(token)

            for node in ast.walk(cls):
                if id(node) in skip:
                    continue
                if isinstance(node, ast.Assign):
                    toks = [qt_ctor(node.value)] * len(node.targets)
                    for t, tok in zip(node.targets, toks):
                        for a in _self_attr_of(t):
                            if isinstance(t, (ast.Tuple, ast.List)):
                                bind(a, "?")     # 元组解包对不上左右，猜不得
                            else:
                                bind(a, tok)
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    for a in _self_attr_of(node.target):
                        bind(a, "?")             # += 之后的值跟初值不是一回事；只标注没赋值也无从判
                elif isinstance(node, (ast.For, ast.comprehension)):
                    for a in _self_attr_of(node.target):
                        bind(a, "?")             # 循环/推导式目标：类型由被迭代的集合决定
                elif isinstance(node, ast.Delete):
                    for t in node.targets:
                        for a in _self_attr_of(t):
                            bind(a, "?")          # 删过再赋，中间读到的可能是别的
            for node in ast.walk(cls):
                if id(node) in skip:
                    continue
                if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)):
                    continue
                inner = node.value
                if not (isinstance(inner.value, ast.Name) and inner.value.id == "self"):
                    continue                     # 三级链的最外层那一跳：接收者是 self.<属性>.<成员>
                got = types.get(inner.attr)
                if not got or len(got) != 1 or "?" in got:
                    continue
                c = next(iter(got))
                if _py_side(node.attr):
                    continue                     # 同第二遍：下划线开头的是自家字段，不归 Qt 成员账
                refs.add((names[c], c, node.attr, f"{rel}:{node.lineno} self.{inner.attr}"))
    return refs


def find_bad(refs) -> tuple[int, list[tuple[str, str, str, str, str]]]:
    """逐条 hasattr。回 (检查了多少条, [(模块, 类, 成员, 出处, 原因)])。

    回结构化元组而不是现成的句子：这份审计的自检要按 (类, 成员) 点名核对，
    从渲染好的字符串里切词，切错一个空格就悄悄变成"没抓到"或"误报"。

    `cls` 有两种身份，得分开对待：一种是 `PyQt5.QtWidgets` 里的 `QWidget`（类），
    另一种是 `from PyQt5 import QtCore` 之后写出来的 `PyQt5.QtCore`（**子模块**）。
    后者在只 `import PyQt5` 的进程里 `hasattr` 恒为 False——本机实测：
    `hasattr(PyQt5, "QtCore")` 是 False，补一句 `from PyQt5 import QtCore` 就变 True，
    而 `QtCore.PYQT_VERSION_STR` 一直存在。所以拿不到属性时先试着把子模块导出来，
    导得出来就继续查成员，导不出来才许说"PyQt5 里没有 X"。少了这一步，审计器会把
    **自己没导**写成**产品没有**：app/doctor.py 那两行（PYQT_VERSION_STR / QT_VERSION_STR）
    就这么连着报了两条假红。
    """
    total = 0
    out: list[tuple[str, str, str, str, str]] = []
    cache: dict[str, object] = {}
    for mod, cls, attr, where in sorted(refs):
        total += 1
        if mod not in cache:
            try:
                cache[mod] = importlib.import_module(mod)
            except Exception as exc:
                cache[mod] = f"__import_failed__: {exc}"
        m = cache[mod]
        if isinstance(m, str):
            out.append((mod, cls, attr, where, f"导入 {mod} 失败：{m}"))
            continue
        target = getattr(m, cls, None)
        if target is None:
            try:
                target = importlib.import_module(f"{mod}.{cls}")
            except Exception:
                target = None
        if target is None:
            out.append((mod, cls, attr, where, f"{mod} 里没有 {cls}"))
            continue
        if not hasattr(target, attr):
            out.append((mod, cls, attr, where, "本机 PyQt5 上不存在"))
    return total, out


def show(label: str, bad) -> list[str]:
    return [f"[FAIL] {label}：{mod}.{cls}.{attr} {why} —— {where}"
            for mod, cls, attr, where, why in bad]


def collect_calls() -> tuple[int, list[tuple[str, int, str, str, str, str]]]:
    """第三遍：同文件里的 `self.某方法(…)` 调用点，参数名与个数对得上定义吗。

    真中过的一次：keymap_dialog 里 `_validate(self, msg="")` 的定义就这一个参数，而
    "恢复默认"和"保存"两个按钮的处理函数写成 `self._validate("…", ok=True)`。前两份审计
    都看不见它——接收者是 self，`_validate` 这个名字也确实存在，只有**参数表**不匹配。
    症状还是那一条最坏的：TypeError 发生在 Qt 槽里，没有 traceback，进程直接没了
    （rc=127、stderr 全空）。用户那边看到的是"点保存就闪退"。

    看得见的范围（也是明说的盲区）：只查"调用点与定义在同一个文件同一个类里"的 self 调用。
    方法定义在基类或别的模块 → 这里跳过；通过别的对象（`dlg._validate(…)`）调用 → 那一类
    归实例成员审计的形状，本遍不管；带 `*xs` / `**kw` 的调用无法计数，跳过；定义里吃
    `**kw` 的，任何参数名都合法，也不报。
    """
    bad: list[tuple[str, int, str, str, str, str]] = []
    total = 0
    for rel, src in sources():
        tree = ast.parse(src, rel)
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            # 名字 -> (全部参数名, 位置参数表(有序), 必填个数, 有 *args, 有 **kwargs)
            sigs: dict[str, tuple[set[str], list[str], int, bool, bool]] = {}
            for fn in [n for n in cls.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                a = fn.args
                pos = [x.arg for x in a.posonlyargs + a.args if x.arg != "self"]
                kwonly = [x.arg for x in a.kwonlyargs]
                sigs[fn.name] = (set(pos + kwonly), pos, len(pos) - len(a.defaults),
                                 a.vararg is not None, a.kwarg is not None)
            for node in ast.walk(cls):
                g = node.func if isinstance(node, ast.Call) else None
                if not (isinstance(g, ast.Attribute) and isinstance(g.value, ast.Name)
                        and g.value.id == "self"):
                    continue
                name = g.attr
                if name not in sigs:
                    continue                       # 定义在基类/别处：这遍看不见，见上面盲区
                total += 1
                names, pos, required, has_star, has_dstar = sigs[name]
                starred = any(isinstance(x, ast.Starred) for x in node.args)
                kw_starred = any(k.arg is None for k in node.keywords)
                kwn = [k.arg for k in node.keywords if k.arg]
                given = len(node.args)
                if not kw_starred:
                    for kw in kwn:
                        if kw not in names and not has_dstar:
                            bad.append((rel, node.lineno, cls.name, name, kw,
                                        "定义里没有这个参数名"))
                if starred or kw_starred:
                    continue                       # 展开实参没法数，宁可不判
                for kw in kwn:
                    if kw in pos[:given]:
                        bad.append((rel, node.lineno, cls.name, name, kw,
                                    "同一个参数既按位置又按名字给"))
                if given > len(pos) and not has_star:
                    bad.append((rel, node.lineno, cls.name, name, f"位置 {given} 个，定义里 {len(pos)} 个",
                                "位置参数超出定义"))
                missing = [p for i, p in enumerate(pos[:required]) if i >= given and p not in kwn]
                if missing:
                    bad.append((rel, node.lineno, cls.name, name, "、".join(missing),
                                "必填参数没给（位置与关键字都没覆盖）"))
    return total, bad


def main() -> int:
    groups = [("类成员", collect()), ("实例成员", collect_instances()), ("实例属性成员", collect_attrs())]
    total = 0
    lines: list[str] = []
    for label, refs in groups:
        if not refs:
            print(f"---- {label}：没收集到任何引用——扫描范围或导入写法变了，这本身就是失败。")
            return 1
        n, bad = find_bad(refs)
        total += n
        lines += show(label, bad)
    n_calls, bad_calls = collect_calls()
    if not n_calls:
        print("---- 调用点：一个 self.方法(…) 都没扫到——app/ 空了或全换写法了，这本身就是失败。")
        return 1
    for rel, lineno, cname, fname, what, why in bad_calls:
        lines.append(f"[FAIL] 调用点：{rel}:{lineno} 的 self.{fname}({cname}) {why} —— {what}")
    for ln in lines:
        print(ln)
    print(f"\n---- 排查汇总：{total} 条成员引用（类级 + 实例级 + 实例属性级）+ {n_calls} 处 self 调用点，"
          f"{len(lines)} 条在本机对不上 ----")
    return 1 if lines else 0


# ---------------------------------------------------------------- 自检
# 这份审计自己也得有已知答案对照，而且要把它**看不见什么**一并钉住：局部变量那一遍只能认
# "这个变量在本作用域里被直接赋成过唯一一个 Qt 类的构造结果"，for 目标、形参、推导式里的
# 同名变量一律作废。作废换来的是不误报（误报会让人学会忽略这份报告），代价是
# `for sc in self._shortcuts: sc.clearShortcut()` 这种形状它看不见 —— 那一类由
# tests/test_ui.py 的"连装两遍快捷键后按一次只触发一次"在**运行时**兜：真少了方法就是
# AttributeError，写在 Qt 槽里就是进程没了（rc=127、stderr 全空），跑不过去。
#
# 第四遍把另一条同样形状、同样死法的坑接了过来：接收者是 `self.<属性>`（属性在本类里赋成
# 唯一一个 Qt 构造结果）的 333 处调用点，局部变量那一遍一条都不认。它自己的盲区写在
# ATTR_CASES 里并逐条断言"一个都不许报"：工厂函数的返回值、本类从没赋过（基类或别处赋的）、
# 同一属性赋成两种 Qt 类、`for self.x in …` 的循环目标、赋成 None 或 del 过的属性（整类作废，
# 连之前那行对的一起不判）、以及三级链 `self.vol.valueChanged.connect(…)` 的最外那一跳——
# 最后一类由 tests/test_ui.py 在建窗口时兜：信号名写错，connect 当场 AttributeError。
_CAUGHT_SRC = '''
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QShortcut, QWidget


class Demo(QWidget):
    def caught(self):
        sc = QShortcut(Qt.Key_M, self)
        sc.clearShortcut()          # 已知坏：本机 QShortcut 没这个方法
        sc.setEnabled(False)        # 已知好
        sc.setParent(None)          # 已知好
'''

_BLIND_SRC = '''
from PyQt5.QtWidgets import QWidget


class Demo(QWidget):
    def blind(self):
        for sc in self._shortcuts:
            sc.clearShortcut()      # 已知看不见：sc 只来自 for 目标，类型无从判
'''

_SHADOW_SRC = '''
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QCheckBox


class Demo(QWidget):
    def shadows(self):
        c = QColor("#FFFFFF")
        for c, _ in [("a", 1)]:     # 同名循环变量：这里的 c 其实是字符串
            print(c.upper())
        w = QCheckBox("x")
        w = self._widgets.get("k")  # 同一变量又被赋成非 Qt 值 → 作废
        w.setText("y")
'''

_PY_SIDE_SRC = '''
from PyQt5.QtWidgets import QCheckBox


class Demo(QWidget):
    def link(self):
        c = QCheckBox("x")
        c._gelu_custom = True           # 已知好：Python 侧挂在控件上的字段（Qt 公开成员没一个下划线开头）
        print(c._row_label.text())      # 同上：读自家字段，不归 Qt 成员的账
        c.setText("y")                  # 已知好，而且必须照旧扫到（放过下划线不等于放过这一遍）
        c.geluNoSuchMethod()            # 已知坏：不带下划线的假 Qt 名字，漏了就是这一遍失职
'''

SELF_CASES = [
    ("直接赋成 Qt 类后调不存在的方法 → 必须点名", _CAUGHT_SRC,
     [("QShortcut", "clearShortcut")], [("QShortcut", "clearShortcut")],
     [("QShortcut", "setEnabled"), ("QShortcut", "setParent")], []),
    ("只来自 for 目标的同名变量 → 已知盲区（不许假装看得见）", _BLIND_SRC,
     [], [], [], [("QShortcut", "clearShortcut")]),
    ("同名循环变量 / 重复赋值 → 不许造出误报", _SHADOW_SRC,
     [], [], [], [("QColor", "upper"), ("QCheckBox", "setText")]),
    ("下划线开头的自家字段挂在控件上 → 不报；不带下划线的假 Qt 成员 → 照旧报", _PY_SIDE_SRC,
     [("QCheckBox", "setText"), ("QCheckBox", "geluNoSuchMethod")],
     [("QCheckBox", "geluNoSuchMethod")],
     [("QCheckBox", "setText")],
     [("QCheckBox", "_gelu_custom"), ("QCheckBox", "_row_label")]),
]

# 第四遍（接收者是 self.<属性> 的成员引用）的已知答案。三条样例各管一件事：该抓的换个形状
# 也得抓到、判不了的必须明说是盲区、作废过的一个都不许报。
_ATTR_CAUGHT_SRC = '''
from PyQt5.QtCore import Qt, QThreadPool
from PyQt5.QtWidgets import QLabel, QShortcut, QSlider, QWidget


class Demo(QWidget):
    def __init__(self):
        super().__init__()
        self._pool = QThreadPool(self)
        self.sc = QShortcut(Qt.Key_M, self)
        self.lbl = QLabel("x", self)
        self.vol = QSlider(self)

    def noop(self):
        pass

    def caught(self):
        self._pool.waitForFinished(3000)         # 已知坏：本机只有 waitForDone，waitForFinished 是 Qt6 的名字
        self.sc.clearShortcut()                  # 已知坏：第二遍那条坑换了"接收者是实例属性"的形状
        self.sc.setShortcut(Qt.Key_J)            # 已知坏：本机 QShortcut 连 setShortcut 都没包出来
        self.vol.nopeNotAName()                  # 已知坏：QSlider 没这个名字
        self._pool.setMaxThreadCount(4)          # 已知好
        self.lbl.setText("y")                    # 已知好
        self.sc.activated.connect(self.noop)     # 已知好且只判一跳：activated 在，connect 那跳不归本遍
'''

_ATTR_BLIND_SRC = '''
from PyQt5.QtWidgets import QCheckBox, QLabel, QWidget


class Demo(QWidget):
    def make(self):
        return None

    def __init__(self):
        super().__init__()
        self.sc = make_shortcut(self)     # 盲区：赋成工厂函数的结果，类型无从判
        self.other = None                 # 盲区：本类里从没赋成 Qt 构造（基类或别处赋的）
        self.w = QLabel(self)
        self.w = QCheckBox(self)          # 盲区：同一属性赋成两个不同 Qt 类 → 作废
        for self.item in []:              # 盲区：循环目标是 self.<属性>，类型由集合决定
            self.item.setText("z")

    def blind(self):
        self.sc.clearShortcut()           # 上面 sc 来自工厂函数 → 不许假装看得见
        self.other.clearShortcut()        # 上面 other 从没赋成 Qt 构造 → 不许猜
        self.w.setText("x")               # 上面 w 有两种赋法 → 不许二选一
'''

_ATTR_SHADOW_SRC = '''
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QCheckBox, QShortcut, QWidget


class Demo(QWidget):
    def __init__(self):
        super().__init__()
        self.box = QCheckBox("x", self)
        self.sc = QShortcut(Qt.Key_M, self)

    def use(self):
        self.box.setText("a")             # 这行本身没错，但下面把 box 作废了 → 整类不判（宁可漏判）
        self.box = None                   # 赋成 None → 这个属性的类型不再唯一
        self.sc.setShortcut(Qt.Key_J)     # 真坏的名字，可下面 del 过 → 这一遍不判，是明说的漏判
        del self.sc
'''

ATTR_CASES = [
    ("实例属性成员：换个形状的同一条坑（waitForFinished / clearShortcut / setShortcut / 造的名字）"
     "必须逐个点名，且三条正确写法一个都不许报", _ATTR_CAUGHT_SRC,
     [("QThreadPool", "waitForFinished"), ("QShortcut", "clearShortcut"),
      ("QShortcut", "setShortcut"), ("QSlider", "nopeNotAName"), ("QShortcut", "activated")],
     [("QThreadPool", "waitForFinished"), ("QShortcut", "clearShortcut"),
      ("QShortcut", "setShortcut"), ("QSlider", "nopeNotAName")],
     [("QThreadPool", "setMaxThreadCount"), ("QLabel", "setText"), ("QShortcut", "activated")],
     [("QShortcut", "connect")]),          # 三级链最外层那一跳不许挂到 QShortcut 的账上
    ("实例属性成员：自己声明的四个盲区（工厂函数返回值 / 本类没赋过 / 赋成两种 Qt 类 / "
     "循环目标）必须一个都不报——不许假装看得见", _ATTR_BLIND_SRC,
     [], [], [], [("QShortcut", "clearShortcut"), ("QLabel", "setText"), ("QCheckBox", "setText")]),
    ("实例属性成员：赋成 None / del 过的属性要整类作废，一个都不许报（漏判换来的不误报）",
     _ATTR_SHADOW_SRC, [], [], [], [("QCheckBox", "setText"), ("QShortcut", "setShortcut")]),
]

# 第三遍（self 调用点 vs 定义的参数表）的已知答案对照。
_CALLS_CAUGHT_SRC = '''
from PyQt5.QtWidgets import QWidget


class Demo(QWidget):
    def _validate(self, msg=""):
        pass

    def _set(self, a, b=""):
        pass

    def _need(self, a, b):
        pass

    def fine(self):
        self._validate("x")             # 已知好
        self._set("a", "b")             # 已知好
        self._need("a", "b")            # 已知好
        self._need("a", b="b")          # 已知好：同一个参数用关键写法给，不叫"给两遍"
        self._need(a="a", b="b")        # 已知好：全用关键字给满必填（旧写法在这里会误报）
        self._set("a", b="b")           # 已知好：可选项按名字给

    def bad(self):
        self._validate("x", ok=True)    # 已知坏：真中过一次的那条（Qt 槽里 TypeError=闪退）
        self._set("a", "b", "c")        # 已知坏：位置参数超出定义
        self._need()                   # 已知坏：必填的两个一个都没给
        self._set("a", a="z")           # 已知坏：同一个参数既按位置又按名字
'''

_CALLS_BLIND_SRC = '''
from PyQt5.QtWidgets import QWidget


class Other(QWidget):
    def _validate(self, msg=""):
        pass


class Demo(QWidget):
    def _note(self, **kw):
        pass

    def _star(self, a, b=1):
        pass

    def blind(self):
        self._note(ok=True)             # 盲区：定义吃 **kw，任何参数名都合法
        xs = ["a"]
        self._star(*xs)                 # 盲区：星号展开没法计数
        self.from_base(nope=1)          # 盲区：方法不在本文件本类里（基类／别处）
        o = Other(self)
        o._validate(msg="x", ok=True)   # 盲区：接收者不是 self（归实例成员那一遍的形状）
'''

CALL_CASES = [
    ("调用点参数表：四种坏法（多余参数名 / 位置超出 / 少必填 / 位置与名字重复）必须逐个点名，"
     "且六种正确写法一个都不许报", _CALLS_CAUGHT_SRC,
     [("Demo", "_validate", "定义里没有这个参数名"),
      ("Demo", "_set", "位置参数超出定义"),
      ("Demo", "_set", "同一个参数既按位置又按名字给"),
      ("Demo", "_need", "必填参数没给（位置与关键字都没覆盖）")]),
    ("调用点参数表：自己声明的四个盲区（**kw / 星号展开 / 基类方法 / 非 self 接收者）"
     "必须一个都不报", _CALLS_BLIND_SRC, []),
]

# find_bad 的已知答案。第三项（"导入失败"）的原因串里带异常原文，所以 expect 的第四项
# 按**子串**比对，别的三项仍是精确点名（模块/类/成员三样都得对上才算同一条）。
# 第四元素 unbind＝"跑这条之前要先把哪个子模块从父包上摘下来"。前两条必须有这一步：
# `hasattr(PyQt5, "QtCore")` 取决于**这个进程之前有没有 import 过它**，而 --selftest 前面
# 几遍早就把 QtWidgets 导进来了（它连带把 QtCore 也绑上），于是"真成员放行"这条会在
# 坏实现上也判绿——实测就是这样：不加这一步，把修法摘掉，selftest 照样全绿。摘掉之后
# 前提若不成立（仍绑着），这条自己判 FAIL 并写明"不作数"，不许闷声当自己过了。
RESOLVE_CASES = [
    ("子模块写法不误报：`from PyQt5 import QtCore` 之后的真成员必须放行（本轮假红的原样）",
     {("PyQt5", "QtCore", "PYQT_VERSION_STR", "case.py:1"),
      ("PyQt5", "QtCore", "QT_VERSION_STR", "case.py:2")}, [], "PyQt5.QtCore"),
    ("子模块导得开≠成员存在：QtCore 里没有的成员照样点名",
     {("PyQt5", "QtCore", "GELU_NO_SUCH_MEMBER", "case.py:3")},
     [("PyQt5", "QtCore", "GELU_NO_SUCH_MEMBER", "本机 PyQt5 上不存在")], "PyQt5.QtCore"),
    ("子模块压根不存在：不许被'先试 import 一下'兜过去",
     {("PyQt5", "GeluNoSuchModule", "anything", "case.py:4")},
     [("PyQt5", "GeluNoSuchModule", "anything", "PyQt5 里没有 GeluNoSuchModule")], None),
    ("模块本体导不开：单列成'导入失败'，一条都不许多报或漏报",
     {("PyQt5.GeluNoSuchModule", "Whatever", "x", "case.py:5")},
     [("PyQt5.GeluNoSuchModule", "Whatever", "x", "失败：")], None),
    ("老功能没被这步弄坏：`PyQt5.QtWidgets` 里的类成员，真存在的放行、不存在的名",
     {("PyQt5.QtWidgets", "QWidget", "show", "case.py:6"),
      ("PyQt5.QtWidgets", "QWidget", "gelu_no_such_method", "case.py:7")},
     [("PyQt5.QtWidgets", "QWidget", "gelu_no_such_method", "本机 PyQt5 上不存在")], None),
]


def _member_cases(cases, collect_fn) -> int:
    """跑一遍"成员引用"类样例：该扫到的、该报红的、该不报红的、压根不许扫到的，四类各核各的。

    第二遍与第四遍共用这套对照，只是传进来的收集器不同（一个认局部变量，一个认 self.<属性>）。
    """
    global SYNTHETIC
    fails = 0
    for label, src, must_collect, must_flag, ok_names, must_not in cases:
        SYNTHETIC = [("case.py", src)]
        refs = collect_fn()
        got = {(c, a) for _m, c, a, _w in refs}
        _n, bad = find_bad(refs)
        flagged = {(c, a) for _m, c, a, _w, _why in bad}
        missing = [x for x in must_collect if x not in got]
        unflagged = [x for x in must_flag if x not in flagged]
        false = [x for x in must_not if x in got or x in flagged]
        wrong_ok = [x for x in ok_names if x in flagged]
        passed = not missing and not unflagged and not false and not wrong_ok
        fails += 0 if passed else 1
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
        print(f"       扫到={sorted(got)} 报红={sorted(flagged)} ｜没扫到={missing} 没报红={unflagged} "
              f"误报={false} 好写法被报={wrong_ok}")
    return fails


def selftest() -> int:
    global SYNTHETIC
    fails = _member_cases(SELF_CASES, collect_instances)
    # 第四遍的三条对照同上，只是收集器换成 collect_attrs：它管的是 `self.<属性>.<成员>`。
    fails += _member_cases(ATTR_CASES, collect_attrs)

    # 解析那一步（find_bad）也要有已知答案。前三遍管的是"引用收没收进来"，这一遍管的是
    # "收进来之后有没有被正确判掉"：本轮的两条假红就出在这一步，而它原先**一条对照都没有**
    # ——所以它能在好代码上连报两行而 --selftest 照样全绿。
    for label, refs, expect, unbind in RESOLVE_CASES:
        pre = ""
        if unbind:
            pkg, _, sub = unbind.rpartition(".")
            m = importlib.import_module(pkg)
            if hasattr(m, sub):
                delattr(m, sub)          # 还原不了也没关系：下一句就把它重新导出来
            pre = f"前提：hasattr({pkg},{sub})={hasattr(m, sub)}"
            if hasattr(m, sub):
                print(f"[FAIL] {label}\n       前提不成立（子模块仍绑在父包上），这条不作数")
                fails += 1
                continue
        _n, bad = find_bad(refs)
        got = [(mod, cls, attr, why) for mod, cls, attr, _w, why in bad]

        def hit(x, e):
            return x[0] == e[0] and x[1] == e[1] and x[2] == e[2] and e[3] in x[3]

        wrong = ([] if len(got) == len(expect) else [f"条数：实际 {len(got)} 该 {len(expect)}"])
        wrong += [f"没点名 {e}" for e in expect if not any(hit(x, e) for x in got)]
        wrong += [f"多报 {x}" for x in got if not any(hit(x, e) for e in expect)]
        passed = not wrong
        fails += 0 if passed else 1
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
        print(f"       该报={sorted(set(e[:3] for e in expect))}\n       实际报={sorted(set(x[:3] for x in got))}"
              f"{' 对不上=' + str(wrong) if wrong else ''}"
              f"{(' ' + pre) if pre else ''}")

    # 第三遍（调用点参数表）的已知答案：该点名的三种坏法逐个点名，且它自己声明的四个
    # 盲区必须**一个都不报**——这一遍的存在理由就是"槽里 TypeError = 进程没了"，
    # 一旦它开始误报 `**kw`／基类方法，人就学会忽略这份报告，那条最坏的坑又没人管了。
    for label, src, expect in CALL_CASES:
        SYNTHETIC = [("case.py", src)]
        _n, bad = collect_calls()
        got = {(c, f, y) for _r, _l, c, f, _w, y in bad}
        wrong = sorted(got ^ set(expect))
        passed = not wrong
        fails += 0 if passed else 1
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
        print(f"       该报={sorted(set(expect))}\n       实际报={sorted(got)} 对不上={wrong}")
    SYNTHETIC = None
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(selftest() if "--selftest" in sys.argv else main())
