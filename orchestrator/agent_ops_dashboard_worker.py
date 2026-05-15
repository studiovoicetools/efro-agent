#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent")
ORCH = ROOT / "orchestrator"
RESULTS = ORCH / "worker-results"

HYGIENE_MD = ORCH / "WORKTREE_HYGIENE_STATUS.md"
HOLD_MD = ORCH / "HOLD_DIRTY_TRIAGE_STATUS.md"
PROPOSAL_MD = ORCH / "CLEANUP_PROPOSAL_STATUS.md"
DRY_RUN_MD = ORCH / "CLEANUP_DRY_RUN_STATUS.md"
REVIEW_PROOF_JSON = RESULTS / "review-proof-v1.json"

OUT_MD = ORCH / "AGENT_OPS_DASHBOARD_STATUS.md"
OUT_JSON = RESULTS / "agent-ops-dashboard-v1.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def read_json(path: Path) -> dict:
    text = read_text(path)
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def first_match(pattern: str, text: str, default: str = "") -> str:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else default


def generated_at(text: str) -> str:
    return first_match(r"^Generated:\s*(.+)$", text, "missing")


def parse_status_counts(hygiene: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for line in hygiene.splitlines():
        match = re.match(r"^\|\s*([A-Z_]+)\s*\|\s*(\d+)\s*\|$", line)
        if match:
            counts[match.group(1)] = int(match.group(2))
    return counts


def parse_int(label: str, text: str, default: int = 0) -> int:
    value = first_match(rf"^{re.escape(label)}:\s*(\d+)\s*$", text)
    return int(value) if value.isdigit() else default


def parse_top_hold_dirty(hold: str) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for line in hold.splitlines():
        if not line.startswith("| "):
            continue
        parts = [p.strip() for p in line.strip().strip("|").split("|")]
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        rows.append(
            {
                "rank": int(parts[0]),
                "name": parts[1],
                "repo": parts[2],
                "branch": parts[3],
                "dirty_files": int(parts[4]) if parts[4].isdigit() else 0,
                "topic": parts[5],
                "blockers": parts[6],
            }
        )
    return rows[:5]


def main() -> int:
    hygiene = read_text(HYGIENE_MD)
    hold = read_text(HOLD_MD)
    proposal = read_text(PROPOSAL_MD)
    dry_run = read_text(DRY_RUN_MD)
    review_proof = read_json(REVIEW_PROOF_JSON)

    counts = parse_status_counts(hygiene)
    top_hold_dirty = parse_top_hold_dirty(hold)
    review_counts = review_proof.get("counts", {}) if isinstance(review_proof.get("counts", {}), dict) else {}

    payload = {
        "generated": now(),
        "mode": "read_only_agent_ops_dashboard",
        "sources": {
            "worktree_hygiene": generated_at(hygiene),
            "hold_dirty_triage": generated_at(hold),
            "cleanup_proposal": generated_at(proposal),
            "cleanup_dry_run": generated_at(dry_run),
            "review_proof": str(review_proof.get("generated", "missing")),
        },
        "status_counts": counts,
        "cleanup": {
            "remove_candidate_owner_only": parse_int("Total REMOVE_CANDIDATE_OWNER_ONLY", proposal),
            "proposed_batch_size": parse_int("Proposed batch size", proposal),
            "dry_run_selected": parse_int("Selected candidates", dry_run),
            "safe_for_owner_cleanup": parse_int("Safe for owner-approved cleanup", dry_run),
            "blocked": parse_int("Blocked", dry_run),
        },
        "top_hold_dirty": top_hold_dirty,
        "review_proof": {
            "review_clean_count": int(review_proof.get("review_clean_count", 0) or 0),
            "proven_count": int(review_proof.get("proven_count", 0) or 0),
            "counts": review_counts,
        },
        "boundary": "P2 automatic cleanup is complete; remaining groups are protected owner-review groups.",
    }

    RESULTS.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# EFRO Agent Ops Dashboard Status",
        "",
        f"Generated: {payload['generated']}",
        "",
        "Mode: read-only generated dashboard. No delete. No reset. No clean. No merge. No patch.",
        "",
        "## Source freshness",
        "",
        "| Source | Generated |",
        "|---|---|",
        f"| Worktree hygiene | {payload['sources']['worktree_hygiene']} |",
        f"| HOLD_DIRTY triage | {payload['sources']['hold_dirty_triage']} |",
        f"| Cleanup proposal | {payload['sources']['cleanup_proposal']} |",
        f"| Cleanup dry-run | {payload['sources']['cleanup_dry_run']} |",
        f"| REVIEW proof | {payload['sources']['review_proof']} |",
        "",
        "## Worktree status counts",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]

    for key in sorted(counts):
        lines.append(f"| {key} | {counts[key]} |")

    review = payload["review_proof"]
    review_counts_out = review["counts"]
    cleanup = payload["cleanup"]
    lines += [
        "",
        "## REVIEW proof summary",
        "",
        f"- Clean REVIEW items inspected: {review['review_clean_count']}",
        f"- Proven evidence candidates: {review['proven_count']}",
        f"- PATCH_EQUIVALENT_TO_MAIN: {review_counts_out.get('PATCH_EQUIVALENT_TO_MAIN', 0)}",
        f"- CLEAN_BUT_NOT_PROVEN: {review_counts_out.get('CLEAN_BUT_NOT_PROVEN', 0)}",
        "",
        "## Cleanup boundary",
        "",
        f"- REMOVE_CANDIDATE_OWNER_ONLY: {cleanup['remove_candidate_owner_only']}",
        f"- Proposed batch size: {cleanup['proposed_batch_size']}",
        f"- Dry-run selected candidates: {cleanup['dry_run_selected']}",
        f"- Safe for owner-approved cleanup: {cleanup['safe_for_owner_cleanup']}",
        f"- Blocked cleanup candidates: {cleanup['blocked']}",
        "",
        "P2 automatic cleanup is complete. Remaining groups are protected review groups, not safe-delete groups.",
        "",
        "## Top HOLD_DIRTY owner-review candidates",
        "",
        "| # | Name | Repo | Branch | Dirty files | Topic | Blockers |",
        "|---:|---|---|---|---:|---|---|",
    ]

    for item in top_hold_dirty:
        lines.append(
            f"| {item['rank']} | {item['name']} | {item['repo']} | {item['branch']} | "
            f"{item['dirty_files']} | {item['topic']} | {item['blockers']} |"
        )

    lines += [
        "",
        "## No-go rules",
        "",
        "- Do not run automatic cleanup against protected groups.",
        "- Do not merge dirty worktrees wholesale.",
        "- Do not use skipped live tests as positive evidence.",
        "- Do not reset, clean or delete protected worktrees without explicit owner decision.",
        "- Rebuild selected ideas only from current main branches and rerun gates.",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(OUT_MD)
    print(OUT_JSON)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
