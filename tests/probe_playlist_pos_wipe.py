"""小探测（一次性，不进常跑链）：`_on_start_file` 抹缓存那一下，`playlist-pos` 还看得见吗。

它回答的是 2026-10-17 那四条真机红的机制链（leg1「添加＝追加」在真窗口上变成了整表替换）：

    ①订阅表里有 playlist-pos 吗（内核会不会往缓存里推）
    ②它在 `_FILE_SCOPED` 里吗（换文件时会不会被抹）
    ③正播第 2 条时缓存值是多少
    ④`start-file` 之后缓存里还剩什么、`prop("playlist-pos", -1)` 回什么
    ⑤此时点「添加」，真正发给内核的第一条命令是什么
    ⑥判决：是不是被发成了 `replace`（整表替换）

④⑤⑥ 三行是判据的形状：**不猜**"抹了应该没事"，而是把 `Player._fire` 换成记录器，
看发出去的字节。两本留档：

    tests/out/probe-playlist-pos-wipe.txt          修前（playlist-pos 还在表里）
    tests/out/probe-playlist-pos-wipe-postfix.txt  修后（已摘出去）
    —— ②④⑤⑥ 四行必须翻面；没翻面就是没修。修前那份是当时仓库根的一次性草稿跑的，
       内容与本脚本逐字相同（只是那时它叫 `_probe_pos.py`、ROOT 取自身所在目录）。

它跑在隔离数据目录（`GELU_DATA` 指到 %TEMP% 下一次性目录），不碰用户真配置、不起真窗口、
不发 IPC、不出声——"⑤发出去的第一条命令"是记录器里的字符串，不是真按的按钮；
真按按钮那一腿在 `tests/test_ui.py`（leg1）。
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

TMP = os.path.join(os.environ.get("TEMP", "C:" + chr(92) + "temp"),
                   f"gelu-probe-pos-{os.getpid()}")
os.makedirs(os.path.join(TMP, "logs"), exist_ok=True)
os.environ["GELU_DATA"] = TMP
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, ROOT)

from app import config as cfgmod                      # noqa: E402
from app.engine.player import MpvPlayer, _OBSERVED     # noqa: E402
_FILE_SCOPED = MpvPlayer._FILE_SCOPED

C = os.path.join(HERE, "samples", "gelu-sample-3.mkv")
pl = MpvPlayer(cfgmod.Config(os.path.join(TMP, "config.json")))

print("① 订阅表里有 playlist-pos：", "playlist-pos" in _OBSERVED)
print("② _FILE_SCOPED 里有 playlist-pos：", "playlist-pos" in _FILE_SCOPED)

pl._props = {"playlist-pos": 1,
             "path": os.path.join(HERE, "samples", "gelu-sample-2.mp4")}
print("③ 正播 sample-2（缓存）：pos=%r path=%r" % (pl.prop("playlist-pos", -1),
                                                os.path.basename(pl.prop("path"))))

pl._on_start_file({"event": "start-file"})
print("④ start-file 之后：剩下 %s | prop('playlist-pos', -1)=%r prop('playlist-pos')=%r"
      % (sorted(pl._props), pl.prop("playlist-pos", -1), pl.prop("playlist-pos")))

fire = []
pl._fire = lambda c: fire.append(c)
pl.open_files([C], append=True, remember=False)
print("⑤ 点「添加」发出去的第一条 =", fire[:1])
print("⑥ 判决：缓存被抹 ⇒ 首条发成 replace（整表替换） =",
      bool(fire and fire[0][:3] == ["loadfile", C, "replace"]))
