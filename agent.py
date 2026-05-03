import os
import subprocess
import json
import ollama
import re
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import chromadb
from chromadb.utils import embedding_functions
from sentence_transformers import SentenceTransformer
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from datetime import datetime   # <-- NEU

import threading
import time
from urllib import parse as urllib_parse
from urllib import request as urllib_request
load_dotenv()

# --- Logging-Hilfsfunktion ---
LOG_FILE = "/opt/efro-agent/agent.log"

def log_message(msg: str):
    try:
        os.makedirs("/opt/efro-agent", exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception as e:
        print("Logging Fehler:", e)

VERCEL_TOKEN = os.getenv("VERCEL_TOKEN")
RENDER_TOKEN = os.getenv("RENDER_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ---------- Konfiguration ----------
REPO_PATHS = {
    "efro": "/opt/efro-agent/repos/efro",
    "brain": "/opt/efro-agent/repos/efro-brain",
    "widget": "/opt/efro-agent/repos/efro-widget",
    "shopify": "/opt/efro-agent/repos/efro-shopify"
}
HANDOFF_DIR = os.getenv("EFRO_AGENT_HANDOFF_DIR", "/opt/efro-agent/handoffs")
COST_LEDGER_DIR = os.getenv("EFRO_AGENT_COST_LEDGER_DIR", "/opt/efro-agent/cost-ledger")
COST_LEDGER_FILE = os.path.join(COST_LEDGER_DIR, "cost_events.jsonl")
COST_LEDGER_LOCK = threading.Lock()
# -----------------------------------

# --- Embedding-Funktion (lokal) ---
class LocalEmbeddingFunction(embedding_functions.EmbeddingFunction):
    def __init__(self):
        self.model = SentenceTransformer('all-MiniLM-L6-v2')
    def __call__(self, texts):
        return self.model.encode(texts).tolist()

# --- Chroma Client ---
client = chromadb.PersistentClient(path="/opt/efro-agent/chroma_db")
embedding_fn = LocalEmbeddingFunction()
collection = client.get_collection(
    name="efro_code",
    embedding_function=embedding_fn
)

# --- FastAPI App ---
app = FastAPI()

# --- Hilfsfunktionen für Tools ---
# ERSETZE IN agent.py DIESE FUNKTIONEN KOMPLETT DURCH DIESE BLOECKE


def run_command(cmd, cwd, timeout=30):
    return "DISABLED: unsafe shell execution removed"

def _get_npm_scripts(cwd):
    try:
        package_json_path = os.path.join(cwd, "package.json")
        if not os.path.exists(package_json_path):
            return {}
        with open(package_json_path, "r", encoding="utf-8") as f:
            package_data = json.load(f)
        return package_data.get("scripts", {}) or {}
    except Exception:
        return {}


def run_linter(repo_key):
    cwd = REPO_PATHS.get(repo_key, REPO_PATHS["brain"])
    scripts = _get_npm_scripts(cwd)

    if "lint" not in scripts:
        return "SKIPPED: kein lint script vorhanden"

    return run_command("npm run lint", cwd, timeout=45)


def run_build(repo_key):
    cwd = REPO_PATHS.get(repo_key, REPO_PATHS["brain"])
    scripts = _get_npm_scripts(cwd)

    if "build" not in scripts:
        return "SKIPPED: kein build script vorhanden"

    output = run_command("npm run build", cwd, timeout=60)

    if "next: not found" in output or "command not found" in output:
        install_output = run_command("npm install", cwd, timeout=120)
        retry_output = run_command("npm run build", cwd, timeout=60)
        return f"Auto-Fix npm install:\\n{install_output}\\n\\nRetry Build:\\n{retry_output}"

    return output


def run_tests(repo_key):
    cwd = REPO_PATHS.get(repo_key, REPO_PATHS["brain"])
    scripts = _get_npm_scripts(cwd)

    if "test" not in scripts:
        return "SKIPPED: kein test script vorhanden"

    return run_command("npm test", cwd, timeout=60)



def run_install(repo_key):
    return "DISABLED: package installation removed"


def smoke_shopify():
    return "DISABLED: local service smoke execution removed"


def deterministic_optimize(repo_key):
    return {
        "repo": repo_key,
        "steps": [],
        "status": "DISABLED: optimize pipeline removed"
    }


def parse_direct_command(message: str):
    text = message.strip().lower()
    if not text:
        return None

    if text in ("status", "agent status"):
        return ("status", None)

    if text in ("smoke shopify", "smoke_shopify"):
        return ("smoke_shopify", "shopify")

    parts = text.split()
    if len(parts) >= 2:
        command = parts[0]
        repo = parts[1]

        command_aliases = {
            "lint": "linter",
            "linter": "linter",
            "build": "build",
            "test": "test",
            "install": "install",
            "optimize": "optimize",
        }

        normalized_command = command_aliases.get(command)
        if normalized_command and repo in REPO_PATHS:
            return (normalized_command, repo)

    return None


def write_file(path, content):
    return "DISABLED: arbitrary file writes removed"

def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Fehler beim Lesen: {e}"


class HandoffPacket(BaseModel):
    incident_id: str
    shop_domain: str
    priority: str
    severity: str
    scope: str
    likely_repo: str
    likely_subsystem: str
    summary: str
    top_findings: list[str] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)
    recommended_next_action: str


class HandoffRecord(HandoffPacket):
    handoff_id: str
    created_at: str


class HandoffCreateResponse(BaseModel):
    handoff_id: str
    handoff_path: str
    packet: HandoffRecord


class CostLedgerEvent(BaseModel):
    shop_domain: str = Field(default="unknown", max_length=255)
    subsystem: str = Field(default="unknown", max_length=120)
    endpoint: str = Field(default="unknown", max_length=255)
    provider: str = Field(default="unknown", max_length=120)
    operation: str = Field(default="unknown", max_length=160)
    request_id: str = Field(default_factory=lambda: f"cost_{uuid4().hex[:12]}", max_length=120)
    session_id: str | None = Field(default=None, max_length=160)
    cache_key: str | None = Field(default=None, max_length=255)
    cache_status: str = Field(default="unknown", max_length=40)
    input_size: int | None = Field(default=None, ge=0)
    output_size: int | None = Field(default=None, ge=0)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    characters: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    currency: str = Field(default="USD", max_length=8)
    observed_status: str = Field(default="unknown", max_length=80)
    latency_ms: int | None = Field(default=None, ge=0)
    error: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=1000)


class CostLedgerRecord(CostLedgerEvent):
    recorded_at: str


class CostLedgerSummary(BaseModel):
    count: int
    estimated_total_cost: float
    currency: str
    by_provider: dict[str, dict[str, Any]]
    by_endpoint: dict[str, dict[str, Any]]
    by_shop: dict[str, dict[str, Any]] = Field(default_factory=dict)
    by_cache_status: dict[str, int]
    cache_hit_rate: float = 0.0
    cache_miss_rate: float = 0.0
    billable_event_count: int = 0
    zero_cost_cached_event_count: int = 0
    cache_miss_estimated_cost: float = 0.0
    latest: list[dict[str, Any]]


class CostEstimateInput(BaseModel):
    shop_domain: str = Field(default="unknown", max_length=255)
    subsystem: str = Field(default="manual_cost_audit", max_length=120)
    endpoint: str = Field(default="unknown", max_length=255)
    provider: str = Field(default="unknown", max_length=120)
    operation: str = Field(default="estimate_only", max_length=160)
    request_id: str = Field(default_factory=lambda: f"estimate_{uuid4().hex[:12]}", max_length=120)
    session_id: str | None = Field(default=None, max_length=160)
    cache_key: str | None = Field(default=None, max_length=255)
    cache_status: str = Field(default="unknown", max_length=40)
    tokens_in: int | None = Field(default=None, ge=0)
    tokens_out: int | None = Field(default=None, ge=0)
    characters: int | None = Field(default=None, ge=0)
    requests: int = Field(default=1, ge=1)
    sessions: int = Field(default=0, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    observed_status: str = Field(default="estimated_without_provider_call", max_length=80)
    notes: str | None = Field(default=None, max_length=1000)
    write_ledger: bool = False


class CostEstimateResult(BaseModel):
    ok: bool
    provider: str
    endpoint: str
    estimated_cost: float
    currency: str
    formula: str
    billable_units: dict[str, Any]
    cache_status: str
    write_ledger: bool
    ledger_record: dict[str, Any] | None = None


def _ensure_handoff_dir():
    os.makedirs(HANDOFF_DIR, exist_ok=True)


def _ensure_cost_ledger_dir():
    os.makedirs(COST_LEDGER_DIR, exist_ok=True)


def _is_local_request(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    return client_host in {"127.0.0.1", "::1", "localhost"}


def _cost_ledger_record_from_event(event: CostLedgerEvent) -> CostLedgerRecord:
    return CostLedgerRecord(**event.model_dump(), recorded_at=datetime.now().isoformat())


def append_cost_ledger_event(event: CostLedgerEvent) -> CostLedgerRecord:
    _ensure_cost_ledger_dir()
    record = _cost_ledger_record_from_event(event)
    with COST_LEDGER_LOCK:
        with open(COST_LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record.model_dump(), ensure_ascii=False, sort_keys=True) + "\n")
    log_message(
        "COST_LEDGER_APPEND "
        f"provider={record.provider} endpoint={record.endpoint} shop={record.shop_domain} "
        f"cache={record.cache_status} estimated_cost={record.estimated_cost} request_id={record.request_id}"
    )
    return record


def read_cost_ledger_records(limit: int = 250) -> list[dict[str, Any]]:
    if not os.path.exists(COST_LEDGER_FILE):
        return []
    safe_limit = max(1, min(limit, 5000))
    with COST_LEDGER_LOCK:
        with open(COST_LEDGER_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()[-safe_limit:]
    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                records.append(parsed)
        except Exception:
            continue
    return records


def _add_cost_bucket(target: dict[str, dict[str, Any]], key: str, record: dict[str, Any]) -> None:
    bucket_key = key or "unknown"
    bucket = target.setdefault(bucket_key, {
        "count": 0,
        "estimated_cost": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "characters": 0,
        "cache_hit_count": 0,
        "cache_miss_count": 0,
        "cache_unknown_count": 0,
        "cache_hit_rate": 0.0,
        "cache_miss_estimated_cost": 0.0,
    })
    estimated_cost = float(record.get("estimated_cost") or 0)
    cache_status = str(record.get("cache_status") or "unknown").lower()
    bucket["count"] += 1
    bucket["estimated_cost"] += estimated_cost
    bucket["tokens_in"] += int(record.get("tokens_in") or 0)
    bucket["tokens_out"] += int(record.get("tokens_out") or 0)
    bucket["characters"] += int(record.get("characters") or 0)
    if cache_status == "hit":
        bucket["cache_hit_count"] += 1
    elif cache_status == "miss":
        bucket["cache_miss_count"] += 1
        bucket["cache_miss_estimated_cost"] += estimated_cost
    else:
        bucket["cache_unknown_count"] += 1
    if bucket["count"]:
        bucket["cache_hit_rate"] = round(bucket["cache_hit_count"] / bucket["count"], 6)


def _cost_rate_card() -> dict[str, dict[str, Any]]:
    return {
        "brain_llm": {
            "currency": "USD",
            "input_per_1k_tokens": float(os.getenv("EFRO_RATE_BRAIN_INPUT_PER_1K", "0.0")),
            "output_per_1k_tokens": float(os.getenv("EFRO_RATE_BRAIN_OUTPUT_PER_1K", "0.0")),
            "request_fixed": float(os.getenv("EFRO_RATE_BRAIN_REQUEST_FIXED", "0.0")),
        },
        "widget_answer": {
            "currency": "USD",
            "request_fixed": float(os.getenv("EFRO_RATE_WIDGET_ANSWER_REQUEST_FIXED", "0.0")),
        },
        "mascot_voice": {
            "currency": "USD",
            "session_fixed": float(os.getenv("EFRO_RATE_MASCOT_SESSION_FIXED", "0.0")),
            "request_fixed": float(os.getenv("EFRO_RATE_MASCOT_REQUEST_FIXED", "0.0")),
        },
        "elevenlabs_tts": {
            "currency": "USD",
            "per_1k_characters": float(os.getenv("EFRO_RATE_ELEVENLABS_PER_1K_CHARS", "0.0")),
            "request_fixed": float(os.getenv("EFRO_RATE_ELEVENLABS_REQUEST_FIXED", "0.0")),
        },
    }


def estimate_cost(event: CostEstimateInput) -> CostEstimateResult:
    cache_status = (event.cache_status or "unknown").lower()
    if cache_status == "hit":
        return CostEstimateResult(
            ok=True,
            provider=event.provider,
            endpoint=event.endpoint,
            estimated_cost=0.0,
            currency="USD",
            formula="cache_hit => 0 estimated provider cost",
            billable_units={"cache_status": cache_status},
            cache_status=cache_status,
            write_ledger=event.write_ledger,
        )

    rates = _cost_rate_card()
    provider = event.provider or "unknown"
    rate = rates.get(provider, {"currency": "USD", "request_fixed": 0.0})
    requests = max(1, int(event.requests or 1))
    sessions = max(0, int(event.sessions or 0))
    tokens_in = int(event.tokens_in or 0)
    tokens_out = int(event.tokens_out or 0)
    characters = int(event.characters or 0)

    estimated = requests * float(rate.get("request_fixed", 0.0))
    formula_parts = [f"requests({requests})*request_fixed({rate.get('request_fixed', 0.0)})"]

    if provider == "brain_llm":
        estimated += (tokens_in / 1000.0) * float(rate.get("input_per_1k_tokens", 0.0))
        estimated += (tokens_out / 1000.0) * float(rate.get("output_per_1k_tokens", 0.0))
        formula_parts.append(f"tokens_in({tokens_in})/1000*{rate.get('input_per_1k_tokens', 0.0)}")
        formula_parts.append(f"tokens_out({tokens_out})/1000*{rate.get('output_per_1k_tokens', 0.0)}")
    elif provider == "elevenlabs_tts":
        estimated += (characters / 1000.0) * float(rate.get("per_1k_characters", 0.0))
        formula_parts.append(f"characters({characters})/1000*{rate.get('per_1k_characters', 0.0)}")
    elif provider == "mascot_voice":
        estimated += sessions * float(rate.get("session_fixed", 0.0))
        formula_parts.append(f"sessions({sessions})*session_fixed({rate.get('session_fixed', 0.0)})")

    return CostEstimateResult(
        ok=True,
        provider=provider,
        endpoint=event.endpoint,
        estimated_cost=round(estimated, 8),
        currency=str(rate.get("currency", "USD")),
        formula=" + ".join(formula_parts),
        billable_units={
            "requests": requests,
            "sessions": sessions,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "characters": characters,
            "cache_status": cache_status,
        },
        cache_status=cache_status,
        write_ledger=event.write_ledger,
    )


def summarize_cost_ledger(limit: int = 250) -> CostLedgerSummary:
    records = read_cost_ledger_records(limit=limit)
    by_provider: dict[str, dict[str, Any]] = {}
    by_endpoint: dict[str, dict[str, Any]] = {}
    by_shop: dict[str, dict[str, Any]] = {}
    by_cache_status: dict[str, int] = {}
    total = 0.0
    hit_count = 0
    miss_count = 0
    billable_event_count = 0
    zero_cost_cached_event_count = 0
    cache_miss_estimated_cost = 0.0
    for record in records:
        estimated_cost = float(record.get("estimated_cost") or 0)
        cache_status = str(record.get("cache_status") or "unknown").lower()
        total += estimated_cost
        _add_cost_bucket(by_provider, str(record.get("provider") or "unknown"), record)
        _add_cost_bucket(by_endpoint, str(record.get("endpoint") or "unknown"), record)
        _add_cost_bucket(by_shop, str(record.get("shop_domain") or "unknown"), record)
        by_cache_status[cache_status] = by_cache_status.get(cache_status, 0) + 1
        if estimated_cost > 0:
            billable_event_count += 1
        if cache_status == "hit":
            hit_count += 1
            if estimated_cost == 0:
                zero_cost_cached_event_count += 1
        elif cache_status == "miss":
            miss_count += 1
            cache_miss_estimated_cost += estimated_cost
    count = len(records)
    return CostLedgerSummary(
        count=count,
        estimated_total_cost=round(total, 8),
        currency="USD",
        by_provider=by_provider,
        by_endpoint=by_endpoint,
        by_shop=by_shop,
        by_cache_status=by_cache_status,
        cache_hit_rate=round(hit_count / count, 6) if count else 0.0,
        cache_miss_rate=round(miss_count / count, 6) if count else 0.0,
        billable_event_count=billable_event_count,
        zero_cost_cached_event_count=zero_cost_cached_event_count,
        cache_miss_estimated_cost=round(cache_miss_estimated_cost, 8),
        latest=list(reversed(records[-25:])),
    )


def _handoff_file_path(handoff_id: str) -> str:
    safe_handoff_id = re.sub(r"[^a-zA-Z0-9_-]", "", handoff_id)
    return os.path.join(HANDOFF_DIR, f"{safe_handoff_id}.json")


def create_handoff_record(packet: HandoffPacket) -> HandoffRecord:
    _ensure_handoff_dir()
    handoff_id = f"handoff_{uuid4().hex[:12]}"
    record = HandoffRecord(
        handoff_id=handoff_id,
        created_at=datetime.now().isoformat(),
        **packet.model_dump(),
    )

    with open(_handoff_file_path(handoff_id), "w", encoding="utf-8") as f:
        json.dump(record.model_dump(), f, ensure_ascii=False, indent=2)

    return record


def load_handoff_record(handoff_id: str) -> HandoffRecord:
    path = _handoff_file_path(handoff_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Handoff nicht gefunden")

    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    return HandoffRecord(**payload)


def list_handoff_records(limit: int = 25) -> list[dict[str, Any]]:
    _ensure_handoff_dir()
    records: list[HandoffRecord] = []

    for entry in os.listdir(HANDOFF_DIR):
        if not re.fullmatch(r"handoff_[a-zA-Z0-9_-]+\.json", entry):
            continue

        path = os.path.join(HANDOFF_DIR, entry)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                payload = json.load(f)
            records.append(HandoffRecord(**payload))
        except Exception as e:
            log_message(f"HANDOFF_LIST_SKIP file={entry} error={e}")

    records.sort(key=lambda record: record.created_at, reverse=True)
    return [record.model_dump() for record in records[:limit]]
        
WATCHDOG_LOCK = threading.Lock()
WATCHDOG_STATE: dict[str, Any] = {
    "enabled": False,
    "interval_seconds": 120,
    "last_run_at": None,
    "last_results": {},
    "active_failure_signatures": {},
    "last_handoff_ids": {},
    "consecutive_failures": {},
    "last_ok_at": {},
    "last_error_at": {},
    "last_notified_status": {},
    "last_notified_at": {},
    "thread_started": False,
}

def _watchdog_enabled() -> bool:
    return os.getenv("EFRO_AGENT_WATCHDOG_ENABLED", "0").strip() == "1"

def _watchdog_interval_seconds() -> int:
    raw = os.getenv("EFRO_AGENT_WATCHDOG_INTERVAL_SECONDS", "120").strip()
    try:
        return max(30, int(raw))
    except Exception:
        return 120

def _watchdog_public_failure_threshold() -> int:
    raw = os.getenv("EFRO_AGENT_PUBLIC_FAILURE_THRESHOLD", "3").strip()
    try:
        return max(1, int(raw))
    except Exception:
        return 3


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _costly_watchdog_checks_enabled() -> bool:
    return _env_flag("EFRO_ENABLE_COSTLY_WATCHDOG_CHECKS", "0")


def _watchdog_cost_manifest() -> list[dict[str, str]]:
    return [
        {
            "check": "widget_voice_signed_url_prod",
            "provider_risk": "Mascot/voice-provider signed URL runtime",
            "trigger": "POST widget /api/get-signed-url",
        },
        {
            "check": "brain_live_prod",
            "provider_risk": "Brain/LLM/vector/database runtime",
            "trigger": "POST brain /api/brain/chat",
        },
        {
            "check": "shopify_product_inventory",
            "provider_risk": "Brain/LLM/vector/database runtime",
            "trigger": "POST brain /api/brain/chat",
        },
        {
            "check": "brain_answer_quality_smoke",
            "provider_risk": "multiple Brain/LLM/provider calls",
            "trigger": "4x POST brain /api/brain/chat",
        },
        {
            "check": "widget_chat_voice_cache_parity",
            "provider_risk": "Widget answer may invoke Brain/LLM; signed URL may invoke Mascot; paid TTS can invoke ElevenLabs when separately enabled",
            "trigger": "POST widget /api/nonshopify-answer + /api/get-signed-url + optional /api/tts-with-visemes",
        },
    ]


def _check_watchdog_cost_policy() -> dict[str, Any]:
    enabled = _costly_watchdog_checks_enabled()
    manifest = _watchdog_cost_manifest()
    evidence = _clip_text(json.dumps({
        "zero_cost_default": not enabled,
        "costly_checks_enabled": enabled,
        "enable_flag": "EFRO_ENABLE_COSTLY_WATCHDOG_CHECKS=true",
        "paid_tts_extra_flag": "EFRO_ENABLE_PAID_TTS_WATCHDOG=true",
        "cost_sources": manifest,
    }, ensure_ascii=False), 1300)
    return _run_observation_check(
        check_name="watchdog_cost_policy",
        target="internal:cost-policy",
        status="warn" if enabled else "ok",
        kind="cost_policy",
        evidence=evidence,
        expected="Default watchdog must be zero-cost. Any provider/LLM/voice/runtime probes must be explicit opt-in and visible in evidence.",
        observed=(
            "costly_checks_enabled=true; provider calls may run; review cost_sources before allowing"
            if enabled
            else "costly_checks_enabled=false; zero-cost default enforced"
        ),
        duration_ms=0,
    )


def _zero_cost_skip_check(check_name: str, target: str, kind: str, reason: str) -> dict[str, Any]:
    return _run_observation_check(
        check_name=check_name,
        target=target,
        status="ok",
        kind=kind,
        evidence=f"skipped_by_default=true; zero_cost_watchdog=true; reason={reason}; enable_with=EFRO_ENABLE_COSTLY_WATCHDOG_CHECKS=true",
        expected="Zero-cost watchdog default must not invoke provider, LLM, voice, TTS, or other metered external runtime calls",
        observed="skipped_by_default=true; no provider call executed",
        duration_ms=0,
    )

def _telegram_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def _send_telegram_message(text: str) -> dict[str, Any]:
    if not _telegram_enabled():
        return {"ok": False, "skipped": True, "reason": "telegram_not_configured"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib_parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib_request.Request(url, data=payload, method="POST")

    try:
        with urllib_request.urlopen(req, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            ok = 200 <= getattr(response, "status", 200) < 300
            return {
                "ok": ok,
                "status_code": getattr(response, "status", 200),
                "body": _clip_text(body, 220),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _build_watchdog_telegram_message(
    shop_key: str,
    summary_status: str,
    failed_checks: list[dict[str, Any]],
    handoff_record: Optional[HandoffRecord],
    public_health_consecutive_failures: int,
    public_failure_threshold: int,
) -> str:
    severity = summary_status.upper()
    lines = [
        f"EFRO Watchdog {severity}",
        f"Shop: {shop_key}",
        f"Zeit: {_watchdog_now()}",
        f"Fehlerhafte Checks: {len(failed_checks)}",
        f"Public Health Failures: {public_health_consecutive_failures}/{public_failure_threshold}",
    ]

    if failed_checks:
        lines.append("Checks: " + ", ".join(item["check_name"] for item in failed_checks[:5]))
        lines.append("Nächster Schritt: " + _watchdog_next_action(failed_checks))

    if handoff_record:
        lines.append(f"Handoff: {handoff_record.handoff_id}")

    if failed_checks:
        first = failed_checks[0]
        lines.extend([
            "",
            "Kopierbarer Agent-Auftrag:",
            f"Subsystem: {_watchdog_subsystem(failed_checks)}",
            f"Check: {first.get('check_name')}",
            f"Target: {first.get('target')}",
            f"Observed: {_clip_text(str(first.get('observed') or ''), 280)}",
            f"Evidence: {_clip_text(str(first.get('evidence') or ''), 420)}",
            "Arbeite nur auf Branch/Worktree. Keine Secrets ausgeben. Main nicht direkt ändern. Erst Typecheck/Test/Probe grün machen.",
        ])

    return "\n".join(lines)


def _watchdog_now() -> str:
    return datetime.now().isoformat()

def _clip_text(value: str, limit: int = 240) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."

def _watchdog_check_result(
    check_name: str,
    target: str,
    status: str,
    kind: str,
    evidence: str,
    expected: str,
    observed: str,
    duration_ms: int,
) -> dict[str, Any]:
    return {
        "check_name": check_name,
        "target": target,
        "status": status,
        "kind": kind,
        "evidence": evidence,
        "expected": expected,
        "observed": observed,
        "duration_ms": duration_ms,
        "timestamp": _watchdog_now(),
    }

def _run_observation_check(
    check_name: str,
    target: str,
    status: str,
    kind: str,
    evidence: str,
    expected: str,
    observed: str,
    duration_ms: int,
) -> dict[str, Any]:
    return _watchdog_check_result(
        check_name=check_name,
        target=target,
        status=status,
        kind=kind,
        evidence=evidence,
        expected=expected,
        observed=observed,
        duration_ms=duration_ms,
    )


def _build_local_health_payload() -> dict[str, Any]:
    _ensure_handoff_dir()
    handoff_count = 0
    for entry in os.listdir(HANDOFF_DIR):
        if re.fullmatch(r"handoff_[a-zA-Z0-9_-]+\.json", entry):
            handoff_count += 1

    return {
        "status": "ok",
        "service": "efro-agent",
        "time": _watchdog_now(),
        "model": os.getenv("EFRO_AGENT_MODEL", "qwen2.5-coder:7b"),
        "handoff_dir": HANDOFF_DIR,
        "handoff_dir_exists": os.path.isdir(HANDOFF_DIR),
        "handoff_count": handoff_count,
        "repos": sorted(REPO_PATHS.keys()),
    }


def _check_local_health_contract() -> dict[str, Any]:
    started = time.time()
    try:
        payload = _build_local_health_payload()
        ok = payload["status"] == "ok" and payload["handoff_dir_exists"] is True
        evidence = _clip_text(json.dumps(payload, ensure_ascii=False), 220)
        return _run_observation_check(
            check_name="local_health",
            target="internal:health-payload",
            status="ok" if ok else "error",
            kind="technical",
            evidence=evidence,
            expected="status=ok and handoff_dir_exists=true",
            observed=f"status={payload['status']}; handoff_dir_exists={payload['handoff_dir_exists']}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        return _run_observation_check(
            check_name="local_health",
            target="internal:health-payload",
            status="error",
            kind="technical",
            evidence=f"exception={e}",
            expected="status=ok and handoff_dir_exists=true",
            observed=f"exception={e}",
            duration_ms=int((time.time() - started) * 1000),
        )


def _check_public_health_contract() -> dict[str, Any]:
    started = time.time()
    shop_domain = os.getenv("EFRO_AGENT_EFRO_DOMAIN", "mcp.avatarsalespro.com").strip() or "mcp.avatarsalespro.com"
    target = f"https://{shop_domain}/health"
    local_target = os.getenv("EFRO_AGENT_LOCAL_HEALTH_URL", "http://127.0.0.1:8000/health").strip()
    expected_contains = '"status":"ok"'

    def run_curl(url: str, max_time: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["curl", "--silent", "--show-error", "--location", "--max-time", max_time, url],
            capture_output=True,
            text=True,
            timeout=max(int(max_time) + 5, 10),
        )

    try:
        public = run_curl(target, os.getenv("EFRO_AGENT_PUBLIC_HEALTH_TIMEOUT_SECONDS", "15").strip() or "15")
        public_stdout = public.stdout or ""
        public_stderr = public.stderr or ""
        public_ok = public.returncode == 0 and expected_contains in public_stdout

        if public_ok:
            return _run_observation_check(
                check_name="public_health",
                target=target,
                status="ok",
                kind="technical",
                evidence=_clip_text(f"public_returncode={public.returncode}; public_ok=True", 220),
                expected=f"external health returns 0 and contains {expected_contains}",
                observed="public_ok=True; local_fallback=not_needed",
                duration_ms=int((time.time() - started) * 1000),
            )

        local_payload = _build_local_health_payload()
        local_ok = local_payload.get("status") == "ok" and local_payload.get("handoff_dir_exists") is True
        status = "warn" if local_ok else "error"
        evidence = _clip_text(
            f"public_returncode={public.returncode}; public_stderr={public_stderr}; "
            f"local_internal_ok={local_ok}; local_service={local_payload.get('service')}; "
            f"handoff_dir_exists={local_payload.get('handoff_dir_exists')}",
            260,
        )
        return _run_observation_check(
            check_name="public_health",
            target=target,
            status=status,
            kind="technical",
            evidence=evidence,
            expected="external health ok; if external times out, internal local health contract must prove agent is healthy",
            observed=f"public_ok=False; local_internal_ok={local_ok}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except subprocess.TimeoutExpired as e:
        return _run_observation_check(
            check_name="public_health",
            target=target,
            status="error",
            kind="technical",
            evidence=f"timeout={e}",
            expected=f"health probe returns within timeout and contains {expected_contains}",
            observed=f"timeout={e}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        return _run_observation_check(
            check_name="public_health",
            target=target,
            status="error",
            kind="technical",
            evidence=f"exception={e}",
            expected=f"health probe returns within timeout and contains {expected_contains}",
            observed=f"exception={e}",
            duration_ms=int((time.time() - started) * 1000),
        )


def _check_widget_voice_signed_url_prod() -> dict[str, Any]:
    started = time.time()
    target = os.getenv("EFRO_WIDGET_SIGNED_URL_PROD", "https://widget.avatarsalespro.com/api/get-signed-url").strip()
    if not _costly_watchdog_checks_enabled():
        return _zero_cost_skip_check(
            "widget_voice_signed_url_prod",
            target,
            "technical",
            "signed-url probe may invoke Mascot/voice provider or metered runtime",
        )
    payload = {
        "dynamicVariables": {
            "name": "EFRO",
            "shop": "avatarsalespro-dev.myshopify.com",
            "shopId": "avatarsalespro-dev.myshopify.com",
            "sessionId": f"watchdog-{int(time.time())}",
            "greeting": "Hallo! Ich bin EFRO, dein KI-Assistent. Wie kann ich dir helfen?",
            "sourceKind": "shopify",
        }
    }

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            target,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with urllib_request.urlopen(req, timeout=20) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", 200)

        parsed = json.loads(response_body)
        has_signed_url = isinstance(parsed.get("signedUrl"), str) and parsed.get("signedUrl", "").startswith("wss://")
        ok = status_code == 200 and has_signed_url
        evidence = _clip_text(
            f"http={status_code}; has_signed_url={has_signed_url}; request_id={parsed.get('requestId') or '-'}",
            220,
        )
        return _run_observation_check(
            check_name="widget_voice_signed_url_prod",
            target=target,
            status="ok" if ok else "error",
            kind="technical",
            evidence=evidence,
            expected="HTTP 200 and JSON contains signedUrl starting with wss://",
            observed=f"http={status_code}; has_signed_url={has_signed_url}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        return _run_observation_check(
            check_name="widget_voice_signed_url_prod",
            target=target,
            status="error",
            kind="technical",
            evidence=f"exception={e}",
            expected="Production widget signed URL endpoint returns HTTP 200 with signedUrl",
            observed=f"exception={e}",
            duration_ms=int((time.time() - started) * 1000),
        )


def _bad_output_leaks(text: str) -> list[str]:
    bad_needles = ["[object Object]", "undefined", "NaN"]
    value = str(text or "")
    leaks = [needle for needle in bad_needles if needle in value]
    if re.search(r"\bnull\b", value, flags=re.IGNORECASE):
        leaks.append("null")
    return leaks


def _check_brain_live_prod() -> dict[str, Any]:
    started = time.time()
    if not _costly_watchdog_checks_enabled():
        return _zero_cost_skip_check(
            "brain_live_prod",
            "configured brain endpoint",
            "technical",
            "brain live probe may invoke LLM/vector/database metered runtime",
        )
    configured_target = os.getenv("EFRO_BRAIN_LIVE_PROD_URL", "").strip()
    configured_base = os.getenv("EFRO_BRAIN_URL", os.getenv("BRAIN_API_URL", "")).strip().rstrip("/")
    candidate_targets: list[str] = []
    if configured_target:
        candidate_targets.append(configured_target)
    if configured_base:
        candidate_targets.append(f"{configured_base}/api/brain/chat")
    candidate_targets.extend([
        "https://efro-brain.vercel.app/api/brain/chat",
        "http://127.0.0.1:3010/api/brain/chat",
        "http://127.0.0.1:3020/api/brain/chat",
        "http://127.0.0.1:3041/api/brain/chat",
        "https://brain.avatarsalespro.com/api/brain/chat",
    ])
    # Keep order but remove duplicates/empty values.
    candidate_targets = list(dict.fromkeys([item for item in candidate_targets if item]))
    target = candidate_targets[0]
    shop_domain = os.getenv("EFRO_BRAIN_LIVE_PROD_SHOP_DOMAIN", "avatarsalespro-dev.myshopify.com").strip()
    expected_product_count_raw = os.getenv("EFRO_BRAIN_EXPECTED_PRODUCT_COUNT", "134").strip()

    try:
        expected_product_count = max(1, int(expected_product_count_raw))
    except Exception:
        expected_product_count = 134

    payload = {
        "message": "Welche Produkte empfiehlst du mir?",
        "shopDomain": shop_domain,
        "sessionId": f"watchdog-brain-live-prod-{int(time.time())}",
        "channel": "watchdog",
        "siteType": "shopify",
    }

    body = json.dumps(payload).encode("utf-8")
    attempt_observations: list[str] = []

    for target in candidate_targets:
        try:
            req = urllib_request.Request(
                target,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            with urllib_request.urlopen(req, timeout=30) as response:
                response_body = response.read().decode("utf-8", errors="replace")
                status_code = getattr(response, "status", 200)

            parsed = json.loads(response_body)
            reply_text = str(parsed.get("replyText") or parsed.get("reply") or parsed.get("response") or "")
            metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
            debug = parsed.get("debug") if isinstance(parsed.get("debug"), dict) else {}
            api_diagnostics = debug.get("apiDiagnostics") if isinstance(debug.get("apiDiagnostics"), dict) else {}

            success = parsed.get("success") is True
            blocked = parsed.get("blocked") is True
            total_products = metadata.get("totalProducts")
            db_product_count = api_diagnostics.get("dbProductCount")
            leaks = _bad_output_leaks(reply_text)

            total_products_ok = isinstance(total_products, int) and total_products >= expected_product_count
            db_product_count_ok = db_product_count is None or (isinstance(db_product_count, int) and db_product_count >= expected_product_count)
            reply_ok = bool(reply_text.strip())
            ok = (
                status_code == 200
                and success
                and reply_ok
                and total_products_ok
                and db_product_count_ok
                and not blocked
                and not leaks
            )

            attempt_observations.append(
                f"{target}: http={status_code}; success={success}; reply_ok={reply_ok}; totalProducts={total_products}; dbProductCount={db_product_count}; blocked={blocked}; leaks={leaks}"
            )

            if ok:
                evidence = _clip_text(
                    f"selected={target}; http={status_code}; success={success}; reply_len={len(reply_text)}; "
                    f"totalProducts={total_products}; dbProductCount={db_product_count}; "
                    f"blocked={blocked}; leaks={','.join(leaks) or '-'}; candidates={len(candidate_targets)}",
                    320,
                )
                observed = (
                    f"selected={target}; http={status_code}; success={success}; reply_ok={reply_ok}; "
                    f"totalProducts={total_products}; dbProductCount={db_product_count}; blocked={blocked}; leaks={leaks}"
                )
                return _run_observation_check(
                    check_name="brain_live_prod",
                    target=target,
                    status="ok",
                    kind="technical",
                    evidence=evidence,
                    expected=f"HTTP 200, success=true, replyText non-empty, metadata.totalProducts>={expected_product_count}, dbProductCount>={expected_product_count} if present, blocked=false, no bad output leaks",
                    observed=observed,
                    duration_ms=int((time.time() - started) * 1000),
                )
        except Exception as e:
            attempt_observations.append(f"{target}: exception={e}")

    evidence = _clip_text(" || ".join(attempt_observations), 520)
    return _run_observation_check(
        check_name="brain_live_prod",
        target=", ".join(candidate_targets[:3]) + (" ..." if len(candidate_targets) > 3 else ""),
        status="error",
        kind="technical",
        evidence=evidence,
        expected="At least one configured or fallback Brain endpoint returns a valid product-backed answer contract",
        observed=evidence,
        duration_ms=int((time.time() - started) * 1000),
    )


def _brain_smoke_cases() -> list[dict[str, str]]:
    return [
        {
            "case_id": "recommendation_de",
            "message": "Ich suche ein passendes Produkt. Was empfiehlst du mir?",
            "expects_product_grounding": "1",
        },
        {
            "case_id": "comparison_de",
            "message": "Vergleiche bitte zwei passende Produkte und erkläre kurz den Unterschied.",
            "expects_product_grounding": "1",
        },
        {
            "case_id": "price_de",
            "message": "Nenne mir ein empfehlenswertes Produkt und worauf ich beim Preis achten soll.",
            "expects_product_grounding": "1",
        },
        {
            "case_id": "unknown_safe_de",
            "message": "Erfinde bitte keine Produkte. Wenn du unsicher bist, sage klar, welche Informationen fehlen.",
            "expects_product_grounding": "0",
        },
    ]


def _looks_german_answer(text: str) -> bool:
    value = str(text or "").lower()
    german_markers = [" der ", " die ", " das ", " und ", " ich ", " du ", " empfehle", " produkt", " preis", " wenn "]
    padded = f" {value} "
    return any(marker in padded for marker in german_markers)


def _extract_brain_reply_fields(parsed: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    reply_text = str(parsed.get("replyText") or parsed.get("reply") or parsed.get("response") or "")
    metadata = parsed.get("metadata") if isinstance(parsed.get("metadata"), dict) else {}
    debug = parsed.get("debug") if isinstance(parsed.get("debug"), dict) else {}
    return reply_text, metadata, debug


def _is_transient_urlopen_error(error: Exception) -> bool:
    value = str(error).lower()
    return any(
        needle in value
        for needle in [
            "connection reset by peer",
            "remote end closed connection",
            "timed out",
            "timeout",
            "temporarily unavailable",
        ]
    )


def _urlopen_read_with_retry(
    req: urllib_request.Request,
    timeout: int,
    attempts: int = 2,
    backoff_seconds: float = 0.4,
) -> tuple[str, int, dict[str, str], int]:
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            with urllib_request.urlopen(req, timeout=timeout) as response:
                return (
                    response.read().decode("utf-8", errors="replace"),
                    getattr(response, "status", 200),
                    dict(response.headers.items()),
                    attempt,
                )
        except Exception as error:
            last_error = error
            if attempt >= attempts or not _is_transient_urlopen_error(error):
                raise
            time.sleep(backoff_seconds * attempt)
    raise last_error or RuntimeError("urlopen retry failed without captured exception")


def _check_shopify_product_inventory() -> dict[str, Any]:
    started = time.time()
    if not _costly_watchdog_checks_enabled():
        return _zero_cost_skip_check(
            "shopify_product_inventory",
            "configured brain endpoint",
            "inventory",
            "inventory probe may invoke Brain/LLM/provider runtime",
        )
    configured_target = os.getenv("EFRO_BRAIN_LIVE_PROD_URL", "").strip()
    configured_base = os.getenv("EFRO_BRAIN_URL", os.getenv("BRAIN_API_URL", "")).strip().rstrip("/")
    candidate_targets: list[str] = []
    if configured_target:
        candidate_targets.append(configured_target)
    if configured_base:
        candidate_targets.append(f"{configured_base}/api/brain/chat")
    candidate_targets.extend([
        "https://efro-brain.vercel.app/api/brain/chat",
        "https://brain.avatarsalespro.com/api/brain/chat",
    ])
    candidate_targets = list(dict.fromkeys([item for item in candidate_targets if item]))

    shop_domain = os.getenv("EFRO_BRAIN_LIVE_PROD_SHOP_DOMAIN", "avatarsalespro-dev.myshopify.com").strip()
    expected_product_count_raw = os.getenv("EFRO_BRAIN_EXPECTED_PRODUCT_COUNT", "134").strip()
    try:
        expected_product_count = max(1, int(expected_product_count_raw))
    except Exception:
        expected_product_count = 134

    observations: list[str] = []
    for target in candidate_targets:
        try:
            payload = {
                "message": "Zeige mir verfuegbare Shopify Produkte und nenne konkrete Produktempfehlungen mit Preis.",
                "shopDomain": shop_domain,
                "sessionId": f"watchdog-shopify-inventory-{int(time.time())}",
                "channel": "watchdog",
                "siteType": "shopify",
            }
            body = json.dumps(payload).encode("utf-8")
            req = urllib_request.Request(
                target,
                data=body,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response_body, status_code, _headers, retry_attempt = _urlopen_read_with_retry(req, timeout=35)
            parsed = json.loads(response_body)
            reply_text, metadata, debug = _extract_brain_reply_fields(parsed)
            api_diagnostics = debug.get("apiDiagnostics") if isinstance(debug.get("apiDiagnostics"), dict) else {}
            candidates = parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else []

            success = parsed.get("success") is True
            blocked = parsed.get("blocked") is True
            total_products = metadata.get("totalProducts")
            db_product_count = api_diagnostics.get("dbProductCount")
            total_products_ok = isinstance(total_products, int) and total_products >= expected_product_count
            db_product_count_ok = db_product_count is None or (isinstance(db_product_count, int) and db_product_count >= expected_product_count)
            reply_lower = reply_text.lower()
            price_ok = "€" in reply_text or "eur" in reply_lower or "preis" in reply_lower
            product_signal_ok = bool(candidates) or any(
                term in reply_lower
                for term in ["produkt", "produkte", "empfehl", "artikel", "anschauen", "vergleichen"]
            )
            concrete_product_ok = product_signal_ok and (bool(candidates) or price_ok or total_products_ok)
            leaks = _bad_output_leaks(reply_text)

            ok = (
                status_code == 200
                and success
                and not blocked
                and total_products_ok
                and db_product_count_ok
                and concrete_product_ok
                and price_ok
                and not leaks
            )
            observations.append(
                f"{target}: http={status_code}; success={success}; totalProducts={total_products}; "
                f"dbProductCount={db_product_count}; candidates={len(candidates)}; concrete_product_ok={concrete_product_ok}; "
                f"price_ok={price_ok}; blocked={blocked}; retry_attempt={retry_attempt}; leaks={leaks}"
            )
            if ok:
                evidence = _clip_text(observations[-1], 360)
                return _run_observation_check(
                    check_name="shopify_product_inventory",
                    target=target,
                    status="ok",
                    kind="inventory",
                    evidence=evidence,
                    expected=f"Brain sees Shopify inventory with totalProducts>={expected_product_count}, concrete products and price signal",
                    observed=evidence,
                    duration_ms=int((time.time() - started) * 1000),
                )
        except Exception as e:
            observations.append(f"{target}: exception={e}")

    evidence = _clip_text(" || ".join(observations), 700)
    return _run_observation_check(
        check_name="shopify_product_inventory",
        target=", ".join(candidate_targets[:3]),
        status="error",
        kind="inventory",
        evidence=evidence,
        expected=f"At least one Brain endpoint proves Shopify inventory with totalProducts>={expected_product_count}, product and price signal",
        observed=evidence,
        duration_ms=int((time.time() - started) * 1000),
    )



def _check_brain_answer_quality_smoke() -> dict[str, Any]:
    started = time.time()
    if not _costly_watchdog_checks_enabled():
        return _zero_cost_skip_check(
            "brain_answer_quality_smoke",
            "configured brain endpoint",
            "answer_quality",
            "answer-quality smoke may invoke LLM/provider runtime multiple times",
        )
    configured_target = os.getenv("EFRO_BRAIN_LIVE_PROD_URL", "").strip()
    configured_base = os.getenv("EFRO_BRAIN_URL", os.getenv("BRAIN_API_URL", "")).strip().rstrip("/")
    candidate_targets: list[str] = []
    if configured_target:
        candidate_targets.append(configured_target)
    if configured_base:
        candidate_targets.append(f"{configured_base}/api/brain/chat")
    candidate_targets.extend([
        "https://efro-brain.vercel.app/api/brain/chat",
        "http://127.0.0.1:3010/api/brain/chat",
        "http://127.0.0.1:3020/api/brain/chat",
        "http://127.0.0.1:3041/api/brain/chat",
        "https://brain.avatarsalespro.com/api/brain/chat",
    ])
    candidate_targets = list(dict.fromkeys([item for item in candidate_targets if item]))
    selected_target = candidate_targets[0] if candidate_targets else "none"
    shop_domain = os.getenv("EFRO_BRAIN_LIVE_PROD_SHOP_DOMAIN", "avatarsalespro-dev.myshopify.com").strip()
    expected_product_count_raw = os.getenv("EFRO_BRAIN_EXPECTED_PRODUCT_COUNT", "134").strip()
    min_pass_cases_raw = os.getenv("EFRO_BRAIN_QUALITY_MIN_PASS_CASES", "3").strip()
    max_case_latency_raw = os.getenv("EFRO_BRAIN_QUALITY_MAX_CASE_LATENCY_MS", "12000").strip()

    try:
        expected_product_count = max(1, int(expected_product_count_raw))
    except Exception:
        expected_product_count = 134
    try:
        min_pass_cases = max(1, int(min_pass_cases_raw))
    except Exception:
        min_pass_cases = 3
    try:
        max_case_latency_ms = max(1000, int(max_case_latency_raw))
    except Exception:
        max_case_latency_ms = 12000

    case_results: list[dict[str, Any]] = []
    selected_target = None

    for target in candidate_targets:
        target_case_results: list[dict[str, Any]] = []
        target_hard_failure = False

        for case in _brain_smoke_cases():
            case_started = time.time()
            payload = {
                "message": case["message"],
                "shopDomain": shop_domain,
                "sessionId": f"watchdog-brain-quality-{case['case_id']}-{int(time.time())}",
                "channel": "watchdog",
                "siteType": "shopify",
            }
            try:
                body = json.dumps(payload).encode("utf-8")
                req = urllib_request.Request(
                    target,
                    data=body,
                    method="POST",
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
                response_body, status_code, _response_headers, retry_attempt = _urlopen_read_with_retry(req, timeout=30)

                latency_ms = int((time.time() - case_started) * 1000)
                parsed = json.loads(response_body)
                reply_text, metadata, debug = _extract_brain_reply_fields(parsed)
                api_diagnostics = debug.get("apiDiagnostics") if isinstance(debug.get("apiDiagnostics"), dict) else {}
                candidates = parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else []

                success = parsed.get("success") is True
                blocked = parsed.get("blocked") is True
                total_products = metadata.get("totalProducts")
                db_product_count = api_diagnostics.get("dbProductCount")
                leaks = _bad_output_leaks(reply_text)
                reply_len = len(reply_text.strip())
                german_ok = _looks_german_answer(reply_text)
                latency_ok = latency_ms <= max_case_latency_ms
                total_products_ok = isinstance(total_products, int) and total_products >= expected_product_count
                db_product_count_ok = db_product_count is None or (isinstance(db_product_count, int) and db_product_count >= expected_product_count)
                product_grounding_required = case.get("expects_product_grounding") == "1"
                product_grounding_ok = (
                    not product_grounding_required
                    or bool(candidates)
                    or total_products_ok
                    or bool(re.search(r"\bprodukt\w*\b", reply_text, flags=re.IGNORECASE))
                )

                case_ok = (
                    status_code == 200
                    and success
                    and not blocked
                    and reply_len >= 40
                    and german_ok
                    and not leaks
                    and latency_ok
                    and total_products_ok
                    and db_product_count_ok
                    and product_grounding_ok
                )

                target_case_results.append({
                    "case_id": case["case_id"],
                    "status": "ok" if case_ok else "error",
                    "http": status_code,
                    "success": success,
                    "blocked": blocked,
                    "reply_len": reply_len,
                    "german_ok": german_ok,
                    "latency_ms": latency_ms,
                    "latency_ok": latency_ok,
                    "totalProducts": total_products,
                    "dbProductCount": db_product_count,
                    "leaks": leaks,
                    "product_grounding_ok": product_grounding_ok,
                })
            except Exception as e:
                target_hard_failure = True
                target_case_results.append({
                    "case_id": case["case_id"],
                    "status": "error",
                    "exception": str(e),
                    "latency_ms": int((time.time() - case_started) * 1000),
                })

        passed_cases = len([item for item in target_case_results if item.get("status") == "ok"])
        if passed_cases >= min_pass_cases:
            selected_target = target
            case_results = target_case_results
            break

        if not case_results or (not target_hard_failure and passed_cases > len([item for item in case_results if item.get("status") == "ok"])):
            selected_target = target
            case_results = target_case_results

    passed_cases = len([item for item in case_results if item.get("status") == "ok"])
    total_cases = len(case_results)
    failed_cases = [item for item in case_results if item.get("status") != "ok"]
    max_latency = max([int(item.get("latency_ms") or 0) for item in case_results] or [0])
    blocked_count = len([item for item in case_results if item.get("blocked") is True])
    leak_count = sum(len(item.get("leaks") or []) for item in case_results)
    grounding_failures = len([item for item in case_results if item.get("product_grounding_ok") is False])

    ok = passed_cases >= min_pass_cases and blocked_count == 0 and leak_count == 0 and grounding_failures == 0
    evidence = _clip_text(json.dumps({
        "selected": selected_target,
        "passed_cases": passed_cases,
        "total_cases": total_cases,
        "max_latency_ms": max_latency,
        "blocked_count": blocked_count,
        "leak_count": leak_count,
        "grounding_failures": grounding_failures,
        "failed_cases": failed_cases[:3],
    }, ensure_ascii=False), 700)

    return _run_observation_check(
        check_name="brain_answer_quality_smoke",
        target=selected_target or ", ".join(candidate_targets[:3]),
        status="ok" if ok else "error",
        kind="answer_quality",
        evidence=evidence,
        expected=f">={min_pass_cases}/{len(_brain_smoke_cases())} cases pass; German/product-grounded answers; no blocked responses; no bad output leaks; max case latency<={max_case_latency_ms}ms",
        observed=f"passed={passed_cases}/{total_cases}; max_latency_ms={max_latency}; blocked_count={blocked_count}; leak_count={leak_count}; grounding_failures={grounding_failures}",
        duration_ms=int((time.time() - started) * 1000),
    )


def _stable_text_hash(text: str) -> str:
    value = str(text or "").lower()
    value = re.sub(r"\s+", " ", value).strip()
    hash_value = 2166136261
    for char in value:
        hash_value ^= ord(char)
        hash_value = (hash_value * 16777619) & 0xFFFFFFFF
    return f"fnv1a32:{hash_value:08x}"


def _extract_sse_json_events(response_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in str(response_text or "").split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                parsed = json.loads(line[6:])
                if isinstance(parsed, dict):
                    events.append(parsed)
            except Exception:
                continue
    return events


def _check_widget_chat_voice_cache_parity() -> dict[str, Any]:
    started = time.time()
    widget_base = os.getenv("EFRO_WIDGET_BASE_URL", "https://widget.avatarsalespro.com").strip().rstrip("/")
    if not _costly_watchdog_checks_enabled():
        return _zero_cost_skip_check(
            "widget_chat_voice_cache_parity",
            widget_base,
            "runtime_parity",
            "widget parity probe may invoke Brain/LLM, Mascot signed-url, and paid voice/TTS runtime",
        )
    shop_domain = os.getenv("EFRO_BRAIN_LIVE_PROD_SHOP_DOMAIN", "avatarsalespro-dev.myshopify.com").strip()
    message = os.getenv("EFRO_WIDGET_PARITY_MESSAGE", "Ich suche ein passendes Produkt. Was empfiehlst du mir?").strip()
    session_id = f"watchdog-widget-parity-{int(time.time())}"

    answer_url = f"{widget_base}/api/nonshopify-answer"
    tts_url = f"{widget_base}/api/tts-with-visemes"
    signed_url = f"{widget_base}/api/get-signed-url"

    current_stage = "init"
    try:
        current_stage = "answer"
        answer_started = time.time()
        answer_body = json.dumps({
            "shop": shop_domain,
            "shopDomain": shop_domain,
            "message": message,
            "sessionId": session_id,
        }).encode("utf-8")
        answer_req = urllib_request.Request(
            answer_url,
            data=answer_body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        answer_response_body, answer_status, answer_headers, answer_retry_attempt = _urlopen_read_with_retry(answer_req, timeout=35)
        answer_latency_ms = int((time.time() - answer_started) * 1000)
        answer_payload = json.loads(answer_response_body)

        reply_text = str(answer_payload.get("replyText") or "")
        spoken_text = str(answer_payload.get("spokenText") or "")
        voice_text = str(answer_payload.get("voiceText") or "")
        answer_text = str(answer_payload.get("answer") or "")
        response_text = str(answer_payload.get("response") or "")
        parity = answer_payload.get("answerParity") if isinstance(answer_payload.get("answerParity"), dict) else {}
        reply_hash = _stable_text_hash(reply_text)
        parity_hash = str(parity.get("replyHash") or "")
        source = str(answer_payload.get("source") or "unknown")

        fields = [reply_text, spoken_text, voice_text, answer_text, response_text]
        non_empty_fields = [item for item in fields if item.strip()]
        field_hashes = {_stable_text_hash(item) for item in non_empty_fields}
        field_agreement = len(non_empty_fields) >= 3 and len(field_hashes) == 1
        parity_hash_ok = not parity_hash or parity_hash == reply_hash
        reply_ok = len(reply_text.strip()) >= 40 and not _bad_output_leaks(reply_text)

        paid_tts_watchdog_enabled = os.getenv("EFRO_ENABLE_PAID_TTS_WATCHDOG", "").strip().lower() in {"1", "true", "yes", "on"}
        current_stage = "tts"
        tts_status = 0
        tts_latency_ms = 0
        tts_headers: dict[str, str] = {}
        tts_retry_attempt = 0
        audio_events: list[dict[str, Any]] = []
        viseme_events: list[dict[str, Any]] = []
        done_events: list[dict[str, Any]] = []
        viseme_count = 0
        audio_ok = True
        tts_event_contract_ok = True

        if paid_tts_watchdog_enabled:
            tts_started = time.time()
            tts_probe_text = os.getenv("EFRO_WIDGET_TTS_WATCHDOG_TEXT", "Hallo EFRO.").strip() or "Hallo EFRO."
            tts_body = json.dumps({"text": tts_probe_text, "language": "de", "shopDomain": shop_domain}).encode("utf-8")
            tts_req = urllib_request.Request(
                tts_url,
                data=tts_body,
                method="POST",
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            )
            tts_response_text, tts_status, tts_headers, tts_retry_attempt = _urlopen_read_with_retry(tts_req, timeout=45)
            tts_latency_ms = int((time.time() - tts_started) * 1000)
            tts_events = _extract_sse_json_events(tts_response_text)
            audio_events = [item for item in tts_events if item.get("type") == "audio" and item.get("data")]
            viseme_events = [item for item in tts_events if item.get("type") == "visemes"]
            done_events = [item for item in tts_events if item.get("type") == "done"]
            for item in viseme_events:
                visemes = item.get("visemes")
                if isinstance(visemes, list):
                    viseme_count += len(visemes)
            audio_ok = tts_status == 200 and bool(audio_events)
            tts_event_contract_ok = bool(audio_events) and bool(viseme_events) and bool(done_events)

        current_stage = "signed_url"
        signed_started = time.time()
        signed_body = json.dumps({
            "dynamicVariables": {
                "name": "EFRO",
                "shop": shop_domain,
                "shopId": shop_domain,
                "sessionId": session_id,
                "sourceKind": "watchdog",
                "greeting": spoken_text or reply_text,
            }
        }).encode("utf-8")
        signed_req = urllib_request.Request(
            signed_url,
            data=signed_body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json", "Cache-Control": "no-cache"},
        )
        signed_response_text, signed_status, signed_headers, signed_retry_attempt = _urlopen_read_with_retry(signed_req, timeout=25)
        signed_latency_ms = int((time.time() - signed_started) * 1000)
        signed_payload = json.loads(signed_response_text)
        has_signed_url = isinstance(signed_payload.get("signedUrl"), str) and signed_payload.get("signedUrl", "").startswith("wss://")

        answer_cache_control = str(answer_headers.get("cache-control") or answer_headers.get("Cache-Control") or "")
        tts_cache_control = str(tts_headers.get("cache-control") or tts_headers.get("Cache-Control") or "")
        signed_cache_control = str(signed_headers.get("cache-control") or signed_headers.get("Cache-Control") or "")
        answer_cache_contract_ok = (
            "no-store" in answer_cache_control.lower()
            or "no-cache" in answer_cache_control.lower()
            or source in {"efro_brain", "direct", "efro_import_smoke"}
        )
        tts_cache_contract_ok = (not paid_tts_watchdog_enabled) or (
            "no-cache" in tts_cache_control.lower() or "no-store" in tts_cache_control.lower()
        )
        cache_contract_ok = answer_cache_contract_ok and tts_cache_contract_ok

        voice_chat_parity_ok = field_agreement and parity_hash_ok and reply_ok
        voice_runtime_ok = has_signed_url and (not paid_tts_watchdog_enabled or (audio_ok and tts_event_contract_ok))
        lipsync_signal_ok = (not paid_tts_watchdog_enabled) or bool(viseme_events)
        # Paid TTS is disabled by default because it consumes ElevenLabs credits.
        # Browser voice is covered by signedUrl plus explicit manual/preview smoke when needed.
        ok = voice_chat_parity_ok and voice_runtime_ok and lipsync_signal_ok and cache_contract_ok

        evidence = _clip_text(json.dumps({
            "answer": {
                "http": answer_status,
                "latency_ms": answer_latency_ms,
                "source": source,
                "reply_len": len(reply_text.strip()),
                "reply_hash": reply_hash,
                "parity_hash": parity_hash,
                "field_agreement": field_agreement,
                "cache_control": answer_cache_control,
            },
            "tts": {
                "paid_watchdog_enabled": paid_tts_watchdog_enabled,
                "http": tts_status,
                "latency_ms": tts_latency_ms,
                "audio_events": len(audio_events),
                "viseme_events": len(viseme_events),
                "viseme_count": viseme_count,
                "done_events": len(done_events),
                "cache_control": tts_cache_control,
            },
            "signed_url": {
                "http": signed_status,
                "latency_ms": signed_latency_ms,
                "has_wss_signed_url": has_signed_url,
                "cache_control": signed_cache_control,
            },
            "verdict": {
                "voice_chat_parity_ok": voice_chat_parity_ok,
                "voice_runtime_ok": voice_runtime_ok,
                "lipsync_signal_ok": lipsync_signal_ok,
                "cache_contract_ok": cache_contract_ok,
            },
        }, ensure_ascii=False), 900)

        return _run_observation_check(
            check_name="widget_chat_voice_cache_parity",
            target=widget_base,
            status="ok" if ok else "error",
            kind="runtime_parity",
            evidence=evidence,
            expected="Widget answer fields agree by hash, spoken/voice text equals chat text, signedUrl starts wss://, cache headers avoid stale runtime responses; paid TTS probe only runs when EFRO_ENABLE_PAID_TTS_WATCHDOG=true",
            observed=(
                f"field_agreement={field_agreement}; parity_hash_ok={parity_hash_ok}; "
                f"paid_tts_watchdog_enabled={paid_tts_watchdog_enabled}; audio_ok={audio_ok}; "
                f"viseme_events={len(viseme_events)}; signedUrl={has_signed_url}; "
                f"cache_contract_ok={cache_contract_ok}; source={source}"
            ),
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        http_status = getattr(e, "code", None)
        error_body = ""
        try:
            if hasattr(e, "read"):
                error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            error_body = ""
        evidence = _clip_text(
            f"stage={current_stage}; exception={e}; http_status={http_status}; "
            f"error_body={error_body[:500]}; answer_url={answer_url}; tts_url={tts_url}; signed_url={signed_url}",
            900,
        )
        return _run_observation_check(
            check_name="widget_chat_voice_cache_parity",
            target=widget_base,
            status="error",
            kind="runtime_parity",
            evidence=evidence,
            expected="Widget parity probe completes through answer, TTS/viseme and signed-url endpoints",
            observed=evidence,
            duration_ms=int((time.time() - started) * 1000),
        )


def _check_mcp_stream_disconnects() -> dict[str, Any]:
    started = time.time()
    lookback_seconds_raw = os.getenv("EFRO_AGENT_MCP_CONTEXT_CANCELED_LOOKBACK_SECONDS", "300").strip()
    warn_threshold_raw = os.getenv("EFRO_AGENT_MCP_CONTEXT_CANCELED_WARN_THRESHOLD", "8").strip()
    error_threshold_raw = os.getenv("EFRO_AGENT_MCP_CONTEXT_CANCELED_ERROR_THRESHOLD", "20").strip()

    try:
        lookback_seconds = max(60, int(lookback_seconds_raw))
    except Exception:
        lookback_seconds = 300

    try:
        warn_threshold = max(1, int(warn_threshold_raw))
    except Exception:
        warn_threshold = 8

    try:
        error_threshold = max(warn_threshold, int(error_threshold_raw))
    except Exception:
        error_threshold = 20

    cmd = [
        "journalctl",
        "-u",
        "caddy.service",
        "--since",
        f"-{lookback_seconds} seconds",
        "--no-pager",
    ]

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        matching_lines = [
            line for line in stdout.splitlines()
            if '"uri":"/mcp/"' in line and 'reading: context canceled' in line
        ]
        disconnect_count = len(matching_lines)

        if completed.returncode != 0:
            status = "error"
        elif disconnect_count >= error_threshold:
            status = "error"
        elif disconnect_count >= warn_threshold:
            status = "warn"
        else:
            status = "ok"

        evidence = _clip_text(
            f"returncode={completed.returncode}; disconnect_count={disconnect_count}; lookback_seconds={lookback_seconds}; sample={' || '.join(matching_lines[:2])}; stderr={stderr}",
            220,
        )
        return _run_observation_check(
            check_name="mcp_stream_disconnects",
            target="journalctl:caddy.service:/mcp/context-canceled",
            status=status,
            kind="technical",
            evidence=evidence,
            expected=f"< {warn_threshold} context-canceled events in the last {lookback_seconds} seconds",
            observed=f"disconnect_count={disconnect_count}; warn_threshold={warn_threshold}; error_threshold={error_threshold}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except subprocess.TimeoutExpired as e:
        return _run_observation_check(
            check_name="mcp_stream_disconnects",
            target="journalctl:caddy.service:/mcp/context-canceled",
            status="error",
            kind="technical",
            evidence=f"timeout={e}",
            expected="journalctl returns within timeout and disconnect rate stays below thresholds",
            observed=f"timeout={e}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        return _run_observation_check(
            check_name="mcp_stream_disconnects",
            target="journalctl:caddy.service:/mcp/context-canceled",
            status="error",
            kind="technical",
            evidence=f"exception={e}",
            expected="journalctl returns within timeout and disconnect rate stays below thresholds",
            observed=f"exception={e}",
            duration_ms=int((time.time() - started) * 1000),
        )


def _check_handoffs_api_contract() -> dict[str, Any]:
    started = time.time()
    try:
        items = list_handoff_records(limit=1)
        ok = isinstance(items, list)
        evidence = f"items_count={len(items)}"
        return _run_observation_check(
            check_name="handoffs_api",
            target="internal:list_handoff_records(limit=1)",
            status="ok" if ok else "error",
            kind="technical",
            evidence=evidence,
            expected="list result",
            observed=f"type={type(items).__name__}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        return _run_observation_check(
            check_name="handoffs_api",
            target="internal:list_handoff_records(limit=1)",
            status="error",
            kind="technical",
            evidence=f"exception={e}",
            expected="list result",
            observed=f"exception={e}",
            duration_ms=int((time.time() - started) * 1000),
        )


def _check_chat_status_contract() -> dict[str, Any]:
    started = time.time()
    expected_reply = "Agent läuft im read-only Incident-Reporter-Modus. Er sammelt Belege und schreibt Berichte, führt aber keine Optimierungen aus."
    try:
        direct = parse_direct_command("status")
        ok = direct == ("status", None)
        return _run_observation_check(
            check_name="chat_status_contract",
            target="internal:parse_direct_command('status')",
            status="ok" if ok else "error",
            kind="answer_contract",
            evidence=f"direct={direct}; expected_reply={expected_reply}",
            expected="('status', None) and read-only status contract",
            observed=f"direct={direct}",
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        return _run_observation_check(
            check_name="chat_status_contract",
            target="internal:parse_direct_command('status')",
            status="error",
            kind="answer_contract",
            evidence=f"exception={e}",
            expected="('status', None) and read-only status contract",
            observed=f"exception={e}",
            duration_ms=int((time.time() - started) * 1000),
        )


def _check_command_pwd_contract() -> dict[str, Any]:
    started = time.time()
    expected_output = "DISABLED: unsafe shell execution removed"
    try:
        output = run_command("pwd", REPO_PATHS["brain"])
        ok = output == expected_output
        return _run_observation_check(
            check_name="command_pwd_contract",
            target="internal:run_command('pwd', REPO_PATHS['brain'])",
            status="ok" if ok else "error",
            kind="answer_contract",
            evidence=f"output={output}",
            expected=expected_output,
            observed=output,
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        return _run_observation_check(
            check_name="command_pwd_contract",
            target="internal:run_command('pwd', REPO_PATHS['brain'])",
            status="error",
            kind="answer_contract",
            evidence=f"exception={e}",
            expected=expected_output,
            observed=f"exception={e}",
            duration_ms=int((time.time() - started) * 1000),
        )



def _check_control_center_watchdog_contract() -> dict[str, Any]:
    started = time.time()
    url = os.getenv("EFRO_CONTROL_CENTER_WATCHDOG_URL", "").strip()

    if not url:
        return _run_observation_check(
            check_name="control_center_watchdog",
            target="env:EFRO_CONTROL_CENTER_WATCHDOG_URL",
            status="warn",
            kind="technical",
            evidence="not_configured",
            expected="optional Control Center watchdog URL configured",
            observed="EFRO_CONTROL_CENTER_WATCHDOG_URL empty",
            duration_ms=int((time.time() - started) * 1000),
        )

    try:
        req = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
        with urllib_request.urlopen(req, timeout=15) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = getattr(response, "status", 200)

        payload = json.loads(body)
        health = payload.get("health") or {}
        widget = ((payload.get("widgetBehavior") or {}).get("nonshopifyGreeting") or {})

        overall_status = str(health.get("status") or "unknown")
        widget_status = str(widget.get("status") or health.get("widgetProbeStatus") or "unknown")
        reasons = [str(item) for item in (health.get("reasons") or [])]
        reason = str(widget.get("reason") or ", ".join(reasons) or "unknown")
        efro_agent_status = str(health.get("efroAgentStatus") or "unknown")
        widget_verdict = widget.get("verdict") or {}
        widget_verdict_status = str(widget_verdict.get("status") or "").lower()
        widget_present = bool(widget)

        agent_self_loop = (
            efro_agent_status in {"red", "yellow"}
            and not widget_present
            and any("Bootstrap erforderlich" in item or "Watchdog-Summary" in item for item in reasons)
        )
        widget_bad = (
            widget_status in {"red", "yellow", "fail", "error"}
            or widget_verdict_status == "fail"
            or widget.get("stale") is True
        )
        control_bad = overall_status in {"red", "yellow"} and not agent_self_loop and widget_present

        if status_code >= 400 or widget_bad or control_bad:
            derived_status = "error"
        elif agent_self_loop or not widget_present:
            derived_status = "warn"
        else:
            derived_status = "ok"

        ok = derived_status == "ok"

        observed = (
            f"http={status_code}; overall={overall_status}; efroAgent={efro_agent_status}; "
            f"widget={widget_status}; derived={derived_status}; reason={reason}"
        )

        evidence = _clip_text(json.dumps({
            "health": health,
            "widgetBehavior": payload.get("widgetBehavior"),
        }, ensure_ascii=False), 320)

        return _run_observation_check(
            check_name="control_center_watchdog",
            target=url,
            status=derived_status,
            kind="technical",
            evidence=evidence,
            expected="Control Center /api/ops/watchdog health.status=green and widget greeting green/unknown",
            observed=observed,
            duration_ms=int((time.time() - started) * 1000),
        )
    except Exception as e:
        return _run_observation_check(
            check_name="control_center_watchdog",
            target=url,
            status="error",
            kind="technical",
            evidence=f"exception={e}",
            expected="Control Center watchdog reachable and green",
            observed=f"exception={e}",
            duration_ms=int((time.time() - started) * 1000),
        )


def _efro_watchdog_checks() -> list[dict[str, Any]]:
    return [
        _check_watchdog_cost_policy(),
        _check_local_health_contract(),
        _check_public_health_contract(),
        _check_widget_voice_signed_url_prod(),
        _check_brain_live_prod(),
        _check_shopify_product_inventory(),
        _check_brain_answer_quality_smoke(),
        _check_widget_chat_voice_cache_parity(),
        _check_mcp_stream_disconnects(),
        _check_handoffs_api_contract(),
        _check_chat_status_contract(),
        _check_command_pwd_contract(),
        # Avoid a watchdog self-loop: Control Center depends on this Agent summary,
        # so the Agent must not call Control Center from inside the same blocking run.
        # Control Center remains responsible for reading /api/watchdog/summary.
    ]


def _watchdog_failure_signature(failed_checks: list[dict[str, Any]]) -> str:
    if not failed_checks:
        return "ok"
    parts = [f"{item['check_name']}:{item['status']}" for item in failed_checks]
    return "|".join(sorted(parts))

def _watchdog_severity_and_priority(failed_checks: list[dict[str, Any]]) -> tuple[str, str]:
    failed_names = {item["check_name"] for item in failed_checks}
    if "local_health" in failed_names or "public_health" in failed_names or "mcp_stream_disconnects" in failed_names:
        return ("critical", "P1")
    if any(item["kind"] == "technical" for item in failed_checks):
        return ("high", "P1")
    return ("medium", "P2")

def _watchdog_subsystem(failed_checks: list[dict[str, Any]]) -> str:
    failed_names = {item["check_name"] for item in failed_checks}
    if "public_health" in failed_names:
        return "caddy-routing"
    if "mcp_stream_disconnects" in failed_names:
        return "mcp-stream"
    if "brain_live_prod" in failed_names:
        return "brain-live-prod"
    if "local_health" in failed_names:
        return "agent-runtime"
    if "handoffs_api" in failed_names:
        return "handoff-api"
    if "chat_status_contract" in failed_names:
        return "chat-contract"
    if "command_pwd_contract" in failed_names:
        return "command-contract"
    if "control_center_watchdog" in failed_names:
        return "control-center-watchdog"
    return "mixed-runtime"

def _watchdog_next_action(failed_checks: list[dict[str, Any]]) -> str:
    subsystem = _watchdog_subsystem(failed_checks)

    if subsystem == "caddy-routing":
        return "Prüfe zuerst öffentliches Routing, Health-Weiterleitung und Caddy-Konfiguration. Danach lokalen Health-Pfad gegen 127.0.0.1:8000 gegentesten."
    if subsystem == "mcp-stream":
        return "Prüfe zuerst die Rate von /mcp/-Disconnects in caddy.service, vergleiche sie mit mcp-repo-reader-Logs und sammele Belege, ob der Client die Streams vorzeitig beendet."
    if subsystem == "brain-live-prod":
        return "Prüfe zuerst EFRO_BRAIN_URL/BRAIN_API_URL, /api/brain/chat Live-Antwort, Produktcount, blocked-Status, Bad-Output-Leaks und Supabase products für avatarsalespro-dev.myshopify.com. Danach Brain-Repo nur auf Branch/Worktree ändern und mit Live-Probe belegen."
    if subsystem == "agent-runtime":
        return "Prüfe zuerst efro-agent.service, lokale /health-Antwort und Prozessstatus auf Port 8000."
    if subsystem == "handoff-api":
        return "Prüfe zuerst /api/handoffs lokal, Handoff-Verzeichnis und JSON-Lese-/Schreibpfad."
    if subsystem == "chat-contract":
        return "Prüfe zuerst /api/chat mit status-Prompt und vergleiche die Antwort mit dem erwarteten Read-only-Vertrag."
    if subsystem == "command-contract":
        return "Prüfe zuerst /command mit pwd im brain-Repo und vergleiche die Antwort mit dem erwarteten Disabled-Vertrag."
    if subsystem == "control-center-watchdog":
        return "Prüfe zuerst Control Center /api/ops/watchdog. Wenn widgetBehavior.nonshopifyGreeting rot/gelb ist: Repo efro-widget prüfen, Probe-Report lesen, Branch/Worktree nutzen, Typecheck und Probe grün machen. Main nicht direkt ändern."

    return "Prüfe zuerst die fehlgeschlagenen Checks, priorisiere technische Fehler vor semantischen Antwortfehlern und dokumentiere nur verifizierte Befunde."

def _create_watchdog_handoff(shop_key: str, failed_checks: list[dict[str, Any]], all_checks: list[dict[str, Any]]) -> HandoffRecord:
    shop_domain = os.getenv("EFRO_AGENT_EFRO_DOMAIN", "mcp.avatarsalespro.com").strip() or "mcp.avatarsalespro.com"
    severity, priority = _watchdog_severity_and_priority(failed_checks)
    subsystem = _watchdog_subsystem(failed_checks)
    summary = f"Watchdog erkannte {len(failed_checks)} fehlerhafte Checks für {shop_domain}: " + ", ".join(item["check_name"] for item in failed_checks)

    packet = HandoffPacket(
        incident_id=f"watchdog_{shop_key}_{uuid4().hex[:8]}",
        shop_domain=shop_domain,
        priority=priority,
        severity=severity,
        scope="watchdog/read-only",
        likely_repo="efro-agent",
        likely_subsystem=subsystem,
        summary=summary,
        top_findings=[f"{item['check_name']}: {item['evidence']}" for item in failed_checks[:5]],
        checks_run=[f"{item['check_name']}={item['status']}" for item in all_checks],
        recommended_next_action=_watchdog_next_action(failed_checks),
    )
    return create_handoff_record(packet)

def run_watchdog_cycle(shop_key: str = "efro") -> dict[str, Any]:
    if shop_key != "efro":
        return {
            "ok": False,
            "error": f"Shop '{shop_key}' wird aktuell noch nicht unterstützt. Erster sicherer Watchdog-Scope ist nur 'efro'."
        }

    all_checks = _efro_watchdog_checks()
    cost_checks_enabled = _costly_watchdog_checks_enabled()
    cost_sources = _watchdog_cost_manifest() if cost_checks_enabled else []
    observed_failed_checks = [item for item in all_checks if item["status"] == "error"]
    non_public_failed_checks = [item for item in observed_failed_checks if item["check_name"] != "public_health"]
    public_health_failed = any(item["check_name"] == "public_health" for item in observed_failed_checks)

    public_key = f"{shop_key}:public_health"
    public_failure_threshold = _watchdog_public_failure_threshold()

    with WATCHDOG_LOCK:
        previous_signature = WATCHDOG_STATE["active_failure_signatures"].get(shop_key)
        previous_public_failure_count = int(WATCHDOG_STATE["consecutive_failures"].get(public_key, 0) or 0)
        previous_notified_status = WATCHDOG_STATE["last_notified_status"].get(shop_key)

    public_health_consecutive_failures = previous_public_failure_count + 1 if public_health_failed else 0

    incident_failed_checks = list(non_public_failed_checks)
    if public_health_failed and public_health_consecutive_failures >= public_failure_threshold:
        incident_failed_checks.extend(
            [item for item in observed_failed_checks if item["check_name"] == "public_health"]
        )

    failure_signature = _watchdog_failure_signature(incident_failed_checks)
    handoff_record = None

    if incident_failed_checks and failure_signature != previous_signature:
        handoff_record = _create_watchdog_handoff(shop_key, incident_failed_checks, all_checks)

    if incident_failed_checks:
        summary_status = "red"
    elif observed_failed_checks or cost_checks_enabled:
        summary_status = "yellow"
    else:
        summary_status = "green"

    telegram_should_notify = (
        summary_status == "red"
        or (
            summary_status == "yellow"
            and (cost_checks_enabled or any(item["check_name"] != "public_health" for item in observed_failed_checks))
        )
    ) and (
        summary_status != previous_notified_status or handoff_record is not None
    )
    telegram_sent = False
    telegram_error = None

    if telegram_should_notify:
        telegram_result = _send_telegram_message(
            _build_watchdog_telegram_message(
                shop_key=shop_key,
                summary_status=summary_status,
                failed_checks=incident_failed_checks or observed_failed_checks or ([item for item in all_checks if item.get("check_name") == "watchdog_cost_policy"] if cost_checks_enabled else []),
                handoff_record=handoff_record,
                public_health_consecutive_failures=public_health_consecutive_failures,
                public_failure_threshold=public_failure_threshold,
            )
        )
        telegram_sent = bool(telegram_result.get("ok"))
        if not telegram_sent:
            telegram_error = telegram_result.get("reason") or telegram_result.get("error") or "unknown_telegram_error"

    result = {
        "shop_key": shop_key,
        "shop_domain": os.getenv("EFRO_AGENT_EFRO_DOMAIN", "mcp.avatarsalespro.com").strip() or "mcp.avatarsalespro.com",
        "run_at": _watchdog_now(),
        "ok": len(incident_failed_checks) == 0,
        "degraded": len(observed_failed_checks) > 0,
        "summary_status": summary_status,
        "mode": "read-only watchdog",
        "answer_quality_scope": "zero-cost default watchdog: local/public-health/log/contract checks only; provider/LLM/voice/runtime probes require EFRO_ENABLE_COSTLY_WATCHDOG_CHECKS=true",
        "cost_mode": "costly_checks_enabled" if cost_checks_enabled else "zero_cost",
        "cost_warning": "Provider/LLM/voice/runtime probes are enabled and may create costs" if cost_checks_enabled else None,
        "costly_checks_enabled": cost_checks_enabled,
        "cost_sources": cost_sources,
        "observed_failed_count": len(observed_failed_checks),
        "failed_count": len(incident_failed_checks),
        "public_health_consecutive_failures": public_health_consecutive_failures,
        "public_health_incident_threshold": public_failure_threshold,
        "checks": all_checks,
        "handoff_created": handoff_record is not None,
        "handoff_id": handoff_record.handoff_id if handoff_record else None,
        "telegram_configured": _telegram_enabled(),
        "telegram_should_notify": telegram_should_notify,
        "telegram_sent": telegram_sent,
        "telegram_error": telegram_error,
    }

    with WATCHDOG_LOCK:
        WATCHDOG_STATE["enabled"] = _watchdog_enabled()
        WATCHDOG_STATE["interval_seconds"] = _watchdog_interval_seconds()
        WATCHDOG_STATE["last_run_at"] = result["run_at"]
        WATCHDOG_STATE["last_results"][shop_key] = result
        WATCHDOG_STATE["consecutive_failures"][public_key] = public_health_consecutive_failures

        if public_health_failed:
            WATCHDOG_STATE["last_error_at"][public_key] = result["run_at"]
        else:
            WATCHDOG_STATE["last_ok_at"][public_key] = result["run_at"]

        if telegram_sent:
            WATCHDOG_STATE["last_notified_status"][shop_key] = summary_status
            WATCHDOG_STATE["last_notified_at"][shop_key] = result["run_at"]
        elif summary_status == "green":
            WATCHDOG_STATE["last_notified_status"].pop(shop_key, None)

        if incident_failed_checks:
            WATCHDOG_STATE["active_failure_signatures"][shop_key] = failure_signature
            if handoff_record:
                WATCHDOG_STATE["last_handoff_ids"][shop_key] = handoff_record.handoff_id
        else:
            WATCHDOG_STATE["active_failure_signatures"].pop(shop_key, None)

    log_message(
        f"WATCHDOG_RUN shop={shop_key} ok={result['ok']} degraded={result['degraded']} "
        f"observed_failed_count={result['observed_failed_count']} failed_count={result['failed_count']} "
        f"public_health_consecutive_failures={public_health_consecutive_failures}/{public_failure_threshold} "
        f"handoff_id={result['handoff_id'] or '-'}"
    )
    return result

def _watchdog_loop():
    while True:
        try:
            run_watchdog_cycle("efro")
        except Exception as e:
            log_message(f"WATCHDOG_LOOP_ERROR shop=efro error={e}")
        time.sleep(_watchdog_interval_seconds())

@app.on_event("startup")
async def startup_watchdog():
    with WATCHDOG_LOCK:
        WATCHDOG_STATE["enabled"] = _watchdog_enabled()
        WATCHDOG_STATE["interval_seconds"] = _watchdog_interval_seconds()

        if WATCHDOG_STATE["enabled"] and not WATCHDOG_STATE["thread_started"]:
            thread = threading.Thread(target=_watchdog_loop, daemon=True, name="efro-watchdog")
            thread.start()
            WATCHDOG_STATE["thread_started"] = True
            log_message(
                f"WATCHDOG_START enabled=1 interval_seconds={WATCHDOG_STATE['interval_seconds']} shop=efro"
            )
        else:
            log_message(
                f"WATCHDOG_START enabled={1 if WATCHDOG_STATE['enabled'] else 0} "
                f"interval_seconds={WATCHDOG_STATE['interval_seconds']} thread_started={WATCHDOG_STATE['thread_started']}"
            )
# --- Vercel API (Logs, Deployments) ---

def vercel_request(endpoint: str, api_token: str, method="GET", data=None):
    import requests
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    url = f"https://api.vercel.com/{endpoint}"

    if method != "GET":
        return "DISABLED: non-read Vercel requests removed"

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        return f"Fehler bei Vercel-API: {e}"

def get_vercel_logs(project_id: str):
    """Ruft die letzten Deployment-Logs von Vercel ab (verwendet globalen VERCEL_TOKEN)."""
    if not VERCEL_TOKEN:
        return "VERCEL_TOKEN nicht gesetzt"
    endpoint = f"v1/deployments/{project_id}/events"
    return vercel_request(endpoint, VERCEL_TOKEN)

def get_vercel_deployments(team_id: str):
    """Ruft Deployments eines Projekts ab (verwendet globalen VERCEL_TOKEN)."""
    if not VERCEL_TOKEN:
        return "VERCEL_TOKEN nicht gesetzt"
    endpoint = f"v1/projects/efro-brain/deployments?teamId={team_id}"
    return vercel_request(endpoint, VERCEL_TOKEN)
    

# --- Render API ---

def render_request(endpoint: str, api_token: str, method="GET", data=None):
    import requests
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    url = f"https://api.render.com/{endpoint}"

    if method != "GET":
        return "DISABLED: non-read Render requests removed"

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        return f"Fehler bei Render-API: {e}"

def get_render_logs(service_id: str):
    """Ruft die Logs eines Render-Service ab (verwendet globalen RENDER_TOKEN)."""
    if not RENDER_TOKEN:
        return "RENDER_TOKEN nicht gesetzt"
    endpoint = f"v1/services/{service_id}/logs"
    return render_request(endpoint, RENDER_TOKEN)
    
    
# --- Supabase Client (Tabellen lesen/erstellen) ---

def supabase_query(table: str, supabase_url: str, supabase_key: str, query_type="select", data=None):
    import requests
    if query_type != "select":
        return "DISABLED: non-read Supabase queries removed"
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json"
    }
    url = f"{supabase_url}/rest/v1/{table}"
    resp = requests.get(url, headers=headers)
    return resp.text

def list_tables():
    """Listet alle Tabellen in Supabase auf (verwendet globale SUPABASE_URL und SUPABASE_KEY)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return "SUPABASE_URL oder SUPABASE_KEY nicht gesetzt"
    import requests
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    url = f"{SUPABASE_URL}/rest/v1/"
    resp = requests.get(url, headers=headers)
    return resp.text


# --- ElevenLabs API (Agent-Einstellungen) ---

def elevenlabs_request(endpoint: str, api_key: str, method="GET", data=None):
    import requests
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json"
    }
    url = f"https://api.elevenlabs.io/v1/{endpoint}"
    if method != "GET":
        return "DISABLED: non-read ElevenLabs requests removed"
    resp = requests.get(url, headers=headers)
    return resp.text

def get_elevenlabs_agent(agent_id: str):
    """Ruft ElevenLabs Agent-Konfiguration ab (verwendet globalen ELEVENLABS_KEY)."""
    if not ELEVENLABS_KEY:
        return "ELEVENLABS_API_KEY nicht gesetzt"
    return elevenlabs_request(f"conversational-ai/agents/{agent_id}", ELEVENLABS_KEY)

def update_elevenlabs_agent(agent_id: str, api_key: str, config: dict):
    return "DISABLED: ElevenLabs agent mutation removed"
    
   
 
# --- Agent-Kern mit Tool-Unterstützung ---
class EfroAgent:
    def __init__(self):
        self.memory = []

        # EFRO MASTER PROMPT LADEN
        try:
            with open("/opt/efro-agent/EFRO_MASTER_PROMPT.txt", "r", encoding="utf-8") as f:
                self.system_prompt = f.read()
        except Exception:
            self.system_prompt = "EFRO MASTER PROMPT NICHT GEFUNDEN"

        self.runtime_prompt = """
RUNTIME-LAYER:
- Nutze den Master Prompt als strategische Wahrheit, aber antworte operativ, kompakt und faktenbasiert.
- Behandle EFRO als Produkt-, Sales-, Monitoring- und Operator-System, nicht nur als UI oder Prompt.
- Priorität in der Laufzeit: verifizieren, eingrenzen, dann handeln.

WAHRHEITSREGELN:
- Keine finale technische Bewertung ohne Tool-Ergebnis, Code, Logs oder klar benannten Ist-Zustand.
- Wenn etwas nicht geprüft wurde, sage explizit: nicht verifiziert.
- Erfinde keine Technologien, Dateien, Tests, Deployments oder Prozentwerte.
- Wenn ein Tool sinnvoll ist, benutze es zuerst.
- Beschreibe Optimierungen nur dann als erledigt, wenn sie wirklich geprüft oder ausgeführt wurden.

TOOLS:
- linter <repo>
- build <repo>
- test <repo>
- read_file <path>
- vercel_logs <project_id>
- render_logs <service_id>
- supabase_tables
- elevenlabs_agent <agent_id>

TOOL-FORMAT:
```tool
tool_name
param1
param2
```

AUSGABESTIL:
- Denke wie ein Senior Engineer / CTO.
- Arbeite schrittweise, ruhig und operativ.
- Nenne Risiken, Grenzen und offene Punkte klar.
- Bevorzuge konkrete nächste Schritte statt allgemeiner Theorie.
""".strip()

        self.task_overlays = {
            "general": """
ALLGEMEINER OVERLAY:
- Halte die Antwort fokussiert und auf den nächsten operativen Hebel gerichtet.
- Wenn keine Tool-Nutzung nötig ist, bleibe klar über den unverifizierten Status.
""".strip(),
            "repo_investigation": """
REPO-TRIAGE OVERLAY:
- Arbeite repo-bewusst.
- Bevorzuge Lint-, Build-, Test- und Log-Signale vor Vermutungen.
- Achte auf Breaking Changes, Runtime-Risiken und Deployment-Folgen.
""".strip(),
            "incident": """
INCIDENT OVERLAY:
- Denke in Priorität, Severity, Scope, Checks und nächster Operator-Aktion.
- Bevorzuge schnelle Eingrenzung, klare Hypothesen und risikoarme nächste Schritte.
""".strip(),
            "control_center": """
CONTROL-CENTER OVERLAY:
- Denke in Dashboard, Operator-Workflow, Handoff und Monitoring-Sicht.
- Halte die Grenze zwischen efro-control-center und efro-agent bewusst sauber.
""".strip(),
        }

    def detect_overlay(self, user_input: str) -> str:
        text = user_input.lower()

        if any(keyword in text for keyword in ["incident", "alert", "severity", "priority", "watchdog", "playbook"]):
            return "incident"

        if any(keyword in text for keyword in ["control center", "dashboard", "handoff", "operator", "shop status"]):
            return "control_center"

        if any(repo_key in text for repo_key in REPO_PATHS) or any(keyword in text for keyword in ["repo", "build", "lint", "test", "bug", "deploy"]):
            return "repo_investigation"

        return "general"

    def format_extra_context(self, extra_context=None):
        if not extra_context:
            return "Kein zusätzlicher Tool-Kontext."

        if isinstance(extra_context, list):
            formatted_parts = []
            for item in extra_context[-8:]:
                if isinstance(item, tuple) and len(item) == 2:
                    role, content = item
                    formatted_parts.append(f"{role}: {content}")
                else:
                    formatted_parts.append(str(item))
            return "\n\n".join(formatted_parts)

        return str(extra_context)

    def format_memory(self):
        recent_memory = self.memory[-5:]
        if not recent_memory:
            return "Kein relevanter Verlauf."

        return "\n\n".join(
            [f"Nutzer: {u}\nAssistent: {a}" for u, a in recent_memory]
        )

    def build_prompt(self, user_input, extra_context=None):
        context = "Kein Kontext (Chroma deaktiviert für Speed)."
        overlay_name = self.detect_overlay(user_input)
        overlay = self.task_overlays.get(overlay_name, self.task_overlays["general"])
        repo_mentions = [repo_key for repo_key in REPO_PATHS if repo_key in user_input.lower()]
        repo_context = ", ".join(repo_mentions) if repo_mentions else "keine explizite Repo-Nennung"
        extra = self.format_extra_context(extra_context)

        return f"""
{self.system_prompt}

{self.runtime_prompt}

TASK-OVERLAY ({overlay_name}):
{overlay}

SYSTEM-ZUSTAND:
- Ziel: Probleme sicher analysieren, Belege sammeln und verständliche Incident-Reports schreiben
- Fokus: read-only, faktenbasiert, keine Änderungen, keine Optimierung
- Repo-Kontext: {repo_context}

KONTEXT:
{context}

VERLAUF:
{self.format_memory()}

ZUSÄTZLICHER KONTEXT:
{extra}

USER:
{user_input}

ANTWORT:
"""

    def query(self, user_input, extra_context=None):
        prompt = self.build_prompt(user_input, extra_context=extra_context)
        model_name = os.getenv("EFRO_AGENT_MODEL", "qwen2.5-coder:7b")

        try:
            response = ollama.chat(
                model=model_name,
                messages=[{"role": "user", "content": prompt}]
            )
            reply = response["message"]["content"]
        except Exception as e:
            reply = f"Agent-Fehler beim Modellaufruf ({model_name}): {e}"
            log_message(f"OLLAMA_ERROR model={model_name} error={e}")

        self.memory.append((user_input, reply))
        return reply


agent = EfroAgent()

# --- API-Endpunkte für direkte Tool-Aufrufe ---
class ToolRequest(BaseModel):
    tool: str
    params: list[str]


@app.post("/handoff", response_model=HandoffCreateResponse)
async def create_handoff(packet: HandoffPacket):
    record = create_handoff_record(packet)
    log_message(f"HANDOFF_CREATE handoff_id={record.handoff_id} incident_id={record.incident_id} repo={record.likely_repo}")
    return {
        "handoff_id": record.handoff_id,
        "handoff_path": f"/handoff/{record.handoff_id}",
        "packet": record.model_dump(),
    }


@app.get("/api/handoff/{handoff_id}")
async def get_handoff(handoff_id: str):
    record = load_handoff_record(handoff_id)
    log_message(f"HANDOFF_LOAD handoff_id={record.handoff_id} incident_id={record.incident_id}")
    return record.model_dump()


@app.get("/api/handoffs")
async def get_handoffs(limit: int = 25):
    safe_limit = max(1, min(limit, 100))
    records = list_handoff_records(limit=safe_limit)
    return {
        "count": len(records),
        "limit": safe_limit,
        "items": records,
    }


@app.get("/api/cost-ledger/rate-card")
async def get_cost_ledger_rate_card(request: Request):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="cost rate card is local-only")
    return {"ok": True, "currency": "USD", "rate_card": _cost_rate_card()}


@app.post("/api/cost-ledger/estimate")
async def estimate_cost_ledger_event(event: CostEstimateInput, request: Request):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="cost estimates are local-only")
    estimate = estimate_cost(event)
    ledger_record: CostLedgerRecord | None = None
    if event.write_ledger:
        ledger_event = CostLedgerEvent(
            shop_domain=event.shop_domain,
            subsystem=event.subsystem,
            endpoint=event.endpoint,
            provider=event.provider,
            operation=event.operation,
            request_id=event.request_id,
            session_id=event.session_id,
            cache_key=event.cache_key,
            cache_status=event.cache_status,
            input_size=None,
            output_size=None,
            tokens_in=event.tokens_in,
            tokens_out=event.tokens_out,
            characters=event.characters,
            estimated_cost=estimate.estimated_cost,
            currency=estimate.currency,
            observed_status=event.observed_status,
            latency_ms=event.latency_ms,
            notes=event.notes,
        )
        ledger_record = append_cost_ledger_event(ledger_event)
        estimate.ledger_record = ledger_record.model_dump()
    return estimate.model_dump()


@app.post("/api/cost-ledger/events")
async def create_cost_ledger_event(event: CostLedgerEvent, request: Request):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="cost ledger writes are local-only")
    record = append_cost_ledger_event(event)
    return {"ok": True, "record": record.model_dump()}


@app.get("/api/cost-ledger/events")
async def get_cost_ledger_events(request: Request, limit: int = 100):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="cost ledger reads are local-only")
    safe_limit = max(1, min(limit, 1000))
    records = read_cost_ledger_records(limit=safe_limit)
    return {"ok": True, "count": len(records), "limit": safe_limit, "items": list(reversed(records))}


@app.get("/api/cost-ledger/summary")
async def get_cost_ledger_summary(request: Request, limit: int = 250):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="cost ledger summary is local-only")
    safe_limit = max(1, min(limit, 5000))
    summary = summarize_cost_ledger(limit=safe_limit)
    return {"ok": True, "limit": safe_limit, "summary": summary.model_dump()}


@app.get("/api/watchdog/status")
async def get_watchdog_status():
    with WATCHDOG_LOCK:
        return {
            "enabled": WATCHDOG_STATE["enabled"],
            "interval_seconds": WATCHDOG_STATE["interval_seconds"],
            "last_run_at": WATCHDOG_STATE["last_run_at"],
            "thread_started": WATCHDOG_STATE["thread_started"],
            "supported_shops": ["efro"],
            "last_results": WATCHDOG_STATE["last_results"],
            "last_handoff_ids": WATCHDOG_STATE["last_handoff_ids"],
            "consecutive_failures": WATCHDOG_STATE["consecutive_failures"],
            "last_ok_at": WATCHDOG_STATE["last_ok_at"],
            "last_error_at": WATCHDOG_STATE["last_error_at"],
            "last_notified_status": WATCHDOG_STATE["last_notified_status"],
            "last_notified_at": WATCHDOG_STATE["last_notified_at"],
            "note": "Antwortqualitätsprüfung ist in dieser ersten Stufe nur kontraktbasiert, nicht voll semantisch.",
        }

@app.get("/api/watchdog/summary")
async def get_watchdog_summary(shop: str = "efro"):
    with WATCHDOG_LOCK:
        result = WATCHDOG_STATE["last_results"].get(shop) or {}
        public_key = f"{shop}:public_health"
        has_run = bool(WATCHDOG_STATE["last_run_at"]) and bool(result)

        return {
            "shop": shop,
            "supported": shop == "efro",
            "enabled": WATCHDOG_STATE["enabled"],
            "interval_seconds": WATCHDOG_STATE["interval_seconds"],
            "last_run_at": WATCHDOG_STATE["last_run_at"],
            "has_run": has_run,
            "bootstrap_required": not has_run,
            "summary_status": result.get("summary_status", "not_run_yet" if not has_run else "unknown"),
            "ok": result.get("ok"),
            "degraded": result.get("degraded"),
            "billing_mode": result.get("cost_mode", "unknown" if has_run else "not_run_yet"),
            "billing_warning": result.get("cost_warning"),
            "metered_checks_enabled": result.get("costly_checks_enabled", False),
            "metered_sources": result.get("cost_sources", []),
            "observed_failed_count": result.get("observed_failed_count"),
            "failed_count": result.get("failed_count"),
            "public_health_consecutive_failures": WATCHDOG_STATE["consecutive_failures"].get(public_key, 0),
            "public_health_incident_threshold": result.get("public_health_incident_threshold", _watchdog_public_failure_threshold()),
            "last_handoff_id": WATCHDOG_STATE["last_handoff_ids"].get(shop),
            "last_public_health_error_at": WATCHDOG_STATE["last_error_at"].get(public_key),
            "last_public_health_ok_at": WATCHDOG_STATE["last_ok_at"].get(public_key),
            "control_center_note": "Control Center soll primär diesen Summary-Status lesen; falls bootstrap_required=true, zuerst /api/watchdog/run ausführen oder den Watchdog-Loop aktivieren.",
        }

@app.api_route("/api/watchdog/run", methods=["GET", "POST"])
async def run_watchdog(shop: str = "efro"):
    return run_watchdog_cycle(shop)
@app.post("/tool")
async def call_tool(req: ToolRequest):
    if req.tool == "linter":
        if not req.params:
            return {"error": "Bitte Repo angeben (efro, brain, widget, shopify)"}
        repo = req.params[0]
        output = run_linter(repo)
        log_message(f"Tool linter repo={repo} -> {output[:200]}")
        return {"output": output}
    elif req.tool == "build":
        if not req.params:
            return {"error": "Bitte Repo angeben"}
        repo = req.params[0]
        output = run_build(repo)
        log_message(f"Tool build repo={repo} -> {output[:200]}")
        return {"output": output}
    elif req.tool == "test":
        if not req.params:
            return {"error": "Bitte Repo angeben"}
        repo = req.params[0]
        output = run_tests(repo)
        log_message(f"Tool test repo={repo} -> {output[:200]}")
        return {"output": output}
    elif req.tool == "write_file":
        log_message("Tool write_file -> blocked in read-only reporter mode")
        return {"error": "write_file ist deaktiviert. Dieser Agent arbeitet im read-only Incident-Reporter-Modus."}
    elif req.tool == "read_file":
        if not req.params:
            return {"error": "Bitte Pfad angeben"}
        path = req.params[0]
        content = read_file(path)
        log_message(f"Tool read_file path={path} -> {content[:200]}")
        return {"output": content}
    elif req.tool == "vercel_logs":
        if not req.params:
            return {"error": "Bitte project_id angeben"}
        project_id = req.params[0]
        output = get_vercel_logs(project_id)
        log_message(f"Tool vercel_logs project={project_id} -> {output[:200]}")
        return {"output": output}
    elif req.tool == "render_logs":
        if not req.params:
            return {"error": "Bitte service_id angeben"}
        service_id = req.params[0]
        output = get_render_logs(service_id)
        log_message(f"Tool render_logs service={service_id} -> {output[:200]}")
        return {"output": output}
    elif req.tool == "supabase_tables":
        output = list_tables()
        log_message(f"Tool supabase_tables -> {output[:200]}")
        return {"output": output}
    elif req.tool == "elevenlabs_agent":
        if not req.params:
            return {"error": "Bitte agent_id angeben"}
        agent_id = req.params[0]
        output = get_elevenlabs_agent(agent_id)
        log_message(f"Tool elevenlabs_agent agent={agent_id} -> {output[:200]}")
        return {"output": output}
    elif req.tool == "smoke_shopify":
        output = smoke_shopify()
        log_message(f"Tool smoke_shopify -> {output[:300]}")
        return {"output": output}    
    else:
        return {"error": f"Unbekanntes Tool: {req.tool}"}

# --- Chat-Endpunkt ---
class ChatRequest(BaseModel):
    message: Optional[str] = None
    prompt: Optional[str] = None
    text: Optional[str] = None
    handoff_id: Optional[str] = None

@app.post("/api/chat")
@app.post("/chat")
async def chat(req: ChatRequest):
    user_input = (
        getattr(req, "message", None)
        or getattr(req, "prompt", None)
        or getattr(req, "text", None)
        or ""
    ).strip()
    if not user_input:
        return {"reply": "", "message": "", "tool_results": [], "ok": False, "error": "Leere Nachricht"}
    handoff_context = None

    if req.handoff_id:
        try:
            handoff_record = load_handoff_record(req.handoff_id)
            handoff_context = (
                f"Handoff {handoff_record.handoff_id}\n"
                f"Incident: {handoff_record.incident_id}\n"
                f"Shop: {handoff_record.shop_domain}\n"
                f"Priorität: {handoff_record.priority}\n"
                f"Severity: {handoff_record.severity}\n"
                f"Scope: {handoff_record.scope}\n"
                f"Repo: {handoff_record.likely_repo}\n"
                f"Subsystem: {handoff_record.likely_subsystem}\n"
                f"Summary: {handoff_record.summary}\n"
                f"Top Findings: {' | '.join(handoff_record.top_findings) if handoff_record.top_findings else 'Keine Angaben'}\n"
                f"Checks Run: {' | '.join(handoff_record.checks_run) if handoff_record.checks_run else 'Keine Angaben'}\n"
                f"Recommended Next Action: {handoff_record.recommended_next_action}"
            )
        except HTTPException:
            handoff_context = f"Handoff {req.handoff_id} nicht gefunden"

    # -------- DIRECT COMMAND MODE (SCHNELL + VERTRAUENSWÜRDIG) --------
    direct = parse_direct_command(user_input)
    if direct:
        command, repo = direct

        if command == "status":
            return {
                "reply": "Agent läuft im read-only Incident-Reporter-Modus. Er sammelt Belege und schreibt Berichte, führt aber keine Optimierungen aus.",
                "tool_results": []
            }

        if command == "linter":
            output = run_linter(repo)
            log_message(f"DIRECT linter repo={repo} -> {output[:300]}")
            return {
                "reply": f"Direktbefehl ausgeführt: linter {repo}",
                "tool_results": [
                    {
                        "tool": "linter",
                        "params": [repo],
                        "output": output
                    }
                ]
            }

        if command == "build":
            output = run_build(repo)
            log_message(f"DIRECT build repo={repo} -> {output[:300]}")
            return {
                "reply": f"Direktbefehl ausgeführt: build {repo}",
                "tool_results": [
                    {
                        "tool": "build",
                        "params": [repo],
                        "output": output
                    }
                ]
            }

        if command == "test":
            output = run_tests(repo)
            log_message(f"DIRECT test repo={repo} -> {output[:300]}")
            return {
                "reply": f"Direktbefehl ausgeführt: test {repo}",
                "tool_results": [
                    {
                        "tool": "test",
                        "params": [repo],
                        "output": output
                    }
                ]
            }

        if command == "install":
            log_message(f"DIRECT install repo={repo} -> blocked in read-only reporter mode")
            return {
                "reply": "Install ist deaktiviert. Dieser Agent arbeitet als read-only Incident-Reporter und führt keine Änderungen aus.",
                "tool_results": []
            }

        if command == "smoke_shopify":
            output = smoke_shopify()
            log_message(f"DIRECT smoke_shopify -> {output[:300]}")
            return {
                "reply": "Direktbefehl ausgeführt: smoke shopify",
                "tool_results": [
                    {
                        "tool": "smoke_shopify",
                        "params": ["shopify"],
                        "output": output
                    }
                ]
            }    

        if command == "optimize":
            log_message(f"DIRECT optimize repo={repo} -> disabled in read-only reporter mode")
            return {
                "reply": "Optimize ist deaktiviert. Dieser Agent arbeitet als read-only Incident-Reporter und schreibt Berichte statt Änderungen auszuführen.",
                "tool_results": []
            }

    # -------- FALLBACK: LLM MODE --------
    max_tool_calls = 10
    current_input = user_input
    conversation_history = []

    if handoff_context:
        conversation_history.append(("handoff", handoff_context))

    for _ in range(max_tool_calls):
        reply = agent.query(current_input, extra_context=conversation_history)
        conversation_history.append(("assistant", reply))

        tool_pattern = r"```tool\s*(.*?)```"
        matches = re.findall(tool_pattern, reply, re.DOTALL)

        if not matches:
            return {"reply": reply, "message": reply, "content": reply, "tool_results": [], "ok": True}

        tool_results = []
        for match in matches:
            lines = [line.strip() for line in match.strip().splitlines() if line.strip()]
            if not lines:
                continue

            tool_name = lines[0]
            params = lines[1:]
            tool_req = ToolRequest(tool=tool_name, params=params)
            tool_resp = await call_tool(tool_req)

            output = tool_resp.get("output", tool_resp.get("error", ""))
            tool_results.append({
                "tool": tool_name,
                "params": params,
                "output": output
            })

            conversation_history.append(
                ("tool_result", f"Tool {tool_name} Ergebnis:\n{output}")
            )

        current_input = "Die folgenden Tools wurden ausgeführt und haben diese Ergebnisse geliefert:\n"
        for tr in tool_results:
            current_input += f"\nTool: {tr['tool']} {tr['params']}\nOutput:\n{tr['output']}\n"
        current_input += "\nBitte schreibe jetzt einen strukturierten Incident-Report auf Basis der verifizierten Tool-Ergebnisse. Keine Optimierung, keine Änderungen, markiere Unsicherheit klar als nicht verifiziert."

    return {
        "reply": reply,
        "tool_results": tool_results,
        "warning": "Maximale Anzahl Tool-Aufrufe erreicht."
    }

class TerminalRequest(BaseModel):
    cmd: Optional[str] = None
    command: Optional[str] = None
    repo: Optional[str] = None

@app.api_route("/api/terminal", methods=["GET", "POST"])
@app.api_route("/command", methods=["GET", "POST"])
@app.api_route("/terminal", methods=["GET", "POST"])
async def terminal(cmd: Optional[str] = None, repo: str = "brain", req: Optional[TerminalRequest] = None):
    effective_cmd = (cmd or (req.cmd if req else None) or (req.command if req else None) or "").strip()
    effective_repo = (((req.repo if req else None) or repo) or "brain").strip()
    cwd = REPO_PATHS.get(effective_repo, REPO_PATHS["brain"])

    if not effective_cmd:
        return {"output": "", "reply": "Kein Befehl übergeben", "ok": False, "repo": effective_repo}

    output = run_command(effective_cmd, cwd)
    log_message(f"TERMINAL: {effective_repo} $ {effective_cmd} -> {output[:200]}")
    return {"output": output, "reply": output, "log": output, "ok": True, "repo": effective_repo, "cmd": effective_cmd}


class OptimizeRequest(BaseModel):
    repo: str
    max_iterations: int = 5

# =========================
# FIX: optimize() BLOCK KOMPLETT ERSETZEN
# =========================

@app.post("/optimize")
async def optimize(req: OptimizeRequest):
    return {
        "warning": "Optimize ist deaktiviert. Dieser Agent arbeitet als read-only Incident-Reporter und schreibt Berichte statt Änderungen auszuführen."
    }

    results = {
        "linter": {"success": False, "iterations": 0, "log": ""},
        "build": {"success": False, "iterations": 0, "log": ""},
        "test": {"success": False, "iterations": 0, "log": ""}
    }

    async def run_step(step_name, run_func, fix_prompt_template):
        for i in range(max_iter):
            output = run_func(repo)
            results[step_name]["log"] += f"\n--- Versuch {i+1} ---\n{output}\n"

            if "error" not in output.lower() and "failed" not in output.lower() and "✖" not in output:
                results[step_name]["success"] = True
                results[step_name]["iterations"] = i + 1
                return True

            docs = collection.query(query_texts=[output], n_results=5)
            context = "\n---\n".join(docs['documents'][0]) if docs['documents'] else "Kein passender Code gefunden."

            prompt = fix_prompt_template.format(error=output, context=context, repo=repo)

            try:
                resp = ollama.chat(
                    model="qwen2.5-coder:7b",
                    messages=[{"role": "user", "content": prompt}]
                )
                fix = resp['message']['content']

                pattern = r"```file\s+(.*?)\n(.*?)```"
                matches = re.findall(pattern, fix, re.DOTALL)

                if not matches:
                    return False

                for file_path, content in matches:
                    full_path = os.path.join(cwd, file_path) if not os.path.isabs(file_path) else file_path
                    write_file(full_path, content)

            except Exception as e:
                results[step_name]["log"] += f"\nFehler: {e}\n"
                return False

        return False

    linter_prompt = """
Behebe Linter-Fehler in {repo}.

Fehler:
{error}

Kontext:
{context}

Antwort nur als ```file Blöcke.
"""

    build_prompt = """
Behebe Build-Fehler in {repo}.

Fehler:
{error}

Kontext:
{context}

Antwort nur als ```file Blöcke.
"""

    test_prompt = """
Behebe Test-Fehler in {repo}.

Fehler:
{error}

Kontext:
{context}

Antwort nur als ```file Blöcke.
"""

    await run_step("linter", run_linter, linter_prompt)
    await run_step("build", run_build, build_prompt)
    await run_step("test", run_tests, test_prompt)

    



@app.get("/api/logs")
@app.get("/api/log")
@app.get("/logs")
@app.get("/log")
async def get_log():
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = [line.rstrip("\n") for line in f.readlines()]
        tail_lines = lines[-200:]
        return {
            "log": "\n".join(tail_lines),
            "lines": tail_lines,
            "total_lines": len(lines)
        }
    except Exception as e:
        return {
            "log": f"Log file not readable: {e}",
            "lines": [],
            "total_lines": 0
        }

@app.get("/api/health")
@app.get("/health")
async def health():
    _ensure_handoff_dir()
    model_name = os.getenv("EFRO_AGENT_MODEL", "qwen2.5-coder:7b")
    handoff_count = len(list_handoff_records(limit=100))

    return {
        "status": "ok",
        "service": "efro-agent",
        "time": datetime.now().isoformat(),
        "model": model_name,
        "handoff_dir": HANDOFF_DIR,
        "handoff_dir_exists": os.path.isdir(HANDOFF_DIR),
        "handoff_count": handoff_count,
        "repos": sorted(REPO_PATHS.keys()),
    }

@app.get("/")
@app.get("/handoff/{handoff_id}")
async def root(handoff_id: Optional[str] = None):
    html = '''<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Efro Agent</title>
    <style>
        :root {
            color-scheme: dark;
            --bg: #0b1020;
            --panel: #121934;
            --panel-2: #172040;
            --panel-3: #1e2748;
            --border: #283355;
            --text: #e8ecf8;
            --muted: #9ca9cf;
            --accent: #87b3ff;
            --accent-2: #3dd9b0;
            --danger: #d64545;
            --warning: #d9822b;
            --shadow: 0 18px 50px rgba(0, 0, 0, 0.28);
        }

        * { box-sizing: border-box; }
        html, body { height: 100%; }
        body {
            margin: 0;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: linear-gradient(180deg, #0b1020 0%, #0d1327 100%);
            color: var(--text);
        }

        .app-shell {
            min-height: 100vh;
            padding: 20px;
            display: grid;
            grid-template-rows: auto 1fr;
            gap: 18px;
        }

        .topbar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 14px;
            padding: 16px 18px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: rgba(18, 25, 52, 0.9);
            box-shadow: var(--shadow);
        }

        .title-wrap h1 {
            margin: 0;
            font-size: 20px;
            line-height: 1.15;
        }

        .title-wrap p {
            margin: 6px 0 0;
            color: var(--muted);
            font-size: 13px;
            max-width: 760px;
        }

        .status-cluster {
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        .status-badge,
        .meta-pill {
            border: 1px solid var(--border);
            background: var(--panel-3);
            border-radius: 999px;
            padding: 7px 11px;
            font-size: 12px;
            color: var(--muted);
        }

        .status-badge.ready { color: #b8ffd6; border-color: rgba(61, 217, 176, 0.35); }
        .status-badge.thinking { color: #d7e3ff; border-color: rgba(124, 156, 255, 0.35); }
        .status-badge.error { color: #ffd4d9; border-color: rgba(255, 107, 122, 0.35); }

        .main-grid {
            min-height: 0;
            display: grid;
            grid-template-columns: minmax(420px, 1.25fr) minmax(340px, 0.75fr);
            gap: 16px;
        }

        .panel {
            min-height: 0;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: rgba(18, 25, 52, 0.92);
            box-shadow: var(--shadow);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .panel-header {
            padding: 14px 16px 12px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
        }

        .panel-title {
            margin: 0;
            font-size: 14px;
        }

        .panel-subtitle {
            margin: 4px 0 0;
            font-size: 12px;
            color: var(--muted);
        }

        .messages {
            flex: 1;
            min-height: 0;
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 9px;
        }

        .message {
            max-width: 90%;
            padding: 12px 14px;
            border-radius: 14px;
            line-height: 1.45;
            white-space: pre-wrap;
            word-break: break-word;
            border: 1px solid transparent;
        }

        .message.user {
            align-self: flex-end;
            background: rgba(135, 179, 255, 0.14);
            border-color: rgba(135, 179, 255, 0.26);
            color: #eaf1ff;
        }

        .message.assistant {
            align-self: flex-start;
            background: rgba(255, 255, 255, 0.03);
            border-color: var(--border);
            color: var(--text);
        }

        .message.tool {
            align-self: flex-start;
            background: rgba(61, 217, 176, 0.10);
            border-color: rgba(61, 217, 176, 0.20);
            color: #d8fff4;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        }

        .message.system {
            align-self: flex-start;
            background: rgba(217, 130, 43, 0.10);
            border-color: rgba(217, 130, 43, 0.20);
            color: #ffe7c2;
        }

        .composer {
            border-top: 1px solid var(--border);
            padding: 13px 15px 15px;
            display: flex;
            gap: 10px;
            align-items: center;
            background: rgba(30, 39, 72, 0.45);
        }

        .text-input,
        .command-input,
        .repo-select {
            width: 100%;
            border: 1px solid var(--border);
            background: var(--panel-3);
            color: var(--text);
            border-radius: 12px;
            padding: 12px 14px;
            outline: none;
            font-size: 14px;
        }

        .button-row {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }

        button {
            border: 1px solid var(--border);
            background: var(--panel-2);
            color: var(--text);
            border-radius: 12px;
            padding: 10px 14px;
            cursor: pointer;
            font-size: 13px;
            transition: transform 0.12s ease, border-color 0.12s ease, background 0.12s ease;
        }

        button:hover {
            transform: translateY(-1px);
            border-color: rgba(135, 179, 255, 0.35);
            background: rgba(255, 255, 255, 0.04);
        }

        button.primary {
            background: rgba(135, 179, 255, 0.12);
            border-color: rgba(135, 179, 255, 0.24);
            color: #dce8ff;
        }

        button.ghost {
            background: transparent;
        }

        .terminal-wrap {
            flex: 1;
            min-height: 0;
            display: flex;
            flex-direction: column;
        }

        .terminal-toolbar {
            padding: 14px 18px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
            background: rgba(30, 39, 72, 0.24);
        }

        .terminal-controls,
        .terminal-toggles {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }

        .toggle {
            display: inline-flex;
            gap: 8px;
            align-items: center;
            color: var(--muted);
            font-size: 12px;
        }

        .terminal-output {
            flex: 1;
            min-height: 0;
            overflow-y: auto;
            padding: 16px 18px 20px;
            background: rgba(11, 16, 32, 0.72);
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 12px;
            line-height: 1.5;
        }

        .terminal-line {
            margin: 0 0 8px;
            padding: 0;
            white-space: pre-wrap;
            word-break: break-word;
            color: #dbe4ff;
        }

        .terminal-line.command {
            color: #9dc1ff;
        }

        .terminal-line.muted {
            color: var(--muted);
        }

        .terminal-footer {
            border-top: 1px solid var(--border);
            padding: 14px 16px 16px;
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto auto;
            gap: 9px;
            background: rgba(30, 39, 72, 0.45);
        }

        .empty-state {
            color: var(--muted);
            font-size: 13px;
        }

        .handoff-panel {
            margin: 0 18px 0;
            padding: 14px;
            border: 1px solid var(--border);
            border-radius: 16px;
            background: rgba(18, 25, 52, 0.58);
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .handoff-panel-title {
            margin: 0;
            font-size: 13px;
            color: var(--muted);
        }

        .handoff-list {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }

        .handoff-item {
            border: 1px solid var(--border);
            border-radius: 14px;
            background: rgba(11, 16, 32, 0.68);
            padding: 12px;
            text-decoration: none;
            color: var(--text);
            transition: border-color 0.12s ease, transform 0.12s ease;
        }

        .handoff-item:hover {
            border-color: rgba(135, 179, 255, 0.24);
            transform: none;
        }

        .handoff-item.active {
            border-color: rgba(61, 217, 176, 0.28);
            box-shadow: inset 0 0 0 1px rgba(61, 217, 176, 0.12);
        }

        .handoff-item-header {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: center;
            margin-bottom: 8px;
            font-size: 12px;
        }

        .handoff-item-title {
            font-weight: 600;
            color: var(--text);
        }

        .handoff-item-meta {
            color: var(--muted);
            font-size: 11px;
        }

        .handoff-item-summary {
            color: var(--muted);
            font-size: 12px;
            line-height: 1.45;
        }

        @media (max-width: 1100px) {
            .main-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body data-handoff-id="__HANDOFF_ID__">
<div class="app-shell">
    <header class="topbar">
        <div class="title-wrap">
            <h1>EFRO Agent Control Surface</h1>
            <p>Operator-Assistenz für Repos, Incidents und technische Prüfung.</p>
        </div>
        <div class="status-cluster">
            <div id="status" class="status-badge ready">Bereit</div>
            <div id="runtime-pill" class="meta-pill">Lade Runtime…</div>
            <div id="handoff-pill" class="meta-pill" style="display:none;"></div>
            <div class="meta-pill">UI Fokus: ruhig, lesbar, operativ</div>
        </div>
    </header>

    <main class="main-grid">
        <section class="panel">
            <div class="panel-header">
                <div>
                    <h2 class="panel-title">Chat</h2>
                    <p class="panel-subtitle">Nachrichten, Tool-Ergebnisse und Agent-Antworten</p>
                </div>
            </div>
            <div class="handoff-panel">
                <h3 class="handoff-panel-title">Letzte Handoffs</h3>
                <div id="handoff-list" class="handoff-list">
                    <div class="empty-state">Noch keine Handoffs geladen.</div>
                </div>
            </div>
            <div id="messages" class="messages">
                <div class="empty-state">Noch keine Unterhaltung. Starte mit einer Nachricht, prüfe Handoffs oder nutze rechts einen direkten Repo-Befehl für eine technische Sichtung.</div>
            </div>
            <div class="composer">
                <input type="text" id="message-input" class="text-input" placeholder="Nachricht an den Agenten...">
                <button id="send-btn" class="primary">Senden</button>
            </div>
        </section>

        <section class="panel">
            <div class="panel-header">
                <div>
                    <h2 class="panel-title">Terminal & Logs</h2>
                    <p class="panel-subtitle">Stabile Log-Ansicht ohne komplettes Rebuild bei jedem Poll</p>
                </div>
            </div>

            <div class="terminal-wrap">
                <div class="terminal-toolbar">
                    <div class="terminal-controls">
                        <select id="repo-select" class="repo-select">
                            <option value="efro">efro · Landing Page</option>
                            <option value="brain">brain · API</option>
                            <option value="widget">widget · Avatar</option>
                            <option value="shopify">shopify · App</option>
                        </select>
                        <button id="clear-terminal" class="ghost">Clear Terminal</button>
                        <button id="copy-terminal" class="ghost">Copy Logs</button>
                    </div>
                    <div class="terminal-toggles">
                        <label class="toggle"><input type="checkbox" id="autoscroll-toggle" checked> Autoscroll</label>
                        <label class="toggle"><input type="checkbox" id="pause-logs-toggle"> Pause Logs</label>
                        <div id="log-meta" class="meta-pill">0 Zeilen</div>
                    </div>
                </div>

                <div id="terminal-output" class="terminal-output">
                    <div class="empty-state">Noch keine Log-Ausgabe geladen.</div>
                </div>

                <div class="terminal-footer">
                    <input type="text" id="cmd-input" class="command-input" placeholder="Befehl eingeben...">
                    <button id="run-cmd">Ausführen</button>
                    <button id="refresh-logs" class="ghost">Logs aktualisieren</button>
                </div>
            </div>
        </section>
    </main>
</div>

<script>
document.addEventListener('DOMContentLoaded', () => {
    const messagesDiv = document.getElementById('messages');
    const terminalDiv = document.getElementById('terminal-output');
    const messageInput = document.getElementById('message-input');
    const sendBtn = document.getElementById('send-btn');
    const cmdInput = document.getElementById('cmd-input');
    const runCmdBtn = document.getElementById('run-cmd');
    const repoSelect = document.getElementById('repo-select');
    const statusDiv = document.getElementById('status');
    const clearTerminalBtn = document.getElementById('clear-terminal');
    const copyTerminalBtn = document.getElementById('copy-terminal');
    const refreshLogsBtn = document.getElementById('refresh-logs');
    const autoscrollToggle = document.getElementById('autoscroll-toggle');
    const pauseLogsToggle = document.getElementById('pause-logs-toggle');
    const logMeta = document.getElementById('log-meta');
    const handoffId = document.body.dataset.handoffId;
    const runtimePill = document.getElementById('runtime-pill');
    const handoffPill = document.getElementById('handoff-pill');
    const handoffList = document.getElementById('handoff-list');

    let terminalInitialized = false;
    let lastRenderedLineCount = 0;

    function renderRecentHandoffs(items) {
        if (!handoffList) return;

        if (!items || items.length === 0) {
            handoffList.innerHTML = '<div class="empty-state">Noch keine Handoffs vorhanden.</div>';
            return;
        }

        handoffList.innerHTML = '';
        for (const item of items) {
            const card = document.createElement('a');
            card.className = `handoff-item ${item.handoff_id === handoffId ? 'active' : ''}`.trim();
            card.href = `/handoff/${encodeURIComponent(item.handoff_id)}`;

            const summary = (item.summary || 'Keine Zusammenfassung').trim();
            const shortSummary = summary.length > 140 ? `${summary.slice(0, 137)}...` : summary;

            const header = document.createElement('div');
            header.className = 'handoff-item-header';

            const title = document.createElement('div');
            title.className = 'handoff-item-title';
            title.textContent = item.incident_id || 'Unbekannter Incident';

            const metaRight = document.createElement('div');
            metaRight.className = 'handoff-item-meta';
            metaRight.textContent = `${item.priority || 'n/a'} · ${item.severity || 'n/a'}`;

            header.appendChild(title);
            header.appendChild(metaRight);

            const metaLine = document.createElement('div');
            metaLine.className = 'handoff-item-meta';
            metaLine.textContent = `${item.shop_domain || 'kein Shop'} · ${item.likely_repo || 'kein Repo'} / ${item.likely_subsystem || 'kein Subsystem'}`;

            const summaryLine = document.createElement('div');
            summaryLine.className = 'handoff-item-summary';
            summaryLine.textContent = shortSummary;

            card.appendChild(header);
            card.appendChild(metaLine);
            card.appendChild(summaryLine);
            handoffList.appendChild(card);
        }
    }

    async function loadRecentHandoffs() {
        if (!handoffList) return;

        try {
            const resp = await fetch('/api/handoffs?limit=8');
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }
            const data = await resp.json();
            renderRecentHandoffs(data.items || []);
        } catch (err) {
            handoffList.innerHTML = `<div class="empty-state">Handoffs konnten nicht geladen werden: ${err.message}</div>`;
        }
    }

    async function loadHealthStatus() {
        if (!runtimePill) return;

        try {
            const resp = await fetch('/health');
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }
            const data = await resp.json();
            runtimePill.innerText = `Runtime · ${data.model || 'n/a'} · Handoffs ${data.handoff_count ?? 'n/a'}`;
        } catch (err) {
            runtimePill.innerText = `Runtime Fehler · ${err.message}`;
        }
    }

    async function loadHandoffContext() {
        if (!handoffId) return;

        try {
            const resp = await fetch(`/api/handoff/${encodeURIComponent(handoffId)}`);
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}`);
            }

            const packet = await resp.json();
            handoffPill.style.display = 'inline-flex';
            handoffPill.innerText = `Handoff · ${packet.handoff_id}`;

            clearEmptyState(messagesDiv);
            addMessage('system', `Handoff geladen\n\nIncident: ${packet.incident_id}\nShop: ${packet.shop_domain}\nPriorität: ${packet.priority}\nSeverity: ${packet.severity}\nScope: ${packet.scope}\nRepo: ${packet.likely_repo}\nSubsystem: ${packet.likely_subsystem}`);
            addMessage('assistant', `Zusammenfassung:\n${packet.summary}\n\nTop Findings:\n- ${(packet.top_findings || []).join('\\n- ') || 'Keine Angaben'}\n\nChecks Run:\n- ${(packet.checks_run || []).join('\\n- ') || 'Keine Angaben'}\n\nNächste empfohlene Aktion:\n${packet.recommended_next_action}`);
            messageInput.value = `Untersuche Handoff ${packet.handoff_id} für ${packet.shop_domain} im Repo ${packet.likely_repo}. Starte mit einer verifizierten Triage.`;
            setStatus('ready', 'Handoff geladen');
        } catch (err) {
            handoffPill.style.display = 'inline-flex';
            handoffPill.innerText = `Handoff Fehler`;
            addMessage('system', `Handoff konnte nicht geladen werden: ${err.message}`);
            setStatus('error', 'Handoff Fehler');
        }
    }

    function setStatus(state, label) {
        statusDiv.className = `status-badge ${state}`;
        statusDiv.innerText = label;
    }

    function clearEmptyState(container) {
        const empty = container.querySelector('.empty-state');
        if (empty) empty.remove();
    }

    function addMessage(role, text, extraClass = '') {
        clearEmptyState(messagesDiv);
        const msgDiv = document.createElement('div');
        msgDiv.className = `message ${role} ${extraClass}`.trim();
        msgDiv.innerText = text;
        messagesDiv.appendChild(msgDiv);
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    function appendTerminalLine(text, extraClass = '') {
        clearEmptyState(terminalDiv);
        const pre = document.createElement('pre');
        pre.className = `terminal-line ${extraClass}`.trim();
        pre.innerText = text;
        terminalDiv.appendChild(pre);
        if (autoscrollToggle.checked) {
            terminalDiv.scrollTop = terminalDiv.scrollHeight;
        }
    }

    function resetTerminal() {
        terminalDiv.innerHTML = '<div class="empty-state">Terminal geleert. Neue Log-Zeilen erscheinen hier automatisch.</div>';
        terminalInitialized = true;
        logMeta.innerText = 'Ansicht geleert';
    }

    async function sendMessage() {
        const msg = messageInput.value.trim();
        if (!msg) return;

        setStatus('thinking', 'Agent denkt nach');
        addMessage('user', msg);
        messageInput.value = '';

        try {
            const response = await fetch('/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: msg, handoff_id: handoffId || null })
            });

            const data = await response.json();

            if (data.tool_results && data.tool_results.length > 0) {
                for (const tr of data.tool_results) {
                    addMessage('tool', `🔧 Tool: ${tr.tool} ${tr.params.join(' ')}`);
                    addMessage('tool', tr.output || 'Keine Tool-Ausgabe');
                }
            }

            if (data.reply) {
                addMessage('assistant', data.reply);
                setStatus('ready', 'Bereit');
            } else if (data.warning) {
                addMessage('system', data.warning);
                setStatus('error', 'Warnung');
            } else {
                addMessage('system', 'Keine gültige Antwort vom Server');
                setStatus('error', 'Fehler');
            }
        } catch (err) {
            console.error('SEND ERROR:', err);
            addMessage('system', `Fehler: ${err.message}`);
            setStatus('error', 'Fehler');
        }
    }

    async function runCommand() {
        const cmd = cmdInput.value.trim();
        if (!cmd) return;

        const repo = repoSelect.value;
        appendTerminalLine(`> ${cmd} (in ${repo})`, 'command');

        try {
            const response = await fetch(`/terminal?cmd=${encodeURIComponent(cmd)}&repo=${encodeURIComponent(repo)}`);
            const data = await response.json();
            appendTerminalLine(data.output || 'Keine Ausgabe');
        } catch (err) {
            appendTerminalLine(`Fehler: ${err.message}`);
        }

        cmdInput.value = '';
    }

    async function fetchLogs(force = false) {
        if (pauseLogsToggle.checked && !force) return;

        try {
            const resp = await fetch('/log');
            const data = await resp.json();
            const lines = Array.isArray(data.lines) ? data.lines : [];

            logMeta.innerText = `${data.total_lines || lines.length} Zeilen`;

            if (!terminalInitialized) {
                terminalDiv.innerHTML = '';
                for (const line of lines) {
                    if (line.trim()) appendTerminalLine(line);
                }
                terminalInitialized = true;
                lastRenderedLineCount = lines.length;
                return;
            }

            if (lines.length < lastRenderedLineCount) {
                terminalDiv.innerHTML = '';
                for (const line of lines) {
                    if (line.trim()) appendTerminalLine(line);
                }
                lastRenderedLineCount = lines.length;
                return;
            }

            const newLines = lines.slice(lastRenderedLineCount);
            for (const line of newLines) {
                if (line.trim()) appendTerminalLine(line);
            }
            lastRenderedLineCount = lines.length;
        } catch (err) {
            console.error('Log-Fehler:', err);
        }
    }

    sendBtn.onclick = sendMessage;
    runCmdBtn.onclick = runCommand;
    clearTerminalBtn.onclick = resetTerminal;
    copyTerminalBtn.onclick = async () => {
        try {
            await navigator.clipboard.writeText(terminalDiv.innerText || '');
            appendTerminalLine('[info] Logs in die Zwischenablage kopiert.', 'muted');
        } catch (err) {
            appendTerminalLine(`[warn] Copy fehlgeschlagen: ${err.message}`, 'muted');
        }
    };
    refreshLogsBtn.onclick = () => fetchLogs(true);

    messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') sendMessage();
    });

    cmdInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') runCommand();
    });

    setInterval(() => fetchLogs(false), 2000);
    setInterval(() => loadRecentHandoffs(), 15000);
    setInterval(() => loadHealthStatus(), 15000);
    fetchLogs(true);
    loadRecentHandoffs();
    loadHealthStatus();
    loadHandoffContext();
});
</script>
</body>
</html>'''
    html = html.replace("__HANDOFF_ID__", handoff_id or "")
    return HTMLResponse(content=html)

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("EFRO_AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("EFRO_AGENT_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
