"""资源占用实测：外壳与内核进程的 CPU/内存，加上 nvidia-smi 的整机 GPU 读数。

这一层为什么单独存在，而不是把统计页那几行数字直接算完：

1. **两种"没有数"必须分开**。内核没回报某个属性、psutil 没装、nvidia-smi 不在 PATH、
   驱动回一句 `[N/A]`——这四种情况对用户是四种不同的结论。把它们统一画成 0，
   就等于宣称"这一项测到了，是 0"，而统计页的既有纪律是"没测到就整行不画"
   （见 app/ui/stats_panel.py 的头注释）。所以本模块一律返回 `None` + 一句原因，
   由调用方决定画不画，绝不返回 0 顶包。
2. **nvidia-smi 的读数不是本播放器的读数**。它给的是**整块 GPU 现在有多少百分比在忙**，
   桌面上任何一个浏览器都在往里贡献。所以报表里这一项永远带着"整机"两个字，
   而进程级的数只有 CPU/内存是真的（psutil 按 pid 取）。写这句不是谦虚：
   把"GPU 62%"标成播放器的占用，用户就会以为换个播放器能省一半显存。
3. **采样要占时间窗口**，所以它不能跑在 UI 线程上；本模块只负责"给我一个窗口我给你一组数"，
   线程归线程（统计页用 QThreadPool 起，见 `sample_window`）。

本机实测（2026-10-08，RTX 3070 Ti Laptop + 472.xx 驱动，`--format=csv,noheader,nounits`）：
    NVIDIA GeForce RTX 3070 Ti Laptop GPU, 5, 3295, 8192, 53, 32.87, 1485, 0x0000000000000001
分隔符是**逗号加空格**（不是逗号），行尾是 CRLF；不带 `nounits` 时值里带单位
（`6 %`、`32.90 W`），而 `power.limit`、`fan.speed` 在这台笔记本上是 `[N/A]`。
解析器两种都吃得下，并且把 `[N/A]`、`N/A`、空串一律判成"没测到"。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any

# 查询字段：顺序即 parse_smi() 的列顺序。多要一个 name 是为了把"哪块卡"写进报表——
# 双显卡机型上取第一行不等于取独显。
SMI_FIELDS: tuple[str, ...] = (
    "name", "utilization.gpu", "memory.used", "memory.total",
    "temperature.gpu", "power.draw", "clocks.sm", "clocks_throttle_reasons.active",
)
NUMERIC_FIELDS: tuple[str, ...] = SMI_FIELDS[1:]

# NVML 的 throttle reason 位定义（nvidia-ml.h 里那组）。这张表是**解释**用的：
# 读到一个没见过的位，宁可写"未知位 0x…"也不能默默丢掉——丢掉的位恰好可能是
# "被功耗墙压住了"，而那正是笔记本用户最想看见的一条。
THROTTLE_BITS: tuple[tuple[int, str], ...] = (
    (0x0001, "空闲"),
    (0x0002, "应用时钟上限"),
    (0x0004, "软件功率封顶"),
    (0x0008, "硬件降频"),
    (0x0010, "同步时钟加成"),
    (0x0020, "未知原因"),
    (0x0040, "软件热降频"),
    (0x0080, "硬件热降频"),
    (0x0100, "功率刹车（外部限流）"),
)

# 这三位不是"卡被限住了"，而是状态的说明：空闲＝压根没活干，应用时钟上限＝钟被
# 驱动的应用档钉住（超频软件常见），同步加成＝跟别的设备同步钟。把它们混进
# "降频原因"里，用户会以为机器过热，而真实原因只是"这会儿没人用 GPU"。
INFO_BITS: frozenset[int] = frozenset({0x0001, 0x0002, 0x0010})

# 名字集合形式：limit_reasons() 要按名字滤，按位滤会把"未知位"一起滤掉（它不在表里）。
_INFO_NAMES: frozenset[str] = frozenset(name for bit, name in THROTTLE_BITS
                                        if bit in INFO_BITS)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- nvidia-smi
def nvidia_smi_path() -> str | None:
    """先 PATH，再 System32 的固定位置。

    固定位置那条不是多余的：pythonw 启动（右键菜单那条路）继承的 PATH 有时不含
    System32，而 nvidia-smi.exe 就住在那儿。
    """
    found = shutil.which("nvidia-smi")
    if found:
        return found
    fixed = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32", "nvidia-smi.exe")
    return fixed if os.path.exists(fixed) else None


def _num(raw: str) -> float | None:
    """把 '5'、'32.87'、'6 %'、'1485 MHz' 收成数字；'N/A'、'[N/A]'、'' 一律 None。

    注意这里**不**用 `float(re.sub(r"[^0-9.]", "", s))` 那种"剥掉非数字"的写法：
    `[N/A]` 会被剥成 `A`→再抛异常，看着像处理过了，其实全靠异常兜底。这里显式取
    "第一个数字 token"，边界写得住：`abc`→None、`-3.5 W`→-3.5、`1,485`→1（千分位
    在 `parse_smi` 的逗号分列阶段就已经被拆成两列了，走不到这里；真要吃得下分组格式
    得先定地区约定，不在这里猜）。
    """
    s = (raw or "").strip()
    if not s or s.upper() in ("N/A", "[N/A]", "NA", "NOT SUPPORTED", "ERR"):
        return None
    start = -1
    for i, ch in enumerate(s):
        if ch.isdigit() or (ch in "+-" and i + 1 < len(s) and s[i + 1].isdigit()):
            start = i
            break
    if start < 0:
        return None
    j = start + 1 if s[start] in "+-" else start
    while j < len(s) and (s[j].isdigit() or s[j] == "."):
        j += 1
    try:
        return float(s[start:j])
    except ValueError:
        return None


def parse_smi(text: str) -> list[dict[str, Any]]:
    """把 nvidia-smi 的 csv 输出解析成 [{field: value}]，一块卡一条。

    坏输入返回空列表（**不**抛异常）：调用方把它当"没测到"，而不是当产品坏了。
    表头行会被跳过——`noheader` 是我们自己给的，但用户手工贴来的文本不一定带。
    """
    rows: list[dict[str, Any]] = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or line.lower().startswith("name,"):
            continue
        cols = [c.strip() for c in line.split(",")]
        if len(cols) < len(SMI_FIELDS):
            continue
        if cols[0].lower() == "name":          # 带表头的那一行
            continue
        row: dict[str, Any] = {}
        for i, field in enumerate(SMI_FIELDS):
            raw = cols[i]
            if field == "name":
                row[field] = raw
            elif field == "clocks_throttle_reasons.active":
                row[field] = raw
                row["_throttle"] = _mask(raw)
            else:
                row[field] = _num(raw)
        rows.append(row)
    return rows


def _mask(raw: str) -> int | None:
    """'0x0000000000000004' → 4；解不开就 None（不猜 0：0 的意思是"没降频"）。"""
    s = (raw or "").strip()
    if not s or s.upper() in ("N/A", "[N/A]"):
        return None
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s, 0)
    except ValueError:
        return None


def throttle_names(mask: int | None) -> list[str]:
    """降频位 → 人话（全部位都翻，含 0x1 空闲那三位状态位）。mask 为 None 返回空列表。"""
    if mask is None:
        return []
    if mask == 0:
        return []
    out = [name for bit, name in THROTTLE_BITS if mask & bit]
    known = 0
    for bit, _ in THROTTLE_BITS:
        known |= bit
    rest = mask & ~known
    if rest:
        out.append(f"未知位 0x{rest:x}")
    return out


def limit_reasons(mask: int | None) -> list[str]:
    """只留"真的被限住了"那几位：把 空闲/应用时钟上限/同步加成 当状态而不是病因。

    这一条是给报表那行"降频原因"用的：0x1 是那台笔记本最常见的读数（GPU 闲着），
    照原样写进"降频原因"会让人以为机器过热。未知位**永远留下**——它可能正是
    这张表没覆盖到的那条限流。
    """
    return [n for n in throttle_names(mask) if n not in _INFO_NAMES]


def query_smi(exe: str | None = None, timeout: float = 2.5) -> tuple[list[dict[str, Any]], str]:
    """真跑一次 nvidia-smi。返回 (行, 原因)，行空时原因非空。

    超时/找不到/非零退出**都不许抛**：统计页把 GPU 那几行藏起来就行，
    为一个测不到的读数把整页崩掉是本末倒置。
    """
    exe = exe if exe is not None else nvidia_smi_path()
    if not exe or not os.path.exists(exe):
        return [], "没找到 nvidia-smi（这台机器没装 N 卡驱动，或它不在 PATH 里）"
    cmd = [exe, "--query-gpu=" + ",".join(SMI_FIELDS),
           "--format=csv,noheader,nounits"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return [], f"nvidia-smi {timeout}s 没回话（驱动或被占用，不是播放器卡住）"
    except OSError as exc:
        return [], f"nvidia-smi 起不来：{exc}"
    text = _decode(r.stdout or b"")
    if r.returncode != 0:
        return [], (f"nvidia-smi 退出码 {r.returncode}："
                    + (_decode(r.stderr or b"").strip() or text.strip())[:120])
    rows = parse_smi(text)
    if not rows:
        return [], f"nvidia-smi 回了话但解析不出行：{text.strip()[:80]!r}"
    return rows, ""


def _decode(raw: bytes) -> str:
    """nvidia-smi 的输出按本机 ANSI（GBK）解，解不动再退回 UTF-8 替换。

    这台机器 ACP=936；真按 UTF-8 硬解会把卡名里的怪字符变成替换符，
    报表就写出没人看得懂的一行。
    """
    for enc in ("gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", "replace")


# ---------------------------------------------------------------- 进程 CPU/内存
def psutil_unavailable() -> str:
    """返回原因串（空串＝可用）。单独成一个函数，是为了让验收脚本能直接判它。"""
    try:
        import psutil                                    # noqa: F401
    except ImportError as exc:
        return f"没装 psutil：CPU/内存这一项没测（不是 0，是没测）——{exc}"
    return ""


def cpu_share(pct_one_core: float, logical_cores: int) -> float:
    """单核百分比 → 整机百分比。逻辑核数拿不到就原样返回，别除出个 Infinity。"""
    if logical_cores <= 0:
        return pct_one_core
    return pct_one_core / float(logical_cores)


def sample_window(pids: dict[str, int | None], window: float = 1.0
                  ) -> tuple[dict[str, dict[str, Any]], dict[str, Any], str]:
    """在 `window` 秒的窗口里量各进程的 CPU/内存，顺带取一次 GPU 读数。

    会阻塞 `window` 秒——只能跑在工作线程里。返回 (进程读数, GPU 读数, 原因)：
      - 进程读数：{名字: {"cpu": 单核%, "cpu_share": 整机%, "rss_mb": MB, "status": str}}
      - GPU 读数：{"util": %|"None", "mem_used"/"mem_total": MiB, "temp": C,
                   "power": W, "clock": MHz, "throttle": [str], "name": str,
                   "gpu_count": int}，取不到就是空 dict
      - 原因非空表示 GPU 那一侧没测到（进程侧可能仍然有数）。
    """
    wanted = {k: v for k, v in pids.items() if v}
    gpu_rows, why = ({}, "")
    if window <= 0:
        raise ValueError("窗口必须大于 0 秒：CPU 百分比是**两个时刻之差**，单点取不出来")
    reason = psutil_unavailable()
    procs: dict[str, dict[str, Any]] = {}
    handles: dict[str, Any] = {}
    if not reason:
        import psutil
        for name, pid in wanted.items():
            try:
                h = psutil.Process(int(pid))
                h.cpu_percent(None)          # 第一下只是开始计数，返回值必须丢掉
                handles[name] = h
            except Exception as exc:         # NoSuchProcess / AccessDenied
                procs[name] = {"cpu": None, "cpu_share": None, "rss_mb": None,
                               "status": f"进程不在了（{type(exc).__name__}）"}
        if handles:
            time.sleep(window)
        cores = 0
        try:
            cores = int(psutil.cpu_count(logical=True) or 0)
        except Exception:
            cores = 0
        for name, h in handles.items():
            try:
                pct = float(h.cpu_percent(None))
                rss = int(h.memory_info().rss)
            except Exception as exc:
                procs[name] = {"cpu": None, "cpu_share": None, "rss_mb": None,
                               "status": f"读不到（{type(exc).__name__}）"}
                continue
            procs[name] = {"cpu": pct, "cpu_share": cpu_share(pct, cores),
                           "rss_mb": rss / 1048576.0, "status": "ok",
                           "cores": cores}
    elif wanted:
        for name in wanted:
            procs[name] = {"cpu": None, "cpu_share": None, "rss_mb": None,
                           "status": "没装 psutil"}
    # 按调用方给的名字顺序出：死掉的进程是在起句柄那一轮就写进去的，活的要等窗口
    # 结束——不重排的话"内核"会跳到"外壳"前面，报表每轮顺序都在变。
    procs = {name: procs[name] for name in wanted if name in procs}
    rows, gpu_why = query_smi()
    if rows:
        first = rows[0]
        gpu_rows = {"name": first.get("name") or "",
                    "util": first.get("utilization.gpu"),
                    "mem_used": first.get("memory.used"),
                    "mem_total": first.get("memory.total"),
                    "temp": first.get("temperature.gpu"),
                    "power": first.get("power.draw"),
                    "clock": first.get("clocks.sm"),
                    "mask": first.get("_throttle"),
                    "throttle": throttle_names(first.get("_throttle")),
                    "limits": limit_reasons(first.get("_throttle")),
                    "gpu_count": len(rows)}
    else:
        why = gpu_why
    return procs, gpu_rows, why


# ---------------------------------------------------------------- 报表
# 这三个格式化函数是公开的（`pct_text` / `num_text` / `mb_text`）：统计页的行文案和
# 复制出去的文本报表必须用**同一套**写法，否则同一份读数在页面上写 "没测到"、
# 粘出去写 "—"，用户会以为程序在读数字的中间变卦。
def mb_text(v: Any) -> str:
    return f"{v:.0f} MB" if isinstance(v, (int, float)) else "没测到"


def pct_text(v: Any) -> str:
    return f"{v:.1f}%" if isinstance(v, (int, float)) else "没测到"


def num_text(v: Any, digits: int = 0) -> str:
    return f"{v:.{digits}f}" if isinstance(v, (int, float)) else "没测到"


def _val(v: Any, unit: str, digits: int = 0) -> str:
    return f"{v:.{digits}f}{unit}" if isinstance(v, (int, float)) else "没测到"


def build_report(procs: dict[str, dict[str, Any]], gpu: dict[str, Any],
                 frame: dict[str, Any] | None = None, gpu_why: str = "",
                 window: float = 1.0) -> str:
    """把两组读数拼成一段可以直接粘给别人看的纯文本。

    每一行都写清"这是谁/怎么量的"：CPU 是窗口内的平均值，GPU 是**整机**瞬时值，
    取不到的项写"没测到"并带上原因——一个数字都不许靠 0 顶包。
    """
    lines = [f"资源占用报表（采样窗口 {window:g} 秒）"]
    lines.append("— 进程（psutil，按 pid 取，窗口内平均）")
    if procs:
        for name, d in procs.items():
            if d.get("cpu") is None:
                lines.append(f"  {name}：{d.get('status') or '没测到'}")
                continue
            lines.append(f"  {name}：CPU {pct_text(d['cpu'])}（单核基准）"
                         f"· 约占整机 {pct_text(d.get('cpu_share'))} · 内存 {mb_text(d.get('rss_mb'))}")
    else:
        lines.append("  没测到（内核还没起来，或 psutil 不可用：" +
                     (psutil_unavailable() or "无进程可查") + "）")
    lines.append("— GPU（nvidia-smi，整机瞬时值，不是本播放器独占）")
    if gpu:
        limits = gpu.get("limits") or []
        state = [n for n in (gpu.get("throttle") or []) if n not in limits]
        lines.append(f"  {gpu.get('name') or '未具名卡'}："
                     f"利用率 {pct_text(gpu.get('util'))} · 显存 {_val(gpu.get('mem_used'), ' MiB')}"
                     f"/{_val(gpu.get('mem_total'), ' MiB')}"
                     f" · 温度 {_val(gpu.get('temp'), '°C')}"
                     f" · 功耗 {_val(gpu.get('power'), ' W', 1)}"
                     f" · SM 时钟 {_val(gpu.get('clock'), ' MHz')}")
        if int(gpu.get("gpu_count", 1) or 1) > 1:
            lines.append(f"  （查到 {gpu['gpu_count']} 块卡，上面取的是第一块）")
        lines.append("  限流/降频：" + ("、".join(limits) if limits else "无"))
        if state:
            lines.append("  驱动报的状态位：" + "、".join(state))
    else:
        lines.append("  没测到：" + (gpu_why or "nvidia-smi 没给读数"))
    if frame:
        lines.append("— 内核回报的播放侧计数（只列它真给了的）")
        for key in ("estimated-vf-fps", "frame-drop-count", "decoder-frame-drop-count",
                    "demuxer-cache-duration", "avsync"):
            v = frame.get(key)
            if v is None:
                continue
            if isinstance(v, float):
                lines.append(f"  {key}：{v:.3f}")
            else:
                lines.append(f"  {key}：{v}")
    return "\n".join(lines)
