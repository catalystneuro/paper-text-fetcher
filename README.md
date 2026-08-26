# paper-text-fetcher

Retrieve the full text of a scholarly article from its DOI, and get a clear
answer when the full text is not available.

Most DOI-to-text code treats any retrieved string as success. That is a problem,
because a title, an abstract, and a reference list can be retrieved for nearly
every DOI in existence, while the article body can be retrieved for only some of
them. Code that does not distinguish the two will quietly analyze abstracts in
place of papers, and the failure is invisible: there is no exception, no empty
string, and no obviously wrong output. It just produces conclusions drawn from
the wrong text.

This library treats that distinction as the primary thing it reports.

## Installation

```bash
pip install -e ".[all]"
python -m playwright install chromium   # only if you installed the browser extra
```

The core install needs `requests`, `beautifulsoup4`, and `lxml`. The `pdf` extra
adds PyMuPDF for open-access PDFs, and the `browser` extra adds Playwright for
preprint servers and publisher pages that render their content with JavaScript.
Both are optional, and the fetcher degrades gracefully without them, but coverage
drops noticeably.

## Usage

```python
from paper_text_fetcher import PaperFetcher

fetcher = PaperFetcher(
    cache_dir='.paper_cache',
    contact_email='you@example.org',
)

result = fetcher.get_paper_text_detailed('10.1038/s41586-023-06031-6')

if result['status'] == 'full_text':
    analyze(result['text'])
else:
    log_skip(result['doi'], result['reason'])
```

`get_paper_text_detailed()` returns a dict with these keys:

| Key | Meaning |
|-----|---------|
| `text` | The retrieved text, or `None` if nothing came back |
| `source` | `'+'`-joined list of contributing sources, such as `'europe_pmc+crossref'` |
| `status` | `'full_text'`, `'unknown'`, `'metadata_only'`, or `'unavailable'` |
| `has_full_text` | Whether a source delivered a structurally verified article body |
| `reason` | Why the result is not full text, when it is not |
| `from_cache` | Whether the result came from the local cache |

The four statuses are worth distinguishing. `full_text` means the body was
retrieved and structurally verified. `unknown` means substantial text was
retrieved but nothing structural vouches for it being the body — it may be a
genuine article from a publisher page the library has no body selector for, or
it may be a landing page; the caller decides whether to use it, flag it, or
judge it by other means. `metadata_only` means the DOI resolves and we
retrieved a title, an abstract, and usually a reference list, but no source
would give us the body, which normally indicates a closed-access article with
no open copy. That result is still useful for mining the bibliography, and it
is returned rather than discarded, but it should not be fed to anything that
expects a paper. `unavailable` means no source returned anything at all.

`get_paper_text()` remains available and returns a `(text, source, from_cache)`
tuple for callers that predate the status field. It cannot express the
distinction above, so new code should prefer the detailed form.

The fetcher holds a `requests` session and, when the browser extra is
installed, a headless Chromium instance that is launched once and reused across
fetches. Use it as a context manager, or call `close()`, so both are released:

```python
with PaperFetcher(cache_dir='.paper_cache', contact_email='you@example.org') as fetcher:
    for doi in dois:
        result = fetcher.get_paper_text_detailed(doi)
```

## Sources

Sources are tried in a fallback chain, and results from several of them are
combined, because CrossRef supplies a reference list that the full-text sources
often omit.

| Source | Provides | Notes |
|--------|----------|-------|
| Europe PMC | Body via PMCID or preprint ID | Highest quality when available |
| NCBI PMC | Body via DOI to PMCID conversion | |
| CrossRef | Title, abstract, references | Metadata only, never counted as full text |
| Elsevier ScienceDirect | Body for `10.1016/` DOIs | Needs an API key and entitlement |
| Unpaywall | Body from open-access PDFs | Needs `contact_email` |
| Publisher HTML | Body by scraping the DOI redirect | Most likely to return a landing page |
| Playwright | Body from bioRxiv, medRxiv, PMC, publishers | Optional, bypasses JavaScript and bot checks |

## How Full Text Is Detected

A result is `full_text` only when the document's own structure marks the
retrieved text as the article body. Guessing from the text itself — length,
keywords, section-heading heuristics — is what earlier versions did, and it
passed front matter and landing pages as articles, so no amount of retrieved
text upgrades a result on its own.

What counts as structural evidence differs per source:

| Source | Evidence |
|--------|----------|
| Europe PMC, NCBI PMC | A substantive `<body>` element in the JATS record; both return `<front>`-only records for articles that are indexed but not open |
| PMC via Playwright | The rendered page's article-body container, which PMC renders from the same JATS `<body>` |
| bioRxiv / medRxiv | The `<article>` element on the `.full` page of the preprint server |
| Publisher HTML | A selector that specifically marks the article body (Nature's `.c-article-body` and similar) |
| Unpaywall PDFs | Provenance: the URL is Unpaywall's open-access copy of the article itself, so a valid PDF with substantial text is the article |
| Elsevier ScienceDirect | None yet; its plain-text response is reported as `unknown` |

Text that arrives without such evidence — a publisher page where only a generic
`article` or `main` container matched, or no container at all — is reported as
`unknown`, never as `full_text`. A landing page puts its abstract in the same
generic containers a full-text page uses, so their presence proves nothing.

Sanity gates still apply on top: text must clear a length floor
(`MIN_FULL_TEXT_CHARS`, 6000, so an abstract-sized fragment is never taken) and
must not open with known paywall or bot-check phrases.

The rules live in `validation.py` and the DOM extraction helpers in
`fetcher.py` as pure functions, so they can be tested without touching the
network.

The design is deliberately conservative: when a publisher's markup is not
recognized, the result degrades to `unknown` rather than to a false
`full_text`, so failures are visible instead of silent.

## Caching

Text is cached as one JSON file per DOI in `cache_dir`. Each entry records its
status and the version of the validation rules it was written under. Filenames
are the percent-encoded, lowercased DOI, which is reversible and so cannot map
two different DOIs onto one file. Entries written under the older scheme are
still found and read.

Entries written under an older validation version were judged by rules since
found unreliable, and their flat text cannot be verified structurally after
the fact. A full-text claim in such an entry is therefore served as `unknown`
rather than trusted, and the entry becomes subject to the TTL below, so the
paper is eventually refetched and re-verified. This matters when adopting this
version against an existing cache: entries that really hold front matter or
landing pages stop being reported as full text immediately, and each cached
paper is re-verified at most once.

Results without a verified body — metadata-only and unknown alike — expire
after `metadata_cache_ttl_days` (7 by default, `None` to disable). Verified
full-text entries never expire. The asymmetry is deliberate: a body does not
stop being a body, but a paper that was closed access last month may be open
today, and without expiry it would never be retried. Caching these results at
all matters for throughput, since a closed-access DOI otherwise re-walks the
entire fallback chain, browser included, on every lookup.

## Request Pacing

Requests to the metadata APIs are spaced by a per-host minimum interval, so the
delay is paid only when two requests to the same service would land too close
together. A fetch that succeeds on its first source waits for nothing.

## Identifying Yourself

`contact_email` is passed to NCBI, Unpaywall, and CrossRef so they can reach you
about traffic from your tool. NCBI asks for it, and Unpaywall requires it on
every request, so the Unpaywall source is skipped entirely when it is missing. It
is worth setting for anything beyond a handful of DOIs, since these are free
services run on public funding and the polite pools are noticeably faster than
the anonymous ones.

## Testing

```bash
python -m pytest tests/
```

The tests cover the validation rules, the DOM extraction helpers, the
composite-result resolution, and the cache, and none of them touch the
network. There is no test coverage of the live source fetchers themselves,
which would need either recorded fixtures or live requests.
