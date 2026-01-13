"""
MIRROR TRADING STRATEGY - DETAILED BUYING & SELLING EXAMPLES
============================================================================

This guide explains exactly how the mirror strategy works for buying (Flow 1)
and selling (Flow 2) with real-world examples.

============================================================================
PART 1: HOW BUYING WORKS (FLOW 1 - TRADE MIRRORING)
============================================================================

OVERVIEW:
─────────
Flow 1 continuously watches the whale's trades and copies them.
It runs every 2 seconds to catch opportunities quickly.

Configuration:
├─ MIRROR_TRADE_POLLING_INTERVAL_SEC: 2 seconds
├─ MIRROR_TRADE_TIME_WINDOW_MINUTES: 10 minutes
├─ MIRROR_MAX_ORDER_SIZE_USD: $1.00
├─ MIN_BUY_PRICE: 0.10 (won't buy below this)
└─ MAX_BUY_PRICE: 0.85 (won't buy above this)

THE 6-STEP BUYING PROCESS:
──────────────────────────

Step 1: Check Your Balance
──────────────────────────
Every 2 seconds, Flow 1 checks: "Do I have money to trade?"

Example:
┌─────────────────────────────────────┐
│ Your USDC Balance Check             │
├─────────────────────────────────────┤
│ Current balance: $47.50             │
│ Balance cached for 30 seconds       │
│ → Can trade? YES                    │
└─────────────────────────────────────┘

If balance is $0 → Stop, come back later
If balance > $0 → Continue to Step 2


Step 2: Fetch Whale's Recent Trades
────────────────────────────────────
Looks at whale's position entries from the last 10 minutes.

Example Timeline:
┌────────────────────────────────────────────────┐
│ 14:00:00 - Whale buys 100 YES @ $0.45         │
│ 14:02:15 - Whale sells 50 YES @ $0.47         │
│ 14:05:30 - Whale buys 200 NO @ $0.30          │ ← Within 10 min window
│ 14:07:45 - Whale sells 100 NO @ $0.32         │ ← Look back from now
│ 14:09:50 - Whale buys 75 YES @ $0.55          │
│ 14:10:00 - NOW                                 │
│ 14:10:02 - (Only these 4 are visible)         │
└────────────────────────────────────────────────┘

All 4 trades within last 10 minutes are candidates.


Step 3: Analyze Trades for Opportunities
─────────────────────────────────────────
For each whale trade, check if it's worth copying.

Example Opportunity Analysis:

Whale Trade #1: Buys 100 YES @ $0.45
┌────────────────────────────────────┐
│ Market Question:                   │
│ "Will Trump win 2028 election?"   │
│ Whale's position: YES              │
│ Whale's entry price: $0.45         │
│ Whale's size: 100 shares           │
├────────────────────────────────────┤
│ Do we own this already? NO         │
│ Price within bounds? YES ✓         │
│   (0.45 is between 0.10-0.85)     │
│ Recent entry? YES ✓                │
│   (bought 3 seconds ago)          │
├────────────────────────────────────┤
│ VERDICT: BUY THIS! 💚             │
│ Action: Mirror the trade          │
└────────────────────────────────────┘

Whale Trade #2: Buys 200 NO @ $0.30
┌────────────────────────────────────┐
│ Market Question:                   │
│ "Will UK GDP grow >2% next year?" │
│ Whale's position: NO               │
│ Whale's entry price: $0.30         │
│ Whale's size: 200 shares           │
├────────────────────────────────────┤
│ Do we own this already? YES ✓      │
│   (we own 150 NO already)         │
│ → Skip! We're already in          │
│ VERDICT: SKIP 🚫                  │
└────────────────────────────────────┘

Whale Trade #3: Sells 100 NO @ $0.32
┌────────────────────────────────────┐
│ This is a SELL trade               │
│ → Skip during Flow 1               │
│ (Flow 2 handles selling)           │
│ VERDICT: SKIP 🚫                   │
└────────────────────────────────────┘


Step 4: Check Execution Criteria
────────────────────────────────
For each identified opportunity, validate it meets requirements.

For opportunity: "Buy 100 YES @ $0.45"
┌────────────────────────────────────┐
│ Order Validation                   │
├────────────────────────────────────┤
│ Price check: $0.45                 │
│   Min allowed: $0.10 ✓             │
│   Max allowed: $0.85 ✓             │
│ Size check: 100 shares             │
│   Order size: $1.00 worth of YES   │
│   (100 shares × $0.01 = $1.00)    │
│   Max allowed: $1.00 ✓             │
│ Entry price guard: YES ✓           │
│   (within safety thresholds)      │
│ Balance check: $47.50 available    │
│   Need: $1.00 ✓                    │
├────────────────────────────────────┤
│ RESULT: ALL CHECKS PASS ✓         │
│ → Execute trade                    │
└────────────────────────────────────┘


Step 5: Execute the Buy Order
─────────────────────────────
Place the order on Polymarket.

Your Order Placement:
┌────────────────────────────────────────┐
│ BUY ORDER EXECUTION                    │
├────────────────────────────────────────┤
│ Market: "Will Trump win 2028?"        │
│ Position: YES                          │
│ Entry Price: $0.45                     │
│ Order Amount: $1.00 USDC               │
│ Order Type: LIMIT (not market)        │
│ Price Buffer: 4.0%                     │
│   → Limit price: $0.468                │
│   → Our limit is 4% above whale's     │
│ Time to fill: Usually instant         │
├────────────────────────────────────────┤
│ STATUS: ✅ ORDER PLACED                │
│ Your new balance: $46.50               │
│   ($47.50 - $1.00 for this trade)     │
│ You now own: ~2.2 shares of YES       │
│   ($1.00 ÷ $0.45 = 2.22 shares)      │
└────────────────────────────────────────┘


Step 6: Update Cache and Log
────────────────────────────
Record the trade and invalidate balance cache.

┌────────────────────────────────────┐
│ Post-Trade Actions                 │
├────────────────────────────────────┤
│ Log: "Flow 1: Executed 1 trade"    │
│ Log detail:                        │
│   - Action: BUY                    │
│   - Market: Trump 2028             │
│   - Price: $0.45                   │
│   - Size: $1.00                    │
│ Invalidate balance cache           │
│   → Next check will fetch fresh   │
│ Latency: 5-10 seconds from         │
│   whale's trade to our trade       │
└────────────────────────────────────┘


COMPLETE FLOW 1 EXAMPLE - START TO FINISH:
───────────────────────────────────────────

Timeline (real numbers):

14:05:30.000
  └─ Whale buys 100 YES @ $0.45 on Polymarket
     (This is captured when whale broadcasts transaction)

14:05:32.000 (Flow 1 polling runs)
  ├─ Check balance: $47.50 ✓
  ├─ Fetch recent whale trades (last 10 min)
  ├─ Find whale's 14:05:30 trade
  ├─ Verify: not in own positions, price OK
  ├─ Place limit order for YES @ $0.468
  └─ Log: "Trade executed"

14:05:33.200
  └─ Your order fills
     You now own YES @ $0.45 (latency: ~3 seconds)

14:05:34.000 (Flow 1 polling runs again)
  ├─ Check balance: $46.50 (updated from $47.50)
  └─ Continue checking for more opportunities


============================================================================
PART 2: HOW SELLING WORKS (FLOW 2 - POSITION ALIGNMENT)
============================================================================

OVERVIEW:
─────────
Flow 2 detects when the whale EXITS positions and immediately sells your
matching positions. This is called "exit following" - don't hold positions
the whale has abandoned.

Configuration:
├─ MIRROR_POSITION_ALIGNMENT_INTERVAL_SEC: 60 seconds
├─ MIRROR_CLOSED_POSITIONS_LOOK_BACK_LIMIT: 10 positions
└─ MIRROR_SELL_PRICE_BUFFER_PERCENT: 2.0%

THE 4-STEP SELLING PROCESS:
────────────────────────────

Step 1: Get Your Current Positions
──────────────────────────────────
Check what you currently own.

Your Current Portfolio (14:06:30):
┌─────────────────────────────────────────────────┐
│ Position 1:                                     │
│ ├─ Market: "Will Trump win 2028?"              │
│ ├─ Position: YES                                │
│ ├─ Shares owned: 2.2                            │
│ ├─ Entry price: $0.45                           │
│ ├─ Current price: $0.47                         │
│ └─ Current value: $1.03                         │
│                                                 │
│ Position 2:                                     │
│ ├─ Market: "Will UK GDP grow >2%?"             │
│ ├─ Position: NO                                 │
│ ├─ Shares owned: 5.0                            │
│ ├─ Entry price: $0.18                           │
│ ├─ Current price: $0.21                         │
│ └─ Current value: $1.05                         │
│                                                 │
│ Position 3:                                     │
│ ├─ Market: "When will AI reach AGI?"           │
│ ├─ Position: YES                                │
│ ├─ Shares owned: 3.3                            │
│ ├─ Entry price: $0.29                           │
│ ├─ Current price: $0.32                         │
│ └─ Current value: $1.06                         │
└─────────────────────────────────────────────────┘


Step 2: Get Whale's Closed Positions
────────────────────────────────────
Check which positions the whale has EXITED recently.

Whale's Transaction History (Last 10 transactions):
┌──────────────────────────────────────────────────┐
│ Transaction 1 (14:02:15):                        │
│ └─ SOLD 50 YES in "Trump 2028" @ $0.47          │
│    (Whale is exiting this position)             │
│                                                  │
│ Transaction 2 (14:04:30):                        │
│ └─ SOLD 100 NO in "UK GDP" @ $0.22              │
│    (Whale is exiting this position)             │
│                                                  │
│ Transaction 3 (14:05:30):                        │
│ └─ BOUGHT 100 YES in "Trump 2028" @ $0.45      │
│    (Whale bought back! Still in this trade)    │
│                                                  │
│ Transactions 4-10:                               │
│ └─ Other whale activity...                      │
└──────────────────────────────────────────────────┘

Whale's last 10 closed positions detected:
├─ NO longer has YES in "Trump 2028" (EXITED)
├─ NO longer has NO in "UK GDP" (EXITED)
└─ Still has YES in "Trump 2028" (ACTIVE)


Step 3: Find Matching Positions to Sell
────────────────────────────────────────
Compare whale's current positions with yours.

Comparison Matrix:
┌──────────────────────────────────────────┐
│ Your Position │ Whale Owns? │ Action    │
├──────────────────────────────────────────┤
│ YES in       │ YES (just   │ KEEP      │
│ Trump 2028   │ re-bought)  │ ✓         │
│              │             │           │
│ NO in UK GDP │ NO (exited) │ SELL NOW! │
│              │             │ 💥        │
│              │             │           │
│ YES in AGI   │ ? Unknown   │ KEEP      │
│              │             │ (whale    │
│              │             │ status    │
│              │             │ unknown)  │
└──────────────────────────────────────────┘

SELL SIGNALS DETECTED:
┌────────────────────────────────────────┐
│ Action: SELL                           │
│ Market: "Will UK GDP grow >2%?"       │
│ Position: NO (sell the NO position)    │
│ Shares to sell: 5.0                    │
│ Current price: $0.21                   │
│ Current value: $1.05                   │
│ Profit: $0.87 (entry $0.18, now $0.21)│
│ Reason: Whale exited this market       │
└────────────────────────────────────────┘


Step 4: Execute the Sell Order
──────────────────────────────
Place the sell order on Polymarket.

Sell Order Details:
┌────────────────────────────────────────────┐
│ SELL ORDER EXECUTION                       │
├────────────────────────────────────────────┤
│ Market: "Will UK GDP grow >2%?"           │
│ Position: NO (what you own)                │
│ Current Price: $0.21                       │
│ Order Type: LIMIT (not market)            │
│ Price Buffer: 2.0%                         │
│   → Limit price: $0.2058                   │
│   → We sell 2% below current price        │
│   → Ensures it fills                      │
│ Amount: Sell all 5.0 shares               │
│ Proceeds: ~$1.03 USDC                      │
│ Total profit on this trade: ~$0.87        │
├────────────────────────────────────────────┤
│ STATUS: ✅ ORDER PLACED                    │
│ Your new balance: $47.53                   │
│   ($46.50 previous - $0 used + $1.03 sold)│
│ You now own: 0 shares of this position    │
│   (completely exited)                     │
└────────────────────────────────────────────┘


WHY SELL WHEN WHALE EXITS?
──────────────────────────
This is called "exit following" and has huge benefits:

Scenario 1: Without exit following (❌ BAD)
┌──────────────────────────────────────────────┐
│ 14:05:00 - Whale buys 100 YES @ $0.40       │
│ 14:05:05 - You copy, buy 100 YES @ $0.40   │
│ 14:06:00 - Whale sells all 100 @ $0.44      │
│ 14:06:30 - You still own 100 YES           │
│            (whale has moved on)             │
│ 14:08:00 - Market news negative             │
│ 14:08:30 - Price crashes to $0.15           │
│ 14:09:00 - You finally notice and sell @ $0.15
│ LOSS: $0.25 per share × 100 = -$25 loss!  │
│                                              │
│ Root cause: You held whale's "dead" trade  │
└──────────────────────────────────────────────┘

Scenario 2: With exit following (✅ GOOD)
┌──────────────────────────────────────────────┐
│ 14:05:00 - Whale buys 100 YES @ $0.40       │
│ 14:05:05 - You copy, buy 100 YES @ $0.40   │
│ 14:06:00 - Whale sells all 100 @ $0.44      │
│ 14:06:30 - Flow 2 detects whale exit       │
│ 14:06:35 - You immediately sell @ $0.43     │
│ PROFIT: $0.03 per share × 100 = +$3 profit!│
│                                              │
│ You followed whale out at peak price!       │
└──────────────────────────────────────────────┘

The difference: $3 profit vs $25 loss = $28 better result!


COMPLETE FLOW 2 EXAMPLE - START TO FINISH:
───────────────────────────────────────────

Timeline (real numbers):

14:05:00
  └─ Whale buys 100 YES @ $0.40

14:05:05
  └─ Flow 1 copies: You buy 100 YES @ $0.40

14:06:00
  └─ Whale sells 100 YES @ $0.44
     (Whale is done with this trade)

14:06:00 (Your position state)
  ├─ Market: Trump 2028
  ├─ Position: YES (what you own)
  ├─ Entry: $0.40
  ├─ Current: $0.44
  ├─ Unrealized profit: $4.00
  └─ Status: YOU STILL OWN IT

14:07:00 (Flow 2 runs - 60 second check)
  ├─ Check own positions: 100 YES @ $0.40
  ├─ Check whale's closed trades
  ├─ Find: Whale exited YES @ $0.44
  ├─ Match found: You own YES, whale doesn't
  ├─ Action: SELL NOW
  └─ Place limit order @ $0.432 (2% below)

14:07:01
  └─ Order fills: You sell at $0.432
     REALIZED PROFIT: $3.20

14:07:02
  ├─ You have: $0 of this position
  ├─ Cash back: +$43.20
  ├─ Whale status: EXITED
  └─ Your status: EXITED (following whale)


============================================================================
PART 3: REAL EXAMPLE SCENARIO
============================================================================

THE TRUMP 2028 PREDICTION MARKET
═════════════════════════════════

Starting State (14:00:00):
────────────────────────
Your wallet: $50.00 USDC
Whale address: 0x742d35Cc6634C0532925a3b844Bc9e7595f
Your address: 0x8Ba4dF08d8fDf3D0...

Market: "Will Trump win 2028 election?"
├─ YES Price: $0.40 (chances 40%)
├─ NO Price: $0.60 (chances 60%)
└─ Whale has: 500 YES shares (major supporter)


THE 30-MINUTE TRADING SEQUENCE:
═══════════════════════════════

14:05:00 - WHALE ENTERS
───────────────────────
Whale buys 100 YES @ $0.40

What happens:
┌─────────────────────────────────────┐
│ Whale Transaction:                  │
│ ├─ Buys: 100 shares of YES         │
│ ├─ Entry price: $0.40              │
│ ├─ Investment: $40.00              │
│ ├─ Timestamp: 14:05:00             │
│ └─ Status: BROADCAST TO CHAIN      │
└─────────────────────────────────────┘

Market impact:
├─ Whale buys 100 YES = bullish signal
├─ Price rises slightly: $0.40 → $0.41
├─ Other traders notice
└─ Liquidity: Good supply available


14:05:02 - FLOW 1 DETECTS (Running every 2 sec)
────────────────────────────────────────────────
Flow 1 polling cycle #2688:

┌─────────────────────────────────────┐
│ Flow 1 Cycle Execution:             │
│ 1. Check balance: $50.00 ✓          │
│ 2. Fetch whale trades (10 min):     │
│    └─ Found: Whale bought 100 YES  │
│ 3. Analyze opportunity:             │
│    ├─ Price: $0.41 (within bounds) │
│    ├─ Not in portfolio yet         │
│    ├─ Recent entry (2 sec ago)     │
│    └─ All checks PASS              │
│ 4. Execute order:                   │
│    ├─ Buy $1.00 of YES @ $0.41    │
│    ├─ Gets ~2.44 shares            │
│    └─ New balance: $49.00           │
└─────────────────────────────────────┘

Result:
├─ You now own: 2.44 YES @ $0.41
├─ Latency: 2 seconds behind whale
└─ Your portfolio value: $49.00 + $1.00 = $50.00

Price impact:
├─ Your buy adds demand
├─ Whale already bought + you buy = momentum
└─ Price: $0.41 → $0.42


14:15:00 - WHALE STILL HOLDING
───────────────────────────────
10 minutes pass. Whale hasn't exited.
What whale has done:
├─ Still holds original 100 YES
├─ Watching price: Now $0.52
├─ Unrealized gain: +$1,200
└─ Status: WAITING FOR MORE GAINS


Your portfolio after 10 minutes:
├─ YES position: 2.44 shares @ $0.41 entry
├─ Current price: $0.52
├─ Current value: $1.27
├─ Unrealized gain: $0.27
└─ Status: FOLLOWING WHALE'S LEAD


14:20:00 - WHALE EXITS (SELLS)
──────────────────────────────
Major news breaks: "Scandal emerges"
Whale immediately sells 100 YES @ $0.49

┌─────────────────────────────────────┐
│ Whale Transaction:                  │
│ ├─ Sells: 100 shares of YES        │
│ ├─ Exit price: $0.49               │
│ ├─ Proceeds: $49.00                │
│ ├─ Profit: $9.00 (from $40 investment)
│ ├─ Timestamp: 14:20:00             │
│ └─ Status: BROADCASTS TO CHAIN     │
└─────────────────────────────────────┘

Market impact:
├─ Whale exits: Major holder leaving
├─ Sells pressure: Prices drop
├─ Price: $0.52 → $0.48
├─ Other smart traders notice exit
└─ Signals: Maybe whale knows something?


14:21:00 - FLOW 2 DETECTS (Running every 60 sec)
──────────────────────────────────────────────────
Flow 2 alignment check (runs at 14:21:00, 14:22:00, etc.):

┌─────────────────────────────────────┐
│ Flow 2 Cycle Execution:             │
│ 1. Get own positions:               │
│    └─ Own 2.44 YES @ $0.41         │
│ 2. Get whale's recent exits:        │
│    └─ Whale exited YES @ $0.49     │
│ 3. Match check:                     │
│    ├─ Own: YES                     │
│    ├─ Whale owns: NOT ANYMORE      │
│    ├─ Match found: YES → SELL      │
│ 4. Execute sell order:              │
│    ├─ Sell 2.44 YES @ $0.47        │
│    │  (2% below current $0.48)     │
│    ├─ Proceeds: $1.15               │
│    └─ Balance now: $50.15           │
└─────────────────────────────────────┘

Result:
┌─────────────────────────────────────┐
│ TRADE SUMMARY:                      │
│ Entry (14:05:02): Bought $1.00 of   │
│                   YES @ $0.41       │
│ Exit (14:21:00): Sold 2.44 YES @    │
│                   $0.47             │
├─────────────────────────────────────┤
│ Entry cost: $1.00                   │
│ Exit proceeds: $1.15                │
│ Profit: $0.15 (15% return)          │
│ Time held: 16 minutes               │
│ Return rate: 57% per hour!          │
└─────────────────────────────────────┘

Price history:
├─ 14:05:00: Whale buys @ $0.40
├─ 14:05:02: You buy @ $0.41
├─ 14:15:00: Price reaches $0.52 (peak)
├─ 14:20:00: Whale exits @ $0.49
├─ 14:21:00: You exit @ $0.47 (following)
└─ 14:25:00: Price drops to $0.30 (crash!)

Without exit following:
├─ You would still own at 14:25:00
├─ Price would be $0.30 (from $0.47)
├─ Loss: -$0.41 per share
├─ Total loss: -$1.00 (100% loss on trade)
└─ Instead of +$0.15 profit → -$1.00 loss

With exit following (your result):
├─ Profit locked in: $0.15
├─ Protected from crash
├─ Market moved against you but you exited early
└─ Smart trading! 🎯


14:25:00 ONWARDS - NEXT OPPORTUNITY
────────────────────────────────────
Balance: $50.15
Flow 1 and Flow 2 continue running...

Looking for next whale trades to mirror!


============================================================================
PART 4: KEY INSIGHTS & MECHANICS
============================================================================

HOW THE MARGIN WORKS:
──────────────────────

Buy Side (Flow 1):
┌──────────────────────────────────────┐
│ Whale buys at: $0.45                │
│ + Time delay: 2-5 seconds           │
│ You buy at: $0.468 (4% markup)      │
│ ┌────────────────────────────────────┤
│ │ Why the 4% markup?                 │
│ │ • Price moved in whale's favor     │
│ │ • Network latency = price slippage │
│ │ • Ensure we get filled            │
│ │ • Small cost for guaranteed entry  │
│ └────────────────────────────────────┘
└──────────────────────────────────────┘

Sell Side (Flow 2):
┌──────────────────────────────────────┐
│ Price at exit: $0.47                │
│ You sell at: $0.461 (2% discount)   │
│ ┌────────────────────────────────────┤
│ │ Why the 2% discount?               │
│ │ • Ensure order fills quickly       │
│ │ • We're exiting whale's position   │
│ │ • Small markdown for guarantee     │
│ └────────────────────────────────────┘
└──────────────────────────────────────┘

Net Margin:
├─ Entry: +4% (we pay slightly more)
├─ Exit: -2% (we get slightly less)
├─ Net spread: -6% on the way out
├─ BUT: We get out when whale does!
└─ Better than crashing with the trade


RISK MANAGEMENT BUILT IN:
─────────────────────────

Price Guards:
├─ MIN_BUY_PRICE: $0.10
│  └─ Don't buy positions < 10% probability
├─ MAX_BUY_PRICE: $0.85
│  └─ Don't buy positions > 85% probability
└─ These prevent "garbage" trades

Order Size Guards:
├─ MIRROR_MAX_ORDER_SIZE_USD: $1.00
│  └─ Conservative position size
├─ Never risks more than ~2% of balance per trade
│  ($1 on $50 = 2%)
└─ Limits catastrophic losses

Balance Cache:
├─ MIRROR_BALANCE_CACHE_SECONDS: 30
│  └─ Don't hammer balance API
├─ Stale balance possible but rare
└─ Trade-off: Speed vs perfect accuracy

Entry Price Guard:
├─ ENTRY_PRICE_GUARD: Set in constants
│  └─ Max allowed difference from whale price
├─ Prevents buying 30 seconds after whale
└─ Only fresh whale trades


FREQUENCY LOGIC:
────────────────

Flow 1: Every 2 seconds
├─ Reason: Catch entries fast (beat other copiers)
├─ First to copy whale = best price
├─ Later copiers get worse prices
└─ High frequency = competitive advantage

Flow 2: Every 60 seconds
├─ Reason: Whale exits are rare
├─ No need to check every second
├─ 1-minute lag acceptable for exits
├─ Saves API calls
└─ Lower frequency = less expensive

Flow 3: Every 60 seconds
├─ Reason: Markets resolve even less frequently
├─ Redemptions are passive events
├─ No urgency on closed positions
└─ Optimal: check when needed


============================================================================
PART 5: WHAT CAN GO WRONG & HOW IT'S HANDLED
============================================================================

Issue: Balance Check Fails (Network Error)
──────────────────────────────────────────
┌──────────────────────────────────────┐
│ Flow 1 Cycle 2688:                   │
│ └─ Check balance: NETWORK ERROR!     │
│    • API timeout                     │
│    • Polymarket service down         │
│    • Network connectivity issue      │
├──────────────────────────────────────┤
│ Handling:                            │
│ ├─ Uses cached balance from 30s ago  │
│ ├─ Proceeds with caution             │
│ ├─ Logs the error                    │
│ ├─ Next cycle tries again            │
│ └─ If persists, trading stops safely │
└──────────────────────────────────────┘


Issue: Order Doesn't Fill
──────────────────────────
┌──────────────────────────────────────┐
│ Flow 1 places order @ $0.468         │
│ 5 seconds pass...                    │
│ Order NOT filled (market moved)      │
├──────────────────────────────────────┤
│ What happens:                        │
│ ├─ Order remains open on Polymarket  │
│ ├─ Next Flow 1 cycle notices         │
│ ├─ Detects: Already in this trade    │
│ ├─ Skips: Don't buy again            │
│ └─ Result: Position accumulates      │
│            slowly (maybe 1-2 cycles) │
└──────────────────────────────────────┘


Issue: Whale Exits Your Position Doesn't Exist
───────────────────────────────────────────────
┌──────────────────────────────────────┐
│ Flow 2 check:                        │
│ ├─ Whale exited "Trump 2028" YES    │
│ ├─ We check own positions            │
│ └─ We don't own YES in that market   │
│    (maybe never bought it)           │
├──────────────────────────────────────┤
│ Handling:                            │
│ ├─ Log: "Whale position not found"  │
│ ├─ Skip: No position to sell         │
│ └─ Continue: Check next position     │
└──────────────────────────────────────┘


Issue: Multiple Copies of Same Trade
──────────────────────────────────────
┌──────────────────────────────────────┐
│ Whale buys 100 YES @ $0.40           │
│                                      │
│ 14:05:02 - Flow 1 cycle 1:           │
│ └─ Sees trade, buys $1 of YES       │
│                                      │
│ 14:05:04 - Flow 1 cycle 2:           │
│ └─ Sees SAME trade again?            │
│    • API returned same entry         │
│    • Duplicate detected              │
│    • Skips: Already in position      │
│                                      │
│ Result: No double-buying             │
└──────────────────────────────────────┘


============================================================================
PART 6: PROFIT CALCULATION EXAMPLES
============================================================================

Example 1: Simple Win
─────────────────────

Trade 1: "Will Elon step down as Twitter CEO by 2026?"
├─ Whale buys 200 YES @ $0.25
├─ You buy $2.00 of YES @ $0.258 (4% markup)
│  └─ Get 7.75 shares
├─ Price rises: $0.25 → $0.70 (Elon steps down!)
├─ Flow 2 detects whale exit @ $0.69
├─ You sell 7.75 YES @ $0.677 (2% markdown)
│  └─ Get $5.25 proceeds

┌────────────────────────────────────┐
│ P&L Calculation:                   │
├────────────────────────────────────┤
│ Entry cost: $2.00                  │
│ Exit proceeds: $5.25               │
│ Gross profit: $3.25                │
│ Return: +162.5%                    │
│ Annualized: Huge!                  │
│ Time held: 45 minutes              │
└────────────────────────────────────┘


Example 2: Underwater Exit (Still Wins)
──────────────────────────────────────

Trade 2: "Will SPY close above 500 by EOY 2025?"
├─ Whale buys 150 YES @ $0.65
├─ You buy $1.50 of YES @ $0.676 (4% markup)
│  └─ Get 2.22 shares
├─ Price drops: $0.65 → $0.30 (SPY bearish)
├─ Whale immediately exits @ $0.30
├─ Flow 2 detects exit
├─ You sell 2.22 YES @ $0.294 (2% markdown)
│  └─ Get $0.65 proceeds

┌────────────────────────────────────┐
│ P&L Calculation:                   │
├────────────────────────────────────┤
│ Entry cost: $1.50                  │
│ Exit proceeds: $0.65               │
│ Loss: -$0.85                       │
│ Loss rate: -56.7%                  │
│ BUT: Still better than whale!      │
│      Whale also lost, BUT we       │
│      exited BEFORE further drops   │
│      Whale: -53% ($0.65→$0.30)    │
│      You: -57% (cost basis higher) │
│      BUT you exited at whale's     │
│      decision, not your pain       │
└────────────────────────────────────┘

Why even losing trades can be valuable:
├─ You lose less than whale (better cost basis)
├─ You follow whale out before cascade crashes
├─ Cut losses early instead of holding
├─ Stay liquid for next whale opportunity
└─ Psychology: Losses hurt less when following


Example 3: The Perfect Day
──────────────────────────

Assume: Whale is 3x better than random traders

Trade 1: Win $2.00 (162% return, 45 min)
Trade 2: Loss -$0.85 (as above, 30 min)
Trade 3: Win $1.50 (115% return, 60 min)
Trade 4: Win $0.75 (50% return, 90 min)
Trade 5: Break even $0.00 (whale took loss)

Day Summary:
├─ Starting balance: $50.00
├─ Trades: 5 total
├─ Gross profit: $2.00 + -$0.85 + $1.50 + $0.75 + $0.00 = +$3.40
├─ Ending balance: $53.40
├─ Daily return: 6.8%
├─ Time spent: 4 hours
├─ No hands-on trading!

Annualized (if sustainable):
├─ 6.8% daily = 2,482% annually!
├─ Obviously not sustainable at 6.8%
├─ More realistic: 0.5-2% daily = 182%-730% annually
├─ Still phenomenal vs 8-10% stock market returns

Real expected returns:
├─ Very good whale selection: 1-2% daily
├─ Good whale: 0.5-1% daily
├─ Average whale: 0.1-0.5% daily
├─ Bad whale: Can lose money
└─ Key: Pick the whale carefully!


============================================================================
SUMMARY
============================================================================

BUYING (Flow 1):
───────────────
1. Every 2 seconds, check whale's recent trades (last 10 minutes)
2. For each whale trade: Check if it's worth copying
3. Validate: Price in range, not already owned, fresh entry
4. Buy fixed $1 orders with 4% safety margin
5. Latency: 5-10 seconds behind whale (acceptable)
6. Net cost: ~4% above whale's entry (insurance for speed)

SELLING (Flow 2):
─────────────────
1. Every 60 seconds, check if whale has exited positions
2. Find positions you own that whale no longer has
3. Immediately sell at 2% discount to ensure fill
4. Exit following: Don't hold positions whale abandoned
5. Key insight: Better to exit early than crash later
6. Benefit: Huge protection from drawdowns

RESULT:
───────
You're not betting on your analysis.
You're betting on the whale's skill and judgment.
The whale is almost certainly better than you at trading.
So copy them and profit from their edge.

It's that simple! 🚀
"""
