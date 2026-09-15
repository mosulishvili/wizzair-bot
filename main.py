#!/usr/bin/env python3
"""
WizzAir Alert Bot - GitHub Actions edition
-------------------------------------------
Runs ONCE per invocation (the workflow calls it on a schedule, e.g. every
30 minutes). Each run:
  1. Polls Telegram for new commands (/start, /setprice, /price) and
     updates config.json.
  2. Checks KUT<->BCN fares and sends an alert if a fare is below the
     threshold and hasn't already been alerted.
  3. Saves config.json / state.json - the workflow commits these back
     to the repo so state survives between runs.

The bot token is read from the TELEGRAM_BOT_TOKEN environment variable
(set as a GitHub Actions secret) - never hardcode it in this file.

WizzAir has no official public API and changes the version number in
their internal endpoint (be.wizzair.com/<version>/Api/...) periodically.
This script auto-discovers the current version from wizzair.com when
the cached one stops working, and caches the working version in
config.json so most runs don't need to re-discover it.
"""

import json
import os
import re
import time
import urllib.request
import urllib.error
from datetime import date, timedelta

CONFIG_FILE = "config.json"
STATE_FILE = "state.json"

DEFAULT_CONFIG = {
    "chat_id": None,
    "threshold_eur": 50,
    "routes": [["KUT", "BCN"], ["BCN", "KUT"]],
    "months_ahead": 6,
    "update_offset": 0,
    "api_version": "27.5.0"
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return dict(default)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def tg_api(method, params=None, retries=3, backoff=5):
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set - add it as a repo secret.")
        return None
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"[telegram] request failed (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    return None


def tg_send(cfg, text):
    if not cfg.get("chat_id"):
        print("[telegram] no chat_id yet - send /start to the bot first")
        return
    tg_api("sendMessage", {"chat_id": cfg["chat_id"], "text": text})


def handle_updates(cfg):
    result = tg_api("getUpdates", {"offset": cfg.get("update_offset", 0), "timeout": 0})
    if not result or not result.get("ok"):
        return cfg

    for upd in result.get("result", []):
        cfg["update_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = msg.get("chat", {}).get("id")
        if not text or not chat_id:
            continue

        if text.startswith("/start"):
            cfg["chat_id"] = chat_id
            tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"დარეგისტრირდი! ალერტები მოვა, როცა ფასი {cfg['threshold_eur']}€-ზე დაბალი იქნება.\n"
                        f"ფასის შესაცვლელად: /setprice 45"
            })

        elif text.startswith("/setprice"):
            parts = text.split()
            if len(parts) >= 2 and parts[1].replace(".", "", 1).isdigit():
                cfg["threshold_eur"] = float(parts[1])
                cfg["chat_id"] = chat_id
                tg_api("sendMessage", {
                    "chat_id": chat_id,
                    "text": f"✅ ახალი ზღვარი: {cfg['threshold_eur']}€"
                })
            else:
                tg_api("sendMessage", {"chat_id": chat_id, "text": "გამოიყენე: /setprice 45"})

        elif text.startswith("/price"):
            tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": f"მიმდინარე ზღვარი: {cfg['threshold_eur']}€"
            })

        elif text.startswith("/status"):
            tg_api("sendMessage", {
                "chat_id": chat_id,
                "text": "ბოტი აქტიურია (GitHub Actions) და ამოწმებს ფასებს რეგულარულად."
            })

    return cfg


# ---------------------------------------------------------------------------
# WizzAir price lookup (unofficial endpoint - version changes over time)
# ---------------------------------------------------------------------------

VERSION_RE = re.compile(r"be\.wizzair\.com/(\d+\.\d+\.\d+)/Api", re.IGNORECASE)


def _http_get_text(url, retries=2, backoff=5):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
            print(f"[wizzair] GET failed (attempt {attempt}/{retries}) for {url}: {e}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise last_error


def discover_api_version():
    """Try to find the current be.wizzair.com/<version>/Api prefix by
    scanning wizzair.com's timetable page and its linked JS bundles."""
    try:
        html = _http_get_text("https://wizzair.com/en-gb/flights/timetable")
    except Exception as e:
        print(f"[wizzair] could not load timetable page: {e}")
        return None

    m = VERSION_RE.search(html)
    if m:
        return m.group(1)

    script_srcs = re.findall(r'<script[^>]+src="([^"]+\.js)"', html)
    for src in script_srcs[:15]:
        url = src if src.startswith("http") else "https://wizzair.com" + src
        try:
            js = _http_get_text(url)
        except Exception:
            continue
        m2 = VERSION_RE.search(js)
        if m2:
            return m2.group(1)

    return None


def fetch_cheapest_fares(origin, destination, date_from, date_to, version, retries=2, backoff=5):
    payload = {
        "flightList": [{
            "departureStation": origin,
            "arrivalStation": destination,
            "from": date_from.isoformat(),
            "to": date_to.isoformat()
        }],
        "priceType": "regular",
        "adultCount": 1,
        "childCount": 0,
        "infantCount": 0,
        "isFlightChange": False,
        "isSeniorOrStudent": False,
        "dayInterval": 1
    }
    url = f"https://be.wizzair.com/{version}/Api/search/timetable"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": UA,
            "Accept": "application/json"
        },
    )

    last_error = None
    data = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = e
            print(f"[wizzair] fare request failed (attempt {attempt}/{retries}) for "
                  f"{origin}->{destination}: {e}")
            if attempt < retries:
                time.sleep(backoff * attempt)
    if data is None:
        raise last_error

    fares = []
    for flight in data.get("outboundFlights", []):
        price = flight.get("price", {}).get("amount")
        dep = flight.get("departureDate")
        if price is not None and dep:
            fares.append((dep, float(price)))
    return fares


def fetch_with_version_fallback(cfg, origin, destination, date_from, date_to):
    version = cfg.get("api_version", DEFAULT_CONFIG["api_version"])
    try:
        return fetch_cheapest_fares(origin, destination, date_from, date_to, version)
    except Exception as e:
        print(f"[wizzair] fetch failed for {origin}->{destination} with version {version}: {e}")

    print("[wizzair] trying to auto-discover current API version...")
    new_version = discover_api_version()
    if not new_version or new_version == version:
        print("[wizzair] version discovery failed or unchanged - giving up for this run.")
        return []

    print(f"[wizzair] discovered version {new_version}, retrying...")
    cfg["api_version"] = new_version
    try:
        return fetch_cheapest_fares(origin, destination, date_from, date_to, new_version)
    except Exception as e:
        print(f"[wizzair] retry failed for {origin}->{destination} with version {new_version}: {e}")
        return []


def check_prices(cfg, state):
    today = date.today()
    horizon = today + timedelta(days=30 * cfg.get("months_ahead", 6))
    threshold = cfg.get("threshold_eur", 50)
    alerted = state.setdefault("alerted", {})

    for origin, destination in cfg.get("routes", []):
        fares = fetch_with_version_fallback(cfg, origin, destination, today, horizon)
        for dep_date, price in fares:
            key = f"{origin}-{destination}-{dep_date}"
            if price <= threshold and alerted.get(key) != price:
                tg_send(
                    cfg,
                    f"✈️ {origin} → {destination}\n📅 {dep_date}\n💶 {price}€ (ზღვარი: {threshold}€)"
                )
                alerted[key] = price
            elif price > threshold and key in alerted:
                del alerted[key]


def main():
    cfg = load_json(CONFIG_FILE, DEFAULT_CONFIG)
    state = load_json(STATE_FILE, {"alerted": {}})

    cfg = handle_updates(cfg)
    check_prices(cfg, state)

    save_json(CONFIG_FILE, cfg)
    save_json(STATE_FILE, state)
    print("Run complete.")


if __name__ == "__main__":
    main()
