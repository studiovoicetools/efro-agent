#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_REPOS = {"efro", "efro-widget", "efro-shopify", "efro-brain"}
ALLOWED_STATUS = {"ready", "preflight", "hold", "done", "review"}

OWNER_ONLY_WORDS = {
    "push",
    "deploy",
    "publish",
    "main promotion",
    "destructive cleanup",
    "billing activation",
    "pricing edit",
    "landingpage edit",
    "paid provider live gate",
}

REQUIRED_FIELDS = [
    "id",
    "repo",
    "worktree",
    "status",
    "allowed_files",
    "forbidden_files",
    "required_gates",
    "success_condition",
    "stop_condition",
]


@dataclass
class TaskValidationResult:
    ok: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def safe_rel_path(value: str) -> bool:
    p = Path(str(value))
    text = str(value).replace("\\", "/")
    lowered = text.lower()

    if not text.strip():
        return False
    if p.is_absolute() or ".." in p.parts:
        return False
    if lowered.startswith(".env") or "/.env" in lowered:
        return False
    if "secret" in lowered or "token" in lowered or "key" in lowered:
        return False

    return True


def overlaps(left: str, right: str) -> bool:
    a = left.replace("\\", "/").rstrip("/")
    b = right.replace("\\", "/").rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def validate_task(task: dict[str, Any]) -> TaskValidationResult:
    blockers: list[str] = []
    warnings: list[str] = []

    status = str(task.get("status", "")).strip().lower()
    required_fields = REQUIRED_FIELDS
    if status == "done":
        required_fields = ["id", "repo", "worktree", "status", "allowed_files", "forbidden_files", "required_gates"]

    for field_name in required_fields:
        if field_name not in task:
            blockers.append(f"missing required field: {field_name}")

    task_id = str(task.get("id", "")).strip()
    repo = str(task.get("repo", "")).strip()
    worktree = str(task.get("worktree", "")).strip()

    if not task_id:
        blockers.append("empty task id")
    if repo not in ALLOWED_REPOS:
        blockers.append(f"repo not allowed: {repo}")
    if status and status not in ALLOWED_STATUS:
        blockers.append(f"unsupported status: {status}")
    if not worktree or worktree == "main":
        blockers.append("task must use a non-main worktree")

    allowed_files = as_list(task.get("allowed_files"))
    forbidden_files = as_list(task.get("forbidden_files"))
    required_gates = as_list(task.get("required_gates"))

    if not allowed_files:
        blockers.append("allowed_files must not be empty")
    if not forbidden_files:
        warnings.append("forbidden_files is empty")
    if not required_gates:
        blockers.append("required_gates must not be empty")

    for rel in allowed_files:
        if not safe_rel_path(rel):
            blockers.append(f"unsafe allowed path: {rel}")

    for rel in forbidden_files:
        p = Path(str(rel))
        text = str(rel).replace("\\", "/")
        if not text.strip() or p.is_absolute() or ".." in p.parts:
            blockers.append(f"unsafe forbidden path: {rel}")

    for allowed in allowed_files:
        for forbidden in forbidden_files:
            if overlaps(allowed, forbidden):
                blockers.append(f"allowed/forbidden overlap: {allowed} <-> {forbidden}")

    combined_text = " ".join(
        str(v).lower()
        for v in task.values()
        if not isinstance(v, (list, dict))
    )

    if status != "done" and any(word in combined_text for word in OWNER_ONLY_WORDS):
        if task.get("owner_approved_execution") is not True:
            blockers.append("owner-only action referenced without owner_approved_execution=true")

    return TaskValidationResult(ok=not blockers, blockers=blockers, warnings=warnings)


def validate_tasks(tasks: list[dict[str, Any]]) -> dict[str, TaskValidationResult]:
    return {
        str(task.get("id", f"task-{i}")): validate_task(task)
        for i, task in enumerate(tasks)
    }
