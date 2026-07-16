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


def main() -> None:
    if load_dotenv:
        load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")

    report_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "reports" / "telegram_summary.txt"
    text = report_path.read_text(encoding="utf-8")
    payload = {
        "chat_id": chat_id,
        "text": trim_for_telegram(text),
        "disable_web_page_preview": True,
    }
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data=payload, timeout=30)
    response.raise_for_status()
    print("Telegram sent")


if __name__ == "__main__":
    main()

