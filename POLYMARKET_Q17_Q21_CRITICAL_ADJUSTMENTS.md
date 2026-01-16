# Polymarket Q17-Q21 Response Analysis & Critical Adjustments

**Date**: January 16, 2026  
**Status**: ⚠️ **CRITICAL ADJUSTMENTS REQUIRED**  
**Priority**: P0 (Production Blocker)

---

## Executive Summary

Polymarket support provided detailed responses to Q17-Q21. Analysis reveals **1 critical bug** (using unstable category.slug instead of category.id) and **4 enhancement opportunities** (score field, WebSocket sequence tracking, FOK orders, composite quality metrics).

### Critical Finding
**PRODUCTION BLOCKER**: Current category filtering uses `category.slug` and `category.label` which have `updatedAt` timestamps, indicating they can change over time. This could cause markets to suddenly fail filtering mid-lifecycle.

**Required Action**: Refactor to use `category.id` (stable identifier) instead of slug/label.

---

## Q17: Score Field Methodology

### ❌ Polymarket Response Summary
- **No general "score" field exists** for market quality assessment
- Only liquidity rewards scoring exists (not designed for quality filtering)
- **Recommendation**: Build composite quality metrics from available data

### 🔧 Required Adjustments

#### 1. Remove Score Field Logging
**File**: `src/strategies/market_making_strategy.py` (Lines 2265-2283)

**Current Code** (WRONG):
```python
score = market.get('score')
if score is not None:
    try:
        score_value = float(score)
        logger.debug(f"[SCORE ANALYSIS] {market_id}: score={score_value:.4f}")
```

**Action**: Remove entire Layer 9 score field block - field doesn't exist as quality metric.

#### 2. Build Composite Quality Metric
Design institutional-grade quality scoring from available fields:

```python
def calculate_market_quality_score(market: Dict[str, Any]) -> float:
    """
    Composite quality score (0-100) using available market data
    
    Components:
    - Liquidity depth (40%): Total $ on both sides within 2% of mid
    - Volume consistency (30%): 24h volume / liquidity ratio (stability)
    - Spread tightness (20%): Normalized spread (tighter = better)
    - Order book depth (10%): Number of orders per side (diversity)
    
    Thresholds:
    - Excellent (90-100): Institutional-grade liquid markets
    - Good (70-90): Suitable for market making
    - Fair (50-70): Arbitrage only
    - Poor (<50): Reject
    """
    # Implementation deferred to separate ticket
    pass
```

**Priority**: P2 (Enhancement) - Current volume/liquidity/spread filters are sufficient for production.

---

## Q18: Category Slug Stability ⚠️ CRITICAL

### ❌ Polymarket Response Summary
- **Slugs can change over time** (createdAt/updatedAt timestamps exist)
- **Use `category.id` instead of `slug` for stability**
- Consider `tag_id` parameter with `/markets` endpoint as primary filtering method
- No complete list of category slugs provided (must discover via API)

### 🔧 Required Adjustments ⚠️ **P0 PRIORITY**

#### 1. Refactor Category Filtering to Use category.id
**File**: `src/strategies/market_making_strategy.py` (Lines 2175-2210)

**Current Code** (WRONG - uses unstable slugs):
```python
priority_category_slugs = [
    'crypto', 'cryptocurrency', 'bitcoin', 'ethereum',
    'politics', 'elections', 'us-politics',
    'sports', 'nfl', 'nba', 'soccer',
]

for cat in categories:
    if isinstance(cat, dict):
        cat_slug = cat.get('slug', '').lower()
        cat_label = cat.get('label', '').lower()
        if any(priority_slug in cat_slug or priority_slug in cat_label 
               for priority_slug in priority_category_slugs):
            category_match = True
            break
```

**Required Fix**:
```python
# POLYMARKET FEEDBACK (Jan 2026): Use category.id for stability
# "IDs are typically more stable identifiers than human-readable slugs
# that might get updated for SEO or clarity reasons."
#
# Priority category IDs (institutional-grade high-volume markets)
# Discovery: Query /markets endpoint and examine categories arrays
PRIORITY_CATEGORY_IDS = [
    # Crypto markets (to be discovered from API)
    # 'cat_crypto_id_here',
    # Politics markets
    # 'cat_politics_id_here',
    # Sports markets
    # 'cat_sports_id_here',
]

# Check if market matches any priority category by ID
for cat in categories:
    if isinstance(cat, dict):
        cat_id = cat.get('id', '')
        if cat_id in PRIORITY_CATEGORY_IDS:
            category_match = True
            break

# Fallback: Use slug matching ONLY if no IDs configured yet
if not PRIORITY_CATEGORY_IDS:
    # Temporary slug-based matching until IDs discovered
    priority_category_slugs = ['crypto', 'politics', 'sports']
    for cat in categories:
        cat_slug = cat.get('slug', '').lower()
        if any(slug in cat_slug for slug in priority_category_slugs):
            category_match = True
            break
```

#### 2. Discover Category IDs from API
**Action**: Create discovery script to fetch all active category IDs:

```python
# scripts/discover_category_ids.py
import requests

# Fetch sample markets and extract all unique category IDs
response = requests.get('https://gamma-api.polymarket.com/markets')
markets = response.json()

category_map = {}
for market in markets:
    for cat in market.get('categories', []):
        cat_id = cat.get('id')
        cat_slug = cat.get('slug')
        cat_label = cat.get('label')
        if cat_id:
            category_map[cat_id] = {'slug': cat_slug, 'label': cat_label}

# Print mapping for constants.py
for cat_id, info in category_map.items():
    print(f"'{cat_id}',  # {info['slug']} - {info['label']}")
```

#### 3. Alternative: Use tag_id Parameter
Polymarket recommends using `tag_id` parameter with `/markets` endpoint:
```python
# Example: Fetch only crypto markets using tag_id
response = requests.get(
    'https://gamma-api.polymarket.com/markets',
    params={'tag_id': 'crypto_tag_id_here'}
)
```

**Decision**: Investigate tag_id approach vs category.id filtering. Tag_id may be superior for market discovery.

---

## Q19: Multi-Leg Arbitrage Staleness Detection

### ✅ Polymarket Response Summary
- **Reject entire opportunity if ANY leg exceeds staleness** ✓ (confirms our approach)
- WebSocket latency ~100ms, markets don't update synchronously
- **Cancel all pending orders immediately if staleness detected mid-execution**
- Multi-leg needs **tighter thresholds** (multiplied risk)
- **Recommendation**: Use FOK (Fill-Or-Kill) orders for atomic execution

### 🔧 Required Adjustments

#### 1. Document Current Multi-Leg Staleness Detection ✅
**Current Implementation** (CORRECT):

**File**: `src/strategies/arb_scanner.py`
```python
# Each leg checked against ARB_DATA_STALENESS_THRESHOLD (1.0 seconds)
# If ANY leg fails staleness check, entire opportunity rejected
# This is correct per Q19 response
```

**Status**: ✅ No changes needed - current approach validated by Polymarket.

#### 2. Add Mid-Execution Staleness Detection
**File**: `src/core/atomic_depth_aware_executor.py`

**Enhancement**:
```python
async def execute_atomic_arb(self, ...):
    """
    POLYMARKET FEEDBACK (Q19): If staleness detected mid-execution,
    cancel all pending orders immediately using cancelOrders() or cancelAll().
    
    Completing partial fills with stale data exposes to adverse selection risk.
    """
    
    # Before placing orders, final staleness check
    for leg in opportunity.legs:
        if time.time() - leg.book_snapshot_time > ARB_DATA_STALENESS_THRESHOLD:
            logger.error(
                f"[STALENESS ABORT] Leg {leg.market_id} went stale "
                f"({time.time() - leg.book_snapshot_time:.3f}s > {ARB_DATA_STALENESS_THRESHOLD}s) "
                f"- CANCELLING ALL ORDERS"
            )
            await self._cancel_all_orders()
            raise StaleDataError("Multi-leg opportunity went stale before execution")
```

**Priority**: P1 (Safety Enhancement)

#### 3. Consider Tighter Multi-Leg Thresholds
**File**: `src/config/constants.py`

**Current**: `ARB_DATA_STALENESS_THRESHOLD = 1.0` seconds  
**Recommendation**: Consider `0.5` seconds for multi-leg (Polymarket suggested 0.5-1s range)

**Rationale**: Multi-leg strategies multiply risk across positions - tighter threshold reduces adverse selection.

**Action**: Monitor production staleness rejection rates, tighten if needed.

#### 4. Implement FOK Orders for Atomic Execution ✅
**Current Implementation**: Already using FOK orders!

**File**: `src/core/atomic_depth_aware_executor.py`
```python
order_type: str = "FOK",  # Line 136
```

**Status**: ✅ Already implemented - no changes needed.

---

## Q20: Order Book Concentration Benchmarking

### ❌ Polymarket Response Summary
- **No typical concentration ratios documented**
- Need to calculate ourselves from order book data
- **Recommendation**: Use combined bid/ask metrics (most institutional traders do this)
- **Skip concentration checks for <10 orders per side** (small sample size unreliable)

### 🔧 Required Adjustments

#### 1. Document Current Concentration Approach ✅
**Current Implementation**: Uses combined bid/ask liquidity metrics

**File**: `src/strategies/market_making_strategy.py`
```python
# Current: Check total liquidity, not concentration
# This is acceptable per Q20 response - no changes needed
```

**Status**: ✅ Current approach validated.

#### 2. Consider Adding Concentration Filter (Optional Enhancement)
**Priority**: P3 (Nice-to-have)

```python
def calculate_concentration_ratio(bids: List[Dict], asks: List[Dict]) -> float:
    """
    Calculate top-3 order concentration (combined bid+ask)
    
    Returns:
        Ratio of top 3 orders' liquidity to total liquidity (0-1)
        
    Skip if fewer than 10 orders per side (Polymarket recommendation)
    """
    if len(bids) < 10 or len(asks) < 10:
        return None  # Skip concentration check for thin books
        
    # Sort by size
    sorted_orders = sorted(
        bids + asks,
        key=lambda x: float(x.get('size', 0)),
        reverse=True
    )
    
    top_3_liquidity = sum(float(o.get('size', 0)) for o in sorted_orders[:3])
    total_liquidity = sum(float(o.get('size', 0)) for o in sorted_orders)
    
    return top_3_liquidity / total_liquidity if total_liquidity > 0 else 0

# Reject if concentration > 0.6 (top 3 orders = 60%+ of liquidity)
# This indicates thin market with high manipulation risk
```

---

## Q21: WebSocket Sequence Number Gap Handling ⚠️ CRITICAL

### ❌ Polymarket Response Summary
- **No explicit sequence numbers in WebSocket message payloads**
- **Must implement own counter system** to track missed messages
- **No guidance on gap handling** (snapshot/resubscribe/continue)
- No info on typical gap frequency
- **Recommendation**: Maintain local orderbook with incremental updates + reconnection logic with exponential backoff

### 🔧 Required Adjustments ⚠️ **P0 PRIORITY**

#### 1. Implement Sequence Tracking System
**File**: `src/core/market_data_manager.py` (New class)

**Required Implementation**:
```python
@dataclass
class SequenceTracker:
    """
    Track WebSocket message sequence to detect gaps
    
    POLYMARKET FEEDBACK (Q21): No explicit sequence numbers in messages,
    must implement own counter system.
    """
    market_id: str
    expected_sequence: int = 0
    last_message_time: float = 0.0
    gap_count: int = 0
    
    def validate_sequence(self, message_id: int) -> bool:
        """
        Validate message sequence, detect gaps
        
        Returns:
            True if sequence valid, False if gap detected
        """
        if message_id != self.expected_sequence:
            self.gap_count += 1
            logger.warning(
                f"[SEQUENCE GAP] {self.market_id}: Expected {self.expected_sequence}, "
                f"got {message_id} (gap={message_id - self.expected_sequence})"
            )
            return False
        
        self.expected_sequence += 1
        self.last_message_time = time.time()
        return True


class PolymarketWSManager:
    """
    ENHANCEMENT: Add sequence tracking to existing WebSocket manager
    """
    def __init__(self):
        self._sequence_trackers: Dict[str, SequenceTracker] = {}
        self._local_sequence_counter = 0  # Our own message counter
        
    async def _on_message(self, message: Dict[str, Any]):
        """
        POLYMARKET FEEDBACK (Q21): Implement sequence tracking at message level
        """
        # Assign local sequence number to each message
        self._local_sequence_counter += 1
        message['_local_seq'] = self._local_sequence_counter
        
        market_id = message.get('market', message.get('asset_id'))
        
        if market_id:
            if market_id not in self._sequence_trackers:
                self._sequence_trackers[market_id] = SequenceTracker(market_id)
            
            tracker = self._sequence_trackers[market_id]
            
            # Check if we missed messages (compare with expected sequence)
            # NOTE: Since Polymarket doesn't send sequence numbers, we track
            # our own received message count. Gaps would appear as:
            # - Sudden timestamp jumps (>500ms between messages on active market)
            # - Order book inconsistencies (impossible state transitions)
            
            if not tracker.validate_sequence(self._local_sequence_counter):
                # GAP DETECTED - trigger recovery
                await self._handle_sequence_gap(market_id)
```

#### 2. Implement Gap Recovery Protocol
**File**: `src/core/market_data_manager.py`

**Required Implementation**:
```python
async def _handle_sequence_gap(self, market_id: str):
    """
    Handle detected sequence gap
    
    POLYMARKET FEEDBACK (Q21): No guidance on gap handling from Polymarket.
    
    Strategy:
    1. Log gap for monitoring
    2. Mark orderbook as stale (force refresh from REST API)
    3. If gaps frequent (>5 in 60s), trigger reconnection
    """
    tracker = self._sequence_trackers.get(market_id)
    
    logger.error(
        f"[GAP RECOVERY] {market_id}: Sequence gap detected "
        f"(total gaps: {tracker.gap_count})"
    )
    
    # Strategy 1: Mark orderbook as stale (force REST API refresh)
    if market_id in self._cache._markets:
        self._cache._markets[market_id].is_stale = True
        logger.info(f"[GAP RECOVERY] {market_id}: Marked orderbook as stale")
    
    # Strategy 2: If gaps frequent, reconnect WebSocket
    if tracker.gap_count > 5:
        logger.critical(
            f"[GAP RECOVERY] {market_id}: {tracker.gap_count} gaps in session "
            f"- RECONNECTING WEBSOCKET"
        )
        await self._reconnect_with_backoff()
        
        # Reset gap counter after reconnection
        tracker.gap_count = 0

async def _reconnect_with_backoff(self):
    """
    Reconnect with exponential backoff
    
    POLYMARKET FEEDBACK (Q21): Recommended reconnection logic with exponential backoff
    """
    # Already implemented in current code - just needs sequence tracking addition
    pass
```

#### 3. Add Timestamp-Based Gap Detection
**Enhancement**: Since Polymarket doesn't send sequence numbers, use timestamp analysis:

```python
def detect_timestamp_gap(self, market_id: str, current_time: float) -> bool:
    """
    Detect sequence gaps via timestamp analysis
    
    POLYMARKET FEEDBACK (Q21): No explicit sequence numbers, must use heuristics
    
    Heuristic: If active market (>$10k volume) has no updates for >500ms,
    likely missed messages (WebSocket latency is ~100ms)
    """
    tracker = self._sequence_trackers.get(market_id)
    if not tracker:
        return False
    
    time_since_last = current_time - tracker.last_message_time
    
    # For active markets, >500ms silence suggests gap
    if time_since_last > 0.5:
        logger.warning(
            f"[TIMESTAMP GAP] {market_id}: {time_since_last:.3f}s since last message "
            f"(expected <500ms for active market)"
        )
        return True
    
    return False
```

**Priority**: P0 (Critical for data integrity)

---

## Follow-Up Questions for Polymarket

### Q22: Category/Tag ID Discovery
**Context**: Q18 response recommends using `category.id` and `tag_id` but doesn't provide complete lists.

**Question**: 
1. Can you provide the complete list of active `category.id` values and their corresponding slugs/labels?
2. What are the available `tag_id` values for the `/markets` endpoint filtering?
3. Which approach is preferred for institutional filtering: `category.id` matching or `tag_id` parameter?

### Q23: WebSocket Gap Frequency
**Context**: Q21 mentions implementing sequence tracking but doesn't specify typical gap frequency.

**Question**:
1. In production, what is the typical frequency of WebSocket message gaps (gaps per hour)?
2. At what gap rate should we trigger reconnection (e.g., >5 gaps per minute)?
3. Do you provide any WebSocket health metrics or status endpoints we can monitor?

### Q24: Composite Quality Metric Validation
**Context**: Q17 recommends building quality metrics from available data.

**Question**:
1. Can you validate our proposed composite quality score formula (liquidity 40%, volume consistency 30%, spread 20%, depth 10%)?
2. What are typical ranges for these metrics across different market categories?
3. Are there any hidden quality indicators in the API we should leverage?

---

## Implementation Priority Matrix

| Priority | Task | Impact | Effort | Status |
|----------|------|--------|--------|--------|
| **P0** | Refactor category filtering to use `category.id` | **HIGH** (Stability) | Medium | ⏳ TODO |
| **P0** | Implement WebSocket sequence tracking | **HIGH** (Data Integrity) | High | ⏳ TODO |
| **P0** | Discover category IDs from API | **HIGH** (Dependency) | Low | ⏳ TODO |
| **P1** | Add mid-execution staleness abort | Medium (Safety) | Low | ⏳ TODO |
| **P1** | Implement gap recovery protocol | Medium (Reliability) | Medium | ⏳ TODO |
| **P2** | Remove score field logging | Low (Cleanup) | Low | ⏳ TODO |
| **P2** | Build composite quality metric | Low (Enhancement) | High | 📋 DEFERRED |
| **P3** | Add concentration ratio filter | Low (Nice-to-have) | Medium | 📋 DEFERRED |

---

## Production Risk Assessment

### Critical Risks (P0)
1. **Category Slug Instability**: Current code uses `category.slug`/`category.label` which can change mid-market-lifecycle, causing sudden filtering failures. **Impact**: Markets could suddenly disappear from trading pipeline.

2. **WebSocket Gap Blindness**: No sequence tracking means we don't detect missed messages. **Impact**: Stale orderbook data could lead to adverse fills.

### Mitigation Strategy
1. **Immediate**: Deploy category.id refactor (estimated 2-4 hours)
2. **Immediate**: Deploy sequence tracking (estimated 4-6 hours)
3. **Short-term**: Submit Q22-Q24 to Polymarket for validation
4. **Medium-term**: Build composite quality metric (pending Q24 response)

---

## Next Steps

1. ✅ Create this analysis document
2. ⏳ Implement P0 fixes (category.id + sequence tracking)
3. ⏳ Create category ID discovery script
4. ⏳ Submit Q22-Q24 to Polymarket support
5. ⏳ Deploy P0 fixes to production
6. 📋 Implement P1 enhancements after validation
7. 📋 Defer P2/P3 enhancements pending further guidance
