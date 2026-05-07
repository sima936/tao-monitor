#!/usr/bin/env python3
import os, json, time, requests, logging
from pathlib import Path

VTRUST_THRESHOLD = 0.80
ALERT_COOLDOWN = 86400
RANK_CHANGE_ALERT = 10
EMISSION_DROP_PCT = 15
NEW_SUBNET_VTRUST = 0.80
API_KEY = os.environ.get("TAOSTATS_API_KEY", "")
BOT = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
BASE_URL = "https://api.taostats.io/api"
LOG_DIR = Path(os.path.expanduser("~/tao_logs"))
LOG_FILE = LOG_DIR / "enhanced_monitor.log"
STATE_FILE = LOG_DIR / "enhanced_state.json"

STAKES = [
    {"subnet":0,"name":"SN0 Root","hotkey":"5HK5tp6t2S59DywmHRWPBVJeJ86T61KjurYqeooqj8sREpeN","staked":5.94},
    {"subnet":64,"name":"SN64 Chutes","hotkey":"5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ","staked":1.96},
    {"subnet":62,"name":"SN62 Ridges","hotkey":"5DjyacaS3eWLPhCKsS3neNSJonzfxJmD3gcrMTFDc4eHsn62","staked":1.24},
    {"subnet":4,"name":"SN4 Targon","hotkey":"5Hp18g9P8hLGKp9W3ZDr4bvJwba6b6bY3P2u3VdYf8yMR8FM","staked":1.04},
    {"subnet":75,"name":"SN75 Hippius","hotkey":"5G1Qj93Fy22grpiGKq6BEvqqmS2HVRs3jaEdMhq9absQzs6g","staked":0.79},
    {"subnet":68,"name":"SN68 Nova","hotkey":"5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg","staked":0.58},
    {"subnet":51,"name":"SN51 Lium.io","hotkey":"5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u","staked":0.35},
]
STAKED_SUBNETS = {s["subnet"] for s in STAKES}
logging.basicConfig(filename=LOG_FILE, level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)
def safe_float(v):
    try: return float(v) if v else None
    except: return None
def safe_int(v):
    try: return int(v) if v else None
    except: return None
def hdrs(): return {"Authorization": API_KEY}
def send(msg):
    if not BOT or not CHAT: return
    try: requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage", json={"chat_id":CHAT,"text":msg,"parse_mode":"Markdown"}, timeout=10)
    except Exception as e: log.error(f"Telegram error: {e}")
def load_state():
    try: return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    except: return {}
def save_state(s): STATE_FILE.write_text(json.dumps(s, indent=2))

def get_metagraph(subnet, hotkey):
    try:
        r = requests.get(f"{BASE_URL}/metagraph/latest/v1?netuid={subnet}&hotkey={hotkey}", headers=hdrs(), timeout=15)
        if r.status_code == 200:
            d = r.json().get("data", [])
            if d: return d[0]
    except Exception as e: log.warning(f"Metagraph error {subnet}: {e}")
    return None
def get_subnet_list():
    try:
        r = requests.get(f"{BASE_URL}/subnet/latest/v1", headers=hdrs(), timeout=15)
        if r.status_code == 200: return r.json().get("data", [])
    except Exception as e: log.warning(f"Subnet list error: {e}")
    return []
def check_stakes(state, now):
    alerts = []
    for s in STAKES:
        data = get_metagraph(s["subnet"], s["hotkey"])
        if not data:
            log.warning(f"{s["name"]} unavailable - skipping")
            continue
        vtrust = safe_float(data.get("validator_trust"))
        rank = safe_int(data.get("rank"))
        emission = safe_float(data.get("emission"))
        key = s["hotkey"]
        if vtrust is not None and vtrust < VTRUST_THRESHOLD:
            last = state.get("last_alerted", {}).get(key, 0)
            if now - last > ALERT_COOLDOWN:
                alerts.append(f"VTrust ALERT: {s["name"]} score={round(vtrust,4)} below {VTRUST_THRESHOLD}")
                state.setdefault("last_alerted", {})[key] = now
        if rank is not None:
            prev = state.get("ranks", {}).get(key)
            if prev is not None and abs(rank - prev) >= RANK_CHANGE_ALERT:
                direction = "dropped" if rank > prev else "improved"
                alerts.append(f"Rank Change: {s["name"]} {direction} {prev} -> {rank}")
            state.setdefault("ranks", {})[key] = rank
        if emission is not None:
            prev_e = safe_float(state.get("emissions", {}).get(key))
            if prev_e and prev_e > 0:
                drop = ((prev_e - emission) / prev_e) * 100
                if drop >= EMISSION_DROP_PCT:
                    alerts.append(f"Emission Drop: {s["name"]} fell {round(drop,1)}%")
            state.setdefault("emissions", {})[key] = emission
        log.info(f"{s["name"]} vtrust={vtrust} rank={rank} emission={emission}")
    return alerts
def check_new_subnets(state):
    alerts = []
    seen = set(state.get("seen_subnets", []))
    for subnet in get_subnet_list():
        netuid = subnet.get("netuid")
        if netuid is None or netuid in STAKED_SUBNETS or netuid in seen: continue
        seen.add(netuid)
        vtrust = safe_float(subnet.get("validator_trust") or subnet.get("mean_validator_trust"))
        if vtrust and vtrust >= NEW_SUBNET_VTRUST:
            alerts.append(f"New Subnet: SN{netuid} vtrust={round(vtrust,4)}")
    state["seen_subnets"] = list(seen)
    return alerts
def run():
    log.info("Enhanced monitor started.")
    state = load_state()
    now = time.time()
    alerts = check_stakes(state, now) + check_new_subnets(state)
    save_state(state)
    if alerts:
        for a in alerts: send(a)
    else:
        send("TAO Monitor: All positions healthy.")
    log.info(f"Done. {len(alerts)} alerts.")
if __name__ == "__main__":
    run()
