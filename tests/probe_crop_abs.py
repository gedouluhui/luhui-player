import os, sys, time
os.environ.setdefault("GELU_DATA", os.path.abspath("tests/.selftest-data"))
sys.path.insert(0, os.path.abspath("."))
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtGui import QImage
from app import config as cfgmod
from app.engine.player import MpvPlayer
app = QCoreApplication.instance() or QCoreApplication([])
OUT = os.path.abspath("tests/out/probe-frames"); os.makedirs(OUT, exist_ok=True)
cfg = cfgmod.Config(); cfg["resume"]=False; cfg["screenshot_dir"]=OUT; cfg.save()
p = MpvPlayer(cfg); p.sig_log.connect(lambda s: None)
assert p.start(hwnd=0)
p.open_files([os.path.join("tests","samples","gelu-sample-1.mp4")], remember=False)
time.sleep(2.0)
def names(): return [x.get("name") for x in (p.get("vf",[]) or []) if isinstance(x,dict)]
def shot(tag):
    path = os.path.join(OUT, f"c6-{tag}.png").replace("\\","/")
    if os.path.exists(path): os.remove(path)
    p._req(["screenshot-to-file", path, "video"], timeout=8)
    for _ in range(80):
        if os.path.exists(path): break
        time.sleep(0.1)
    if not os.path.exists(path): return "无文件"
    im = QImage(path); sz = os.path.getsize(path); os.remove(path)
    return f"{im.width()}×{im.height()} @{sz}B"
def run(tag, chain, expect):
    p._req(["change-list","vf","set",""], timeout=3.0); time.sleep(0.3)
    o = os.path.getsize(p.log_path)
    p._req(["change-list","vf","set",chain], timeout=3.0)
    time.sleep(0.9)
    with open(p.log_path, encoding="utf-8", errors="ignore") as fh:
        fh.seek(o); tail = fh.read()
    errs = [l.strip()[-120:] for l in tail.splitlines() if "][e]" in l or "][w]" in l]
    got = shot(tag)
    ok = got.split(" @")[0] == expect
    print(f"[{'PASS' if ok else 'FAIL'}] {tag}: 截图={got} 期望={expect} listed={names()}")
    for e in errs[:3]: print("       ", e)

def crop_expr(n, d):
    w = f"trunc((iw+ih*{n}/{d}-abs(iw-ih*{n}/{d}))/4)*2"
    h = f"trunc((ih+iw*{d}/{n}-abs(ih-iw*{d}/{n}))/4)*2"
    return f"crop={w}:{h}:(iw-{w})/2:(ih-{h})/2"

for (n, d, exp) in ((235, 100, "1280×544"), (4, 3, "960×720"), (1, 1, "720×720"),
                    (16, 9, "1280×720"), (21, 9, "1280×548")):
    run(f"{n}:{d}", crop_expr(n, d), exp)
print("\n带 format 前缀 + 其它滤镜同链（契约顺序）：")
c = "format=yuv420p," + crop_expr(235, 100) + ",hflip,unsharp=5:5:1.00:5:5:0.0"
run("chain-235", c, "1280×544")
print("\n数字写法（对照）：")
run("numeric", "crop=960:720:160:0", "960×720")
p.quit()
