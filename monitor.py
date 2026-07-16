from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

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


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"
REPORT_DIR = ROOT / "reports"
PAGES_DIR = ROOT / "docs"
PAGES_ARCHIVE_DIR = PAGES_DIR / "reports"
STATE_PATH = DATA_DIR / "state.json"

START_URL = "https://www.pagcor.ph/regulatory/index.php"
ALLOWED_PREFIX = "https://www.pagcor.ph/regulatory/"
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b", re.I)
logging.getLogger("pypdf").setLevel(logging.ERROR)

DATE_RE = re.compile(
    r"\b(?:as of\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}\b",
    re.I,
)
REGULATORY_ENTITY_RE = re.compile(
    r"\b(?:licensee|licensees|licensed|accredited|accreditation|registered|cancelled|operator|operators|administrator|administrators|brand|brands|domain|domains|url|urls)\b",
    re.I,
)
SEVERITY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}


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


def extract_html_text(html: bytes) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return normalize_text(soup.get_text(" ", strip=True))


def extract_title(html: bytes, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.string:
        return normalize_text(soup.title.string)
    h1 = soup.find(["h1", "h2", "h3"])
    return normalize_text(h1.get_text(" ", strip=True)) if h1 else fallback


def extract_pdf_text(path: Path) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(str(path))
        chunks = [(page.extract_text() or "") for page in reader.pages]
        return normalize_text("\n".join(chunks))
    except Exception:
        return ""


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

    if kind == "html":
        links = extract_links(content, url)
        title = extract_title(content, title)
        text = extract_html_text(content)
    else:
        local = save_download(url, content, digest)
        local_path = str(local.relative_to(ROOT))
        if kind == "pdf":
            text = extract_pdf_text(local)

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
    )


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"meta": {}, "resources": {}}


def save_state(result: RunResult) -> None:
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
    }
    STATE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def discover_and_snapshot() -> RunResult:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    start_url = os.getenv("PAGCOR_START_URL", START_URL)
    max_resources = int(os.getenv("PAGCOR_MAX_PAGES", "300"))
    delay = float(os.getenv("PAGCOR_REQUEST_DELAY_SECONDS", "0.5"))
    timeout_seconds = float(os.getenv("PAGCOR_REQUEST_TIMEOUT_SECONDS", "45"))
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    DATA_DIR.mkdir(exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    PAGES_DIR.mkdir(exist_ok=True)
    PAGES_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (PAGES_DIR / ".nojekyll").write_text("", encoding="utf-8")

    session = requests.Session()
    session.headers.update({"User-Agent": "PAGCOR regulatory monitor/1.0"})

    queue = [start_url]
    queued = {start_url}
    seen: set[str] = set()
    resources: dict[str, ResourceSnapshot] = {}
    failures: list[dict] = []

    while queue and len(seen) < max_resources:
        url = queue.pop(0)
        if url in seen or not is_monitorable(url):
            continue
        seen.add(url)
        try:
            snapshot = snapshot_resource(session, url, checked_at, timeout_seconds)
        except Exception as exc:
            failures.append({"url": url, "error": str(exc), "checked_at": checked_at})
            print(f"Failed: {url} ({exc})")
            time.sleep(delay)
            continue

        resources[url] = snapshot
        if snapshot.kind == "html":
            for link in snapshot.links:
                linked_url = link["url"]
                if linked_url not in seen and linked_url not in queued:
                    queue.append(linked_url)
                    queued.add(linked_url)
        time.sleep(delay)

    return RunResult(resources=resources, failures=failures, max_resources_hit=bool(queue), checked_at=checked_at)


def evidence_text(snapshot: ResourceSnapshot, change: dict | None = None) -> str:
    parts = [snapshot.url, snapshot.title, snapshot.kind]
    if change:
        for key in ("added_links", "removed_links"):
            for item in change.get(key, []):
                parts.extend([item.get("text", ""), item.get("url", "")])
        parts.extend(change.get("added_domains", []))
        parts.extend(change.get("removed_domains", []))
    return " ".join(parts).lower()


def severity_for(snapshot: ResourceSnapshot, change_type: str, change: dict | None = None) -> str:
    text = evidence_text(snapshot, change)
    critical_patterns = [
        "cancelled",
        "reported websites",
        "counterfeit",
        "registered brands",
        "domain names",
        "domain-names",
        "licensees",
        "accredited",
        "gaming system administrator",
        "regulatory framework",
        "amendment",
    ]
    high_patterns = [
        "announcement",
        "notice",
        "application kit",
        "requirements",
        "schedule of fees",
        "industry statistic",
        "industry data",
        "player exclusion",
    ]
    medium_patterns = ["manual", "guideline", "form", "technical standard", "standard"]

    if any(pattern in text for pattern in critical_patterns):
        return "Critical"
    if change and (change.get("added_domains") or change.get("removed_domains")):
        return "Critical"
    if any(pattern in text for pattern in high_patterns):
        return "High"
    if any(pattern in text for pattern in medium_patterns):
        return "Medium"
    if snapshot.kind in {"pdf", "excel", "document", "csv"} and change_type in {"added", "content_changed"}:
        return "Medium"
    return "Low"


def business_impact(snapshot: ResourceSnapshot, severity: str, change_type: str, change: dict) -> str:
    text = evidence_text(snapshot, change)
    if "cancelled" in text:
        return "可能涉及業者資格取消或市場准入狀態變化，需優先人工複核。"
    if "domain" in text or change.get("added_domains") or change.get("removed_domains"):
        return "可能涉及合法品牌、平台或網域名單變動，會直接影響市場與合規判讀。"
    if "registered brands" in text or "licensees" in text or "accredited" in text:
        return "可能涉及受監管業者、品牌或認可實體名單變動。"
    if "regulatory framework" in text or "amendment" in text:
        return "可能涉及正式規範或修訂，需評估對營運與合規流程的影響。"
    if "announcement" in text or "notice" in text:
        return "PAGCOR 發布公告或通知，需確認是否有市場或合規影響。"
    if "industry" in text or "player exclusion" in text:
        return "產業統計或排除資料更新，可作為市場追蹤與報告來源。"
    if severity == "Medium":
        return "文件或流程資料有變動，建議排入例行檢視。"
    return "低風險變動，已留痕供追溯。"


def compare_snapshots(previous: dict, current: RunResult) -> list[dict]:
    prev_resources = previous.get("resources", {})
    changes: list[dict] = []

    for url, snapshot in current.resources.items():
        old = prev_resources.get(url)
        if not old:
            changes.append({"type": "added", "url": url, "snapshot": snapshot})
            continue

        if snapshot.kind == "html":
            content_changed = old.get("text_sha256") != snapshot.text_sha256
        else:
            content_changed = old.get("sha256") != snapshot.sha256 or old.get("text_sha256") != snapshot.text_sha256

        if content_changed:
            changes.append(
                {
                    "type": "content_changed",
                    "url": url,
                    "snapshot": snapshot,
                    "added_domains": sorted(set(snapshot.domains) - set(old.get("domains", []))),
                    "removed_domains": sorted(set(old.get("domains", [])) - set(snapshot.domains)),
                    "added_dates": sorted(set(snapshot.dates) - set(old.get("dates", []))),
                    "removed_dates": sorted(set(old.get("dates", [])) - set(snapshot.dates)),
                }
            )

        if snapshot.kind == "html" and old.get("links") != snapshot.links:
            old_links = {item["url"]: item for item in old.get("links", [])}
            new_links = {item["url"]: item for item in snapshot.links}
            changes.append(
                {
                    "type": "links_changed",
                    "url": url,
                    "snapshot": snapshot,
                    "added_links": [new_links[u] for u in sorted(set(new_links) - set(old_links))],
                    "removed_links": [old_links[u] for u in sorted(set(old_links) - set(new_links))],
                }
            )

    if not current.max_resources_hit:
        for url, old in prev_resources.items():
            if url not in current.resources:
                old_snapshot = ResourceSnapshot(**old)
                changes.append({"type": "removed", "url": url, "snapshot": old_snapshot})

    for failure in current.failures:
        if failure["url"] in prev_resources:
            old_snapshot = ResourceSnapshot(**prev_resources[failure["url"]])
            changes.append({"type": "fetch_failed", "url": failure["url"], "snapshot": old_snapshot, "error": failure["error"]})

    for change in changes:
        severity = severity_for(change["snapshot"], change["type"], change)
        change["severity"] = severity
        change["impact"] = business_impact(change["snapshot"], severity, change["type"], change)
    return changes


def format_list(items: list[str], limit: int = 12) -> str:
    if not items:
        return "無"
    shown = items[:limit]
    suffix = f"，另有 {len(items) - limit} 項" if len(items) > limit else ""
    return "、".join(shown) + suffix


def change_label(change_type: str) -> str:
    return {
        "added": "新增資源",
        "removed": "移除資源",
        "content_changed": "內容更新",
        "links_changed": "連結清單更新",
        "fetch_failed": "抓取失敗",
    }.get(change_type, change_type)


def render_change(lines: list[str], idx: int, change: dict, include_details: bool = True) -> None:
    snapshot = change["snapshot"]
    lines += [
        f"### {idx}. [{change['severity']}] {snapshot.title}",
        "",
        f"- 變動：{change_label(change['type'])}",
        f"- 來源：{snapshot.url}",
        f"- 格式：{snapshot.kind}",
        f"- 可能影響：{change['impact']}",
    ]
    if change["type"] == "content_changed":
        if change.get("added_dates") or change.get("removed_dates"):
            lines.append(f"- 日期變動：新增 {format_list(change.get('added_dates', []))}；移除 {format_list(change.get('removed_dates', []))}")
        if change.get("added_domains") or change.get("removed_domains"):
            lines.append(f"- Domain 變動：新增 {format_list(change.get('added_domains', []))}；移除 {format_list(change.get('removed_domains', []))}")
    if change["type"] == "links_changed" and include_details:
        added = [item["text"] or item["url"] for item in change.get("added_links", [])]
        removed = [item["text"] or item["url"] for item in change.get("removed_links", [])]
        lines.append(f"- 新增連結：{format_list(added)}")
        lines.append(f"- 移除連結：{format_list(removed)}")
    if change["type"] == "fetch_failed":
        lines.append(f"- 錯誤：{change.get('error', '')}")
    lines += ["", "建議：Critical / High 請優先人工複核來源文件；Medium 排入例行檢視；Low 保留追溯。", ""]



def markdown_to_basic_html(markdown: str) -> str:
    body_lines = []
    in_list = False
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if not line:
            if in_list:
                body_lines.append("</ul>")
                in_list = False
            continue
        if line.startswith("# "):
            if in_list:
                body_lines.append("</ul>")
                in_list = False
            body_lines.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_list:
                body_lines.append("</ul>")
                in_list = False
            body_lines.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            if in_list:
                body_lines.append("</ul>")
                in_list = False
            body_lines.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("- "):
            if not in_list:
                body_lines.append("<ul>")
                in_list = True
            body_lines.append(f"<li>{html.escape(line[2:])}</li>")
        else:
            if in_list:
                body_lines.append("</ul>")
                in_list = False
            body_lines.append(f"<p>{html.escape(line)}</p>")
    if in_list:
        body_lines.append("</ul>")
    return """<!doctype html>
<html lang=\"zh-Hant\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>PAGCOR Regulatory Daily Monitor</title>
<style>
body{font-family:Arial,'Microsoft JhengHei',sans-serif;line-height:1.6;margin:32px;max-width:1100px;color:#1f2937;background:#f8fafc}
h1,h2,h3{line-height:1.25;color:#111827}h1{font-size:28px}h2{font-size:22px;margin-top:28px;border-bottom:1px solid #d1d5db;padding-bottom:6px}h3{font-size:18px;margin-top:22px}
ul{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:14px 20px 14px 34px}li{margin:5px 0}p{background:#fff;border-left:4px solid #9ca3af;padding:10px 14px}code{background:#e5e7eb;padding:2px 5px;border-radius:4px}
</style>
</head>
<body>
""" + "\n".join(body_lines) + "\n</body>\n</html>\n"
def render_reports(changes: list[dict], run: RunResult) -> Path:
    now = datetime.now()
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    for change in changes:
        counts[change["severity"]] += 1
    ordered = sorted(changes, key=lambda c: (SEVERITY_ORDER[c["severity"]], c["snapshot"].title, c["url"]))

    lines = [
        "# PAGCOR Regulatory Daily Monitor",
        "",
        f"- 檢查時間：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 監控資源數：{len(run.resources)}",
        f"- 抓取失敗數：{len(run.failures)}",
        f"- 達到資源上限：{'是' if run.max_resources_hit else '否'}",
        f"- 變動總數：{len(changes)}",
        "",
        "## 分級摘要",
        "",
        f"- Critical: {counts['Critical']}",
        f"- High: {counts['High']}",
        f"- Medium: {counts['Medium']}",
        f"- Low: {counts['Low']}",
        "",
    ]
    if run.max_resources_hit:
        lines += [
            "## 注意",
            "",
            "本次達到 `PAGCOR_MAX_PAGES` 上限。系統不會在這種情況下判定舊資源已被移除，以避免誤報。若要完整全站移除偵測，請提高上限後重跑。",
            "",
        ]

    urgent = [c for c in ordered if c["severity"] in {"Critical", "High"}]
    if urgent:
        lines += ["## 需要優先閱讀", ""]
        for idx, change in enumerate(urgent, 1):
            render_change(lines, idx, change)

    medium = [c for c in ordered if c["severity"] == "Medium"]
    low = [c for c in ordered if c["severity"] == "Low"]
    if medium:
        lines += ["## 例行檢視", ""]
        for idx, change in enumerate(medium, 1):
            render_change(lines, idx, change)
    if low:
        lines += ["## 低風險留痕", ""]
        for idx, change in enumerate(low, 1):
            render_change(lines, idx, change, include_details=False)
    if not ordered:
        lines += ["## 今日結果", "", "未偵測到變動。", ""]

    if run.failures:
        lines += ["## 抓取失敗", ""]
        for failure in run.failures:
            lines.append(f"- {failure['url']}：{failure['error']}")
        lines.append("")

    report_text = "\n".join(lines)
    report_path = REPORT_DIR / f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.md"
    report_path.write_text(report_text, encoding="utf-8")
    (REPORT_DIR / "latest.md").write_text(report_text, encoding="utf-8")
    html_text = markdown_to_basic_html(report_text)
    (REPORT_DIR / f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.html").write_text(html_text, encoding="utf-8")
    (REPORT_DIR / "latest.html").write_text(html_text, encoding="utf-8")
    (PAGES_DIR / "index.html").write_text(html_text, encoding="utf-8")
    (PAGES_DIR / "latest.html").write_text(html_text, encoding="utf-8")
    (PAGES_ARCHIVE_DIR / f"{now.strftime('%Y-%m-%d_%H-%M-%S')}.html").write_text(html_text, encoding="utf-8")

    summary_lines = [
        "PAGCOR Regulatory Daily Monitor",
        f"檢查時間：{now.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"變動總數：{len(changes)}",
        f"Critical: {counts['Critical']} | High: {counts['High']} | Medium: {counts['Medium']} | Low: {counts['Low']}",
        f"監控資源數：{len(run.resources)} | 抓取失敗：{len(run.failures)}",
        "",
    ]
    if urgent:
        summary_lines.append("優先閱讀：")
        for idx, change in enumerate(urgent[:8], 1):
            summary_lines.append(f"{idx}. [{change['severity']}] {change_label(change['type'])} - {change['snapshot'].title}")
            summary_lines.append(f"   影響：{change['impact']}")
        if len(urgent) > 8:
            summary_lines.append(f"另有 {len(urgent) - 8} 個 Critical/High 變動，請看完整報告。")
    else:
        summary_lines.append("未偵測到 Critical / High 變動。")
    summary_lines.append("")
    pages_url = os.getenv("GITHUB_PAGES_URL", "").strip()
    summary_lines.append(f"完整報告：{pages_url}" if pages_url else "完整報告：reports/latest.html")
    (REPORT_DIR / "telegram_summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    return report_path


def main() -> None:
    previous = load_state()
    run = discover_and_snapshot()
    changes = compare_snapshots(previous, run)
    report = render_reports(changes, run)
    save_state(run)
    print(f"Report: {report}")
    print(f"Resources: {len(run.resources)}")
    print(f"Failures: {len(run.failures)}")
    print(f"Max resources hit: {run.max_resources_hit}")
    print(f"Changes: {len(changes)}")


if __name__ == "__main__":
    main()

