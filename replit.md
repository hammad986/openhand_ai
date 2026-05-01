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
