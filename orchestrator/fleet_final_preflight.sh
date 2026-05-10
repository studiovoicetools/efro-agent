#!/usr/bin/env bash
set -euo pipefail

WT="${EFRO_FLEET_WORKTREE:-/opt/efro-agent-worker-fleet-controller-20260510}"
BASE="${EFRO_FLEET_BASE:-/opt/efro-agent}"

cd "$WT"

echo "== EFRO Fleet Final Preflight =="
echo "worktree=$WT"
echo "base=$BASE"
echo

echo "== 1) Python compile gates =="
python3 -m py_compile \
  orchestrator/task_schema.py \
  orchestrator/task_locks.py \
  orchestrator/worker_fleet_controller.py \
  gatekeeper/efro_gatekeeper.py

echo "== 2) Controller self-test =="
PYTHONPATH="$WT/orchestrator" python3 orchestrator/worker_fleet_controller.py --self-test
sed -n '1,220p' orchestrator/WORKER_FLEET_CONTROLLER_STATUS.md

echo "== 3) Runtime queue dry-run =="
PYTHONPATH="$WT/orchestrator" python3 orchestrator/worker_fleet_controller.py
sed -n '1,220p' orchestrator/WORKER_FLEET_CONTROLLER_STATUS.md

echo "== 4) Promotion check =="
set +e
PYTHONPATH="$WT/orchestrator" python3 orchestrator/worker_fleet_controller.py --promotion-check
PROMO_RC="$?"
set -e
sed -n '1,220p' orchestrator/WORKER_FLEET_CONTROLLER_STATUS.md

if [ "${EFRO_FLEET_OWNER_APPROVED_PUSH:-}" = "true" ]; then
  test "$PROMO_RC" = "0"
else
  test "$PROMO_RC" = "5"
  echo "Promotion correctly HOLD without EFRO_FLEET_OWNER_APPROVED_PUSH=true"
fi

echo "== 5) Git cleanliness =="
echo "--- worktree ---"
git status --short
test -z "$(git status --short)"

echo "--- base ---"
git -C "$BASE" status --short
test -z "$(git -C "$BASE" status --short)"

echo "== FINAL PREFLIGHT RESULT: GO =="
