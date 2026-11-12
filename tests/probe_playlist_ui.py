"""离屏 + 无头内核探针：播放列表的多选、筛选、拖拽排序与批量移除。

判据只有一条：界面报给内核的**步骤**能不能把 `playlist` 变成用户松手时看到的那份顺序。
所以这里既量界面（筛选不改行号、多选不被高亮打散、Delete 只带走看得见的行），
也把面板算出的步骤原样喂给真内核（--vo=null --ao=null，无窗口），再读回 playlist 对账。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from PyQt5.QtCore import Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.engine.ipc import MpvIpc  # noqa: E402
from app.engine.player import MpvPlayer  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.playlist_panel import PlaylistPanel  # noqa: E402

TMP = os.path.join(HERE, ".probe-playlist-ui")
APP = QApplication.instance() or QApplication(sys.argv[:1])
theme.install(APP, str(cfgmod.DEFAULTS.get("theme_accent", "#4C8DFF")))
MPV = os.path.join(ROOT, "runtime", "mpv", "mpv.exe")


def pump(sec=0.2):
    end = time.monotonic() + sec
    while time.monotonic() < end:
        APP.processEvents()
        time.sleep(0.005)


def make_files(n: int) -> list[str]:
    os.makedirs(TMP, exist_ok=True)
    src = os.path.join(HERE, "samples", "gelu-sample-1.mp4")
    out = []
    for i in range(n):
        p = os.path.join(TMP, f"track-{i}.mp4")
        if not os.path.exists(p):
            shutil.copyfile(src, p)
        out.append(p)
    return out


def entries(paths: list[str], current: int = 0) -> list[dict]:
    return [{"filename": p, "id": i, "current": i == current,
             "playing": i == current} for i, p in enumerate(paths)]


def apply_steps(order: list[str], steps: list) -> list[str]:
    cur = list(order)
    for j, t in steps:
        cur.insert(t, cur.pop(j))
    return cur


def ui_probe(paths: list[str]) -> dict:
    panel = PlaylistPanel(MpvPlayer(cfgmod.Config()))
    panel.resize(320, 520)
    panel.show()
    panel.rebuild(entries(paths, 0))
    panel._rebuild_now()
    pump(0.05)
    names = [os.path.basename(p) for p in paths]

    # 多选 + 高亮不打散选择（先清掉重建时"当前条目"自带的那一行选中）
    panel.list.clearSelection()
    panel.list.item(1).setSelected(True)
    panel.list.item(3).setSelected(True)
    panel._highlight(0)
    sel = sorted(panel.list.row(it) for it in panel.list.selectedItems())

    # 向上整块拖：第 2、4 行拖到最前
    got: list[list] = []
    panel.sig_move_steps.connect(got.append)
    panel._on_move_rows([1, 3], 0)
    up = apply_steps(names, got[-1]) if got else []

    # 向下整块拖：第 1、3 行拖到末尾
    got.clear()
    panel._on_move_rows([0, 2], len(names))
    down = apply_steps(names, got[-1]) if got else []

    # 原地：不该产生任何步骤
    got.clear()
    panel._on_move_rows([1], 1)
    panel._on_move_rows([1], 2)
    noop = len(got)

    # 筛选：只藏不改行号；Delete 只带看得见的行
    panel.filter.setText("track-2")
    pump(0.05)
    visible = [i for i in range(panel.list.count()) if not panel.list.item(i).isHidden()]
    hidden_rows_same = [panel.list.row(panel.list.item(i)) for i in visible]
    count_text = panel.lbl_count.text()
    panel.list.blockSignals(True)
    for i in range(panel.list.count()):
        panel.list.item(i).setSelected(True)
    panel.list.blockSignals(False)
    removed: list[list] = []
    panel.sig_remove_rows.connect(removed.append)
    QTest.keyClick(panel.list, Qt.Key_Delete)
    pump(0.05)
    del_rows = removed[-1] if removed else []

    # 重建后选择按路径保住
    panel.filter.setText("")
    panel.list.blockSignals(True)
    panel.list.clearSelection()
    panel.list.item(1).setSelected(True)
    panel.list.item(3).setSelected(True)
    panel.list.blockSignals(False)
    panel.rebuild(entries(paths, 2))
    panel._rebuild_now()
    pump(0.05)
    kept = sorted(panel.list.row(it) for it in panel.list.selectedItems())

    panel.deleteLater()
    return {"sel": sel, "up": up, "down": down, "noop": noop, "visible": visible,
            "hidden_rows_same": hidden_rows_same, "count": count_text,
            "del_rows": del_rows, "kept": kept, "names": names}


def kernel_probe(paths: list[str]) -> dict:
    stamp = int(time.time() * 1000)
    pipe = rf"\\.\pipe\gelu-probe-plui-{stamp}"
    p = subprocess.Popen(
        [MPV, f"--input-ipc-server={pipe}", "--no-config", "--terminal=no",
         "--vo=null", "--ao=null", "--no-audio", "--idle=yes", "--keep-open=yes",
         "--pause=yes", "--volume=0", "--really-quiet"],
        cwd=os.path.dirname(MPV), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    ipc = MpvIpc(pipe)
    try:
        if not ipc.connect(timeout=10.0, proc=p):
            return {"env": "连不上 IPC"}
        for i, sample in enumerate(paths):
            ipc.fire(["loadfile", sample, "replace" if i == 0 else "append"])
        time.sleep(1.2)
        pl = ipc.get("playlist") or []
        if len(pl) != len(paths):
            return {"env": f"playlist 只有 {len(pl)} 条（要 {len(paths)}）"}
        panel = PlaylistPanel(MpvPlayer(cfgmod.Config()))
        panel.rebuild(pl)
        panel._rebuild_now()
        steps: list[list] = []
        panel.sig_move_steps.connect(steps.append)
        panel._on_move_rows([1, 3], len(paths))     # 把第 2、4 条拖到末尾
        if not steps:
            return {"env": "面板没有给出步骤"}
        for j, t in steps[0]:
            ipc.fire(["playlist-move", j, t])
        time.sleep(0.8)
        after = [os.path.basename(str(e.get("filename") or ""))
                 for e in (ipc.get("playlist") or [])]
        want = apply_steps([os.path.basename(x) for x in paths], steps[0])
        panel.deleteLater()
        return {"env": "", "steps": steps[0], "after": after, "want": want}
    finally:
        try:
            ipc.fire(["quit"])
        except Exception:
            pass
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()


def main() -> int:
    paths = make_files(5)
    ui = ui_probe(paths)
    want_up = ["track-1.mp4", "track-3.mp4", "track-0.mp4", "track-2.mp4", "track-4.mp4"]
    want_down = ["track-1.mp4", "track-3.mp4", "track-4.mp4", "track-0.mp4", "track-2.mp4"]
    ok_ui = (ui["sel"] == [1, 3] and ui["up"] == want_up and ui["down"] == want_down
             and ui["noop"] == 0 and ui["visible"] == [2] and ui["hidden_rows_same"] == [2]
             and ui["count"] == "1/5" and ui["del_rows"] == [2] and ui["kept"] == [1, 3])
    print(f"[{'PASS' if ok_ui else 'FAIL'}] 界面：多选={ui['sel']} 上移={ui['up']} "
          f"下移={ui['down']} 原地步骤={ui['noop']} 筛选可见={ui['visible']}"
          f"（行号={ui['hidden_rows_same']} 计数={ui['count']}） Delete={ui['del_rows']} "
          f"重建后选择={ui['kept']}")
    k = kernel_probe(paths)
    if k.get("env"):
        print(f"[ENV] 内核这轮没跑成：{k['env']}")
        return 1 if not ok_ui else 0
    ok_k = k["after"] == k["want"]
    print(f"[{'PASS' if ok_k else 'FAIL'}] 真内核：步骤={k['steps']} 结果={k['after']} "
          f"期望={k['want']}")
    return 0 if (ok_ui and ok_k) else 1


if __name__ == "__main__":
    sys.exit(main())
