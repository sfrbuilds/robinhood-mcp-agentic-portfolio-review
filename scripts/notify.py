#!/usr/bin/env python3
"""
notify.py
---------
Send the portfolio review output to Telegram.
Called by run_review.sh after a successful review.

Usage:
    python scripts/notify.py --file reviews/2026-06-10_0945_morning.txt
    python scripts/notify.py --message "Direct message text"
    cat review.txt | python scripts/notify.py --stdin
"""

import sys
import os
import argparse
import requests
from datetime import datetime

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_CHARS = 4000  # Telegram message limit


def send(text: str, token: str = None, chat_id: str = None) -> bool:
    token   = token   or BOT_TOKEN
    chat_id = chat_id or CHAT_ID

    if not token or not chat_id:
        print("[notify] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Skipping.", file=sys.stderr)
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Split into chunks if over limit
    chunks = [text[i:i + MAX_CHARS] for i in range(0, len(text), MAX_CHARS)]
    success = True
    for i, chunk in enumerate(chunks):
        prefix = f"[{i+1}/{len(chunks)}] " if len(chunks) > 1 else ""
        resp = requests.post(url, json={
            "chat_id":    chat_id,
            "text":       prefix + chunk,
            "parse_mode": "Markdown",
        }, timeout=10)
        if not resp.ok:
            print(f"[notify] Telegram error: {resp.text}", file=sys.stderr)
            success = False

    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",    help="Path to review text file to send")
    parser.add_argument("--message", help="Direct message text")
    parser.add_argument("--stdin",   action="store_true", help="Read from stdin")
    parser.add_argument("--session", default="", help="Session label (morning/afternoon)")
    args = parser.parse_args()

    if args.file:
        with open(args.file) as f:
            content = f.read().strip()
    elif args.message:
        content = args.message
    elif args.stdin:
        content = sys.stdin.read().strip()
    else:
        parser.print_help()
        sys.exit(1)

    # Prepend a header line
    now   = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    label = f" ({args.session})" if args.session else ""
    header = f"*Portfolio Review{label} — {now}*\n\n"
    full   = header + content

    ok = send(full)
    sys.exit(0 if ok else 1)
