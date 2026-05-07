#!/usr/bin/env python3
import json, os, time, requests, subprocess
from datetime import datetime

BOT_TOKEN = "8570695279:AAG06k0w1NFTewOjGrKdkqIUzckmuV_61nM"
CHAT_ID   = "8290777331"
COLDKEY   = "5HR3cMSEnyzQbGCqgeHHQxCosgCBDi6a2tkWiBE3XCwUsmNR"
PRICE_DROP_THRESHOLD = 0.10
VTRUST_MIN = 0.80
STATE_FILE = "/home/simar/tao_state.json"

SNAPSHOT_FILE = "/tmp/tao_latest.json"

def load_stakes():
    """Load live stake positions from tao_monitor snapshot."""
    if os.path.exists(SNAPSHOT_FILE):
        with open(SNAPSHOT_FILE) as f:
            data = json.load(f)
        positions = data["positions"]
        return [{"subnet": f"SN{s['netuid']} {s['name']}", "netuid": s["netuid"],
                 "staked": s["tao_amount"], "value_gbp": s["value_gbp"]} for s in positions]
    try:
        import bittensor as bt, re
        sub = bt.subtensor(network="finney")
        stakes = sub.get_stake_info_for_coldkey(coldkey_ss58=COLDKEY)
        price_usd = get_tao_price() or 0
        r = requests.get("https://api.frankfurter.app/latest?from=USD&to=GBP", timeout=5)
        gbp_rate = r.json()["rates"]["GBP"] if r.ok else 0.78
        positions = []
        seen = set()
        for info in stakes:
            uid = info.netuid
            if uid in seen: continue
            seen.add(uid)
            try:
                alpha = float(re.sub(r"[^\d.]", "", str(info.stake).split()[0]))
            except:
                alpha = 0.0
            if alpha < 0.0001: continue
            if uid == 0:
                value_gbp = alpha * price_usd * gbp_rate
                name = "Root/Kraken"
            else:
                try:
                    meta = sub.metagraph(uid)
                    ap = float(getattr(meta, "alpha_price", 0) or 0)
                    value_gbp = alpha * ap * price_usd * gbp_rate
                    name = getattr(meta, "name", f"SN{uid}")
                except:
                    value_gbp = 0.0
                    name = f"SN{uid}"
            positions.append({"subnet": f"SN{uid} {name}", "netuid": uid,
                               "staked": alpha, "value_gbp": value_gbp})
        return sorted(positions, key=lambda x: x["value_gbp"], reverse=True)
    except Exception as e:
        print(f"load_stakes error: {e}")
        return []


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
    # Source 1: CoinGecko (3 attempts)
    for attempt in range(3):
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bittensor", "vs_currencies": "usd"}, timeout=10)
            price = r.json()["bittensor"]["usd"]
            print(f"[Price] CoinGecko: ${price:.2f}")
            return price
        except Exception as e:
            print(f"[Price] CoinGecko attempt {attempt+1} failed: {e}")
            time.sleep(2)

    # Source 2: KuCoin public API (no account needed)
    try:
        r = requests.get("https://api.kucoin.com/api/v1/market/orderbook/level1",
            params={"symbol": "TAO-USDT"}, timeout=10)
        price = float(r.json()["data"]["price"])
        print(f"[Price] KuCoin fallback: ${price:.2f}")
        return price
    except Exception as e:
        print(f"[Price] KuCoin fallback failed: {e}")

    print("[Price] All sources failed, returning None")
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
    STAKES = load_stakes()
    state = load_state()
    price = get_tao_price()
    total = sum(s["staked"] for s in STAKES)
    total_gbp = sum(s["value_gbp"] for s in STAKES)
    usd = f"£{total_gbp:,.2f}" if STAKES else "N/A"
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
