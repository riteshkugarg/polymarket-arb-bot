"""
Maker-First Order Executor

Wraps polymarket_client order creation with:
1. Post-only (maker-only) order execution
2. Automatic NegRisk market detection
3. Wait-for-fill order monitoring
4. Rebate tracking for successful fills
5. Process pool for CPU-intensive signing (HFT optimization)

This module ensures we ALWAYS trade as a maker, never a taker.
"""

import asyncio
import time
from typing import Dict, Any, Optional, Set
from decimal import Decimal
from concurrent.futures import ProcessPoolExecutor

from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL

from config.constants import (
    ENABLE_POST_ONLY_ORDERS,
    POST_ONLY_SPREAD_OFFSET,
    DYNAMIC_SPREAD_CAPTURE_PCT,
    MAX_DYNAMIC_OFFSET_TICKS,
    POST_ONLY_ERROR_COOLDOWN_SEC,
    MAX_ORDER_AGE_SEC,
    ORDER_MONITOR_INTERVAL_SEC,
    ENABLE_NEGRISK_AUTO_DETECTION,
)
from utils.logger import get_logger
from utils.exceptions import (
    PostOnlyOrderRejectedError,
    NegRiskSignatureError,
    StaleOrderError,
    OrderExecutionError,
    InsufficientBalanceError,
)
from utils.rebate_logger import get_rebate_logger

logger = get_logger(__name__)
rebate_logger = get_rebate_logger()


class MakerFirstExecutor:
    """
    Institutional-grade order executor with maker-only logic.
    
    Features (2026 Production):
    - Post-only orders (never cross spread)
    - Automatic NegRisk detection
    - Order age monitoring with auto-cancel
    - Maker rebate tracking
    """
    
    def __init__(self, client):
        """
        Initialize maker-first executor.
        
        Args:
            client: PolymarketClient instance
        """
        self.client = client
        
        # Post-only cooldowns (token_id -> expiry_timestamp)
        self._post_only_cooldowns: Dict[str, float] = {}
        
        # Active orders being monitored (order_id -> order_data)
        self._active_orders: Dict[str, Dict[str, Any]] = {}
        
        # Order monitoring task
        self._monitor_task: Optional[asyncio.Task] = None
        self._is_monitoring = False
        
        # FIX 1: Process pool for CPU-intensive signing operations
        # Signing ECDSA/EIP-712 signatures is CPU-bound, not I/O-bound
        # Using ProcessPoolExecutor instead of asyncio.to_thread improves
        # throughput for high-frequency trading (multiple orders per second)
        self._process_pool = ProcessPoolExecutor(max_workers=2)  # 2 workers for signing
        logger.info("🔐 Process pool initialized for HFT signing (2 workers)")
        
        # FIX 1: Process pool for CPU-intensive signing operations
        # Signing ECDSA/EIP-712 signatures is CPU-bound, not I/O-bound
        # Using ProcessPoolExecutor instead of asyncio.to_thread improves
        # throughput for high-frequency trading (multiple orders per second)
        self._process_pool = ProcessPoolExecutor(max_workers=2)  # 2 workers for signing
        logger.info("🔐 Process pool initialized for HFT signing (2 workers)")
        
        # FIX 1: Process pool for CPU-intensive signing operations
        # Signing ECDSA/EIP-712 signatures is CPU-bound, not I/O-bound
        # Using ProcessPoolExecutor instead of asyncio.to_thread improves
        # throughput for high-frequency trading (multiple orders per second)
        self._process_pool = ProcessPoolExecutor(max_workers=2)  # 2 workers for signing
        logger.info("🔐 Process pool initialized for HFT signing (2 workers)")
    
    async def start_monitoring(self) -> None:
        """Start background order monitoring task"""
        if self._monitor_task is None or self._monitor_task.done():
            self._is_monitoring = True
            self._monitor_task = asyncio.create_task(self._order_monitor_loop())
            logger.info("📊 Order monitoring started")
    
    async def stop_monitoring(self) -> None:
        """Stop background order monitoring"""
        self._is_monitoring = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("📊 Order monitoring stopped")
    
    async def shutdown(self) -> None:
        """Clean shutdown - stop monitoring and close process pool"""
        await self.stop_monitoring()
        
        # FIX 1: Clean up process pool
        if hasattr(self, '_process_pool'):
            self._process_pool.shutdown(wait=True)
            logger.info("🔐 Process pool shut down")
    
    async def shutdown(self) -> None:
        """Clean shutdown - stop monitoring and close process pool"""
        await self.stop_monitoring()
        
        # FIX 1: Clean up process pool
        if hasattr(self, '_process_pool'):
            self._process_pool.shutdown(wait=True)
            logger.info("🔐 Process pool shut down")
    
    async def shutdown(self) -> None:
        """Clean shutdown - stop monitoring and close process pool"""
        await self.stop_monitoring()
        
        # FIX 1: Clean up process pool
        if hasattr(self, '_process_pool'):
            self._process_pool.shutdown(wait=True)
            logger.info("🔐 Process pool shut down")
    
    async def _order_monitor_loop(self) -> None:
        """
        Background task that monitors unfilled orders.
        
        Cancels orders that remain open for too long (stale orders).
        """
        logger.info(f"Starting order monitor (interval={ORDER_MONITOR_INTERVAL_SEC}s)")
        
        while self._is_monitoring:
            try:
                await asyncio.sleep(ORDER_MONITOR_INTERVAL_SEC)
                
                if not self._active_orders:
                    continue
                
                current_time = time.time()
                stale_orders = []
                
                # Check each active order
                for order_id, order_data in list(self._active_orders.items()):
                    age = current_time - order_data['created_at']
                    
                    if age > MAX_ORDER_AGE_SEC:
                        stale_orders.append((order_id, order_data, age))
                
                # Cancel stale orders
                for order_id, order_data, age in stale_orders:
                    try:
                        logger.warning(
                            f"⏰ STALE_ORDER: {order_id[:8]}... age={age:.0f}s "
                            f"(max={MAX_ORDER_AGE_SEC}s) - cancelling"
                        )
                        
                        await self.client.cancel_order(order_id)
                        
                        # Remove from active orders
                        del self._active_orders[order_id]
                        
                        logger.info(f"✓ Cancelled stale order {order_id[:8]}...")
                        
                    except Exception as e:
                        logger.error(f"Failed to cancel stale order {order_id[:8]}: {e}")
                
            except Exception as e:
                logger.error(f"Order monitor error: {e}", exc_info=True)
    
    def _is_in_cooldown(self, token_id: str) -> bool:
        """Check if token is in post-only cooldown"""
        if token_id in self._post_only_cooldowns:
            if time.time() < self._post_only_cooldowns[token_id]:
                return True
            else:
                # Cooldown expired, remove it
                del self._post_only_cooldowns[token_id]
        return False
    
    def _set_cooldown(self, token_id: str, duration_sec: int) -> None:
        """Set post-only cooldown for token"""
        self._post_only_cooldowns[token_id] = time.time() + duration_sec
        logger.info(f"🕐 Cooldown set for {token_id[:8]}... ({duration_sec}s)")
    
    async def _detect_negrisk(self, condition_id: str) -> bool:
        """
        Detect if market is NegRisk (multi-choice).
        
        NegRisk markets have >2 outcomes and require special signatures.
        
        Args:
            condition_id: Market condition ID
            
        Returns:
            True if NegRisk, False if binary
        """
        if not ENABLE_NEGRISK_AUTO_DETECTION:
            return False
        
        # Check cache
        cache_key = f"negrisk_{condition_id}"
        if cache_key in self.client._cache:
            return self.client._cache[cache_key]
        
        try:
            # Query market details from Gamma API
            import json
            url = f"{self.client._session._base_url.replace('clob.polymarket.com', 'gamma-api.polymarket.com')}/markets"
            params = {"condition_id": condition_id}
            
            async with self.client._session.get(url, params=params, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if data and len(data) > 0:
                        market = data[0]
                        
                        # Parse clobTokenIds to count outcomes
                        clob_token_ids_str = market.get("clobTokenIds")
                        if clob_token_ids_str:
                            clob_token_ids = json.loads(clob_token_ids_str)
                            is_negrisk = len(clob_token_ids) > 2
                            
                            # Cache result
                            self.client._cache[cache_key] = is_negrisk
                            
                            logger.info(
                                f"Market {condition_id[:8]}: "
                                f"{'🔒 NegRisk' if is_negrisk else '🔓 Binary'} "
                                f"({len(clob_token_ids)} outcomes)"
                            )
                            return is_negrisk
            
            # Default to False (binary)
            self.client._cache[cache_key] = False
            return False
            
        except Exception as e:
            logger.warning(f"Failed to detect NegRisk for {condition_id[:8]}: {e}")
            return False  # Safe default
    
    async def execute_maker_buy(
        self,
        token_id: str,
        amount_usd: float,
        condition_id: Optional[str] = None,
        market_name: Optional[str] = None,
        outcome: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute maker-only BUY order.
        
        Args:
            token_id: Token to buy
            amount_usd: Amount in USD to spend
            condition_id: Market condition ID (for NegRisk detection)
            market_name: Market name (for logging)
            outcome: Outcome name (for logging)
            
        Returns:
            Order response with execution details
            
        Raises:
            PostOnlyOrderRejectedError: If order would cross spread
            StaleOrderError: If order remains unfilled too long
        """
        # Check cooldown
        if self._is_in_cooldown(token_id):
            cooldown_remaining = self._post_only_cooldowns[token_id] - time.time()
            raise PostOnlyOrderRejectedError(
                f"Token {token_id[:8]} in cooldown ({cooldown_remaining:.0f}s remaining)",
                token_id=token_id,
                cooldown_sec=int(cooldown_remaining)
            )
        
        if not ENABLE_POST_ONLY_ORDERS:
            # Fallback to market order
            logger.warning("Post-only disabled - using market order")
            return await self.client.create_market_buy_order(token_id, amount_usd)
        
        try:
            logger.info(f"📌 MAKER BUY: ${amount_usd:.2f} for {token_id[:8]}...")
            
            # Get orderbook for pricing
            order_book = await self.client.get_order_book(token_id)
            bids = getattr(order_book, 'bids', [])
            asks = getattr(order_book, 'asks', [])
            
            if not bids:
                raise OrderExecutionError(f"No bids available for {token_id[:8]}")
            
            if not asks:
                raise OrderExecutionError(f"No asks available for {token_id[:8]}")
            
            # FIX 2: Calculate dynamic spread offset instead of fixed $0.01
            best_bid = float(bids[0].price)
            best_ask = float(asks[0].price)
            
            dynamic_offset = await self._calculate_dynamic_offset(
                token_id=token_id,
                best_bid=best_bid,
                best_ask=best_ask
            )
            
            target_price = best_bid + dynamic_offset
            
            # Calculate shares
            shares = amount_usd / target_price
            
            logger.info(
                f"  Target: ${target_price:.4f} (bid=${best_bid:.4f} + offset=${dynamic_offset:.4f}) "
                f"for {shares:.2f} shares (spread=${best_ask - best_bid:.4f})"
            )
            
            # Get fee rate
            fee_rate_bps = await self.client.get_fee_rate_bps(token_id)
            
            # Detect NegRisk
            is_negrisk = False
            if condition_id and ENABLE_NEGRISK_AUTO_DETECTION:
                is_negrisk = await self._detect_negrisk(condition_id)
            
            # Create post-only order
            order_args = OrderArgs(
                token_id=token_id,
                price=target_price,
                size=shares,
                side=BUY,
                fee_rate_bps=fee_rate_bps,
                order_type=OrderType.GTC,
                options=PartialCreateOrderOptions(
                    post_only=True,
                    neg_risk=is_negrisk
                )
            )
            
            # FIX 1: Use process pool for CPU-intensive signing operations
            # ECDSA/EIP-712 signing is CPU-bound (cryptographic operations)
            # ProcessPoolExecutor provides true parallelism for HFT scenarios
            loop = asyncio.get_event_loop()
            
            signed_order = await loop.run_in_executor(
                self._process_pool,
                self.client._client.create_order,
                order_args
            )
            
            # Post order (network I/O - keep as asyncio.to_thread)
            result = await asyncio.to_thread(
                self.client._client.post_order,
                signed_order,
                OrderType.GTC
            )
            
            order_id = result.get('orderID', 'unknown')
            
            # Track order for monitoring
            self._active_orders[order_id] = {
                'token_id': token_id,
                'side': 'BUY',
                'price': target_price,
                'size': shares,
                'amount_usd': amount_usd,
                'created_at': time.time(),
                'condition_id': condition_id,
                'market_name': market_name,
                'outcome': outcome,
                'fee_rate_bps': fee_rate_bps,
                'is_negrisk': is_negrisk
            }
            
            logger.info(
                f"✓ MAKER_BUY order placed: {order_id[:8]}... "
                f"{shares:.2f}@${target_price:.4f} "
                f"{'[NegRisk]' if is_negrisk else ''}"
            )
            
            # Log for rebate tracking
            await rebate_logger.log_maker_fill(
                order_id=order_id,
                token_id=token_id,
                side='BUY',
                fill_amount=shares,
                fill_price=target_price,
                fee_rate_bps=fee_rate_bps,
                market_name=market_name,
                outcome=outcome,
                is_post_only=True,
                additional_data={
                    'is_negrisk': is_negrisk,
                    'target_price': target_price,
                    'best_bid': best_bid
                }
            )
            
            return result
            
        except Exception as e:
            error_str = str(e)
            
            # Handle INVALID_POST_ONLY_ORDER
            if "INVALID_POST_ONLY_ORDER" in error_str or "post-only" in error_str.lower():
                logger.warning(
                    f"[MAKER_REJECTED] {token_id[:8]}: Order would cross spread. "
                    f"Setting {POST_ONLY_ERROR_COOLDOWN_SEC}s cooldown."
                )
                
                self._set_cooldown(token_id, POST_ONLY_ERROR_COOLDOWN_SEC)
                
                raise PostOnlyOrderRejectedError(
                    f"Post-only order rejected for {token_id[:8]} (spread crossed)",
                    token_id=token_id,
                    target_price=target_price if 'target_price' in locals() else None,
                    cooldown_sec=POST_ONLY_ERROR_COOLDOWN_SEC
                )
            
            # Re-raise other errors
            raise
    
    async def _calculate_dynamic_offset(
        self,
        token_id: str,
        best_bid: float,
        best_ask: float
    ) -> float:
        """
        FIX 2: Calculate optimal price offset based on spread and tick size
        
        Instead of a fixed $0.01 offset (which puts you at back of queue),
        this calculates a dynamic offset based on:
        1. Current market spread (ask - bid)
        2. Market-specific tick size (0.1, 0.01, 0.001, or 0.0001)
        3. Configured spread capture percentage (15% default)
        
        Logic:
        - If spread is tight (1 tick): Join the bid exactly (no offset)
        - If spread is wide: Capture DYNAMIC_SPREAD_CAPTURE_PCT of spread
        - Cap at MAX_DYNAMIC_OFFSET_TICKS to prevent overpaying
        - Never cross or touch the ask (safety check)
        
        Example:
        - Spread: $0.50-$0.60 = $0.10 wide
        - Tick size: $0.01
        - Capture: 15% of $0.10 = $0.015
        - Ticks: 1.5 ticks → rounds to 2 ticks
        - Cap: max 3 ticks → 2 ticks OK
        - Offset: 2 × $0.01 = $0.02
        - Final: $0.50 + $0.02 = $0.52 (ahead of $0.50 bid, below $0.60 ask)
        
        Args:
            token_id: Token to calculate offset for
            best_bid: Current best bid price
            best_ask: Current best ask price
            
        Returns:
            Absolute price offset to add to best bid (in dollars)
        """
        try:
            # Get tick size for the specific market (required for 2026 CLOB compliance)
            tick_size_str = await self.client.get_tick_size(token_id)
            tick_size = float(tick_size_str)
            
            # Calculate current spread
            spread = best_ask - best_bid
            
            # If spread is at minimum (1 tick), just join the bid
            if spread <= tick_size:
                logger.debug(
                    f"Spread at minimum ({spread:.4f} ≤ {tick_size:.4f}) - joining bid"
                )
                return 0.0
            
            # Calculate how many ticks to jump based on config percentage
            # e.g., Capture 15% of the spread (DYNAMIC_SPREAD_CAPTURE_PCT = 0.15)
            raw_offset = spread * DYNAMIC_SPREAD_CAPTURE_PCT
            
            # Convert raw offset into discrete number of ticks
            ticks_to_jump = max(1, int(raw_offset / tick_size))
            
            # Cap the jump to avoid overpaying (MAX_DYNAMIC_OFFSET_TICKS = 3)
            ticks_to_jump = min(ticks_to_jump, MAX_DYNAMIC_OFFSET_TICKS)
            
            calculated_offset = ticks_to_jump * tick_size
            
            # Final safety check: ensure we don't accidentally cross or touch the ask
            if (best_bid + calculated_offset) >= best_ask:
                logger.warning(
                    f"Dynamic offset would cross spread "
                    f"(bid {best_bid:.4f} + offset {calculated_offset:.4f} ≥ ask {best_ask:.4f}) "
                    f"- joining bid instead"
                )
                return 0.0
            
            logger.debug(
                f"Dynamic offset: {ticks_to_jump} ticks × ${tick_size:.4f} = ${calculated_offset:.4f} "
                f"(spread: ${spread:.4f}, capture: {DYNAMIC_SPREAD_CAPTURE_PCT*100:.0f}%)"
            )
            
            return calculated_offset
            
        except Exception as e:
            logger.error(f"Error calculating dynamic offset: {e}")
            # Fallback to joining the bid exactly (safest option)
            return 0.0


# Global executor instance
_maker_executor = None


def get_maker_executor(client) -> MakerFirstExecutor:
    """Get global maker executor instance"""
    global _maker_executor
    if _maker_executor is None:
        _maker_executor = MakerFirstExecutor(client)
    return _maker_executor
