# ⚡ Nexora AI Platform

> **Production-grade, multi-agent AI development platform with intelligent model routing, autonomous goal execution, and a full SaaS billing system.**

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-3.x-black?logo=flask)](https://flask.palletsprojects.com)
[![License](https://img.shields.io/badge/License-MIT-green)](#)
[![Razorpay](https://img.shields.io/badge/Billing-Razorpay-blue)](https://razorpay.com)

---

## 🚀 Overview

Nexora is an autonomous AI coding platform. Describe what you want to build — the AI plans, codes, debugs, and ships it step by step. Built on a **Plan → Execute → Observe → Fix** loop with 15+ AI providers, 5 specialist agents, and a fully integrated SaaS billing system.

**Live capabilities:**
- 🤖 Autonomous multi-agent coding with 5 specialist AI agents
- 🧠 3-tier memory system with semantic recall across sessions
- 🎯 Goal decomposition and autonomous task execution (Phase 16)
- ⚡ Intelligent model routing across 15+ AI providers (Phase 9)
- 💰 Razorpay SaaS billing — subscriptions, invoices, webhooks (Phase 36)
- 🔑 BYOK for any supported provider (Phase 5)
- 🛡️ Production security: JWT auth, rate limiting, kill switch (Phase 19)

---

## ⚡ Features

### Core AI Engine
| Feature | Description |
|---------|-------------|
| Multi-Agent Pipeline | Manager → Research → Coding → Debug agents in coordination |
| 5 Specialist Agents | Reviewer, Debugger, Tester, Security Auditor, Optimizer |
| Intelligent Routing | Auto-selects best model per task (fastest / cheapest / smartest) |
| Auto-Failover | Switches providers on rate-limit or failure — zero downtime |
| Self-Improvement | Learns from past errors to avoid repeating mistakes (Phase 14) |
| Context Compression | 3-tier system to maximise context window efficiency (Phase 13) |

### Platform
| Feature | Description |
|---------|-------------|
| 15+ AI Providers | OpenAI, Gemini, Claude, Groq, OpenRouter, Ollama, xAI, Bedrock and more |
| BYOK System | Per-user API key management with encrypted storage |
| Session Memory | Short + long-term memory with ChromaDB vector store |
| Task Scheduler | Background autonomous agent scheduling — cron-like (Phase 18) |
| Goal AI | High-level goal decomposition into executable task graphs (Phase 16) |
| SaaS Billing | Full Razorpay integration: orders, verification, webhooks, invoices |

---

## 🧠 AI Capabilities

### The Agent Loop
```
User Prompt → Planning Agent → Task Decomposition
                             ↓
                    Coding Agent → Tool Execution → File Writes
                             ↓
                    Debug Agent → Test & Verify → Fix Loop
                             ↓
                    Review Agent → Quality Check → Output
```

### Model Intelligence Routing (Phase 9)
The router assigns the optimal model per task type:
- **Planning** — High-reasoning: GPT-4o, Claude 3.5, Gemini Pro
- **Coding** — Code-optimised: GPT-4o, Claude 3.5 Sonnet, DeepSeek Coder
- **Debug** — Fast iterative: Groq Llama, GPT-4o-mini

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Browser (SPA)                    │
│            templates/index.html (~16k LOC)          │
└─────────────────────┬───────────────────────────────┘
                      │ HTTP / REST
┌─────────────────────▼───────────────────────────────┐
│              Flask App (web_app.py)                 │
│         ~10,000 lines · 36 feature phases           │
├────────────┬────────────┬───────────┬───────────────┤
│  agent.py  │ router.py  │memory.py  │  payments.py  │
│  Agent     │  Model     │ 3-tier    │  Razorpay     │
│  Loop      │  Router    │ Memory    │  Billing      │
└────────────┴────────────┴───────────┴───────────────┘
                      │
┌─────────────────────▼───────────────────────────────┐
│              SQLite Databases                       │
│   sessions.db  (sessions, settings, scheduler)      │
│   billing.db   (subscriptions, invoices, events)    │
└─────────────────────────────────────────────────────┘
```

**Key files:**
```
web_app.py         # Flask app — all routes and business logic
agent.py           # Core agent execution loop
orchestrator.py    # Multi-agent orchestration
router.py          # LLM routing logic
model_router.py    # Intelligent model selection
memory.py          # 3-tier memory system
payments.py        # Razorpay billing integration
auth_system.py     # JWT authentication
tools.py + tools/  # Extensible tool registry
templates/
  index.html       # Main SPA (~16,000 lines)
  docs.html        # Documentation page
```

---

## 🔌 API Providers

| Provider | Models | Notes |
|----------|--------|-------|
| OpenAI | GPT-4o, GPT-4-turbo, GPT-3.5 | BYOK supported |
| Google Gemini | 1.5 Pro / Flash / Ultra | BYOK supported |
| Anthropic | Claude 3.5 Sonnet / Haiku / Opus | BYOK supported |
| Groq | Llama 3, Mixtral | Ultra-fast inference |
| OpenRouter | 100+ models | Single API key |
| xAI / Grok | Grok-2 | BYOK supported |
| AWS Bedrock | Claude, Titan | Enterprise |
| Azure OpenAI | GPT-4 | Enterprise |
| DeepSeek | DeepSeek Coder | Code-optimised |
| Mistral | Large / Small | EU models |
| Ollama | Any local model | Self-hosted |

---

## 💰 Pricing

| Plan | Price | Daily Runs | Key Features |
|------|-------|-----------|--------------|
| **Free** | ₹0/month | 10 runs | Lite routing, basic agents |
| **Pro** | ₹20/month | Unlimited | Pro routing, all 5 agents, memory |
| **Elite** | ₹50/month | Unlimited | Elite routing, multi-agent, scheduler, goals |

All plans include BYOK. Billing via Razorpay (INR). Yearly plans available at ~17% discount.

---

## 🔑 BYOK System

Users supply their own API keys for any supported provider:

1. Open Provider Settings in the platform UI
2. Enter your API key for any provider
3. Keys are encrypted and stored per-session in SQLite
4. Router automatically uses your keys first, then platform keys as fallback

---

## 🛠️ Installation

### Prerequisites
- Python 3.10+
- pip

### Steps

```bash
# 1. Clone
git clone https://github.com/your-org/nexora-ai
cd nexora-ai

# 2. Install
pip install -r requirements.txt

# 3. Set secrets
export SECRET_KEY="your-long-random-secret"
export JWT_SECRET="your-long-random-jwt-secret"

# 4. Run
python web_app.py
```

Open `http://localhost:5000`. All databases are created automatically on first run.

### Optional Secrets

```bash
# AI Providers
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...
ANTHROPIC_API_KEY=sk-ant-...
GROQ_API_KEY=gsk_...

# Billing (Razorpay)
RAZORPAY_KEY_ID=rzp_live_...
RAZORPAY_KEY_SECRET=...

# Email receipts (Resend)
EMAIL_API_KEY=re_...
EMAIL_FROM=billing@your-domain.com
```

---

## 🚀 Deployment

### Replit (Recommended)
1. Import to Replit
2. Add secrets in the Secrets panel
3. Click **Deploy** — TLS, health checks, and auto-restart included

### Gunicorn
```bash
gunicorn -w 2 -b 0.0.0.0:5000 web_app:app
```

### Production Checklist
- [ ] Strong `SECRET_KEY` and `JWT_SECRET` (32+ random chars)
- [ ] Razorpay **live** keys (not test keys)
- [ ] Webhook URL: `https://your-domain.com/api/payments/webhook`
- [ ] Verified sender email domain for `EMAIL_FROM`
- [ ] HTTPS enabled

---

## 📸 Screenshots

| Main Interface | Billing & Plans | Documentation |
|---------------|----------------|---------------|
| *Main app UI* | *Upgrade modal* | */docs page* |

---

## 📌 Roadmap

- [ ] WebSocket real-time streaming
- [ ] Team/org accounts with RBAC
- [ ] GitHub integration — commit, PR, review
- [ ] Plugin marketplace for custom tools
- [ ] Mobile app (React Native / Expo)
- [ ] Multi-tenant SaaS mode

---

## 📄 License

MIT License

---

<p align="center"><strong>⚡ Nexora AI Platform</strong> — Built for developers, powered by AI</p>
