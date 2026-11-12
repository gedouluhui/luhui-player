"""看门狗：屏幕一给像素就把 R3-2e 的真机验收整条链自己跑完，产物落 tests/out/。

为什么要有这份文件（而不是"等你解锁再跑"）：真机验收的判据要看屏幕，而显示器休眠/锁屏时
`test_gif_real.py` 诚实回 rc=3。这是**外部条件**，不是产品失败，也不该变成用户的手工活：
条件一翻转就该自动接上。

行为：
  每 POLL 秒问一次 `python tests/test_gif_real.py`——
    rc=3   ⇒ 屏幕还不给像素，继续等（每一次等待都在日志里留一行时间戳）；
    rc=0/1 ⇒ 屏幕醒了、真机跑完，接着把 --falsify 与 --residue 也跑一遍，写 FINAL 与 DONE 哨兵；
    其它   ⇒ 视为环境外故障（缺 ffmpeg/样片、崩了），照样写 FINAL 点名是哪一步。
  超过 MAX_WAIT 还没等到屏幕：FINAL 里明写"到点屏幕未醒，这几条判据没跑"，不静默消失。

用法：
    python tests/watch_gif_real.py            # 后台挂着（自己会等）
    产物：tests/out/FINAL-gif-real-<stamp>.md、tests/out/DONE-<stamp>.ok
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(HERE, "out")
STAMP = time.strftime("%Y%m%d-%H%M%S")
FINAL = os.path.join(OUT, f"FINAL-gif-real-{STAMP}.md")
SENTINEL = os.path.join(OUT, f"DONE-{STAMP}.ok")
LOG = os.path.join(OUT, f"watch-gif-real-{STAMP}.log")

POLL = 30                # 秒：问一次"屏幕给不给像素"的间隔
MAX_WAIT = 8 * 3600      # 秒：到点没等到就留说明，不静默消失
RUN_TIMEOUT = 900        # 秒：单步的硬上限，卡住也要有 rc

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def note(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def already_running() -> bool:
    """幂等闸门：同一台机器上挂着两个看门狗会互相抢屏幕、各自写一份 FINAL。

    两条本机实测的坑：①**按命令行认，不按镜像名认**（按名字批杀/批判把用户自己的进程
    算进来过，见记忆 [[feedback-windows-edge-automation-safety]]）；②排除自己要用**调用方
    的 pid**，不是 `$PID`——那是 powershell 自己的 pid，用它排除等于没排除，首版就是这么
    一上来判成"已经有一个在跑"然后自己退出的（假幂等，比不幂等更坏）。
    """
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Where-Object { $_.CommandLine -match 'watch_gif_real' -and "
              "$_.ProcessId -ne " + str(os.getpid()) + " } | "
              "Measure-Object | Select-Object -Expand Count")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "").strip() not in ("", "0")
    except BaseException:  # noqa: BLE001 - 查不到就当没有，宁可多跑一份也不饿死
        return False


def step(args: list[str]) -> tuple[int, str]:
    """跑一步，永不让它把整链带走：超时/异常都变成可报告的 rc + 尾巴。"""
    try:
        r = subprocess.run([sys.executable, "-u"] + args, cwd=ROOT,
                           capture_output=True, text=True, timeout=RUN_TIMEOUT,
                           encoding="utf-8", errors="replace")
        out = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
        return int(r.returncode), out
    except subprocess.TimeoutExpired:
        return 124, f"这一步超过 {RUN_TIMEOUT} 秒没回来（卡住，不是判红）"
    except BaseException as exc:  # noqa: BLE001
        return 125, f"这一步根本没起来：{type(exc).__name__}: {exc}"


def write_final(rcs: dict[str, int], tails: dict[str, str], woke: bool) -> None:
    lines = [f"# R3-2e 截图工坊真机验收（看门狗自动跑完，{STAMP}）", ""]
    lines.append(f"- 屏幕等到像素：{'是' if woke else '否（到点未醒）'}")
    for key, rc in rcs.items():
        mark = "✅" if rc == 0 else ("⏳" if rc == 3 else "❌")
        lines.append(f"- {mark} `{key}` → rc={rc}")
    lines += ["", "## 每步回执（尾部）", ""]
    for key, tail in tails.items():
        lines.append(f"### {key}（rc={rcs[key]}）")
        lines.append("```")
        lines.append("\n".join(tail.splitlines()[-40:]))
        lines.append("```")
        lines.append("")
    with open(FINAL, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    if woke and all(v == 0 for v in rcs.values()):
        with open(SENTINEL, "w", encoding="utf-8") as fh:
            fh.write("all green\n")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    if already_running():
        note("已有一个看门狗在等屏幕，这份退出（幂等闸门）")
        return 0
    note(f"开始等屏幕给像素：每 {POLL} 秒问一次，最长 {MAX_WAIT // 3600} 小时")
    deadline = time.monotonic() + MAX_WAIT
    rcs: dict[str, int] = {}
    tails: dict[str, str] = {}
    woke = False
    while time.monotonic() < deadline:
        rc, tail = step([os.path.join("tests", "test_gif_real.py")])
        if rc == 3:
            note("屏幕还不给像素（rc=3），继续等")
            time.sleep(POLL)
            continue
        rcs["real(真机)"] = rc
        tails["real(真机)"] = tail
        woke = True
        note(f"屏幕醒了，真机跑完 rc={rc}")
        break
    if not woke:
        rcs["real(真机)"] = 3
        tails["real(真机)"] = f"等满 {MAX_WAIT // 3600} 小时屏幕没醒，三条像素类判据没跑。"
        note("到点屏幕未醒：写 FINAL 说明，不静默消失")
        write_final(rcs, tails, woke)
        return 3
    for key, args in (("falsify(证伪 12 组)", [os.path.join("tests", "test_gif_real.py"), "--falsify"]),
                      ("residue(关窗残留)", [os.path.join("tests", "test_gif_real.py"), "--residue"])):
        rc, tail = step(args)
        rcs[key] = rc
        tails[key] = tail
        note(f"{key} → rc={rc}")
    write_final(rcs, tails, woke)
    note(f"FINAL → {FINAL}")
    return 0 if all(v == 0 for v in rcs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
