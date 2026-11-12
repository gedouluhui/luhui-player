"""#73 那把「播放键说法翻转」尺到底有没有牙——在**真内核 + 真窗口**的通路上验一遍。

为什么要单独开这一趟（而不是引用证伪台账 U6h 那八条形状）：`tests/test_ui.py --falsify`
里的 U6h 喂的是**已知答案的字符串形状**，它能证明纯函数 `play_tip_flip` 判得对，
但它不经过内核、不经过 Qt 的跨线程投递。#73 的历史恰好坏在投递时序上
（`player._apply()` 先写 `_props` 缓存、再发 `sig_pause`，见 `app/engine/player.py:422`／`:429`），
所以"这把尺在真跑的那条链上会不会红"必须用真链路量一次，不能靠纯函数那一头代答。

两种坏法（都在内存里改，产品文件一个字不动，结尾用 md5 对账）：
  M1 **摘回旧映射**：把登记在 `btn.__dict__[form_rows.TIP_ATTR]` 里那半句"说法"换回
      2026-10-13 之前的状态口径（暂停时说「暂停」）。这是 #73 那条真红的原样。
      期望：新尺 2 秒内**不放行**，而且拦下来的是"说法"那一半——内核 `pause` 那半必须已经到 True
      （否则"没放行"是内核没回话，不是尺有牙）。
  M2 **说法冻住**：把那半句换成常量「播放」，于是按下之后重画出来的还是一字不差的老话。
      这是 r13/r8 那种"偶然绿"的可观察形状：判据分不清"跨线程还没送达"和"送到但内容没变"，
      两种在它眼里都是 tip_now == tip_before。
      期望：新尺**不放行**；**旧写法**（`toolTip().startswith("播放")`）在这一刻放行——
      那正是六趟以上把反义读成通过的形状。M2 不是"模拟送达慢"，它造的就是判据唯一能看见的
      那一份差异，所以它对旧写法是**绿灯**、对新尺是**红灯**。

判决必须全对，另外三条断言不许松：
  · `mut != base`（变异必须真的改了字面：没改动的变异会被误记成"尺漏网"，那是空操作不是证据）
  · 还原后两条控制腿**重新变绿**（证明"没放行"不是我的探针自己立不起来）
  · 退出前 `app/ui/controls_bar.py`／`app/ui/form_rows.py` 的 md5 与进入时一致

跑法（要真桌面会话，和 `test_ui.py` 同一条规矩；offscreen 下 `MainWindow` 起不了内核的 wid 嵌入）：
    python tests/probe_playtip_teeth.py > tests/out/probe-playtip-teeth-r15.log 2>&1

退出码：0 = 判决与断言全部符合预期；1 = 有一条不符（那要么尺没牙、要么探针坏了）；
        2 = 前提立不住（缺样片／内核起不来／没播起来），**这一趟不作数**，不判产品。
"""
from __future__ import annotations

import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
if hasattr(sys.stdout, "reconfigure"):      # 判决行里带中文与 U+2192，GBK 控制台重定向会崩
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import test_ui as T                          # noqa: E402  复用同一套机器：pump/wait_for/那把纯尺
from app.ui import form_rows, keymap         # noqa: E402

BUDGET = 2.0
PRODUCT_FILES = ("app/ui/controls_bar.py", "app/ui/form_rows.py")
# 台账里指的那句"复跑命令记在日志的抬头里"就靠这一行兑现：日志第一行是命令，不是事后补的说明。
RUN_CMD = "python tests/probe_playtip_teeth.py > tests/out/probe-playtip-teeth-r15.log 2>&1"
VERDICTS: list[tuple[str, bool]] = []
win = btn = None                             # 装配后由 main() 填；judge/old_prefix_judge 读这两个


def md5(path: str) -> str:
    with open(os.path.join(ROOT, path), "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def say(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'OK ' if ok else 'BAD'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    VERDICTS.append((name, bool(ok)))
    return bool(ok)


def set_saying(fn) -> str:
    """只换登记里那半句"说法"，键名那一半（`（Space/K）`）仍由生效表现算。

    必须改 `__dict__` 里那一份。第一版这里写的是 `type(bar)._play_saying = lambda s: ...`，
    跑出来"尺没牙"——那是假结论：登记在构造期就把**绑定方法对象**存进了
    `btn.__dict__[TIP_ATTR]`（`form_rows.bind_key_tip` → `bind_key_tip_parts`），
    改类属性动的是另一棵树，`paint_key_tip` 调的还是原来那颗。改完必须断言
    重画出来的字符串和按之前**不一样**，否则"摘掉映射"这件事本身没生效，
    后面那条"尺不放行"就什么都证明不了。
    """
    cfg, parts = btn.__dict__[form_rows.TIP_ATTR]
    actions = parts[0][1]
    btn.__dict__[form_rows.TIP_ATTR] = (cfg, ((fn, actions),))
    return form_rows.paint_key_tip(btn)


def flip(want_pause: bool) -> dict:
    """按一次播放/暂停，然后按验收里 `_judge73` 的**同一形状**读两半，把读到的数都留下。

    返回的字段是给判决用的（不是给字符串匹配用的）：判决若去 grep 自己的 detail 文案，
    文案一改判决就漂——那正是这一轮 #73 学到的那类坑。
    """
    before = btn.toolTip()
    win.dispatch("toggle_play")
    rec = {"released": False, "ms": 0.0, "pause_raw": None, "pause_ok": False,
           "saying_ok": False, "saying_why": "", "tip": before, "before": before}
    t0 = time.monotonic()

    def one() -> bool:
        raw = win.player.prop("pause", not want_pause)
        rec["pause_raw"] = raw
        rec["pause_ok"] = bool(raw) is bool(want_pause)
        if not rec["pause_ok"]:
            return False
        ok, why = T.play_tip_flip(want_pause, before, btn.toolTip())
        rec["saying_ok"], rec["saying_why"], rec["tip"] = ok, why, btn.toolTip()
        return ok

    rec["released"] = T.wait_for(one, BUDGET)
    rec["ms"] = (time.monotonic() - t0) * 1000.0
    rec["same_as_before"] = str(rec["tip"] or "") == str(before or "")
    rec["saying"] = str(rec["tip"] or "").split("（", 1)[0].strip()
    rec["want"] = "播放" if want_pause else "暂停"
    # r14 之前那句判据的说法那一半：只比前缀。留着它不是为了复用，是为了**当场对照**。
    rec["old_prefix_ok"] = rec["saying"].startswith(rec["want"])
    rec["detail"] = (f"放行={rec['released']} 耗时={rec['ms']:.0f}ms "
                     f"内核pause={rec['pause_raw']!r} tip={rec['tip']!r} 按之前={before!r} "
                     f"说法那半={rec['saying_why'] or '(状态那半没到，说法还没比)'}")
    return rec


# ---------------------------------------------------------------- 装配
def main() -> int:
    global win, btn
    print(f"跑法（照着这一行复跑就能重造本文件）：$ {RUN_CMD}", flush=True)
    print(f"probe_playtip_teeth md5={md5('tests/probe_playtip_teeth.py')}", flush=True)
    print(f"  读数依赖：test_ui.py md5={md5('tests/test_ui.py')} "
          f"controls_bar.py md5={md5(PRODUCT_FILES[0])} form_rows.py md5={md5(PRODUCT_FILES[1])}",
          flush=True)
    for p in T.FILES:
        if not os.path.exists(p):
            print(f"[ENV] 缺少样片 {p}，这一趟不作数（先跑 python tests/gen_sample.py）", flush=True)
            return 2
    start_md5 = {p: md5(p) for p in PRODUCT_FILES}

    app = T.QApplication.instance() or T.QApplication(sys.argv[:1])
    T.APP = app            # **前提**：test_ui.pump() 在 APP is None 时直接 return，
    # 上一版探针没绑这一句，于是"重画永远不来"被读成"新尺拦得住"——那不是尺有牙，是探针自己
    # 把事件循环关在门外。现在开跑前把它当场断言掉。
    say("探针前提：test_ui.APP 已绑定（不然 pump 是空转，整趟读数都不作数）",
        T.APP is app, f"APP={type(T.APP).__name__}")

    cfg = T.make_cfg()
    T.theme.install(app, str(cfg["theme_accent"]))
    win = T.build_window(cfg)
    btn = win.bar.btn_play
    orig_entry = btn.__dict__[form_rows.TIP_ATTR]

    win._begin_engine()
    end = time.monotonic() + 12.0
    while not win.player.running and time.monotonic() < end:
        T.pump(0.05)
    if not win.player.running:
        print("[ENV] 内核起不来，这一趟不作数", flush=True)
        return 2
    win.add_paths(T.FILES)
    playing = T.wait_for(lambda: "sample-1" in str(win.player.prop("path", ""))
                         and win.player.prop("pause", True) is False, 12.0)
    if not playing:
        print(f"[ENV] 样片没播起来（path={win.player.prop('path')!r} "
              f"pause={win.player.prop('pause')!r}），这一趟不作数", flush=True)
        return 2
    T.pump(0.3)
    base = btn.toolTip()
    print(f"起点：播放中 tip={base!r} _paused={win.bar._paused!r} "
          f"_play_saying()={win.bar._play_saying()!r}", flush=True)
    say("前提：出厂映射在播放态说的是「暂停」（动作口径：点我暂停）",
        base.split("（", 1)[0].strip() == "暂停", f"tip={base!r}")

    try:
        # ---- 控制腿 A/B：不加任何变异，新尺必须放行 -----------------------------
        a = flip(True)
        say("对照 A：出厂映射下按空格暂停，新尺放行（探针本身立得住）", a["released"], a["detail"])
        b = flip(False)
        say("对照 B：出厂映射下再按一次恢复播放，同一把尺倒着量也放行", b["released"], b["detail"])

        # ---- M1：摘回旧映射（状态口径，暂停时说「暂停」）--------------------------
        mut = set_saying(lambda: ("暂停" if win.bar._paused else "播放")
                         if win.bar._paused is not None else "播放/暂停")
        say("M1 变异生效：摘回旧映射之后那句确实变了字", mut != base,
            f"base={base!r} → mut={mut!r}")
        m1 = flip(True)
        say("M1 判决：新尺在 2 秒内不放行，且拦住它的是说法那半（内核那半已到 True）",
            (not m1["released"]) and m1["pause_ok"] and (not m1["saying_ok"]), m1["detail"])
        say("M1 对照：旧写法（只比前缀）在这一刻同样不放行 ⇒ #73 靠方向那半就抓得住",
            not m1["old_prefix_ok"],
            f"startswith({m1['want']!r})={m1['old_prefix_ok']} tip={m1['tip']!r}")

        # 还原 + 重新变绿（摘完必须跑一次正向对照，否则"拦住"可能是还原没做干净）
        btn.__dict__[form_rows.TIP_ATTR] = orig_entry
        form_rows.paint_key_tip(btn)
        r1 = flip(False)
        say("M1 还原后的控制腿：恢复播放，新尺重新放行（探针不是只会说'不放行'）",
            r1["released"], r1["detail"])

        # ---- M2：说法冻住（常量「播放」，重画出来一字不差）-------------------------
        mut2 = set_saying(lambda: "播放")
        say("M2 变异生效：播放态被冻成「播放」，与出厂那句不一样", mut2 != base,
            f"base={base!r} → mut={mut2!r}")
        m2 = flip(True)
        say("M2 判决：新尺不放行，且形状正是「tip 与按之前一字不差」（跨线程送达之前的旧话那一类）",
            (not m2["released"]) and m2["pause_ok"] and m2["same_as_before"]
            and m2["saying"] == m2["want"], m2["detail"])
        say("M2 对照：旧写法（只比前缀）在这一刻**放行** ⇒ r13/r8 那种偶然绿是真的，不是事后编的故事",
            m2["old_prefix_ok"], f"startswith({m2['want']!r})={m2['old_prefix_ok']} tip={m2['tip']!r}")

        btn.__dict__[form_rows.TIP_ATTR] = orig_entry
        form_rows.paint_key_tip(btn)
        r2 = flip(False)
        say("M2 还原后的控制腿：恢复播放，新尺重新放行", r2["released"], r2["detail"])
        same_obj = (btn.__dict__[form_rows.TIP_ATTR] is orig_entry
                    and btn.__dict__[form_rows.TIP_ATTR][1][0][0] is orig_entry[1][0][0])
        note = keymap.tip_note(keymap.effective(cfg.get("keymap")), orig_entry[1][0][1])
        say("还原干净：登记项与进入时是同一份（说法那颗 callable 没被换掉，键名仍由生效表现算）",
            same_obj, f"tip={btn.toolTip()!r} 键名半句={note!r}")
    finally:
        drift_note = ""
        try:
            btn.__dict__[form_rows.TIP_ATTR] = orig_entry
            form_rows.paint_key_tip(btn)
        except Exception as exc:                      # 清理路上不许崩：崩了 md5 对账就跑不到
            drift_note = f"（还原异常：{exc}）"
        try:
            win.close()
        except Exception as exc:
            drift_note += f"（关窗异常：{exc}）"
        T.pump(0.2)

    drift = [p for p, h in start_md5.items() if md5(p) != h]
    say("产品文件未被就地改写（本趟只动内存里的登记项）", not drift,
        f"漂移={drift or '无'}{drift_note}")
    print(f"收尾读数：tip={btn.toolTip()!r}", flush=True)

    bad = [n for n, ok in VERDICTS if not ok]
    print(f"\n---- 汇总：{len(VERDICTS) - len(bad)}/{len(VERDICTS)} 条判决符合预期"
          + (f"；不符：{bad}" if bad else "；全部符合") + " ----", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
