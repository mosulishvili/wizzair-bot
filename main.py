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
"""

import json
import os
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
    "update_offset": 0
}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return dict(default)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def tg_api(method, params=None):
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN is not set - add it as a repo secret.")
        return None
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"[telegram] request failed: {e}")
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
# WizzAir price lookup (unofficial endpoint - may change without notice)
# ---------------------------------------------------------------------------

WIZZ_TIMETABLE_URL = "https://be.wizzair.com/27.5.0/Api/search/timetable"


def fetch_cheapest_fares(origin, destination, date_from, date_to):
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
        "infantCount": 0
    }
    req = urllib.request.Request(
        WIZZ_TIMETABLE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[wizzair] fetch failed for {origin}->{destination}: {e}")
        return []

    fares = []
    for flight in data.get("outboundFlights", []):
        price = flight.get("price", {}).get("amount")
        dep = flight.get("departureDate")
        if price is not None and dep:
            fares.append((dep, float(price)))
    return fares


def check_prices(cfg, state):
    today = date.today()
    horizon = today + timedelta(days=30 * cfg.get("months_ahead", 6))
    threshold = cfg.get("threshold_eur", 50)
    alerted = state.setdefault("alerted", {})

    for origin, destination in cfg.get("routes", []):
        fares = fetch_cheapest_fares(origin, destination, today, horizon)
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
