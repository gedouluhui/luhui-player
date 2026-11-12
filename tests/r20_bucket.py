# -*- coding: utf-8 -*-
"""`[FINAL]` 判决档位这一个字段的唯一真源：探针怎么**印**、看门狗怎么**读**，都在这一个文件里。

为什么要有这个文件（2026-10-20 挂看门狗当晚发现的）：`tests/watch_fullscreen_r20.py` 拿探针那句
判决决定"要不要再占用户那片 11 分钟"，而它上一版是拿**话术**判的——`"没能复现" not in final`。
探针这一轮的判决文字写的是「没复现：…」（没有「能」字），于是那一版看门狗会把**好趟**读成
"量到缺陷"，直接跳过用户原盘那一趟，还把"冒烟趟就量到了缺陷形状"写进哨兵。分流方向反了，
而两头的 rc 都是 0：这正是记忆 [[feedback-self-written-detectors-need-controls]] 里
"文本匹配全绿、但消费它的程序不认"那一类，同一个坑第二次不许再踩。

所以口径改成三件事：
  ① 判决归**三档**（`LEGAL`：复现／没复现／量不到），由探针在每条分档分支里当场写死；
  ② 印出来的形状只由 `emit()` 一处产生，探针那行 `[FINAL]` 必须调它（静态腿⑩盯着盘上源码，
     把"又写回字面量"这条路钉死——两处手抄迟早漂，这个仓已经在计数、提示行、md5 上各漂过一次）；
  ③ 看门狗只许调 `parse()` 读那个字段；读不到／不认识／自洽=False 一律**拒收**（空档名＋原因），
     原因必须印进哨兵。绝不许再拿"话术里有没有某个词"猜方向（静态腿⑫就是抓这个的）。

拒收而不是兜底成某一档：字段读不出来就是**量具回执失灵**，按本仓死规矩记 rc=1 停在那儿点名，
不重跑磨绿、也不猜。猜的两种错法不对称但都不可恢复：把缺陷判成"没复现"＝白多占一趟前台，
把"没复现"判成缺陷＝少跑一趟、结论缺一块证据，而后一种正是上一版实发的方向。

跑法（全部纯离线：不起窗、不碰屏幕、不写盘，锁屏也能跑）：
    python tests/r20_bucket.py                     # 自检（默认）
    python tests/r20_bucket.py --selfcheck-mutate  # 证伪：逐条注坏尺子，要求自检翻红并点名
退出码：0 自检全对／证伪全被抓；1 有条目判错（自检）或有条目没被抓（证伪）。
"""
from __future__ import annotations

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "probe_fullscreen_fit.py")
WATCH = os.path.join(HERE, "watch_fullscreen_r20.py")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LEGAL = ("复现", "没复现", "量不到")
SEP = "；"                       # 字段段的结束符；emit/parse 共用这一个常量，两边不许各写一个

# ── 证伪用的旋钮（`--selfcheck-mutate` 逐条关掉）────────────────────────────────
# 每一条都必须让上面某档自检翻红，否则这条旋钮是假的（记 MISSED，不放过）。
# 默认值＝口径收紧的样子；这些旋钮**只给证伪用**，产品与看门狗一律按默认值跑。
TAKE_LAST = True                 # False＝取第一条匹配（多趟日志里会读到上一趟的旧判决）
REQUIRE_LEGAL = True             # False＝字段值不在 LEGAL 里也放行（「未分类」被当成能开跑）
REQUIRE_OK = True                # False＝不看「档位自洽」（分档与话术对不上也照用）
MISSING_REFUSES = True           # False＝没有字段就当"没复现"（缺回执被兜底成某一档）
STOP_AT_SPACE = True             # False＝值收到结束符（把「档位自洽=True」一起吃进档名）
# 静态腿读源码时用的**内存覆盖**（证伪专用；本文件从不写盘，见 selfcheck_mutate 的注释）。
SRC: dict[str, str] = {}


def emit(bucket: str, ok: bool) -> str:
    """探针那行 `[FINAL]` 里属于本字段的这一段。**唯一**的打印形状。"""
    return f"档位={bucket} 档位自洽={ok}{SEP}"


def parse(text: str) -> tuple[str, str]:
    """从任意一段文本（整本日志或一行 `[FINAL]`）读出档位。

    返回 `(bucket, why)`：bucket 非空＝可信的三档之一；bucket 为空＝**拒收**，`why` 说清为什么
    （调用方必须把 why 印进自己的哨兵，不许静默当成某一档）。
    """
    hits = re.findall(r"档位=(.+?)(?:" + SEP + r"|$)", text or "")
    if not hits:
        if MISSING_REFUSES:
            return "", "回执里没有『档位=』字段（这一版探针不带档位，或 [FINAL] 行没落盘）⇒ 分流无据"
        return "没复现", "（旋钮关着：缺字段被兜底成『没复现』——这就是被注坏的那把尺）"
    raw = (hits[-1] if TAKE_LAST else hits[0])
    val = raw.split(None, 1)[0] if STOP_AT_SPACE else raw
    if REQUIRE_LEGAL and val not in LEGAL:
        return "", f"档位={val!r} 不在合法三档 {LEGAL} 里 ⇒ 不认识的回执不许拿来分流"
    m = re.search(r"档位自洽=(\w+)", text or "")
    if REQUIRE_OK:
        if m is None:
            return "", f"档位={val} 这一行没有『档位自洽=』⇒ 回执形状不对（不认识＝拒收）"
        if m.group(1) != "True":
            return "", (f"档位={val} 但 档位自洽={m.group(1)}：分档与判决话术对不上"
                        "（有人改了文案没改分档，或改了分档没改文案）")
    return val, ""


def _read(path: str) -> str:
    if path in SRC:                              # 证伪时的内存副本，真文件一个字都不动
        return SRC[path]
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError as exc:
        return f"<<读不动：{type(exc).__name__}:{exc}>>"


def _prose_matches(src: str) -> list[str]:
    """找出源码里**活代码**中"拿判决话术当分流条件"的那几处（字符串 in/not in 一个 final 变量）。

    为什么走 AST 而不是正则扫文本：看门狗的注释与 docstring 里本来就该留一笔"上一版为什么错"
    （那些话里必然带 `"没能复现" not in final1` 这样的引文），正则会把讲解当成回退、当场误红；
    而真正要禁的是**执行得到的那条比较**。AST 只认 `Compare(Const str, In/NotIn, Name 含 final)`
    这一种形状，注释与 docstring（它们是 Expr(Constant)，不是 Compare）天然不在里面。
    源码读不进 AST 一律算抓到（读不进＝这一趟根本跑不起来，比话术分流更该停）。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return [f"<源码读不进 AST：{exc}>"]
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left, right = node.left, node.comparators[0]
        op = node.ops[0]
        if (isinstance(left, ast.Constant) and isinstance(left.value, str)
                and re.search(r"没复现|没能复现|量不到", left.value)
                and isinstance(right, ast.Name) and "final" in right.id):
            hits.append(f'第{node.lineno}行 "{left.value}" '
                        f'{"not in" if isinstance(op, ast.NotIn) else "in"} {right.id}')
    return hits


def static_legs() -> list[tuple[str, bool]]:
    """⑩–⑬：只看**盘上源码**的静态腿（从文本判，所以注坏只改内存副本，不写盘）。

    为什么 runtime 那九条不够：①–⑨ 证的是"emit 与 parse 彼此认得"，可它俩在同一文件里、
    用同一个 `SEP` 拼出来，本来就很难不认得。真正会发生的事故是**消费端绕过这两个函数**：
    探针把 `档位=` 又写回字面量（emit 成了没人调的摆设），或看门狗把分流改回话术匹配
    （parse 成了没人调的摆设）。这两种改法①–⑨一个字都不会红，只能靠读源码抓。
    """
    ps = _read(PROBE)
    ws = _read(WATCH)
    lines = ps.splitlines()
    fin = [i for i, ln in enumerate(lines) if "[FINAL] R20 全屏几何探针" in ln]
    # 判决那一行＝带 `{verdict}` 的那条（其余 `[FINAL] …：没跑——…` 是 rc=1/3 的占位，不该带档位）。
    vd = [i for i in fin if "{verdict}" in lines[i]]
    one = len(vd) == 1
    row = lines[vd[0]] if one else ""
    ok10 = one and "emit(" in row and "档位=" not in row
    ok11_note = ""
    # ⑪ 判决链里每个 `verdict =` 必须配一句同分支的 `bucket =`（新增一档忘了分类要被抓）。
    lo = next((i for i, ln in enumerate(lines) if 'bucket = "未分类"' in ln), -1)
    hi = next((i for i, ln in enumerate(lines) if i > lo and "[FINAL] R20 全屏几何探针" in ln), -1)
    chain = lines[lo + 1:hi] if lo >= 0 and hi > lo else []
    nb = sum(1 for ln in chain if re.match(r"\s+bucket = ", ln))
    nv = sum(1 for ln in chain if re.match(r"\s+verdict = ", ln))
    ok11 = bool(chain) and nb == nv > 1
    # ⑫⑬ 看门狗：不许有"话术 in final"那类**活代码**里的文本匹配；必须真的 import 并调用 parse()。
    prose = _prose_matches(ws)
    ok13 = bool(re.search(r"\bparse\(", ws)) and "import r20_bucket" in ws
    if not ok11:
        ok11_note = f"（链长 {len(chain)} 行，bucket={nb}／verdict={nv}）"
    return [("⑩", ok10, f"探针那行判决 [FINAL] 由 emit() 打印，行内不许再手写字面量『档位=』"
           f"（判决行数={len(vd)}/1，调 emit={'emit(' in row}）"),
            ("⑪", ok11, f"判决链里每个 `verdict =` 都配一句同分支 `bucket =`{ok11_note}"),
            ("⑫", not prose, "看门狗活代码里没有『拿话术判分流』的文本匹配（必须走 parse）"
             + (f"——抓到 {len(prose)} 处：{prose}" if prose else "")),
            ("⑬", ok13, "看门狗确实 import 并调用 parse()（不调用的话⑫是空闸）")]


def roundtrip_legs() -> list[tuple[str, bool, str]]:
    """①–⑨：已知答案对照（三档 round-trip ＋ 五种坏回执一律拒收 ＋ 多趟取最后一条）。"""
    out: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        out.append((name, ok, detail))

    # ①–③ 探针印出来的字节，看门狗必须读回同一档。
    for b in LEGAL:
        line = "[FINAL] R20 全屏几何探针：" + emit(b, True) + "（判决话术略）"
        got = parse(line)
        add("档", got[0] == b and got[1] == "", f"emit(『{b}』) 原样交给 parse ⇒ 必须读回『{b}』")
    # ④ 缺字段（上一版探针／[FINAL] 没落盘）：拒收，不许兜底成某一档。
    got = parse("[FINAL] R20 全屏几何探针：没复现：两趟全屏每一拍……")
    add("档", got[0] == "" and "没有『档位=』" in got[1], "回执里没有『档位=』⇒ 必须拒收")
    # ⑤ 未知档（新加一档忘了分类 ⇒ 探针印「未分类」）：拒收。
    got = parse("[FINAL] " + emit("未分类", True) + "x")
    add("档", got[0] == "" and "不在合法三档" in got[1], "档位=未分类 ⇒ 必须拒收，不许猜方向")
    # ⑥ 分档与话术对不上（档位自洽=False）：拒收。
    got = parse("[FINAL] " + emit("复现", False) + "没复现：……")
    add("档", got[0] == "" and "档位自洽=False" in got[1], "档位=复现 但 档位自洽=False ⇒ 必须拒收")
    # ⑦ 只印档位、没印自洽：形状不对 ⇒ 拒收。
    got = parse("[FINAL] 档位=复现；A：子窗口没跟着铺满")
    add("档", got[0] == "" and "没有『档位自洽=』" in got[1], "有『档位=』但没有『档位自洽=』⇒ 必须拒收")
    # ⑧ 一本日志里有两趟 ⇒ 必须取最后一条（读旧的＝分流方向反）。
    two = ("[FINAL] R20 全屏几何探针：" + emit("复现", True) + "A：……\n"
           "[FINAL] R20 全屏几何探针：" + emit("没复现", True) + "没复现：……")
    got = parse(two)
    add("档", got[0] == "没复现", "同一本日志两趟 ⇒ 取最后一条（读到上一条会判成『复现』）")
    # ⑨ 空文本：拒收。
    got = parse("")
    add("档", got[0] == "" and "没有『档位=』" in got[1], "空文本 ⇒ 必须拒收，不许默认某一档")
    return out


def all_legs() -> list[tuple[str, bool, str]]:
    """**标签与计数同源**这一个函数：自检打印的条数、证伪点名的红，都从这里算，不另数一遍。"""
    out = [(f"{chr(0x2460 + i)}", ok, d) for i, (tag, ok, d) in enumerate(roundtrip_legs())]
    # round-trip 那九条按序取 ①–⑨；静态腿自己的编号是⑩–⑬（由 static_legs 直接给出，不重排）。
    return out + [(t, ok, d) for t, ok, d in static_legs()]


def selfcheck(verbose: bool = True) -> tuple[int, list[str]]:
    rows = all_legs()
    bad = [t for t, ok, _d in rows if not ok]
    if verbose:
        for t, ok, d in rows:
            print(f"  [{'OK' if ok else 'WRONG'}] {t} {d}")
        print(f"[FINAL] r20_bucket 自检：共 {len(rows)} 条（round-trip "
              f"{len(roundtrip_legs())} ＋ 静态 {len(static_legs())}），判错 {len(bad)} 条"
              f"{f'：{bad}' if bad else ''} ⇒ rc={1 if bad else 0}")
    return (1 if bad else 0), bad


# ── 证伪台账：每条＝(名字, 注法, 期望翻红的条目, 期望的形状) ───────────────────────
# 注法两种：("knob", 名, 值) 改本文件旋钮；("src", 路径, 锚点, 替换) 改**内存里的源码副本**。
# 为什么注法只改内存不改盘：静态腿读的是文本，注在内存里就够它翻红；而这两个文件此刻可能正被
# 看门狗那一串 import/执行（本仓死规矩：会改盘上文本产物的趟绝不并跑，
# [[feedback-no-concurrent-file-mutating-sweeps]]），就地改一次就把别人正在跑的那趟读成另一版。
MUTANTS: tuple[tuple[str, tuple, list[str], str], ...] = (
    ("D1 分流只看第一条匹配（旧趟的判决冒充本轮）",
     ("knob", "TAKE_LAST", False), ["⑧"], "⑧ 期望『没复现』却读到『复现』"),
    ("D2 未知档也放行（「未分类」被当成能开跑）",
     ("knob", "REQUIRE_LEGAL", False), ["⑤"], "⑤ 拒收变成放行"),
    ("D3 不看『档位自洽』（文案与分档对不上照样用）",
     ("knob", "REQUIRE_OK", False), ["⑥", "⑦"], "⑥⑦ 拒收变成放行"),
    ("D4 缺字段兜底成『没复现』（⇒ 静默少跑一趟，结论缺一块证据）",
     ("knob", "MISSING_REFUSES", False), ["④", "⑨"], "④⑨ 拒收变成『没复现』"),
    ("D5 值一直收到结束符（档名里带着『档位自洽=True』⇒ 永远不在 LEGAL 里）",
     ("knob", "STOP_AT_SPACE", False), ["①", "②", "③"],
     "round-trip 三条全废（⑧ 顺带也废：同一处取值口径）"),
    ("D6 恒真桩：parse 一律报『复现』（把量不到也当缺陷，白多占一趟）",
     ("func", lambda t: ("复现", "")), ["②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨"],
     "除①之外全红（① 复现 恰好蒙对）"),
    ("D7 探针那行 [FINAL] 把 emit() 换回手写字面量（静态腿⑩要抓的那条）",
     ("src", PROBE, "emit(bucket, bucket_ok)", "{档位}"), ["⑩"], "⑩ 抓到『没调 emit／写了字面量』"),
    ("D8 判决链里把一句 bucket = 抹掉（新增一档忘了分类）",
     ("src", PROBE, '        bucket = "没复现"', '        pass'), ["⑪"], "⑪ bucket 数 < verdict 数"),
    ("D9 看门狗把分流改回话术匹配（静态腿⑫要抓的那条）",
     ("src", WATCH, 'b1, w1 = r20_bucket.parse(final1)', 'b1, w1 = ("没复现" in final1, "")'),
     ["⑫"], "⑫ 抓到活代码里的文本匹配"),
    ("D10 看门狗不调 parse 了（那⑫成了空闸：没坏话术，但也没读字段）",
     ("src", WATCH, "r20_bucket.parse(final", "None(final"), ["⑬"], "⑬ 抓到『没调 parse』"),
    ("D11 探针把兜底那行 `bucket = \"未分类\"` 摘掉（判决链失去锚点，链长不可信）",
     ("src", PROBE, '    bucket = "未分类"', '    pass'), ["⑪"], "⑪ 找不到锚点＝链长不作数"),
)


def selfcheck_mutate() -> int:
    """逐条注坏 → 要求自检翻红**并且点名到期望那几条**；再跑 D0 恒等对照。"""
    print("证伪台账：逐条注坏尺子 → 要求 r20_bucket 自检翻红并点名到条目")
    base_probe, base_watch = _read(PROBE), _read(WATCH)
    bad = 0
    for name, (kind, *a), must, expect_txt in MUTANTS:
        saved_knob: list[tuple[str, object]] = []
        SRC.clear()
        red: list[str] = []
        verdict = ""
        try:
            if kind == "knob":
                key, val = a
                saved_knob.append((key, globals()[key]))
                globals()[key] = val
            elif kind == "func":
                saved_knob.append(("parse", globals()["parse"]))
                globals()["parse"] = a[0]
            else:
                path, old, new = a
                txt = base_probe if path == PROBE else base_watch
                if txt.count(old) < 1:
                    raise AssertionError(f"锚点在 {os.path.basename(path)} 里找不到：{old[:34]!r}")
                mutated = txt.replace(old, new)
                if mutated == txt:                       # 注成空操作＝变异失效，记 NOP 而不是漏网
                    print(f"  [NOP] {name}⇒ 变异没改动任何字节（锚点＝替换？），这条不算抓没抓到")
                    bad += 1
                    continue
                SRC[path] = mutated
            code, red = selfcheck(verbose=False)
            covered = bool(red) and all(x in red for x in must)
            if not covered:
                bad += 1
            verdict = "抓到" if covered else "判不到"
            detail = f"；翻红＝{red}"
        except BaseException as exc:                   # noqa: BLE001
            bad += 1
            verdict = f"CRASH({type(exc).__name__}:{exc})"
            detail = ""
        finally:
            for k, v in saved_knob:
                globals()[k] = v
            SRC.clear()
        print(f"  [{'CAUGHT' if verdict == '抓到' else 'MISSED'}] {name}⇒ 期望 {expect_txt}"
              f"{verdict and '；结局＝' + verdict}{detail}")
    code, red_base = selfcheck(verbose=False)          # D0 恒等对照：真尺子原样跑一遍必须全绿
    if red_base:
        bad += 1
    print(f"  [{'PASS' if not red_base else 'MISSED'}] D0 恒等注入（真尺子原样交回去）⇒ "
          f"全部条目必须绿，判成翻红就说明红是『重新装配』带来的：{red_base or '全绿'}")
    total = len(all_legs())
    rc = 1 if bad else 0
    print(f"[FINAL] r20_bucket 证伪：共 {len(MUTANTS)} 条坏尺子 ＋ 1 条恒等对照，"
          f"判不到的 {bad} 条（每条对照 {total} 档自检）⇒ rc={rc}")
    return rc


def main() -> int:
    if "--selfcheck-mutate" in sys.argv:
        return selfcheck_mutate()
    return selfcheck()[0]


if __name__ == "__main__":
    raise SystemExit(main())
