"""
Custom Exception Classes for Polymarket Arbitrage Bot

Provides hierarchy of specific exceptions for different failure scenarios,
enabling precise error handling and recovery strategies throughout the bot.

Exception Hierarchy:
├── PolymarketBotError (Base)
│   ├── ConfigurationError
│   ├── AuthenticationError
│   ├── APIError
│   │   ├── RateLimitError
│   │   ├── APITimeoutError
│   │   └── InvalidResponseError
│   ├── TradingError
│   │   ├── InsufficientBalanceError
│   │   ├── OrderRejectionError
│   │   ├── InvalidOrderError
│   │   └── FOKOrderNotFilledError
│   ├── StrategyError
│   ├── CircuitBreakerError
│   ├── HealthCheckError
│   └── DataValidationError
"""

from typing import Optional, Dict, Any


class PolymarketBotError(Exception):
    """
    Base exception for all Polymarket bot errors.
    All other exceptions inherit from this.
    Enables catching all bot errors with: except PolymarketBotError
    """

    def __init__(
        self,
        message: str,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        original_error: Optional[Exception] = None
    ):
        """
        Initialize bot error with structured information.

        Args:
            message: Human-readable error message
            error_code: Error code for classification (e.g., 'FOK_NOT_FILLED')
            details: Additional context dict
            original_error: Original exception that caused this (for error chaining)
        """
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        self.original_error = original_error
        super().__init__(self.message)

    def __str__(self) -> str:
        error_str = f"{self.__class__.__name__}: {self.message}"
        if self.error_code:
            error_str += f" (Code: {self.error_code})"
        if self.details:
            error_str += f" | Details: {self.details}"
        return error_str


# ============================================================================
# CONFIGURATION & INITIALIZATION ERRORS
# ============================================================================

class ConfigurationError(PolymarketBotError):
    """
    Raised when configuration is invalid or incomplete.
    Examples: Missing AWS credentials, invalid wallet address, bad constants
    Action: Fix configuration and restart bot
    """
    pass


class AuthenticationError(PolymarketBotError):
    """
    Raised when authentication fails.
    Examples: Invalid private key, expired L2 credentials, bad signature
    Action: Verify credentials in AWS Secrets Manager
    """
    pass


# ============================================================================
# API & NETWORK ERRORS
# ============================================================================

class APIError(PolymarketBotError):
    """
    Base exception for Polymarket API errors.
    Includes HTTP status code and response data for debugging.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_data: Optional[Dict[str, Any]] = None,
        **kwargs
    ):
        self.status_code = status_code
        self.response_data = response_data
        super().__init__(message, **kwargs)


class RateLimitError(APIError):
    """
    Raised when API rate limit is exceeded (HTTP 429).
    Bot should backoff exponentially and retry after rate limit window.
    Action: Implement exponential backoff retry logic
    """
    pass


class APITimeoutError(APIError):
    """
    Raised when API request times out.
    May indicate network issues or API server problems.
    Action: Retry with exponential backoff
    """
    pass


class InvalidResponseError(APIError):
    """
    Raised when API response cannot be parsed or is invalid.
    Indicates potential API changes or data corruption.
    Action: Log response and alert operator
    """
    pass


# ============================================================================
# TRADING & ORDER ERRORS
# ============================================================================

class TradingError(PolymarketBotError):
    """Base exception for trading-related errors"""
    pass


class OrderExecutionError(TradingError):
    """
    Raised when order execution fails for any reason.
    Generic error for order placement, validation, or execution issues.
    Action: Log error details and retry or skip order
    """
    pass


class InsufficientBalanceError(TradingError):
    """
    Raised when attempting to place order without sufficient USDC balance.
    Bot should check balance before placing orders.
    
    Action: Stop trading until more USDC is deposited
    """

    def __init__(
        self,
        message: str,
        required: Optional[float] = None,
        available: Optional[float] = None,
        **kwargs
    ):
        self.required = required
        self.available = available
        super().__init__(message, **kwargs)


class OrderRejectionError(TradingError):
    """
    Raised when order is rejected by Polymarket exchange.
    Includes specific Polymarket error codes for debugging.
    
    Common Polymarket error codes:
    - FOK_ORDER_NOT_FILLED_ERROR: No liquidity for FOK order
    - INVALID_ORDER_NOT_ENOUGH_BALANCE: Insufficient USDC
    - INVALID_ORDER_EXPIRATION: Order expiration time invalid
    - MARKET_NOT_READY: Market not in tradeable state
    """

    def __init__(
        self,
        message: str,
        order_data: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        **kwargs
    ):
        self.order_data = order_data
        super().__init__(message, error_code=error_code, **kwargs)


class InvalidOrderError(TradingError):
    """
    Raised when order parameters are invalid.
    Examples: Price outside valid range, size below minimum, bad market
    Action: Review and fix order parameters before retrying
    """
    pass


class FOKOrderNotFilledError(OrderRejectionError):
    """
    Raised when FOK (Fill-Or-Kill) order cannot be fully filled.

    Per Polymarket support: FOK orders don't fail silently.
    Error code: FOK_ORDER_NOT_FILLED_ERROR
    Message: "order couldn't be fully filled, FOK orders are fully filled/killed"

    This is NOT a critical error - it means no immediate match exists.
    Bot should handle gracefully and retry or skip.
    """

    def __init__(
        self,
        message: str,
        token_id: Optional[str] = None,
        amount: Optional[float] = None,
        **kwargs
    ):
        self.token_id = token_id
        self.amount = amount
        super().__init__(message, error_code="FOK_ORDER_NOT_FILLED_ERROR", **kwargs)


class SlippageExceededError(TradingError):
    """
    Raised when slippage exceeds acceptable limits.
    Action: Increase slippage tolerance or use limit orders instead
    """

    def __init__(
        self,
        message: str,
        expected_price: Optional[float] = None,
        actual_price: Optional[float] = None,
        **kwargs
    ):
        self.expected_price = expected_price
        self.actual_price = actual_price
        super().__init__(message, **kwargs)


class PriceGuardError(TradingError):
    """
    Raised when price guard check fails (price too far from reference).
    Action: Wait for better pricing or increase guard threshold
    """

    def __init__(
        self,
        message: str,
        target_price: Optional[float] = None,
        reference_price: Optional[float] = None,
        max_deviation: Optional[float] = None
    ):
        super().__init__(message)
        self.target_price = target_price
        self.reference_price = reference_price
        self.max_deviation = max_deviation


class PostOnlyOrderRejectedError(OrderRejectionError):
    """
    Raised when post-only order is rejected because it would cross the spread.
    
    Error code: INVALID_POST_ONLY_ORDER
    
    This means our limit order price would immediately execute as a taker order,
    but we specified post_only=True to ensure maker-only execution.
    
    Action: Wait for next price update (cooldown period) before retrying.
    Do NOT retry immediately - this prevents becoming a taker.
    """
    
    def __init__(
        self,
        message: str,
        token_id: Optional[str] = None,
        target_price: Optional[float] = None,
        cooldown_sec: Optional[int] = None,
        **kwargs
    ):
        self.token_id = token_id
        self.target_price = target_price
        self.cooldown_sec = cooldown_sec
        super().__init__(
            message, 
            error_code="INVALID_POST_ONLY_ORDER",
            **kwargs
        )


class NegRiskSignatureError(OrderRejectionError):
    """
    Raised when NegRisk market requires special signature but flag was not set.
    
    NegRisk markets (multi-choice with >2 outcomes) require:
    - neg_risk=True in PartialCreateOrderOptions
    - Special signature validation on CLOB
    
    Action: Auto-detect NegRisk markets and set flag before signing.
    """
    
    def __init__(
        self,
        message: str,
        condition_id: Optional[str] = None,
        outcome_count: Optional[int] = None,
        **kwargs
    ):
        self.condition_id = condition_id
        self.outcome_count = outcome_count
        super().__init__(
            message,
            error_code="NEGRISK_SIGNATURE_REQUIRED",
            **kwargs
        )


class StaleOrderError(TradingError):
    """
    Raised when order remains unfilled for too long (stale).
    
    Indicates order price is no longer competitive or market has moved.
    
    Action: Cancel order and recalculate pricing for current market conditions.
    """
    
    def __init__(
        self,
        message: str,
        order_id: Optional[str] = None,
        age_seconds: Optional[int] = None,
        max_age_seconds: Optional[int] = None,
        **kwargs
    ):
        self.order_id = order_id
        self.age_seconds = age_seconds
        self.max_age_seconds = max_age_seconds
        super().__init__(
            message,
            error_code="ORDER_TOO_OLD",
            **kwargs
        )


class PriceGuardError(TradingError):
    """
    Raised when price guard check fails (price too far from reference).
    Action: Wait for better pricing or increase guard threshold
    """

    def __init__(
        self,
        message: str,
        target_price: Optional[float] = None,
        current_price: Optional[float] = None,
        **kwargs
    ):
        self.target_price = target_price
        self.current_price = current_price
        super().__init__(message, **kwargs)


# ============================================================================
# STRATEGY & BUSINESS LOGIC ERRORS
# ============================================================================

class StrategyError(PolymarketBotError):
    """
    Raised when strategy encounters an error during execution.
    Examples: Failed to analyze market, invalid opportunity structure
    """
    pass


class CircuitBreakerError(PolymarketBotError):
    """
    Raised when circuit breaker is triggered (e.g., daily loss limit exceeded).
    Bot stops trading until manual intervention or daily reset.
    
    Action: Stop trading, review losses, restart bot after reset
    """

    def __init__(
        self,
        message: str,
        total_loss: Optional[float] = None,
        threshold: Optional[float] = None,
        **kwargs
    ):
        self.total_loss = total_loss
        self.threshold = threshold
        super().__init__(message, **kwargs)


# ============================================================================
# MONITORING & HEALTH ERRORS
# ============================================================================

class HealthCheckError(PolymarketBotError):
    """
    Raised when health check fails.
    Examples: API unavailable, database disconnected, memory too high
    Action: Investigate system health and restart if needed
    """
    pass


# ============================================================================
# DATA & VALIDATION ERRORS
# ============================================================================

class DataValidationError(PolymarketBotError):
    """
    Raised when data validation fails.
    Examples: Invalid token ID, bad market structure, corrupted position data
    """
    pass


class NetworkError(PolymarketBotError):
    """Raised when network/RPC calls fail"""
    def __init__(self, message: str, retry_count: int = 0):
        self.retry_count = retry_count
        super().__init__(message)


class TimeoutError(PolymarketBotError):
    """Raised when operations timeout"""
    pass


class HealthCheckError(PolymarketBotError):
    """Raised when health checks fail"""
    pass
