#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent")
SOURCE = ROOT / "orchestrator/worker-results/secrets-hygiene-v1.json"
OUT_MD = ROOT / "orchestrator/SECRETS_CLASSIFIER_STATUS.md"
OUT_JSON = ROOT / "orchestrator/worker-results/secrets-classifier-v1.json"

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def classify_env_path(repo: str, path: str, tracked: bool) -> tuple[str, str]:
    lower = path.lower()

    if lower.endswith(":zone.identifier"):
        if tracked:
            return "TRACKED_ZONE_IDENTIFIER_REMOVE_CANDIDATE", "Windows zone metadata is tracked; remove from git after review."
        return "ZONE_IDENTIFIER_ARTIFACT", "Windows zone metadata artifact; cleanup candidate after worktree decision."

    if path.endswith(".env.example") or ".env.example" in path:
        if tracked:
            return "SAFE_TEMPLATE_REVIEW", "Tracked template file; verify placeholders only."
        return "OLD_WORKTREE_TEMPLATE", "Untracked template in old worktree or runtime area."

    if path in {".env", ".env.local"} or path.endswith("/.env") or path.endswith("/.env.local"):
        if tracked:
            return "ROTATE_REQUIRED_TRACKED_ENV", "Tracked env-like file; treat as critical until reviewed."
        if path.startswith("repos/"):
            return "OLD_WORKTREE_LOCAL_ENV_REVIEW", "Untracked env-like file in old worktree; review before cleanup."
        return "LOCAL_ENV_REVIEW", "Untracked local env file; verify ignored and rotate if exposed."

    if tracked:
        return "TRACKED_ENV_REVIEW", "Tracked env-like file; review required."

    return "ENV_REVIEW", "Env-like file; review required."

def classify_secret_finding(repo: str, path: str, category: str) -> tuple[str, str]:
    lower = path.lower()

    if repo == "efro-agent" and path in {"orchestrator/secrets_hygiene_worker.py", "orchestrator/secrets_classifier_worker.py"}:
        return "SCANNER_PATTERN_SELF_REFERENCE", "Scanner matched its own detection pattern; no secret value printed."

    if repo == "efro-agent" and path.startswith("orchestrator/") and path.endswith("_STATUS.md"):
        return "GENERATED_REPORT_SELF_REFERENCE", "Generated status report contains redacted classifier text; no secret value printed."

    if repo == "efro-agent" and path.startswith("orchestrator/worker-results/"):
        return "GENERATED_RESULT_SELF_REFERENCE", "Generated worker result contains redacted classifier data; no secret value printed."

    if lower.endswith(".env.example") or ".env.example" in lower:
        return "TEMPLATE_SECRET_PATTERN_REVIEW", "Template contains secret-like placeholder pattern; verify placeholder only."

    if lower.endswith(":zone.identifier"):
        return "ZONE_IDENTIFIER_SECRET_PATTERN", "Windows metadata file matched a pattern; cleanup/review artifact."

    if path.startswith("repos/"):
        return "OLD_WORKTREE_SECRET_PATTERN_REVIEW", "Secret-like pattern inside old worktree; review during worktree cleanup."

    if category in {"vercel_oidc_token", "jwt_like", "private_key", "bearer_token"}:
        return "ROTATE_REQUIRED_SECRET_CANDIDATE", "High-risk secret-like pattern; rotate if value may be valid."

    return "SECRET_PATTERN_REVIEW", "Secret-like pattern; review without printing value."

def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"Missing source: {SOURCE}")

    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    repos = data.get("results", [])

    env_items = []
    finding_items = []

    for repo_result in repos:
        repo = repo_result.get("repo", "unknown")

        for item in repo_result.get("env_files", []):
            path = item.get("path", "")
            tracked = bool(item.get("tracked", False))
            classification, action = classify_env_path(repo, path, tracked)
            env_items.append({
                "repo": repo,
                "path": path,
                "tracked": tracked,
                "classification": classification,
                "action": action,
                "content_printed": False,
            })

        for item in repo_result.get("secret_findings", []):
            path = item.get("path", "")
            line = item.get("line", "")
            category = item.get("category", "")
            classification, action = classify_secret_finding(repo, path, category)
            finding_items.append({
                "repo": repo,
                "path": path,
                "line": line,
                "category": category,
                "classification": classification,
                "action": action,
                "value_printed": False,
            })

    counts = {}
    for item in env_items + finding_items:
        key = item["classification"]
        counts[key] = counts.get(key, 0) + 1

    high_risk = sum(
        counts.get(k, 0)
        for k in [
            "ROTATE_REQUIRED_TRACKED_ENV",
            "ROTATE_REQUIRED_SECRET_CANDIDATE",
            "TRACKED_ENV_REVIEW",
            "TRACKED_ZONE_IDENTIFIER_REMOVE_CANDIDATE",
        ]
    )

    status = "HOLD" if high_risk > 0 else "REVIEW"

    payload = {
        "generated": now(),
        "mode": "V1 classifier only. Uses redacted hygiene JSON. No secret values read or printed.",
        "status": status,
        "classification_counts": counts,
        "high_risk_count": high_risk,
        "env_items": env_items,
        "secret_finding_items": finding_items,
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# EFRO Secrets Classifier Status",
        "",
        f"Generated: {now()}",
        "",
        "Mode: V1 classifier only. Uses redacted hygiene JSON. No secret values read. No secret values printed. No product code writes. No push.",
        "",
        f"Overall status: {status}",
        f"High-risk count: {high_risk}",
        "",
        "## Classification Counts",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]

    for key, value in sorted(counts.items()):
        lines.append(f"| {key} | {value} |")

    lines += [
        "",
        "## High-risk / tracked env review",
        "",
        "| Repo | Path | Tracked | Classification | Action |",
        "|---|---|---|---|---|",
    ]

    for item in env_items:
        if item["classification"] in {
            "ROTATE_REQUIRED_TRACKED_ENV",
            "TRACKED_ENV_REVIEW",
            "TRACKED_ZONE_IDENTIFIER_REMOVE_CANDIDATE",
            "SAFE_TEMPLATE_REVIEW",
        }:
            lines.append(
                f"| {item['repo']} | {item['path']} | {item['tracked']} | "
                f"{item['classification']} | {item['action']} |"
            )

    lines += [
        "",
        "## High-risk secret candidates",
        "",
        "| Repo | Path | Line | Category | Classification | Value printed |",
        "|---|---|---:|---|---|---|",
    ]

    for item in finding_items:
        if item["classification"] == "ROTATE_REQUIRED_SECRET_CANDIDATE":
            lines.append(
                f"| {item['repo']} | {item['path']} | {item['line']} | "
                f"{item['category']} | {item['classification']} | {item['value_printed']} |"
            )

    lines += [
        "",
        "## Required handling",
        "",
        "1. Do not paste secret values into chat.",
        "2. Rotate any token that may have been exposed.",
        "3. Review tracked env-like files first.",
        "4. Remove tracked Zone.Identifier artifacts only after explicit owner approval.",
        "5. Treat old worktree findings as cleanup-linked, not immediate deletion.",
        "6. Keep `.env*` ignored unless the file is a deliberate placeholder template.",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(OUT_MD)
    print(OUT_JSON)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
