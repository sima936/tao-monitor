"""
TAO Monitor — API Debug Tool
=============================
Inspect raw Taostats API responses to fix field mapping issues.

Run on Infinity8:
    python debug_api.py --api-key "$TAOSTATS_API_KEY" --netuid 4

Prints:
  1. Raw pool/latest response for the subnet (all fields)
  2. Raw metagraph response (first 3 neurons, to check field names)
  3. Diagnosis of known issues (seven_day_prices, stake fields)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests


BASE_URL = "https://api.taostats.io"


def api_get(api_key: str, endpoint: str, params: dict = None) -> dict:
    headers = {"Authorization": api_key, "Accept": "application/json"}
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def inspect_pool(api_key: str, netuid: int):
    print(f"\n{'='*60}")
    print(f"POOL LATEST — SN{netuid}")
    print(f"{'='*60}")

    data = api_get(api_key, "/api/dtao/pool/latest/v1", {"netuid": netuid})
    pools = data.get("data", [])

    if not pools:
        print(f"ERROR: No data returned. Full response:\n{json.dumps(data, indent=2)}")
        return None

    pool = pools[0]

    # Print all top-level keys and their types/preview values
    print(f"\nAll fields returned for SN{netuid}:\n")
    for k, v in pool.items():
        if isinstance(v, list):
            print(f"  {k:35s} list[{len(v)}]  first={v[0] if v else 'empty'}")
        elif isinstance(v, dict):
            print(f"  {k:35s} dict  keys={list(v.keys())}")
        else:
            print(f"  {k:35s} {str(v)[:60]}")

    # Specific diagnosis
    print(f"\n--- DIAGNOSIS ---")

    price = pool.get("price")
    print(f"price:                 {price!r}  (type: {type(price).__name__})")

    total_tao = pool.get("total_tao")
    liquidity = pool.get("liquidity")
    print(f"total_tao:             {total_tao!r}")
    print(f"liquidity:             {liquidity!r}")

    # Seven day prices — check exact field name
    sdp = pool.get("seven_day_prices")
    sdp_alt1 = pool.get("price_history")
    sdp_alt2 = pool.get("prices")
    sdp_alt3 = pool.get("history")
    print(f"\nseven_day_prices:      {repr(sdp)[:80] if sdp else 'EMPTY/MISSING'}")
    print(f"price_history:         {repr(sdp_alt1)[:80] if sdp_alt1 else 'not present'}")
    print(f"prices:                {repr(sdp_alt2)[:80] if sdp_alt2 else 'not present'}")
    print(f"history:               {repr(sdp_alt3)[:80] if sdp_alt3 else 'not present'}")

    # Look for any list field that might contain price history
    print(f"\nAll list fields (potential price history):")
    for k, v in pool.items():
        if isinstance(v, list) and len(v) > 0:
            print(f"  {k}: {len(v)} items, first={v[0]!r}")

    return pool


def inspect_metagraph(api_key: str, netuid: int):
    print(f"\n{'='*60}")
    print(f"METAGRAPH LATEST — SN{netuid}")
    print(f"{'='*60}")

    time.sleep(12.5)  # rate limit

    data = api_get(api_key, "/api/dtao/metagraph/latest/v1", {"netuid": netuid})
    neurons = data.get("data", [])

    if not neurons:
        print(f"ERROR: No data returned. Full response:\n{json.dumps(data, indent=2)}")
        return

    print(f"\nTotal neurons: {len(neurons)}")
    print(f"\nFirst neuron — all fields:")
    neuron = neurons[0]
    for k, v in neuron.items():
        if isinstance(v, dict):
            print(f"  {k:30s} dict  keys={list(v.keys())}  val={v}")
        else:
            print(f"  {k:30s} {str(v)[:60]}")

    # Specific stake field diagnosis
    print(f"\n--- STAKE FIELD DIAGNOSIS ---")
    stake = neuron.get("stake")
    stake_tao = neuron.get("stake_tao")
    alpha_stake = neuron.get("alpha_stake")
    tao_stake = neuron.get("tao_stake")
    coldkey = neuron.get("coldkey")

    print(f"stake:       {stake!r}")
    print(f"stake_tao:   {stake_tao!r}")
    print(f"alpha_stake: {alpha_stake!r}")
    print(f"tao_stake:   {tao_stake!r}")
    print(f"coldkey:     {coldkey!r} (type: {type(coldkey).__name__})")

    # Check if coldkey is a string or dict
    if isinstance(coldkey, dict):
        print(f"  coldkey.ss58: {coldkey.get('ss58')!r}")

    # Compute a sample gini from whatever stake field is populated
    print(f"\n--- CONCENTRATION SAMPLE (first 20 neurons) ---")
    sample = neurons[:20]
    for field in ["stake", "stake_tao", "alpha_stake", "tao_stake"]:
        vals = []
        for n in sample:
            v = n.get(field, 0)
            if v:
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    pass
        if vals:
            total = sum(vals)
            print(f"  {field}: {len(vals)} non-zero values, "
                  f"max={max(vals):.4f}, sum={total:.4f}")


def inspect_pool_history(api_key: str, netuid: int):
    print(f"\n{'='*60}")
    print(f"POOL HISTORY — SN{netuid} (limit=10)")
    print(f"{'='*60}")

    time.sleep(12.5)

    data = api_get(api_key, "/api/dtao/pool/history/v1", {"netuid": netuid, "limit": 10})
    entries = data.get("data", [])

    if not entries:
        print(f"No data. Full response:\n{json.dumps(data, indent=2)[:500]}")
        return

    print(f"\nTotal entries returned: {len(entries)}")
    print(f"\nFirst entry — all fields:")
    for k, v in entries[0].items():
        print(f"  {k:30s} {str(v)[:60]}")

    print(f"\nLast entry:")
    for k, v in entries[-1].items():
        print(f"  {k:30s} {str(v)[:60]}")


def main():
    parser = argparse.ArgumentParser(description="Debug Taostats API field mapping")
    parser.add_argument("--api-key", default=os.environ.get("TAOSTATS_API_KEY"))
    parser.add_argument("--netuid", type=int, default=4, help="Subnet to inspect (default: 4)")
    parser.add_argument("--skip-metagraph", action="store_true")
    parser.add_argument("--skip-history", action="store_true")
    args = parser.parse_args()

    if not args.api_key:
        print("ERROR: --api-key required or set TAOSTATS_API_KEY")
        sys.exit(1)

    inspect_pool(args.api_key, args.netuid)

    if not args.skip_metagraph:
        inspect_metagraph(args.api_key, args.netuid)

    if not args.skip_history:
        inspect_pool_history(args.api_key, args.netuid)

    print(f"\n{'='*60}")
    print("NEXT STEPS:")
    print("  1. Copy the actual field names above into taostats_fetch.py")
    print("  2. Fix pool_to_metrics() to use real price history field")
    print("  3. Fix concentration_from_metagraph() to use real stake field")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
