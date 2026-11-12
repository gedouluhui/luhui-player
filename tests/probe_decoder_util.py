"""#86 的第二段探测：**判据用的每一个读数，先证明它有鉴别力**，再写进 e2e。

要判的是"硬解到底在不在跑"。候选三把尺：
  ① 内核自报 `hwdec-current`（产品路径已实测会回 'd3d11va'）；
  ② 请求值 `hwdec`／`options/hwdec`——**它回的是命令行里那个串**，`--hwdec=bogus`
     照样回 'bogus'（perf.py 记的静默降级就是它），所以这把尺注定零鉴别力；
  ③ `nvidia-smi utilization.decoder`——驱动可能压根不给这个字段（恒 0 或 N/A），
     那就不能拿它当判据，只能当佐证。

所以这趟不带产品，裸起内核（属性与日志是同一份内核给的），**同一支片子跑两档**：
`--hwdec=nvdec` 与 `--hwdec=no`，每档在一个窗口里反复问读数和 nvidia-smi。
判"两档有差别"之前先跑同档两趟当对照组——不然差别可能只是窗口抖。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")
SAMPLE = os.path.join(HERE, "samples", "uhd", "hevc-4k10.mp4")

from player_harness import MpvIpc, ask, kill_pids, pipe_of, tell  # noqa: E402
from app.engine import usage  # noqa: E402

PROPS = ["hwdec-current", "hwdec", "options/hwdec", "current-gpu-context",
         "current-vo", "video-format", "width", "height", "estimated-vf-fps",
         "container-fps", "frame-drop-count", "decoder-frame-drop-count",
         "time-pos", "duration", "idle-active", "pause", "avsync"]


def smi_decoder() -> tuple[float | None, str]:
    """直接问 nvidia-smi 要解码器占用（**不走 usage.SMI_FIELDS**：那张表里没有这一项，
    借它的字段名去查会拿到错位的一列）。取不到就回 None＋原因，不许拿 0 冒充"取到 0"。"""
    exe = usage.nvidia_smi_path()
    if not exe:
        return None, "找不到 nvidia-smi"
    import subprocess as sp
    r = sp.run([exe, "--query-gpu=utilization.gpu,utilization.decoder,memory.used",
                "--format=csv,noheader,nounits"], capture_output=True, timeout=5,
               creationflags=getattr(sp, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        return None, f"rc={r.returncode} {r.stderr.decode('gbk', 'ignore')[:120]}"
    txt = usage._decode(r.stdout)
    cols = [c.strip() for c in txt.strip().splitlines()[0].split(",")]
    if len(cols) < 2:
        return None, f"读数形状不认识：{txt.strip()[:120]}"
    dec = usage._num(cols[1])
    if dec is None:
        return None, f"decoder 字段不支持（原样 {cols[1]!r}）"
    return float(dec), ""


def one_run(hwdec: str, window: float = 8.0) -> dict:
    """裸起内核播 4K，在 window 秒里按秒取一组读数。"""
    pipe = rf"\\.\pipe\probe-gpu-{int(time.time())}-{hwdec.replace('=', '-')}"
    cmd = [MPV, SAMPLE, f"--hwdec={hwdec}", "--vo=gpu", "--gpu-api=d3d11",
           "--input-ipc-server=" + pipe, "--no-config", "--terminal=no",
           "--force-window=no", "--idle=no", "--keep-open=yes", "--mute=yes",
           "--volume=0", "--really-quiet"]
    p = subprocess.Popen(cmd, cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print(f"  [run] hwdec={hwdec} pid={p.pid}")
    got, why = ask(pipe, PROPS, wait=15.0)
    out: dict = {"hwdec_arg": hwdec, "pid": p.pid, "first": got, "why": why,
                 "dec_samples": [], "gpu_samples": [], "cpu": {}, "vf_fps": []}
    if not got:
        print(f"  [run] 问不到属性：{why}")
        kill_pids([p.pid])
        out["dead"] = True
        return out
    tell(pipe, ["set_property", "pause", False])
    t0 = time.time()
    while time.time() < t0 + window:
        g, _ = ask(pipe, ["hwdec-current", "estimated-vf-fps", "frame-drop-count",
                          "time-pos", "avsync"], wait=2.0)
        d, dg = smi_decoder()
        out["dec_samples"].append(d)
        out["gpu_samples"].append(g and g.get("estimated-vf-fps"))
        if d is not None:
            out["dec_max"] = max([x for x in out["dec_samples"] if x is not None])
        time.sleep(0.5)
    # CPU：窗口末尾再量 3 秒（psutil 要 priming，两次取差）
    from player_harness import cpu_window
    cpu, cwhy = cpu_window({"mpv": p.pid}, 3.0)
    out["cpu"] = cpu
    out["cpu_why"] = cwhy
    last, _ = ask(pipe, ["hwdec-current", "time-pos", "duration", "frame-drop-count",
                         "decoder-frame-drop-count", "estimated-vf-fps", "avsync",
                         "current-gpu-context", "video-format", "width", "height"],
                  wait=3.0)
    out["last"] = last
    tell(pipe, ["quit"])
    time.sleep(1.5)
    kill_pids([p.pid])
    return out


def show(tag: str, r: dict) -> None:
    f, l = r.get("first") or {}, r.get("last") or {}
    ds = [x for x in r["dec_samples"] if x is not None]
    print(f"  <<{tag}>> hwdec-current={f.get('hwdec-current')!r} 请求值={f.get('hwdec')!r} "
          f"context={f.get('current-gpu-context')!r} {f.get('width')}x{f.get('height')} "
          f"{f.get('video-format')}")
    print(f"       vf-fps 末={l.get('estimated-vf-fps')} 掉帧={l.get('frame-drop-count')} "
          f"解码器掉帧={l.get('decoder-frame-drop-count')} avsync={l.get('avsync')} "
          f"进度={l.get('time-pos')}/{l.get('duration')}")
    print(f"       nvidia-smi decoder：{ds if ds else '一条也没取到'}  cpu={r['cpu']} "
          f"{r.get('cpu_why', '')}")


def run() -> int:
    if MpvIpc is None or not os.path.exists(SAMPLE):
        print(f"[probe] 依赖不齐或没有 4K 样片（MpvIpc={MpvIpc is not None} "
              f"sample={os.path.exists(SAMPLE)}）")
        return 2
    print(f"[probe] 样片={SAMPLE}")
    res = {}
    for tag, arg in (("硬解nvdec", "nvdec"), ("软解", "no"), ("硬解第二次(对照)", "nvdec")):
        res[tag] = one_run(arg)
        show(tag, res[tag])
    a, b, c = res["硬解nvdec"], res["软解"], res["硬解第二次(对照)"]
    da = [x for x in a["dec_samples"] if x is not None]
    db = [x for x in b["dec_samples"] if x is not None]
    print("\n[probe] —— 鉴别力结论 ——")
    print(f"  尺①hwdec-current：硬解={a['first'].get('hwdec-current')!r} vs "
          f"软解={b['first'].get('hwdec-current')!r} → "
          f"{'有鉴别力' if a['first'].get('hwdec-current') != b['first'].get('hwdec-current') else '无鉴别力！'}")
    print(f"  尺②请求值 hwdec：硬解={a['first'].get('hwdec')!r} vs 软解={b['first'].get('hwdec')!r} "
          f"（这俩**必然**不同，因为它是命令行回显；真正的陷阱是 bogus 也照样回显）")
    print(f"  尺③decoder 占用：硬解峰值={max(da) if da else None} 软解峰值="
          f"{max(db) if db else None} → "
          f"{'有鉴别力' if da and db and max(da) > max(db) else '不能当判据（当佐证或不采）'}")
    ca, cb = a["cpu"].get("mpv"), b["cpu"].get("mpv")
    print(f"  尺④mpv CPU：硬解={ca} 软解={cb} 复测硬解={c['cpu'].get('mpv')}"
          f" → 档间差={'有' if ca is not None and cb is not None and cb > ca else '无/未测'}"
          f"，同档抖动={'看复测'}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
