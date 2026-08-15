"""
Fetch the full text of a scholarly article given its DOI.

`PaperFetcher.get_paper_text_detailed()` tries a chain of sources and reports
not only what it retrieved but whether the result is an article body or only
metadata. See `validation.py` for why that distinction is enforced.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

import requests
from bs4 import BeautifulSoup

from .cache import TextCache
from .validation import (
    MIN_FULL_TEXT_CHARS,
    has_full_text_source,
    is_full_text,
    looks_like_paywall_or_landing_page,
    xml_has_body,
)

# Try to import playwright for browser-rendered sources
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# Try to import PyMuPDF for PDF text extraction
try:
    import fitz
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False


DEFAULT_USER_AGENT = (
    'paper-text-fetcher/0.1 '
    '(https://github.com/bendichter/paper-text-fetcher)'
)

BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

# Minimum seconds between successive requests to the same host. NCBI asks for
# at most 3 requests per second without an API key; the others have no hard
# published limit but are free services worth pacing. Requests to hosts not
# listed here (publisher sites, PDF hosts) are not throttled, since we make at
# most one or two requests to any of them per DOI.
HOST_MIN_INTERVALS = {
    'eutils.ncbi.nlm.nih.gov': 0.34,
    'pmc.ncbi.nlm.nih.gov': 0.34,
    'www.ebi.ac.uk': 0.2,
    'api.crossref.org': 0.2,
    'api.unpaywall.org': 0.2,
}

# DOI prefixes registered to Elsevier and its imprints, used to decide whether
# the ScienceDirect article API is worth asking. 10.1016 is Elsevier proper;
# the others are Saunders, Harcourt, Mosby, and Urban & Fischer, all served by
# the same endpoint.
ELSEVIER_DOI_PREFIXES = ('10.1016/', '10.1053/', '10.1054/', '10.1067/', '10.1078/')


def format_crossref_reference(index: int, ref: dict) -> str:
    """
    Render a single CrossRef reference entry as a numbered text block.

    Always emits `[index] ...`, where the body is the best metadata available
    from the entry: prefer `unstructured` (the publisher's formatted citation
    string), then a synthesized title+journal+year line, then bare DOI. The
    DOI is appended at the end when present. The leading `[index]` keeps the
    entry parseable as a numbered bibliography regardless of what metadata
    follows, so callers can map in-text [N] citations onto reference entries.
    """
    parts = []
    unstructured = ref.get('unstructured')
    if unstructured:
        parts.append(unstructured.strip())
    else:
        title = ref.get('article-title')
        journal = ref.get('journal-title')
        year = ref.get('year')
        synthesized = '. '.join(
            piece for piece in (title, journal, year) if piece
        )
        if synthesized:
            parts.append(synthesized + '.')

    doi = ref.get('DOI')
    if doi:
        parts.append(doi)

    body = ' '.join(parts) if parts else ''
    return f"[{index}] {body}".rstrip()


# Manuscripts under review carry line numbers in the margin. PDF extraction
# drops them inline, so a sentence comes out as "...and record 356 357 fig 1a
# the keys were equipped...". Anything reading the text then has to cope with
# integers appearing mid-sentence, and a quote taken from the paper will not
# match the extracted text.
#
# They are identifiable by position rather than by content: a line number is a
# bare integer sitting in the margin, in a narrow vertical band, repeated many
# times down the page. A number inside a sentence fails all three tests.
LINE_NUMBER_MIN_PER_PAGE = 5      # fewer than this is not a numbered margin
LINE_NUMBER_BAND_FRACTION = 0.10  # margin is the outer tenth of the page width


def _is_bare_integer(text: str) -> bool:
    stripped = text.strip()
    return stripped.isdigit() and len(stripped) <= 4


def _line_number_span_keys(data: dict, page_rect) -> set:
    """
    Identify margin line numbers in a parsed page, keyed by position in the tree.

    Keys are (block, line, span) indices rather than object identity: the parse
    has to be shared with whoever rebuilds the text, because `get_text('dict')`
    returns fresh objects on every call and identity does not survive a second
    parse.

    Returns an empty set unless the page really looks line-numbered, so a paper
    that merely mentions numbers keeps every one of them.
    """
    width = page_rect.width or 1
    left_edge = page_rect.x0 + width * LINE_NUMBER_BAND_FRACTION
    right_edge = page_rect.x1 - width * LINE_NUMBER_BAND_FRACTION

    candidates = []   # (key, x0)
    for bi, block in enumerate(data.get('blocks', [])):
        if block.get('type') != 0:
            continue
        for li, line in enumerate(block.get('lines', [])):
            for si, span in enumerate(line.get('spans', [])):
                if not _is_bare_integer(span.get('text', '')):
                    continue
                x0, _, x1, _ = span.get('bbox', (0, 0, 0, 0))
                if x1 <= left_edge or x0 >= right_edge:
                    candidates.append(((bi, li, si), x0))

    if len(candidates) < LINE_NUMBER_MIN_PER_PAGE:
        return set()

    # Require a shared vertical band. A numbered margin is a column; a stray
    # marginal digit such as a figure label or page number is not.
    banded = {key for key, x0 in candidates
              if sum(1 for _, other in candidates if abs(other - x0) <= 12)
              >= LINE_NUMBER_MIN_PER_PAGE}
    return banded if len(banded) >= LINE_NUMBER_MIN_PER_PAGE else set()


def _page_text_without_line_numbers(page) -> str:
    """Extract a page's text, dropping any margin line numbers it carries."""
    try:
        data = page.get_text('dict')
    except Exception:
        return page.get_text()

    drop = _line_number_span_keys(data, page.rect)
    if not drop:
        return page.get_text()

    out = []
    for bi, block in enumerate(data.get('blocks', [])):
        if block.get('type') != 0:
            continue
        for li, line in enumerate(block.get('lines', [])):
            parts = [span.get('text', '')
                     for si, span in enumerate(line.get('spans', []))
                     if (bi, li, si) not in drop]
            text = ''.join(parts).strip()
            if text:
                out.append(text)
    return '\n'.join(out)


class PaperFetcher:
    """Fetch full text of scientific papers from multiple sources."""

    def __init__(
        self,
        cache_dir: str | Path,
        contact_email: str | None = None,
        tool_name: str = 'paper-text-fetcher',
        user_agent: str | None = None,
        api_keys: dict[str, str] | None = None,
        verbose: bool = False,
        use_cache: bool = True,
        metadata_cache_ttl_days: float | None = 7.0,
    ):
        """
        Args:
            cache_dir: Directory for the JSON-per-DOI text cache.
            contact_email: Address sent to NCBI, Unpaywall, and CrossRef so they
                can reach you about traffic from this tool. NCBI asks for it and
                Unpaywall requires it, so the Unpaywall source is skipped when it
                is not supplied. Set it if you intend to fetch at any volume.
            tool_name: Identifier sent alongside `contact_email` to NCBI.
            user_agent: Overrides the default User-Agent header.
            api_keys: Optional credentials for sources that need them. Currently
                recognises 'elsevier' for the ScienceDirect full-text API.
            verbose: Print per-source progress to stderr.
            use_cache: Whether to read and write the on-disk cache.
            metadata_cache_ttl_days: How long cached metadata-only results stay
                valid before the fallback chain is retried, since closed-access
                papers do become open later. Full-text entries never expire.
                None disables expiry.
        """
        self.verbose = verbose
        self.contact_email = contact_email
        self.tool_name = tool_name
        self.api_keys = api_keys or {}
        self.cache = TextCache(
            Path(cache_dir),
            enabled=use_cache,
            metadata_ttl_days=metadata_cache_ttl_days,
        )

        agent = user_agent or DEFAULT_USER_AGENT
        if contact_email and 'mailto:' not in agent:
            agent = f"{agent} (mailto:{contact_email})"

        self.session = requests.Session()
        self.session.headers.update({'User-Agent': agent})

        self._last_request_at: dict[str, float] = {}
        # Playwright's synchronous handles belong to the thread that created
        # them, so browser state is per-thread rather than per-fetcher. Sharing
        # one fetcher across a thread pool and driving a single browser from all
        # of them deadlocks: the workers block in waitpid on a driver that is
        # waiting for its own thread, and the process spins at full CPU with no
        # progress. The lock additionally keeps launches serialized, since
        # starting several Chromium instances at once is where that stall began.
        self._tls = threading.local()
        self._browser_lock = threading.Lock()

    @property
    def use_cache(self) -> bool:
        return self.cache.enabled

    @property
    def cache_dir(self) -> Path:
        return self.cache.cache_dir

    def log(self, message: str):
        """Print message if verbose mode is enabled."""
        if self.verbose:
            print(f"[DEBUG] {message}", file=sys.stderr)

    def _ncbi_params(self) -> dict[str, str]:
        """Identification parameters NCBI asks callers to send."""
        params = {'tool': self.tool_name}
        if self.contact_email:
            params['email'] = self.contact_email
        return params

    def _polite_get(self, url: str, **kwargs) -> requests.Response:
        """
        session.get with a per-host minimum interval between requests.

        Only waits when a request to the same rate-limited host would come too
        soon after the previous one, so single fetches pay little or nothing
        while batch runs stay within each service's request-rate guidance.
        """
        host = urlparse(url).netloc
        min_interval = HOST_MIN_INTERVALS.get(host, 0.0)
        if min_interval:
            elapsed = time.monotonic() - self._last_request_at.get(host, 0.0)
            wait = min_interval - elapsed
            if wait > 0:
                time.sleep(wait)
        try:
            return self.session.get(url, **kwargs)
        finally:
            if min_interval:
                self._last_request_at[host] = time.monotonic()

    # ------------------------------------------------------------------ #
    # Playwright browser lifecycle
    # ------------------------------------------------------------------ #

    def _get_browser(self):
        """
        Return a shared headless Chromium instance, launching it on first use.

        Launching Chromium costs about a second, so one instance is reused
        across all Playwright fetches rather than launched per call. Call
        `close()` (or use the fetcher as a context manager) to release it.
        """
        if not PLAYWRIGHT_AVAILABLE:
            return None
        if getattr(self._tls, 'browser', None) is None:
            with self._browser_lock:
                try:
                    self._tls.playwright = sync_playwright().start()
                    self._tls.browser = self._tls.playwright.chromium.launch(
                        headless=True,
                        args=['--disable-blink-features=AutomationControlled']
                    )
                except Exception as e:
                    self.log(f"Failed to launch browser: {e}")
                    self._close_browser()
                    return None
        return self._tls.browser

    def _close_browser(self):
        """Release this thread's browser. Other threads keep their own."""
        browser = getattr(self._tls, 'browser', None)
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
            self._tls.browser = None
        playwright = getattr(self._tls, 'playwright', None)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
            self._tls.playwright = None

    def close(self):
        """Release the shared browser and the HTTP session."""
        self._close_browser()
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Utility helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def is_preprint_doi(doi: str) -> bool:
        """Check if a DOI is from a preprint server (bioRxiv/medRxiv)."""
        return doi.startswith('10.1101/')

    def get_pmcid_for_doi(self, doi: str) -> Optional[str]:
        """
        Get PMCID for a DOI using NCBI ID converter.

        Returns the PMCID if found, None otherwise.
        """
        converter_url = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
        params = {
            'ids': doi,
            'format': 'json',
            **self._ncbi_params(),
        }

        try:
            resp = self._polite_get(converter_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            records = data.get('records', [])
            if records and records[0].get('pmcid'):
                return records[0]['pmcid']
        except Exception as e:
            self.log(f"Error getting PMCID: {e}")

        return None

    # ------------------------------------------------------------------ #
    # Source-specific fetchers
    # ------------------------------------------------------------------ #

    def _extract_jats_text(self, content: bytes, record_id: str) -> Optional[str]:
        """
        Extract article text and hyperlinks from a JATS full-text XML record.

        Returns None for abstract-only records (no substantive <body>); see
        validation.xml_has_body. Parsed with html.parser rather than lxml:
        lxml-xml truncates table content in STAR Methods sections, and lxml's
        HTML mode wraps the document in its own <body> element, which would
        defeat the abstract-only check.
        """
        soup = BeautifulSoup(content, 'html.parser')

        if not xml_has_body(soup):
            self.log(
                f"Record {record_id} has no article body (abstract-only), skipping"
            )
            return None

        text = soup.get_text(separator=' ', strip=True)

        # Also extract hyperlink URLs from ext-link elements
        ext_links = []
        for link in soup.find_all('ext-link'):
            href = link.get('xlink:href', '') or link.get('href', '')
            if href:
                ext_links.append(href)

        if ext_links:
            self.log(f"Found {len(ext_links)} hyperlinks in XML")
            text = text + '\n\n[HYPERLINKS]\n' + '\n'.join(ext_links)

        return text

    def get_text_from_europe_pmc(self, doi: str) -> tuple[Optional[str], Optional[str]]:
        """
        Get full text from Europe PMC.

        Supports both PMC articles (via PMCID) and preprints (via PPR ID).
        Abstract-only records are skipped: see validation.xml_has_body.

        Returns tuple of (text, pmcid) - pmcid is returned even if text fetch fails,
        for potential Playwright fallback.
        """
        self.log(f"Trying Europe PMC for DOI: {doi}")
        pmcid_found = None

        search_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
        params = {
            # Backslash-escape any quote in the DOI so it cannot terminate the
            # quoted query term early
            'query': f'DOI:"{doi.replace(chr(34), chr(92) + chr(34))}"',
            'format': 'json',
            'resultType': 'core'
        }

        try:
            resp = self._polite_get(search_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            if data.get('resultList', {}).get('result'):
                result = data['resultList']['result'][0]
                pmcid = result.get('pmcid')

                # Try PMCID first (for published articles)
                if pmcid:
                    pmcid_found = pmcid
                    self.log(f"Found PMCID: {pmcid}, fetching full text")
                    fulltext_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"

                    try:
                        ft_resp = self._polite_get(fulltext_url, timeout=30)
                        if ft_resp.status_code == 200:
                            text = self._extract_jats_text(ft_resp.content, pmcid)
                            return text, pmcid_found
                    except Exception as e:
                        self.log(f"Error fetching full text: {e}")

                # Try PPR ID for preprints (bioRxiv, medRxiv, etc.)
                full_text_ids = result.get('fullTextIdList', {}).get('fullTextId', [])
                for ft_id in full_text_ids:
                    if ft_id.startswith('PPR'):
                        self.log(f"Found preprint ID: {ft_id}, fetching full text")
                        fulltext_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{ft_id}/fullTextXML"

                        try:
                            ft_resp = self._polite_get(fulltext_url, timeout=30)
                            if ft_resp.status_code == 200:
                                text = self._extract_jats_text(ft_resp.content, ft_id)
                                if text is None:
                                    continue
                                return text, pmcid_found
                        except Exception as e:
                            self.log(f"Error fetching preprint full text: {e}")

                if not pmcid and not full_text_ids:
                    self.log("No PMCID or preprint ID available, skipping abstract-only result")

        except Exception as e:
            self.log(f"Europe PMC error: {e}")

        return None, pmcid_found

    def get_text_from_pmc(self, doi: str) -> tuple[Optional[str], Optional[str]]:
        """
        Get full text from NCBI PubMed Central.

        Returns tuple of (text, pmcid) - pmcid is returned for potential Playwright fallback.
        """
        self.log(f"Trying NCBI PMC for DOI: {doi}")

        pmcid = self.get_pmcid_for_doi(doi)
        if not pmcid:
            return None, None
        self.log(f"Found PMCID: {pmcid}")

        try:
            efetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            params = {
                'db': 'pmc',
                'id': pmcid,
                'rettype': 'xml',
                **self._ncbi_params(),
            }

            ft_resp = self._polite_get(efetch_url, params=params, timeout=30)
            if ft_resp.status_code == 200:
                soup = BeautifulSoup(ft_resp.content, 'lxml-xml')

                if not xml_has_body(soup):
                    self.log(
                        f"NCBI PMC record for {pmcid} has no article body "
                        "(abstract-only), skipping"
                    )
                    return None, pmcid

                return soup.get_text(separator=' ', strip=True), pmcid

        except Exception as e:
            self.log(f"NCBI PMC error: {e}")

        return None, pmcid

    def get_text_from_crossref(self, doi: str) -> Optional[str]:
        """
        Get metadata from CrossRef (title, abstract, references).

        This is a fallback that provides limited text.
        """
        self.log(f"Trying CrossRef for DOI: {doi}")

        url = f"https://api.crossref.org/works/{quote(doi, safe='')}"

        try:
            resp = self._polite_get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            message = data.get('message', {})
            text_parts = []

            # Title
            if message.get('title'):
                text_parts.extend(message['title'])

            # Abstract
            if message.get('abstract'):
                abstract = BeautifulSoup(message['abstract'], 'html.parser').get_text()
                text_parts.append(abstract)

            # References (often the only place a cited dataset DOI appears).
            # Emit one entry per ref, 1-indexed and prefixed with [N], so the
            # downstream resolver can map in-text [N] citations to the correct
            # bibliography entry. Always emit something for every ref so
            # positions stay aligned with the published bibliography even when
            # CrossRef has no DOI deposited for an entry.
            for index, ref in enumerate(message.get('reference', []), start=1):
                text_parts.append(format_crossref_reference(index, ref))

            if text_parts:
                return '\n\n'.join(text_parts)

        except Exception as e:
            self.log(f"CrossRef error: {e}")

        return None

    def get_text_from_pmc_playwright(self, pmcid: str) -> Optional[str]:
        """
        Get full text from PMC using Playwright.

        The PMC API sometimes returns incomplete text (e.g., author manuscripts
        missing data availability sections). This method scrapes the full HTML
        page which often contains more complete content.

        Args:
            pmcid: The PMC ID (e.g., 'PMC11093107')

        Requires: pip install playwright && playwright install chromium
        """
        browser = self._get_browser()
        if browser is None:
            self.log("Playwright not available, skipping PMC browser fetch")
            return None

        self.log(f"Trying PMC via Playwright for PMCID: {pmcid}")

        context = None
        try:
            context = browser.new_context(user_agent=BROWSER_USER_AGENT)
            page = context.new_page()

            url = f'https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/'
            self.log(f"Navigating to: {url}")

            page.goto(url, wait_until='domcontentloaded', timeout=30000)
            # Wait for the article content rather than a fixed interval; on
            # timeout fall through and judge whatever rendered
            try:
                page.wait_for_selector('article, main', timeout=10000)
            except Exception:
                pass

            title = page.title()
            if 'not found' in title.lower() or '404' in title or 'error' in title.lower():
                self.log(f"Page not found for {pmcid}")
                return None

            text = page.inner_text('body')

            if is_full_text(text, 'pmc_playwright'):
                self.log(f"Got {len(text)} chars from PMC via Playwright")
                return text

            self.log(
                f"PMC Playwright page is not full text "
                f"({len(text) if text else 0} chars)"
            )

        except Exception as e:
            self.log(f"PMC Playwright error: {e}")
            self._close_browser()
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

        return None

    def get_text_from_biorxiv_playwright(self, doi: str) -> Optional[str]:
        """
        Get full text from bioRxiv/medRxiv using Playwright to bypass Cloudflare.

        Requires: pip install playwright && playwright install chromium
        """
        if not self.is_preprint_doi(doi):
            return None

        browser = self._get_browser()
        if browser is None:
            self.log("Playwright not available, skipping bioRxiv browser fetch")
            return None

        self.log(f"Trying bioRxiv/medRxiv via Playwright for DOI: {doi}")

        context = None
        try:
            context = browser.new_context(user_agent=BROWSER_USER_AGENT)
            page = context.new_page()

            for server in ['biorxiv', 'medrxiv']:
                url = f'https://www.{server}.org/content/{quote(doi, safe="/")}v1.full'
                self.log(f"Navigating to: {url}")

                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=30000)
                    # The article appears once any Cloudflare challenge has
                    # passed; on timeout fall through and judge what rendered
                    try:
                        page.wait_for_selector(
                            '.article.fulltext-view, article', timeout=15000
                        )
                    except Exception:
                        pass

                    title = page.title()
                    if 'not found' in title.lower() or '404' in title:
                        self.log(f"Page not found on {server}")
                        continue

                    article = page.query_selector('article')
                    if article:
                        text = article.inner_text()
                    else:
                        text = page.inner_text('body')

                    if is_full_text(text, 'playwright_biorxiv'):
                        self.log(f"Got {len(text)} chars from {server} via Playwright")
                        return text
                    self.log(
                        f"{server} page is not full text "
                        f"({len(text) if text else 0} chars)"
                    )
                except Exception as e:
                    self.log(f"Error fetching from {server}: {e}")
                    continue

        except Exception as e:
            self.log(f"Playwright error: {e}")
            self._close_browser()
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

        return None

    def get_text_from_publisher_playwright(self, doi: str) -> Optional[str]:
        """
        Scrape full text from publisher's HTML page using Playwright.

        This is a fallback for when regular HTTP requests fail (403 Forbidden, etc).

        Requires: pip install playwright && playwright install chromium
        """
        browser = self._get_browser()
        if browser is None:
            self.log("Playwright not available, skipping publisher browser fetch")
            return None

        self.log(f"Trying publisher HTML via Playwright for DOI: {doi}")

        context = None
        try:
            context = browser.new_context(user_agent=BROWSER_USER_AGENT)
            page = context.new_page()

            doi_url = f'https://doi.org/{quote(doi, safe="/")}'
            self.log(f"Navigating to: {doi_url}")

            page.goto(doi_url, wait_until='domcontentloaded', timeout=30000)
            try:
                page.wait_for_selector('article, main, [role="main"]', timeout=8000)
            except Exception:
                pass

            title = page.title()
            if 'not found' in title.lower() or '404' in title or 'error' in title.lower():
                self.log(f"Page not found for {doi}")
                return None

            text = page.inner_text('body')

            if text and len(text) >= MIN_FULL_TEXT_CHARS and not looks_like_paywall_or_landing_page(text):
                self.log(f"Got {len(text)} chars from publisher via Playwright")
                return text

            self.log(
                f"Publisher Playwright page is not full text "
                f"({len(text) if text else 0} chars)"
            )

        except Exception as e:
            self.log(f"Publisher Playwright error: {e}")
            self._close_browser()
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

        return None

    def get_text_from_publisher_html(self, doi: str) -> Optional[str]:
        """
        Scrape full text from publisher's open access HTML page.

        Works with Nature, Springer, Cell, Elsevier, and other open access papers.
        Falls back to Playwright if regular HTTP request fails.
        """
        self.log(f"Trying publisher HTML for DOI: {doi}")

        doi_url = f"https://doi.org/{quote(doi, safe='/')}"

        try:
            headers = {
                'User-Agent': BROWSER_USER_AGENT,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }

            resp = self._polite_get(doi_url, headers=headers, timeout=30, allow_redirects=True)
            resp.raise_for_status()

            content_type = resp.headers.get('content-type', '')
            if 'text/html' not in content_type:
                self.log(f"Not HTML content: {content_type}")
                return None

            soup = BeautifulSoup(resp.content, 'lxml')

            # noscript must go too: React-based publisher sites put an "enable
            # JavaScript" banner there that would land at the top of the
            # extracted text and read as a bot-check page
            for element in soup(['script', 'style', 'noscript', 'nav', 'header', 'footer']):
                element.decompose()

            article_content = None
            selectors = [
                'article',
                '[role="main"]',
                '.article-content',
                '.article__body',
                '#article-body',
                '.c-article-body',  # Nature
                '.article-section',
                'main',
            ]

            for selector in selectors:
                article_content = soup.select_one(selector)
                if article_content:
                    break

            if article_content:
                text = article_content.get_text(separator=' ', strip=True)
            else:
                text = soup.get_text(separator=' ', strip=True)

            if len(text) >= MIN_FULL_TEXT_CHARS and not looks_like_paywall_or_landing_page(text):
                self.log(f"Got {len(text)} chars from publisher HTML")
                return text
            else:
                self.log(
                    f"Publisher HTML is not full text ({len(text)} chars, "
                    "paywall/landing page or abstract only)"
                )
                self.log("Trying Playwright fallback")
                return self.get_text_from_publisher_playwright(doi)

        except Exception as e:
            self.log(f"Publisher HTML error: {e}")
            self.log("Trying Playwright fallback for publisher HTML")
            return self.get_text_from_publisher_playwright(doi)

    def extract_text_from_pdf_url(self, url: str) -> Optional[str]:
        """Download a PDF from a URL and extract text using PyMuPDF."""
        if not PYMUPDF_AVAILABLE:
            self.log("PyMuPDF not available, skipping PDF extraction")
            return None
        try:
            # stream=True so the content-type check happens on the headers,
            # before the body of a non-PDF is pulled down
            with self._polite_get(url, timeout=60, stream=True) as resp:
                if resp.status_code != 200:
                    self.log(f"PDF download failed: HTTP {resp.status_code}")
                    return None
                content_type = resp.headers.get('content-type', '')
                path_is_pdf = urlparse(url).path.lower().endswith('.pdf')
                if 'pdf' not in content_type and not path_is_pdf:
                    self.log(f"Not a PDF: {content_type}")
                    return None
                pdf_bytes = resp.content
            with fitz.open(stream=pdf_bytes, filetype='pdf') as doc:
                text = '\n'.join(
                    _page_text_without_line_numbers(page) for page in doc).strip()
            if len(text) >= MIN_FULL_TEXT_CHARS and not looks_like_paywall_or_landing_page(text):
                return text
            self.log(
                f"PDF is not full text ({len(text)} chars); likely a cover page, "
                "abstract, or failed extraction"
            )
            return None
        except Exception as e:
            self.log(f"PDF extraction error: {e}")
            return None

    def get_text_from_elsevier(self, doi: str) -> Optional[str]:
        """
        Get paper full text via the Elsevier ScienceDirect API.

        Needs an `elsevier` entry in `api_keys`, or ELSEVIER_API_KEY /
        SCOPUS_API_KEY in the environment. Elsevier issues one key per developer
        account covering both the Scopus and ScienceDirect endpoints, so either
        name works. Retrieval still depends on your entitlement: without an
        institutional subscription many articles return 403.
        """
        api_key = (
            self.api_keys.get('elsevier')
            or os.environ.get('ELSEVIER_API_KEY')
            or os.environ.get('SCOPUS_API_KEY')
        )
        if not api_key:
            return None

        # Only Elsevier-published DOIs are served by this endpoint
        if not doi.startswith(ELSEVIER_DOI_PREFIXES):
            return None

        self.log(f"Trying Elsevier API for DOI: {doi}")
        try:
            resp = self._polite_get(
                f"https://api.elsevier.com/content/article/doi/{quote(doi, safe='/')}",
                headers={
                    'X-ELS-APIKey': api_key,
                    'Accept': 'text/plain',
                },
                timeout=30,
            )
            if resp.status_code == 200:
                text = resp.text.strip()
                if is_full_text(text, 'elsevier'):
                    self.log(f"Got text from Elsevier API ({len(text)} chars)")
                    return text
                self.log(
                    f"Elsevier API returned metadata only ({len(text)} chars), "
                    "not the article body"
                )
            elif resp.status_code == 401:
                self.log("Elsevier API: unauthorized (check API key)")
            elif resp.status_code == 403:
                self.log("Elsevier API: forbidden (subscription required)")
            else:
                self.log(f"Elsevier API: status {resp.status_code}")
        except Exception as e:
            self.log(f"Elsevier API error: {e}")
        return None

    def get_text_from_unpaywall(self, doi: str) -> Optional[str]:
        """
        Get paper text via Unpaywall open-access PDF lookup.

        Unpaywall requires a contact address on every request, so this source is
        skipped when `contact_email` was not supplied.
        """
        if not self.contact_email:
            self.log("Unpaywall requires contact_email, skipping")
            return None

        self.log(f"Trying Unpaywall for DOI: {doi}")
        try:
            resp = self._polite_get(
                f"https://api.unpaywall.org/v2/{quote(doi, safe='/')}",
                params={'email': self.contact_email},
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get('is_oa'):
                self.log("Unpaywall: not OA")
                return None
            for loc in data.get('oa_locations', []):
                pdf_url = loc.get('url_for_pdf')
                if not pdf_url:
                    continue
                # Skip PMC PDFs — they return HTML redirects; we use PMC Playwright instead
                if 'pmc.ncbi.nlm.nih.gov' in pdf_url:
                    continue
                self.log(f"Unpaywall PDF: {pdf_url[:80]}")
                text = self.extract_text_from_pdf_url(pdf_url)
                if text:
                    return text
        except Exception as e:
            self.log(f"Unpaywall error: {e}")
        return None

    # ------------------------------------------------------------------ #
    # Main orchestrator
    # ------------------------------------------------------------------ #

    def get_paper_text(self, doi: str) -> tuple[Optional[str], str, bool]:
        """
        Get paper text from multiple sources with a fallback chain.

        Returns tuple of (text, source_name, from_cache) or (None, '', False) if not found.
        Combines text from multiple sources to maximize coverage.

        The returned text may be metadata only (CrossRef title, abstract, and
        reference list) when no source had the article body. Callers that need
        the body must use `get_paper_text_detailed` and check `has_full_text`.
        """
        result = self.get_paper_text_detailed(doi)
        return result['text'], result['source'], result['from_cache']

    def get_paper_text_detailed(self, doi: str) -> dict:
        """
        Get paper text plus an explicit account of what was retrieved.

        Returns a dict with:
          text           - combined text, or None if nothing was retrieved
          source         - '+'-joined contributing sources ('' if none)
          from_cache     - whether the result came from the local cache
          has_full_text  - whether any source delivered the article body
          status         - 'full_text', 'metadata_only', or 'unavailable'
          reason         - human-readable explanation when not full text

        `status` is the field to branch on. 'metadata_only' means we have the
        title, abstract, and references but no body: usable for mining the
        reference list, not for judging data reuse, since reuse is described in
        Methods and Data Availability sections.
        """
        # Check cache first
        cached = self.cache.get(doi)
        if cached and cached[0]:
            cached_text, cached_source, cached_full = cached
            return {
                'text': cached_text,
                'source': cached_source,
                'from_cache': True,
                'has_full_text': cached_full,
                'status': 'full_text' if cached_full else 'metadata_only',
                'reason': None if cached_full else (
                    'No source provided the article body; cached result is '
                    'metadata only (title, abstract, references)'
                ),
            }

        text_parts = []
        sources_used = []
        pmcid = None  # Track PMCID for potential Playwright fallback

        # For bioRxiv/medRxiv preprints (10.1101/...), use dedicated method first
        if self.is_preprint_doi(doi):
            self.log(f"Preprint DOI detected, trying bioRxiv/medRxiv Playwright first: {doi}")
            playwright_text = self.get_text_from_biorxiv_playwright(doi)
            if is_full_text(playwright_text, 'playwright_biorxiv'):
                self.log(f"Got text from bioRxiv Playwright ({len(playwright_text)} chars)")
                text_parts.append(playwright_text)
                sources_used.append('playwright_biorxiv')

            # Also try CrossRef for references
            crossref_text = self.get_text_from_crossref(doi)
            if crossref_text and len(crossref_text) > 100:
                self.log(f"Got text from crossref ({len(crossref_text)} chars)")
                text_parts.append(crossref_text)
                if 'crossref' not in sources_used:
                    sources_used.append('crossref')

            # If bioRxiv Playwright failed, try Europe PMC (some preprints are indexed there)
            if not sources_used or sources_used == ['crossref']:
                text, europe_pmc_pmcid = self.get_text_from_europe_pmc(doi)
                if is_full_text(text, 'europe_pmc'):
                    self.log(f"Got text from europe_pmc ({len(text)} chars)")
                    text_parts.insert(0, text)
                    sources_used.insert(0, 'europe_pmc')
        else:
            # For non-preprint DOIs, try Europe PMC first
            text, europe_pmc_pmcid = self.get_text_from_europe_pmc(doi)
            if europe_pmc_pmcid:
                pmcid = europe_pmc_pmcid
            if is_full_text(text, 'europe_pmc'):
                self.log(f"Got text from europe_pmc ({len(text)} chars)")
                text_parts.append(text)
                sources_used.append('europe_pmc')
            else:
                # Try NCBI PMC
                text, ncbi_pmcid = self.get_text_from_pmc(doi)
                if ncbi_pmcid:
                    pmcid = ncbi_pmcid
                if is_full_text(text, 'ncbi_pmc'):
                    self.log(f"Got text from ncbi_pmc ({len(text)} chars)")
                    text_parts.append(text)
                    sources_used.append('ncbi_pmc')

            # Always try CrossRef for the reference list
            crossref_text = self.get_text_from_crossref(doi)
            if crossref_text and len(crossref_text) > 100:
                self.log(f"Got text from crossref ({len(crossref_text)} chars)")
                text_parts.append(crossref_text)
                if 'crossref' not in sources_used:
                    sources_used.append('crossref')

            # If PMC text is short, try Playwright for more complete content
            MIN_PMC_TEXT_FOR_COMPLETENESS = 15000
            pmc_text_length = len(text_parts[0]) if text_parts and sources_used and sources_used[0] in ('europe_pmc', 'ncbi_pmc') else 0

            if pmc_text_length > 0 and pmc_text_length < MIN_PMC_TEXT_FOR_COMPLETENESS:
                if not pmcid:
                    pmcid = self.get_pmcid_for_doi(doi)
                if pmcid:
                    self.log(f"PMC text seems short ({pmc_text_length} chars), trying Playwright for {pmcid}")
                    playwright_text = self.get_text_from_pmc_playwright(pmcid)
                    if playwright_text and len(playwright_text) > pmc_text_length:
                        self.log(f"Got better text from PMC Playwright ({len(playwright_text)} chars vs {pmc_text_length})")
                        text_parts[0] = playwright_text
                        sources_used[0] = 'pmc_playwright'

            # If we don't have PMC full text, try other sources
            if not sources_used or sources_used == ['crossref']:
                # Try Elsevier API (for 10.1016/ DOIs)
                elsevier_text = self.get_text_from_elsevier(doi)
                if is_full_text(elsevier_text, 'elsevier'):
                    self.log(f"Got text from Elsevier API ({len(elsevier_text)} chars)")
                    text_parts.append(elsevier_text)
                    sources_used.append('elsevier')

            if not sources_used or sources_used == ['crossref']:
                # Try Unpaywall for OA PDF
                unpaywall_text = self.get_text_from_unpaywall(doi)
                if is_full_text(unpaywall_text, 'unpaywall'):
                    self.log(f"Got text from Unpaywall ({len(unpaywall_text)} chars)")
                    text_parts.append(unpaywall_text)
                    sources_used.append('unpaywall')

            if not sources_used or sources_used == ['crossref']:
                # Try scraping publisher HTML as fallback
                publisher_text = self.get_text_from_publisher_html(doi)
                if is_full_text(publisher_text, 'publisher_html'):
                    self.log(f"Got text from publisher ({len(publisher_text)} chars)")
                    text_parts.append(publisher_text)
                    sources_used.append('publisher_html')

                # If publisher HTML failed but we have a PMCID, try PMC Playwright
                if (not sources_used or sources_used == ['crossref']) and pmcid:
                    self.log(f"Publisher blocked, trying PMC Playwright for {pmcid}")
                    pmc_playwright_text = self.get_text_from_pmc_playwright(pmcid)
                    if is_full_text(pmc_playwright_text, 'pmc_playwright'):
                        self.log(f"Got text from PMC Playwright ({len(pmc_playwright_text)} chars)")
                        text_parts.append(pmc_playwright_text)
                        sources_used.append('pmc_playwright')

        if text_parts:
            combined_text = '\n\n'.join(text_parts)
            source_str = '+'.join(sources_used)
            has_full_text = has_full_text_source(source_str)

            # Metadata-only results are cached too; the cache expires them
            # after its TTL so the fallback chain is eventually retried
            self.cache.put(doi, combined_text, source_str, has_full_text)

            if not has_full_text:
                self.log(
                    f"No full text available for {doi}; returning metadata only "
                    f"(sources: {source_str})"
                )

            return {
                'text': combined_text,
                'source': source_str,
                'from_cache': False,
                'has_full_text': has_full_text,
                'status': 'full_text' if has_full_text else 'metadata_only',
                'reason': None if has_full_text else (
                    f'No source provided the article body (got: {source_str}). '
                    'The paper is likely closed access with no OA copy.'
                ),
            }

        return {
            'text': None,
            'source': '',
            'from_cache': False,
            'has_full_text': False,
            'status': 'unavailable',
            'reason': 'No source returned any text for this DOI',
        }
