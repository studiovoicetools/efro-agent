#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent")
TASKS = ROOT / "orchestrator/tasks.json"
QUEUE_STATUS = ROOT / "orchestrator/EFRO_QUEUE_STATUS.md"
GATEKEEPER = ROOT / "gatekeeper/efro_gatekeeper.py"
GATE_STATUS = ROOT / "gatekeeper/EFRO_AUTOPILOT_STATUS.md"

REPOS = {
    "efro": ROOT / "repos/efro",
    "efro-widget": ROOT / "repos/efro-widget",
    "efro-brain": ROOT / "repos/efro-brain",
    "efro-shopify": ROOT / "repos/efro-shopify",
}

def run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
        )
        return p.returncode, p.stdout.strip()
    except Exception as exc:
        return 1, f"ERROR: {exc}"

def git_status(path: Path) -> tuple[str, str, str]:
    if not path.exists():
        return "missing", "", ""
    _, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    _, head = run(["git", "rev-parse", "--short", "HEAD"], path)
    _, dirty = run(["git", "status", "--porcelain"], path)
    return branch, head, dirty

def worktree_path(repo: str, worktree: str) -> Path:
    return ROOT / "repos" / f"{repo}-{worktree}"

def load_tasks() -> list[dict]:
    return json.loads(TASKS.read_text(encoding="utf-8"))

def write_status(tasks: list[dict]) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# EFRO Queue Status",
        "",
        f"Generated: {now}",
        "",
        "| Task | Queue | Repo | Worktree | Git | Next |",
        "|---|---|---|---|---|---|",
    ]

    for task in tasks:
        repo = task.get("repo", "")
        wt = task.get("worktree", "")
        path = worktree_path(repo, wt)
        branch, head, dirty = git_status(path)

        if branch == "missing":
            git = "missing"
        elif dirty:
            git = f"HOLD dirty={len([x for x in dirty.splitlines() if x.strip()])} branch={branch} head={head}"
        else:
            git = f"clean branch={branch} head={head}"

        lines.append(
            f"| {task.get('id')} | {task.get('status')} | {repo} | {wt} | {git} | {task.get('next_action','')} |"
        )

    lines += ["", "## Gatekeeper Snapshot", ""]
    if GATE_STATUS.exists():
        lines.append(GATE_STATUS.read_text(encoding="utf-8"))
    else:
        lines.append("Gatekeeper status missing.")

    QUEUE_STATUS.write_text("\n".join(lines) + "\n", encoding="utf-8")

def main() -> int:
    if GATEKEEPER.exists():
        run(["/usr/bin/python3", str(GATEKEEPER)])

    tasks = load_tasks()
    write_status(tasks)
    print(QUEUE_STATUS)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
