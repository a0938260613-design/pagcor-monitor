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
    return text[: limit - 80].rstrip() + "\n\n...(完整報告請查看 reports/latest.md)"


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
    parser.add_argument("report_path", nargs="?", default=None, help="Report file to send (defaults to reports/telegram_summary.txt)")
    parser.add_argument(
        "--chat-id",
        dest="chat_id",
        default=None,
        help="Send only to this chat id, ignoring TELEGRAM_CHAT_ID/TELEGRAM_CHAT_IDS. "
        "For manual testing only — run_daily.bat/publish_pages.bat never pass this, so the twice-daily automation is unaffected.",
    )
    return parser.parse_args()


def main() -> None:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    args = parse_args()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = [args.chat_id] if args.chat_id else telegram_chat_ids()
    if not token or not chat_ids:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID/TELEGRAM_CHAT_IDS in .env")

    report_path = Path(args.report_path) if args.report_path else ROOT / "reports" / "telegram_summary.txt"
    text = trim_for_telegram(report_path.read_text(encoding="utf-8"))
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

