"""
Tests for the pure parts of the fetcher: the DOM extraction helpers and the
composite-result resolution. Nothing here touches the network.
"""

from paper_text_fetcher import (
    BODY_EVIDENCE_CONFIRMED,
    BODY_EVIDENCE_UNVERIFIED,
    extract_pmc_article_body,
    extract_publisher_article,
    resolve_fetch_result,
)


# Long enough to clear the structural body floor (500 chars)
BODY_PARAGRAPH = 'We recorded from neurons and analysed the spiking data. ' * 20


class TestResolveFetchResult:
    def test_confirmed_body_part_gives_full_text(self):
        result = resolve_fetch_result([
            ('the article body', 'europe_pmc', BODY_EVIDENCE_CONFIRMED),
            ('[1] a reference', 'crossref', None),
        ])
        assert result == {
            'text': 'the article body\n\n[1] a reference',
            'source': 'europe_pmc+crossref',
            'from_cache': False,
            'has_full_text': True,
            'status': 'full_text',
            'reason': None,
        }

    def test_unverified_part_gives_unknown_despite_full_text_source_name(self):
        # The regression from issue #3: front matter plus references reached
        # the classifier as full text because the verdict came from source
        # names. Unverified text must surface as 'unknown', never 'full_text'.
        result = resolve_fetch_result([
            ('[1] a reference', 'crossref', None),
            ('front matter and abstract', 'publisher_html', BODY_EVIDENCE_UNVERIFIED),
        ])
        assert result == {
            'text': '[1] a reference\n\nfront matter and abstract',
            'source': 'crossref+publisher_html',
            'from_cache': False,
            'has_full_text': False,
            'status': 'unknown',
            'reason': (
                'Text was retrieved (sources: crossref+publisher_html) but '
                'nothing structural marks it as the article body; it may be '
                'a landing page or front matter.'
            ),
        }

    def test_confirmed_part_outranks_unverified_part(self):
        result = resolve_fetch_result([
            ('maybe a body', 'elsevier', BODY_EVIDENCE_UNVERIFIED),
            ('a verified body', 'unpaywall', BODY_EVIDENCE_CONFIRMED),
        ])
        assert result['status'] == 'full_text'
        assert result['has_full_text'] is True

    def test_metadata_only_part_gives_metadata_only(self):
        result = resolve_fetch_result([
            ('title, abstract, references', 'crossref', None),
        ])
        assert result == {
            'text': 'title, abstract, references',
            'source': 'crossref',
            'from_cache': False,
            'has_full_text': False,
            'status': 'metadata_only',
            'reason': (
                'No source provided the article body (got: crossref). '
                'The paper is likely closed access with no OA copy.'
            ),
        }

    def test_no_parts_gives_unavailable(self):
        assert resolve_fetch_result([]) == {
            'text': None,
            'source': '',
            'from_cache': False,
            'has_full_text': False,
            'status': 'unavailable',
            'reason': 'No source returned any text for this DOI',
        }


class TestExtractPmcArticleBody:
    def test_page_with_article_body_section(self):
        html = (
            '<html><body><nav>PMC site navigation</nav>'
            '<section class="body main-article-body">'
            f'<p>{BODY_PARAGRAPH}</p></section></body></html>'
        )
        assert extract_pmc_article_body(html) == BODY_PARAGRAPH.strip()

    def test_page_without_article_body_section(self):
        html = (
            '<html><body><section class="front-matter">'
            f'<p>{BODY_PARAGRAPH}</p></section></body></html>'
        )
        assert extract_pmc_article_body(html) is None

    def test_stub_body_section_is_rejected(self):
        html = (
            '<html><body><section class="body main-article-body">'
            '<p>Too short.</p></section></body></html>'
        )
        assert extract_pmc_article_body(html) is None


class TestExtractPublisherArticle:
    def test_body_selector_gives_confirmed_evidence(self):
        html = (
            '<html><body><nav>Journal home</nav>'
            f'<div class="c-article-body"><p>{BODY_PARAGRAPH}</p></div>'
            '</body></html>'
        )
        text, evidence = extract_publisher_article(html)
        assert text == BODY_PARAGRAPH.strip()
        assert evidence == BODY_EVIDENCE_CONFIRMED

    def test_generic_container_gives_unverified_evidence(self):
        # A landing page puts its abstract in the same <article> container a
        # full-text page uses, so the container alone proves nothing.
        html = (
            '<html><body><article><h1>Title</h1>'
            '<p>Abstract of a paper we will not show you.</p></article>'
            '</body></html>'
        )
        text, evidence = extract_publisher_article(html)
        assert text == 'Title Abstract of a paper we will not show you.'
        assert evidence == BODY_EVIDENCE_UNVERIFIED

    def test_no_container_falls_back_to_whole_page_unverified(self):
        html = '<html><body><p>Loose text on a bare page.</p></body></html>'
        text, evidence = extract_publisher_article(html)
        assert text == 'Loose text on a bare page.'
        assert evidence == BODY_EVIDENCE_UNVERIFIED

    def test_page_chrome_is_stripped(self):
        html = (
            '<html><body>'
            '<noscript>You need to enable JavaScript to run this app.</noscript>'
            '<script>analytics();</script>'
            '<header>Masthead</header><footer>Imprint</footer>'
            f'<div class="article-content"><p>{BODY_PARAGRAPH}</p></div>'
            '</body></html>'
        )
        text, evidence = extract_publisher_article(html)
        assert text == BODY_PARAGRAPH.strip()
        assert evidence == BODY_EVIDENCE_CONFIRMED
