# Polymarket Support - Follow-Up Questions (Round 2)

**Date:** January 16, 2026  
**Context:** Institutional-grade bot implementing Phase 1-3 upgrades based on your feedback  
**Previous Response:** Reviewed answers Q1-Q16, implemented all recommendations

---

## Q17: Score Field Methodology

**Context:** You mentioned "We do provide a `score` field in market responses that could serve as a quality indicator" (Q1).

**Questions:**

1. **Scoring Methodology:** What algorithm is used to calculate the `score` field? Is it:
   - Volume-weighted (higher volume = higher score)?
   - Liquidity-weighted (tighter spreads = higher score)?
   - Time-decay adjusted (newer markets penalized)?
   - Composite metric (multiple factors)?

2. **Score Range:** What numerical range does the score use?
   - 0-1 (probability-style)?
   - 0-100 (percentile)?
   - Unbounded (can exceed 1 or 100)?

3. **Interpretation:** Do higher scores **always** indicate better market quality, or are there edge cases where low-score markets are still legitimate/tradeable?

4. **Filtering Recommendation:** For institutional market making with $50-500 capital allocation, what minimum score threshold would you recommend?
   - Example: Should we filter out markets with `score < 0.5`?

**Why This Matters:** We want to add score-based filtering as Layer 9 in our Tier-1 filter, but need to understand the methodology to avoid false positives.

---

## Q18: Category Slug Stability & Complete List

**Context:** You recommended using the `categories` array with `slug` or `id` fields instead of text matching (Q13).

**Questions:**

1. **Slug Stability:** Are `category.slug` values stable over time, or can they change for existing markets?
   - If slugs change, will historical markets be updated retroactively?

2. **Complete Slug List:** What's the comprehensive list of active category slugs we can filter on?
   - Your examples: `POLITICS`, `SPORTS`, `CRYPTO`, `CULTURE`, `ECONOMICS`, `TECH`, `FINANCE`, `WEATHER`, `MENTIONS`
   - Are there additional slugs not listed in documentation?
   - Case-sensitive or case-insensitive matching?

3. **Coverage:** If we filter by `category.slug`, will this capture **all** relevant markets, or do some markets lack category metadata?
   - What percentage of markets have null/empty categories?

4. **Slug vs ID:** Should we use `category.slug` or `category.id` for filtering?
   - Which is more stable for production use?
   - Do IDs map 1:1 with slugs (e.g., crypto = ID 5)?

**Why This Matters:** Our market making strategy specializes in crypto/politics/sports. We need reliable category filtering to allocate capital efficiently.

---

## Q19: Staleness Detection for Multi-Leg Arbitrage

**Context:** You recommended 500ms-1s staleness for arbitrage (Q10). Our multi-outcome arbitrage requires atomic execution across 3-7 constituent markets.

**Questions:**

1. **Multi-Leg Staleness Policy:** For multi-leg arbitrage (3-7 constituent markets), should we reject the **entire** opportunity if **any** leg exceeds the staleness threshold?
   - Example: Leg 1 = 0.5s old, Leg 2 = 0.8s old, Leg 3 = 1.2s old → Abort?
   - Or: Use maximum staleness across all legs?

2. **WebSocket Update Synchrony:** Do constituent markets in the same event update **synchronously** via WebSocket, or with temporal skew?
   - If skewed, what's typical delay between first and last leg updates?

3. **Mid-Execution Staleness:** If we detect staleness **mid-execution** (after locking some legs but before completing basket), best practice is:
   - Cancel all pending orders + abort?
   - Complete partial fills + accept orphan risk?
   - Immediate liquidation of filled legs?

4. **Threshold Differentiation:** Should we use different staleness thresholds for atomic multi-leg vs single-market execution?
   - Current: 1.0s for both
   - Recommended: Tighter for multi-leg (e.g., 500ms)?

**Why This Matters:** Multi-leg arbitrage has cumulative staleness risk. One stale leg can invalidate the entire basket, causing adverse fills.

---

## Q20: Order Book Concentration Percentile Benchmarking

**Context:** You confirmed 40% top-order concentration is reasonable for institutional filtering (Q2), and recommended analyzing order book distribution before large trades.

**Questions:**

1. **Typical Concentration:** What's the typical (median/mean) concentration ratio across **liquid** Polymarket markets?
   - Example: "Top 3 orders = X% of total depth" in well-functioning markets
   - This helps us calibrate whether 40% is conservative/aggressive

2. **Category Differences:** Are there category-specific concentration patterns?
   - Example: Do politics markets have more concentrated liquidity than crypto markets?
   - Should we use different thresholds per category?

3. **Bid vs Ask Asymmetry:** Should we calculate separate concentration thresholds for **bid** vs **ask** sides?
   - Or use a combined metric (average of both sides)?
   - Do ask-side concentrations tend to differ from bid-side?

4. **Small Order Books:** For markets with <10 orders per side, should we skip concentration checks entirely?
   - Insufficient sample size for meaningful statistics?
   - Or apply stricter threshold (e.g., 30% instead of 40%)?

**Why This Matters:** We want to ensure our 40% threshold isn't accidentally filtering out normal market microstructure or allowing whale-dominated books.

---

## Q21: WebSocket Sequence Number Gap Handling

**Context:** You mentioned "sequence number tracking to detect missed messages and maintain a local orderbook with incremental updates" (Q11).

**Questions:**

1. **Sequence Number Availability:** Does the Polymarket WebSocket feed include **explicit sequence numbers** in message payloads?
   - Or do we need to implement our own incrementing counter?
   - Field name if explicit (e.g., `seq`, `sequence_id`, `message_id`)?

2. **Gap Detection Protocol:** If we detect a gap in sequence numbers (e.g., receive seq 105 after seq 103), recommended action is:
   - Request full order book snapshot via REST API?
   - Unsubscribe + resubscribe to affected market?
   - Continue with incremental updates (gap is benign)?

3. **Gap Frequency:** What's the typical frequency of sequence gaps in production?
   - Daily occurrences?
   - Weekly?
   - Rare edge case during network instability?

4. **Snapshot/Resync Messages:** Does Polymarket send any **explicit** "snapshot" or "resync" messages to reset local state?
   - Or must we always infer from REST API when state becomes inconsistent?

**Why This Matters:** Incorrect order book state due to missed WebSocket messages can cause catastrophic execution errors (trading against stale book = adverse selection).

---

## Summary of Action Items

Based on your responses to Q17-Q21, we will:

1. **Q17:** Implement score-based filtering in Tier-1 filter (Layer 9)
2. **Q18:** Refine category filtering with stable slug/ID mappings
3. **Q19:** Tighten multi-leg arbitrage staleness policy (potentially 500ms)
4. **Q20:** Calibrate concentration thresholds against market benchmarks
5. **Q21:** Implement robust WebSocket gap detection and recovery

---

## Current Implementation Status

**Phase 1 (DEPLOYED):**
- ✅ Staleness thresholds: MM=2s, Arb=1s (per your Q10 recommendation)
- ✅ Blacklist manager: Using `endDateIso` field (per your Q4 recommendation)
- ✅ NegRisk flag: Verified propagation from detection → signature

**Phase 2 (DEPLOYED):**
- ✅ Extreme prices: Arb widened to 0.05-0.95 (per your Q6 feedback)
- ✅ Category filtering: Using structured `categories` array (per your Q13 feedback)
- ✅ Tick sizes: Accepting 0.1, 0.01, 0.001, 0.0001 (per your Q15 feedback)

**Phase 3 (IN PROGRESS):**
- 🔄 Score field: Logging for analysis, awaiting Q17 response for threshold
- ✅ NegRisk audit: Flow verified (event detection → order signature)
- 🔄 Follow-up questions: Sent Q17-Q21 for final clarification

---

Thank you for your continued support in helping us achieve institutional-grade standards!
