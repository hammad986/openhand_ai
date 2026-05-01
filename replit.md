# Nexora AI Platform

## Overview
This project is an autonomous coding agent featuring a multi-LLM router with auto-failover and cost-aware routing. It incorporates a web-based user interface and terminal features, operating on a Plan → Execute → Observe → Fix development loop. The system aims to provide a robust, self-improving, and highly customizable environment for AI-driven software development.

## User Preferences
I prefer iterative development with clear, concise feedback. I value transparency in the AI's decision-making process and prefer to be asked before major architectural changes are implemented. I appreciate detailed explanations when new features or complex solutions are introduced.

## System Architecture
The system is built around a Flask web application (`web_app.py`) serving both frontend and backend on port 5000. The core AI components include `agent.py`, `orchestrator.py`, and `router.py`. A dynamic tool system is implemented via `tools.py` and a dedicated `tools/` directory. Memory management utilizes `memory.py`, `vector_store.py`, and `long_term_memory.py` for both short-term and long-term context, powered by chromadb and sentence-transformers. Shared state across agents is managed by `mcp_context.py`. HTML templates are stored in `/templates/`, and session data is persisted in a SQLite database (`sessions.db`) with per-session workspaces.

The UI, referred to as "NX Advanced Interface (v2)", features a three-panel layout with a resizable left panel for AI thinking and session controls, a tabbed center panel for logs, code, terminal, and metrics, and a right inspector panel for live status, model info, and error handling. Key UI/UX features include dark/light theme toggle, real-time token/cost tracking, session history, prompt templates, AI suggestion dropdowns, and an animated SVG star logo.

The system incorporates a multi-provider LLM system (Phase 5) with intelligent routing based on plan modes (Lite, Pro, Elite) and user-defined preferences (Cheapest, Fastest, Smartest). A Decision Intelligence Layer (Phase 6) allows users to prioritize and lock specific providers, and a smart recommendation engine suggests optimal LLMs based on task type.

A Structured Agent System (Phase 7) employs five specialist agents (Code Reviewer, Debugger, Test Generator, Security Auditor, Performance Optimizer) whose execution is gated by the chosen plan mode and triggered smartly based on context.

The system includes a robust Model Intelligence Routing System (Phase 9) that assigns specific LLMs for `planning`, `coding`, and `debug` roles based on the selected plan tier, with automatic fallback mechanisms. An Agent Intelligence & Memory System (Phase 10) provides short-term memory, semantic long-term memory integration, and self-correction prompts based on past errors.

A Multi-Agent Collaboration System (Phase 11) orchestrates a 4-agent pipeline (Manager, Research, Coding, Debug) for complex tasks. Further intelligence upgrades include a 3-tier Intelligent Context Compression (Phase 13), a Self-Improving AI System (Phase 14) with post-session reflection, and an AI Learning Dashboard (Phase 15). The system supports Autonomous Goal-Driven AI (Phase 16) with task decomposition and execution graph visualization (Phase 17). A Task Scheduler (Phase 18) allows for background autonomous agents and scheduled task execution.

Security and production hardening (Phase 19) include rate limiting, input sanitization, enhanced authentication, structured logging, and administrative controls.

A real SaaS billing system (Phase 36) is integrated via Razorpay. It includes:
- `payments.py` — order creation, HMAC signature verification, webhook processing, subscription activation/expiry, invoice HTML generation, and Resend email delivery.
- Routes: `/api/payments/create-order`, `/api/payments/verify`, `/api/payments/webhook`, `/api/invoice/<id>`, `/api/billing/info`, `/api/billing/invoices`, `/api/billing/cancel`, `/api/payments/plans`, `/api/billing/webhook-status` (NEW).
- Billing DB: `billing.db` (SQLite, auto-created) with `subscriptions`, `invoices`, `payment_events` tables.
- UI: billing cycle toggle (Monthly/Yearly), INR plan pricing (₹20/₹50 per month), real Razorpay checkout popup, subscription status chip, invoice history with download links, billing mini-block in inspector panel, **Billing Setup Guide tab** in Settings (NEW).
- **Secrets required to activate**: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` (from Razorpay dashboard), `RAZORPAY_WEBHOOK_SECRET` (for webhook validation), and optionally `EMAIL_API_KEY` (Resend for invoice emails), `EMAIL_FROM`.
- **Billing Setup Guide**: Settings → 💳 Billing Setup — shows webhook URL, connection status (Connected/Partial/Not Configured), and step-by-step Razorpay setup instructions.

## Authentication System (Enterprise, Phase 50 v2)
Complete multi-tenant auth rebuilt from scratch. Key files: `auth_system.py`, `web_app.py`.

**Database** (`saas_platform.db`):
- `users` table: `id`, `username`, `email` (unique indexed), `name`, `password`, `provider` (local/google/github), `created_at`, `updated_at`, `total_tasks`, `total_tokens`
- `auth_sessions` table: `id`, `user_id`, `refresh_token` (unique), `device_info`, `ip_address`, `created_at`, `expires_at`

**API Endpoints**:
| Method | Route | Auth | Description |
|--------|-------|------|-------------|
| POST | `/api/auth/signup` | — | Email/password registration → returns access+refresh tokens |
| POST | `/api/auth/login` | — | Email or username login → returns access+refresh tokens |
| POST | `/api/auth/refresh` | — | Refresh token rotation → new access+refresh tokens |
| POST | `/api/auth/logout` | — | Revoke current session |
| POST | `/api/auth/logout-all` | ✓ | Revoke all sessions (all devices) |
| GET | `/api/auth/me` | ✓ | Current user info |
| GET | `/api/auth/sessions` | ✓ | List all active sessions |
| DELETE | `/api/auth/sessions/<id>` | ✓ | Revoke a specific session |
| GET | `/api/auth/google` | — | Start Google OAuth flow |
| GET | `/api/auth/google/callback` | — | Google OAuth callback |
| GET | `/api/auth/github` | — | Start GitHub OAuth flow |
| GET | `/api/auth/github/callback` | — | GitHub OAuth callback |

**Token Design**:
- Access token: JWT, 15 min (configurable via `ACCESS_TOKEN_MINUTES`)
- Refresh token: 128-char hex, 30 days (configurable via `REFRESH_TOKEN_DAYS`), rotated on every refresh
- Brute force: 5 failed attempts per IP per 60s triggers lockout

**UI** (`templates/index.html`):
- Full-screen auth gate overlay (shown when no valid token)
- Sign In / Create Account tabs
- Google + GitHub OAuth buttons
- User badge (top-right) with name/initials after login
- Account panel: session list, per-session revoke, logout-all
- Auto-refresh: token refreshed 60s before expiry; 401 triggers immediate refresh
- OAuth callback: tokens extracted from URL params, URL cleaned

**Secrets needed for OAuth** (add via Replit Secrets):
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` → from Google Cloud Console → Credentials
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` → from GitHub → Settings → Developer Settings → OAuth Apps
- Callback URLs to register: `https://<your-domain>/api/auth/google/callback` and `.../github/callback`

## Security (Updated)
- Rate limiting on all API endpoints (tight limits on auth/task-queuing routes)
- Security headers on every response: `X-Content-Type-Options`, `X-Frame-Options: SAMEORIGIN`, `X-XSS-Protection`, `Referrer-Policy`
- SQL column-name whitelist in `db_update_session` to prevent injection
- JWT secret loaded exclusively from environment secrets (never hardcoded)
- Debug mode controlled by `FLASK_DEBUG` env var (defaults off)
- Gunicorn configured for 1 worker + 8 threads (required for shared in-memory state)

## Branding
Platform name: **NEXORA**. All files updated to remove "openhand"/"OpenHand" references.
Coupon codes: `NEXORA`, `NEXORA90`, `HAMMAD30`, `ELITE7`, `TRYAGENT`, `AGENTELITE`, `PROLIFE`, `ELITE30`.

## External Dependencies
- **Core Packages**: Flask, gunicorn, requests, psutil, bcrypt, PyJWT, python-dotenv, tiktoken, razorpay.
- **Optional/Lazy-loaded Packages**: chromadb, sentence-transformers, playwright.
- **Third-party Services**: OpenAI, Gemini, Anthropic, Groq, OpenRouter, xAI/Grok, AWS Bedrock, Azure OpenAI, Together AI, Fireworks AI, NVIDIA NIM, DeepSeek, Mistral, Cohere, HuggingFace, Replicate, ElevenLabs, Deepgram (for various LLM and multimodal capabilities). Razorpay (payments), Resend (invoice emails).
- **Database**: SQLite — `sessions.db` (sessions, settings, scheduler), `billing.db` (subscriptions, invoices, payment events).