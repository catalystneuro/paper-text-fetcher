"""
Tests for the full-text detection rules.

These are the rules that decide whether a fetch succeeded, so they carry most
of the library's risk. They are pure functions, so none of this touches the
network.
"""

import pytest
from bs4 import BeautifulSoup

from paper_text_fetcher import (
    MIN_FULL_TEXT_CHARS,
    has_full_text_source,
    is_full_text,
    looks_like_paywall_or_landing_page,
    xml_has_body,
)


def make_body(n_chars: int = MIN_FULL_TEXT_CHARS + 1000) -> str:
    """Build text that reads like an article body and is long enough to pass."""
    body = (
        "Introduction. We investigated the question. "
        "Methods. We collected measurements and analysed them. "
        "Results. The effect was present. "
        "Discussion. The effect is consistent with prior work. "
        "Data availability. Available on request. "
    )
    return (body * (n_chars // len(body) + 1))[:n_chars]


class TestHasFullTextSource:
    def test_metadata_source_alone_is_not_full_text(self):
        assert has_full_text_source('crossref') is False

    def test_body_source_combined_with_metadata_counts(self):
        assert has_full_text_source('europe_pmc+crossref') is True

    def test_empty_and_none_are_false(self):
        assert has_full_text_source('') is False
        assert has_full_text_source(None) is False

    def test_unknown_source_is_not_assumed_to_have_body(self):
        assert has_full_text_source('some_new_source') is False


class TestLooksLikePaywallOrLandingPage:
    def test_body_text_is_accepted(self):
        assert looks_like_paywall_or_landing_page(make_body()) is False

    def test_empty_text_is_rejected(self):
        assert looks_like_paywall_or_landing_page('') is True
        assert looks_like_paywall_or_landing_page(None) is True

    @pytest.mark.parametrize('marker', [
        'Access through your institution',
        'Purchase access',
        'Checking your browser',
        'Please enable JavaScript and cookies to continue',
        'Are you a robot',
    ])
    def test_paywall_and_bot_check_markers_are_rejected(self, marker):
        # The marker appears at the top, as it does on a real interstitial,
        # with body-like text after it to prove the marker is what rejects it.
        text = marker + ' ' + make_body()
        assert looks_like_paywall_or_landing_page(text) is True

    def test_noscript_javascript_banner_does_not_reject_a_body(self):
        # React-based publisher sites put this banner in a <noscript> tag at
        # the top of genuine full-text pages. It must not read as a bot check.
        text = 'You need to enable JavaScript to run this app. ' + make_body()
        assert looks_like_paywall_or_landing_page(text) is False


class TestIsFullText:
    def test_accepts_long_body_from_body_source(self):
        assert is_full_text(make_body(), 'europe_pmc') is True

    def test_rejects_body_text_from_metadata_only_source(self):
        # The regression that motivated this library: a long CrossRef record is
        # still not an article body, however many characters it runs to.
        assert is_full_text(make_body(), 'crossref') is False

    def test_rejects_short_text_even_from_body_source(self):
        assert is_full_text('Methods and results.', 'publisher_html') is False

    def test_rejects_landing_page_from_body_source(self):
        landing = 'Buy this article ' + make_body()
        assert is_full_text(landing, 'publisher_html') is False

    def test_rejects_empty_text(self):
        assert is_full_text(None, 'europe_pmc') is False
        assert is_full_text('', 'europe_pmc') is False

    def test_boundary_at_minimum_length(self):
        just_under = make_body(MIN_FULL_TEXT_CHARS - 1)
        just_over = make_body(MIN_FULL_TEXT_CHARS)
        assert is_full_text(just_under, 'europe_pmc') is False
        assert is_full_text(just_over, 'europe_pmc') is True


class TestXmlHasBody:
    def test_record_with_substantive_body(self):
        paragraph = 'We recorded from neurons and analysed the spiking. ' * 20
        xml = (
            '<article><front><abstract>Short.</abstract></front>'
            f'<body><p>{paragraph}</p></body></article>'
        )
        assert xml_has_body(BeautifulSoup(xml, 'lxml-xml')) is True

    def test_abstract_only_record_has_no_body(self):
        xml = (
            '<article><front><article-title>Title</article-title>'
            '<abstract>An abstract, however detailed, is not a body.</abstract>'
            '</front></article>'
        )
        assert xml_has_body(BeautifulSoup(xml, 'lxml-xml')) is False

    def test_stub_body_is_rejected(self):
        xml = '<article><body><p>Too short to be an article body.</p></body></article>'
        assert xml_has_body(BeautifulSoup(xml, 'lxml-xml')) is False
