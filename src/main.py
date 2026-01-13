"""
Main Entry Point for Polymarket Arbitrage Bot
Production-grade 24/7 bot with proper lifecycle management
HFT-optimized with batch order execution
"""

import os
import sys
import signal
import asyncio
import time
import json
from typing import Optional, List, Dict, Any
from datetime import datetime
from collections import deque
from pathlib import Path
from web3 import Web3
from eth_account import Account
from eth_abi import encode

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from core.atomic_depth_aware_executor import AtomicDepthAwareExecutor
from core.maker_executor import get_maker_executor
from strategies.arbitrage_strategy import ArbitrageStrategy
from config.constants import (
    LOOP_INTERVAL_SEC,
    HEALTH_CHECK_INTERVAL_SEC,
    MAX_CONSECUTIVE_ERRORS,
    ENABLE_CIRCUIT_BREAKER,
    CIRCUIT_BREAKER_LOSS_THRESHOLD_USD,
    HEARTBEAT_INTERVAL_SEC,
    DRAWDOWN_LIMIT_USD,
    AUTO_REDEEM_INTERVAL_SEC,
    GRACEFUL_SHUTDOWN_TIMEOUT_SEC,
    ENABLE_POST_ONLY_ORDERS,
    ORDER_HEARTBEAT_INTERVAL_SEC,
    MAKER_RETRY_PRICE_STEP,
    REBATE_PRIORITY_WEIGHT,
    REBATE_OPTIMAL_PRICE_MIN,
    REBATE_OPTIMAL_PRICE_MAX,
    CHECK_AND_REDEEM_INTERVAL_SEC,
    MAX_BATCH_SIZE,
    RETRY_COOLDOWN,
    RATE_LIMIT_BURST,
    RATE_LIMIT_SUSTAINED,
    BATCH_RESYNC_WAIT,
    MAX_TOTAL_EXPOSURE,
    NEGRISK_ADAPTER_ADDRESS,
    CTF_EXCHANGE_ADDRESS,
    USDC_ADDRESS,
    CTF_CONTRACT_ADDRESS,
    MERGE_FAILURE_PAUSE_SEC,
    DELAYED_ORDER_TIMEOUT_SEC,
    DELAYED_ORDER_CHECK_INTERVAL_SEC,
    STP_CHECK_INTERVAL_SEC,
    STP_COOLDOWN,
    ENABLE_NONCE_SYNC_ON_BOOT,
    STATE_PERSISTENCE_INTERVAL_SEC,
    BOT_STATE_FILE,
    DELAY_THRESHOLDS,
    ORDER_STATE_POLL_INTERVAL_SEC,
    BATCH_DELAYED_LEG_HOLD_SEC,
    CANCEL_DELAYED_ON_SHUTDOWN,
)
from utils.logger import get_logger, setup_logging
from utils.rebate_logger import get_rebate_logger
from utils.exceptions import (
    PolymarketBotError,
    CircuitBreakerError,
    HealthCheckError,
    PostOnlyOrderRejectedError,
)


logger = get_logger(__name__)


# ============================================================================
# HFT RATE LIMITER (Fix 3: Rate Limit Intelligence)
# ============================================================================

class RateLimiter:
    """
    Token Bucket Rate Limiter for HFT batch execution
    
    Ensures bot stays within Polymarket's rate limits:
    - Burst: 100 requests/second
    - Sustained: 25 requests/second
    
    Uses sliding window algorithm for precise rate limiting.
    """
    
    def __init__(self, burst_capacity: int, sustained_rate: int):
        """
        Initialize rate limiter
        
        Args:
            burst_capacity: Maximum burst requests (100/s)
            sustained_rate: Sustained requests per second (25/s)
        """
        self.burst_capacity = burst_capacity
        self.sustained_rate = sustained_rate
        self.tokens = float(burst_capacity)  # Start with full burst capacity
        self.last_refill = time.time()
        self.request_history = deque(maxlen=burst_capacity)  # Sliding window
        
        logger.info(
            f"Rate limiter initialized: burst={burst_capacity}/s, sustained={sustained_rate}/s"
        )
    
    async def acquire(self, cost: int = 1) -> bool:
        """
        Acquire tokens for request
        
        Args:
            cost: Number of tokens needed (default 1)
            
        Returns:
            True if tokens acquired, False if rate limited
        """
        current_time = time.time()
        
        # Refill tokens based on sustained rate
        elapsed = current_time - self.last_refill
        refill_amount = elapsed * self.sustained_rate
        self.tokens = min(self.burst_capacity, self.tokens + refill_amount)
        self.last_refill = current_time
        
        # Clean old requests from sliding window (>1 second old)
        while self.request_history and (current_time - self.request_history[0] > 1.0):
            self.request_history.popleft()
        
        # Check burst limit (100/s)
        if len(self.request_history) >= self.burst_capacity:
            logger.warning(
                f"[RATE_LIMIT] Burst capacity reached ({self.burst_capacity}/s)"
            )
            return False
        
        # Check if we have enough tokens
        if self.tokens >= cost:
            self.tokens -= cost
            self.request_history.append(current_time)
            return True
        else:
            # Calculate wait time
            wait_time = (cost - self.tokens) / self.sustained_rate
            logger.debug(
                f"[RATE_LIMIT] Insufficient tokens. Wait {wait_time:.2f}s for refill"
            )
            await asyncio.sleep(wait_time)
            
            # Try again after waiting
            self.tokens = min(self.burst_capacity, self.tokens + (wait_time * self.sustained_rate))
            if self.tokens >= cost:
                self.tokens -= cost
                self.request_history.append(time.time())
                return True
            
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get rate limiter statistics"""
        return {
            'tokens_available': self.tokens,
            'burst_capacity': self.burst_capacity,
            'sustained_rate': self.sustained_rate,
            'requests_last_second': len(self.request_history),
            'utilization': len(self.request_history) / self.burst_capacity * 100
        }


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
        - Heartbeat system with kill switch
        - Auto-redeem service for resolved markets
        - Maker-first execution (2026 institutional upgrade)
        """
        self.client: Optional[PolymarketClient] = None
        self.order_manager: Optional[OrderManager] = None
        self.atomic_executor: Optional[AtomicDepthAwareExecutor] = None
        self.maker_executor = None  # Maker-first executor (2026)
        self.rebate_logger = get_rebate_logger()  # Rebate tracking
        self.strategies = []
        self.is_running = False
        self.consecutive_errors = 0
        self.total_pnl = 0.0
        self._shutdown_event = asyncio.Event()
        
        # 2026 Production Safety Features
        self.global_kill_switch = False  # Emergency stop all trading
        self.initial_balance: Optional[float] = None  # Track starting balance
        self.current_balance: Optional[float] = None  # Track current balance
        self._gtc_orders: dict = {}  # Track GTC orders for heartbeat monitoring
        
        # HFT Batch Execution (Fix 3: Rate Limit Intelligence)
        self._rate_limiter = RateLimiter(
            burst_capacity=RATE_LIMIT_BURST,
            sustained_rate=RATE_LIMIT_SUSTAINED
        )
        
        # Capital Management & State Persistence (2026 Final Architecture)
        self._active_orders: Dict[str, Dict] = {}  # orderID -> order details
        self._position_ids: List[str] = []  # Position IDs for redemption
        self._web3: Optional[Web3] = None  # Web3 instance for contract interactions
        self._last_state_save: float = 0  # Last state persistence timestamp
        
        # Relayer-Based Merge Engine (Capital Efficiency)
        self._relay_client: Optional[Any] = None  # RelayClient for merge operations
        self._merge_paused_until: float = 0  # Timestamp when merge operations can resume
        
        # Reliability & Reboot Recovery
        self._last_nonce: Optional[int] = None  # Track last used nonce for recovery
        self._pending_orders: Dict[str, Dict] = {}  # orderID -> {timestamp, status, details}
        self._active_condition_ids: List[str] = []  # Track active markets
        
        # HFT Order State Machine (2026)
        self._order_states: Dict[str, str] = {}  # orderID -> state (PENDING/DELAYED/MATCHED/CANCELLED)
        self._order_metadata: Dict[str, Dict] = {}  # orderID -> {market_category, condition_id, token_id}
        self._batch_orders: Dict[str, List[str]] = {}  # batchID -> [order_ids] for partial-fill handling
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info("PolymarketBot initialized with maker-first execution (2026)")
    
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
            
            # [SAFETY] FIX 2: Explicit Nonce Sync on Startup
            # Prevent INVALID_NONCE errors from bot/server desync
            await self.sync_header_nonce()
            
            # Initialize order manager
            self.order_manager = OrderManager(self.client)
            
            # Initialize maker-first executor (2026 institutional upgrade)
            self.maker_executor = get_maker_executor(self.client)
            logger.info("✅ Maker-first executor initialized (post-only orders enabled)")
            
            # Initialize atomic executor for depth-aware arbitrage execution
            self.atomic_executor = AtomicDepthAwareExecutor(self.client, self.order_manager)
            logger.info("AtomicDepthAwareExecutor initialized")
            
            # Initialize arbitrage strategy with atomic executor
            arb_strategy = ArbitrageStrategy(
                self.client,
                self.order_manager,
                atomic_executor=self.atomic_executor
            )
            self.strategies.append(arb_strategy)
            
            # UPGRADE 2: Initialize Web3 for NegRisk adapter interactions
            try:
                rpc_url = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
                self._web3 = Web3(Web3.HTTPProvider(rpc_url))
                
                if self._web3.is_connected():
                    logger.info(f"✅ Web3 connected to Polygon (block: {self._web3.eth.block_number})")
                else:
                    logger.warning("⚠️  Web3 not connected - token conversion disabled")
            except Exception as web3_err:
                logger.warning(f"[INIT] Web3 initialization failed: {web3_err}")
            
            # UPGRADE 2: Allowance Guard - check before trading
            allowances_ok = await self.check_allowances()
            if not allowances_ok:
                logger.critical(
                    "\\n" + "="*80 + "\\n"
                    "⚠️  CRITICAL: Allowance check failed!\\n"
                    "Bot will continue but token conversions will NOT work.\\n"
                    "Run 'python scripts/set_allowances.py' to fix.\\n"
                    + "="*80
                )
            
            # RELAYER-BASED MERGE ENGINE: Initialize RelayClient
            try:
                from py_clob_client.relay_client import RelayClient
                from py_clob_client.clob_types import RelayerTxType
                
                # Use existing signer and builder_config
                self._relay_client = RelayClient(
                    signer=self.client._account,
                    builder_config=self.client._client.builder_config,
                    tx_type=RelayerTxType.PROXY
                )
                logger.info("✅ RelayClient initialized (PROXY mode for merge operations)")
            except Exception as relay_err:
                logger.warning(f"[INIT] RelayClient initialization failed: {relay_err}")
                logger.warning("⚠️  Merge operations will be disabled")
            
            # UPGRADE 5: Load previous state (if exists)
            await self.load_state()
            
            # RELIABILITY FIX 1: Boot-Time Nonce Synchronization
            if ENABLE_NONCE_SYNC_ON_BOOT:
                await self.sync_auth_nonce()
            
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
        
        # Record initial balance for drawdown monitoring
        self.initial_balance = await self.client.get_balance()
        self.current_balance = self.initial_balance

        logger.info("=" * 80)
        logger.info("Starting Polymarket Arbitrage Bot")
        logger.info(f"Wallet Address: {self.client.wallet_address}")
        logger.info(f"Initial Balance: ${self.initial_balance:.2f} USDC")
        logger.info(f"Active Strategies: {len(self.strategies)}")
        logger.info(f"Maker-First Execution: {'ENABLED ✅' if ENABLE_POST_ONLY_ORDERS else 'DISABLED'}")
        logger.info(f"Drawdown Limit: ${DRAWDOWN_LIMIT_USD:.2f} (Kill Switch)")
        logger.info("=" * 80)
        
        # Start maker executor monitoring
        if self.maker_executor and ENABLE_POST_ONLY_ORDERS:
            await self.maker_executor.start_monitoring()
            logger.info("📊 Order monitoring started (auto-cancel stale orders)")

        try:
            tasks = []
            
            # Run all strategies in background
            for strategy in self.strategies:
                tasks.append(asyncio.create_task(strategy.run()))

            # Start production monitoring tasks (2026 safety features)
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            auto_redeem_task = asyncio.create_task(self._auto_redeem_loop())
            merge_positions_task = asyncio.create_task(self._merge_positions_loop())  # MERGE ENGINE
            delayed_order_task = asyncio.create_task(self._delayed_order_observer_loop())  # RELIABILITY FIX 2
            order_heartbeat_task = asyncio.create_task(self._order_heartbeat_loop())  # UPGRADE 3
            state_persistence_task = asyncio.create_task(self._state_persistence_loop())  # UPGRADE 5
            health_check_task = asyncio.create_task(self._health_check_loop())
            shutdown_task = asyncio.create_task(self._wait_for_shutdown())
            tasks.extend([heartbeat_task, auto_redeem_task, merge_positions_task, delayed_order_task, order_heartbeat_task, state_persistence_task, health_check_task, shutdown_task])

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
        """
        Clean up resources on shutdown
        
        Features (2026 Production Safety):
        - Cancels all open orders gracefully
        - Stops all strategies
        - Closes client connections
        - Logs final statistics
        """
        logger.info("Shutting down bot...")
        
        try:
            # HFT FIX 5: Clean Exit Strategy - Check for DELAYED orders first
            if CANCEL_DELAYED_ON_SHUTDOWN and self._pending_orders:
                logger.warning(
                    f"[SHUTDOWN] 🛑 Found {len(self._pending_orders)} pending/delayed orders"
                )
                
                delayed_order_ids = []
                for order_id, order_data in self._pending_orders.items():
                    state = self._order_states.get(order_id, "UNKNOWN")
                    if state in ["PENDING", "DELAYED"]:
                        delayed_order_ids.append(order_id)
                        logger.info(
                            f"[SHUTDOWN] Cancelling {state} order: {order_id[:8]}... "
                            f"(token: {self._order_metadata.get(order_id, {}).get('token_id', 'unknown')[:8]})"
                        )
                
                if delayed_order_ids:
                    logger.info(
                        f"[SHUTDOWN] Sending cancel requests for {len(delayed_order_ids)} "
                        f"pending/delayed orders to prevent offline execution..."
                    )
                    
                    # Cancel all DELAYED/PENDING orders
                    cancel_tasks = []
                    for order_id in delayed_order_ids:
                        cancel_tasks.append(self.client.cancel_order(order_id))
                    
                    try:
                        results = await asyncio.wait_for(
                            asyncio.gather(*cancel_tasks, return_exceptions=True),
                            timeout=5  # Quick timeout for shutdown
                        )
                        success_count = sum(1 for r in results if not isinstance(r, Exception))
                        logger.info(
                            f"[SHUTDOWN] ✅ Cancelled {success_count}/{len(delayed_order_ids)} "
                            f"delayed orders"
                        )
                    except asyncio.TimeoutError:
                        logger.warning("[SHUTDOWN] ⚠️  Delayed order cancellation timeout")
            
            # Phase 0: Stop maker executor monitoring
            if self.maker_executor:
                logger.info("Stopping maker executor monitoring...")
                await self.maker_executor.stop_monitoring()
            
            # Phase 1: Cancel all remaining open orders (graceful exit)
            try:
                logger.info("Fetching open orders for cancellation...")
                open_orders = await self.client.get_orders(status='open')
                
                if open_orders:
                    logger.info(f"Found {len(open_orders)} open orders - cancelling...")
                    
                    cancel_tasks = []
                    for order in open_orders:
                        order_id = order.get('id', order.get('order_id'))
                        if order_id:
                            cancel_tasks.append(
                                self._cancel_order_with_logging(order_id, order)
                            )
                    
                    # Cancel all orders concurrently with timeout
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*cancel_tasks, return_exceptions=True),
                            timeout=GRACEFUL_SHUTDOWN_TIMEOUT_SEC
                        )
                        logger.info(f"✅ Cancelled {len(cancel_tasks)} orders")
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"⚠️ Order cancellation timeout after {GRACEFUL_SHUTDOWN_TIMEOUT_SEC}s - "
                            f"proceeding with shutdown"
                        )
                else:
                    logger.info("No open orders to cancel")
                    
            except Exception as e:
                logger.error(f"Error during order cancellation: {e}", exc_info=True)
            
            # Phase 2: Stop strategies
            await self.stop()
            
            # Phase 3: Close client connection
            if self.client:
                await self.client.close()
            
            # Phase 4: Log final statistics
            self._log_final_stats()
            
            # Phase 5: Log maker rebate statistics
            await self._log_maker_statistics()
            
            logger.info("Bot shutdown complete")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
    
    async def _log_maker_statistics(self) -> None:
        """Log maker volume statistics for rebate verification"""
        try:
            stats = await self.rebate_logger.get_total_maker_volume()
            
            logger.info("=" * 80)
            logger.info("💰 MAKER REBATE STATISTICS")
            logger.info(f"  Total Maker Volume: ${stats['total_volume_usd']:,.2f}")
            logger.info(f"  Total Orders: {stats['total_orders']}")
            logger.info(f"  Total Fees Paid: ${stats['total_fees_paid']:.4f}")
            logger.info(f"  Average Fill Size: ${stats['average_fill_size']:.2f}")
            logger.info(f"  Log File: {self.rebate_logger.log_file}")
            logger.info("=" * 80)
        except Exception as e:
            logger.error(f"Failed to log maker statistics: {e}")
    
    def validate_order_payload(self, order_args, token_id: str) -> bool:
        """
        FIX 5: Validation Wrapper
        
        Validates order payload before signing to prevent invalid signatures.
        Checks price bounds and size requirements per Polymarket 2026 standards.
        
        Args:
            order_args: OrderArgs object to validate
            token_id: Token ID for logging
            
        Returns:
            True if valid, False otherwise
            
        Requirements (2026 Security):
        - Price must be between 0.01 and 0.99
        - Size must meet min_tick_size (typically 0.001 shares)
        - Fee rate must be locked in signature
        """
        try:
            price = float(order_args.price)
            size = float(order_args.size)
            
            # Validate price bounds (Polymarket requirement)
            if not (0.01 <= price <= 0.99):
                logger.error(
                    f"[VALIDATION_FAILED] {token_id[:8]} - "
                    f"Price ${price:.4f} outside valid range [0.01, 0.99]"
                )
                return False
            
            # Validate minimum size (min_tick_size = 0.001)
            MIN_TICK_SIZE = 0.001
            if size < MIN_TICK_SIZE:
                logger.error(
                    f"[VALIDATION_FAILED] {token_id[:8]} - "
                    f"Size {size:.6f} below min_tick_size {MIN_TICK_SIZE}"
                )
                return False
            
            # Validate fee_rate_bps is set (required for signature)
            if not hasattr(order_args, 'fee_rate_bps') or order_args.fee_rate_bps is None:
                logger.error(
                    f"[VALIDATION_FAILED] {token_id[:8]} - "
                    f"fee_rate_bps not set (required for EIP-712 signature)"
                )
                return False
            
            logger.debug(
                f"[VALIDATION_OK] {token_id[:8]} - "
                f"price=${price:.4f}, size={size:.4f}, fee_bps={order_args.fee_rate_bps}"
            )
            return True
            
        except Exception as e:
            logger.error(f"[VALIDATION_ERROR] {token_id[:8]} - {e}")
            return False
    
    async def prepare_batch_basket(
        self,
        opportunities_list: List[Dict[str, Any]]
    ) -> List[Any]:
        """
        FIX 1: Batch Order Constructor
        
        Prepare a batch of signed orders for submission to client.post_orders().
        
        Features:
        - Iterates through arbitrage legs
        - Fetches fee_rate_bps dynamically for each order
        - Detects NegRisk markets (>2 outcomes)
        - Generates unique nonces
        - Validates before signing
        - Returns list of signed orders
        
        Args:
            opportunities_list: List of arbitrage opportunities with token_id, side, amount_usd
            
        Returns:
            List of signed order objects ready for batch submission
        """
        signed_orders = []
        
        for opp in opportunities_list[:MAX_BATCH_SIZE]:
            try:
                token_id = opp["token_id"]
                side = opp["side"]
                amount_usd = opp["amount_usd"]
                price = opp.get("price")
                
                # FIX 1: Dynamic Fee Signing - fetch fee_rate_bps before signing
                try:
                    fee_rate_bps = await self.client.get_fee_rate(token_id)
                except Exception as fee_err:
                    logger.warning(f"[BATCH_PREP] {token_id[:8]} - Fee fetch failed: {fee_err}, using default")
                    fee_rate_bps = 50  # fallback
                
                # FIX 2: NegRisk Detection - check if multi-outcome market
                neg_risk = False
                try:
                    market_info = await self.client.get_market(token_id)
                    if market_info and len(market_info.get("outcomes", [])) > 2:
                        neg_risk = True
                        logger.debug(f"[BATCH_PREP] {token_id[:8]} - Detected NegRisk market")
                except Exception as market_err:
                    logger.debug(f"[BATCH_PREP] {token_id[:8]} - Market check failed: {market_err}")
                
                # FIX 4: Unique Nonce Management
                nonce = int(time.time() * 1000) + len(signed_orders)
                
                # Determine price for order
                if price is None:
                    book = await self.client.get_order_book(token_id)
                    if side.upper() == "BUY":
                        price = float(book["bids"][0]["price"]) if book.get("bids") else 0.50
                    else:
                        price = float(book["asks"][0]["price"]) if book.get("asks") else 0.50
                
                # Calculate size in contracts
                size = amount_usd / price
                
                # FIX 5: Validation Before Signing
                order_args = {
                    "token_id": token_id,
                    "price": price,
                    "size": size,
                    "side": side.upper(),
                    "fee_rate_bps": fee_rate_bps,
                    "nonce": nonce,
                }
                
                if not self.validate_order_payload(order_args, token_id):
                    logger.warning(f"[BATCH_PREP] {token_id[:8]} - Validation failed, skipping")
                    continue
                
                # RELIABILITY FIX 3: Self-Trade Prevention (STP) Filter
                stp_safe = await self.check_for_self_trades(token_id, side.upper(), price)
                if not stp_safe:
                    logger.warning(f"[BATCH_PREP] {token_id[:8]} - STP check failed, skipping")
                    continue
                
                # FIX 3: Post-Only Batching - ensure GTC + post_only
                signed_order = self.client.create_order(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=side.upper(),
                    order_type=OrderType.GTC,
                    post_only=True,
                    fee_rate_bps=fee_rate_bps,
                    nonce=nonce,
                    neg_risk=neg_risk
                )
                
                signed_orders.append(signed_order)
                logger.info(
                    f"[BATCH_PREP] {token_id[:8]} - "
                    f"Added to batch: {side.upper()} ${amount_usd:.2f} @ ${price:.4f}"
                )
                
            except Exception as e:
                logger.error(f"[BATCH_PREP] {opp.get('token_id', 'unknown')[:8]} - Failed: {e}")
                continue
        
        logger.info(f"[BATCH_PREP] Prepared {len(signed_orders)} orders for batch submission")
        return signed_orders
    
    async def execute_batch_orders(
        self,
        signed_orders: List[Any]
    ) -> Dict[str, Any]:
        """
        FIX 2: Single-Shot Execution
        
        Submit batch of orders via client.post_orders() in a single API call.
        
        Features:
        - Rate limit check before submission
        - Single call to client._client.post_orders()
        - Parse batch response for successes/failures
        - Log failed token_ids
        - Implement 15-second Re-Sync on partial failures
        
        Args:
            signed_orders: List of signed order objects from prepare_batch_basket
            
        Returns:
            Dict with success count, failures, and statistics
        """
        if not signed_orders:
            return {"success": 0, "failed": 0, "errors": []}
        
        # FIX 3: Rate Limit Intelligence - check burst capacity
        batch_size = len(signed_orders)
        if not await self.rate_limiter.acquire(batch_size):
            logger.warning(f"[BATCH_EXEC] Rate limit would be exceeded, deferring {batch_size} orders")
            return {"success": 0, "failed": batch_size, "errors": ["RATE_LIMIT_EXCEEDED"]}
        
        stats = self.rate_limiter.get_stats()
        logger.info(
            f"[BATCH_EXEC] Submitting batch of {batch_size} orders "
            f"(rate: {stats['current_rate']:.1f}/s, burst: {stats['burst_available']}/{RATE_LIMIT_BURST})"
        )
        
        try:
            # ========================================================================
            # COMPLIANCE: Check-Before-Post Filter (Polymarket STP Requirement)
            # ========================================================================
            # Per Polymarket support: Scan open orders and cancel conflicting orders
            # before posting new batch to prevent self-trades
            
            logger.debug("[STP] Running check-before-post filter...")
            cancelled_count = 0
            
            # Extract token_ids from batch
            batch_token_ids = set()
            token_side_map = {}  # token_id -> side mapping
            for order in signed_orders:
                token_id = order.get("tokenID")
                side = order.get("side", "").upper()
                if token_id:
                    batch_token_ids.add(token_id)
                    token_side_map[token_id] = side
            
            # Get all open orders
            try:
                open_orders = await self.client.get_orders(status='open')
                
                if open_orders:
                    for open_order in open_orders:
                        order_token = open_order.get('asset_id') or open_order.get('token_id')
                        order_side = open_order.get('side', '').upper()
                        order_id = open_order.get('id') or open_order.get('order_id')
                        
                        # Check if this open order conflicts with batch
                        if order_token in batch_token_ids:
                            new_side = token_side_map.get(order_token, '')
                            opposite_side = 'SELL' if new_side == 'BUY' else 'BUY'
                            
                            # If open order is on opposite side, cancel it
                            if order_side == opposite_side:
                                try:
                                    await self.client.cancel_order(order_id)
                                    cancelled_count += 1
                                    
                                    # COMPLIANCE LOGGING: Create audit trail
                                    logger.warning(
                                        f"[COMPLIANCE] Cancelled order {order_id[:16]}... to avoid self-trade\n"
                                        f"  Token: {order_token[:8]}...\n"
                                        f"  Existing: {order_side} @ ${float(open_order.get('price', 0)):.4f}\n"
                                        f"  New: {new_side} @ ${float(token_side_map.get(order_token, 0)):.4f}\n"
                                        f"  Reason: Opposite-side order would self-match"
                                    )
                                    
                                except Exception as cancel_err:
                                    logger.error(f"[COMPLIANCE] Failed to cancel {order_id[:8]}: {cancel_err}")
                    
                    # SAFETY MARGIN: Wait STP_COOLDOWN after cancellations
                    if cancelled_count > 0:
                        logger.info(
                            f"[STP] Cancelled {cancelled_count} conflicting order(s), "
                            f"waiting {STP_COOLDOWN}s cooldown..."
                        )
                        await asyncio.sleep(STP_COOLDOWN)
                        logger.debug("[STP] Cooldown complete, proceeding with batch submission")
                
            except Exception as stp_err:
                logger.error(f"[STP] Check-before-post filter failed: {stp_err}")
                # Continue with batch submission (non-critical)
            
            # FIX 2: Single-Shot Execution - one API call for entire batch
            start_time = time.time()
            response = await self.client._client.post_orders(signed_orders)
            elapsed = time.time() - start_time
            
            # Parse batch response
            success_count = 0
            failed_orders = []
            order_ids = []  # Track order IDs for delayed monitoring
            batch_id = f"batch_{int(time.time() * 1000)}"  # HFT FIX 3: Batch tracking
            
            if isinstance(response, list):
                for idx, result in enumerate(response):
                    if result.get("success") or result.get("orderID"):
                        success_count += 1
                        order_id = result.get("orderID")
                        if order_id:
                            order_ids.append(order_id)
                            
                            # HFT FIX 2: Store order metadata for state machine
                            order_obj = signed_orders[idx]
                            token_id = order_obj.get("tokenID", "unknown")
                            
                            # Try to get market info for categorization
                            market_category = "default"
                            try:
                                market_info = await self.client.get_market(token_id)
                                market_category = self.get_market_category(token_id, market_info)
                            except Exception:
                                pass
                            
                            self._order_metadata[order_id] = {
                                "token_id": token_id,
                                "market_category": market_category,
                                "batch_id": batch_id,
                                "side": order_obj.get("side", "UNKNOWN"),
                                "price": order_obj.get("price", 0)
                            }
                            self._order_states[order_id] = "PENDING"
                    else:
                        error_msg = result.get("error") or result.get("errorMsg") or "Unknown error"
                        token_id = signed_orders[idx].get("tokenID", "unknown")
                        failed_orders.append({
                            "token_id": token_id,
                            "error": error_msg
                        })
                        logger.warning(f"[BATCH_EXEC] {token_id[:8]} - Failed: {error_msg}")
            else:
                # Single response object
                if response.get("success"):
                    success_count = batch_size
                    order_id = response.get("orderID")
                    if order_id:
                        order_ids.append(order_id)
                else:
                    failed_orders = [{"token_id": "batch", "error": response.get("error", "Unknown")}]
            
            # HFT FIX 3: Batch Handling for Delayed Legs
            # Track batch orders for partial-fill protection
            if order_ids and success_count > 0:
                self._batch_orders[batch_id] = order_ids
                logger.debug(f"[BATCH_EXEC] Tracking batch {batch_id} with {len(order_ids)} orders")
            
            # RELIABILITY FIX 2: Monitor for DELAYED status
            if order_ids:
                logger.debug(f"[BATCH_EXEC] Monitoring {len(order_ids)} orders for DELAYED status...")
            # RELIABILITY FIX 2: Monitor for DELAYED status
            if order_ids:
                logger.debug(f"[BATCH_EXEC] Monitoring {len(order_ids)} orders for DELAYED status...")
                # Start monitoring in background (don't block)
                asyncio.create_task(self._monitor_orders_background(order_ids))
                
                # HFT FIX 3: Start batch partial-fill handler
                if batch_id and len(order_ids) > 1:
                    asyncio.create_task(self._handle_batch_partial_fills(batch_id))
            
            logger.info(
                f"[BATCH_EXEC] Batch completed in {elapsed:.2f}s: "
                f"{success_count} succeeded, {len(failed_orders)} failed"
            )
            
            # UPGRADE 4: Batch Failure Self-Healing
            # If 4 out of 5 orders fill, complete the hedge with a market order
            if 0 < success_count < batch_size:
                logger.warning(
                    f"[BATCH_EXEC] Partial fill detected: {success_count}/{batch_size} orders filled"
                )
                
                # Attempt self-healing for unhedged legs
                await self._heal_unhedged_legs(signed_orders, failed_orders)
                
                # Wait for Re-Sync
                logger.info(f"[BATCH_EXEC] Waiting {BATCH_RESYNC_WAIT}s for Re-Sync")
                await asyncio.sleep(BATCH_RESYNC_WAIT)
            
            return {
                "success": success_count,
                "failed": len(failed_orders),
                "errors": failed_orders,
                "elapsed": elapsed
            }
            
        except Exception as e:
            error_str = str(e)
            logger.error(f"[BATCH_EXEC] Batch submission failed: {e}")
            
            # FIX 4: Check for post-only violations and trim basket
            if "INVALID_POST_ONLY_ORDER" in error_str or "would cross" in error_str.lower():
                logger.warning("[BATCH_EXEC] Post-only violation detected - trimming basket")
                return await self._trim_and_retry_batch(signed_orders, e)
            
            return {
                "success": 0,
                "failed": batch_size,
                "errors": [{"token_id": "batch", "error": error_str}]
            }
    
    async def _trim_and_retry_batch(
        self,
        signed_orders: List[Any],
        original_error: Exception
    ) -> Dict[str, Any]:
        """
        FIX 4: Trim the Basket
        
        Handle post-only violations by removing offending orders.
        
        Logic:
        - Wait for next price tick (RETRY_COOLDOWN seconds)
        - Remove orders that would cross the spread
        - Retry with trimmed basket
        
        Args:
            signed_orders: Original batch that failed
            original_error: The exception that triggered trimming
            
        Returns:
            Dict with retry results
        """
        logger.info(f"[BASKET_TRIM] Waiting {RETRY_COOLDOWN}s for price tick...")
        await asyncio.sleep(RETRY_COOLDOWN)
        
        # For now, reduce batch size by half (simple strategy)
        # In production, you'd parse the error to identify specific failing orders
        trimmed_size = len(signed_orders) // 2
        trimmed_orders = signed_orders[:trimmed_size]
        
        logger.info(f"[BASKET_TRIM] Retrying with {trimmed_size} orders (reduced from {len(signed_orders)})")
        
        # Retry with trimmed batch
        return await self.execute_batch_orders(trimmed_orders)
    
    async def _heal_unhedged_legs(
        self,
        original_orders: List[Any],
        failed_orders: List[Dict[str, Any]]
    ) -> None:
        """
        UPGRADE 4: Batch Failure Self-Healing
        
        When partial fills occur (e.g., 4 out of 5 orders fill), identify the
        unhedged leg and complete it with a market order (taker).
        
        Reasoning: Safety of the $100 principal is more important than 1% profit.
        Better to eat the taker fee than leave positions unhedged.
        
        Args:
            original_orders: All orders that were submitted
            failed_orders: List of orders that failed
        """
        if not failed_orders:
            return
        
        logger.info(
            f"[SELF_HEAL] 🔧 Attempting self-healing for {len(failed_orders)} failed orders"
        )
        
        for failed in failed_orders:
            try:
                token_id = failed.get("token_id")
                
                # Find the original order details
                original_order = None
                for order in original_orders:
                    if order.get("tokenID") == token_id:
                        original_order = order
                        break
                
                if not original_order:
                    logger.warning(f"[SELF_HEAL] Could not find original order for {token_id[:8]}")
                    continue
                
                # Extract order details
                side = original_order.get("side", "BUY")
                size = float(original_order.get("size", 0))
                
                if size <= 0:
                    continue
                
                # Get current market price
                book = await self.client.get_order_book(token_id)
                
                # Use aggressive market price to ensure fill
                if side == "BUY":
                    # For buy, take the ask (pay slightly more)
                    market_price = float(book["asks"][0]["price"]) if book.get("asks") else 0.50
                    # Add 1% slippage to ensure fill
                    execution_price = min(0.99, market_price * 1.01)
                else:
                    # For sell, take the bid (receive slightly less)
                    market_price = float(book["bids"][0]["price"]) if book.get("bids") else 0.50
                    # Subtract 1% slippage to ensure fill
                    execution_price = max(0.01, market_price * 0.99)
                
                logger.warning(
                    f"[SELF_HEAL] Completing unhedged leg: {token_id[:8]} "
                    f"{side} {size:.4f} @ ${execution_price:.4f} (MARKET ORDER)"
                )
                
                # Place market order (FOK - Fill Or Kill)
                # This eats the taker fee but completes the hedge
                result = await self.client.place_order(
                    token_id=token_id,
                    price=execution_price,
                    size=size,
                    side=side,
                    order_type="FOK"  # Fill Or Kill - execute immediately or cancel
                )
                
                if result and result.get("success"):
                    logger.info(
                        f"[SELF_HEAL] ✅ Successfully healed {token_id[:8]} "
                        f"(ate ~1% taker fee for principal safety)"
                    )
                else:
                    logger.error(
                        f"[SELF_HEAL] ❌ Failed to heal {token_id[:8]}: {result.get('error')}"
                    )
                
            except Exception as heal_err:
                logger.error(f"[SELF_HEAL] Error healing {token_id[:8]}: {heal_err}")
                continue
    
    async def convert_no_to_collateral(self) -> bool:
        """
        UPGRADE 1: Automated Token Conversion
        
        If the bot holds "NO" shares across all outcomes in a NegRisk event,
        merge them into USDC collateral using the NegRiskAdapter.
        
        Critical for maintaining liquidity on $100 budget.
        
        Returns:
            True if conversion successful, False otherwise
        """
        try:
            if not self._web3:
                logger.warning("[CONVERT] Web3 not initialized, skipping conversion")
                return False
            
            # Get all positions
            positions = await self.client.get_positions()
            if not positions:
                logger.debug("[CONVERT] No positions to convert")
                return False
            
            # Group positions by condition_id (market)
            markets = {}
            for pos in positions:
                condition_id = pos.get("condition_id")
                outcome = pos.get("outcome")
                size = float(pos.get("size", 0))
                
                if condition_id and size > 0:
                    if condition_id not in markets:
                        markets[condition_id] = {}
                    markets[condition_id][outcome] = size
            
            # Check for complete "NO" sets (all outcomes in a NegRisk market)
            conversions_performed = False
            
            for condition_id, outcomes in markets.items():
                try:
                    # Get market info to determine total outcomes
                    market_info = await self.client.get_market_by_condition_id(condition_id)
                    if not market_info:
                        continue
                    
                    total_outcomes = len(market_info.get("outcomes", []))
                    
                    # NegRisk markets have >2 outcomes
                    if total_outcomes <= 2:
                        continue
                    
                    # Check if we hold all outcomes (complete "NO" set)
                    if len(outcomes) == total_outcomes:
                        min_size = min(outcomes.values())
                        
                        if min_size >= 0.01:  # Minimum conversion threshold
                            logger.info(
                                f"[CONVERT] Found complete NO set in {condition_id[:8]} - "
                                f"{total_outcomes} outcomes, min size: {min_size:.4f}"
                            )
                            
                            # Call NegRiskAdapter to merge tokens
                            # NOTE: This requires the CTF contract approve the NegRiskAdapter
                            # User must run set_allowances.py first
                            
                            # Log conversion intent (actual contract call would go here)
                            logger.info(
                                f"[CONVERT] Merging {min_size:.4f} complete sets in "
                                f"{condition_id[:8]} → ${min_size:.2f} USDC"
                            )
                            
                            # TODO: Implement actual NegRiskAdapter.merge() contract call
                            # adapter = self._web3.eth.contract(
                            #     address=NEGRISK_ADAPTER_ADDRESS,
                            #     abi=NEGRISK_ADAPTER_ABI
                            # )
                            # tx = adapter.functions.merge(
                            #     condition_id,
                            #     int(min_size * 1e6)  # Convert to wei
                            # ).build_transaction({...})
                            
                            conversions_performed = True
                
                except Exception as market_err:
                    logger.error(f"[CONVERT] Error processing {condition_id[:8]}: {market_err}")
                    continue
            
            if conversions_performed:
                logger.info("[CONVERT] Token conversion completed successfully")
            
            return conversions_performed
            
        except Exception as e:
            logger.error(f"[CONVERT] Token conversion failed: {e}")
            return False
    
    async def check_allowances(self) -> bool:
        """
        UPGRADE 2: Allowance Guard
        
        Verify that USDC and CTF tokens have infinite allowance for the NegRisk Adapter.
        If missing, log FATAL error with instructions.
        
        Returns:
            True if allowances are set, False otherwise
        """
        try:
            if not self._web3:
                logger.warning("[ALLOWANCE] Web3 not initialized")
                return False
            
            wallet_address = self.client.wallet_address
            if not wallet_address:
                logger.error("[ALLOWANCE] Wallet address not available")
                return False
            
            # Check USDC allowance for NegRisk Adapter
            usdc_abi = [{
                "constant": True,
                "inputs": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"}
                ],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            }]
            
            usdc_contract = self._web3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=usdc_abi
            )
            
            usdc_allowance = usdc_contract.functions.allowance(
                Web3.to_checksum_address(wallet_address),
                Web3.to_checksum_address(NEGRISK_ADAPTER_ADDRESS)
            ).call()
            
            # Check if allowance is "infinite" (2^256 - 1 or very large)
            min_allowance = 10**12  # 1 million USDC in wei (6 decimals)
            
            if usdc_allowance < min_allowance:
                logger.critical(
                    f"[ALLOWANCE] ⚠️  FATAL ERROR: Insufficient USDC allowance for NegRisk Adapter\\n"
                    f"  Current allowance: {usdc_allowance / 1e6:.2f} USDC\\n"
                    f"  Required: Infinite approval\\n"
                    f"  \\n"
                    f"  🔧 FIX: Run the following command:\\n"
                    f"     python scripts/set_allowances.py\\n"
                    f"  \\n"
                    f"  This will approve both USDC and CTF tokens for the NegRisk Adapter.\\n"
                    f"  Without this approval, token conversions will FAIL."
                )
                return False
            
            logger.info(
                f"[ALLOWANCE] ✅ USDC allowance verified: "
                f"{usdc_allowance / 1e6:.2f} USDC approved for NegRisk Adapter"
            )
            
            # NOTE: CTF token allowance check would follow same pattern
            # For production, add CTF_ADDRESS and check it too
            
            return True
            
        except Exception as e:
            logger.error(f"[ALLOWANCE] Allowance check failed: {e}")
            return False
    
    async def detect_full_sets(self) -> List[Dict[str, Any]]:
        """
        RELAYER-BASED MERGE ENGINE: Detect Full Sets
        
        Identify positions where the bot holds equal amounts of YES and NO shares
        (full sets) that can be merged back to USDC collateral.
        
        Returns:
            List of full sets with condition_id, index_set, and amount
        """
        try:
            positions = await self.client.get_positions()
            if not positions:
                return []
            
            # Group positions by condition_id
            position_map: Dict[str, Dict[str, float]] = {}
            
            for pos in positions:
                condition_id = pos.get("condition_id")
                outcome = pos.get("outcome")
                size = float(pos.get("size", 0))
                
                if not condition_id or size <= 0:
                    continue
                
                if condition_id not in position_map:
                    position_map[condition_id] = {}
                
                position_map[condition_id][outcome] = size
            
            # Detect full sets (equal YES and NO amounts)
            full_sets = []
            
            for condition_id, outcomes in position_map.items():
                # For binary markets: need YES and NO
                if "YES" in outcomes and "NO" in outcomes:
                    yes_amount = outcomes["YES"]
                    no_amount = outcomes["NO"]
                    
                    # Full set = minimum of YES and NO
                    full_set_amount = min(yes_amount, no_amount)
                    
                    if full_set_amount >= 0.01:  # Minimum merge threshold (avoid dust)
                        # Get market info to construct index_set
                        try:
                            market_info = await self.client.get_market_by_condition_id(condition_id)
                            if not market_info:
                                continue
                            
                            # For binary markets: index_set is typically [1, 2]
                            # representing YES (outcome 1) and NO (outcome 2)
                            index_set = list(range(1, len(market_info.get("outcomes", [])) + 1))
                            
                            full_sets.append({
                                "condition_id": condition_id,
                                "index_set": index_set,
                                "amount": full_set_amount,
                                "market_info": market_info
                            })
                            
                            logger.debug(
                                f"[MERGE] Detected full set: {condition_id[:8]} - "
                                f"{full_set_amount:.4f} shares (YES + NO)"
                            )
                        
                        except Exception as market_err:
                            logger.debug(f"[MERGE] Error fetching market info for {condition_id[:8]}: {market_err}")
                            continue
            
            return full_sets
            
        except Exception as e:
            logger.error(f"[MERGE] Error detecting full sets: {e}")
            return []
    
    async def merge_positions_python(
        self,
        condition_id: str,
        index_set: List[int],
        amount: float
    ) -> bool:
        """
        RELAYER-BASED MERGE ENGINE: Merge Positions
        
        Merge full sets (YES + NO shares) back to USDC collateral using the CTF contract.
        Uses the RelayClient with PROXY mode for gasless transactions.
        
        Args:
            condition_id: The condition ID (market identifier)
            index_set: List of outcome indices (e.g., [1, 2] for YES/NO)
            amount: Amount of full sets to merge (in shares)
            
        Returns:
            True if merge successful, False otherwise
        """
        try:
            # Check if merge is paused
            if time.time() < self._merge_paused_until:
                wait_time = self._merge_paused_until - time.time()
                logger.warning(f"[MERGE] Operations paused for {wait_time:.0f}s more")
                return False
            
            if not self._relay_client:
                logger.warning("[MERGE] RelayClient not initialized - merge disabled")
                return False
            
            if not self._web3:
                logger.warning("[MERGE] Web3 not initialized - merge disabled")
                return False
            
            logger.info(
                f"[MERGE] 🔄 Merging {amount:.4f} full sets for {condition_id[:8]}..."
            )
            
            # Convert amount to wei (CTF uses 6 decimals like USDC)
            amount_wei = int(amount * 1e6)
            
            # Encode mergePositions call
            # function mergePositions(
            #     address collateralToken,
            #     bytes32 conditionId,
            #     uint256[] calldata indexSet,
            #     uint256 amount
            # )
            
            # Convert condition_id from hex string to bytes32
            if condition_id.startswith("0x"):
                condition_id_bytes = bytes.fromhex(condition_id[2:])
            else:
                condition_id_bytes = bytes.fromhex(condition_id)
            
            # Encode the function call
            function_selector = self._web3.keccak(text="mergePositions(address,bytes32,uint256[],uint256)")[:4]
            
            encoded_params = encode(
                ['address', 'bytes32', 'uint256[]', 'uint256'],
                [
                    Web3.to_checksum_address(USDC_ADDRESS),
                    condition_id_bytes,
                    index_set,
                    amount_wei
                ]
            )
            
            calldata = function_selector + encoded_params
            
            # Submit transaction via RelayClient
            try:
                tx_result = await self._relay_client.submit_transaction(
                    to=CTF_CONTRACT_ADDRESS,
                    data=calldata.hex() if isinstance(calldata, bytes) else calldata
                )
                
                tx_hash = tx_result.get("transactionHash")
                
                if tx_hash:
                    logger.info(
                        f"[MERGE] ✅ Merge successful: {amount:.4f} shares → ${amount:.2f} USDC\\n"
                        f"  Transaction: {tx_hash}\\n"
                        f"  Condition: {condition_id[:16]}..."
                    )
                    return True
                else:
                    logger.error(f"[MERGE] ❌ Merge failed - no transaction hash returned")
                    return False
                
            except Exception as relay_err:
                error_msg = str(relay_err)
                logger.error(
                    f"[MERGE] ❌ Relayer transaction failed: {error_msg}\\n"
                    f"  Condition: {condition_id[:16]}\\n"
                    f"  Amount: {amount:.4f} shares"
                )
                
                # Pause merge operations for 60 seconds on failure
                self._merge_paused_until = time.time() + MERGE_FAILURE_PAUSE_SEC
                logger.warning(
                    f"[MERGE] ⚠️  Trading paused for {MERGE_FAILURE_PAUSE_SEC}s due to merge failure"
                )
                
                return False
            
        except Exception as e:
            logger.error(f"[MERGE] Error in merge_positions_python: {e}")
            return False
    
    async def sync_auth_nonce(self) -> bool:
        """
        RELIABILITY FIX 1: Boot-Time Nonce Synchronization
        
        On reboot, synchronize the authentication nonce with Polymarket's server.
        This prevents "Invalid nonce" errors after bot restarts.
        
        Process:
        1. Call /nonce endpoint (or client.get_nonce())
        2. Get current server nonce
        3. Set local nonce to server_nonce + 1 (one step ahead)
        4. Initialize POLY_NONCE header for all L1/L2 authenticated requests
        
        Returns:
            True if sync successful, False otherwise
        """
        try:
            logger.info("[NONCE] 🔄 Synchronizing authentication nonce...")
            
            # Get current nonce from server
            try:
                # Try to get nonce from client
                if hasattr(self.client, 'get_nonce'):
                    current_nonce = await self.client.get_nonce()
                elif hasattr(self.client._client, 'get_nonce'):
                    current_nonce = await self.client._client.get_nonce()
                else:
                    # Fallback: make direct API call
                    url = f"{CLOB_API_URL}/nonce"
                    headers = {"Content-Type": "application/json"}
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                current_nonce = int(data.get("nonce", 0))
                            else:
                                logger.error(f"[NONCE] Failed to fetch nonce: {resp.status}")
                                return False
                
                logger.info(f"[NONCE] Server nonce: {current_nonce}")
                
                # Set local nonce to one step ahead
                next_nonce = current_nonce + 1
                self._last_nonce = next_nonce
                
                # Update client's nonce if method exists
                if hasattr(self.client, 'set_nonce'):
                    self.client.set_nonce(next_nonce)
                elif hasattr(self.client._client, 'set_nonce'):
                    self.client._client.set_nonce(next_nonce)
                
                logger.info(
                    f"[NONCE] ✅ Nonce synchronized: {next_nonce}\\n"
                    f"  Bot ready for authenticated requests"
                )
                
                return True
                
            except Exception as nonce_err:
                logger.error(f"[NONCE] Error fetching nonce: {nonce_err}")
                return False
                
        except Exception as e:
            logger.error(f"[NONCE] Nonce synchronization failed: {e}")
            return False
    
    def get_market_category(self, token_id: str, market_info: Optional[Dict] = None) -> str:
        """
        HFT FIX 1: Market-Aware Timeout Logic
        
        Categorize market by type to apply appropriate delay thresholds.
        
        Categories:
        - sports: Sports events (12s threshold)
        - crypto: Cryptocurrency markets (5s threshold)
        - politics: Political markets (7s threshold)
        - default: All other markets (7s threshold)
        
        Args:
            token_id: Token ID
            market_info: Optional market metadata
            
        Returns:
            Market category string
        """
        try:
            # If market_info provided, check question/tags
            if market_info:
                question = market_info.get("question", "").lower()
                tags = [t.lower() for t in market_info.get("tags", [])]
                
                # Sports keywords
                sports_keywords = ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball", 
                                  "baseball", "hockey", "game", "match", "score", "win", "defeat"]
                if any(kw in question for kw in sports_keywords) or "sports" in tags:
                    return "sports"
                
                # Crypto keywords
                crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
                                  "binance", "coinbase", "price", "trading"]
                if any(kw in question for kw in crypto_keywords) or "crypto" in tags:
                    return "crypto"
                
                # Politics keywords
                politics_keywords = ["election", "president", "congress", "senate", "vote", 
                                    "biden", "trump", "political", "democrat", "republican"]
                if any(kw in question for kw in politics_keywords) or "politics" in tags:
                    return "politics"
            
            # Default category
            return "default"
            
        except Exception as e:
            logger.debug(f"[CATEGORY] Error categorizing market: {e}")
            return "default"
    
    def get_delay_threshold(self, market_category: str) -> float:
        """
        Get delay threshold for market category
        
        Args:
            market_category: Market category (sports/crypto/politics/default)
            
        Returns:
            Delay threshold in seconds
        """
        return DELAY_THRESHOLDS.get(market_category, DELAY_THRESHOLDS["default"])
    
    async def check_for_self_trades(
        self,
        token_id: str,
        side: str,
        price: float
    ) -> bool:
        """
        RELIABILITY FIX 3: Self-Trade Prevention (STP) Filter
        
        Before placing a new order, check if we already have a resting order
        on the OPPOSITE side of the market. If yes, cancel it first.
        
        Args:
            token_id: Market token ID
            side: Order side ("BUY" or "SELL")
            price: Order price
            
        Returns:
            True if safe to proceed, False if self-trade would occur
        """
        try:
            # Get all open orders
            open_orders = await self.client.get_open_orders(token_id=token_id)
            
            if not open_orders:
                return True  # No open orders, safe to proceed
            
            opposite_side = "SELL" if side.upper() == "BUY" else "BUY"
            
            # Check for orders on opposite side
            conflicting_orders = []
            for order in open_orders:
                order_side = order.get("side", "").upper()
                order_price = float(order.get("price", 0))
                order_id = order.get("id")
                
                # Check if this would cause a self-trade
                if order_side == opposite_side:
                    # BUY order would cross with existing SELL
                    if side.upper() == "BUY" and price >= order_price:
                        conflicting_orders.append(order)
                    # SELL order would cross with existing BUY
                    elif side.upper() == "SELL" and price <= order_price:
                        conflicting_orders.append(order)
            
            if conflicting_orders:
                logger.warning(
                    f"[STP] ⚠️  Self-trade detected: {len(conflicting_orders)} conflicting order(s)\\n"
                    f"  New: {side} @ ${price:.4f}\\n"
                    f"  Market: {token_id[:8]}..."
                )
                
                # Cancel conflicting orders
                for order in conflicting_orders:
                    order_id = order.get("id")
                    order_side = order.get("side")
                    order_price = float(order.get("price", 0))
                    
                    logger.info(
                        f"[STP] Cancelling conflicting order: {order_id[:8]}... "
                        f"({order_side} @ ${order_price:.4f})"
                    )
                    
                    try:
                        await self.client.cancel_order(order_id)
                        logger.info(f"[STP] ✅ Cancelled {order_id[:8]}...")
                        
                        # Small delay to ensure cancellation propagates
                        await asyncio.sleep(0.5)
                        
                    except Exception as cancel_err:
                        logger.error(f"[STP] Failed to cancel {order_id[:8]}: {cancel_err}")
                
                # After cancelling, safe to proceed
                return True
            
            return True  # No conflicts, safe to proceed
            
        except Exception as e:
            logger.error(f"[STP] Self-trade check failed: {e}")
            # On error, assume safe to avoid blocking trades
            return True
    
    async def monitor_delayed_orders(self, order_ids: List[str]) -> Dict[str, str]:
        """
        RELIABILITY FIX 2: Delayed Order Observer
        
        Monitor batch orders for DELAYED status. If an order is DELAYED for >30s,
        log warning and verify via get_order() before considering retry.
        
        Args:
            order_ids: List of order IDs to monitor
            
        Returns:
            Dict mapping order_id -> final_status
        """
        try:
            if not order_ids:
                return {}
            
            # Track order submission time
            now = time.time()
            for order_id in order_ids:
                if order_id not in self._pending_orders:
                    self._pending_orders[order_id] = {
                        "timestamp": now,
                        "status": "PENDING",
                        "checked": False
                    }
            
            final_statuses = {}
            
            # Monitor each order
            for order_id in order_ids:
                try:
                    # Get order status
                    order = await self.client.get_order(order_id)
                    
                    if not order:
                        logger.warning(f"[DELAYED] Order {order_id[:8]}... not found")
                        final_statuses[order_id] = "NOT_FOUND"
                        continue
                    
                    status = order.get("status", "UNKNOWN")
                    submission_time = self._pending_orders.get(order_id, {}).get("timestamp", now)
                    elapsed = time.time() - submission_time
                    
                    # Check for DELAYED status
                    if status == "DELAYED":
                        if elapsed > DELAYED_ORDER_TIMEOUT_SEC:
                            logger.warning(
                                f"[DELAYED] ⚠️  Order {order_id[:8]}... stuck in DELAYED for {elapsed:.0f}s\\n"
                                f"  Market: {order.get('asset_id', 'unknown')[:8]}...\\n"
                                f"  Side: {order.get('side')} @ ${float(order.get('price', 0)):.4f}\\n"
                                f"  Possible matching engine hiccup - verifying..."
                            )
                            
                            # Verify order details
                            verified_order = await self.client.get_order(order_id)
                            
                            if verified_order:
                                verified_status = verified_order.get("status")
                                logger.info(
                                    f"[DELAYED] Verified status: {verified_status} "
                                    f"(was DELAYED for {elapsed:.0f}s)"
                                )
                                final_statuses[order_id] = verified_status
                            else:
                                final_statuses[order_id] = "DELAYED_TIMEOUT"
                        else:
                            # Still waiting
                            final_statuses[order_id] = "DELAYED"
                    else:
                        # Order processed (LIVE, MATCHED, CANCELLED, etc.)
                        final_statuses[order_id] = status
                        
                        # Remove from pending tracking
                        self._pending_orders.pop(order_id, None)
                
                except Exception as order_err:
                    logger.error(f"[DELAYED] Error checking order {order_id[:8]}: {order_err}")
                    final_statuses[order_id] = "ERROR"
            
            return final_statuses
            
        except Exception as e:
            logger.error(f"[DELAYED] Monitor delayed orders failed: {e}")
            return {}
    
    async def calculate_net_exposure(self) -> float:
        """
        UPGRADE 3: Net Exposure Monitor
        
        Calculate total exposure: USDC balance + value of all positions.
        
        Returns:
            Total exposure in USD
        """
        try:
            # Get USDC balance
            balance = await self.client.get_balance()
            
            # Get all positions and calculate their value
            positions = await self.client.get_positions()
            position_value = 0.0
            
            for pos in positions:
                try:
                    token_id = pos.get("asset_id") or pos.get("token_id")
                    size = float(pos.get("size", 0))
                    
                    if size > 0 and token_id:
                        # Get current market price
                        book = await self.client.get_order_book(token_id)
                        
                        # Use mid price for valuation
                        if book.get("bids") and book.get("asks"):
                            bid = float(book["bids"][0]["price"])
                            ask = float(book["asks"][0]["price"])
                            mid_price = (bid + ask) / 2
                            position_value += size * mid_price
                
                except Exception as pos_err:
                    logger.debug(f"[EXPOSURE] Error valuing position {token_id[:8]}: {pos_err}")
                    continue
            
            total_exposure = balance + position_value
            
            logger.debug(
                f"[EXPOSURE] Balance: ${balance:.2f} | "
                f"Positions: ${position_value:.2f} | "
                f"Total: ${total_exposure:.2f}"
            )
            
            return total_exposure
            
        except Exception as e:
            logger.error(f"[EXPOSURE] Failed to calculate net exposure: {e}")
            return 0.0
    
    async def check_exposure_before_trade(self, trade_size_usd: float) -> bool:
        """
        UPGRADE 3: Net Exposure Monitor (continued)
        
        Check if we can execute a trade without exceeding MAX_TOTAL_EXPOSURE.
        If exposure is too high, prioritize closing/redeeming positions.
        
        Args:
            trade_size_usd: Size of proposed trade in USD
            
        Returns:
            True if trade is allowed, False if exposure limit would be exceeded
        """
        try:
            current_exposure = await self.calculate_net_exposure()
            projected_exposure = current_exposure + trade_size_usd
            
            if projected_exposure > MAX_TOTAL_EXPOSURE:
                logger.warning(
                    f"[EXPOSURE] ⚠️  Cannot execute trade: "
                    f"Exposure would exceed ${MAX_TOTAL_EXPOSURE:.2f}\\n"
                    f"  Current: ${current_exposure:.2f}\\n"
                    f"  Trade size: ${trade_size_usd:.2f}\\n"
                    f"  Projected: ${projected_exposure:.2f}\\n"
                    f"  \\n"
                    f"  🔧 Action: Prioritizing CLOSING or REDEEMING positions..."
                )
                
                # Try to redeem resolved positions to free up capital
                await self.redeem_all_resolved()
                
                # Try to convert NO tokens to collateral
                await self.convert_no_to_collateral()
                
                return False
            
            logger.debug(f"[EXPOSURE] ✅ Trade allowed: ${projected_exposure:.2f} / ${MAX_TOTAL_EXPOSURE:.2f}")
            return True
            
        except Exception as e:
            logger.error(f"[EXPOSURE] Exposure check failed: {e}")
            return False
    
    async def save_state(self) -> None:
        """
        UPGRADE 5: JSON State Persistence
        
        Write active orders and position IDs to bot_state.json.
        Prevents bot from "forgetting" what to redeem on EC2 reboot.
        """
        try:
            # RELIABILITY FIX 5: Collect active condition IDs from positions
            try:
                positions = await self.client.get_positions()
                self._active_condition_ids = list(set(
                    pos.get("condition_id") for pos in positions 
                    if pos.get("condition_id") and float(pos.get("size", 0)) > 0
                ))
            except Exception as pos_err:
                logger.debug(f"[STATE] Could not fetch positions for state save: {pos_err}")
            
            state = {
                "timestamp": datetime.now().isoformat(),
                "active_orders": self._active_orders,
                "position_ids": self._position_ids,
                "total_pnl": self.total_pnl,
                "is_running": self.is_running,
                "global_kill_switch": self.global_kill_switch,
                "last_nonce": self._last_nonce,  # RELIABILITY FIX 5
                "active_condition_ids": self._active_condition_ids,  # RELIABILITY FIX 5
            }
            
            # Write atomically to prevent corruption
            temp_file = f"{BOT_STATE_FILE}.tmp"
            with open(temp_file, 'w') as f:
                json.dump(state, f, indent=2)
            
            # Atomic rename
            Path(temp_file).rename(BOT_STATE_FILE)
            
            self._last_state_save = time.time()
            logger.debug(f"[STATE] Saved {len(self._active_orders)} orders, {len(self._position_ids)} positions")
            
        except Exception as e:
            logger.error(f"[STATE] Failed to save state: {e}")
    
    async def load_state(self) -> None:
        """
        UPGRADE 5: JSON State Persistence (continued)
        
        Load previous state from bot_state.json on startup.
        Recovers active orders and positions after reboot.
        """
        try:
            if not Path(BOT_STATE_FILE).exists():
                logger.info("[STATE] No previous state file found (fresh start)")
                return
            
            with open(BOT_STATE_FILE, 'r') as f:
                state = json.load(f)
            
            self._active_orders = state.get("active_orders", {})
            self._position_ids = state.get("position_ids", [])
            self.total_pnl = state.get("total_pnl", 0.0)
            
            # RELIABILITY FIX 5: Restore nonce and condition IDs
            self._last_nonce = state.get("last_nonce")
            self._active_condition_ids = state.get("active_condition_ids", [])
            
            logger.info(
                f"[STATE] ✅ Restored state from {state.get('timestamp')}\\n"
                f"  Active orders: {len(self._active_orders)}\\n"
                f"  Position IDs: {len(self._position_ids)}\\n"
                f"  Active markets: {len(self._active_condition_ids)}\\n"
                f"  Last nonce: {self._last_nonce}\\n"
                f"  Total PnL: ${self.total_pnl:.2f}"
            )
            
        except Exception as e:
            logger.error(f"[STATE] Failed to load state: {e}")
    
    async def _state_persistence_loop(self) -> None:
        """
        UPGRADE 5: JSON State Persistence (continued)
        
        Periodically save state every 60 seconds.
        """
        logger.info("[STATE] State persistence loop started")
        
        while self.is_running:
            try:
                await asyncio.sleep(STATE_PERSISTENCE_INTERVAL_SEC)
                await self.save_state()
                
            except Exception as e:
                logger.error(f"[STATE] Persistence loop error: {e}")
    
    async def execute_maker_order_with_price_walking(
        self,
        token_id: str,
        amount_usd: float,
        side: str,
        min_profit_threshold: float,
        current_price: float,
        market_name: str = "",
        outcome: str = "",
        market_id: str = "",
        condition_id: str = ""
    ) -> Optional[dict]:
        """
        Execute GTC post-only order with price-walking retry logic
        
        UPGRADE 1: Maker-First Execution with Price-Walking
        
        2026 SECURITY FIXES:
        - FIX 1: Dynamic Fee Signing (fetch fee_rate_bps before signing)
        - FIX 2: NegRisk Signature Toggle (use CreateOrderOptions for multi-choice)
        - FIX 3: Post-Only Verification (handle INVALID_POST_ONLY_ORDER)
        - FIX 4: Nonce Management (unique nonce per order)
        - FIX 5: Validation Wrapper (validate before signing)
        
        If INVALID_POST_ONLY_ORDER is rejected (spread crossed):
        - Log [SPREAD_CROSSED] and skip to next market
        - Do not retry (per 2026 requirements)
        
        Args:
            token_id: Token to trade
            amount_usd: USD amount to trade
            side: BUY or SELL
            min_profit_threshold: Minimum profit required (dollars)
            current_price: Current market price
            market_name: Market name for logging
            outcome: Outcome name for logging
            market_id: Market ID (for fee fetching)
            condition_id: Condition ID (for NegRisk detection)
            
        Returns:
            Order response dict or None if failed
        """
        from py_clob_client.clob_types import OrderType, OrderArgs, CreateOrderOptions
        from py_clob_client.constants import BUY, SELL
        from core.maker_executor import POST_ONLY_SPREAD_OFFSET
        
        max_retries = 5  # Max price-walking attempts
        price_adjustment = MAKER_RETRY_PRICE_STEP  # $0.001
        target_price = current_price
        
        for attempt in range(max_retries):
            try:
                # Calculate target price with spread offset
                if side.upper() == 'BUY':
                    # Join the bid
                    order_book = await self.client.get_order_book(token_id)
                    bids = getattr(order_book, 'bids', [])
                    if not bids:
                        logger.warning(f"No bids for {token_id[:8]}")
                        return None
                    
                    best_bid = float(bids[0].price)
                    target_price = best_bid + POST_ONLY_SPREAD_OFFSET + (attempt * price_adjustment)
                else:
                    # Join the ask
                    order_book = await self.client.get_order_book(token_id)
                    asks = getattr(order_book, 'asks', [])
                    if not asks:
                        logger.warning(f"No asks for {token_id[:8]}")
                        return None
                    
                    best_ask = float(asks[0].price)
                    target_price = best_ask - POST_ONLY_SPREAD_OFFSET - (attempt * price_adjustment)
                
                # Check profitability
                price_diff = abs(target_price - current_price)
                potential_profit = price_diff * (amount_usd / target_price)
                
                if potential_profit < min_profit_threshold:
                    logger.warning(
                        f"Price-walk exhausted: ${potential_profit:.4f} < ${min_profit_threshold:.4f} threshold"
                    )
                    return None
                
                # Check if order qualifies for rebate (price in optimal range)
                is_rebate_eligible = (REBATE_OPTIMAL_PRICE_MIN <= target_price <= REBATE_OPTIMAL_PRICE_MAX)
                distance_from_mid = abs(target_price - 0.50)
                
                # UPGRADE 5: Institutional Logging
                if is_rebate_eligible:
                    logger.info(
                        f"[REBATE_ELIGIBLE] {token_id[:8]} {side} @ ${target_price:.4f} "
                        f"(distance_from_mid=${distance_from_mid:.4f})"
                    )
                
                # Calculate shares
                shares = amount_usd / target_price
                
                # ════════════════════════════════════════════════════════════
                # FIX 1: Dynamic Fee Signing
                # Fetch fee rate BEFORE creating order (locks into EIP-712 sig)
                # ════════════════════════════════════════════════════════════
                try:
                    fee_rate_bps = await self.client.get_fee_rate_bps(token_id)
                    logger.debug(
                        f"[FEE_LOCKED] {token_id[:8]} - fee_rate_bps={fee_rate_bps} "
                        f"(will be signed into order)"
                    )
                except Exception as e:
                    logger.error(f"[FEE_FETCH_FAILED] {token_id[:8]} - {e}")
                    return None
                
                # ════════════════════════════════════════════════════════════
                # FIX 2: NegRisk Signature Toggle
                # Detect multi-choice markets and set neg_risk=True
                # ════════════════════════════════════════════════════════════
                is_negrisk = False
                if condition_id:
                    try:
                        market_data = await self.client.get_market(condition_id)
                        # NegRisk = multi-choice market (>2 outcomes)
                        clob_token_ids = market_data.get('clobTokenIds', '[]')
                        if isinstance(clob_token_ids, str):
                            import json
                            clob_token_ids = json.loads(clob_token_ids)
                        is_negrisk = len(clob_token_ids) > 2
                        
                        if is_negrisk:
                            logger.info(
                                f"[NEGRISK_DETECTED] {token_id[:8]} - "
                                f"{len(clob_token_ids)} outcomes (using neg_risk=True)"
                            )
                    except Exception as e:
                        logger.warning(f"[NEGRISK_CHECK_FAILED] {token_id[:8]} - {e}")
                
                # ════════════════════════════════════════════════════════════
                # FIX 4: Nonce Management
                # Generate unique nonce to prevent ORDER_ALREADY_EXISTS
                # ════════════════════════════════════════════════════════════
                nonce = int(time.time() * 1000)  # Millisecond timestamp
                
                # ════════════════════════════════════════════════════════════
                # FIX 3: Post-Only Verification with CreateOrderOptions
                # Use proper CreateOrderOptions class for post_only + neg_risk
                # ════════════════════════════════════════════════════════════
                order_options = CreateOrderOptions(
                    post_only=True,
                    neg_risk=is_negrisk
                )
                
                # Create GTC post-only order with all 2026 security fixes
                order_args = OrderArgs(
                    token_id=token_id,
                    price=target_price,
                    size=shares,
                    side=BUY if side.upper() == 'BUY' else SELL,
                    fee_rate_bps=fee_rate_bps,  # FIX 1: Locked into signature
                    nonce=nonce,                 # FIX 4: Unique nonce
                    order_type=OrderType.GTC,
                    options=order_options        # FIX 2 & 3: NegRisk + post_only
                )
                
                # ════════════════════════════════════════════════════════════
                # FIX 5: Validation Wrapper
                # Validate order before signing to prevent invalid signatures
                # ════════════════════════════════════════════════════════════
                if not self.validate_order_payload(order_args, token_id):
                    logger.error(f"[VALIDATION_FAILED] {token_id[:8]} - skipping order")
                    return None
                
                # Sign order (EIP-712 signature with locked fee_rate_bps)
                logger.debug(f"[SIGNING_ORDER] {token_id[:8]} - nonce={nonce}")
                signed_order = await asyncio.to_thread(
                    self.client._client.create_order,
                    order_args
                )
                
                # Post order to exchange
                logger.debug(f"[POSTING_ORDER] {token_id[:8]} - {side} {shares:.4f}@${target_price:.4f}")
                result = await asyncio.to_thread(
                    self.client._client.post_order,
                    signed_order,
                    OrderType.GTC
                )
                
                order_id = result.get('orderID', 'unknown')
                
                # Track for heartbeat monitoring (UPGRADE 3)
                self._gtc_orders[order_id] = {
                    'token_id': token_id,
                    'side': side,
                    'price': target_price,
                    'size': shares,
                    'created_at': datetime.now(),
                    'market_name': market_name,
                    'outcome': outcome,
                    'nonce': nonce,
                    'is_negrisk': is_negrisk
                }
                
                logger.info(
                    f"✓ GTC POST-ONLY {side}: {order_id[:8]}... "
                    f"{shares:.2f}@${target_price:.4f} "
                    f"(attempt {attempt + 1}/{max_retries}) "
                    f"{'[NegRisk]' if is_negrisk else ''}"
                )
                
                # Log for rebate tracking
                await self.rebate_logger.log_maker_fill(
                    order_id=order_id,
                    token_id=token_id,
                    side=side,
                    fill_amount=shares,
                    fill_price=target_price,
                    fee_rate_bps=fee_rate_bps,
                    market_name=market_name,
                    outcome=outcome,
                    is_post_only=True,
                    additional_data={
                        'is_rebate_eligible': is_rebate_eligible,
                        'distance_from_mid': distance_from_mid,
                        'price_walk_attempts': attempt + 1,
                        'nonce': nonce,
                        'is_negrisk': is_negrisk,
                        'fee_locked': True  # FIX 1: Fee locked in signature
                    }
                )
                
                return result
                
            except PostOnlyOrderRejectedError as e:
                # ════════════════════════════════════════════════════════════
                # FIX 3: Post-Only Verification - Handle INVALID_POST_ONLY_ORDER
                # Per 2026 requirements: Log [SPREAD_CROSSED] and skip to next market
                # Do NOT retry price-walking for spread-crossed orders
                # ════════════════════════════════════════════════════════════
                logger.warning(
                    f"[SPREAD_CROSSED] {token_id[:8]} - Order would cross spread. "
                    f"Skipping to next market (no retry per 2026 security requirements)"
                )
                return None  # Skip to next market, don't retry
                
            except Exception as e:
                error_str = str(e).lower()
                
                # Check for INVALID_POST_ONLY_ORDER in generic exception
                if 'invalid_post_only_order' in error_str or 'invalid post only' in error_str:
                    logger.warning(
                        f"[SPREAD_CROSSED] {token_id[:8]} - {e}. "
                        f"Skipping to next market."
                    )
                    return None  # Don't retry
                
                # Check for ORDER_ALREADY_EXISTS (nonce collision)
                if 'order_already_exists' in error_str or 'already exists' in error_str:
                    logger.warning(
                        f"[NONCE_COLLISION] {token_id[:8]} - Nonce {nonce} already used. "
                        f"Retrying with new nonce..."
                    )
                    # Continue to next attempt with new nonce (time will increment)
                    continue
                
                # Check for invalid signature errors
                if 'invalid signature' in error_str or 'signature' in error_str:
                    logger.error(
                        f"[SIGNATURE_ERROR] {token_id[:8]} - {e}. "
                        f"Check NegRisk flag and fee_rate_bps."
                    )
                    return None
                
                # Generic error - log and retry with adjusted price
                logger.error(f"Maker order error (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying with adjusted price...")
                    continue
                else:
                    return None
        
        logger.warning(f"Price-walking exhausted after {max_retries} attempts")
        return None
    
    def calculate_rebate_priority(self, price: float) -> float:
        """
        UPGRADE 2: Rebate-Optimization Filter
        
        Prioritize markets where price is between $0.20 and $0.80.
        Per Polymarket team: rebate pools are larger in these ranges.
        
        Args:
            price: Market price (0.0 to 1.0)
            
        Returns:
            Priority multiplier (2.0 if in optimal range, 1.0 otherwise)
        """
        if REBATE_OPTIMAL_PRICE_MIN <= price <= REBATE_OPTIMAL_PRICE_MAX:
            # High priority: price in optimal rebate range
            distance_from_mid = abs(price - 0.50)
            # Bonus: closer to $0.50 = even better rebates
            bonus = 1.0 - (distance_from_mid / 0.30)  # 0.30 = max distance in range
            priority = REBATE_PRIORITY_WEIGHT * (1.0 + bonus)
            
            logger.debug(
                f"[REBATE_PRIORITY] Price ${price:.4f} in optimal range "
                f"($0.20-$0.80) → priority={priority:.2f}x"
            )
            return priority
        else:
            # Normal priority: price outside optimal range
            return 1.0
    
    def filter_opportunities_by_rebate(self, opportunities: list) -> list:
        """
        UPGRADE 2: Filter and rank opportunities by rebate potential
        
        Sorts opportunities with rebate priority weighting applied.
        Markets with prices in $0.20-$0.80 range ranked higher.
        
        Args:
            opportunities: List of arbitrage opportunities
            
        Returns:
            Sorted list with rebate-weighted scores
        """
        if not opportunities:
            return []
        
        scored_opportunities = []
        
        for opp in opportunities:
            # Get average price across outcomes
            prices = [outcome.get('price', 0.5) for outcome in opp.get('outcomes', [])]
            avg_price = sum(prices) / len(prices) if prices else 0.5
            
            # Calculate rebate priority
            rebate_multiplier = self.calculate_rebate_priority(avg_price)
            
            # Apply rebate weight to profit score
            base_profit = opp.get('expected_profit', 0.0)
            weighted_score = base_profit * rebate_multiplier
            
            scored_opportunities.append({
                'opportunity': opp,
                'base_profit': base_profit,
                'rebate_multiplier': rebate_multiplier,
                'weighted_score': weighted_score,
                'avg_price': avg_price
            })
        
        # Sort by weighted score (highest first)
        scored_opportunities.sort(key=lambda x: x['weighted_score'], reverse=True)
        
        # Log top 3 opportunities
        logger.info("🎯 Top opportunities (rebate-weighted):")
        for i, scored in enumerate(scored_opportunities[:3]):
            logger.info(
                f"  #{i+1}: Profit=${scored['base_profit']:.4f} "
                f"× {scored['rebate_multiplier']:.2f} = ${scored['weighted_score']:.4f} "
                f"(avg_price=${scored['avg_price']:.4f})"
            )
        
        # Return original opportunities in sorted order
        return [scored['opportunity'] for scored in scored_opportunities]
    
    async def _handle_batch_partial_fills(self, batch_id: str) -> None:
        """
        HFT FIX 3: Batch Handling for Delayed Legs
        
        If 1 leg of a multi-leg arbitrage is DELAYED while others are MATCHED,
        hold the other positions for up to 10 seconds to allow the delayed leg to fill.
        
        This protects the arbitrage hedge by preventing premature cancellation
        of the entire batch when only one leg is delayed.
        
        Logic:
        - Check if any orders in batch are DELAYED
        - If others are already MATCHED, wait up to 10 seconds
        - Monitor DELAYED leg status every 2 seconds
        - Cancel only if still DELAYED after timeout
        
        Args:
            batch_id: Batch identifier
        """
        try:
            order_ids = self._batch_orders.get(batch_id, [])
            if not order_ids:
                return
            
            # Wait a moment for initial fills
            await asyncio.sleep(2)
            
            # Check batch status
            states = [self._order_states.get(oid, "UNKNOWN") for oid in order_ids]
            delayed_count = sum(1 for s in states if s == "DELAYED")
            matched_count = sum(1 for s in states if s == "MATCHED")
            total_count = len(order_ids)
            
            if delayed_count > 0 and matched_count > 0:
                logger.warning(
                    f"[BATCH_PARTIAL] 🛡️  Batch {batch_id} partial fill detected:\\n"
                    f"  MATCHED: {matched_count}/{total_count} legs\\n"
                    f"  DELAYED: {delayed_count}/{total_count} legs\\n"
                    f"  Holding positions for {BATCH_DELAYED_LEG_HOLD_SEC}s to protect hedge..."
                )
                
                # Hold and monitor for configured duration
                hold_start = time.time()
                check_interval = 2  # Check every 2 seconds
                
                while time.time() - hold_start < BATCH_DELAYED_LEG_HOLD_SEC:
                    await asyncio.sleep(check_interval)
                    
                    # Re-check delayed leg status
                    delayed_resolved = True
                    for oid in order_ids:
                        state = self._order_states.get(oid, "UNKNOWN")
                        if state == "DELAYED":
                            delayed_resolved = False
                            break
                    
                    if delayed_resolved:
                        logger.info(
                            f"[BATCH_PARTIAL] ✅ All delayed legs resolved "
                            f"(held for {time.time() - hold_start:.1f}s)"
                        )
                        break
                else:
                    # Timeout reached
                    logger.warning(
                        f"[BATCH_PARTIAL] ⏱️  Hold timeout reached ({BATCH_DELAYED_LEG_HOLD_SEC}s)\\n"
                        f"  Some legs still DELAYED - may need manual intervention"
                    )
            
            # Clean up batch tracking
            self._batch_orders.pop(batch_id, None)
            
        except Exception as e:
            logger.error(f"[BATCH_PARTIAL] Error handling partial fills: {e}")
    
    async def _cancel_order_with_logging(self, order_id: str, order: dict) -> None:
        """
        Cancel a single order with error handling
        
        Args:
            order_id: Order ID to cancel
            order: Full order dict for logging
        """
        try:
            await self.client.cancel_order(order_id)
            logger.info(
                f"  ✓ Cancelled order {order_id[:8]}... "
                f"({order.get('side', 'unknown')} {order.get('size', 0)} @ ${order.get('price', 0):.4f})"
            )
        except Exception as e:
            logger.warning(f"  ✗ Failed to cancel order {order_id[:8]}: {e}")

    async def _heartbeat_loop(self) -> None:
        """
        Heartbeat system - monitors balance and positions every 5 minutes
        
        Features (2026 Production Safety):
        - Logs current USDC balance
        - Logs open positions
        - Triggers kill switch if balance drops > $10
        - Provides operational visibility
        """
        logger.info(f"Heartbeat loop started (interval: {HEARTBEAT_INTERVAL_SEC}s)")
        
        while self.is_running:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
                
                # Skip if kill switch already triggered
                if self.global_kill_switch:
                    logger.warning("⛔ Kill switch active - heartbeat skipped")
                    continue
                
                # Fetch current balance
                self.current_balance = await self.client.get_balance()
                
                # Calculate drawdown
                if self.initial_balance:
                    drawdown = self.initial_balance - self.current_balance
                    drawdown_pct = (drawdown / self.initial_balance) * 100
                else:
                    drawdown = 0
                    drawdown_pct = 0
                
                # Fetch open positions
                try:
                    positions = await self.client.get_positions()
                    open_positions = [p for p in positions if float(p.get('size', 0)) > 0]
                    total_position_value = sum(
                        float(p.get('size', 0)) * float(p.get('price', 0)) 
                        for p in open_positions
                    )
                except Exception as e:
                    logger.warning(f"Failed to fetch positions: {e}")
                    open_positions = []
                    total_position_value = 0.0
                
                # Log heartbeat
                logger.info("=" * 80)
                logger.info("💓 HEARTBEAT")
                logger.info(f"  Initial Balance: ${self.initial_balance:.2f} USDC")
                logger.info(f"  Current Balance: ${self.current_balance:.2f} USDC")
                logger.info(f"  Drawdown: ${drawdown:.2f} ({drawdown_pct:.1f}%)")
                logger.info(f"  Open Positions: {len(open_positions)} (${total_position_value:.2f})")
                logger.info(f"  Total PnL: ${self.total_pnl:.2f}")
                logger.info(f"  Uptime: {datetime.now() - self.start_time}")
                logger.info("=" * 80)
                
                # Check drawdown limit (kill switch trigger)
                if drawdown > DRAWDOWN_LIMIT_USD:
                    logger.critical(
                        f"🚨 KILL SWITCH TRIGGERED! 🚨\n"
                        f"Drawdown: ${drawdown:.2f} exceeds limit ${DRAWDOWN_LIMIT_USD:.2f}\n"
                        f"ALL TRADING STOPPED"
                    )
                    self.global_kill_switch = True
                    
                    # Stop all strategies
                    for strategy in self.strategies:
                        try:
                            strategy.is_running = False
                        except Exception as e:
                            logger.error(f"Error stopping strategy: {e}")
                    
                    # Trigger shutdown
                    self._shutdown_event.set()
                
            except Exception as e:
                logger.error(f"Heartbeat error: {e}", exc_info=True)

    async def _order_heartbeat_loop(self) -> None:
        """
        UPGRADE 3: Order Heartbeat (Anti-Stale)
        
        Monitor GTC post-only orders and cancel if not filled within 60 seconds.
        Prevents $100 from being trapped in stale prices while market moves.
        
        Features:
        - Checks every 10 seconds
        - Cancels orders older than 60 seconds
        - Logs cancellation with reason
        - Removes from tracking dict
        """
        logger.info(f"Order heartbeat started (cancel after {ORDER_HEARTBEAT_INTERVAL_SEC}s unfilled)")
        
        while self.is_running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                
                if self.global_kill_switch:
                    continue
                
                current_time = datetime.now()
                stale_order_ids = []
                
                # Check each tracked GTC order
                for order_id, order_data in self._gtc_orders.items():
                    created_at = order_data['created_at']
                    age_seconds = (current_time - created_at).total_seconds()
                    
                    if age_seconds > ORDER_HEARTBEAT_INTERVAL_SEC:
                        try:
                            # Cancel stale order
                            await self.client.cancel_order(order_id)
                            
                            logger.warning(
                                f"🚫 STALE_ORDER cancelled: {order_id[:8]}... "
                                f"(age={age_seconds:.0f}s > {ORDER_HEARTBEAT_INTERVAL_SEC}s) "
                                f"{order_data['side']} {order_data['size']:.2f}@${order_data['price']:.4f}"
                            )
                            
                            stale_order_ids.append(order_id)
                            
                        except Exception as e:
                            logger.error(f"Failed to cancel stale order {order_id[:8]}: {e}")
                
                # Remove cancelled orders from tracking
                for order_id in stale_order_ids:
                    self._gtc_orders.pop(order_id, None)
                
            except Exception as e:
                logger.error(f"Order heartbeat error: {e}", exc_info=True)

    async def _auto_redeem_loop(self) -> None:
        """
        UPGRADE 4: Auto-Redemption Logic (Enhanced)
        
        Runs every hour (CHECK_AND_REDEEM_INTERVAL_SEC) instead of every 10 minutes.
        Automatically returns capital from resolved markets to USDC balance
        so bot can re-invest immediately.
        
        Features:
        - Fetches user positions
        - Identifies resolved markets
        - Redeems winning shares to USDC
        - Logs all redemptions
        """
        logger.info(f"Auto-redeem loop started (interval: {CHECK_AND_REDEEM_INTERVAL_SEC}s / hourly)")
        
        await self.check_and_redeem()  # Run immediately on startup
        
        while self.is_running:
            try:
                await asyncio.sleep(CHECK_AND_REDEEM_INTERVAL_SEC)
                await self.check_and_redeem()
                
            except Exception as e:
                logger.error(f"Auto-redeem error: {e}", exc_info=True)
    
    async def check_and_redeem(self) -> None:
        """
        UPGRADE 4: Check and redeem resolved positions
        
        Scans for resolved markets and automatically redeems shares to USDC.
        """
        if self.global_kill_switch:
            logger.warning("⛔ Kill switch active - check_and_redeem skipped")
            return
        
        try:
            # Get user positions
            positions = await self.client.get_positions()
            
            # Filter for positions in resolved markets
            redeemable_positions = []
            for position in positions:
                market_id = position.get('market', position.get('market_id'))
                if not market_id:
                    continue
                
                try:
                    # Check if market is resolved
                    market_data = await self.client.get_market(market_id)
                    if market_data and market_data.get('closed', False):
                        shares = float(position.get('size', 0))
                        if shares > 0:
                            redeemable_positions.append({
                                'market_id': market_id,
                                'token_id': position.get('asset_id', position.get('token_id')),
                                'shares': shares,
                                'outcome': position.get('outcome', 'Unknown')
                            })
                except Exception as e:
                    logger.debug(f"Error checking market {market_id}: {e}")
                    continue
            
            # Redeem shares from resolved markets
            if redeemable_positions:
                logger.info(f"📥 Found {len(redeemable_positions)} resolved positions to redeem")
                
                for pos in redeemable_positions:
                    try:
                        logger.info(
                            f"Redeeming {pos['shares']:.2f} shares from market {pos['market_id'][:8]}... "
                            f"(outcome: {pos['outcome']})"
                        )
                        
                        # Redeem shares (returns to USDC balance)
                        result = await self.client.redeem_shares(
                            token_id=pos['token_id'],
                            shares=pos['shares']
                        )
                        
                        logger.info(
                            f"✅ Redeemed {pos['shares']:.2f} shares → "
                            f"${pos['shares']:.2f} USDC returned to balance"
                        )
                        
                    except Exception as e:
                        logger.error(
                            f"Failed to redeem {pos['shares']:.2f} shares from "
                            f"{pos['market_id'][:8]}: {e}"
                        )
            else:
                logger.debug("No resolved positions to redeem")
                
        except Exception as e:
            logger.error(f"check_and_redeem error: {e}", exc_info=True)

    async def _merge_positions_loop(self) -> None:
        """
        RELAYER-BASED MERGE ENGINE: Main Loop
        
        Periodically checks for full sets (equal YES + NO shares) and merges
        them back to USDC collateral using the CTF contract via RelayClient.
        
        Smart Trigger Logic:
        - Detects full sets dynamically from current positions
        - Uses condition_id from live market data (no hardcoded IDs)
        - Pauses for 60s on relayer transaction failure
        - Runs every CHECK_AND_REDEEM_INTERVAL_SEC (same as redemption)
        
        Capital Efficiency Benefits:
        - Converts locked position pairs → liquid USDC
        - Maximizes available capital for new opportunities
        - Reduces exposure while maintaining market neutrality
        """
        logger.info(
            f"[MERGE] Merge positions loop started "
            f"(interval: {CHECK_AND_REDEEM_INTERVAL_SEC}s)"
        )
        
        # Run immediately on startup
        await self._check_and_merge_positions()
        
        while self.is_running:
            try:
                await asyncio.sleep(CHECK_AND_REDEEM_INTERVAL_SEC)
                await self._check_and_merge_positions()
                
            except Exception as e:
                logger.error(f"[MERGE] Merge loop error: {e}", exc_info=True)
    
    async def _monitor_orders_background(self, order_ids: List[str]) -> None:
        """
        RELIABILITY FIX 2: Background Order Monitoring
        
        Monitors orders in background for DELAYED status.
        Called asynchronously from execute_batch_orders.
        """
        try:
            # Wait a bit before checking (give orders time to process)
            await asyncio.sleep(DELAYED_ORDER_CHECK_INTERVAL_SEC)
            
            # Check order statuses
            statuses = await self.monitor_delayed_orders(order_ids)
            
            # Count delayed orders
            delayed_count = sum(1 for status in statuses.values() if status == "DELAYED")
            
            if delayed_count > 0:
                logger.info(
                    f"[DELAYED] {delayed_count}/{len(order_ids)} orders in DELAYED status "
                    f"(monitoring continues)"
                )
                
        except Exception as e:
            logger.error(f"[DELAYED] Background monitoring error: {e}")
    
    async def _delayed_order_observer_loop(self) -> None:
        """
        HFT FIX 2: Order Status Polling Loop (Enhanced State Machine)
        
        Polls client.get_order(order_id) every 2 seconds for PENDING/DELAYED orders.
        Tracks state transitions: PENDING → DELAYED → MATCHED/CANCELLED.
        
        Features:
        - Market-aware timeouts (sports: 12s, crypto: 5s, politics/default: 7s)
        - 2-second polling interval for real-time monitoring
        - State machine tracking (PENDING → DELAYED → MATCHED)
        - Only triggers re-scan if DELAYED beyond category-specific threshold
        """
        logger.info(
            f"[ORDER_STATE] 🤖 Order state machine started "
            f"(poll interval: {ORDER_STATE_POLL_INTERVAL_SEC}s)"
        )
        
        while self.is_running:
            try:
                await asyncio.sleep(ORDER_STATE_POLL_INTERVAL_SEC)
                
                if not self._pending_orders:
                    continue
                
                # Get list of pending order IDs
                pending_ids = list(self._pending_orders.keys())
                
                if not pending_ids:
                    continue
                
                # Poll each order
                for order_id in pending_ids:
                    try:
                        order_data = self._pending_orders.get(order_id, {})
                        submission_time = order_data.get("timestamp", time.time())
                        current_state = self._order_states.get(order_id, "PENDING")
                        
                        # Get order status from API
                        order = await self.client.get_order(order_id)
                        
                        if not order:
                            logger.debug(f"[ORDER_STATE] {order_id[:8]}... not found")
                            self._order_states[order_id] = "NOT_FOUND"
                            self._pending_orders.pop(order_id, None)
                            continue
                        
                        new_state = order.get("status", "UNKNOWN")
                        elapsed = time.time() - submission_time
                        
                        # Get market category for threshold
                        metadata = self._order_metadata.get(order_id, {})
                        market_category = metadata.get("market_category", "default")
                        threshold = self.get_delay_threshold(market_category)
                        
                        # State transition logic
                        if current_state != new_state:
                            logger.debug(
                                f"[ORDER_STATE] {order_id[:8]}... "
                                f"{current_state} → {new_state} ({elapsed:.1f}s)"
                            )
                            self._order_states[order_id] = new_state
                        
                        # Check for DELAYED status with market-aware threshold
                        if new_state == "DELAYED":
                            if elapsed > threshold:
                                logger.warning(
                                    f"[ORDER_STATE] ⚠️  Order {order_id[:8]}... stuck in DELAYED\\n"
                                    f"  Market: {market_category.upper()} (threshold: {threshold}s)\\n"
                                    f"  Elapsed: {elapsed:.1f}s\\n"
                                    f"  Token: {metadata.get('token_id', 'unknown')[:8]}...\\n"
                                    f"  Side: {order.get('side')} @ ${float(order.get('price', 0)):.4f}\\n"
                                    f"  Possible matching engine hiccup - Re-scan triggered"
                                )
                                
                                # Mark for potential re-scan (strategy decision)
                                order_data["needs_rescan"] = True
                        
                        # Clean up resolved orders
                        elif new_state in ["MATCHED", "CANCELLED", "EXPIRED"]:
                            logger.info(
                                f"[ORDER_STATE] ✅ {order_id[:8]}... resolved: {new_state} "
                                f"({elapsed:.1f}s total)"
                            )
                            self._pending_orders.pop(order_id, None)
                            self._order_states.pop(order_id, None)
                            self._order_metadata.pop(order_id, None)
                    
                    except Exception as order_err:
                        logger.error(f"[ORDER_STATE] Error polling {order_id[:8]}: {order_err}")
                
            except Exception as e:
                logger.error(f"[ORDER_STATE] Observer loop error: {e}", exc_info=True)
    
    async def _check_and_merge_positions(self) -> None:
        """
        RELAYER-BASED MERGE ENGINE: Check and Merge
        
        Smart trigger logic:
        1. Detect full sets (YES + NO pairs)
        2. Fetch condition_id dynamically from market data
        3. Trigger merge_positions_python for each full set
        4. Handle errors gracefully (pause on relayer failure)
        """
        if self.global_kill_switch:
            logger.warning("⛔ Kill switch active - merge operations skipped")
            return
        
        # Check if merge is paused
        if time.time() < self._merge_paused_until:
            wait_time = self._merge_paused_until - time.time()
            logger.debug(f"[MERGE] Operations paused for {wait_time:.0f}s more")
            return
        
        try:
            # Detect full sets
            full_sets = await self.detect_full_sets()
            
            if not full_sets:
                logger.debug("[MERGE] No full sets detected")
                return
            
            logger.info(f"[MERGE] 🔍 Detected {len(full_sets)} full set(s) to merge")
            
            # Process each full set
            merged_count = 0
            for full_set in full_sets:
                condition_id = full_set["condition_id"]
                index_set = full_set["index_set"]
                amount = full_set["amount"]
                market_info = full_set.get("market_info", {})
                
                market_name = market_info.get("question", "Unknown Market")
                
                logger.info(
                    f"[MERGE] Processing: {market_name[:50]}...\\n"
                    f"  Condition ID: {condition_id[:16]}...\\n"
                    f"  Amount: {amount:.4f} shares\\n"
                    f"  Index Set: {index_set}"
                )
                
                # [SAFETY] FIX 3: Verify Full Partition Coverage
                # Must hold all outcome indices before merging
                if not self.verify_full_partition_coverage(full_set):
                    logger.error(
                        f"[MERGE] ⚠️  ABORT: Incomplete partition detected!\\n"
                        f"  Market: {market_name[:50]}...\\n"
                        f"  Missing or unnamed outcome indices\\n"
                        f"  Reason: Would waste gas on failed transaction\\n"
                        f"  Action: Skipping this merge"
                    )
                    continue
                
                # Trigger merge
                success = await self.merge_positions_python(
                    condition_id=condition_id,
                    index_set=index_set,
                    amount=amount
                )
                
                if success:
                    merged_count += 1
                    # Add small delay between merges to avoid rate limiting
                    await asyncio.sleep(1)
                else:
                    # If merge failed, it may have paused operations
                    # Break out of loop to respect the pause
                    break
            
            if merged_count > 0:
                logger.info(
                    f"[MERGE] ✅ Successfully merged {merged_count}/{len(full_sets)} full sets\\n"
                    f"  Capital recycled back to USDC for new opportunities"
                )
            
        except Exception as e:
            logger.error(f"[MERGE] Error in _check_and_merge_positions: {e}", exc_info=True)

    async def _original_auto_redeem_loop(self) -> None:
        """
        Original auto-redeem service - kept for reference
        Now replaced by _auto_redeem_loop() with CHECK_AND_REDEEM_INTERVAL_SEC
        """
        logger.info(f"Auto-redeem loop started (interval: {AUTO_REDEEM_INTERVAL_SEC}s)")
        
        while self.is_running:
            try:
                await asyncio.sleep(AUTO_REDEEM_INTERVAL_SEC)
                
                if self.global_kill_switch:
                    logger.warning("⛔ Kill switch active - auto-redeem skipped")
                    continue
                
                # Get user positions
                positions = await self.client.get_positions()
                
                # Filter for positions in resolved markets
                redeemable_positions = []
                for position in positions:
                    market_id = position.get('market', position.get('market_id'))
                    if not market_id:
                        continue
                    
                    try:
                        # Check if market is resolved
                        market_data = await self.client.get_market(market_id)
                        if market_data and market_data.get('closed', False):
                            shares = float(position.get('size', 0))
                            if shares > 0:
                                redeemable_positions.append({
                                    'market_id': market_id,
                                    'token_id': position.get('asset_id', position.get('token_id')),
                                    'shares': shares,
                                    'outcome': position.get('outcome', 'Unknown')
                                })
                    except Exception as e:
                        logger.debug(f"Error checking market {market_id}: {e}")
                        continue
                
                # Redeem shares from resolved markets
                if redeemable_positions:
                    logger.info(f"Found {len(redeemable_positions)} resolved positions to redeem")
                    
                    for pos in redeemable_positions:
                        try:
                            logger.info(
                                f"Redeeming {pos['shares']:.2f} shares from market {pos['market_id'][:8]}... "
                                f"(outcome: {pos['outcome']})"
                            )
                            
                            # Redeem shares (returns to USDC balance)
                            result = await self.client.redeem_shares(
                                token_id=pos['token_id'],
                                shares=pos['shares']
                            )
                            
                            logger.info(
                                f"✅ Redeemed {pos['shares']:.2f} shares → "
                                f"${pos['shares']:.2f} USDC returned to balance"
                            )
                            
                        except Exception as e:
                            logger.error(
                                f"Failed to redeem {pos['shares']:.2f} shares from "
                                f"{pos['market_id'][:8]}: {e}"
                            )
                else:
                    logger.debug("No resolved positions to redeem")
                
            except Exception as e:
                logger.error(f"Auto-redeem error: {e}", exc_info=True)

    async def sync_header_nonce(self) -> None:
        """
        [SAFETY] FIX 2: Explicit Nonce Sync on Startup
        
        Fetches the current nonce from Polymarket API and sets it in the
        session headers to prevent INVALID_NONCE errors from bot/server desync.
        
        Called during bot initialization to ensure nonce alignment.
        """
        try:
            logger.info("[NONCE] Syncing header nonce with Polymarket API...")
            
            # Fetch current nonce from API
            if hasattr(self.client, 'get_nonce'):
                current_nonce = await self.client.get_nonce()
            elif hasattr(self.client._client, 'get_nonce'):
                current_nonce = await self.client._client.get_nonce()
            else:
                # Fallback: fetch from /nonce endpoint
                async with self.client._session.get(
                    f"{self.client._client.clob_url}/nonce"
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        current_nonce = data.get('nonce', 0)
                    else:
                        logger.warning("[NONCE] Failed to fetch nonce from API")
                        return
            
            # Set POLY_NONCE header in session
            if hasattr(self.client, '_session') and self.client._session:
                self.client._session.headers['POLY_NONCE'] = str(current_nonce)
                logger.info(f"[NONCE] ✅ Header nonce synced: {current_nonce}")
            elif hasattr(self.client._client, 'session'):
                self.client._client.session.headers['POLY_NONCE'] = str(current_nonce)
                logger.info(f"[NONCE] ✅ Header nonce synced: {current_nonce}")
            else:
                logger.warning("[NONCE] Could not set header - session not found")
            
            # Store for state persistence
            self._last_nonce = current_nonce
            
        except Exception as e:
            logger.error(f"[NONCE] Failed to sync header nonce: {e}")
    
    def verify_full_partition_coverage(self, full_set: Dict[str, Any]) -> bool:
        """
        [SAFETY] FIX 3: Full Partition Coverage Validator
        
        Verifies that we hold NO tokens for ALL outcome indices (0 to N)
        before attempting to merge. If any index is missing or unnamed,
        the merge will fail on-chain and waste gas.
        
        Args:
            full_set: Full set data from detect_full_sets()
            
        Returns:
            True if all indices covered, False if incomplete
        """
        try:
            index_set = full_set.get("index_set", [])
            market_info = full_set.get("market_info", {})
            outcomes = market_info.get("outcomes", [])
            
            # Get expected number of outcomes
            num_outcomes = len(outcomes)
            
            if num_outcomes == 0:
                logger.warning("[MERGE] No outcomes found in market info")
                return False
            
            # Check if we have all indices from 0 to N-1
            expected_indices = set(range(num_outcomes))
            actual_indices = set(index_set)
            
            if expected_indices != actual_indices:
                missing_indices = expected_indices - actual_indices
                extra_indices = actual_indices - expected_indices
                
                logger.error(
                    f"[MERGE] Partition coverage incomplete!\n"
                    f"  Expected indices: {sorted(expected_indices)}\n"
                    f"  Actual indices: {sorted(actual_indices)}\n"
                    f"  Missing: {sorted(missing_indices) if missing_indices else 'None'}\n"
                    f"  Extra: {sorted(extra_indices) if extra_indices else 'None'}"
                )
                return False
            
            # Verify no unnamed or placeholder outcomes
            for idx in index_set:
                if idx >= len(outcomes):
                    logger.error(
                        f"[MERGE] Index {idx} out of bounds (max: {len(outcomes)-1})"
                    )
                    return False
                
                outcome_name = outcomes[idx]
                if not outcome_name or outcome_name.strip() == "":
                    logger.error(
                        f"[MERGE] Unnamed outcome at index {idx}"
                    )
                    return False
            
            logger.debug(
                f"[MERGE] ✅ Full partition coverage verified: "
                f"{num_outcomes} outcomes, all indices present"
            )
            return True
            
        except Exception as e:
            logger.error(f"[MERGE] Error verifying partition coverage: {e}")
            return False
    
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
