# Nexora AI Platform

## Overview
Nexora is an autonomous coding agent platform designed to streamline software development through AI-driven processes. It features a multi-LLM router with intelligent routing and auto-failover, a comprehensive web-based UI, and terminal integration. The system operates on a Plan → Execute → Observe → Fix development loop, aiming for a robust, self-improving, and highly customizable environment for AI-assisted software engineering. Its core purpose is to provide an efficient and intelligent platform for AI-powered development, with capabilities extending to complex task orchestration, self-correction, and collaborative agent systems.

## User Preferences
I prefer iterative development with clear, concise feedback. I value transparency in the AI's decision-making process and prefer to be asked before major architectural changes are implemented. I appreciate detailed explanations when new features or complex solutions are introduced.

## System Architecture
The platform is built on a Flask web application (`web_app.py`) serving both frontend and backend. Core AI functionalities are handled by `agent.py`, `orchestrator.py`, and a multi-LLM `router.py` with cost-aware and failover capabilities. A dynamic tool system is implemented via `tools.py` and a `tools/` directory. Memory management employs `memory.py`, `vector_store.py`, and `long_term_memory.py` using chromadb and sentence-transformers, with shared state managed by `mcp_context.py`. Session data is persisted in SQLite (`sessions.db`) within per-session workspaces, and HTML templates are located in `/templates/`.

The UI, "NX Advanced Interface (v2)", features a three-panel layout for AI thinking, session controls, logs, code, terminal, metrics, live status, model info, and error handling. It includes dark/light themes, real-time token/cost tracking, session history, prompt templates, AI suggestion dropdowns, and an animated SVG logo.

The system incorporates a multi-provider LLM system with intelligent routing based on plan modes (Lite, Pro, Elite) and user preferences (Cheapest, Fastest, Smartest). A Decision Intelligence Layer allows users to prioritize LLM providers, and a smart recommendation engine suggests optimal LLMs for tasks. A Structured Agent System employs specialist agents (Code Reviewer, Debugger, Test Generator, Security Auditor, Performance Optimizer) whose execution is triggered contextually and gated by the chosen plan mode. A Model Intelligence Routing System assigns specific LLMs for planning, coding, and debugging roles with automatic fallbacks. An Agent Intelligence & Memory System provides short-term memory, semantic long-term memory integration, and self-correction.

Further enhancements include a Multi-Agent Collaboration System (Manager, Research, Coding, Debug agents), 3-tier Intelligent Context Compression, a Self-Improving AI System with post-session reflection, and an AI Learning Dashboard. The platform supports Autonomous Goal-Driven AI with task decomposition, execution graph visualization, and a Task Scheduler for background agents.

Security features include rate limiting, input sanitization, enhanced authentication, structured logging, and administrative controls.

A comprehensive real SaaS billing system is integrated, including `payments.py` for order creation, webhook processing, subscription management, and invoice generation. It utilizes `billing.db` for subscription, invoice, and payment event data. An Admin Control Panel at `/admin` provides role-based access for system configuration, user management, feature toggling, pricing adjustments, token/rate limits, coupon management, AI provider controls, model routing, concurrency settings, and a configuration history with rollback.

The authentication system is multi-tenant, managing `users` and `auth_sessions` in `saas_platform.db`. It supports email/password and OAuth (Google, GitHub) logins, JWT access tokens, refresh token rotation, and session management. An Idempotency + Billing Safety Layer (`idempotency.py`) provides transaction safety, daily token usage tracking, payment deduplication, provider failure logging, and protected routes with caching and replay mechanisms. A Real-Time Notification & Event System (`notifications.py`) offers persistent, prioritized, SSE-pushed notifications with email fallbacks, storing data in `saas_platform.db`. A Customer Support System (`support.py`) manages support tickets in `support.db`, featuring AI auto-tagging, billing info auto-attachment, and email notifications.

## External Dependencies
- **Core Packages**: Flask, gunicorn, requests, psutil, bcrypt, PyJWT, python-dotenv, tiktoken, razorpay.
- **Optional/Lazy-loaded Packages**: chromadb, sentence-transformers, playwright.
- **Third-party Services**: OpenAI, Gemini, Anthropic, Groq, OpenRouter, xAI/Grok, AWS Bedrock, Azure OpenAI, Together AI, Fireworks AI, NVIDIA NIM, DeepSeek, Mistral, Cohere, HuggingFace, Replicate, ElevenLabs, Deepgram (for LLM and multimodal capabilities). Razorpay (payments), Resend (invoice emails).
- **Databases**: SQLite (`sessions.db`, `billing.db`, `saas_platform.db`).