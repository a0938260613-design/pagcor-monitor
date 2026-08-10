"""MeitY (meity.gov.in/documents) monitor - a second, independent monitor
alongside monitor.py (PAGCOR). Runs as its own process, keeps its own state
file and report tree, and never imports/calls anything PAGCOR-specific -
only a handful of generic rendering utilities are shared from monitor.py
(dual_span, sha256_text, format_bytes, markdown_to_basic_html, the language-
toggle script/style, and SEVERITY_ORDER), so a bug in either monitor can
never break the other.

Unlike PAGCOR's site, meity.gov.in/documents is a Next.js frontend with no
server-rendered content - but it's backed by a WordPress REST API that
returns complete, structured JSON (confirmed: nested files are already fully
included in each listing response, no per-post follow-up request needed).
So this crawler is a handful of paginated `requests` calls, not a BFS link
crawl - no Playwright, no BeautifulSoup, no checkpointing needed.
"""

from __future__ import annotations

import html
import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

from monitor import (
    LANG_TOGGLE_INIT_JS,
    LANG_TOGGLE_SCRIPT,
    LANG_TOGGLE_STYLE,
    SEVERITY_ORDER,
    dual_span,
    format_bytes,
    markdown_to_basic_html,
    sha256_text,
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
PAGES_DIR = ROOT / "docs" / "meity"
PAGES_ARCHIVE_DIR = PAGES_DIR / "reports"
HISTORY_JSON_PATH = PAGES_DIR / "history.json"
HISTORY_HTML_PATH = PAGES_DIR / "history.html"
STATE_PATH = DATA_DIR / "meity_state.json"

DOCUMENTS_PAGE_URL = "https://www.meity.gov.in/documents"
API_URL = "https://www.meity.gov.in/cms/wp-json/document/documents"
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    # The API silently times out without a Referer (looks like a simple
    # bot-filter, not full browser-fingerprint checking) - confirmed this
    # exact value works for every type= value, no per-tab variation needed.
    "Referer": DOCUMENTS_PAGE_URL,
}
PAGE_LIMIT = 50

# type= values are case-sensitive and confirmed by watching the site's own
# network requests per nav tab - note "Guideline" is singular despite the
# nav label ("Guidelines") being plural.
DOC_TYPES = [
    "Report",
    "Act and policies",
    "Orders and Notices",
    "Publications",
    "Press Release",
    "Gazettes Notifications",
    "Guideline",
]

DOC_TYPE_LABELS: dict[str, tuple[str, str]] = {
    "Report": ("報告", "Report"),
    "Act and policies": ("法規與政策", "Act and Policies"),
    "Orders and Notices": ("命令與公告", "Orders and Notices"),
    "Publications": ("出版品", "Publications"),
    "Press Release": ("新聞稿", "Press Release"),
    "Gazettes Notifications": ("公報通知", "Gazettes Notifications"),
    "Guideline": ("指引", "Guidelines"),
}

# The nav tab URL slug for each type, so a change's "來源" line points at the
# specific category page a human would browse, not just the generic /documents
# root. "Report" has no slug of its own - it's the default tab at /documents.
DOC_TYPE_URL_SLUGS: dict[str, str] = {
    "Report": "",
    "Act and policies": "act-and-policies",
    "Orders and Notices": "orders-and-notices",
    "Publications": "publications",
    "Press Release": "press-release",
    "Gazettes Notifications": "gazettes-notifications",
    "Guideline": "guidelines",
}

SEVERITY_BY_TYPE: dict[str, str] = {
    "Act and policies": "Critical",
    "Gazettes Notifications": "Critical",
    "Orders and Notices": "High",
    "Report": "High",
    "Guideline": "Medium",
    "Publications": "Medium",
    "Press Release": "Medium",
}
DEFAULT_SEVERITY = "Medium"

IMPACT_BY_TYPE: dict[str, tuple[str, str]] = {
    "Act and policies": ("可能涉及法規、政策修正，建議優先審閱。", "May involve a regulatory or policy amendment - prioritize review."),
    "Gazettes Notifications": ("正式官方公報，具法律效力，建議優先審閱。", "Official gazette notification with legal effect - prioritize review."),
    "Orders and Notices": ("官方命令或公告更新，建議排入審閱。", "Official order or notice update - schedule for review."),
    "Report": ("報告或統計資料更新，可作為追蹤與研究來源。", "Report or statistical data update - useful for tracking and research."),
    "Guideline": ("指引文件更新，建議確認是否影響現行作業。", "Guideline document updated - confirm whether it affects current practice."),
    "Publications": ("出版品更新，建議排入例行檢視。", "Publication updated - schedule for routine review."),
    "Press Release": ("新聞稿更新，屬對外發布訊息，建議留意。", "Press release update - public-facing announcement, worth noting."),
}
DEFAULT_IMPACT = ("文件更新，建議排入例行檢視。", "Document updated - schedule for routine review.")


@dataclass
class MeityFileSnapshot:
    entry_title: str
    file_type: str
    pdf_type: str
    language: list[str] = field(default_factory=list)
    pdf_id: str = ""
    pdf_title: str = ""
    filename: str = ""
    filesize: int = 0
    url: str = ""
    link: str = ""


@dataclass
class MeityPostSnapshot:
    post_id: str
    doc_type: str
    post_title: str
    year: str
    post_modified: str
    files: list[dict]
    content_hash: str
    checked_at: str


def doc_type_source_url(doc_type: str) -> str:
    slug = DOC_TYPE_URL_SLUGS.get(doc_type, "")
    return f"{DOCUMENTS_PAGE_URL}/{slug}" if slug else DOCUMENTS_PAGE_URL


def doc_type_label(doc_type: str) -> tuple[str, str]:
    return DOC_TYPE_LABELS.get(doc_type, (doc_type, doc_type))


def normalize_files(raw_files: list[dict] | None) -> list[dict]:
    normalized = []
    for f in raw_files or []:
        pdf = f.get("pdf") or {}
        snap = MeityFileSnapshot(
            entry_title=f.get("title") or "",
            file_type=f.get("type") or "",
            pdf_type=f.get("pdf_type") or "",
            language=f.get("language") or [],
            pdf_id=str(pdf.get("id") or ""),
            pdf_title=pdf.get("title") or "",
            filename=pdf.get("filename") or "",
            filesize=int(pdf.get("filesize") or 0),
            url=pdf.get("url") or "",
            link=pdf.get("link") or "",
        )
        normalized.append(asdict(snap))
    normalized.sort(key=lambda x: x["pdf_id"] or x["url"])
    return normalized


def compute_content_hash(post_title: str, year: str, doc_type: str, files: list[dict]) -> str:
    payload = json.dumps(
        {"title": post_title, "year": year, "doc_type": doc_type, "files": files},
        sort_keys=True, ensure_ascii=False,
    )
    return sha256_text(payload)


def snapshot_post(raw_post: dict, doc_type: str, checked_at: str) -> dict:
    acf = raw_post.get("acf_data") or {}
    post_title = acf.get("title") or raw_post.get("post_title") or ""
    year = str(acf.get("year") or "")
    files = normalize_files(acf.get("file"))
    content_hash = compute_content_hash(post_title, year, doc_type, files)
    snap = MeityPostSnapshot(
        post_id=str(raw_post.get("ID") or ""),
        doc_type=doc_type,
        post_title=post_title,
        year=year,
        post_modified=raw_post.get("post_modified") or "",
        files=files,
        content_hash=content_hash,
        checked_at=checked_at,
    )
    return asdict(snap)


def fetch_json(session: requests.Session, params: dict, timeout: float, attempts: int, delay: float) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.get(API_URL, params=params, headers=REQUEST_HEADERS, timeout=timeout)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "json" not in content_type:
                raise ValueError(f"unexpected content-type {content_type!r} (possible WAF/challenge page, not the real API)")
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - genuinely want to retry on anything
            last_exc = exc
            if attempt < attempts:
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def fetch_all_posts_for_type(session: requests.Session, doc_type: str, timeout: float, delay: float, attempts: int) -> list[dict] | None:
    """Fetch every post for one document type, paginating through total_pages.
    Returns None (not []) on failure, so callers can tell "genuinely zero
    posts" apart from "couldn't reach the API this run" - the latter must
    never be treated as "everything in this type was removed"."""
    posts: list[dict] = []
    page = 1
    try:
        while True:
            data = fetch_json(
                session,
                {"type": doc_type, "limit": PAGE_LIMIT, "page": page, "sort": "year", "order": "DESC", "search": ""},
                timeout, attempts, delay,
            )
            posts.extend(data.get("posts") or [])
            total_pages = int(data.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1
            time.sleep(delay)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to fetch type={doc_type!r}: {exc}")
        return None
    return posts


def discover_and_snapshot() -> tuple[dict, set[str]]:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    delay = float(os.getenv("MEITY_REQUEST_DELAY_SECONDS", "0.3"))
    timeout_seconds = float(os.getenv("MEITY_REQUEST_TIMEOUT_SECONDS", "30"))
    fetch_attempts = max(1, int(os.getenv("MEITY_FETCH_ATTEMPTS", "3")))
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    PAGES_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    posts: dict[str, dict] = {}
    failed_types: set[str] = set()

    for doc_type in DOC_TYPES:
        raw_posts = fetch_all_posts_for_type(session, doc_type, timeout_seconds, delay, fetch_attempts)
        if raw_posts is None:
            failed_types.add(doc_type)
            continue
        for raw_post in raw_posts:
            snap = snapshot_post(raw_post, doc_type, checked_at)
            posts[snap["post_id"]] = snap
        time.sleep(delay)

    return {"posts": posts, "checked_at": checked_at}, failed_types


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(run: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")


def compare_snapshots(previous: dict, current_posts: dict[str, dict], failed_types: set[str]) -> list[dict]:
    old_posts: dict[str, dict] = previous.get("posts", {})
    old_ids, new_ids = set(old_posts), set(current_posts)
    changes: list[dict] = []

    for post_id in sorted(old_ids - new_ids):
        old = old_posts[post_id]
        if old.get("doc_type") in failed_types:
            # Its type failed to fetch this run - it isn't gone, we just
            # couldn't check. Never report a false removal.
            continue
        changes.append({"type": "post_removed", "post_id": post_id, "doc_type": old.get("doc_type"), "snapshot": old, "old_snapshot": old})

    for post_id in sorted(new_ids - old_ids):
        new = current_posts[post_id]
        changes.append({"type": "post_added", "post_id": post_id, "doc_type": new.get("doc_type"), "snapshot": new})

    for post_id in sorted(new_ids & old_ids):
        old, new = old_posts[post_id], current_posts[post_id]
        if old.get("content_hash") == new.get("content_hash") and old.get("post_modified") == new.get("post_modified"):
            continue
        old_files = {f["pdf_id"] or f["url"]: f for f in old.get("files", [])}
        new_files = {f["pdf_id"] or f["url"]: f for f in new.get("files", [])}
        files_added = [new_files[k] for k in sorted(set(new_files) - set(old_files))]
        files_removed = [old_files[k] for k in sorted(set(old_files) - set(new_files))]
        files_changed = [(old_files[k], new_files[k]) for k in sorted(set(old_files) & set(new_files)) if old_files[k] != new_files[k]]
        changes.append({
            "type": "post_changed", "post_id": post_id, "doc_type": new.get("doc_type"),
            "snapshot": new, "old_snapshot": old,
            "files_added": files_added, "files_removed": files_removed, "files_changed": files_changed,
            "metadata_only": old.get("content_hash") == new.get("content_hash"),
        })
    return changes


def classify_severity(change: dict) -> tuple[str, tuple[str, str]]:
    doc_type = change.get("doc_type", "")
    if change["type"] == "post_changed" and change.get("metadata_only"):
        return "Low", ("僅中繼資料變動（如修改時間戳記），檔案內容未變。", "Metadata-only change (e.g. modified timestamp); file contents unchanged.")
    label_zh, label_en = doc_type_label(doc_type)
    severity = SEVERITY_BY_TYPE.get(doc_type, DEFAULT_SEVERITY)
    return severity, (f"分類「{label_zh}」的預設嚴重度。", f"Default severity for the \"{label_en}\" category.")


def render_meity_change(lines: list[tuple[str, str]], idx: int, change: dict, include_details: bool = True) -> None:
    severity, severity_reason = change["severity"], change["severity_reason"]
    doc_type = change.get("doc_type", "")
    label_zh, label_en = doc_type_label(doc_type)
    snapshot = change["snapshot"]
    title = snapshot.get("post_title") or "(untitled)"
    change_type = change["type"]

    if change_type == "post_added":
        heading_zh, heading_en = f"🆕 新增文件：{title}", f"🆕 New document: {title}"
    elif change_type == "post_removed":
        heading_zh, heading_en = f"🗑️ 文件下架：{title}", f"🗑️ Document removed: {title}"
    else:
        n_files = len(change.get("files_added", [])) + len(change.get("files_removed", [])) + len(change.get("files_changed", []))
        heading_zh, heading_en = f"📝 文件更新：{title}（{n_files} 個檔案變動）", f"📝 Document updated: {title} ({n_files} file(s) changed)"

    lines.append((f"{idx}. [{severity}] {heading_zh}", f"{idx}. [{severity}] {heading_en}"))
    lines.append((f"- 分類：{label_zh}", f"- Category: {label_en}"))
    source_url = doc_type_source_url(doc_type)
    lines.append((f"- 來源：{source_url}", f"- Source: {source_url}"))
    if not include_details:
        lines.append(("", ""))
        return

    impact_zh, impact_en = IMPACT_BY_TYPE.get(doc_type, DEFAULT_IMPACT)
    lines.append((f"- 可能影響：{impact_zh}", f"- Possible impact: {impact_en}"))
    lines.append((f"- 分類依據：{severity_reason[0]}", f"- Classification basis: {severity_reason[1]}"))

    if change_type == "post_added":
        for f in snapshot.get("files", []):
            size = format_bytes(int(f.get("filesize") or 0))
            lines.append((f"  - 檔案：{f['entry_title']}（{size}）", f"  - File: {f['entry_title']} ({size})"))
            lines.append((f"    - 來源：{f['url']}", f"    - Source: {f['url']}"))
    elif change_type == "post_changed":
        for f in change.get("files_added", []):
            size = format_bytes(int(f.get("filesize") or 0))
            lines.append((f"  - 新增檔案：{f['entry_title']}（{size}）", f"  - Added file: {f['entry_title']} ({size})"))
            lines.append((f"    - 來源：{f['url']}", f"    - Source: {f['url']}"))
        for f in change.get("files_removed", []):
            lines.append((f"  - 移除檔案：{f['entry_title']}", f"  - Removed file: {f['entry_title']}"))
        for _old_f, new_f in change.get("files_changed", []):
            size = format_bytes(int(new_f.get("filesize") or 0))
            lines.append((f"  - 檔案變動：{new_f['entry_title']}（{size}）", f"  - File changed: {new_f['entry_title']} ({size})"))
            lines.append((f"    - 來源：{new_f['url']}", f"    - Source: {new_f['url']}"))
    lines.append(("", ""))


def load_meity_history() -> list[dict]:
    if not HISTORY_JSON_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_meity_history_entry(now: datetime, run: dict, changes: list[dict], counts: dict) -> list[dict]:
    history = load_meity_history()
    entry = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "file": f"reports/{now.strftime('%Y-%m-%d_%H-%M-%S')}.html",
        "posts": len(run.get("posts", {})),
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


def render_meity_history_html(history: list[dict]) -> str:
    rows = []
    for entry in sorted(history, key=lambda e: e["timestamp"], reverse=True):
        badge_parts = [f"{label} {entry[key]}" for key, label in (("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low")) if entry.get(key)]
        badges = html.escape("、".join(badge_parts)) if badge_parts else dual_span("無變動", "No changes")
        rows.append(
            f'<tr><td><a href="{html.escape(entry.get("file", ""))}">{html.escape(entry.get("timestamp", ""))}</a></td>'
            f'<td>{entry.get("posts", "-")}</td>'
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
<title>MeitY Documents Monitor - History</title>
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
<h1>{dual_span("MeitY Documents Monitor - 歷史紀錄", "MeitY Documents Monitor - History")} <button id="lang-toggle" type="button">EN</button></h1>
<p class=\"back\"><a href=\"index.html\">&larr; {dual_span("回到最新報告", "Back to latest report")}</a></p>
<table>
<thead><tr><th>{dual_span("檢查時間", "Checked At")}</th><th>{dual_span("監控分類數", "Categories")}</th><th>{dual_span("變動總數", "Changes")}</th><th>{dual_span("分級摘要", "Severity Summary")}</th></tr></thead>
<tbody>
""" + table_rows + """
</tbody>
</table>
""" + LANG_TOGGLE_INIT_JS + """
</body>
</html>
"""


def render_meity_reports(changes: list[dict], run: dict, failed_types: set[str]) -> Path:
    now = datetime.now()
    for c in changes:
        c["severity"], c["severity_reason"] = classify_severity(c)
    ordered = sorted(changes, key=lambda c: (SEVERITY_ORDER[c["severity"]], c.get("doc_type", ""), c.get("post_id", "")))
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    for c in ordered:
        counts[c["severity"]] += 1

    lines: list[tuple[str, str]] = [("# MeitY 文件監控", "# MeitY Documents Monitor"), ("", "")]

    urgent = [c for c in ordered if c["severity"] in {"Critical", "High"}]
    if urgent:
        lines += [
            ("## 需要優先閱讀", "## Priority Review"), ("", ""),
            ("建議：以下項目請優先人工複核來源文件。", "Recommendation: please prioritize manual review of the source documents for the items below."), ("", ""),
        ]
        for idx, c in enumerate(urgent, 1):
            render_meity_change(lines, idx, c)
    medium = [c for c in ordered if c["severity"] == "Medium"]
    low = [c for c in ordered if c["severity"] == "Low"]
    if medium:
        lines += [
            ("## 例行檢視", "## Routine Review"), ("", ""),
            ("建議：非急迫，排入例行檢視即可。", "Recommendation: not urgent, schedule for routine review."), ("", ""),
        ]
        for idx, c in enumerate(medium, 1):
            render_meity_change(lines, idx, c)
    if low:
        lines += [
            ("## 低風險留痕", "## Low-Risk Log"), ("", ""),
            ("以下為低風險變動，僅留存追溯紀錄，不需要立即處理。", "The items below are low-risk changes, kept only for audit trail - no immediate action needed."), ("", ""),
        ]
        for idx, c in enumerate(low, 1):
            render_meity_change(lines, idx, c, include_details=False)
    if not ordered:
        lines += [("## 今日結果", "## Result"), ("", ""), ("未偵測到變動。", "No changes detected."), ("", "")]
    if failed_types:
        failed_zh = "、".join(doc_type_label(t)[0] for t in sorted(failed_types))
        failed_en = ", ".join(doc_type_label(t)[1] for t in sorted(failed_types))
        lines += [
            ("## 抓取失敗", "## Fetch Failures"), ("", ""),
            (f"以下分類本次抓取失敗，未列入比對，基準保留待下次重試：{failed_zh}", f"The following categories failed to fetch this run and were excluded from comparison; baseline preserved for a clean retry next run: {failed_en}"), ("", ""),
        ]

    report_text = "\n".join(zh for zh, _en in lines)
    (REPORT_DIR / f"meity_{now.strftime('%Y-%m-%d_%H-%M-%S')}.md").write_text(report_text, encoding="utf-8")
    (REPORT_DIR / "meity_latest.md").write_text(report_text, encoding="utf-8")

    pages_url = os.getenv("GITHUB_PAGES_URL", "").strip()
    pagcor_href = f"{pages_url.rstrip('/')}/index.html" if pages_url else "../index.html"
    meity_history_href = f"{pages_url.rstrip('/')}/meity/history.html" if pages_url else "history.html"
    html_text = markdown_to_basic_html(
        lines,
        page_title="MeitY Documents Monitor",
        cross_links=[(pagcor_href, "🇵🇭 PAGCOR 監控", "🇵🇭 PAGCOR Monitor")],
        history_href=meity_history_href,
    )
    archive_path = PAGES_ARCHIVE_DIR / f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.html"
    archive_path.write_text(html_text, encoding="utf-8")
    (PAGES_DIR / "index.html").write_text(html_text, encoding="utf-8")
    (PAGES_DIR / "latest.html").write_text(html_text, encoding="utf-8")
    (REPORT_DIR / f"meity_{now.strftime('%Y-%m-%d_%H-%M-%S')}.html").write_text(html_text, encoding="utf-8")
    (REPORT_DIR / "meity_latest.html").write_text(html_text, encoding="utf-8")

    history = append_meity_history_entry(now, run, ordered, counts)
    HISTORY_HTML_PATH.write_text(render_meity_history_html(history), encoding="utf-8")

    type_counts = Counter(c.get("doc_type", "") for c in ordered)
    summary_lines = [
        "MeitY Documents Monitor",
        f"檢查時間：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"監控分類數：{len(DOC_TYPES)}",
        f"抓取失敗：{len(failed_types)}",
        f"變動總數：{len(ordered)}",
        "",
        "分級摘要：",
        f"Critical: {counts['Critical']}",
        f"High: {counts['High']}",
        f"Medium: {counts['Medium']}",
        f"Low: {counts['Low']}",
        "",
    ]
    if type_counts:
        sorted_types = sorted(type_counts.items(), key=lambda item: -item[1])
        summary_lines.append("分類分布：")
        summary_lines.append("、".join(f"{doc_type_label(t)[0]} {n}" for t, n in sorted_types))
        summary_lines.append("")
    if pages_url:
        archive_url = f"{pages_url.rstrip('/')}/meity/reports/{now.strftime('%Y-%m-%d_%H-%M-%S')}.html"
        summary_lines.append(f"本次報告：{archive_url}")
        summary_lines.append(f"歷史紀錄：{pages_url.rstrip('/')}/meity/history.html")
    else:
        summary_lines.append(f"本次報告：docs/meity/reports/{now.strftime('%Y-%m-%d_%H-%M-%S')}.html")
    (REPORT_DIR / "meity_telegram_summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")

    return archive_path


def main() -> None:
    previous = load_state()
    run, failed_types = discover_and_snapshot()
    changes = compare_snapshots(previous, run["posts"], failed_types)
    report = render_meity_reports(changes, run, failed_types)
    if not failed_types:
        save_state(run)
    else:
        print(f"State not updated: {len(failed_types)} categor(ies) failed to fetch this run ({', '.join(sorted(failed_types))}) - previous state preserved so next run can retry cleanly.")
    print(f"Report: {report}")
    print(f"Posts: {len(run['posts'])}")
    print(f"Failed categories: {len(failed_types)}")
    print(f"Changes: {len(changes)}")


if __name__ == "__main__":
    main()
