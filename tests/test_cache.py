"""
Tests for the on-disk cache, including how it treats entries written before the
full-text flag existed and entries written under the legacy filename scheme.
"""

import json
from datetime import datetime, timedelta, timezone

from paper_text_fetcher import TextCache, cache_filename, legacy_cache_filename


BODY_TEXT = (
    'Introduction. Methods. Results. Discussion. Data availability. '
    * 200
)


class TestCacheFilename:
    def test_is_filesystem_safe(self):
        assert '/' not in cache_filename('10.1038/nn.4497')
        assert cache_filename('10.1038/nn.4497') == '10.1038%2Fnn.4497.json'

    def test_distinct_dois_get_distinct_files(self):
        # The legacy scheme mapped both of these to '10.1_a_b.json'
        assert cache_filename('10.1/a_b') != cache_filename('10.1/a/b')

    def test_doi_case_is_normalized(self):
        # DOIs are case-insensitive, so casing must not split the entry
        assert cache_filename('10.1038/ABC') == cache_filename('10.1038/abc')


def test_roundtrip(tmp_path):
    cache = TextCache(tmp_path)
    assert cache.put('10.1/x', 'body text', 'europe_pmc', True) is True
    assert cache.get('10.1/x') == ('body text', 'europe_pmc', True)


def test_missing_entry_returns_none(tmp_path):
    assert TextCache(tmp_path).get('10.1/absent') is None


def test_disabled_cache_neither_reads_nor_writes(tmp_path):
    cache = TextCache(tmp_path, enabled=False)
    assert cache.put('10.1/x', 'body', 'europe_pmc', True) is False
    assert cache.get('10.1/x') is None


def test_corrupt_entry_is_treated_as_a_miss(tmp_path):
    cache = TextCache(tmp_path)
    cache.path_for('10.1/bad').write_text('{not valid json')
    assert cache.get('10.1/bad') is None


def test_entry_under_legacy_filename_is_found(tmp_path):
    """Caches written by older versions must keep working after the scheme change."""
    cache = TextCache(tmp_path)
    (tmp_path / legacy_cache_filename('10.1038/nn.4497')).write_text(json.dumps({
        'doi': '10.1038/nn.4497',
        'text': BODY_TEXT,
        'source': 'europe_pmc',
        'has_full_text': True,
        'cached_at': datetime.now(timezone.utc).isoformat(),
    }))
    assert cache.get('10.1038/nn.4497') == (BODY_TEXT, 'europe_pmc', True)


def test_legacy_entry_with_body_is_rejudged_from_content(tmp_path):
    """
    An entry with no has_full_text flag whose text reads like a body is
    rejudged as full text and served.
    """
    cache = TextCache(tmp_path)
    cache.path_for('10.1/legacy').write_text(json.dumps({
        'doi': '10.1/legacy',
        'text': BODY_TEXT,
        'source': 'publisher_html',
        # no has_full_text key, as written by older versions
    }))
    text, source, has_full_text = cache.get('10.1/legacy')
    assert has_full_text is True


def test_legacy_landing_page_entry_is_a_miss(tmp_path):
    """
    A legacy entry claiming publisher_html but holding a landing page must not
    be served as full text. It is rejudged as metadata-only and, since its age
    is unknown, treated as expired so the paper is refetched.
    """
    cache = TextCache(tmp_path)
    cache.path_for('10.1/legacy').write_text(json.dumps({
        'doi': '10.1/legacy',
        'text': 'Access through your institution. Abstract only.',
        'source': 'crossref+publisher_html',
    }))
    assert cache.get('10.1/legacy') is None


def test_stored_flag_is_trusted_when_present(tmp_path):
    cache = TextCache(tmp_path)
    cache.put('10.1/x', 'short', 'europe_pmc', False)
    assert cache.get('10.1/x')[2] is False


class TestMetadataTtl:
    def test_fresh_metadata_entry_is_served(self, tmp_path):
        cache = TextCache(tmp_path, metadata_ttl_days=7.0)
        cache.put('10.1/meta', 'title and refs', 'crossref', False)
        assert cache.get('10.1/meta') == ('title and refs', 'crossref', False)

    def test_expired_metadata_entry_is_a_miss(self, tmp_path):
        cache = TextCache(tmp_path, metadata_ttl_days=7.0)
        stale = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        cache.path_for('10.1/meta').write_text(json.dumps({
            'doi': '10.1/meta',
            'text': 'title and refs',
            'source': 'crossref',
            'has_full_text': False,
            'cached_at': stale,
        }))
        assert cache.get('10.1/meta') is None

    def test_full_text_entries_never_expire(self, tmp_path):
        cache = TextCache(tmp_path, metadata_ttl_days=7.0)
        old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        cache.path_for('10.1/full').write_text(json.dumps({
            'doi': '10.1/full',
            'text': BODY_TEXT,
            'source': 'europe_pmc',
            'has_full_text': True,
            'cached_at': old,
        }))
        assert cache.get('10.1/full') == (BODY_TEXT, 'europe_pmc', True)

    def test_none_ttl_disables_expiry(self, tmp_path):
        cache = TextCache(tmp_path, metadata_ttl_days=None)
        stale = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
        cache.path_for('10.1/meta').write_text(json.dumps({
            'doi': '10.1/meta',
            'text': 'title and refs',
            'source': 'crossref',
            'has_full_text': False,
            'cached_at': stale,
        }))
        assert cache.get('10.1/meta') == ('title and refs', 'crossref', False)
