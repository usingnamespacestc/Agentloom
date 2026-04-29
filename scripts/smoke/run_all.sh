#!/usr/bin/env bash
# Run every smoke script in this directory in order, report
# summary at the end. Runs sequentially because each script
# creates + tears down its own chatflow; parallel would race on
# the workspace toggle and other shared state.
#
# Usage:
#   bash scripts/smoke/run_all.sh              # all scripts
#   bash scripts/smoke/run_all.sh 01 02        # only matching ids
#   AGENTLOOM_BACKEND=http://other:8000 bash scripts/smoke/run_all.sh
#   AGENTLOOM_SMOKE_PROVIDER=ark AGENTLOOM_SMOKE_MODEL=foo bash scripts/smoke/run_all.sh
#
# Exit code 0 only if every script PASSes.

set -u
cd "$(dirname "$0")/../.." || exit 2

# Activate the agentloom conda env so httpx + agentloom imports
# resolve. If the user already has it activated this is a no-op.
source ~/miniconda3/etc/profile.d/conda.sh 2>/dev/null
conda activate agentloom 2>/dev/null

# Filter scripts by the args (positional, match anywhere in name).
filter_args=("$@")
match() {
  local name="$1"
  if [ ${#filter_args[@]} -eq 0 ]; then return 0; fi
  for f in "${filter_args[@]}"; do
    [[ "$name" == *"$f"* ]] && return 0
  done
  return 1
}

scripts=()
while IFS= read -r path; do
  scripts+=("$path")
done < <(find "$(dirname "$0")" -maxdepth 1 -name "[0-9]*.py" -o -name "combo_*.py" | sort)

passed=0
failed=0
failed_names=()
total_start=$(date +%s)

for path in "${scripts[@]}"; do
  name=$(basename "$path")
  if ! match "$name"; then continue; fi
  echo
  echo "════════════════════════════════════════════════════════════"
  echo "  Running: $name"
  echo "════════════════════════════════════════════════════════════"
  if python "$path"; then
    passed=$((passed + 1))
  else
    failed=$((failed + 1))
    failed_names+=("$name")
  fi
done

total_elapsed=$(( $(date +%s) - total_start ))

echo
echo "════════════════════════════════════════════════════════════"
echo "  Summary: $passed passed, $failed failed in ${total_elapsed}s"
echo "════════════════════════════════════════════════════════════"
if [ $failed -gt 0 ]; then
  for n in "${failed_names[@]}"; do echo "  ✗ $n"; done
  exit 1
fi
