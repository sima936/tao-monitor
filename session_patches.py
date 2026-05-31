"""
TAO Monitor — Patches for This Session
========================================
Two changes needed in the existing files.

1. subnet_scoring_engine.py — add CONVICTION_HOLDS bypass
2. run_scoring.py — integrate price_cache

Apply these manually on Infinity8 / GitHub editor.
"""

# ─────────────────────────────────────────────────────────────────────────────
# PATCH 1: subnet_scoring_engine.py
# ─────────────────────────────────────────────────────────────────────────────
# Add near the top, after the MAX_GENIE_SCORE constant:

CONVICTION_HOLDS = {0, 4, 51, 64}
# These subnets bypass ALL pre-filters — they're held for fundamentals,
# not rotated on momentum. SN0 (Root/Kraken hotkey), SN4 (Targon),
# SN51 (lium.io), SN64 (Chutes).
# They still get scored so you see their Markov regime in Telegram.


# Then modify apply_pre_filters() — replace the function body:

def apply_pre_filters_PATCHED(
    metrics,
    max_price=0.08,
    min_pool=5.0,
    max_pool=200000.0,
    max_genie=0.85,
):
    # Conviction holds bypass all pre-filters
    if metrics.subnet_id in CONVICTION_HOLDS:
        return "pass"  # FilterResult.PASS

    if len(metrics.price_history) < 9:  # SUBNET_WINDOW + 2
        return "fail_insufficient_data"
    if metrics.token_price >= max_price:
        return "fail_price_too_high"
    if metrics.pool_depth < min_pool:
        return "fail_pool_too_shallow"
    if metrics.pool_depth > max_pool:
        return "fail_pool_too_deep"
    if metrics.genie_score >= max_genie:
        return "fail_genie_concentrated"
    return "pass"


# Also add a helper to flag conviction holds in Telegram output.
# In format_telegram_alert(), the top subnets section already uses
# a 📌 marker for current_holdings — conviction holds will show that.
# No other change needed in the formatter.


# ─────────────────────────────────────────────────────────────────────────────
# PATCH 2: run_scoring.py — integrate price cache
# ─────────────────────────────────────────────────────────────────────────────
# At the top of run_scoring.py, add:

# from price_cache import PriceCache, update_cache_from_metrics
# PRICE_DB = Path.home() / "tao_monitor" / "price_history.db"

# In the run() function, after fetching all_metrics (line ~70), add:

# cache = PriceCache(PRICE_DB)
# update_cache_from_metrics(cache, all_metrics)   # persist this cycle's prices
# cache.enrich_metrics(all_metrics)               # replace short history with cached
# logger.info(cache.status())                     # log cache health

# That's the entire integration — 4 lines.


# ─────────────────────────────────────────────────────────────────────────────
# DEPLOYMENT STEPS (Infinity8 SSH)
# ─────────────────────────────────────────────────────────────────────────────
"""
1. Upload files to Infinity8:
   scp debug_api.py price_cache.py user@infinity8-host:~/tao_monitor/

2. Run debug tool first to confirm field names:
   cd ~/tao_monitor
   python debug_api.py --api-key "$TAOSTATS_API_KEY" --netuid 4

3. Update taostats_fetch.py with real field names from debug output
   (seven_day_prices key, stake field in metagraph)

4. Backfill price history for holdings:
   python price_cache.py backfill --api-key "$TAOSTATS_API_KEY" \
     --netuids 4,51,62,64,68,75 --limit 200

5. Check cache status:
   python price_cache.py status

6. Apply Patch 1 to subnet_scoring_engine.py (CONVICTION_HOLDS)
   Apply Patch 2 to run_scoring.py (4 lines)

7. Run scoring cycle manually to verify:
   python run_scoring.py --api-key "$TAOSTATS_API_KEY" --verbose
"""
