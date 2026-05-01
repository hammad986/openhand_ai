# 🤖 Multi-Agent AI Dev System

Lightweight, self-fixing AI coding agent — optimized for low-spec machines (8GB RAM, no GPU).

---

## ⚡ Quick Start (3 steps)

```bash
# 1. Install
pip install -r requirements.txt

# 2. Add API key
cp .env.example .env
# Edit .env → add your GROQ_API_KEY (free at console.groq.com)

# 3. Run
python main.py "Build a Flask hello world app"
```

---

## 🏗️ Architecture

```
User Task
    │
    ▼
┌─────────────────────────────────────────┐
│           BRAIN LAYER (router.py)        │
│  Groq → Local(Ollama) → Together →      │
│  OpenRouter → NVIDIA                    │
│  Auto-failover + Cost-aware routing     │
└──────────────────┬──────────────────────┘
                   │
    ┌──────────────▼──────────────┐
    │      AGENT LOOP (agent.py)   │
    │  Plan → Execute → Observe → │
    │  Fix → Loop → Done           │
    └──────────────┬──────────────┘
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────┐    ┌─────────────────┐
│ TOOLS        │    │ MEMORY          │
│ read_file    │    │ Chat history    │
│ write_file   │    │ Task logs (SQL) │
│ run_python   │    │ KV store (JSON) │
│ run_shell    │    │ Code snippets   │
│ git_commit   │    └─────────────────┘
└──────────────┘
```

---

## 🧠 API Priority (auto-selected)

| Priority | API         | Cost    | Notes              |
|----------|-------------|---------|---------------------|
| 1st      | Groq        | Free    | Fastest             |
| 2nd      | Ollama      | Free    | Local, no internet  |
| 3rd      | Together.ai | $0.0009 | Cheap               |
| 4th      | OpenRouter  | $0.001  | Many models         |
| 5th      | NVIDIA      | $0.001  | Backup              |

---

## 📟 CLI Usage

```bash
# Single task
python main.py "Create a REST API with Flask"

# Interactive mode
python main.py

# Chat mode (no tools)
python main.py --chat

# Force specific API
python main.py --model groq "Build login page"
python main.py --model local "Write a Python script"

# View task history
python main.py --history

# Clear memory
python main.py --clear
```

---

## 📁 Project Structure

```
ai-agent/
├── main.py          ← CLI entry point
├── agent.py         ← Core agent loop
├── router.py        ← Multi-API router
├── tools.py         ← Tool system
├── memory.py        ← Memory (JSON + SQLite)
├── config.py        ← All settings
├── requirements.txt
├── .env.example     ← API key template
└── workspace/       ← Agent writes files here
```

---

## 🔧 Available Tools

| Tool           | What it does                      |
|----------------|-----------------------------------|
| `write_file`   | Create or overwrite a file         |
| `read_file`    | Read file contents                 |
| `list_files`   | List workspace files               |
| `run_python`   | Execute Python code                |
| `run_shell`    | Run shell command                  |
| `search_replace` | Edit specific text in file       |
| `git_commit`   | Auto-commit changes                |
| `delete_file`  | Delete a file                      |

---

## 🔥 Advanced Features

### 1. Multi-API Failover
```
Task sent → Groq fails → auto-retry on Ollama → success
```

### 2. Cost-Aware Routing
Free APIs tried first. Paid APIs only as last resort.

### 3. Self-Fixing Loop
```
Code fails → error sent back to LLM → LLM fixes → retry
```

### 4. Persistent Memory
All tasks, code, and history saved to `memory.db` and `memory.json`.

---

## 🔌 Optional: Local AI (Ollama)

For 100% offline use:
```bash
# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# Pull a coding model (3.8GB)
ollama pull codellama

# Agent will use it automatically as fallback
```

---

## 📦 Get Free API Keys

- **Groq** (fastest, free): https://console.groq.com
- **OpenRouter** (many models): https://openrouter.ai
- **Together.ai**: https://api.together.xyz
- **NVIDIA**: https://integrate.api.nvidia.com
