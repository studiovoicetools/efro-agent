import os
import subprocess
import json
import re
from pathlib import Path
from threading import Lock
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from datetime import datetime

try:
    import ollama  # type: ignore
except ImportError:
    ollama = None

try:
    import chromadb  # type: ignore
    from chromadb.utils import embedding_functions  # type: ignore
except ImportError:
    chromadb = None
    embedding_functions = None

try:
    from sentence_transformers import SentenceTransformer  # type: ignore
except ImportError:
    SentenceTransformer = None

load_dotenv()

# --- Logging / Runtime / Status ---
BASE_DIR = Path(os.getenv("EFRO_AGENT_BASE_DIR", "/opt/efro-agent"))
LOG_FILE = str(BASE_DIR / "agent.log")
HOST = os.getenv("EFRO_AGENT_HOST", "0.0.0.0")
PORT = int(os.getenv("EFRO_AGENT_PORT", "8000"))
LOG_TAIL_LINES = int(os.getenv("EFRO_AGENT_LOG_TAIL_LINES", "400"))
LOG_LOCK = Lock()
STATUS_STATE: Dict[str, Any] = {
    "state": "idle",
    "detail": "Bereit",
    "last_error": None,
    "last_chat_at": None,
    "last_tool_at": None,
}

def set_status(state: str, detail: str, error: Optional[str] = None):
    STATUS_STATE["state"] = state
    STATUS_STATE["detail"] = detail
    STATUS_STATE["last_error"] = error


def log_message(msg: str):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with LOG_LOCK:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now().isoformat()}] {msg}\n")
    except Exception as e:
        print("Logging Fehler:", e)

VERCEL_TOKEN = os.getenv("VERCEL_TOKEN")
RENDER_TOKEN = os.getenv("RENDER_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
ELEVENLABS_KEY = os.getenv("ELEVENLABS_API_KEY")


# ---------- Konfiguration ----------
REPO_PATHS = {
    "efro": "/opt/efro-agent/repos/efro",
    "brain": "/opt/efro-agent/repos/efro-brain",
    "widget": "/opt/efro-agent/repos/efro-widget",
    "shopify": "/opt/efro-agent/repos/efro-shopify"
}
# -----------------------------------

# --- Embedding-Funktion (lokal / optional) ---
if embedding_functions is not None:
    class LocalEmbeddingFunction(embedding_functions.EmbeddingFunction):
        def __init__(self):
            if SentenceTransformer is None:
                raise RuntimeError("sentence_transformers nicht verfügbar")
            self.model = SentenceTransformer('all-MiniLM-L6-v2')

        def __call__(self, texts):
            return self.model.encode(texts).tolist()
else:
    class LocalEmbeddingFunction:
        def __init__(self):
            raise RuntimeError("chromadb embedding_functions nicht verfügbar")

        def __call__(self, texts):
            raise RuntimeError("Embedding-Funktion nicht verfügbar")

# --- Chroma Client (optional) ---
client = None
embedding_fn = None
collection = None
if chromadb is not None and embedding_functions is not None and SentenceTransformer is not None:
    try:
        client = chromadb.PersistentClient(path=str(BASE_DIR / "chroma_db"))
        embedding_fn = LocalEmbeddingFunction()
        collection = client.get_or_create_collection(
            name="efro_code",
            embedding_function=embedding_fn
        )
    except Exception as e:
        log_message(f"Chroma deaktiviert: {e}")
        client = None
        embedding_fn = None
        collection = None

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
    if text in ("status", "agent status"):
        return ("status", None)

    patterns = [
        (r"^(?:linter|lint)\s+(efro|brain|widget|shopify)$", "linter"),
        (r"^build\s+(efro|brain|widget|shopify)$", "build"),
        (r"^test\s+(efro|brain|widget|shopify)$", "test"),
        (r"^install\s+(efro|brain|widget|shopify)$", "install"),
        (r"^optimize\s+(efro|brain|widget|shopify)$", "optimize"),
        (r"^smoke\s+shopify$", "smoke_shopify"),
    ]

    for pattern, command in patterns:
        match = re.match(pattern, text)
        if match:
            repo = match.group(1) if match.groups() else None
            return (command, repo)

    return None


def write_file(path, content):
    return "DISABLED: arbitrary file writes removed"

def read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Fehler beim Lesen: {e}"
        
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
    resp = requests.get(url, headers=headers)
    return resp.text

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
    resp = requests.get(url, headers=headers)
    return resp.text

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
RUNTIME_RULES = """
REGELN FÜR WAHRHEIT:
- Du darfst keine Zusammenfassungen geben, ohne vorher Tools benutzt zu haben.
- Jede technische Aussage muss auf einem Tool-Ergebnis basieren.
- Wenn kein Tool genutzt wurde, darfst du keine finale Bewertung geben.
- Wenn du unsicher bist, sage explizit: nicht verifiziert.
- Bevor du optimierst: 1. prüfen, 2. Output analysieren, 3. dann entscheiden.

VERBOTEN:
- erfundene Technologien
- erfundene Tests
- erfundene Deployments
- erfundene Prozentzahlen

WICHTIG:
- Behaupte nur etwas, wenn echter Code, echte Logs oder echte Tool-Ergebnisse es belegen.
- Wenn ein Tool sinnvoll ist, benutze es zuerst.
- Antworte faktenbasiert, nicht fantasiebasiert.
- Beschreibe bei Optimierungen nur reale Schritte.

TOOLS DIE DU NUTZEN KANNST:
- linter <repo>
- build <repo>
- test <repo>
- write_file <path> <content>
- read_file <path>
- vercel_logs <project_id>
- render_logs <service_id>
- supabase_tables
- elevenlabs_agent <agent_id>

WENN DU EIN TOOL VERWENDEN WILLST, NUTZE EXAKT DIESES FORMAT:
```tool
tool_name
param1
param2
```
""".strip()


class EfroAgent:
    def __init__(self):
        self.memory = []
        self.master_prompt_path = BASE_DIR / "EFRO_MASTER_PROMPT.txt"
        self.master_prompt = self._load_master_prompt()

    def _load_master_prompt(self) -> str:
        try:
            return self.master_prompt_path.read_text(encoding="utf-8")
        except Exception:
            return "EFRO MASTER PROMPT NICHT GEFUNDEN"

    def build_runtime_prompt(self, user_input: str, extra_context=None) -> str:
        context = "Kein Kontext (Chroma deaktiviert für Speed)."
        extra_chunks = []
        if extra_context:
            if isinstance(extra_context, list):
                for item in extra_context:
                    if isinstance(item, tuple) and len(item) == 2:
                        extra_chunks.append(f"{item[0]}:\n{item[1]}")
                    else:
                        extra_chunks.append(str(item))
            else:
                extra_chunks.append(str(extra_context))
        extra = f"\nZusätzlicher Kontext (Tool-Ergebnisse):\n" + "\n\n".join(extra_chunks) + "\n" if extra_chunks else ""
        return f"""
{self.master_prompt}

{RUNTIME_RULES}

SYSTEM-ZUSTAND:
- Ziel: EFRO Projekt stabil + deployfähig + fehlerfrei
- Fokus: keine Fehler, keine Breaking Changes, produktionsreif

KONTEXT:
{context}

VERLAUF:
{self.format_memory()}
{extra}

USER:
{user_input}

ANTWORT:
""".strip()

    def query(self, user_input, extra_context=None):
        prompt = self.build_runtime_prompt(user_input, extra_context=extra_context)
        if ollama is None:
            reply = (
                "Ollama ist in dieser Laufzeitumgebung nicht installiert. "
                "Health-, Handoff- und UI-Endpunkte bleiben verfügbar, "
                "aber Chat-Antworten über das lokale LLM sind derzeit nicht nutzbar."
            )
            self.memory.append((user_input, reply))
            return reply

        response = ollama.chat(
            model="qwen2.5-coder:7b",
            messages=[{"role": "user", "content": prompt}]
        )
        reply = response["message"]["content"]
        self.memory.append((user_input, reply))
        return reply

    def format_memory(self):
        return "\n".join(
            [f"Nutzer: {u}\nAssistent: {a}" for u, a in self.memory[-5:]]
        )


agent = EfroAgent()
HANDOFFS: Dict[str, Dict[str, Any]] = {}


def build_handoff_context(handoff: Dict[str, Any]) -> str:
    source = handoff.get("source", "unbekannt")
    summary = handoff.get("summary", "")
    payload = handoff.get("payload") or {}
    return (
        f"HANDOFF\n"
        f"- id: {handoff.get('id')}\n"
        f"- source: {source}\n"
        f"- summary: {summary}\n"
        f"- payload: {json.dumps(payload, ensure_ascii=False)}"
    )


# --- API-Endpunkte für direkte Tool-Aufrufe ---
class ToolRequest(BaseModel):
    tool: str
    params: list[str]


class HandoffRequest(BaseModel):
    source: str = "manual"
    summary: str
    payload: Optional[Dict[str, Any]] = None

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
        if len(req.params) < 2:
            return {"error": "Bitte Pfad und Inhalt angeben"}
        path = req.params[0]
        content = " ".join(req.params[1:])
        result = write_file(path, content)
        log_message(f"Tool write_file path={path} -> {result[:200]}")
        return {"output": result}
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

@app.get("/handoffs")
async def list_handoffs():
    return {"handoffs": list(HANDOFFS.values())}


@app.get("/handoff/{handoff_id}")
async def get_handoff(handoff_id: str):
    handoff = HANDOFFS.get(handoff_id)
    if not handoff:
        raise HTTPException(status_code=404, detail="Handoff nicht gefunden")
    return handoff


@app.post("/handoff")
async def create_handoff(req: HandoffRequest):
    handoff_id = f"handoff-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    handoff = {
        "id": handoff_id,
        "source": req.source,
        "summary": req.summary,
        "payload": req.payload or {},
        "created_at": datetime.now().isoformat(),
    }
    HANDOFFS[handoff_id] = handoff
    log_message(f"HANDOFF created id={handoff_id} source={req.source}")
    return handoff


# --- Chat-Endpunkt ---
class ChatRequest(BaseModel):
    message: str
    handoff_id: Optional[str] = None

@app.post("/chat")
async def chat(req: ChatRequest):
    user_input = req.message.strip()
    STATUS_STATE["last_chat_at"] = datetime.now().isoformat()
    set_status("busy", "Chat-Anfrage wird verarbeitet")

    extra_context_blocks = []
    if req.handoff_id:
        handoff = HANDOFFS.get(req.handoff_id)
        if not handoff:
            set_status("error", "Handoff nicht gefunden", error="handoff_not_found")
            raise HTTPException(status_code=404, detail="Handoff nicht gefunden")
        extra_context_blocks.append(build_handoff_context(handoff))

    # -------- DIRECT COMMAND MODE (SCHNELL + VERTRAUENSWÜRDIG) --------
    direct = parse_direct_command(user_input)
    if direct:
        command, repo = direct

        if command == "status":
            set_status("idle", "Bereit")
            return {
                "reply": f"Agent läuft. Status: {STATUS_STATE['detail']}. Host/Port: {HOST}:{PORT}. Verfügbare Direktbefehle: linter <repo>, build <repo>, test <repo>, install <repo>, optimize <repo>, smoke shopify. Health: /health",
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
            output = run_install(repo)
            log_message(f"DIRECT install repo={repo} -> {output[:300]}")
            return {
                "reply": f"Direktbefehl ausgeführt: install {repo}",
                "tool_results": [
                    {
                        "tool": "install",
                        "params": [repo],
                        "output": output
                    }
                ]
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
            result = deterministic_optimize(repo)
            summary = []
            for step in result["steps"]:
                first_line = step["output"].strip().splitlines()[0] if step["output"].strip() else "Keine Ausgabe"
                summary.append(f"{step['tool']} {repo}: {first_line}")

            log_message(f"DIRECT optimize repo={repo} -> {' | '.join(summary)[:500]}")
            return {
                "reply": "Deterministische Optimierung abgeschlossen:\n" + "\n".join(summary),
                "tool_results": result["steps"]
            }

    # -------- FALLBACK: LLM MODE --------
    max_tool_calls = 10
    current_input = user_input
    conversation_history = []

    for _ in range(max_tool_calls):
        reply = agent.query(current_input, extra_context=extra_context_blocks + conversation_history)
        conversation_history.append(("assistant", reply))

        tool_pattern = r"```tool\s*(.*?)```"
        matches = re.findall(tool_pattern, reply, re.DOTALL)

        if not matches:
            set_status("idle", "Bereit")
            return {"reply": reply, "tool_results": []}

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
            STATUS_STATE["last_tool_at"] = datetime.now().isoformat()
            set_status("busy", f"Tool läuft: {tool_name}")
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
        current_input += "\nBitte fahre mit der Optimierung fort oder gib eine Antwort."

    return {
        "reply": reply,
        "tool_results": tool_results,
        "warning": "Maximale Anzahl Tool-Aufrufe erreicht."
    }

@app.get("/terminal")
async def terminal(cmd: str, repo: str = "brain"):
    cwd = REPO_PATHS.get(repo, REPO_PATHS["brain"])
    output = run_command(cmd, cwd)
    log_message(f"TERMINAL: {repo} $ {cmd} -> {output[:200]}")
    return {"output": output}


class OptimizeRequest(BaseModel):
    repo: str
    max_iterations: int = 5

# =========================
# FIX: optimize() BLOCK KOMPLETT ERSETZEN
# =========================

@app.post("/optimize")
async def optimize(req: OptimizeRequest):
    repo = req.repo
    max_iter = req.max_iterations
    cwd = REPO_PATHS.get(repo)

    if not cwd:
        return {"error": f"Repo '{repo}' nicht bekannt."}

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

    return results

@app.get("/health")
async def health():
    return {
        "ok": True,
        "service": "efro-agent",
        "state": STATUS_STATE["state"],
        "detail": STATUS_STATE["detail"],
        "last_error": STATUS_STATE["last_error"],
        "host": HOST,
        "port": PORT,
        "log_file": LOG_FILE,
        "prompt_path": str(BASE_DIR / "EFRO_MASTER_PROMPT.txt"),
    }


@app.get("/log")
async def get_log(limit: int = LOG_TAIL_LINES):
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            all_lines = [line.rstrip("\n") for line in f.readlines()]
        if limit <= 0:
            limit = LOG_TAIL_LINES
        tail = all_lines[-limit:]
        return {"lines": tail, "total_lines": len(all_lines)}
    except Exception as e:
        return {"lines": [f"Log file not readable: {e}"], "total_lines": 0}


@app.get("/")
async def root():
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
            --panel: #11182d;
            --panel-soft: #18213b;
            --border: #2a3558;
            --text: #e8ecf8;
            --muted: #a6b0cf;
            --accent: #6ea8fe;
            --good: #41d392;
            --warn: #ffcb6b;
            --bad: #ff7a90;
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Inter, ui-sans-serif, system-ui, sans-serif;
            background: var(--bg);
            color: var(--text);
        }
        .shell {
            display: grid;
            grid-template-columns: minmax(0, 1.35fr) minmax(360px, 0.95fr);
            gap: 16px;
            min-height: 100vh;
            padding: 16px;
        }
        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 18px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.22);
            overflow: hidden;
        }
        .panel-head {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            padding: 14px 16px;
            border-bottom: 1px solid var(--border);
            background: rgba(255,255,255,0.02);
        }
        .title { font-size: 18px; font-weight: 700; }
        .muted { color: var(--muted); font-size: 12px; }
        .status-pill {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: var(--panel-soft);
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 8px 12px;
            font-size: 12px;
        }
        .dot {
            width: 10px;
            height: 10px;
            border-radius: 999px;
            background: var(--good);
        }
        .layout-col {
            display: flex;
            flex-direction: column;
            min-height: calc(100vh - 32px);
        }
        .chat-body {
            display: flex;
            flex-direction: column;
            min-height: 0;
            height: calc(100vh - 120px);
        }
        #messages {
            flex: 1;
            overflow-y: auto;
            padding: 16px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .message {
            max-width: 90%;
            border-radius: 14px;
            padding: 10px 12px;
            white-space: pre-wrap;
            word-break: break-word;
            line-height: 1.45;
        }
        .user { align-self: flex-end; background: var(--accent); color: #081225; }
        .assistant { align-self: flex-start; background: #eef3ff; color: #11182d; }
        .tool { align-self: flex-start; background: #173327; color: #dff8ec; border: 1px solid #25533e; }
        .system { align-self: flex-start; background: #3a2a10; color: #ffe2a7; border: 1px solid #6a4f1d; }
        .composer {
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 10px;
            padding: 16px;
            border-top: 1px solid var(--border);
        }
        input, select, button {
            border-radius: 12px;
            border: 1px solid var(--border);
            background: var(--panel-soft);
            color: var(--text);
            padding: 10px 12px;
            font: inherit;
        }
        button { cursor: pointer; }
        button:hover { filter: brightness(1.08); }
        .terminal-wrap {
            display: flex;
            flex-direction: column;
            min-height: calc(100vh - 32px);
        }
        .terminal-tools {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            padding: 12px 16px;
            border-bottom: 1px solid var(--border);
        }
        .terminal {
            flex: 1;
            min-height: 0;
            overflow-y: auto;
            padding: 16px;
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 12px;
            line-height: 1.45;
            background: #0a0f1d;
        }
        .terminal-line {
            white-space: pre-wrap;
            margin: 0 0 6px 0;
            color: #d8def2;
        }
        .terminal-meta {
            color: var(--muted);
            font-size: 12px;
            padding: 0 16px 12px;
        }
        .control-row {
            display: grid;
            grid-template-columns: 1fr 150px auto;
            gap: 8px;
            padding: 12px 16px 16px;
            border-top: 1px solid var(--border);
        }
        .toggle-on { border-color: var(--accent); }
        @media (max-width: 1100px) {
            .shell { grid-template-columns: 1fr; }
            .chat-body, .terminal-wrap { min-height: unset; height: auto; }
            .terminal { min-height: 320px; }
        }
    </style>
</head>
<body>
<div class="shell">
    <section class="panel layout-col">
        <div class="panel-head">
            <div>
                <div class="title">Efro Agent</div>
                <div class="muted">Chat, Tool-Ausgaben und ehrlicher Laufzeitstatus</div>
            </div>
            <div class="status-pill"><span class="dot" id="status-dot"></span><span id="status">Bereit</span></div>
        </div>
        <div class="chat-body">
            <div id="messages"></div>
            <div class="composer">
                <input type="text" id="message-input" placeholder="Nachricht an den Agenten...">
                <button id="send-btn">Senden</button>
            </div>
        </div>
    </section>

    <aside class="panel terminal-wrap">
        <div class="panel-head">
            <div>
                <div class="title">Terminal & Logs</div>
                <div class="muted">Inkrementelles Polling, keine chaotische Voll-Neuzeichnung</div>
            </div>
            <div class="status-pill"><span id="log-state">Polling aktiv</span></div>
        </div>
        <div class="terminal-tools">
            <button id="clear-terminal">Clear Terminal</button>
            <button id="pause-logs">Pause Logs</button>
            <button id="autoscroll-toggle" class="toggle-on">Autoscroll</button>
        </div>
        <div id="terminal-output" class="terminal"></div>
        <div class="terminal-meta" id="terminal-meta">Noch keine Logs geladen.</div>
        <div class="control-row">
            <input type="text" id="cmd-input" placeholder="Befehl...">
            <select id="repo-select">
                <option value="efro">efro</option>
                <option value="brain">brain</option>
                <option value="widget">widget</option>
                <option value="shopify">shopify</option>
            </select>
            <button id="run-cmd">Ausführen</button>
        </div>
    </aside>
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
    const statusDot = document.getElementById('status-dot');
    const logState = document.getElementById('log-state');
    const terminalMeta = document.getElementById('terminal-meta');
    const clearTerminalBtn = document.getElementById('clear-terminal');
    const pauseLogsBtn = document.getElementById('pause-logs');
    const autoscrollBtn = document.getElementById('autoscroll-toggle');

    let logsPaused = false;
    let autoScroll = true;
    let knownTotalLines = 0;
    let clearBaseline = 0;

    function setStatus(text, kind = 'ready') {
        statusDiv.innerText = text;
        statusDot.style.background = kind === 'error' ? 'var(--bad)' : kind === 'busy' ? 'var(--warn)' : 'var(--good)';
    }

    function addMessage(role, text, extraClass = '') {
        const msgDiv = document.createElement('div');
        msgDiv.className = `message ${role} ${extraClass}`;
        msgDiv.innerText = text;
        messagesDiv.appendChild(msgDiv);
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    function appendTerminalLine(text) {
        const line = document.createElement('div');
        line.className = 'terminal-line';
        line.innerText = text;
        terminalDiv.appendChild(line);
        if (autoScroll) {
            terminalDiv.scrollTop = terminalDiv.scrollHeight;
        }
    }

    function renderTerminalSnapshot(lines) {
        // Vollreset nur für initiale Snapshot-Ladung oder Log-Rotation,
        // nicht als normaler Polling-Standardpfad.
        terminalDiv.innerHTML = '';
        for (const line of lines) {
            if (line && line.trim()) {
                appendTerminalLine(line);
            }
        }
    }

    async function sendMessage() {
        const msg = messageInput.value.trim();
        if (!msg) return;
        setStatus('Agent denkt nach...', 'busy');
        addMessage('user', msg);
        messageInput.value = '';
        try {
            const response = await fetch('/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: msg })
            });
            const data = await response.json();
            if (data.tool_results && data.tool_results.length > 0) {
                for (const tr of data.tool_results) {
                    addMessage('system', `🔧 Tool: ${tr.tool} ${(tr.params || []).join(' ')}`, 'tool');
                    addMessage('system', tr.output, 'tool');
                }
            }
            if (data.reply) {
                addMessage('assistant', data.reply);
                setStatus('Bereit', 'ready');
            } else if (data.warning) {
                addMessage('system', data.warning, 'system');
                setStatus('Warnung', 'busy');
            } else {
                addMessage('system', 'Keine gültige Antwort vom Server', 'system');
                setStatus('Fehler', 'error');
            }
        } catch (err) {
            addMessage('system', `Fehler: ${err.message}`, 'system');
            setStatus('Fehler', 'error');
        }
    }

    async function runCommand() {
        const cmd = cmdInput.value.trim();
        if (!cmd) return;
        const repo = repoSelect.value;
        appendTerminalLine(`> ${cmd} (in ${repo})`);
        try {
            const response = await fetch(`/terminal?cmd=${encodeURIComponent(cmd)}&repo=${encodeURIComponent(repo)}`);
            const data = await response.json();
            appendTerminalLine(data.output || 'Keine Ausgabe');
        } catch (err) {
            appendTerminalLine(`Fehler: ${err.message}`);
        }
        cmdInput.value = '';
    }

    async function fetchLogs() {
        if (logsPaused) {
            return;
        }
        try {
            const resp = await fetch('/log');
            const data = await resp.json();
            const lines = Array.isArray(data.lines) ? data.lines : [];
            const totalLines = Number.isInteger(data.total_lines) ? data.total_lines : lines.length;
            terminalMeta.innerText = `${lines.length} sichtbare Zeilen · ${totalLines} Gesamtzeilen`;

            if (knownTotalLines === 0 || totalLines < knownTotalLines) {
                renderTerminalSnapshot(lines.slice(Math.max(0, clearBaseline - Math.max(0, totalLines - lines.length))));
            } else {
                const visibleStart = Math.max(clearBaseline, totalLines - lines.length);
                const appendStart = Math.max(knownTotalLines, visibleStart);
                const offset = Math.max(0, appendStart - (totalLines - lines.length));
                for (const line of lines.slice(offset)) {
                    if (line && line.trim()) {
                        appendTerminalLine(line);
                    }
                }
            }

            knownTotalLines = totalLines;
        } catch (err) {
            terminalMeta.innerText = `Log-Fehler: ${err.message}`;
        }
    }

    clearTerminalBtn.onclick = () => {
        // Lokales Clear-Verhalten auf Nutzeraktion, nicht automatischer Polling-Reset.
        terminalDiv.innerHTML = '';
        clearBaseline = knownTotalLines;
        terminalMeta.innerText = 'Terminal lokal geleert.';
    };

    pauseLogsBtn.onclick = () => {
        logsPaused = !logsPaused;
        pauseLogsBtn.innerText = logsPaused ? 'Logs pausiert' : 'Pause Logs';
        logState.innerText = logsPaused ? 'Polling pausiert' : 'Polling aktiv';
        pauseLogsBtn.classList.toggle('toggle-on', logsPaused);
    };

    autoscrollBtn.onclick = () => {
        autoScroll = !autoScroll;
        autoscrollBtn.innerText = autoScroll ? 'Autoscroll' : 'Autoscroll aus';
        autoscrollBtn.classList.toggle('toggle-on', autoScroll);
    };

    sendBtn.onclick = sendMessage;
    runCmdBtn.onclick = runCommand;
    messageInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') sendMessage(); });
    cmdInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') runCommand(); });

    setInterval(fetchLogs, 2000);
    fetchLogs();
});
</script>
</body>
</html>'''
    return HTMLResponse(content=html)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
