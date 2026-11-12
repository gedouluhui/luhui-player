"""资源实测（app/engine/usage.py + 统计页那四行）的验收：不看像素，跑真测。

用法：
    python tests/test_usage.py              # 验收：全部判据必须 PASS
    python tests/test_usage.py --falsify    # 证伪：把实现换成常见错误写法，对应判据必须翻红

为什么这份能脱离屏幕跑：它量的是**数字与状态**，不是画面。CPU/内存按 pid 取，
GPU 从 nvidia-smi 的 csv 取，界面那四行只是把数写进 QLabel 的显隐——offscreen 平台
就能验完（同 tests/test_gif_wiring.py 的做法），所以显示器休眠时也在跑。

三条老规矩（本仓所有验收脚本共用）：
  · 判据不许恒真 —— 每条都由 --falsify 里对应的坏写法证伪；
  · 判据自己不许崩 —— 每条包在 try/except BaseException 里，崩了单独记 FAIL；
  · 环境不给要单独报码 —— 没装 psutil 或没有 nvidia-smi 时退 2，不写成产品失败。

再加一条本文件特有的：**"测到了"必须由一次真测证明**。CPU 百分比是"两个时刻之差"，
把 `time.sleep(window)` 摘掉的写法照样跑出一堆 0.0%、照样"看起来正常"。所以这里起一个
真在烧 CPU 的子进程，要求量到 ≥50%——这条不通，其余数字都不作数。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
TMP = tempfile.mkdtemp(prefix="gelu-usage-")
os.environ["GELU_DATA"] = os.path.join(TMP, "data")      # 必须在 import config 之前
os.environ["QT_QPA_PLATFORM"] = "offscreen"              # 不在用户桌面长窗口，也不依赖屏幕醒着
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication                   # noqa: E402

from app import config as cfgmod                           # noqa: E402
from app.engine import usage as U                          # noqa: E402
from app.engine.player import MpvPlayer                    # noqa: E402
from app.ui import stats_panel as SP                       # noqa: E402

APP = QApplication.instance() or QApplication(sys.argv)

USAGE_PY = os.path.join(ROOT, "app", "engine", "usage.py")
PANEL_PY = os.path.join(ROOT, "app", "ui", "stats_panel.py")
UI_TEST = os.path.join(ROOT, "tests", "test_ui.py")

# 本机 2026-10-08 实测的两条原样输出（RTX 3070 Ti Laptop）。第一条是 --format=csv,noheader
# ,nounits（我们实际下发的那条），第二条是不带 nounits 的：值里带单位。两条都得吃。
LINE_NOUNITS = ("NVIDIA GeForce RTX 3070 Ti Laptop GPU, 5, 3295, 8192, 53, "
                "32.87, 1485, 0x0000000000000001")
LINE_UNITS = ("NVIDIA GeForce RTX 3070 Ti Laptop GPU, 6 %, 3295 MiB, 8192 MiB, "
              "53 C, 32.90 W, 1485 MHz, 0x0000000000000004")
LINE_NA = "GPU, [N/A], N/A, , 53, 31.0, 0, 0x0000000000000000"

results: list[tuple[bool, str, str]] = []


def say(tag: str, name: str, detail: str = "") -> None:
    print(f"[{tag}] {name}" + (f" —— {detail}" if detail else ""))


def emit(items: list[tuple[str, bool, str]]) -> None:
    for name, ok, detail in items:
        results.append((bool(ok), name, detail))
        say("PASS" if ok else "FAIL", name, detail)


def guard(name: str, fn) -> None:
    """判据收集器自己崩了要记成 FAIL，不能拿 traceback 冒充结论（也不能冒充 PASS）。"""
    try:
        emit(fn())
    except BaseException as exc:      # noqa: BLE001 —— 这里就是要兜住一切
        results.append((False, name, f"判据崩了：{type(exc).__name__}: {exc}"))
        say("FAIL", name + "（判据崩了）", f"{type(exc).__name__}: {exc}"[:150])
        if os.environ.get("GELU_USAGE_TB"):
            traceback.print_exc()


def pump(seconds: float, pred=None) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds and (pred is None or not pred()):
        APP.processEvents()
        time.sleep(0.01)
    APP.processEvents()
    return bool(pred is None or pred())


# ---------------------------------------------------------------- 解析 csv
def items_parse() -> list[tuple[str, bool, str]]:
    rows = U.parse_smi(LINE_NOUNITS + "\r\n")
    one = rows[0] if rows else {}
    rows2 = U.parse_smi(LINE_UNITS + "\n")
    na = U.parse_smi(LINE_NA + "\n")
    hdr = U.parse_smi("name,utilization.gpu,memory.used,memory.total,temperature.gpu,"
                      "power.draw,clocks.sm,clocks_throttle_reasons.active\r\n"
                      + LINE_NOUNITS + "\r\n")
    two = U.parse_smi(LINE_NOUNITS + "\r\n" + LINE_UNITS + "\r\n")
    return [
        ("真实一行解析成 8 个字段（含卡名）", len(rows) == 1 and len(one) >= 8, str(len(rows))),
        ("nounits 那行的六个数各就各位",
         [one.get(k) for k in ("utilization.gpu", "memory.used", "memory.total",
                               "temperature.gpu", "power.draw", "clocks.sm")]
         == [5.0, 3295.0, 8192.0, 53.0, 32.87, 1485.0],
         str([one.get(k) for k in U.NUMERIC_FIELDS])),
        ("带单位的行也一样解出数字（6 %→6、32.90 W→32.9）",
         len(rows2) == 1 and rows2[0]["utilization.gpu"] == 6.0
         and rows2[0]["power.draw"] == 32.9, str(rows2[0].get("power.draw"))),
        ("[N/A] / N/A / 空串一律 None，不许顶成 0",
         len(na) == 1 and na[0]["utilization.gpu"] is None
         and na[0]["memory.used"] is None and na[0]["memory.total"] is None,
         str([na[0].get(k) for k in ("utilization.gpu", "memory.used", "memory.total")])
         if na else "没解析出行"),
        ("有表头时表头不算一块卡", len(hdr) == 1, f"行数={len(hdr)}"),
        ("两块卡就出两行（报表要说明取的是第一块）", len(two) == 2, f"行数={len(two)}"),
        ("空文本/垃圾文本返回空列表而不是抛", U.parse_smi("") == []
         and U.parse_smi("nvidia-smi: command not found") == [], ""),
    ]


# ---------------------------------------------------------------- 降频位
def items_throttle() -> list[tuple[str, bool, str]]:
    return [
        ("0x1（空闲）翻成『空闲』，但**不许**进限流名单",
         U.throttle_names(0x1) == ["空闲"] and U.limit_reasons(0x1) == [],
         f"全翻={U.throttle_names(0x1)} 限流={U.limit_reasons(0x1)}"),
        ("0x4 软件功率封顶要留在限流名单里（这台笔记本最常踩的一条）",
         U.limit_reasons(0x4) == ["软件功率封顶"], str(U.limit_reasons(0x4))),
        ("多位一起（热降频+功率刹车）两条都要在",
         set(U.limit_reasons(0x80 | 0x100)) == {"硬件热降频", "功率刹车（外部限流）"},
         str(U.limit_reasons(0x80 | 0x100))),
        ("没见过的位不许丢：写『未知位』带原值",
         U.limit_reasons(0x10000) == ["未知位 0x10000"], str(U.limit_reasons(0x10000))),
        ("位为 None＝没测到 → 空名单（不许当 0 说『不限流』）",
         U.limit_reasons(None) == [] and U.throttle_names(None) == [], ""),
        ("位为 0 就是真的不限流", U.throttle_names(0) == [], ""),
        ("解析器把 active 那列同时留成原文和位掩码",
         U.parse_smi(LINE_NOUNITS)[0]["_throttle"] == 1
         and U.parse_smi(LINE_NOUNITS)[0]["clocks_throttle_reasons.active"]
         == "0x0000000000000001", ""),
    ]


# ---------------------------------------------------------------- 进程量测（真测）
def busy_child() -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "x=0\nwhile True:\n x+=1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def items_cpu() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = [
        ("单核基准 200% 在 20 核机器上是整机 10%",
         abs(U.cpu_share(200.0, 20) - 10.0) < 1e-9, f"{U.cpu_share(200.0, 20)}"),
        ("核数量不到就不许除（否则整机占比变 inf 或炸）",
         U.cpu_share(37.5, 0) == 37.5, str(U.cpu_share(37.5, 0))),
        ("窗口 ≤0 直接抛 ValueError：CPU% 是两时刻之差，单点取不出来",
         _raises(lambda: U.sample_window({"a": os.getpid()}, window=0)), ""),
        ("psutil 可用性与缺装分开报（本机应可用）",
         U.psutil_unavailable() == "", U.psutil_unavailable()[:80] or "可用"),
    ]
    proc = busy_child()
    try:
        pump(0.6)                       # 让它真烧起来，别把启动那一下当成占用
        t0 = time.time()
        procs, gpu, why = U.sample_window({"烧CPU的": proc.pid, "已经不在了": 4294967295},
                                          window=1.0)
        cost = time.time() - t0
        busy = procs.get("烧CPU的") or {}
        gone = procs.get("已经不在了") or {}
        out.append((
            "真测：烧 CPU 的子进程在 1 秒窗口里量到 ≥50%（摘掉 sleep 这条必翻红）",
            isinstance(busy.get("cpu"), (int, float)) and busy["cpu"] >= 50.0,
            f"cpu={busy.get('cpu')} rss={busy.get('rss_mb')}"))
        out.append(("真测：同一个窗口里内存也真量到了（>0 而不是没测到）",
                    isinstance(busy.get("rss_mb"), (int, float)) and busy["rss_mb"] > 0,
                    f"{busy.get('rss_mb')} MB"))
        out.append(("真测：整机占比 = 单核占比 ÷ 逻辑核数（同一份数，不另算一遍）",
                    bool(busy.get("cpu")) and busy.get("cores")
                    and abs(busy["cpu_share"] - busy["cpu"] / busy["cores"]) < 1e-6,
                    f"share={busy.get('cpu_share')} cores={busy.get('cores')}"))
        out.append(("已经不在了的 pid 记成状态文本，不写 0%（也不许让整轮采样跟着崩）",
                    busy.get("cpu") is not None and gone.get("cpu") is None
                    and bool(gone.get("status")), str(gone)[:120]))
        out.append(("返回顺序跟着调用方给的名字走（死掉那个不许插到前面）",
                    list(procs.keys()) == ["烧CPU的", "已经不在了"], str(list(procs.keys()))))
        out.append(("采样确实花了那个窗口那么久（不是假睡：耗时 ≥ 窗口的一半）",
                    cost >= 0.5, f"耗时 {cost:.2f}s"))
        rows, why2 = U.query_smi()
        out.append(("真跑 nvidia-smi：解析得出且原因串为空（取不到就不该有行）",
                    bool(rows) and why2 == "", f"{len(rows)} 行 / {why2[:60]}"))
        out.append(("GPU 读数带『整机』该有的那三件：利用率、显存、限流名单键都在",
                    bool(rows) and {"utilization.gpu", "memory.used",
                                    "clocks_throttle_reasons.active"} <= set(rows[0]),
                    str(sorted(rows[0].keys())[:4]) if rows else ""))
        if gpu:
            out.append(("真测：GPU 那侧拿到的限流名单里没有『空闲』",
                        "空闲" not in (gpu.get("limits") or []),
                        str(gpu.get("limits")) + " / 全翻 " + str(gpu.get("throttle"))))
        else:
            out.append(("真测：GPU 没给读数时必须留下原因（空原因=静默失踪）",
                        bool(why), repr(why)[:80]))
    finally:
        proc.kill()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
    return out


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    except BaseException:
        return False
    return False


# ---------------------------------------------------------------- 报表文本
SAMPLE_PROCS = {"内核": {"cpu": 42.0, "cpu_share": 2.1, "rss_mb": 310.5, "status": "ok"},
                "外壳": {"cpu": 8.0, "cpu_share": 0.4, "rss_mb": 260.25, "status": "ok"}}
# 「有一路没量到」的版本单独造：状态文本是**使用者**写给那一行的，报表只照抄。
# 之所以不把它塞进 SAMPLE_PROCS 再断言界面显示出来——那是测试先把答案写进 fixture、
# 再要求代码把这句话显示回来，等于自证，代码里那段补状态文本的分支一条也没走过。
SAMPLE_PROCS_DEAD = dict(SAMPLE_PROCS,
                         外壳={"cpu": None, "cpu_share": None, "rss_mb": None,
                               "status": "采样时这一路没起来（pid 是空的）"})
SAMPLE_GPU = {"name": "NVIDIA GeForce RTX 3070 Ti Laptop GPU", "util": 62.0,
              "mem_used": 3290.0, "mem_total": 8192.0, "temp": 71.0, "power": 88.4,
              "clock": 1410.0, "mask": 0x4 | 0x1, "throttle": ["空闲", "软件功率封顶"],
              "limits": ["软件功率封顶"], "gpu_count": 2}


def items_report() -> list[tuple[str, bool, str]]:
    both = U.build_report(SAMPLE_PROCS, SAMPLE_GPU,
                          {"estimated-vf-fps": 59.94, "frame-drop-count": 3}, "", 3.0)
    text = U.build_report(SAMPLE_PROCS_DEAD, SAMPLE_GPU,
                          {"estimated-vf-fps": 59.94, "frame-drop-count": 3}, "", 3.0)
    no_gpu = U.build_report(SAMPLE_PROCS_DEAD, {}, None, "没找到 nvidia-smi", 3.0)
    empty = U.build_report({}, {}, None, "没装 psutil", 3.0)
    return [
        ("报表写清采样窗口", f"采样窗口 {3:g} 秒" in text, text.splitlines()[0]),
        ("GPU 那一节标『整机』，不许冒充本播放器独占",
         "整机" in text and "不是本播放器独占" in text,
         [l for l in text.splitlines() if "GPU" in l][:1]),
        ("限流原因写进报表（软件功率封顶）", "软件功率封顶" in text, ""),
        ("『空闲』这类状态位单列，不混进限流那行",
         any(("状态位" in l and "空闲" in l) for l in text.splitlines())
         and not any(("限流/降频" in l and "空闲" in l) for l in text.splitlines()), ""),
        ("内核那行有 CPU、整机占比和内存三个数",
         "42.0%" in text and "2.1%" in text and "310 MB" in text, ""),
        ("两路都有数时两行都写数（不能只写内核那一行）",
         "8.0%" in both and "260 MB" in both and "没测到" not in both,
         [l for l in both.splitlines() if "外壳" in l][:1]),
        ("没量到的那一路写状态原文，不写 0", "采样时这一路没起来" in text
         and "内核：没测到" not in text, ""),
        ("多卡时报表自己说明取的是第一块", "查到 2 块卡" in text, ""),
        ("播放侧只列内核真给了的数（丢帧 3 帧在，没给的 bitrate 不出现）",
         "frame-drop-count" in text and "3" in text and "video-bitrate" not in text, ""),
        ("GPU 取不到：整节换成原因原文，不留空槽",
         "没测到" in no_gpu and "没找到 nvidia-smi" in no_gpu, ""),
        ("两头都没量到：报表照样成文，且一个数字都不许冒出来",
         empty.count("没测到") >= 2 and "0.0%" not in empty, ""),
    ]


# ---------------------------------------------------------------- 界面接线
class QuietPlayer(MpvPlayer):
    """真 QObject、真信号，就是不建进程也不发 IPC（同 tests/test_gif_wiring.py 的办法）。"""

    @property
    def running(self) -> bool:
        return False

    @property
    def mpv_pid(self) -> int | None:
        return None


def items_wiring() -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []
    cfg = cfgmod.Config()
    panel = SP.StatsPanel(QuietPlayer(cfg), cfg)
    # **必须 show**：offscreen 平台上父窗没显示时，子控件的 isVisible() 一律 False——
    # 那样"采样前整行藏着"这条就变成恒真判据（藏着的和填了数的都判 False），而
    # "投回界面后四行有数"又永远判红。show() 在 offscreen 下不会在用户桌面长窗口。
    panel.show()
    pump(0.05)
    keys = ("usage_mpv", "usage_shell", "usage_gpu", "usage_clock")
    out.append(("统计页有这四行，且采样前**整行藏着**（没量过就不画）",
                all(k in panel._rows for k in keys)
                and not any(panel._rows[k].isVisible() for k in keys),
                str({k: panel._rows[k].isVisible() for k in keys})))
    out.append(("按钮在（采样 + 复制），复制在未采样前禁用",
                hasattr(panel, "btn_usage") and hasattr(panel, "btn_copy")
                and not panel.btn_copy.isEnabled(), ""))
    # 直接喂一次结果（不走线程）：把"填行"这件事和"量数"分开判。
    # 外壳这一路**故意不给读数**、且 pid 给 None：那一行的文案必须由面板自己补出来，
    # 不能拿 fixture 里预先写好的句子回显——所以这里喂的 procs 只有内核一条。
    panel._usage_pids = {"内核": 1234, "外壳": None}
    panel._on_sample_done({"内核": dict(SAMPLE_PROCS["内核"])}, dict(SAMPLE_GPU), "", "")
    # value_text() 而不是 val.text()：后者是**按当前宽度裁过**的显示串（窄侧栏上会带省略号），
    # 这一节要判的是"这一行填进去的是哪几个数"，不是"屏幕露出了多少"。
    texts = {k: panel._rows[k].value_text() for k in keys}
    vis = {k: panel._rows[k].isVisible() for k in keys}
    out.append(("喂进结果后四行都可见", all(vis.values()), str(vis)))
    out.append(("内核那行写的是 usage 层那几个数，界面不另算一遍",
                "42.0%" in texts["usage_mpv"] and "2.1%" in texts["usage_mpv"]
                and "310 MB" in texts["usage_mpv"], texts["usage_mpv"]))
    out.append(("pid 是空的那一路写状态原文，不画 0%",
                "没起来" in texts["usage_shell"] and "0.0%" not in texts["usage_shell"],
                texts["usage_shell"]))
    out.append(("GPU 那行带『整机』口径（利用率+显存+温度）",
                "62.0%" in texts["usage_gpu"] and "3290" in texts["usage_gpu"]
                and "71" in texts["usage_gpu"], texts["usage_gpu"]))
    out.append(("时钟那行把限流写出来", "软件功率封顶" in texts["usage_clock"]
                and "1410 MHz" in texts["usage_clock"], texts["usage_clock"]))
    out.append(("复制用的报表已经拼好", "整机" in panel._report
                and "采样窗口" in panel._report, panel._report.splitlines()[0]))
    out.append(("采样完成后复制按钮可用", panel.btn_copy.isEnabled(), ""))
    # GPU 取不到的那一侧：四行里只藏 GPU 行，原因写进时钟行
    panel2 = SP.StatsPanel(QuietPlayer(cfg), cfg)
    panel2.show()
    pump(0.05)
    panel2._usage_pids = {"内核": None, "外壳": os.getpid()}
    panel2._on_sample_done({"外壳": dict(SAMPLE_PROCS["外壳"])}, {}, "没找到 nvidia-smi", "")
    out.append(("GPU 没读数：GPU 行藏起来，原因落在时钟那行（不留空槽也不写 0）",
                not panel2._rows["usage_gpu"].isVisible()
                and "没找到 nvidia-smi" in panel2._rows["usage_clock"].value_text(),
                panel2._rows["usage_clock"].value_text()))
    # 采样任务自己崩了（err 非空）：那时 pid 都好着，绝不能写成"这一路没起来"——
    # 把程序的故障说成"用户没放片子"，是本页要躲的第二类谎。
    paneE = SP.StatsPanel(QuietPlayer(cfg), cfg)
    paneE.show()
    pump(0.05)
    paneE._usage_pids = {"内核": 1234, "外壳": 4321}
    paneE._on_sample_done({}, {}, "", "ValueError: 窗口必须大于 0 秒")
    errt = {k: paneE._rows[k].value_text() for k in ("usage_mpv", "usage_shell")}
    out.append(("采样任务崩了：两行写『采样没跑成』，不许冒『pid 是空的』",
                all("采样没跑成" in t for t in errt.values())
                and not any("pid 是空的" in t for t in errt.values())
                and "ValueError" in errt["usage_mpv"], str(errt)))
    # 真走线程池：点按钮不许把 UI 线程睡住，回来要把行填上
    old_window = SP.USAGE_WINDOW_S
    SP.USAGE_WINDOW_S = 0.7
    try:
        panel3 = SP.StatsPanel(QuietPlayer(cfg), cfg)
        panel3.show()
        pump(0.05)
        t0 = time.time()
        panel3._sample_now()
        cost = time.time() - t0
        out.append(("点一下按钮**不睡 UI 线程**（返回耗时远小于采样窗口）",
                    cost < 0.25, f"返回用了 {cost * 1000:.0f}ms，窗口 {SP.USAGE_WINDOW_S}s"))
        out.append(("采样在途时按钮禁用（防止连点堆任务）",
                    not panel3.btn_usage.isEnabled(), ""))
        first = panel3._job
        panel3._sample_now()
        out.append(("在途时再点一次不会起第二个任务", panel3._job is first, ""))
        ok = pump(6.0, lambda: panel3._job is None)
        out.append(("线程池把结果投回界面，四行随之有数（没屏幕也投得回）",
                    ok and panel3._rows["usage_shell"].isVisible(),
                    "6 秒没回来" if not ok else panel3._rows["usage_shell"].value_text()))
        out.append(("回来之后按钮重新可用、文案变『再采』",
                    panel3.btn_usage.isEnabled() and "再采" in panel3.btn_usage.text(),
                    panel3.btn_usage.text()))
    finally:
        SP.USAGE_WINDOW_S = old_window
    return out


def items_crossfile() -> list[tuple[str, bool, str]]:
    src = open(UI_TEST, encoding="utf-8").read()
    seg = src.split("SYNTH_ROWS = (", 1)
    body = seg[1].split(")", 1)[0] if len(seg) > 1 else ""
    missing = [k for k in ("usage_mpv", "usage_shell", "usage_gpu", "usage_clock")
               if f'"{k}"' not in body]
    return [
        ("test_ui 的『行必须有数据源』白名单收下了这四行（否则真机验收会判成死行）",
         bool(body) and not missing, f"缺={missing}"),
        ("player 暴露 mpv_pid 且没起来时给 None（属性而不是 proc 本体）",
         hasattr(MpvPlayer, "mpv_pid") and isinstance(
             getattr(MpvPlayer, "mpv_pid"), property)
         and QuietPlayer(cfgmod.Config()).mpv_pid is None, ""),
    ]


# ---------------------------------------------------------------- 证伪
def _nl_text(path: str) -> tuple[str, str]:
    raw = open(path, encoding="utf-8", newline="").read()
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw, nl


def mutate(path: str, old: list[str], new: list[str], want: tuple[str, ...],
           collector) -> tuple[bool, str]:
    """把源码里那几行换成"常见错误写法"→ 本文件判据必须点名 want 里那条 → 立刻还原并核 md5。

    锚点用**行列表**而不是整段字符串：拼的时候按文件自己的换行符接，避免"锚点在 LF 文件里
    写 \\r\\n 所以永远匹配不上"这种自缚（本仓踩过一次）。出现 0 次或 >1 次都算锚点坏了，
    直接判这条证伪不作数——不许拿"没匹配上"当成"抓到"。

    改完盘上字节必须 `importlib.reload` 一次：模块早就 import 进来了，只改文件不重载的话，
    判据跑的还是内存里那份好代码，整轮证伪会"全都翻红不了"——那是证伪器自己坏了。
    还原之后同样要再 reload 一次，否则对照组跑的是坏代码。
    """
    import hashlib
    import importlib
    keep = open(path, "rb").read()
    before = hashlib.md5(keep).hexdigest()
    text, nl = _nl_text(path)
    old_s = nl.join(old)
    new_s = nl.join(new)
    n = text.count(old_s)
    if n != 1:
        return False, f"锚点在 {os.path.basename(path)} 里出现 {n} 次（应为 1 次），这条不作数"

    def _reload() -> None:
        importlib.reload(U)
        importlib.reload(SP)

    try:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text.replace(old_s, new_s))
        _reload()
        bad = [name for name, ok, _ in collector() if not ok]
        hit = [b for b in bad if any(w in b for w in want)]
        if not bad:
            return False, "改成坏写法后判据一条都没翻红（恒真）"
        if not hit:
            return False, "翻红了但没点到名：" + "；".join(bad)[:120]
        return True, f"点名 {len(hit)} 条：" + hit[0][:70]
    finally:
        open(path, "wb").write(keep)
        _reload()
        if hashlib.md5(open(path, "rb").read()).hexdigest() != before:
            print("       [!] 还原后 md5 变了——本轮不作数")


def collectors() -> list[tuple[str, str, list[str], list[str], tuple[str, ...], any]]:
    return [
        ("_num 把 N/A 顶成 0（把『没测到』说成『测到 0』）", USAGE_PY,
         ['    if not s or s.upper() in ("N/A", "[N/A]", "NA", "NOT SUPPORTED", "ERR"):',
          "        return None"],
         ['    if not s or s.upper() in ("N/A", "[N/A]", "NA", "NOT SUPPORTED", "ERR"):',
          "        return 0.0"],
         ("[N/A]",), items_parse),
        ("limit_reasons 不过滤状态位（『空闲』被写成降频原因）", USAGE_PY,
         ["    return [n for n in throttle_names(mask) if n not in _INFO_NAMES]"],
         ["    return throttle_names(mask)"],
         ("0x1", "空闲", "限流名单里没有"),
         lambda: items_throttle() + items_report() + items_wiring()),
        ("parse_smi 把表头当成一块卡（两道兜底一起摘；只摘一道不算坏写法，另一道还兜着）",
         USAGE_PY,
         ['    for line in (text or "").replace("\\r\\n", "\\n").split("\\n"):',
          '        line = line.strip()',
          '        if not line or line.lower().startswith("name,"):',
          '            continue',
          '        cols = [c.strip() for c in line.split(",")]',
          '        if len(cols) < len(SMI_FIELDS):',
          '            continue',
          '        if cols[0].lower() == "name":          # 带表头的那一行',
          '            continue'],
         ['    for line in (text or "").replace("\\r\\n", "\\n").split("\\n"):',
          '        line = line.strip()',
          '        if not line:',
          '            continue',
          '        cols = [c.strip() for c in line.split(",")]',
          '        if len(cols) < len(SMI_FIELDS):',
          '            continue'],
         ("表头",), items_parse),
        ("sample_window 不睡窗口（CPU% 变成两个同一时刻之差）", USAGE_PY,
         ["        if handles:", "            time.sleep(window)"],
         ["        if handles:", "            pass"],
         ("真测：烧 CPU",), items_cpu),
        ("报表不标『整机』（把 GPU 占用算到播放器头上）", USAGE_PY,
         ['    lines.append("— GPU（nvidia-smi，整机瞬时值，不是本播放器独占）")'],
         ['    lines.append("— GPU（nvidia-smi 读数）")'],
         ("整机",), items_report),
        ("界面把没量到的 GPU 行也画出来（写 0 或写空槽）", PANEL_PY,
         ['            rows["usage_gpu"].show_value(None)',
          '            rows["usage_clock"].show_value(',
          '                self._usage_why[:60] if self._usage_why else None)'],
         ['            rows["usage_gpu"].show_value("0.0% · 没读数")',
          '            rows["usage_clock"].show_value("0 MHz")'],
         ("GPU 没读数",), items_wiring),
        ("白名单没收下这四行（真机验收会把它们当死行）", UI_TEST,
         ['              "usage_mpv", "usage_shell", "usage_gpu", "usage_clock")'],
         ['              "usage_mpv", "usage_shell")'],
         ("白名单",), items_crossfile),
        ("四行建出来不藏（页面上摆出「内核  —」这种说不清是 0 还是没测到的空槽）", PANEL_PY,
         ["            self._rows[key].show_value(None)"],
         ["            pass"],
         ("采样前",), items_wiring),
        ("补状态文本不分原因（采样崩了也说成「用户没放片子」）", PANEL_PY,
         ['            if err:', '                status = "采样没跑成：" + err[:48]',
          '            elif pid:', '                status = "这一路没量到（进程侧没回报，见时钟行）"',
          '            else:', '                status = "采样时这一路没起来（pid 是空的）"'],
         ['            status = "采样时这一路没起来（pid 是空的）"'],
         ("采样没跑成",), items_wiring),
    ]


def falsify() -> int:
    print("\n== 证伪：把实现换成常见错误写法，对应判据必须点名翻红 ==")
    miss = 0
    for name, path, old, new, want, coll in collectors():
        hit, why = mutate(path, old, new, want, coll)
        miss += 0 if hit else 1
        say("CAUGHT" if hit else "MISSED", name, why)
    print("\n== 对照组：全部还原后验收必须重新全绿 ==")
    ok = True
    for label, coll in (("解析", items_parse), ("降频位", items_throttle),
                        ("报表", items_report), ("接线", items_wiring),
                        ("跨文件", items_crossfile)):
        bad = [n for n, k, _ in coll() if not k]
        say("PASS" if not bad else "FAIL", f"还原后 {label} 一组重新全绿",
            "仍翻红：" + "；".join(bad)[:120])
        ok = ok and not bad
    results.append(("全部还原后判据重新全绿（对照组）", ok, ""))
    print(f"\n证伪 {len(collectors())} 条，漏网 {miss} 条")
    return 1 if miss or not ok else 0


# ---------------------------------------------------------------- 入口
def env_gate() -> int:
    """环境不满足就单独报码：这不是产品失败，别拿它判红。"""
    if U.psutil_unavailable():
        print("[ENV] " + U.psutil_unavailable() + " —— 这份验收要靠它证明『真量到了』")
        return 2
    if not U.nvidia_smi_path():
        print("[ENV] 这台机器没有 nvidia-smi：真跑那两条判据没有对照，退 2")
        return 2
    if not os.path.exists(UI_TEST):
        print("[ENV] 找不到 tests/test_ui.py，跨文件那条判据无从对账")
        return 2
    return 0


def main() -> int:
    code = env_gate()
    if code:
        return code
    print(f"== 资源实测验收（{os.path.basename(USAGE_PY)} + 统计页四行）==")
    say("NOTE", "nvidia-smi 取的是", str(U.nvidia_smi_path()))
    for label, coll in (("csv 解析", items_parse), ("降频位", items_throttle),
                        ("进程量测（真测）", items_cpu), ("报表文本", items_report),
                        ("界面接线", items_wiring), ("跨文件对账", items_crossfile)):
        guard(label, coll)
    fails = [name for ok, name, _ in results if not ok]
    print(f"\n共 {len(results)} 项，失败 {len(fails)} 项"
          + ("：" + "；".join(fails)[:200] if fails else ""))
    print("口径：CPU/内存按 pid 量、GPU 是 nvidia-smi 的**整机**读数；"
          "任何一项量不到都写『没测到』并留原因，一个数字都不许靠 0 顶包。")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(falsify() if "--falsify" in sys.argv else main())
