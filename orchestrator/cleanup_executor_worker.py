#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent")
REPOS_ROOT = ROOT / "repos"
DRY_RUN = ROOT / "orchestrator/worker-results/cleanup-dry-run-v1.json"
OUT_MD = ROOT / "orchestrator/CLEANUP_EXECUTOR_STATUS.md"
OUT_JSON = ROOT / "orchestrator/worker-results/cleanup-executor-v1.json"

APPROVAL_TOKEN = "I_APPROVE_CLEANUP_BATCH_1_20260509"
BASE_REPOS = {"efro", "efro-widget", "efro-brain", "efro-shopify"}

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> tuple[int, str]:
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

def validate(path: Path, name: str) -> list[str]:
    blockers = []

    if name in BASE_REPOS:
        blockers.append("base_repo_refused")

    if not path.exists():
        blockers.append("path_missing")
        return blockers

    if not str(path.resolve()).startswith(str(REPOS_ROOT.resolve())):
        blockers.append("path_outside_repos_root")

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

    return blockers

def main() -> int:
    if not DRY_RUN.exists():
        raise SystemExit(f"Missing dry-run evidence: {DRY_RUN}")

    dry = json.loads(DRY_RUN.read_text(encoding="utf-8"))
    candidates = [
        row for row in dry.get("results", [])
        if row.get("safe_for_owner_cleanup") is True
    ]

    approved = os.environ.get("EFRO_CLEANUP_APPROVED") == APPROVAL_TOKEN
    mode = "EXECUTE_OWNER_APPROVED" if approved else "LOCKED_NO_DELETE"

    results = []
    removed = 0
    blocked = 0

    for row in candidates:
        name = row.get("name", "")
        path = Path(row.get("path", ""))
        blockers = validate(path, name)

        item = {
            "name": name,
            "path": str(path),
            "head": row.get("head", ""),
            "blockers": blockers,
            "removed": False,
            "output": "",
        }

        if blockers:
            blocked += 1
            results.append(item)
            continue

        if approved:
            repo_base = REPOS_ROOT / str(row.get("repo", ""))
            rc, out = run(["git", "worktree", "remove", str(path)], repo_base, 60)
            item["output"] = out
            if rc == 0:
                item["removed"] = True
                removed += 1
            else:
                item["blockers"].append(f"remove_failed:{rc}")
                blocked += 1

        results.append(item)

    payload = {
        "generated": now(),
        "mode": mode,
        "approved": approved,
        "candidate_count": len(candidates),
        "removed_count": removed,
        "blocked_count": blocked,
        "results": results,
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# EFRO Cleanup Executor Status",
        "",
        f"Generated: {now()}",
        "",
        f"Mode: {mode}",
        "",
        f"Approved: {approved}",
        f"Candidates: {len(candidates)}",
        f"Removed: {removed}",
        f"Blocked: {blocked}",
        "",
        "## Results",
        "",
        "| # | Name | Removed | Blockers |",
        "|---:|---|---|---|",
    ]

    for i, row in enumerate(results, 1):
        lines.append(
            f"| {i} | {row['name']} | {row['removed']} | {', '.join(row['blockers'])} |"
        )

    lines += [
        "",
        "## Safety",
        "",
        "Without EFRO_CLEANUP_APPROVED matching the exact approval token, this executor performs no deletion.",
        "This locked run is only an installation and validation step.",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(OUT_MD)
    print(OUT_JSON)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
