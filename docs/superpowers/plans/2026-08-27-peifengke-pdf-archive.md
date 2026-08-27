# Peifengke PDF Archive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a resumable archive command that discovers every publicly indexed “培风客” article it can find, renders one layout-preserving PDF per accessible article, and reports every success and failure.

**Architecture:** A standalone `scripts.wechat_archive` package discovers article candidates from several public index adapters, normalizes them into a durable manifest, resolves the best public source, prepares the source HTML for reliable image loading, and prints it with the installed Google Chrome. Poppler validates every PDF; the pipeline persists state after each article so a stopped run resumes safely.

**Tech Stack:** Python 3.13 standard library, Google Chrome 151 headless printing, Poppler `pdfinfo`/`pdftotext`, pytest.

---

## File map

- Create `scripts/wechat_archive/__init__.py`: public package exports.
- Create `scripts/wechat_archive/models.py`: article/status data types and stable identifiers.
- Create `scripts/wechat_archive/naming.py`: title normalization and safe PDF paths.
- Create `scripts/wechat_archive/storage.py`: atomic manifest, failure, and summary persistence.
- Create `scripts/wechat_archive/fetch.py`: allowlisted, rate-limited public HTTP fetching.
- Create `scripts/wechat_archive/discovery.py`: index adapters and bounded pagination crawl.
- Create `scripts/wechat_archive/content.py`: source classification, link resolution, and printable HTML preparation.
- Create `scripts/wechat_archive/render.py`: Chrome printing and Poppler PDF validation.
- Create `scripts/wechat_archive/pipeline.py`: discovery, resolution, rendering, resume, and reporting orchestration.
- Create `scripts/wechat_archive/__main__.py`: CLI and default “培风客” configuration.
- Create `tests/fixtures/wechat_archive/*.html`: deterministic index and article pages.
- Create `tests/test_wechat_archive_models.py`: model and naming tests.
- Create `tests/test_wechat_archive_storage.py`: persistence and report tests.
- Create `tests/test_wechat_archive_fetch.py`: URL policy and retry tests.
- Create `tests/test_wechat_archive_discovery.py`: adapter and pagination tests.
- Create `tests/test_wechat_archive_content.py`: source classification and printable HTML tests.
- Create `tests/test_wechat_archive_render.py`: Chrome command and PDF validation tests.
- Create `tests/test_wechat_archive_pipeline.py`: resume and end-to-end orchestration tests.
- Create `tests/test_wechat_archive_cli.py`: CLI default and argument tests.
- Modify `README.md`: document the local archive command and output location.

### Task 1: Article model and deterministic filenames

**Files:**
- Create: `scripts/wechat_archive/__init__.py`
- Create: `scripts/wechat_archive/models.py`
- Create: `scripts/wechat_archive/naming.py`
- Test: `tests/test_wechat_archive_models.py`

- [ ] **Step 1: Write failing model and naming tests**

```python
from datetime import date

from scripts.wechat_archive.models import ArticleRecord, ArchiveStatus, stable_article_key
from scripts.wechat_archive.naming import pdf_relative_path


def test_stable_key_ignores_title_spacing_and_case():
    left = stable_article_key(" FOMC  Preview ", date(2026, 7, 18), "")
    right = stable_article_key("fomc preview", date(2026, 7, 18), "")
    assert left == right


def test_pdf_path_is_year_partitioned_and_sanitized():
    record = ArticleRecord(
        key="abc123",
        title='经济/市场: "展望"',
        published_on=date(2026, 8, 19),
    )
    assert pdf_relative_path(record) == "2026/2026-08-19_经济-市场-展望.pdf"


def test_new_record_starts_discovered():
    record = ArticleRecord(key="k", title="标题", published_on=None)
    assert record.status is ArchiveStatus.DISCOVERED
    assert record.index_urls == []
```

- [ ] **Step 2: Run the test and confirm the missing package failure**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_models.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'scripts.wechat_archive'`.

- [ ] **Step 3: Implement the data model and naming rules**

```python
# scripts/wechat_archive/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from hashlib import sha256


class ArchiveStatus(StrEnum):
    DISCOVERED = "discovered"
    READY_ORIGINAL = "ready_original"
    READY_MIRROR = "ready_mirror"
    RENDERED = "rendered"
    FAILED = "failed"
    SKIPPED_EXISTING = "skipped_existing"


def stable_article_key(title: str, published_on: date | None, canonical_url: str) -> str:
    normalized_title = " ".join(title.casefold().split())
    day = published_on.isoformat() if published_on else "unknown-date"
    identity = canonical_url.strip() or f"{day}|{normalized_title}"
    return sha256(identity.encode("utf-8")).hexdigest()[:16]


@dataclass
class ArticleRecord:
    key: str
    title: str
    published_on: date | None
    index_urls: list[str] = field(default_factory=list)
    candidate_urls: list[str] = field(default_factory=list)
    source_url: str = ""
    source_type: str = ""
    status: ArchiveStatus = ArchiveStatus.DISCOVERED
    pdf_path: str = ""
    pdf_sha256: str = ""
    pdf_bytes: int = 0
    error: str = ""
```

```python
# scripts/wechat_archive/naming.py
from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

from .models import ArticleRecord


def safe_title(value: str, limit: int = 96) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*-\s*", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip(" .-")
    return value[:limit].rstrip(" .-") or "untitled"


def pdf_relative_path(record: ArticleRecord) -> str:
    day = record.published_on.isoformat() if record.published_on else "unknown-date"
    year = str(record.published_on.year) if record.published_on else "unknown-year"
    name = f"{day}_{safe_title(record.title)}.pdf"
    return str(PurePosixPath(year, name))
```

```python
# scripts/wechat_archive/__init__.py
from .models import ArticleRecord, ArchiveStatus

__all__ = ["ArticleRecord", "ArchiveStatus"]
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_models.py -q`

Expected: `3 passed`.

- [ ] **Step 5: Commit the model unit**

```bash
git add scripts/wechat_archive/__init__.py scripts/wechat_archive/models.py scripts/wechat_archive/naming.py tests/test_wechat_archive_models.py
git commit -m "feat: model WeChat archive records"
```

### Task 2: Atomic manifest and reports

**Files:**
- Create: `scripts/wechat_archive/storage.py`
- Test: `tests/test_wechat_archive_storage.py`

- [ ] **Step 1: Write failing round-trip and summary tests**

```python
import csv
import json
from datetime import date

from scripts.wechat_archive.models import ArticleRecord, ArchiveStatus
from scripts.wechat_archive.storage import ManifestStore


def test_manifest_round_trip_preserves_lists_and_status(tmp_path):
    store = ManifestStore(tmp_path)
    record = ArticleRecord(
        key="k1",
        title="文章",
        published_on=date(2025, 7, 8),
        index_urls=["https://freewechat.com/profile/x"],
        candidate_urls=["https://freewechat.com/a/1"],
        status=ArchiveStatus.READY_MIRROR,
    )
    store.save({record.key: record})
    loaded = store.load()
    assert loaded["k1"] == record


def test_reports_count_rendered_mirror_and_failed(tmp_path):
    store = ManifestStore(tmp_path)
    records = {
        "a": ArticleRecord("a", "A", None, source_type="original", status=ArchiveStatus.RENDERED),
        "b": ArticleRecord("b", "B", None, source_type="mirror", status=ArchiveStatus.RENDERED),
        "c": ArticleRecord("c", "C", None, status=ArchiveStatus.FAILED, error="deleted"),
    }
    store.save(records)
    store.write_reports(records, discovered_count=4)
    summary = json.loads((tmp_path / "run-summary.json").read_text())
    assert summary == {
        "discovered": 4,
        "deduplicated": 3,
        "rendered_original": 1,
        "rendered_mirror": 1,
        "failed": 1,
        "skipped_existing": 0,
    }
    with (tmp_path / "failures.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["key"] == "c"
    assert rows[0]["error"] == "deleted"
```

- [ ] **Step 2: Run the tests and confirm the missing store failure**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_storage.py -q`

Expected: collection fails because `scripts.wechat_archive.storage` does not exist.

- [ ] **Step 3: Implement atomic CSV persistence and reports**

Implement `ManifestStore` with these exact public methods:

```python
class ManifestStore:
    def __init__(self, root: Path):
        self.root = root

    def load(self) -> dict[str, ArticleRecord]:
        """Return an empty dict when manifest.csv does not exist."""

    def save(self, records: Mapping[str, ArticleRecord]) -> None:
        """Write manifest.csv.tmp, fsync it, then os.replace it onto manifest.csv."""

    def write_reports(
        self,
        records: Mapping[str, ArticleRecord],
        discovered_count: int,
    ) -> None:
        """Atomically write failures.csv and run-summary.json."""
```

Use this fixed manifest column order so later tasks can rely on it:

```python
FIELDS = [
    "key", "title", "published_on", "index_urls", "candidate_urls",
    "source_url", "source_type", "status", "pdf_path", "pdf_sha256",
    "pdf_bytes", "error",
]
```

Encode URL lists with `json.dumps(value, ensure_ascii=False)` and decode them with `json.loads`. Encode unknown dates as an empty string. Sort rows by `(published_on or date.min, title, key)` before writing.

- [ ] **Step 4: Run the focused tests**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_storage.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit persistence**

```bash
git add scripts/wechat_archive/storage.py tests/test_wechat_archive_storage.py
git commit -m "feat: persist WeChat archive progress"
```

### Task 3: Safe, polite public fetcher

**Files:**
- Create: `scripts/wechat_archive/fetch.py`
- Test: `tests/test_wechat_archive_fetch.py`

- [ ] **Step 1: Write failing URL-policy and retry tests**

```python
from urllib.error import HTTPError

import pytest

from scripts.wechat_archive.fetch import PublicFetcher, UnsafeSourceURL


class Response:
    status = 200
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def __init__(self, url: str, body: bytes):
        self.url = url
        self.body = body

    def geturl(self):
        return self.url

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_fetcher_rejects_unknown_and_private_hosts():
    fetcher = PublicFetcher({"mp.weixin.qq.com"}, opener=lambda request, timeout: None, delay=0)
    with pytest.raises(UnsafeSourceURL):
        fetcher.fetch("http://127.0.0.1/private")
    with pytest.raises(UnsafeSourceURL):
        fetcher.fetch("https://example.com/article")


def test_fetcher_retries_one_transient_503():
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 503, "busy", {}, None)
        return Response(request.full_url, "正文".encode())

    fetcher = PublicFetcher({"mp.weixin.qq.com"}, opener=opener, delay=0, backoff=0, retries=1)
    result = fetcher.fetch("https://mp.weixin.qq.com/s/abc")
    assert result.text == "正文"
    assert len(calls) == 2
```

- [ ] **Step 2: Run the tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_fetch.py -q`

Expected: collection fails because `scripts.wechat_archive.fetch` does not exist.

- [ ] **Step 3: Implement the allowlisted fetcher**

Create:

```python
@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    text: str


class UnsafeSourceURL(ValueError):
    pass
```

`PublicFetcher.fetch(url)` must enforce `https` or `http`, an exact host in its constructor allowlist, and the same policy on the final redirect URL. Use `urllib.request.Request` with a normal desktop user agent and no cookie jar. Retry only `HTTPError` status 429 or 500–599 and `URLError`, with `backoff * 2**attempt`; do not retry 401, 403, or 404. Serialize requests through a monotonic-clock delay. Decode from the response charset and fall back to UTF-8 with replacement.

- [ ] **Step 4: Run the focused tests**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_fetch.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit the fetcher**

```bash
git add scripts/wechat_archive/fetch.py tests/test_wechat_archive_fetch.py
git commit -m "feat: add polite public page fetcher"
```

### Task 4: Public index discovery and cross-source merge

**Files:**
- Create: `scripts/wechat_archive/discovery.py`
- Create: `tests/fixtures/wechat_archive/jintiankansha-page.html`
- Create: `tests/fixtures/wechat_archive/freewechat-page.html`
- Create: `tests/fixtures/wechat_archive/data258-page.html`
- Test: `tests/test_wechat_archive_discovery.py`

- [ ] **Step 1: Add compact deterministic HTML fixtures**

The “今天看啥” fixture contains two `/t/` article links, their dates, and one `?page=2` link. The FreeWeChat fixture contains one duplicate title/date and one unique `/a/` article. The Data258 fixture contains one dated article link. Keep each fixture under 30 lines and use only invented text except the public account name.

- [ ] **Step 2: Write failing adapter and merge tests**

```python
from pathlib import Path

from scripts.wechat_archive.discovery import (
    discover_data258,
    discover_freewechat,
    discover_jintiankansha,
    merge_candidates,
)

FIXTURES = Path("tests/fixtures/wechat_archive")


def test_jintiankansha_adapter_returns_articles_and_next_pages():
    html = (FIXTURES / "jintiankansha-page.html").read_text()
    articles, pages = discover_jintiankansha(html, "https://www.jintiankansha.com/column/x")
    assert [item.title for item in articles] == ["市场周报", "长期话题"]
    assert pages == ["https://www.jintiankansha.com/column/x?page=2"]


def test_cross_source_merge_keeps_all_evidence():
    today_html = (FIXTURES / "jintiankansha-page.html").read_text()
    free_html = (FIXTURES / "freewechat-page.html").read_text()
    today, _ = discover_jintiankansha(today_html, "https://www.jintiankansha.com/column/x")
    free, _ = discover_freewechat(free_html, "https://freewechat.com/profile/x")
    merged = merge_candidates(today + free)
    weekly = next(record for record in merged.values() if record.title == "市场周报")
    assert len(weekly.index_urls) == 2
    assert len(merged) == 3


def test_data258_adapter_returns_article_links():
    html = (FIXTURES / "data258-page.html").read_text()
    articles, pages = discover_data258(html, "https://mp.data258.com/article/category/peifengke")
    assert [(item.title, item.published_on.isoformat()) for item in articles] == [
        ("商品市场观察", "2026-04-03")
    ]
    assert pages == []
```

- [ ] **Step 3: Run the tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_discovery.py -q`

Expected: collection fails because `scripts.wechat_archive.discovery` does not exist.

- [ ] **Step 4: Implement adapters and bounded crawl**

Define:

```python
@dataclass(frozen=True)
class Candidate:
    title: str
    published_on: date | None
    index_url: str
    candidate_url: str


Adapter = Callable[[str, str], tuple[list[Candidate], list[str]]]
```

Use an `HTMLParser` subclass to capture anchors and surrounding text. Implement `discover_jintiankansha`, `discover_freewechat`, and `discover_data258` under the same `Adapter` signature. Resolve links with `urljoin`, accept only the expected article/pagination path patterns for each adapter, and parse dates with `date.fromisoformat`. `merge_candidates` creates `ArticleRecord` values using `stable_article_key`, merges URL evidence without duplicates, and prefers a known date over an unknown date.

Add `crawl_index(start_url, adapter, fetcher, max_pages)` using a FIFO queue and visited set. It must stop at `max_pages`, reject pagination that leaves the starting host, and return both candidates and a list of page-level errors.

- [ ] **Step 5: Run discovery tests**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_discovery.py -q`

Expected: `3 passed`.

- [ ] **Step 6: Commit discovery**

```bash
git add scripts/wechat_archive/discovery.py tests/fixtures/wechat_archive tests/test_wechat_archive_discovery.py
git commit -m "feat: discover public WeChat article indexes"
```

### Task 5: Source classification and printable HTML

**Files:**
- Create: `scripts/wechat_archive/content.py`
- Create: `tests/fixtures/wechat_archive/original-article.html`
- Create: `tests/fixtures/wechat_archive/login-page.html`
- Create: `tests/fixtures/wechat_archive/mirror-summary.html`
- Test: `tests/test_wechat_archive_content.py`

- [ ] **Step 1: Write failing classification and transformation tests**

```python
from pathlib import Path

from scripts.wechat_archive.content import PageKind, assess_page, make_printable_html

FIXTURES = Path("tests/fixtures/wechat_archive")


def test_original_article_is_accepted_and_lazy_image_is_promoted():
    html = (FIXTURES / "original-article.html").read_text()
    assessment = assess_page(html, "培风客测试长文")
    assert assessment.kind is PageKind.ARTICLE
    printable = make_printable_html(html, "https://mp.weixin.qq.com/s/example")
    assert '<base href="https://mp.weixin.qq.com/s/example">' in printable
    assert 'src="https://mmbiz.qpic.cn/test.jpg"' in printable
    assert "data-src=" not in printable


def test_login_and_short_summary_are_rejected():
    login = (FIXTURES / "login-page.html").read_text()
    summary = (FIXTURES / "mirror-summary.html").read_text()
    assert assess_page(login, "培风客测试长文").kind is PageKind.LOGIN
    assert assess_page(summary, "培风客测试长文").kind is PageKind.SUMMARY
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_content.py -q`

Expected: collection fails because `scripts.wechat_archive.content` does not exist.

- [ ] **Step 3: Implement deterministic page assessment**

Define `PageKind` values `ARTICLE`, `LOGIN`, `CAPTCHA`, `DELETED`, `SUMMARY`, and `ERROR`, plus:

```python
@dataclass(frozen=True)
class PageAssessment:
    kind: PageKind
    text_length: int
    title_matches: bool
    reason: str
```

Strip tags and normalize whitespace for assessment. Detect explicit Chinese and English login/captcha/deleted/error markers before article acceptance. Require a normalized title match and at least 500 visible characters for `ARTICLE`; classify a matching page below that threshold as `SUMMARY`.

Implement `extract_wechat_links(html, base_url)` to return deduplicated `https://mp.weixin.qq.com/s/` links. Implement `make_printable_html` to inject a `<base>` element and print CSS, remove script and iframe elements, promote `data-src` to `src`, remove lazy-loading attributes, and hide known non-article selectors without rewriting the article body.

- [ ] **Step 4: Run content tests**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_content.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Commit content handling**

```bash
git add scripts/wechat_archive/content.py tests/fixtures/wechat_archive tests/test_wechat_archive_content.py
git commit -m "feat: prepare public articles for PDF"
```

### Task 6: Chrome rendering and Poppler validation

**Files:**
- Create: `scripts/wechat_archive/render.py`
- Test: `tests/test_wechat_archive_render.py`

- [ ] **Step 1: Write failing command and validation tests**

```python
from pathlib import Path

from scripts.wechat_archive.render import ChromeRenderer, PDFValidation, validate_pdf


def test_chrome_command_uses_isolated_profile_and_no_headers(tmp_path):
    renderer = ChromeRenderer(Path("/usr/bin/google-chrome"))
    command = renderer.command(
        html_path=tmp_path / "article.html",
        pdf_path=tmp_path / "article.pdf",
        profile_path=tmp_path / "profile",
    )
    assert "--headless=new" in command
    assert "--no-pdf-header-footer" in command
    assert any(arg.startswith("--user-data-dir=") for arg in command)
    assert command[-1].startswith("file://")


def test_validate_pdf_uses_poppler_output(monkeypatch, tmp_path):
    pdf = tmp_path / "article.pdf"
    pdf.write_bytes(b"%PDF-1.7" + b"x" * 25000)

    def fake_run(command, **kwargs):
        if command[0].endswith("pdfinfo"):
            return type("Done", (), {"returncode": 0, "stdout": "Pages: 3\n", "stderr": ""})()
        return type("Done", (), {"returncode": 0, "stdout": "培风客测试长文 正文", "stderr": ""})()

    monkeypatch.setattr("scripts.wechat_archive.render.subprocess.run", fake_run)
    result = validate_pdf(pdf, "培风客测试长文")
    assert result == PDFValidation(valid=True, pages=3, reason="")
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_render.py -q`

Expected: collection fails because `scripts.wechat_archive.render` does not exist.

- [ ] **Step 3: Implement isolated Chrome printing**

`ChromeRenderer.command` returns this parameter family with absolute paths:

```python
[
    str(chrome_path),
    "--headless=new",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-extensions",
    "--no-first-run",
    "--no-pdf-header-footer",
    "--run-all-compositor-stages-before-draw",
    "--virtual-time-budget=15000",
    f"--user-data-dir={profile_path}",
    f"--print-to-pdf={pdf_path}",
    html_path.resolve().as_uri(),
]
```

`render(html, final_pdf)` writes a temporary HTML file and temporary PDF under the output root, creates a fresh temporary Chrome profile, runs the command with a 90-second timeout, validates the temporary PDF, and calls `os.replace` only after validation. It must not add `--no-sandbox`; a Chrome sandbox failure is a batch-stopping error.

Implement:

```python
@dataclass(frozen=True)
class PDFValidation:
    valid: bool
    pages: int
    reason: str


def validate_pdf(path: Path, expected_title: str) -> PDFValidation:
    """Check size/header, parse Pages from pdfinfo, and confirm title text via pdftotext."""
```

Use a 20 KiB minimum size, require `%PDF-`, require at least one page, and compare normalized text against the first 24 normalized title characters.

- [ ] **Step 4: Run rendering tests**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_render.py -q`

Expected: `2 passed`.

- [ ] **Step 5: Perform a local synthetic Chrome smoke test**

Create a temporary HTML page through the renderer API, print it to `.tmp/wechat-archive-smoke.pdf`, and run:

```bash
pdfinfo .tmp/wechat-archive-smoke.pdf
pdftotext .tmp/wechat-archive-smoke.pdf -
```

Expected: `pdfinfo` reports at least one page and extracted text contains the synthetic title. Do not commit the PDF.

- [ ] **Step 6: Commit rendering**

```bash
git add scripts/wechat_archive/render.py tests/test_wechat_archive_render.py
git commit -m "feat: render and validate article PDFs"
```

### Task 7: Resumable archive pipeline

**Files:**
- Create: `scripts/wechat_archive/pipeline.py`
- Test: `tests/test_wechat_archive_pipeline.py`

- [ ] **Step 1: Write failing resume and failure-isolation tests**

Use in-memory fake discovery, fetch, and renderer collaborators. The first test seeds one valid existing PDF and asserts it becomes `skipped_existing` without a fetch. The second supplies one accepted article and one login page, then asserts one becomes `rendered`, the other `failed`, and both remain in the saved manifest.

The test constructs the pipeline through this interface:

```python
pipeline = ArchivePipeline(
    output_root=tmp_path,
    store=store,
    discover=discover,
    fetch=fetch,
    render=render,
    validate=validate,
)
result = pipeline.run(max_articles=None)
```

Assert `result.discovered`, `result.rendered`, `result.failed`, and `result.skipped_existing` exact counts.

- [ ] **Step 2: Run tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_pipeline.py -q`

Expected: collection fails because `scripts.wechat_archive.pipeline` does not exist.

- [ ] **Step 3: Implement source resolution and per-article checkpoints**

Define:

```python
@dataclass(frozen=True)
class RunResult:
    discovered: int
    deduplicated: int
    rendered: int
    failed: int
    skipped_existing: int
```

For each merged record, use this order:

1. If the manifest status is `RENDERED` or `SKIPPED_EXISTING` and `validate_pdf` still passes, set `SKIPPED_EXISTING` and do not fetch.
2. Fetch each candidate page. Extract and prepend any public WeChat original links.
3. Assess original pages before mirror pages.
4. Reject login, captcha, deleted, error, title mismatch, and summary pages.
5. Set `READY_ORIGINAL` or `READY_MIRROR`, save the manifest, render the page, hash the PDF, set `RENDERED`, and save again.
6. If all sources fail, set `FAILED` with a semicolon-separated bounded error message and save.
7. After every record, rewrite reports so interruption loses at most the current article.

Limit stored error text to 500 characters and continue after article-level failures. Propagate `OSError` for disk exhaustion and a dedicated renderer-startup error so fatal local failures stop the batch.

- [ ] **Step 4: Run pipeline tests**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_pipeline.py -q`

Expected: all pipeline tests pass.

- [ ] **Step 5: Run all archive unit tests**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_*.py -q`

Expected: all tests pass with no network access.

- [ ] **Step 6: Commit orchestration**

```bash
git add scripts/wechat_archive/pipeline.py tests/test_wechat_archive_pipeline.py
git commit -m "feat: orchestrate resumable WeChat archiving"
```

### Task 8: CLI, documentation, and inventory-only live test

**Files:**
- Create: `scripts/wechat_archive/__main__.py`
- Modify: `README.md`
- Test: `tests/test_wechat_archive_cli.py`

- [ ] **Step 1: Write failing CLI parsing test**

```python
from pathlib import Path

from scripts.wechat_archive.__main__ import parse_args


def test_cli_defaults_target_peifengke():
    args = parse_args([])
    assert args.output == Path("output/wechat/peifengke")
    assert args.delay == 2.0
    assert args.max_pages == 200
    assert args.inventory_only is False
```

- [ ] **Step 2: Run the CLI test and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_cli.py -q`

Expected: collection fails because `scripts.wechat_archive.__main__` does not exist.

- [ ] **Step 3: Implement the CLI**

Expose these arguments:

```text
--output PATH                 default output/wechat/peifengke
--delay SECONDS               default 2.0
--timeout SECONDS             default 30.0
--max-pages COUNT             default 200
--max-articles COUNT          optional sample bound
--inventory-only              discover and write manifest without rendering
--chrome PATH                 default /usr/bin/google-chrome
```

Hard-code only the confirmed account configuration:

```python
INDEXES = [
    ("https://www.jintiankansha.com/column/Fj9DVZlo9X", discover_jintiankansha),
    ("https://freewechat.com/profile/Mzg4NzY3NzIwOA%3D%3D", discover_freewechat),
    ("https://mp.data258.com/article/category/peifengke", discover_data258),
]

ALLOWED_HOSTS = {
    "www.jintiankansha.com",
    "freewechat.com",
    "www.freewechat.com",
    "mp.data258.com",
    "mp.weixin.qq.com",
}
```

Exit nonzero for fatal local failures, but return zero when individual article failures are fully reported.

- [ ] **Step 4: Add README usage**

Document the command, the `output/wechat/peifengke/` layout, the public-only access boundary, and these two phases:

```bash
.venv/bin/python -m scripts.wechat_archive --inventory-only
.venv/bin/python -m scripts.wechat_archive --max-articles 3
```

- [ ] **Step 5: Run unit tests and CLI help**

Run: `.venv/bin/python -m pytest tests/test_wechat_archive_*.py -q`

Expected: all archive tests pass.

Run: `.venv/bin/python -m scripts.wechat_archive --help`

Expected: help lists every argument above.

- [ ] **Step 6: Run the live inventory phase**

Run: `.venv/bin/python -m scripts.wechat_archive --inventory-only`

Expected: `output/wechat/peifengke/manifest.csv` and `run-summary.json` exist, the discovered count is positive, and index failures are explicit rather than silent. This step requires approved network access.

- [ ] **Step 7: Commit CLI and documentation**

```bash
git add scripts/wechat_archive/__main__.py scripts/wechat_archive/discovery.py README.md tests/test_wechat_archive_cli.py tests/test_wechat_archive_discovery.py
git commit -m "feat: expose Peifengke archive command"
```

### Task 9: Three-article visual gate and full archive run

**Files:**
- Produce, not commit: `output/wechat/peifengke/**`

- [ ] **Step 1: Run the three-article sample**

Run: `.venv/bin/python -m scripts.wechat_archive --max-articles 3`

Expected: at least one PDF is produced or each unavailable sample has a concrete failure reason. A Chrome startup failure stops the run.

- [ ] **Step 2: Validate every sample PDF mechanically**

Run `pdfinfo` and `pdftotext` for each sample PDF. Confirm at least one page, nontrivial file size, and the expected title in extracted text.

- [ ] **Step 3: Inspect the first page and a content-heavy page visually**

Convert selected pages with `pdftoppm -png`, then inspect the PNG files with the local image viewer. Confirm the title, main text column, inline images, Chinese font rendering, and absence of login/captcha overlays. If image loading or clipping fails, add a failing renderer/content regression test before changing code.

- [ ] **Step 4: Run the complete archive**

Run: `.venv/bin/python -m scripts.wechat_archive`

Expected: the process resumes from the sample manifest, writes one PDF per successful record, and completes with `manifest.csv`, `failures.csv`, and `run-summary.json` mutually consistent.

- [ ] **Step 5: Verify final counts and rerun idempotence**

Count PDF paths listed as `rendered` or `skipped_existing` and compare them with PDFs under `output/wechat/peifengke/`. Run the full command again and confirm unchanged articles are reported as `skipped_existing` without rewriting their PDF hashes.

- [ ] **Step 6: Run repository verification**

Run: `.venv/bin/python -m pytest -q`

Expected: the full repository suite passes.

Run: `.venv/bin/ruff check scripts/wechat_archive tests/test_wechat_archive_*.py`

Expected: no lint errors.

- [ ] **Step 7: Commit any sample-gate fixes**

If Steps 1–6 required code or test changes, commit only those source changes. Do not add `output/` or `.tmp/`:

```bash
git add scripts/wechat_archive tests/test_wechat_archive_*.py README.md
git commit -m "fix: harden Peifengke PDF archiving"
```

If no source changes were required, skip this commit.
