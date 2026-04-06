import os
import subprocess
import json
import ollama
import re
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import chromadb
from chromadb.utils import embedding_functions
from sentence_transformers import SentenceTransformer
from typing import List, Optional, Dict, Any
from dotenv import load_dotenv
from datetime import datetime   # <-- NEU

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


# ---------- Konfiguration ----------
REPO_PATHS = {
    "efro": "/opt/efro-agent/repos/efro",
    "brain": "/opt/efro-agent/repos/efro-brain",
    "widget": "/opt/efro-agent/repos/efro-widget",
    "shopify": "/opt/efro-agent/repos/efro-shopify"
}
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

    def query(self, user_input, extra_context=None):
        # Chroma vorerst deaktiviert für Speed
        context = "Kein Kontext (Chroma deaktiviert für Speed)."
        extra = f"\nZusätzlicher Kontext (Tool-Ergebnisse):\n{extra_context}\n" if extra_context else ""

        prompt = f"""
{self.system_prompt}



REGELN FÜR WAHRHEIT:

- Du darfst keine Zusammenfassungen geben, ohne vorher Tools benutzt zu haben.
- Jede technische Aussage muss auf einem Tool-Ergebnis basieren.
- Wenn kein Tool genutzt wurde → darfst du KEINE finale Bewertung geben.
- Wenn du unsicher bist → sag explizit "nicht verifiziert".
- Bevor du optimierst:
  1. prüfe mit Tools
  2. analysiere Output
  3. entscheide dann

VERBOTEN:
- erfundene Technologien
- erfundene Tests
- erfundene Deployments
- erfundene Prozentzahlen

WICHTIG:
- Erfinde niemals Technologien, Dateien, Tests, Datenbanken oder Ergebnisse.
- Behaupte nur etwas, wenn es durch echten Code, echte Logs oder echte Tool-Ergebnisse belegt ist.
- Wenn du etwas nicht geprüft hast, sage klar: "nicht verifiziert".
- Wenn ein Tool sinnvoll ist, benutze es zuerst.
- Gib keine erfundenen Prozentwerte für Tests oder Deployment-Status an.
- Antworte faktenbasiert, nicht fantasiebasiert.
- Wenn du optimierst, beschreibe nur reale Schritte, die du tatsächlich geprüft oder ausgeführt hast.


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

WICHTIG:
- Wenn ein Tool sinnvoll ist, benutze es direkt
- Wenn mehrere Schritte nötig sind, führe sie logisch aus
- Denke wie ein Senior Engineer / CTO

ANTWORT:
"""

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

# --- API-Endpunkte für direkte Tool-Aufrufe ---
class ToolRequest(BaseModel):
    tool: str
    params: list[str]

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

# --- Chat-Endpunkt ---
class ChatRequest(BaseModel):
    message: str

@app.post("/chat")
async def chat(req: ChatRequest):
    user_input = req.message.strip()

    # -------- DIRECT COMMAND MODE (SCHNELL + VERTRAUENSWÜRDIG) --------
    direct = parse_direct_command(user_input)
    if direct:
        command, repo = direct

        if command == "status":
            return {
                "reply": "Agent läuft. Direkte Befehle: linter <repo>, build <repo>, test <repo>, install <repo>, optimize <repo>",
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
        reply = agent.query(current_input, extra_context=conversation_history)
        conversation_history.append(("assistant", reply))

        tool_pattern = r"```tool\s*(.*?)```"
        matches = re.findall(tool_pattern, reply, re.DOTALL)

        if not matches:
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

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "efro-agent",
        "time": datetime.now().isoformat()
    }

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
            --panel: #121a2b;
            --panel-2: #182236;
            --panel-3: #0f1728;
            --border: #26324d;
            --text: #eef2ff;
            --muted: #9ca8c3;
            --accent: #7c9cff;
            --accent-2: #3dd9b0;
            --danger: #ff6b7a;
            --warning: #ffb454;
            --shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
        }

        * { box-sizing: border-box; }
        html, body { height: 100%; }
        body {
            margin: 0;
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: linear-gradient(180deg, #0a0f1d 0%, #0e1528 100%);
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
            gap: 16px;
            padding: 18px 20px;
            border: 1px solid var(--border);
            border-radius: 18px;
            background: rgba(18, 26, 43, 0.92);
            box-shadow: var(--shadow);
        }

        .title-wrap h1 {
            margin: 0;
            font-size: 22px;
            line-height: 1.2;
        }

        .title-wrap p {
            margin: 6px 0 0;
            color: var(--muted);
            font-size: 13px;
        }

        .status-cluster {
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }

        .status-badge,
        .meta-pill {
            border: 1px solid var(--border);
            background: var(--panel-3);
            border-radius: 999px;
            padding: 8px 12px;
            font-size: 12px;
            color: var(--muted);
        }

        .status-badge.ready { color: #b8ffd6; border-color: rgba(61, 217, 176, 0.35); }
        .status-badge.thinking { color: #d7e3ff; border-color: rgba(124, 156, 255, 0.35); }
        .status-badge.error { color: #ffd4d9; border-color: rgba(255, 107, 122, 0.35); }

        .main-grid {
            min-height: 0;
            display: grid;
            grid-template-columns: minmax(360px, 1.1fr) minmax(420px, 0.9fr);
            gap: 18px;
        }

        .panel {
            min-height: 0;
            border: 1px solid var(--border);
            border-radius: 20px;
            background: rgba(18, 26, 43, 0.94);
            box-shadow: var(--shadow);
            display: flex;
            flex-direction: column;
            overflow: hidden;
        }

        .panel-header {
            padding: 18px 20px 14px;
            border-bottom: 1px solid var(--border);
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
        }

        .panel-title {
            margin: 0;
            font-size: 16px;
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
            padding: 18px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        .message {
            max-width: 92%;
            padding: 12px 14px;
            border-radius: 16px;
            line-height: 1.45;
            white-space: pre-wrap;
            word-break: break-word;
            border: 1px solid transparent;
        }

        .message.user {
            align-self: flex-end;
            background: linear-gradient(135deg, #6f8fff, #5575ff);
            color: white;
        }

        .message.assistant {
            align-self: flex-start;
            background: #eef2ff;
            color: #101828;
        }

        .message.tool {
            align-self: flex-start;
            background: rgba(61, 217, 176, 0.12);
            border-color: rgba(61, 217, 176, 0.22);
            color: #d8fff4;
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        }

        .message.system {
            align-self: flex-start;
            background: rgba(255, 180, 84, 0.12);
            border-color: rgba(255, 180, 84, 0.22);
            color: #ffe7c2;
        }

        .composer {
            border-top: 1px solid var(--border);
            padding: 16px 18px 18px;
            display: flex;
            gap: 12px;
            align-items: center;
            background: rgba(15, 23, 40, 0.9);
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
            border-color: rgba(124, 156, 255, 0.4);
        }

        button.primary {
            background: linear-gradient(135deg, #6d8dff, #4d6fff);
            border-color: rgba(124, 156, 255, 0.45);
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
            background: #0a1020;
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
            padding: 16px 18px 18px;
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto auto;
            gap: 10px;
            background: rgba(15, 23, 40, 0.9);
        }

        .empty-state {
            color: var(--muted);
            font-size: 13px;
        }

        @media (max-width: 1100px) {
            .main-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
<div class="app-shell">
    <header class="topbar">
        <div class="title-wrap">
            <h1>EFRO Agent Control Surface</h1>
            <p>Operator-Assistenz für Repos, Incidents und technische Prüfung.</p>
        </div>
        <div class="status-cluster">
            <div id="status" class="status-badge ready">Bereit</div>
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
            <div id="messages" class="messages">
                <div class="empty-state">Noch keine Unterhaltung. Starte mit einer Nachricht oder einem direkten Repo-Befehl.</div>
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

    let terminalInitialized = false;
    let lastRenderedLineCount = 0;

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
                body: JSON.stringify({ message: msg })
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
    fetchLogs(true);
});
</script>
</body>
</html>'''
    return HTMLResponse(content=html)

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("EFRO_AGENT_HOST", "127.0.0.1")
    port = int(os.getenv("EFRO_AGENT_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
