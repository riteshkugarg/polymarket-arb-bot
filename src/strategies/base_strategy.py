"""
Base Strategy Abstract Class
Defines the interface that all trading strategies must implement
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import asyncio

from src.core.polymarket_client import PolymarketClient
from src.core.order_manager import OrderManager
from src.utils.logger import get_logger
from src.utils.exceptions import StrategyError


logger = get_logger(__name__)


class BaseStrategy(ABC):
    """
    Abstract base class for trading strategies
    All strategies must inherit from this class and implement required methods
    """

    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize strategy
        
        Args:
            client: Polymarket client instance
            order_manager: Order manager instance
            config: Strategy-specific configuration
        """
        self.client = client
        self.order_manager = order_manager
        self.config = config or {}
        self.is_running = False
        self._stop_event = asyncio.Event()
        
        self.name = self.__class__.__name__
        logger.info(f"Strategy initialized: {self.name}")

    @abstractmethod
    async def execute(self) -> None:
        """
        Main strategy execution logic
        This method should contain the core strategy logic
        Must be implemented by subclasses
        """
        pass

    @abstractmethod
    async def analyze_opportunity(self) -> Optional[Dict[str, Any]]:
        """
        Analyze market for trading opportunities
        
        Returns:
            Dictionary with opportunity details or None if no opportunity
            
        Expected return format:
        {
            'action': 'BUY' or 'SELL',
            'token_id': str,
            'size': float,
            'price': float (optional),
            'confidence': float (0-1),
            'metadata': dict (optional)
        }
        """
        pass

    @abstractmethod
    async def should_execute_trade(self, opportunity: Dict[str, Any]) -> bool:
        """
        Determine if an opportunity should be executed
        
        Args:
            opportunity: Opportunity dict from analyze_opportunity
            
        Returns:
            True if trade should be executed, False otherwise
        """
        pass

    async def run(self) -> None:
        """
        Main strategy loop
        Continuously executes strategy until stopped
        """
        if self.is_running:
            logger.warning(f"Strategy {self.name} is already running")
            return

        self.is_running = True
        logger.info(f"Starting strategy: {self.name}")

        try:
            await self.on_start()

            while self.is_running and not self._stop_event.is_set():
                try:
                    await self.execute()
                    
                    # Check interval from config
                    interval = self.config.get('check_interval_sec', 15)
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=interval
                    )
                    
                except asyncio.TimeoutError:
                    # Normal timeout, continue loop
                    continue
                except Exception as e:
                    logger.error(f"Error in strategy execution: {e}", exc_info=True)
                    await self.on_error(e)
                    
                    # Wait before retrying after error
                    error_backoff = self.config.get('error_backoff_sec', 30)
                    await asyncio.sleep(error_backoff)

        except Exception as e:
            logger.error(f"Fatal error in strategy {self.name}: {e}", exc_info=True)
            raise StrategyError(f"Strategy {self.name} failed: {e}")
        finally:
            await self.on_stop()
            self.is_running = False
            logger.info(f"Strategy stopped: {self.name}")

    async def stop(self) -> None:
        """Stop the strategy gracefully"""
        if not self.is_running:
            logger.warning(f"Strategy {self.name} is not running")
            return

        logger.info(f"Stopping strategy: {self.name}")
        self.is_running = False
        self._stop_event.set()

    async def on_start(self) -> None:
        """
        Hook called when strategy starts
        Override in subclass for custom initialization
        """
        logger.debug(f"Strategy {self.name} starting")

    async def on_stop(self) -> None:
        """
        Hook called when strategy stops
        Override in subclass for custom cleanup
        """
        logger.debug(f"Strategy {self.name} stopping")

    async def on_error(self, error: Exception) -> None:
        """
        Hook called when an error occurs during execution
        Override in subclass for custom error handling
        
        Args:
            error: The exception that occurred
        """
        logger.error(f"Strategy {self.name} error: {error}")

    def get_status(self) -> Dict[str, Any]:
        """
        Get current strategy status
        
        Returns:
            Dictionary with strategy status information
        """
        return {
            'name': self.name,
            'is_running': self.is_running,
            'config': self.config,
        }

    def update_config(self, config: Dict[str, Any]) -> None:
        """
        Update strategy configuration
        
        Args:
            config: New configuration parameters
        """
        self.config.update(config)
        logger.info(f"Strategy {self.name} config updated: {config}")
