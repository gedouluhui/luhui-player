import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.engine import usage

print("INFO_NAMES:", sorted(usage._INFO_NAMES))
print("limits(0x1):", usage.limit_reasons(0x1), "| all:", usage.throttle_names(0x1))
print("limits(0x4|0x1):", usage.limit_reasons(0x5))
print("limits(0x10000):", usage.limit_reasons(0x10000))
print("limits(None):", usage.limit_reasons(None), usage.throttle_names(None))
print("NUM:", usage._num("1,485"), usage._num("[N/A]"), usage._num("-3.5 W"), usage._num("0"))
p, gp, why = usage.sample_window({"内核": 99999999, "外壳": os.getpid()}, window=0.3)
print("ORDER:", list(p.keys()))
print("GP keys:", sorted(gp.keys()))
print(usage.build_report(p, gp, {"estimated-vf-fps": 59.94, "avsync": -0.001}, "", 0.3))
