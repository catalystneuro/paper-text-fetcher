"""
Tests for the on-disk cache, including how it treats entries written before the
full-text flag existed.
"""

import json

from paper_text_fetcher import TextCache, cache_filename


def test_cache_filename_is_filesystem_safe():
    assert cache_filename('10.1038/nn.4497') == '10.1038_nn.4497.json'


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


def test_legacy_entry_is_rejudged_from_content(tmp_path):
    """
    A legacy entry claiming publisher_html but holding a landing page must not
    be reported as full text just because of its source name.
    """
    cache = TextCache(tmp_path)
    cache.path_for('10.1/legacy').write_text(json.dumps({
        'doi': '10.1/legacy',
        'text': 'Access through your institution. Abstract only.',
        'source': 'crossref+publisher_html',
        # no has_full_text key, as written by older versions
    }))
    text, source, has_full_text = cache.get('10.1/legacy')
    assert source == 'crossref+publisher_html'
    assert has_full_text is False


def test_stored_flag_is_trusted_when_present(tmp_path):
    cache = TextCache(tmp_path)
    cache.put('10.1/x', 'short', 'europe_pmc', False)
    assert cache.get('10.1/x')[2] is False
