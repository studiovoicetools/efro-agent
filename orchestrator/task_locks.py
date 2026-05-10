#!/usr/bin/env python3
from __future__ import annotations

from typing import Any

ACTIVE_STATUSES = {"ready", "preflight", "review"}


def _paths(task: dict[str, Any]) -> list[str]:
    raw = task.get("allowed_files")
    if not isinstance(raw, list):
        return []
    return [str(x).replace("\\", "/").rstrip("/") for x in raw if str(x).strip()]


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def find_overlaps(tasks: list[dict[str, Any]]) -> list[str]:
    overlaps: list[str] = []

    active = [
        task for task in tasks
        if str(task.get("status", "")).lower() in ACTIVE_STATUSES
    ]

    for i, left in enumerate(active):
        for right in active[i + 1:]:
            if left.get("repo") != right.get("repo"):
                continue

            for left_path in _paths(left):
                for right_path in _paths(right):
                    if _overlaps(left_path, right_path):
                        overlaps.append(
                            f"{left.get('id')} overlaps {right.get('id')} on {left_path} <-> {right_path}"
                        )

    return overlaps
