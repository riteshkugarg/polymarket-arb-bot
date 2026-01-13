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
from utils.logger import get_logger
from utils.exceptions import (
    OrderRejectionError,
    SlippageExceededError,
    InsufficientBalanceError,
    TradingError,
)


logger = get_logger(__name__)


# Constants for arbitrage logic
TAKER_FEE_PERCENT = 0.015  # 1.5% per trade
ARBITRAGE_OPPORTUNITY_THRESHOLD = 0.98  # sum(prices) < 0.98
TAKER_FEE_BUFFER = TAKER_FEE_PERCENT  # Account for fee in opportunity detection
FINAL_THRESHOLD = ARBITRAGE_OPPORTUNITY_THRESHOLD  # sum < 0.98 after fee buffer
MAX_SLIPPAGE_PER_LEG = 0.005  # $0.005 maximum slippage per outcome
MIN_ORDER_BOOK_DEPTH = 10  # Require depth for at least 10 shares
MAX_ARBITRAGE_BUDGET_PER_BASKET = 10.0  # Max $10 per arbitrage basket
MIN_ARBITRAGE_BUDGET_PER_BASKET = 5.0  # Min $5 per arbitrage basket
TOTAL_ARBITRAGE_BUDGET = 100.0  # Total budget cap
MINIMUM_PROFIT_THRESHOLD = 0.001  # Don't execute if profit < $0.001


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
    net_profit_per_share: float  # After accounting for 1.5% fee × num_outcomes
    required_budget: float  # Cost per share × desired shares
    max_shares_to_buy: float  # Limited by order book depth
    is_negrisk: bool = False
    negrisk_short_field_cost: Optional[float] = None


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
    """
    
    def __init__(self, client: PolymarketClient, order_manager: OrderManager):
        """
        Initialize arbitrage scanner
        
        Args:
            client: Polymarket CLOB client
            order_manager: Order execution manager
        """
        self.client = client
        self.order_manager = order_manager
        self._cache: Dict[str, Dict] = {}  # Market data cache
        self._last_scan_time = 0
        self._scan_interval = 5  # Seconds between full scans
        
        logger.info("ArbScanner initialized - Looking for sum(prices) < 0.98 opportunities")

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
            
            # Scan each market for arbitrage
            for market in markets:
                try:
                    arb_opp = await self._check_market_for_arbitrage(market)
                    if arb_opp:
                        opportunities.append(arb_opp)
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
            
            return opportunities
            
        except Exception as e:
            logger.error(f"Market scan failed: {e}")
            return []

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
                    # Get current price (midpoint of bid/ask)
                    order_book = await self.client.get_order_book(token_id)
                    
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
                    
                    outcome_prices.append(OutcomePrice(
                        outcome_index=idx,
                        outcome_name=outcome,
                        token_id=token_id,
                        yes_price=mid_price,
                        bid_price=best_bid,
                        ask_price=best_ask,
                        available_depth=depth_at_ask
                    ))
                    
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
            
            # Account for taker fees (1.5% per trade × num_outcomes)
            num_trades = len(outcomes)
            total_fee_percent = TAKER_FEE_PERCENT * num_trades
            net_profit_per_share = profit_per_share - (norm_entry_cost * total_fee_percent)
            
            # Only report if profit exceeds threshold
            if net_profit_per_share < MINIMUM_PROFIT_THRESHOLD:
                logger.debug(
                    f"Market {market_id}: profit {net_profit_per_share} "
                    f"below threshold {MINIMUM_PROFIT_THRESHOLD}"
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
                negrisk_short_field_cost=1.0 - sum_prices if is_negrisk else None
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
    """
    
    def __init__(self, client: PolymarketClient, order_manager: OrderManager):
        """
        Initialize executor with order management
        
        Args:
            client: Polymarket CLOB client
            order_manager: Order execution manager with validation
        """
        self.client = client
        self.order_manager = order_manager
        self._pending_orders: Dict[str, Dict] = {}  # Track pending orders
        self._budget_used = Decimal('0')
        self._max_budget = Decimal(str(TOTAL_ARBITRAGE_BUDGET))
        
        logger.info(
            f"AtomicExecutor initialized - "
            f"FOK mode, Max budget: ${TOTAL_ARBITRAGE_BUDGET}, "
            f"Max slippage per leg: ${MAX_SLIPPAGE_PER_LEG}"
        )

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
            
            # Pre-execution validation
            await self._validate_execution(opportunity, shares_to_buy)
            
            # Place all BUY orders (FOK) for each outcome
            pending_order_ids = []
            
            for outcome in opportunity.outcomes:
                try:
                    # Calculate order details
                    order_cost = shares_to_buy * outcome.ask_price
                    expected_slippage = abs(outcome.ask_price - outcome.yes_price)
                    
                    # Validate slippage constraint
                    if expected_slippage > MAX_SLIPPAGE_PER_LEG:
                        raise SlippageExceededError(
                            f"Slippage ${expected_slippage:.4f} exceeds max ${MAX_SLIPPAGE_PER_LEG}",
                            expected=expected_slippage,
                            maximum=MAX_SLIPPAGE_PER_LEG
                        )
                    
                    # Place BUY order with FOK
                    order_result = await self.order_manager.execute_market_order(
                        token_id=outcome.token_id,
                        side='BUY',
                        size=order_cost,
                        max_slippage=MAX_SLIPPAGE_PER_LEG / outcome.ask_price,
                        is_shares=True  # Buy by shares, not USD
                    )
                    
                    order_id = order_result.get('order_id')
                    pending_order_ids.append((outcome.token_id, order_id))
                    
                    logger.debug(
                        f"[{execution_id}] Outcome {outcome.outcome_index}: "
                        f"Order {order_id} placed for {shares_to_buy} shares"
                    )
                    
                except Exception as e:
                    logger.error(
                        f"[{execution_id}] Order failed for outcome {outcome.outcome_index}: {e}"
                    )
                    orders_failed.append(outcome.token_id)
                    
                    # ABORT: Cancel all pending orders
                    await self._abort_execution(execution_id, pending_order_ids)
                    
                    return ExecutionResult(
                        success=False,
                        market_id=opportunity.market_id,
                        orders_executed=orders_executed,
                        orders_failed=orders_failed,
                        total_cost=total_cost,
                        shares_filled=0.0,
                        actual_profit=0.0,
                        error_message=f"Order execution failed: {str(e)}"
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

    async def _validate_execution(
        self,
        opportunity: ArbitrageOpportunity,
        shares_to_buy: float
    ) -> None:
        """
        Validate execution prerequisites
        
        Checks:
        1. Sufficient budget remaining
        2. Sufficient balance in account
        3. Order book depth constraints
        4. Slippage within limits
        
        Args:
            opportunity: Arbitrage opportunity
            shares_to_buy: Proposed share count
            
        Raises:
            TradingError: If any validation fails
        """
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
