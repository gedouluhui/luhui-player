"""R17 收口链：把本轮改过的东西（双击加载、单实例转交、播放列表追加语义、启动器与注册模板）
在**不需要屏幕**的那几条腿上按顺序再跑一遍，并把每一趟的 rc 记成一本能对账的账。

为什么要有这个文件：这些腿各自都能单独跑，但"记得全"这件事只有串行才成立。
本仓的死规矩（`docs/design.md` §3、以及 tests/out 里那几本账的形状）是：
  · **会改盘上文本产物的趟不许并跑**（`test_launchers.py --falsify` 与
    `falsify_openwith_shell.py` 都是"逐份改坏→要求点名→还原后 md5 对账"，两趟同时跑，
    第二趟会把第一趟的**变异态**当干净基线抓快照——r11 实发过一次）；
  · **会自己起真窗口的趟与 test_ui 不许并跑**（test_ui 的按键通道与像素判据量的是
    "前台此刻归谁"，另一趟弹自己的窗口，读到的就不是被测者）。所以整条链开着
    `tests/out/chain-r17.lock`，`tests/watch_ui_r17.py` 见到锁就不起趟，等这里收掉。
所以：一条一条来，`subprocess.call` 同步等，全程持锁，`finally` 里拆锁——
拆锁排在"写哨兵"之前不对，排在之后才对：哨兵里没有这一行的时候锁必须还在。

跑法：
    python tests/chain_r17.py            # 全链，串行
日志：tests/out/chain-r17.log（每趟两行：开跑 / 跑完 rc=…）
哨兵：tests/out/chain-r17-done.flag（逐趟 rc＋分类＋[FINAL] 一句人话）
退出码：0 全绿；1 至少一趟真失败；2 有"环境不给"（rc=2 那类）但没有真失败；3 链没跑完就中断。

判据一律不在这里重写：每条腿自己那本账里有牙（正跑＋`--falsify`/`--expect-red` 反向对照），
这里只负责"按顺序点齐、把 rc 归成三家人话"。
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT_DIR = os.path.join(HERE, "out")
LOCK = os.path.join(OUT_DIR, "chain-r17.lock")
FLAG = os.path.join(OUT_DIR, "chain-r17-done.flag")
LOG = os.path.join(OUT_DIR, "chain-r17.log")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# (名字, 参数)。顺序是**有讲究的**：
#  ① 引擎/实例层先跑（不碰注册表、不起新窗口，出问题最快定位到本轮那两处改动）；
#  ② 再跑只读核对（注册表现数、外壳枚举）——它们只报现状，不会改任何东西；
#  ③ 然后才是会改盘上文本产物的证伪趟（一次只一趟，绝不并跑）；
#  ④ 真起实例的 e2e 排在尾巴上：这两趟会弹真窗口，排在最后是为了让"锁还开着"的时间窗
#     尽量短（看门狗在等这把锁）。
LEGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("实例层·单实例转交与参数收下（正跑）", ("tests/test_instance.py",)),
    ("实例层·证伪（常见错误写法必须各自顶红）", ("tests/test_instance.py", "--falsify")),
    ("故障自检（出厂装配，只读）", ("-m", "app.doctor")),
    ("故障自检·证伪（已知坏事实必须翻红）", ("-m", "app.doctor", "--falsify")),
    ("故障自检·负对照（没有内核的装配里必须报缺）", ("-m", "app.doctor", "--neg")),
    # 名字里不带"几条判据／几种坏写法"：那两个数是**表长**，加一条判据就得记着回来改这里，
    # 上一轮就漏了一次（真数 21／10、名字里还写着 20／9）。条数由每趟自己的 `[FINAL]` 打印。
    ("播放列表追加语义（引擎＋接线）", ("tests/test_append_semantics.py",)),
    ("播放列表追加语义·证伪（每种坏写法必须全被抓）", ("tests/test_append_semantics.py", "--falsify")),
    # 「已载入／已追加」那句话的本体：离线两趟（判据＋证伪）证明"这句话只在内核推来证据
    # 之后才说"，真内核那一腿（排在尾巴的 e2e）证明"内核确实会推来那一手"。
    # 这一对是 2026-10-19 双击黑屏那笔账的直接后果：当时界面那句"已载入 1 个文件"是
    # 写完管道就说的，而内核把整条 loadfile 退了回去——离线全绿、用户那边黑的。
    ("打开回执（那句话只认内核推来的证据）", ("tests/test_open_receipt.py",)),
    ("打开回执·证伪（回到那句谎必须顶红）", ("tests/test_open_receipt.py", "--falsify")),
    # 媒体库这两趟本轮才接进链：R17 给「最近播放」加了规范化去重（同一部片的 `F:/` 与
    # `F:\` 两种写法不许并排占两格），那条判据与它的证伪以前只在手跑时绿过——
    # "只有手跑过的判据"等于没有判据，下一轮改坏了没人拦。
    ("媒体库（真扫描＋数字对账，含最近播放去重）", ("tests/test_library.py",)),
    ("媒体库·证伪（静默给错数与去重键退化必须被点名）", ("tests/test_library.py", "--falsify")),
    ("启动器与注册模板·只复查盘上产物＋生成器自测", ("packaging/make_launchers.py",
                                                   "--check", "--selftest")),
    ("启动器验收（正跑）", ("tests/test_launchers.py",)),
    ("启动器验收·证伪（逐份改坏必须点名，还原后 md5 对账）", ("tests/test_launchers.py", "--falsify")),
    ("「打开方式」注册表现数核对＋模板 lint", ("tests/check_openwith.py", "--check")),
    ("「打开方式」核对器自证有牙（每种坏法必须被点名）", ("tests/check_openwith.py", "--selftest")),
    ("右键外壳枚举（正跑）", ("tests/check_openwith_shell.py",)),
    ("右键外壳枚举·反向对照（不可能的名字必须翻红）", ("tests/check_openwith_shell.py", "--expect-red")),
    ("右键外壳·证伪（改坏模板必须被点到名）", ("tests/falsify_openwith_shell.py",)),
    ("端到端：按注册表里那条命令原样执行（正跑）", ("tests/e2e_openwith.py",)),
    ("端到端·反向对照（坏路径必须翻红）", ("tests/e2e_openwith.py", "--expect-red",
                                        "C:\\no-such-dir\\no-such-video.mp4")),
    ("端到端：已开实例再双击（正跑）", ("tests/e2e_instance_handoff.py",)),
    ("端到端：已开实例再双击·反向对照", ("tests/e2e_instance_handoff.py", "--expect-red")),
    # GPU 这两趟本轮才接进链：「硬解到底在不在跑」是对用户报的成品级卖点，而它一直只在手跑时绿过
    # ——只有手跑过的判据等于没有判据。反向对照那条（强制软解时要求"硬解在跑"必须翻红）
    # 就是①那趟的牙，两趟必须同链跑，否则①的绿没人能证明它不是恒真。
    # 真内核 loadfile 那一腿排在离线两趟之后：它验的是"内核认不认这条命令、认了之后
    # 到底从续播位置起没起"，离线那两趟再绿也答不了这个问题（2026-10-19 就是这么骗过去的）。
    # 这趟自己也有牙——注入当年的凶形状三句同时翻红，留档 tests/out/e2e-loadfile-drift-1019-215401.txt。
    ("端到端：真内核 loadfile 回执（含续播与鉴别力对照）", ("tests/e2e_loadfile_receipt.py",)),
    ("端到端：GPU 加速实测（三档装配·串行）", ("tests/e2e_gpu.py",)),
    ("端到端：GPU 实测·反向对照（软解那趟不许报硬解）", ("tests/e2e_gpu.py", "--expect-red")),
)


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")


def classify(rc: int) -> str:
    """rc → 三家人话。**不许**把 rc=2（环境不给）和 rc=1（真失败）混成"没通过"：
    这两家的处置完全不同——一家是"换个时间/换个装配重跑"，另一家是"产品坏了，去查"。"""
    if rc == 0:
        return "绿"
    if rc == 2:
        return "环境不给（不是产品坏）"
    if rc == 3:
        return "本轮不作数（判废/缺证据）"
    return f"真失败（rc={rc}）"


def run_leg(name: str, args: tuple[str, ...]) -> int:
    # 两种形状分开拼：`-m app.doctor` 是模块，其余是脚本绝对路径。
    # （写成 `script[3:]` 那类字符串切片会把 "-m" 自己当脚本名，拼出 `python -m '' app.doctor`
    #  这种谁也没法解释的命令行——判据红不红先不说，命令行本身就是假的。）
    if args[0] == "-m":
        cmd = [sys.executable, "-m", args[1], *args[2:]]
    else:
        cmd = [sys.executable, os.path.join(ROOT, args[0]), *args[1:]]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    log(f"开跑 [{name}]：{' '.join(os.path.relpath(c, ROOT) if os.path.isabs(c) else c for c in cmd)}")
    t0 = time.time()
    with open(LOG, "a", encoding="utf-8", errors="replace", newline="\n") as fh:
        fh.write(f"\n======== {name} ========\n")
        fh.flush()
        rc = subprocess.call(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)
    log(f"跑完 rc={rc}（{classify(rc)}）用时 {time.time() - t0:.0f}s：[{name}]")
    return rc


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(LOCK):
        # 已有锁＝另一条链正在跑。本仓的死规矩是"会改文件的趟一次只一趟"，所以这里**拒跑**，
        # 不排队也不抢——抢跑的那趟会把别人的基线读成自己的。
        log(f"拒跑：锁还开着（{LOCK}，改动时间 "
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(LOCK)))}）。"
            "先看一眼是不是有一条链正在跑（或上次中断没拆锁）。")
        return 3
    with open(LOCK, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"pid={os.getpid()} started_at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    if os.path.exists(LOG):
        keep_log = os.path.join(OUT_DIR, f"chain-r17.pre-{datetime.datetime.now():%m%d-%H%M%S}.log")
        with open(LOG, "rb") as src, open(keep_log, "wb") as dst:
            dst.write(src.read())                 # 上一趟中断的账先留底，再开新一本（本机没有 git）
        os.remove(LOG)
    if os.path.exists(FLAG):
        keep = os.path.join(OUT_DIR, f"chain-r17-done.pre-{datetime.datetime.now():%m%d-%H%M%S}.flag")
        with open(FLAG, "rb") as src, open(keep, "wb") as dst:
            dst.write(src.read())                 # 本机没有 git：旧账先带时间戳留底，再覆写
        log(f"旧哨兵已另抄为 {os.path.basename(keep)}（不覆写已引用过的那份）")
    if os.path.exists(LOG):
        os.remove(LOG)               # 本轮的日志从这一行起重写；旧一本已在 OUT 里按时间戳留着
    log(f"链开跑，共 {len(LEGS)} 趟（串行，全程持锁；看门狗见锁不起 UI 趟）")
    rcs: list[tuple[str, int]] = []
    rc_int = 1
    try:
        for name, args in LEGS:
            rcs.append((name, run_leg(name, args)))
        bad = [n for n, r in rcs if classify(r).startswith("真失败")]
        envy = [n for n, r in rcs if r == 2]
        na = [n for n, r in rcs if r == 3]
        lines = [f"generated_at={time.strftime('%Y-%m-%d %H:%M:%S')}",
                 f"legs={len(rcs)}/{len(LEGS)}"]
        lines += [f"{n}: rc={r}（{classify(r)}）" for n, r in rcs]
        lines.append(f"[FINAL] 绿 {sum(1 for _n, r in rcs if r == 0)}／真失败 {len(bad)}／"
                     f"环境不给 {len(envy)}／本轮不作数 {len(na)} —— 趟数由脚本自己数，"
                     f"共 {len(LEGS)} 趟"
                     + (f"；点名：{bad}" if bad else ""))
        lines.append("DONE")
        with open(FLAG, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        log(lines[-2])
        rc_int = 1 if bad else (2 if envy or na else 0)
    except BaseException as exc:                                  # noqa: BLE001
        log(f"链中断：{type(exc).__name__}: {exc}")
        import traceback
        print(traceback.format_exc(), flush=True)
        with open(FLAG, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"[FINAL] 链中断于第 {len(rcs) + 1} 趟："
                     f"{type(exc).__name__}: {exc}\nDONE\n")
        rc_int = 3
    finally:
        # 拆锁必须发生在"哨兵已落盘"之后：哨兵没有 [FINAL] 的那段时间里，锁还在，
        # 看门狗就不会以为链已结束而起 UI 趟。
        try:
            os.remove(LOCK)
            log(f"锁已拆：{os.path.basename(LOCK)}（UI 看门狗可以起趟了）")
        except OSError as exc:
            log(f"锁没拆掉（要人工看一眼 {LOCK}）：{type(exc).__name__}: {exc}")
    return rc_int


if __name__ == "__main__":
    sys.exit(main())
