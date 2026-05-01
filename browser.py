"""
browser.py - Browser Automation Tool
Uses Playwright for full browser control:
  • Navigate, click, fill forms
  • Screenshot + DOM inspection
  • Console errors (DevTools level)
  • Network request monitoring

Install: pip install playwright && playwright install chromium
"""

import base64
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Lazy import so system works even without Playwright ───────────────────────
try:
    from playwright.sync_api import sync_playwright, Page, Browser, ConsoleMessage
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not installed. Run: pip install playwright && playwright install chromium")


class BrowserTool:
    """
    Stateful browser session — one browser instance, reused across actions.
    """

    def __init__(self, workspace: str = "./workspace", headless: bool = True):
        self.workspace  = Path(workspace)
        self.headless   = headless
        self._pw        = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page]       = None
        self._console_logs: list         = []
        self._network_logs: list         = []

    # ─────────────────────────────────────────────────────
    # Session management
    # ─────────────────────────────────────────────────────
    def start(self) -> dict:
        if not PLAYWRIGHT_AVAILABLE:
            return self._err("Playwright not installed. Run: pip install playwright && playwright install chromium")
        if self._browser:
            return {"success": True, "output": "Browser already running", "error": ""}
        try:
            self._pw      = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
            ctx           = self._browser.new_context(viewport={"width": 1280, "height": 800})
            self._page    = ctx.new_page()

            # DevTools: capture console messages
            self._page.on("console", self._on_console)
            # DevTools: capture network failures
            self._page.on("requestfailed", self._on_request_failed)

            logger.info("[Browser] Started")
            return {"success": True, "output": "Browser started (Chromium headless)", "error": ""}
        except Exception as e:
            return self._err(str(e))

    def stop(self) -> dict:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
            self._browser = self._page = self._pw = None
            return {"success": True, "output": "Browser stopped", "error": ""}
        except Exception as e:
            return self._err(str(e))

    # ─────────────────────────────────────────────────────
    # Navigation
    # ─────────────────────────────────────────────────────
    def navigate(self, url: str, wait_until: str = "networkidle") -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            resp = p.goto(url, wait_until=wait_until, timeout=30000)
            status = resp.status if resp else "?"
            return {"success": True, "output": f"Navigated to {url} [HTTP {status}]", "error": ""}
        except Exception as e:
            return self._err(str(e))

    def get_url(self) -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        return {"success": True, "output": p.url, "error": ""}

    # ─────────────────────────────────────────────────────
    # Interaction
    # ─────────────────────────────────────────────────────
    def click(self, selector: str) -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            p.click(selector, timeout=10000)
            return {"success": True, "output": f"Clicked: {selector}", "error": ""}
        except Exception as e:
            return self._err(f"click({selector}): {e}")

    def fill(self, selector: str, value: str) -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            p.fill(selector, value, timeout=10000)
            return {"success": True, "output": f"Filled '{selector}' with '{value}'", "error": ""}
        except Exception as e:
            return self._err(f"fill({selector}): {e}")

    def select(self, selector: str, value: str) -> dict:
        """Select dropdown option"""
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            p.select_option(selector, value, timeout=10000)
            return {"success": True, "output": f"Selected '{value}' in '{selector}'", "error": ""}
        except Exception as e:
            return self._err(str(e))

    def press(self, selector: str, key: str) -> dict:
        """Press key (Enter, Tab, Escape, etc.)"""
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            p.press(selector, key)
            return {"success": True, "output": f"Pressed {key} on {selector}", "error": ""}
        except Exception as e:
            return self._err(str(e))

    def hover(self, selector: str) -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            p.hover(selector, timeout=10000)
            return {"success": True, "output": f"Hovered: {selector}", "error": ""}
        except Exception as e:
            return self._err(str(e))

    def wait_for(self, selector: str, timeout: int = 10000) -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            p.wait_for_selector(selector, timeout=timeout)
            return {"success": True, "output": f"Element found: {selector}", "error": ""}
        except Exception as e:
            return self._err(f"wait_for({selector}): {e}")

    # ─────────────────────────────────────────────────────
    # DOM inspection
    # ─────────────────────────────────────────────────────
    def get_text(self, selector: str = "body") -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            text = p.inner_text(selector, timeout=5000)
            return {"success": True, "output": text[:3000], "error": ""}
        except Exception as e:
            return self._err(str(e))

    def get_html(self, selector: str = "body") -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            html = p.inner_html(selector, timeout=5000)
            return {"success": True, "output": html[:5000], "error": ""}
        except Exception as e:
            return self._err(str(e))

    def get_attribute(self, selector: str, attribute: str) -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            val = p.get_attribute(selector, attribute, timeout=5000)
            return {"success": True, "output": str(val), "error": ""}
        except Exception as e:
            return self._err(str(e))

    def evaluate_js(self, script: str) -> dict:
        """Run JavaScript in browser context (DevTools level)"""
        p = self._require_page()
        if isinstance(p, dict): return p
        try:
            result = p.evaluate(script)
            return {"success": True, "output": str(result), "error": ""}
        except Exception as e:
            return self._err(str(e))

    # ─────────────────────────────────────────────────────
    # DevTools: logs & errors
    # ─────────────────────────────────────────────────────
    def get_console_logs(self) -> dict:
        """Return all captured console logs (like DevTools Console)"""
        logs = "\n".join(
            f"[{l['type'].upper()}] {l['text']}" for l in self._console_logs
        ) or "(no console output)"
        return {"success": True, "output": logs, "error": ""}

    def get_errors(self) -> dict:
        """Return only console errors"""
        errors = [l for l in self._console_logs if l["type"] in ("error", "warning")]
        out = "\n".join(f"[{e['type'].upper()}] {e['text']}" for e in errors) or "(no errors)"
        return {"success": True, "output": out, "error": ""}

    def get_network_failures(self) -> dict:
        out = "\n".join(self._network_logs) or "(no network failures)"
        return {"success": True, "output": out, "error": ""}

    def clear_logs(self) -> dict:
        self._console_logs.clear()
        self._network_logs.clear()
        return {"success": True, "output": "Logs cleared", "error": ""}

    # ─────────────────────────────────────────────────────
    # Screenshot
    # ─────────────────────────────────────────────────────
    def screenshot(self, filename: str = "screenshot.png", full_page: bool = True) -> dict:
        p = self._require_page()
        if isinstance(p, dict): return p
        path = self.workspace / filename
        try:
            p.screenshot(path=str(path), full_page=full_page)
            return {"success": True, "output": f"Screenshot saved: {path}", "error": ""}
        except Exception as e:
            return self._err(str(e))

    # ─────────────────────────────────────────────────────
    # Form automation helper
    # ─────────────────────────────────────────────────────
    def fill_form(self, fields: dict) -> dict:
        """
        Fill multiple form fields at once.
        fields = {"#username": "admin", "#password": "secret", ...}
        """
        results = []
        for selector, value in fields.items():
            r = self.fill(selector, value)
            results.append(f"{'✅' if r['success'] else '❌'} {selector}: {r['output'] or r['error']}")
        return {"success": True, "output": "\n".join(results), "error": ""}

    # ─────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────
    def _require_page(self):
        if not self._page:
            r = self.start()
            if not r["success"]:
                return r
        return self._page

    def _on_console(self, msg: "ConsoleMessage"):
        self._console_logs.append({"type": msg.type, "text": msg.text})

    def _on_request_failed(self, request):
        self._network_logs.append(f"{request.method} {request.url} → {request.failure}")

    def _err(self, msg: str) -> dict:
        return {"success": False, "output": "", "error": msg}

    # ─────────────────────────────────────────────────────
    # Tool schema for agent
    # ─────────────────────────────────────────────────────
    @staticmethod
    def schema() -> str:
        return """
BROWSER TOOLS:
9.  browser_start()                          – Launch browser session
10. browser_stop()                           – Close browser
11. browser_navigate(url)                    – Go to URL
12. browser_click(selector)                  – Click element (CSS selector)
13. browser_fill(selector, value)            – Fill input field
14. browser_fill_form(fields: dict)          – Fill multiple fields at once
15. browser_press(selector, key)             – Press key (Enter/Tab/Escape)
16. browser_wait_for(selector)               – Wait for element to appear
17. browser_get_text(selector?)              – Get visible text
18. browser_get_html(selector?)              – Get HTML source
19. browser_evaluate_js(script)             – Run JavaScript (DevTools level)
20. browser_get_console_logs()              – Get all console output
21. browser_get_errors()                    – Get console errors only
22. browser_screenshot(filename?)           – Take screenshot
"""