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
    if text in ("status", "agent status"):
        return ("status", None)
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

    



## 5. **Fehlerbehandlung in den API-Request-Funktionen**  
##Problem: Bei Netzwerkfehlern könnten Exceptions auftreten.  
##Lösung: Ersetze die Funktionen `vercel_request` und `render_request` durch robuste Versionen mit `try/except`.

### Einfügen: Ersetze die beiden Funktionen.

#python
def vercel_request(endpoint: str, api_token: str, method="GET", data=None):
    import requests
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    url = f"https://api.vercel.com/{endpoint}"
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=30)
        elif method == "POST":
            resp = requests.post(url, headers=headers, json=data, timeout=30)
        else:
            return "Unsupported method"
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        return f"Fehler bei Vercel-API: {e}"
def render_request(endpoint: str, api_token: str, method="GET", data=None):
    import requests
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }
    url = f"https://api.render.com/{endpoint}"
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=30)
        elif method == "POST":
            resp = requests.post(url, headers=headers, json=data, timeout=30)
        else:
            return "Unsupported method"
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException as e:
        return f"Fehler bei Render-API: {e}"        

    
@app.get("/log")
async def get_log():
    try:
        with open(LOG_FILE, "r") as f:
            lines = f.readlines()[-100:]  # letzte 100 Zeilen
        return {"log": "".join(lines)}
    except Exception as e:
        return {"log": f"Log file not readable: {e}"}

@app.get("/")
async def root():
    html = '''<!DOCTYPE html>
<html>
<head>
    <title>Efro Agent</title>
    <style>
        body { font-family: sans-serif; display: flex; margin: 0; }
        #chat { width: 60%; border-right: 1px solid #ccc; padding: 10px; display: flex; flex-direction: column; height: 100vh; }
        #terminal { width: 40%; padding: 10px; background: #1e1e1e; color: #d4d4d4; font-family: monospace; overflow-y: auto; height: 100vh; }
        #messages { flex: 1; overflow-y: auto; border-bottom: 1px solid #ccc; margin-bottom: 10px; }
        .message { margin: 8px; padding: 6px; border-radius: 8px; max-width: 90%; word-wrap: break-word; }
        .user { background: #007acc; color: white; align-self: flex-end; }
        .assistant { background: #f1f1f1; color: black; align-self: flex-start; }
        .tool { background: #2ecc71; color: white; align-self: flex-start; font-family: monospace; }
        .system { background: #f39c12; color: white; align-self: flex-start; font-family: monospace; }
        #status { background: #34495e; color: white; padding: 5px; margin-bottom: 10px; border-radius: 4px; font-size: 12px; text-align: center; }
        #input-area { display: flex; }
        #message-input { flex: 1; padding: 8px; }
        button { padding: 8px; }
        .command-output { background: #2d2d2d; color: #ccc; padding: 5px; margin-top: 5px; font-family: monospace; white-space: pre-wrap; }
    </style>
</head>
<body>
<div id="chat">
    <h3>Efro Agent – Chat</h3>
    <div id="status">Bereit</div>
    <div id="messages"></div>
    <div id="input-area">
        <input type="text" id="message-input" placeholder="Nachricht...">
        <button id="send-btn">Senden</button>
    </div>
</div>
<div id="terminal">
    <h3>Terminal</h3>
    <div id="terminal-output"></div>
    <div style="margin-top: 10px;">
        <input type="text" id="cmd-input" placeholder="Befehl...">
        <button id="run-cmd">Ausführen</button>
        <select id="repo-select">
            <option value="efro">efro (Landing Page)</option>
            <option value="brain">Brain API</option>
            <option value="widget">Widget (Avatar)</option>
            <option value="shopify">Shopify App</option>
        </select>
    </div>
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

    function addMessage(role, text, extraClass = '') {
        const msgDiv = document.createElement('div');
        msgDiv.className = `message ${role} ${extraClass}`;
        msgDiv.innerText = text;
        messagesDiv.appendChild(msgDiv);
        messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    function addTerminalOutput(text) {
        const pre = document.createElement('pre');
        pre.innerText = text;
        terminalDiv.appendChild(pre);
        terminalDiv.scrollTop = terminalDiv.scrollHeight;
    }

    async function sendMessage() {
        const msg = messageInput.value.trim();
        if (!msg) return;

        statusDiv.innerText = 'Agent denkt nach...';
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
                    addMessage('system', `🔧 Tool: ${tr.tool} ${tr.params.join(' ')}`, 'tool');
                    addMessage('system', tr.output, 'tool');
                }
            }

            if (data.reply) {
                addMessage('assistant', data.reply);
            } else if (data.warning) {
                addMessage('system', data.warning, 'system');
            } else {
                addMessage('system', 'Keine gültige Antwort vom Server', 'system');
            }

            statusDiv.innerText = 'Bereit';
        } catch (err) {
            console.error('SEND ERROR:', err);
            addMessage('system', `Fehler: ${err.message}`, 'system');
            statusDiv.innerText = 'Fehler';
        }
    }

    async function runCommand() {
        const cmd = cmdInput.value.trim();
        if (!cmd) return;

        const repo = repoSelect.value;
        addTerminalOutput(`> ${cmd} (in ${repo})`);

        try {
            const response = await fetch(`/terminal?cmd=${encodeURIComponent(cmd)}&repo=${encodeURIComponent(repo)}`);
            const data = await response.json();
            addTerminalOutput(data.output);
        } catch (err) {
            addTerminalOutput(`Fehler: ${err.message}`);
        }

        cmdInput.value = '';
    }

    async function fetchLogs() {
        try {
            const resp = await fetch('/log');
            const data = await resp.json();

            if (data.log !== undefined) {
                terminalDiv.innerHTML = '';
                const lines = data.log.split('\\n');
                for (const line of lines) {
                    if (line.trim()) {
                        addTerminalOutput(line);
                    }
                }
            }
        } catch (err) {
            console.error('Log-Fehler:', err);
        }
    }

    sendBtn.onclick = sendMessage;
    runCmdBtn.onclick = runCommand;

    messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') sendMessage();
    });

    cmdInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') runCommand();
    });

    setInterval(fetchLogs, 2000);
    fetchLogs();
});
</script>
</body>
</html>'''
    return HTMLResponse(content=html)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
