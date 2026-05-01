"""
browser_automation.py — Phase 42: Full Browser Automation (Playwright)
=======================================================================
Integrates Playwright for full headless browser automation capabilities:
- Open websites and render Javascript
- Scrape structured data
- Take screenshots
- Extract dynamic assets
"""
import logging
import os
import json
import asyncio
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class BrowserAutomation:
    def __init__(self):
        self._playwright_installed = False
        self._check_installation()

    def _check_installation(self):
        try:
            import playwright
            self._playwright_installed = True
        except ImportError:
            logger.warning("[BrowserAutomation] Playwright is not installed. Browser automation disabled.")
            logger.warning("To enable: pip install playwright && playwright install chromium")
            self._playwright_installed = False

    async def _run_task(self, action: str, url: str, **kwargs) -> Dict[str, Any]:
        if not self._playwright_installed:
            return {"error": "Playwright is not installed on the system."}

        from playwright.async_api import async_playwright
        
        result = {"url": url, "action": action, "success": False}
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
                
                logger.info(f"[BrowserAutomation] Navigating to {url}")
                await page.goto(url, wait_until="networkidle", timeout=30000)

                if action == "screenshot":
                    path = kwargs.get("path", "./workspace/screenshots/capture.png")
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    await page.screenshot(path=path, full_page=kwargs.get("full_page", False))
                    result["screenshot_path"] = path
                    result["success"] = True

                elif action == "scrape_text":
                    # Remove scripts, styles, etc., and extract visible text
                    text = await page.evaluate('''() => {
                        const s = document.createElement('div');
                        s.innerHTML = document.body.innerHTML;
                        const scripts = s.getElementsByTagName('script');
                        let i = scripts.length; while (i--) { scripts[i].parentNode.removeChild(scripts[i]); }
                        const styles = s.getElementsByTagName('style');
                        i = styles.length; while (i--) { styles[i].parentNode.removeChild(styles[i]); }
                        return s.innerText.replace(/\\n\\s*\\n/g, '\\n').trim();
                    }''')
                    result["text"] = text[:50000] # Limit size
                    result["title"] = await page.title()
                    result["success"] = True
                    
                elif action == "extract_assets":
                    # Find all images
                    images = await page.evaluate('''() => {
                        return Array.from(document.querySelectorAll('img')).map(img => img.src).filter(src => src && src.startsWith('http'));
                    }''')
                    result["images"] = images
                    result["success"] = True

                await browser.close()
                return result
                
        except Exception as e:
            logger.error(f"[BrowserAutomation] Task failed: {str(e)}")
            result["error"] = str(e)
            return result

    def run(self, action: str, url: str, **kwargs) -> Dict[str, Any]:
        """Synchronous wrapper for browser automation tasks."""
        logger.info(f"[BrowserAutomation] Executing task: {action} on {url}")
        
        # Determine if we are already in an event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're in a thread that already has a running loop (like an async endpoint)
                # Note: This is a simplistic handling. In a real complex async app, 
                # you'd want to schedule this properly or use an executor.
                raise RuntimeError("Cannot run sync wrapper from inside a running async loop. Use async methods directly.")
        except RuntimeError:
            pass

        return asyncio.run(self._run_task(action, url, **kwargs))

# Singleton
_browser_instance = None
def get_browser_automation() -> BrowserAutomation:
    global _browser_instance
    if _browser_instance is None:
        _browser_instance = BrowserAutomation()
    return _browser_instance
