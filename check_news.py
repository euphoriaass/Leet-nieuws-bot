"""
Checkt leet.city/news op nieuwe berichten en stuurt dan een @everyone via Discord webhook.
De webhook URL komt uit de GitHub Secret DISCORD_WEBHOOK_URL (nooit in de code zetten!).
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

NEWS_URL = "https://www.leet.city/news"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = "seen.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
}


def get_news_links() -> list[str]:
    r = requests.get(NEWS_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    links = []
    for href in re.findall(r'href=["\']([^"\']*news[^"\']*)["\']', r.text, re.I):
        url = urljoin(NEWS_URL, href).split("#")[0].rstrip("/")
        if url.lower() in (NEWS_URL.lower(), NEWS_URL.lower() + "/"):
            continue  # link naar de nieuwspagina zelf
        if url not in links:
            links.append(url)
    return links


def load_seen() -> list[str] | None:
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_seen(links: list[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(links[:200], f, indent=2)


def send_ping(new_links: list[str]) -> None:
    lines = "\n".join(f"• {link}" for link in new_links[:5])
    payload = {
        "username": "Leet Nieuws",
        "content": f"@everyone 📰 Er staat een nieuw nieuwsbericht op Leet! {NEWS_URL}",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [{
            "title": "📰 Nieuw nieuwsbericht!",
            "description": f"Lees het hier:\n{lines}",
            "url": new_links[0],
            "color": 0x5865F2,
            "footer": {"text": "leet.city"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    r.raise_for_status()
    print(f"✅ Ping verstuurd voor {len(new_links)} nieuw(e) bericht(en)")


def main() -> None:
    links = get_news_links()
    if not links:
        print("⚠️ Geen nieuwslinks gevonden op de pagina, niks verstuurd.")
        sys.exit(1)

    seen = load_seen()
    if seen is None:
        save_seen(links)
        print(f"Eerste run: {len(links)} bestaande berichten opgeslagen, geen ping.")
        return

    new = [l for l in links if l not in seen]
    if new:
        send_ping(new)
        save_seen(new + seen)
    else:
        print("Niks nieuws.")


if __name__ == "__main__":
    main()
