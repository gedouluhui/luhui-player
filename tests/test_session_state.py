"""会话四态闭环自检：内核真值 → 配置 → 下次启动参数（以及反向的配置 → 参数）。

用法：
    python tests/test_session_state.py            # 验收：全部 PASS 才算过
    python tests/test_session_state.py --falsify  # 证伪：注入已知坏实现，判据必须翻红

为什么单开一条：
  音量/静音/倍速/循环/乱序这五样，"关掉再开就复位"是曾经的真 bug。现在由
  `MainWindow._remember_playback_state` 在 closeEvent 里从**内核缓存**回写，再由
  `MpvPlayer.build_args` 在下次启动时带回内核。这条链跨两个模块、五个键，任何一环偷懒
  都不会崩：只会安静地"重启就复位"，用户根本不知道该报什么。所以这里不测"函数被调过"，
  而是测**盘上的 config.json** 和**真正要递给内核的 argv**：
      内核回报 → cfg 内存值 → cfg 落盘 → 新 Config 从盘上读回 → build_args 的 argv。
  两端都从证据出发，中间任何一段掉链子都会有一条判据红。

不测真内核：那一段由 tests/test_engine.py 负责。这里用假内核喂"内核本来会回报的读数"，
专测这段回写/拼参的类型收口与映射；界面全程 offscreen，不弹窗。
"""
from __future__ import annotations

import json
import os
import shutil
import sys

# 用户还在用电脑：必须在导入 Qt / app 之前设 offscreen。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, ".selftest-session")
os.environ["GELU_DATA"] = DATA
shutil.rmtree(DATA, ignore_errors=True)
os.makedirs(DATA, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine.player import MpvPlayer  # noqa: E402
from app.ui import main_window as mw  # noqa: E402

APP = None
results: list[tuple[str, bool, str]] = []
falsified: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


# ---------------------------------------------------------------- 判据（证伪复用同一份）
class FakeKernel:
    """只回缓存里的值；问不到就回调用方给的 default——和 MpvPlayer.prop 的契约一致。"""

    def __init__(self, values: dict):
        self.values = dict(values)

    def prop(self, name, default=None):
        return self.values.get(name, default)


def args_map(cfg) -> dict[str, str]:
    """把 build_args 的 argv 拆成 key→value；无 '=' 的键记 True。"""
    out: dict[str, str] = {}
    for a in MpvPlayer(cfg).build_args(0):
        if a.startswith("--"):
            k, sep, v = a.partition("=")
            out[k] = v if sep else "True"
    return out


BUILD_CASES = [
    # (配置四态, 必须出现的 --键=值, 必须不出现的 --键)
    ({"mute": True, "speed": 1.75, "loop_playlist": "no", "shuffle": False},
     {"--mute": "yes", "--speed": "1.75"},
     ("--loop-file", "--loop-playlist", "--shuffle")),
    ({"mute": False, "speed": 1.0, "loop_playlist": "inf", "shuffle": True},
     {"--mute": "no", "--speed": "1.0", "--loop-playlist": "inf", "--shuffle": "yes"},
     ("--loop-file",)),
    ({"mute": True, "speed": 2.5, "loop_playlist": "one", "shuffle": True},
     {"--mute": "yes", "--speed": "2.5", "--loop-file": "inf", "--shuffle": "yes"},
     ("--loop-playlist",)),
    ({"mute": False, "speed": 0.5, "loop_playlist": "no", "shuffle": False},
     {"--mute": "no", "--speed": "0.5"},
     ("--loop-file", "--loop-playlist", "--shuffle")),
]


def judge_build_args() -> list[str]:
    bad: list[str] = []
    for four, want, absent in BUILD_CASES:
        cfg = cfgmod.Config(os.path.join(DATA, "build-case.json"))
        for k, v in four.items():
            cfg[k] = v
        amap = args_map(cfg)
        for k, v in want.items():
            if amap.get(k) != v:
                bad.append(f"{four} → {k}={amap.get(k)!r}（要 {v!r}）")
        for k in absent:
            if k in amap:
                bad.append(f"{four} → 不该出现 {k}（实际 {amap[k]!r}）")
    return bad


def judge_kernel_flag() -> list[str]:
    """表里逐项喂。用 mw.kernel_flag（产品真正调用的那个绑定）而不是 player 里的原名——
    否则把界面的导入打桩成 bool() 时这条判据还会绿，等于没守住产品那条路。"""
    f = mw.kernel_flag
    pairs = [
        ("no", False), ("NO", False), ("false", False), ("0", False), ("", False),
        (" off ", False), ("yes", True), ("TRUE", True), ("1", True), ("on", True),
        (True, True), (False, False), (1, True), (0, False),
    ]
    bad: list[str] = []
    for raw, want in pairs:
        got = f(raw, not want)      # 给相反的默认值：不许靠默认值蒙对
        if got is not want:
            bad.append(f"{raw!r} → {got!r}（要 {want!r}）")
    if f(None, True) is not True or f(None, False) is not False:
        bad.append("None 必须回默认值")
    if f("garbage", True) is not True or f("garbage", False) is not False:
        bad.append("不认识的字符串必须回默认值")
    return bad


def judge_loop_mode() -> list[str]:
    win = mw.MainWindow.__new__(mw.MainWindow)
    bad: list[str] = []
    cases = [
        ({"loop-file": "no", "loop-playlist": "no"}, "no"),
        ({"loop-file": "inf", "loop-playlist": "no"}, "one"),
        ({"loop-file": "yes", "loop-playlist": "no"}, "one"),
        ({"loop-file": "no", "loop-playlist": "inf"}, "inf"),
        ({"loop-file": "no", "loop-playlist": "yes"}, "inf"),
        ({"loop-file": "inf", "loop-playlist": "inf"}, "one"),   # 两道都开时单曲优先
        ({"loop-file": "false", "loop-playlist": "0"}, "no"),
        ({}, "no"),
    ]
    for props, want in cases:
        got = win._loop_mode_from_kernel(FakeKernel(props))
        if got != want:
            bad.append(f"{props} → {got!r}（要 {want!r}）")
    return bad


def round_trip(kernel_values: dict, tag: str) -> dict:
    """喂一个假内核，返回这一趟的每一道读数（内存、盘上、下次 argv）。"""
    win = mw.MainWindow.__new__(mw.MainWindow)
    cfg = cfgmod.Config(os.path.join(DATA, f"roundtrip-{tag}.json"))
    win.cfg = cfg
    win.player = FakeKernel(kernel_values)
    state = win._remember_playback_state()
    saved_ok = cfg.save()
    try:
        with open(cfg.path, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
    except OSError as exc:
        on_disk = {"_error": str(exc)}
    fresh = cfgmod.Config(cfg.path)
    return {
        "state": state,
        "mem": {k: cfg[k] for k in ("volume", "mute", "speed", "shuffle", "loop_playlist")},
        "disk": {k: on_disk.get(k, "<缺>") for k in
                 ("volume", "mute", "speed", "shuffle", "loop_playlist")},
        "next": args_map(fresh),
        "saved": saved_ok,
    }


def judge_roundtrip() -> list[str]:
    bad: list[str] = []
    # ① 内核四态与配置默认值刻意全不同；volume 给真值 0（旧写法会把它变回 100）。
    r = round_trip({"volume": 0, "mute": True, "speed": 2.5, "shuffle": True,
                    "loop-file": "inf", "loop-playlist": "no"}, "full")
    want = {"volume": 0, "mute": True, "speed": 2.5, "shuffle": True, "loop_playlist": "one"}
    if not r["saved"]:
        bad.append("配置没有落盘成功")
    for face in ("state", "mem", "disk"):
        if r[face] != want:
            bad.append(f"{face}={r[face]}（要 {want}）")
    for k, v in (("--volume", "0"), ("--mute", "yes"), ("--speed", "2.5"),
                 ("--loop-file", "inf"), ("--shuffle", "yes")):
        if r["next"].get(k) != v:
            bad.append(f"下次启动 {k}={r['next'].get(k)!r}（要 {v!r}）")
    if "--loop-playlist" in r["next"]:
        bad.append("下次启动不该同时带 --loop-playlist（配置是单曲循环）")

    # ② 字符串坏形状：mpv 的 loop 族本来就是字符串；bool("no") 会把它当"开"。
    r = round_trip({"volume": 88, "mute": "no", "speed": 1.25, "shuffle": "false",
                    "loop-file": "no", "loop-playlist": "inf"}, "strings")
    want = {"volume": 88, "mute": False, "speed": 1.25, "shuffle": False, "loop_playlist": "inf"}
    if r["state"] != want or r["disk"] != want:
        bad.append(f"字符串 no/false 被读反：state={r['state']} disk={r['disk']}")

    # ③ 内核刚死/没有媒体：所有 prop 都回 default。配置必须原样保留、不许崩、不许清零。
    cfg = cfgmod.Config(os.path.join(DATA, "empty.json"))
    for k, v in {"volume": 137, "mute": True, "speed": 0.75,
                 "shuffle": True, "loop_playlist": "inf"}.items():
        cfg[k] = v
    win = mw.MainWindow.__new__(mw.MainWindow)
    win.cfg = cfg
    win.player = FakeKernel({})
    state = win._remember_playback_state()
    four = {k: state.get(k) for k in ("volume", "mute", "speed", "shuffle")}
    if four != {"volume": 137, "mute": True, "speed": 0.75, "shuffle": True}:
        bad.append(f"无内核读数时不该清掉配置：{state}")
    if state.get("loop_playlist") != "no":
        bad.append(f"没有循环属性时该回 no：{state.get('loop_playlist')!r}")

    # ④ 越界/坏类型必须收口：250 不许原样落盘，0 倍速不许写进下次 --speed。
    r = round_trip({"volume": 250, "mute": "garbage", "speed": 0.0,
                    "shuffle": 1, "loop-file": "no", "loop-playlist": "no"}, "clamp")
    if not (r["state"]["volume"] == 200 and r["state"]["speed"] == 1.0):
        bad.append(f"250/0.0 没收口：{r['state']}")
    if r["state"]["shuffle"] is not True:
        bad.append(f"数字 1 的开关没收成 True：{r['state']['shuffle']!r}")
    if r["next"].get("--volume") != "200" or r["next"].get("--speed") != "1.0":
        bad.append(f"收口没传到启动参数：{r['next'].get('--volume')!r}/"
                   f"{r['next'].get('--speed')!r}")
    return bad


# ---------------------------------------------------------------- 证伪
def falsify() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    base_remember = mw.MainWindow._remember_playback_state
    base_loop = mw.MainWindow._loop_mode_from_kernel
    base_loop_args = MpvPlayer._loop_args
    orig_flag = mw.kernel_flag

    def caught(label: str, judge) -> None:
        bad = judge()
        falsified.append((label, bool(bad),
                          "；".join(bad[:3]) if bad else "判据居然还是绿的"))

    # M1 旧写法：volume 读一次 `or 100`，真值 0 被当成"没读到"。
    def old_volume_remember(self, kernel=None):
        st = base_remember(self, kernel)
        p = self.player if kernel is None else kernel
        st["volume"] = int(p.prop("volume", self.cfg["volume"]) or 100)
        self.cfg["volume"] = st["volume"]
        return st

    mw.MainWindow._remember_playback_state = old_volume_remember
    mutated_remember = mw.MainWindow._remember_playback_state
    try:
        caught("M1 音量真值 0 被旧写法 `or 100` 复活（roundtrip 判据）", judge_roundtrip)
    finally:
        mw.MainWindow._remember_playback_state = base_remember
    falsified.append(("M1b 打桩真的换掉了方法对象",
                      mutated_remember is not base_remember
                      and mw.MainWindow._remember_playback_state is base_remember, ""))

    # M2 漏写 mute：只回写剩下几样。判据读的是盘上字节，必须点名。
    def drop_mute(self, kernel=None):
        st = base_remember(self, kernel)
        st.pop("mute", None)
        self.cfg.data.pop("mute", None)
        return st

    mw.MainWindow._remember_playback_state = drop_mute
    try:
        caught("M2 漏写 mute（盘上缺键）必须被抓到", judge_roundtrip)
    finally:
        mw.MainWindow._remember_playback_state = base_remember

    # M3 回写读界面意图而不是内核：配置默认 False/1.0，假内核说 True/2.5。
    def from_cfg(self, kernel=None):
        st = {k: self.cfg[k] for k in ("volume", "mute", "speed", "shuffle", "loop_playlist")}
        for k, v in st.items():
            self.cfg[k] = v
        return st

    mw.MainWindow._remember_playback_state = from_cfg
    try:
        caught("M3 回写读配置而不是内核真值必须被抓到", judge_roundtrip)
    finally:
        mw.MainWindow._remember_playback_state = base_remember

    # M4 循环映射永远回 no（等于这功能没接线）。
    mw.MainWindow._loop_mode_from_kernel = lambda self, kernel=None: "no"
    mutated_loop = mw.MainWindow._loop_mode_from_kernel
    try:
        caught("M4 循环映射恒回 no 必须被 loop 判据点名", judge_loop_mode)
        caught("M4b 循环映射恒回 no 也必须让 roundtrip 翻红", judge_roundtrip)
    finally:
        mw.MainWindow._loop_mode_from_kernel = base_loop
    falsified.append(("M4c 打桩真的换掉了循环映射",
                      mutated_loop is not base_loop
                      and mw.MainWindow._loop_mode_from_kernel is base_loop, ""))

    # M5 启动参数漏掉 shuffle（配置读对了、发不下去）。
    def no_shuffle(c):
        mode = str(c.get("loop_playlist", "no") or "no")
        if mode == "one":
            return ["--loop-file=inf"]
        if mode in ("inf", "yes", "true"):
            return ["--loop-playlist=inf"]
        return []

    MpvPlayer._loop_args = staticmethod(no_shuffle)
    mutated_args = MpvPlayer._loop_args
    try:
        caught("M5 build_args 漏掉 --shuffle 必须被 argv 判据抓到", judge_build_args)
    finally:
        MpvPlayer._loop_args = staticmethod(base_loop_args)
    falsified.append(("M5b 打桩真的换掉了拼参函数（且复原后仍是 staticmethod）",
                      mutated_args is not base_loop_args
                      and MpvPlayer._loop_args is base_loop_args, ""))

    # M6 产品那条路把开关解析器换成 bool()：loop 族字符串会被读反。
    #    `kernel_flag` 比旧写法多一个 default 参数，所以桩要把它吞掉再调 bool——
    #    直接塞 bool 会 TypeError，那不是"判据抓到了"，是桩自己坏了。
    mw.kernel_flag = lambda v, d=False: bool(v)
    try:
        caught("M6 用 bool() 当开关解析器必须被表抓住", judge_kernel_flag)
        caught("M6b bool() 也必须让 roundtrip 的字符串样本翻红", judge_roundtrip)
    finally:
        mw.kernel_flag = orig_flag
    falsified.append(("M6c 打桩真的换掉了开关解析器", mw.kernel_flag is orig_flag, ""))

    # caught() 自己会不会把 MISSED 也写成 CAUGHT？用一对已知答案做内部对照。
    n0 = len(falsified)
    caught("_自检：恒红判据必须记 CAUGHT", lambda: ["假红"])
    self_check_ok = len(falsified) == n0 + 1 and falsified[-1][1] is True
    falsified.pop()
    caught("_自检：全绿判据必须记 MISSED", lambda: [])
    self_check_green = len(falsified) == n0 + 1 and falsified[-1][1] is False
    falsified.pop()
    falsified.append(("caught() 自己会把红记 CAUGHT、把绿记 MISSED（判据不恒真）",
                      self_check_ok and self_check_green, ""))

    return report()


def report() -> int:
    passed = sum(1 for _, ok, _ in results if ok)
    fails = [n for n, ok, _ in results if not ok]
    print(f"\n---- 汇总：{passed}/{len(results)} 通过"
          + (f"；失败项：{fails}" if fails else "；无失败") + " ----", flush=True)
    if falsified:
        print("==== 证伪（坏样本必须被抓出来）====")
        for name, ok, detail in falsified:
            print(f"[{'CAUGHT' if ok else 'MISSED'}] {name}"
                  + (f"  —— {detail}" if detail else ""), flush=True)
        missed = [n for n, ok, _ in falsified if not ok]
        print(f"---- 证伪收口：抓到 {len(falsified) - len(missed)}/{len(falsified)}"
              f"（漏网 {len(missed)}） ----", flush=True)
        if missed:
            return 1
    return 0 if passed == len(results) else 1


def main() -> int:
    global APP
    APP = QApplication.instance() or QApplication([])
    build_bad = judge_build_args()
    loop_bad = judge_loop_mode()
    flag_bad = judge_kernel_flag()
    round_bad = judge_roundtrip()
    check("配置 → 启动参数：四种组合逐键对得上（mute/speed/loop/shuffle）",
          not build_bad, "；".join(build_bad[:4]))
    check("内核循环串 → 三档模式（no/one/inf，双开单曲优先）",
          not loop_bad, "；".join(loop_bad[:4]))
    check("开关解析表：字符串 no/false/0 是假，yes/true/1 是真，None 回默认",
          not flag_bad, "；".join(flag_bad[:4]))
    check("内核 → 配置 → 落盘 → 下次 argv 的整条回环（含音量真值 0、字符串坏形状、"
          "无内核读数、越界值收口）",
          not round_bad, "；".join(round_bad[:6]))
    return report()


if __name__ == "__main__":
    sys.exit(falsify() if "--falsify" in sys.argv else main())
