import json, os, re, sys, time
from pathlib import Path
from datetime import date, timedelta

try:
    from swoop import deals, explore, price_explore_all, Region
except Exception as exc:
    print(f"IMPORT_ERROR: {exc}")
    sys.exit(2)

STATE_FILE = Path(".errorfare_hunter_state.json")
ALERT_THRESHOLD = 65
URGENT_THRESHOLD = 85
ONE_WAY_PER_ORIGIN = 8
ONE_WAY_JOB_BATCH = 4

ORIGINS = ["DUS","CGN","FRA","BER","HAM","MUC","AMS","EIN","BRU","LUX"]

ASIA = {
    "japan","tokyo","osaka","kyoto","seoul","south korea","china","beijing","shanghai",
    "hong kong","taiwan","taipei","thailand","bangkok","phuket","vietnam","hanoi",
    "ho chi minh","singapore","malaysia","kuala lumpur","indonesia","bali","jakarta",
    "philippines","manila","india","mumbai","delhi","asia"
}
AFRICA = {"zanzibar","mombasa","kenya","mauritius","seychelles","namibia","cape town",
          "south africa","tanzania","morocco","egypt","africa"}
LONG_HAUL = ASIA | AFRICA | {"usa","united states","canada","new york","los angeles",
                              "miami","san francisco","boston","toronto","vancouver",
                              "australia","sydney","melbourne","new zealand","brazil",
                              "argentina"}
SHORT_HAUL = {"mallorca","palma","london","paris","rome","madrid","barcelona","lisbon",
              "vienna","zurich","basel","prague","budapest"}

def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"sent_keys": [], "seen_fingerprints": []}

def save_state(state):
    state["sent_keys"] = state.get("sent_keys", [])[-2000:]
    state["seen_fingerprints"] = state.get("seen_fingerprints", [])[-5000:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

def env(name):
    return os.environ.get(name, "").strip()

def telegram(text):
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram secrets fehlen.")
        return False
    import urllib.request, urllib.parse
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"User-Agent":"ErrorFareHunter/4.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as exc:
        print("Telegram-Fehler:", exc)
        return False

def norm(x):
    return re.sub(r"\s+", " ", str(x or "")).strip()

def text_of_deal(d):
    vals = []
    for attr in ["origin","destination","destination_city","price","currency","discount_pct",
                 "departure_date","return_date","airlines","cabin","trip_length"]:
        try:
            vals.append(str(getattr(d, attr, "")))
        except Exception:
            pass
    return " ".join(vals).lower()

def destination_name(d):
    return norm(getattr(d, "destination_city", "") or getattr(d, "destination", ""))

def price_eur(d):
    try:
        p=float(getattr(d,"price"))
    except Exception:
        return None
    cur=str(getattr(d,"currency","EUR") or "EUR").upper()
    # Swoop normally returns the Google Flights currency for the search locale.
    # Treat non-EUR as approximate only for scoring; alert text preserves currency.
    if cur in ("EUR","€"): return p
    if cur == "USD": return p * 0.86
    if cur == "GBP": return p * 1.15
    return p

def region_for(d):
    t=text_of_deal(d)
    if any(k in t for k in ASIA): return "Asien"
    if any(k in t for k in AFRICA): return "Afrika"
    if any(k in t for k in {"usa","canada","new york","los angeles","miami","toronto","vancouver","australia","sydney","brazil","argentina"}): return "Langstrecke"
    if any(k in t for k in SHORT_HAUL): return "Europa"
    return "Sonstige"

def cabin_guess(d):
    t=text_of_deal(d)
    if "first" in t: return "First"
    if "business" in t: return "Business"
    if "premium economy" in t or "premium-economy" in t: return "Premium Economy"
    return "Economy"

def score(d):
    p=price_eur(d)
    if p is None: return 0, []
    t=text_of_deal(d)
    region=region_for(d)
    cabin=cabin_guess(d)
    reasons=[]
    s=0

    # Strong price bands for the user's target.
    if region=="Asien":
        if cabin=="Business" and p <= 500: s+=70; reasons.append("Asien-Business ≤500€")
        elif cabin=="First" and p <= 900: s+=80; reasons.append("Asien-First ≤900€")
        elif cabin=="Premium Economy" and p <= 300: s+=65; reasons.append("Asien-Premium-Economy ≤300€")
        elif p <= 250: s+=60; reasons.append("Asien-Economy ≤250€")
        elif p <= 350: s+=45; reasons.append("Asien-Economy ≤350€")
        elif p <= 450: s+=30; reasons.append("Asien-Economy ≤450€")
    elif region=="Afrika":
        if cabin=="Business" and p <= 550: s+=70; reasons.append("Afrika-Business ≤550€")
        elif cabin=="Premium Economy" and p <= 250: s+=65; reasons.append("Afrika-Premium-Economy ≤250€")
        elif p <= 120: s+=60; reasons.append("Afrika-Economy ≤120€")
        elif p <= 180: s+=45; reasons.append("Afrika-Economy ≤180€")
    elif region=="Langstrecke":
        if cabin=="Business" and p <= 500: s+=65; reasons.append("Langstrecken-Business ≤500€")
        elif p <= 220: s+=55; reasons.append("Langstrecken-Economy ≤220€")
        elif p <= 300: s+=40; reasons.append("Langstrecken-Economy ≤300€")
    else:
        if p <= 70: s+=15; reasons.append("Europa ≤70€")
        elif p <= 100: s+=8; reasons.append("Europa ≤100€")

    origin=str(getattr(d,"origin","") or "").upper()
    if origin in ORIGINS:
        s+=10; reasons.append("bevorzugter Abflug")

    try:
        disc=float(getattr(d,"discount_pct"))
        if disc >= 60: s+=15; reasons.append(f"{disc:.0f}% unter Google-Referenz")
        elif disc >= 45: s+=10; reasons.append(f"{disc:.0f}% unter Google-Referenz")
    except Exception:
        pass

    # Prevent ordinary European bargains from becoming alerts.
    if region=="Europa" and s < 70:
        s=0
        reasons=[]

    return min(100,s), reasons

def fingerprint(d):
    try:
        return str(getattr(d,"fingerprint"))
    except Exception:
        return "|".join([
            str(getattr(d,"origin","")), str(getattr(d,"destination","")),
            str(getattr(d,"departure_date","")), str(getattr(d,"return_date","")),
            str(getattr(d,"price",""))
        ])

def deal_link(d):
    # Google Flights search is reconstructed from route/date where possible.
    origin=str(getattr(d,"origin","") or "")
    dest=str(getattr(d,"destination","") or "")
    dep=str(getattr(d,"departure_date","") or "")
    ret=str(getattr(d,"return_date","") or "")
    if origin and dest and dep:
        url=f"https://www.google.com/travel/flights?q=Flights%20from%20{origin}%20to%20{dest}%20on%20{dep}"
        if ret: url += f"%20returning%20{ret}"
        return url
    return "https://www.google.com/travel/flights"

def format_alarm(d, sc, reasons):
    p=getattr(d,"price",None)
    cur=str(getattr(d,"currency","EUR") or "EUR")
    origin=norm(getattr(d,"origin",""))
    dest=norm(getattr(d,"destination_city","") or getattr(d,"destination",""))
    cabin=cabin_guess(d)
    region=region_for(d)
    disc=getattr(d,"discount_pct","")
    dep=getattr(d,"departure_date","")
    ret=getattr(d,"return_date","")
    dates=f"{dep}" + (f" → {ret}" if ret else "")
    urgent="🚨 SOFORT-ALARM" if sc>=URGENT_THRESHOLD else "🔥 ERROR-FARE-KANDIDAT"
    why="\n".join(f"• {x}" for x in reasons) or "• ungewöhnlich günstiger Google-Flights-Treffer"
    return (
        f"{urgent}\n\n"
        f"💰 Preis: {p} {cur}\n"
        f"💺 Klasse: {cabin}\n"
        f"📊 Score: {sc}/100\n"
        f"🌍 Region: {region}\n"
        f"✈️ Route: {origin} → {dest}\n"
        f"📅 Datum: {dates}\n"
        + (f"📉 Google-Referenzrabatt: {float(disc):.0f}%\n" if disc != "" else "")
        + "\n🔎 Warum:\n" + why +
        "\n\n⚠️ Preis direkt bei der Buchung prüfen – Google-Flights-Preise können sich sehr schnell ändern.\n"
        f"🔗 Suche: {deal_link(d)}"
    )


def one_way_score(price: float | None, region: str, cabin: str, origin: str, destination: str):
    if price is None:
        return 0, []
    reasons = []
    s = 0
    if region == "Asien":
        if cabin == "Business" and price <= 550: s += 75; reasons.append("One-Way Asien-Business ≤550€")
        elif cabin == "First" and price <= 950: s += 85; reasons.append("One-Way Asien-First ≤950€")
        elif cabin == "Premium Economy" and price <= 300: s += 70; reasons.append("One-Way Asien-Premium-Economy ≤300€")
        elif price <= 180: s += 75; reasons.append("One-Way Asien-Economy ≤180€")
        elif price <= 230: s += 60; reasons.append("One-Way Asien-Economy ≤230€")
        elif price <= 300: s += 45; reasons.append("One-Way Asien-Economy ≤300€")
    elif region == "Afrika":
        if cabin == "Business" and price <= 600: s += 75; reasons.append("One-Way Afrika-Business ≤600€")
        elif cabin == "Premium Economy" and price <= 280: s += 70; reasons.append("One-Way Afrika-Premium-Economy ≤280€")
        elif price <= 90: s += 80; reasons.append("One-Way Afrika-Economy ≤90€")
        elif price <= 140: s += 65; reasons.append("One-Way Afrika-Economy ≤140€")
        elif price <= 190: s += 50; reasons.append("One-Way Afrika-Economy ≤190€")
    else:
        if cabin == "Business" and price <= 550: s += 70; reasons.append("One-Way Langstrecken-Business ≤550€")
        elif price <= 220: s += 65; reasons.append("One-Way Langstrecken-Economy ≤220€")
        elif price <= 300: s += 45; reasons.append("One-Way Langstrecken-Economy ≤300€")
    if origin in ORIGINS:
        s += 10; reasons.append("bevorzugter Abflug")
    return min(100, s), reasons


def one_way_region_from_text(destination: str, country: str = ""):
    t = f"{destination} {country}".lower()
    if any(k in t for k in ASIA):
        return "Asien"
    if any(k in t for k in AFRICA):
        return "Afrika"
    if any(k in t for k in {"usa","united states","canada","new york","los angeles","miami","toronto","vancouver","australia","sydney","melbourne","new zealand","brazil","argentina","south africa"}):
        return "Langstrecke"
    return "Sonstige"


ONE_WAY_CABINS = ["economy", "premium-economy", "business", "first"]

def one_way_cabin_label(cabin: str):
    return {
        "economy": "Economy",
        "premium-economy": "Premium Economy",
        "business": "Business",
        "first": "First",
    }.get(cabin, "Economy")


def add_one_way_deals(origin: str, cabin: str, out: list):
    label = one_way_cabin_label(cabin)
    try:
        # Do NOT use swoop's region filter here. Region classification depends on
        # airportsdata; we classify the returned destinations ourselves so a missing
        # optional package can never silently turn the whole scan into zero results.
        exp = explore(origin, cabin=cabin, one_way=True)
        raw = list(getattr(exp, "destinations", []) or [])
        ranked = []
        for d in raw:
            destination = norm(getattr(d, "destination_name", "") or getattr(d, "destination", ""))
            country = norm(getattr(d, "destination_country", ""))
            region = one_way_region_from_text(destination, country)
            if region in {"Asien", "Afrika", "Langstrecke"}:
                ranked.append((0 if region in {"Asien", "Afrika"} else 1, d, region))
        ranked.sort(key=lambda x: x[0])
        chosen = [x[1:] for x in ranked[:ONE_WAY_PER_ORIGIN]]
        if not chosen:
            print(f"{origin} {label}: keine relevanten One-Way-Ziele aus Explore")
            return

        dests = [x[0] for x in chosen]
        regions = [x[1] for x in chosen]
        prices = price_explore_all(dests)
        for d, region, pr in zip(dests, regions, prices):
            if pr is None:
                continue
            price = float(getattr(pr, "price", 0) or 0)
            currency = str(getattr(pr, "currency", "EUR") or "EUR").upper()
            if currency == "USD": price_eur_value = price * 0.86
            elif currency == "GBP": price_eur_value = price * 1.15
            else: price_eur_value = price
            destination = norm(getattr(d, "destination_name", "") or getattr(d, "destination", ""))
            sc, reasons = one_way_score(price_eur_value, region, label, origin, destination)
            if sc >= ALERT_THRESHOLD:
                fp = f"OW|{origin}|{getattr(d,'destination','')}|{getattr(d,'departure_date','')}|{label}|{round(price,0)}"
                out.append((sc, {
                    "kind":"one-way", "fingerprint":fp, "origin":origin,
                    "destination":destination, "price":price, "currency":currency,
                    "region":region, "cabin":label, "departure_date":getattr(d,'departure_date',''),
                    "return_date":"", "reasons":reasons,
                }))
        print(f"{origin} {label}: {len(dests)} One-Way-Ziele geprüft")
    except Exception as exc:
        print(f"{origin} {label}: One-Way FEHLER {type(exc).__name__}: {exc}")


def format_one_way_alarm(d: dict, sc: int):
    urgent = "🚨 SOFORT-ALARM" if sc >= URGENT_THRESHOLD else "🔥 ERROR-FARE-KANDIDAT"
    why = "\n".join(f"• {x}" for x in d["reasons"])
    date_text = d["departure_date"] or "Google-Vorschlag"
    url = f"https://www.google.com/travel/flights?q=Flights%20from%20{d['origin']}%20to%20{d['destination']}%20on%20{date_text}"
    return (f"{urgent}\n\n💰 Preis: {d['price']} {d['currency']}\n💺 Klasse: {d['cabin']}\n"
            f"📊 Score: {sc}/100\n🌍 Region: {d['region']}\n✈️ Route: {d['origin']} → {d['destination']}\n"
            f"📅 Datum: {date_text}\n🔎 Warum:\n{why}\n\n"
            f"⚠️ One-Way-Fund aus unabhängiger Google-Flights-Suche. Preis sofort prüfen.\n🔗 Suche: {url}")

def main():
    state = load_state()
    sent = set(state.get("sent_keys", []))
    seen = set(state.get("seen_fingerprints", []))
    all_deals = []
    one_way = []
    errors = 0

    print("=== ERROR FARE HUNTER V4 INDEPENDENT+ ===")
    print("Roundtrip: direkte Google-Flights-Deals | One-Way: gestaffelte Explore-Suche")

    # Roundtrip: every run, all preferred origins. This is the fast broad scan.
    for origin in ORIGINS:
        try:
            result = deals(origin, max_price=1200, min_discount_pct=35)
            ds = list(getattr(result, "deals", []) or [])
            print(f"{origin}: {len(ds)} Roundtrip-Kandidaten")
            all_deals.extend(ds)
        except Exception as exc:
            errors += 1
            print(f"{origin}: ROUNDTRIP FEHLER {type(exc).__name__}: {exc}")
        time.sleep(0.35)

    candidates = []
    for d in all_deals:
        fp = fingerprint(d)
        if fp in seen:
            continue
        seen.add(fp)
        sc, reasons = score(d)
        if sc >= ALERT_THRESHOLD:
            candidates.append((sc, d, reasons, "roundtrip"))

    # One-way: rotate through origin + cabin combinations. We deliberately avoid
    # swoop's region filter (see add_one_way_deals) and classify destinations locally.
    jobs = [(o, c) for o in ORIGINS for c in ONE_WAY_CABINS]
    cursor = int(state.get("one_way_cursor", 0))
    selected = [jobs[(cursor + i) % len(jobs)] for i in range(ONE_WAY_JOB_BATCH)]
    state["one_way_cursor"] = (cursor + ONE_WAY_JOB_BATCH) % len(jobs)
    for origin, cabin in selected:
        add_one_way_deals(origin, cabin, one_way)
        time.sleep(0.35)

    for sc, d in one_way:
        if d["fingerprint"] not in seen:
            seen.add(d["fingerprint"])
            candidates.append((sc, d, d["reasons"], "one-way"))

    candidates.sort(key=lambda x: x[0], reverse=True)
    sent_now = 0
    for sc, d, reasons, kind in candidates[:3]:
        if kind == "one-way":
            key = d["fingerprint"]
            text = format_one_way_alarm(d, sc)
        else:
            key = f"{fingerprint(d)}|{round(float(getattr(d,'price',0) or 0),0)}"
            text = format_alarm(d, sc, reasons)
        if key in sent:
            continue
        if telegram(text):
            sent.add(key)
            sent_now += 1

    state["sent_keys"] = list(sent)[-3000:]
    state["seen_fingerprints"] = list(seen)[-8000:]
    save_state(state)

    print(f"Roundtrip-Preis-Kandidaten: {len(all_deals)}")
    print(f"One-Way-Kandidaten geprüft: {len(one_way)}")
    print(f"Neue starke Kandidaten: {len(candidates)}")
    print(f"Neue Telegram-Alarme: {sent_now}")
    print(f"API/Quelle-Fehler: {errors}")
    print("SCAN ABGESCHLOSSEN")


if __name__ == "__main__":
    main()
