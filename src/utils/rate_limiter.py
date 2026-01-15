"""
Token Bucket Rate Limiter - Institutional-Grade Request Throttling

Implements a token-bucket algorithm for smooth rate limiting with burst capacity.
Superior to static sleep timers for high-frequency trading applications.

Mathematical Model:
==================
- Bucket capacity: Maximum burst size (tokens)
- Refill rate: Tokens added per second
- Token cost: Number of tokens consumed per request

Algorithm:
1. Request arrives → Check if tokens available
2. If tokens >= cost → Consume tokens, allow request
3. If tokens < cost → Wait until bucket refills
4. Background: Continuously refill bucket at constant rate

Advantages over static sleep:
- Allows bursts up to bucket capacity
- Smoother rate limiting (no fixed delays)
- Better API utilization (fills "holes" in traffic)
- Industry standard for API rate limiting

Example (10 req/sec, burst 20):
- Can send 20 requests instantly (burst)
- Then throttles to 10 req/sec sustained
- If idle for 2 seconds, accumulates 20 tokens again

Author: Institutional HFT Team
Date: January 2026
"""

import time
import asyncio
from typing import Final
from decimal import Decimal


class TokenBucketRateLimiter:
    """
    Token Bucket Rate Limiter with asynchronous support.
    
    Attributes:
        rate: Tokens per second (sustained rate)
        capacity: Maximum burst capacity (tokens)
        tokens: Current token count
        last_update: Last refill timestamp
    """
    
    def __init__(self, rate: float, capacity: float):
        """
        Initialize token bucket rate limiter.
        
        Args:
            rate: Tokens per second (sustained rate, e.g., 10.0 = 10 req/sec)
            capacity: Maximum burst capacity (e.g., 20.0 = 20 req burst)
        
        Example:
            # Allow 10 requests per second with 20-request burst
            limiter = TokenBucketRateLimiter(rate=10.0, capacity=20.0)
        """
        self.rate: Final[float] = rate
        self.capacity: Final[float] = capacity
        self.tokens: float = capacity  # Start with full bucket
        self.last_update: float = time.time()
        self._lock = asyncio.Lock()
    
    def _refill(self) -> None:
        """
        Refill bucket based on elapsed time.
        
        Tokens added = rate × elapsed_time
        Capped at bucket capacity.
        """
        now = time.time()
        elapsed = now - self.last_update
        
        # Add tokens based on elapsed time
        self.tokens = min(self.capacity, self.tokens + (self.rate * elapsed))
        self.last_update = now
    
    async def acquire(self, cost: float = 1.0) -> None:
        """
        Acquire tokens (async, blocks if insufficient).
        
        Args:
            cost: Number of tokens to consume (default: 1.0)
        
        Behavior:
            - If tokens available: Consume and return immediately
            - If tokens insufficient: Wait until bucket refills
        
        Example:
            await limiter.acquire()  # Wait for 1 token
            # Execute request
        """
        async with self._lock:
            while True:
                self._refill()
                
                if self.tokens >= cost:
                    # Sufficient tokens - consume and proceed
                    self.tokens -= cost
                    return
                
                # Insufficient tokens - calculate wait time
                deficit = cost - self.tokens
                wait_time = deficit / self.rate
                
                # Sleep until bucket refills
                await asyncio.sleep(wait_time)
    
    def try_acquire(self, cost: float = 1.0) -> bool:
        """
        Try to acquire tokens (non-blocking).
        
        Args:
            cost: Number of tokens to consume (default: 1.0)
        
        Returns:
            True if tokens consumed, False if insufficient
        
        Example:
            if limiter.try_acquire():
                # Execute request
            else:
                # Rate limit hit - skip or defer
        """
        self._refill()
        
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        
        return False
    
    def get_available_tokens(self) -> float:
        """
        Get current token count (non-blocking).
        
        Returns:
            Number of tokens currently available
        """
        self._refill()
        return self.tokens
    
    def reset(self) -> None:
        """Reset bucket to full capacity."""
        self.tokens = self.capacity
        self.last_update = time.time()


# ============================================================================
# INSTITUTIONAL STANDARD RATE LIMITERS (Polymarket API)
# ============================================================================

# L2 Order Placement Rate Limiter (POST /order)
# Polymarket limit: 3500 req/10s burst, 36000 req/10min sustained
# Conservative: Target 10 req/sec (well below 350 req/sec burst)
ORDER_PLACEMENT_RATE_LIMITER = TokenBucketRateLimiter(
    rate=10.0,      # 10 requests per second sustained
    capacity=20.0   # Allow 20-request burst
)

# L2 Order Cancellation Rate Limiter (DELETE /order)
# Polymarket limit: 3000 req/10s burst, 30000 req/10min sustained
# Conservative: Target 10 req/sec (same as placement for simplicity)
ORDER_CANCELLATION_RATE_LIMITER = TokenBucketRateLimiter(
    rate=10.0,      # 10 requests per second sustained
    capacity=20.0   # Allow 20-request burst
)

# CLOB Read Rate Limiter (GET /book, /price)
# Polymarket limit: 1500 req/10s (150 req/sec)
# Conservative: Target 50 req/sec (well below limit)
CLOB_READ_RATE_LIMITER = TokenBucketRateLimiter(
    rate=50.0,      # 50 requests per second sustained
    capacity=100.0  # Allow 100-request burst
)
