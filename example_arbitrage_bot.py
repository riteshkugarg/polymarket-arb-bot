#!/usr/bin/env python
"""
ARBITRAGE BOT: PRODUCTION EXAMPLE

Full working example demonstrating how to:
1. Initialize the arbitrage scanner and executor
2. Scan for multi-outcome arbitrage opportunities  
3. Execute atomically with FOK logic
4. Manage $100 budget across all trades
5. Track metrics and status

Usage:
------
    python example_arbitrage_bot.py

The script demonstrates:
- Mathematical arbitrage detection (sum < 0.98)
- Atomic execution (all-or-nothing)
- Budget management and tracking
- Error handling and circuit breaker logic
- Integration with existing Polymarket bot framework
"""

import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from strategies.arbitrage_strategy import ArbitrageStrategy
from utils.logger import get_logger


logger = get_logger(__name__)


async def print_status(strategy):
    """Print current strategy status and metrics"""
    status = strategy.get_strategy_status()
    
    print("\n" + "="*80)
    print("ARBITRAGE STRATEGY STATUS")
    print("="*80)
    print(f"Running:                    {status['is_running']}")
    print(f"Circuit breaker active:     {status['circuit_breaker_active']}")
    print(f"Consecutive failures:       {status['consecutive_failures']}")
    print()
    print("EXECUTION METRICS:")
    print(f"  Total executions:         {status['total_executions']}")
    print(f"  Successful executions:    {status['successful_executions']}")
    print(f"  Failed executions:        {status['failed_executions']}")
    if status['total_executions'] > 0:
        success_rate = (status['successful_executions'] / status['total_executions']) * 100
        print(f"  Success rate:             {success_rate:.1f}%")
    print()
    print("BUDGET TRACKING:")
    print(f"  Total budget:             ${status['budget_total']:.2f}")
    print(f"  Budget used:              ${status['budget_used']:.2f}")
    print(f"  Budget remaining:         ${status['budget_remaining']:.2f}")
    print(f"  Utilization:              {status['budget_utilization_percent']:.1f}%")
    print()
    print("PROFITABILITY:")
    print(f"  Total profit:             ${status['total_profit']:.2f}")
    if status['total_executions'] > 0:
        avg_profit = status['total_profit'] / status['total_executions']
        print(f"  Avg profit per execution: ${avg_profit:.4f}")
    print("="*80 + "\n")


async def main():
    """
    Main bot loop demonstrating arbitrage strategy execution
    
    Flow:
    1. Initialize Polymarket client and order manager
    2. Create and validate arbitrage strategy
    3. Run strategy for demonstration period
    4. Periodically print metrics and status
    5. Graceful shutdown with status summary
    """
    
    logger.info("╔" + "="*78 + "╗")
    logger.info("║" + " "*20 + "POLYMARKET ARBITRAGE BOT INITIALIZED" + " "*24 + "║")
    logger.info("╚" + "="*78 + "╝")
    
    try:
        # ════════════════════════════════════════════════════════════════════════
        # STEP 1: Initialize Client and Order Manager
        # ════════════════════════════════════════════════════════════════════════
        
        logger.info("\n📡 Initializing Polymarket client...")
        client = PolymarketClient()
        await client.initialize()
        logger.info("✅ Polymarket client initialized")
        
        logger.info("📋 Initializing order manager...")
        order_manager = OrderManager(client)
        logger.info("✅ Order manager initialized")
        
        # ════════════════════════════════════════════════════════════════════════
        # STEP 2: Create and Validate Arbitrage Strategy
        # ════════════════════════════════════════════════════════════════════════
        
        logger.info("\n🎯 Creating arbitrage strategy...")
        strategy = ArbitrageStrategy(client, order_manager)
        logger.info("✅ Arbitrage strategy created")
        
        logger.info("🔍 Validating strategy configuration...")
        await strategy.validate_configuration()
        logger.info("✅ Strategy configuration validated")
        
        # ════════════════════════════════════════════════════════════════════════
        # STEP 3: Run Strategy
        # ════════════════════════════════════════════════════════════════════════
        
        logger.info("\n🚀 Starting arbitrage strategy main loop...")
        logger.info("   Scanning for multi-outcome arbitrage opportunities")
        logger.info("   Sum of outcome prices < 0.98 = PROFIT OPPORTUNITY")
        logger.info("   Budget cap: $100 | Execution model: Atomic FOK")
        logger.info("   Status updates every 30 seconds...\n")
        
        # Create strategy task
        strategy_task = asyncio.create_task(strategy.run())
        
        # Status update loop
        status_interval = 30  # seconds
        elapsed_time = 0
        demo_duration = 300  # 5 minutes demo
        
        try:
            while elapsed_time < demo_duration:
                # Wait and print status
                await asyncio.sleep(status_interval)
                elapsed_time += status_interval
                
                await print_status(strategy)
                
                logger.info(f"Running for {elapsed_time}s / {demo_duration}s...")
                
        except KeyboardInterrupt:
            logger.info("\n⚠️  Keyboard interrupt received - shutting down...")
        
        # ════════════════════════════════════════════════════════════════════════
        # STEP 4: Graceful Shutdown
        # ════════════════════════════════════════════════════════════════════════
        
        logger.info("\n🛑 Stopping arbitrage strategy...")
        await strategy.stop()
        
        # Wait for task to complete
        try:
            await asyncio.wait_for(strategy_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Strategy task did not stop within timeout")
            strategy_task.cancel()
        
        logger.info("✅ Arbitrage strategy stopped")
        
        # ════════════════════════════════════════════════════════════════════════
        # STEP 5: Final Status Summary
        # ════════════════════════════════════════════════════════════════════════
        
        await print_status(strategy)
        
        # Success summary
        final_status = strategy.get_strategy_status()
        logger.info("\n📊 FINAL SUMMARY:")
        logger.info(f"   Executions:         {final_status['successful_executions']}/{final_status['total_executions']}")
        logger.info(f"   Total profit:       ${final_status['total_profit']:.2f}")
        logger.info(f"   Budget used:        ${final_status['budget_used']:.2f}/{final_status['budget_total']:.2f}")
        
        if final_status['total_profit'] > 0:
            logger.info(f"   ✅ NET PROFIT: ${final_status['total_profit']:.2f}")
        else:
            logger.warning(f"   No profit realized in this session")
        
        logger.info("\n" + "="*80)
        logger.info("ARBITRAGE BOT SHUTDOWN COMPLETE")
        logger.info("="*80)
        
    except Exception as e:
        logger.error(f"\n❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


# ════════════════════════════════════════════════════════════════════════
# EXAMPLE: MANUAL OPPORTUNITY TESTING
# ════════════════════════════════════════════════════════════════════════

async def test_manual_scan():
    """
    Example: Manually scan and inspect opportunities without executing
    
    Useful for:
    - Testing the scanner on specific markets
    - Analyzing detected opportunities
    - Validating profit calculations
    - Debugging market data
    """
    
    logger.info("\n" + "="*80)
    logger.info("MANUAL OPPORTUNITY SCANNER TEST")
    logger.info("="*80 + "\n")
    
    try:
        # Initialize client
        client = PolymarketClient()
        await client.initialize()
        order_manager = OrderManager(client)
        
        # Create scanner (without executor)
        from strategies.arb_scanner import ArbScanner
        scanner = ArbScanner(client, order_manager)
        
        logger.info("Scanning markets for arbitrage opportunities...")
        opportunities = await scanner.scan_markets(limit=50)
        
        if not opportunities:
            logger.info("No arbitrage opportunities found")
            return
        
        logger.info(f"\nFound {len(opportunities)} opportunities:\n")
        
        for i, opp in enumerate(opportunities[:5], 1):  # Show top 5
            logger.info(f"{i}. Market {opp.market_id[:8]}...")
            logger.info(f"   Type:           {opp.market_type.value}")
            logger.info(f"   Sum of prices:  {opp.sum_prices:.4f} (< 0.98 = arb)")
            logger.info(f"   Profit/share:   ${opp.profit_per_share:.6f}")
            logger.info(f"   Net profit:     ${opp.net_profit_per_share:.6f}")
            logger.info(f"   Max shares:     {opp.max_shares_to_buy:.1f}")
            logger.info(f"   Outcomes:       {len(opp.outcomes)}")
            logger.info()
            
    except Exception as e:
        logger.error(f"Test failed: {e}", exc_info=True)


if __name__ == '__main__':
    """
    Run the arbitrage bot example
    
    Two modes:
    1. python example_arbitrage_bot.py    → Run full bot with 5-min demo
    2. Comment out main() and uncomment test_manual_scan() → Scan without executing
    """
    
    # Run full bot example
    asyncio.run(main())
    
    # Uncomment below to test scanner only (no execution)
    # asyncio.run(test_manual_scan())
