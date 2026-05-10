#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/efro-agent")
REPOS_ROOT = ROOT / "repos"
OUT_MD = ROOT / "orchestrator/SECRETS_HYGIENE_STATUS.md"
OUT_JSON = ROOT / "orchestrator/worker-results/secrets-hygiene-v1.json"

REPOS = {
    "efro": REPOS_ROOT / "efro",
    "efro-widget": REPOS_ROOT / "efro-widget",
    "efro-brain": REPOS_ROOT / "efro-brain",
    "efro-shopify": REPOS_ROOT / "efro-shopify",
    "efro-agent": ROOT,
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".next",
    "dist",
    "build",
    "coverage",
    ".turbo",
    ".vercel",
    "__pycache__",
    "worker-results",
}

TEXT_SUFFIXES = {
    ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt", ".yml", ".yaml",
    ".py", ".sh", ".env", ".local", ".example", ".template"
}

SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)?PRIVATE KEY-----")),
    ("vercel_oidc_token", re.compile(r"VERCEL_OIDC_TOKEN", re.I)),
    ("generic_api_key_assignment", re.compile(r"(API_KEY|SECRET|TOKEN|PASSWORD)\s*=\s*['\"]?[^'\"\s]{12,}", re.I)),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.I)),
    ("jwt_like", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def run(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip()
    except Exception as exc:
        return 1, f"ERROR:{type(exc).__name__}"

def is_git_repo(path: Path) -> bool:
    rc, out = run(["git", "rev-parse", "--is-inside-work-tree"], path)
    return rc == 0 and out == "true"

def tracked_files(path: Path) -> set[str]:
    if not is_git_repo(path):
        return set()
    rc, out = run(["git", "ls-files"], path, 60)
    if rc != 0:
        return set()
    return set(line.strip() for line in out.splitlines() if line.strip())

def should_skip(path: Path) -> bool:
    return any(part in SKIP_DIRS for part in path.parts)

def is_text_candidate(path: Path) -> bool:
    if path.name.startswith(".env"):
        return True
    if path.suffix in TEXT_SUFFIXES:
        return True
    return False

def scan_repo(name: str, root: Path) -> dict:
    repo_result = {
        "repo": name,
        "root": str(root),
        "exists": root.exists(),
        "env_files": [],
        "tracked_env_files": [],
        "secret_findings": [],
        "errors": [],
    }

    if not root.exists():
        repo_result["errors"].append("repo_path_missing")
        return repo_result

    tracked = tracked_files(root)

    for rel in sorted(tracked):
        if Path(rel).name.startswith(".env") or "/.env" in rel:
            repo_result["tracked_env_files"].append(rel)

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if should_skip(path):
            continue

        try:
            rel = str(path.relative_to(root))
        except Exception:
            continue

        if path.name.startswith(".env") or "/.env" in rel:
            repo_result["env_files"].append({
                "path": rel,
                "tracked": rel in tracked,
                "content_printed": False,
            })
            continue

        if not is_text_candidate(path):
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for line_no, line in enumerate(text.splitlines(), 1):
            for category, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    repo_result["secret_findings"].append({
                        "path": rel,
                        "line": line_no,
                        "category": category,
                        "value_printed": False,
                    })
                    break

    return repo_result

def main() -> int:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)

    results = [scan_repo(name, path) for name, path in REPOS.items()]

    total_env = sum(len(r["env_files"]) for r in results)
    total_tracked_env = sum(len(r["tracked_env_files"]) for r in results)
    total_findings = sum(len(r["secret_findings"]) for r in results)

    status = "GO" if total_tracked_env == 0 and total_findings == 0 else "HOLD"

    payload = {
        "generated": now(),
        "mode": "V1 redacted scan only. No secret values printed. No writes to product code.",
        "status": status,
        "total_env_files": total_env,
        "total_tracked_env_files": total_tracked_env,
        "total_secret_findings": total_findings,
        "results": results,
    }

    OUT_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# EFRO Secrets Hygiene Status",
        "",
        f"Generated: {now()}",
        "",
        "Mode: V1 redacted scan only. No secret values printed. No product code writes. No push.",
        "",
        f"Overall status: {status}",
        "",
        "## Summary",
        "",
        f"- Env files found: {total_env}",
        f"- Tracked env files: {total_tracked_env}",
        f"- Redacted secret findings: {total_findings}",
        "",
        "## Repo Results",
        "",
        "| Repo | Env files | Tracked env files | Redacted findings | Status |",
        "|---|---:|---:|---:|---|",
    ]

    for r in results:
        repo_status = "GO" if len(r["tracked_env_files"]) == 0 and len(r["secret_findings"]) == 0 else "HOLD"
        lines.append(
            f"| {r['repo']} | {len(r['env_files'])} | {len(r['tracked_env_files'])} | "
            f"{len(r['secret_findings'])} | {repo_status} |"
        )

    lines += [
        "",
        "## Env files",
        "",
        "| Repo | Path | Tracked | Content printed |",
        "|---|---|---|---|",
    ]

    for r in results:
        for item in r["env_files"]:
            lines.append(f"| {r['repo']} | {item['path']} | {item['tracked']} | {item['content_printed']} |")

    lines += [
        "",
        "## Redacted secret findings",
        "",
        "| Repo | Path | Line | Category | Value printed |",
        "|---|---|---:|---|---|",
    ]

    for r in results:
        for item in r["secret_findings"]:
            lines.append(
                f"| {r['repo']} | {item['path']} | {item['line']} | "
                f"{item['category']} | {item['value_printed']} |"
            )

    lines += [
        "",
        "## Required handling",
        "",
        "- Do not paste secret values into chat.",
        "- Rotate any token that may have been exposed.",
        "- Ensure `.env*` files are not tracked.",
        "- Future grep/search tasks must exclude `.env*` or redact values.",
        "",
    ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(OUT_MD)
    print(OUT_JSON)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
