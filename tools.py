"""
tools.py - Complete Tool Registry v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FILE TOOLS:    read/write/delete/list/diff_edit/search_replace
CODE EXEC:     run_python, run_shell
SERVER TOOLS:  server_start, server_stop, server_test
GIT TOOLS:     git_init, git_commit, git_push, git_status
BROWSER TOOLS: navigate, click, fill, eval_js, errors, screenshot
"""

import subprocess, tempfile, time, logging, shlex, sys, socket, platform
from pathlib import Path
from config import Config
from browser import BrowserTool

logger = logging.getLogger(__name__)


class Tools:
    def __init__(self, config: Config):
        self.config    = config
        self.workspace = Path(config.WORKSPACE_DIR)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._browser  = BrowserTool(workspace=str(self.workspace))
        self._servers  = {}   # name → {process, port, command}

    # ── Dispatcher ────────────────────────────────────────────────────────────
    def execute(self, tool_name: str, **kwargs) -> dict:
        registry = {
            "read_file": self.read_file, "write_file": self.write_file,
            "append_file": self.append_file,
            "list_files": self.list_files, "delete_file": self.delete_file,
            "diff_edit": self.diff_edit, "search_replace": self.search_replace,
            "run_python": self.run_python, "run_shell": self.run_shell,
            "server_start": self.server_start, "server_stop": self.server_stop,
            "server_test": self.server_test,
            "git_init": self.git_init, "git_commit": self.git_commit,
            "git_push": self.git_push, "git_status": self.git_status,
            "browser_start": self._browser.start, "browser_stop": self._browser.stop,
            "browser_navigate": self._browser.navigate,
            "browser_click": self._browser.click, "browser_fill": self._browser.fill,
            "browser_fill_form": self._browser.fill_form,
            "browser_press": self._browser.press,
            "browser_wait_for": self._browser.wait_for,
            "browser_get_text": self._browser.get_text,
            "browser_get_html": self._browser.get_html,
            "browser_evaluate_js": self._browser.evaluate_js,
            "browser_get_errors": self._browser.get_errors,
            "browser_get_console_logs": self._browser.get_console_logs,
            "browser_screenshot": self._browser.screenshot,
        }
        if tool_name not in registry:
            return self._err(f"Unknown tool: '{tool_name}'\nAvailable: {sorted(registry)}")
        try:
            return registry[tool_name](**kwargs)
        except TypeError as e:
            return self._err(f"Wrong args for {tool_name}: {e}")
        except Exception as e:
            logger.exception(f"Tool {tool_name} crashed")
            return self._err(str(e))

    # ── File tools ────────────────────────────────────────────────────────────
    def read_file(self, path: str) -> dict:
        p = self._res(path)
        if not p.exists(): return self._err(f"Not found: {path}")
        return self._ok(p.read_text(encoding="utf-8"))

    def write_file(self, path: str, content: str) -> dict:
        p = self._res(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        logger.info(f"[Tools] Written: {p}")
        print(f"[FILE_WRITE] {path} ({len(content)} chars)", flush=True)
        return self._ok(f"Written: {path} ({len(content)} chars)")

    def append_file(self, path: str, content: str) -> dict:
        p = self._res(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"[Tools] Appended: {p}")
        print(f"[FILE_EDIT] {path}", flush=True)
        return self._ok(f"Appended to: {path} ({len(content)} chars)")

    def delete_file(self, path: str) -> dict:
        p = self._res(path)
        if not p.exists(): return self._err(f"Not found: {path}")
        p.unlink()
        return self._ok(f"Deleted: {path}")

    def list_files(self, subdir: str = "") -> dict:
        t = self._res(subdir) if subdir else self.workspace
        if not t.exists(): return self._err("Dir not found")
        files = sorted(str(f.relative_to(self.workspace)) for f in t.rglob("*") if f.is_file())
        return self._ok("\n".join(files) or "(empty)")

    def search_replace(self, path: str, search: str, replace: str) -> dict:
        r = self.read_file(path)
        if not r["success"]: return r
        if search not in r["output"]: return self._err(f"Pattern not found in {path}")
        return self.write_file(path, r["output"].replace(search, replace, 1))

    def diff_edit(self, path: str, edits: list) -> dict:
        """
        Patch-style multi-edit.
        edits = [{"search": "old", "replace": "new"}, ...]
        """
        r = self.read_file(path)
        if not r["success"]: return r
        content, applied = r["output"], 0
        for e in edits:
            s, rep = e.get("search",""), e.get("replace","")
            if s in content:
                content = content.replace(s, rep, 1); applied += 1
            else:
                logger.warning(f"[diff_edit] Not found: {s[:40]!r}")
        result = self.write_file(path, content)
        result["output"] = f"Applied {applied}/{len(edits)} patches to {path}"
        return result

    # ── Code execution ────────────────────────────────────────────────────────
    def run_python(self, code: str = None, path: str = None, timeout: int = 30) -> dict:
        if path:
            fp = self._res(path)
            if not fp.exists():
                return self._err(
                    f"Not found: {path!r}  (resolved → {fp})  "
                    f"cwd={self.workspace}"
                )
            # Always run from workspace root so Flask templates/, static/ etc. resolve correctly
            logger.debug(f"[run_python] path={fp} | cwd={self.workspace}")
            return self._run([sys.executable, str(fp)], cwd=str(self.workspace), timeout=timeout)
        if code:
            # Compatibility: models sometimes send code='script.py' instead of path='script.py'.
            maybe_path = self._res(code.strip()) if isinstance(code, str) else None
            if maybe_path and maybe_path.exists() and maybe_path.suffix == ".py":
                logger.debug(f"[run_python] code-as-path={maybe_path} | cwd={self.workspace}")
                return self._run([sys.executable, str(maybe_path)], cwd=str(self.workspace), timeout=timeout)

            tmp = tempfile.NamedTemporaryFile(suffix=".py", mode="w",
                                              dir=str(self.workspace), delete=False)
            tmp.write(code); tmp.close()
            try:
                return self._run([sys.executable, tmp.name], cwd=str(self.workspace), timeout=timeout)
            finally:
                try:
                    Path(tmp.name).unlink()
                except OSError:
                    pass
        return self._err("Provide 'code' or 'path'")

    def run_shell(self, command: str, timeout: int = 60) -> dict:
        # Block genuinely destructive patterns only — pipes/redirects are allowed
        BLOCKED = [
            "rm -rf /", "rm -rf ~", "mkfs", ":(){ :|:& };:",
            "shutdown", "reboot",
            "del /f /s /q c:\\", "format c:",
            "> /dev/sda", "dd if=/dev/zero",
        ]
        cmd_lower = command.strip().lower()
        for b in BLOCKED:
            if b in cmd_lower:
                return self._err(f"Blocked dangerous command: {b}")
        # Route through platform shell so &&, ||, |, ; and > all work naturally.
        # Windows: cmd /c (supports && on ALL versions; PowerShell 5 does not)
        # Linux/Mac: bash -c
        if platform.system() == "Windows":
            argv = ["cmd", "/c", command]
        else:
            argv = ["bash", "-c", command]
        return self._run(argv, cwd=str(self.workspace), timeout=timeout)

    # ── Server tools ──────────────────────────────────────────────────────────
    def server_start(self, command: str, port: int, name: str = "default",
                     wait_seconds: int = 15) -> dict:
        if name in self._servers:
            return self._ok(f"'{name}' already running on :{port}")
        argv = self._prepare_command(command)
        proc = subprocess.Popen(argv, cwd=str(self.workspace),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Wait until the port is actually accepting connections (not just a fixed sleep)
        ready = self._wait_for_port(port, timeout=max(wait_seconds, 15))
        if proc.poll() is not None or not ready:
            proc.terminate()
            stderr_out = ""
            try:
                stderr_out = proc.stderr.read().decode(errors="replace")[:600]
            except Exception:
                pass
            reason = "crashed" if proc.poll() is not None else f"did not bind on :{port} within {max(wait_seconds, 15)}s"
            return self._err(f"Server {reason}.\n{stderr_out}")
        self._servers[name] = {"process": proc, "port": port, "command": argv}
        return self._ok(f"Server '{name}' running → http://localhost:{port}")

    def server_stop(self, name: str = "default") -> dict:
        s = self._servers.pop(name, None)
        if not s: return self._err(f"No server: {name}")
        s["process"].terminate()
        return self._ok(f"Server '{name}' stopped")

    def _wait_for_port(self, port: int, timeout: int = 15) -> bool:
        """Poll until TCP port accepts a connection or timeout expires."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return True
            except OSError:
                time.sleep(0.4)
        return False

    def server_test(self, url: str, method: str = "GET",
                    expect_status: int = 200, data: dict = None) -> dict:
        import urllib.request, json as _j
        try:
            body = _j.dumps(data).encode() if data else None
            req  = urllib.request.Request(url, data=body, method=method,
                                          headers={"Content-Type":"application/json"} if data else {})
            with urllib.request.urlopen(req, timeout=10) as resp:
                text   = resp.read().decode()[:2000]
                status = resp.status
                ok     = status == expect_status
                return {"success": ok, "output": f"HTTP {status}\n{text}",
                        "error": "" if ok else f"Expected {expect_status}, got {status}"}
        except Exception as e:
            return self._err(str(e))

    # ── Git tools ─────────────────────────────────────────────────────────────
    def git_init(self) -> dict:
        return self._run(["git", "init"], cwd=str(self.workspace))

    def git_status(self) -> dict:
        return self._run(["git", "status", "--short"], cwd=str(self.workspace))

    def git_commit(self, message: str = "Agent auto-commit") -> dict:
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", message]):
            r = self._run(cmd, cwd=str(self.workspace))
            if not r["success"] and "nothing to commit" not in r["output"] + r["error"]:
                return self._err(f"git failed: {r['error']}")
        return self._ok("Committed")

    def git_push(self, remote: str = "origin", branch: str = "main") -> dict:
        return self._run(["git", "push", remote, branch], cwd=str(self.workspace))

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _res(self, path: str) -> Path:
        """Resolve a relative path against the workspace root.

        Guards against the common LLM mistake of passing 'workspace/app.py'
        when the workspace is already rooted at ./workspace/, which would
        produce the double-prefix path workspace/workspace/app.py.
        """
        p = Path(path)
        if p.is_absolute():
            logger.debug(f"[_res] absolute: {p}")
            return p
        # Strip ONE leading segment that exactly matches the workspace folder name
        # so both 'app.py' and 'workspace/app.py' resolve to workspace/app.py.
        ws_name = self.workspace.resolve().name  # e.g. 'workspace'
        if p.parts and p.parts[0] == ws_name:
            p = Path(*p.parts[1:]) if len(p.parts) > 1 else Path(".")
        resolved = (self.workspace / p).resolve()
        logger.debug(f"[_res] {path!r} → {resolved}")
        return resolved

    def _run(self, cmd, cwd=None, timeout=30) -> dict:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               cwd=cwd or str(self.workspace), timeout=timeout)
            out = (r.stdout or "").strip()
            err = (r.stderr or "").strip()
            if r.returncode == 0:
                return self._ok(out or "(no output)")
            return {"success": False, "output": out, "error": err or out}
        except subprocess.TimeoutExpired:
            return self._err(f"Timeout after {timeout}s")
        except Exception as e:
            return self._err(str(e))

    def _parse_command(self, command: str) -> list[str]:
        try:
            return shlex.split(command, posix=False)
        except ValueError:
            return command.split()

    def _prepare_command(self, command: str | list[str]) -> list[str]:
        argv = command if isinstance(command, list) else self._parse_command(command)
        if not argv:
            raise ValueError("Empty command")

        head = argv[0].lower()
        if head in ("python", "python3", "py"):
            argv[0] = sys.executable
        elif head == "pip":
            argv = [sys.executable, "-m", "pip", *argv[1:]]
        return argv

    def _ok(self, o):  return {"success": True,  "output": o,  "error": ""}
    def _err(self, e): return {"success": False, "output": "", "error": e}

    @staticmethod
    def schema() -> str:
        return """
═══════════════════════════════════════════
AVAILABLE TOOLS
═══════════════════════════════════════════
📁 FILE
  read_file(path)
  write_file(path, content)
    append_file(path, content)
  list_files(subdir?)
  delete_file(path)
  diff_edit(path, edits=[{"search":..,"replace":..}])
  search_replace(path, search, replace)

🐍 CODE EXECUTION
  run_python(code?, path?, timeout?)
  run_shell(command, timeout?)

🌐 SERVER
  server_start(command, port, name?, wait_seconds?)
  server_stop(name?)
  server_test(url, method?, expect_status?, data?)

🔀 GIT
  git_init() | git_status() | git_commit(message?) | git_push(remote?, branch?)

🌍 BROWSER
  browser_navigate(url) | browser_click(selector) | browser_fill(selector, value)
  browser_fill_form(fields={selector:value}) | browser_press(selector, key)
  browser_wait_for(selector) | browser_get_text(selector?) | browser_get_html(selector?)
  browser_evaluate_js(script) | browser_get_errors() | browser_screenshot(filename?)
  browser_start() | browser_stop()
═══════════════════════════════════════════
"""