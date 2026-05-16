#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/opt/efro-agent")
REPOS_ROOT = ROOT / "repos"
HYGIENE_JSON = ROOT / "orchestrator/worker-results/worktree-hygiene-v1.json"
OUT_MD = ROOT / "orchestrator/REVIEW_PROOF_STATUS.md"
OUT_JSON = ROOT / "orchestrator/worker-results/review-proof-v1.json"

BASE_REPOS = {"efro", "efro-widget", "efro-brain", "efro-shopify"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(cmd: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip()
    except Exception as exc:
        return 1, f"ERROR: {exc}"


def safe_path(raw: str) -> Path | None:
    try:
        p = Path(raw).resolve()
        if not str(p).startswith(str(REPOS_ROOT.resolve())):
            return None
        return p
    except Exception:
        return None


def load_rows() -> list[dict[str, Any]]:
    data = json.loads(HYGIENE_JSON.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    return rows if isinstance(rows, list) else []


def inspect_review(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("name", ""))
    repo = str(row.get("repo", ""))
    branch = str(row.get("branch", ""))
    raw_path = str(row.get("path", ""))
    path = safe_path(raw_path)

    item: dict[str, Any] = {
        "name": name,
        "repo": repo,
        "branch": branch,
        "path": raw_path,
        "head": row.get("head", ""),
        "last_commit": row.get("last_commit", ""),
        "classification": "BLOCKED",
        "proof": "",
        "blockers": [],
        "read_only": True,
    }

    if repo not in BASE_REPOS:
        item["blockers"].append("repo_not_allowed")
        return item

    base = REPOS_ROOT / repo
    if not base.exists():
        item["blockers"].append("base_repo_missing")
        return item

    if path is None:
        item["blockers"].append("path_outside_repos_root")
        return item

    if not path.exists():
        item["blockers"].append("worktree_path_missing")
        return item

    rc, status = run(["git", "status", "--porcelain"], path)
    if rc != 0:
        item["blockers"].append("git_status_failed")
        item["proof"] = status
        return item

    if status.strip():
        item["classification"] = "BLOCKED"
        item["blockers"].append("worktree_not_clean")
        item["proof"] = status
        return item

    rc, full_head = run(["git", "rev-parse", "HEAD"], path)
    if rc != 0 or not full_head:
        item["blockers"].append("head_rev_parse_failed")
        item["proof"] = full_head
        return item

    item["full_head"] = full_head

    rc, main_rev = run(["git", "rev-parse", "main"], base)
    if rc != 0 or not main_rev:
        item["blockers"].append("main_rev_parse_failed")
        item["proof"] = main_rev
        return item

    item["main_head"] = main_rev

    rc, out = run(["git", "merge-base", "--is-ancestor", full_head, "main"], base)
    if rc == 0:
        item["classification"] = "PROVEN_MERGED_BY_ANCESTOR"
        item["proof"] = "HEAD is ancestor of main"
        return item

    rc, cherry = run(["git", "cherry", "main", "HEAD"], path)
    if rc == 0:
        lines = [line.strip() for line in cherry.splitlines() if line.strip()]
        if lines and all(line.startswith("-") for line in lines):
            item["classification"] = "PATCH_EQUIVALENT_TO_MAIN"
            item["proof"] = "git cherry marks all non-main commits as patch-equivalent"
            item["cherry"] = lines[:20]
            return item

        if not lines:
            item["classification"] = "CLEAN_BUT_NOT_PROVEN"
            item["proof"] = "git cherry produced no rows but ancestor check failed"
            return item

        item["classification"] = "CLEAN_BUT_NOT_PROVEN"
        item["proof"] = "git cherry found commits not patch-equivalent to main"
        item["cherry"] = lines[:20]
        return item

    item["classification"] = "CLEAN_BUT_NOT_PROVEN"
    item["proof"] = f"git cherry failed: {cherry}"
    return item


def main() -> int:
    rows = [
        row for row in load_rows()
        if row.get("status") == "REVIEW" and row.get("clean") is True
    ]

    items = [inspect_review(row) for row in rows]

    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get("classification", "UNKNOWN"))
        counts[key] = counts.get(key, 0) + 1

    proven = [
        item for item in items
        if item["classification"] in {"PROVEN_MERGED_BY_ANCESTOR", "PATCH_EQUIVALENT_TO_MAIN"}
    ]

    owner_removal_candidates = [
        {
            "name": i.get("name", ""),
            "repo": i.get("repo", ""),
            "branch": i.get("branch", ""),
            "path": i.get("path", ""),
            "classification": i.get("classification", ""),
            "proof": i.get("proof", ""),
        }
        for i in proven
    ]

    payload = {
        "generated": now(),
        "mode": "read_only_review_proof",
        "source": str(HYGIENE_JSON),
        "review_clean_count": len(rows),
        "counts": counts,
        "proven_count": len(proven),
        "owner_removal_candidate_count": len(owner_removal_candidates),
        "owner_removal_candidates": owner_removal_candidates,
        "items": items,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# EFRO REVIEW Proof Status",
        "",
        f"Generated: {payload['generated']}",
        "",
        "Mode: read-only. No delete. No reset. No clean. No merge. No push.",
        "",
        f"Clean REVIEW items inspected: {len(rows)}",
        f"Proven safe-classification count: {len(proven)}",
        "",
        "## Classification counts",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]

    for key in sorted(counts):
        lines.append(f"| {key} | {counts[key]} |")

    lines += [
        "",
        "## Proven candidates",
        "",
        "These are evidence candidates only. They are not deletion commands.",
        "",
        "| # | Name | Repo | Branch | Classification | Proof |",
        "|---:|---|---|---|---|---|",
    ]

    for i, item in enumerate(proven[:80], 1):
        lines.append(
            f"| {i} | {item['name']} | {item['repo']} | {item['branch']} | "
            f"{item['classification']} | {item['proof']} |"
        )

    lines += [
        "",
        "## Owner-removal candidates",
        "",
        f"Owner-removal evidence candidates: {len(owner_removal_candidates)}",
        "These are proposed worktree removals only after explicit owner approval.",
        "Use repo-explicit local commands only.",
        "",
        "| # | Name | Repo | Path |",
        "|---:|---|---|---|",
    ]

    for i, item in enumerate(owner_removal_candidates[:80], 1):
        lines.append(f"| {i} | {item['name']} | {item['repo']} | {item['path']} |")

    lines += [
        "",
        "## Rule",
        "",
        "This worker only proves status. It does not authorize destructive cleanup.",
        "Owner must explicitly decide before removing any worktree or branch.",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(OUT_MD)
    print(OUT_JSON)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
