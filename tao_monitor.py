import re
#!/usr/bin/env python3
"""Monitor TAO positions across subnets and log portfolio value."""

import asyncio
import json
import logging
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

import bittensor
from dotenv import load_dotenv

load_dotenv()

COLDKEY         = os.getenv("COLDKEY_ADDRESS")
WALLET_NAME     = os.getenv("WALLET_NAME", "tao_main")
DASHBOARD_URL   = os.getenv("DASHBOARD_URL", "none")
COINGECKO_KEY   = os.getenv("COINGECKO_API_KEY", "none")

LOG_DIR = Path(__file__).parent
LOG_FILE = LOG_DIR / "tao_monitor.log"

VTRUST_WARN_THRESHOLD = 0.1  # flag anything below this

# Alpha token GBP values are approximations based on bonding curve pricing,
# not precise mark-to-market figures.
SUBNET_VALIDATORS = {
    0:  ("Root → TAO.com", "root",  "5Ckaoft1B1CQ9zBV2FLVju4KPuMQzJVn7QUf3JeTvTq1uUes", 0.500),
    64: ("Chutes",          "alpha", "5Dt7HZ7Zpw4DppPxFM7Ke3Cm7sDAWhsZXmM5ZAmE7dSVJbcQ", 0.165),
    62: ("Ridges",          "alpha", "5Djyacas3eWLPhCKsS3neNSJonzfxJmD3gcrMTFDc4eHsn62", 0.110),
    4:  ("Targon",          "alpha", "5Hp18g9P8hLGKp9W3ZDr4bvJwba6b6bY3P2u3VdYf8yMR8FM", 0.095),
    75: ("Hippius",         "alpha", "5G1Qj93Fy22grpiGKq6BEvqqmS2HVRs3jaEdMhq9absQzs6g", 0.070),
    68: ("Nova",            "alpha", "5F1tQr8K2VfBr2pG5MpAQf62n5xSAsjuCZheQUy82csaPavg", 0.035),
    51: ("Lium",            "alpha", "5D7aRtpmVBKsQRzMA2ioUPL25onJPzBjiFVVt5uPZ3TDsn51", 0.035),
    55: ("Ko/Precog",       "alpha", "5CzSYnS88EpVv7Kve7U1VCYKjCbtKpxZNHMacAy3BkfCsn55", 0.025),
}


def setup_logging():
    """Configure logging with 7-day rotation."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )
    rotate_logs()


def rotate_logs():
    """Delete log entries older than 7 days by rewriting the log file."""
    if not LOG_FILE.exists():
        return
    cutoff = datetime.now() - timedelta(days=7)
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines(keepends=True)
        kept = []
        for line in lines:
            try:
                ts_str = line[:19]
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                if ts >= cutoff:
                    kept.append(line)
            except (ValueError, IndexError):
                kept.append(line)
        LOG_FILE.write_text("".join(kept))
    except Exception as e:
        logging.warning(f"Log rotation failed: {e}")


def fetch_tao_price():
    """Fetch TAO/GBP and TAO/USD from CoinGecko."""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=gbp,usd"
    headers = {"User-Agent": "tao-monitor/1.0"}
    if COINGECKO_KEY and COINGECKO_KEY != "none":
        headers["x-cg-demo-api-key"] = COINGECKO_KEY
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data["bittensor"]["gbp"], data["bittensor"]["usd"]
    except Exception as e:
        logging.error(f"Failed to fetch TAO price: {e}")
        return None, None


async def fetch_vtrust(sub, netuid: int, hotkey: str) -> float | None:
    """Fetch vtrust for our hotkey on a given subnet via metagraph."""
    try:
        mg = await sub.metagraph(netuid)
        if hotkey in mg.hotkeys:
            uid = mg.hotkeys.index(hotkey)
            return float(mg.validator_trust[uid])
        return None
    except Exception as e:
        logging.warning(f"Could not fetch vtrust for SN{netuid}: {e}")
        return None


async def fetch_positions(sub):
    """Fetch stake amounts and vtrust for all positions."""
    positions = {}
    for netuid, (name, stake_type, hotkey, pct) in SUBNET_VALIDATORS.items():
        try:
            stake_dict = await sub.get_stake_for_coldkey_and_hotkey(
                coldkey_ss58=COLDKEY,
                hotkey_ss58=hotkey,
            )
            # SDK returns dict of StakeInfo keyed by netuid
            if isinstance(stake_dict, dict):
                info = stake_dict.get(netuid)
                _s = re.sub(r"[^\d.]", "", str(info.stake)); tao_amount = float(_s) if _s else 0.0
            else:
                tao_amount = float(stake_dict) if stake_dict else 0.0

            subnet_price_tao = 1.0
            if stake_type == "alpha":
                try:
                    price_raw = await sub.get_subnet_price(netuid)
                    subnet_price_tao = float(price_raw.tao) if hasattr(price_raw, "tao") else float(price_raw)
                except Exception as e:
                    logging.warning(f"Could not get subnet price for SN{netuid}: {e}")

            # Fetch vtrust for this hotkey on this subnet
            vtrust = await fetch_vtrust(sub, netuid, hotkey)

            positions[netuid] = {
                "name": name,
                "stake_type": stake_type,
                "hotkey": hotkey,
                "tao_amount": tao_amount,
                "subnet_price_tao": subnet_price_tao,
                "vtrust": vtrust,
            }
        except Exception as e:
            logging.error(f"Failed to fetch position for SN{netuid} {name}: {e}")

    return positions


def calculate_values(positions, tao_gbp, tao_usd):
    """Calculate GBP value for each position."""
    results = []
    total_gbp = 0.0
    for netuid, pos in positions.items():
        if pos["stake_type"] == "root":
            value_gbp = pos["tao_amount"] * tao_gbp
        else:
            value_gbp = pos["tao_amount"] * pos["subnet_price_tao"] * tao_gbp
        total_gbp += value_gbp
        results.append({
            "netuid": netuid,
            "name": pos["name"],
            "tao_amount": pos["tao_amount"],
            "subnet_price_tao": pos["subnet_price_tao"],
            "value_gbp": value_gbp,
            "vtrust": pos.get("vtrust"),
        })
    return results, total_gbp


def log_positions(results, total_gbp, tao_gbp, tao_usd):
    logging.info("=" * 60)
    logging.info(f"TAO Monitor — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info(f"TAO Price: £{tao_gbp:.2f} GBP / ${tao_usd:.2f} USD")
    logging.info("-" * 60)
    warnings = []
    for r in results:
        vtrust = r.get("vtrust")
        if vtrust is not None:
            vtrust_str = f"vtrust={vtrust:.4f}"
            if vtrust < VTRUST_WARN_THRESHOLD:
                vtrust_str += " ⚠️"
                warnings.append(f"SN{r['netuid']} {r['name']}: vtrust={vtrust:.4f}")
        else:
            vtrust_str = "vtrust=N/A"
        logging.info(
            f"SN{r['netuid']:<4} {r['name']:<20} "
            f"{r['tao_amount']:.4f}  →  £{r['value_gbp']:.2f}  {vtrust_str}"
        )
    logging.info("-" * 60)
    logging.info(f"Total portfolio value: £{total_gbp:.2f} GBP")
    if warnings:
        logging.warning("⚠️  LOW VTRUST ALERTS:")
        for w in warnings:
            logging.warning(f"   {w}")
    logging.info("=" * 60)


def save_snapshot(results, total_gbp, tao_gbp, tao_usd):
    snapshot = {
        "timestamp": datetime.now().isoformat(),
        "tao_gbp": tao_gbp,
        "tao_usd": tao_usd,
        "total_gbp": total_gbp,
        "positions": results,
    }

    if DASHBOARD_URL and DASHBOARD_URL != "none":
        try:
            import urllib.request
            payload = json.dumps(snapshot).encode()
            req = urllib.request.Request(
                f"{DASHBOARD_URL}/api/tao-snapshot",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15):
                logging.info(f"Snapshot POSTed to dashboard.")
        except Exception as e:
            logging.error(f"Failed to POST snapshot to dashboard: {e}")
    else:
        out = Path("/tmp/tao_latest.json")
        try:
            out.write_text(json.dumps(snapshot, indent=2))
            logging.info(f"Snapshot saved to {out}")
        except Exception as e:
            logging.error(f"Failed to save snapshot: {e}")


async def main():
    setup_logging()
    logging.info("tao_monitor starting...")

    if not COLDKEY:
        logging.error("COLDKEY_ADDRESS not set in .env — aborting")
        sys.exit(1)

    tao_gbp, tao_usd = fetch_tao_price()
    if tao_gbp is None:
        logging.error("Could not fetch TAO price — aborting this run")
        sys.exit(1)

    try:
        sub = bittensor.AsyncSubtensor(network="finney")
        positions = await fetch_positions(sub)
    except Exception as e:
        logging.error(f"Failed to connect to Bittensor network: {e}")
        sys.exit(1)

    results, total_gbp = calculate_values(positions, tao_gbp, tao_usd)
    log_positions(results, total_gbp, tao_gbp, tao_usd)
    save_snapshot(results, total_gbp, tao_gbp, tao_usd)


if __name__ == "__main__":
    asyncio.run(main())
