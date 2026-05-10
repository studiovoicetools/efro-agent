#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent")
REPOS_ROOT = ROOT / "repos"
PROPOSAL = ROOT / "orchestrator/worker-results/cleanup-proposal-v1.json"
TASKS_JSON = ROOT / "orchestrator/tasks.json"
OUT_MD = ROOT / "orchestrator/CLEANUP_DRY_RUN_STATUS.md"
OUT_JSON = ROOT / "orchestrator/worker-results/cleanup-dry-run-v1.json"

BASE_REPOS = {"efro", "efro-widget", "efro-brain", "efro-shopify"}

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def run(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip()
    except Exception as exc:
        return 1, f"ERROR: {exc}"

def active_worktrees() -> set[str]:
    if not TASKS_JSON.exists():
        return set()
    data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
    active = set()
    for task in data:
        if task.get("status") in {"active", "ready_for_worker", "approved", "running"}:
            repo = str(task.get("repo", ""))
            worktree = str(task.get("worktree", ""))
            if repo and worktree:
                active.add(f"{repo}-{worktree}")
    return active

def validate_candidate(row: dict, active: set[str]) -> dict:
    name = row.get("name", "")
    repo = row.get("repo", "")
    path = REPOS_ROOT / name
    blockers = []

    if not name:
        blockers.append("missing_name")

    if name in BASE_REPOS:
        blockers.append("base_repo_refused")

    if name in active:
        blockers.append("active_task_refused")

    if not path.exists():
        blockers.append("path_missing")

    if path.exists() and not str(path.resolve()).startswith(str(REPOS_ROOT.resolve())):
        blockers.append("path_outside_repos_root")

    if path.exists():
        rc, inside = run(["git", "rev-parse", "--is-inside-work-tree"], path)
        if rc != 0 or inside != "true":
            blockers.append("not_git_worktree")

        _, status = run(["git", "status", "--porcelain"], path)
        dirty = [line for line in status.splitlines() if line.strip()]
        if dirty:
            blockers.append(f"dirty:{len(dirty)}")

        rc, _ = run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], path)
        if rc != 0:
            blockers.append("not_merged_into_origin_main")

    return {
        "name": name,
        "repo": repo,
        "path": str(path),
        "branch": row.get("branch", ""),
        "head": row.get("head", ""),
        "safe_for_owner_cleanup": len(blockers) == 0,
        "blockers": blockers,
        "dry_run_command": f"git worktree remove {path}" if len(blockers) == 0 else "",
    }

def main() -> int:
    if not PROPOSAL.exists():
        raise SystemExit(f"Missing proposal: {PROPOSAL}")

    proposal = json.loads(PROPOSAL.read_text(encoding="utf-8"))
    selected = proposal.get("selected", [])
    active = active_worktrees()

    results = [validate_candidate(row, active) for row in selected]
    safe = [r for r in results if r["safe_for_owner_cleanup"]]
    blocked = [r for r in results if not r["safe_for_owner_cleanup"]]

    payload = {
        "generated": now(),
        "mode": "dry_run_only_no_delete",
        "selected_count": len(selected),
        "safe_count": len(safe),
        "blocked_count": len(blocked),
        "results": results,
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# EFRO Cleanup Dry Run Status",
        "",
        f"Generated: {now()}",
        "",
        "Mode: V1 dry-run only. No delete. No reset. No clean. No push.",
        "",
        f"Selected candidates: {len(selected)}",
        f"Safe for owner-approved cleanup: {len(safe)}",
        f"Blocked: {len(blocked)}",
        "",
        "## Results",
        "",
        "| # | Name | Repo | Head | Safe | Blockers |",
        "|---:|---|---|---|---|---|",
    ]

    for i, r in enumerate(results, 1):
        lines.append(
            f"| {i} | {r['name']} | {r['repo']} | {r['head']} | "
            f"{r['safe_for_owner_cleanup']} | {', '.join(r['blockers'])} |"
        )

    lines += [
        "",
        "## Dry-run commands",
        "",
    ]

    for r in safe:
        lines.append(f"- {r['dry_run_command']}")

    lines += [
        "",
        "No cleanup has been performed.",
        "The owner must explicitly approve before any worktree removal command is executed.",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(OUT_MD)
    print(OUT_JSON)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
