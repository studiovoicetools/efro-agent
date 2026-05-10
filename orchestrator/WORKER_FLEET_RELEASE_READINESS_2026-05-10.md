# EFRO Worker Fleet Release Readiness — 2026-05-10

Status: review summary for `os/worker-fleet-controller-20260510`.
Mode: no push, no merge, no deploy, no live queue mutation.

## Purpose

This document summarizes the Worker Fleet Controller safety branch before any merge or push decision.

## Safety layers added

1. Task schema validation.
2. File ownership / overlap checks.
3. Fleet dry-run status output.
4. Guarded candidate queue apply.
5. HOLD-safe queue apply behavior.
6. Smart owner-only detection.
7. Regression candidate files.
8. Gatekeeper target for the fleet controller worktree.
9. Queue restore guard.
10. Non-zero exit code for blocked queue apply.
11. Promotion / push preflight guard.
12. Worker execution guard.
13. Post-worker diff / commit guard.
14. End-to-end self-test guard.

## Safety rules

The controller must not push, merge, deploy, publish, or mutate production state by default.

Queue writes require:

- `EFRO_FLEET_ENABLE_QUEUE_WRITE=true`
- `EFRO_FLEET_OWNER_APPROVED=true`

Promotion/push checks require:

- `EFRO_FLEET_OWNER_APPROVED_PUSH=true`

Commit checks require:

- `EFRO_FLEET_OWNER_APPROVED_COMMIT=true`

Without explicit approvals, the controller returns HOLD with non-zero exit code.

## Tested behavior

Validated during branch work:

- Self-test returns GO.
- Normal dry-run remains GO for existing done tasks.
- Invalid `.env` allowed path returns HOLD.
- Overlap candidates return HOLD.
- Queue apply without env approval returns HOLD and non-zero exit code.
- Restore without env approval returns HOLD.
- Promotion check without owner push approval returns HOLD.
- Execution check blocks missing worktree.
- Commit check blocks non-committable done task.
- Base runtime queue remained unchanged during tests.

## Current readiness decision

Review readiness: GO.

Merge readiness: HOLD until owner reviews branch and final pre-merge command set is green.

Push readiness: HOLD until owner explicitly approves push/promotion and the promotion check passes.

## Final pre-merge gates

Run before merge or push:

```bash
cd /opt/efro-agent-worker-fleet-controller-20260510
python3 -m py_compile \
  orchestrator/task_schema.py \
  orchestrator/task_locks.py \
  orchestrator/worker_fleet_controller.py \
  gatekeeper/efro_gatekeeper.py
PYTHONPATH="$PWD/orchestrator" python3 orchestrator/worker_fleet_controller.py --self-test
PYTHONPATH="$PWD/orchestrator" python3 orchestrator/worker_fleet_controller.py --promotion-check || true
git status --short
git -C /opt/efro-agent status --short

