#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/opt/efro-agent")
TASKS_JSON = ROOT / "orchestrator/tasks.json"
RESULTS_DIR = ROOT / "orchestrator/worker-results"
STATUS_MD = ROOT / "orchestrator/SAFE_CODING_WORKER_STATUS.md"

ALLOWED_REPOS = {"efro", "efro-widget", "efro-shopify", "efro-brain"}
OWNER_ONLY = {"push", "main promotion", "deploy", "billing activation", "destructive cleanup", "paid provider live gate"}

REQUIRED = ["id", "repo", "worktree", "status", "allowed_files", "forbidden_files", "required_gates"]
KNOWN_QUARANTINE = {
    "efro-widget-widget-gemini-tts-lipsync-20260508": "old unverified Gemini/LipSync worktree",
}

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def run(cmd: list[str], cwd: Path | None = None, timeout: int = 180) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception as exc:
        return 1, f"ERROR: {exc}"

def load_tasks() -> list[dict[str, Any]]:
    if not TASKS_JSON.exists():
        return []
    data = json.loads(TASKS_JSON.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []

def save_tasks(tasks: list[dict[str, Any]]) -> None:
    backup = TASKS_JSON.with_suffix(f".json.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    TASKS_JSON.rename(backup)
    TASKS_JSON.write_text(json.dumps(tasks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def wt_path(repo: str, worktree: str) -> Path:
    return ROOT / "repos" / f"{repo}-{worktree}"

def safe_rel(path: str) -> bool:
    p = Path(path)
    s = path.replace("\\", "/")
    return not p.is_absolute() and ".." not in p.parts and not s.startswith(".env") and "/.env" not in s and "secret" not in s.lower()

def matches(path: str, patterns: list[str]) -> bool:
    p = path.replace("\\", "/")
    return any(p == x or p.startswith(x.rstrip("/") + "/") for x in patterns)

def git_info(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "clean": False, "branch": "", "head": "", "dirty": []}
    _, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], path)
    _, head = run(["git", "rev-parse", "--short", "HEAD"], path)
    _, raw = run(["git", "status", "--porcelain"], path)
    dirty = [x for x in raw.splitlines() if x.strip() and not x.startswith("ERROR")]
    return {"exists": True, "clean": len(dirty) == 0, "branch": branch, "head": head, "dirty": dirty[:30]}

def legal_gate(path: Path) -> tuple[bool, str]:
    files = [path / "src/app/impressum/page.tsx", path / "src/app/datenschutz/page.tsx"]
    bad = re.compile(r"\[|Platzhalter|studiovoicetools|Derin|index:\s*false|follow:\s*false|noindex|HIER|TODO", re.I)
    hits = []
    for f in files:
        if f.exists():
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if bad.search(line):
                    hits.append(f"{f.relative_to(path)}:{i}:{line.strip()}")
    return (len(hits) == 0, "\n".join(hits[:20]))

def claims_gate(path: Path) -> tuple[bool, str]:
    bad = re.compile(r"(live billing|self-serve billing live|full production|full lipsync|fully production-proven|billing is live)", re.I)
    roots = [path / "src/app", path / "docs", path / "README.md"]
    hits = []
    for root in roots:
        files = [root] if root.is_file() else list(root.rglob("*")) if root.exists() else []
        for f in files:
            if f.is_file() and f.suffix in {".ts", ".tsx", ".md", ".txt"}:
                for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if bad.search(line):
                        hits.append(f"{f.relative_to(path)}:{i}:{line.strip()}")
    return (len(hits) == 0, "\n".join(hits[:20]))

def run_gate(gate: str, path: Path) -> tuple[bool, str]:
    if gate == "typescript":
        tsc = path / "node_modules/typescript/bin/tsc"
        cmd = [str(tsc), "-p", "tsconfig.json", "--noEmit"] if tsc.exists() else ["npx", "tsc", "-p", "tsconfig.json", "--noEmit"]
        rc, out = run(cmd, path, 240)
        return rc == 0, out[-4000:]
    if gate in {"brain_quality_gate", "zero_cost_gate"}:
        rc, out = run(["npm", "run", "test:sales-readiness:zero-cost"], path, 600)
        return rc == 0, out[-4000:]
    if gate == "legal_gate":
        return legal_gate(path)
    if gate == "claims_gate":
        return claims_gate(path)
    if gate in {"cost_safety_gate"}:
        return True, "cost_safety_gate: no paid provider call performed by worker"
    return False, f"unsupported_gate:{gate}"

def apply_patches(task: dict[str, Any], path: Path) -> tuple[bool, list[str], str]:
    changed: list[str] = []
    allowed = task.get("allowed_files") or []
    forbidden = task.get("forbidden_files") or []
    patches = task.get("patches") or []

    for patch in patches:
        rel = str(patch.get("file", ""))
        old = patch.get("old")
        new = patch.get("new")

        if not rel or not safe_rel(rel):
            return False, changed, f"unsafe_file:{rel}"
        if not matches(rel, allowed):
            return False, changed, f"file_not_allowed:{rel}"
        if matches(rel, forbidden):
            return False, changed, f"file_forbidden:{rel}"
        if not isinstance(old, str) or not isinstance(new, str):
            return False, changed, f"bad_patch_text:{rel}"

        target = path / rel
        if not target.exists():
            return False, changed, f"file_missing:{rel}"

        text = target.read_text(encoding="utf-8")
        count = text.count(old)
        if count != 1:
            return False, changed, f"old_text_occurrences:{rel}:{count}"

        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        changed.append(rel)

    return True, changed, "patches_applied"

def rollback(path: Path, files: list[str]) -> None:
    for rel in files:
        run(["git", "checkout", "--", rel], path, 60)

def process_task(task: dict[str, Any]) -> dict[str, Any]:
    tid = str(task.get("id", "UNKNOWN"))
    repo = str(task.get("repo", ""))
    worktree = str(task.get("worktree", ""))
    mode = str(task.get("execution_mode", "preflight"))
    approved = bool(task.get("owner_approved_execution", False))
    gates = task.get("required_gates") or []

    result = {"task_id": tid, "repo": repo, "worktree": worktree, "mode": mode, "status": "HOLD", "evidence": [], "blockers": [], "commit": ""}

    if str(task.get("status", "")) == "done":
        result["status"] = "DONE"
        result["commit"] = str(task.get("last_commit", ""))
        result["evidence"].append(str(task.get("note", "completed")))
        return result

    for field in REQUIRED:
        if not task.get(field):
            result["blockers"].append(f"missing:{field}")

    if repo not in ALLOWED_REPOS:
        result["blockers"].append(f"repo_not_allowed:{repo}")

    key = f"{repo}-{worktree}"
    if key in KNOWN_QUARANTINE:
        result["status"] = "NO-GO"
        result["blockers"].append(f"quarantine:{KNOWN_QUARANTINE[key]}")
        return result

    path = wt_path(repo, worktree)
    gi = git_info(path)
    result["evidence"].append(f"exists={gi['exists']}")
    result["evidence"].append(f"branch={gi['branch']}")
    result["evidence"].append(f"head={gi['head']}")
    result["evidence"].append(f"clean={gi['clean']}")

    if not gi["exists"]:
        result["blockers"].append("worktree_missing")
    if gi["branch"] == "main":
        result["blockers"].append("refuse_main_worktree")
    if not gi["clean"] and mode != "preflight":
        result["blockers"].append("worktree_dirty_before_execution")

    if result["blockers"]:
        return result

    if mode == "preflight" or not approved:
        result["status"] = "READY_FOR_WORKER" if task.get("status") in {"active", "ready_for_worker", "approved"} else "HOLD"
        if not approved:
            result["evidence"].append("execution_not_owner_approved")
        return result

    if mode != "patch_gate_commit":
        result["blockers"].append(f"unsupported_execution_mode:{mode}")
        return result

    ok, changed, msg = apply_patches(task, path)
    result["evidence"].append(msg)
    if not ok:
        result["blockers"].append(msg)
        return result

    gate_outputs = []
    for gate in gates:
        gok, gout = run_gate(str(gate), path)
        gate_outputs.append(f"{gate}:{'PASS' if gok else 'FAIL'}")
        if not gok:
            rollback(path, changed)
            result["status"] = "NO-GO"
            result["blockers"].append(f"gate_failed:{gate}")
            result["evidence"].append("\n".join(gate_outputs))
            result["evidence"].append(gout)
            return result

    msg = str(task.get("commit_message") or f"worker: {tid}")
    run(["git", "add", *changed], path, 60)
    rc, out = run(["git", "commit", "-m", msg], path, 120)
    if rc != 0:
        rollback(path, changed)
        result["blockers"].append("commit_failed")
        result["evidence"].append(out)
        return result

    _, head = run(["git", "rev-parse", "--short", "HEAD"], path)
    task["status"] = "done"
    task["last_commit"] = head
    task["completed_at"] = now()

    result["status"] = "GO"
    result["commit"] = head
    result["evidence"].append("changed=" + ",".join(changed))
    result["evidence"].append("gates=" + ",".join(gate_outputs))
    return result

def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks()
    results = []

    for task in tasks:
        if not isinstance(task, dict):
            continue
        res = process_task(task)
        results.append(res)
        (RESULTS_DIR / f"{res['task_id']}.safe-worker-v1.json").write_text(json.dumps(res, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if any(r.get("status") == "GO" for r in results):
        save_tasks(tasks)

    lines = [
        "# EFRO Safe Coding Worker Status",
        "",
        f"Generated: {now()}",
        "",
        "Mode: V1 controlled patch/gate/commit. No pushes. No main worktree. Owner approval required per task.",
        "",
        "| Task | Status | Repo | Worktree | Commit | Blockers |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['task_id']} | {r['status']} | {r['repo']} | {r['worktree']} | {r.get('commit','')} | {', '.join(r.get('blockers', []))} |")
    STATUS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(STATUS_MD)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
