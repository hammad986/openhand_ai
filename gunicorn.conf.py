# gunicorn.conf.py — Phase 19: Production WSGI Server Configuration
# ═══════════════════════════════════════════════════════════════════
# Usage: gunicorn -c gunicorn.conf.py web_app:app
# ═══════════════════════════════════════════════════════════════════

import os
import multiprocessing

# ── Binding ──────────────────────────────────────────────────────────────────
bind    = f"0.0.0.0:{os.getenv('PORT', '5000')}"

# ── Worker config ─────────────────────────────────────────────────────────────
# For a Flask + background-thread app (scheduler, queue worker) we use
# exactly 1 sync worker to avoid sharing in-memory state across processes.
# Scale vertically (threads) rather than horizontally (processes).
workers     = 1
threads     = int(os.getenv("GUNICORN_THREADS", "4"))
worker_class = "sync"
timeout     = int(os.getenv("GUNICORN_TIMEOUT", "120"))
keepalive   = 5

# ── Logging ──────────────────────────────────────────────────────────────────
loglevel      = os.getenv("GUNICORN_LOG_LEVEL", "info")
accesslog     = "-"          # stdout
errorlog      = "-"          # stderr
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Security ─────────────────────────────────────────────────────────────────
# Do not expose the Gunicorn version header
server_name = "openhand"

# ── Process title ────────────────────────────────────────────────────────────
proc_name = "openhand-ai"

# ── Lifecycle hooks ───────────────────────────────────────────────────────────
def on_starting(server):
    server.log.info("OpenHand AI starting up (production mode).")

def on_exit(server):
    server.log.info("OpenHand AI shutting down.")
