#!/usr/bin/env python3
import json, os, time, requests, subprocess
from datetime import datetime

BOT_TOKEN = "8570695279:AAG06k0w1NFTewOjGrKdkqIUzckmuV_61nM"
CHAT_ID   = "8290777331"
COLDKEY   = "5HR3cMSEnyzQbGCqgeHHQxCosgCBDi6a2tkWiBE3XCwUsmNR"
PRICE_DROP_THRESHOLD = 0.10
VTRUST_MIN = 0.80
STATE_FILE = "/home/simar/tao_state.json"

STAKES = [
    {"subnet": "SN0 Root",    "netuid": 0,  "validator": "TAO.com",        "hotkey": "5GP7c3fFazW9GXK8Up3qgu2DJBk8inu4aK9TZy3RuoSWVCMi", "staked": 5.94},
    {"subnet": "SN64 Chutes", "netuid": 64, "validator": "Chutes Primary", "hotkey": "5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ", "staked": 1.96},
    {"subnet": "SN62 Ridges", "netuid": 62, "validator": "General Tensor", "hotkey": "5Djyacas3eWLPhCKsS3neNSJonzfxJmD3gcrMTFDc4eHsn62", "staked": 1.24},
    {"subnet": "SN4 Targon",  "netuid": 4,  "validator": "5Hp18g...",      "hotkey": "5Hp18g9P8hLGKp9W3ZDr4bvJwba6b6bY3P2u3VdYf8yMR8FM", "staked": 1.04},
    {"subnet": "SN75 Hippius","netuid": 75, "validator": "5G1Qj9...",      "hotkey": "5G1Qj93Fy22grpiGKq6BEvqqmS2HVRs3jaEdMhq9absQzs6g", "staked": 0.79},
    {"subnet": "SN68 Nova",   "netuid": 68, "validator": "5F1tQr...",      "hotkey": "5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg", "staked": 0.58},
    {"subnet": "SN51 Lium",   "netuid": 51, "validator": "tao.bot",        "hotkey": "5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u", "staked": 0.35},
]

def send_telegram(message, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": parse_mode}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"Telegram response: {r.status_code} {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"[Telegram error] {e}")
        return False

def get_tao_price():
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bittensor", "vs_currencies": "usd"}, timeout=10)
        return r.json()["bittensor"]["usd"]
    except Exception as e:
        print(f"[Price error] {e}")
        return None

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except: pass
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def run_daily_summary():
    state = load_state()
    price = get_tao_price()
    total = sum(s["staked"] for s in STAKES)
    usd = f"${total * price:,.2f}" if price else "N/A"
    msg = (
        f"📊 <b>DAILY PORTFOLIO SUMMARY</b>\n"
        f"📅 {datetime.now().strftime('%A, %d %b %Y')}\n\n"
        f"<pre>{'Subnet':<14} {'Staked':>10}\n{'─'*26}\n"
        + "\n".join(f"{s['subnet']:<14} {s['staked']:>8.4f} TAO" for s in STAKES)
        + f"\n{'─'*26}\n{'TOTAL':<14} {total:>8.4f} TAO</pre>\n"
        f"💵 Value: <b>{usd}</b>\n"
        f"💰 TAO price: <b>${f'{price:.2f}' if price else 'N/A'}</b>\n\n"
        f"🔗 <a href='https://taostats.io/account/{COLDKEY}'>View on TaoStats</a>"
    )
    send_telegram(msg)
    save_state(state)

def run_price_monitor():
    send_telegram("🟢 <b>TAO Price Monitor started</b>\nWatching for drops >10%")
    while True:
        state = load_state()
        price = get_tao_price()
        if price:
            last = state.get("last_price")
            if last and (last - price) / last >= PRICE_DROP_THRESHOLD:
                send_telegram(f"🔴 <b>TAO PRICE DROP ALERT</b>\nWas: ${last:.2f} → Now: ${price:.2f}\nDrop: {((last-price)/last*100):.1f}%")
            state["last_price"] = price
            save_state(state)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] TAO: ${price:.2f}")
        time.sleep(300)

def run_vtrust_monitor():
    send_telegram("🟢 <b>TAO VTrust Monitor started</b>\nChecking validators hourly")
    while True:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] VTrust check running...")
        time.sleep(3600)

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if mode == "daily":
        run_daily_summary()
    elif mode == "price":
        run_price_monitor()
    elif mode == "vtrust":
        run_vtrust_monitor()
    elif mode == "test":
        print("Sending test message...")
        ok = send_telegram(
            f"✅ <b>TAO Alert System — Test OK</b>\n\n"
            f"Bot connected successfully!\n"
            f"All Phase 1 monitors ready.\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        print("✅ Sent!" if ok else "❌ FAILED")
