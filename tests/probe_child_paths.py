"""一次性探针（§6 第 27 条的续账）：交给 ffmpeg 的每一条路径，是不是都绝对？

mpv 那一头已经收口（`player.mpv_path()` 一律 abspath），但**换 cwd 的子进程**才是这一族的
失效形状：`gif.py` 用 `Popen(cmd, cwd=os.path.dirname(out))` 起 ffmpeg，
所以它拿到的 `video` 若是相对路径，就会在"输出目录"下重新解释——文件明明在，却报"找不到"。
本探针从仓库根目录喂一条**相对**路径（和用户在命令行里敲
`python app/main.py tests/samples/x.mp4` 同一个形状），然后逐处把真正会进命令行的
字符串捞出来问 `isabs`：
mpv 回报的 path、播放列表里那一项、缩略图 worker 的 video、封面 worker 的 out、
工坊的 gif spec。只打印事实，不产生判据；用完即可丢弃。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from PyQt5.QtWidgets import QApplication    # noqa: E402
import test_ui as TU                        # noqa: E402
from app import config as cfgmod            # noqa: E402


SEEN: list[tuple[str, str]] = []


def show(tag: str, val: str) -> None:
    SEEN.append((tag, val))
    print(f"  {tag:<26} isabs={os.path.isabs(val) if val else '—(空)'}  {val!r}")


def main() -> int:
    os.chdir(ROOT)                     # 模拟"在仓库根目录敲相对路径"
    app = QApplication.instance() or QApplication(sys.argv[:1])
    TU.APP = app
    cfg = TU.make_cfg()
    win = TU.build_window(cfg)
    rel = os.path.join("tests", "samples", "gelu-sample-1.mp4")
    print(f"喂进去的相对路径：{rel!r}（exists={os.path.exists(rel)}）")
    win.add_paths([rel])
    ok = TU.wait_for(lambda: float(win.player.prop("duration", 0) or 0) > 0, 15.0)
    print(f"内核回报时长：{ok} —— 说明这条路 mpv 自己找到了文件")

    print("\n---- 会进子进程命令行的那些字符串 ----")
    show("mpv path", str(win.player.prop("path") or ""))
    show("gif_state video", str(win.gif_state().get("video") or ""))
    show("FFMPEG_EXE", cfgmod.FFMPEG_EXE)
    show("MPV_EXE", cfgmod.MPV_EXE)
    show("DATA_DIR", cfgmod.DATA_DIR)
    show("screenshot_dir", str(cfg["screenshot_dir"]))

    win.thumbs.request(rel, 10.0)          # 直接拿相对路径喂缩略图链，看它自己怎么处置
    TU.pump(0.3)
    for k, w in getattr(win.thumbs, "_active", {}).items():
        show(f"thumbs worker video（{k[:8]}…）", str(getattr(w, "video", "")))
        show("thumbs worker out_dir", str(getattr(w, "out_dir", "")))
    if not getattr(win.thumbs, "_active", {}):
        print("  （主窗那份是缓存命中 ⇒ 没有活跃 worker，改拿一份干净的 ThumbStore 直接问 worker 本人）")
        from app.engine.thumbs import ThumbStore        # noqa: E402
        tmp_store = ThumbStore(cfgmod.FFMPEG_EXE, os.path.join(TU.OUT, "probe-child-paths"))
        tmp_store.request(rel, 10.0)
        TU.pump(0.4)
        for k, w in getattr(tmp_store, "_active", {}).items():
            show(f"干净 store 的 worker video（{k[:8]}…）", str(getattr(w, "video", "")))
            show("干净 store 的 worker out_dir", str(getattr(w, "out_dir", "")))
        if not getattr(tmp_store, "_active", {}):
            print("  干净 store 也没起 worker，失败表：",
                  list(getattr(tmp_store, "_failed", {}).values())[:1])
        tmp_store.deleteLater()

    leaks = [t for t, v in SEEN
             if v and not os.path.isabs(v)]
    print(f"\n---- 结论：非绝对的会进子进程的路径 = {leaks or '没有'} ----")
    win.close()
    TU.pump(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
