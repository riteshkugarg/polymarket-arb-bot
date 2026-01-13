"""
MIRROR STRATEGY - 3 PARALLEL FLOWS ARCHITECTURE
Polymarket Arbitrage Bot - January 2026

===============================================================================
OVERVIEW
===============================================================================

The Mirror Strategy now implements 3 loosely-coupled parallel flows that run
asynchronously at different frequencies. This design maximizes efficiency and
minimizes latency while keeping components independent and fault-tolerant.

┌─────────────────────────────────────────────────────────────────┐
│                     MIRROR STRATEGY (Main)                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────┐  │
│  │ FLOW 1           │  │ FLOW 2           │  │ FLOW 3      │  │
│  │ Trade Mirroring  │  │ Position Align   │  │ Redemption  │  │
│  │                  │  │                  │  │             │  │
│  │ Frequency:       │  │ Frequency:       │  │ Frequency:  │  │
│  │ Every 2-5s       │  │ Every 60s        │  │ Every 60s   │  │
│  │                  │  │                  │  │             │  │
│  │ Purpose:         │  │ Purpose:         │  │ Purpose:    │  │
│  │ Copy whale's     │  │ Sell positions   │  │ Redeem      │  │
│  │ trades (buy &    │  │ whale exited     │  │ closed      │  │
│  │ sell)            │  │                  │  │ positions   │  │
│  │                  │  │                  │  │             │  │
│  │ Status: Active   │  │ Status: Active   │  │ Status:     │  │
│  │                  │  │                  │  │ Stub (API)  │  │
│  └──────────────────┘  └──────────────────┘  └─────────────┘  │
│        │                       │                     │         │
│        └───────────┬───────────┴──────────┬──────────┘         │
│                    │                      │                    │
│          ┌─────────▼──────────┐  ┌────────▼──────┐            │
│          │ Shared Resources   │  │ Order Manager │            │
│          │ ─────────────────  │  └────────┬──────┘            │
│          │ - PolymarketClient │           │                   │
│          │ - Balance Cache    │           │                   │
│          │ - Position Cache   │           ▼                   │
│          │ - Config           │      Execute Orders            │
│          └────────────────────┘      (Buy/Sell)               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

===============================================================================
FLOW 1: TRADE MIRRORING (High Frequency - Every 2-5 Seconds)
===============================================================================

PURPOSE:
────────
Continuously monitor the whale's recent trades and execute matching orders
immediately. This is the core of the mirror strategy - copying profitable
trades before market conditions change.

CONFIGURATION (in constants.py):
────────────────────────────────
MIRROR_TRADE_POLLING_INTERVAL_SEC = 2        # Check every 2 seconds (fast!)
MIRROR_TRADE_TIME_WINDOW_MINUTES = 10        # Only look at trades < 10 min old
MIRROR_ENTRY_DELAY_SEC = 0                   # No delay between trades
MIRROR_BALANCE_CACHE_SECONDS = 30            # Cache balance for 30s

MIRROR_USE_PROPORTIONAL_SIZE = False         # Use fixed order size
MIRROR_MAX_ORDER_SIZE_USD = 1.0              # Max $1 per order
MIRROR_USE_MARKET_ORDERS = False             # Use limit orders
MIRROR_LIMIT_ORDER_PRICE_BUFFER_PERCENT = 4  # 4% buffer for limit pricing

EXECUTION FLOW:
───────────────
1. Get cached balance (reduces API calls)
   └─ Cache for 30s to avoid redundant calls
   └─ If zero balance, skip cycle

2. Fetch whale's RECENT trades
   └─ Only trades from last 10 minutes
   └─ Ignores old positions (whale may be at loss)
   └─ Reduces API load vs fetching all positions

3. Validate token IDs
   └─ Check if markets are still active
   └─ Skip closed/invalid markets

4. Get our current positions
   └─ Avoid buying positions we already own
   └─ Track which markets we're in

5. Build trading opportunities
   └─ For each whale trade:
      ├─ Check price bounds (0.10 ≤ price ≤ 0.85)
      ├─ Apply entry price guard (±0.05% deviation)
      ├─ Calculate order size (fixed $1)
      └─ Only trades not in our portfolio

6. Execute qualifying trades
   └─ For each opportunity:
      ├─ Run safety checks (balance, slippage)
      ├─ Place order (BUY/SELL)
      ├─ Invalidate balance cache
      ├─ Log execution
      └─ Wait MIRROR_ENTRY_DELAY_SEC before next

7. Sleep 2 seconds, repeat

LATENCY:
────────
Whale trade → Our trade: ~5-10 seconds typical
- Whale trade happens
- 0-2s: Wait for next cycle
- 1-2s: Fetch whale trades
- 1-2s: Validate and analyze
- 1-2s: Execute our trade
Total: 2-8 seconds behind (vs 5-30s for polling)

BENEFITS:
─────────
✓ Low latency - catch good trades early
✓ Frequent checks - no opportunities missed
✓ Balance cache - reduces API load
✓ Easy to understand - single purpose

RISKS:
──────
✗ Rapid fire orders - risk of cascading losses
✗ High API load - could hit rate limits
✗ Size accumulation - if not careful

MITIGATION:
───────────
→ Fixed small order size ($1) limits exposure
→ Price guards prevent bad entries
→ Slippage checks prevent overpaying
→ Circuit breaker stops on losses

===============================================================================
FLOW 2: POSITION ALIGNMENT (Lower Frequency - Every 60 Seconds)
===============================================================================

PURPOSE:
────────
Detect when the whale exits a position and immediately sell our matching
position. This is "exit following" - if whale is getting out, we should too.

KEY INSIGHT:
────────────
A position the whale closed might still be in our portfolio. If the whale
is exiting, they likely have a reason (market turning against them, taking
profits, rebalancing). We should follow immediately to avoid holding a
"dead" position.

CONFIGURATION (in constants.py):
────────────────────────────────
MIRROR_POSITION_ALIGNMENT_INTERVAL_SEC = 60     # Check every 60 seconds
MIRROR_CLOSED_POSITIONS_LOOK_BACK_LIMIT = 10    # Check whale's last 10 closes
MIRROR_SELL_IMMEDIATELY_ON_WHALE_EXIT = True    # Sell asap if detected
MIRROR_SELL_ORDER_TYPE = 'LIMIT'                # Use limit orders
MIRROR_SELL_PRICE_BUFFER_PERCENT = 2.0          # 2% buffer for exit

EXECUTION FLOW:
───────────────
1. Get our current positions
   └─ All markets we're holding shares in

2. Fetch whale's CLOSED positions
   └─ Check whale's last 10 closed positions
   └─ Identifies recent whale exits

3. Find intersection
   └─ For each whale-closed position:
      └─ Check if we still own it

4. For each matched position:
   └─ Get current market price
   └─ Calculate our position value
   └─ Skip if below dust threshold ($0.10)
   └─ Create SELL opportunity

5. Execute sells
   └─ Sell immediately (exit following)
   └─ Log whale exit detection
   └─ Free USDC for new opportunities

6. Sleep 60 seconds, repeat

EXAMPLE:
────────
Time 0s:  Whale buys YES at $0.45 (1000 shares = $450)
Time 2s:  We buy YES at $0.46 (100 shares = $46) - Flow 1
Time 30s: Market at $0.60, whale closes all YES and buys NO
Time 60s: Flow 2 detects whale closed YES position
Time 61s: We immediately sell our YES position at $0.59
Result:  +$13 profit on $46 investment (28% gain!)

WHY NOT WAIT?
─────────────
If whale is exiting, the trend may be turning against us.
Every second we hold, price could drop further.
Early exit = higher profit / lower loss.

BENEFITS:
─────────
✓ Exit following - profit from whale's insight
✓ Prevents "bag holding" - don't stay in losing trades
✓ Frees USDC - capital for new opportunities
✓ Lower frequency - less API load

RISKS:
──────
✗ False exits - whale may re-enter the same position
✗ Premature exits - whale may exit, then market goes our way
✗ Order rejection - selling may fail (no liquidity)

MITIGATION:
───────────
→ Limit orders with small buffer - don't panic sell
→ Dust threshold - avoid selling tiny positions
→ Error logging - understand why sells fail

===============================================================================
FLOW 3: POSITION REDEMPTION (Lower Frequency - Every 60 Seconds)
===============================================================================

PURPOSE:
────────
When a Polymarket market resolves (outcome is determined), winning shares
can be redeemed for $1 USDC each. This flow automatically collects profits
from closed markets.

HOW POLYMARKET RESOLUTION WORKS:
────────────────────────────────
1. Market opens: Users trade contracts for YES/NO outcome
2. Market trading period: Prices fluctuate, we trade
3. Outcome determined: Official resolution data becomes available
4. Market resolves: Smart contract determines winner
5. Redemption: Winning shares = $1 USDC each, losing shares = $0

EXAMPLE:
────────
Market: "Will Bitcoin reach $100k by Dec 2024?"

Scenario 1 (Win):
┌────────────────────────────────┐
│ We buy 100 YES at $0.40 = $40  │
│ Bitcoin reaches $100k!         │
│ Market resolves to YES         │
│ Redeem: 100 YES × $1 = $100    │
│ Profit: $60 (150% gain!)       │
└────────────────────────────────┘

Scenario 2 (Loss):
┌────────────────────────────────┐
│ We buy 100 YES at $0.80 = $80  │
│ Bitcoin doesn't reach $100k    │
│ Market resolves to NO          │
│ Redeem: 100 YES × $0 = $0      │
│ Loss: $80 (total loss)         │
└────────────────────────────────┘

CONFIGURATION (in constants.py):
────────────────────────────────
MIRROR_POSITION_REDEMPTION_INTERVAL_SEC = 60      # Check every 60 seconds
MIRROR_AUTO_REDEEM_CLOSED_POSITIONS = True        # Automatically redeem
MIRROR_BATCH_REDEEM_SIZE = 5                      # Redeem max 5 per cycle

CURRENT STATUS:
───────────────
⚠️  STUB IMPLEMENTATION - Waiting for Polymarket API support

The flow structure is in place and ready. Implementation awaits:
- Polymarket redemption API endpoint
- Get closed positions API (partially available)
- Redemption transaction handling
- Integration with order_manager.py

FUTURE IMPLEMENTATION:
──────────────────────
1. Get closed/resolved positions
   └─ Markets where outcome is determined

2. Check outcomes
   └─ Winning positions we own
   └─ Losing positions we own

3. Redeem winning shares
   └─ Send smart contract call
   └─ Receive $1 USDC per winning share

4. Log profits
   └─ Track redemption gains
   └─ Update performance metrics

BENEFITS:
─────────
✓ Automatic profit collection
✓ Frees USDC locked in closed markets
✓ Requires no trading - just redemption
✓ Pure profit (if won)

CURRENT LIMITATION:
───────────────────
Flow 3 is currently a stub because Polymarket's redemption API is
not yet fully documented or available. The pattern is established
and implementation can be added once API support is confirmed.

===============================================================================
LOOSELY COUPLED DESIGN
===============================================================================

All 3 flows are designed to be LOOSELY COUPLED:

INDEPENDENCE:
─────────────
Each flow:
├─ Has its own async task loop
├─ Runs at independent frequency
├─ Can fail without affecting others
├─ Has isolated error handling
└─ Reports status independently

SHARED RESOURCES:
─────────────────
Minimal coupling through shared resources:

✓ PolymarketClient
  └─ Single instance, thread-safe
  └─ All flows use same client

✓ OrderManager
  └─ Single instance, handles order execution
  └─ All flows use for placing orders

✓ Configuration
  └─ Read-only after initialization
  └─ No shared state changes

✓ Logging
  └─ Read-only, async-safe
  └─ All flows log independently

✗ Balance Cache (Flow 1 only)
  └─ Not shared with other flows
  └─ Invalidated on each trade

COORDINATED SHUTDOWN:
─────────────────────
When stopping the strategy:
1. Stop signal sent
2. All flows gracefully shutdown
3. Tasks cancelled
4. Logger shutdown
5. Resources cleaned

NO BLOCKING:
────────────
Flows don't wait for each other:
- Flow 1 doesn't wait for Flow 2
- Flow 2 doesn't wait for Flow 3
- All run independently
- Minimal delays from order execution

ERROR ISOLATION:
────────────────
If Flow 2 crashes:
- Flow 1 continues trading
- Flow 3 continues checking redemptions
- Flow 2 restarts after 5s backoff
- No cascade failures

===============================================================================
CONFIGURATION MANAGEMENT
===============================================================================

All configurations are in: src/config/constants.py

GROUPED BY FLOW:
────────────────
✓ Flow 1 parameters: MIRROR_TRADE_*
✓ Flow 2 parameters: MIRROR_POSITION_ALIGNMENT_*
✓ Flow 3 parameters: MIRROR_POSITION_REDEMPTION_*
✓ General parameters: MIRROR_*

This makes it easy to:
- Find parameters for a specific flow
- Understand what each value does
- Adjust one flow without affecting others
- Share common settings (e.g., address)

DYNAMIC CONFIGURATION:
──────────────────────
All parameters are read from MIRROR_STRATEGY_CONFIG dict:

config = {
    'flow_1_interval_sec': 2,
    'flow_1_max_order_size_usd': 1.0,
    'flow_2_interval_sec': 60,
    'flow_2_sell_price_buffer_percent': 2.0,
    'flow_3_interval_sec': 60,
    'flow_3_auto_redeem': True,
}

This allows:
- Runtime parameter changes (future enhancement)
- Different strategies with different configs
- A/B testing parameters easily
- Documented configuration versioning

===============================================================================
DEPLOYMENT RECOMMENDATIONS
===============================================================================

STARTING WITH CONSERVATIVE SETTINGS:
────────────────────────────────────
1. Start all 3 flows enabled
2. Set Flow 1 interval to 5 seconds initially
3. Set Flow 2 & 3 intervals to 60 seconds
4. Use fixed order size of $1
5. Monitor first 24 hours

SCALING UP:
───────────
Day 1-7:    Keep defaults ($1 orders, 2-5s polling)
Week 2:     If profitable, increase to $2-3 orders
Week 3:     Consider proportional sizing
Week 4:     Add more strategy instances

MONITORING:
───────────
Track per flow:
- Execution frequency (how often each runs)
- Success rate (% orders filled vs rejected)
- Average profit/loss per trade
- API call count (rate limit monitoring)
- Error rates and types

OPTIMIZATION:
──────────────
Based on real performance:
- Adjust polling interval (2-10s range)
- Tune order sizes
- Optimize price buffers
- Monitor balance cache effectiveness

===============================================================================
TROUBLESHOOTING
===============================================================================

FLOW 1 NOT EXECUTING TRADES:
─────────────────────────────
Check:
├─ Balance > 0 (no funds)
├─ ENABLE_TIME_BASED_FILTERING = True
├─ Whale actually trading (check logs)
├─ Price guards not too tight (increase MIN_BUY_PRICE)
└─ Order size valid (≥0.01, ≤MAX_ORDER_USD)

FLOW 2 NOT SELLING EXITS:
──────────────────────────
Check:
├─ Whale actually closing positions
├─ We own matching positions
├─ Position value > DUST_THRESHOLD
├─ Sell orders not failing
└─ Closed positions API returning data

FLOW 3 NO REDEMPTIONS:
──────────────────────
Note: Currently stub implementation
- Polymarket redemption API not yet available
- Implementation ready, awaiting API support

HIGH API USAGE:
───────────────
Solutions:
├─ Increase Flow 1 interval (5s instead of 2s)
├─ Increase Flow 2 interval (120s instead of 60s)
├─ Enable balance caching (already done)
├─ Reduce MIRROR_CLOSED_POSITIONS_LOOK_BACK_LIMIT
└─ Use fewer concurrent whale wallets

LOSING MONEY:
──────────────
Check:
├─ Entry price guards too loose (whale entering at bad prices)
├─ Order size too large (reduce MIRROR_MAX_ORDER_SIZE_USD)
├─ Slippage excessive (increase MIRROR_LIMIT_ORDER_PRICE_BUFFER_PERCENT)
├─ Circuit breaker threshold too high
└─ Market conditions unfavorable for mirroring

===============================================================================
FUTURE ENHANCEMENTS
===============================================================================

SHORT TERM:
───────────
✓ Flow 3: Implement redemption when API available
✓ Add flow-level metrics and monitoring
✓ Implement proportional order sizing
✓ Add circuit breaker per flow

MEDIUM TERM:
────────────
✓ Multi-whale tracking (track 5-10 whales)
✓ Adaptive polling interval based on whale activity
✓ Machine learning order size optimization
✓ Sentiment-based position sizing

LONG TERM:
──────────
✓ Dynamic flow frequency based on opportunity
✓ Parallel whale tracking across instances
✓ Advanced position redemption strategies
✓ Derivative strategies (grid, arbitrage, etc.)

===============================================================================
SUMMARY
===============================================================================

The Mirror Strategy 3-Flow architecture provides:

✨ EFFICIENCY
   └─ Parallel flows eliminate blocking
   └─ Different frequencies for different tasks
   └─ Minimal shared state

🔒 RELIABILITY
   └─ Independent error handling per flow
   └─ Graceful degradation (one flow fails, others continue)
   └─ Automatic recovery with backoff

📊 VISIBILITY
   └─ Flow-specific logging
   └─ Clear separation of concerns
   └─ Easy performance monitoring

🛠️ MAINTAINABILITY
   └─ Each flow has single responsibility
   └─ Configuration grouped by flow
   └─ Easy to add new flows

This design is production-ready and scalable for handling multiple whales
and strategies in parallel.

Next: Deploy and monitor! See PRODUCTION_DEPLOYMENT.md for AWS setup.
"""
