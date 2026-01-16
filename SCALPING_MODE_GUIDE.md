# Polymarket Bot - Scalping Mode Guide (Institutional-Grade)

## Overview
**Scalping Mode** targets short-term liquid markets (<24hrs) using institutional-grade intelligence from **Polymarket support (Jan 2026)**.

### Key Features
- **Time-Based Discovery**: Uses `end_date_min/max` filters per Polymarket best practice
- **Fee-Rate Detection**: Identifies 15-min crypto markets with 20% maker rebates
- **Orderbook Depth Priority**: Focuses on exit speed over volume
- **Adaptive Learning**: Builds dynamic tag list from successful markets
- **$1K Volume Minimum**: Baseline recommended by support
- **15s Order TTL**: Faster capital rotation
- **5% Max Spread**: More flexibility than broad mode

---

## Quick Start

```bash
# Enable scalping mode
SCALPING_MODE=true python src/main.py

# Or for systemd service (already configured)
sudo systemctl restart polymarket-bot
```

---

## Configuration (Data-Driven)

| Parameter | Scalping | Broad | Source |
|-----------|----------|-------|--------|
| Settlement | 15min-24hr | 15min-3day | Support: 15-min markets exist |
| Volume Min | $1,000 | $10,000 | Support: baseline suggestion |
| Discovery | Time-filtered | Tag-based | Support: use end_date_min/max |
| Fee Check | ✅ Yes | ❌ No | Support: identify rebates |
| Order TTL | 15s | 25s | Fast rotation |
| Max Spread | 5% | 3% | More flexible |

---

## Discovery Flow (Polymarket Support Guidance)

1. **Time Query**: `/events?tag_id=235&end_date_min=NOW&end_date_max=NOW+24h`
2. **Fee Check**: `/fee-rate?token_id={id}` (>0 = 15-min rebate market)
3. **Adaptive Tags**: Build list from markets meeting criteria
4. **Scoring**: `(volume/hours) × (1 + fee_boost)` - prioritizes fast + rebates

---

## Expected Logs

```
🎯 SCALPING MODE: Time-based discovery on tag 235 (Bitcoin)
Found 15 events in time window
🎯 Fee-enabled market found: Bitcoin 11:45-11:50 (fee_rate=1000 bps)
Time-filtered discovery: 3 tags, 2 fee-enabled markets
✅ Time-based discovery found 3 qualifying tags
```

---

## Maker Rebates (Support Confirmed)

- **Current Rate**: 20% (Jan 12-18, 2026)
- **Taker Fee**: 1.56% at 50% probability
- **Markets**: 15-min crypto only
- **Qualification**: No minimum, just fill maker orders

---

## Troubleshooting

### No Markets Found
```
⚠️ No markets found in time window
```
**Normal** - 15-min markets may not exist currently. Bot falls back to <24hr discovery.

### Performance
- **Settlement**: 15min-24hrs (realistic)
- **Turnover**: 1-8x/day
- **$72 Capital**: Potentially $144/day (2x turnover)

---

## Notes

Based on official Polymarket support guidance (Jan 2026). Institutional-grade implementation with:
- Time-based filtering (recommended by support)
- Fee-rate detection (identifies rebate opportunities)
- Adaptive tag learning (data-driven)
- Conservative rate limits (2s-32s backoff)

For details: https://docs.polymarket.com/#get-events
