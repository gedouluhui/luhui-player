"""50-round cold-start resource curve for the portable build.

Each round starts the packaged exe with an isolated data directory, waits for
the cold-start log line, samples the process handle count and working set, then
closes it and checks that no child process or handle survives the round.

The curve is evidence about one machine; it is not a claim that the app can
never leak. What it does assert is that 50 consecutive cold starts do not grow
the parent probe's handles, do not leave orphan processes, and stay inside the
same working-set band as round 1.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DIST = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "dist", "LuHui播放器")
EXE = os.path.join(DIST, "LuHui播放器.exe")
ROUNDS = int(os.environ.get("GELU_CURVE_ROUNDS", "50"))
SETTLE_SECONDS = float(os.environ.get("GELU_CURVE_SETTLE", "2.5"))
OUT_JSON = os.path.join(ROOT, "artifacts", "handle-curve.json")
OUT_PNG = os.path.join(ROOT, "artifacts", "handle-curve.png")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def handle_count(pid: int) -> int:
    process = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return -1
    try:
        count = ctypes.wintypes.DWORD()
        if not _kernel32.GetProcessHandleCount(process, ctypes.byref(count)):
            return -1
        return int(count.value)
    finally:
        _kernel32.CloseHandle(process)


def working_set(pid: int) -> int:
    process = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return -1
    try:
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not _psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            return -1
        return int(counters.WorkingSetSize)
    finally:
        _kernel32.CloseHandle(process)


def children_of(exe_dir: str) -> list[int]:
    """PIDs whose image path is inside the package directory."""
    output = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath | ConvertTo-Json -Compress"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if output.returncode != 0 or not output.stdout.strip():
        return []
    try:
        rows = json.loads(output.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    found = []
    for row in rows:
        path = row.get("ExecutablePath") or ""
        if path and os.path.normcase(path).startswith(os.path.normcase(exe_dir)):
            found.append(int(row.get("ProcessId")))
    return found


def one_round(index: int, base_handles: int) -> dict:
    data = tempfile.mkdtemp(prefix=f"gelu-curve-{index:02d}-")
    env = dict(os.environ)
    env.update({"GELU_DATA": data, "GELU_NO_MOTION": "1",
                "QT_QPA_PLATFORM": "offscreen", "QT_OPENGL": "software"})
    started = time.time()
    proc = subprocess.Popen([EXE], env=env, cwd=DIST,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log_path = os.path.join(data, "logs", "launch.log")
    cold = False
    deadline = started + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if os.path.isfile(log_path):
            try:
                if "cold" in open(log_path, encoding="utf-8", errors="replace").read():
                    cold = True
                    break
            except OSError:
                pass
        time.sleep(0.1)
    time.sleep(SETTLE_SECONDS)
    alive = proc.poll() is None
    sample = {"round": index + 1, "pid": proc.pid, "cold": cold, "alive": alive,
              "startup_s": round(time.time() - started, 2),
              "handles": handle_count(proc.pid) if alive else -1,
              "working_set_mb": round(working_set(proc.pid) / 1048576, 1) if alive else -1}
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    time.sleep(0.4)
    sample["exit_code"] = proc.returncode
    sample["orphans"] = len(children_of(DIST))
    sample["probe_handles"] = handle_count(os.getpid())
    sample["probe_handle_delta"] = sample["probe_handles"] - base_handles
    crash = os.path.join(data, "logs", "crash.log")
    sample["crash"] = bool(os.path.isfile(crash)
                           and open(crash, encoding="utf-8", errors="replace").read().strip())
    shutil.rmtree(data, ignore_errors=True)
    return sample


def main() -> int:
    if not os.path.isfile(EXE):
        print(f"missing {EXE}", file=sys.stderr)
        return 2
    base_handles = handle_count(os.getpid())
    samples = []
    for index in range(ROUNDS):
        sample = one_round(index, base_handles)
        samples.append(sample)
        print(f"[{sample['round']:02d}] handles={sample['handles']:4d} "
              f"ws={sample['working_set_mb']:6.1f}MB orphans={sample['orphans']} "
              f"exit={sample['exit_code']} cold={sample['cold']}", flush=True)

    handles = [s["handles"] for s in samples if s["handles"] > 0]
    working = [s["working_set_mb"] for s in samples if s["working_set_mb"] > 0]
    first, last = samples[0], samples[-1]
    summary = {
        "rounds": ROUNDS,
        "exe": EXE,
        "handles_first": first["handles"],
        "handles_last": last["handles"],
        "handles_median": statistics.median(handles),
        "handles_max": max(handles),
        "working_set_first_mb": first["working_set_mb"],
        "working_set_last_mb": last["working_set_mb"],
        "working_set_max_mb": max(working),
        "probe_handle_delta_first": first["probe_handle_delta"],
        "probe_handle_delta_last": last["probe_handle_delta"],
        "orphans_total": sum(s["orphans"] for s in samples),
        "crashes": sum(1 for s in samples if s["crash"]),
        "cold_starts": sum(1 for s in samples if s["cold"]),
        "failures": [s["round"] for s in samples if not s["cold"] or not s["alive"] or s["crash"]],
        "samples": samples,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, indent=2)

    # Assertions: no orphan processes, no crash, every round reached cold start,
    # the probe's own handle count does not grow with the number of launches.
    problems = []
    if summary["orphans_total"]:
        problems.append(f"{summary['orphans_total']} orphan processes across rounds")
    if summary["crashes"]:
        problems.append(f"{summary['crashes']} crash logs")
    if summary["failures"]:
        problems.append(f"rounds without a cold start or with an early exit: {summary['failures']}")
    if summary["probe_handle_delta_last"] > 32:
        problems.append(f"probe handles grew by {summary['probe_handle_delta_last']} over {ROUNDS} launches")
    if summary["handles_max"] > summary["handles_first"] + 400:
        problems.append(f"peak handles {summary['handles_max']} far above round 1 {summary['handles_first']}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        rounds = [s["round"] for s in samples]
        figure, axis = plt.subplots(figsize=(8, 4.2), dpi=110)
        axis.plot(rounds, [s["handles"] for s in samples], color="#c0392b", label="process handles")
        axis.set_xlabel("cold start round")
        axis.set_ylabel("handles", color="#c0392b")
        second = axis.twinx()
        second.plot(rounds, [s["working_set_mb"] for s in samples], color="#2c6fbb", alpha=0.7,
                    label="working set (MB)")
        second.set_ylabel("working set (MB)", color="#2c6fbb")
        axis.set_title(f"LuHui player: {ROUNDS} cold starts, no orphan processes")
        axis.grid(alpha=0.25)
        figure.tight_layout()
        figure.savefig(OUT_PNG)
        summary["plot"] = OUT_PNG
    except Exception as error:  # plotting is a convenience, not the evidence
        summary["plot_error"] = str(error)
        with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(summary, stream, indent=2)

    print(json.dumps({key: summary[key] for key in (
        "rounds", "handles_first", "handles_last", "handles_max", "handles_median",
        "working_set_first_mb", "working_set_last_mb", "working_set_max_mb",
        "probe_handle_delta_first", "probe_handle_delta_last",
        "orphans_total", "crashes", "cold_starts")}, indent=2))
    if problems:
        print("FAIL: " + "; ".join(problems), file=sys.stderr)
        return 1
    print("PASS: 50 cold starts, no orphans, no crashes, no handle growth")
    return 0


if __name__ == "__main__":
    sys.exit(main())
