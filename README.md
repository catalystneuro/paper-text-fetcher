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
| `status` | `'full_text'`, `'metadata_only'`, or `'unavailable'` |
| `has_full_text` | Whether any source delivered the article body |
| `reason` | Why the result is not full text, when it is not |
| `from_cache` | Whether the result came from the local cache |

The three statuses are worth distinguishing. `full_text` means the body was
retrieved. `metadata_only` means the DOI resolves and we retrieved a title, an
abstract, and usually a reference list, but no source would give us the body,
which normally indicates a closed-access article with no open copy. That result
is still useful for mining the bibliography, and it is returned rather than
discarded, but it should not be fed to anything that expects a paper.
`unavailable` means no source returned anything at all.

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

Two conditions must both hold. A source capable of delivering a body must claim
to have done so, and the text itself must read like a body.

The second condition is necessary because the publisher-HTML and PDF paths
accept whatever the server returns, and servers routinely return a paywall
interstitial or an abstract landing page with HTTP 200. Those pages can run to
several thousand characters of navigation, abstract, and references without
containing a single sentence of the paper. So text must clear a length floor
(`MIN_FULL_TEXT_CHARS`, 6000), must not match known paywall and bot-check
phrases, and must contain at least one section heading that essentially every
research article has and no abstract does.

For JATS records from Europe PMC and NCBI there is a more reliable signal, since
both return a record containing only `<front>` when an article is indexed but not
open. The presence of a substantive `<body>` element is checked directly.

These rules are in `validation.py` as pure functions, so they can be tested and
tuned without touching the network.

The heuristics are deliberately conservative and will reject some genuine full
text. An article with no Methods, Results, Discussion, or Acknowledgements
section will be classified as metadata only, which is the correct outcome for
most uses but wrong if you specifically want short commentaries and editorials.
Measured against a corpus of 73,500 cached papers, the rules reject 0.2% of
Europe PMC results and 0.1% of bioRxiv results, and inspection of a sample of
those found them to be genuine front-matter-only records rather than false
positives.

## Caching

Text is cached as one JSON file per DOI in `cache_dir`. Each entry records
whether it holds an article body. Filenames are the percent-encoded, lowercased
DOI, which is reversible and so cannot map two different DOIs onto one file.
Entries written under the older scheme are still found and read.

Entries written before the full-text flag existed are re-judged from their
content on read rather than trusted by their source name. This matters when
adopting the library against an existing cache: the older code stored landing
pages under the `publisher_html` source, so believing the source name would
carry the original error forward. Re-judging on read corrects it without a
refetch.

Metadata-only results are cached as well, but they expire after
`metadata_cache_ttl_days` (7 by default, `None` to disable). Full-text entries
never expire. The asymmetry is deliberate: a body does not stop being a body,
but a paper that was closed access last month may be open today, and without
expiry it would never be retried. Caching these results at all matters for
throughput, since a closed-access DOI otherwise re-walks the entire fallback
chain, browser included, on every lookup.

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

The tests cover the validation rules and the cache, and none of them touch the
network. There is no test coverage of the individual source fetchers, which
would need either recorded fixtures or live requests.
