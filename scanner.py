#!/usr/bin/env python3
from __future__ import annotations

import json, os, re, time
from datetime import date, timedelta
from pathlib import Path

try:
    from swoop import deals, search
except Exception as exc:
    print(f"IMPORT_ERROR: {exc}")
    raise SystemExit(2)

STATE_FILE = Path(".errorfare_hunter_state.json")
ALERT_THRESHOLD = 65
URGENT_THRESHOLD = 85
ROUNDTRIP_MAX_PRICE = 1200
ROUNDTRIP_MIN_DISCOUNT = 35
ONE_WAY_JOBS_PER_RUN = 12
REQUEST_PAUSE = 0.25

ORIGINS = ["DUS","CGN","FRA","BER","HAM","MUC","AMS","EIN","BRU","LUX"]

# High-value destinations. Direct search is used instead of Explore because
# Explore is discovery-only and its result set is geographic-scope dependent.
TARGETS = [
    ("NRT", "Tokyo", "Asien"), ("HND", "Tokyo", "Asien"), ("KIX", "Osaka", "Asien"),
    ("ICN", "Seoul", "Asien"), ("BKK", "Bangkok", "Asien"), ("SIN", "Singapore", "Asien"),
    ("HKG", "Hong Kong", "Asien"), ("TPE", "Taipei", "Asien"), ("KUL", "Kuala Lumpur", "Asien"),
    ("DPS", "Bali", "Asien"), ("SGN", "Ho Chi Minh City", "Asien"), ("HAN", "Hanoi", "Asien"),
    ("MNL", "Manila", "Asien"), ("BOM", "Mumbai", "Asien"), ("DEL", "Delhi", "Asien"),
    ("MBA", "Mombasa", "Afrika"), ("ZNZ", "Zanzibar", "Afrika"), ("NBO", "Nairobi", "Afrika"),
    ("MRU", "Mauritius", "Afrika"), ("CPT", "Cape Town", "Afrika"), ("JNB", "Johannesburg", "Afrika"),
    ("JFK", "New York", "Langstrecke"), ("YYZ", "Toronto", "Langstrecke"),
    ("YVR", "Vancouver", "Langstrecke"), ("SYD", "Sydney", "Langstrecke"),
]
CABINS = [("economy", "Economy"), ("premium-economy", "Premium Economy"), ("business", "Business"), ("first", "First")]
# Different dates are rotated so we cover near-, medium- and farther-out fares.
DATE_OFFSETS = [14, 35, 70]

ASIA = {"Asien"}
AFRICA = {"Afrika"}


def load_state():
    try: return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception: return {"sent_keys": [], "seen_fingerprints": [], "one_way_cursor": 0, "one_way_date_cursor": 0}

def save_state(s):
    s["sent_keys"] = list(dict.fromkeys(s.get("sent_keys", [])))[-3000:]
    s["seen_fingerprints"] = list(dict.fromkeys(s.get("seen_fingerprints", [])))[-8000:]
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

def env(n): return os.environ.get(n, "").strip()

def telegram(text):
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not token or not chat: print("Telegram secrets fehlen."); return False
    import urllib.request, urllib.parse
    data=urllib.parse.urlencode({"chat_id":chat,"text":text,"disable_web_page_preview":"true"}).encode()
    req=urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",data=data,headers={"User-Agent":"ErrorFareHunter/5.0"})
    try:
        with urllib.request.urlopen(req,timeout=20) as r: return 200 <= r.status < 300
    except Exception as e: print("Telegram-Fehler:",e); return False

def eur_price(price, currency):
    try: p=float(price)
    except Exception: return None
    c=str(currency or "EUR").upper()
    if c in ("EUR","€"): return p
    if c=="USD": return p*0.85
    if c=="GBP": return p*1.15
    return p

def get_currency(obj):
    return str(getattr(obj,"currency",None) or "EUR").upper()

def roundtrip_score(d):
    p=eur_price(getattr(d,"price",None),get_currency(d));
    if p is None: return 0,[]
    t=" ".join(str(getattr(d,a,"") or "") for a in ["destination","destination_city","origin","cabin","airlines"]).lower()
    region="Sonstige"
    for name,_,r in TARGETS:
        if name.lower() in t: region=r; break
    cabin=str(getattr(d,"cabin","") or "").lower()
    s=0; why=[]
    if region=="Asien":
        if "business" in cabin and p<=500: s+=70; why.append("Asien-Business ≤500€")
        elif "first" in cabin and p<=900: s+=80; why.append("Asien-First ≤900€")
        elif "premium" in cabin and p<=300: s+=65; why.append("Asien-Premium-Economy ≤300€")
        elif p<=250: s+=60; why.append("Asien-Economy ≤250€")
        elif p<=350: s+=45; why.append("Asien-Economy ≤350€")
    elif region=="Afrika":
        if "business" in cabin and p<=550: s+=70; why.append("Afrika-Business ≤550€")
        elif "premium" in cabin and p<=250: s+=65; why.append("Afrika-Premium-Economy ≤250€")
        elif p<=120: s+=60; why.append("Afrika-Economy ≤120€")
        elif p<=180: s+=45; why.append("Afrika-Economy ≤180€")
    elif region=="Langstrecke":
        if "business" in cabin and p<=500: s+=65; why.append("Langstrecken-Business ≤500€")
        elif p<=220: s+=55; why.append("Langstrecken-Economy ≤220€")
        elif p<=300: s+=40; why.append("Langstrecken-Economy ≤300€")
    try:
        disc=float(getattr(d,"discount_pct",0) or 0)
        if disc>=60: s+=15; why.append(f"{disc:.0f}% unter Google-Referenz")
        elif disc>=45: s+=10; why.append(f"{disc:.0f}% unter Google-Referenz")
    except Exception: pass
    return min(100,s),why

def result_price(option):
    return eur_price(getattr(option,"price",None),get_currency(option))

def one_way_score(price, region, cabin, origin):
    s=0; why=[]
    if region=="Asien":
        if cabin=="Business" and price<=550: s+=75; why.append("One-Way Asien-Business ≤550€")
        elif cabin=="First" and price<=950: s+=85; why.append("One-Way Asien-First ≤950€")
        elif cabin=="Premium Economy" and price<=300: s+=70; why.append("One-Way Asien-Premium-Economy ≤300€")
        elif cabin=="Economy" and price<=180: s+=75; why.append("One-Way Asien-Economy ≤180€")
        elif cabin=="Economy" and price<=230: s+=60; why.append("One-Way Asien-Economy ≤230€")
        elif cabin=="Economy" and price<=300: s+=45; why.append("One-Way Asien-Economy ≤300€")
    elif region=="Afrika":
        if cabin=="Business" and price<=600: s+=75; why.append("One-Way Afrika-Business ≤600€")
        elif cabin=="Premium Economy" and price<=280: s+=70; why.append("One-Way Afrika-Premium-Economy ≤280€")
        elif cabin=="Economy" and price<=90: s+=80; why.append("One-Way Afrika-Economy ≤90€")
        elif cabin=="Economy" and price<=140: s+=65; why.append("One-Way Afrika-Economy ≤140€")
        elif cabin=="Economy" and price<=190: s+=50; why.append("One-Way Afrika-Economy ≤190€")
    elif region=="Langstrecke":
        if cabin=="Business" and price<=550: s+=70; why.append("One-Way Langstrecken-Business ≤550€")
        elif cabin=="First" and price<=900: s+=75; why.append("One-Way Langstrecken-First ≤900€")
        elif cabin=="Economy" and price<=220: s+=65; why.append("One-Way Langstrecken-Economy ≤220€")
        elif cabin=="Economy" and price<=300: s+=45; why.append("One-Way Langstrecken-Economy ≤300€")
    if origin in ORIGINS: s+=10; why.append("bevorzugter Abflug")
    return min(100,s),why

def flight_url(origin,dest,dep,cabin):
    return "https://www.google.com/travel/flights?q=" + __import__('urllib.parse').parse.quote(f"Flights from {origin} to {dest} on {dep} {cabin}")

def alarm_oneway(x,score):
    urgent="🚨 SOFORT-ALARM" if score>=URGENT_THRESHOLD else "🔥 ERROR-FARE-KANDIDAT"
    why="\n".join("• "+w for w in x["why"])
    return (f"{urgent}\n\n💰 Preis: {x['price_raw']} {x['currency']}\n💺 Klasse: {x['cabin']}\n📊 Score: {score}/100\n"
            f"🌍 Region: {x['region']}\n✈️ Route: {x['origin']} → {x['destination']}\n📅 Datum: {x['date']}\n🔎 Warum:\n{why}\n\n"
            "⚠️ Unabhängiger direkter Google-Flights-Suchtreffer. Preis sofort prüfen.\n"
            f"🔗 Suche: {x['url']}")

def alarm_roundtrip(d,score,why):
    o=str(getattr(d,"origin","") or ""); dest=str(getattr(d,"destination_city","") or getattr(d,"destination","") or "")
    dep=str(getattr(d,"departure_date","") or ""); ret=str(getattr(d,"return_date","") or "")
    cabin=str(getattr(d,"cabin","") or "Economy")
    urgent="🚨 SOFORT-ALARM" if score>=URGENT_THRESHOLD else "🔥 ERROR-FARE-KANDIDAT"
    url=flight_url(o,dest,dep,cabin)
    return (f"{urgent}\n\n💰 Preis: {getattr(d,'price',0)} {get_currency(d)}\n💺 Klasse: {cabin}\n📊 Score: {score}/100\n"
            f"✈️ Route: {o} → {dest}\n📅 Datum: {dep}"+(f" → {ret}" if ret else "")+"\n🔎 Warum:\n"+
            "\n".join("• "+w for w in why)+f"\n\n⚠️ Preis sofort prüfen.\n🔗 Suche: {url}")

def main():
    state=load_state(); sent=set(state.get("sent_keys",[])); seen=set(state.get("seen_fingerprints",[]))
    candidates=[]; errors=0; rt_count=0; ow_count=0
    print("=== ERROR FARE HUNTER V5 DIRECT ===")
    print("Roundtrip: Google-Flights-Deals | One-Way: direkte Zielsuche mit swoop.search()")
    for origin in ORIGINS:
        try:
            r=deals(origin,max_price=ROUNDTRIP_MAX_PRICE,min_discount_pct=ROUNDTRIP_MIN_DISCOUNT)
            ds=list(getattr(r,"deals",[]) or []); rt_count+=len(ds); print(f"{origin}: {len(ds)} Roundtrip-Kandidaten")
            for d in ds:
                fp="RT|"+"|".join(str(getattr(d,a,"") or "") for a in ["origin","destination","departure_date","return_date","price","cabin"])
                if fp in seen: continue
                seen.add(fp); sc,why=roundtrip_score(d)
                if sc>=ALERT_THRESHOLD: candidates.append((sc,"roundtrip",d,why,fp))
        except Exception as e: errors+=1; print(f"{origin}: ROUNDTRIP FEHLER {type(e).__name__}: {e}")
        time.sleep(REQUEST_PAUSE)
    jobs=[]
    for oi,o in enumerate(ORIGINS):
        for ti,(dest,name,region) in enumerate(TARGETS):
            # Economy is checked most often; premium/business/first are rotated separately.
            jobs.append((o,dest,name,region,"economy","Economy"))
            if ti%2==0: jobs.append((o,dest,name,region,"premium-economy","Premium Economy"))
            if ti%2==0: jobs.append((o,dest,name,region,"business","Business"))
            if ti%7==0: jobs.append((o,dest,name,region,"first","First"))
    cur=int(state.get("one_way_cursor",0)); dcur=int(state.get("one_way_date_cursor",0))
    selected=[jobs[(cur+i)%len(jobs)] for i in range(ONE_WAY_JOBS_PER_RUN)]
    state["one_way_cursor"]=(cur+ONE_WAY_JOBS_PER_RUN)%len(jobs); state["one_way_date_cursor"]=(dcur+1)%len(DATE_OFFSETS)
    for j,(o,dest,name,region,cabin,label) in enumerate(selected):
        dep=(date.today()+timedelta(days=DATE_OFFSETS[(dcur+j)%len(DATE_OFFSETS)])).isoformat()
        try:
            r=search(o,dest,dep,cabin=cabin,sort="cheapest",retries=2)
            opts=list(getattr(r,"results",[]) or [])
            if not opts: print(f"{o} → {name} {label}: keine Ergebnisse"); continue
            best=min(opts,key=lambda z: (result_price(z) if result_price(z) is not None else 10**9))
            p=result_price(best)
            if p is None: continue
            sc,why=one_way_score(p,region,label,o)
            ow_count+=1
            print(f"{o} → {name} {label} {dep}: {getattr(best,'price',p)} {get_currency(best)} | Score {sc}")
            if sc>=ALERT_THRESHOLD:
                fp=f"OW|{o}|{dest}|{dep}|{label}|{round(p)}"
                if fp not in seen:
                    seen.add(fp)
                    candidates.append((sc,"one-way",{"origin":o,"destination":name,"price_raw":getattr(best,'price',p),"currency":get_currency(best),"cabin":label,"region":region,"date":dep,"why":why,"url":flight_url(o,dest,dep,label)},why,fp))
        except Exception as e: errors+=1; print(f"{o} → {name} {label}: ONE-WAY FEHLER {type(e).__name__}: {e}")
        time.sleep(REQUEST_PAUSE)
    candidates.sort(key=lambda z:z[0],reverse=True)
    sent_now=0
    for sc,kind,obj,why,fp in candidates[:3]:
        if fp in sent: continue
        text=alarm_oneway(obj,sc) if kind=="one-way" else alarm_roundtrip(obj,sc,why)
        if telegram(text): sent.add(fp); sent_now+=1
    state["sent_keys"]=list(sent)[-3000:]; state["seen_fingerprints"]=list(seen)[-8000:]; save_state(state)
    print(f"Roundtrip-Preis-Kandidaten: {rt_count}")
    print(f"One-Way-Suchen geprüft: {ow_count}")
    print(f"Neue starke Kandidaten: {len(candidates)}")
    print(f"Neue Telegram-Alarme: {sent_now}")
    print(f"API/Quelle-Fehler: {errors}")
    print("SCAN ABGESCHLOSSEN")

if __name__=="__main__": main()
