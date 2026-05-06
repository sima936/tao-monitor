import os, json, time, requests, logging
from datetime import datetime
from pathlib import Path

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

LOG_DIR    = Path(os.path.expanduser("~/tao_logs"))
STATE_FILE = LOG_DIR / "vtrust_state.json"
LOG_FILE   = LOG_DIR / "vtrust_monitor.log"

VTRUST_THRESHOLD = 0.80
ALERT_COOLDOWN   = 86400  # 24 hours

# All hotkeys synced with tao_monitor.py
# SN0 Root uses taostats API (metagraph) — hotkey is Kraken
STAKES = [
    {"subnet": 0,  "name": "SN0 Root",       "hotkey": "5Ckaoft1B1CQ9zBV2FLVju4KPuMQzJVn7QUf3JeTvTq1uUes", "staked": 6.00},
    {"subnet": 64, "name": "SN64 Chutes",    "hotkey": "5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ", "staked": 1.96},
    {"subnet": 62, "name": "SN62 Ridges",    "hotkey": "5Djyacas3eWLPhCKsS3neNSJonzfxJmD3gcrMTFDc4eHsn62", "staked": 1.24},
    {"subnet": 4,  "name": "SN4 Targon",     "hotkey": "5Hp18g9P8hLGKp9W3ZDr4bvJwba6b6bY3P2u3VdYf8yMR8FM", "staked": 1.04},
    {"subnet": 75, "name": "SN75 Hippius",   "hotkey": "5G1Qj93Fy22grpiGKq6BEvqqmS2HVRs3jaEdMhq9absQzs6g", "staked": 0.79},
    {"subnet": 68, "name": "SN68 Nova",      "hotkey": "5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg", "staked": 0.58},
    {"subnet": 51, "name": "SN51 Lium",      "hotkey": "5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51", "staked": 0.35},
    {"subnet": 55, "name": "SN55 Ko/Precog", "hotkey": "5CzSYnS88EpVv7Kve7U1VCYKjCbtKpxZNHMacAy3BkfCsn55", "staked": 0.31},
]

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
log = logging.getLogger(__name__)


def send(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except Exception as e:
        log.error(e)


def get_vt(subnet, hotkey):
    """Fetch vtrust from taostats metagraph API.
    SN0 Root: vtrust reflects weight-setting performance on Root network.
    Returns None if unavailable or not a validator on that subnet.
    """
    try:
        r = requests.get(
            f"https://api.taostats.io/api/metagraph/latest/v1?netuid={subnet}&hotkey={hotkey}",
            headers={"Authorization": os.environ.get("TAOSTATS_API_KEY", "")},
            timeout=15
        )
        if r.status_code == 200:
            d = r.json().get("data", [])
            if d and d[0].get("validator_trust") is not None:
                return float(d[0]["validator_trust"])
    except Exception as e:
        log.warning(e)
    return None


def load_state():
    try:
        return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"last_alerted": {}}
    except:
        return {"last_alerted": {}}


def run():
    state = load_state()
    now   = time.time()
    ok    = []

    for s in STAKES:
        vt = get_vt(s["subnet"], s["hotkey"])

        if vt is None:
            # SN0 Root may return None if hotkey not in metagraph as validator
            ok.append(f"? {s['name']} unavailable")
            log.warning(f"{s['name']} vtrust unavailable — check hotkey or API key")
            continue

        log.info(f"{s['name']} vtrust={round(vt, 4)}")

        if vt < VTRUST_THRESHOLD:
            last = state["last_alerted"].get(s["hotkey"], 0)
            if now - last < ALERT_COOLDOWN:
                continue
            msg = f"⚠️ VTrust ALERT: {s['name']}\nscore={round(vt, 4)} below threshold {VTRUST_THRESHOLD}\nHotkey: {s['hotkey']}"
            send(msg)
            state["last_alerted"][s["hotkey"]] = now
            log.warning(f"ALERT sent for {s['name']} vtrust={round(vt, 4)}")
        else:
            ok.append(f"{s['name']} ✅ {round(vt, 4)}")

    STATE_FILE.write_text(json.dumps(state, indent=2))

    if ok:
        send("✅ VTrust OK:\n" + "\n".join(ok))

    log.info("vtrust check complete")


run()
