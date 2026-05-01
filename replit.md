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

### Phase 5: BYOK Multi-Provider System (Production-Grade)
- **Provider Registry**: 19 providers across 4 categories — Core (OpenAI, Gemini, Anthropic, Groq, OpenRouter), High Value (xAI/Grok, AWS Bedrock, Azure OpenAI, Together AI, Fireworks AI, NVIDIA NIM), Open/Fallback (DeepSeek, Mistral, Cohere, HuggingFace), Multimodal (Replicate, ElevenLabs, Deepgram), Local (Ollama).
- **Intelligent Routing**: `p5_get_best_provider(plan, keys)` picks fastest/best provider for each plan mode. Lite→fastest (Groq/DeepSeek), Pro→balanced (Gemini/OpenAI/Anthropic), Elite→strongest (Anthropic/OpenAI/xAI). Fallback chain auto-computed from `PLAN_PROVIDER_PREFERENCE` in `config.py`.
- **BYOK API Keys Panel**: Settings modal's BYOK tab now groups providers by category with colored status dots, capability tags, model list, masked key display, and per-key clear buttons.
- **Provider Selector**: Header badge shows active provider with colored dot (green=platform key, orange=BYOK key, grey=unavailable). Click to open dropdown menu grouped by category with speed/capability indicators.
- **Routing API**: New `/api/p5/routing?plan=<lite|pro|elite>` endpoint returns `recommended` provider, `fallback_chain`, and full availability matrix.
- **Failover Bar**: Horizontal bar below header shows `⚡ Failover: OldProvider → NewProvider (reason)`. Auto-dismissed after 12s. Triggered by `[FAILOVER]` or `switching…to` log patterns.
- **Inspector Routing Info**: `Route: <Provider>` + `Fallback: A → B → C` shown in the right inspector's model section.
- **Expanded Force Model Dropdown**: All 19 providers listed with optgroup categories in Settings > Advanced.
- **`config.py` extended**: All new API key env vars, endpoint URLs, model defaults, cost-per-1k, capability map, and `PLAN_PROVIDER_PREFERENCE` dict added.
- **`web_app.py` extended**: `PROVIDERS` dict with full metadata (category, url, speed, quality, caps, models, plan_pref). `p5_get_best_provider()` routing function. `/api/providers` returns rich catalog. `/api/p5/routing` new endpoint.

### Phase 6: Decision Intelligence Layer
- **Priority Selector**: Three-mode routing control (💰 Cheapest / ⚡ Fastest / 🧠 Smartest) persisted in `localStorage` (`p6_priority`) and synced to backend `/api/p6/priority`. Affects all AUTO routing decisions.
- **Provider Lock**: Pin a specific provider from the Intelligence tab; auto-unpin by selecting "Auto". Syncs with Phase 5 provider badge.
- **Smart Recommendation Engine**: `p6OnTaskType()` debounces 600ms after the user types, calls `/api/p6/recommend` with task text + plan + priority, shows an inline recommendation bar below the header with the top provider and a one-click "Use ↗" button.
- **AUTO Pre-execution Hook**: Overrides `nxRunOrStop` — when provider is unlocked (AUTO), fetches recommendation just before running and applies the best provider silently.
- **Intelligence Settings Tab**: New "🧠 Intelligence" tab in Settings modal with: priority selector, provider lock buttons (one per capable provider), full provider comparison table (latency bars, cost tiers, quality tiers, capabilities, success rate, lock toggle), and live performance badges.
- **Performance Recording**: Status badge MutationObserver records success/fail events to `/api/p6/perf/record` for live runtime measurements.
- **Backend functions**: `p6_analyze_task()`, `p6_recommend()`, `p6_record_perf()`, `p6_compute_badges()` in `web_app.py`. Static intelligence matrix `_P6_INTEL` (19 providers × latency/cost/quality/caps). Task pattern mapping `_P6_TASK_PATTERNS`.
- **New endpoints**: `GET /api/p6/performance` (full perf + badges), `POST /api/p6/perf/record`, `POST /api/p6/recommend`, `GET|POST /api/p6/priority`.

### Phase 4: Intelligence & Personalization Layer
- **Dark/Light Theme**: Full theme toggle (🌙/☀️ button in header). CSS `light-theme` class on `<body>` with all vars overridden. Theme persisted in `localStorage` (`p4_theme`). Smooth variable-based transitions.
- **Token/Cost Tracker Pill**: Live `🔢 Ntok · $X.XXXX` pill in header polling `/api/costs/totals` every 5s (and `/api/session/<sid>` when a session is active). Shows only when usage > 0.
- **Session History**: Left panel `Recent Sessions` block lists last 8 sessions from `/api/sessions`, colored status dots, click-to-restore via `selectSession()`. Refreshes every 15s.
- **Prompt Templates**: Categorized chips (Build / Fix / API / Test / Saved) in left panel with custom template save (★ button), saved to `localStorage` (`p4_saved_tpls`), deletable per-chip.
- **AI Suggestion Dropdown**: Input-driven suggestion box under `taskInput` — matches keywords (flask, api, test, react, etc.) and shows completions. Closes on blur. Idle suggestions (3 random tasks) when no session is running.
- **Personalization**: Last plan mode, last model, last session ID all persisted in `localStorage` (`p4_prefs`). Restored on page load after a 500-800ms delay to let UI initialize.
- **UX Intelligence**: MutationObserver-based smooth auto-scroll for logArea (respects `autoScroll` checkbox). Step log-line highlighting for `Step`/`Executing` entries (brief left-border flash).

### Phase 3: Productization Layer
- **Plan System**: 3 modes — Lite (fast/no planning, max 5 steps), Pro (reasoning+planning, max 15 steps), Elite (full autonomy, max 50 steps). Plan mode sent to backend with every task; capability flags (`ALLOW_PLANNING`, `ALLOW_REASONING`, `ALLOW_DEBUG`, `ALLOW_SELF_CORRECTION`, `MAX_AGENT_STEPS`) passed as env vars to agent subprocess.
- **"+" Attach Menu**: Upload File, Upload Image, Upload Folder (multi-file), Import from GitHub — animated dropdown from command bar
- **GitHub Import**: Modal UI calls `/api/github/import`, shows repo context badge, auto-fills task input
- **Context Bar**: Shows active file/repo badges below header, removable, auto-hides when empty
- **Drag & Drop**: Drop files directly onto command bar for instant attachment
- **Branding**: Animated SVG star logo with gradient rotation, logo hover animation

### Phase 7: Structured Agent System
- **5 Specialist Agents**: Code Reviewer, Debugger, Test Generator, Security Auditor, Performance Optimizer
- **Plan-Gated Execution**: Lite → no agents; Pro → Debugger + Code Reviewer; Elite → full 5-agent pipeline
- **Smart Triggering**: Debugger fires on errors; Security fires on backend/auth code; Optimizer fires on heavy/perf tasks; Code Reviewer always runs; Tester fires on code tasks
- **Pipeline Order**: Debugger → Code Reviewer → Security Auditor → Performance Optimizer → Test Generator
- **UI Integration**: Collapsible "Agents" section in the Inspector panel with status indicators, per-agent toggles, and inline results
- **Master Toggle**: Enable/disable the entire agent system from Inspector
- **Per-Agent Toggles**: Each agent has its own ON/OFF toggle
- **Manual Re-run**: "Run Agent Pipeline" button to manually trigger after a session
- **Backend**: `_P7_PIPELINES` dict (in-memory), threaded execution, 5 analysis functions in `web_app.py`
- **New Endpoints**: `GET /api/p7/agents`, `GET|POST /api/p7/config`, `POST /api/p7/pipeline/run`, `GET /api/p7/pipeline/status/<sid>`, `POST /api/p7/pipeline/clear/<sid>`

### Phase 8: Monetization & Access Control Layer
- **Plans**: Free (10 Pro runs/day, no Elite), Pro ($29/mo — 20 Elite runs/month, 10 Pro/day), Elite ($79/mo — unlimited everything)
- **Plan gating**: `/api/queue-task` checks `_p8_check_plan_gate()` before each task; returns `{ok:false, gated:true, reason:...}` when limit exceeded or plan insufficient
- **Usage tracking**: Per-session counters via `get_setting`/`set_setting` with daily (Pro) and monthly (Elite) reset logic
- **Coupons**: `HAMMAD30`→Pro/30d, `ELITE7`→Elite/7d, `PROLIFE`→Pro/lifetime, `ELITE30`→Elite/30d, `OPENHAND`→Pro/7d, `TRYAGENT`→Pro/7d, `AGENTELITE`→Elite/14d
- **Header badge**: `p8-sub-badge` element shows current plan icon + name with colour-coded styling; click opens upgrade modal
- **Upgrade modal**: Full plan comparison cards (Free/Pro/Elite), usage stats grid, coupon input — launched via `p8OpenUpgradeModal()`
- **Settings > Plan tab**: Shows current plan badge, usage bars, coupon input, BYOK Priority toggle (Pro/Elite only)
- **Inspector usage mini-block**: Live progress bars for Pro daily runs and Elite monthly runs shown below model info
- **BYOK Priority mode**: Toggle stored in settings; forces fastest provider routing when using personal API keys
- **New Endpoints**: `GET /api/plan/info`, `POST /api/plan/set`, `POST /api/plan/apply-coupon`, `POST /api/plan/check`, `POST /api/plan/byok-priority`

### Phase 9: Model Intelligence Routing System
- **Three Roles**: `planning` (task decomposition) · `coding` (code generation) · `debug` (error analysis)
- **Plan-tier routing**:
  - Lite → Groq/Llama-3.3-70B for planning+debug, DeepSeek-V3 for coding (8K/4K ctx)
  - Pro → Gemini-1.5-Pro for planning+debug, Qwen2.5-Coder-32B (via OpenRouter) for coding (32K/16K ctx)
  - Elite → DeepSeek-R1 for all three roles (65K/32K ctx)
- **Fallback chain**: Each role has 2 fallbacks; system walks chain automatically on key-missing or rate-limit
- **Fallback log**: In-memory ring buffer of last 50 fallback events; queryable via `/api/p9/fallback-log`
- **Provider availability check**: `_p9_provider_available()` checks env keys + BYOK + health.auth_failed before selecting
- **Token-aware limits**: `context_limit` per role/plan; `chat_role_p9()` caps `max_tokens` to `context_limit/4` for prompt headroom
- **DeepSeek direct**: Added to `_available_apis()` + `_dispatch()` using OpenAI-compatible API
- **Inspector panel**: Live "Active Routing" section shows Planning/Coding/Debug model per plan mode, colour-coded provider dots, fallback indicator (amber dot + "FB" badge)
- **Auto-sync**: JS polls every 30s; re-fetches on plan-selector change and task events
- **New Endpoints**: `GET /api/p9/routing?plan_mode=`, `POST /api/p9/route-for-role`, `GET /api/p9/fallback-log`, `GET /api/p9/providers`

### Phase 10: Agent Intelligence & Memory System
- **Short-term memory (STM)**: In-memory per-session deque (last 20 tasks) via `_P10_STM`
- **`p10_record_task(sid, task, result, success)`**: Appends a timestamped entry to STM + long-term memory
- **`p10_inject_context(sid, task)`**: Retrieves recent STM + semantic matches from LongTermMemory; formats a context block injected into prompts
- **`p10_self_correct_prompt(sid, task, prev_error)`**: Builds "retry prompt" that includes prior failure + learned hints
- **`p10_get_score(sid)`**: Returns quality score dict (calls, success_rate, retry_rate, grade, quality_score)
- **New Endpoints**: `GET /api/agent/score`, `GET /api/memory/recent`, `GET /api/memory/insights`
- **Inspector panel**: "🧠 Intelligence Score" section shows grade badge (A-F), success rate, total calls, last 5 memory items with coloured left-border (✓ success / ✗ fail)
- **Auto-refresh**: Polls `/api/agent/score` + `/api/memory/recent` every 45s; also refreshes on `nxTaskDone` event

### Phase 11: Multi-Agent Collaboration System
- **`P11AgentTeam` class**: 4-agent pipeline — Manager (plan) → Research (gather) → Coding (implement) → Debug (verify)
- **Run lifecycle**: `start(task)` spawns a background thread; status tracked in `_P11_RUNS` dict; each step updates `steps[]` + `log_lines[]`
- **Cancellation**: `cancel()` sets a threading `Event`; each agent step checks it before proceeding
- **New Endpoints**: `POST /api/p11/team/run`, `GET /api/p11/team/status`, `POST /api/p11/team/cancel`, `GET /api/p11/team/list`
- **"🤖 AI Team" center tab**: Agent cards grid (Manager/Research/Coding/Debug) with live status coloring; scrollable log stream; task input + Run/Cancel buttons
- **Tab switching**: setActiveTab patched with `_p11_origSetTab` wrapper to show/hide `#tabAiteam` panel

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

## Phase 13–16 Intelligence Upgrades

### Phase 13 — Intelligent Context Compression
- **3-tier memory**: Tier 1 = last 5 messages, Tier 2 = compressed summary, Tier 3 = Phase 10 LTM
- **Auto-summarization**: fires in background thread when session > 10 messages
- **DB**: `chat_summaries` table (session_id PRIMARY KEY, summary, msg_count, updated_at)
- **Edit safety**: `_clear_chat_summary(sid)` called on prompt edit + chat clear
- **Endpoints**: `GET /api/chat/<sid>/summary`
- **UI**: "⚡ Context optimized" badge in Chat tab footer, expandable "View summary"

### Phase 14 — Self-Improving AI System
- **Post-session reflection**: async thread triggers `evolution_engine.reflect_on_task()` after every completed task
- **Settings**: `p14_learning_enabled`, `p14_auto_optimize` keys stored in settings table
- **Endpoints**: `GET /api/learning/insights`, `POST /api/learning/settings`, `POST /api/learning/reset`, `POST /api/learning/enhance`
- **Inspector**: "🧠 Learning Insights" collapsible section with strategy win rates + recent reflections
- **Settings → Intelligence tab**: Enable Learning + Auto Prompt Optimization toggles

### Phase 15 — AI Learning Dashboard
- **Endpoints**: `GET /api/dashboard/metrics`, `GET /api/dashboard/timeline`, `GET /api/dashboard/failure-analysis`, `GET /api/learning/export`
- **Tab**: "📊 Learning" in More ▾ dropdown (`nxTab-learning`)
- **UI**: 4-metric summary row, success trend sparkline (CSS bars), strategy win-rate bar chart, failure pattern analysis, learning timeline, controls panel
- **Auto-refresh**: every 45s when tab is visible

### Phase 16 — Autonomous Goal-Driven AI
- **Endpoints**: `POST /api/goals/decompose`, `GET /api/goals/chain/<cid>`
- **Tab**: "🎯 Goal Mode" in More ▾ dropdown (`nxTab-goals`)
- **UI**: Goal input textarea + priority selector, task breakdown list, progress bar, active chains panel
- **Execution**: uses existing chain runner (`runner.create_chain()` + `/api/chains/<cid>/run-next`)
- **Polling**: 5s interval updates task statuses and progress bar
