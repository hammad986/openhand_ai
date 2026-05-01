# Multi-Agent AI Dev System

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

## External Dependencies
- **Core Packages**: Flask, gunicorn, requests, psutil, bcrypt, PyJWT, python-dotenv, tiktoken.
- **Optional/Lazy-loaded Packages**: chromadb, sentence-transformers, playwright.
- **Third-party Services**: OpenAI, Gemini, Anthropic, Groq, OpenRouter, xAI/Grok, AWS Bedrock, Azure OpenAI, Together AI, Fireworks AI, NVIDIA NIM, DeepSeek, Mistral, Cohere, HuggingFace, Replicate, ElevenLabs, Deepgram (for various LLM and multimodal capabilities).
- **Database**: SQLite (for session data, scheduler data, and chat summaries).