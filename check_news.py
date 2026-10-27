"""
Checkt leet.city op nieuwe nieuwsberichten en stuurt dan een @everyone via Discord webhook.
Probeert: 1) sitemap.xml  2) de nieuws-API die de site zelf gebruikt.
De webhook URL komt uit de GitHub Secret DISCORD_WEBHOOK_URL.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

SITE = "https://www.leet.city"
NEWS_URL = f"{SITE}/news"
WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = "seen.json"

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
})


def from_sitemap() -> list[dict]:
    try:
        r = S.get(f"{SITE}/sitemap.xml", timeout=20)
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text)
    except Exception as e:
        print(f"Sitemap mislukt: {e}")
        return []
    print(f"Sitemap: {len(locs)} urls")
    for l in locs[:30]:
        print(f"  sitemap: {l}")
    news = [l for l in locs if "news" in l.lower() and l.rstrip("/") != NEWS_URL]
    return [{"key": l, "title": None, "url": l} for l in news]


def find_list(data):
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data
    if isinstance(data, dict):
        for v in data.values():
            found = find_list(v)
            if found:
                return found
    return None


def from_api() -> list[dict]:
    html = S.get(NEWS_URL, timeout=20).text
    scripts = re.findall(r'src=["\'](/assets/[^"\']+\.js)["\']', html)
    scripts += re.findall(r'href=["\'](/assets/[^"\']+\.js)["\']', html)
    candidates = set()
    for src in dict.fromkeys(scripts):
        try:
            js = S.get(urljoin(SITE, src), timeout=20).text
        except Exception:
            continue
        for m in re.findall(r'["\'`]((?:https?://[^"\'`\s]*)?/[^"\'`\s]*news[^"\'`\s]*)["\'`]', js, re.I):
            if "${" not in m and not m.endswith((".js", ".css", ".png")):
                candidates.add(m)
    print(f"API-kandidaten: {sorted(candidates)}")

    for path in sorted(candidates, key=lambda p: ("api" not in p.lower(), len(p))):
        url = urljoin(SITE, path)
        try:
            r = S.get(url, timeout=20, headers={"Accept": "application/json"})
            data = r.json()
        except Exception:
            continue
        items = find_list(data)
        if not items:
            continue
        print(f"✅ Nieuws-API gevonden: {url} ({len(items)} items)")
        print(f"  voorbeeld: {json.dumps(items[0], ensure_ascii=False)[:500]}")
        out = []
        for it in items:
            key = it.get("id") or it.get("slug") or it.get("title")
            if key is None:
                continue
            title = it.get("title") or it.get("name") or it.get("subject")
            out.append({"key": f"api:{key}", "title": title, "url": NEWS_URL})
        if out:
            return out
    return []


def load_seen():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_seen(keys: list[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(keys[:300], f, indent=2)


def send_ping(new: list[dict]) -> None:
    lines = "\n".join(f"• **{n['title']}**" if n["title"] else f"• {n['url']}" for n in new[:5])
    payload = {
        "username": "Leet Nieuws",
        "content": f"@everyone 📰 Er staat een nieuw nieuwsbericht op Leet! {NEWS_URL}",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [{
            "title": "📰 Nieuw nieuwsbericht!",
            "description": f"{lines}\n\nLees het op {NEWS_URL}",
            "url": new[0]["url"],
            "color": 0x5865F2,
            "footer": {"text": "leet.city"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    r.raise_for_status()
    print(f"✅ Ping verstuurd voor {len(new)} nieuw(e) bericht(en)")


def main() -> None:
    items = from_sitemap() or from_api()
    if not items:
        print("⚠️ Geen nieuwsberichten gevonden via sitemap of API, niks verstuurd.")
        sys.exit(1)

    keys = [i["key"] for i in items]
    seen = load_seen()
    if seen is None:
        save_seen(keys)
        print(f"Eerste run: {len(keys)} bestaande berichten opgeslagen, geen ping.")
        return

    new = [i for i in items if i["key"] not in seen]
    if new:
        send_ping(new)
        save_seen([n["key"] for n in new] + seen)
    else:
        print("Niks nieuws.")


if __name__ == "__main__":
    main()
