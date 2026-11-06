#!/bin/bash
# Usage: sudo ./run_repeats.sh [runs]     (default 3)
RUNS=${1:-3}
cd "$(dirname "$0")"
USER_HOME=$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)
RYU="$USER_HOME/ryuenv/bin/ryu-manager"
LOG=results/repeats.log
mkdir -p results
: > "$LOG"

run_one() {                      # $1 = experiment, $2 = optional --static
  mn -c > /dev/null 2>&1
  pkill -f ryu-manager 2>/dev/null
  sleep 1
  "$RYU" --observe-links controller/diag_controller.py > /tmp/ryu.log 2>&1 &
  RYU_PID=$!
  sleep 6
  OUT=$(python3 diag/analyzer.py $1 $2 2>&1 | grep -E "RESULT|CONGESTION")
  echo "$1 $2|$OUT" >> "$LOG"
  echo "$1 $2 -> $OUT"
  kill $RYU_PID 2>/dev/null
  sleep 1
}

for i in $(seq 1 "$RUNS"); do
  echo "=== round $i of $RUNS ==="
  run_one failure
  run_one failure --static
  run_one congestion
  run_one congestion --static
done

mn -c > /dev/null 2>&1
python3 diag/summarize.py
chown -R "${SUDO_USER:-$USER}" results
