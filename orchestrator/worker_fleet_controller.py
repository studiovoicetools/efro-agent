#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from task_locks import find_overlaps
from task_schema import validate_tasks

SOURCE_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path(os.environ.get("EFRO_FLEET_RUNTIME_ROOT", "/opt/efro-agent"))
TASKS_JSON = RUNTIME_ROOT / "orchestrator/tasks.json"
STATUS_MD = SOURCE_ROOT / "orchestrator/WORKER_FLEET_CONTROLLER_STATUS.md"
RESULTS_DIR = SOURCE_ROOT / "orchestrator/worker-results"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_tasks() -> list[dict[str, Any]]:
    if not TASKS_JSON.exists():
        return []
    data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def status_label(ok: bool, blockers: list[str]) -> str:
    if ok:
        return "GO"
    if blockers:
        return "HOLD"
    return "REVIEW"


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    tasks = load_tasks()
    validations = validate_tasks(tasks)
    overlaps = find_overlaps(tasks)

    lines: list[str] = [
        "# EFRO Worker Fleet Controller Status",
        "",
        f"Generated: {now()}",
        "",
        "Mode: V1 dry-run validation only. No queue mutation. No push. No deploy.",
        "",
        "| Task | Status | Blockers | Warnings |",
        "|---|---|---|---|",
    ]

    result: dict[str, Any] = {
        "generated_at": now(),
        "mode": "dry-run",
        "task_count": len(tasks),
        "overlaps": overlaps,
        "tasks": {},
    }

    for task_id, validation in validations.items():
        label = status_label(validation.ok, validation.blockers)
        blockers = "<br>".join(validation.blockers) if validation.blockers else ""
        warnings = "<br>".join(validation.warnings) if validation.warnings else ""
        lines.append(f"| {task_id} | {label} | {blockers} | {warnings} |")
        result["tasks"][task_id] = {
            "status": label,
            "blockers": validation.blockers,
            "warnings": validation.warnings,
        }

    lines += ["", "## File overlap check", ""]
    if overlaps:
        for item in overlaps:
            lines.append(f"- HOLD: {item}")
    else:
        lines.append("No active allowed_files overlaps detected.")

    STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result_path = RESULTS_DIR / "worker-fleet-controller-v1.json"
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(STATUS_MD)
    print(result_path)
    return 0 if not overlaps else 2


if __name__ == "__main__":
    raise SystemExit(main())
