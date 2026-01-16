"""
Arbitrage Scanner & Atomic Executor

Implements mathematical combinatorial arbitrage detection and execution.

Core Concepts:
──────────────
ARBITRAGE OPPORTUNITY: Market inefficiency where sum of YES prices across
all outcomes < 0.98 (accounting for 1.5% taker fee buffer), allowing
risk-free profit by buying all outcomes.

ATOMIC EXECUTION: All-or-nothing execution model. Every leg of the arbitrage
must fill (FOK) without exceeding $0.005 slippage per outcome, or the entire
sequence is aborted to prevent being "legged in" (holding losing positions).

NEGRISK HANDLING: Markets where the conditional outcome is the ABSENCE of
an event (e.g., "Will candidate X lose?"). Normalized by computing the
"short the field" cost (buy NO on all outcomes) which equals 1.0 - sum(YES).

BUDGET MANAGEMENT: Maximize $100 total budget. Each arbitrage basket costs
$5-$10 to execute (number of outcomes × per-outcome cost).

MATHEMATICAL FORMULA:
═════════════════════

For multi-outcome market (e.g., 3 outcomes):
  sum_yes = price_outcome1 + price_outcome2 + price_outcome3
  
  If sum_yes < 0.98:
    arbitrage_profit_per_share = 1.0 - sum_yes (at maturity)
    
  Cost per share = sum_yes
  Shares_to_buy = min(budget_remaining / sum_yes, order_book_depth)
  
  Gross Profit = Shares * (1.0 - sum_yes)
  Net Profit = Gross_Profit - Taker_Fee (1.5% per trade × num_outcomes)

For NegRisk markets (inverse markets):
  short_field_cost = 1.0 - sum_yes  (cost to hedge all outcomes)
  normalized_entry = min(sum_yes, short_field_cost)

EXECUTION CONSTRAINTS:
══════════════════════
1. FOK (Fill-or-Kill): Each order must fill completely or be rejected
2. Slippage: No individual leg can exceed $0.005 slippage
3. Depth: Order book must support 10+ shares at target price
4. Balance: Must have sufficient USDC for entire basket upfront
5. Abort: If any leg fails, cancel all pending legs immediately
"""

from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from decimal import Decimal
import asyncio
import time
from enum import Enum

from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from core.market_data_manager import MarketDataManager, MarketSnapshot
from config.constants import (
    ARBITRAGE_STRATEGY_CAPITAL,
    ARBITRAGE_TAKER_FEE_PERCENT,
    DATA_STALENESS_THRESHOLD,  # INSTITUTIONAL: Use global staleness constant
)
from utils.logger import get_logger
from utils.exceptions import (
    OrderRejectionError,
    SlippageExceededError,
    InsufficientBalanceError,
    TradingError,
)


logger = get_logger(__name__)


# Constants for arbitrage logic
# INSTITUTIONAL UPGRADE: 1.0% actual fee (not 1.2% conservative buffer)
# Previous: 1.2% (too conservative - missed profitable opportunities)
# Rationale: Use actual fee tier for accurate profit calculations
TAKER_FEE_PERCENT = ARBITRAGE_TAKER_FEE_PERCENT  # Import from constants for flexibility
ARBITRAGE_OPPORTUNITY_THRESHOLD = 0.992  # UPGRADED: sum(prices) < 0.992 (~0.8% inefficiency)
TAKER_FEE_BUFFER = TAKER_FEE_PERCENT  # Account for fee in opportunity detection
FINAL_THRESHOLD = ARBITRAGE_OPPORTUNITY_THRESHOLD  # sum < 0.992 after fee buffer

# SMART SLIPPAGE: Dynamic based on order book depth (replaces flat $0.005)
# Thin books (< 20 shares) = tight slippage to avoid impact
# Medium books (20-100 shares) = moderate slippage
# Deep books (> 100 shares) = looser slippage
# INSTITUTIONAL UPGRADE: Increased loose slippage from 0.010 to 0.015
# Rationale: Allows fills in fast-moving arbitrage baskets with deep liquidity
SLIPPAGE_TIGHT = 0.002  # $0.002 for thin books
SLIPPAGE_MODERATE = 0.005  # $0.005 for medium books (legacy default)
SLIPPAGE_LOOSE = 0.015  # $0.015 for deep books (UPGRADED from 0.010)
DEPTH_THRESHOLD_THIN = 20  # shares
DEPTH_THRESHOLD_MEDIUM = 100  # shares

# Depth validation
# Per Polymarket Support (Jan 2026): Lower to 5 shares for small capital markets
# Previous: 10 shares (too strict)
MIN_ORDER_BOOK_DEPTH = 5  # Require depth for at least 5 shares
MAX_ARBITRAGE_BUDGET_PER_BASKET = 10.0  # Max $10 per arbitrage basket
MIN_ARBITRAGE_BUDGET_PER_BASKET = 5.0  # Min $5 per arbitrage basket
# CRITICAL FIX: Use budget from constants.py (not hardcoded)
# Was: 100.0 (hardcoded) → Now: ARBITRAGE_STRATEGY_CAPITAL (from constants)
# This ensures scanner budget matches allocated capital
TOTAL_ARBITRAGE_BUDGET = ARBITRAGE_STRATEGY_CAPITAL  # Total budget from constants.py
MINIMUM_PROFIT_THRESHOLD = 0.001  # Don't execute if profit < $0.001

# INSTITUTIONAL UPGRADE (Phase 1): Microstructure Quality & Liquidity Standards
# Per institutional audit - arbitrage needs per-leg quality validation
MIN_ARB_LEG_LIQUIDITY_USD = 2.0  # Minimum $2 dollar liquidity per leg
MAX_ARB_LEG_SPREAD_PERCENT = 0.10  # Maximum 10% spread per leg (looser than MM's 3%)
MIN_ARB_LEG_BID = 0.02  # Reject extreme long-shot bids (same as MM)
MAX_ARB_LEG_ASK = 0.98  # Reject extreme favorites (same as MM)


class MarketType(Enum):
    """Classification of market types"""
    BINARY = "binary"  # 2 outcomes (YES/NO)
    MULTI_CHOICE = "multi_choice"  # 3+ outcomes
    NEGRISK = "negrisk"  # Inverse market (negation of primary event)


@dataclass
class OutcomePrice:
    """Represents a single outcome in a market"""
    outcome_index: int
    outcome_name: str
    token_id: str
    yes_price: float  # Probability-weighted price (0.0-1.0)
    bid_price: float  # Best bid price from order book
    ask_price: float  # Best ask price from order book
    available_depth: float  # How many shares available at ask price


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity"""
    market_id: str
    condition_id: str
    market_type: MarketType
    outcomes: List[OutcomePrice]
    sum_prices: float  # Sum of YES prices across outcomes
    profit_per_share: float  # 1.0 - sum_prices (before fees)
    net_profit_per_share: float  # After accounting for dynamic fees
    required_budget: float  # Cost per share × desired shares
    max_shares_to_buy: float  # Limited by order book depth
    is_negrisk: bool = False
    negrisk_short_field_cost: Optional[float] = None
    # Dynamic fee rates (2026 update)
    fee_rates_bps: Optional[Dict[str, int]] = None  # token_id -> fee in basis points
    total_fee_amount: float = 0.0  # Total fee in dollars
    
    # ═══════════════════════════════════════════════════════════════════════════════
    # P2 FIX: LATENCY BUDGET TRACKING (2026 Production)
    # ═══════════════════════════════════════════════════════════════════════════════
    # Track opportunity age to discard stale opportunities before execution
    # In HFT environment, 500ms+ latency makes opportunities unprofitable
    # This prevents executing on stale prices and taking adverse fills
    # ═══════════════════════════════════════════════════════════════════════════════
    discovery_timestamp: float = 0.0  # Will be set on creation
    max_age_ms: float = 500.0  # 500ms latency budget
    
    def __post_init__(self):
        """Set discovery timestamp after initialization"""
        if self.discovery_timestamp == 0.0:
            self.discovery_timestamp = time.time()
    
    def get_age_ms(self) -> float:
        """Get opportunity age in milliseconds"""
        return (time.time() - self.discovery_timestamp) * 1000
    
    def is_stale(self) -> bool:
        """P2 FIX: Check if opportunity has exceeded latency budget
        
        In HFT environment, opportunities stale quickly due to:
        - Other traders taking liquidity
        - Market prices moving
        - Book depth changes
        
        Returns:
            True if opportunity age exceeds max_age_ms threshold
        """
        return self.get_age_ms() > self.max_age_ms


@dataclass
class ExecutionResult:
    """Result of atomic execution attempt"""
    success: bool
    market_id: str
    orders_executed: List[str]  # Order IDs that filled
    orders_failed: List[str]  # Order IDs that failed or were cancelled
    total_cost: float  # Total USDC spent
    shares_filled: float  # Shares obtained across all outcomes
    actual_profit: float  # Realized profit (or loss if negative)
    error_message: Optional[str] = None


class ArbScanner:
    """
    Detects multi-outcome arbitrage opportunities across Polymarket
    
    Responsibility: Identify markets where sum(outcome_prices) < 0.98,
    accounting for 1.5% taker fee buffer.
    
    Does NOT execute - only identifies opportunities for AtomicExecutor.
    
    2026 UPGRADE: WebSocket-driven via MarketDataManager
    - Reads from shared MarketStateCache (sub-50ms latency)
    - Falls back to REST only if cache is stale
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        market_data_manager: Optional[Any] = None,
        max_budget: Optional[float] = None  # INSTITUTIONAL: Dynamic capital allocation
    ):
        """
        Initialize arbitrage scanner
        
        Args:
            client: Polymarket CLOB client
            order_manager: Order execution manager
            market_data_manager: Real-time market data manager (optional)
            max_budget: Dynamically allocated capital (optional)
        """
        self.client = client
        self.order_manager = order_manager
        self.market_data_manager = market_data_manager  # WebSocket data source
        self._max_budget = max_budget  # Store for runtime use
        self._cache: Dict[str, Dict] = {}  # Market data cache
        self._last_scan_time = 0
        self._scan_interval = 5  # Seconds between full scans
        
        # FLAW 3 FIX: Order book cache to prevent rate limit suicide
        # Cache format: {token_id: (order_book_data, timestamp)}
        self._orderbook_cache: Dict[str, tuple] = {}
        self._cache_ttl_seconds = 2.0  # Cache for 2 seconds
        
        logger.info(
            "ArbScanner initialized - Looking for sum(prices) < 0.98 opportunities\\n"
            f"  Order book cache TTL: {self._cache_ttl_seconds}s (rate limit protection)\\n"
            f"  WebSocket Mode: {'ENABLED' if market_data_manager else 'DISABLED (REST fallback)'}\\n"
            f"  SMART SLIPPAGE: Depth-based ({SLIPPAGE_TIGHT:.3f} - {SLIPPAGE_LOOSE:.3f})"
            + (f"\\n  Max Budget: ${max_budget:.2f}" if max_budget is not None else "")
        )
    
    def _validate_leg_microstructure(self, outcome: OutcomePrice, market_id: str = "unknown") -> bool:
        """INSTITUTIONAL UPGRADE (Phase 1): Validate individual arbitrage leg quality
        
        Per institutional audit - arbitrage needs per-leg microstructure validation to:
        1. Avoid wide spread legs (>10% spread creates execution risk)
        2. Reject extreme prices (long-shots bid <=0.02, favorites ask >=0.98)
        3. Ensure reasonable dollar liquidity per leg (not just share count)
        
        This is DIFFERENT from market making's 3% spread threshold because:
        - Arbitrage executes multi-leg baskets (cumulative slippage across legs)
        - Arbitrage holds for seconds (vs MM holding days)
        - Arbitrage needs looser per-leg tolerance but strict basket-level math
        
        Args:
            outcome: Outcome price data with bid/ask/depth
            market_id: Market ID for logging (optional)
            
        Returns:
            bool: True if leg passes microstructure quality checks, False otherwise
        """
        bid = outcome.bid_price
        ask = outcome.ask_price
        depth = outcome.available_depth
        
        # CHECK 1: Extreme price detection (same threshold as market making)
        # Reject long-shot bids (<=2 cents) - adverse selection risk
        if bid <= MIN_ARB_LEG_BID:
            logger.debug(
                f"[ARB REJECT] {market_id}: LEG {outcome.outcome_name[:20]} - "
                f"Extreme bid {bid:.4f} <= {MIN_ARB_LEG_BID:.2f} (long-shot risk)"
            )
            return False
        
        # Reject favorite asks (>=98 cents) - minimal profit potential
        if ask >= MAX_ARB_LEG_ASK:
            logger.debug(
                f"[ARB REJECT] {market_id}: LEG {outcome.outcome_name[:20]} - "
                f"Extreme ask {ask:.4f} >= {MAX_ARB_LEG_ASK:.2f} (favorite risk)"
            )
            return False
        
        # CHECK 2: Spread quality per leg (10% maximum vs MM's 3%)
        # Arbitrage needs looser tolerance since multi-leg execution
        if bid > 0:  # Avoid division by zero
            mid_price = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid_price if mid_price > 0 else 1.0
            
            if spread_pct > MAX_ARB_LEG_SPREAD_PERCENT:
                logger.debug(
                    f"[ARB REJECT] {market_id}: LEG {outcome.outcome_name[:20]} - "
                    f"Wide spread {spread_pct:.2%} > {MAX_ARB_LEG_SPREAD_PERCENT:.0%} "
                    f"(bid={bid:.4f}, ask={ask:.4f})"
                )
                return False
        
        # CHECK 3: Dollar liquidity per leg (not just share count)
        # Ensure at least $2 available per leg to avoid thin book slippage
        leg_liquidity_usd = depth * ask  # Dollar value at ask price
        
        if leg_liquidity_usd < MIN_ARB_LEG_LIQUIDITY_USD:
            logger.debug(
                f"[ARB REJECT] {market_id}: LEG {outcome.outcome_name[:20]} - "
                f"Thin liquidity ${leg_liquidity_usd:.2f} < ${MIN_ARB_LEG_LIQUIDITY_USD:.2f} "
                f"({depth:.1f} shares @ ${ask:.4f})"
            )
            return False
        
        # All checks passed
        return True
    
    def _calculate_smart_slippage(self, available_depth: float) -> float:
        """Calculate dynamic slippage tolerance based on order book depth
        
        Logic:
        - Thin books (< 20 shares): Use tight slippage (0.002) to minimize impact
        - Medium books (20-100 shares): Use moderate slippage (0.005)
        - Deep books (> 100 shares): Use loose slippage (0.010) for more opportunities
        
        Args:
            available_depth: Number of shares available at best ask
            
        Returns:
            Maximum slippage tolerance for this leg (in dollars)
        """
        if available_depth < DEPTH_THRESHOLD_THIN:
            # Thin book - tighten slippage to avoid impact
            return SLIPPAGE_TIGHT
        elif available_depth < DEPTH_THRESHOLD_MEDIUM:
            # Medium depth - use moderate slippage
            return SLIPPAGE_MODERATE
        else:
            # Deep book - can afford looser slippage
            return SLIPPAGE_LOOSE

    async def scan_markets(
        self,
        market_ids: Optional[List[str]] = None,
        limit: int = 100
    ) -> List[ArbitrageOpportunity]:
        """
        Scan markets for arbitrage opportunities
        
        Strategy:
        1. Fetch all active markets (or specified subset)
        2. Filter for multi-outcome markets (3+ outcomes)
        3. Fetch current prices for each outcome
        4. Check if sum(prices) < 0.98 (our threshold)
        5. For each opportunity, calculate potential profit
        6. Return sorted by profit/budget ratio (highest first)
        
        Args:
            market_ids: Specific markets to scan (None = all active)
            limit: Maximum number of markets to scan per call
            
        Returns:
            List of ArbitrageOpportunity objects, sorted by profitability
        """
        opportunities = []
        
        try:
            # Fetch markets
            if market_ids:
                markets = []
                for mid in market_ids[:limit]:
                    try:
                        market_data = await self.client.get_market(mid)
                        if market_data:
                            markets.append(market_data)
                    except Exception as e:
                        logger.debug(f"Skipping market {mid}: {e}")
                        continue
            else:
                # Fetch active markets from API
                response = await self.client.get_markets()
                markets = response.get('data', [])[:limit]
            
            logger.debug(f"Scanning {len(markets)} markets for arbitrage opportunities")
            
            # Track best near-miss for diagnostic logging
            best_near_miss = None
            best_sum = 1.0
            
            # Scan each market for arbitrage
            for market in markets:
                try:
                    arb_opp = await self._check_market_for_arbitrage(market)
                    if arb_opp:
                        opportunities.append(arb_opp)
                    else:
                        # Track closest opportunity even if not executable
                        market_sum = self._get_market_sum_prices(market)
                        if market_sum and FINAL_THRESHOLD <= market_sum < best_sum:
                            best_sum = market_sum
                            best_near_miss = market.get('question', 'Unknown')[:60]
                except Exception as e:
                    logger.debug(f"Error scanning market: {e}")
                    continue
            
            # Sort by profit/budget ratio (highest ROI first)
            opportunities.sort(
                key=lambda x: (x.net_profit_per_share / x.required_budget) 
                if x.required_budget > 0 else 0,
                reverse=True
            )
            
            logger.info(
                f"Scan complete: Found {len(opportunities)} arbitrage opportunities "
                f"(threshold: sum < {FINAL_THRESHOLD})"
            )
            
            # Log closest near-miss for market insight
            if not opportunities and best_near_miss:
                logger.info(
                    f"  Closest opportunity: sum={best_sum:.4f} "
                    f"(need {FINAL_THRESHOLD:.2f}) - {best_near_miss}"
                )
            
            return opportunities
            
        except Exception as e:
            logger.error(f"Market scan failed: {e}")
            return []

    def _get_market_sum_prices(self, market: Dict[str, Any]) -> Optional[float]:
        """
        Quick check for market sum of mid prices (for near-miss logging)
        
        Returns sum of mid prices if available, None otherwise
        """
        try:
            tokens = market.get('tokens', [])
            if len(tokens) < 3:
                return None
            
            total = 0.0
            for token in tokens:
                best_bid = float(token.get('price', 0))
                best_ask = float(token.get('price', 0))  # Using same for quick check
                mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else best_bid
                total += mid
            
            return total if total > 0 else None
        except:
            return None

    async def scan_events(
        self,
        events: Optional[List[Dict[str, Any]]] = None,
        limit: int = 100
    ) -> List[ArbitrageOpportunity]:
        """
        Scan events for multi-outcome arbitrage opportunities (INSTITUTION-GRADE 2026)
        
        Per Polymarket Support (Jan 2026):
        - Multi-outcome arbitrage works across EVENTS, not individual markets
        - Each event contains multiple binary markets (Yes/No)
        - Arbitrage exists when sum(YES prices across all markets) < $1.00
        - Must validate against ORDER BOOK DEPTH, not just midpoint prices
        - Handle NegRisk events (winner-take-all, unnamed placeholders)
        
        Example:
            Event: "2024 US Presidential Election"
            Markets within event:
              - Trump market: YES price = $0.45 (from order book ASK)
              - Biden market: YES price = $0.35 (from order book ASK)  
              - Harris market: YES price = $0.15 (from order book ASK)
            Sum of YES prices: $0.95
            Arbitrage profit: $1.00 - $0.95 = $0.05 per complete set
            
        Strategy:
        1. For each event with 3+ outcomes
        2. Fetch order book for EACH market's YES outcome  
        3. Calculate sum of best ASK prices (actual entry cost)
        4. If sum < $0.98, validate depth and create opportunity
        5. Return sorted by ROI (net_profit / required_budget)
        
        Args:
            events: List of event objects from get_events() (None = fetch fresh)
            limit: Maximum events to scan
            
        Returns:
            List of ArbitrageOpportunity objects sorted by profitability
        """
        opportunities = []
        
        try:
            # Fetch events if not provided
            if events is None:
                logger.debug("Fetching multi-outcome events for arbitrage scanning...")
                response = await self.client.get_events(
                    limit=limit,
                    closed=False,
                    active=True
                )
                events = response.get('data', [])
            
            logger.debug(f"Scanning {len(events[:limit])} events for arbitrage opportunities")
            
            # Track statistics for diagnostic logging
            events_scanned = 0
            events_with_arb = 0
            best_near_miss = None
            best_sum = 1.0
            
            # Scan each event
            for event in events[:limit]:
                events_scanned += 1
                
                try:
                    # Skip binary events (only 2 outcomes)
                    outcomes = event.get('outcomes', [])
                    if len(outcomes) < 3:
                        continue
                    
                    # Skip NegRisk events with unnamed placeholders
                    if event.get('negRisk', False):
                        named_outcomes = [o for o in outcomes if o and len(o) > 0]
                        if len(named_outcomes) < len(outcomes):
                            logger.debug(f"Skipping NegRisk event with unnamed placeholders: {event.get('id')}")
                            continue
                    
                    # Check event for arbitrage
                    arb_opp = await self._check_event_for_arbitrage(event)
                    
                    if arb_opp:
                        opportunities.append(arb_opp)
                        events_with_arb += 1
                    else:
                        # Track closest near-miss
                        event_sum = self._get_event_sum_prices(event)
                        if event_sum and FINAL_THRESHOLD <= event_sum < best_sum:
                            best_sum = event_sum
                            best_near_miss = event.get('title', 'Unknown')[:60]
                            
                except Exception as e:
                    logger.debug(f"Error scanning event {event.get('id', 'unknown')}: {e}")
                    continue
            
            # Sort by ROI (profit per dollar invested)
            opportunities.sort(
                key=lambda x: (x.net_profit_per_share / x.required_budget) if x.required_budget > 0 else 0,
                reverse=True
            )
            
            logger.info(
                f"Event scan complete: {events_with_arb} opportunities found "
                f"({events_scanned} events scanned, threshold: sum < {FINAL_THRESHOLD})"
            )
            
            # Log closest near-miss for market insight
            if not opportunities and best_near_miss:
                logger.info(
                    f"  Closest opportunity: sum={best_sum:.4f} "
                    f"(need < {FINAL_THRESHOLD:.2f}) - {best_near_miss}"
                )
            
            return opportunities
            
        except Exception as e:
            logger.error(f"Event scan failed: {e}", exc_info=True)
            return []

    def _get_event_sum_prices(self, event: Dict[str, Any]) -> Optional[float]:
        """
        Quick sum of outcome prices from event (for near-miss diagnostics)
        Uses outcomePrices array if available
        """
        try:
            prices = event.get('outcomePrices', [])
            if len(prices) < 3:
                return None
            return sum(float(p) for p in prices)
        except:
            return None

    async def _check_event_for_arbitrage(
        self,
        event: Dict[str, Any]
    ) -> Optional[ArbitrageOpportunity]:
        """
        Check single event for multi-outcome arbitrage opportunity
        
        Per Polymarket Support:
        - Fetch order book for each market within event
        - Use best ASK price (actual purchase cost), NOT midpoint
        - Validate sufficient depth on ALL legs before accepting opportunity
        - Calculate smart slippage based on depth per leg
        
        INSTITUTIONAL UPGRADE (Phase 1):
        - Validate event-level AND market-level status (active, not closed)
        - Validate CLOB enablement for all constituent markets
        - Reject if any market is closed/inactive/disabled
        
        Args:
            event: Event data from /events API
            
        Returns:
            ArbitrageOpportunity if valid, None otherwise
        """
        try:
            event_id = event.get('id', 'unknown')
            outcomes = event.get('outcomes', [])
            token_ids = event.get('clobTokenIds', [])
            
            # INSTITUTIONAL CHECK 1: Event-level status validation
            # Per audit - event can be active while constituent markets are closed
            if event.get('closed', False):
                logger.debug(f"[ARB REJECT] {event_id}: Event is closed")
                return None
            
            if not event.get('active', True):
                logger.debug(f"[ARB REJECT] {event_id}: Event is inactive")
                return None
            
            # INSTITUTIONAL CHECK 2: Market-level status validation
            # Validate each constituent market within the event
            markets = event.get('markets', [])
            if markets:  # If markets array is present, validate each
                for market in markets:
                    if market.get('closed', False):
                        logger.debug(
                            f"[ARB REJECT] {event_id}: Constituent market {market.get('id', 'unknown')} is closed"
                        )
                        return None
                    
                    if not market.get('active', True):
                        logger.debug(
                            f"[ARB REJECT] {event_id}: Constituent market {market.get('id', 'unknown')} is inactive"
                        )
                        return None
                    
                    # INSTITUTIONAL CHECK 3: CLOB enablement (cross-strategy consistency)
                    # Same check as market making strategy Layer 3
                    if market.get('enableOrderBook') is False:
                        logger.debug(
                            f"[ARB REJECT] {event_id}: Constituent market {market.get('id', 'unknown')} has CLOB disabled"
                        )
                        return None
            
            # Validation: Must have same number of outcomes and token IDs
            if len(outcomes) != len(token_ids):
                logger.debug(
                    f"Skipping event {event_id}: outcome/token mismatch "
                    f"({len(outcomes)} outcomes, {len(token_ids)} tokens)"
                )
                return None
            
            # Fetch order books for ALL outcomes (with depth validation)
            outcome_prices: List[OutcomePrice] = []
            total_ask_sum = Decimal('0')
            
            for i, (outcome_name, token_id) in enumerate(zip(outcomes, token_ids)):
                try:
                    # Get order book (from cache or REST)
                    order_book = await self._get_cached_order_book(token_id)
                    
                    if not order_book or 'asks' not in order_book:
                        logger.debug(f"Skipping {event_id}: no asks for outcome {outcome_name}")
                        return None
                    
                    asks = order_book['asks']
                    if not asks or len(asks) == 0:
                        logger.debug(f"Skipping {event_id}: empty ask book for {outcome_name}")
                        return None
                    
                    # Get best ask (actual purchase price)
                    best_ask = Decimal(str(asks[0]['price']))
                    available_depth = Decimal(str(asks[0]['size']))
                    
                    # Validate minimum depth
                    if available_depth < MIN_ORDER_BOOK_DEPTH:
                        logger.debug(
                            f"Skipping {event_id}: insufficient depth on {outcome_name} "
                            f"({available_depth} < {MIN_ORDER_BOOK_DEPTH})"
                        )
                        return None
                    
                    # Calculate smart slippage for this leg
                    slippage = self._calculate_smart_slippage(float(available_depth))
                    
                    outcome_prices.append(OutcomePrice(
                        outcome_name=outcome_name,
                        token_id=token_id,
                        price=best_ask,
                        available_depth=available_depth,
                        slippage_tolerance=Decimal(str(slippage))
                    ))
                    
                    total_ask_sum += best_ask
                    
                except Exception as e:
                    logger.debug(f"Error fetching order book for {outcome_name}: {e}")
                    return None
            
            # Check if arbitrage exists (sum of asks < threshold)
            if total_ask_sum >= Decimal(str(FINAL_THRESHOLD)):
                return None  # No arbitrage
            
            # Calculate profit metrics
            profit_per_share = Decimal('1.0') - total_ask_sum
            net_profit_per_share = profit_per_share - Decimal(str(sum(op.slippage_tolerance for op in outcome_prices)))
            
            # Skip if net profit too small
            if net_profit_per_share <= Decimal('0.001'):
                return None
            
            # Calculate max shares based on depth
            min_depth = min(op.available_depth for op in outcome_prices)
            max_shares = min(
                float(min_depth),
                MAX_ARBITRAGE_BUDGET_PER_BASKET / float(total_ask_sum)
            )
            
            required_budget = min(
                MIN_ARBITRAGE_BUDGET_PER_BASKET,
                max_shares * float(total_ask_sum)
            )
            
            # Create opportunity
            return ArbitrageOpportunity(
                market_id=event_id,
                condition_id=event.get('conditionId', event_id),
                market_type=MarketType.NEGRISK if event.get('negRisk') else MarketType.STANDARD,
                outcomes=outcome_prices,
                sum_prices=total_ask_sum,
                profit_per_share=profit_per_share,
                net_profit_per_share=net_profit_per_share,
                required_budget=required_budget,
                max_shares_to_buy=max_shares,
                expected_profit=float(net_profit_per_share) * max_shares,
                arbitrage_profit_pct=float(profit_per_share / total_ask_sum * 100)
            )
            
        except Exception as e:
            logger.error(f"Error checking event for arbitrage: {e}", exc_info=True)
            return None

    async def _get_cached_order_book(self, token_id: str):
        """
        2026 UPGRADE: Get order book from WebSocket cache or REST fallback
        
        Priority:
        1. Read from MarketStateCache (sub-50ms latency)
        2. If stale or missing, fallback to REST API
        3. Cache REST response locally (2s TTL)
        
        Args:
            token_id: Token to fetch order book for
            
        Returns:
            Order book data (fresh from cache or REST)
        """
        # PRIORITY 1: Try WebSocket cache first (if available)
        if self.market_data_manager:
            cached_book = self.market_data_manager.get_order_book(token_id)
            
            if cached_book and not self.market_data_manager.is_market_stale(token_id):
                # Cache hit with fresh data - return immediately
                logger.debug(f"[CACHE HIT] {token_id[:8]}... from WebSocket cache")
                return cached_book
            
            # Stale cache - force REST refresh
            if cached_book:
                logger.debug(f"[STALE CACHE] {token_id[:8]}... refreshing from REST")
                success = await self.market_data_manager.force_refresh_from_rest(token_id)
                if success:
                    return self.market_data_manager.get_order_book(token_id)
        
        # PRIORITY 2: Local cache (for systems without MarketDataManager)
        current_time = time.time()
        if token_id in self._orderbook_cache:
            cached_book, cache_time = self._orderbook_cache[token_id]
            
            if (current_time - cache_time) < self._cache_ttl_seconds:
                # Local cache hit
                logger.debug(f"[LOCAL CACHE] {token_id[:8]}... from local cache")
                return cached_book
        
        # PRIORITY 3: Fetch from REST API
        try:
            logger.debug(f"[REST FETCH] {token_id[:8]}... fetching from API")
            order_book = await self.client.get_order_book(token_id)
            
            # Store in local cache
            self._orderbook_cache[token_id] = (order_book, current_time)
            
            return order_book
            
        except Exception as e:
            # If fetch fails and we have stale cache, return stale data
            if token_id in self._orderbook_cache:
                logger.warning(
                    f"Order book fetch failed for {token_id[:8]}, using stale local cache"
                )
                cached_book, _ = self._orderbook_cache[token_id]
                return cached_book
            else:
                # No cache and fetch failed - re-raise
                raise e
    
    async def _check_market_for_arbitrage(
        self,
        market: Dict[str, Any]
    ) -> Optional[ArbitrageOpportunity]:
        """
        Check single market for arbitrage opportunity
        
        Args:
            market: Market data from API
            
        Returns:
            ArbitrageOpportunity if found, None otherwise
        """
        try:
            # Extract market IDs
            market_id = market.get('id')
            condition_id = market.get('conditionId')
            
            if not market_id or not condition_id:
                return None
            
            # [SAFETY] FIX 1: Enhanced Augmented NegRisk Filter
            # Check BOTH enableNegRisk AND negRiskAugmented from Gamma API
            # Per Polymarket team: Augmented markets require unnamed placeholders for merge
            # Since we can't trade placeholders, we MUST skip these markets
            enable_neg_risk = market.get('enableNegRisk', False)
            neg_risk_augmented = market.get('negRiskAugmented', False)
            
            if enable_neg_risk and neg_risk_augmented:
                logger.warning(
                    f"[SAFETY] Skipping Augmented NegRisk Market: {market_id[:8]}...\n"
                    f"  Flags: enableNegRisk={enable_neg_risk}, negRiskAugmented={neg_risk_augmented}\n"
                    f"  Reason: Requires unnamed placeholders for merge (capital lock risk)\n"
                    f"  Action: $100 principal protection - SKIP"
                )
                return None
            
            # Log if standard NegRisk (tradeable)
            if enable_neg_risk and not neg_risk_augmented:
                logger.debug(
                    f"[NegRisk] Market {market_id[:8]} is standard NegRisk (tradeable)"
                )
            
            # Check market type - must be multi-outcome for arbitrage
            outcomes = market.get('outcomes', [])
            if not outcomes or len(outcomes) < 2:
                return None  # Skip binary or invalid markets
            
            # Detect market type
            is_negrisk = self._is_negrisk_market(market)
            market_type = (
                MarketType.MULTI_CHOICE if len(outcomes) > 2 else
                MarketType.BINARY if len(outcomes) == 2 else
                None
            )
            
            if not market_type:
                return None
            
            # Get token IDs for outcomes (from clobTokenIds array)
            token_ids = market.get('clobTokenIds', [])
            if len(token_ids) != len(outcomes):
                logger.debug(f"Market {market_id}: token_ids mismatch")
                return None
            
            # Fetch current prices and order book depth for each outcome
            outcome_prices = []
            sum_prices = 0.0
            
            for idx, (outcome, token_id) in enumerate(zip(outcomes, token_ids)):
                try:
                    # FLAW 3 FIX: Use cached order book to prevent rate limits
                    order_book = await self._get_cached_order_book(token_id)
                    
                    # Parse order book
                    bids = getattr(order_book, 'bids', [])
                    asks = getattr(order_book, 'asks', [])
                    
                    if not bids or not asks:
                        logger.debug(f"No order book data for {token_id}")
                        return None  # Skip if order book is empty
                    
                    # Extract prices
                    best_bid = float(bids[0]['price']) if bids else 0.0
                    best_ask = float(asks[0]['price']) if asks else 1.0
                    mid_price = (best_bid + best_ask) / 2.0
                    
                    # Calculate available depth at ask price
                    depth_at_ask = sum(
                        float(ask['size']) for ask in asks 
                        if float(ask['price']) <= best_ask + 0.01
                    )
                    
                    if depth_at_ask < MIN_ORDER_BOOK_DEPTH:
                        logger.debug(
                            f"Insufficient depth for {token_id}: "
                            f"{depth_at_ask} < {MIN_ORDER_BOOK_DEPTH}"
                        )
                        return None  # Skip if insufficient depth
                    
                    outcome_price = OutcomePrice(
                        outcome_index=idx,
                        outcome_name=outcome,
                        token_id=token_id,
                        yes_price=mid_price,
                        bid_price=best_bid,
                        ask_price=best_ask,
                        available_depth=depth_at_ask
                    )
                    
                    # INSTITUTIONAL UPGRADE (Phase 1): Validate leg microstructure quality
                    # Reject entire opportunity if any leg fails quality checks
                    if not self._validate_leg_microstructure(outcome_price, market_id=market_id):
                        return None  # Entire opportunity rejected if one leg is poor quality
                    
                    outcome_prices.append(outcome_price)
                    sum_prices += mid_price
                    
                except Exception as e:
                    logger.debug(f"Error fetching order book for {token_id}: {e}")
                    return None
            
            # Check if this is an arbitrage opportunity
            # For NegRisk, also check "short the field" cost
            if is_negrisk:
                short_field_cost = 1.0 - sum_prices
                norm_entry_cost = min(sum_prices, short_field_cost)
            else:
                norm_entry_cost = sum_prices
            
            # Profitability check: sum < 0.98
            if norm_entry_cost >= FINAL_THRESHOLD:
                return None  # Not an opportunity
            
            # Calculate profit
            profit_per_share = 1.0 - norm_entry_cost
            
            # 2026 UPDATE: Fetch dynamic fee rates for each token
            fee_rates_bps = {}
            total_fee_percent = 0.0
            
            try:
                for outcome in outcome_prices:
                    fee_bps = await self.client.get_fee_rate_bps(outcome.token_id)
                    fee_rates_bps[outcome.token_id] = fee_bps
                    # Convert basis points to decimal (100 bps = 1%)
                    total_fee_percent += (fee_bps / 10000.0)
                
                logger.debug(
                    f"Market {market_id}: Dynamic fees fetched - "
                    f"Total: {total_fee_percent*100:.2f}% across {len(outcomes)} outcomes"
                )
            except Exception as e:
                # Fallback to static 1.5% per trade if dynamic fetch fails
                logger.warning(
                    f"Failed to fetch dynamic fees for {market_id}, "
                    f"using fallback {TAKER_FEE_PERCENT*100}% per trade: {e}"
                )
                total_fee_percent = TAKER_FEE_PERCENT * len(outcomes)
                fee_rates_bps = None
            
            # Net profit = Gross profit - Total fees
            # Formula: (1.0 - sum_prices) - (entry_cost × total_fee_percent)
            total_fee_amount = norm_entry_cost * total_fee_percent
            net_profit_per_share = profit_per_share - total_fee_amount
            
            # Only report if profit exceeds threshold
            if net_profit_per_share < MINIMUM_PROFIT_THRESHOLD:
                logger.debug(
                    f"Market {market_id}: profit {net_profit_per_share} "
                    f"below threshold {MINIMUM_PROFIT_THRESHOLD}"
                )
                return None
            
            # [SAFETY] FIX 2: "Other" Outcome Verification for Full Set Merge
            # For NegRisk markets, verify we have the "Other" token for complete partition
            has_other_token = False
            if is_negrisk:
                for outcome in outcome_prices:
                    if outcome.outcome_name.lower() in ['other', 'others', 'none of the above']:
                        has_other_token = True
                        logger.debug(
                            f"[MERGE_READY] Market {market_id[:8]} has 'Other' token: "
                            f"{outcome.token_id[:8]}..."
                        )
                        break
                
                if not has_other_token:
                    logger.warning(
                        f"[MERGE_INCOMPLETE] Skipping NegRisk market {market_id[:8]}\n"
                        f"  Reason: Missing 'Other' outcome token\n"
                        f"  Impact: Cannot form complete partition for merge\n"
                        f"  Action: Skip to prevent partial position lock"
                    )
                    return None
            
            # Calculate required budget and max shares
            min_shares_for_profile = MIN_ORDER_BOOK_DEPTH
            min_depth = min(op.available_depth for op in outcome_prices)
            max_shares = min(min_depth, MAX_ARBITRAGE_BUDGET_PER_BASKET / norm_entry_cost)
            
            required_budget = min(
                MIN_ARBITRAGE_BUDGET_PER_BASKET,
                max_shares * norm_entry_cost
            )
            
            # Create opportunity object
            return ArbitrageOpportunity(
                market_id=market_id,
                condition_id=condition_id,
                market_type=market_type,
                outcomes=outcome_prices,
                sum_prices=sum_prices,
                profit_per_share=profit_per_share,
                net_profit_per_share=net_profit_per_share,
                required_budget=required_budget,
                max_shares_to_buy=max_shares,
                is_negrisk=is_negrisk,
                negrisk_short_field_cost=1.0 - sum_prices if is_negrisk else None,
                fee_rates_bps=fee_rates_bps,
                total_fee_amount=total_fee_amount
            )
            
        except Exception as e:
            logger.debug(f"Error checking market for arbitrage: {e}")
            return None

    def _is_negrisk_market(self, market: Dict[str, Any]) -> bool:
        """
        Detect if market is NegRisk (inverse/negation market)
        
        NegRisk indicators:
        - Market question contains "NOT", "lose", "decline", "fail" etc.
        - Market is marked as negRisk in API response
        - Outcome probabilities are inverted (NOT relationship)
        
        Args:
            market: Market data
            
        Returns:
            True if market appears to be NegRisk/inverse
        """
        # Check for negrisk flag in market data
        if market.get('negRisk', False):
            return True
        
        # Check question text for indicators
        question = market.get('question', '').lower()
        negrisk_indicators = ['not', 'won\'t', 'fail', 'lose', 'decline', 'unable']
        
        for indicator in negrisk_indicators:
            if indicator in question:
                return True
        
        return False


class AtomicExecutor:
    """
    Executes arbitrage opportunities with atomic (all-or-nothing) semantics
    
    Responsibility: Execute all legs of an arbitrage basket with FOK logic.
    If any leg fails, abort the entire sequence and cancel pending orders.
    
    Safety: Validates slippage, balance, and order book depth before execution.
    
    INSTITUTIONAL UPGRADE (Phase 1): Staleness validation before execution
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        market_data_manager: Optional[Any] = None
    ):
        """
        Initialize executor with order management
        
        Args:
            client: Polymarket CLOB client
            order_manager: Order execution manager with validation
            market_data_manager: MarketDataManager for staleness check (optional)
        """
        self.client = client
        self.order_manager = order_manager
        self.market_data_manager = market_data_manager  # INSTITUTIONAL: For staleness check
        self._pending_orders: Dict[str, Dict] = {}  # Track pending orders
        self._budget_used = Decimal('0')
        self._max_budget = Decimal(str(TOTAL_ARBITRAGE_BUDGET))
        
        # Create scanner instance for smart slippage calculation
        self._scanner = None  # Will be set by _calculate_smart_slippage if needed
        
        logger.info(
            f"AtomicExecutor initialized - "
            f"FOK mode, Max budget: ${TOTAL_ARBITRAGE_BUDGET}, "
            f"Smart slippage: {SLIPPAGE_TIGHT:.3f} - {SLIPPAGE_LOOSE:.3f} (depth-based)"
            + (", Staleness check: ENABLED" if market_data_manager else ", Staleness check: DISABLED")
        )
    
    def _calculate_smart_slippage(self, available_depth: float) -> float:
        """Calculate dynamic slippage tolerance based on order book depth
        
        Logic:
        - Thin books (< 20 shares): Use tight slippage (0.002) to minimize impact
        - Medium books (20-100 shares): Use moderate slippage (0.005)
        - Deep books (> 100 shares): Use loose slippage (0.010) for more opportunities
        
        Args:
            available_depth: Number of shares available at best ask
            
        Returns:
            Maximum slippage tolerance for this leg (in dollars)
        """
        if available_depth < DEPTH_THRESHOLD_THIN:
            # Thin book - tighten slippage to avoid impact
            return SLIPPAGE_TIGHT
        elif available_depth < DEPTH_THRESHOLD_MEDIUM:
            # Medium depth - use moderate slippage
            return SLIPPAGE_MODERATE
        else:
            # Deep book - can afford looser slippage
            return SLIPPAGE_LOOSE

    async def execute(
        self,
        opportunity: ArbitrageOpportunity,
        shares_to_buy: float
    ) -> ExecutionResult:
        """
        Atomically execute arbitrage opportunity (all-or-nothing)
        
        Execution Flow:
        1. Validate budget and slippage constraints
        2. Place BUY orders for all outcomes (FOK)
        3. If all fill → return success
        4. If any fails → cancel pending and return failure
        
        Args:
            opportunity: Detected arbitrage opportunity
            shares_to_buy: Number of shares to purchase per outcome
            
        Returns:
            ExecutionResult with details of execution attempt
        """
        start_time = time.time()
        execution_id = f"{opportunity.market_id}_{int(start_time)}"
        orders_executed = []
        orders_failed = []
        total_cost = 0.0
        
        try:
            logger.info(
                f"[{execution_id}] Executing arbitrage: "
                f"{opportunity.market_type.value} market, "
                f"{shares_to_buy} shares @ {opportunity.required_budget:.2f} USDC"
            )
            
            # Pre-execution validation (INSTITUTIONAL: includes staleness check)
            await self._validate_execution(opportunity, shares_to_buy, self.market_data_manager)
            
            # FLAW 1 FIX: Use asyncio.gather() for TRUE atomic execution
            # All orders fire at the same millisecond - no sequential "legging-in"
            # 
            # PRODUCTION NOTE: API Limitation on True Atomicity
            # ================================================
            # While asyncio.gather() fires HTTP requests concurrently from Python's
            # perspective, the Polymarket CLOB API does NOT support:
            #   - Batch order submission
            #   - Atomic multi-market order bundles  
            #   - Fill-all-or-cancel across multiple outcomes
            # 
            # The API processes each order SEQUENTIALLY (one POST at a time).
            # In institutional environments, this creates a race condition where:
            #   1. Leg 1 fills at T+0ms
            #   2. Leg 2 arrives at T+50ms → market moved → rejected
            #   3. Result: Partial fill (\"legged in\" with directional exposure)
            # 
            # MITIGATION: Emergency liquidation routine (see _emergency_liquidation)
            # If any leg fails, immediately market-sell filled positions to return
            # to cash. Expected loss: 2-5% per failed attempt (vs. 100% without).
            # 
            # Verified with Polymarket Support (Jan 2026): No batch endpoints available.
            # See PRODUCTION_RISK_ANALYSIS.md for full risk assessment.
            async def place_single_order(outcome):
                """Place order for single outcome - used in parallel execution"""
                try:
                    # Calculate order details
                    order_cost = shares_to_buy * outcome.ask_price
                    expected_slippage = abs(outcome.ask_price - outcome.yes_price)
                    
                    # SMART SLIPPAGE: Calculate dynamic slippage based on depth
                    max_slippage = self._calculate_smart_slippage(outcome.available_depth)
                    
                    # Validate slippage constraint
                    if expected_slippage > max_slippage:
                        raise SlippageExceededError(
                            f"Slippage ${expected_slippage:.4f} exceeds smart max ${max_slippage:.4f} "
                            f"(depth: {outcome.available_depth:.1f} shares)",
                            expected=expected_slippage,
                            maximum=max_slippage
                        )
                    
                    # Place BUY order with FOK
                    order_result = await self.order_manager.execute_market_order(
                        token_id=outcome.token_id,
                        side='BUY',
                        size=order_cost,
                        max_slippage=max_slippage / outcome.ask_price,
                        is_shares=True  # Buy by shares, not USD
                    )
                    
                    order_id = order_result.get('order_id')
                    logger.debug(
                        f"[{execution_id}] Outcome {outcome.outcome_index}: "
                        f"Order {order_id} placed for {shares_to_buy} shares "
                        f"(slippage: {max_slippage:.4f}, depth: {outcome.available_depth:.1f})"
                    )
                    return (outcome.token_id, order_id, outcome.outcome_index, None)
                    
                except Exception as e:
                    logger.error(
                        f"[{execution_id}] Order failed for outcome {outcome.outcome_index}: {e}"
                    )
                    return (outcome.token_id, None, outcome.outcome_index, e)
            
            # Fire all orders concurrently using asyncio.gather()
            logger.info(f"[{execution_id}] Firing {len(opportunity.outcomes)} orders concurrently...")
            results = await asyncio.gather(
                *[place_single_order(outcome) for outcome in opportunity.outcomes],
                return_exceptions=True
            )
            
            # Check results - ALL must succeed or we abort
            pending_order_ids = []
            filled_outcomes = []  # Track successfully filled legs
            
            for result in results:
                if isinstance(result, Exception):
                    # Exception during order placement
                    logger.error(f"[{execution_id}] Order exception: {result}")
                    orders_failed.append("exception")
                    break
                    
                token_id, order_id, outcome_idx, error = result
                
                if error or not order_id:
                    # Order failed
                    orders_failed.append(token_id)
                    logger.error(f"[{execution_id}] Leg {outcome_idx} FAILED - ABORTING basket")
                    break
                else:
                    # Order succeeded
                    pending_order_ids.append((token_id, order_id))
                    filled_outcomes.append((token_id, order_id, outcome_idx))
            
            # Check if all legs filled
            if len(filled_outcomes) != len(opportunity.outcomes):
                # FLAW 2 FIX: Emergency Liquidation - market sell filled legs
                logger.error(
                    f"[{execution_id}] ⚠️  PARTIAL FILL DETECTED! "
                    f"{len(filled_outcomes)}/{len(opportunity.outcomes)} legs filled"
                )
                
                # Immediately liquidate filled positions
                await self._emergency_liquidation(execution_id, filled_outcomes, shares_to_buy)
                
                return ExecutionResult(
                    success=False,
                    market_id=opportunity.market_id,
                    orders_executed=[oid for _, oid, _ in filled_outcomes],
                    orders_failed=orders_failed,
                    total_cost=total_cost,
                    shares_filled=0.0,
                    actual_profit=0.0,
                    error_message=f"Partial basket fill - emergency liquidation executed"
                )
            
            # All orders placed successfully
            orders_executed = [oid for _, oid in pending_order_ids]
            total_cost = shares_to_buy * opportunity.required_budget
            
            # Update budget tracking
            self._budget_used += Decimal(str(total_cost))
            
            # Calculate actual profit
            profit = shares_to_buy * opportunity.net_profit_per_share
            
            elapsed = time.time() - start_time
            logger.info(
                f"[{execution_id}] ✅ EXECUTION SUCCESS: "
                f"{len(orders_executed)} orders filled in {elapsed:.2f}s, "
                f"Cost: ${total_cost:.2f}, Profit: ${profit:.2f}"
            )
            
            return ExecutionResult(
                success=True,
                market_id=opportunity.market_id,
                orders_executed=orders_executed,
                orders_failed=[],
                total_cost=total_cost,
                shares_filled=shares_to_buy,
                actual_profit=profit,
                error_message=None
            )
            
        except Exception as e:
            logger.error(f"[{execution_id}] Execution failed: {e}")
            return ExecutionResult(
                success=False,
                market_id=opportunity.market_id,
                orders_executed=orders_executed,
                orders_failed=orders_failed,
                total_cost=total_cost,
                shares_filled=0.0,
                actual_profit=0.0,
                error_message=str(e)
            )

    async def _emergency_liquidation(
        self,
        execution_id: str,
        filled_outcomes: List[tuple],
        shares: float
    ) -> None:
        """
        FLAW 2 FIX: Emergency Liquidation Routine
        
        If partial basket execution occurs (some legs fill, others fail),
        immediately market-sell all filled positions to return to cash.
        
        This prevents "legging-in" losses where we hold incomplete hedges.
        
        Args:
            execution_id: Execution identifier
            filled_outcomes: List of (token_id, order_id, outcome_idx) that filled
            shares: Number of shares to liquidate per outcome
        """
        logger.warning(
            f"[{execution_id}] 🚨 EMERGENCY LIQUIDATION: "
            f"Market-selling {len(filled_outcomes)} filled legs"
        )
        
        liquidation_tasks = []
        
        for token_id, order_id, outcome_idx in filled_outcomes:
            async def liquidate_position(tid, oid, idx):
                try:
                    # Get current bid to determine market sell price
                    book = await self.client.get_order_book(tid)
                    bids = getattr(book, 'bids', [])
                    
                    if not bids:
                        logger.error(
                            f"[{execution_id}] No bids for {tid[:8]} - cannot liquidate"
                        )
                        return False
                    
                    # Market sell at 2% below best bid to ensure fill
                    best_bid = float(bids[0]['price'])
                    liquidation_price = max(0.01, best_bid * 0.98)
                    
                    result = await self.order_manager.execute_market_order(
                        token_id=tid,
                        side='SELL',
                        size=shares,
                        max_slippage=0.05,  # Accept 5% slippage for emergency exit
                        is_shares=True
                    )
                    
                    if result and result.get('success'):
                        logger.info(
                            f"[{execution_id}] ✅ Liquidated leg {idx}: "
                            f"{shares} shares @ ${liquidation_price:.4f}"
                        )
                        return True
                    else:
                        logger.error(
                            f"[{execution_id}] ❌ Failed to liquidate leg {idx}"
                        )
                        return False
                        
                except Exception as e:
                    logger.error(
                        f"[{execution_id}] Liquidation error for leg {idx}: {e}"
                    )
                    return False
            
            liquidation_tasks.append(liquidate_position(token_id, order_id, outcome_idx))
        
        # Execute all liquidations concurrently
        liquidation_results = await asyncio.gather(*liquidation_tasks, return_exceptions=True)
        
        success_count = sum(1 for r in liquidation_results if r is True)
        logger.warning(
            f"[{execution_id}] Emergency liquidation complete: "
            f"{success_count}/{len(filled_outcomes)} positions closed"
        )
    
    async def _validate_execution(
        self,
        opportunity: ArbitrageOpportunity,
        shares_to_buy: float,
        market_data_manager: Optional[Any] = None
    ) -> None:
        """
        Validate execution prerequisites
        
        Checks:
        1. Sufficient budget remaining
        2. Sufficient balance in account
        3. Order book depth constraints
        4. Slippage within limits
        5. INSTITUTIONAL (Phase 1): Data staleness check (avoid adverse fills)
        
        Args:
            opportunity: Arbitrage opportunity
            shares_to_buy: Proposed share count
            market_data_manager: MarketDataManager for staleness check (optional)
            
        Raises:
            TradingError: If any validation fails
        """
        # INSTITUTIONAL CHECK (Phase 1): Staleness validation before execution
        # Per audit - prevent adverse fills on stale data (>5s old)
        if market_data_manager:
            for outcome in opportunity.outcomes:
                if market_data_manager.is_market_stale(outcome.token_id):
                    raise TradingError(
                        f"[ARB ABORT] Stale data detected for {outcome.token_id[:8]}... "
                        f"(>{DATA_STALENESS_THRESHOLD}s since last update). "
                        f"Aborting to prevent adverse fill."
                    )
                    logger.warning(
                        f"[STALENESS ABORT] Rejected arbitrage execution on stale data "
                        f"for {opportunity.market_id}"
                    )
        
        # Check budget
        required_cost = shares_to_buy * opportunity.required_budget
        budget_remaining = self._max_budget - self._budget_used
        
        if required_cost > float(budget_remaining):
            raise TradingError(
                f"Insufficient budget: ${required_cost:.2f} required, "
                f"${budget_remaining:.2f} remaining"
            )
        
        # Check balance
        balance = await self.client.get_balance()
        if balance < required_cost:
            raise InsufficientBalanceError(
                f"Insufficient balance: {balance} USDC < {required_cost} USDC",
                required=required_cost,
                available=float(balance)
            )
        
        # Check order book depth for all outcomes
        for outcome in opportunity.outcomes:
            if outcome.available_depth < shares_to_buy:
                raise TradingError(
                    f"Insufficient order book depth for {outcome.outcome_name}: "
                    f"{outcome.available_depth} shares < {shares_to_buy} required"
                )
        
        logger.debug(f"Pre-execution validation passed for {opportunity.market_id}")

    async def _abort_execution(
        self,
        execution_id: str,
        pending_order_ids: List[Tuple[str, str]]
    ) -> None:
        """
        Cancel all pending orders in case of execution failure
        
        Args:
            execution_id: Execution identifier for logging
            pending_order_ids: List of (token_id, order_id) tuples
        """
        logger.warning(
            f"[{execution_id}] Aborting execution - cancelling {len(pending_order_ids)} pending orders"
        )
        
        for token_id, order_id in pending_order_ids:
            try:
                await self.client.cancel_order(order_id)
                logger.debug(f"[{execution_id}] Cancelled order {order_id}")
            except Exception as e:
                logger.error(
                    f"[{execution_id}] Failed to cancel order {order_id}: {e}"
                )

    def get_budget_status(self) -> Dict[str, float]:
        """
        Get current budget usage status
        
        Returns:
            Dictionary with budget metrics
        """
        return {
            'total_budget': float(self._max_budget),
            'used_budget': float(self._budget_used),
            'remaining_budget': float(self._max_budget - self._budget_used),
            'utilization_percent': (float(self._budget_used) / float(self._max_budget)) * 100
        }

    def reset_budget(self) -> None:
        """Reset budget tracking (typically after day close)"""
        self._budget_used = Decimal('0')
        logger.info("Budget tracking reset for new trading day")
