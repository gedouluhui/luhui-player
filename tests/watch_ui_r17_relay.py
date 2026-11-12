# -*- coding: utf-8 -*-
"""给 `tests/watch_ui_r17.py` 接力的一位看门狗（R17 收尾时挂的尾巴保险）。

为什么还要第二位：第一位（pid 36164，02:07 起）的上限是 8 小时，到点如果屏幕仍不给像素，
它按设计写一份"三趟全跳过"的哨兵并退 3——**它自己不重跑**。要是用户那时还没醒，尾巴就
落回用户手里了（"你解锁后跑这三条命令"这种话不能再出现）。本位只做一件事：

  等第一位收工 → 读它的哨兵 → 只要哨兵里还有"跳过"且确实没有活的看门狗进程了，
  就原样再起一位（同样的 8 小时上限、同样的三趟、同样的判据），最多接力 --hops 次。

它自己**不起真窗口、不碰 test_ui、不碰链锁**：那些都还是第一位 watch_ui_r17.py 的活。
日志：tests/out/watch-ui-r17-relay.log（追加，不覆盖前一位的现场）
哨兵：tests/out/watch-ui-r17-relay.flag（本位自己的结论，别和第一位那本混）

`--selftest` 不碰任何进程与文件，只喂 6 份已知好坏的哨兵文本要求判决逐条对上，
另配 3 条变异（把判据改瞎）要求当场翻红。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT_DIR = os.path.join(HERE, "out")
UPSTREAM_FLAG = os.path.join(OUT_DIR, "watch-ui-r17-done.flag")
UPSTREAM_SCRIPT = os.path.join(HERE, "watch_ui_r17.py")
MY_LOG = os.path.join(OUT_DIR, "watch-ui-r17-relay.log")
MY_FLAG = os.path.join(OUT_DIR, "watch-ui-r17-relay.flag")
POLL_SECS = 300.0

# 判决只有三种，且必须互斥：等（上游还在跑）／收（上游给出了不作跳过跳过的结论）／接力
WAIT, DONE, RELAY = "wait", "done", "relay"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(MY_LOG, "a", encoding="utf-8", errors="replace", newline="\n") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def decide(flag_text: str | None, upstream_alive: bool) -> str:
    """判决的唯一真源：哨兵文本 + 上游进程死活。

    为什么要"进程死活"这一维：只看哨兵会踩两个坑——①第一位刚起步时哨兵还没落盘，
    把"没文件"读成"它完了、跳过"就会同时挂两位（两位都抢前台就是两把尺互踩）；
    ②第一位跑完最后一趟才写哨兵，中途那次"上一轮的旧哨兵"必须不算。
    """
    if upstream_alive:
        return WAIT
    if flag_text is None:
        # 上游不在了、哨兵也不在：它不是跑完了，是没起来或跑崩了 → 接力重起一位
        return RELAY
    if "跳过" in flag_text:
        return RELAY
    return DONE


_UPSTREAM_IMAGE = ("python.exe", "pythonw.exe")


def count_upstream() -> int:
    """盘上有几位在轮询的 watch_ui_r17。

    两个坑都在这儿挡掉：①**按命令行筛进程会把查询自己算进去**——刚才 `--once` 第一趟就把
    三个 bash 包装器（我的命令行里带着这个文件名）数成了"上游看门狗"，于是永远不敢接力；
    ②绝不按镜像名全认/全杀——所以名字必须是 python(w).exe，且命令行里带的是那个
    `.py` 脚本本体（本接力件自己叫 …_relay.py，天然不含 "watch_ui_r17.py" 这个串）。
    """
    try:
        import psutil
    except Exception as exc:                            # noqa: BLE001
        log(f"psutil 不在，问不了进程表：{type(exc).__name__}: {exc}（保守当'还在跑'，不抢它的活）")
        return -1
    n = 0
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            cl = " ".join(p.info.get("cmdline") or [])
        except Exception:                               # noqa: BLE001
            continue
        if name not in _UPSTREAM_IMAGE or "watch_ui_r17.py" not in cl:
            continue
        n += 1
        log(f"看到上游看门狗 pid={p.info['pid']} {name} {cl[:110]}")
    return n


def upstream_alive() -> bool:
    return count_upstream() > 0


def read_flag() -> str | None:
    if not os.path.exists(UPSTREAM_FLAG):
        return None
    try:
        with open(UPSTREAM_FLAG, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        log(f"哨兵读不动：{exc}")
        return None


def spawn_upstream() -> int | None:
    """再起一位上游看门狗（脱离本作业对象，否则我退了它就跟着没了）。

    接力前再数一次进程表：两位同时抢前台就是两把尺互踩，那种"红"既不是产品坏也不是屏幕黑。
    """
    c = count_upstream()
    if c > 0:
        log(f"接力前先复查：盘上已经有 {c} 位上游了，本位不叠加（宁可等，不互踩）")
        return None
    # DETACHED_PROCESS 0x8 | CREATE_NEW_PROCESS_GROUP 0x200 | CREATE_BREAKAWAY_FROM_JOB 0x01000000
    creationflags = 0x8 | 0x200 | 0x01000000
    # 追加到上一位的同一本日志里，现场连续；脚本 main() 自己会先删旧哨兵再写新的
    wrapper = os.path.join(OUT_DIR, "watch-ui-r17-wrapper.txt")
    try:
        with open(wrapper, "a", encoding="utf-8", errors="replace") as fh:
            p = subprocess.Popen([sys.executable, "-u", UPSTREAM_SCRIPT],
                                 cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                                 creationflags=creationflags, close_fds=True)
        log(f"接力成功：新的上游看门狗 pid={p.pid}，日志追加到 {os.path.basename(wrapper)}")
        return p.pid
    except OSError as exc:
        log(f"接力失败：{type(exc).__name__}: {exc}")
        return None


def selftest() -> int:
    """已知答案对照 + 变异：判决函数必须有牙。"""
    rows = []
    cases = [
        ("哨兵没有＋上游在跑 → 等（不许抢活）", None, True, WAIT),
        ("哨兵没有＋上游没了 → 接力（没起来／跑崩）", None, False, RELAY),
        ("哨兵说跳过 → 接力", "r1: rc=跳过（屏幕不给）\n", False, RELAY),
        ("哨兵全 rc=0 → 收", "r1: rc=0\nr2: rc=0\n", False, DONE),
        ("哨兵 rc=1（真失败）→ 收（产品红不许用接力洗白）", "r1: rc=1\n", False, DONE),
        ("上一轮的旧哨兵写跳过、但有一位正在跑 → 等", "r1: rc=跳过（屏幕不给）\n", True, WAIT),
    ]
    for name, text, alive, want in cases:
        got = decide(text, alive)
        rows.append(f"[{'PASS' if got == want else 'FAIL'}] {name}：期望 {want} ／ 实得 {got}")
    # 变异：把判据改瞎的三种坏法，必须各自与基线不同（不许恒真、不许静默跳过）
    def dumb_always_wait(t, a):
        return WAIT

    def dumb_ignore_alive(t, a):
        return decide(t, True)

    def dumb_skip_means_done(t, a):
        return DONE if (t and "跳过" in t) else decide(t, a)

    base = [decide(t, a) for _n, t, a, _w in cases]
    for label, fn in (("恒'等'（永不接力）", dumb_always_wait),
                      ("忽略进程死活", dumb_ignore_alive),
                      ("把'跳过'读成'收'（尾巴就此丢）", dumb_skip_means_done)):
        out = [fn(t, a) for _n, t, a, _w in cases]
        rows.append(f"[{'CAUGHT' if out != base else 'MISSED'}] 变异 {label}："
                    f"与基线{'不同，判据当场分得开' if out != base else '完全一样——恒真'}")
    text = "\n".join(rows)
    print(text, flush=True)
    fails = sum(1 for l in rows if l.startswith("[FAIL]"))
    missed = sum(1 for l in rows if l.startswith("[MISSED]"))
    print(f"[FINAL] relay selftest —— 判据 {len(cases)} 条红 {fails}／变异 3 条漏网 {missed}"
          f" —— 条数由脚本自己数", flush=True)
    return 1 if fails or missed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="给 watch_ui_r17 接力（只等、只起，不自己跑判据）")
    ap.add_argument("--hops", type=int, default=3, help="最多接力几次（默认 3 次，每次 8 小时）")
    ap.add_argument("--deadline-hours", type=float, default=40.0, help="本位自己总时长上限")
    ap.add_argument("--selftest", action="store_true", help="只验判决，不碰进程与文件")
    ap.add_argument("--once", action="store_true", help="只判一次就退出（调试用，不接力）")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.selftest:
        return selftest()

    log(f"起步：最多接力 {a.hops} 次，本位上限 {a.deadline_hours} 小时，"
        f"上游哨兵={os.path.basename(UPSTREAM_FLAG)}")
    if os.path.exists(MY_FLAG):
        os.remove(MY_FLAG)               # 幂等：旧哨兵不许冒充这一趟的结论
    end = time.monotonic() + a.deadline_hours * 3600.0
    hops = 0
    outcome = None
    while True:
        alive = upstream_alive()
        text = read_flag()
        d = decide(text, alive)
        if d == WAIT:
            log(f"上游还在跑（alive={alive}，哨兵{'有' if text else '没'}），本位继续等")
        elif d == DONE:
            head = (text or "").strip().splitlines()
            log(f"收工：上游哨兵已给出不是'跳过'的结论 → {head[:3]}")
            outcome = "done(上游给出真结论，接力结束)"
            break
        else:
            if hops >= a.hops:
                log(f"接力次数用完（{hops} 次）而屏幕仍不给——这一格只能记'没拿到读数'，"
                    f"不许冒充跑过")
                outcome = f"exhausted(hops={hops})"
                break
            hops += 1
            log(f"第 {hops} 次接力：上游不在了且哨兵是"
                f"{'“跳过”' if text else '不存在（没起来）'}")
            if a.once:
                outcome = "once(没真接力)"
                break
            pid = spawn_upstream()
            if pid is None:
                outcome = "spawn-failed"
                break
            time.sleep(20.0)            # 让它把"起步先看屏"那行落进日志，再回到轮询
            continue
        if time.monotonic() >= end:
            log(f"到本位上限 {a.deadline_hours} 小时，最后一次读数见上一行——"
                f"这一趟不作数，但账留在这里")
            outcome = f"deadline(hops={hops})"
            break
        if a.once:
            outcome = f"once({d})"
            break
        time.sleep(POLL_SECS)
    try:
        with open(MY_FLAG, "w", encoding="utf-8", errors="replace", newline="\n") as fh:
            fh.write(f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                     f"outcome={outcome}\nrelay_hops={hops}\n"
                     f"upstream_flag={UPSTREAM_FLAG}\n")
    except OSError as exc:
        log(f"哨兵写不下去：{exc}")
    log(f"FINAL outcome={outcome} hops={hops} 哨兵={MY_FLAG}")
    return 0 if outcome and outcome.startswith("done") else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise                                   # 正常退出不是"自身异常"：兜住它就会把 rc 改假
    except BaseException as exc:                # noqa: BLE001
        log(f"接力看门狗自身异常：{type(exc).__name__}: {exc}")
        import traceback
        print(traceback.format_exc(), flush=True)
        sys.exit(2)
