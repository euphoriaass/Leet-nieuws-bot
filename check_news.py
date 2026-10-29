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


# Zet op False als je NOOIT de voorpagina-foto wilt (dan geen afbeelding als het bericht er geen heeft)
FALLBACK_TO_COVER = True

BODY_IMG_PATTERNS = [
    r'<img[^>]+src=["\']([^"\']+)["\']',            # HTML
    r'!\[[^\]]*\]\(([^)\s]+)\)',                     # Markdown
    r'\[img[^\]]*\]\s*([^\[\s]+)\s*\[/img\]',         # BBCode
    r'(https?://[^\s"\'<>()]+\.(?:png|jpe?g|gif|webp))', # losse link
]


def find_body_images(obj, cover: str | None = None) -> list[str]:
    """Zoekt alle afbeeldingen IN de tekst van het bericht (niet de voorpagina-foto)."""
    strings = []

    def walk(o, key=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in IMAGE_KEYS:
                    continue  # voorpagina-/thumbnailvelden overslaan
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and len(o) > 20:
            strings.append(o)

    walk(obj)
    found = []
    for text in strings:
        hits = []
        for pat in BODY_IMG_PATTERNS:
            for m in re.finditer(pat, text, re.I):
                hits.append((m.start(), m.group(1)))
        for _, raw in sorted(hits):  # volgorde zoals in het artikel
            url = urljoin(SITE, htmllib.unescape(raw.strip()))
            if url != cover and url not in found:
                found.append(url)
    return found


API_BASE = None
JS_TEMPLATES: set[str] = set()
JS_SNIPPETS: set[str] = set()


def _json_get(url: str):
    try:
        r = S.get(url, timeout=15, headers={"Accept": "application/json"})
    except Exception as e:
        print(f"   detail {url} -> fout ({type(e).__name__})")
        return None
    ok = r.ok and "json" in r.headers.get("content-type", "")
    print(f"   detail {url} -> {r.status_code}{'' if ok else ' (geen json)' if r.ok else ''}")
    if not ok:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    # lege of foutmeldings-json overslaan
    if not data or (isinstance(data, dict) and set(data) <= {"error", "message", "status", "code"}):
        return None
    return data


def fetch_detail(item: dict) -> dict | None:
    """Haalt het volledige nieuwsbericht op via de API (probeert meerdere adresvormen)."""
    if not API_BASE:
        return None
    slug, nid = item.get("slug"), item.get("id")
    idents = [x for x in dict.fromkeys([slug, nid]) if x is not None]

    candidates = []
    # a) templates uit de JS van de site
    for t in sorted(JS_TEMPLATES):
        for ident in idents:
            path = re.sub(r"\$\{[^}]*\}", str(ident), t, count=0)
            path = path[path.lower().find("/news"):] if "/news" in path.lower() else path
            if path.startswith("/"):
                candidates.append(API_BASE + path)
    # b) gangbare vormen
    for ident in idents:
        for pat in ["/news/{}", "/news/article/{}", "/news/articles/{}", "/news/post/{}",
                    "/news/slug/{}", "/news/id/{}", "/news/view/{}", "/news/get/{}",
                    "/news/latest/{}", "/news?slug={}", "/news?id={}", "/articles/{}", "/article/{}"]:
            candidates.append(API_BASE + pat.format(ident))

    for url in dict.fromkeys(candidates):
        data = _json_get(url)
        if data is not None:
            print(f"   ✅ artikel gevonden via {url}")
            if isinstance(data, dict):
                print(f"      velden: {list(data.keys())}")
            return data

    print(f"   JS-templates: {sorted(JS_TEMPLATES)[:15]}")
    for sn in sorted(JS_SNIPPETS)[:10]:
        print(f"   JS: {sn}")
    return None


def render_page_images(url: str) -> list[dict]:
    """Opent de pagina in een echte (onzichtbare) Chrome en geeft alle afbeeldingen terug."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("   playwright niet geinstalleerd")
        return []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=S.headers["User-Agent"],
                                    viewport={"width": 1280, "height": 2000})
            page.goto(url, wait_until="networkidle", timeout=60000)
            page.wait_for_timeout(2000)
            for _ in range(5):  # naar beneden scrollen zodat alle afbeeldingen laden
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(500)
            page.wait_for_timeout(1500)
            imgs = page.evaluate("""() => Array.from(document.images).map(i => ({
                src: i.currentSrc || i.src, w: i.naturalWidth, h: i.naturalHeight }))""")
            browser.close()
            return imgs
    except Exception as e:
        print(f"   browser fout: {type(e).__name__}: {e}")
        return []


def pick_images(item: dict, cover: str | None) -> list[str]:
    url = item["url"]
    print(f"   pagina openen: {url}")
    imgs = render_page_images(url)
    print(f"   {len(imgs)} afbeeldingen op de pagina")
    norm = lambda u: (u or "").split("?")[0]
    big = [i for i in imgs
           if i["w"] >= 300 and i["h"] >= 150 and norm(i["src"]) != norm(cover)]
    for i in big:
        print(f"     {i['w']}x{i['h']} {i['src']}")
    pngs = [i for i in big if ".png" in norm(i["src"]).lower()]
    choice = (pngs or big)
    if choice:
        print(f"   ✅ gekozen (laatste {'png' if pngs else 'afbeelding'}): {choice[-1]['src']}")
        return [choice[-1]["src"]]
    print(f"   geen afbeelding gevonden{' -> voorpagina-foto' if FALLBACK_TO_COVER and cover else ''}")
    return [cover] if FALLBACK_TO_COVER and cover else []


# ---------- nieuws ophalen ----------

def from_api() -> list[dict]:
    global API_BASE
    page = S.get(NEWS_URL, timeout=20).text
    scripts = re.findall(r'(?:src|href)=["\'](/assets/[^"\']+\.js)["\']', page)
    paths, bases = set(), set()
    for src in dict.fromkeys(scripts):
        try:
            js = S.get(urljoin(SITE, src), timeout=20).text
        except Exception:
            continue
        paths.update(re.findall(r'["\'`](/news/[a-z/]+)["\'`]', js, re.I))
        # template strings zoals `/news/${slug}` of `${API}/news/article/${id}`
        for t in re.findall(r'`([^`]{0,80}news[^`]{0,80})`', js, re.I):
            if "${" in t:
                JS_TEMPLATES.add(t)
        # stukjes code rond "news/" voor de log (debug)
        for m in re.finditer(r'.{0,60}["\'`]/news/["\'`]?.{0,60}', js):
            JS_SNIPPETS.add(m.group(0).replace("\n", " "))
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
            API_BASE = base
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
                    "raw": it,
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
    return [{"key": l, "title": "Nieuw nieuwsbericht", "url": l, "text": None, "image": None, "raw": {}} for l in news]


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
        print(f"Bericht: {n['title']}")
        images = pick_images(n, n["image"])
        embed = {
            "title": f"📰 {n['title']}"[:256],
            "url": n["url"],
            "color": 0x5865F2,
            "footer": {"text": "leet.city"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if n.get("text"):
            embed["description"] = n["text"]
        if images:
            embed["image"] = {"url": images[0]}
        embeds.append(embed)
        # extra afbeeldingen met dezelfde url -> Discord voegt ze samen tot een galerij
        for extra in images[1:]:
            embeds.append({"url": n["url"], "image": {"url": extra}})
    embeds = embeds[:10]

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
    print(f"Nieuwste: {latest['title']}")

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
