# -*- coding: utf-8 -*-
"""留档诊断脚本（**不是判据、不进任何链**）：R20 那条「播放列表点添加＝加长」的气泡为什么是空。

保留理由（这里原先写的是"跑完就删"）：它产出的 `tests/out/probe-append-receipt-hidden2.log`
是 `_settle_open_receipt` 的 docstring、`tests/test_open_receipt.py` 的 R5 与 `tests/test_ui.py`
的 6.6 三处引用的实测依据（"一次初始载入里 playlist 连推 4 次、长度全是 2"）。删了脚本，
那几处就只剩一份没有生成者的日志。日常判据不需要它——只有再怀疑推送顺序时才用
`--hidden`（showMinimized，不抢前台）重跑；默认那条会建可见窗，用户在用电脑时别跑。

 hypotheses（读代码得出的两条，都要在这趟里被分清）：
   H1 回执被一声**与这一趟无关**的 `file-loaded`／或一条**还没变长**的 playlist 推送提前
      消费掉了：`_settle_open_receipt` 第一件事就 `self._open_claim = None`，而 `open_receipt`
      在证据没到时返回 `say=False` ⇒  claim 没了、话也没说 ⇒ 气泡永远空。
   H2 追加那一路的凭据（`plays=False` 等 playlist 变长）压根没走到 `_settle_open_receipt`
      （推送顺序/信号连线问题）。

分清办法：把 `_arm_open_receipt`/`_settle_open_receipt` 与内核的 `sig_playlist`/`sig_file_loaded`
都包一层，按单调时间戳把**每一声**连同当时的 `claim`/`_pl_len` 打出来。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_ui as T                                       # noqa: E402
from PyQt5.QtWidgets import QApplication                  # noqa: E402
from app.ui import theme                                  # noqa: E402

T0 = time.monotonic()


def stamp() -> str:
    return f"{time.monotonic() - T0:7.2f}s"


def main() -> int:
    T._enable_high_dpi()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    # 必须把 QApplication 交回给 T.APP：`T.pump()`/`T.wait_for()` 的第一行就是
    # `if APP is None: return`（test_ui.py:150）。第一趟忘了赋值 ⇒ 整趟一个事件都没跑，
    # 跨线程推送全堆在队列里没送达（`_pl_len` 恒 0、气泡恒空），量到的形状和真机完全不同——
    # 那是量具失灵，不是产品的坏。
    T.APP = app
    cfg = T.make_cfg()
    theme.install(app, str(cfg["theme_accent"]))
    if "--hidden" in sys.argv:
        # 这一趟只查**事件顺序**（回执什么时候被谁消费），一个像素都不量 ⇒ 不必占前台。
        # 此刻 `tests/desk_busy.py` 报的是别的仓的窗口在抢前台（Salt Player 的设置对话框），
        # 冒出来一只窗会把人家那一拍的截屏搅了。可见态那一份留给看门狗链里跑。
        win = T.MainWindow(cfg, files=[])
        win.resize(1180, 720)
        win.showMinimized()
        T.pump(0.3)
    else:
        win = T.build_window(cfg)
    # 异步启动：发起后由事件循环推进握手（产品 GUI 是 40ms 定时器，这里手动 pump）。
    win._begin_engine()
    end = time.monotonic() + 12.0
    while not win.player.running and time.monotonic() < end:
        T.pump(0.05)
    if not win.player.running:
        print("内核起不来，诊断作废")
        return 2
    def traced(tag, fn):
        def inner(*a, **k):
            c = win._open_claim
            print(f"[{stamp()}] {tag}{a} {k} → claim="
                  f"{None if c is None else {'n': c['n'], 'plays': c['plays'], 'before': c['before']}} "
                  f"_pl_len={win._pl_len} osd={win.osd._text!r}")
            r = fn(*a, **k)
            c2 = win._open_claim
            print(f"[{stamp()}] {tag} 之后 → claim={'没了' if c2 is None else '还挂着'} "
                  f"osd={win.osd._text!r}")
            return r
        return inner

    win._settle_open_receipt = traced("settle ", win._settle_open_receipt)
    win._arm_open_receipt = traced("arm   ", win._arm_open_receipt)
    # 直接数 `_apply`：缓存与 sig_playlist 都在它里面（player.py:419-443），
    # 分不清"内核没推"还是"推了但界面没接"时，这一层是唯一能分的地方。
    _seen = {}
    _orig_apply = win.player._apply
    import threading as _th

    def apply_traced(name, value):
        _seen[name] = _seen.get(name, 0) + 1
        if name in ("playlist", "playlist-count"):
            print(f"[{stamp()}] _apply({name!r}) 第{_seen[name]}次 长度="
                  f"{len(value) if isinstance(value, list) else value!r} "
                  f"线程={_th.current_thread().name}")
        return _orig_apply(name, value)

    win.player._apply = apply_traced
    win.player.sig_playlist.connect(lambda pl: print(
        f"[{stamp()}] 收到 sig_playlist 长度={len(pl or [])} "
        f"线程={_th.current_thread().name}（这条只记，不改行为）"))
    win.player.sig_file_loaded.connect(lambda: print(f"[{stamp()}] 收到 sig_file_loaded"))

    def pos_now():
        v = win.player.prop("playlist-pos")
        return -1 if v is None else int(v)

    files = T.FILES[:2]
    win.add_paths(list(files))
    ok = T.wait_for(lambda: pos_now() >= 0, 8.0)
    win.player.goto_index(1)                      # 对齐 6.6 的前提：正播的是**最后**那条
    T.wait_for(lambda: pos_now() == 1, 6.0)
    print(f"[{stamp()}] 起步：两条并正播={ok} n={T._snap(win)['n']} "
          f"pos={win.player.prop('playlist-pos')} _pl_len={win._pl_len}")

    G3 = os.path.join(T.SAMPLES, "gelu-sample-3.mkv")
    win.osd._text = ""
    b = T._snap(win)
    # 前提闸门：`_pl_len` 是界面在**主线程**里由 `sig_playlist` 记的账。它不等于缓存里的列表
    # 长度就说明这一趟的事件根本没送达（上一趟就是这么失灵的：`T.APP` 没赋值 ⇒ pump 空转），
    # 那时候量到的"气泡空"是量具的坏，拿它去改产品就是拿幻觉当证据。
    if win._pl_len != b["n"]:
        print(f"[{stamp()}] [量具失灵] _pl_len={win._pl_len} ≠ 缓存列表长度={b['n']}"
              f" ⇒ 主线程没收到 sig_playlist，这一趟不作数（别拿它的读数去改任何东西）")
        return 3
    print(f"[{stamp()}] === 发 sig_add_paths（追加 1 条）之前 "
          f"n={b['n']} pos={b['pos']} path={b['path']!r}")
    t_fire = time.monotonic()
    win.panel.sig_add_paths.emit([G3])
    while time.monotonic() - t_fire < 8.0:
        T.pump(0.1)
    a = T._snap(win)
    print(f"[{stamp()}] === 8s 之后 n={a['n']} pos={a['pos']} _pl_len={win._pl_len} "
          f"claim={'没了' if win._open_claim is None else win._open_claim} 气泡={win.osd._text!r} "
          f"推送计数={_seen}")
    ok2, d2 = T.add_verdict("playing", b, a, win.osd._text)
    print(f"[{stamp()}] 判据复述：{ok2} —— {d2}")
    # 再等到超过 OPEN_RECEIPT_MS：走到超时那一路说明**证据从头到尾没到过**，
    # 到这儿才说话说明证据到过但没被认（两条的修法完全不同，不许混）。
    t_wait = time.monotonic()
    while time.monotonic() - t_wait < 16.0:
        T.pump(0.2)
    print(f"[{stamp()}] === 再等 16s（越过 20s 超时）气泡={win.osd._text!r} "
          f"claim={'没了' if win._open_claim is None else '还挂着'} 推送计数={_seen}")
    win.close()
    T.pump(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
