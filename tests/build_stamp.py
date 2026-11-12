# -*- coding: utf-8 -*-
"""量具的"这一拍量的是哪一版产品"：把被测文件的盘上字节 md5 印进日志（本仓库没有 git）。

为什么要有这一条（2026-10-20）：R20 那条链是**排队**跑的——看门狗等桌面空出来才起趟，而等的
这段时间我还在改产品代码。于是"日志里那份判决量的是改动前还是改动后"只有印在日志里才说得清。
不印只有两种下场：回头猜，或者把已被引用过的那份留档作废重跑（本仓为"顺手重跑作废了已引用的
md5/时刻"付过一次账，见记忆 [[feedback-no-vcs-and-overwriting-artifacts]]）。隔壁仓同一形状的
做法是把生成者脚本 md5 印进报告；这里连**被测产品**的指纹也要印，因为要判读的是产品行为。

口径五条（每条都有已知答案，注入证伪的条数由脚本自己打印）：
  ① 声明清单里每个文件都要在行内点名（漏一个＝那一处的改动看不见）；
  ② 指纹必须与独立重算**逐字符**相同，且算的是**盘上字节**——文本模式读会做 CRLF 换算，
     那种 md5 与别人算的对不上（同一条文件在两份工具里"看起来变了"，比不印还糟）；
  ③ 内容变⇒指纹变，且别的不跟着变（按文件名算哈希是最阴的坏法：行看着挺全，对内容彻底失明）；
  ④ 声明了盘上没有的文件⇒写「不在盘上」，绝不静默给空串或跳过（空串会被读成"和上一拍一样"）；
  ⑤ 整份清单压成的那**一个**短指纹（哨兵／台账只装得下它）必须随内容翻面、同内容必须复现。

跑法：
    python tests/build_stamp.py                     # 打一行真产品的被测指纹
    python tests/build_stamp.py --selfcheck         # 四条已知答案（只动临时目录里的副本）
    python tests/build_stamp.py --selfcheck-mutate   # 注 4 条坏尺子，逐条要求翻红
退出码：0 正常；1 自检／证伪有错。
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# R20 那本判决**真正依赖**的文件：占布局行的外壳（chrome/控制条/标题栏）、画面控件、
# 浮层范式、滑轨、主题与尺寸相关的样式、内核进程与 IPC、Win32 铺窗调用、配置，
# 外加量具自己与被它合账的验收件。少声明一个＝那一处的改动不进指纹；多声明不影响判读，
# 所以宁滥勿缺。
WATCHED = (
    "app/ui/main_window.py",
    "app/ui/chrome.py",
    "app/ui/controls_bar.py",
    "app/ui/title_bar.py",
    "app/ui/video_surface.py",
    "app/ui/floaters.py",
    "app/ui/scrubber.py",
    "app/ui/theme.py",
    "app/engine/player.py",
    "app/engine/ipc.py",
    "app/win32.py",
    "app/config.py",
    "tests/probe_fullscreen_fit.py",
    "tests/test_ui.py",
)


def digest(path: str) -> str:
    """这一个文件的盘上字节 md5；读不到就明说读不到（空串＝假绿光，见口径④）。"""
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError as exc:
        return f"不在盘上（{type(exc).__name__}）"


def stamps(root: str = ROOT, watched: tuple[str, ...] = WATCHED) -> list[tuple[str, str]]:
    """（相对路径, 指纹）逐条。`root`/`watched` 可注入：自检把它指到临时目录，绝不碰产品文件。"""
    out: list[tuple[str, str]] = []
    for rel in watched:
        p = rel if os.path.isabs(rel) else os.path.join(root, rel)
        out.append((rel, digest(p)))
    return out


def line_for(root: str, watched: tuple[str, ...]) -> str:
    """行身（不含前缀）：`名字=指纹` 逐个点名；缺的当场标出来。"""
    return " ".join(f"{os.path.basename(r)}={v[:12] if len(v) == 32 else v}"
                    for r, v in stamps(root, watched))


def stamp_short(root: str = ROOT, watched: tuple[str, ...] = WATCHED) -> str:
    """把整份被测清单压成 12 位——给哨兵／台账那种"一行装不下 14 个 md5"的场合用。

    压的是**各文件指纹的串**，不是文件名字符串：所以任何一个字节变了它必变（口径⑤在管这件事）。
    """
    body = " ".join(v for _r, v in stamps(root, watched))
    return hashlib.md5(body.encode("utf-8")).hexdigest()[:12]


def line(root: str = ROOT) -> str:
    """给量具直接印进日志的那一行。**不带 `----` 前缀**：看门狗按 `----` 现读汇总行
    （见 watch_fullscreen_r20.read_summary 的取行规则），这行不该混进那本账。"""
    rows = stamps(root, WATCHED)
    miss = [r for r, v in rows if len(v) != 32]
    return (f"[被测] 产品指纹（盘上字节 md5 前 12 位，共 {len(rows)} 个文件"
            + (f"，**缺 {len(miss)} 个：{miss}**" if miss else "，无缺档")
            + f"）短={stamp_short(root)} ←整份压成的一个数，台账／哨兵引用它"
            + f"｜{line_for(root, WATCHED)}")


# ------------------------------------------------------------------ 自检
def run_legs(verbose: bool = True) -> list[int]:
    """每条已知答案逐条对，返回**翻红的编号**。全程只动临时目录里的副本，产品文件一个字节不碰。"""
    def say(s: str) -> None:
        if verbose:
            print(s)

    red: list[int] = []
    with tempfile.TemporaryDirectory(prefix="buildstamp-") as tmp:
        files = ("a.py", "b.py", "c.py")
        for i, name in enumerate(files):
            with open(os.path.join(tmp, name), "wb") as fh:
                fh.write(f"x = {i}\n".encode("utf-8"))
        got = dict(stamps(tmp, files))

        ln = line_for(tmp, files)
        miss1 = [n for n in files if os.path.basename(n) + "=" not in ln]
        say(f"  [{'PASS' if not miss1 else 'FAIL'}] ①声明清单里 {len(files)} 个文件必须逐个在行内点名"
            f"⇒ 没点名的 {miss1 or '无'}（漏一个＝那一处的改动不进指纹，事后无从判断那一拍量的是哪版）")
        if miss1:
            red.append(1)

        ok2 = all(got[n] == hashlib.md5(open(os.path.join(tmp, n), "rb").read()).hexdigest()
                  for n in files)
        say(f"  [{'PASS' if ok2 else 'FAIL'}] ②每个指纹必须与独立重算**逐字符**相同"
            "（算的是盘上字节，不是内存里再编码一遍的文本——CRLF 换算过的那种对不上别人那份）")
        if not ok2:
            red.append(2)

        before = dict(stamps(tmp, files))
        with open(os.path.join(tmp, "b.py"), "wb") as fh:
            fh.write(b"x = 999\n")
        after = dict(stamps(tmp, files))
        ok3 = (before["b.py"] != after["b.py"]
               and before["a.py"] == after["a.py"]
               and before["c.py"] == after["c.py"])
        say(f"  [{'PASS' if ok3 else 'FAIL'}] ③改一个字节⇒该文件指纹必须变、没动的两个不许跟着变"
            f"⇒ b {before['b.py'][:8]}→{after['b.py'][:8]}")
        if not ok3:
            red.append(3)

        # ⑤ 压出来的短指纹：内容变⇒它变；同内容⇒必须复现同一个值
        sp_before = stamp_short(tmp, files)
        with open(os.path.join(tmp, "b.py"), "wb") as fh:
            fh.write(bytearray("x = 1" + chr(10), "utf-8"))   # 换成 a.py 那份内容：另一个文件的字节流进 b
        sp_back = stamp_short(tmp, files)
        with open(os.path.join(tmp, "b.py"), "wb") as fh:
            fh.write(bytearray("x = 999" + chr(10), "utf-8"))
        sp_same = stamp_short(tmp, files)
        ok5 = (sp_before != sp_back) and (sp_back != sp_same) and (sp_same == sp_before)
        say(f"  [{'PASS' if ok5 else 'FAIL'}] ⑤短指纹（{sp_before}→{sp_back}→{sp_same}）必须随内容翻面、"
            "同内容必须复现（哨兵里只放得下这一个数，它瞎了整个哨兵就不辨版本）")
        if not ok5:
            red.append(5)

        ghost = os.path.join(tmp, "nope.py")
        gv = dict(stamps(tmp, (ghost,)))[ghost]
        ok4 = gv.startswith("不在盘上")
        say(f"  [{'PASS' if ok4 else 'FAIL'}] ④声明了盘上没有的文件⇒必须写「不在盘上」"
            f"（拿到 {gv!r}：空串会被读成\"和上一拍一样\"，那是最省事的假绿光）")
        if not ok4:
            red.append(4)
    return red


def selfcheck() -> int:
    red = run_legs()
    rc = 1 if red else 0
    print(f"[FINAL] build_stamp 自检：共 5 条已知答案，错 {len(red)} 条"
          + (f"（红在 {red}）" if red else "") + f" ⇒ rc={rc}")
    if not red:
        print("  " + line())            # 顺带把真指纹打到终端：这行才是量具将要印出去的那行
    return rc


# ------------------------------------------------------------------ 证伪
def _quiet(path: str) -> str:
    """坏尺子：读不到就当"和别的文件一样没变"（口径④要挡的就是这个）。签名与被替换的
    `digest(path)` 必须一致——上一版这里是 `_quiet(root, rel)`，注入后当场 TypeError，
    证伪器把"注崩了"记成 CRASH 而不是假称抓到，这条 MISSED 就是它自己交出来的。"""
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return ""


def _by_name(path: str) -> str:
    """坏尺子：拿**文件名**算哈希——最阴的一种，行看着挺全、每个位置都有串，但对内容彻底失明。"""
    return hashlib.md5(os.path.basename(path).encode("utf-8")).hexdigest()


def _head_line(root: str, watched: tuple[str, ...]) -> str:
    """坏尺子：只印前两个，后面的静默漏掉。"""
    return " ".join(f"{os.path.basename(r)}={digest(os.path.join(root, r))[:12]}"
                    for r, v in stamps(root, tuple(watched))[:2])


MUTANTS = (
    ("S1 指纹恒返同一个常量",
     "②必须翻红：常量当然和独立重算对不上——这一条挡的就是「报告里那串不是从盘上算的」",
     lambda m: setattr(m, "stamps", lambda root=ROOT, watched=WATCHED:
                      [(r, "deadbeef" * 4) for r in watched]),
     [2]),
    ("S2 读不到就交空串（静默漏档）",
     "④必须翻红：空串会被读成「和上一拍一样」，缺文件必须点名",
     lambda m: setattr(m, "digest", _quiet),
     [4]),
    ("S3 按文件名算哈希（内容变了看不出来）",
     "③必须翻红：改字节后指纹不变＝这版和上一版根本分不出来",
     lambda m: setattr(m, "digest", _by_name),
     [3]),
    ("S5 短指纹恒返同一个常量",
     "⑤必须翻红：哨兵里只带这一个数，它不辨版本＝整本账不知道自己在读哪一版",
     lambda m: setattr(m, "stamp_short", lambda root=ROOT, watched=WATCHED: "0123456789ab"),
     [5]),
    ("S4 行里只印前 2 个文件",
     "①必须翻红：漏点名的那份改动看不见",
     lambda m: setattr(m, "line_for", _head_line),
     [1]),
)


def selfcheck_mutate() -> int:
    """证伪：逐条把坏尺子注进本模块，要求每条已知答案按声明翻红；恒等注入必须不翻红。

    条数一律由 `len(MUTANTS)` 现算并打印，正文里不抄数（手抄一份期望值＝每轮必滞后）。"""
    base = sys.modules[__name__]
    keys = ("stamps", "digest", "line_for", "stamp_short")
    red_base = run_legs(verbose=False)
    print(f"  基线（真尺子）：错 {len(red_base)} 条 rc={1 if red_base else 0}"
          "（基线不绿，下面的『翻红』就没有对照，先修基线）"
          + (f" 红在 {red_base}" if red_base else ""))
    bad = 0
    for name, expect, inject, must in MUTANTS:
        saved = {k: getattr(base, k) for k in keys}
        verdict = "注了"
        red: list[int] = []
        try:
            inject(base)
            if {k: getattr(base, k) for k in keys} == saved:
                verdict = "NOP（模块属性一字未变，这条不作数）"
            else:
                red = run_legs(verbose=False)
        except BaseException as exc:                   # noqa: BLE001
            verdict = f"CRASH({type(exc).__name__}:{exc})"
        finally:
            for k, v in saved.items():
                setattr(base, k, v)
        covered = verdict.startswith("注了") and all(x in red for x in must)
        if not covered:
            bad += 1
        print(f"  [{'CAUGHT' if covered else 'MISSED'}] {name}⇒ 期望 {expect}"
              f"；结局＝翻红 {red or '无'}（{verdict}）")
    red_id = run_legs(verbose=False)
    if red_id:
        bad += 1
    print(f"  [{'PASS' if not red_id else 'MISSED'}] S0 恒等注入（真尺子原样交回去）⇒ 各条必须全绿"
          f"（红了就说明红是「重新装配」带来的，不是坏尺子带来的）；结局＝{red_id or '全绿'}")
    rc = 1 if bad else 0
    print(f"[FINAL] build_stamp 证伪：共 {len(MUTANTS)} 条坏尺子 + 1 条恒等对照，"
          f"判不到的 {bad} 条 ⇒ rc={rc}")
    return rc


def main() -> int:
    if "--selfcheck-mutate" in sys.argv:
        return selfcheck_mutate()
    if "--selfcheck" in sys.argv:
        return selfcheck()
    print(line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
