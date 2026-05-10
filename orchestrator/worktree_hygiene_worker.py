#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/opt/efro-agent")
REPOS_ROOT = ROOT / "repos"
TASKS_JSON = ROOT / "orchestrator/tasks.json"
STATUS_MD = ROOT / "orchestrator/WORKTREE_HYGIENE_STATUS.md"
RESULT_JSON = ROOT / "orchestrator/worker-results/worktree-hygiene-v1.json"

BASE_REPOS = {"efro", "efro-widget", "efro-brain", "efro-shopify"}
OWNER_ONLY = [
    "delete worktree",
    "git reset --hard",
    "git clean",
    "remove branch",
    "destructive cleanup",
]

RISK_WORDS = [
    "old",
    "lipsync",
    "gemini-tts-lipsync",
    "admin-chat",
    "remove-final",
    "backup",
    "bak",
]

DONE_HINTS = [
    "legal-shopify-review-20260508",
    "brain-answer-quality-gates-20260508",
    "gemini-voice-provider-20260509",
]

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

def load_tasks() -> list[dict[str, Any]]:
    if not TASKS_JSON.exists():
        return []
    try:
        data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []

def active_task_worktrees() -> set[str]:
    active = set()
    for task in load_tasks():
        if not isinstance(task, dict):
            continue
        if task.get("status") in {"active", "ready_for_worker", "approved", "running"}:
            repo = str(task.get("repo", ""))
            worktree = str(task.get("worktree", ""))
            if repo and worktree:
                active.add(f"{repo}-{worktree}")
    return active

def is_git_repo(path: Path) -> bool:
    rc, out = run(["git", "rev-parse", "--is-inside-work-tree"], path)
    return rc == 0 and out == "true"

def git_info(path: Path) -> dict[str, Any]:
    _, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    _, head = run(["git", "rev-parse", "--short", "HEAD"], path)
    _, status = run(["git", "status", "--porcelain"], path)
    _, upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], path)
    _, log = run(["git", "log", "--oneline", "-1"], path)

    dirty = [line for line in status.splitlines() if line.strip() and not line.startswith("ERROR")]
    return {
        "branch": branch if not branch.startswith("ERROR") else "",
        "head": head if not head.startswith("ERROR") else "",
        "upstream": upstream if not upstream.startswith("ERROR") else "",
        "clean": len(dirty) == 0,
        "dirty_count": len(dirty),
        "dirty": dirty[:20],
        "last_commit": log if not log.startswith("ERROR") else "",
    }

def main_parent(repo_name: str) -> str:
    for base in sorted(BASE_REPOS, key=len, reverse=True):
        if repo_name == base:
            return base
        if repo_name.startswith(base + "-"):
            return base
    return ""

def merged_into_origin_main(path: Path) -> bool:
    rc, _ = run(["git", "merge-base", "--is-ancestor", "HEAD", "origin/main"], path)
    return rc == 0

def classify(name: str, path: Path, info: dict[str, Any], active: set[str]) -> tuple[str, str]:
    lower = name.lower()

    if name in BASE_REPOS:
        if info["clean"]:
            return "KEEP", "base repo clean"
        return "HOLD", "base repo dirty"

    if name in active:
        return "KEEP_ACTIVE", "referenced by active task"

    if any(hint in name for hint in DONE_HINTS):
        if info["clean"]:
            return "DONE_KEEP_UNTIL_OWNER_CLEANUP", "known completed task worktree"
        return "HOLD", "known completed task but dirty"

    if any(word in lower for word in RISK_WORDS):
        if info["clean"]:
            return "QUARANTINE_REVIEW", "risk keyword in worktree name"
        return "NO_GO_DIRTY_QUARANTINE", "risk keyword and dirty"

    if not info["clean"]:
        return "HOLD_DIRTY", "dirty worktree"

    if merged_into_origin_main(path):
        return "REMOVE_CANDIDATE_OWNER_ONLY", "clean and merged into origin/main"

    return "REVIEW", "clean but not proven merged"

def discover() -> list[dict[str, Any]]:
    active = active_task_worktrees()
    rows = []

    if not REPOS_ROOT.exists():
        return rows

    for path in sorted(REPOS_ROOT.iterdir(), key=lambda p: p.name):
        if not path.is_dir():
            continue

        parent = main_parent(path.name)
        if not parent:
            continue

        if not is_git_repo(path):
            rows.append({
                "name": path.name,
                "path": str(path),
                "repo": parent,
                "status": "UNKNOWN",
                "reason": "not a git worktree",
            })
            continue

        info = git_info(path)
        status, reason = classify(path.name, path, info, active)

        rows.append({
            "name": path.name,
            "path": str(path),
            "repo": parent,
            "branch": info["branch"],
            "head": info["head"],
            "upstream": info["upstream"],
            "clean": info["clean"],
            "dirty_count": info["dirty_count"],
            "dirty": info["dirty"],
            "last_commit": info["last_commit"],
            "status": status,
            "reason": reason,
        })

    return rows

def write_status(rows: list[dict[str, Any]]) -> None:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1

    lines = [
        "# EFRO Worktree Hygiene Status",
        "",
        f"Generated: {now()}",
        "",
        "Mode: V1 read-only inventory. No delete. No reset. No clean. No push.",
        "",
        "## Owner-only actions",
        "",
    ]

    for item in OWNER_ONLY:
        lines.append(f"- {item}")

    lines += [
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]

    for status, count in sorted(counts.items()):
        lines.append(f"| {status} | {count} |")

    lines += [
        "",
        "## Inventory",
        "",
        "| Name | Repo | Branch | Head | Clean | Status | Reason |",
        "|---|---|---|---|---|---|---|",
    ]

    for row in rows:
        lines.append(
            f"| {row.get('name','')} | {row.get('repo','')} | {row.get('branch','')} | "
            f"{row.get('head','')} | {row.get('clean','')} | {row.get('status','')} | {row.get('reason','')} |"
        )

    lines += [
        "",
        "## Rule",
        "",
        "No worktree may be removed unless this worker marks it as REMOVE_CANDIDATE_OWNER_ONLY and the owner explicitly approves cleanup.",
        "",
    ]

    STATUS_MD.write_text("\n".join(lines), encoding="utf-8")
    RESULT_JSON.write_text(json.dumps({"generated": now(), "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")

def main() -> int:
    rows = discover()
    write_status(rows)
    print(STATUS_MD)
    print(RESULT_JSON)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
