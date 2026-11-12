"""证伪趟（量具校准，不参与产品判定）：给 `tests/check_openwith_shell.py` 的四条判据各注入
一处静默 bug，逐条要求它**当场翻红**，而且要点名是哪条红的。注入落空（文件没变）记 NOP，
不许记成"抓到"。

为什么要有这一趟：`check_openwith_shell.py` 报的绿本身只是"量具说绿"。needle 相等、teeth
哨兵、blind 对照组这三样要是写成恒真/恒假，产品真出问题它也照样绿。每条判据都得回答：
"把它拆掉，这本账会不会红？"

纪律（都是栽过的地方）：
· 会改文件的趟一次只跑一条，副本存在即拒跑；每条跑完立刻删副本，变异态不在盘上过夜。
· 每条必须断言 `mutated != base`（字节级），否则记 NOP。
· `rc != 0` 还不够：必须日志里出现一条带指定标签、且文字里点名那条判据的行，才算 CAUGHT。
· 产品文件一个字节都不动（只往 `_mut_openwith_shell.py` 写）。
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "check_openwith_shell.py")
COPY = os.path.join(HERE, "_mut_openwith_shell.py")

BASE = open(TARGET, encoding="utf-8").read()

EXPECT_RED_LINE = "反向对照"

# (名字, old, new, 追加参数, 要求的标签, 标签行里必须出现的文字)
MUTATIONS = [
    # 这条变异落在 ENV 分支而不是 FAIL 分支，是量具自己设计的：整个扩展名的候选**全部**取不到
    # 名字 ⇒ 先判"腿断了、本账不作数"，再判产品红。所以期望标签是 ENV＋那句点名。
    ("取名字走错槽（下标 4→9）",
     "S_GET_MENU_NAME = 4", "S_GET_MENU_NAME = 9",
     [], "ENV", "取名字这条腿断了"),
    ("鉴别力哨兵把真扩展名当假扩展名（.qqqzzznotatype→.mkv）",
     'probe_ext = ".qqqzzznotatype"', 'probe_ext = ".mkv"',
     [], "ENV", "EXE 这条判据在本机没有鉴别力"),
    ("对照组阈值改成不可能满足（<3→<999）",
     "if len(foreign) < 3:", "if len(foreign) < 999:",
     [], "ENV", "对照组没列够第三方候选"),
    ("DEFAULT 命中判定摘掉 ⇒ 反向对照就不该再判红",
     "        if not any(n == needle for n in names):\n            missing.append(ext)",
     "        if False:\n            missing.append(ext)",
     ["--expect-red"], "FAIL", EXPECT_RED_LINE),
]


def run(src: str, extra: list[str]) -> tuple[int, str]:
    with open(COPY, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(src)
    proc = subprocess.run([sys.executable, COPY, *extra],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if os.path.exists(COPY):
        print(f"[ABORT] 上一次的副本还赖在盘上：{COPY}\n会改文件的趟不许并跑，先确认它不是别人在用")
        return 3
    rows = []
    caught = missed = nop = 0
    for name, old, new, extra, tag, must_say in MUTATIONS:
        if old not in BASE:
            nop += 1
            rows.append(("NOP", name, "锚点在基线里找不到，注入根本没发生"))
            continue
        mutated = BASE.replace(old, new, 1)
        if mutated == BASE:
            nop += 1
            rows.append(("NOP", name, "替换后字节完全一样（replace 命中的是别处？）"))
            continue
        rc, log = run(mutated, extra)
        try:
            os.remove(COPY)
        except OSError as exc:
            rows.append(("NOP", name, f"副本没删掉：{exc}"))
        named = [ln for ln in log.splitlines() if ln.startswith(f"[{tag}]") and must_say in ln]
        if rc != 0 and named:
            caught += 1
            rows.append(("CAUGHT", name, f"rc={rc}；红在：{named[0][:120]}"))
        else:
            missed += 1
            rows.append(("MISSED", name,
                         f"rc={rc}（要求非 0）；带[{tag}]且点名「{must_say}」的行 {len(named)} 条"
                         + ("；末 3 行日志：" + " ⏎ ".join(log.strip().splitlines()[-3:]) if not named else "")))
    print("---- 证伪收口（每条要求当场翻红并点名）----")
    for st, name, why in rows:
        print(f"  {st:6} {name} —— {why}")
    total = len(MUTATIONS)
    ok = (missed == 0 and nop == 0 and caught == total)
    print(f"[FINAL] CAUGHT {caught}／MISSED {missed}／NOP {nop}（共 {total} 条变异）—— "
          f"{'四条判据都拆得动' if ok else '有判据拆不动，别把它们的绿当证据'} rc={0 if ok else 1}")
    print("[DONE]")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
