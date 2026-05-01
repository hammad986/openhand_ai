# Multi-Agent AI Dev System

## Overview
A lightweight, self-fixing autonomous coding agent with a multi-LLM router (auto failover + cost-aware routing), web UI, and terminal/automation features. Implements a Plan → Execute → Observe → Fix loop.

## Architecture
- **Web frontend/backend**: Flask app (`web_app.py`) served on port 5000
- **Agent core**: `agent.py`, `orchestrator.py`, `router.py`
- **Tool system**: `tools.py` + `/tools/` directory
- **Memory**: `memory.py`, `vector_store.py`, `long_term_memory.py` (uses chromadb + sentence-transformers, loaded lazily)
- **MCP Context**: `mcp_context.py` — shared state across agents
- **Templates**: HTML UI in `/templates/`
- **Session data**: SQLite (`sessions.db`) + per-session workspaces (`./workspace/<sid>/`)

## UI — NX Advanced Interface (v2) + Phase 3 Productization
The UI was upgraded from the basic "Phase 57" bridge to a full feature-complete platform UI:
- **Header**: Logo (SVG star, gradient animation) + Plan selector (Lite/Pro/Elite badge) + command bar + "+" attach menu + voice + model selector + status badge + sessions + ⌘K palette + settings
- **Left Panel** (280px, resizable): AI Thinking stream, session card (shows active plan mode), HITL controls, quick start examples
- **Center Panel** (tabbed): Logs, Preview, Code/Files (Monaco), Terminal (PTY), Metrics, Agents, Timeline, Steps
- **Right Inspector** (290px, resizable): Live status stats, model info, system metrics, error card + Fix-with-AI, decisions, output
- **Status Bar**: Model name | Mode | Status | Session | Keyboard hints
- **Command Palette** (Ctrl+K): All major actions searchable
- **Keyboard Shortcuts**: Ctrl+Enter (run), Ctrl+K (palette), Ctrl+S (save file), Escape (close)
- **Resizable panels**: Drag handles between all 3 columns

### Phase 3: Productization Layer
- **Plan System**: 3 modes — Lite (fast/no planning, max 5 steps), Pro (reasoning+planning, max 15 steps), Elite (full autonomy, max 50 steps). Plan mode sent to backend with every task; capability flags (`ALLOW_PLANNING`, `ALLOW_REASONING`, `ALLOW_DEBUG`, `ALLOW_SELF_CORRECTION`, `MAX_AGENT_STEPS`) passed as env vars to agent subprocess.
- **"+" Attach Menu**: Upload File, Upload Image, Upload Folder (multi-file), Import from GitHub — animated dropdown from command bar
- **GitHub Import**: Modal UI calls `/api/github/import`, shows repo context badge, auto-fills task input
- **Context Bar**: Shows active file/repo badges below header, removable, auto-hides when empty
- **Drag & Drop**: Drop files directly onto command bar for instant attachment
- **Branding**: Animated SVG star logo with gradient rotation, logo hover animation

## Running the App
- **Workflow**: "Start application" — runs `python web_app.py`
- **Port**: 5000
- **Host**: 0.0.0.0

## Key Files
- `web_app.py` — Main Flask app (~6500 lines), multi-session SaaS control platform
- `agent.py` — Core agent loop
- `router.py` — Multi-LLM routing with failover
- `tools.py` — Tool system
- `config.py` — Configuration
- `mcp_context.py` — Multi-Context Protocol shared state

## Dependencies
Core packages (all pre-installed via pip):
- Flask, gunicorn, requests, psutil, bcrypt, PyJWT, python-dotenv, tiktoken

Optional heavy packages (disk-intensive, loaded lazily at runtime):
- chromadb, sentence-transformers, playwright

## Deployment
- Target: autoscale
- Run command: `gunicorn --bind=0.0.0.0:5000 --reuse-port -w 4 --threads 8 --timeout 120 web_app:app`
