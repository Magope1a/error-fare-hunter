#!/usr/bin/env python3
from __future__ import annotations

import html as html_lib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# ============================================================
# CONFIG
# ============================================================

EFA_CHANNEL = "errorfarealerts"
EFA_FEED_URL = f"https://t.me/s/{EFA_CHANNEL}"
SF_ERROR_URL = "https://www.secretflying.com/error-fares/"
SF_BASE = "https://www.secretflying.com"
STATE_FILE = Path(".errorfare_hunter_state.json")

MAX_EFA_PAGES = 12
MAX_SF_PAGES = 3
MAX_ARTICLE_FETCHES = 12
REQUEST_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (compatible; ErrorFareHunter/Final; +https://github.com/)"
)

# Alert policy: high recall, still selective enough for Telegram.
ALERT_THRESHOLD = 65
URGENT_THRESHOLD = 80

# ============================================================
# KEYWORDS
# ============================================================

ASIA = {
    "japan", "tokyo", "tokio", "osaka", "kyoto", "fukuoka", "nagoya",
    "south korea", "seoul", "busan",
    "china", "beijing", "shanghai", "guangzhou", "shenzhen",
    "hong kong", "taiwan", "taipei",
    "thailand", "bangkok", "phuket", "chiang mai",
    "vietnam", "hanoi", "ho chi minh", "saigon", "danang", "da nang",
    "singapore", "malaysia", "kuala lumpur", "penang",
    "indonesia", "bali", "jakarta",
    "philippines", "manila", "cebu",
    "india", "mumbai", "delhi", "goa",
    "nepal", "kathmandu", "sri lanka", "colombo",
    "cambodia", "phnom penh", "siem reap",
    "maldives", "male", "mongolia", "ulaanbaatar",
    "uzbekistan", "tashkent", "kazakhstan", "almaty",
    "kyrgyzstan", "bishkek", "asia",
}
NORTH_AMERICA = {
    "usa", "united states", "new york", "los angeles", "chicago", "miami",
    "boston", "san francisco", "seattle", "las vegas", "washington",
    "canada", "toronto", "vancouver", "montreal", "mexico", "cancun",
}
SOUTH_AMERICA = {
    "brazil", "sao paulo", "são paulo", "rio de janeiro", "argentina",
    "buenos aires", "chile", "santiago", "peru", "lima", "colombia", "bogota",
}
OCEANIA = {
    "australia", "sydney", "melbourne", "brisbane", "perth", "new zealand",
    "auckland", "christchurch",
}
AFRICA = {
    "south africa", "cape town", "johannesburg", "kenya", "mombasa", "nairobi",
    "tanzania", "zanzibar", "morocco", "marrakesh", "egypt", "cairo",
}
MIDDLE_EAST = {
    "uae", "dubai", "abu dhabi", "qatar", "doha", "saudi arabia", "jeddah",
    "riyadh", "oman", "muscat", "israel", "tel aviv", "jordan", "amman",
}
PREFERRED = {
    "düsseldorf", "duesseldorf", "dus", "köln", "cologne", "cgn",
    "frankfurt", "fra", "berlin", "ber", "hamburg", "ham",
    "münchen", "munich", "muc", "amsterdam", "ams", "eindhoven", "ein",
    "brussels", "brüssel", "bru", "luxembourg", "lux",
}
SHORT_HAUL = {
    "mallorca", "palma", "london", "paris", "rome", "madrid", "barcelona",
    "lisbon", "vienna", "zurich", "basel", "prague", "budapest", "amsterdam",
    "brussels", "brüssel", "berlin", "düsseldorf", "frankfurt",
}

GENERIC_FOOTER = (
    "error fares are price errors in travel deals",
    "our algorithm detects error fares",
    "notifies you immediately",
    "sign up for free now",
    "errorfarealerts.com",
    "errorfarealerts",
)

ERROR_SIGNALS = (
    "error fare", "error-fare", "mistake fare", "mistake-fare",
    "pricing mistake", "price error", "pricing error",
    "fehlerpreis", "preisfehler", "price mistake", "fare error",
    "fuel dump", "fuel dumping",
)

GENERIC_DEAL_WORDS = (
    "deal", "preiskracher", "kracher", "last-minute", "angebot",
    "flugdeal", "top-deal", "mega-deal", "sale",
)

LONG_HAUL_REGIONS = {
    "Asien", "Nordamerika", "Südamerika", "Australien/Ozeanien", "Afrika"
}

# ============================================================
# DATA
# ============================================================

@dataclass
class Deal:
    key: str
    source: str
    source_id: str
    source_url: str
    article_url: str | None
    title: str
    text: str
    price: float | None
    cabin: str
    airline: str | None
    origin: str | None
    destination: str | None
    travel_dates: str | None
    baggage: str | None
    stops: str | None
    explicit_error: bool
    region: str
    preferred_departure: bool
    short_haul: bool
    score: int
    level: str
    reasons: list[str]

# ============================================================
# NETWORK / HTML
# ============================================================

def fetch(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "de,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="replace")


def clean_html(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<style.*?</style>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(fragment).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()

# ============================================================
# PARSING
# ============================================================

def normalize_price_value(value: str) -> float | None:
    try:
        s = value.strip().replace("\xa0", "")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        elif s.count(".") > 1:
            s = s.replace(".", "")
        return float(s)
    except Exception:
        return None


def extract_prices(text: str) -> list[float]:
    patterns = [
        r"(?:€|EUR)\s*([0-9][0-9.,]*)",
        r"([0-9][0-9.,]*)\s*(?:€|EUR)",
        r"ab\s*([0-9][0-9.,]*)\s*(?:Euro|EUR|€)",
    ]
    values: list[float] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            price = normalize_price_value(match.group(1))
            if price is not None and 1 <= price <= 100000:
                values.append(price)
    return sorted(set(values))


def extract_price(text: str) -> float | None:
    prices = extract_prices(text)
    return min(prices) if prices else None


def detect_cabin(text: str) -> str:
    t = text.lower()
    if re.search(r"\bfirst\s*-?class\b|\bfirst class\b", t):
        return "First"
    if re.search(r"\bbusiness\s*-?class\b|\bbusiness class\b", t):
        return "Business"
    if re.search(r"\bpremium\s*-?economy\b|\bpremium economy\b", t):
        return "Premium Economy"
    if re.search(r"\beconomy\s*-?class\b|\beconomy class\b|\beconomy\b", t):
        return "Economy"
    return "Unbekannt"


def remove_generic_footer(text: str) -> str:
    cleaned = text.lower()
    for phrase in GENERIC_FOOTER:
        cleaned = cleaned.replace(phrase, " ")
    return cleaned


def detect_explicit_error(title: str, article_text: str, source_is_error_page: bool = False) -> bool:
    if source_is_error_page:
        return True
    scope = remove_generic_footer((title + "\n" + article_text[:5000]).lower())
    return any(signal in scope for signal in ERROR_SIGNALS)


def region_for(text: str) -> str:
    t = text.lower()
    if any(x in t for x in ASIA):
        return "Asien"
    if any(x in t for x in NORTH_AMERICA):
        return "Nordamerika"
    if any(x in t for x in SOUTH_AMERICA):
        return "Südamerika"
    if any(x in t for x in OCEANIA):
        return "Australien/Ozeanien"
    if any(x in t for x in AFRICA):
        return "Afrika"
    if any(x in t for x in MIDDLE_EAST):
        return "Nahost"
    return "Europa/sonstige"


def has_any(text: str, words: Iterable[str]) -> bool:
    t = text.lower()
    return any(word in t for word in words)


def first_label(text: str, labels: Iterable[str]) -> str | None:
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*[:：]\s*([^\n]{3,160})",
            text,
            flags=re.I,
        )
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip(" .")
    return None


def extract_meta_from_article(url: str | None) -> tuple[str, dict[str, str]]:
    if not url:
        return "", {}
    try:
        page = fetch(url)
    except Exception as exc:
        print(f"Artikelabruf fehlgeschlagen: {type(exc).__name__}")
        return "", {}

    title = ""
    match = re.search(r"<h1[^>]*>(.*?)</h1>", page, flags=re.S | re.I)
    if match:
        title = clean_html(match.group(1))
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.S | re.I)
        if match:
            title = clean_html(match.group(1))

    text = clean_html(page)
    details: dict[str, str] = {}

    for field, labels in {
        "airline": ("Airline", "Airlines"),
        "cabin": ("Reiseklasse", "Bookingclass", "Cabin", "Cabin class"),
        "travel_dates": ("Reisezeitraum", "Zeitraum", "Travel period", "Dates"),
        "stops": ("Umstieg", "Stops", "Stopover"),
    }.items():
        value = first_label(text, labels)
        if value:
            details[field] = value

    baggage_lines = [
        line.strip()
        for line in text.splitlines()
        if re.search(r"Handgepäck|Aufgabegepäck|Gepäck|baggage", line, flags=re.I)
    ]
    if baggage_lines:
        details["baggage"] = " / ".join(baggage_lines[:3])

    route = re.search(
        r"(?:von|from)\s+(.{2,120}?\(([A-Z]{3})\))\s+"
        r"(?:nach|to)\s+(.{2,140}?\(([A-Z]{3})\))",
        text,
        flags=re.I,
    )
    if route:
        details["origin"] = re.sub(r"\s+", " ", route.group(1)).strip()
        details["destination"] = re.sub(r"\s+", " ", route.group(3)).strip()

    if "origin" not in details:
        origin = first_label(text, ("Abflug", "From", "Origin"))
        if origin:
            details["origin"] = origin
    if "destination" not in details:
        destination = first_label(text, ("Ziel", "To", "Destination"))
        if destination:
            details["destination"] = destination

    return title, details

# ============================================================
# SOURCES
# ============================================================

def extract_efa_posts(page: str) -> list[dict]:
    marks = list(re.finditer(r'data-post="' + re.escape(EFA_CHANNEL) + r'/(\d+)"', page))
    posts: list[dict] = []
    for i, mark in enumerate(marks):
        chunk = page[mark.start(): marks[i + 1].start() if i + 1 < len(marks) else len(page)]
        post_id = int(mark.group(1))
        text_match = re.search(
            r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>',
            chunk,
            flags=re.S | re.I,
        )
        text = clean_html(text_match.group(1) if text_match else chunk)
        urls = re.findall(r'href="(https?://[^" ]+)"', chunk, flags=re.I)
        article_url = None
        for raw in urls:
            url = html_lib.unescape(raw)
            if "errorfarealerts.com/" in url and "/newsletter/" not in url:
                article_url = url
                break
        time_match = re.search(r'<time[^>]+datetime="([^"]+)"', chunk, flags=re.I)
        posts.append({
            "id": post_id,
            "source_id": str(post_id),
            "source": "ErrorFareAlerts",
            "source_url": f"https://t.me/{EFA_CHANNEL}/{post_id}",
            "article_url": article_url,
            "text": text,
            "published_at": time_match.group(1) if time_match else None,
        })
    posts.sort(key=lambda item: item["id"])
    return posts


def fetch_efa_since(last_seen: int) -> tuple[list[dict], int, bool]:
    found: dict[int, dict] = {}
    before: int | None = None
    latest = last_seen
    reached = last_seen == 0

    for _ in range(MAX_EFA_PAGES):
        url = EFA_FEED_URL if before is None else f"{EFA_FEED_URL}?before={before}"
        page = fetch(url)
        posts = extract_efa_posts(page)
        if not posts:
            break
        for post in posts:
            found[post["id"]] = post
            latest = max(latest, post["id"])
        oldest = min(post["id"] for post in posts)
        if last_seen == 0 or oldest <= last_seen or any(post["id"] == last_seen for post in posts):
            reached = True
            break
        before = oldest
        time.sleep(0.2)

    new_posts = [post for pid, post in sorted(found.items()) if pid > last_seen]
    return new_posts, latest, reached


def extract_sf_links(page: str) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, label in re.findall(r'href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.S | re.I):
        href = html_lib.unescape(href)
        label = clean_html(label)
        if href.startswith("/"):
            href = SF_BASE + href
        if not href.startswith(SF_BASE + "/"):
            continue
        if any(part in href for part in ("/error-fares/", "/category/", "/wp-content/", "/tag/")):
            continue
        if href.rstrip("/") == SF_BASE:
            continue
        if href in seen:
            continue
        seen.add(href)
        if label:
            results.append((href, label))
    return results


def fetch_sf_recent() -> list[dict]:
    found: dict[str, dict] = {}
    for page_no in range(1, MAX_SF_PAGES + 1):
        url = SF_ERROR_URL if page_no == 1 else f"{SF_ERROR_URL.rstrip('/')}/page/{page_no}/"
        try:
            page = fetch(url)
        except Exception as exc:
            print(f"Secret Flying Quelle fehlgeschlagen (Seite {page_no}): {type(exc).__name__}")
            continue
        for url2, label in extract_sf_links(page):
            found[url2] = {
                "source": "SecretFlying",
                "source_id": url2,
                "source_url": url2,
                "article_url": url2,
                "text": label,
                "published_at": None,
            }
    return list(found.values())

# ============================================================
# SCORING
# ============================================================

def score_deal(title: str, text: str, price: float | None, cabin: str, explicit_error: bool) -> tuple[int, str, list[str]]:
    full = f"{title}\n{text}".lower()
    if "newsletter" in full:
        return 0, "IGNORE", ["Newsletter"]

    region = region_for(full)
    asia = region == "Asien"
    long_haul = region in LONG_HAUL_REGIONS
    middle = region == "Nahost"
    short = has_any(full, SHORT_HAUL) and not long_haul
    preferred = has_any(full, PREFERRED)

    score = 0
    reasons: list[str] = []

    if explicit_error:
        score += 25
        reasons.append("Error-Fare/Mistake-Fare-Signal")

    if price is not None:
        if cabin == "First":
            if asia and price <= 900:
                score += 70
                reasons.append("First Class Asien ≤900€")
            elif long_haul and price <= 800:
                score += 62
                reasons.append("First Class Langstrecke ≤800€")
            elif price <= 600:
                score += 40
                reasons.append("First Class ≤600€")

        elif cabin == "Business":
            if asia:
                if price <= 400:
                    score += 75
                    reasons.append("Business Asien ≤400€")
                elif price <= 500:
                    score += 68
                    reasons.append("Business Asien ≤500€")
                elif price <= 600:
                    score += 58
                    reasons.append("Business Asien ≤600€")
                elif price <= 750:
                    score += 42
                    reasons.append("Business Asien ≤750€")
            elif long_haul:
                if price <= 400:
                    score += 72
                    reasons.append("Langstrecken-Business ≤400€")
                elif price <= 500:
                    score += 65
                    reasons.append("Langstrecken-Business ≤500€")
                elif price <= 600:
                    score += 52
                    reasons.append("Langstrecken-Business ≤600€")
                elif price <= 700:
                    score += 38
                    reasons.append("Langstrecken-Business ≤700€")
            elif middle:
                if price <= 450:
                    score += 55
                    reasons.append("Nahost-Business ≤450€")
                elif price <= 600:
                    score += 38
                    reasons.append("Nahost-Business ≤600€")
            else:
                if price <= 180:
                    score += 42
                    reasons.append("Business ≤180€")
                elif price <= 250:
                    score += 34
                    reasons.append("Business ≤250€")
                elif price <= 300:
                    score += 22
                    reasons.append("Business ≤300€")

        elif cabin == "Premium Economy":
            if asia and price <= 450:
                score += 48
                reasons.append("Premium Economy Asien ≤450€")
            elif long_haul and price <= 400:
                score += 38
                reasons.append("Premium Economy Langstrecke ≤400€")

        elif cabin == "Economy":
            if asia:
                if price <= 150:
                    score += 75
                    reasons.append("Economy Asien ≤150€")
                elif price <= 200:
                    score += 68
                    reasons.append("Economy Asien ≤200€")
                elif price <= 250:
                    score += 58
                    reasons.append("Economy Asien ≤250€")
                elif price <= 300:
                    score += 46
                    reasons.append("Economy Asien ≤300€")
                elif price <= 350:
                    score += 32
                    reasons.append("Economy Asien ≤350€")
                elif price <= 400:
                    score += 22
                    reasons.append("Economy Asien ≤400€")
            elif long_haul:
                if price <= 150:
                    score += 65
                    reasons.append("Langstrecken-Economy ≤150€")
                elif price <= 200:
                    score += 58
                    reasons.append("Langstrecken-Economy ≤200€")
                elif price <= 250:
                    score += 47
                    reasons.append("Langstrecken-Economy ≤250€")
                elif price <= 300:
                    score += 34
                    reasons.append("Langstrecken-Economy ≤300€")
                elif price <= 350:
                    score += 22
                    reasons.append("Langstrecken-Economy ≤350€")
            elif middle:
                if price <= 180:
                    score += 48
                    reasons.append("Nahost-Economy ≤180€")
                elif price <= 220:
                    score += 36
                    reasons.append("Nahost-Economy ≤220€")
            else:
                # Ordinary cheap European Economy is intentionally not an alert.
                if price <= 40 and short:
                    score += 3

        else:
            if asia:
                if price <= 180:
                    score += 55
                    reasons.append("Asien ≤180€ (Klasse unbekannt)")
                elif price <= 250:
                    score += 45
                    reasons.append("Asien ≤250€ (Klasse unbekannt)")
                elif price <= 300:
                    score += 34
                    reasons.append("Asien ≤300€ (Klasse unbekannt)")
            elif long_haul:
                if price <= 180:
                    score += 58
                    reasons.append("Langstrecke ≤180€ (Klasse unbekannt)")
                elif price <= 250:
                    score += 46
                    reasons.append("Langstrecke ≤250€ (Klasse unbekannt)")
                elif price <= 300:
                    score += 32
                    reasons.append("Langstrecke ≤300€ (Klasse unbekannt)")

    if asia:
        score += 10
        reasons.append("Asien priorisiert")
    if preferred:
        score += 6
        reasons.append("bevorzugter Abflug")
    if middle:
        score += 3
        reasons.append("Nahost")

    # Europe short-haul suppression; do not punish extraordinary Business errors.
    if short and cabin == "Economy" and not explicit_error:
        score -= 25
        reasons.append("Europa-Kurzstrecke abgewertet")
    elif short and cabin == "Unbekannt" and not explicit_error:
        score -= 10
        reasons.append("Kurzstrecke abgewertet")

    # Normal marketing language is never evidence of an error.
    if any(word in title.lower() for word in GENERIC_DEAL_WORDS) and not explicit_error:
        score -= 2

    score = max(0, min(100, int(score)))
    if score >= URGENT_THRESHOLD:
        level = "🚨 SOFORT-ALARM"
    elif score >= ALERT_THRESHOLD:
        level = "🔥 ERROR-FARE-KANDIDAT"
    elif score >= 45:
        level = "🟡 BEOBACHTEN"
    else:
        level = "❌ IGNORIEREN"
    return score, level, reasons

# ============================================================
# BUILD / FORMAT
# ============================================================

def build_deal(item: dict, source_is_error_page: bool = False) -> Deal:
    article_title, details = extract_meta_from_article(item.get("article_url")) if item.get("article_url") else ("", {})
    effective_title = article_title or item.get("text", "").split("\n", 1)[0].strip()
    merged = "\n".join([
        effective_title,
        item.get("text", ""),
        details.get("origin", ""),
        details.get("destination", ""),
        details.get("airline", ""),
        details.get("travel_dates", ""),
        details.get("cabin", ""),
        details.get("baggage", ""),
        details.get("stops", ""),
    ])

    price = extract_price(merged)
    cabin = details.get("cabin") or detect_cabin(merged)
    cabin_lower = cabin.lower()
    if cabin_lower.startswith("business"):
        cabin = "Business"
    elif cabin_lower.startswith("economy"):
        cabin = "Economy"
    elif cabin_lower.startswith("premium"):
        cabin = "Premium Economy"
    elif cabin_lower.startswith("first"):
        cabin = "First"
    else:
        cabin = detect_cabin(merged)

    explicit_error = detect_explicit_error(
        effective_title,
        item.get("text", "") + "\n" + article_title,
        source_is_error_page=source_is_error_page,
    )
    region = region_for(merged)
    preferred = has_any(merged, PREFERRED)
    short = has_any(merged, SHORT_HAUL) and region not in LONG_HAUL_REGIONS

    score, level, reasons = score_deal(
        effective_title,
        merged,
        price,
        cabin,
        explicit_error,
    )

    if details.get("origin") and details.get("destination"):
        reasons.append(f"{details['origin']} → {details['destination']}")

    key = f"{item['source']}:{item['source_id']}"
    return Deal(
        key=key,
        source=item["source"],
        source_id=item["source_id"],
        source_url=item["source_url"],
        article_url=item.get("article_url"),
        title=effective_title,
        text=item.get("text", ""),
        price=price,
        cabin=cabin,
        airline=details.get("airline"),
        origin=details.get("origin"),
        destination=details.get("destination"),
        travel_dates=details.get("travel_dates"),
        baggage=details.get("baggage"),
        stops=details.get("stops"),
        explicit_error=explicit_error,
        region=region,
        preferred_departure=preferred,
        short_haul=short,
        score=score,
        level=level,
        reasons=reasons[:10],
    )


def format_alert(deal: Deal) -> str:
    price = f"{deal.price:.0f} €" if deal.price is not None else "Preis nicht erkannt"
    lines = [
        f"{deal.level}",
        "",
        f"💰 Preis: {price}",
        f"💺 Klasse: {deal.cabin}",
        f"📊 Score: {deal.score}/100",
        f"🌍 Region: {deal.region}",
    ]
    if deal.origin or deal.destination:
        lines.append(f"✈️ Route: {deal.origin or '?'} → {deal.destination or '?'}")
    if deal.airline:
        lines.append(f"🏷️ Airline: {deal.airline}")
    if deal.travel_dates:
        lines.append(f"📅 Termine: {deal.travel_dates}")
    if deal.stops:
        lines.append(f"🔄 Umstieg: {deal.stops}")
    if deal.baggage:
        lines.append(f"🧳 Gepäck: {deal.baggage}")
    lines.append("")
    lines.append("🔎 Warum:")
    lines.extend(f"• {reason}" for reason in deal.reasons[:8])
    lines.append("")
    lines.append("⚠️ Preis sofort beim Anbieter prüfen – Error Fares können sehr schnell verschwinden.")
    if deal.article_url:
        lines.append(f"🔗 Deal: {deal.article_url}")
    lines.append(f"🔗 Quelle: {deal.source_url}")
    return "\n".join(lines)[:3900]

# ============================================================
# STATE
# ============================================================

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "initialized": False,
            "efa_last_seen": 0,
            "sf_seen": [],
            "sent_keys": [],
            "pending": {},
        }
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Ungültiger State")
        state.setdefault("initialized", False)
        state.setdefault("efa_last_seen", 0)
        state.setdefault("sf_seen", [])
        state.setdefault("sent_keys", [])
        state.setdefault("pending", {})
        return state
    except Exception:
        return {
            "initialized": False,
            "efa_last_seen": 0,
            "sf_seen": [],
            "sent_keys": [],
            "pending": {},
        }


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Telegram-Secrets fehlen.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": "false",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok"):
            print("Telegram: erfolgreich gesendet.")
            return True
        print("Telegram API Fehler:", result)
        return False
    except Exception as exc:
        print("Telegram-Verbindungsfehler:", type(exc).__name__)
        return False

# ============================================================
# MAIN
# ============================================================

def main(test_latest: bool = False) -> int:
    state = load_state()
    sent = set(str(x) for x in state.get("sent_keys", []))
    pending = state.get("pending", {}) or {}

    print("=== ERROR FARE HUNTER FINAL ===")
    print(f"Alert-Schwelle: {ALERT_THRESHOLD}")
    print(f"Sofort-Alarm: {URGENT_THRESHOLD}")

    # ----------------------------
    # Initial baseline
    # ----------------------------
    if not state.get("initialized", False):
        try:
            efa_now = extract_efa_posts(fetch(EFA_FEED_URL))
            state["efa_last_seen"] = max((p["id"] for p in efa_now), default=0)
        except Exception as exc:
            print("ErrorFareAlerts Initialisierung fehlgeschlagen:", type(exc).__name__)
            return 1

        try:
            sf_now = fetch_sf_recent()
            state["sf_seen"] = [p["source_id"] for p in sf_now][-300:]
        except Exception as exc:
            print("Secret Flying Initialisierung fehlgeschlagen:", type(exc).__name__)
            state["sf_seen"] = []

        state["initialized"] = True
        state["sent_keys"] = []
        state["pending"] = {}
        save_state(state)

        print(f"Erstinitialisierung: EFA-Basis {state['efa_last_seen']}, SF-Basis {len(state['sf_seen'])} Artikel")

        if not test_latest:
            print("Baseline gesetzt – noch kein historischer Alarm.")
            return 0

        # Manual smoke test: evaluate current feeds, send max one.
        candidates: list[Deal] = []
        for item in efa_now[-20:]:
            if item.get("article_url"):
                candidates.append(build_deal(item, source_is_error_page=False))
        for item in sf_now[-20:]:
            candidates.append(build_deal(item, source_is_error_page=True))
        candidates = [d for d in candidates if d.score >= ALERT_THRESHOLD]
        if candidates:
            candidates.sort(key=lambda d: d.score, reverse=True)
            chosen = candidates[0]
            print(f"TESTALARM: {chosen.key} {chosen.score}/100")
            if send_telegram(format_alert(chosen)):
                sent.add(chosen.key)
                state["sent_keys"] = sorted(sent)[-500:]
                save_state(state)
        else:
            print("TESTALARM: Kein Kandidat über Schwelle.")
        return 0

    # ----------------------------
    # New ErrorFareAlerts posts
    # ----------------------------
    last_efa = int(state.get("efa_last_seen", 0))
    try:
        efa_posts, latest_efa, reached = fetch_efa_since(last_efa)
    except Exception as exc:
        print("ErrorFareAlerts Abruf fehlgeschlagen:", type(exc).__name__)
        efa_posts, latest_efa, reached = [], last_efa, False

    print(f"EFA letzte ID: {last_efa}")
    print(f"EFA neue Beiträge: {len(efa_posts)}")
    if not reached:
        print("WARNUNG: EFA-Pagination hat die letzte bekannte ID nicht erreicht.")
    else:
        state["efa_last_seen"] = max(last_efa, latest_efa)

    # ----------------------------
    # Secret Flying current error-fare listings
    # ----------------------------
    sf_seen = set(str(x) for x in state.get("sf_seen", []))
    try:
        sf_current = fetch_sf_recent()
    except Exception as exc:
        print("Secret Flying Abruf fehlgeschlagen:", type(exc).__name__)
        sf_current = []

    sf_new = [p for p in sf_current if str(p["source_id"]) not in sf_seen]
    print(f"Secret Flying neue/noch nicht gesehene Artikel: {len(sf_new)}")

    # ----------------------------
    # Retry pending Telegram
    # ----------------------------
    if pending:
        print(f"Ausstehende Telegram-Meldungen: {len(pending)}")
        for key, payload in list(pending.items())[:5]:
            msg = payload.get("message") if isinstance(payload, dict) else None
            if msg and send_telegram(msg):
                sent.add(key)
                pending.pop(key, None)

    # ----------------------------
    # Build candidates
    # ----------------------------
    candidates: list[Deal] = []
    article_fetches = 0

    for item in efa_posts:
        key = f"{item['source']}:{item['source_id']}"
        if key in sent or key in pending:
            continue

        quick_text = item.get("text", "")
        quick_price = extract_price(quick_text)
        quick_cabin = detect_cabin(quick_text)
        quick_region = region_for(quick_text)
        plausible = (
            quick_cabin in {"Business", "First"}
            or quick_region in LONG_HAUL_REGIONS
            or (quick_price is not None and quick_price <= 350)
        )

        if not plausible:
            continue
        if not item.get("article_url"):
            deal = build_deal(item)
        elif article_fetches < MAX_ARTICLE_FETCHES:
            article_fetches += 1
            deal = build_deal(item)
        else:
            deal = build_deal({**item, "article_url": None})

        if deal.score >= ALERT_THRESHOLD:
            candidates.append(deal)

    for item in sf_new:
        key = f"{item['source']}:{item['source_id']}"
        if key in sent or key in pending:
            continue
        if article_fetches >= MAX_ARTICLE_FETCHES:
            break
        article_fetches += 1
        deal = build_deal(item, source_is_error_page=True)
        if deal.score >= ALERT_THRESHOLD:
            candidates.append(deal)

    # ----------------------------
    # Cross-source dedupe by normalized title
    # ----------------------------
    unique: dict[str, Deal] = {}
    for deal in candidates:
        norm = re.sub(r"[^a-z0-9]+", " ", deal.title.lower()).strip()
        norm = re.sub(r"\b(error|fare|deal|flugdeal|from|ab|only|roundtrip|return)\b", " ", norm)
        norm = re.sub(r"\s+", " ", norm).strip()
        dedupe_key = norm[:180] or deal.key
        if dedupe_key not in unique or deal.score > unique[dedupe_key].score:
            unique[dedupe_key] = deal

    ordered = sorted(unique.values(), key=lambda d: d.score, reverse=True)
    print(f"Relevante Kandidaten: {len(ordered)}")

    # ----------------------------
    # Queue and send
    # ----------------------------
    for deal in ordered[:8]:
        if deal.key in sent or deal.key in pending:
            continue
        pending[deal.key] = {
            "message": format_alert(deal),
            "score": deal.score,
            "created_at": int(time.time()),
        }

    for key, payload in sorted(
        pending.items(),
        key=lambda item: int(item[1].get("score", 0)),
        reverse=True,
    )[:8]:
        if key in sent:
            pending.pop(key, None)
            continue
        if send_telegram(payload.get("message", "")):
            sent.add(key)
            pending.pop(key, None)

    # Mark newly observed SF items as seen only after the source was fetched.
    sf_seen.update(str(p["source_id"]) for p in sf_current)

    # Bound state size.
    state["sf_seen"] = list(sorted(sf_seen))[-500:]
    state["sent_keys"] = list(sorted(sent))[-500:]
    state["pending"] = dict(list(pending.items())[-30:])
    save_state(state)

    print(f"Neue Telegram-Meldungen: {len(sent) - len(set(str(x) for x in state.get('sent_keys', []))) if False else 'siehe Telegram-Logs'}")
    print(f"Gespeicherte Meldungen: {len(state['sent_keys'])}")
    print(f"Ausstehende Meldungen: {len(state['pending'])}")
    print("SCAN ABGESCHLOSSEN")
    return 0


if __name__ == "__main__":
    test_latest = os.environ.get("TEST_LATEST", "0") == "1"
    raise SystemExit(main(test_latest=test_latest))
