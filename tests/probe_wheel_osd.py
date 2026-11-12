"""无头探针：滚轮 OSD 的落点（消息 → 播放器信号 → 窗口气泡）。

离屏装配一个不启动内核的主窗口（`files=[]` 且不调 `_begin_engine`，全程无窗口上屏），
只验这条链路的三个接点：
  ① `_write_input_conf` 写出的四行就是探针 `tests/probe_wheel_order.py` 实测过的那四行；
  ② `MpvPlayer._on_client_message` 只认 `luhui-wheel` 前缀，别的 script-message 不误报；
  ③ 音量/快进的声音（含静音后缀、坏载荷不炸）确实是用户会看到的那句话。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("GELU_DATA", os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".selftest-data-wheel")))

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication                          # noqa: E402

from app import config as cfgmod                                   # noqa: E402
from app.engine.player import MpvPlayer                            # noqa: E402
from app.ui.main_window import MainWindow                          # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


def main() -> int:
    app = QApplication.instance() or QApplication([])
    os.makedirs(cfgmod.DATA_DIR, exist_ok=True)
    cfg = cfgmod.Config()
    cfg["resume"] = False
    cfg.save()

    # ① input.conf 的落盘内容
    p = MpvPlayer(cfg)
    with open(p._write_input_conf(), "r", encoding="utf-8") as fh:
        body = fh.read()
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.startswith("#")]
    check("input.conf 四行：音量带 ${volume}、seek 是 relative（旧 absolute 会永远跳回 5 秒）",
          len(lines) == 4
          and body.count("${volume}") == 2
          and body.count("relative+exact") == 2
          and "absolute" not in body
          and all("script-message luhui-wheel" in ln for ln in lines),
          f"{lines}")

    # ② 消息过滤 + 信号
    got: list[tuple[str, str]] = []
    p.sig_wheel.connect(lambda k, v: got.append((k, v)))
    p._on_client_message({"event": "client-message", "args": ["luhui-wheel", "vol", "42"]})
    p._on_client_message({"event": "client-message", "args": ["luhui-wheel", "seek", "-5"]})
    p._on_client_message({"event": "client-message", "args": ["someone-else", "x"]})
    p._on_client_message({"event": "client-message"})
    check("client-message 只转发自己的 luhui-wheel 两条，别的消息与空载荷不误报",
          got == [("vol", "42"), ("seek", "-5")], f"{got}")

    # ③ 主窗口的 OSD 文案
    win = MainWindow(cfg, files=[])
    win.player.sig_wheel.emit("vol", "42")
    app.processEvents()
    check("内核回声 → 播放器信号 → 窗口气泡：「音量 42%」",
          win.osd._text == "音量 42%", f"{win.osd._text!r}")

    win.player._props["mute"] = True
    win.player.sig_wheel.emit("vol", "200")
    app.processEvents()
    check("静音状态下滚轮改音量：气泡补一句「（静音中）」（音量确实在变，别让人以为没动）",
          win.osd._text == "音量 200%（静音中）", f"{win.osd._text!r}")
    win.player._props["mute"] = False

    win.player.sig_wheel.emit("seek", "+5")
    app.processEvents()
    check("Ctrl+滚轮快进：气泡「+5 秒」", win.osd._text == "+5 秒", f"{win.osd._text!r}")

    before = win.osd._text
    win.player._on_client_message({"args": ["luhui-wheel", "vol", "not-a-number"]})
    win.player._on_client_message({"args": ["luhui-wheel", "seek", "garbage"]})
    app.processEvents()
    check("坏载荷（内核不该发，但也不许炸）：坏音量不改气泡，坏方向按「-5 秒」兜底",
          win.osd._text == "-5 秒" and win.osd.last_error is None,
          f"前={before!r} 后={win.osd._text!r} 绘制错误={win.osd.last_error}")

    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n---- 汇总：{len(results) - n_fail}/{len(results)} 通过"
          + (f"；失败：{[n for n, ok, _ in results if not ok]}" if n_fail else "") + " ----",
          flush=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
