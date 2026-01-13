"""
Main Entry Point for Polymarket Arbitrage Bot
Production-grade 24/7 bot with proper lifecycle management
"""

import os
import sys
import signal
import asyncio
from typing import Optional
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from strategies.mirror_strategy import MirrorStrategy
from config.constants import (
    LOOP_INTERVAL_SEC,
    HEALTH_CHECK_INTERVAL_SEC,
    MAX_CONSECUTIVE_ERRORS,
    ENABLE_CIRCUIT_BREAKER,
    CIRCUIT_BREAKER_LOSS_THRESHOLD_USD,
    USE_WEBSOCKET_DETECTION,
)
from core.whale_ws_listener import WhaleWebSocketListener
from utils.logger import get_logger, setup_logging
from utils.exceptions import (
    PolymarketBotError,
    CircuitBreakerError,
    HealthCheckError,
)


logger = get_logger(__name__)


class PolymarketBot:
    """
    Main bot orchestrator
    Manages lifecycle, health checks, and strategy execution
    """

    def __init__(self):
        """
        Initialize bot with production-grade features:
        - Graceful shutdown handling
        - Circuit breaker for safety
        - Health monitoring
        - Error recovery mechanisms
        """
        self.client: Optional[PolymarketClient] = None
        self.order_manager: Optional[OrderManager] = None
        self.strategies = []
        self.is_running = False
        self.consecutive_errors = 0
        self.total_pnl = 0.0
        self._shutdown_event = asyncio.Event()
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info("PolymarketBot initialized")
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        signal_name = signal.Signals(signum).name
        logger.info(f"Received {signal_name} signal, initiating graceful shutdown...")
        self.is_running = False
        # Set shutdown event to wake up any waiting tasks
        if not self._shutdown_event.is_set():
            self._shutdown_event.set()

    async def initialize(self) -> None:
        """Initialize all bot components"""
        try:
            logger.info("Initializing bot components...")
            
            # Initialize Polymarket client
            self.client = PolymarketClient()
            await self.client.initialize()
            
            # Initialize order manager
            self.order_manager = OrderManager(self.client)
            
            # Initialize strategies
            mirror_strategy = MirrorStrategy(self.client, self.order_manager)
            self.strategies.append(mirror_strategy)
            
            logger.info(f"Bot initialized with {len(self.strategies)} strategies")
            
        except Exception as e:
            logger.error(f"Failed to initialize bot: {e}", exc_info=True)
            raise

    async def start(self) -> None:
        """Start the bot and all strategies (polling or WebSocket mode)"""
        if self.is_running:
            logger.warning("Bot is already running")
            return

        self.is_running = True
        self.start_time = datetime.now()

        logger.info("=" * 80)
        logger.info("Starting Polymarket Arbitrage Bot")
        logger.info(f"Wallet Address: {self.client.wallet_address}")
        logger.info(f"Active Strategies: {len(self.strategies)}")
        logger.info(f"WebSocket detection enabled: {USE_WEBSOCKET_DETECTION}")
        logger.info("=" * 80)

        try:
            tasks = []
            if USE_WEBSOCKET_DETECTION:
                # WebSocket event-driven mode
                async def on_whale_trade(trade):
                    # For best-in-class: trigger mirror strategy on each whale trade
                    logger.info(f"Triggering mirror strategy on whale trade event...")
                    for strategy in self.strategies:
                        if hasattr(strategy, "execute"):
                            await strategy.execute()

                ws_listener = WhaleWebSocketListener(on_trade_callback=on_whale_trade)
                ws_task = asyncio.create_task(ws_listener.listen())
                tasks.append(ws_task)
            else:
                # Default polling mode: run all strategies in background
                for strategy in self.strategies:
                    tasks.append(asyncio.create_task(strategy.run()))

            # Start health check and shutdown monitor
            health_check_task = asyncio.create_task(self._health_check_loop())
            shutdown_task = asyncio.create_task(self._wait_for_shutdown())
            tasks.extend([health_check_task, shutdown_task])

            # Wait for shutdown or error
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED
            )
            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        except Exception as e:
            logger.error(f"Fatal error in bot execution: {e}", exc_info=True)
            raise
        finally:
            await self.shutdown()

    async def _wait_for_shutdown(self) -> None:
        """Wait for shutdown event"""
        await self._shutdown_event.wait()
        logger.info("Shutdown event detected, stopping bot...")
        self.is_running = False

    async def stop(self) -> None:
        """Stop the bot gracefully"""
        logger.info("Stopping bot...")
        self.is_running = False
        
        # Stop all strategies
        for strategy in self.strategies:
            try:
                await strategy.stop()
            except Exception as e:
                logger.error(f"Error stopping strategy {strategy.name}: {e}")

    async def shutdown(self) -> None:
        """Clean up resources on shutdown"""
        logger.info("Shutting down bot...")
        
        try:
            # Stop strategies
            await self.stop()
            
            # Close client connection
            if self.client:
                await self.client.close()
            
            # Log final statistics
            self._log_final_stats()
            
            logger.info("Bot shutdown complete")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

    async def _health_check_loop(self) -> None:
        """Periodic health checks for the bot"""
        logger.info("Health check loop started")
        
        while self.is_running:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL_SEC)
                await self._perform_health_check()
                
            except Exception as e:
                logger.error(f"Health check failed: {e}")
                self.consecutive_errors += 1
                
                if self.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.critical(
                        f"Maximum consecutive errors ({MAX_CONSECUTIVE_ERRORS}) reached"
                    )
                    raise HealthCheckError(
                        "Maximum consecutive health check failures"
                    )

    async def _perform_health_check(self) -> None:
        """Perform health checks on bot components"""
        try:
            # Check client connection
            if not self.client or not self.client._is_initialized:
                raise HealthCheckError("Client not initialized")
            
            # Check wallet balance
            balance = await self.client.get_balance()
            logger.debug(f"Health check - Balance: {balance} USDC")
            
            # Check circuit breaker
            if ENABLE_CIRCUIT_BREAKER:
                if abs(self.total_pnl) >= CIRCUIT_BREAKER_LOSS_THRESHOLD_USD:
                    logger.critical(
                        f"Circuit breaker triggered! Total PnL: {self.total_pnl} USD"
                    )
                    raise CircuitBreakerError(
                        "Circuit breaker triggered due to excessive losses",
                        total_loss=self.total_pnl
                    )
            
            # Check strategy status
            for strategy in self.strategies:
                if not strategy.is_running:
                    logger.warning(f"Strategy {strategy.name} is not running")
            
            # Reset consecutive errors on successful check
            self.consecutive_errors = 0
            logger.debug("Health check passed")
            
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            raise

    def _log_final_stats(self) -> None:
        """Log final statistics on shutdown"""
        if not self.start_time:
            return
        
        runtime = datetime.now() - self.start_time
        
        logger.info("=" * 80)
        logger.info("BOT FINAL STATISTICS")
        logger.info("=" * 80)
        logger.info(f"Runtime: {runtime}")
        logger.info(f"Total PnL: {self.total_pnl:.2f} USD")
        
        if self.order_manager:
            daily_volume = self.order_manager.get_daily_volume()
            logger.info(f"Daily Volume: {daily_volume} USDC")
        
        for strategy in self.strategies:
            status = strategy.get_status()
            logger.info(f"Strategy {status['name']}: Running={status['is_running']}")
        
        logger.info("=" * 80)


async def main():
    """Main entry point"""
    try:
        # Setup logging
        setup_logging()
        
        logger.info("Starting Polymarket Arbitrage Bot...")
        
        # Create and initialize bot
        bot = PolymarketBot()
        await bot.initialize()
        
        # Start bot (runs until stopped)
        await bot.start()
        
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except CircuitBreakerError as e:
        logger.critical(f"Circuit breaker triggered: {e}")
        sys.exit(1)
    except PolymarketBotError as e:
        logger.error(f"Bot error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.critical(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    """
    Entry point for production deployment
    Run with: python -m main
    """
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
