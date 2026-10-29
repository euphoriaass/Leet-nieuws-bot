"""
Leet.city nieuws-bot
- Checkt leet.city op nieuwe nieuwsberichten (via sitemap of de nieuws-API van de site)
- Stuurt bij elk nieuw bericht een @everyone met titel, korte tekst en afbeelding naar Discord
- Webhook URL(s) komen uit de GitHub Secret DISCORD_WEBHOOK_URL (één per regel)
- TEST_MODE=true stuurt het nieuwste bericht opnieuw, zonder iets op te slaan
"""

import html as htmllib
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests

SITE = "https://www.leet.city"
NEWS_URL = f"{SITE}/news"
STATE_FILE = "seen.json"
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
WEBHOOK_URLS = [u for u in re.split(r"[\s,]+", os.environ["DISCORD_WEBHOOK_URL"]) if u]

IMAGE_KEYS = ["image", "imageUrl", "image_url", "img", "thumbnail", "thumbnailUrl", "thumb",
              "banner", "bannerUrl", "cover", "coverImage", "header", "headerImage",
              "picture", "photo", "media", "promoImage", "topstory"]
TEXT_KEYS = ["shortStory", "short_story", "summary", "excerpt", "description", "intro",
             "teaser", "subtitle", "content", "body", "longStory", "text"]
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")

S = requests.Session()
S.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8",
})


# ---------- hulpfuncties ----------

def find_list(data):
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data
    if isinstance(data, dict):
        for v in data.values():
            found = find_list(v)
            if found:
                return found
    return None


def clean_text(raw: str, limit: int = 300) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", htmllib.unescape(text)).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def find_image(item: dict) -> str | None:
    # 1) bekende velden
    for key in IMAGE_KEYS:
        val = item.get(key)
        if isinstance(val, dict):
            val = val.get("url") or val.get("src") or val.get("path")
        if isinstance(val, str) and val.strip():
            return urljoin(SITE, val.strip())
    # 2) elk veld dat op een afbeelding eindigt
    for val in item.values():
        if isinstance(val, str) and val.lower().split("?")[0].endswith(IMG_EXT):
            return urljoin(SITE, val)
    # 3) eerste <img> in de tekst
    for val in item.values():
        if isinstance(val, str):
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', val, re.I)
            if m:
                return urljoin(SITE, m.group(1))
    return None


def find_text(item: dict) -> str | None:
    for key in TEXT_KEYS:
        val = item.get(key)
        if isinstance(val, str) and clean_text(val):
            return clean_text(val)
    return None


# ---------- nieuws ophalen ----------

def from_api() -> list[dict]:
    page = S.get(NEWS_URL, timeout=20).text
    scripts = re.findall(r'(?:src|href)=["\'](/assets/[^"\']+\.js)["\']', page)
    paths, bases = set(), set()
    for src in dict.fromkeys(scripts):
        try:
            js = S.get(urljoin(SITE, src), timeout=20).text
        except Exception:
            continue
        paths.update(re.findall(r'["\'`](/news/[a-z/]+)["\'`]', js, re.I))
        for m in re.findall(r'["\'`](https?://[^"\'`\s]+)["\'`]', js):
            low = m.lower()
            if ("leet" in low or "api" in low) and not low.endswith((".js", ".css", ".svg") + IMG_EXT):
                bases.add(m.rstrip("/"))

    bases |= {"https://api.leet.city", "https://api.leet.city/api", "https://www.leet.city/api",
              "https://leet.city/api", "https://api.leet.city/v1"}
    tries = [p for p in ["/news/latest", "/news/latest/generic", "/news"] if p in paths or p == "/news"]

    for base in sorted(bases, key=lambda b: ("api" not in b.lower(), b)):
        for path in tries:
            url = base + path
            try:
                r = S.get(url, timeout=15, headers={"Accept": "application/json"})
                if "json" not in r.headers.get("content-type", ""):
                    continue
                items = find_list(r.json())
            except Exception:
                continue
            if not items:
                continue
            print(f"✅ Nieuws-API: {url} ({len(items)} items)")
            print(f"   velden: {list(items[0].keys())}")
            out = []
            for it in items:
                key = it.get("id") or it.get("slug") or it.get("title")
                if key is None:
                    continue
                slug = it.get("slug") or it.get("id")
                out.append({
                    "key": f"api:{key}",
                    "title": it.get("title") or it.get("name") or it.get("subject") or "Nieuw nieuwsbericht",
                    "url": f"{NEWS_URL}/{slug}" if slug else NEWS_URL,
                    "text": find_text(it),
                    "image": find_image(it),
                })
            if out:
                return out
    return []


def from_sitemap() -> list[dict]:
    try:
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", S.get(f"{SITE}/sitemap.xml", timeout=20).text)
    except Exception:
        return []
    news = [l for l in locs if "/news/" in l.lower() and l.rstrip("/") != NEWS_URL]
    return [{"key": l, "title": "Nieuw nieuwsbericht", "url": l, "text": None, "image": None} for l in news]


# ---------- opslaan ----------

def load_seen():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_seen(keys: list[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(keys[:300], f, indent=2)


# ---------- Discord ----------

def send_ping(new: list[dict]) -> None:
    embeds = []
    for n in new[:10]:  # Discord max 10 embeds per bericht
        embed = {
            "title": f"📰 {n['title']}"[:256],
            "url": n["url"],
            "color": 0x5865F2,
            "footer": {"text": "leet.city"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if n.get("text"):
            embed["description"] = n["text"]
        if n.get("image"):
            embed["image"] = {"url": n["image"]}
        embeds.append(embed)

    payload = {
        "username": "Leet Nieuws",
        "content": f"@everyone 📰 Er staat een nieuw nieuwsbericht op Leet! {NEWS_URL}",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": embeds,
    }
    for i, url in enumerate(WEBHOOK_URLS, 1):
        r = requests.post(url, json=payload, timeout=15)
        if r.ok:
            print(f"✅ Webhook {i}: verstuurd ({len(embeds)} bericht(en))")
        else:
            print(f"❌ Webhook {i} mislukt ({r.status_code}): {r.text[:200]}")


# ---------- main ----------

def main() -> None:
    items = from_api() or from_sitemap()
    if not items:
        print("⚠️ Geen nieuwsberichten gevonden, niks verstuurd.")
        sys.exit(1)

    latest = items[0]
    print(f"Nieuwste: {latest['title']} | afbeelding: {latest['image'] or 'geen gevonden'}")

    if TEST_MODE:
        print("🧪 Testmodus: nieuwste bericht opnieuw versturen")
        send_ping([latest])
        return

    seen = load_seen()
    keys = [i["key"] for i in items]
    if seen is None:
        save_seen(keys)
        print(f"Eerste run: {len(keys)} berichten opgeslagen, geen ping.")
        return

    new = [i for i in items if i["key"] not in seen]
    if new:
        send_ping(new)
        save_seen([n["key"] for n in new] + seen)
    else:
        print("Niks nieuws.")


if __name__ == "__main__":
    main()
