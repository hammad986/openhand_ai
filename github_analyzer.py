"""
github_analyzer.py — Phase 42: GitHub Integration (Replit-style import)
========================================================================
Clones, parses, and analyzes GitHub repositories.
- Code quality analysis
- Missing file detection
- Auto-completion suggestions
"""
import os
import subprocess
import logging
import json
import shutil
from pathlib import Path
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class GithubAnalyzer:
    def __init__(self, workspace_dir: str = "./workspace/repos"):
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def clone_repo(self, repo_url: str) -> Optional[str]:
        """Clones a GitHub repository into the workspace. Returns local path."""
        # Sanitize URL
        if not repo_url.startswith("https://github.com/"):
            logger.error(f"[GithubAnalyzer] Invalid GitHub URL: {repo_url}")
            return None
            
        repo_name = repo_url.rstrip("/").split("/")[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[:-4]
            
        local_path = self.workspace_dir / repo_name
        
        # Clean existing if present
        if local_path.exists():
            shutil.rmtree(local_path, ignore_errors=True)
            
        logger.info(f"[GithubAnalyzer] Cloning {repo_url} into {local_path}")
        
        try:
            # Using subprocess to run git clone
            result = subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, str(local_path)],
                capture_output=True, text=True, check=True
            )
            return str(local_path)
        except subprocess.CalledProcessError as e:
            logger.error(f"[GithubAnalyzer] Git clone failed: {e.stderr}")
            return None
        except Exception as e:
            logger.error(f"[GithubAnalyzer] Failed to clone repo: {e}")
            return None

    def analyze_structure(self, local_path: str) -> Dict[str, Any]:
        """Parses directory structure and identifies project type."""
        path = Path(local_path)
        if not path.exists():
            return {"error": "Path does not exist"}

        structure = {}
        file_count = 0
        ext_counts = {}
        
        # Walk directory up to a certain depth/limit
        for root, dirs, files in os.walk(local_path):
            # Skip hidden dirs like .git
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            for file in files:
                if file.startswith('.'): continue
                
                file_count += 1
                ext = Path(file).suffix.lower()
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
                
                if file_count > 1000: # Limit analysis to prevent overwhelming
                    break
            
            if file_count > 1000:
                break

        # Determine project type
        project_type = "unknown"
        if "package.json" in [f.name for f in path.iterdir() if f.is_file()]:
            project_type = "node"
        elif "requirements.txt" in [f.name for f in path.iterdir() if f.is_file()] or "pyproject.toml" in [f.name for f in path.iterdir() if f.is_file()]:
            project_type = "python"
        elif "index.html" in [f.name for f in path.iterdir() if f.is_file()] and not ext_counts.get('.py') and not ext_counts.get('.js', 0) > 5:
            project_type = "static_web"
            
        return {
            "type": project_type,
            "total_files": file_count,
            "extensions": ext_counts,
            "path": local_path
        }

    def generate_completion_plan(self, structure: Dict[str, Any]) -> Dict[str, Any]:
        """Identifies missing components and suggests next steps."""
        ptype = structure.get("type", "unknown")
        missing = []
        suggestions = []
        
        # We can simulate the LLM call or use hardcoded rules for common patterns
        if ptype == "python":
            missing.append("Dockerfile (for deployment)")
            if not structure.get("extensions", {}).get(".md"):
                missing.append("README.md")
            suggestions.append("Set up a virtual environment and install dependencies.")
            suggestions.append("Run static analysis (flake8/black) to ensure code quality.")
            
        elif ptype == "node":
            missing.append("Dockerfile (for deployment)")
            suggestions.append("Run `npm install` and test the build process.")
            suggestions.append("Check for outdated dependencies in package.json.")
            
        elif ptype == "static_web":
            suggestions.append("Optimize assets (images, minified CSS/JS).")
            suggestions.append("Deploy to a static hosting platform (Vercel/Netlify).")
            
        else:
            suggestions.append("Analyze codebase with LLM to determine intent.")
            
        return {
            "missing_files": missing,
            "suggestions": suggestions,
            "ready_for_completion": len(missing) > 0 or ptype == "unknown"
        }

# Singleton
_github_instance = None
def get_github_analyzer() -> GithubAnalyzer:
    global _github_instance
    if _github_instance is None:
        _github_instance = GithubAnalyzer()
    return _github_instance
