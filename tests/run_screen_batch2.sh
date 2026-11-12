#!/usr/bin/env bash
# 屏幕亮着时的第二批：真机按键验收、资源占用实测、以及**换档字号**的逐屏体检。
# 为什么第二批仍串行：这几套都要抢前台（按键/抓屏），并行会让遮挡闸门点出自家窗口。
set -u
cd "$(dirname "$0")/.." || exit 9
export PYTHONIOENCODING=utf-8
export GELU_DATA=tests/.selftest-data
TS() { date +%Y%m%d-%H%M%S; }
RUN=tests/logs/screen-batch2-$(date +%Y%m%d-%H%M%S).log
: > "$RUN"
declare -a JOBS=(
  "tests/test_keymap.py|"
  "tests/test_usage.py|"
  "tests/capture_sweep.py|--font-scale 1.5"
  "tests/capture_sweep.py|--font-scale 3.0"
)
for spec in "${JOBS[@]}"; do
  f="${spec%%|*}"; flag="${spec#*|}"
  base=$(basename "$f" .py)
  tag=$(echo "${flag:-accept}" | tr -d ' -' )
  log="tests/logs/${base}-${tag}-$(TS).log"
  echo ">>> $f $flag -> $log"
  python "$f" $flag > "$log" 2>&1
  rc=$?
  echo "    rc=$rc  $(tail -1 "$log" | cut -c1-130)"
  echo "$f [$flag] rc=$rc log=$log" >> "$RUN"
done
echo "ALL DONE -> $RUN"
