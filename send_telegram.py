from __future__ import annotations

import argparse
import os
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


ROOT = Path(__file__).resolve().parent


def trim_for_telegram(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80].rstrip() + "\n\n...(完整報告請查看網站上的最新報告)"


def telegram_chat_ids() -> list[str]:
    primary = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    extra = os.getenv("TELEGRAM_CHAT_IDS", "").strip()
    raw_ids = [primary] if primary else []
    if extra:
        raw_ids.extend(item.strip() for item in extra.split(","))
    seen = set()
    chat_ids = []
    for chat_id in raw_ids:
        if chat_id and chat_id not in seen:
            seen.add(chat_id)
            chat_ids.append(chat_id)
    return chat_ids


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "report_paths", nargs="*", default=[],
        help="Report file(s) to send (defaults to reports/telegram_summary.txt if none given). "
        "Multiple paths are combined into a single message, in the order given.",
    )
    parser.add_argument(
        "--chat-id",
        dest="chat_id",
        default=None,
        help="Send only to this chat id, ignoring TELEGRAM_CHAT_ID/TELEGRAM_CHAT_IDS. "
        "For manual testing only — run_daily.bat/publish_pages.bat never pass this, so the twice-daily automation is unaffected.",
    )
    parser.add_argument(
        "--push-failed-alert",
        action="store_true",
        help="Send an operator alert that git push failed (report links will 404 until fixed), "
        "quoting the last GitHub error from logs/publish_pages.log. Goes to TELEGRAM_ALERT_CHAT_ID, "
        "else the primary TELEGRAM_CHAT_ID only - not every subscriber chat.",
    )
    return parser.parse_args()


def push_failed_alert_text() -> str:
    """The 2026-09-28 outage went unnoticed for 18 days because a failed push
    only ever reached the log; this puts the actual GitHub rejection reason in
    front of a human the same run it happens."""
    log_path = ROOT / "logs" / "publish_pages.log"
    reason = "（log 中找不到錯誤訊息）"
    try:
        lines = log_path.read_bytes().decode("utf-8", errors="replace").splitlines()
        errors = [l.strip() for l in lines[-200:] if "error:" in l and "Trace:" not in l and "gh.io" not in l]
        # Prefer the line naming the actual cause (e.g. "File ... exceeds
        # GitHub's file size limit") over the generic "failed to push" tail.
        specific = [e for e in errors if "exceeds" in e or "denied" in e or "rejected" in e]
        if errors:
            reason = (specific or errors)[-1]
    except OSError:
        pass
    return (
        "⚠️ PAGCOR/MeitY 監控：git push 失敗（重試 3 次）\n"
        "本次報告已產生、TG 摘要已送出，但網站沒有更新，報告連結會顯示 404，直到問題修好為止。\n\n"
        f"GitHub 錯誤：{reason}\n\n"
        "詳見 logs/publish_pages.log"
    )


def main() -> None:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    args = parse_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if args.chat_id:
        chat_ids = [args.chat_id]
    elif args.push_failed_alert:
        alert_id = os.getenv("TELEGRAM_ALERT_CHAT_ID", "").strip() or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        chat_ids = [alert_id] if alert_id else []
    else:
        chat_ids = telegram_chat_ids()
    if not token or not chat_ids:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID/TELEGRAM_CHAT_IDS in .env")

    if args.push_failed_alert:
        text = push_failed_alert_text()
    else:
        report_paths = [Path(p) for p in args.report_paths] if args.report_paths else [ROOT / "reports" / "telegram_summary.txt"]
        section_separator = "\n\n" + "━" * 20 + "\n\n"
        text = trim_for_telegram(section_separator.join(p.read_text(encoding="utf-8") for p in report_paths))
    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, timeout=30)
        response.raise_for_status()
    print(f"Telegram sent to {len(chat_ids)} chat(s)")


if __name__ == "__main__":
    main()

