"""逐屏体检有没有"漏了一页"的结构性尺子：`capture_sweep.py` 那张 SURFACES 表与侧栏页签的对账。

为什么单独有这一把（2026-10-12，§6 第 34 条的后续）：加「声音均衡」那一页时，
`capture_sweep.py` 侧要动**四处**（`SURFACES` 一行、`s_sidebar_*` 一个捕法、捕法里
`self._sidebar(N)` 的下标、`widget_of` 的 table 一条）。四处都是**手抄名单**，少抄任何一处
都不会报错——那一页就是"永远没有档"，而整趟体检照旧全绿。这与"五把尺抄着旧页名"是同一类病，
只是发生在留档侧而不是判据侧。

这一把量具**不建窗口、不抓屏、不写文件**：它只把两张表从源码里解析出来对账。所以
- 屏幕睡着也跑得动（不像 `capture_sweep.py` 本身，它在离屏装配下会把七张表面集体读成贴地板，
   hence 拒跑 rc=2）；
- 与看门狗那条腿不构成"会改文件的扫并跑"（它一个字节都不落 `tests/out`）。

跑法：
    python tests/probe_surface_cover.py            # 验收：判据全绿并把对账表打印出来（条数由脚本自己数）
    python tests/probe_surface_cover.py --falsify  # 证伪：内存里摘掉承重件，逐条要求点名

退出码：0 全绿；1 有判据红（或坏样本没抓到）；2 连测量对象都没有（两份源码读不到／结构解析不出）。
"""
from __future__ import annotations

import ast
import os
import sys

if hasattr(sys.stdout, "reconfigure"):     # Windows 控制台默认 GBK，中文判据名会糊
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
MW = os.path.join(ROOT, "app", "ui", "main_window.py")
SW = os.path.join(ROOT, "tests", "capture_sweep.py")


def say(*a) -> None:
    """每行当场落屏：长趟跑到第几条得看得见，不许等收尾一次性刷。"""
    print(*a, flush=True)


class CannotMeasure(Exception):
    """没有测量对象（文件缺、结构解析不出）——那是 rc=2，不是产品红。"""


# ---------------------------------------------------------------- 解析（只读源码，不 import Qt）

def _assign_value(tree: ast.AST, name: str, owner: str = "") -> ast.expr:
    """找 `name = <value>` 或 `name: <ann> = <value>`；owner 非空时只在该类体内找。"""
    nodes = [tree]
    if owner:
        nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == owner]
        if not nodes:
            raise CannotMeasure(f"找不到类 {owner}")
    for root in nodes:
        for n in ast.walk(root):
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == name for t in n.targets):
                return n.value
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) \
                    and n.target.id == name and n.value is not None:
                return n.value
    raise CannotMeasure(f"找不到赋值 {name}（owner={owner or '<模块>'}）")


def _attr_assign_value(tree: ast.AST, attr: str) -> ast.expr:
    """找 `self.<attr> ... = <value>`（Assign 与 AnnAssign 两种写法都认）。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == attr for t in n.targets):
            return n.value
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Attribute) \
                and n.target.attr == attr and n.value is not None:
            return n.value
    raise CannotMeasure(f"找不到 self.{attr} 的赋值")


def _str(el: ast.expr) -> str:
    if isinstance(el, ast.Constant) and isinstance(el.value, str):
        return el.value
    raise CannotMeasure(f"这一项不是字符串字面量：{ast.dump(el)[:60]}")


def _int(el: ast.expr) -> int:
    if isinstance(el, ast.Constant) and isinstance(el.value, int):
        return el.value
    raise CannotMeasure(f"这一项不是整数字面量：{ast.dump(el)[:60]}")


def pages_from_main_window() -> list[str]:
    """侧栏页名，按 `tab_defs` 的字面顺序——与产品同源，不抄第二份名单。"""
    with open(MW, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    val = _attr_assign_value(tree, "tab_defs")
    if not isinstance(val, ast.Tuple):
        raise CannotMeasure("tab_defs 不是元组字面量")
    out = []
    for item in val.elts:
        if not isinstance(item, ast.Tuple) or not item.elts:
            raise CannotMeasure("tab_defs 的某一位不是两位元组（形状纪律被破了）")
        out.append(_str(item.elts[0]))
    if not out:
        raise CannotMeasure("tab_defs 是空的")
    return out


class SweepTables:
    """`capture_sweep.py` 里那三处手抄名单，一次解析完。"""

    def __init__(self, src: str) -> None:
        tree = ast.parse(src)
        # ① SURFACES：键 → 表面中文名（第三个位）
        val = _assign_value(tree, "SURFACES")
        if not isinstance(val, ast.List):
            raise CannotMeasure("SURFACES 不是列表字面量")
        self.surfaces: dict[str, str] = {}
        for item in val.elts:
            if not isinstance(item, ast.Tuple) or len(item.elts) < 3:
                raise CannotMeasure("SURFACES 的某一行不足三位")
            key = _str(item.elts[0])
            if key in self.surfaces:
                raise CannotMeasure(f"SURFACES 有重复键 {key}")
            self.surfaces[key] = _str(item.elts[2])
        # ② 捕法：`s_sidebar_x` 里 `self._sidebar(N)` 的那个 N
        self.pick: dict[str, int] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name.startswith("s_sidebar_"):
                slug = n.name[len("s_sidebar_"):]
                idxs = []
                for c in ast.walk(n):
                    if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                            and c.func.attr == "_sidebar" and c.args:
                        idxs.append(_int(c.args[0]))
                if len(idxs) != 1:
                    raise CannotMeasure(f"{n.name} 里 _sidebar(...) 调用不是恰好一次（实得 {idxs}）")
                self.pick[slug] = idxs[0]
        # ③ widget_of 的 table：键 → 有没有条目
        self.roots: dict[str, bool] = {}
        wd = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "widget_of"]
        if not wd:
            raise CannotMeasure("找不到 widget_of")
        table = None
        for n in ast.walk(wd[0]):
            if isinstance(n, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "table" for t in n.targets):
                table = n.value
        if not isinstance(table, ast.Dict):
            raise CannotMeasure("widget_of 里的 table 不是字典字面量")
        for k in table.keys:
            self.roots[_str(k)] = True


def load() -> tuple[list[str], SweepTables]:
    for p in (MW, SW):
        if not os.path.exists(p):
            raise CannotMeasure(f"读不到 {p}")
    with open(SW, encoding="utf-8") as fh:
        return pages_from_main_window(), SweepTables(fh.read())


# ---------------------------------------------------------------- 五判据

def sidebar_of_pages(pages: list[str], t: SweepTables) -> list[tuple[int, str, str, str]]:
    """把 (页下标, 页名, 表面键, 表面标签) 拼出来；表面键按标签里的页名认，认不到就留空。

    为什么不按 SURFACES 的**位置**认：SURFACES 里夹着整窗/控制条/菜单等三十来行，
    按下标读会把"第几行"当"第几页"，加一行非侧栏表面就全体错位——那正是本轮要治的病。
    """
    rows = []
    for i, name in enumerate(pages):
        hit = [(k, lab) for k, lab in t.surfaces.items()
               if k.startswith("sidebar-") and lab.endswith(name)]
        rows.append((i, name, hit[0][0] if hit else "", hit[0][1] if hit else ""))
    return rows


def slug_of(key: str) -> str:
    """`sidebar-audio-eq` → 捕法名尾段应当是 `audio_eq`。

    这条归一化不是凑数：Python 标识符不能带连字符，所以产品侧的键用 `-`、方法名用 `_`
    （`s_sidebar_audio_eq` 捕 `sidebar-audio-eq`）。第一版判据直接拿键尾段比方法名，
    于是均衡页被报成"有档没人拍"——那是判据瞎，不是产品漏（§6 第 34 条说的就是这类"红得没道理"）。
    """
    return key[len("sidebar-"):].replace("-", "_") if key.startswith("sidebar-") else key


def judges(pages: list[str], t: SweepTables) -> list[tuple[str, bool, str]]:
    """返回 [(判据名, 过不过, 详情)]。这一族全部是纯函数，喂什么结构出什么判决。"""
    out: list[tuple[str, bool, str]] = []
    rows = sidebar_of_pages(pages, t)
    sw_keys = sorted(k for k in t.surfaces if k.startswith("sidebar-"))

    # 第 0 条先问量具自己瞎不瞎：解析不出任何捕法/根，后面五条会集体"没问题"地绿。
    blind = not sw_keys or not t.pick or not t.roots
    out.append(("量具自己不瞎：三处名单都真解析出了东西（空表不算全绿）",
                not blind,
                f"侧栏表面={len(sw_keys)} 捕法={len(t.pick)} widget_of={len(t.roots)}"))

    missing = [f"{i}:{n}" for i, n, k, _ in rows if not k]
    out.append(("每一页都有一个侧栏表面档（少抄 SURFACES 一行不许静默过去）",
                not missing,
                f"页数={len(pages)} 侧栏表面={len(sw_keys)} 缺={missing or '无'}"))
    extra = [k for k in sw_keys if not any(k == kk for _i, _n, kk, _l in rows)]
    out.append(("侧栏表面不许反过来多出页签没有的（登记表与页签一一对应）",
                not extra, f"多出来的表面={extra or '无'}"))

    no_method = [k for k in sw_keys if slug_of(k) not in t.pick]
    out.append(("每个侧栏表面都有一个 `s_sidebar_*` 捕法（有档没人拍就是空表）",
                not no_method, f"捕法={len(t.pick)} 处，缺={no_method or '无'}"))

    idx = sorted(t.pick.values())
    want = list(range(len(pages)))
    dup = [i for i in set(idx) if idx.count(i) > 1]
    out.append(("捕法里的 `_sidebar(N)` 下标正好覆盖每一页、一页只一次（抄错页要在这里红）",
                idx == want, f"下标={idx} 应为={want} 重复={dup or '无'}"))

    no_root = [k for k in sw_keys if k not in t.roots]
    out.append(("每个侧栏表面在 `widget_of` 里有根（没根则走树的尺子一条也不看它）",
                not no_root, f"table 共 {len(t.roots)} 条，缺={no_root or '无'}"))

    mis = []
    for i, name, k, lab in rows:
        if k:
            slug = slug_of(k)
            if t.pick.get(slug) != i:
                mis.append(f"{name}：捕法拍的是第 {t.pick.get(slug)} 页")
    out.append(("表面标签与它实际拍的那一页同名（名单齐了但配错页，这条兜底）",
                not mis, f"错配={mis or '无'}"))
    return out


def report(pages: list[str], t: SweepTables) -> int:
    rows = sidebar_of_pages(pages, t)
    say("对账表（页下标 / 页名 / SURFACES 键 / _sidebar 下标 / widget_of 有根）：")
    for i, name, k, lab in rows:
        slug = slug_of(k) if k else ""
        say(f"  [{i}] {name:<5} 表面={k or '（没有）':<22} 捕法下标={t.pick.get(slug, '—')} "
            f"根={'有' if k in t.roots else '无'}")
    bad = 0
    rows_j = judges(pages, t)
    for name, ok, detail in rows_j:
        say(f"[{'PASS' if ok else 'FAIL'}] {name}  —— {detail}")
        bad += 0 if ok else 1
    say(f"---- 逐屏体检的侧栏覆盖：{len(rows_j) - bad}/{len(rows_j)} 过，红 {bad} 条 ----")
    return 1 if bad else 0


# ---------------------------------------------------------------- 证伪

def _mutate(pages: list[str], t: SweepTables, which: str) -> tuple[list[str], SweepTables, str]:
    """内存里摘一件承重件，返回新表与"该点名的判据"关键字。原表不许被就地改坏。"""
    import copy
    p2, t2 = list(pages), copy.deepcopy(t)
    if which == "drop_surface":
        k = sorted(k for k in t2.surfaces if k.startswith("sidebar-"))[-1]
        del t2.surfaces[k]
        return p2, t2, "每一页都有一个侧栏表面档"
    if which == "drop_method":
        slug = sorted(t2.pick)[-1]
        del t2.pick[slug]
        return p2, t2, "都有一个 `s_sidebar_*` 捕法"
    if which == "dup_index":
        slug = sorted(t2.pick)[-1]
        other = sorted(t2.pick)[-2]
        t2.pick[slug] = t2.pick[other]
        return p2, t2, "正好覆盖每一页、一页只一次"
    if which == "wrong_page":
        slug = sorted(t2.pick)[-1]
        t2.pick[slug] = 0
        return p2, t2, "名单齐了但配错页"
    if which == "drop_root":
        k = sorted(k for k in t2.roots if k.startswith("sidebar-"))[-1]
        del t2.roots[k]
        return p2, t2, "在 `widget_of` 里有根"
    if which == "drop_surface_extra":
        t2.surfaces["sidebar-幽灵页"] = "侧栏·不存在的页"
        t2.pick["幽灵页"] = 99
        t2.roots["sidebar-幽灵页"] = True
        return p2, t2, "不许反过来多出页签没有的"
    if which == "blind_ruler":
        # 模拟"解析器静默交出空表"：三处名单全清空，量具必须自曝失明而不是全绿。
        t2.pick.clear()
        t2.roots.clear()
        t2.surfaces = {k: v for k, v in t2.surfaces.items() if not k.startswith("sidebar-")}
        return p2, t2, "量具自己不瞎"
    raise CannotMeasure(f"不认识注入 {which}")


def falsify() -> int:
    pages, tables = load()
    cases = ["drop_surface", "drop_method", "dup_index", "wrong_page", "drop_root",
             "drop_surface_extra", "blind_ruler"]
    say("---- 证伪：内存里摘掉承重件，同批判据必须逐条点名 ----")
    base = {n: ok for n, ok, _ in judges(pages, tables)}
    if not all(base.values()):
        say("[说明] 基线本身就有红：下面这些「抓到」不作数，先把验收跑绿。")
    caught = missed = nop = 0
    snap = (dict(tables.surfaces), dict(tables.pick), dict(tables.roots))
    for which in cases:
        p2, t2, keyword = _mutate(pages, tables, which)
        if (dict(t2.surfaces), dict(t2.pick), dict(t2.roots)) == snap:
            # 注入自己失效了（改了个不存在的键、或写回同一个值）——这既不是"抓到"也不是"漏网"，
            # 是这一条压根没被测过。记 NOP 并按"没抓到"算，别让一条空操作冒充台账。
            nop += 1
            say(f"[NOP] 注入「{which}」没改动任何一张表（这条不作数）")
            continue
        hit = [n for n, ok, _ in judges(p2, t2) if not ok]
        named = [n for n in hit if keyword in n]
        if named:
            caught += 1
            say(f"[CAUGHT] 摘掉「{which}」⇒ 点名「{named[0]}」（共 {len(hit)} 条红）")
        elif hit:
            missed += 1
            say(f"[MISSED] 摘掉「{which}」有红但没按预期点名：{hit}")
        else:
            nop += 1
            say(f"[MISSED] 摘掉「{which}」判据一条不红（静默漏网）")
    total = len(cases)
    say(f"---- 证伪：注入 {total} 处，抓到 {caught} 处，漏网 {missed + nop} 处 ----")
    return 0 if caught == total else 1


def main(argv: list[str]) -> int:
    try:
        if "--falsify" in argv:
            return falsify()
        pages, tables = load()
        return report(pages, tables)
    except CannotMeasure as e:
        say(f"[ENV] 连测量对象都没有：{e}（退 2，不算产品红）")
        return 2
    except (OSError, SyntaxError, ValueError, KeyError, TypeError, AttributeError) as e:
        say(f"[ENV] 解析量具自己没跑成：{type(e).__name__}: {e}（退 2，不算产品红）")
        return 2
    except BaseException as e:                       # 断言自己不许崩成 traceback 冒充"抓到"
        say(f"[ENV] 量具撞上未预期异常：{type(e).__name__}: {e}（退 2）")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
