# Follow-Up Questions for Polymarket Support (Q22-Q24)

**Date**: January 16, 2026  
**Context**: Follow-up to Q17-Q21 responses for institutional-grade implementation

---

## Q22: Category/Tag ID Discovery & Filtering Strategy

**Context**: Your Q18 response recommended using `category.id` instead of `slug` for stability, and mentioned the `tag_id` parameter with `/markets` endpoint as an alternative filtering approach. However, no complete list of category IDs or tag IDs was provided.

**Questions**:

1. **Category ID List**: Can you provide the complete list of active `category.id` values and their corresponding slugs/labels? This would allow us to populate our filtering configuration without reverse-engineering via API scraping.

2. **Tag ID System**: What are the available `tag_id` values for the `/markets` endpoint filtering? How does the tag system differ from the categories array?

3. **Preferred Approach**: For institutional-grade market filtering, which approach do you recommend:
   - Matching against `category.id` values in the `categories` array (client-side filtering)
   - Using `tag_id` parameter in API requests (server-side filtering)
   - Combination of both?

4. **Category Coverage**: Do all active markets have category metadata, or should we maintain fallback text-based keyword matching for markets without structured categories?

**Current Implementation**:
- Created `scripts/discover_category_ids.py` to fetch IDs from API
- Refactored filtering to use `category.id` instead of `slug`/`label`
- `MM_TARGET_CATEGORIES` constant awaiting ID population

---

## Q23: WebSocket Gap Frequency & Health Metrics

**Context**: Your Q21 response confirmed that WebSocket messages don't include explicit sequence numbers, requiring us to implement our own counter system. However, no guidance was provided on typical gap frequency or recovery thresholds.

**Questions**:

1. **Production Gap Frequency**: In normal production conditions, what is the typical frequency of WebSocket message gaps or drops? (e.g., gaps per hour, or percentage of messages dropped)

2. **Reconnection Threshold**: At what gap rate should institutional clients trigger WebSocket reconnection? For example:
   - Should we reconnect after 5 gaps in 60 seconds?
   - Should we reconnect if any market has >10% message loss?
   - Is there a recommended "unhealthy connection" threshold?

3. **Health Monitoring**: Do you provide any WebSocket health metrics, status endpoints, or connection quality indicators that clients can monitor? For example:
   - Server-side sequence numbers we can validate against?
   - Connection quality metrics in authenticated WebSocket feeds?
   - Status API endpoint showing WebSocket feed health?

4. **Gap Detection Heuristics**: Since messages lack sequence numbers, what heuristics do you recommend for detecting missed messages?
   - Timestamp analysis (e.g., >500ms between messages on active market)?
   - Order book state inconsistencies (e.g., impossible price transitions)?
   - Volume/trade mismatch detection?

**Current Implementation**:
- Planning to implement local message counter (`_local_sequence_counter`)
- Timestamp-based gap detection for active markets (>500ms silence = potential gap)
- Exponential backoff reconnection logic (already exists)
- Need validation on thresholds before production deployment

---

## Q24: Composite Quality Metric Validation

**Context**: Your Q17 response indicated no general `score` field exists for market quality assessment, and recommended building custom quality metrics from available market data (volume, liquidity, spread, order book depth).

**Questions**:

1. **Formula Validation**: Can you validate our proposed composite quality score formula?
   - **Liquidity Depth (40% weight)**: Total $ on both sides within 2% of midpoint
   - **Volume Consistency (30% weight)**: 24h volume / liquidity ratio (measures stability vs speculation)
   - **Spread Tightness (20% weight)**: Normalized spread (tighter = better)
   - **Order Book Depth (10% weight)**: Number of orders per side (diversity of market makers)
   
   Is this a reasonable institutional-grade quality metric, or should we weight components differently?

2. **Category Benchmarks**: What are typical ranges for these metrics across different market categories?
   - **Politics markets**: Typical liquidity, volume, spread?
   - **Crypto markets**: Typical liquidity, volume, spread?
   - **Sports markets**: Typical liquidity, volume, spread?
   
   This would help us set category-specific quality thresholds.

3. **Hidden Quality Indicators**: Are there any other quality indicators in the API responses that we should leverage? For example:
   - Market maker count or diversity?
   - Historical resolution accuracy?
   - Time-to-resolution metrics?
   - Dispute rate or controversy indicators?

4. **Minimum Quality Thresholds**: For institutional-grade market making, what minimum quality thresholds would you recommend?
   - Minimum liquidity: $500 current, is this sufficient?
   - Minimum 24h volume: $1000 current, is this sufficient?
   - Maximum spread: 5% current, is this appropriate?

**Current Implementation**:
- Using 8-layer filter (liquidity, volume, spread, extremes, staleness, category, tick size, blacklist)
- Planning to add composite quality metric as 9th layer (P2 enhancement)
- Need validation on formula and thresholds before implementation

---

## Priority & Timeline

| Question | Priority | Blocking | Timeline Needed |
|----------|----------|----------|-----------------|
| **Q22** | **P0** | **YES** | ASAP (blocks production deployment) |
| **Q23** | **P1** | Partial | 1-2 weeks (need for monitoring setup) |
| **Q24** | **P2** | NO | 2-4 weeks (enhancement, not critical) |

**Critical Path**: Q22 (category IDs) is blocking production deployment. Without stable category IDs, our filtering could fail mid-market-lifecycle if slugs are updated for SEO/clarity.

---

## Context: Current Implementation Status

### ✅ Completed (Based on Q17-Q21 Responses)
1. Refactored category filtering to use `category.id` instead of `slug`/`label`
2. Removed non-existent `score` field logging
3. Created category ID discovery script
4. Updated constants.py with category ID TODOs
5. Comprehensive analysis document created

### ⏳ Awaiting Q22-Q24 Responses
1. **Q22**: Populate `MM_TARGET_CATEGORIES` with actual category IDs
2. **Q23**: Set WebSocket gap thresholds and implement monitoring
3. **Q24**: Build and tune composite quality metric

### 📋 Deferred (Pending Polymarket Guidance)
1. WebSocket sequence tracking system (P0 - awaiting Q23)
2. Composite quality metric (P2 - awaiting Q24)
3. Category-specific quality thresholds (P2 - awaiting Q24)

---

## Technical Details for Reference

### Category Filtering Code (market_making_strategy.py)
```python
# PRIMARY: Match by stable category.id (Polymarket institutional standard)
for cat in categories:
    if isinstance(cat, dict):
        cat_id = cat.get('id', '')
        if cat_id in MM_TARGET_CATEGORIES:
            category_match = True
            logger.debug(f"[CATEGORY MATCH] {market_id}: Matched category.id={cat_id}")
            break
```

### WebSocket Sequence Tracking (Planned)
```python
class SequenceTracker:
    market_id: str
    expected_sequence: int = 0
    last_message_time: float = 0.0
    gap_count: int = 0
    
    def validate_sequence(self, message_id: int) -> bool:
        if message_id != self.expected_sequence:
            self.gap_count += 1
            logger.warning(f"[SEQUENCE GAP] {self.market_id}: Gap detected")
            return False
        self.expected_sequence += 1
        return True
```

### Composite Quality Score (Planned)
```python
def calculate_market_quality_score(market: Dict[str, Any]) -> float:
    """
    Returns score 0-100 based on:
    - Liquidity depth (40%)
    - Volume consistency (30%)
    - Spread tightness (20%)
    - Order book depth (10%)
    """
    # Implementation deferred pending Q24 response
    pass
```

---

## Expected Response Format

For efficiency, please provide:

1. **Q22**: List of category IDs (JSON or CSV format preferred)
   ```json
   {
     "cat_abc123": {"slug": "politics", "label": "Politics"},
     "cat_def456": {"slug": "crypto", "label": "Cryptocurrency"}
   }
   ```

2. **Q23**: Numerical thresholds for monitoring
   - Typical gap rate: X gaps per hour
   - Reconnection trigger: >Y gaps in Z seconds
   - Healthy connection definition: <X% message loss

3. **Q24**: Benchmark ranges per category
   - Politics: Liquidity $X-Y, Volume $A-B, Spread C%-D%
   - Crypto: Liquidity $X-Y, Volume $A-B, Spread C%-D%
   - Sports: Liquidity $X-Y, Volume $A-B, Spread C%-D%

---

## Appreciation

Thank you for the detailed responses to Q17-Q21. The guidance on using `category.id` (Q18) prevented a critical production stability issue, and the confirmation on multi-leg staleness detection (Q19) validated our atomic execution approach.

These follow-up questions (Q22-Q24) will enable us to complete our institutional-grade implementation with full confidence in production stability.
