"""
Rules for deciding whether retrieved text is an article body.

The distinction matters because most downstream uses of a paper's text depend
on content that appears only in the body. An abstract, a title, and a reference
list can be retrieved for almost any DOI, so a fetcher that reports success
whenever it got *something* will silently supply metadata where a body was
required.

A result counts as full text only on structural evidence gathered at fetch
time — a JATS <body> element, a publisher page's article-body node — recorded
as the body-evidence values below. The text checks here are sanity gates on
top of that, not the verdict itself.

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

# What a fetched part can prove about containing the article body. 'body' means
# structural evidence: the source's own document structure marked the text as
# the article body (a JATS <body> element, a publisher page's article-body
# node, an article PDF). 'unverified' means substantial text was retrieved but
# nothing structural vouches for it, so it may be a landing page or front
# matter. Metadata parts (CrossRef) carry no evidence at all.
BODY_EVIDENCE_CONFIRMED = 'body'
BODY_EVIDENCE_UNVERIFIED = 'unverified'

# Version of the validation rules. Stored in every cache entry; entries written
# under an older version were judged by rules since found unreliable, so their
# full-text claims are demoted to 'unknown' on read.
VALIDATION_VERSION = 2

# Minimum length for text to plausibly be a body rather than an abstract plus
# navigation boilerplate. A bare abstract runs 1-2K characters; a landing page
# with navigation, a cookie banner, and a reference list can reach 5K with no
# body at all.
MIN_FULL_TEXT_CHARS = 6000

# Minimum characters a structural body container must hold to count as a body.
# Both the JATS <body> check and the PMC page body-node check use this floor
# to reject stub bodies.
MIN_STRUCTURAL_BODY_CHARS = 500

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
    # Cloudflare's bot-check phrasing. The broader 'enable javascript' also
    # matches the <noscript> banner that React-based publisher sites put at the
    # top of genuine full-text pages, so it must not be matched on its own.
    'enable javascript and cookies',
    'are you a robot',
    'unusual traffic',
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
    Report whether text is a paywall interstitial or bot check.

    Deciding whether text is an article body is not done here: that question is
    answered structurally at fetch time (a JATS <body> element, a publisher
    page's article-body node) and reported as body evidence. This check only
    catches pages that are visibly not an article at all.
    """
    if not text:
        return True
    head = text.lower()[:4000]
    return any(marker in head for marker in PAYWALL_MARKERS)


def is_full_text(text: str | None, source: str | None) -> bool:
    """
    Report whether a retrieved blob is plausibly an article body.

    A sanity gate, not proof: a source capable of delivering a body, enough
    text to be more than an abstract, and no paywall interstitial at the top.
    Structural body evidence, gathered at fetch time, is what upgrades a
    result to full text.
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
    return len(body.get_text(strip=True)) > MIN_STRUCTURAL_BODY_CHARS
