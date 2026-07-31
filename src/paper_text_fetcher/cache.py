"""
On-disk cache of retrieved paper text, one JSON file per DOI.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .validation import is_full_text


def cache_filename(doi: str) -> str:
    """Map a DOI to a filesystem-safe filename."""
    safe_doi = doi.replace('/', '_').replace(':', '_').replace('\\', '_')
    return f"{safe_doi}.json"


class TextCache:
    """
    JSON-per-DOI cache of paper text.

    Entries record whether the stored text is an article body. Entries written
    before that flag existed are re-judged from their content on read rather
    than trusted by source name, because the publisher-HTML and PDF paths
    previously stored landing pages and abstracts as though they were bodies.
    """

    def __init__(self, cache_dir: Path, enabled: bool = True):
        self.cache_dir = Path(cache_dir)
        self.enabled = enabled
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, doi: str) -> Path:
        return self.cache_dir / cache_filename(doi)

    def get(self, doi: str) -> Optional[tuple[str, str, bool]]:
        """
        Return (text, source, has_full_text) for a cached DOI, or None.

        Returns None both when there is no entry and when the entry is
        unreadable, so a corrupt file simply causes a refetch.
        """
        if not self.enabled:
            return None

        cache_path = self.path_for(doi)
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

        source = data.get('source', '')
        text = data.get('text')
        has_full_text = data.get('has_full_text')
        if has_full_text is None:
            has_full_text = is_full_text(text, source)
        return text, source, bool(has_full_text)

    def put(self, doi: str, text: str, source: str, has_full_text: bool) -> bool:
        """Store text for a DOI. Returns whether the write succeeded."""
        if not self.enabled:
            return False

        try:
            with open(self.path_for(doi), 'w') as f:
                json.dump({
                    'doi': doi,
                    'text': text,
                    'source': source,
                    'has_full_text': has_full_text,
                    'cached_at': datetime.now(timezone.utc).isoformat(),
                }, f)
            return True
        except OSError:
            return False
