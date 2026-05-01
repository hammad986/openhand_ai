#!/usr/bin/env python3
"""
main.py - CLI Entry Point v2
Commands:
  python main.py "Build login page"        → run task
  python main.py --chat                    → interactive chat
  python main.py --history                 → task history
  python main.py --learn                   → show learnings
  python main.py --stats                   → API health
  python main.py --project myapp           → set active project
  python main.py --model groq "task"       → force API
  python main.py --clear                   → clear memory
"""

import argparse
import sys
from agent  import Agent
from memory import Memory
from config import Config


BANNER = """
╔═══════════════════════════════════════════════════╗
║   🤖 Multi-Agent AI Dev System  v2.0              ║
║   Browser • Git • Server • Self-Fixing • Memory   ║
╚═══════════════════════════════════════════════════╝
"""


def main():
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Multi-Agent AI Dev System")
    parser.add_argument("task",     nargs="?", help="Task to run")
    parser.add_argument("--chat",   action="store_true", help="Interactive chat")
    parser.add_argument("--history",action="store_true", help="Task history")
    parser.add_argument("--learn",  action="store_true", help="Show learnings")
    parser.add_argument("--stats",  action="store_true", help="API health stats")
    parser.add_argument("--clear",  action="store_true", help="Clear memory")
    parser.add_argument("--model",  type=str,  help="Force API (groq/local/openrouter/nvidia/together)")
    parser.add_argument("--project",type=str,  help="Set active project name")
    parser.add_argument("--headed", action="store_true", help="Browser in headed (visible) mode")
    args = parser.parse_args()

    print(BANNER)
    config = Config()
    memory = Memory(config)
    agent  = Agent(config=config, memory=memory, force_model=args.model)

    # ── Modifiers ─────────────────────────────────────
    if args.headed:
        agent.tools._browser.headless = False

    if args.clear:
        memory.clear()
        print("✅ Memory cleared."); return

    if args.history:
        memory.show_history(); return

    if args.learn:
        memory.show_learnings(); return

    if args.stats:
        agent.router.print_stats(); return

    if args.project:
        memory.set_project(args.project)
        print(f"✅ Active project: {args.project}"); return

    # ── Run task ──────────────────────────────────────
    if args.task:
        result = agent.run(args.task)
        print(f"\n📋 Result:\n{result}")
        return

    # ── Interactive mode ──────────────────────────────
    if args.chat:
        mode = "💬 Chat"
    else:
        mode = "🤖 Agent"

    print(f"{mode} mode — type 'exit' to quit, 'stats' for API health\n")
    while True:
        try:
            inp = input(f"{mode} → ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋 Bye!"); break

        if not inp: continue
        if inp.lower() in ("exit","quit","q"): print("👋 Bye!"); break
        if inp.lower() == "stats": agent.router.print_stats(); continue
        if inp.lower() == "history": memory.show_history(); continue
        if inp.lower() == "learnings": memory.show_learnings(); continue

        if args.chat:
            print(agent.chat(inp))
        else:
            agent.run(inp)


if __name__ == "__main__":
    main()