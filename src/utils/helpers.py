"""
Security Validators and Helper Utilities for Polymarket Arbitrage Bot

Provides:
- Address validation (Ethereum/Polygon)
- Price bounds checking
- Order parameter validation
- Slippage verification
- Circuit breaker logic
- Safe mathematical operations

All functions include comprehensive error handling and logging.
"""

import re
from typing import Tuple, Optional, Dict, Any
from decimal import Decimal, ROUND_DOWN
import asyncio
from functools import wraps

from utils.logger import get_logger
from utils.exceptions import (
    DataValidationError,
    InvalidOrderError,
    PriceGuardError,
    SlippageExceededError,
    CircuitBreakerError,
)
from config.constants import (
    MIN_BUY_PRICE,
    MAX_BUY_PRICE,
    MAX_SLIPPAGE_PERCENT,
    ENTRY_PRICE_GUARD,
    MIN_ORDER_SHARES,
    MAX_ORDER_USD,
    CIRCUIT_BREAKER_LOSS_THRESHOLD_USD,
)


logger = get_logger(__name__)


# ============================================================================
# 1. ADDRESS VALIDATION
# ============================================================================

def validate_ethereum_address(address: str) -> bool:
    """
    Validate Ethereum/Polygon address format (0x prefixed hex).

    Args:
        address: Address string to validate

    Returns:
        True if valid, False otherwise

    Raises:
        DataValidationError: If address is malformed
    """
    if not isinstance(address, str):
        raise DataValidationError(
            f"Address must be string, got {type(address).__name__}",
            details={'address': str(address)}
        )

    # Check format: 0x followed by 40 hex characters (20 bytes)
    if not re.match(r'^0x[0-9a-fA-F]{40}$', address):
        raise DataValidationError(
            f"Invalid Ethereum address format",
            error_code='INVALID_ADDRESS_FORMAT',
            details={'address': address, 'expected_format': '0x + 40 hex chars'}
        )

    return True


def validate_wallet_addresses(
    proxy_address: str,
    signer_address: Optional[str] = None
) -> bool:
    """
    Validate both proxy and signer wallet addresses.

    Args:
        proxy_address: Proxy wallet address (where funds are held)
        signer_address: Optional signer wallet address (for authentication)

    Returns:
        True if both addresses are valid

    Raises:
        DataValidationError: If any address is invalid
    """
    try:
        validate_ethereum_address(proxy_address)
        if signer_address:
            validate_ethereum_address(signer_address)

        logger.info(
            "Wallet addresses validated",
            extra={
                'proxy_address': proxy_address,
                'has_signer': signer_address is not None
            }
        )
        return True

    except DataValidationError as e:
        logger.error(f"Wallet validation failed: {e}")
        raise


# ============================================================================
# 2. PRICE BOUNDS VALIDATION
# ============================================================================

def validate_price_bounds(price: float, reason: str = "general") -> bool:
    """
    Validate that price is within acceptable bounds for trading.

    Prevents buying outcomes that are too cheap (unlikely) or too expensive (no upside).

    Args:
        price: Price to validate (0.0 - 1.0 range for binary markets)
        reason: Reason for validation (for logging)

    Returns:
        True if price is within bounds

    Raises:
        InvalidOrderError: If price is outside valid bounds
    """
    if not isinstance(price, (int, float)):
        raise InvalidOrderError(
            f"Price must be numeric, got {type(price).__name__}"
        )

    if not (0.0 <= price <= 1.0):
        raise InvalidOrderError(
            f"Price must be between 0 and 1, got {price}",
            details={'price': price, 'valid_range': '0.0-1.0'}
        )

    if price < MIN_BUY_PRICE:
        raise InvalidOrderError(
            f"Price {price} below minimum buy price {MIN_BUY_PRICE}",
            error_code='PRICE_BELOW_MIN',
            details={'price': price, 'min_price': MIN_BUY_PRICE}
        )

    if price > MAX_BUY_PRICE:
        raise InvalidOrderError(
            f"Price {price} above maximum buy price {MAX_BUY_PRICE}",
            error_code='PRICE_ABOVE_MAX',
            details={'price': price, 'max_price': MAX_BUY_PRICE}
        )

    return True


def validate_entry_price_guard(
    whale_price: float,
    our_price: float,
    tolerance: float = ENTRY_PRICE_GUARD
) -> bool:
    """
    Validate that our execution price is within acceptable deviation from whale's price.

    Prevents buying at significantly worse prices than the whale.

    Args:
        whale_price: Price at which whale entered
        our_price: Our proposed entry price
        tolerance: Maximum acceptable price deviation (default from constants)

    Returns:
        True if price deviation is acceptable

    Raises:
        PriceGuardError: If price deviation exceeds tolerance
    """
    if whale_price <= 0:
        raise InvalidOrderError(
            f"Whale price must be positive, got {whale_price}",
            details={'whale_price': whale_price}
        )

    # Calculate percentage deviation
    deviation = abs(our_price - whale_price) / whale_price

    if deviation > tolerance:
        raise PriceGuardError(
            f"Price deviation {deviation:.2%} exceeds tolerance {tolerance:.2%}",
            error_code='ENTRY_PRICE_GUARD_FAILED',
            target_price=whale_price,
            current_price=our_price,
            details={
                'whale_price': whale_price,
                'our_price': our_price,
                'deviation': f"{deviation:.2%}",
                'tolerance': f"{tolerance:.2%}"
            }
        )

    return True


# ============================================================================
# 3. ORDER PARAMETER VALIDATION
# ============================================================================

def validate_order_size(size_usd: float, size_shares: float) -> bool:
    """
    Validate order size parameters.

    Ensures:
    - Size is positive and reasonable
    - Meets Polymarket minimum (5 shares)
    - Doesn't exceed maximum

    Args:
        size_usd: Order size in USDC
        size_shares: Order size in shares

    Returns:
        True if order size is valid

    Raises:
        InvalidOrderError: If size is invalid
    """
    if size_usd <= 0:
        raise InvalidOrderError(
            f"Order size must be positive, got {size_usd}",
            details={'size_usd': size_usd}
        )

    if size_shares < MIN_ORDER_SHARES:
        raise InvalidOrderError(
            f"Order size {size_shares} shares below minimum {MIN_ORDER_SHARES}",
            error_code='ORDER_SIZE_BELOW_MINIMUM',
            details={'size_shares': size_shares, 'min_shares': MIN_ORDER_SHARES}
        )

    if size_usd > MAX_ORDER_USD:
        raise InvalidOrderError(
            f"Order size ${size_usd} exceeds maximum ${MAX_ORDER_USD}",
            error_code='ORDER_SIZE_EXCEEDS_MAXIMUM',
            details={'size_usd': size_usd, 'max_usd': MAX_ORDER_USD}
        )

    return True


def validate_order_parameters(
    token_id: str,
    side: str,
    price: float,
    size: float
) -> bool:
    """
    Validate complete order parameters before submission.

    Args:
        token_id: Market token ID
        side: BUY or SELL
        price: Execution price (0.0-1.0)
        size: Order size in USDC

    Returns:
        True if all parameters are valid

    Raises:
        InvalidOrderError: If any parameter is invalid
    """
    # Validate token ID
    if not token_id or not isinstance(token_id, str):
        raise InvalidOrderError(
            "Token ID must be a non-empty string",
            details={'token_id': token_id}
        )

    # Validate side
    if side.upper() not in ['BUY', 'SELL']:
        raise InvalidOrderError(
            f"Side must be BUY or SELL, got {side}",
            details={'side': side}
        )

    # Validate price
    validate_price_bounds(price, reason=f"{side} order")

    # Validate size
    validate_order_size(size_usd=size, size_shares=int(size))

    return True


# ============================================================================
# 4. SLIPPAGE VALIDATION
# ============================================================================

def validate_slippage(
    expected_price: float,
    actual_price: float,
    max_slippage: float = MAX_SLIPPAGE_PERCENT,
    side: str = "BUY"
) -> bool:
    """
    Validate that execution slippage is within acceptable limits.

    Args:
        expected_price: Expected execution price
        actual_price: Actual execution price
        max_slippage: Maximum acceptable slippage (default from constants)
        side: BUY or SELL (for directional slippage check)

    Returns:
        True if slippage is acceptable

    Raises:
        SlippageExceededError: If slippage exceeds tolerance
    """
    if expected_price <= 0:
        raise InvalidOrderError(
            f"Expected price must be positive, got {expected_price}"
        )

    # Calculate slippage percentage
    slippage = abs(actual_price - expected_price) / expected_price

    if slippage > max_slippage:
        raise SlippageExceededError(
            f"Slippage {slippage:.2%} exceeds maximum {max_slippage:.2%}",
            error_code='SLIPPAGE_EXCEEDED',
            expected_price=expected_price,
            actual_price=actual_price,
            details={
                'expected_price': expected_price,
                'actual_price': actual_price,
                'slippage': f"{slippage:.2%}",
                'max_slippage': f"{max_slippage:.2%}",
                'side': side
            }
        )

    return True


# ============================================================================
# 5. CIRCUIT BREAKER & LOSS LIMITS
# ============================================================================

def validate_circuit_breaker(
    total_loss_usd: float,
    threshold: float = CIRCUIT_BREAKER_LOSS_THRESHOLD_USD
) -> bool:
    """
    Check if cumulative losses exceed circuit breaker threshold.

    Circuit breaker prevents catastrophic losses by stopping all trading.

    Args:
        total_loss_usd: Total unrealized + realized losses (negative value)
        threshold: Loss threshold that triggers circuit breaker

    Returns:
        True if losses are within acceptable range

    Raises:
        CircuitBreakerError: If losses exceed threshold
    """
    # Note: total_loss_usd should be negative (e.g., -150.0 for $150 loss)
    if total_loss_usd < 0 and abs(total_loss_usd) > threshold:
        raise CircuitBreakerError(
            f"Cumulative losses ${abs(total_loss_usd):.2f} exceed threshold ${threshold:.2f}",
            error_code='CIRCUIT_BREAKER_TRIGGERED',
            total_loss=total_loss_usd,
            threshold=threshold,
            details={
                'total_loss': f"${abs(total_loss_usd):.2f}",
                'threshold': f"${threshold:.2f}",
                'action': 'Stop all trading immediately'
            }
        )

    return True


# ============================================================================
# 6. SAFE MATHEMATICAL OPERATIONS
# ============================================================================

def safe_decimal_divide(
    numerator: float,
    denominator: float,
    decimals: int = 6
) -> Decimal:
    """
    Safely divide two numbers with proper decimal handling.

    Prevents division by zero and rounding errors.

    Args:
        numerator: Dividend
        denominator: Divisor
        decimals: Decimal places for rounding

    Returns:
        Result as Decimal with proper rounding

    Raises:
        ValueError: If denominator is zero
    """
    if denominator == 0:
        raise ValueError("Cannot divide by zero")

    result = Decimal(str(numerator)) / Decimal(str(denominator))
    # Round down to avoid overestimating our holdings
    return result.quantize(
        Decimal(10) ** -decimals,
        rounding=ROUND_DOWN
    )


def safe_decimal_multiply(
    value1: float,
    value2: float,
    decimals: int = 6
) -> Decimal:
    """
    Safely multiply two numbers with proper decimal handling.

    Args:
        value1: First operand
        value2: Second operand
        decimals: Decimal places for rounding

    Returns:
        Result as Decimal with proper rounding
    """
    result = Decimal(str(value1)) * Decimal(str(value2))
    return result.quantize(
        Decimal(10) ** -decimals,
        rounding=ROUND_DOWN
    )


# ============================================================================
# 7. ASYNC HELPER DECORATORS
# ============================================================================

def async_retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0):
    """
    Decorator for async functions with exponential backoff retry logic.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Base delay between retries (seconds)

    Returns:
        Decorated async function with retry logic
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            delay = base_delay
            last_error = None

            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Attempt {attempt + 1} failed, retrying in {delay}s",
                            extra={
                                'function': func.__name__,
                                'attempt': attempt + 1,
                                'max_retries': max_retries,
                                'error': str(e)
                            }
                        )
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 60)  # Exponential backoff, max 60s

            logger.error(
                f"Function {func.__name__} failed after {max_retries} attempts",
                exc_info=last_error,
                extra={'function': func.__name__, 'attempts': max_retries}
            )
            raise last_error

        return wrapper
    return decorator


def rate_limit(calls_per_second: float):
    """
    Decorator to rate limit async function calls.

    Args:
        calls_per_second: Maximum calls per second

    Returns:
        Decorated async function with rate limiting
    """
    min_interval = 1.0 / calls_per_second
    last_called = [0.0]

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            elapsed = asyncio.get_event_loop().time() - last_called[0]
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)

            last_called[0] = asyncio.get_event_loop().time()
            return await func(*args, **kwargs)

        return wrapper
    return decorator
