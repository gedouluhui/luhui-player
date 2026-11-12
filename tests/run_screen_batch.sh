#!/usr/bin/env bash
# 屏幕亮着这段时间能跑的"真机半套"，一趟串完（各写各的日志，绝不覆盖旧证据）。
# 为什么串行：这几套都要抢前台抓屏，同时跑会互相把对方的采样点压住（遮挡闸门会点出自家窗口）。
set -u
cd "$(dirname "$0")/.." || exit 9
export PYTHONIOENCODING=utf-8
export GELU_DATA=tests/.selftest-data
TS() { date +%Y%m%d-%H%M%S; }
declare -a JOBS=(
  "tests/test_transform.py"
  "tests/test_transform.py --falsify"
  "tests/test_gif_real.py"
  "tests/test_gif_real.py --falsify"
  "tests/test_keymap.py --falsify"
  "tests/smoke_launch.py"
)
RUN=tests/logs/screen-batch-$(date +%Y%m%d-%H%M%S).log
: > "$RUN"
for spec in "${JOBS[@]}"; do
  set -- $spec
  f="$1"; flag="${2:-}"
  base=$(basename "$f" .py)
  log="tests/logs/${base}${flag:+-falsify}-$(TS).log"
  echo ">>> $f $flag -> $log"
  python "$f" $flag > "$log" 2>&1
  rc=$?
  echo "    rc=$rc  $(tail -1 "$log" | cut -c1-120)"
  echo "$f $flag rc=$rc log=$log" >> "$RUN"
done
echo "ALL DONE -> $RUN"
