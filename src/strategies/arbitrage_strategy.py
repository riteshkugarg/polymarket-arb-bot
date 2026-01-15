"""
Arbitrage Strategy - Integration with Base Strategy Framework

Orchestrates the ArbScanner and AtomicExecutor to run as a continuous strategy
alongside the existing mirror trading flow.

Architecture:
═════════════
FLOW: ARB SCANNING (Frequent - every 3 seconds)
────────────────────────────────────────────────
1. Scan markets for multi-outcome arbitrage opportunities
   - Fetch active markets from Polymarket API
   - Check if sum(outcome_prices) < 0.98
   - Validate order book depth (min 10 shares per outcome)

2. For each opportunity found:
   - Validate budget constraints ($100 total cap)
   - Calculate maximum profit potential
   - Check if net profit > $0.001 (threshold)

3. Execute atomically or defer:
   - If opportunity meets profit threshold: Execute with FOK logic
   - If execution fails: Abort entire basket, no partial fills
   - Update budget tracking and continue scanning

Safety:
───────
- Atomic execution: All legs fill or none
- Budget management: Never exceed $100 total
- Slippage limits: Max $0.005 per outcome
- Circuit breaker: Pause on consecutive failures
- NegRisk handling: Normalize inverse market pricing

Integration:
─────────────
- Runs independently of mirror strategy
- Uses same PolymarketClient and OrderManager
- Respects global budget constraints
- Logs to same logging system
- Can be toggled on/off via config flag
"""

from typing import Dict, Any, Optional, List, Set
import asyncio
from datetime import datetime
from decimal import Decimal

from strategies.base_strategy import BaseStrategy
from strategies.arb_scanner import ArbScanner, AtomicExecutor, ArbitrageOpportunity
from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from core.atomic_depth_aware_executor import AtomicDepthAwareExecutor, ExecutionPhase
from core.market_data_manager import MarketDataManager, FillEvent
from config.constants import (
    PROXY_WALLET_ADDRESS,
    API_TIMEOUT_SEC,
    MAX_RETRIES,
)
from utils.logger import get_logger, log_trade_event
from utils.exceptions import StrategyError


logger = get_logger(__name__)


# Configuration constants
# FIX 2: Scan Latency - Current 3s polling is too slow for 2026 HFT environment
# RECOMMENDED: Replace with WebSocket push architecture (listen to CLOB book channel)
#              and trigger arb check on price updates only, not on blind timer.
# INSTITUTIONAL UPGRADE: Reduced to 0.5s for faster opportunity detection
# Previous: 1s (too slow for competitive 2026 environment)
ARB_SCAN_INTERVAL_SEC = 0.5  # How often to scan for opportunities (INTERIM - switch to WS)
ARB_EXECUTION_COOLDOWN_SEC = 5  # Minimum time between executions
ARB_MAX_CONSECUTIVE_FAILURES = 3  # Circuit breaker threshold (system errors only)
# INSTITUTIONAL UPGRADE: Increased from 50 to 200 markets per scan
# Rationale: With 300+ active markets, 50-market limit creates blind spots
ARB_OPPORTUNITY_REFRESH_LIMIT = 200  # Max markets to scan per iteration


class ArbitrageStrategy(BaseStrategy):
    """
    Arbitrage Trading Strategy - Detects & executes multi-outcome opportunities
    
    Three-part execution model:
    1. Continuous scanning for sum(prices) < 0.98 opportunities
    2. Atomic execution with FOK logic (all legs or nothing)
    3. Budget management across all arb trades
    
    Independent from mirror strategy - runs in parallel.
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        config: Optional[Dict[str, Any]] = None,
        atomic_executor: Optional[AtomicDepthAwareExecutor] = None,
        market_data_manager: Optional[MarketDataManager] = None,
        execution_gateway: Optional[Any] = None,  # CRITICAL FIX #1: Centralized routing
        inventory_manager: Optional[Any] = None,  # AUDIT FIX: Unified position tracking
        risk_controller: Optional[Any] = None  # AUDIT FIX: Circuit breaker authority
    ):
        """
        Initialize arbitrage strategy
        
        Args:
            client: Polymarket CLOB client
            order_manager: Order execution manager
            config: Optional configuration overrides
            atomic_executor: Optional AtomicDepthAwareExecutor for depth-aware execution
            market_data_manager: WebSocket market data manager
            execution_gateway: Centralized order routing with STP
            inventory_manager: Unified inventory tracking authority
            risk_controller: Risk management and circuit breaker
        """
        super().__init__(client, order_manager, config)
        
        # Market data manager for real-time WebSocket data
        self._market_data_manager = market_data_manager
        
        # Risk management (AUDIT FIX)
        self._inventory_manager = inventory_manager
        self._risk_controller = risk_controller
        
        # Pass market_data_manager to scanner for cache access
        self.scanner = ArbScanner(client, order_manager, market_data_manager=market_data_manager)
        self.executor = AtomicExecutor(client, order_manager)
        self.atomic_executor = atomic_executor or AtomicDepthAwareExecutor(client, order_manager)
        self.use_depth_aware_executor = atomic_executor is not None
        
        # Strategy state (use is_running from BaseStrategy)
        # self.is_running is already set by BaseStrategy.__init__()
        self._consecutive_failures = 0
        self._circuit_breaker_active = False
        self._last_execution_time = 0
        self._executed_opportunities: Dict[str, float] = {}  # market_id -> timestamp
        
        # Event-driven architecture state
        self._arb_eligible_markets: Set[str] = set()  # Asset IDs that are arb-eligible
        self._arb_eligible_events: List[Dict[str, Any]] = []  # Multi-outcome events for arbitrage
        self._pending_scan = False  # Debounce flag to prevent duplicate scans
        self._scan_lock = asyncio.Lock()
        self._market_making_strategy: Optional[Any] = None  # Reference for cross-strategy coordination
        
        # Metrics
        self._total_arb_executions = 0
        self._successful_executions = 0
        self._failed_executions = 0
        self._total_profit = Decimal('0')
        
        logger.info(
            f"ArbitrageStrategy initialized - "
            f"Scan interval: {ARB_SCAN_INTERVAL_SEC}s, "
            f"Execution cooldown: {ARB_EXECUTION_COOLDOWN_SEC}s"
        )
        
        # Register fill handler if WebSocket manager available
        if self._market_data_manager:
            self._market_data_manager.register_fill_handler(
                'arbitrage',
                self.handle_fill_event
            )
            logger.info("✅ Registered for real-time fill events via WebSocket")
    
    async def handle_fill_event(self, fill: FillEvent) -> None:
        """
        Handle real-time fill event from WebSocket
        
        Called by MarketDataManager when /user channel receives a fill.
        Logs profit/loss and recycles capital immediately.
        """
        try:
            logger.info(
                f"[ARB Fill] {fill.side} {fill.size:.1f} @ {fill.price:.4f} "
                f"(order: {fill.order_id[:8]}..., market: {fill.market_id[:8] if fill.market_id else 'unknown'}...)"
            )
            
            # Log trade event for P&L tracking
            log_trade_event(
                event_type='ARBITRAGE_FILL',
                market_id=fill.market_id or 'unknown',
                action=fill.side,
                token_id=fill.asset_id,
                shares=fill.size,
                price=fill.price,
                reason=f"Arbitrage leg filled - order {fill.order_id[:8]}..."
            )
            
            # Note: Capital recycling is automatic - atomic execution ensures
            # capital is freed when all legs fill (no partial fills possible)
            
        except Exception as e:
            logger.error(f"Error handling arbitrage fill event: {e}", exc_info=True)

    # ========================================================================
    # BaseStrategy Abstract Method Implementations
    # ========================================================================
    
    async def execute(self) -> None:
        """
        Main strategy execution - delegates to run() method
        Required by BaseStrategy interface
        """
        await self.run()
    
    async def analyze_opportunity(self) -> Optional[Dict[str, Any]]:
        """
        Analyze market for arbitrage opportunities
        Required by BaseStrategy interface
        
        Returns:
            Dictionary with best opportunity details or None
        """
        try:
            # Use cached events for faster scanning
            opportunities = await self.scanner.scan_events(
                events=self._arb_eligible_events,
                limit=ARB_OPPORTUNITY_REFRESH_LIMIT
            )
            
            if not opportunities:
                return None
            
            # Filter executable opportunities
            executable = [opp for opp in opportunities if self._is_opportunity_executable(opp)]
            
            if not executable:
                return None
            
            # Return best opportunity
            top_opp = executable[0]
            return {
                'action': 'BUY_ALL_OUTCOMES',
                'market_id': top_opp.market_id,
                'size': top_opp.max_shares_to_buy,
                'confidence': min(top_opp.arbitrage_profit_pct / 5.0, 1.0),  # 5% = 100% confidence
                'metadata': {
                    'sum_prices': float(top_opp.sum_prices),
                    'expected_profit': float(top_opp.expected_profit),
                    'profit_pct': float(top_opp.arbitrage_profit_pct),
                    'outcome_count': len(top_opp.outcomes)
                }
            }
        except Exception as e:
            logger.error(f"Error analyzing opportunities: {e}", exc_info=True)
            return None
    
    async def should_execute_trade(self, opportunity: Dict[str, Any]) -> bool:
        """
        Determine if opportunity should be executed
        Required by BaseStrategy interface
        
        Args:
            opportunity: Opportunity details from analyze_opportunity()
            
        Returns:
            True if should execute, False otherwise
        """
        try:
            # Check circuit breaker
            if self._circuit_breaker_active:
                return False
            
            # Check execution cooldown
            time_since_last = datetime.now().timestamp() - self._last_execution_time
            if time_since_last < ARB_EXECUTION_COOLDOWN_SEC:
                return False
            
            # Check budget
            if self._get_budget_remaining() < Decimal('10'):
                logger.warning("Insufficient budget remaining")
                return False
            
            # Check confidence threshold
            confidence = opportunity.get('confidence', 0)
            if confidence < 0.2:  # Minimum 20% confidence (1% profit)
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking trade execution: {e}", exc_info=True)
            return False

    # ========================================================================
    # Event-Driven Strategy Implementation
    # ========================================================================
    
    async def run(self) -> None:
        """
        EVENT-DRIVEN Strategy - Subscribe to price updates instead of polling
        
        New Architecture:
        1. Discover all multi-outcome markets (3+ outcomes)
        2. Subscribe to WebSocket price updates for those markets
        3. Trigger arb scan ONLY when prices change in arb-eligible markets
        4. Execute opportunities with cross-strategy coordination
        """
        if self.is_running:
            logger.warning("ArbitrageStrategy already running")
            return
        
        self.is_running = True
        logger.info("🚀 ArbitrageStrategy started (EVENT-DRIVEN MODE)")
        
        try:
            # Initialize: Discover arb-eligible markets
            await self._discover_arb_eligible_markets()
            
            # Register for price update events
            if self._market_data_manager:
                self._market_data_manager.cache.register_market_update_handler(
                    'arbitrage_scanner',
                    self._on_market_update,
                    market_filter=self._arb_eligible_markets
                )
                logger.info(
                    f"✅ Subscribed to {len(self._arb_eligible_markets)} arb-eligible markets "
                    f"(EVENT-DRIVEN - no more polling!)"
                )
            else:
                # Fallback to polling if no WebSocket manager
                logger.warning("No MarketDataManager - falling back to polling mode")
                while self.is_running:
                    try:
                        await self._arb_scan_loop()
                        await asyncio.sleep(ARB_SCAN_INTERVAL_SEC)
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        logger.error(f"Polling mode error: {e}")
                        await asyncio.sleep(5)
                return
            
            # Keep strategy alive (event handlers run in background)
            while self.is_running:
                await asyncio.sleep(1)
                
                # Periodic health check
                if self._circuit_breaker_active:
                    logger.warning("Circuit breaker active - strategy paused")
                    await asyncio.sleep(30)
                    
        except asyncio.CancelledError:
            logger.info("ArbitrageStrategy cancelled")
        finally:
            # Cleanup
            if self._market_data_manager:
                self._market_data_manager.cache.unregister_market_update_handler('arbitrage_scanner')
            self.is_running = False
            logger.info("🛑 ArbitrageStrategy stopped")

    async def stop(self) -> None:
        """Stop the strategy loop"""
        self.is_running = False
        logger.info("Stopping ArbitrageStrategy")
    
    def set_market_making_strategy(self, mm_strategy: Any) -> None:
        """Set reference to MarketMakingStrategy for cross-strategy coordination"""
        self._market_making_strategy = mm_strategy
        logger.info("✅ Cross-strategy coordination enabled with MarketMakingStrategy")
    
    async def _discover_arb_eligible_markets(self) -> None:
        """
        Discover multi-outcome arbitrage opportunities using Events API
        
        Per Polymarket Support (Jan 2026):
        - Multi-outcome markets are structured as EVENTS containing multiple binary MARKETS
        - Each market in an event is binary (Yes/No), prices sum to $1 within each market
        - Arbitrage opportunity: sum(YES prices across all markets in event) < $1.00
        - Use /events endpoint, not /markets
        - Filter by outcomes array length (>= 3 for multi-outcome)
        - Handle NegRisk events (winner-take-all, can have augmented placeholders)
        
        Example Event Structure:
            Event: "2024 US Presidential Election"
            Markets: [
                {outcome: "Trump", price: $0.45},
                {outcome: "Biden", price: $0.35},
                {outcome: "Harris", price: $0.15}
            ]
            Sum of YES prices: $0.45 + $0.35 + $0.15 = $0.95
            Arbitrage profit: $1.00 - $0.95 = $0.05 per share
        """
        try:
            logger.info("Discovering multi-outcome arbitrage events...")
            
            # Fetch events with pagination (per Polymarket support, must specify limit)
            all_events = []
            offset = 0
            limit = 100
            
            while True:
                response = await self.client.get_events(
                    limit=limit,
                    offset=offset,
                    closed=False,  # Only active events
                    active=True
                )
                
                events = response.get('data', [])
                if not events:
                    break  # No more events
                
                all_events.extend(events)
                offset += limit
                
                # DISCOVERY MODE: Scan up to 2000 events (was 500 - too low)
                # Multi-outcome events may be rare - need larger sample
                if len(all_events) >= 2000:
                    logger.info(f"Reached 2000 event limit - stopping discovery")
                    break
            
            logger.debug(f"Fetched {len(all_events)} total events from Gamma API")
            
            # DEBUG: Analyze outcome count distribution
            outcome_distribution = {}
            for event in all_events:
                outcomes = event.get('outcomes', [])
                count = len(outcomes)
                outcome_distribution[count] = outcome_distribution.get(count, 0) + 1
            
            logger.info(
                f"📊 OUTCOME DISTRIBUTION ({len(all_events)} events):\n"
                f"   2 outcomes (binary): {outcome_distribution.get(2, 0)}\n"
                f"   3 outcomes: {outcome_distribution.get(3, 0)}\n"
                f"   4+ outcomes: {sum(v for k, v in outcome_distribution.items() if k >= 4)}\n"
                f"   Full breakdown: {dict(sorted(outcome_distribution.items()))}"
            )
            
            # Sample first event to understand structure (debugging)
            if all_events:
                sample = all_events[0]
                logger.debug(
                    f"Sample event structure:\n"
                    f"  - outcomes: {len(sample.get('outcomes', []))}\n"
                    f"  - markets: {len(sample.get('markets', []))}\n"
                    f"  - clobTokenIds: {len(sample.get('clobTokenIds', []))}\n"
                    f"  - negRisk: {sample.get('negRisk', False)}\n"
                    f"  - keys: {list(sample.keys())[:15]}"
                )
            
            # Filter for multi-outcome events (3+ outcomes)
            multi_outcome_events = []
            rejection_stats = {'binary': 0, 'negrisk_placeholders': 0}
            
            for event in all_events:
                outcomes = event.get('outcomes', [])
                
                # Skip binary events (only 2 outcomes)
                if len(outcomes) < 3:
                    rejection_stats['binary'] += 1
                    continue
                
                # Skip NegRisk augmented events with unnamed placeholders
                # Per Polymarket support: Only trade named outcomes
                if event.get('negRisk', False):
                    # Check if any outcomes are unnamed/placeholders
                    named_outcomes = [o for o in outcomes if o and len(o) > 0]
                    if len(named_outcomes) < len(outcomes):
                        rejection_stats['negrisk_placeholders'] += 1
                        logger.debug(
                            f"Skipping NegRisk event {event.get('id')} with unnamed placeholders"
                        )
                        continue
                
                multi_outcome_events.append(event)
                
                # Subscribe to all token IDs in this event
                token_ids = event.get('clobTokenIds', [])
                for token_id in token_ids:
                    self._arb_eligible_markets.add(token_id)
            
            logger.info(
                f"🔍 FILTERING ANALYSIS:\n"
                f"   ❌ Rejected (binary <3 outcomes): {rejection_stats['binary']}\n"
                f"   ❌ Rejected (NegRisk placeholders): {rejection_stats['negrisk_placeholders']}\n"
                f"   ✅ PASSED FILTER: {len(multi_outcome_events)} events\n"
                f"   ✅ Total assets: {len(self._arb_eligible_markets)}"
            )
            
            # FALLBACK: If no multi-outcome events found, log guidance
            if len(multi_outcome_events) == 0:
                logger.warning(
                    f"⚠️  NO MULTI-OUTCOME EVENTS FOUND (scanned {len(all_events)} events)\n"
                    f"   Per Polymarket Support: Most markets are binary (2 outcomes)\n"
                    f"   Binary arbitrage is IMPOSSIBLE by design (YES + NO = $1.00)\n"
                    f"   \n"
                    f"   RECOMMENDATION: Focus on Market Making strategy\n"
                    f"   Or consider cross-exchange arbitrage (Polymarket ↔ Kalshi/Manifold)"
                )
            
            logger.info(
                f"Discovered {len(self._arb_eligible_markets)} arb-eligible assets "
                f"across {len(multi_outcome_events)} multi-outcome events "
                f"(out of {len(all_events)} total events)"
            )
            
            # Store events for arb scanning
            self._arb_eligible_events = multi_outcome_events
            
        except Exception as e:
            logger.error(f"Failed to discover arb-eligible markets: {e}", exc_info=True)
    
    async def _on_market_update(self, asset_id: str, snapshot: Any) -> None:
        """Handle price update event (EVENT-DRIVEN callback)
        
        Called by MarketDataManager when price changes in arb-eligible market.
        Triggers arb scan with debouncing to prevent excessive scanning.
        """
        try:
            # Debounce: Skip if scan already pending
            if self._pending_scan:
                return
            
            async with self._scan_lock:
                self._pending_scan = True
                
                # Small delay to batch multiple price updates
                await asyncio.sleep(0.1)
                
                logger.debug(
                    f"[EVENT] Price update in {asset_id[:8]}... - triggering arb scan"
                )
                
                # Trigger scan
                await self._arb_scan_loop()
                
                self._pending_scan = False
                
        except Exception as e:
            logger.error(f"Market update handler error: {e}", exc_info=True)
            self._pending_scan = False

    async def _arb_scan_loop(self) -> None:
        """
        Main arbitrage scanning loop (INSTITUTION-GRADE 2026)
        
        Per Polymarket Support:
        - Scan EVENTS for multi-outcome arbitrage (not individual markets)
        - Validate against order book depth (not just midpoint prices)
        - Execute top opportunities atomically with FOK logic
        - Cross-strategy coordination for inventory management
        
        Flow:
        1. Scan cached events for arbitrage opportunities
        2. Filter by profitability and execution readiness
        3. Prioritize by MM inventory reduction (if coordinated)
        4. Execute top opportunity atomically
        5. Update metrics and budget tracking
        """
        try:
            # Use cached arb-eligible events (discovered at startup)
            # This avoids re-fetching events on every price update
            opportunities = await self.scanner.scan_events(
                events=self._arb_eligible_events,
                limit=ARB_OPPORTUNITY_REFRESH_LIMIT
            )
            
            if not opportunities:
                return  # No opportunities this iteration
            
            # Filter for execution readiness
            executable_opps = [
                opp for opp in opportunities
                if self._is_opportunity_executable(opp)
            ]
            
            if not executable_opps:
                logger.debug(f"No executable opportunities (found {len(opportunities)} total)")
                return
            
            # CROSS-STRATEGY COORDINATION: Prioritize opportunities that reduce MM inventory
            if self._market_making_strategy:
                executable_opps = self._prioritize_by_mm_inventory(executable_opps)
            
            # Execute top opportunity
            top_opportunity = executable_opps[0]
            
            # Check execution cooldown
            time_since_last = datetime.now().timestamp() - self._last_execution_time
            if time_since_last < ARB_EXECUTION_COOLDOWN_SEC:
                logger.debug(
                    f"Execution cooldown active ({time_since_last:.1f}s / {ARB_EXECUTION_COOLDOWN_SEC}s)"
                )
                return
            
            # Calculate share count
            shares_to_buy = min(
                top_opportunity.max_shares_to_buy,
                top_opportunity.required_budget / top_opportunity.sum_prices
            )
            
            if shares_to_buy < 1.0:
                logger.debug(f"Share count too low: {shares_to_buy} < 1.0")
                return
            
            # CRITICAL FIX #1: Pause MM strategy during arb execution
            # Prevents inventory race condition and taker fee leakage
            mm_paused = False
            try:
                if self._market_making_strategy and hasattr(self._market_making_strategy, 'pause_for_arb'):
                    await self._market_making_strategy.pause_for_arb(top_opportunity.market_id)
                    mm_paused = True
                    logger.info(
                        f"⏸️  PAUSED MM on {top_opportunity.market_id[:8]}... during arb execution "
                        f"(prevents inventory race + self-trade)"
                    )
                
                # Execute using atomic depth-aware executor if available
                if self.use_depth_aware_executor:
                    logger.debug("Using AtomicDepthAwareExecutor for execution...")
                    result = await self._execute_atomic_depth_aware(top_opportunity, shares_to_buy)
                else:
                    logger.debug("Using standard AtomicExecutor for execution...")
                    result = await self.executor.execute(top_opportunity, shares_to_buy)
                
            finally:
                # CRITICAL: Always resume MM (even if arb fails)
                if mm_paused and self._market_making_strategy:
                    await self._market_making_strategy.resume_from_arb(top_opportunity.market_id)
                    logger.info(f"▶️  RESUMED MM on {top_opportunity.market_id[:8]}...")
            
            # Update metrics
            self._total_arb_executions += 1
            self._last_execution_time = datetime.now().timestamp()
            
            if result.success:
                self._successful_executions += 1
                self._total_profit += Decimal(str(result.actual_profit))
                self._consecutive_failures = 0
                
                # Log successful execution
                log_trade_event(
                    event_type="arbitrage_execution",
                    market_id=result.market_id,
                    side="BUY_ALL_OUTCOMES",
                    size=result.shares_filled,
                    price=result.total_cost / result.shares_filled,
                    cost=result.total_cost,
                    profit=result.actual_profit,
                    execution_id=f"{result.market_id}_{int(self._last_execution_time)}"
                )
                
                logger.info(
                    f"✅ Arbitrage executed: Market {result.market_id[:8]}... "
                    f"Cost: ${result.total_cost:.2f}, Profit: ${result.actual_profit:.2f}, "
                    f"Budget remaining: ${self._get_budget_remaining():.2f}"
                )
                
                # Mark as executed (prevent repeated execution)
                self._executed_opportunities[result.market_id] = self._last_execution_time
                
            else:
                self._failed_executions += 1
                self._consecutive_failures += 1
                
                logger.warning(
                    f"❌ Arbitrage execution failed: {result.error_message}"
                )
            
            # Check circuit breaker
            if self._circuit_breaker_active and self._consecutive_failures == 0:
                self._circuit_breaker_active = False
                logger.info("✅ Circuit breaker reset")
                
        except Exception as e:
            logger.error(f"Error in arb scan loop: {e}")
            self._consecutive_failures += 1

    async def _execute_atomic_depth_aware(
        self,
        opportunity: ArbitrageOpportunity,
        shares_to_buy: float
    ) -> Any:
        """
        Execute using AtomicDepthAwareExecutor with depth validation
        
        Args:
            opportunity: ArbitrageOpportunity detected by scanner
            shares_to_buy: Number of shares to buy per outcome
            
        Returns:
            Execution result object (converted from atomic executor format)
        """
        try:
            # Build outcome list for atomic executor
            outcomes = [
                (op.token_id, op.outcome_name, op.ask_price)
                for op in opportunity.outcomes
            ]
            
            logger.debug(
                f"Atomic execution: Market {opportunity.market_id[:8]}..., "
                f"Shares: {shares_to_buy}, Outcomes: {len(outcomes)}"
            )
            
            # Execute atomically with depth awareness
            result = await self.atomic_executor.execute_atomic_basket(
                market_id=opportunity.market_id,
                outcomes=outcomes,
                side="BUY",
                size=shares_to_buy,
                order_type="FOK"  # Fill-or-Kill for safety
            )
            
            # Convert atomic executor result to format compatible with existing code
            from strategies.arb_scanner import ExecutionResult
            
            if result.success:
                profit = opportunity.net_profit_per_share * result.filled_shares
                return ExecutionResult(
                    success=True,
                    market_id=opportunity.market_id,
                    orders_executed=[o.order_id for o in result.orders if o.order_id],
                    orders_failed=[],
                    total_cost=float(result.total_cost),
                    shares_filled=result.filled_shares,
                    actual_profit=profit,
                    error_message=""
                )
            else:
                # FIX 1: Post-Execution Orphan Risk - Liquidate partial fills immediately
                error_msg = f"Atomic execution failed at phase {result.execution_phase.value}"
                if result.partial_fills:
                    error_msg += f" (PARTIAL FILLS DETECTED: {len(result.partial_fills)} orders)"
                    logger.error(
                        f"⚠️  ORPHAN RISK: {len(result.partial_fills)} legs filled but basket incomplete. "
                        f"Initiating emergency liquidation..."
                    )
                    
                    # Extract filled order details from result
                    filled_legs = [
                        (order.token_id, order.order_id, idx)
                        for idx, order in enumerate(result.orders)
                        if order.order_id in result.partial_fills
                    ]
                    
                    # Call emergency liquidation to market-sell orphaned positions
                    await self._revert_positions(
                        execution_id=f"{opportunity.market_id}_{int(datetime.now().timestamp())}",
                        filled_legs=filled_legs,
                        shares=result.filled_shares
                    )
                    
                    error_msg += " → Emergency liquidation completed"
                
                return ExecutionResult(
                    success=False,
                    market_id=opportunity.market_id,
                    orders_executed=result.partial_fills,
                    orders_failed=[o.order_id for o in result.orders if o.order_id not in result.partial_fills],
                    total_cost=0,
                    shares_filled=0,
                    actual_profit=0,
                    error_message=error_msg
                )
                
        except Exception as e:
            logger.error(f"Atomic depth-aware execution error: {e}")
            return ExecutionResult(
                success=False,
                market_id=opportunity.market_id,
                total_cost=0,
                shares_filled=0,
                actual_profit=0,
                error_message=str(e)
            )

    def _is_opportunity_executable(self, opportunity: ArbitrageOpportunity) -> bool:
        """
        Check if opportunity should be executed
        
        Filters:
        1. Sufficient budget remaining
        2. Not already executed recently
        3. Circuit breaker not active
        4. ROI meets minimum threshold (0.3% = 30 basis points)
        
        Args:
            opportunity: ArbitrageOpportunity to check
            
        Returns:
            True if executable
        """
        # Check circuit breaker
        if self._circuit_breaker_active:
            return False
        
        # FIX 4: Use minimum ROI % instead of flat dollar threshold
        # Prevents locking up capital for negligible gains
        MIN_ROI_PERCENT = 0.003  # 0.3% minimum ROI (30 basis points)
        
        if opportunity.required_budget > 0:
            roi = opportunity.net_profit_per_share / opportunity.required_budget
            if roi < MIN_ROI_PERCENT:
                logger.debug(
                    f"ROI too low: {roi*100:.3f}% < {MIN_ROI_PERCENT*100:.1f}% "
                    f"(profit ${opportunity.net_profit_per_share:.4f} / budget ${opportunity.required_budget:.2f})"
                )
                return False
        else:
            # Fallback: if required_budget is 0 (edge case), use flat threshold
            if opportunity.net_profit_per_share < 0.001:
                return False
        
        # Check budget
        budget_remaining = self._get_budget_remaining()
        if opportunity.required_budget > float(budget_remaining):
            return False
        
        # Check if already executed recently (within 60 seconds)
        last_exec_time = self._executed_opportunities.get(opportunity.market_id, 0)
        if datetime.now().timestamp() - last_exec_time < 60:
            return False
        
        return True

    async def _revert_positions(
        self,
        execution_id: str,
        filled_legs: List[tuple],
        shares: float
    ) -> None:
        """
        FIX 1: Emergency Liquidation for Post-Execution Orphan Risk
        
        If Phase 1 fills but Phase 2 fails, we're left with incomplete hedge.
        Market-sell all filled positions immediately to return to cash.
        
        This prevents capital lock in un-hedged positions that guarantee loss.
        
        Args:
            execution_id: Execution identifier for logging
            filled_legs: List of (token_id, order_id, outcome_idx) that filled
            shares: Number of shares to liquidate per leg
        """
        logger.warning(
            f"[{execution_id}] 🚨 REVERTING POSITIONS: "
            f"Market-selling {len(filled_legs)} orphaned legs to prevent loss"
        )
        
        liquidation_tasks = []
        
        for token_id, order_id, outcome_idx in filled_legs:
            async def liquidate_leg(tid, oid, idx):
                try:
                    # Get current order book from cache or REST
                    book = None
                    if self._market_data_manager:
                        book_data = self._market_data_manager.get_order_book(tid)
                        if book_data:
                            # Convert cache format to expected format
                            class OrderBookStub:
                                def __init__(self, bids, asks):
                                    self.bids = bids
                                    self.asks = asks
                            book = OrderBookStub(book_data.get('bids', []), book_data.get('asks', []))
                    
                    if not book:
                        # Fallback to REST
                        book = await self.client.get_order_book(tid)
                    
                    bids = getattr(book, 'bids', [])
                    
                    if not bids:
                        logger.error(
                            f"[{execution_id}] No bids for {tid[:8]} - cannot revert"
                        )
                        return False
                    
                    # Market sell at best bid (accept 3% slippage for emergency)
                    best_bid = float(bids[0]['price'])
                    sell_price = max(0.01, best_bid * 0.97)
                    
                    result = await self.order_manager.execute_market_order(
                        token_id=tid,
                        side='SELL',
                        size=shares,
                        max_slippage=0.05,  # Accept 5% slippage for emergency exit
                        is_shares=True
                    )
                    
                    if result and result.get('success'):
                        logger.info(
                            f"[{execution_id}] ✅ Reverted leg {idx}: "
                            f"{shares} shares @ ${sell_price:.4f}"
                        )
                        return True
                    else:
                        logger.error(
                            f"[{execution_id}] ❌ Failed to revert leg {idx}"
                        )
                        return False
                        
                except Exception as e:
                    logger.error(
                        f"[{execution_id}] Reversion error for leg {idx}: {e}"
                    )
                    return False
            
            liquidation_tasks.append(liquidate_leg(token_id, order_id, outcome_idx))
        
        # Execute all liquidations concurrently
        results = await asyncio.gather(*liquidation_tasks, return_exceptions=True)
        
        success_count = sum(1 for r in results if r is True)
        logger.warning(
            f"[{execution_id}] Position reversion complete: "
            f"{success_count}/{len(filled_legs)} legs liquidated"
        )
        
        if success_count < len(filled_legs):
            logger.error(
                f"[{execution_id}] ⚠️  INCOMPLETE REVERSION: "
                f"{len(filled_legs) - success_count} legs still orphaned - manual intervention required"
            )
    
    def _prioritize_by_mm_inventory(
        self,
        opportunities: List[ArbitrageOpportunity]
    ) -> List[ArbitrageOpportunity]:
        """Prioritize arb opportunities that reduce MM inventory risk
        
        Logic:
        - Check MM strategy's current inventory for each market
        - If MM is long YES and arb requires buying NO, prioritize higher
        - If MM is short and arb requires buying YES, prioritize higher
        - Score = base_roi + inventory_reduction_bonus
        """
        if not self._market_making_strategy:
            return opportunities
        
        scored_opps = []
        
        for opp in opportunities:
            base_roi = opp.net_profit_per_share / opp.required_budget if opp.required_budget > 0 else 0
            
            # Check if MM has inventory in this market
            mm_inventory = self._market_making_strategy.get_market_inventory(opp.market_id)
            
            inventory_bonus = 0.0
            if mm_inventory:
                # Calculate if arb trade would reduce MM inventory
                for outcome in opp.outcomes:
                    token_inventory = mm_inventory.get(outcome.token_id, 0)
                    
                    # If MM is long this token, arb buying it doesn't help
                    # If MM is short this token, arb buying it helps neutralize
                    if token_inventory < 0:
                        # MM is short - arb buying this token helps
                        inventory_bonus += abs(token_inventory) * 0.01  # 1% bonus per share
                
                if inventory_bonus > 0:
                    logger.info(
                        f"[CROSS-STRATEGY] Arb on {opp.market_id[:8]}... helps reduce "
                        f"MM inventory (bonus: +{inventory_bonus*100:.1f}%)"
                    )
            
            # Combined score
            total_score = base_roi + inventory_bonus
            scored_opps.append((total_score, opp))
        
        # Re-sort by combined score
        scored_opps.sort(key=lambda x: x[0], reverse=True)
        return [opp for _, opp in scored_opps]
    
    def _is_system_error(self, error: Exception) -> bool:
        """
        FIX 3: Distinguish system errors from market errors
        
        System Errors (trigger circuit breaker):
        - API timeouts (429, 503, 504)
        - Network failures
        - Insufficient balance
        - Authentication errors
        
        Market Errors (do NOT trigger circuit breaker):
        - FOK order rejection (someone else hit the bid)
        - Price moved (opportunity gone)
        - Slippage exceeded
        - Order book depth changed
        
        Args:
            error: Exception that occurred
            
        Returns:
            True if system error, False if market error
        """
        error_str = str(error).lower()
        
        # System error patterns
        system_error_keywords = [
            'timeout',
            '429',  # Rate limit
            '503',  # Service unavailable
            '504',  # Gateway timeout
            'connection',
            'network',
            'insufficient balance',
            'authentication',
            'unauthorized',
            'api key',
        ]
        
        # Market error patterns (normal trading conditions)
        market_error_keywords = [
            'fill-or-kill',
            'fok',
            'slippage',
            'price moved',
            'order rejected',
            'insufficient depth',
            'opportunity',
        ]
        
        # Check for market errors first (more common)
        for keyword in market_error_keywords:
            if keyword in error_str:
                return False  # Market error - don't trigger circuit breaker
        
        # Check for system errors
        for keyword in system_error_keywords:
            if keyword in error_str:
                return True  # System error - count toward circuit breaker
        
        # Default: treat unknown errors as system errors (conservative)
        return True
    
    def _get_budget_remaining(self) -> Decimal:
        """Get remaining budget for arbitrage execution"""
        budget_status = self.executor.get_budget_status()
        return Decimal(str(budget_status['remaining_budget']))

    def get_strategy_status(self) -> Dict[str, Any]:
        """
        Get current strategy status
        
        Returns:
            Status dictionary with metrics and state
        """
        budget_status = self.executor.get_budget_status()
        
        return {
            'is_running': self.is_running,
            'circuit_breaker_active': self._circuit_breaker_active,
            'consecutive_failures': self._consecutive_failures,
            'total_executions': self._total_arb_executions,
            'successful_executions': self._successful_executions,
            'failed_executions': self._failed_executions,
            'total_profit': float(self._total_profit),
            'budget_total': budget_status['total_budget'],
            'budget_used': budget_status['used_budget'],
            'budget_remaining': budget_status['remaining_budget'],
            'budget_utilization_percent': budget_status['utilization_percent'],
        }

    async def validate_configuration(self) -> None:
        """
        Validate strategy configuration before starting
        
        Raises:
            StrategyError: If configuration is invalid
        """
        try:
            # Test market data fetch
            markets = await self.client.get_markets()
            if not markets or 'data' not in markets:
                raise StrategyError("Unable to fetch markets from Polymarket")
            
            # Test order book fetch (on first market)
            if markets['data']:
                market = markets['data'][0]
                token_ids = market.get('clobTokenIds', [])
                if token_ids:
                    try:
                        await self.client.get_order_book(token_ids[0])
                    except Exception as e:
                        logger.warning(f"Order book fetch test failed: {e}")
            
            logger.info("✅ ArbitrageStrategy configuration validated")
            
        except Exception as e:
            raise StrategyError(f"Configuration validation failed: {e}")
