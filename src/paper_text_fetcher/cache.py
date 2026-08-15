"""
On-disk cache of retrieved paper text, one JSON file per DOI.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from .validation import is_full_text


def cache_filename(doi: str) -> str:
    """
    Map a DOI to a filesystem-safe filename.

    The DOI is lowercased (DOIs are case-insensitive per the spec, so this
    keeps '10.1038/ABC' and '10.1038/abc' from getting separate entries) and
    percent-encoded, which is reversible and therefore collision-free. The
    older scheme replaced '/', ':', and '\\' with '_', which mapped distinct
    DOIs like '10.1/a_b' and '10.1/a/b' onto the same file.
    """
    return f"{quote(doi.lower(), safe='')}.json"


def legacy_cache_filename(doi: str) -> str:
    """The pre-0.2 filename scheme, kept so existing caches remain readable."""
    safe_doi = doi.replace('/', '_').replace(':', '_').replace('\\', '_')
    return f"{safe_doi}.json"


class TextCache:
    """
    JSON-per-DOI cache of paper text.

    Entries record whether the stored text is an article body. Entries written
    before that flag existed are re-judged from their content on read rather
    than trusted by source name, because the publisher-HTML and PDF paths
    previously stored landing pages and abstracts as though they were bodies.

    Entries that do not hold an article body expire after `metadata_ttl_days`,
    so a paper that was closed access when first fetched is retried once the
    entry ages out (papers do become open access later). Full-text entries
    never expire. Pass `metadata_ttl_days=None` to disable expiry.
    """

    def __init__(
        self,
        cache_dir: Path,
        enabled: bool = True,
        metadata_ttl_days: float | None = 7.0,
    ):
        self.cache_dir = Path(cache_dir)
        self.enabled = enabled
        self.metadata_ttl_days = metadata_ttl_days
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, doi: str) -> Path:
        return self.cache_dir / cache_filename(doi)

    def _existing_path_for(self, doi: str) -> Optional[Path]:
        """Find the entry for a DOI, checking the legacy filename scheme too."""
        path = self.path_for(doi)
        if path.exists():
            return path
        legacy = self.cache_dir / legacy_cache_filename(doi)
        if legacy.exists():
            return legacy
        return None

    def _metadata_entry_is_expired(self, data: dict) -> bool:
        if self.metadata_ttl_days is None:
            return False
        cached_at = data.get('cached_at')
        if not cached_at:
            # No timestamp means a pre-TTL entry; retry rather than serve a
            # metadata-only result of unknown age forever.
            return True
        try:
            written = datetime.fromisoformat(cached_at)
        except ValueError:
            return True
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - written
        return age.total_seconds() > self.metadata_ttl_days * 86400

    def get(self, doi: str) -> Optional[tuple[str, str, bool]]:
        """
        Return (text, source, has_full_text) for a cached DOI, or None.

        Returns None both when there is no entry and when the entry is
        unreadable, so a corrupt file simply causes a refetch. Metadata-only
        entries past their TTL are also treated as misses.
        """
        if not self.enabled:
            return None

        cache_path = self._existing_path_for(doi)
        if cache_path is None:
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

        if not has_full_text and self._metadata_entry_is_expired(data):
            return None

        return text, source, bool(has_full_text)

    def put(self, doi: str, text: str, source: str, has_full_text: bool) -> bool:
        """Store text for a DOI. Returns whether the write succeeded."""
        if not self.enabled:
            return False

        # Write to a temp file and rename so an interrupted write cannot leave
        # a truncated entry, and concurrent writers cannot interleave.
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self.cache_dir, prefix='.tmp-', suffix='.json'
            )
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump({
                        'doi': doi,
                        'text': text,
                        'source': source,
                        'has_full_text': has_full_text,
                        'cached_at': datetime.now(timezone.utc).isoformat(),
                    }, f)
                os.replace(tmp_path, self.path_for(doi))
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
            # Retire any entry written under the pre-0.2 filename. Without this
            # a refetch leaves both copies on disk: reads would still be correct,
            # since path_for is checked first, but the stale text stays behind
            # for anything that rebuilds the old filename itself.
            legacy = self.cache_dir / legacy_cache_filename(doi)
            if legacy != self.path_for(doi):
                legacy.unlink(missing_ok=True)
            return True
        except OSError:
            return False
