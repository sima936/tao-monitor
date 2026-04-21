#!/usr/bin/env python3
import os, time, requests, logging
from datetime import datetime

LOG_DIR = os.path.expanduser("~/tao_logs")
os.makedirs(LOG_DIR, exist_ok=True)
JOBS = {
    "vtrust_monitor": {"log": os.path.join(LOG_DIR, "vtrust_monitor.log"), "max_silence_hours": 7},
    "rebalancer": {"log": os.path.join(LOG_DIR, "rebalancer.log"), "max_silence_hours": 25},
}
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
WATCHDOG_LOG = os.path.join(LOG_DIR, "watchdog.log")
logging.basicConfig(filename=WATCHDOG_LOG, level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logging.error(f"Telegram error: {e}")

def run():
    logging.info("Watchdog check started.")
    now = time.time()
    alerts = []
    for job, cfg in JOBS.items():
        if not os.path.exists(cfg["log"]):
            alerts.append(f"⚠️ `{job}` log file missing!")
            continue
        silence_hours = (now - os.path.getmtime(cfg["log"])) / 3600
        if silence_hours > cfg["max_silence_hours"]:
            last_seen = datetime.fromtimestamp(os.path.getmtime(cfg["log"])).strftime("%Y-%m-%d %H:%M:%S")
            alerts.append(f"🚨 *TAO Watchdog Alert*\nJob `{job}` silent for *{silence_hours:.1f}h*\nLast seen: `{last_seen}`\nCheck cron on Infinity8.")
            logging.warning(f"{job} silent {silence_hours:.1f}h")
        else:
            logging.info(f"{job} OK — {silence_hours:.1f}h ago")
    for a in alerts:
        send_telegram(a)
    logging.info("All jobs healthy." if not alerts else "Alerts sent.")

if __name__ == "__main__":
    run()

