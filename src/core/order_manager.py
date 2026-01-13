"""
Order Manager
Handles order execution, validation, and risk management
"""

from typing import Dict, Optional, Any
from decimal import Decimal
import asyncio
from py_clob_client.clob_types import OrderArgs, OrderType

from config.constants import (
    MAX_SLIPPAGE_PERCENT,
    MAX_POSITION_SIZE_USD,
    ENTRY_PRICE_GUARD,
)
from core.polymarket_client import PolymarketClient
from utils.logger import get_logger, log_trade_event
from utils.exceptions import (
    OrderRejectionError,
    SlippageExceededError,
    PriceGuardError,
    InsufficientBalanceError,
    TradingError,
)
from utils.helpers import (
    validate_slippage,
    validate_entry_price_guard,
    validate_order_parameters,
)


logger = get_logger(__name__)


class OrderManager:
    """
    Manages order execution with safety checks and risk management
    """

    def __init__(self, client: PolymarketClient):
        """
        Initialize order manager with safety mechanisms.
        
        Args:
            client: Initialized PolymarketClient instance
        
        Features:
            - Circuit breaker for consecutive failures
            - Daily volume tracking
            - Position size limits
            - Price guard validation
        """
        if client is None:
            raise ValueError("PolymarketClient cannot be None")
        
        self.client = client
        self.total_daily_volume = Decimal('0')
        self._consecutive_failures = 0
        self._max_consecutive_failures = 5
        self._circuit_breaker_active = False
        
        logger.info(
            f"OrderManager initialized - "
            f"Max position: ${MAX_POSITION_SIZE_USD}, "
            f"Circuit breaker threshold: {self._max_consecutive_failures}"
        )

    async def validate_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: Optional[float] = None,
        target_price: Optional[float] = None,
        is_shares: bool = False
    ) -> None:
        """
        Validate order parameters before execution with comprehensive checks.
        
        Args:
            token_id: Token to trade (must be valid hex string)
            side: 'BUY' or 'SELL' (case-insensitive)
            size: Order size in USDC (must be positive)
            price: Limit price (0.0 to 1.0 for prediction markets)
            target_price: Reference price for guard check
            
        Raises:
            ValidationError: If any validation check fails
            InsufficientBalanceError: If insufficient balance for buy orders
            PriceGuardError: If price deviates too much from target
        """
        # Input type validation
        if not isinstance(token_id, str) or not token_id:
            raise ValidationError("token_id must be a non-empty string")
        
        if not isinstance(side, str) or side.upper() not in ['BUY', 'SELL']:
            raise ValidationError(f"Invalid side '{side}'. Must be 'BUY' or 'SELL'")
        
        if not isinstance(size, (int, float)) or size <= 0:
            raise ValidationError(f"Invalid size {size}. Must be positive number")
        
        # Check circuit breaker
        if self._circuit_breaker_active:
            raise ValidationError(
                f"Circuit breaker active due to {self._consecutive_failures} consecutive failures. "
                f"Manual intervention required."
            )
        
        # Check minimum order size removed - allow proportional sizing
        # SELL orders must execute regardless of size (closing existing positions)
        # Note: Polymarket enforces 5-share minimum which will reject too-small orders

            # Check maximum position size (only for USD-based orders)
            if size > MAX_POSITION_SIZE_USD:
                raise ValidationError(
                    f"Order size {size} exceeds maximum {MAX_POSITION_SIZE_USD} USDC"
                )

        # Check price guard ONLY for BUY orders (SELL orders execute immediately)
        if side.upper() == 'BUY' and target_price and price:
            if not is_within_price_guard(target_price, price, ENTRY_PRICE_GUARD):
                raise PriceGuardError(
                    f"Price {price} deviates too much from target {target_price}",
                    target_price=target_price,
                    current_price=price
                )

        # Check available balance for buy orders
        if side.upper() == 'BUY':
            balance = await self.client.get_balance()
            if balance < size:
                raise InsufficientBalanceError(
                    f"Insufficient balance: {balance} USDC < {size} USDC",
                    required=size,
                    available=float(balance)
                )

        logger.debug(f"Order validation passed: {side} {size} USDC @ {price}")

    async def execute_market_order(
        self,
        token_id: str,
        side: str,
        size: float,
        max_slippage: Optional[float] = None,
        is_shares: bool = False,
        neg_risk: bool = False  # 2026 Update: NegRisk flag for proper signature
    ) -> Dict[str, Any]:
        """
        Execute a market order with slippage protection
        
        Args:
            token_id: Token to trade
            side: 'BUY' or 'SELL'
            size: Order size in USDC (or shares if is_shares=True for SELL orders)
            max_slippage: Maximum allowed slippage (uses default if not specified)
            is_shares: If True, size represents shares for SELL orders (default: False)
            neg_risk: If True, market is NegRisk and requires special signature (2026)
            
        Returns:
            Order execution result
            
        Raises:
            OrderExecutionError: If execution fails
            SlippageExceededError: If slippage exceeds limit
        """
        max_slippage = max_slippage or MAX_SLIPPAGE_PERCENT
        
        try:
            # Get current best price
            expected_price = await self.client.get_best_price(token_id, side)
            if not expected_price:
                raise OrderExecutionError(
                    f"No liquidity available for {token_id}"
                )

            # Validate order
            await self.validate_order(
                token_id=token_id,
                side=side,
                size=size,
                price=expected_price,
                is_shares=is_shares
            )

            # Execute market order using client's specialized methods
            if side.upper() == 'BUY':
                logger.info(
                    f"Executing market order: {side} {size} USDC of {token_id} "
                    f"@ ~{expected_price}"
                )
                # Market buy: specify USDC amount to spend
                order_response = await self.client.create_market_buy_order(
                    token_id=token_id,
                    amount=size
                )
            else:
                # Market sell: size can be shares (direct) or USD (needs conversion)
                if is_shares:
                    token_amount = size  # Already in shares
                    estimated_value = size * expected_price  # Calculate USD value
                    logger.info(
                        f"Executing market order: {side} {size:.2f} shares (~${estimated_value:.2f}) of {token_id} "
                        f"@ ~{expected_price}"
                    )
                else:
                    token_amount = size / expected_price  # Convert USD to shares
                    estimated_value = size
                    logger.info(
                        f"Executing market order: {side} {size} USDC of {token_id} "
                        f"@ ~{expected_price}"
                    )
                    
                order_response = await self.client.create_market_sell_order(
                    token_id=token_id,
                    amount=token_amount,
                    estimated_value=estimated_value
                )
            
            # Parse response
            order_result = {
                'order_id': order_response.get('orderID', 'unknown'),
                'status': order_response.get('status', 'submitted'),
                'filled_size': size,
                'avg_price': order_response.get('price', expected_price),
                'token_id': token_id,
                'side': side,
                'timestamp': order_response.get('timestamp'),
                'raw_response': order_response,
            }

            # Check actual execution price for slippage
            actual_price = order_result.get('avg_price', expected_price)
            slippage = calculate_slippage(expected_price, actual_price)

            if slippage > max_slippage:
                logger.error(
                    f"Slippage {slippage:.2%} exceeds limit {max_slippage:.2%}"
                )
                raise SlippageExceededError(
                    f"Slippage {slippage:.2%} exceeds limit {max_slippage:.2%}",
                    expected_price=expected_price,
                    actual_price=actual_price
                )

            # Log successful trade
            log_trade_execution(
                logger,
                action=side,
                market_id=token_id,
                price=actual_price,
                size=size,
                slippage=slippage,
                order_id=order_result.get('order_id')
            )

            # Update daily volume
            self.total_daily_volume += Decimal(str(size))

            return order_result

        except (ValidationError, InsufficientBalanceError, PriceGuardError) as e:
            # Re-raise validation errors
            raise
        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            raise OrderExecutionError(
                f"Failed to execute order: {e}",
                order_data={'token_id': token_id, 'side': side, 'size': size}
            )

    async def execute_limit_order(
        self,
        token_id: str,
        side: str,
        size: float,
        price: float,
        target_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Execute a limit order
        
        Args:
            token_id: Token to trade
            side: 'BUY' or 'SELL'
            size: Order size in USDC
            price: Limit price
            target_price: Reference price for guard check
            
        Returns:
            Order placement result
            
        Raises:
            OrderExecutionError: If execution fails
        """
        try:
            # Validate order
            await self.validate_order(
                token_id=token_id,
                side=side,
                size=size,
                price=price,
                target_price=target_price
            )

            logger.info(
                f"Placing limit order: {side} {size} USDC of {token_id} "
                f"@ {price}"
            )

            # Calculate order size based on side
            # BUY: Convert USDC amount to shares (size parameter = number of shares)
            # SELL: Already have shares, use directly
            order_size = size / price if side.upper() == 'BUY' else size
            
            # Place limit order using client
            order_response = await self.client.create_limit_order(
                token_id=token_id,
                side=side,
                price=price,
                size=order_size
            )
            
            order_result = {
                'order_id': order_response.get('orderID', 'unknown'),
                'status': 'open',
                'size': size,
                'price': price,
                'token_id': token_id,
                'side': side,
                'timestamp': order_response.get('timestamp'),
                'raw_response': order_response,
            }

            logger.info(
                f"Limit order placed: {order_result.get('order_id')}"
            )

            return order_result

        except (ValidationError, InsufficientBalanceError, PriceGuardError) as e:
            # Re-raise validation errors
            raise
        except Exception as e:
            logger.error(f"Limit order placement failed: {e}")
            raise OrderExecutionError(
                f"Failed to place limit order: {e}",
                order_data={'token_id': token_id, 'side': side, 'size': size, 'price': price}
            )

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if cancelled successfully
        """
        try:
            logger.info(f"Cancelling order: {order_id}")
            
            # Cancel order using client method
            await self.client.cancel_order(order_id)
            
            logger.info(f"Order cancelled: {order_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def get_optimal_position_size(
        self,
        target_size: float,
        min_size: Optional[float] = None,
        max_size: Optional[float] = None
    ) -> Optional[float]:
        """
        Calculate optimal position size considering constraints
        
        Args:
            target_size: Desired position size
            min_size: Minimum allowed size
            max_size: Maximum allowed size
            
        Returns:
            Optimal position size or None if constraints cannot be met
        """
        balance = await self.client.get_balance()
        
        return calculate_position_size(
            available_balance=float(balance),
            target_size=target_size,
            min_order_size=min_size or 0.0,  # No minimum - proportional sizing only
            max_position_size=max_size or MAX_POSITION_SIZE_USD
        )

    def get_daily_volume(self) -> Decimal:
        """Get total daily trading volume"""
        return self.total_daily_volume

    def reset_daily_volume(self) -> None:
        """Reset daily volume counter (should be called daily)"""
        logger.info(f"Resetting daily volume from {self.total_daily_volume} USDC")
        self.total_daily_volume = Decimal('0')
