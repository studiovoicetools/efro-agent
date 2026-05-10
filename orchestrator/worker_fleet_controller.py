#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from task_locks import find_overlaps
from task_schema import as_list, validate_task, validate_tasks

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


def run_cmd(cmd: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except Exception as exc:
        return 1, f"ERROR: {exc}"


def git_clean(path: Path) -> tuple[bool, str]:
    rc, out = run_cmd(["git", "status", "--porcelain"], path)
    if rc != 0:
        return False, out
    return out == "", out


def promotion_preflight() -> tuple[bool, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []

    source_clean, source_status = git_clean(SOURCE_ROOT)
    if not source_clean:
        blockers.append(f"source worktree is dirty: {source_status or 'dirty'}")

    runtime_clean, runtime_status = git_clean(RUNTIME_ROOT)
    if not runtime_clean:
        blockers.append(f"runtime repo is dirty: {runtime_status or 'dirty'}")

    if os.environ.get("EFRO_FLEET_OWNER_APPROVED_PUSH") != "true":
        blockers.append("push/promotion blocked: EFRO_FLEET_OWNER_APPROVED_PUSH must be true")

    compile_targets = [
        "orchestrator/task_schema.py",
        "orchestrator/task_locks.py",
        "orchestrator/worker_fleet_controller.py",
        "gatekeeper/efro_gatekeeper.py",
    ]

    for rel in compile_targets:
        target = SOURCE_ROOT / rel
        if not target.exists():
            warnings.append(f"compile target missing: {rel}")
            continue
        rc, out = run_cmd(["python3", "-m", "py_compile", str(target)], SOURCE_ROOT)
        if rc != 0:
            blockers.append(f"compile failed for {rel}: {out}")

    return not blockers, blockers, warnings


def task_by_id(tasks: list[dict[str, Any]], task_id: str) -> dict[str, Any] | None:
    for task in tasks:
        if str(task.get("id", "")) == task_id:
            return task
    return None


def task_worktree_path(task: dict[str, Any]) -> Path:
    repo = str(task.get("repo", ""))
    worktree = str(task.get("worktree", ""))
    return RUNTIME_ROOT / "repos" / f"{repo}-{worktree}"


def path_matches(path: str, patterns: list[str]) -> bool:
    clean = path.replace("\\", "/").rstrip("/")
    for pattern in patterns:
        p = str(pattern).replace("\\", "/").rstrip("/")
        if clean == p or clean.startswith(p + "/"):
            return True
    return False


def changed_files(path: Path) -> tuple[bool, list[str], str]:
    if not path.exists():
        return False, [], f"worktree missing: {path}"

    outputs: list[str] = []
    for cmd in [
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]:
        rc, out = run_cmd(cmd, path)
        if rc != 0:
            return False, [], out
        outputs.extend([line.strip() for line in out.splitlines() if line.strip()])

    return True, sorted(set(outputs)), ""


def execution_preflight(task: dict[str, Any], all_tasks: list[dict[str, Any]]) -> tuple[bool, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []

    validation = validate_task(task)
    blockers.extend(validation.blockers)
    warnings.extend(validation.warnings)

    status = str(task.get("status", "")).lower()
    if status not in {"ready", "preflight", "review"}:
        blockers.append(f"task status is not executable: {status}")

    overlaps = find_overlaps(all_tasks)
    task_id = str(task.get("id", ""))
    for item in overlaps:
        parts = item.split()
        if len(parts) >= 3 and task_id in {parts[0], parts[2]}:
            blockers.append(f"active overlap blocks execution: {item}")

    wt = task_worktree_path(task)
    if not wt.exists():
        blockers.append(f"worktree missing: {wt}")
    else:
        is_clean, detail = git_clean(wt)
        if not is_clean:
            blockers.append(f"task worktree dirty before execution: {detail or 'dirty'}")

    return not blockers, blockers, warnings


def diff_preflight(task: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []

    wt = task_worktree_path(task)
    ok, files, err = changed_files(wt)
    if not ok:
        blockers.append(err)
        return False, blockers, warnings

    allowed = as_list(task.get("allowed_files"))
    forbidden = as_list(task.get("forbidden_files"))

    if not files:
        warnings.append("no changed files detected")

    for file_path in files:
        if not path_matches(file_path, allowed):
            blockers.append(f"changed file outside allowed_files: {file_path}")
        if path_matches(file_path, forbidden):
            blockers.append(f"changed file matches forbidden_files: {file_path}")

    return not blockers, blockers, warnings


def auto_safe_preflight(task: dict[str, Any], all_tasks: list[dict[str, Any]]) -> tuple[bool, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    _ok, bs, ws = execution_preflight(task, all_tasks)
    blockers += bs
    warnings += ws
    repo = str(task.get("repo", ""))
    allowed = as_list(task.get("allowed_files"))
    if repo not in {"efro", "efro-widget", "efro-shopify", "efro-brain"}:
        blockers.append(f"auto-safe blocked: repo not eligible: {repo}")
    if len(allowed) != 1:
        blockers.append("auto-safe blocked: exactly one allowed file required")
    risky = [".env", "src/app/api", "api", "server.js", "package.json", "package-lock.json"]
    for item in allowed:
        if path_matches(item, risky):
            blockers.append(f"auto-safe blocked: risky path: {item}")
    warnings.append("auto-safe check only: no files changed")
    return not blockers, blockers, warnings


def safe_patch_plan_preflight(plan: dict[str, Any], tasks: list[dict[str, Any]]) -> tuple[bool, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    task = task_by_id(tasks, str(plan.get("task_id", "")))
    file_path = str(plan.get("file", ""))
    reps = plan.get("replacements", [])
    if not task:
        blockers.append("safe patch plan blocked: task not found")
    else:
        _ok, bs, ws = auto_safe_preflight(task, tasks)
        blockers += bs
        warnings += ws
        if not path_matches(file_path, as_list(task.get("allowed_files"))):
            blockers.append(f"safe patch plan blocked: file outside allowed_files: {file_path}")
    if not isinstance(reps, list) or not reps:
        blockers.append("safe patch plan blocked: replacements missing")
    if isinstance(reps, list) and len(reps) > 12:
        blockers.append("safe patch plan blocked: too many replacements")
    warnings.append("safe patch plan check only: no files changed")
    return not blockers, blockers, warnings


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_self_test() -> tuple[bool, list[str]]:
    failures: list[str] = []
    candidates = SOURCE_ROOT / "orchestrator/test-candidates"

    valid_path = candidates / "valid-single-task.json"
    invalid_path = candidates / "invalid-unsafe-env.json"
    overlap_path = candidates / "overlap-tasks.json"

    for path in [valid_path, invalid_path, overlap_path]:
        if not path.exists():
            failures.append(f"missing test candidate: {path}")

    if failures:
        return False, failures

    before = sha256_file(TASKS_JSON)

    saved_env = {
        "EFRO_FLEET_ENABLE_QUEUE_WRITE": os.environ.get("EFRO_FLEET_ENABLE_QUEUE_WRITE"),
        "EFRO_FLEET_OWNER_APPROVED": os.environ.get("EFRO_FLEET_OWNER_APPROVED"),
        "EFRO_FLEET_OWNER_APPROVED_PUSH": os.environ.get("EFRO_FLEET_OWNER_APPROVED_PUSH"),
    }

    for key in saved_env:
        os.environ.pop(key, None)

    try:
        valid_tasks = load_tasks(valid_path)
        invalid_tasks = load_tasks(invalid_path)
        overlap_tasks = load_tasks(overlap_path)

        valid_result = validate_tasks(valid_tasks)
        if not all(item.ok for item in valid_result.values()):
            failures.append("valid candidate did not validate")

        invalid_result = validate_tasks(invalid_tasks)
        if all(item.ok for item in invalid_result.values()):
            failures.append("invalid env candidate unexpectedly validated")

        overlap_result = find_overlaps(overlap_tasks)
        if not overlap_result:
            failures.append("overlap candidate did not produce overlap HOLD")

        try:
            write_tasks_with_backup(valid_tasks)
            failures.append("write_tasks_with_backup unexpectedly wrote without env approval")
        except RuntimeError:
            pass

        try:
            restore_queue_from_backup(TASKS_JSON.with_suffix(".json.bak-fleet-NOTREAL"))
            failures.append("restore_queue_from_backup unexpectedly restored without env approval")
        except RuntimeError:
            pass

        execution_ok, execution_blockers, _warnings = execution_preflight(valid_tasks[0], valid_tasks)
        if execution_ok or not any("worktree missing" in item for item in execution_blockers):
            failures.append("execution preflight did not block missing test worktree")

        promotion_ok, promotion_blockers, _warnings = promotion_preflight()
        if promotion_ok or not any("EFRO_FLEET_OWNER_APPROVED_PUSH" in item for item in promotion_blockers):
            failures.append("promotion preflight did not block missing push approval")

    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    after = sha256_file(TASKS_JSON)
    if before != after:
        failures.append("runtime tasks.json checksum changed during self-test")

    return not failures, failures


def safe_patch_plan_cli(args: argparse.Namespace) -> int:
    plan = json.loads(Path(args.safe_patch_plan).read_text(encoding="utf-8"))
    tasks = load_tasks(Path(args.candidate).resolve()) if args.candidate else load_tasks()
    _ok, blockers, warnings = safe_patch_plan_preflight(plan, tasks)
    lines = ["# EFRO Worker Fleet Controller Status", "", f"Generated: {now()}", "", "Mode: V1 safe-patch-plan. No file changes. No push. No deploy.", "", "| Check | Status | Detail |", "|---|---|---|"]
    lines += [f"| Safe patch plan | HOLD | {x} |" for x in blockers]
    if not blockers:
        lines.append("| Safe patch plan | GO | plan is valid for safe runner lane |")
    lines += [f"| Warning | REVIEW | {x} |" for x in warnings]
    STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(STATUS_MD)
    return 0 if not blockers else 10


def auto_safe_cli(args: argparse.Namespace) -> int:
    tid = args.auto_safe_check
    tasks = load_tasks(Path(args.candidate).resolve()) if args.candidate else load_tasks()
    task = task_by_id(tasks, tid)
    blockers: list[str] = []
    warnings: list[str] = []
    if task:
        _ok, blockers, warnings = auto_safe_preflight(task, tasks)
    else:
        blockers.append(f"task not found: {tid}")
    lines = ["# EFRO Worker Fleet Controller Status", "", f"Generated: {now()}", "", "Mode: V1 auto-safe-check. No file changes. No push. No deploy.", "", "| Check | Status | Detail |", "|---|---|---|"]
    lines += [f"| Auto-safe | HOLD | {x} |" for x in blockers]
    if not blockers:
        lines.append("| Auto-safe | GO | task is eligible for safe autonomous lane classification |")
    lines += [f"| Warning | REVIEW | {x} |" for x in warnings]
    STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(STATUS_MD)
    return 0 if not blockers else 9


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EFRO Worker Fleet Controller V1")
    parser.add_argument("--candidate", default="", help="Optional candidate tasks JSON file to validate.")
    parser.add_argument("--apply", action="store_true", help="Apply candidate tasks only with explicit environment approval.")
    parser.add_argument("--restore-backup", default="", help="Restore runtime queue from a fleet-created backup with explicit environment approval.")
    parser.add_argument("--promotion-check", action="store_true", help="Run pre-promotion checks only. No push. No merge. No deploy.")
    parser.add_argument("--execution-check", default="", help="Validate that one task is safe to execute. No worker is run.")
    parser.add_argument("--diff-check", default="", help="Validate changed files for one task after worker run. No commit.")
    parser.add_argument("--commit-check", default="", help="Validate that one task is safe to commit. No commit is created.")
    parser.add_argument("--self-test", action="store_true", help="Run controller safety regression tests. No queue mutation.")
    parser.add_argument("--auto-safe-check", default="", help="Classify whether one task is eligible for safe autonomous execution. No file changes.")
    parser.add_argument("--safe-patch-plan", default="", help="Validate a safe patch plan JSON. No file changes.")
    return parser.parse_args()


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    args = parse_args()

    if args.safe_patch_plan:
        return safe_patch_plan_cli(args)

    if args.auto_safe_check:
        return auto_safe_cli(args)

    if args.self_test:
        ok, failures = run_self_test()
        lines = [
            "# EFRO Worker Fleet Controller Status",
            "",
            f"Generated: {now()}",
            "",
            "Mode: V1 self-test. No queue mutation. No push. No deploy.",
            "",
            "| Test | Status | Detail |",
            "|---|---|---|",
        ]

        if failures:
            for failure in failures:
                lines.append(f"| Self-test | HOLD | {failure} |")
        else:
            lines.append("| Self-test | GO | all controller safety regressions passed |")

        result = {
            "generated_at": now(),
            "mode": "self-test",
            "ok": ok,
            "failures": failures,
        }

        STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result_path = RESULTS_DIR / "worker-fleet-controller-v1.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        print(STATUS_MD)
        print(result_path)
        return 0 if ok else 8

    if args.execution_check or args.diff_check or args.commit_check:
        task_id = args.execution_check or args.diff_check or args.commit_check
        candidate_path = Path(args.candidate).resolve() if args.candidate else None
        tasks = load_tasks(candidate_path) if candidate_path else load_tasks()
        task = task_by_id(tasks, task_id)

        lines = [
            "# EFRO Worker Fleet Controller Status",
            "",
            f"Generated: {now()}",
            "",
            "Mode: V1 worker-execution-guard. No worker run. No commit. No push.",
            f"Task source: `{candidate_path if candidate_path else TASKS_JSON}`",
            "",
            "| Check | Status | Detail |",
            "|---|---|---|",
        ]

        blockers: list[str] = []
        warnings: list[str] = []

        if not task:
            blockers.append(f"task not found: {task_id}")
        elif args.execution_check:
            _ok, blockers, warnings = execution_preflight(task, tasks)
        elif args.diff_check:
            _ok, blockers, warnings = diff_preflight(task)
        else:
            _ok, blockers, warnings = diff_preflight(task)
            if not blockers:
                if os.environ.get("EFRO_FLEET_OWNER_APPROVED_COMMIT") != "true":
                    blockers.append("commit blocked: EFRO_FLEET_OWNER_APPROVED_COMMIT must be true")
                required = as_list(task.get("required_gates"))
                if not required:
                    blockers.append("commit blocked: required_gates missing")
                if str(task.get("status", "")).lower() not in {"ready", "preflight", "review"}:
                    blockers.append(f"commit blocked: task status is not committable: {task.get('status')}")

        if blockers:
            for item in blockers:
                lines.append(f"| Execution | HOLD | {item} |")
        else:
            lines.append("| Execution | GO | guard checks passed |")

        for item in warnings:
            lines.append(f"| Warning | REVIEW | {item} |")

        result = {
            "generated_at": now(),
            "mode": "worker-execution-guard",
            "check_type": "execution" if args.execution_check else ("diff" if args.diff_check else "commit"),
            "task_id": task_id,
            "ok": not blockers,
            "blockers": blockers,
            "warnings": warnings,
        }

        STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result_path = RESULTS_DIR / "worker-fleet-controller-v1.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        print(STATUS_MD)
        print(result_path)
        if args.commit_check and blockers:
            return 7
        return 0 if not blockers else 6

    if args.promotion_check:
        ok, blockers, warnings = promotion_preflight()
        lines = [
            "# EFRO Worker Fleet Controller Status",
            "",
            f"Generated: {now()}",
            "",
            "Mode: V1 promotion-check. No push. No merge. No deploy.",
            "",
            "| Check | Status | Detail |",
            "|---|---|---|",
        ]

        if blockers:
            for item in blockers:
                lines.append(f"| Promotion | HOLD | {item} |")
        else:
            lines.append("| Promotion | GO | all required promotion checks passed |")

        if warnings:
            for item in warnings:
                lines.append(f"| Warning | REVIEW | {item} |")

        result = {
            "generated_at": now(),
            "mode": "promotion-check",
            "ok": ok,
            "blockers": blockers,
            "warnings": warnings,
        }

        STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result_path = RESULTS_DIR / "worker-fleet-controller-v1.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        print(STATUS_MD)
        print(result_path)
        return 0 if ok else 5

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
    if args.apply and not result.get("applied"):
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
