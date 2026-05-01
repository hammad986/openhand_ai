"""
asset_pipeline.py — Phase 42: Asset Automation Pipeline
=========================================================
Automatically fetches, validates, and manages assets for projects.
- Web search for relevant images/icons
- Asset downloading and local caching
- Validation of format and size
- Integration points for HTML generators
"""
import os
import requests
import hashlib
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

class AssetPipeline:
    def __init__(self, workspace_dir: str = "./workspace"):
        self.workspace_dir = Path(workspace_dir)
        self.assets_dir = self.workspace_dir / "assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.registry_file = self.assets_dir / "registry.json"
        self._load_registry()

    def _load_registry(self):
        if self.registry_file.exists():
            try:
                with open(self.registry_file, 'r', encoding='utf-8') as f:
                    self.registry = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load asset registry: {e}")
                self.registry = {}
        else:
            self.registry = {}

    def _save_registry(self):
        with open(self.registry_file, 'w', encoding='utf-8') as f:
            json.dump(self.registry, f, indent=2)

    def _is_valid_image(self, content_type: str, size: int) -> bool:
        valid_types = ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'image/svg+xml']
        if content_type not in valid_types:
            return False
        # Prevent downloading massive files (limit 5MB)
        if size > 5 * 1024 * 1024:
            return False
        return True

    def fetch_image(self, url: str, tag: str, project_id: Optional[str] = None) -> Optional[str]:
        """
        Download an image from a URL, validate it, and store it locally.
        Returns the local relative path if successful, None otherwise.
        """
        try:
            # Generate a unique hash for the URL to use as filename
            url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
            
            # Check if we already have it
            if url_hash in self.registry:
                logger.info(f"[AssetPipeline] Using cached asset for {url}")
                return self.registry[url_hash]['local_path']

            logger.info(f"[AssetPipeline] Fetching asset: {url}")
            response = requests.get(url, stream=True, timeout=10)
            response.raise_for_status()

            content_type = response.headers.get('content-type', '')
            size = int(response.headers.get('content-length', 0))

            if not self._is_valid_image(content_type, size):
                logger.warning(f"[AssetPipeline] Invalid image type ({content_type}) or size ({size})")
                return None

            # Determine extension
            ext = '.jpg'
            if 'png' in content_type: ext = '.png'
            elif 'gif' in content_type: ext = '.gif'
            elif 'webp' in content_type: ext = '.webp'
            elif 'svg' in content_type: ext = '.svg'

            filename = f"{url_hash}{ext}"
            
            # If project_id provided, organize in subfolder
            if project_id:
                proj_dir = self.assets_dir / project_id
                proj_dir.mkdir(exist_ok=True)
                local_path = proj_dir / filename
                rel_path = f"assets/{project_id}/{filename}"
            else:
                local_path = self.assets_dir / filename
                rel_path = f"assets/{filename}"

            with open(local_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Register
            self.registry[url_hash] = {
                "original_url": url,
                "local_path": rel_path,
                "tag": tag,
                "project_id": project_id,
                "content_type": content_type
            }
            self._save_registry()
            
            logger.info(f"[AssetPipeline] Asset saved to {rel_path}")
            return rel_path

        except Exception as e:
            logger.error(f"[AssetPipeline] Failed to fetch image {url}: {e}")
            return None

    def search_and_fetch_images(self, query: str, tag: str, count: int = 1, project_id: Optional[str] = None) -> List[str]:
        """
        Uses an external search API (e.g. Unsplash, if configured, or a fallback free service)
        to find relevant images and download them.
        """
        logger.info(f"[AssetPipeline] Searching images for: '{query}'")
        
        # In a real production system, you'd integrate Unsplash/Pexels API here.
        # For this implementation, we'll use a reliable free placeholder service
        # that actually returns random relevant images based on seed keywords.
        
        encoded_query = urllib.parse.quote(query.replace(' ', ','))
        paths = []
        
        # We append an index to the query to bypass caching on the placeholder service
        for i in range(count):
            # Using Unsplash Source API (deprecated but still works for basic needs)
            # or alternative placeholder like picsum.
            url = f"https://source.unsplash.com/featured/?{encoded_query}&sig={i}"
            
            # Since source.unsplash redirects, we need to resolve it first
            try:
                res = requests.head(url, allow_redirects=True, timeout=5)
                actual_url = res.url
                
                path = self.fetch_image(actual_url, tag, project_id)
                if path:
                    paths.append(path)
            except Exception as e:
                logger.warning(f"[AssetPipeline] Search fetch failed: {e}")
                
        return paths

    def get_assets_for_project(self, project_id: str) -> List[Dict]:
        """Retrieve all registered assets for a specific project."""
        return [asset for asset in self.registry.values() if asset.get('project_id') == project_id]

    def clear_cache(self):
        """Remove all tracked assets (useful for cleanup)."""
        import shutil
        if self.assets_dir.exists():
            for item in self.assets_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                elif item.name != "registry.json":
                    item.unlink()
        self.registry = {}
        self._save_registry()

# Singleton
_pipeline_instance = None
def get_asset_pipeline() -> AssetPipeline:
    global _pipeline_instance
    if _pipeline_instance is None:
        _pipeline_instance = AssetPipeline()
    return _pipeline_instance
