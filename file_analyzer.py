"""
file_analyzer.py — Phase 42: File and Project Analysis
======================================================
Handles large file parsing and project completeness checks.
"""
import os
import hashlib
import logging
from pathlib import Path
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class FileAnalyzer:
    def __init__(self, workspace_dir: str = "./workspace"):
        self.workspace_dir = Path(workspace_dir)

    def analyze_large_file(self, file_path: str, chunk_size: int = 1024 * 1024) -> Dict[str, Any]:
        """
        Analyzes a large file (10MB - 100MB) using streaming to be memory-safe.
        Calculates basic stats without loading the whole file into RAM.
        """
        path = Path(file_path)
        if not path.is_absolute():
            path = self.workspace_dir / path
            
        if not path.exists() or not path.is_file():
            return {"error": f"File not found: {file_path}"}
            
        size_bytes = path.stat().st_size
        if size_bytes > 100 * 1024 * 1024:
            return {"error": f"File too large (>100MB): {size_bytes / (1024*1024):.1f}MB"}
            
        # Streaming analysis
        line_count = 0
        word_count = 0
        md5_hash = hashlib.md5()
        
        try:
            with open(path, 'rb') as f:
                while chunk := f.read(chunk_size):
                    md5_hash.update(chunk)
                    
                    # Basic text counting if it looks like text
                    # (this is a simple heuristic, real implementation might be more robust)
                    try:
                        text_chunk = chunk.decode('utf-8', errors='ignore')
                        line_count += text_chunk.count('\n')
                        word_count += len(text_chunk.split())
                    except Exception:
                        pass # Ignore if not text
                        
            return {
                "file": str(path.name),
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 * 1024), 2),
                "md5": md5_hash.hexdigest(),
                "lines": line_count if line_count > 0 else "N/A (binary)",
                "words": word_count if word_count > 0 else "N/A (binary)"
            }
        except Exception as e:
            return {"error": f"Failed to analyze file: {e}"}

    def detect_incomplete_project(self, project_dir: str) -> Dict[str, Any]:
        """Detects missing components and suggests next steps for a project directory."""
        path = Path(project_dir)
        if not path.is_absolute():
            path = self.workspace_dir / path
            
        if not path.exists() or not path.is_dir():
            return {"error": f"Directory not found: {project_dir}"}
            
        files = [f.name for f in path.iterdir() if f.is_file()]
        
        project_type = "unknown"
        missing = []
        suggestions = []
        
        if "package.json" in files:
            project_type = "node"
            if "node_modules" not in [d.name for d in path.iterdir() if d.is_dir()]:
                missing.append("node_modules (run npm install)")
            if ".env.example" not in files and ".env" not in files:
                missing.append(".env (environment variables)")
                
        elif "requirements.txt" in files or "pyproject.toml" in files:
            project_type = "python"
            if "venv" not in [d.name for d in path.iterdir() if d.is_dir()] and ".venv" not in [d.name for d in path.iterdir() if d.is_dir()]:
                missing.append("Virtual environment (.venv)")
            if "main.py" not in files and "app.py" not in files:
                missing.append("Entry point (main.py or app.py)")
                
        elif "index.html" in files:
            project_type = "static_web"
            if "style.css" not in files and "styles.css" not in files:
                missing.append("CSS Stylesheet")
            if "script.js" not in files and "main.js" not in files:
                missing.append("JavaScript logic")
                
        if "README.md" not in files:
            missing.append("README.md")
            
        if missing:
            suggestions.append(f"Auto-completion: The AI can generate the missing components: {', '.join(missing)}.")
            suggestions.append("Do you want me to complete this project?")
            
        return {
            "type": project_type,
            "missing_components": missing,
            "suggestions": suggestions,
            "is_complete": len(missing) == 0
        }

# Singleton
_analyzer_instance = None
def get_file_analyzer() -> FileAnalyzer:
    global _analyzer_instance
    if _analyzer_instance is None:
        _analyzer_instance = FileAnalyzer()
    return _analyzer_instance
