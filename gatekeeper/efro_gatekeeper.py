#!/usr/bin/env python3
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent/repos")
OUT = Path("/opt/efro-agent/gatekeeper/EFRO_AUTOPILOT_STATUS.md")

TARGETS = [
    {
        "area": "Brain gates",
        "path": ROOT / "efro-brain-brain-answer-quality-gates-20260508",
        "expected": "83f8a10",
        "missing": "HOLD",
        "note": "Brain answer-quality gate worktree.",
    },
    {
        "area": "Legal",
        "path": ROOT / "efro-legal-shopify-review-20260508",
        "expected": "",
        "missing": "HOLD",
        "note": "Legal finalisation worktree.",
    },
    {
        "area": "Gemini old",
        "path": ROOT / "efro-widget-widget-gemini-tts-lipsync-20260508",
        "forced": "NO-GO",
        "note": "Old unverified Gemini/LipSync worktree. Do not merge.",
    },
    {
        "area": "Gemini minimal",
        "path": ROOT / "efro-widget-widget-gemini-tts-minimal-20260508",
        "expected": "",
        "missing": "HOLD",
        "note": "Clean Gemini restart path, if created.",
    },
]

STATIC = [
    ("Billing", "DEFERRED", "Paddle later. No live billing claims.", "Ignore until Paddle phase."),
    ("MCP / Infra", "HOLD", "No maintenance window active.", "Do not touch unless explicitly opened."),
]

def git(path: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=str(path),
            text=True,
            stderr=subprocess.STDOUT,
            timeout=25,
        ).strip()
    except Exception as exc:
        return f"ERROR: {exc}"

def inspect(t: dict) -> tuple[str, str, str, str]:
    area = t["area"]
    path = Path(t["path"])

    if t.get("forced"):
        return area, t["forced"], t.get("note", ""), "Quarantine or restart cleanly."

    if not path.exists():
        return area, t.get("missing", "HOLD"), f"Missing: {path}", "Create/finish only if still in scope."

    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    head = git(path, "rev-parse", "--short", "HEAD")
    dirty = git(path, "status", "--porcelain")

    if branch.startswith("ERROR") or head.startswith("ERROR") or dirty.startswith("ERROR"):
        return area, "HOLD", f"git error branch={branch} head={head}", "Inspect manually."

    dirty_count = len([x for x in dirty.splitlines() if x.strip()])
    if dirty_count:
        return area, "HOLD", f"dirty files={dirty_count}, branch={branch}, head={head}", "Finish, revert, or commit after gates."

    expected = t.get("expected", "")
    if expected and not head.startswith(expected):
        return area, "HOLD", f"clean branch={branch}, head={head}, expected={expected}", "Review commit identity."

    return area, "GO", f"clean branch={branch}, head={head}. {t.get('note','')}", "Review/Merge when owner approves."

def main() -> int:
    rows = [inspect(t) for t in TARGETS]
    rows.extend(STATIC)

    next_actions = []
    for area, status, _proof, action in rows:
        if status in {"HOLD", "NO-GO"} and len(next_actions) < 3:
            next_actions.append(f"{area}: {action}")
    while len(next_actions) < 3:
        next_actions.append("No further blocking action detected.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# EFRO Autopilot Status",
        "",
        f"Generated: {now}",
        "",
        "| Bereich | Status | Beweis | Nächste Aktion |",
        "|---|---|---|---|",
    ]

    for area, status, proof, action in rows:
        proof = str(proof).replace("|", "/")
        action = str(action).replace("|", "/")
        lines.append(f"| {area} | {status} | {proof} | {action} |")

    lines += ["", "## Top 3 Next Actions", ""]
    for i, item in enumerate(next_actions, 1):
        lines.append(f"{i}. {item}")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(OUT)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
