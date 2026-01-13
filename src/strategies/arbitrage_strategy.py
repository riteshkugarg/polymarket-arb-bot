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

from typing import Dict, Any, Optional, List
import asyncio
from datetime import datetime
from decimal import Decimal

from strategies.base_strategy import BaseStrategy
from strategies.arb_scanner import ArbScanner, AtomicExecutor, ArbitrageOpportunity
from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from config.constants import (
    PROXY_WALLET_ADDRESS,
    API_TIMEOUT_SEC,
    MAX_RETRIES,
)
from utils.logger import get_logger, log_trade_event
from utils.exceptions import StrategyError


logger = get_logger(__name__)


# Configuration constants
ARB_SCAN_INTERVAL_SEC = 3  # How often to scan for opportunities
ARB_EXECUTION_COOLDOWN_SEC = 5  # Minimum time between executions
ARB_MAX_CONSECUTIVE_FAILURES = 3  # Circuit breaker threshold
ARB_OPPORTUNITY_REFRESH_LIMIT = 50  # Max markets to scan per iteration


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
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize arbitrage strategy
        
        Args:
            client: Polymarket CLOB client
            order_manager: Order execution manager
            config: Optional configuration overrides
        """
        super().__init__(client, order_manager, config)
        
        self.scanner = ArbScanner(client, order_manager)
        self.executor = AtomicExecutor(client, order_manager)
        
        # Strategy state
        self._is_running = False
        self._consecutive_failures = 0
        self._circuit_breaker_active = False
        self._last_execution_time = 0
        self._executed_opportunities: Dict[str, float] = {}  # market_id -> timestamp
        
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

    async def run(self) -> None:
        """
        Main strategy loop - continuously scan and execute arbitrage
        
        Runs independently alongside mirror strategy
        """
        if self._is_running:
            logger.warning("ArbitrageStrategy already running")
            return
        
        self._is_running = True
        logger.info("🚀 ArbitrageStrategy started")
        
        try:
            while self._is_running:
                try:
                    await self._arb_scan_loop()
                    await asyncio.sleep(ARB_SCAN_INTERVAL_SEC)
                    
                except asyncio.CancelledError:
                    logger.info("ArbitrageStrategy cancelled")
                    break
                except Exception as e:
                    logger.error(f"Unexpected error in arb loop: {e}")
                    self._consecutive_failures += 1
                    
                    # Check circuit breaker
                    if self._consecutive_failures >= ARB_MAX_CONSECUTIVE_FAILURES:
                        self._circuit_breaker_active = True
                        logger.error(
                            f"⚠️  Circuit breaker activated after {self._consecutive_failures} failures"
                        )
                        await asyncio.sleep(30)  # Back off for 30 seconds
                        
        finally:
            self._is_running = False
            logger.info("🛑 ArbitrageStrategy stopped")

    async def stop(self) -> None:
        """Stop the strategy loop"""
        self._is_running = False
        logger.info("Stopping ArbitrageStrategy")

    async def _arb_scan_loop(self) -> None:
        """
        Main arbitrage scanning loop
        
        1. Scan markets for opportunities
        2. Filter by profitability
        3. Execute top opportunities atomically
        4. Update metrics and budget
        """
        try:
            # Scan for opportunities
            opportunities = await self.scanner.scan_markets(limit=ARB_OPPORTUNITY_REFRESH_LIMIT)
            
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
            
            # Execute top opportunity
            top_opportunity = executable_opps[0]
            
            # Check execution cooldown
            time_since_last = datetime.now().timestamp() - self._last_execution_time
            if time_since_last < ARB_EXECUTION_COOLDOWN_SEC:
                logger.debug(
                    f"Execution cooldown active ({time_since_last:.1f}s / {ARB_EXECUTION_COOLDOWN_SEC}s)"
                )
                return
            
            # Execute with optimal share count
            shares_to_buy = min(
                top_opportunity.max_shares_to_buy,
                top_opportunity.required_budget / top_opportunity.sum_prices
            )
            
            if shares_to_buy < 1.0:
                logger.debug(f"Share count too low: {shares_to_buy} < 1.0")
                return
            
            # Execute
            result = await self.executor.execute(top_opportunity, shares_to_buy)
            
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

    def _is_opportunity_executable(self, opportunity: ArbitrageOpportunity) -> bool:
        """
        Check if opportunity should be executed
        
        Filters:
        1. Sufficient budget remaining
        2. Not already executed recently
        3. Circuit breaker not active
        4. Profit meets minimum threshold
        
        Args:
            opportunity: ArbitrageOpportunity to check
            
        Returns:
            True if executable
        """
        # Check circuit breaker
        if self._circuit_breaker_active:
            return False
        
        # Check profit threshold
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
            'is_running': self._is_running,
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
