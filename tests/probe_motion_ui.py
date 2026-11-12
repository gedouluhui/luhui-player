"""无头探针：调节面板「画面平滑」四行的回显与联动（不碰前台屏幕）。

为什么单独探这一页：`tests/test_ui.py` 的整窗验收在离屏装配下会在像素那一关中止，
面板↔内核那几条（`panel_matches_table` / `panel_agrees_with_kernel`）跑不到。
而这次新加的四项里有一个真正的坑：内核把 `deinterlace` 的 no/yes **回报成 JSON
布尔**（实测 `get_property deinterlace` → False/True，auto 仍是字符串），
面板若照 `str(v)` 找下拉项就会多出一枚「当前 False」，看着像内核认了个野值。

这里用一个假 player 把内核"已经推上来的值"直接喂给面板，量四件事：
  ① 控件集合与 `engine/tweaks.py` 那张表一一对应（少画一行＝那档根本没做出来）；
  ② 布尔/字符串两种回报都能落到表里那一项，且不会长出「当前 …」的伪档；
  ③ 未知值仍然原样回显（不许谎报成第 0 项）；
  ④ 插帧与同步方式的双向联动真的会各自改另一边（不是两个各转各的开关）。
"""
from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from PyQt5.QtCore import QObject, pyqtSignal                        # noqa: E402
from PyQt5.QtWidgets import QApplication                            # noqa: E402

from app.engine import tweaks                                        # noqa: E402
from app.ui import tweak_panel                                       # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  —— {detail}" if detail else ""), flush=True)
    return bool(ok)


class FakeCfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakePlayer(QObject):
    """只回答面板会问的几件事；set_tweak 记下命令并把值当成内核已接受。"""

    sig_info = pyqtSignal(dict)          # 与 MpvPlayer 同形状：{prop: value}

    def __init__(self):
        super().__init__()
        self.running = False
        self.store = {
            "video-sync": "audio",
            "interpolation": False,
            "tscale": "mitchell",
            "deinterlace": False,            # 实测形状：布尔，不是 "no"
        }
        self.cmds: list[tuple[str, object]] = []

    def prop(self, prop, default=None):
        return self.store.get(prop, default)

    def set_tweak(self, prop, value, confirm=True):
        self.cmds.append((prop, value))
        self.store[prop] = value
        self.sig_info.emit({prop: value})
        return value, True

    def ab_range(self):
        return None


def item_text(cb) -> str:
    return str(cb.currentText())


def main() -> int:
    app = QApplication.instance() or QApplication([])
    cfg = FakeCfg(keymap={})
    p = FakePlayer()
    tp = tweak_panel.TweakPanel(p, cfg)

    # ① 表 ↔ 面板
    want = {"rows": {s.prop for s in tweaks.NUMS},
            "choices": {c.prop for c in tweaks.CHOICES},
            "flags": {f.prop for f in tweaks.FLAGS}}
    got = {"rows": set(tp._rows), "choices": set(tp._choices), "flags": set(tp._flags)}
    diff = {k: sorted(want[k] ^ got[k]) for k in want if want[k] ^ got[k]}
    check("控件集合与可调项表一一对应（含新加的四项）",
          not diff, f"滑轨 {len(got['rows'])}/{len(want['rows'])} 下拉 {len(got['choices'])}/"
                    f"{len(want['choices'])} 勾选 {len(got['flags'])}/{len(want['flags'])}"
                    + (f" 差={diff}" if diff else ""))

    # ② 布尔回报落到表项上
    de = tp._choices["deinterlace"]
    seen = []
    for raw, want_data, want_text in ((False, "no", "关闭"), (True, "yes", "强制去隔行"),
                                      ("auto", "auto", "自动（只处理隔行片源）")):
        tp._paint_choice("deinterlace", raw)
        seen.append((raw, de.currentData(), item_text(de), getattr(de, "_gelu_custom", False)))
        check(f"内核回报 {raw!r} → 下拉选中「{want_text}」且不造伪档",
              de.currentData() == want_data and want_text in item_text(de)
              and not getattr(de, "_gelu_custom", False),
              f"data={de.currentData()!r} 文字={item_text(de)!r} 伪档={getattr(de, '_gelu_custom', False)}")

    # ③ 未知值原样回显
    tp._paint_choice("deinterlace", "huh")
    check("表外值不谎报成第 0 项（原样回显成「当前 huh」）",
          de.currentData() == "huh" and "huh" in item_text(de),
          f"data={de.currentData()!r} 文字={item_text(de)!r}")

    # ④ 联动：同步方式 → 音频 时自动关插帧
    p.store["interpolation"] = True
    p.store["video-sync"] = "display-resample"
    tp.repaint_from_kernel()
    interp = tp._flags["interpolation"]
    check("前提摆好：插帧开、同步跟随显示器", interp.isChecked() and
          tp._choices["video-sync"].currentData() == "display-resample",
          f"插帧={interp.isChecked()} 同步={tp._choices['video-sync'].currentData()!r}")
    # _on_choice 读的是 combo.currentData()，所以直接把下拉切到 audio 再走用户那条路
    syn = tp._choices["video-sync"]
    syn.setCurrentIndex(syn.findData("audio"))
    status: list[str] = []
    tp.sig_status.connect(status.append)
    tp._on_choice(tweaks.MOTION_SYNC, syn)
    check("把同步改回「跟随音频」→ 插帧被自动关掉，且状态栏点明原因",
          p.store["interpolation"] is False and not interp.isChecked()
          and any("已关掉插帧" in s for s in status),
          f"内核插帧={p.store['interpolation']} 勾选={interp.isChecked()} 状态={status}")

    # ⑤ 联动：开插帧 时同步方式抬到 display-resample
    tp._on_flag(tweaks.INTERPOLATION, True)
    check("打开插帧 → 同步方式自动切到「跟随显示器·重采样音频」，tscale 可用",
          p.store["interpolation"] is True
          and p.store["video-sync"] == "display-resample"
          and tp._choices["tscale"].isEnabled(),
          f"插帧={p.store['interpolation']} 同步={p.store['video-sync']!r} "
          f"tscale可用={tp._choices['tscale'].isEnabled()}")

    # ⑥ 关插帧 → tscale 变灰（它只影响插帧的混合方式）
    tp._on_flag(tweaks.INTERPOLATION, False)
    check("关掉插帧 → 「插帧混合」下拉变灰（避免对着无效项调半天）",
          not tp._choices["tscale"].isEnabled()
          and tp._choices["tscale"].toolTip() != "",
          f"可用={tp._choices['tscale'].isEnabled()} 提示={tp._choices['tscale'].toolTip()!r}")

    app.processEvents()
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print(f"\n---- 汇总：{len(results) - n_fail}/{len(results)} 通过"
          + (f"；失败：{[n for n, ok, _ in results if not ok]}" if n_fail else "") + " ----",
          flush=True)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
