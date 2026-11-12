"""临时证伪：往 app/ 真代码里注入"接收者是 self.<属性>、成员本机不存在"的静默坑，
要求第四遍必须点名到 文件:行；随后原样还原并核对 md5（本仓库没有 git，还原只能靠哈希对账）。

两条变异各管一类接收者，都走**二进制**替换与还原：先用 copy2 留一份原样备份，改的时候
只 replace 一个 ASCII 锚点，还原时整份拷回——文本模式读写会把 CRLF 换算一遍，md5 就对不上，
那种"还原"等于悄悄改了源文件。
  M1 已有的正确调用 `self._pool.waitForDone(` → Qt6 才有的 `self._pool.waitForFinished(`
  M2 控制条的 `self.lbl_time.setText(` → 本机 QLabel 没有的 `self.lbl_time.setTextNope(`
还原后再跑一遍，要求 rc=0 且 md5 与动手前一致。
"""
import hashlib
import os
import shutil
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
STAMP = time.strftime("%Y%m%d-%H%M%S")
LOGDIR = os.path.join(ROOT, "tests", "logs")


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def run_audit(tag):
    r = subprocess.run([sys.executable, os.path.join("tests", "audit_qt_attrs.py")],
                       cwd=ROOT, capture_output=True)
    out = r.stdout.decode("utf-8", "replace")
    open(os.path.join(LOGDIR, "audit-qt-attrsfalsify-%s-%s.log" % (tag, STAMP)),
         "w", encoding="utf-8").write(out)
    return r.returncode, out


MUTS = [
    ("M1", "app/engine/thumbs.py", b"self._pool.waitForDone(", b"self._pool.waitForFinished(",
     ("thumbs.py", "waitForFinished")),
    ("M2", "app/ui/controls_bar.py", b"self.lbl_time.setText(", b"self.lbl_time.setTextNope(",
     ("controls_bar.py", "setTextNope")),
]

fails = 0
for tag, rel, old, new, expect in MUTS:
    p = os.path.join(ROOT, rel)
    bak = p + ".pristine-" + STAMP
    before = md5(p)
    data = open(p, "rb").read()
    if data.count(old) < 1:
        print("[SKIP] %s：%s 里找不到锚点 %s（锚点变了，证伪没做成）" % (tag, rel, old.decode()))
        fails += 1
        continue
    shutil.copy2(p, bak)
    open(p, "wb").write(data.replace(old, new, 1))
    try:
        rc, out = run_audit("%s-injected" % tag)
    finally:
        shutil.copyfile(bak, p)
        os.remove(bak)
    after = md5(p)
    lines = [ln for ln in out.splitlines() if ln.startswith("[FAIL]")]
    named = [ln for ln in lines if expect[0] in ln and expect[1] in ln]
    by4 = [ln for ln in named if "实例属性成员" in ln]
    ok = rc == 1 and bool(named) and bool(by4) and after == before
    fails += 0 if ok else 1
    print("[%s] %s 注入 %s → rc=%d 报红=%d 点名本条=%d 其中第四遍=%d ｜还原 md5 一致=%s" %
          ("CAUGHT" if ok else "MISSED", tag, new.decode(), rc, len(lines), len(named), len(by4),
           after == before))
    for ln in named:
        print("       " + ln)

rc, out = run_audit("restored")
ok = rc == 0
fails += 0 if ok else 1
tail = [ln for ln in out.strip().splitlines() if ln][-1]
print("[%s] 全部还原后重跑：rc=%d ｜%s" % ("PASS" if ok else "FAIL", rc, tail))
print("\n证伪 %d 组 + 还原复跑 1 组，失败 %d 组" % (len(MUTS), fails))
sys.exit(1 if fails else 0)
