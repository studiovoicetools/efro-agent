#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
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


def load_tasks(path: Path = TASKS_JSON) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def queue_write_approved() -> None:
    if os.environ.get("EFRO_FLEET_ENABLE_QUEUE_WRITE") != "true":
        raise RuntimeError("Queue write blocked: EFRO_FLEET_ENABLE_QUEUE_WRITE must be true.")
    if os.environ.get("EFRO_FLEET_OWNER_APPROVED") != "true":
        raise RuntimeError("Queue write blocked: EFRO_FLEET_OWNER_APPROVED must be true.")


def write_tasks_with_backup(tasks: list[dict[str, Any]], target: Path = TASKS_JSON) -> Path:
    queue_write_approved()
    backup = target.with_suffix(f".json.bak-fleet-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target, backup)
    target.write_text(json.dumps(tasks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return backup


def restore_queue_from_backup(backup: Path, target: Path = TASKS_JSON) -> Path:
    queue_write_approved()

    backup = backup.resolve()
    target = target.resolve()
    expected_prefix = str(target.with_suffix(".json")) + ".bak-fleet-"

    if not backup.exists():
        raise RuntimeError(f"Restore blocked: backup not found: {backup}")
    if not str(backup).startswith(expected_prefix):
        raise RuntimeError("Restore blocked: backup path is not a fleet queue backup.")

    pre_restore = target.with_suffix(f".json.bak-pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(target, pre_restore)
    shutil.copy2(backup, target)
    return pre_restore


def status_label(ok: bool, blockers: list[str]) -> str:
    if ok:
        return "GO"
    if blockers:
        return "HOLD"
    return "REVIEW"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EFRO Worker Fleet Controller V1")
    parser.add_argument("--candidate", default="", help="Optional candidate tasks JSON file to validate.")
    parser.add_argument("--apply", action="store_true", help="Apply candidate tasks only with explicit environment approval.")
    parser.add_argument("--restore-backup", default="", help="Restore runtime queue from a fleet-created backup with explicit environment approval.")
    return parser.parse_args()


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    args = parse_args()

    if args.restore_backup:
        restore_source = Path(args.restore_backup).resolve()
        lines = [
            "# EFRO Worker Fleet Controller Status",
            "",
            f"Generated: {now()}",
            "",
            "Mode: V1 queue-restore. No push. No deploy.",
            f"Restore source: `{restore_source}`",
            "",
            "## Queue restore",
            "",
        ]
        result: dict[str, Any] = {
            "generated_at": now(),
            "mode": "queue-restore",
            "restore_source": str(restore_source),
            "restored": False,
            "pre_restore_backup": "",
            "error": "",
        }
        try:
            pre_restore = restore_queue_from_backup(restore_source)
            result["restored"] = True
            result["pre_restore_backup"] = str(pre_restore)
            lines.append(f"Restored queue. Pre-restore backup: `{pre_restore}`")
        except RuntimeError as exc:
            result["error"] = str(exc)
            lines.append(f"HOLD: {exc}")

        STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result_path = RESULTS_DIR / "worker-fleet-controller-v1.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(STATUS_MD)
        print(result_path)
        return 0 if result["restored"] else 4

    candidate_path = Path(args.candidate).resolve() if args.candidate else None
    tasks = load_tasks(candidate_path) if candidate_path else load_tasks()
    validations = validate_tasks(tasks)
    overlaps = find_overlaps(tasks)
    overlap_task_ids: set[str] = set()
    for item in overlaps:
        parts = item.split()
        if len(parts) >= 3 and parts[1] == "overlaps":
            overlap_task_ids.add(parts[0])
            overlap_task_ids.add(parts[2])

    can_apply = args.apply and candidate_path is not None and not overlaps and all(v.ok for v in validations.values())
    mode = "candidate-apply" if args.apply else "dry-run"
    source_label = str(candidate_path) if candidate_path else str(TASKS_JSON)

    lines: list[str] = [
        "# EFRO Worker Fleet Controller Status",
        "",
        f"Generated: {now()}",
        "",
        f"Mode: V1 {mode}. No push. No deploy.",
        f"Task source: `{source_label}`",
        "",
        "| Task | Status | Blockers | Warnings |",
        "|---|---|---|---|",
    ]

    result: dict[str, Any] = {
        "generated_at": now(),
        "mode": mode,
        "task_source": source_label,
        "task_count": len(tasks),
        "overlaps": overlaps,
        "applied": False,
        "backup": "",
        "tasks": {},
    }

    for task_id, validation in validations.items():
        task_blockers = list(validation.blockers)
        if task_id in overlap_task_ids:
            task_blockers.append("active file ownership overlap detected")
        label = status_label(validation.ok and not task_blockers, task_blockers)
        blockers = "<br>".join(task_blockers) if task_blockers else ""
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

    if args.apply:
        lines += ["", "## Candidate apply", ""]
        if not candidate_path:
            lines.append("HOLD: --apply requires --candidate.")
        elif overlaps:
            lines.append("HOLD: file overlaps detected.")
        elif not all(v.ok for v in validations.values()):
            lines.append("HOLD: validation blockers detected.")
        elif not can_apply:
            lines.append("HOLD: apply preconditions not met.")
        else:
            try:
                backup = write_tasks_with_backup(tasks)
                result["applied"] = True
                result["backup"] = str(backup)
                lines.append(f"Applied candidate tasks with backup: `{backup}`")
            except RuntimeError as exc:
                result["applied"] = False
                result["apply_error"] = str(exc)
                lines.append(f"HOLD: {exc}")

    STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result_path = RESULTS_DIR / "worker-fleet-controller-v1.json"
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(STATUS_MD)
    print(result_path)

    if overlaps:
        return 2
    if any(not v.ok for v in validations.values()):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
