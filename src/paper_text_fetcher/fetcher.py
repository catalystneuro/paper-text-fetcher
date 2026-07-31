"""
Fetch the full text of a scholarly article given its DOI.

`PaperFetcher.get_paper_text_detailed()` tries a chain of sources and reports
not only what it retrieved but whether the result is an article body or only
metadata. See `validation.py` for why that distinction is enforced.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

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


DEFAULT_USER_AGENT = (
    'paper-text-fetcher/0.1 '
    '(https://github.com/bendichter/paper-text-fetcher)'
)


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
        """
        self.verbose = verbose
        self.contact_email = contact_email
        self.tool_name = tool_name
        self.api_keys = api_keys or {}
        self.cache = TextCache(Path(cache_dir), enabled=use_cache)

        agent = user_agent or DEFAULT_USER_AGENT
        if contact_email and 'mailto:' not in agent:
            agent = f"{agent} (mailto:{contact_email})"

        self.session = requests.Session()
        self.session.headers.update({'User-Agent': agent})

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
            resp = self.session.get(converter_url, params=params, timeout=30)
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
            'query': f'DOI:"{doi}"',
            'format': 'json',
            'resultType': 'core'
        }

        try:
            resp = self.session.get(search_url, params=params, timeout=30)
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
                        ft_resp = self.session.get(fulltext_url, timeout=30)
                        if ft_resp.status_code == 200:
                            # Use html.parser for more complete text extraction
                            # (lxml-xml truncates table content in STAR Methods)
                            soup = BeautifulSoup(ft_resp.content, 'html.parser')

                            if not xml_has_body(soup):
                                self.log(
                                    f"Europe PMC record for {pmcid} has no article body "
                                    "(abstract-only), skipping"
                                )
                                return None, pmcid_found

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
                            ft_resp = self.session.get(fulltext_url, timeout=30)
                            if ft_resp.status_code == 200:
                                soup = BeautifulSoup(ft_resp.content, 'html.parser')

                                if not xml_has_body(soup):
                                    self.log(
                                        f"Europe PMC preprint record {ft_id} has no article "
                                        "body (abstract-only), skipping"
                                    )
                                    continue

                                text = soup.get_text(separator=' ', strip=True)

                                ext_links = []
                                for link in soup.find_all('ext-link'):
                                    href = link.get('xlink:href', '') or link.get('href', '')
                                    if href:
                                        ext_links.append(href)

                                if ext_links:
                                    self.log(f"Found {len(ext_links)} hyperlinks in preprint XML")
                                    text = text + '\n\n[HYPERLINKS]\n' + '\n'.join(ext_links)

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

        converter_url = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
        params = {
            'ids': doi,
            'format': 'json',
            **self._ncbi_params(),
        }

        try:
            resp = self.session.get(converter_url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            records = data.get('records', [])
            if records and records[0].get('pmcid'):
                pmcid = records[0]['pmcid']
                self.log(f"Found PMCID: {pmcid}")

                efetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
                params = {
                    'db': 'pmc',
                    'id': pmcid,
                    'rettype': 'xml',
                    **self._ncbi_params(),
                }

                ft_resp = self.session.get(efetch_url, params=params, timeout=30)
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

        return None, None

    def get_text_from_crossref(self, doi: str) -> Optional[str]:
        """
        Get metadata from CrossRef (title, abstract, references).

        This is a fallback that provides limited text.
        """
        self.log(f"Trying CrossRef for DOI: {doi}")

        url = f"https://api.crossref.org/works/{quote(doi, safe='')}"

        try:
            resp = self.session.get(url, timeout=30)
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
        if not PLAYWRIGHT_AVAILABLE:
            self.log("Playwright not available, skipping PMC browser fetch")
            return None

        self.log(f"Trying PMC via Playwright for PMCID: {pmcid}")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled']
                )
                context = browser.new_context(
                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                page = context.new_page()

                url = f'https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/'
                self.log(f"Navigating to: {url}")

                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(5000)

                title = page.title()
                if 'not found' in title.lower() or '404' in title or 'error' in title.lower():
                    self.log(f"Page not found for {pmcid}")
                    browser.close()
                    return None

                text = page.inner_text('body')

                if is_full_text(text, 'pmc_playwright'):
                    self.log(f"Got {len(text)} chars from PMC via Playwright")
                    browser.close()
                    return text

                self.log(
                    f"PMC Playwright page is not full text "
                    f"({len(text) if text else 0} chars)"
                )
                browser.close()

        except Exception as e:
            self.log(f"PMC Playwright error: {e}")

        return None

    def get_text_from_biorxiv_playwright(self, doi: str) -> Optional[str]:
        """
        Get full text from bioRxiv/medRxiv using Playwright to bypass Cloudflare.

        Requires: pip install playwright && playwright install chromium
        """
        if not PLAYWRIGHT_AVAILABLE:
            self.log("Playwright not available, skipping bioRxiv browser fetch")
            return None

        if not self.is_preprint_doi(doi):
            return None

        self.log(f"Trying bioRxiv/medRxiv via Playwright for DOI: {doi}")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled']
                )
                context = browser.new_context(
                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                page = context.new_page()

                for server in ['biorxiv', 'medrxiv']:
                    url = f'https://www.{server}.org/content/{doi}v1.full'
                    self.log(f"Navigating to: {url}")

                    try:
                        page.goto(url, wait_until='domcontentloaded', timeout=30000)
                        page.wait_for_timeout(10000)

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
                            browser.close()
                            return text
                        self.log(
                            f"{server} page is not full text "
                            f"({len(text) if text else 0} chars)"
                        )
                    except Exception as e:
                        self.log(f"Error fetching from {server}: {e}")
                        continue

                browser.close()

        except Exception as e:
            self.log(f"Playwright error: {e}")

        return None

    def get_text_from_publisher_playwright(self, doi: str) -> Optional[str]:
        """
        Scrape full text from publisher's HTML page using Playwright.

        This is a fallback for when regular HTTP requests fail (403 Forbidden, etc).

        Requires: pip install playwright && playwright install chromium
        """
        if not PLAYWRIGHT_AVAILABLE:
            self.log("Playwright not available, skipping publisher browser fetch")
            return None

        self.log(f"Trying publisher HTML via Playwright for DOI: {doi}")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=['--disable-blink-features=AutomationControlled']
                )
                context = browser.new_context(
                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                page = context.new_page()

                doi_url = f'https://doi.org/{doi}'
                self.log(f"Navigating to: {doi_url}")

                page.goto(doi_url, wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(5000)

                title = page.title()
                if 'not found' in title.lower() or '404' in title or 'error' in title.lower():
                    self.log(f"Page not found for {doi}")
                    browser.close()
                    return None

                text = page.inner_text('body')

                if text and len(text) >= MIN_FULL_TEXT_CHARS and not looks_like_paywall_or_landing_page(text):
                    self.log(f"Got {len(text)} chars from publisher via Playwright")
                    browser.close()
                    return text

                self.log(
                    f"Publisher Playwright page is not full text "
                    f"({len(text) if text else 0} chars)"
                )
                browser.close()

        except Exception as e:
            self.log(f"Publisher Playwright error: {e}")

        return None

    def get_text_from_publisher_html(self, doi: str) -> Optional[str]:
        """
        Scrape full text from publisher's open access HTML page.

        Works with Nature, Springer, Cell, Elsevier, and other open access papers.
        Falls back to Playwright if regular HTTP request fails.
        """
        self.log(f"Trying publisher HTML for DOI: {doi}")

        doi_url = f"https://doi.org/{doi}"

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
            }

            resp = self.session.get(doi_url, headers=headers, timeout=30, allow_redirects=True)
            resp.raise_for_status()

            content_type = resp.headers.get('content-type', '')
            if 'text/html' not in content_type:
                self.log(f"Not HTML content: {content_type}")
                return None

            soup = BeautifulSoup(resp.content, 'html.parser')

            for element in soup(['script', 'style', 'nav', 'header', 'footer']):
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

        return None

    def extract_text_from_pdf_url(self, url: str) -> Optional[str]:
        """Download a PDF from a URL and extract text using PyMuPDF."""
        import tempfile
        try:
            resp = self.session.get(url, timeout=60, stream=True)
            if resp.status_code != 200:
                self.log(f"PDF download failed: HTTP {resp.status_code}")
                return None
            content_type = resp.headers.get('content-type', '')
            if 'pdf' not in content_type and not url.endswith('.pdf'):
                self.log(f"Not a PDF: {content_type}")
                return None
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                for chunk in resp.iter_content(chunk_size=65536):
                    tmp.write(chunk)
                tmp_path = tmp.name
            import fitz  # PyMuPDF
            pages = []
            with fitz.open(tmp_path) as doc:
                for page in doc:
                    pages.append(page.get_text())
            Path(tmp_path).unlink(missing_ok=True)
            text = '\n'.join(pages).strip()
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
        if not doi.startswith('10.1016/'):
            return None

        self.log(f"Trying Elsevier API for DOI: {doi}")
        try:
            resp = self.session.get(
                f"https://api.elsevier.com/content/article/doi/{doi}",
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
            resp = self.session.get(
                f"https://api.unpaywall.org/v2/{doi}",
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
                time.sleep(0.5)
                # Try NCBI PMC
                text, ncbi_pmcid = self.get_text_from_pmc(doi)
                if ncbi_pmcid:
                    pmcid = ncbi_pmcid
                if is_full_text(text, 'ncbi_pmc'):
                    self.log(f"Got text from ncbi_pmc ({len(text)} chars)")
                    text_parts.append(text)
                    sources_used.append('ncbi_pmc')
                time.sleep(0.5)

            # Always try CrossRef for the reference list
            crossref_text = self.get_text_from_crossref(doi)
            if crossref_text and len(crossref_text) > 100:
                self.log(f"Got text from crossref ({len(crossref_text)} chars)")
                text_parts.append(crossref_text)
                if 'crossref' not in sources_used:
                    sources_used.append('crossref')
            time.sleep(0.5)

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

            # Only cache if we have more than just crossref metadata
            if sources_used != ['crossref']:
                self.cache.put(doi, combined_text, source_str, has_full_text)
            else:
                self.log(f"Skipping cache for crossref-only result: {doi}")

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
