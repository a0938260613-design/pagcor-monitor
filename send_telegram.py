from __future__ import annotations

import os
import sys
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


def main() -> None:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = telegram_chat_ids()
    if not token or not chat_ids:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID/TELEGRAM_CHAT_IDS in .env")

    report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports" / "telegram_summary.txt"
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

