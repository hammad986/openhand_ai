"""app.py — Single-command entry point for OpenHand AI.

Usage:
    python app.py

This is an alias for web_app.py that provides the expected
`python app.py` startup experience. Handles venv detection,
port selection, and opens the browser automatically.
"""
import os
import sys
import time
import threading
import webbrowser

# ── Environment bootstrap ──────────────────────────────────────────────
def _bootstrap():
    """Ensure we're running in the correct environment."""
    base = os.path.dirname(os.path.abspath(__file__))
    
    # Check for .venv
    venv_dirs = [".venv", "venv", "my_env"]
    venv_python = None
    for vd in venv_dirs:
        candidate = os.path.join(base, vd, "Scripts", "python.exe")  # Windows
        if not os.path.exists(candidate):
            candidate = os.path.join(base, vd, "bin", "python")      # Unix
        if os.path.exists(candidate):
            venv_python = candidate
            break

    # If a venv exists but we're not using it, re-exec with it
    if venv_python:
        current = os.path.realpath(sys.executable)
        target  = os.path.realpath(venv_python)
        if current != target:
            print(f"[app.py] Re-launching with venv: {venv_python}")
            os.execv(venv_python, [venv_python] + sys.argv)


_bootstrap()

# ── Banner ─────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 5000))

print(r"""
  ___                   _   _                 _         _    ___
 / _ \ _ __   ___ _ __ | | | | __ _ _ __   __| |   /\  | |  |_ _|
| | | | '_ \ / _ \ '_ \| |_| |/ _` | '_ \ / _` |  /  \ | |   | |
| |_| | |_) |  __/ | | |  _  | (_| | | | | (_| | / /\ \| |___| |
 \___/| .__/ \___|_| |_|_| |_|\__,_|_| |_|\__,_|/_/  \_\_____|___|
      |_|
""")
print(f"  🤖  OpenHand AI — Phase 30 Production Build")
print(f"  🌐  Starting server on http://localhost:{PORT}")
print(f"  ⌨   Press Ctrl+C to stop\n")

# ── Auto-open browser after 1.5s ──────────────────────────────────────
def _open_browser():
    time.sleep(1.5)
    try:
        webbrowser.open(f"http://localhost:{PORT}")
    except Exception:
        pass

if os.environ.get("NO_BROWSER") != "1":
    threading.Thread(target=_open_browser, daemon=True).start()

# ── Import and run web_app ─────────────────────────────────────────────
from web_app import app

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
        threaded=True,
        use_reloader=False,
    )
