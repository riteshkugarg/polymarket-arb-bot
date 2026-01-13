"""
EXAMPLE: Integrating AtomicDepthAwareExecutor with ArbitrageStrategy

This example shows the EXACT integration pattern for using the
atomic executor in your arbitrage strategy.
"""

import asyncio
from typing import Optional
from decimal import Decimal

from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from core.atomic_depth_aware_executor import (
    AtomicDepthAwareExecutor,
    AtomicExecutionResult,
    ExecutionPhase,
)
from strategies.arb_scanner import ArbitrageOpportunity
from utils.logger import get_logger


logger = get_logger(__name__)


class AtomicArbitrageExecutor:
    """
    Wrapper combining ArbScanner opportunities with AtomicDepthAwareExecutor
    
    This is the integration layer between:
    1. ArbScanner (detects opportunities with sum < 0.98)
    2. AtomicDepthAwareExecutor (executes atomically with depth awareness)
    """
    
    def __init__(self, client: PolymarketClient, order_manager: OrderManager):
        """Initialize with clients"""
        self.client = client
        self.order_manager = order_manager
        self.atomic_executor = AtomicDepthAwareExecutor(client, order_manager)
        
        # Budget tracking
        self.budget_total = Decimal('100.0')  # $100 cap
        self.budget_used = Decimal('0')
        self.total_profit = Decimal('0')
        
        # Metrics
        self.execution_count = 0
        self.successful_count = 0
        self.failed_count = 0
        self.partial_fills = []
        
        logger.info("AtomicArbitrageExecutor initialized with $100 budget cap")

    async def execute_opportunity(
        self,
        opportunity: ArbitrageOpportunity,
        target_shares: Optional[float] = None
    ) -> AtomicExecutionResult:
        """
        Execute a detected arbitrage opportunity atomically
        
        Args:
            opportunity: Detected by ArbScanner (has all outcome data)
            target_shares: Override auto-sized shares (default: auto-calculate)
            
        Returns:
            AtomicExecutionResult with full execution details
        """
        self.execution_count += 1
        
        # ════════════════════════════════════════════════════════════════════
        # STEP 1: PREPARE EXECUTION
        # ════════════════════════════════════════════════════════════════════
        
        logger.info(
            f"[ARBTRADE #{self.execution_count}] Starting atomic execution: "
            f"Market {opportunity.market_id[:8]}..., "
            f"Type: {opportunity.market_type.value}"
        )
        
        # Calculate share size
        if target_shares:
            shares_to_buy = target_shares
        else:
            # Auto-size based on remaining budget
            budget_remaining = self.budget_total - self.budget_used
            shares_to_buy = min(
                opportunity.max_shares_to_buy,
                float(budget_remaining) / opportunity.sum_prices
            )
        
        logger.debug(
            f"  Shares to buy: {shares_to_buy:.1f} "
            f"(Budget: ${float(self.budget_used):.2f}/${float(self.budget_total):.2f})"
        )
        
        # ════════════════════════════════════════════════════════════════════
        # STEP 2: BUILD OUTCOMES LIST FOR EXECUTOR
        # ════════════════════════════════════════════════════════════════════
        
        # Convert from scanner format to executor format
        outcomes = [
            (op.token_id, op.outcome_name, op.ask_price)
            for op in opportunity.outcomes
        ]
        
        logger.debug(f"  Outcomes ({len(outcomes)}):")
        for token_id, name, price in outcomes:
            logger.debug(f"    - {name}: ${price:.4f}")
        
        # ════════════════════════════════════════════════════════════════════
        # STEP 3: EXECUTE ATOMICALLY
        # ════════════════════════════════════════════════════════════════════
        
        try:
            result = await self.atomic_executor.execute_atomic_basket(
                market_id=opportunity.market_id,
                outcomes=outcomes,
                side="BUY",
                size=shares_to_buy,
                order_type="FOK"  # Fill-or-Kill for safety
            )
            
        except Exception as e:
            logger.error(f"[ARBTRADE #{self.execution_count}] Unexpected error: {e}")
            result = AtomicExecutionResult(
                success=False,
                execution_phase=ExecutionPhase.PRE_FLIGHT,
                market_id=opportunity.market_id,
                total_cost=Decimal('0'),
                orders=[],
                filled_shares=0.0,
                partial_fills=[],
                error_message=str(e),
                execution_time_ms=0.0
            )
        
        # ════════════════════════════════════════════════════════════════════
        # STEP 4: PROCESS RESULT
        # ════════════════════════════════════════════════════════════════════
        
        if result.success:
            # SUCCESS: Update tracking and metrics
            self.successful_count += 1
            self.budget_used += result.total_cost
            
            # Calculate actual profit
            profit = (opportunity.net_profit_per_share * result.filled_shares)
            self.total_profit += Decimal(str(profit))
            
            logger.info(
                f"[ARBTRADE #{self.execution_count}] ✅ SUCCESS "
                f"({result.execution_time_ms:.0f}ms) | "
                f"Cost: ${float(result.total_cost):.2f} | "
                f"Profit: ${profit:.4f} | "
                f"Shares: {result.filled_shares}"
            )
            
        else:
            # FAILURE: Log details and metrics
            self.failed_count += 1
            
            # Analyze failure phase
            if result.execution_phase == ExecutionPhase.PRE_FLIGHT:
                logger.warning(
                    f"[ARBTRADE #{self.execution_count}] ⚠️  PRE-FLIGHT FAILURE | "
                    f"{result.error_message}"
                )
                
            elif result.execution_phase == ExecutionPhase.CONCURRENT_PLACEMENT:
                logger.error(
                    f"[ARBTRADE #{self.execution_count}] ❌ ORDER PLACEMENT FAILURE | "
                    f"{result.error_message}"
                )
                
            elif result.execution_phase == ExecutionPhase.ABORT:
                logger.critical(
                    f"[ARBTRADE #{self.execution_count}] 🚨 ATOMIC ABORT! | "
                    f"{result.error_message}"
                )
                
                # Log partial fill details if present
                if result.partial_fills:
                    for order in result.orders:
                        if order.order_id in result.partial_fills:
                            logger.critical(
                                f"   PARTIAL FILL: {order.outcome_name} "
                                f"({order.filled_shares}/{order.size} shares)"
                            )
                    
                    self.partial_fills.append({
                        'market_id': opportunity.market_id,
                        'partial_orders': result.partial_fills,
                        'timestamp': asyncio.get_event_loop().time()
                    })
        
        # ════════════════════════════════════════════════════════════════════
        # STEP 5: RETURN RESULT FOR FURTHER PROCESSING
        # ════════════════════════════════════════════════════════════════════
        
        return result

    def get_status(self) -> dict:
        """Get execution status and metrics"""
        success_rate = (
            self.successful_count / self.execution_count * 100
            if self.execution_count > 0 else 0.0
        )
        
        return {
            'execution_count': self.execution_count,
            'successful_count': self.successful_count,
            'failed_count': self.failed_count,
            'success_rate_percent': success_rate,
            'total_profit': float(self.total_profit),
            'budget_used': float(self.budget_used),
            'budget_remaining': float(self.budget_total - self.budget_used),
            'budget_utilization_percent': (
                float(self.budget_used) / float(self.budget_total) * 100
            ),
            'partial_fill_incidents': len(self.partial_fills),
        }


# ════════════════════════════════════════════════════════════════════════════
# EXAMPLE USAGE IN YOUR BOT
# ════════════════════════════════════════════════════════════════════════════

async def example_bot():
    """
    Example: Full bot using atomic arbitrage execution
    """
    # Initialize clients
    client = PolymarketClient()
    await client.initialize()
    
    order_manager = OrderManager(client)
    
    # Initialize scanner and executor
    from strategies.arb_scanner import ArbScanner
    scanner = ArbScanner(client, order_manager)
    executor = AtomicArbitrageExecutor(client, order_manager)
    
    logger.info("=" * 80)
    logger.info("ATOMIC ARBITRAGE BOT STARTING")
    logger.info("=" * 80)
    
    # Main loop
    iteration = 0
    while True:
        iteration += 1
        
        try:
            # SCAN for opportunities
            logger.info(f"\n[ITERATION {iteration}] Scanning markets...")
            opportunities = await scanner.scan_markets(limit=100)
            
            if not opportunities:
                logger.debug("  No opportunities found this iteration")
                await asyncio.sleep(3)
                continue
            
            logger.info(f"  Found {len(opportunities)} opportunities")
            
            # EXECUTE top opportunity
            top_opp = opportunities[0]
            
            logger.info(
                f"  Executing top opportunity: {top_opp.market_id[:8]}... "
                f"(sum={top_opp.sum_prices:.4f}, profit=${top_opp.net_profit_per_share:.6f})"
            )
            
            result = await executor.execute_opportunity(top_opp)
            
            # REPORT metrics
            status = executor.get_status()
            
            logger.info(
                f"\n[STATUS] "
                f"Executions: {status['successful_count']}/{status['execution_count']} | "
                f"Profit: ${status['total_profit']:.2f} | "
                f"Budget: ${status['budget_remaining']:.2f} remaining"
            )
            
            # Check if budget exhausted or partial fills detected
            if status['budget_remaining'] <= 5.0:
                logger.warning("Budget nearly exhausted ($5 remaining)")
                break
            
            if status['partial_fill_incidents'] > 0:
                logger.critical(f"⚠️  {status['partial_fill_incidents']} partial fill incidents detected!")
                # Manual review may be needed
            
            # Rate limit
            await asyncio.sleep(3)
            
        except Exception as e:
            logger.error(f"Bot error: {e}", exc_info=True)
            await asyncio.sleep(5)


# ════════════════════════════════════════════════════════════════════════════
# INTEGRATION CHECKLIST FOR YOUR BOT
# ════════════════════════════════════════════════════════════════════════════

"""
✅ Step 1: Import the executor
   from core.atomic_depth_aware_executor import AtomicDepthAwareExecutor

✅ Step 2: Initialize in your bot __init__
   self.atomic_executor = AtomicDepthAwareExecutor(client, order_manager)

✅ Step 3: Use in your strategy
   result = await self.atomic_executor.execute_atomic_basket(...)

✅ Step 4: Handle result
   if result.success:
       # Update positions and profit
   elif result.partial_fills:
       # CRITICAL: Alert immediately
   else:
       # Safe failure: retry or skip

✅ Step 5: Monitor metrics
   - Execution success rate (target: > 90%)
   - Partial fills (target: 0)
   - Average latency (target: < 500ms)
   - Depth check failures (indicates market conditions)

✅ Step 6: Deploy to EC2
   - All atomic checks enabled
   - Partial fill alerting active
   - Budget constraints enforced
   - Ready for 24/7 operation
"""

if __name__ == '__main__':
    asyncio.run(example_bot())
