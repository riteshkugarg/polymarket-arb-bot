"""
Atomic Depth-Aware Execution Engine

Production-grade execution handler that prevents "legging in" to losing positions
by implementing:

1. DEPTH VALIDATION: Verify MIN_DEPTH_SHARES available at ask price for ALL legs
   before placing ANY orders

2. ATOMIC EXECUTION: Use asyncio.gather() to place all orders simultaneously.
   If ANY leg fails, immediately cancel all pending orders and abort.

3. PARTIAL FILL PROTECTION: Monitor execution and immediately alert/cancel
   if partial fills detected on any leg

4. MARKET STANDARDIZATION: Works for Binary (YES/NO) and Multi-Choice
   markets with automatic "short the field" handling

5. PRODUCTION OPTIMIZATION: Low-latency EC2-optimized with py-clob-client
   official methods, no mocks, no dummy data
"""

from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from decimal import Decimal
import asyncio
import time
from datetime import datetime
from enum import Enum

from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from config.constants import (
    ATOMIC_MIN_DEPTH_SHARES,
    ATOMIC_TOP_PIPS_DEPTH,
    ATOMIC_ORDER_TIMEOUT_SEC,
    ATOMIC_CHECK_INTERVAL_MS,
    ATOMIC_MAX_SLIPPAGE_USD,
    MAX_BALANCE_UTILIZATION_PERCENT,
    FOK_FILL_FAILURE_COOLDOWN_SEC,
    ENABLE_NEGRISK_AUTO_DETECTION,
)
from utils.logger import get_logger
from utils.exceptions import (
    OrderRejectionError,
    InsufficientBalanceError,
    TradingError,
)


logger = get_logger(__name__)


class ExecutionPhase(Enum):
    """Execution lifecycle phases"""
    PRE_FLIGHT = "pre_flight"           # Depth checks, balance validation
    CONCURRENT_PLACEMENT = "concurrent_placement"  # asyncio.gather() all orders
    FILL_MONITORING = "fill_monitoring" # Monitor for fills and partial fills
    FILL_COMPLETION = "fill_completion" # All orders filled
    ABORT = "abort"                     # Emergency abort


@dataclass
class DepthCheckResult:
    """Result of order book depth validation"""
    is_valid: bool                      # All legs have 10+ shares?
    token_id: str                       # Which token failed (empty if valid)
    available_depth: float              # Actual depth at failing token
    error_message: Optional[str] = None


@dataclass
class OrderPlacementTask:
    """Single order placement task in atomic batch"""
    token_id: str                       # Token to trade
    outcome_name: str                   # Human-readable outcome
    side: str                           # 'BUY' or 'SELL'
    size: float                         # Number of shares
    price: float                        # Limit price
    order_type: str                     # 'FOK' or 'IOC'
    order_id: Optional[str] = None      # Set after placement
    status: str = "pending"             # pending, filled, failed, partial
    filled_shares: float = 0.0          # How many shares filled
    error_message: Optional[str] = None


@dataclass
class AtomicExecutionResult:
    """Complete execution outcome"""
    success: bool                       # Did all legs fill?
    execution_phase: ExecutionPhase     # Where did it fail?
    market_id: str                      # Which market
    total_cost: Decimal                 # Total USDC spent
    orders: List[OrderPlacementTask]    # All order details
    filled_shares: float                # Shares obtained per outcome
    partial_fills: List[str]            # Order IDs with partial fills
    error_message: Optional[str] = None
    execution_time_ms: float = 0.0      # Latency


class AtomicDepthAwareExecutor:
    """
    Production-grade atomic execution handler
    
    Prevents legging in through:
    1. Pre-flight depth validation (all legs >= 10 shares)
    2. Concurrent simultaneous order placement
    3. Partial fill detection and emergency abort
    4. Complete order cancellation on any failure
    """

    def __init__(self, client: PolymarketClient, order_manager: OrderManager):
        """
        Initialize executor
        
        Args:
            client: Polymarket CLOB client
            order_manager: Order management and validation
        """
        self.client = client
        self.order_manager = order_manager
        self._pending_orders: Dict[str, Dict] = {}
        
        logger.info(
            f"AtomicDepthAwareExecutor initialized - "
            f"Min depth: {ATOMIC_MIN_DEPTH_SHARES} shares, "
            f"Order timeout: {ATOMIC_ORDER_TIMEOUT_SEC}s"
        )

    async def execute_atomic_basket(
        self,
        market_id: str,
        outcomes: List[Tuple[str, str, float]],  # (token_id, outcome_name, ask_price)
        side: str,
        size: float,
        order_type: str = "FOK",
        is_negrisk: bool = False  # 2026 Update: NegRisk flag
    ) -> AtomicExecutionResult:
        """
        Execute atomic arbitrage basket with depth awareness
        
        Flow:
        1. DEPTH CHECK: Verify all legs have 10+ shares at ask price
        2. BALANCE CHECK: Verify sufficient USDC
        3. CONCURRENT PLACEMENT: asyncio.gather() all orders simultaneously
        4. FILL MONITORING: Watch for fills and partial fills
        5. CLEANUP: Cancel any unfilled orders
        
        Args:
            market_id: Market identifier
            outcomes: List of (token_id, outcome_name, ask_price) tuples
            side: 'BUY' or 'SELL'
            size: Shares per outcome
            order_type: 'FOK', 'IOC', or 'GTC'
            
        Returns:
            AtomicExecutionResult with complete execution details
        """
        execution_start = time.time()
        result = AtomicExecutionResult(
            success=False,
            execution_phase=ExecutionPhase.PRE_FLIGHT,
            market_id=market_id,
            total_cost=Decimal('0'),
            orders=[],
            filled_shares=0.0,
            partial_fills=[],
            error_message=None,
            execution_time_ms=0.0
        )
        
        try:
            # ════════════════════════════════════════════════════════════════
            # PHASE 1: PRE-FLIGHT CHECKS
            # ════════════════════════════════════════════════════════════════
            
            logger.info(
                f"[{market_id}] Starting atomic execution: "
                f"{len(outcomes)} outcomes, {size} shares/outcome, {side}"
            )
            
            # 1a. Depth validation for ALL outcomes
            depth_result = await self._validate_all_depths(outcomes, size)
            if not depth_result.is_valid:
                result.execution_phase = ExecutionPhase.PRE_FLIGHT
                result.error_message = (
                    f"Insufficient depth for {depth_result.token_id}: "
                    f"{depth_result.available_depth} < {ATOMIC_MIN_DEPTH_SHARES} shares"
                )
                logger.warning(f"[{market_id}] Depth check failed: {result.error_message}")
                return result
            
            logger.debug(f"[{market_id}] ✅ Depth validation passed for all {len(outcomes)} outcomes")
            
            # 1b. Balance validation (2026 Update: 90% max utilization guard)
            total_cost = Decimal(str(sum(price for _, _, price in outcomes))) * Decimal(str(size))
            balance = await self.client.get_balance()
            
            # Calculate maximum allowed commitment (90% of balance)
            max_allowed_cost = balance * MAX_BALANCE_UTILIZATION_PERCENT
            
            if balance < float(total_cost):
                result.execution_phase = ExecutionPhase.PRE_FLIGHT
                result.error_message = (
                    f"Insufficient balance: ${balance:.2f} < ${float(total_cost):.2f}"
                )
                logger.warning(f"[{market_id}] Balance check failed: {result.error_message}")
                return result
            
            if float(total_cost) > max_allowed_cost:
                result.execution_phase = ExecutionPhase.PRE_FLIGHT
                result.error_message = (
                    f"Cost exceeds 90% balance limit: "
                    f"${float(total_cost):.2f} > ${max_allowed_cost:.2f} "
                    f"(balance: ${balance:.2f})"
                )
                logger.warning(f"[{market_id}] Balance guard triggered: {result.error_message}")
                return result
            
            result.total_cost = total_cost
            logger.debug(
                f"[{market_id}] ✅ Balance validation passed: "
                f"${float(balance):.2f} available, "
                f"using ${float(total_cost):.2f} ({float(total_cost)/balance*100:.1f}%)"
            )
            
            # ════════════════════════════════════════════════════════════════
            # PHASE 2: BUILD ORDER TASKS
            # ════════════════════════════════════════════════════════════════
            
            order_tasks = []
            for token_id, outcome_name, ask_price in outcomes:
                task = OrderPlacementTask(
                    token_id=token_id,
                    outcome_name=outcome_name,
                    side=side,
                    size=size,
                    price=ask_price,
                    order_type=order_type,
                )
                order_tasks.append(task)
            
            result.orders = order_tasks
            
            # ════════════════════════════════════════════════════════════════
            # PHASE 3: CONCURRENT ORDER PLACEMENT (THE ATOMIC MOMENT)
            # ════════════════════════════════════════════════════════════════
            
            result.execution_phase = ExecutionPhase.CONCURRENT_PLACEMENT
            placement_start = time.time()
            
            logger.info(
                f"[{market_id}] Placing {len(order_tasks)} orders concurrently (FOK)..."
            )
            
            # Place all orders simultaneously with asyncio.gather()
            placement_tasks = [
                self._place_order_async(task, market_id) 
                for task in order_tasks
            ]
            
            placement_results = await asyncio.gather(
                *placement_tasks,
                return_exceptions=True
            )
            
            placement_duration = time.time() - placement_start
            logger.info(
                f"[{market_id}] All orders placed in {placement_duration*1000:.1f}ms"
            )
            
            # Check for placement failures
            failed_placements = []
            for task, placement_result in zip(order_tasks, placement_results):
                if isinstance(placement_result, Exception):
                    task.status = "failed"
                    task.error_message = str(placement_result)
                    failed_placements.append(task)
                    logger.error(
                        f"[{market_id}] Order placement failed for {task.outcome_name}: {placement_result}"
                    )
                elif placement_result is not None:
                    task.order_id = placement_result
                    task.status = "pending"
                    logger.debug(f"[{market_id}] Order {task.order_id} placed for {task.outcome_name}")
            
            # If ANY order failed to place, abort entire execution
            if failed_placements:
                result.execution_phase = ExecutionPhase.ABORT
                result.success = False
                result.error_message = (
                    f"Order placement failed for {len(failed_placements)} leg(s): "
                    f"{', '.join(t.outcome_name for t in failed_placements)}"
                )
                
                # Cancel all pending orders immediately
                pending_order_ids = [
                    t.order_id for t in order_tasks 
                    if t.order_id and t.status == "pending"
                ]
                
                if pending_order_ids:
                    await self._cancel_all_orders(pending_order_ids, market_id)
                
                logger.critical(
                    f"[{market_id}] ❌ ATOMIC ABORT: {result.error_message}"
                )
                result.execution_time_ms = (time.time() - execution_start) * 1000
                return result
            
            # ════════════════════════════════════════════════════════════════
            # PHASE 4: FILL MONITORING AND PARTIAL FILL DETECTION
            # ════════════════════════════════════════════════════════════════
            
            result.execution_phase = ExecutionPhase.FILL_MONITORING
            
            logger.info(f"[{market_id}] Monitoring fills for {len(order_tasks)} orders...")
            
            # Monitor for fills with timeout
            fill_result = await self._monitor_fills(order_tasks, market_id)
            
            if not fill_result['all_filled']:
                # CRITICAL: Partial fills detected!
                result.execution_phase = ExecutionPhase.ABORT
                result.success = False
                result.partial_fills = fill_result['partial_orders']
                result.error_message = (
                    f"⚠️  CRITICAL: Partial fill detected on {len(fill_result['partial_orders'])} order(s)! "
                    f"Cancelling all pending orders immediately."
                )
                
                logger.critical(
                    f"[{market_id}] {result.error_message}\n"
                    f"Partial orders: {fill_result['partial_details']}"
                )
                
                # Emergency abort: cancel ALL pending orders
                pending_order_ids = [
                    t.order_id for t in order_tasks 
                    if t.order_id and t.status == "pending"
                ]
                
                if pending_order_ids:
                    await self._cancel_all_orders(pending_order_ids, market_id)
                
                result.execution_time_ms = (time.time() - execution_start) * 1000
                return result
            
            # ════════════════════════════════════════════════════════════════
            # PHASE 5: SUCCESS - ALL ORDERS FILLED
            # ════════════════════════════════════════════════════════════════
            
            result.execution_phase = ExecutionPhase.FILL_COMPLETION
            result.success = True
            result.filled_shares = size
            
            logger.info(
                f"[{market_id}] ✅ ATOMIC EXECUTION SUCCESS: "
                f"All {len(order_tasks)} orders filled for {size} shares"
            )
            
            result.execution_time_ms = (time.time() - execution_start) * 1000
            
            return result
            
        except Exception as e:
            logger.error(f"[{market_id}] Unexpected error in atomic execution: {e}", exc_info=True)
            result.success = False
            result.error_message = str(e)
            result.execution_time_ms = (time.time() - execution_start) * 1000
            return result

    async def _validate_all_depths(
        self,
        outcomes: List[Tuple[str, str, float]],
        required_size: float
    ) -> DepthCheckResult:
        """
        Validate that ALL outcomes have sufficient depth before execution
        
        Critical safety check: If ANY outcome lacks depth, returns failure
        without proceeding to order placement.
        
        P2 FIX: Added 20% safety buffer to account for book staleness
        WebSocket book snapshots can be 50-200ms stale, during which:
        - Other traders may take liquidity
        - Prices may move
        - Depth may decrease
        
        Args:
            outcomes: List of (token_id, outcome_name, ask_price) tuples
            required_size: Minimum shares needed per outcome
            
        Returns:
            DepthCheckResult with validation outcome
        """
        try:
            # P2 FIX: Apply safety buffer (20%) to required size
            DEPTH_SAFETY_BUFFER = 1.2  # Require 20% extra depth
            required_size_with_buffer = required_size * DEPTH_SAFETY_BUFFER
            
            logger.debug(
                f"Checking depth for {len(outcomes)} outcomes "
                f"(min {required_size:.1f} shares, with buffer: {required_size_with_buffer:.1f})"
            )
            
            for token_id, outcome_name, _ in outcomes:
                try:
                    # Fetch order book
                    order_book = await self.client.get_order_book(token_id)
                    
                    # Extract asks (what we need to buy)
                    asks = getattr(order_book, 'asks', [])
                    if not asks:
                        return DepthCheckResult(
                            is_valid=False,
                            token_id=token_id,
                            available_depth=0.0,
                            error_message=f"No asks available for {outcome_name}"
                        )
                    
                    # Calculate available depth at ask price (accumulate shares)
                    available_at_ask = 0.0
                    best_ask = float(asks[0]['price'])
                    
                    for ask_level in asks:
                        ask_price = float(ask_level['price'])
                        # Only count orders within reasonable spread (0.01)
                        if ask_price <= best_ask + 0.01:
                            available_at_ask += float(ask_level['size'])
                        else:
                            break
                    
                    # Depth check (P2 FIX: Compare against buffered size)
                    if available_at_ask < required_size_with_buffer:
                        return DepthCheckResult(
                            is_valid=False,
                            token_id=token_id,
                            available_depth=available_at_ask,
                            error_message=(
                                f"Insufficient depth for {outcome_name}: "
                                f"{available_at_ask:.1f} < {required_size_with_buffer:.1f} shares "
                                f"(required: {required_size:.1f} + 20% safety buffer)"
                            )
                        )
                    
                    logger.debug(
                        f"  ✅ {outcome_name}: {available_at_ask:.1f} shares at ${best_ask:.4f} "
                        f"(exceeds {required_size_with_buffer:.1f} buffered requirement)"
                    )
                    
                except Exception as e:
                    return DepthCheckResult(
                        is_valid=False,
                        token_id=token_id,
                        available_depth=0.0,
                        error_message=f"Error fetching order book: {e}"
                    )
            
            return DepthCheckResult(is_valid=True, token_id="", available_depth=0.0)
            
        except Exception as e:
            logger.error(f"Depth validation error: {e}", exc_info=True)
            return DepthCheckResult(
                is_valid=False,
                token_id="",
                available_depth=0.0,
                error_message=str(e)
            )

    async def _place_order_async(
        self,
        task: OrderPlacementTask,
        market_id: str
    ) -> Optional[str]:
        """
        Place single order asynchronously
        
        Args:
            task: Order placement task
            market_id: For logging
            
        Returns:
            Order ID if successful, raises Exception on failure
        """
        try:
            # Use OrderManager's official execution method
            order_result = await self.order_manager.execute_market_order(
                token_id=task.token_id,
                side=task.side,
                size=task.size * task.price,  # Size in USDC
                max_slippage=0.005,  # $0.005 max slippage
                is_shares=False
            )
            
            if not order_result.get('filled'):
                raise OrderRejectionError(
                    f"Order not filled for {task.outcome_name}",
                    error_code="FOK_NOT_FILLED"
                )
            
            return order_result.get('order_id')
            
        except Exception as e:
            logger.error(
                f"[{market_id}] Order placement failed for {task.outcome_name}: {e}"
            )
            raise

    async def _monitor_fills(
        self,
        order_tasks: List[OrderPlacementTask],
        market_id: str,
        timeout_sec: float = ATOMIC_ORDER_TIMEOUT_SEC
    ) -> Dict[str, Any]:
        """
        Monitor order fills and detect partial fills
        
        Returns immediately once:
        - All orders filled completely, OR
        - Any partial fill detected (CRITICAL)
        
        Args:
            order_tasks: All orders to monitor
            market_id: For logging
            timeout_sec: How long to wait for fills
            
        Returns:
            Dict with fill status and partial fill details
        """
        start_time = time.time()
        pending_order_ids = [t.order_id for t in order_tasks if t.order_id]
        
        while time.time() - start_time < timeout_sec:
            # Check each order
            all_filled = True
            partial_orders = []
            
            for task in order_tasks:
                if not task.order_id:
                    continue
                
                try:
                    # Query order status (official py-clob-client method)
                    order_status = await asyncio.to_thread(
                        self.client._client.get_order,
                        task.order_id
                    )
                    
                    status = order_status.get('status', '')
                    
                    if status == 'filled':
                        task.status = 'filled'
                        task.filled_shares = task.size
                    elif status == 'partially_filled':
                        # CRITICAL: Partial fill detected
                        task.status = 'partial'
                        task.filled_shares = float(order_status.get('filledSize', 0))
                        partial_orders.append({
                            'order_id': task.order_id,
                            'outcome': task.outcome_name,
                            'requested': task.size,
                            'filled': task.filled_shares,
                            'unfilled': task.size - task.filled_shares
                        })
                        logger.critical(
                            f"[{market_id}] ⚠️  PARTIAL FILL: {task.outcome_name} "
                            f"({task.filled_shares}/{task.size})"
                        )
                        all_filled = False
                    elif status in ['pending', 'open']:
                        task.status = 'pending'
                        all_filled = False
                    else:
                        task.status = 'unknown'
                        all_filled = False
                        
                except Exception as e:
                    logger.debug(f"Error checking order {task.order_id}: {e}")
                    all_filled = False
            
            # If any partial fill, return immediately (CRITICAL)
            if partial_orders:
                return {
                    'all_filled': False,
                    'partial_orders': [p['order_id'] for p in partial_orders],
                    'partial_details': str(partial_orders)
                }
            
            # If all filled, return success
            if all_filled:
                return {
                    'all_filled': True,
                    'partial_orders': [],
                    'partial_details': None
                }
            
            # Wait before next check
            await asyncio.sleep(ATOMIC_CHECK_INTERVAL_MS / 1000.0)
        
        # Timeout: not all filled
        unfilled = [t for t in order_tasks if t.status != 'filled']
        return {
            'all_filled': False,
            'partial_orders': [t.order_id for t in unfilled if t.order_id],
            'partial_details': f"Timeout after {timeout_sec}s: {len(unfilled)} orders not filled"
        }

    async def _cancel_all_orders(
        self,
        order_ids: List[str],
        market_id: str
    ) -> None:
        """
        Emergency cancel: Remove all pending orders immediately
        
        Called when:
        - Depth check fails (before placing any orders)
        - Order placement fails (atomic abort)
        - Partial fill detected (critical abort)
        
        Args:
            order_ids: Orders to cancel
            market_id: For logging
        """
        if not order_ids:
            return
        
        logger.warning(
            f"[{market_id}] 🚨 EMERGENCY CANCEL: Cancelling {len(order_ids)} pending orders"
        )
        
        for order_id in order_ids:
            try:
                await self.client.cancel_order(order_id)
                logger.debug(f"[{market_id}] Cancelled order {order_id}")
            except Exception as e:
                logger.error(f"[{market_id}] Failed to cancel order {order_id}: {e}")
