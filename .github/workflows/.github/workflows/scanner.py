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
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

CHANNEL = "errorfarealerts"
FEED_URL = f"https://t.me/s/{CHANNEL}"
STATE_FILE = Path(".errorfare_state.json")
MAX_PAGES = 12
MAX_ARTICLE_FETCHES = 8
ALERT_THRESHOLD = 40
ERROR_THRESHOLD = 75
STRONG_THRESHOLD = 60
REQUEST_TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; ErrorFareHunter/5.0; +https://github.com/)"

ASIA = {
    "japan", "tokyo", "tokio", "osaka", "kyoto", "fukuoka", "nagoya",
    "south korea", "seoul", "busan",
    "china", "beijing", "shanghai", "guangzhou", "shenzhen",
    "hong kong", "taiwan", "taipei",
    "thailand", "bangkok", "phuket", "chiang mai",
    "vietnam", "hanoi", "ho chi minh", "saigon", "danang",
    "singapore", "malaysia", "kuala lumpur", "penang",
    "indonesia", "bali", "jakarta",
    "philippines", "manila", "cebu",
    "india", "mumbai", "delhi", "goa",
    "nepal", "kathmandu", "sri lanka", "colombo",
    "cambodia", "phnom penh", "siem reap",
    "maldives", "male", "mongolia", "ulaanbaatar",
    "uzbekistan", "tashkent", "kazakhstan", "almaty",
    "kyrgyzstan", "bishkek",
    "asia",
}
NORTH_AMERICA = {
    "usa", "united states", "new york", "los angeles", "chicago", "miami",
    "boston", "san francisco", "seattle", "las vegas", "washington",
    "canada", "toronto", "vancouver", "montreal", "mexico", "cancun",
}
SOUTH_AMERICA = {
    "brazil", "sao paulo", "rio de janeiro", "argentina", "buenos aires",
    "chile", "santiago", "peru", "lima", "colombia", "bogota",
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
)
LOW_SIGNAL_WORDS = ("deal", "preiskracher", "kracher", "last-minute", "angebot", "flugdeal", "top-deal", "mega-deal")

@dataclass
class Deal:
    post_id: str
    telegram_url: str
    article_url: str | None
    published_at: str | None
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


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "de,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def clean_html(fragment: str) -> str:
    fragment = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<style.*?</style>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"</p>|</div>|</li>|</h[1-6]>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(fragment)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def extract_posts(page: str) -> list[tuple[int, str, str, str | None]]:
    marks = list(re.finditer(r'data-post="' + re.escape(CHANNEL) + r'/(\d+)"', page))
    posts: list[tuple[int, str, str, str | None]] = []
    for i, m in enumerate(marks):
        chunk = page[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(page)]
        post_id = int(m.group(1))
        text_match = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', chunk, flags=re.S | re.I)
        text = clean_html(text_match.group(1) if text_match else chunk)
        urls = re.findall(r'href="(https?://[^" ]+)"', chunk, flags=re.I)
        article_url = None
        for u in urls:
            u = html_lib.unescape(u)
            if "errorfarealerts.com/" in u and "/newsletter/" not in u:
                article_url = u
                break
        time_match = re.search(r'<time[^>]+datetime="([^"]+)"', chunk, flags=re.I)
        published_at = time_match.group(1) if time_match else None
        posts.append((post_id, f"https://t.me/{CHANNEL}/{post_id}", text, article_url))
    # Newest first on Telegram web; normalize oldest -> newest.
    posts.sort(key=lambda x: x[0])
    return posts


def fetch_posts_since(last_seen: int) -> tuple[list[tuple[int, str, str, str | None]], int, bool]:
    all_posts: dict[int, tuple[int, str, str, str | None]] = {}
    before: int | None = None
    reached = last_seen == 0
    latest = last_seen
    for _ in range(MAX_PAGES):
        url = FEED_URL if before is None else f"{FEED_URL}?before={before}"
        page = fetch(url)
        posts = extract_posts(page)
        if not posts:
            break
        for p in posts:
            all_posts[p[0]] = p
            latest = max(latest, p[0])
        oldest = min(p[0] for p in posts)
        if any(p[0] == last_seen for p in posts):
            reached = True
            break
        if oldest <= last_seen:
            reached = True
            break
        before = oldest
        time.sleep(0.2)
    new_posts = [p for pid, p in sorted(all_posts.items()) if pid > last_seen]
    return new_posts, latest, reached


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


def extract_price(text: str) -> float | None:
    patterns = [
        r"(?:€|EUR)\s*([0-9][0-9.,]*)",
        r"([0-9][0-9.,]*)\s*(?:€|EUR)",
        r"ab\s*([0-9][0-9.,]*)\s*(?:Euro|EUR|€)",
    ]
    values = []
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.I):
            p = normalize_price_value(m.group(1))
            if p is not None:
                values.append(p)
    return min(values) if values else None


def detect_cabin(text: str) -> str:
    t = text.lower()
    if re.search(r"\bfirst\s*-?class\b|\bfirst\b", t):
        return "First"
    if re.search(r"\bbusiness\s*-?class\b|\bbusiness\b", t):
        return "Business"
    if re.search(r"\beconomy\s*-?class\b|\beconomy\b", t):
        return "Economy"
    return "Unbekannt"


def remove_generic_footer(text: str) -> str:
    cleaned = text.lower()
    for phrase in GENERIC_FOOTER:
        cleaned = cleaned.replace(phrase, " ")
    return cleaned


def detect_explicit_error(title: str, article_text: str) -> bool:
    scope = remove_generic_footer((title + "\n" + article_text[:3500]).lower())
    return any(signal in scope for signal in ERROR_SIGNALS)


def region_for(text: str) -> str:
    t = text.lower()
    if any(x in t for x in ASIA):
        return "Asien"
    if any(x in t for x in NORTH_AMERICA):
        return "Nordamerika"
    if any(x in t for x in OCEANIA):
        return "Australien/Ozeanien"
    if any(x in t for x in SOUTH_AMERICA):
        return "Südamerika"
    if any(x in t for x in AFRICA):
        return "Afrika"
    if any(x in t for x in MIDDLE_EAST):
        return "Nahost"
    return "Europa/sonstige"


def bool_keyword(text: str, keywords: Iterable[str]) -> bool:
    t = text.lower()
    return any(x in t for x in keywords)


def first_label(text: str, labels: Iterable[str]) -> str | None:
    for label in labels:
        m = re.search(rf"{re.escape(label)}\s*[:：]\s*([^\n]{3,120})", text, flags=re.I)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip(" .")
    return None


def extract_article_details(url: str | None) -> tuple[str, dict[str, str]]:
    if not url:
        return "", {}
    try:
        page = fetch(url)
    except Exception as exc:
        print(f"Artikelabruf fehlgeschlagen: {type(exc).__name__}")
        return "", {}
    title = ""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", page, flags=re.S | re.I)
    if m:
        title = clean_html(m.group(1))
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.S | re.I)
        if m:
            title = clean_html(m.group(1))
    text = clean_html(page)
    details: dict[str, str] = {}
    airline = first_label(text, ("Airline",))
    if airline:
        details["airline"] = airline
    cabin = first_label(text, ("Reiseklasse", "Bookingclass", "Cabin"))
    if cabin:
        details["cabin"] = cabin
    travel = first_label(text, ("Reisezeitraum", "Zeitraum", "Travel period"))
    if travel:
        details["travel_dates"] = travel
    baggage_lines = []
    for line in text.splitlines():
        if re.search(r"Handgepäck|Aufgabegepäck|Gepäck", line, flags=re.I):
            baggage_lines.append(line.strip())
    if baggage_lines:
        details["baggage"] = " / ".join(baggage_lines[:3])
    stops = first_label(text, ("Umstieg", "Stops"))
    if stops:
        details["stops"] = stops
    # Best-detail route/date/price when present.
    route_m = re.search(
        r"von\s+(.{2,100}?\(([A-Z]{3})\))\s+nach\s+(.{2,120}?\(([A-Z]{3})\))",
        text,
        flags=re.I,
    )
    if route_m:
        details["origin"] = re.sub(r"\s+", " ", route_m.group(1)).strip()
        details["destination"] = re.sub(r"\s+", " ", route_m.group(3)).strip()
    # Flexible route form without parentheses.
    if "origin" not in details:
        m2 = re.search(r"(?:Abflug|from|von)\s*[:：]?\s*([^\n]{3,100})", text, flags=re.I)
        if m2:
            details["origin"] = re.sub(r"\s+", " ", m2.group(1)).strip(" .")
        m3 = re.search(r"(?:Ziel|to|nach)\s*[:：]?\s*([^\n]{3,120})", text, flags=re.I)
        if m3:
            details["destination"] = re.sub(r"\s+", " ", m3.group(1)).strip(" .")
    booking = None
    for href, label in re.findall(r'href="([^"]+)"[^>]*>(.*?)</a>', page, flags=re.S | re.I):
        label_clean = clean_html(label).lower()
        if label_clean in {"zum deal", "go to deal"}:
            booking = html_lib.unescape(href)
            break
    if booking:
        details["booking_url"] = booking
    return title, details


def score_deal(title: str, text: str, price: float | None, cabin: str, explicit_error: bool) -> tuple[int, str, list[str]]:
    full = f"{title} {text}".lower()
    if "newsletter" in full:
        return 0, "IGNORE", ["Newsletter"]
    region = region_for(full)
    asia = region == "Asien"
    long_haul = region in {"Asien", "Nordamerika", "Australien/Ozeanien", "Südamerika", "Afrika"}
    middle = region == "Nahost"
    short = bool_keyword(full, SHORT_HAUL) and not long_haul
    preferred = bool_keyword(full, PREFERRED)
    score = 0
    reasons: list[str] = []

    if explicit_error:
        score += 35
        reasons.append("konkreter Error-Fare-Hinweis")

    if price is not None:
        if cabin == "First":
            if asia and price <= 900:
                score += 55; reasons.append("First Asien ≤900€")
            elif long_haul and price <= 800:
                score += 50; reasons.append("First Langstrecke ≤800€")
            elif price <= 600:
                score += 35; reasons.append("First ≤600€")
        elif cabin == "Business":
            if asia:
                if price <= 500:
                    score += 55; reasons.append("Business Asien ≤500€")
                elif price <= 650:
                    score += 42; reasons.append("Business Asien ≤650€")
                elif price <= 800:
                    score += 28; reasons.append("Business Asien ≤800€")
            elif long_haul:
                if price <= 450:
                    score += 55; reasons.append("Langstrecken-Business ≤450€")
                elif price <= 550:
                    score += 42; reasons.append("Langstrecken-Business ≤550€")
                elif price <= 700:
                    score += 28; reasons.append("Langstrecken-Business ≤700€")
            elif middle:
                if price <= 450:
                    score += 45; reasons.append("Nahost-Business ≤450€")
                elif price <= 600:
                    score += 30; reasons.append("Nahost-Business ≤600€")
            else:
                # Short-haul Business can still be anomalously cheap,
                # but never receives the same weight as long-haul.
                if price <= 180:
                    score += 30; reasons.append("Business ≤180€")
                elif price <= 250:
                    score += 22; reasons.append("Business ≤250€")
                elif price <= 300:
                    score += 12; reasons.append("Business ≤300€")
        elif cabin == "Economy":
            if asia:
                if price <= 150:
                    score += 65; reasons.append("Economy Asien ≤150€")
                elif price <= 200:
                    score += 55; reasons.append("Economy Asien ≤200€")
                elif price <= 250:
                    score += 45; reasons.append("Economy Asien ≤250€")
                elif price <= 300:
                    score += 35; reasons.append("Economy Asien ≤300€")
                elif price <= 400:
                    score += 20; reasons.append("Economy Asien ≤400€")
                elif price <= 450:
                    score += 35; reasons.append("Economy Asien ≤450€")
            elif long_haul:
                if price <= 150:
                    score += 55; reasons.append("Langstrecken-Economy ≤150€")
                elif price <= 200:
                    score += 48; reasons.append("Langstrecken-Economy ≤200€")
                elif price <= 250:
                    score += 38; reasons.append("Langstrecken-Economy ≤250€")
                elif price <= 300:
                    score += 26; reasons.append("Langstrecken-Economy ≤300€")
                elif price <= 350:
                    score += 16; reasons.append("Langstrecken-Economy ≤350€")
                elif price <= 400:
                    score += 12; reasons.append("Langstrecken-Economy ≤400€")
            elif middle:
                if price <= 180:
                    score += 48; reasons.append("Nahost-Economy ≤180€")
                elif price <= 220:
                    score += 36; reasons.append("Nahost-Economy ≤220€")
            else:
                # Cheap Europe is not automatically an error fare.
                if price <= 40 and short:
                    score += 4
        else:
            if asia:
                if price <= 200:
                    score += 45; reasons.append("Asien ≤200€")
                elif price <= 300:
                    score += 32; reasons.append("Asien ≤300€")
                elif price <= 400:
                    score += 22; reasons.append("Asien ≤400€")
            elif long_haul:
                if price <= 200:
                    score += 48; reasons.append("Langstrecke ≤200€")
                elif price <= 250:
                    score += 38; reasons.append("Langstrecke ≤250€")
                elif price <= 300:
                    score += 28; reasons.append("Langstrecke ≤300€")
                elif price <= 350:
                    score += 18; reasons.append("Langstrecke ≤350€")

    if asia:
        score += 10
        reasons.append("Asien priorisiert")
    if preferred:
        score += 6
        reasons.append("bevorzugter Abflug")
    if middle:
        score += 3
        reasons.append("Nahost")
    if short and cabin == "Economy" and not explicit_error:
        score -= 25
        reasons.append("Europa-Kurzstrecke abgewertet")
    if short and cabin == "Unbekannt" and not explicit_error:
        score -= 10
        reasons.append("Kurzstrecke abgewertet")
    # Very low-price normal-deal wording is not evidence by itself.
    if any(w in title.lower() for w in LOW_SIGNAL_WORDS) and not explicit_error:
        score -= 2

    score = max(0, min(100, int(score)))
    if score >= ERROR_THRESHOLD:
        level = "🚨 SOFORT-ALARM"
    elif score >= STRONG_THRESHOLD:
        level = "🔥 SEHR STARK"
    elif score >= ALERT_THRESHOLD:
        level = "🟡 INTERESSANT"
    else:
        level = "❌ IGNORIEREN"
    return score, level, reasons


def build_deal(post: tuple[int, str, str, str | None]) -> Deal:
    post_id, telegram_url, tg_text, article_url = post
    title = tg_text.split("\n", 1)[0].strip()
    article_title, details = extract_article_details(article_url)
    effective_title = article_title or title
    merged_text = f"{effective_title}\n{tg_text}\n{details.get('origin','')}\n{details.get('destination','')}\n{details.get('airline','')}\n{details.get('travel_dates','')}\n{details.get('cabin','')}"
    price = extract_price(" ".join([tg_text, article_title, article_title, details.get("price", "")]))
    cabin = details.get("cabin") or detect_cabin(merged_text)
    if cabin.lower().startswith("business"):
        cabin = "Business"
    elif cabin.lower().startswith("economy"):
        cabin = "Economy"
    elif cabin.lower().startswith("first"):
        cabin = "First"
    else:
        cabin = detect_cabin(merged_text)
    airline = details.get("airline")
    origin = details.get("origin")
    destination = details.get("destination")
    travel_dates = details.get("travel_dates")
    baggage = details.get("baggage")
    stops = details.get("stops")
    explicit_error = detect_explicit_error(effective_title, tg_text + "\n" + article_title)
    region = region_for(merged_text)
    preferred = bool_keyword(merged_text, PREFERRED)
    short = bool_keyword(merged_text, SHORT_HAUL) and region not in {"Asien", "Nordamerika", "Australien/Ozeanien", "Südamerika", "Afrika"}
    score, level, reasons = score_deal(effective_title, merged_text, price, cabin, explicit_error)
    if origin and destination:
        route_text = f"{origin} → {destination}"
        if route_text not in reasons:
            reasons.append(route_text)
    return Deal(
        post_id=str(post_id),
        telegram_url=telegram_url,
        article_url=article_url,
        published_at=None,
        title=effective_title,
        text=tg_text,
        price=price,
        cabin=cabin,
        airline=airline,
        origin=origin,
        destination=destination,
        travel_dates=travel_dates,
        baggage=baggage,
        stops=stops,
        explicit_error=explicit_error,
        region=region,
        preferred_departure=preferred,
        short_haul=short,
        score=score,
        level=level,
        reasons=reasons,
    )


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"initialized": False, "last_seen_id": 0, "sent_ids": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"initialized": False, "last_seen_id": 0, "sent_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            result = json.loads(r.read().decode("utf-8"))
        if result.get("ok"):
            print("Telegram: erfolgreich gesendet.")
            return True
        print("Telegram API Fehler:", result)
        return False
    except Exception as exc:
        print("Telegram-Verbindungsfehler:", type(exc).__name__)
        return False


def format_alert(deal: Deal) -> str:
    price = f"{deal.price:.0f} €" if deal.price is not None else "nicht erkannt"
    lines = [
        deal.level,
        "",
        f"💰 Preis: {price} Hin & Rück",
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
    for reason in deal.reasons[:8]:
        lines.append(f"• {reason}")
    lines.append("")
    lines.append("⚠️ Preis als Kandidat erkannt – vor Buchung direkt beim Anbieter prüfen.")
    if deal.article_url:
        lines.append(f"🔗 Deal: {deal.article_url}")
    lines.append(f"🔗 Telegram: {deal.telegram_url}")
    return "\n".join(lines)[:3900]


def main(test_latest: bool = False) -> int:
    state = load_state()
    last_seen = int(state.get("last_seen_id", 0))
    sent_ids = set(str(x) for x in state.get("sent_ids", []))
    pending = state.get("pending_alerts", {}) or {}

    # First installation: establish a baseline without blasting history.
    if not state.get("initialized", False):
        current = extract_posts(fetch(FEED_URL))
        latest_current = max((p[0] for p in current), default=0)
        state["initialized"] = True
        state["last_seen_id"] = latest_current
        state["sent_ids"] = []
        state["pending_alerts"] = {}
        save_state(state)
        print(f"Erstinitialisierung abgeschlossen. Basis-Post: {latest_current}")

        if test_latest:
            # Manual smoke test: evaluate current posts, fetch details for candidates,
            # and send only the single highest-scoring candidate.
            test_deals: list[Deal] = []
            for p in current[-20:]:
                deal = build_deal(p)
                print(f"TEST {deal.post_id}: {deal.price} / {deal.cabin} / {deal.score} / {deal.level}")
                if deal.score >= ALERT_THRESHOLD:
                    test_deals.append(deal)
            if test_deals:
                test_deals.sort(key=lambda d: d.score, reverse=True)
                test_deal = test_deals[0]
                if send_telegram(format_alert(test_deal)):
                    sent_ids.add(test_deal.post_id)
                    state["sent_ids"] = list(sorted(sent_ids))[-500:]
                    save_state(state)
        return 0

    posts, latest, reached = fetch_posts_since(last_seen)
    if not reached:
        print("WARNUNG: Letzte bekannte Post-ID wurde in der Pagination nicht erreicht.")

    print(f"Letzte bekannte ID: {last_seen}")
    print(f"Neue Beiträge: {len(posts)}")

    if latest > last_seen:
        state["last_seen_id"] = latest

    # First retry any pending alert messages from previous runs.
    if pending:
        print(f"Ausstehende Telegram-Meldungen: {len(pending)}")
        retry_items = list(pending.items())[:5]
        for post_id, payload in retry_items:
            msg = payload.get("message") if isinstance(payload, dict) else None
            if msg and send_telegram(msg):
                sent_ids.add(str(post_id))
                pending.pop(str(post_id), None)

    # Evaluate new posts. Only fetch article pages for plausible candidates.
    article_fetches = 0
    for p in posts:
        post_id, telegram_url, tg_text, article_url = p
        if str(post_id) in sent_ids or str(post_id) in pending:
            continue

        quick = tg_text.lower()
        quick_price = extract_price(quick)
        quick_cabin = detect_cabin(quick)
        quick_region = region_for(quick)
        quick_candidate = (
            quick_cabin in {"Business", "First"}
            or quick_region in {"Asien", "Nordamerika", "Australien/Ozeanien", "Südamerika", "Afrika", "Nahost"}
            or (quick_price is not None and quick_price <= 350)
        )

        if article_url and quick_candidate and article_fetches < MAX_ARTICLE_FETCHES:
            article_fetches += 1
            deal = build_deal(p)
        else:
            deal = build_deal((post_id, telegram_url, tg_text, None))

        if deal.score >= ALERT_THRESHOLD:
            msg = format_alert(deal)
            # Queue first. If sending fails, the alert survives into the next run.
            pending[str(post_id)] = {
                "message": msg,
                "score": deal.score,
                "created_at": int(time.time()),
            }
            print(f"NEUER KANDIDAT {post_id}: {deal.level} {deal.score}/100")

    # Send new alerts, strongest first, with a small per-run cap.
    ordered = sorted(
        pending.items(),
        key=lambda item: int(item[1].get("score", 0)),
        reverse=True,
    )
    for post_id, payload in ordered[:8]:
        if str(post_id) in sent_ids:
            pending.pop(str(post_id), None)
            continue
        if send_telegram(payload.get("message", "")):
            sent_ids.add(str(post_id))
            pending.pop(str(post_id), None)

    # Bound state size.
    state["sent_ids"] = list(sorted(sent_ids))[-500:]
    state["pending_alerts"] = dict(list(pending.items())[-20:])
    save_state(state)
    print(f"Gespeicherte Meldungen: {len(sent_ids)}")
    print(f"Ausstehende Meldungen: {len(pending)}")
    print("SCAN ABGESCHLOSSEN")
    return 0


if __name__ == "__main__":
    test = os.environ.get("TEST_LATEST", "0") == "1"
    raise SystemExit(main(test_latest=test))
