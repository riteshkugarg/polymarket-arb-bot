"""
ARBITRAGE SERVICE: INTEGRATION & USAGE GUIDE

This document explains how to integrate the ArbScanner and AtomicExecutor
into your Polymarket trading bot.

═══════════════════════════════════════════════════════════════════════════════
QUICK START
═══════════════════════════════════════════════════════════════════════════════

1. IMPORT THE STRATEGY

    from strategies.arbitrage_strategy import ArbitrageStrategy
    from strategies.arb_scanner import ArbScanner, AtomicExecutor

2. INITIALIZE IN YOUR BOT

    client = PolymarketClient()
    await client.initialize()
    
    order_manager = OrderManager(client)
    arb_strategy = ArbitrageStrategy(client, order_manager)

3. RUN THE STRATEGY

    # Run in background alongside other strategies
    arb_task = asyncio.create_task(arb_strategy.run())
    
    # ... your other code ...
    
    # Stop when done
    await arb_strategy.stop()

═══════════════════════════════════════════════════════════════════════════════
CORE CLASSES
═══════════════════════════════════════════════════════════════════════════════

1. ArbScanner
   ────────────
   Detects multi-outcome arbitrage opportunities
   
   Responsibility:
   - Scan markets for sum(prices) < 0.98 opportunities
   - Validate order book depth (min 10 shares per outcome)
   - Calculate profit potential accounting for 1.5% taker fee
   - Handle NegRisk (inverse) market normalization
   
   Key Methods:
   - scan_markets(market_ids=None, limit=100)
     Returns: List[ArbitrageOpportunity], sorted by profit/budget ratio
   
   - _check_market_for_arbitrage(market)
     Returns: Optional[ArbitrageOpportunity]
   
   - _is_negrisk_market(market)
     Returns: bool (True if inverse market)

2. AtomicExecutor
   ───────────────
   Executes arbitrage with atomic (all-or-nothing) semantics
   
   Responsibility:
   - Place BUY orders for all outcomes (FOK)
   - Validate slippage (max $0.005 per leg)
   - Manage budget ($100 total cap)
   - Abort and cancel if any leg fails
   
   Key Methods:
   - execute(opportunity, shares_to_buy)
     Returns: ExecutionResult
   
   - get_budget_status()
     Returns: Dict[str, float] with budget metrics
   
   - reset_budget()
     Resets budget tracking for new trading day

3. ArbitrageStrategy
   ──────────────────
   Orchestrates scanner and executor in a continuous loop
   
   Responsibility:
   - Run scanning loop every 3 seconds
   - Filter opportunities by profitability
   - Execute atomically with cooldown
   - Track metrics and budget
   - Circuit breaker on failures
   
   Key Methods:
   - run()
     Main strategy loop (async)
   
   - stop()
     Stop the strategy loop
   
   - get_strategy_status()
     Returns: Dict[str, Any] with all metrics

═══════════════════════════════════════════════════════════════════════════════
DATA STRUCTURES
═══════════════════════════════════════════════════════════════════════════════

OutcomePrice
─────────────
@dataclass
class OutcomePrice:
    outcome_index: int              # Position in outcomes list (0, 1, 2, ...)
    outcome_name: str               # Human-readable outcome (e.g., "Alice")
    token_id: str                   # CLOB token ID (hex string)
    yes_price: float                # Probability-weighted price (0.0-1.0)
    bid_price: float                # Best bid from order book
    ask_price: float                # Best ask from order book
    available_depth: float          # Shares available at ask price


ArbitrageOpportunity
─────────────────────
@dataclass
class ArbitrageOpportunity:
    market_id: str                  # Polymarket market ID
    condition_id: str               # Conditional market ID
    market_type: MarketType         # BINARY, MULTI_CHOICE, or NEGRISK
    outcomes: List[OutcomePrice]    # Prices for all outcomes
    sum_prices: float               # Sum of YES prices (< 0.98 = arbitrage)
    profit_per_share: float         # 1.0 - sum_prices
    net_profit_per_share: float     # After 1.5% fee × num_outcomes
    required_budget: float          # Cost per share to execute
    max_shares_to_buy: float        # Limited by order book depth
    is_negrisk: bool                # Is inverse market?
    negrisk_short_field_cost: float # For inverse: cost to hedge all outcomes


ExecutionResult
────────────────
@dataclass
class ExecutionResult:
    success: bool                   # Did execution succeed?
    market_id: str                  # Which market
    orders_executed: List[str]      # Order IDs that filled
    orders_failed: List[str]        # Order IDs that failed
    total_cost: float               # Total USDC spent
    shares_filled: float            # Shares obtained per outcome
    actual_profit: float            # Realized profit (or loss)
    error_message: Optional[str]    # Error details if failed

═══════════════════════════════════════════════════════════════════════════════
MATHEMATICAL FORMULAS
═══════════════════════════════════════════════════════════════════════════════

Basic Arbitrage
────────────────
For a 3-outcome market (Alice, Bob, Charlie) with prices:

    sum_prices = price_alice + price_bob + price_charlie
    
If sum_prices < 0.98:
    
    profit_per_share = 1.0 - sum_prices  (at market settlement)
    
    # Account for 1.5% taker fee per trade × number of outcomes
    fee_total = sum_prices × (0.015 × 3 outcomes)
    net_profit_per_share = profit_per_share - fee_total
    
    # Cost to execute
    cost_per_share = sum_prices
    
    # If buying 10 shares
    gross_cost = 10 × sum_prices
    gross_profit = 10 × profit_per_share
    net_profit = 10 × net_profit_per_share


NegRisk (Inverse) Markets
──────────────────────────
In NegRisk, outcomes are INVERTED (short the field):

    sum_prices = original_sum (may be > 0.98)
    
    short_field_cost = 1.0 - sum_prices  (cost to hedge all outcomes)
    
    normalized_entry = min(sum_prices, short_field_cost)
    
    # Now check if normalized_entry < 0.98
    if normalized_entry < 0.98:
        profit = 1.0 - normalized_entry


Slippage Calculation
─────────────────────
    
    mid_price = (bid_price + ask_price) / 2
    slippage_per_share = ask_price - mid_price
    
    # Constraint: slippage < $0.005 per leg
    if slippage_per_share > 0.005:
        REJECT EXECUTION

═══════════════════════════════════════════════════════════════════════════════
EXECUTION FLOW (DETAILED)
═══════════════════════════════════════════════════════════════════════════════

Scanning Phase (ArbScanner.scan_markets)
──────────────────────────────────────────

1. Fetch active markets
   GET /markets?limit=100
   
2. For each market:
   a. Extract outcomes and token IDs
   b. For each token ID:
      - GET /book/{token_id} → Order book
      - Extract best bid/ask and depth
   c. Calculate sum(mid_prices)
   
3. Check arbitrage condition:
   if sum_prices < 0.98:
       if available_depth >= 10 shares:
           if net_profit > $0.001:
               yield ArbitrageOpportunity
               
4. Sort by profit/budget ratio (ROI)


Execution Phase (AtomicExecutor.execute)
──────────────────────────────────────────

1. Validate prerequisites:
   a. Check: budget_used + required_cost <= $100
   b. Check: account_balance >= required_cost
   c. Check: order_book_depth >= shares_to_buy (all outcomes)

2. Place BUY orders (FOK - Fill or Kill):
   For each outcome:
       POST /order with:
           side='BUY'
           token_id={token}
           price={ask_price}
           size={shares_to_buy}
           order_type='FOK'  ← All-or-nothing
           
3. Monitor execution:
   a. If all orders fill:
       return ExecutionResult(success=True)
   
   b. If any order fails:
       for pending_order in pending_orders:
           DELETE /order/{order_id}  ← CANCEL all pending
       return ExecutionResult(success=False)

4. Update state:
   budget_used += total_cost
   log profit and metrics


Abort Scenarios
────────────────
Entire execution is aborted if:

1. Order 1 fills, Order 2 fills, Order 3 FAILS
   → Cancel Orders 1 and 2 immediately
   → User is left with no positions (atomic failure)
   
2. Insufficient balance detected pre-execution
   → Reject without placing any orders
   
3. Slippage exceeds $0.005 on any leg
   → Reject before placing orders
   
4. Order book depth insufficient
   → Reject before placing orders

═══════════════════════════════════════════════════════════════════════════════
INTEGRATION WITH EXISTING BOT
═══════════════════════════════════════════════════════════════════════════════

Running Alongside Mirror Strategy
──────────────────────────────────

    # main.py
    
    async def main():
        # Initialize client and managers
        client = PolymarketClient()
        await client.initialize()
        
        order_manager = OrderManager(client)
        
        # Initialize both strategies
        mirror_strategy = MirrorStrategy(client, order_manager)
        arb_strategy = ArbitrageStrategy(client, order_manager)
        
        # Run independently
        mirror_task = asyncio.create_task(mirror_strategy.run())
        arb_task = asyncio.create_task(arb_strategy.run())
        
        try:
            # Keep both running
            await asyncio.gather(mirror_task, arb_task)
        finally:
            # Cleanup
            await mirror_strategy.stop()
            await arb_strategy.stop()


Monitoring Status
─────────────────

    # Get comprehensive status
    status = arb_strategy.get_strategy_status()
    
    print(f"Running: {status['is_running']}")
    print(f"Executions: {status['successful_executions']}/{status['total_executions']}")
    print(f"Profit: ${status['total_profit']:.2f}")
    print(f"Budget: ${status['budget_used']:.2f} / ${status['budget_total']:.2f}")
    print(f"Circuit breaker: {status['circuit_breaker_active']}")

═══════════════════════════════════════════════════════════════════════════════
CONFIGURATION & TUNING
═══════════════════════════════════════════════════════════════════════════════

Key Constants (in arb_scanner.py)
───────────────────────────────────

TAKER_FEE_PERCENT = 0.015              # 1.5% fee per trade
ARBITRAGE_OPPORTUNITY_THRESHOLD = 0.98 # sum < 0.98
MAX_SLIPPAGE_PER_LEG = 0.005           # $0.005 max
MIN_ORDER_BOOK_DEPTH = 10              # 10+ shares minimum
MAX_ARBITRAGE_BUDGET_PER_BASKET = 10.0 # Max $10 per arb
MIN_ARBITRAGE_BUDGET_PER_BASKET = 5.0  # Min $5 per arb
TOTAL_ARBITRAGE_BUDGET = 100.0         # Total $100 cap
MINIMUM_PROFIT_THRESHOLD = 0.001       # Don't execute if < $0.001


Key Constants (in arbitrage_strategy.py)
─────────────────────────────────────────

ARB_SCAN_INTERVAL_SEC = 3              # Scan every 3 seconds
ARB_EXECUTION_COOLDOWN_SEC = 5         # Min 5s between executions
ARB_MAX_CONSECUTIVE_FAILURES = 3       # Circuit breaker threshold
ARB_OPPORTUNITY_REFRESH_LIMIT = 50     # Scan up to 50 markets


Tuning Recommendations
───────────────────────

1. Decrease MIN_PROFIT_THRESHOLD for more executions
   - Default: $0.001
   - Conservative: $0.005
   - Aggressive: $0.0001

2. Increase TOTAL_ARBITRAGE_BUDGET for more capital
   - Default: $100
   - Note: Increases position risk

3. Decrease ARB_SCAN_INTERVAL_SEC for faster detection
   - Default: 3 seconds
   - Faster: 1-2 seconds
   - Risk: Higher API load

4. Increase MAX_SLIPPAGE_PER_LEG for more flexibility
   - Default: $0.005
   - Conservative: $0.003
   - Aggressive: $0.01

═══════════════════════════════════════════════════════════════════════════════
ERROR HANDLING & LOGGING
═══════════════════════════════════════════════════════════════════════════════

All errors are caught and logged with context:

    # Log format
    [timestamp] ERROR ArbitrageStrategy: Order execution failed: ...
    [timestamp] WARNING ❌ Arbitrage execution failed: ...
    [timestamp] INFO ✅ Arbitrage executed: Market ... Cost: $X, Profit: $Y

Log Levels:
───────────
- DEBUG: Detailed operation flow (scan iterations, validation steps)
- INFO: High-level events (execution success, strategy start/stop)
- WARNING: Failures that don't stop the strategy (execution failure)
- ERROR: Failures that affect strategy health (circuit breaker active)


Debugging Failed Execution
──────────────────────────

Check logs for:
1. Insufficient balance → Increase account balance
2. Slippage exceeded → Increase MAX_SLIPPAGE_PER_LEG
3. Order book depth → Increase MIN_ORDER_BOOK_DEPTH for filtering
4. Circuit breaker active → Check for systematic API/order issues

═══════════════════════════════════════════════════════════════════════════════
PRODUCTION DEPLOYMENT
═══════════════════════════════════════════════════════════════════════════════

Checklist before deploying to production:

☑ Test with mock data (run unit tests)
☑ Test with real API (testnet or small budget)
☑ Monitor circuit breaker activation rate
☑ Verify budget tracking accuracy
☑ Check order book depth at different times
☑ Validate NegRisk detection on sample markets
☑ Confirm FOK logic works (cancel pending on failure)
☑ Check slippage constraints in real conditions
☑ Verify profit calculations match actual fills
☑ Monitor API rate limit usage
☑ Set up alerting for strategy failures
☑ Log all executions for audit trail

═══════════════════════════════════════════════════════════════════════════════
EXAMPLE: FULL INTEGRATION
═══════════════════════════════════════════════════════════════════════════════

    # bot.py
    import asyncio
    from core.polymarket_client import PolymarketClient
    from core.order_manager import OrderManager
    from strategies.mirror_strategy import MirrorStrategy
    from strategies.arbitrage_strategy import ArbitrageStrategy
    from utils.logger import get_logger
    
    logger = get_logger(__name__)
    
    async def main():
        # Initialize client
        client = PolymarketClient()
        await client.initialize()
        logger.info("Polymarket client initialized")
        
        # Initialize order manager
        order_manager = OrderManager(client)
        logger.info("Order manager initialized")
        
        # Initialize strategies
        mirror = MirrorStrategy(client, order_manager)
        arbitrage = ArbitrageStrategy(client, order_manager)
        
        # Validate configurations
        await mirror.validate_configuration()
        await arbitrage.validate_configuration()
        logger.info("Strategy configurations validated")
        
        # Run both strategies concurrently
        mirror_task = asyncio.create_task(mirror.run())
        arb_task = asyncio.create_task(arbitrage.run())
        
        try:
            # Run for 24 hours
            await asyncio.sleep(86400)
        except KeyboardInterrupt:
            logger.info("Shutdown signal received")
        finally:
            # Graceful shutdown
            await mirror.stop()
            await arbitrage.stop()
            
            await asyncio.gather(mirror_task, arb_task, return_exceptions=True)
            logger.info("All strategies stopped")
            
            # Final status
            arb_status = arbitrage.get_strategy_status()
            logger.info(f"Final arbitrage stats:")
            logger.info(f"  Executions: {arb_status['successful_executions']}/{arb_status['total_executions']}")
            logger.info(f"  Profit: ${arb_status['total_profit']:.2f}")
            logger.info(f"  Budget used: ${arb_status['budget_used']:.2f}")
    
    if __name__ == '__main__':
        asyncio.run(main())

═══════════════════════════════════════════════════════════════════════════════
"""

# This is a documentation file - no executable code
pass
