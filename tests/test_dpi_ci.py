# -*- coding: utf-8 -*-
"""混合 DPI、CI 门禁与供应链校验的验收（审计 P2 + 供应链准则）。

三件事，各自都有"看起来做了其实没做"的形状：
  · DPI：清单写了 PerMonitorV2 但进程还是 unaware（pythonw 没有清单）／设了
    PassThrough 但晚于 QApplication；WM_DPICHANGED 来了只让 Qt 自己改，mpv 的原生子
    窗口仍停在旧物理尺寸。
  · CI：工作流文件存在但只跑 `echo`，或者离线/探针混在一起，windows-latest 上必红。
  · 供应链：checksums.sha256 存在但没人核对，等于没有。

用法：
    python tests/test_dpi_ci.py
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)
PY = sys.executable
TMP_DATA = tempfile.mkdtemp(prefix="luhui-dpici-data-")

FAILS: list[str] = []
TOTAL = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global TOTAL
    TOTAL += 1
    print(("[PASS] " if ok else "[FAIL] ") + name + (f" ｜ {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


def run(args, timeout: int = 180, extra_env: dict | None = None, cwd: str = ROOT):
    env = os.environ.copy()
    env.update({"QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8",
                "PYTHONPATH": cwd, "LUHUI_DATA": TMP_DATA, "GELU_DATA": TMP_DATA})
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "<timeout>"
    return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")


def read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


# ==================== D1：进程级 PerMonitorV2 ====================
def d1_awareness() -> None:
    code = r"""
from app import win32
before = win32.dpi_awareness()
ok = win32.enable_per_monitor_v2()
after = win32.dpi_awareness()
pmv2 = win32.is_per_monitor_v2()
print("BEFORE", before)
print("OK", int(ok))
print("AFTER", after)
print("PMV2", int(pmv2))
"""
    rc, out = run([PY, "-u", "-c", code], timeout=120)
    vals = dict(re.findall(r"(BEFORE|OK|AFTER|PMV2)\s+(-?\d+)", out))
    check("D1 进程可升级到 PerMonitorV2（pythonw 无清单时靠 API 补）",
          rc == 0 and vals.get("AFTER") == "2" and vals.get("PMV2") == "1",
          f"rc={rc} before={vals.get('BEFORE')} ok={vals.get('OK')} "
          f"after={vals.get('AFTER')} pmv2={vals.get('PMV2')}")


# ==================== D2：PassThrough 在 QApplication 之前 ====================
def d2_rounding_policy() -> None:
    code = r"""
import inspect
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication
from app import main
main._enable_high_dpi()
app = QApplication([])
print("POLICY", int(QApplication.highDpiScaleFactorRoundingPolicy()))
print("WANT", int(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough))
src = inspect.getsource(main.main)
print("ORDER", int(src.index("_enable_high_dpi()") < src.index("QApplication(")))
"""
    rc, out = run([PY, "-u", "-c", code], timeout=120)
    vals = dict(re.findall(r"(POLICY|WANT|ORDER)\s+(-?\d+)", out))
    policy_ok = vals.get("POLICY") == vals.get("WANT") not in (None, "-1")
    hdr = read(os.path.join(ROOT, "app", "main.py"))
    fn = re.search(r"def _enable_high_dpi\(\) -> None:(.*?)(?=\n\ndef |\n\n# =)",
                   hdr, re.S)
    body = fn.group(1) if fn else ""
    check("D2 小数缩放用 PassThrough，且早于 QApplication",
          rc == 0 and policy_ok and vals.get("ORDER") == "1"
          and "setHighDpiScaleFactorRoundingPolicy" in body
          and "enable_per_monitor_v2" in body,
          f"rc={rc} policy={vals.get('POLICY')} want={vals.get('WANT')} "
          f"order={vals.get('ORDER')}")


# ==================== D3：应用清单 PerMonitorV2 并接进打包 ====================
def d3_manifest() -> None:
    path = os.path.join(ROOT, "packaging", "app.manifest")
    text = read(path)
    valid = False
    try:
        ET.fromstring(text)
        valid = bool(text.strip())
    except ET.ParseError:
        valid = False
    spec = read(os.path.join(ROOT, "packaging", "portable.spec"))
    check("D3 应用清单 PerMonitorV2 + portable.spec 真引用",
          valid and "PerMonitorV2" in text and "true/pm" in text
          and "schemas.microsoft.com/SMI/2016/WindowsSettings" in text
          and "manifest=" in spec and "app.manifest" in spec,
          f"xml={valid} spec_manifest={'app.manifest' in spec}")


# ==================== D4：WM_DPICHANGED 后重铺 mpv 子窗口 ====================
def d4_dpichanged() -> None:
    from PyQt5.QtWidgets import QApplication, QWidget

    from app.ui import chrome as chrome_mod

    app = QApplication.instance() or QApplication([])
    w = QWidget()
    c = chrome_mod.Chrome(w)
    c._hwnd = 0x2199A
    calls: list[int] = []
    c.on_dpi_changed = lambda: calls.append(1)
    msg = chrome_mod._MSG()
    msg.hwnd = 0x2199A
    msg.message = 0x02E0
    msg.pad = 0
    msg.wParam = 0
    msg.lParam = 0
    msg.time = 0
    handled, _res = c._dispatch(b"windows_generic_MSG", ctypes.addressof(msg))
    app.processEvents()
    main_src = read(os.path.join(ROOT, "app", "ui", "main_window.py"))
    wired = bool(re.search(r"self\.chrome\.on_dpi_changed\s*=\s*self\._on_dpi_changed",
                           main_src))
    method = re.search(r"def _on_dpi_changed\(self\).*?refit\(force=True\)",
                       main_src, re.S)
    check("D4 WM_DPICHANGED 触发校准（放行给 Qt，回调重铺子窗口）",
          bool(calls) and handled is False and c.seen.get(0x02E0) == 1
          and wired and bool(method),
          f"calls={len(calls)} seen={c.seen.get(0x02E0)} handled={handled} "
          f"wired={wired} method={bool(method)}")


# ==================== D5：CI 工作流与 pytest 标记 ====================
def d5_ci_yaml() -> None:
    ci_path = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    ci = read(ci_path)
    need = ("windows-latest", "compileall", "tests/ci_leak_scan.py",
            "tests/ci_offline.py", "push:", "pull_request:")
    missing = [t for t in need if t not in ci]
    ini = read(os.path.join(ROOT, "pytest.ini"))
    marked = all(t in ini for t in ("unit:", "probe:", "test_pytest_"))
    check("D5 windows-latest CI：compileall + 防泄漏 + 离线回归，pytest 标记分离",
          os.path.isfile(ci_path) and not missing and marked and "|| true" not in ci,
          f"missing={missing} pytest_ini={marked}")


# ==================== D6：防泄漏扫描器真能抓到个人路径 ====================
def d6_leak_scan() -> None:
    script = os.path.join(ROOT, "tests", "ci_leak_scan.py")
    tmp = tempfile.mkdtemp(prefix="luhui-leak-")
    try:
        leak = os.path.join(tmp, "notes.txt")
        joined = "C:" + chr(92) + "Users" + chr(92) + "alice" + chr(92) + "secret.txt"
        with open(leak, "w", encoding="utf-8") as f:
            f.write("path=" + joined + "\n")
        rc_bad, out_bad = run([PY, "-u", script, "--root", tmp], timeout=120)
        os.remove(leak)
        with open(os.path.join(tmp, "clean.txt"), "w", encoding="utf-8") as f:
            f.write("path=%USERPROFILE%" + chr(92) + "clip.mp4\n")
        rc_ok, out_ok = run([PY, "-u", script, "--root", tmp], timeout=120)
        binary = os.path.join(tmp, "blob.bin")
        with open(binary, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + joined.encode("ascii") + b"\x00\xff")
        rc_bin, _out_bin = run([PY, "-u", script, "--root", tmp], timeout=120)
        check("D6 防泄漏扫描：抓到个人绝对路径，放过占位符与非文本",
              os.path.isfile(script) and rc_bad == 1 and "notes.txt" in out_bad
              and rc_ok == 0 and rc_bin == 0,
              f"bad={rc_bad} clean={rc_ok} bin={rc_bin}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==================== D7：运行时二进制 checksums 与打包前核对 ====================
def d7_checksums() -> None:
    cs = os.path.join(ROOT, "packaging", "runtime_checksums.sha256")
    text = read(cs)
    entries = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", line)
        if m:
            entries[m.group(2).replace("\\", "/")] = m.group(1).lower()
    required = ("runtime/mpv/mpv.exe", "runtime/mpv/mpv/fonts.conf",
                "runtime/ffmpeg/ffmpeg.exe")
    declared = all(p in entries for p in required)

    verifier = os.path.join(ROOT, "packaging", "verify_runtime.py")
    spec = read(os.path.join(ROOT, "packaging", "portable.spec"))
    tmp = tempfile.mkdtemp(prefix="luhui-sum-")
    try:
        rels = ["runtime/mpv/mpv.exe", "runtime/mpv/mpv/fonts.conf",
                "runtime/ffmpeg/ffmpeg.exe"]
        lines = []
        for i, rel in enumerate(rels):
            p = os.path.join(tmp, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(b"payload-%d" % i)
            lines.append(f"{hashlib.sha256(b'payload-%d' % i).hexdigest()}  {rel}")
        sum_file = os.path.join(tmp, "sums.sha256")
        with open(sum_file, "w", encoding="ascii") as f:
            f.write("\n".join(lines) + "\n")
        rc_ok, _ = run([PY, "-u", verifier, "--root", tmp, "--checksums", sum_file],
                       timeout=120)
        with open(os.path.join(tmp, rels[0].replace("/", os.sep)), "ab") as f:
            f.write(b"tamper")
        rc_bad, out_bad = run([PY, "-u", verifier, "--root", tmp, "--checksums", sum_file],
                              timeout=120)
        os.remove(os.path.join(tmp, rels[1].replace("/", os.sep)))
        rc_missing, _ = run([PY, "-u", verifier, "--root", tmp, "--checksums", sum_file],
                            timeout=120)
        check("D7 供应链：checksums.sha256 固定三件套，打包前核对（篡改/缺失必红）",
              os.path.isfile(cs) and declared and os.path.isfile(verifier)
              and rc_ok == 0 and rc_bad == 1 and rc_missing == 1
              and "verify_runtime" in spec,
              f"declared={declared} ok={rc_ok} tamper={rc_bad} missing={rc_missing}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ==================== D8：离线回归跑者与探针分离 ====================
def d8_offline_runner() -> None:
    runner = os.path.join(ROOT, "tests", "ci_offline.py")
    rc, out = run([PY, "-u", runner, "--list"], timeout=120)
    need = ("test_hardening.py", "test_subtitle_drop.py", "test_responsiveness.py",
            "test_win_geom.py", "test_dpi_ci.py", "test_equalizer.py")
    probes = ("test_engine.py", "test_ui.py", "test_gif_real.py", "test_thumbs.py",
              "test_transform.py", "test_engine_async_real.py")
    local = ("test_library.py", "test_shutdown_quiet.py", "test_instance.py",
             "test_launchers.py")
    missing = [n for n in need if n not in out]
    leaked = [n for n in probes + local if re.search(rf"\b{re.escape(n)}\b", out)]
    src = read(runner)
    check("D8 离线回归跑者：只收离线套件，真内核/真屏探针不进门禁",
          os.path.isfile(runner) and rc == 0 and not missing and not leaked
          and "QT_QPA_PLATFORM" in src and "sys.executable" in src,
          f"rc={rc} missing={missing} probes={leaked}")


def main() -> int:
    for fn in (d1_awareness, d2_rounding_policy, d3_manifest, d4_dpichanged,
               d5_ci_yaml, d6_leak_scan, d7_checksums, d8_offline_runner):
        try:
            fn()
        except Exception as exc:
            check(fn.__name__, False, f"{type(exc).__name__}: {exc}")
    print(f"[FINAL] 混合 DPI/CI/供应链：{TOTAL - len(FAILS)}/{TOTAL} 通过；"
          f"红 {len(FAILS)} 条")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(TMP_DATA, ignore_errors=True)
