#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent")
SOURCE = ROOT / "orchestrator/worker-results/worktree-hygiene-v1.json"
OUT_MD = ROOT / "orchestrator/CLEANUP_PROPOSAL_STATUS.md"
OUT_JSON = ROOT / "orchestrator/worker-results/cleanup-proposal-v1.json"

BATCH_LIMIT = 10

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"Missing hygiene inventory: {SOURCE}")

    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    rows = data.get("rows", [])

    candidates = [
        row for row in rows
        if row.get("status") == "REMOVE_CANDIDATE_OWNER_ONLY"
        and row.get("clean") is True
        and row.get("name")
    ]

    candidates = sorted(candidates, key=lambda r: (r.get("repo", ""), r.get("name", "")))
    selected = candidates[:BATCH_LIMIT]

    result = {
        "generated": now(),
        "mode": "proposal_only_no_delete",
        "total_remove_candidates": len(candidates),
        "batch_limit": BATCH_LIMIT,
        "selected": selected,
        "owner_action_required": True,
    }

    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# EFRO Cleanup Proposal Status",
        "",
        f"Generated: {now()}",
        "",
        "Mode: V1 proposal only. No delete. No reset. No clean. No push.",
        "",
        f"Total REMOVE_CANDIDATE_OWNER_ONLY: {len(candidates)}",
        f"Proposed batch size: {len(selected)}",
        "",
        "## Proposed Batch 1",
        "",
        "| # | Name | Repo | Branch | Head | Reason |",
        "|---:|---|---|---|---|---|",
    ]

    for i, row in enumerate(selected, 1):
        lines.append(
            f"| {i} | {row.get('name','')} | {row.get('repo','')} | "
            f"{row.get('branch','')} | {row.get('head','')} | {row.get('reason','')} |"
        )

    lines += [
        "",
        "## Owner decision required",
        "",
        "No cleanup has been performed.",
        "",
        "Approve only if every listed worktree may be removed.",
        "",
        "Dirty, REVIEW, QUARANTINE, and base repos are excluded.",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(OUT_MD)
    print(OUT_JSON)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
