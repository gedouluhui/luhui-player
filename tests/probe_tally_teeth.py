"""U59「证伪台账自打收口」的独立对照探针（只看源码 + 只跑纯函数，不碰产品文件）。

为什么要单开这一份：新加的这条判据本身也可能是一条**恒真的收口**——它印一句"共 84 行"，
但如果它数的是另一本账（`results`）、或者只在"全抓到"时才印，这句话照样看着挺像证据。
`--falsify` 那一趟会原地改产品文件再还原，那种趟一次只能跑一趟；这条只读不写，所以能在
真跑之前先回答"这条到底有没有牙"。

三条腿：
① 静态形状：拿真的 report() 源码过 `tally_wiring_ok`，必须绿；再摆五种坏法（数错对象／
   标签另算一套／自打排在分流之后／干脆不印／退回就地重数），每种都**必须红且点名那一条**，
   并先断言注入真的改动了源码（`mutated != base`）——空操作的"抓到了"是台账里记过的假案。
② 真执行：往 `falsified` 里摆六行已知答案的样本（含元组／整数 1／空串三种非布尔），当场调
   `report()`，把它的输出抓下来，要求**回执自打的那几个数**既等于按桶算出来的数、也等于
   同一份输出里 `[CAUGHT]/[MISSED]/[ENV ]/[JUNK]` 四种标签逐行数出来的数，还等于 rc 该走的
   那一家（有 MISSED ⇒ 1，不是 3）。少任何一条对账，"自打"就只是另一句没人核过的话。
③ 探针自己也要能被证伪：`--selftest` 把判据换成恒真的桩，五种坏法必须全被判成"抓到了"
   ⇒ 自检必须红。跑法 `python tests/probe_tally_teeth.py --selftest`。

退出码：0 全绿；1 有腿不成立；2 前提没立（report 源码里找不到那一句）。
"""
from __future__ import annotations

import contextlib
import inspect
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))
sys.path.insert(0, HERE)

import test_ui as T                                       # noqa: E402

BASE = inspect.getsource(T.report)

# 自打那一句在 report() 里的位置（两个锚点都在，才谈得上"挪动它"或"删掉它"）。
_TALLY_HEAD = '        print(f"---- 证伪台账自打'
_STMT_TAIL = "flush=True)"


def tally_stmt(src: str) -> tuple[int, int]:
    """回自打那一句在 src 里的 (起, 止)；锚点找不到就抛，绝不让注入悄悄变成空操作。"""
    i = src.index(_TALLY_HEAD)
    j = src.index(_STMT_TAIL, i) + len(_STMT_TAIL)
    while j < len(src) and src[j] != "\n":
        raise AssertionError("自打那一句的收尾不是想当然的一行，探针的锚点要重写")
    return i, j + 1


def _m_move_after_rc(src: str) -> str:
    """把自打那一句挪到 `rc = env_exit_code(...)` **之后**（顺序坏法）。"""
    i, j = tally_stmt(src)
    stmt, rest = src[i:j], src[:i] + src[j:]
    k = rest.index("\n", rest.index("rc = env_exit_code(")) + 1
    return rest[:k] + stmt + rest[k:]


def _m_drop(src: str) -> str:
    """干脆不印（回执里没有这句收口）。"""
    i, j = tally_stmt(src)
    return src[:i] + src[j:]


# 五种坏法：每种都要点名它坏在哪一家（①数法／②标签／③位置／④接线）。
MUTATIONS = [
    ("M1 数错对象（把证伪台账数成验收台账）",
     lambda s: s.replace("falsify_tally(falsified)", "falsify_tally(results)"), "①"),
    ("M2 判决行的标签另算一套（非布尔会被打成 ENV，把『记错类型』洗成『屏幕不给』）",
     lambda s: s.replace("falsify_bucket(ok)",
                         '"CAUGHT" if ok is True else ("MISSED" if ok is False else "ENV ")'), "②"),
    ("M3 自打那行排在分流之后（rc=1／rc=3 那两趟印不到，最需要它的恰好是它缺席的）",
     _m_move_after_rc, "③"),
    ("M4 干脆不印（回执里没有这句收口）", _m_drop, "③"),
    ("M5 退回就地重数（同一件事两套数法——上一轮那句 83+1=84 就是这么凑出来的）",
     lambda s: s.replace(
         '        missed, env, junk = tally["names"]["MISSED"], '
         'tally["names"]["ENV "], tally["junk_types"]',
         '        missed = [n for n, ok, _ in falsified if ok is False]\n'
         '        env = [n for n, ok, _ in falsified if ok is None]'), "①"),
]

# 六行已知答案的台账样本：抓／漏／靠屏幕／非布尔（元组、整数 1、空串）各有代表。
FIXTURE = [("F-抓到", True, "细节"), ("F-漏网", False, "细节"), ("F-靠屏幕", None, "细节"),
           ("F-忘了取元组的[0]", ("恒真的元组", "另一条"), "细节"),
           ("F-串回来是整数", 1, "细节"), ("F-空串", "", "细节")]
KEYS = ("total", "caught", "missed", "env", "junk")
EXPECT = {"total": 6, "caught": 1, "missed": 1, "env": 1, "junk": 3}
TAG_RE = re.compile(r"^\[(CAUGHT|MISSED|ENV |JUNK)\]", re.M)


def judge_static(src: str, judge) -> list[str]:
    """返回"这条坏法没被抓到"的名单：判据对坏样本仍然放行＝漏网。"""
    holes = []
    for name, make, want in MUTATIONS:
        try:
            mutated = make(src)
        except BaseException as e:                 # 锚点失效要说出口，不许记成"抓到了"
            holes.append(f"{name}｜锚点没找到（{type(e).__name__}: {e}），这一条不作数")
            continue
        if mutated == src:
            holes.append(f"{name}｜注入是空操作（文本一个字没变），这一条不作数")
            continue
        ok, why = judge(mutated)
        if ok:
            holes.append(f"{name}｜判据仍然放行（恒真）")
        elif want not in why:
            holes.append(f"{name}｜红了但没点名 {want}：{why}")
    return holes


def judge_exec() -> list[str]:
    """真执行一趟 report()：自打的数必须与「按桶重算」和「逐行数标签」都对得上。"""
    holes = []
    keep_f, keep_r, keep_e = list(T.falsified), list(T.results), list(T.env_notes)
    T.falsified[:] = FIXTURE
    T.results.clear()
    T.env_notes.clear()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = T.report()
    finally:
        T.falsified[:] = keep_f
        T.results[:] = keep_r
        T.env_notes[:] = keep_e
    out = buf.getvalue()
    t = T.falsify_tally(FIXTURE)
    got = {k: t[k] for k in KEYS}
    if got != EXPECT:
        holes.append(f"分桶本身就和已知答案不符：{got} ≠ {EXPECT}")
    m = re.search(r"证伪台账自打：共 (\d+) 行 —— CAUGHT (\d+)／MISSED (\d+)／ENV (\d+)／"
                  r"非布尔 (\d+)", out)
    if not m:
        holes.append("回执里没有那句自打收口（它必须排在分流之前，rc=1 这趟也不例外）")
    else:
        printed = dict(zip(KEYS, (int(x) for x in m.groups())))
        if printed != EXPECT:
            holes.append(f"自打的数与按桶重算不符：{printed} ≠ {EXPECT}")
    tags: dict[str, int] = {}
    for tag in TAG_RE.findall(out):
        tags[tag] = tags.get(tag, 0) + 1
    from_tags = {"total": sum(tags.values()), "caught": tags.get("CAUGHT", 0),
                 "missed": tags.get("MISSED", 0), "env": tags.get("ENV ", 0),
                 "junk": tags.get("JUNK", 0)}
    if from_tags != EXPECT:
        holes.append(f"自打的数与同一份输出里逐行数出来的标签不符：{from_tags} ≠ {EXPECT}")
    if rc != 1:
        holes.append(f"有 MISSED 与非布尔样本的一趟 rc 应当是 1（量具不可信优先），实测 rc={rc}")
    return holes


def run(judge=None) -> tuple[list[str], list[str]]:
    """两条腿一起跑，回 (不作数的前提, 漏网)。judge 可换桩——--selftest 就拿恒真桩来证它有牙。"""
    judge = judge or T.tally_wiring_ok
    if "证伪台账自打" not in BASE:
        return (["前提没立：report() 源码里找不到自打收口那一句，静态那条腿量不到东西"], [])
    base_ok, base_why = judge(BASE)
    prem: list[str] = []
    if not base_ok:
        prem.append(f"基线红：真 report 源码被判成不合格（{base_why}），"
                    f"后面每一种红都说不清是谁的功劳")
    holes = judge_static(BASE, judge) + ([] if prem else judge_exec())
    return prem, holes


def always_green(_src: str) -> tuple[bool, str]:
    return True, ""


def main() -> int:
    if "--selftest" in sys.argv:
        # 已知坏样本对照：判据换成恒真的桩时，这条探针必须翻红并点到 M1..M5 每一条。
        prem, holes = run(always_green)
        named = sorted({h.split(" ")[0] for h in holes if re.match(r"^M[1-5] ", h)})
        print(f"[SELFTEST] 恒真桩下的漏网 {len(holes)} 条，其中点名 M1..M5 的 {len(named)} 条："
              f"{named}")
        if prem:
            print("   （前提红：", "；".join(prem), "）")
        if len(named) != len(MUTATIONS):
            print("!! 自检不成立：把判据调回恒真仍有坏法没被抓到 ⇒ 这条探针没有牙")
            return 1
        print("[SELFTEST] 通过：五种坏法在恒真桩下全部暴露 ⇒ 静态那条腿确有鉴别力")
        return 0
    prem, holes = run()
    for name, make, want in MUTATIONS:
        try:
            mutated = make(BASE)
        except BaseException as e:
            print(f"[锚点失效] {name}（{type(e).__name__}: {e}）")
            continue
        if mutated == BASE:
            print(f"[空操作] {name}")
            continue
        ok, why = T.tally_wiring_ok(mutated)
        print(f"[{'漏网' if ok else '抓到'}] {name}（应点名 {want}，实际 {why or '——'}）")
    if prem:
        print("!! 前提：", "；".join(prem))
        return 2
    if holes:
        print("!! 漏网/不成立：")
        for h in holes:
            print("   -", h)
        return 1
    print(f"---- 结论：{len(MUTATIONS)} 种坏法全被抓到；真执行那条对账"
          f"（自打＝按桶重算＝逐行标签，且 rc=1）成立；样本 {EXPECT} ----")
    return 0


if __name__ == "__main__":
    sys.exit(main())
