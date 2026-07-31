"""
Decide whether retrieved text is an article body or merely metadata.

The distinction matters because most downstream uses of a paper's text depend
on content that appears only in the body. An abstract, a title, and a reference
list can be retrieved for almost any DOI, so a fetcher that reports success
whenever it got *something* will silently supply metadata where a body was
required.

These functions are pure and network-free so they can be tested directly.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

# Sources that can deliver the body of a paper. Anything else carries at most a
# title, an abstract, and a reference list.
FULL_TEXT_SOURCES = frozenset({
    'europe_pmc',
    'ncbi_pmc',
    'pmc_playwright',
    'playwright_biorxiv',
    'elsevier',
    'unpaywall',
    'publisher_html',
})

# Sources that provide metadata only, recorded so callers can tell "we looked
# and there is no body" apart from "we never looked".
METADATA_SOURCES = frozenset({'crossref'})

# Minimum length for text to plausibly be a body rather than an abstract plus
# navigation boilerplate. A bare abstract runs 1-2K characters; a landing page
# with navigation, a cookie banner, and a reference list can reach 5K with no
# body at all.
MIN_FULL_TEXT_CHARS = 6000

# Phrases identifying a paywall interstitial or bot check rather than an
# article. Matched case-insensitively against the start of the text.
PAYWALL_MARKERS = (
    'access through your institution',
    'buy this article',
    'purchase access',
    'subscribe to journal',
    'get full access to this article',
    'rent this article',
    'sign in to read',
    'this content is only available',
    'checking your browser',
    'enable javascript',
    'are you a robot',
    'unusual traffic',
)

# Section headings that essentially every research article contains somewhere
# in its body, and that abstracts and landing pages do not.
BODY_MARKERS = (
    'method',
    'results',
    'discussion',
    'materials and',
    'data availability',
    'acknowledg',
)


def has_full_text_source(source: str | None) -> bool:
    """
    Report whether any contributing source in `source` can deliver a body.

    `source` is the '+'-joined list of sources that contributed to a result,
    for example 'europe_pmc+crossref'.
    """
    if not source:
        return False
    return any(part in FULL_TEXT_SOURCES for part in source.split('+'))


def looks_like_paywall_or_landing_page(text: str | None) -> bool:
    """
    Report whether text is a paywall interstitial, bot check, or landing page.

    Publisher pages for closed-access articles still return HTTP 200 with a few
    thousand characters of navigation, abstract, and references, which is why a
    length threshold alone is not enough to identify a body.
    """
    if not text:
        return True
    head = text[:4000].lower()
    if any(marker in head for marker in PAYWALL_MARKERS):
        return True
    return not any(marker in text.lower() for marker in BODY_MARKERS)


def is_full_text(text: str | None, source: str | None) -> bool:
    """
    Report whether a retrieved blob really is an article body.

    Both conditions must hold: a source capable of delivering a body claimed to
    have done so, and the text reads like a body. The source name alone is not
    sufficient, because the publisher-HTML and PDF paths accept whatever the
    server returns and servers return landing pages for articles they will not
    give you.
    """
    if not text or not has_full_text_source(source):
        return False
    return (
        len(text) >= MIN_FULL_TEXT_CHARS
        and not looks_like_paywall_or_landing_page(text)
    )


def xml_has_body(soup: BeautifulSoup) -> bool:
    """
    Report whether a parsed JATS record carries the article body.

    Europe PMC and NCBI both return a record for articles that are indexed but
    not open, containing only <front> (title, authors, abstract). The presence
    of a substantive <body> is what distinguishes a real full-text record.
    """
    body = soup.find('body')
    if body is None:
        return False
    return len(body.get_text(strip=True)) > 500
