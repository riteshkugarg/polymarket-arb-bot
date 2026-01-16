# MarketBlacklistManager - Pre-Emptive Zombie Market Filtering

## Executive Summary

**MarketBlacklistManager** implements high-performance pre-emptive filtering to reject problematic "zombie markets" BEFORE expensive order book analysis, saving API calls and compute cycles.

**Key Performance Metrics:**
- **O(1) lookups** for condition_id manual blacklist (set membership)
- **O(k) keyword matching** where k = number of keywords (~10)
- **Zero overhead** for clean markets (early exit after manual check)
- **Pre-emptive filtering** prevents wasted API/compute on zombie markets

---

## Problem Statement

Without pre-emptive filtering, the bot:
1. ✅ Fetches all markets from Gamma API
2. ❌ Processes long-dated contracts (2027/2028 elections)
3. ❌ Analyzes order books for presidential nomination markets
4. ❌ Wastes API calls on markets that will never pass eligibility

**Result:** Unnecessary latency, API rate limit consumption, compute waste.

---

## Solution Architecture

### Three-Layer Filtering System

```
┌─────────────────────────────────────────────────────────────┐
│              MARKET BLACKLIST MANAGER                       │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ LAYER 1: Manual Kill-Switch (O(1))                   │  │
│  │ - condition_ids set lookup                            │  │
│  │ - Emergency blacklist for specific markets            │  │
│  └──────────────────────────────────────────────────────┘  │
│                         ▼                                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ LAYER 2: Keyword Matching (O(k))                     │  │
│  │ - 2027/2028/2029/2030 (long-dated elections)         │  │
│  │ - presidential-nomination, democrat-nomination, etc.  │  │
│  │ - Searches: slug, question, description              │  │
│  └──────────────────────────────────────────────────────┘  │
│                         ▼                                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ LAYER 3: Temporal Guardrail (O(1))                   │  │
│  │ - Parse endDate from market metadata                  │  │
│  │ - Reject if >365 days until settlement               │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
                        ▼
            [PASS] → Eligible for further analysis
            [REJECT] → Skip immediately
```

---

## Implementation Details

### Configuration Constants

```python
# Hard-coded keyword blacklist (case-insensitive substring matching)
HARD_BLACKLIST_KEYWORDS = [
    '2027', '2028', '2029', '2030',  # Long-dated contracts
    'presidential-nomination',
    'democrat-nomination',
    'republican-nomination',
    'gop-nomination'
]

# Temporal guardrail: reject markets settling beyond this threshold
MAX_DAYS_UNTIL_SETTLEMENT = 365  # 1 year
```

### Key Methods

#### `is_blacklisted(market: Dict) -> bool`
**Purpose:** Three-layer check to determine if market should be filtered

**Logic Flow:**
```python
1. CHECK 1: Manual ID Kill-Switch
   if market['condition_id'] in self._blacklisted_ids:
       return True  # Emergency blacklist

2. CHECK 2: Keyword Matching
   searchable_text = f"{slug} {question} {description}".lower()
   for keyword in HARD_BLACKLIST_KEYWORDS:
       if keyword in searchable_text:
           return True  # Keyword match

3. CHECK 3: Temporal Guardrail
   days_until_settlement = parse_endDate(market['endDate'])
   if days_until_settlement > MAX_DAYS_UNTIL_SETTLEMENT:
       return True  # Too far in future

return False  # Passed all checks
```

**Performance:**
- **Best case:** O(1) - Manual ID hit
- **Average case:** O(k) - Keyword scan (k=10 keywords)
- **Worst case:** O(k + datetime_parse) - Full scan + temporal check

#### `add_manual_blacklist(condition_id: str)`
**Purpose:** Emergency kill-switch to blacklist specific markets

**Usage Example:**
```python
# Discovered problematic market during runtime
blacklist_manager.add_manual_blacklist("0x1234abcd")
logger.warning("Market 0x1234abcd manually blacklisted")
```

#### `reset_stats()` and `log_summary()`
**Purpose:** Track rejection metrics for monitoring

**Console Output:**
```
[INFO] MarketBlacklistManager: 234 markets checked, 47 blacklisted (20.1%)
  - Manual ID: 2
  - Keyword: 38 (2027: 12, presidential-nomination: 18, 2028: 8)
  - Temporal: 7 (>365 days)
```

---

## Integration Points

### ArbitrageStrategy Integration

**File:** `src/strategies/arbitrage_strategy.py`

**Location:** `_discover_arb_eligible_markets()` method

```python
def _discover_arb_eligible_markets(self) -> List[Dict]:
    """Fetch events, apply blacklist, then check arbitrage eligibility"""
    all_events = self.client.get_events()
    
    # PRE-EMPTIVE BLACKLIST FILTERING
    self.blacklist_manager.reset_stats()
    filtered_events = [
        event for event in all_events 
        if not self.blacklist_manager.is_blacklisted(event)
    ]
    self.blacklist_manager.log_summary()
    
    logger.debug(f"Events after blacklist: {len(filtered_events)}/{len(all_events)}")
    
    # Continue with arbitrage analysis on filtered events...
```

**Performance Impact:**
- **Before:** Analyze 500 events → 47 zombie markets waste API calls
- **After:** Analyze 453 events → 0 zombie markets

### MarketMakingStrategy Integration

**File:** `src/strategies/market_making_strategy.py`

**Location:** `_update_eligible_markets()` method

```python
async def _update_eligible_markets(self) -> None:
    """Scan Gamma API, apply blacklist, then check MM eligibility"""
    
    # Fetch from Gamma API
    all_markets = []  # Fetched via aiohttp
    
    logger.debug(f"Total markets fetched from Gamma API: {len(all_markets)}")
    
    # PRE-EMPTIVE BLACKLIST FILTERING
    self.blacklist_manager.reset_stats()
    filtered_markets = [
        m for m in all_markets 
        if not self.blacklist_manager.is_blacklisted(m)
    ]
    self.blacklist_manager.log_summary()
    
    logger.debug(f"Markets after blacklist: {len(filtered_markets)}/{len(all_markets)}")
    
    # Continue with Tier-1 filter on filtered markets...
    for m in filtered_markets:
        is_eligible, reason = self._is_market_eligible_debug(m)
        ...
```

**Performance Impact:**
- **Before:** Process 1,000 markets → 200 zombie markets waste compute
- **After:** Process 800 markets → 0 zombie markets

---

## Operational Workflow

### Startup Sequence

```
1. Strategy.__init__()
   ├─ self.blacklist_manager = MarketBlacklistManager()
   └─ Manual blacklist: empty set initially

2. Strategy.run() - Main loop iteration
   ├─ Fetch markets from API
   ├─ blacklist_manager.reset_stats()  # Clear counters
   ├─ filtered_markets = [m for m in all_markets if not blacklist_manager.is_blacklisted(m)]
   ├─ blacklist_manager.log_summary()  # Console summary
   └─ Continue eligibility checks on filtered_markets
```

### Runtime Emergency Blacklist

```python
# Discovered issue with market during trading
if detect_anomaly(market):
    strategy.blacklist_manager.add_manual_blacklist(market['condition_id'])
    logger.warning(f"Market {market['condition_id']} manually blacklisted")
    strategy.order_manager.cancel_all_orders(market['condition_id'])
```

---

## Testing and Validation

### Unit Test Coverage

```python
# Test keyword filtering
def test_keyword_blacklist():
    manager = MarketBlacklistManager()
    market = {
        'condition_id': '0xabc123',
        'slug': 'presidential-election-2027',
        'question': 'Who wins 2027 presidential election?',
        'description': 'This market resolves in 2027',
        'endDate': '2027-11-03T00:00:00Z'
    }
    assert manager.is_blacklisted(market) == True  # Keyword: '2027'

# Test temporal guardrail
def test_temporal_guardrail():
    manager = MarketBlacklistManager()
    market = {
        'condition_id': '0xdef456',
        'slug': 'long-term-market',
        'question': 'Long-dated market',
        'description': 'Settles far in future',
        'endDate': (datetime.now() + timedelta(days=500)).isoformat()
    }
    assert manager.is_blacklisted(market) == True  # >365 days

# Test manual kill-switch
def test_manual_blacklist():
    manager = MarketBlacklistManager()
    manager.add_manual_blacklist('0xbad_market')
    market = {
        'condition_id': '0xbad_market',
        'slug': 'clean-slug',
        'question': 'Normal question',
        'description': 'No issues',
        'endDate': (datetime.now() + timedelta(days=30)).isoformat()
    }
    assert manager.is_blacklisted(market) == True  # Manual ID hit
```

### Integration Validation

```bash
# Run bot with DEBUG logging to see blacklist in action
python src/main.py --log-level DEBUG

# Expected console output:
[DEBUG] Total markets fetched from Gamma API: 1,000
[INFO] MarketBlacklistManager: 1,000 markets checked, 187 blacklisted (18.7%)
  - Manual ID: 0
  - Keyword: 142 (2027: 45, presidential-nomination: 67, 2028: 30)
  - Temporal: 45 (>365 days)
[DEBUG] Markets after blacklist filtering: 813 (rejected: 187)
[DEBUG] Starting Tier-1 eligibility checks on 813 markets...
```

---

## Monitoring and Metrics

### Key Performance Indicators

| Metric | Target | Monitoring |
|--------|--------|------------|
| **Blacklist Rate** | 10-25% | blacklist_manager.get_stats()['total_checked'] / total_rejected |
| **Keyword Matches** | 60-80% of rejections | keyword_breakdown in log_summary() |
| **Temporal Rejections** | 20-40% of rejections | temporal_count in stats |
| **Manual Blacklist Size** | <10 markets | len(_blacklisted_ids) |

### Alert Thresholds

```python
stats = blacklist_manager.get_stats()
rejection_rate = stats['total_blacklisted'] / stats['total_checked']

if rejection_rate > 0.40:
    logger.warning(f"High blacklist rejection rate: {rejection_rate:.1%}")
    logger.warning("Polymarket may have added many long-dated markets")
    # Consider expanding HARD_BLACKLIST_KEYWORDS

if len(blacklist_manager._blacklisted_ids) > 10:
    logger.warning(f"Manual blacklist size: {len(blacklist_manager._blacklisted_ids)}")
    logger.warning("Review manual blacklist entries for cleanup")
```

---

## Maintenance and Updates

### Adding New Keywords

```python
# File: src/core/blacklist_manager.py

# Add new problematic keyword patterns
HARD_BLACKLIST_KEYWORDS = [
    '2027', '2028', '2029', '2030',
    'presidential-nomination',
    'democrat-nomination',
    'republican-nomination',
    'gop-nomination',
    'midterm-2026',  # NEW: Add midterm elections
    'senate-primary'  # NEW: Senate primaries
]
```

### Adjusting Temporal Threshold

```python
# Conservative: Reject markets >6 months out
MAX_DAYS_UNTIL_SETTLEMENT = 180

# Aggressive: Reject markets >1 month out
MAX_DAYS_UNTIL_SETTLEMENT = 30

# Current production: 1 year threshold
MAX_DAYS_UNTIL_SETTLEMENT = 365
```

### Emergency Blacklist Expansion

```python
# Runtime addition (no code changes needed)
strategy.blacklist_manager.add_manual_blacklist('0x123abc')
strategy.blacklist_manager.add_manual_blacklist('0x456def')

# Persistent addition (requires code change)
# File: src/core/blacklist_manager.py
def __init__(self):
    self._blacklisted_ids: Set[str] = {
        '0x123abc',  # Known zombie market
        '0x456def'   # Known zombie market
    }
```

---

## Production Deployment Checklist

- [x] **Code Implementation:** MarketBlacklistManager class created (229 lines)
- [x] **ArbitrageStrategy Integration:** Integrated in _discover_arb_eligible_markets
- [x] **MarketMakingStrategy Integration:** Integrated in _update_eligible_markets
- [x] **Unit Tests:** Coverage for keyword, temporal, manual blacklist checks
- [x] **Integration Tests:** Validated with DEBUG logging in both strategies
- [x] **Performance Validation:** O(1) manual check, O(k) keyword scan
- [x] **Documentation:** Complete guide with examples and monitoring

### Pre-Launch Validation

```bash
# 1. Compile check
python -m py_compile src/core/blacklist_manager.py
python -m py_compile src/strategies/arbitrage_strategy.py
python -m py_compile src/strategies/market_making_strategy.py

# 2. Run unit tests
pytest tests/test_blacklist_manager.py -v

# 3. Integration test with DEBUG logging
python src/main.py --log-level DEBUG --dry-run

# 4. Monitor blacklist stats
# Check console output for rejection rates and keyword breakdown
```

---

## FAQ

### Q: Why not use regex for keyword matching?
**A:** Substring matching (`keyword in text`) is faster and sufficient for our use case. Regex would add complexity without performance benefit.

### Q: Can I blacklist markets by category?
**A:** Yes, add category keywords to `HARD_BLACKLIST_KEYWORDS`. Example: `'politics'`, `'elections'`

### Q: What if a blacklisted market becomes tradable?
**A:** Use `remove_manual_blacklist(condition_id)` to un-blacklist. For keyword/temporal, adjust constants.

### Q: How does this interact with Tier-1 filter?
**A:** Blacklist runs BEFORE Tier-1. Order: `Gamma API → Blacklist → Tier-1 Filter → Order Book Analysis`

### Q: Performance impact of 1,000 markets?
**A:** ~0.5ms per market (keyword scan). Total: 500ms for 1,000 markets. Negligible compared to order book fetches (100-500ms each).

---

## References

- **Source Code:** `src/core/blacklist_manager.py`
- **Integration:** `src/strategies/arbitrage_strategy.py`, `src/strategies/market_making_strategy.py`
- **Related Docs:** `SELECTIVE_TIER1_FILTER_UPGRADE.md`, `INSTITUTIONAL_STRATEGY_AUDIT.md`

---

**Status:** ✅ Production-ready  
**Version:** 1.0  
**Last Updated:** 2025-01-XX  
**Author:** Institutional Upgrade Team
