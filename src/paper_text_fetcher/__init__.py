"""
paper-text-fetcher: retrieve the full text of a scholarly article from its DOI.

The central idea is that "we got some text" and "we got the article body" are
different outcomes, and conflating them corrupts anything built on top. Every
result reports which one it is:

    from paper_text_fetcher import PaperFetcher

    fetcher = PaperFetcher(cache_dir='.paper_cache', contact_email='you@example.org')
    result = fetcher.get_paper_text_detailed('10.1038/s41586-023-06031-6')

    if result['status'] == 'full_text':
        process(result['text'])
    else:
        skip(result['reason'])
"""

from .fetcher import (
    DEFAULT_USER_AGENT,
    PLAYWRIGHT_AVAILABLE,
    PYMUPDF_AVAILABLE,
    PaperFetcher,
    extract_pmc_article_body,
    extract_publisher_article,
    format_crossref_reference,
    resolve_fetch_result,
)
from .cache import TextCache, cache_filename, legacy_cache_filename
from .validation import (
    BODY_EVIDENCE_CONFIRMED,
    BODY_EVIDENCE_UNVERIFIED,
    FULL_TEXT_SOURCES,
    METADATA_SOURCES,
    MIN_FULL_TEXT_CHARS,
    MIN_STRUCTURAL_BODY_CHARS,
    VALIDATION_VERSION,
    has_full_text_source,
    is_full_text,
    looks_like_paywall_or_landing_page,
    xml_has_body,
)

__version__ = '0.1.0'

__all__ = [
    'PaperFetcher',
    'TextCache',
    'cache_filename',
    'legacy_cache_filename',
    'format_crossref_reference',
    'extract_pmc_article_body',
    'extract_publisher_article',
    'resolve_fetch_result',
    'is_full_text',
    'has_full_text_source',
    'looks_like_paywall_or_landing_page',
    'xml_has_body',
    'BODY_EVIDENCE_CONFIRMED',
    'BODY_EVIDENCE_UNVERIFIED',
    'FULL_TEXT_SOURCES',
    'METADATA_SOURCES',
    'MIN_FULL_TEXT_CHARS',
    'MIN_STRUCTURAL_BODY_CHARS',
    'VALIDATION_VERSION',
    'DEFAULT_USER_AGENT',
    'PLAYWRIGHT_AVAILABLE',
    'PYMUPDF_AVAILABLE',
    '__version__',
]
