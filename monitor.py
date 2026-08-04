from __future__ import annotations

import base64
import difflib
import hashlib
import html
import json
import logging
import os
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover
    PdfReader = None


try:
    import pytesseract
    from pdf2image import convert_from_path
except Exception:  # pragma: no cover
    pytesseract = None
    convert_from_path = None

try:
    import fitz  # PyMuPDF - renders PDF pages to images for report thumbnails
except Exception:  # pragma: no cover
    fitz = None


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
REPORT_DIR = ROOT / "reports"
PAGES_DIR = ROOT / "docs"
PAGES_ARCHIVE_DIR = PAGES_DIR / "reports"
HISTORY_JSON_PATH = PAGES_DIR / "history.json"
HISTORY_HTML_PATH = PAGES_DIR / "history.html"
STATE_PATH = DATA_DIR / "state.json"
CHECKPOINT_PATH = DATA_DIR / "checkpoint.json"

START_URL = "https://www.pagcor.ph/regulatory/index.php"
ALLOWED_PREFIX = "https://www.pagcor.ph/regulatory/"
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
logging.getLogger("pypdf").setLevel(logging.ERROR)

OCR_MIN_PAGE_CHARS = int(os.getenv("PAGCOR_OCR_MIN_PAGE_CHARS", "40"))
OCR_DPI = int(os.getenv("PAGCOR_OCR_DPI", "180"))
OCR_LANG = os.getenv("PAGCOR_OCR_LANG", "eng")

DATE_RE = re.compile(
    r"\b(?:as of\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}\b",
    re.I,
)
REGULATORY_ENTITY_RE = re.compile(
    r"\b(?:licensee|licensees|licensed|accredited|accreditation|registered|cancelled|operator|operators|administrator|administrators|brand|brands|domain|domains|url|urls)\b",
    re.I,
)
SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
PAGE_LABEL_RE = re.compile(r"^第 (\d+) 頁$")
THUMBNAIL_MAX_WIDTH_PX = 700
MARKDOWN_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\((.+)\)$")


@dataclass
class ResourceSnapshot:
    url: str
    kind: str
    title: str
    status_code: int
    content_type: str
    sha256: str
    text_sha256: str
    size: int
    links: list[dict]
    domains: list[str]
    dates: list[str]
    checked_at: str
    local_path: str = ""
    text_blocks: list[dict] = field(default_factory=list)


@dataclass
class RunResult:
    resources: dict[str, ResourceSnapshot]
    failures: list[dict]
    max_resources_hit: bool
    checked_at: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def sha256_text(text: str) -> str:
    return sha256_bytes(normalize_text(text).encode("utf-8"))


def normalize_url(url: str, base: str) -> str:
    absolute = urljoin(base, url)
    absolute, _fragment = urldefrag(absolute)
    return absolute


def resource_kind(url: str, content_type: str = "") -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".pdf") or "pdf" in content_type:
        return "pdf"
    if path.endswith((".xlsx", ".xls")):
        return "excel"
    if path.endswith((".docx", ".doc")):
        return "document"
    if path.endswith(".csv"):
        return "csv"
    return "html"


def is_monitorable(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and url.startswith(ALLOWED_PREFIX)


def extract_links(html: bytes, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    unique: dict[str, dict] = {}
    for a in soup.select("a[href]"):
        href = normalize_url(a.get("href", ""), base_url)
        if not is_monitorable(href):
            continue
        unique[href] = {
            "text": normalize_text(a.get_text(" ", strip=True)),
            "url": href,
            "kind": resource_kind(href),
        }
    return sorted(unique.values(), key=lambda item: item["url"])


def clean_soup(html: bytes) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup


def extract_html_text(html: bytes) -> str:
    return normalize_text(clean_soup(html).get_text(" ", strip=True))


def make_text_block(label: str, text: str) -> dict:
    normalized = normalize_text(text)
    return {
        "label": label,
        "text": normalized,
        "text_sha256": sha256_text(normalized),
        "excerpt": normalized[:500],
    }


def extract_html_blocks(html: bytes) -> list[dict]:
    soup = clean_soup(html)
    main = soup.find("main") or soup.body or soup
    blocks: list[dict] = []
    section_title = "頁面開頭"
    current_title = section_title
    continuation = 0
    current_parts: list[str] = []

    def flush() -> None:
        nonlocal current_parts
        text = normalize_text(" ".join(current_parts))
        if text:
            blocks.append(make_text_block(current_title, text))
        current_parts = []

    for element in main.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th"], recursive=True):
        text = normalize_text(element.get_text(" ", strip=True))
        if not text:
            continue
        if element.name in {"h1", "h2", "h3", "h4"}:
            flush()
            section_title = text[:120]
            current_title = section_title
            continuation = 0
        else:
            current_parts.append(text)
            if len(" ".join(current_parts)) > 2500:
                flush()
                continuation += 1
                current_title = f"{section_title}（續 {continuation}）"
    flush()
    if not blocks:
        whole = extract_html_text(html)
        for idx in range(0, len(whole), 2500):
            blocks.append(make_text_block(f"文字區塊 {idx // 2500 + 1}", whole[idx:idx + 2500]))
    return blocks


def extract_title(html: bytes, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return normalize_text(soup.title.string)
    h1 = soup.find(["h1", "h2", "h3"])
    return normalize_text(h1.get_text(" ", strip=True)) if h1 else fallback


def ocr_pdf_pages(path: Path, page_numbers: list[int]) -> dict[int, str]:
    if pytesseract is None or convert_from_path is None or not page_numbers:
        return {}
    results: dict[int, str] = {}
    for page_number in page_numbers:
        try:
            images = convert_from_path(
                str(path),
                dpi=OCR_DPI,
                first_page=page_number,
                last_page=page_number,
                fmt="png",
            )
            if images:
                results[page_number] = normalize_text(pytesseract.image_to_string(images[0], lang=OCR_LANG))
        except Exception:
            continue
    return results


def extract_pdf_pages(path: Path) -> list[dict]:
    if PdfReader is None:
        return []
    try:
        reader = PdfReader(str(path))
    except Exception:
        return []
    pages = []
    low_text_pages: list[int] = []
    raw_text_by_page: dict[int, str] = {}
    for idx, page in enumerate(reader.pages, 1):
        try:
            text = normalize_text(page.extract_text() or "")
        except Exception:
            text = ""
        raw_text_by_page[idx] = text
        if len(text) < OCR_MIN_PAGE_CHARS:
            low_text_pages.append(idx)

    ocr_text_by_page = ocr_pdf_pages(path, low_text_pages)
    for idx in range(1, len(reader.pages) + 1):
        text = raw_text_by_page.get(idx, "")
        source = "文字抽取"
        ocr_text = ocr_text_by_page.get(idx, "")
        if len(ocr_text) > len(text):
            text = ocr_text
            source = "OCR"
        block = make_text_block(f"第 {idx} 頁", text)
        block["source"] = source
        pages.append(block)
    return pages

def extract_pdf_text(path: Path) -> str:
    return normalize_text("\n".join(block.get("text", "") for block in extract_pdf_pages(path)))


def pdf_search_needle(text: str) -> str:
    """Clean a diff snippet into something more likely to exact-match via
    PyMuPDF's search_for, which needs a literal substring of the page's text
    layer (truncation ellipses and our own "+N more" notes never appear
    verbatim in the PDF, so they'd never match)."""
    text = text.strip()
    if not text or text.startswith("...") or text.startswith("(+"):
        return ""
    if text.endswith("..."):
        text = text[:-3].strip()
    return text


def find_highlight_rects(page, text: str) -> list:
    """Locate a diff snippet on the rendered page. Falls back to shorter
    prefixes of the same snippet, since long snippets are more likely to
    diverge from the PDF's literal text layer (line wraps, extra spacing)."""
    needle = pdf_search_needle(text)
    if not needle:
        return []
    rects = page.search_for(needle)
    if rects:
        return rects
    words = needle.split()
    for n in (8, 5, 3):
        if len(words) > n:
            rects = page.search_for(" ".join(words[:n]))
            if rects:
                return rects
    return []


REMOVED_HIGHLIGHT_COLOR = (0.86, 0.15, 0.15)  # red - matches the caption/legend text shown next to "before" thumbnails
ADDED_HIGHLIGHT_COLOR = (0.13, 0.55, 0.13)  # green - matches the caption/legend text shown next to "after" thumbnails


def render_pdf_page_thumbnail(
    pdf_path: Path,
    page_number: int,
    highlight_texts: list[str] | None = None,
    highlight_color: tuple[float, float, float] = REMOVED_HIGHLIGHT_COLOR,
    max_width_px: int = THUMBNAIL_MAX_WIDTH_PX,
) -> str | None:
    """Render one PDF page to a base64 PNG data URI, for embedding directly in
    the HTML report so a reader can see the changed page without opening the
    file. When highlight_texts is given, draws a highlight_color box around
    each snippet found on the page, so the reader doesn't have to spot the
    difference between two full-page screenshots themselves. Returns None
    (not raised) on any failure - missing library, missing file, out-of-range
    page - so a thumbnail miss never breaks report generation."""
    if fitz is None or not pdf_path.exists():
        return None
    try:
        with fitz.open(pdf_path) as doc:
            if page_number < 1 or page_number > len(doc):
                return None
            page = doc[page_number - 1]
            for text in highlight_texts or []:
                for rect in find_highlight_rects(page, text):
                    padded = fitz.Rect(rect.x0 - 2, rect.y0 - 2, rect.x1 + 2, rect.y1 + 2)
                    page.draw_rect(padded, color=highlight_color, width=2, overlay=True)
            zoom = max_width_px / page.rect.width if page.rect.width else 1.0
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            png_bytes = pixmap.tobytes("png")
        return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    except Exception:
        return None


def save_download(url: str, content: bytes, digest: str) -> Path:
    suffix = Path(urlparse(url).path).suffix.lower() or ".bin"
    path = DOWNLOAD_DIR / f"{digest}{suffix}"
    if not path.exists():
        path.write_bytes(content)
    return path


def fetch(session: requests.Session, url: str, timeout_seconds: float) -> requests.Response:
    response = session.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response


def snapshot_resource(session: requests.Session, url: str, checked_at: str, timeout_seconds: float) -> ResourceSnapshot:
    response = fetch(session, url, timeout_seconds)
    content = response.content
    content_type = response.headers.get("content-type", "")
    kind = resource_kind(url, content_type)
    digest = sha256_bytes(content)
    title = Path(urlparse(url).path).name or url
    links: list[dict] = []
    text = ""
    local_path = ""
    text_blocks: list[dict] = []

    if kind == "html":
        links = extract_links(content, url)
        title = extract_title(content, title)
        text_blocks = extract_html_blocks(content)
        text = normalize_text("\n".join(block.get("text", "") for block in text_blocks))
    else:
        local = save_download(url, content, digest)
        local_path = str(local.relative_to(ROOT))
        if kind == "pdf":
            text_blocks = extract_pdf_pages(local)
            text = normalize_text("\n".join(block.get("text", "") for block in text_blocks))

    domains = sorted(set(m.group(0).lower() for m in DOMAIN_RE.finditer(text)))
    dates = sorted(set(m.group(0) for m in DATE_RE.finditer(text)))

    return ResourceSnapshot(
        url=url,
        kind=kind,
        title=title,
        status_code=response.status_code,
        content_type=content_type,
        sha256=digest,
        text_sha256=sha256_text(text) if text else "",
        size=len(content),
        links=links,
        domains=domains,
        dates=dates,
        checked_at=checked_at,
        local_path=local_path,
        text_blocks=text_blocks,
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"meta": {}, "resources": {}}


def snapshot_from_dict(data: dict) -> ResourceSnapshot:
    data = dict(data)
    data.setdefault("text_blocks", [])
    return ResourceSnapshot(**data)


def save_checkpoint(resources: dict[str, ResourceSnapshot], failures: list[dict], queue: list[str], queued: set[str], seen: set[str], checked_at: str, max_resources: int) -> None:
    payload = {
        "checked_at": checked_at,
        "max_resources": max_resources,
        "resources": {url: asdict(snapshot) for url, snapshot in sorted(resources.items())},
        "failures": failures,
        "queue": queue,
        "queued": sorted(queued),
        "seen": sorted(seen),
    }
    CHECKPOINT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_checkpoint(max_resources: int) -> dict | None:
    if not CHECKPOINT_PATH.exists():
        return None
    try:
        payload = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if int(payload.get("max_resources", 0)) != max_resources:
        clear_checkpoint()
        return None
    return payload


def clear_checkpoint() -> None:
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()


def save_state(result: RunResult, recent_removals: dict | None = None) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    serializable = {url: asdict(snapshot) for url, snapshot in sorted(result.resources.items())}
    payload = {
        "meta": {
            "checked_at": result.checked_at,
            "resource_count": len(result.resources),
            "failure_count": len(result.failures),
            "max_resources_hit": result.max_resources_hit,
        },
        "resources": serializable,
        "failures": result.failures,
        "recent_removals": recent_removals or {},
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def crawl_queue(
    session: requests.Session,
    queue: list[str],
    queued: set[str],
    seen: set[str],
    resources: dict[str, ResourceSnapshot],
    failures: list[dict],
    checked_at: str,
    timeout_seconds: float,
    delay: float,
    max_resources: int,
    fetch_attempts: int,
) -> list[str]:
    """Process a URL queue breadth-first, retrying each fetch up to
    fetch_attempts times. Mutates resources/failures/seen/queued in place
    (shared state across calls, so a follow-up sweep can reuse it). Returns
    the remaining queue - non-empty only if max_resources was hit."""
    while queue and len(seen) < max_resources:
        url = queue.pop(0)
        if url in seen or not is_monitorable(url):
            continue
        seen.add(url)
        snapshot = None
        last_error: Exception | None = None
        for attempt in range(1, fetch_attempts + 1):
            try:
                snapshot = snapshot_resource(session, url, checked_at, timeout_seconds)
                break
            except Exception as exc:
                last_error = exc
                if attempt < fetch_attempts:
                    print(f"Retry {attempt}/{fetch_attempts - 1}: {url} ({exc})")
                    time.sleep(delay * 2)
        if snapshot is None:
            failures[:] = [failure for failure in failures if failure.get("url") != url]
            failures.append({"url": url, "error": str(last_error), "checked_at": checked_at})
            print(f"Failed: {url} ({last_error})")
            save_checkpoint(resources, failures, queue, queued, seen, checked_at, max_resources)
            time.sleep(delay)
            continue

        resources[url] = snapshot
        failures[:] = [failure for failure in failures if failure.get("url") != url]
        if snapshot.kind == "html":
            for link in snapshot.links:
                linked_url = link["url"]
                if linked_url not in seen and linked_url not in queued:
                    queue.append(linked_url)
                    queued.add(linked_url)
        save_checkpoint(resources, failures, queue, queued, seen, checked_at, max_resources)
        time.sleep(delay)
    return queue


def discover_and_snapshot() -> RunResult:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    start_url = os.getenv("PAGCOR_START_URL", START_URL)
    max_resources = int(os.getenv("PAGCOR_MAX_PAGES", "300"))
    delay = float(os.getenv("PAGCOR_REQUEST_DELAY_SECONDS", "0.5"))
    timeout_seconds = float(os.getenv("PAGCOR_REQUEST_TIMEOUT_SECONDS", "45"))
    fetch_attempts = max(1, int(os.getenv("PAGCOR_FETCH_ATTEMPTS", "3")))
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    DATA_DIR.mkdir(exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    PAGES_DIR.mkdir(exist_ok=True)
    PAGES_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (PAGES_DIR / ".nojekyll").write_text("", encoding="utf-8")

    session = requests.Session()
    session.headers.update({"User-Agent": "PAGCOR regulatory monitor/1.0"})

    checkpoint = load_checkpoint(max_resources)
    if checkpoint:
        checked_at = checkpoint.get("checked_at", checked_at)
        queue = list(checkpoint.get("queue", []))
        queued = set(checkpoint.get("queued", []))
        seen = set(checkpoint.get("seen", []))
        resources = {url: snapshot_from_dict(item) for url, item in checkpoint.get("resources", {}).items()}
        failures = list(checkpoint.get("failures", []))
        print(f"Resuming checkpoint: {len(resources)} resources, {len(queue)} queued")
    else:
        queue = [start_url]
        queued = {start_url}
        seen: set[str] = set()
        resources: dict[str, ResourceSnapshot] = {}
        failures: list[dict] = []

    queue = crawl_queue(session, queue, queued, seen, resources, failures, checked_at, timeout_seconds, delay, max_resources, fetch_attempts)

    # Per-URL retries (above) absorb single flaky requests; this handles the
    # case where a batch of URLs failed together (e.g. PAGCOR had a rough
    # minute). Only worth it if the main pass actually finished (queue empty -
    # if we hit max_resources instead, there's a bigger, different problem).
    sweep_rounds = max(0, int(os.getenv("PAGCOR_FINAL_SWEEP_ROUNDS", "1")))
    sweep_wait_seconds = float(os.getenv("PAGCOR_FINAL_SWEEP_WAIT_SECONDS", "30"))
    for sweep_num in range(1, sweep_rounds + 1):
        if not failures or queue:
            break
        retry_urls = [failure["url"] for failure in failures]
        print(f"Final sweep {sweep_num}/{sweep_rounds}: retrying {len(retry_urls)} still-failed resource(s) after {sweep_wait_seconds:.0f}s")
        time.sleep(sweep_wait_seconds)
        for url in retry_urls:
            seen.discard(url)
        queue = crawl_queue(session, retry_urls, queued, seen, resources, failures, checked_at, timeout_seconds, delay, max_resources, fetch_attempts)

    result = RunResult(resources=resources, failures=failures, max_resources_hit=bool(queue), checked_at=checked_at)
    if not result.max_resources_hit:
        clear_checkpoint()
    return result


def evidence_text(snapshot: ResourceSnapshot, change: dict | None = None) -> str:
    parts = [snapshot.url, snapshot.title, snapshot.kind]
    if change:
        for key in ("added_links", "removed_links"):
            for item in change.get(key, []):
                parts.extend([item.get("text", ""), item.get("url", "")])
        parts.extend(change.get("added_domains", []))
        parts.extend(change.get("removed_domains", []))
    return " ".join(parts).lower()


def classify_severity(snapshot: ResourceSnapshot, change_type: str, change: dict | None = None) -> tuple[str, tuple[str, str] | None]:
    """Return (severity, reason). reason is (zh, en) - the matched keyword/
    signal in both languages, so the report can show readers *why* something
    was classified the way it was, instead of just a bare label to trust
    blindly."""
    if change_type == "resurfaced":
        # This isn't a real editorial change (see build_recent_removals) - it's
        # our own crawl having briefly missed a resource that was always there.
        # Force it to Low regardless of what keywords the title matches, so it
        # never competes for attention with genuine new content.
        return "Low", None
    if change_type == "content_changed" and change and change.get("cosmetic_only"):
        # File got a new hash but the extractable text is identical - a
        # re-export/re-save, not a real addition or removal. Force Low so it
        # never outranks a genuine content change in the priority section.
        return "Low", None

    text = evidence_text(snapshot, change)
    # (matched-text pattern, (zh, en) reason shown to readers). Matching
    # stays on the English text actually used on PAGCOR's site; only the
    # displayed reason is translated, so non-engineers aren't shown raw
    # code-search strings like "domain-names" in the report.
    critical_patterns = [
        ("cancelled", ("業者資格被取消", "Operator eligibility cancelled")),
        ("reported websites", ("被回報的違規網站", "Reported non-compliant website")),
        ("counterfeit", ("偽造證書", "Counterfeit certificate")),
        ("registered brands", ("已登記品牌", "Registered brand")),
        ("domain names", ("網域名稱", "Domain name")),
        ("domain-names", ("網域名稱", "Domain name")),
        ("licensees", ("持牌業者", "Licensee")),
        ("accredited", ("認可資格", "Accreditation")),
        ("gaming system administrator", ("遊戲系統管理者", "Gaming system administrator")),
        ("regulatory framework", ("監理規範", "Regulatory framework")),
        ("amendment", ("修訂或修正案", "Amendment")),
    ]
    high_patterns = [
        ("announcement", ("公告", "Announcement")),
        ("notice", ("通知", "Notice")),
        ("application kit", ("申請文件套組", "Application kit")),
        ("requirements", ("申請條件", "Requirements")),
        ("schedule of fees", ("費率表", "Schedule of fees")),
        ("industry statistic", ("產業統計數據", "Industry statistics")),
        ("industry data", ("產業數據", "Industry data")),
        ("player exclusion", ("玩家排除名單", "Player exclusion list")),
    ]
    medium_patterns = [
        ("manual", ("操作手冊", "Operations manual")),
        ("guideline", ("指引", "Guideline")),
        ("form", ("申請表單", "Application form")),
        ("technical standard", ("技術標準", "Technical standard")),
        ("standard", ("標準", "Standard")),
    ]

    if change and (change.get("added_domains") or change.get("removed_domains")):
        return "Critical", ("網域清單變動", "Domain list change")
    for pattern, reason in critical_patterns:
        if pattern in text:
            return "Critical", reason
    for pattern, reason in high_patterns:
        if pattern in text:
            return "High", reason
    for pattern, reason in medium_patterns:
        if pattern in text:
            return "Medium", reason
    if snapshot.kind in {"pdf", "excel", "document", "csv"} and change_type in {"added", "content_changed"}:
        return "Medium", None
    return "Low", None


def severity_for(snapshot: ResourceSnapshot, change_type: str, change: dict | None = None) -> str:
    return classify_severity(snapshot, change_type, change)[0]


def business_impact(snapshot: ResourceSnapshot, severity: str, change_type: str, change: dict) -> tuple[str, str]:
    if change_type == "resurfaced":
        return "非官方異動，是本系統爬取不穩定造成的暫時性遺漏，已自動修正基準，不需要人工處理。", \
            "Not an official change - a temporary gap caused by unstable crawling on our end. The baseline has been auto-corrected; no action needed."
    text = evidence_text(snapshot, change)
    if "cancelled" in text:
        return "可能涉及業者資格取消或市場准入狀態變化，需優先人工複核。", \
            "May involve an operator's eligibility being cancelled or a change in market-access status - prioritize manual review."
    if "domain" in text or change.get("added_domains") or change.get("removed_domains"):
        return "可能涉及合法品牌、平台或網域名單變動，會直接影響市場與合規判讀。", \
            "May involve a change to the list of legitimate brands, platforms, or domains, directly affecting market and compliance assessment."
    if "registered brands" in text or "licensees" in text or "accredited" in text:
        return "可能涉及受監管業者、品牌或認可實體名單變動。", \
            "May involve a change to the list of regulated operators, brands, or accredited entities."
    if "regulatory framework" in text or "amendment" in text:
        return "可能涉及正式規範或修訂，需評估對營運與合規流程的影響。", \
            "May involve a formal regulation or amendment - assess the impact on operations and compliance processes."
    if "announcement" in text or "notice" in text:
        return "PAGCOR 發布公告或通知，需確認是否有市場或合規影響。", \
            "PAGCOR issued an announcement or notice - confirm whether it has market or compliance implications."
    if "industry" in text or "player exclusion" in text:
        return "產業統計或排除資料更新，可作為市場追蹤與報告來源。", \
            "Industry statistics or exclusion data updated - useful as a source for market tracking and reporting."
    if severity == "Medium":
        return "文件或流程資料有變動，建議排入例行檢視。", "Document or process information changed - schedule for routine review."
    return "低風險變動，已留痕供追溯。", "Low-risk change, logged for audit trail."



def readable_title(snapshot: ResourceSnapshot) -> str:
    """Turn a raw filename/URL slug into something a human can scan.

    Report titles were raw filenames (e.g. "Memorandum-on-Amendments-to-...pdf"),
    which forced readers to parse hyphens and extensions themselves. This is
    display-only — it never touches snapshot.title, so it can't affect hashing,
    diffing, or the stored baseline.
    """
    raw = snapshot.title or Path(urlparse(snapshot.url).path).name or snapshot.url
    name = unquote(raw)
    stem = Path(name).stem if snapshot.kind != "html" else name
    stem = stem.replace("_", " ").replace("-", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem or raw


def first_excerpt(snapshot: ResourceSnapshot, limit: int = 140) -> str:
    """First readable snippet of a resource's extracted text, for added/removed
    items that have no diff to show. Falls back to "" (caller decides what to
    say) when text extraction produced nothing, e.g. an un-OCR'd scanned PDF."""
    for block in snapshot.text_blocks or []:
        text = normalize_text(block.get("excerpt") or block.get("text") or "")
        if text:
            return short_text(text, limit)
    return ""


def short_text(text: str, limit: int = 260) -> str:
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


NUMBERED_ROW_SPLIT_RE = re.compile(r"(?=\b\d{1,4}\.\s)")
LEADING_NUMBER_RE = re.compile(r"^\d{1,4}\.\s*")


def split_numbered_rows(text: str) -> list[str]:
    """Split text into rows at numbered-list boundaries ("217. eCASINO GAMES
    ..."). Returns [] if the text doesn't actually look like a numbered list,
    so callers can fall back to plain word-level handling."""
    parts = [p.strip() for p in NUMBERED_ROW_SPLIT_RE.split(text) if p.strip()]
    if len(parts) < 3:
        return []
    numbered = sum(1 for p in parts if LEADING_NUMBER_RE.match(p))
    if numbered < len(parts) * 0.6:
        return []
    return parts


def diff_text_snippets(old_text: str, new_text: str, limit: int = 4) -> tuple[list[str], list[str]]:
    old_text, new_text = normalize_text(old_text), normalize_text(new_text)
    old_rows, new_rows = split_numbered_rows(old_text), split_numbered_rows(new_text)
    if old_rows and new_rows:
        # Numbered list (game lists, fee schedules, etc.): a single insertion
        # shifts every later row's number, which a word-level diff sees as
        # "everything after this point changed" - one giant unreadable blob.
        # Diffing row-by-row with the leading number stripped for comparison
        # (numbers are position, not identity) surfaces just the rows that
        # actually differ, each as its own short readable line.
        old_ids = [LEADING_NUMBER_RE.sub("", r) for r in old_rows]
        new_ids = [LEADING_NUMBER_RE.sub("", r) for r in new_rows]
        matcher = difflib.SequenceMatcher(None, old_ids, new_ids, autojunk=False)
        added: list[str] = []
        removed: list[str] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag in {"replace", "delete"}:
                for row in old_rows[i1:i2]:
                    if len(removed) < limit:
                        removed.append(short_text(row))
            if tag in {"replace", "insert"}:
                for row in new_rows[j1:j2]:
                    if len(added) < limit:
                        added.append(short_text(row))
            if len(added) >= limit and len(removed) >= limit:
                break
        return added, removed

    old_words = old_text.split()
    new_words = new_text.split()
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    added = []
    removed = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in {"replace", "delete"} and len(removed) < limit:
            snippet = short_text(" ".join(old_words[i1:i2]))
            if snippet:
                removed.append(snippet)
        if tag in {"replace", "insert"} and len(added) < limit:
            snippet = short_text(" ".join(new_words[j1:j2]))
            if snippet:
                added.append(snippet)
        if len(added) >= limit and len(removed) >= limit:
            break
    return added, removed


def preview_block_text(text: str, limit: int = 4) -> list[str]:
    """Preview lines for a whole new/removed block. Numbered lists show their
    first few rows individually (plus a "+N more" note) instead of one giant
    truncated run-on blob of the entire page."""
    text = normalize_text(text)
    rows = split_numbered_rows(text)
    if not rows:
        return [short_text(text)]
    shown = [short_text(r) for r in rows[:limit]]
    if len(rows) > limit:
        # Kept language-neutral (not translated zh/en) since this string is
        # inserted verbatim as one of the "added"/"removed" quoted-content
        # items, which render identically on both language sides.
        shown.append(f"... (+{len(rows) - limit} more)")
    return shown


def summarize_text_block_changes(old_blocks: list[dict], new_blocks: list[dict], limit: int = 8) -> list[dict]:
    if not old_blocks or not new_blocks:
        return []
    old_by_label = {block.get("label", ""): block for block in old_blocks}
    new_by_label = {block.get("label", ""): block for block in new_blocks}
    details: list[dict] = []

    for label in sorted(set(new_by_label) - set(old_by_label)):
        block = new_by_label[label]
        details.append({"label": label, "type": "頁面開頭", "added": preview_block_text(block.get("text", "")), "removed": []})
        if len(details) >= limit:
            return details

    for label in sorted(set(old_by_label) - set(new_by_label)):
        block = old_by_label[label]
        details.append({"label": label, "type": "頁面開頭", "added": [], "removed": preview_block_text(block.get("text", ""))})
        if len(details) >= limit:
            return details

    for label in sorted(set(old_by_label) & set(new_by_label)):
        old_block = old_by_label[label]
        new_block = new_by_label[label]
        if old_block.get("text_sha256") == new_block.get("text_sha256"):
            continue
        added, removed = diff_text_snippets(old_block.get("text", ""), new_block.get("text", ""))
        details.append({"label": label, "type": "頁面開頭", "added": added, "removed": removed})
        if len(details) >= limit:
            return details
    return details

RESURFACE_WINDOW_DAYS = 7


def parse_checked_at(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def is_recently_removed(recent_removals: dict, url: str, now: datetime, window_days: int = RESURFACE_WINDOW_DAYS) -> bool:
    removed_at = parse_checked_at(recent_removals.get(url, ""))
    if not removed_at:
        return False
    return timedelta(0) <= (now - removed_at) <= timedelta(days=window_days)


def build_recent_removals(previous: dict, changes: list[dict], checked_at: str) -> dict:
    """Roll forward the "recently removed" ledger used to detect resurfaced resources.

    Keeps entries within RESURFACE_WINDOW_DAYS, drops URLs that resurfaced this
    run (they've served their purpose), and records this run's new removals.
    """
    now = parse_checked_at(checked_at) or datetime.now()
    updated: dict[str, str] = {}
    for url, ts in previous.get("recent_removals", {}).items():
        removed_at = parse_checked_at(ts)
        if removed_at and (now - removed_at) <= timedelta(days=RESURFACE_WINDOW_DAYS):
            updated[url] = ts
    for change in changes:
        if change["type"] == "removed":
            updated[change["url"]] = checked_at
        elif change["type"] == "resurfaced":
            updated.pop(change["url"], None)
    return updated


def crawl_incomplete(previous: dict, current: RunResult, threshold: float = 0.85, min_baseline: int = 20) -> bool:
    """True when this run fetched far fewer resources than the last baseline.

    A steep drop almost always means the crawl was cut short by network
    failures (e.g. PAGCOR site timeouts), not that resources were actually
    removed. min_baseline avoids tripping this on the very first small runs.
    """
    prev_count = len(previous.get("resources", {}))
    if prev_count < min_baseline:
        return False
    return len(current.resources) < threshold * prev_count


def compare_snapshots(
    previous: dict,
    current: RunResult,
    skip_removed_detection: bool = False,
    recent_removals: dict | None = None,
) -> list[dict]:
    prev_resources = previous.get("resources", {})
    recent_removals = recent_removals or {}
    now = parse_checked_at(current.checked_at) or datetime.now()
    changes: list[dict] = []

    for url, snapshot in current.resources.items():
        old = prev_resources.get(url)
        if not old:
            if is_recently_removed(recent_removals, url, now):
                changes.append({"type": "resurfaced", "url": url, "snapshot": snapshot, "removed_at": recent_removals[url]})
            else:
                changes.append({"type": "added", "url": url, "snapshot": snapshot})
            continue

        binary_changed = old.get("sha256") != snapshot.sha256
        text_changed = old.get("text_sha256") != snapshot.text_sha256
        content_changed = text_changed if snapshot.kind == "html" else (binary_changed or text_changed)
        # A file can get a new hash (re-exported, re-compressed, a server-side
        # touch) while its actual extractable text is byte-identical. That is
        # not a real content change - it's noise the reader explicitly said
        # they don't want competing for attention with real additions/removals.
        cosmetic_only = content_changed and snapshot.kind != "html" and binary_changed and not text_changed

        if content_changed:
            old_size = int(old.get("size") or 0)
            changes.append(
                {
                    "type": "content_changed",
                    "url": url,
                    "snapshot": snapshot,
                    "old_size": old_size,
                    "new_size": snapshot.size,
                    "size_delta": snapshot.size - old_size,
                    "binary_changed": binary_changed,
                    "text_changed": text_changed,
                    "cosmetic_only": cosmetic_only,
                    "detail_changes": summarize_text_block_changes(old.get("text_blocks", []), snapshot.text_blocks),
                    "old_local_path": old.get("local_path", ""),
                    "added_domains": sorted(set(snapshot.domains) - set(old.get("domains", []))),
                    "removed_domains": sorted(set(old.get("domains", [])) - set(snapshot.domains)),
                    "added_dates": sorted(set(snapshot.dates) - set(old.get("dates", []))),
                    "removed_dates": sorted(set(old.get("dates", [])) - set(snapshot.dates)),
                }
            )

        if snapshot.kind == "html" and old.get("links") != snapshot.links:
            old_links = {item["url"]: item for item in old.get("links", [])}
            new_links = {item["url"]: item for item in snapshot.links}
            added_links = [new_links[u] for u in sorted(set(new_links) - set(old_links))]
            removed_links = [old_links[u] for u in sorted(set(old_links) - set(new_links))]
            # If the link set (by URL) is unchanged, the only difference was
            # link text/ordering - nothing the reader asked to be told about.
            if added_links or removed_links:
                changes.append(
                    {
                        "type": "links_changed",
                        "url": url,
                        "snapshot": snapshot,
                        "added_links": added_links,
                        "removed_links": removed_links,
                    }
                )

    if not skip_removed_detection:
        for url, old in prev_resources.items():
            if url not in current.resources:
                old_snapshot = snapshot_from_dict(old)
                changes.append({"type": "removed", "url": url, "snapshot": old_snapshot})

    for failure in current.failures:
        if failure["url"] in prev_resources and failure["url"] not in current.resources:
            old_snapshot = snapshot_from_dict(prev_resources[failure["url"]])
            changes.append({"type": "fetch_failed", "url": failure["url"], "snapshot": old_snapshot, "error": failure["error"]})

    for change in changes:
        severity, reason = classify_severity(change["snapshot"], change["type"], change)
        change["severity"] = severity
        change["severity_reason"] = reason
        change["impact"] = business_impact(change["snapshot"], severity, change["type"], change)
    return changes


def format_list(items: list[str], limit: int = 12, lang: str = "zh") -> str:
    if not items:
        return "無" if lang == "zh" else "None"
    shown = items[:limit]
    if len(items) > limit:
        extra = len(items) - limit
        suffix = f"，另有 {extra} 項" if lang == "zh" else f", plus {extra} more"
    else:
        suffix = ""
    return "、".join(shown) + suffix


def change_label(change_type: str) -> tuple[str, str]:
    return {
        "added": ("新增資源", "New resource"),
        "removed": ("移除資源", "Removed resource"),
        "resurfaced": ("重新確認（近期曾消失）", "Reconfirmed (briefly missing recently)"),
        "content_changed": ("內容更新", "Content updated"),
        "links_changed": ("連結清單更新", "Link list updated"),
        "fetch_failed": ("抓取失敗", "Fetch failed"),
    }.get(change_type, (change_type, change_type))


def format_bytes(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.2f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} bytes"


def plain_change_summary(change: dict) -> tuple[str, str]:
    change_type = change["type"]
    if change_type == "added":
        base_zh, base_en = "這是本次第一次被監控到的新資源。", "This is the first time this resource has been monitored. "
        excerpt = first_excerpt(change["snapshot"])
        if excerpt:
            return f"{base_zh}內容開頭：{excerpt}", f"{base_en}Content starts with: {excerpt}"
        return f"{base_zh}請確認它是否為新的公告、表單、名單或統計資料。", \
            f"{base_en}Please confirm whether this is a new announcement, form, list, or statistical report."
    if change_type == "removed":
        base_zh = "這個資源本次已不在可抓取清單中。可能是官方移除、改名、搬移，或來源頁連結被刪除。"
        base_en = "This resource is no longer reachable. It may have been officially removed, renamed, moved, or its source link deleted."
        excerpt = first_excerpt(change["snapshot"])
        if excerpt:
            return f"{base_zh}移除前的內容開頭：{excerpt}", f"{base_en} Content before removal started with: {excerpt}"
        return base_zh, base_en
    if change_type == "resurfaced":
        removed_at = change.get("removed_at", "近期")
        return (
            f"此資源在 {removed_at} 前後曾短暫從可抓取清單中消失，本次重新確認存在。"
            "研判為爬取不穩定所致（例如網站逾時），並非官方異動；系統已自動修正基準，不需要特別處理。"
        ), (
            f"This resource briefly disappeared around {removed_at} and has now been reconfirmed. "
            "Likely caused by unstable crawling (e.g. site timeouts), not an official change; "
            "the baseline has been auto-corrected - no action needed."
        )
    if change_type == "fetch_failed":
        return "這次無法成功讀取既有資源，因此不代表內容真的改變；需要下次重跑或人工開啟確認。", \
            "This resource could not be fetched this time, so this does not necessarily mean its content changed; needs a re-run or manual check."
    if change_type == "links_changed":
        added = len(change.get("added_links", []))
        removed = len(change.get("removed_links", []))
        return f"來源頁面的連結清單改變：新增 {added} 個連結、移除 {removed} 個連結。", \
            f"The link list on the source page changed: {added} link(s) added, {removed} link(s) removed."
    if change_type == "content_changed":
        if change.get("cosmetic_only"):
            return "此檔案已重新產生（例如官方重新輸出或伺服器端調整），但實際文字內容沒有變化，可忽略。", \
                "This file was regenerated (e.g. re-exported officially or a server-side touch), but the actual text content is unchanged - safe to ignore."
        parts_zh, parts_en = [], []
        if change.get("binary_changed"):
            parts_zh.append("檔案本身有更新")
            parts_en.append("the file itself was updated")
        if change.get("text_changed"):
            parts_zh.append("文字內容有變化")
            parts_en.append("text content changed")
        if change.get("size_delta"):
            direction_zh = "增加" if change["size_delta"] > 0 else "減少"
            direction_en = "increased" if change["size_delta"] > 0 else "decreased"
            size_str = format_bytes(abs(change["size_delta"]))
            parts_zh.append(f"大小{direction_zh} {size_str}")
            parts_en.append(f"size {direction_en} by {size_str}")
        if change.get("added_dates") or change.get("removed_dates"):
            parts_zh.append("日期文字有變化")
            parts_en.append("date text changed")
        if change.get("added_domains") or change.get("removed_domains"):
            parts_zh.append("網域文字有變化")
            parts_en.append("domain text changed")
        if parts_zh:
            return "、".join(parts_zh) + "。", ", ".join(parts_en).capitalize() + "."
        return "系統偵測到內容有變化，但沒有抓到可以分類說明的日期、網域或連結差異，建議直接打開來源確認。", \
            "The system detected a content change but found no classifiable date, domain, or link differences - recommend opening the source directly to confirm."
    return "系統偵測到變動，請依來源內容人工複核。", "The system detected a change - please review the source content manually."
COMPACT_CHANGE_TYPES = {"added", "removed", "resurfaced", "fetch_failed"}

DOCUMENT_KIND_LABELS = {"pdf": ("PDF", "PDF"), "excel": ("Excel", "Excel"), "document": ("Word/文件", "Word document"), "csv": ("CSV", "CSV")}


def data_location_label(snapshot: ResourceSnapshot) -> tuple[str, str]:
    """Plain-language answer to "where would I actually see this change?" -
    the HTML page itself, or a file the reader has to open separately."""
    if snapshot.kind == "html":
        return "前端網頁 — 瀏覽器打開網址就看得到", "Front-end webpage - visible directly by opening the URL in a browser"
    kind_zh, kind_en = DOCUMENT_KIND_LABELS.get(snapshot.kind, (snapshot.kind, snapshot.kind))
    return (
        f"後端文件（{kind_zh}）— 需點開檔案才看得到最新內容",
        f"Back-end file ({kind_en}) - must open the file to see the latest content",
    )


def location_summary(change: dict, limit: int = 3) -> tuple[str, str]:
    """"(N locations changed: ...)" suffix for the collapsed item title, so
    readers see how many places in a file changed without expanding it."""
    if change.get("type") != "content_changed":
        return "", ""
    detail_changes = change.get("detail_changes", [])
    if not detail_changes:
        return "", ""
    labels = [d.get("label", "未知位置") for d in detail_changes]
    if len(labels) <= limit:
        shown = "、".join(labels)
        return f"（共 {len(labels)} 處異動：{shown}）", f" ({len(labels)} location(s) changed: {shown})"
    shown = "、".join(labels[:limit])
    return f"（共 {len(labels)} 處異動，含 {shown} 等）", f" ({len(labels)} location(s) changed, including {shown}, etc.)"


def render_change(lines: list[tuple[str, str]], idx: int, change: dict, include_details: bool = True) -> None:
    """Append (zh, en) line pairs describing one change. Every line carries
    the same Markdown-structural prefix in both languages (headings, "- "
    bullets, blank lines) so markdown_to_basic_html can drive HTML structure
    off either side while showing both languages in the same elements."""
    snapshot = change["snapshot"]
    title = readable_title(snapshot)
    change_type = change["type"]
    label_zh, label_en = change_label(change_type)
    loc_zh, loc_en = data_location_label(snapshot)

    is_cosmetic = change_type == "content_changed" and change.get("cosmetic_only")
    if change_type in COMPACT_CHANGE_TYPES or is_cosmetic:
        # These types have no content diff worth showing (the resource simply
        # appeared, vanished, couldn't be fetched, or - for is_cosmetic - only
        # got a new file hash with no real text change), so a full 5-line
        # block per item is pure repetition. One or two lines is enough.
        detail_zh, detail_en = plain_change_summary(change)
        if change_type == "fetch_failed":
            error = change.get("error", "")
            detail_zh = f"{detail_zh}（錯誤：{error}）"
            detail_en = f"{detail_en} (Error: {error})"
        if is_cosmetic:
            label_zh, label_en = "技術性更新（內容未變）", "Technical update (content unchanged)"
        lines += [
            (f"### {idx}. [{change['severity']}] {label_zh}：{title}", f"### {idx}. [{change['severity']}] {label_en}: {title}"),
            ("", ""),
            (f"- {detail_zh}", f"- {detail_en}"),
            (f"- 資料位置：{loc_zh}", f"- Where to find it: {loc_en}"),
        ]
        if change.get("severity_reason"):
            reason_zh, reason_en = change["severity_reason"]
            lines.append((f"- 分類依據：內容涉及「{reason_zh}」", f'- Classification basis: involves "{reason_en}"'))
        lines.append((f"- 來源：{snapshot.url}", f"- Source: {snapshot.url}"))
        lines.append(("", ""))
        return

    summary_zh, summary_en = plain_change_summary(change)
    impact_zh, impact_en = change["impact"]
    loc_summary_zh, loc_summary_en = location_summary(change)
    lines += [
        (f"### {idx}. [{change['severity']}] {title}{loc_summary_zh}", f"### {idx}. [{change['severity']}] {title}{loc_summary_en}"),
        ("", ""),
        (f"- 變動：{label_zh}", f"- Change: {label_en}"),
        (f"- 實際狀況：{summary_zh}", f"- Details: {summary_en}"),
        (f"- 來源：{snapshot.url}", f"- Source: {snapshot.url}"),
        (f"- 資料位置：{loc_zh}", f"- Where to find it: {loc_en}"),
        (f"- 可能影響：{impact_zh}", f"- Possible impact: {impact_en}"),
    ]
    if change.get("severity_reason"):
        reason_zh, reason_en = change["severity_reason"]
        lines.append((f"- 分類依據：內容涉及「{reason_zh}」", f'- Classification basis: involves "{reason_en}"'))
    if change["type"] == "content_changed":
        old_size, new_size, size_delta = change.get("old_size", 0), change.get("new_size", 0), change.get("size_delta", 0)
        lines.append((
            f"- 檔案大小：原本 {format_bytes(old_size)}，現在 {format_bytes(new_size)}，差異 {size_delta:+,} bytes",
            f"- File size: was {format_bytes(old_size)}, now {format_bytes(new_size)}, change {size_delta:+,} bytes",
        ))
        binary_state_zh = "檔案本身有更新" if change.get("binary_changed") else "檔案本身沒有變化"
        binary_state_en = "File itself updated" if change.get("binary_changed") else "File itself unchanged"
        text_state_zh = "文字內容有變化" if change.get("text_changed") else "文字內容沒有變化"
        text_state_en = "text content changed" if change.get("text_changed") else "text content unchanged"
        lines.append((f"- 檔案狀態：{binary_state_zh}；{text_state_zh}", f"- File status: {binary_state_en}; {text_state_en}"))
        if change.get("added_dates") or change.get("removed_dates"):
            added_d, removed_d = change.get("added_dates", []), change.get("removed_dates", [])
            lines.append((
                f"- 日期變動：新增 {format_list(added_d)}；移除 {format_list(removed_d)}",
                f"- Date changes: added {format_list(added_d, lang='en')}; removed {format_list(removed_d, lang='en')}",
            ))
        else:
            lines.append(("- 日期變動：未偵測到新增或移除的日期文字", "- Date changes: no added or removed dates detected"))
        if change.get("added_domains") or change.get("removed_domains"):
            added_dom, removed_dom = change.get("added_domains", []), change.get("removed_domains", [])
            lines.append((
                f"- 網域變動：新增 {format_list(added_dom)}；移除 {format_list(removed_dom)}",
                f"- Domain changes: added {format_list(added_dom, lang='en')}; removed {format_list(removed_dom, lang='en')}",
            ))
        else:
            lines.append(("- 網域變動：未偵測到新增或移除的網域文字", "- Domain changes: no added or removed domains detected"))
        detail_changes = change.get("detail_changes", [])
        if detail_changes:
            lines.append(("- 變動位置與文字片段：", "- Change locations and text snippets:"))
            for detail in detail_changes:
                detail_label = detail.get("label", "未知位置")
                detail_type_zh = detail.get("type", "文字變更")
                detail_type_en = "page start" if detail_type_zh == "頁面開頭" else "text change"
                lines.append((f"  - {detail_label}：{detail_type_zh}", f"  - {detail_label}: {detail_type_en}"))
                for item in detail.get("added", [])[:4]:
                    lines.append((f"    - 新增：{item}", f"    - Added: {item}"))
                for item in detail.get("removed", [])[:4]:
                    lines.append((f"    - 移除：{item}", f"    - Removed: {item}"))
                page_match = PAGE_LABEL_RE.match(detail.get("label", ""))
                if page_match and snapshot.kind == "pdf":
                    page_number = int(page_match.group(1))
                    old_local_path = change.get("old_local_path")
                    if old_local_path and snapshot.local_path:
                        old_thumb = render_pdf_page_thumbnail(
                            ROOT / old_local_path, page_number,
                            highlight_texts=detail.get("removed"), highlight_color=REMOVED_HIGHLIGHT_COLOR,
                        )
                        if old_thumb:
                            # Visible caption, not just alt/title (which most readers never see) -
                            # states which image this is (before/after) and what the box color means.
                            lines.append((
                                f"🔴 第 {page_number} 頁－修改前：紅框標示即將被移除的內容",
                                f"🔴 Page {page_number} - Before: red box marks content that was removed",
                            ))
                            alt = f"Page {page_number} - before"
                            lines.append((f"![{alt}]({old_thumb})", f"![{alt}]({old_thumb})"))
                    if snapshot.local_path:
                        new_thumb = render_pdf_page_thumbnail(
                            ROOT / snapshot.local_path, page_number,
                            highlight_texts=detail.get("added"), highlight_color=ADDED_HIGHLIGHT_COLOR,
                        )
                        if new_thumb:
                            lines.append((
                                f"🟢 第 {page_number} 頁－修改後：綠框標示新增的內容",
                                f"🟢 Page {page_number} - After: green box marks newly added content",
                            ))
                            alt = f"Page {page_number} - after"
                            lines.append((f"![{alt}]({new_thumb})", f"![{alt}]({new_thumb})"))
        else:
            lines.append((
                "- 變動位置與文字片段：目前基準沒有逐頁/分段文字，或此檔案無法抽取文字；本次已建立詳細基準，後續變更會顯示位置。",
                "- Change locations and text snippets: no page/section-level text in the current baseline, or this file's text couldn't be extracted; a detailed baseline has now been established so future changes will show their location.",
            ))
        lines.append((
            "- 判讀方式：如果只有檔案大小改變、但文字內容沒變，很可能只是官方重新輸出或壓縮同一份文件，不一定代表內容有實質修改；仍建議打開來源確認版面與內容。",
            "- How to read this: if only the file size changed but the text content didn't, it's likely just an official re-export or re-compression of the same document, not necessarily a substantive change; still recommended to open the source to confirm layout and content.",
        ))
    if change["type"] == "links_changed" and include_details:
        added = [item["text"] or item["url"] for item in change.get("added_links", [])]
        removed = [item["text"] or item["url"] for item in change.get("removed_links", [])]
        lines.append((f"- 新增連結：{format_list(added)}", f"- Links added: {format_list(added, lang='en')}"))
        lines.append((f"- 移除連結：{format_list(removed)}", f"- Links removed: {format_list(removed, lang='en')}"))
    lines.append(("", ""))



def load_history() -> list[dict]:
    if not HISTORY_JSON_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_history_entry(now: datetime, run: RunResult, changes: list[dict], counts: dict) -> list[dict]:
    """Record this run in the public history log (docs/history.json), so the
    history page can list every past report, newest first, each linking to
    its own permanent archived snapshot."""
    history = load_history()
    entry = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "file": f"reports/{now.strftime('%Y-%m-%d_%H-%M-%S')}.html",
        "resources": len(run.resources),
        "changes": len(changes),
        "critical": counts.get("Critical", 0),
        "high": counts.get("High", 0),
        "medium": counts.get("Medium", 0),
        "low": counts.get("Low", 0),
    }
    history = [e for e in history if e.get("timestamp") != entry["timestamp"]]
    history.append(entry)
    history.sort(key=lambda e: e["timestamp"])
    HISTORY_JSON_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    return history


def render_history_html(history: list[dict]) -> str:
    rows = []
    for entry in sorted(history, key=lambda e: e["timestamp"], reverse=True):
        badge_parts = []
        for key, label in (("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low")):
            if entry.get(key):
                badge_parts.append(f"{label} {entry[key]}")
        badges = html.escape("、".join(badge_parts)) if badge_parts else dual_span("無變動", "No changes")
        rows.append(
            f'<tr><td><a href="{html.escape(entry.get("file", ""))}">{html.escape(entry.get("timestamp", ""))}</a></td>'
            f'<td>{entry.get("resources", "-")}</td>'
            f'<td>{entry.get("changes", 0)}</td>'
            f'<td>{badges}</td></tr>'
        )
    table_rows = "\n".join(rows) if rows else f'<tr><td colspan="4">{dual_span("尚無歷史紀錄", "No history yet")}</td></tr>'
    return """<!doctype html>
<html lang=\"zh-Hant\">
<head>
""" + LANG_TOGGLE_SCRIPT + f"""
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>PAGCOR Regulatory Monitor - History</title>
<style>
body{{font-family:Arial,'Microsoft JhengHei',sans-serif;line-height:1.6;margin:32px;max-width:900px;color:#1f2937;background:#f8fafc}}
h1{{font-size:26px;color:#111827}}
a{{color:#1d4ed8;text-decoration:none}}a:hover{{text-decoration:underline}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid #e5e7eb;font-size:14px}}
th{{background:#f1f5f9;font-weight:600}}
tr:hover td{{background:#f8fafc}}
.back{{display:inline-block;margin-bottom:16px}}
{LANG_TOGGLE_STYLE}
</style>
</head>
<body>
<h1>{dual_span("PAGCOR Regulatory Monitor - 歷史紀錄", "PAGCOR Regulatory Monitor - History")} <button id="lang-toggle" type="button">EN</button></h1>
<p class=\"back\"><a href=\"index.html\">&larr; {dual_span("回到最新報告", "Back to latest report")}</a></p>
<table>
<thead><tr><th>{dual_span("檢查時間", "Checked At")}</th><th>{dual_span("監控資源數", "Resources")}</th><th>{dual_span("變動總數", "Changes")}</th><th>{dual_span("分級摘要", "Severity Summary")}</th></tr></thead>
<tbody>
""" + table_rows + """
</tbody>
</table>
""" + LANG_TOGGLE_INIT_JS + """
</body>
</html>
"""


COLLAPSIBLE_REPORT_SECTIONS = {"例行檢視", "低風險留痕"}

LANG_TOGGLE_SCRIPT = """<script>
(function(){
  var saved = localStorage.getItem('pagcor-lang') || 'zh';
  document.documentElement.setAttribute('data-lang', saved);
})();
</script>"""

LANG_TOGGLE_STYLE = """
.lang-en{display:none}
:root[data-lang="en"] .lang-en{display:inline}
:root[data-lang="en"] .lang-zh{display:none}
#lang-toggle{cursor:pointer;border:1px solid #d1d5db;background:#fff;border-radius:6px;padding:2px 10px;font-size:13px;margin-left:4px}
#lang-toggle:hover{background:#f1f5f9}
"""

LANG_TOGGLE_INIT_JS = """<script>
(function(){
  var btn = document.getElementById('lang-toggle');
  if (!btn) return;
  function label(){ return document.documentElement.getAttribute('data-lang') === 'en' ? '中文' : 'EN'; }
  btn.textContent = label();
  btn.addEventListener('click', function(){
    var next = document.documentElement.getAttribute('data-lang') === 'en' ? 'zh' : 'en';
    document.documentElement.setAttribute('data-lang', next);
    localStorage.setItem('pagcor-lang', next);
    btn.textContent = label();
  });
})();
</script>"""


def dual_span(zh: str, en: str) -> str:
    return f'<span class="lang-zh">{html.escape(zh)}</span><span class="lang-en">{html.escape(en)}</span>'


def markdown_to_basic_html(lines: list[tuple[str, str]]) -> str:
    """Render (zh, en) line pairs as HTML with a quick-nav bar, collapsible
    sections, and a language toggle. Every line's zh and en side must share
    the same Markdown-structural prefix (#, ##, ###, -, blank) - structure is
    driven off the zh side, both languages render into the same elements via
    dual_span, and a data-lang attribute on <html> shows/hides one side.

    Every ### item defaults to collapsed (just the [Severity] title shows) and
    the Medium/Low sections collapse as a whole, so a 76-change report opens as
    a scannable table of contents instead of one long scroll.
    """
    raw_lines = [(zh.rstrip(), en.rstrip()) for zh, en in lines]

    # Pass 1: find ## section headings and count the ### items under each, for the nav bar.
    nav_sections: list[tuple[str, str, str, int]] = []
    current_heading: tuple[str, str] | None = None
    current_count = 0

    def flush_nav() -> None:
        if current_heading is not None:
            nav_sections.append((f"sec-{len(nav_sections)}", current_heading[0], current_heading[1], current_count))

    for zh, en in raw_lines:
        if zh.startswith("## "):
            flush_nav()
            current_heading = (zh[3:], en[3:])
            current_count = 0
        elif zh.startswith("### ") and current_heading is not None:
            current_count += 1
    flush_nav()
    section_ids = {heading_zh: sec_id for sec_id, heading_zh, _, _ in nav_sections}

    # Pass 2: convert to HTML, wrapping ### items and Medium/Low sections in <details>.
    body_lines: list[str] = []
    in_list = False
    item_open = False
    section_open = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            body_lines.append("</ul>")
            in_list = False

    def close_item() -> None:
        nonlocal item_open
        close_list()
        if item_open:
            body_lines.append("</details>")
            item_open = False

    def close_section() -> None:
        nonlocal section_open
        close_item()
        if section_open:
            body_lines.append("</details>")
            section_open = False

    for zh, en in raw_lines:
        if not zh:
            # Note: a blank line always follows "### heading" (standard Markdown),
            # so closing the item here would end it before its bullet list ever
            # renders. Items only close at the next heading / end of document.
            close_list()
            continue
        if zh.startswith("# "):
            close_section()
            body_lines.append(f"<h1>{dual_span(zh[2:], en[2:])}</h1>")
            pages_url = os.getenv("GITHUB_PAGES_URL", "").strip()
            history_href = f"{pages_url.rstrip('/')}/history.html" if pages_url else "history.html"
            nav_links = [f'<a href="{history_href}">📜 {dual_span("歷史紀錄", "History")}</a>']
            nav_links += [
                f'<a href="#{sec_id}">{dual_span(heading_zh, heading_en)}{f" ({count})" if count else ""}</a>'
                for sec_id, heading_zh, heading_en, count in nav_sections
            ]
            body_lines.append(
                f'<nav class="quicknav">{" · ".join(nav_links)} · <button id="lang-toggle" type="button">EN</button></nav>'
            )
        elif zh.startswith("## "):
            close_section()
            heading_zh, heading_en = zh[3:], en[3:]
            sec_id = section_ids.get(heading_zh, "")
            if heading_zh in COLLAPSIBLE_REPORT_SECTIONS:
                body_lines.append(f'<details id="{sec_id}" class="section"><summary><h2>{dual_span(heading_zh, heading_en)}</h2></summary>')
                section_open = True
            else:
                body_lines.append(f'<h2 id="{sec_id}">{dual_span(heading_zh, heading_en)}</h2>')
        elif zh.startswith("### "):
            close_item()
            body_lines.append(f'<details class="item"><summary>{dual_span(zh[4:], en[4:])}</summary>')
            item_open = True
        elif zh.startswith("- "):
            if not in_list:
                body_lines.append("<ul>")
                in_list = True
            body_lines.append(f"<li>{dual_span(zh[2:], en[2:])}</li>")
        elif MARKDOWN_IMAGE_RE.match(zh):
            close_list()
            alt, src = MARKDOWN_IMAGE_RE.match(zh).groups()
            # src is a data: URI we generated ourselves (base64 alphabet only),
            # safe to place unescaped; alt/title (bilingual already) still escaped.
            body_lines.append(f'<img class="page-thumb" alt="{html.escape(alt)}" title="{html.escape(alt)}" src="{src}" loading="lazy">')
        else:
            close_list()
            body_lines.append(f"<p>{dual_span(zh, en)}</p>")
    close_section()

    return """<!doctype html>
<html lang=\"zh-Hant\">
<head>
""" + LANG_TOGGLE_SCRIPT + """
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>PAGCOR Regulatory Daily Monitor</title>
<style>
body{font-family:Arial,'Microsoft JhengHei',sans-serif;line-height:1.6;margin:32px;max-width:1100px;color:#1f2937;background:#f8fafc}
h1,h2,h3{line-height:1.25;color:#111827}h1{font-size:28px}h2{font-size:22px;margin-top:28px;border-bottom:1px solid #d1d5db;padding-bottom:6px;display:inline-block}h3{font-size:18px;margin-top:22px}
ul{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px 20px 14px 34px}li{margin:5px 0}p{background:#fff;border-left:4px solid #9ca3af;padding:10px 14px}code{background:#e5e7eb;padding:2px 5px;border-radius:4px}
nav.quicknav{position:sticky;top:0;background:#f8fafc;padding:10px 0;margin-bottom:8px;border-bottom:1px solid #d1d5db;font-size:14px}
nav.quicknav a{color:#1d4ed8;text-decoration:none;margin-right:4px}nav.quicknav a:hover{text-decoration:underline}
details.item{background:#fff;border:1px solid #e5e7eb;border-radius:8px;margin-top:10px;padding:4px 14px}
details.item summary{cursor:pointer;font-weight:600;padding:8px 0;list-style:revert}
details.item[open] summary{border-bottom:1px solid #e5e7eb;margin-bottom:8px}
details.section summary{cursor:pointer;list-style:none}
details.section summary h2{display:inline}
details.section summary::before{content:"▶ ";font-size:14px;color:#6b7280}
details.section[open] summary::before{content:"▼ "}
img.page-thumb{display:block;max-width:100%;height:auto;margin:8px 0;border:1px solid #d1d5db;border-radius:6px}
""" + LANG_TOGGLE_STYLE + """
</style>
</head>
<body>
""" + "\n".join(body_lines) + "\n" + LANG_TOGGLE_INIT_JS + "\n</body>\n</html>\n"
def render_reports(changes: list[dict], run: RunResult, shortfall: bool = False, prev_resource_count: int = 0) -> Path:
    now = datetime.now()
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    for change in changes:
        counts[change["severity"]] += 1
    ordered = sorted(changes, key=lambda c: (SEVERITY_ORDER[c["severity"]], c["snapshot"].title, c["url"]))

    lines: list[tuple[str, str]] = [
        ("# PAGCOR Regulatory Daily Monitor", "# PAGCOR Regulatory Daily Monitor"),
        ("", ""),
        (f"- 檢查時間：{now.strftime('%Y-%m-%d %H:%M:%S')}", f"- Checked at: {now.strftime('%Y-%m-%d %H:%M:%S')}"),
        (f"- 監控資源數：{len(run.resources)}", f"- Resources monitored: {len(run.resources)}"),
        (f"- 抓取失敗數：{len(run.failures)}", f"- Fetch failures: {len(run.failures)}"),
        (f"- 達到資源上限：{'是' if run.max_resources_hit else '否'}", f"- Resource limit reached: {'Yes' if run.max_resources_hit else 'No'}"),
        (f"- 變動總數：{len(changes)}", f"- Total changes: {len(changes)}"),
        ("", ""),
        ("## 分級摘要", "## Severity Summary"),
        ("", ""),
        (f"- Critical: {counts['Critical']}", f"- Critical: {counts['Critical']}"),
        (f"- High: {counts['High']}", f"- High: {counts['High']}"),
        (f"- Medium: {counts['Medium']}", f"- Medium: {counts['Medium']}"),
        (f"- Low: {counts['Low']}", f"- Low: {counts['Low']}"),
        ("", ""),
    ]
    type_counts = Counter(c["type"] for c in changes)
    if type_counts:
        sorted_types = sorted(type_counts.items(), key=lambda item: -item[1])
        type_summary_zh = "、".join(f"{change_label(t)[0]} {n}" for t, n in sorted_types)
        type_summary_en = ", ".join(f"{change_label(t)[1]} {n}" for t, n in sorted_types)
        lines += [("## 變動類型分布", "## Change Type Distribution"), ("", ""), (f"- {type_summary_zh}", f"- {type_summary_en}"), ("", "")]

    notices: list[tuple[str, str]] = []
    if run.max_resources_hit:
        notices.append((
            "本次監控的資源數量達到系統設定的上限，可能還有頁面沒抓完。系統不會在這種情況下判定舊資源已被移除，以避免誤報。若要完整檢查全站是否有資源被移除，請提高監控上限後重新執行。",
            "This run reached the configured resource limit, so some pages may not have been crawled yet. The system will not treat missing resources as removed in this case, to avoid false alarms. To fully check whether any resources were removed site-wide, raise the monitoring limit and re-run.",
        ))
    if shortfall:
        notices.append((
            f"本次抓到的資源數（{len(run.resources)}）明顯低於上次基準（{prev_resource_count}），疑似爬取不完整（可能是網站逾時或連線問題），而不是真的有資源被移除。系統這次不會判定短少的資源為「已移除」，也不會更新比對基準，等下次抓齊後才會正式比對。",
            f"This run only found {len(run.resources)} resources, well below the last baseline of {prev_resource_count} - the crawl looks incomplete (likely a site timeout or connection issue), not a real removal. The system will not mark the missing resources as \"removed\" this time, nor update the comparison baseline, until a future run completes a full crawl.",
        ))
    anomaly_types = [
        (t, n) for t, n in type_counts.items()
        if t != "resurfaced" and n >= 20 and len(run.resources) > 0 and n / len(run.resources) >= 0.05
    ]
    if anomaly_types:
        sorted_anomalies = sorted(anomaly_types, key=lambda item: -item[1])
        parts_zh = "、".join(f"{change_label(t)[0]}（{n} 筆）" for t, n in sorted_anomalies)
        parts_en = ", ".join(f"{change_label(t)[1]} ({n})" for t, n in sorted_anomalies)
        notices.append((
            f"本次{parts_zh}的數量明顯偏高，建議先確認是否為爬取異常造成的一次性現象，而非真實大量異動，再逐筆檢視。",
            f"The count of {parts_en} this run is unusually high - recommend first confirming whether this is a one-off crawl anomaly rather than a genuine wave of changes, before reviewing item by item.",
        ))
    if notices:
        lines += [("## 注意", "## Notice"), ("", "")]
        for notice_zh, notice_en in notices:
            lines.append((f"- {notice_zh}", f"- {notice_en}"))
        lines.append(("", ""))

    urgent = [c for c in ordered if c["severity"] in {"Critical", "High"}]
    if urgent:
        lines += [
            ("## 需要優先閱讀", "## Priority Review"), ("", ""),
            ("建議：以下項目請優先人工複核來源文件。", "Recommendation: please prioritize manual review of the source documents for the items below."), ("", ""),
        ]
        for idx, change in enumerate(urgent, 1):
            render_change(lines, idx, change)

    medium = [c for c in ordered if c["severity"] == "Medium"]
    low = [c for c in ordered if c["severity"] == "Low"]
    if medium:
        lines += [
            ("## 例行檢視", "## Routine Review"), ("", ""),
            ("建議：非急迫，排入例行檢視即可。", "Recommendation: not urgent, schedule for routine review."), ("", ""),
        ]
        for idx, change in enumerate(medium, 1):
            render_change(lines, idx, change)
    if low:
        lines += [
            ("## 低風險留痕", "## Low-Risk Log"), ("", ""),
            ("以下為低風險變動，僅留存追溯紀錄，不需要立即處理。", "The items below are low-risk changes, kept only for audit trail - no immediate action needed."), ("", ""),
        ]
        for idx, change in enumerate(low, 1):
            render_change(lines, idx, change, include_details=False)
    if not ordered:
        lines += [("## 今日結果", "## Result"), ("", ""), ("未偵測到變動。", "No changes detected."), ("", "")]

    if run.failures:
        lines += [("## 抓取失敗", "## Fetch Failures"), ("", "")]
        for failure in run.failures:
            lines.append((f"- {failure['url']}：{failure['error']}", f"- {failure['url']}: {failure['error']}"))
        lines.append(("", ""))

    report_text = "\n".join(zh for zh, _en in lines)
    report_path = REPORT_DIR / f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    report_path.write_text(report_text, encoding="utf-8")
    (REPORT_DIR / "latest.md").write_text(report_text, encoding="utf-8")
    html_text = markdown_to_basic_html(lines)
    (REPORT_DIR / f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.html").write_text(html_text, encoding="utf-8")
    (REPORT_DIR / "latest.html").write_text(html_text, encoding="utf-8")
    (PAGES_DIR / "index.html").write_text(html_text, encoding="utf-8")
    (PAGES_DIR / "latest.html").write_text(html_text, encoding="utf-8")
    (PAGES_ARCHIVE_DIR / f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.html").write_text(html_text, encoding="utf-8")
    history = append_history_entry(now, run, changes, counts)
    HISTORY_HTML_PATH.write_text(render_history_html(history), encoding="utf-8")

    summary_lines = [
        "PAGCOR Regulatory Daily Monitor",
        f"檢查時間：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"變動總數：{len(changes)}",
        f"Critical: {counts['Critical']} | High: {counts['High']} | Medium: {counts['Medium']} | Low: {counts['Low']}",
        f"監控資源數：{len(run.resources)} | 抓取失敗：{len(run.failures)}",
    ]
    if type_counts:
        summary_lines.append(f"變動類型：{type_summary_zh}")
    summary_lines.append("")
    if urgent:
        summary_lines.append("優先閱讀：")
        for idx, change in enumerate(urgent[:8], 1):
            summary_lines.append(f"{idx}. [{change['severity']}] {change_label(change['type'])[0]} - {readable_title(change['snapshot'])}")
            if change["type"] not in COMPACT_CHANGE_TYPES:
                summary_lines.append(f"   影響：{change['impact'][0]}")
        if len(urgent) > 8:
            summary_lines.append(f"另有 {len(urgent) - 8} 個 Critical/High 變動，請看完整報告。")
    else:
        summary_lines.append("未偵測到 Critical / High 變動。")
    summary_lines.append("")
    pages_url = os.getenv("GITHUB_PAGES_URL", "").strip()
    if pages_url:
        # Link to this run's own archived snapshot, not the "latest" page -
        # the latest page gets overwritten every run, so a Telegram message
        # sent today would otherwise show a different (newer) report by the
        # time someone opens it after the next run.
        archive_url = f"{pages_url.rstrip('/')}/reports/{now.strftime('%Y-%m-%d_%H-%M-%S')}.html"
        summary_lines.append(f"本次報告：{archive_url}")
        summary_lines.append(f"歷史紀錄：{pages_url.rstrip('/')}/history.html")
    else:
        summary_lines.append(f"本次報告：reports/{now.strftime('%Y-%m-%d_%H-%M-%S')}.html")
    (REPORT_DIR / "telegram_summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    return report_path


def main() -> None:
    previous = load_state()
    run = discover_and_snapshot()
    shortfall = (not run.max_resources_hit) and crawl_incomplete(previous, run)
    changes = compare_snapshots(
        previous,
        run,
        skip_removed_detection=run.max_resources_hit or shortfall,
        recent_removals=previous.get("recent_removals", {}),
    )
    report = render_reports(changes, run, shortfall=shortfall, prev_resource_count=len(previous.get("resources", {})))
    if not run.max_resources_hit and not shortfall:
        save_state(run, build_recent_removals(previous, changes, run.checked_at))
    elif run.max_resources_hit:
        print("State not updated because crawl did not finish all queued resources.")
    else:
        print(f"State not updated: only {len(run.resources)} of {len(previous.get('resources', {}))} previous resources were fetched (crawl looks incomplete).")
    print(f"Report: {report}")
    print(f"Resources: {len(run.resources)}")
    print(f"Failures: {len(run.failures)}")
    print(f"Max resources hit: {run.max_resources_hit}")
    print(f"Crawl shortfall: {shortfall}")
    print(f"Changes: {len(changes)}")


if __name__ == "__main__":
    main()






