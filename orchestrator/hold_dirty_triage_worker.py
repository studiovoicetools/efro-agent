#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent")
REPOS_ROOT = ROOT / "repos"
HYGIENE_JSON = ROOT / "orchestrator/worker-results/worktree-hygiene-v1.json"
OUT_MD = ROOT / "orchestrator/HOLD_DIRTY_TRIAGE_STATUS.md"
OUT_JSON = ROOT / "orchestrator/worker-results/hold-dirty-triage-v1.json"


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


def topic_for(row: dict) -> tuple[int, str]:
    repo = str(row.get("repo", ""))
    text = " ".join([
        str(row.get("name", "")),
        str(row.get("branch", "")),
        str(row.get("last_commit", "")),
    ]).lower()

    if repo == "efro-brain":
        if any(w in text for w in ["supabase", "catalog", "orchestrator", "chat", "retrieval", "routing", "runtime", "finalize", "eval", "sellable"]):
            return 1, "brain runtime/retrieval/eval"
        return 2, "brain other"

    if repo == "efro":
        if any(w in text for w in ["claim", "audit", "entitlement", "foundation", "preview", "xlsx", "nonshopify"]):
            return 3, "efro platform/entitlements"
        return 4, "efro other"

    if repo == "efro-shopify":
        return 5, "shopify compliance/review"

    if repo == "efro-widget":
        if any(w in text for w in ["voice", "tts", "browser", "greeting", "fallback", "sync", "gemini"]):
            return 6, "widget voice/tts"
        return 7, "widget other"

    return 9, "other"


def safe_path(raw: str) -> Path | None:
    try:
        p = Path(raw).resolve()
        if not str(p).startswith(str(REPOS_ROOT.resolve())):
            return None
        return p
    except Exception:
        return None


def inspect(row: dict) -> dict:
    path = safe_path(str(row.get("path", "")))
    rank, topic = topic_for(row)

    item = {
        "name": row.get("name", ""),
        "repo": row.get("repo", ""),
        "branch": row.get("branch", ""),
        "head": row.get("head", ""),
        "path": row.get("path", ""),
        "dirty_count": row.get("dirty_count", 0),
        "dirty": row.get("dirty", []),
        "topic": topic,
        "topic_rank": rank,
        "blockers": [],
    }

    if path is None:
        item["blockers"].append("path_outside_repos_root")
        return item

    if not path.exists():
        item["blockers"].append("path_missing")
        return item

    rc, status = run(["git", "status", "--porcelain"], path)
    item["status_porcelain"] = status
    if rc != 0:
        item["blockers"].append("git_status_failed")

    rc, diffstat = run(["git", "diff", "--stat"], path)
    item["diffstat"] = diffstat
    if rc != 0:
        item["blockers"].append("git_diff_stat_failed")

    rc, untracked = run(["git", "ls-files", "--others", "--exclude-standard"], path)
    item["untracked_files"] = untracked.splitlines() if rc == 0 and untracked else []

    return item


def main() -> int:
    data = json.loads(HYGIENE_JSON.read_text(encoding="utf-8"))
    rows = [r for r in data.get("rows", []) if r.get("status") == "HOLD_DIRTY"]

    items = [inspect(r) for r in rows]
    items.sort(key=lambda x: (x["topic_rank"], -int(x.get("dirty_count") or 0), str(x["name"])))

    top = items[:5]

    payload = {
        "generated": now(),
        "mode": "read_only_hold_dirty_triage",
        "hold_dirty_count": len(items),
        "top_owner_review_count": len(top),
        "top_owner_review": top,
        "items": items,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    counts = {}
    for item in items:
        counts[item["topic"]] = counts.get(item["topic"], 0) + 1

    lines = [
        "# EFRO HOLD_DIRTY Triage Status",
        "",
        f"Generated: {payload['generated']}",
        "",
        "Mode: read-only. No delete. No reset. No clean. No merge. No patch.",
        "",
        f"HOLD_DIRTY items: {len(items)}",
        f"Top owner-review candidates: {len(top)}",
        "",
        "## Counts by topic",
        "",
        "| Topic | Count |",
        "|---|---:|",
    ]

    for topic, count in sorted(counts.items()):
        lines.append(f"| {topic} | {count} |")

    lines += [
        "",
        "## Top owner-review candidates",
        "",
        "| # | Name | Repo | Branch | Dirty files | Topic | Blockers |",
        "|---:|---|---|---|---:|---|---|",
    ]

    for i, item in enumerate(top, 1):
        blockers = ", ".join(item.get("blockers", []))
        lines.append(f"| {i} | {item['name']} | {item['repo']} | {item['branch']} | {item['dirty_count']} | {item['topic']} | {blockers} |")

    lines += [
        "",
        "## Rule",
        "",
        "These are owner-review candidates, not cleanup candidates.",
        "Read the actual diff and run relevant gates before copying, merging, archiving or marking obsolete.",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(OUT_MD)
    print(OUT_JSON)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
