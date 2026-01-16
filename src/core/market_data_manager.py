"""
Centralized Market Data Manager - Event-Driven WebSocket Architecture

Responsibilities:
-----------------
1. GlobalMarketCache: Thread-safe shared state for all real-time market data
2. PolymarketWSManager: Single authenticated WebSocket connection
3. Fill Dispatcher: Route fill events to appropriate strategies
4. Stale Data Protection: HFT-grade monitoring with 2-second staleness threshold

Architecture:
-------------
- Single WebSocket connection serves both ArbitrageStrategy and MarketMakingStrategy
- Shared memory cache eliminates REST polling
- asyncio.Queue prevents slow consumers from blocking WebSocket
- 5-second heartbeat with automatic reconnection

Usage:
------
```python
manager = MarketDataManager(client)
await manager.initialize()

# Subscribe to markets
await manager.subscribe_markets(['market_id_1', 'market_id_2'])

# Get latest price (synchronous cache read)
price = manager.get_latest_price('asset_id')

# Register strategy for fill notifications
manager.register_fill_handler('market_making', mm_strategy.handle_fill)
```
"""

from typing import Dict, Any, Optional, List, Callable, Set, Tuple
import asyncio
import time
import json
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.config.constants import DATA_STALENESS_THRESHOLD
from src.utils.logger import get_logger
from src.utils.exceptions import NetworkError


logger = get_logger(__name__)


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class MarketSnapshot:
    """Real-time market data snapshot with OBI (Order Book Imbalance)"""
    asset_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    mid_price: float
    micro_price: float  # Volume-weighted mid
    obi: float  # Order Book Imbalance: (bid_size - ask_size) / (bid_size + ask_size)
    last_update: float  # Unix timestamp
    bids: List[Dict[str, Any]] = field(default_factory=list)
    asks: List[Dict[str, Any]] = field(default_factory=list)
    
    # INSTITUTIONAL UPGRADE: Hash tracking & state validation
    hash: Optional[str] = None  # Orderbook hash for state divergence detection
    last_hash: Optional[str] = None  # Previous hash for duplicate detection
    dirty_cache: bool = False  # True if price_change received but not full book
    last_ws_activity: float = 0.0  # Last time ANY WebSocket message received (for inactive market detection)
    
    def is_stale(self, threshold_seconds: float = None) -> bool:
        """Check if data hasn't been updated in threshold seconds
        
        Args:
            threshold_seconds: Override threshold (defaults to DATA_STALENESS_THRESHOLD)
        
        PRODUCTION-GRADE: 5s threshold (5s heartbeat + buffer for network jitter)
        - Prevents false positives from network latency
        - Aligns with 5s WebSocket heartbeat interval
        - Previous 0.5s was too aggressive (caused spurious "stale data" warnings)
        - Institutional balance: Fast enough to detect real disconnects, slow enough to avoid false alarms
        """
        if threshold_seconds is None:
            threshold_seconds = DATA_STALENESS_THRESHOLD
        return (time.time() - self.last_update) > threshold_seconds
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for compatibility"""
        return {
            'asset_id': self.asset_id,
            'best_bid': self.best_bid,
            'best_ask': self.best_ask,
            'bid_size': self.bid_size,
            'ask_size': self.ask_size,
            'mid_price': self.mid_price,
            'micro_price': self.micro_price,
            'obi': self.obi,
            'last_update': self.last_update,
            'bids': self.bids,
            'asks': self.asks,
            'hash': self.hash,
            'dirty_cache': self.dirty_cache,
            'last_ws_activity': self.last_ws_activity,
        }
    
    def is_inactive(self, inactive_threshold: float = 60.0) -> bool:
        """Check if market has had no WebSocket activity (book or price_change)
        
        Args:
            inactive_threshold: Seconds without WebSocket activity (default: 60s)
        
        Returns:
            True if no WS messages for > threshold seconds
        
        INSTITUTIONAL INSIGHT: For inactive markets, you may be the only quoter.
        Your own orders won't trigger price_change events until someone trades.
        Proactive REST refresh prevents "phantom liquidity" quotes.
        """
        if self.last_ws_activity == 0.0:
            return False  # Never received WS message yet
        return (time.time() - self.last_ws_activity) > inactive_threshold


@dataclass
class FillEvent:
    """Order fill event from /user channel"""
    order_id: str
    client_id: Optional[str]
    asset_id: str
    side: str  # 'BUY' or 'SELL'
    price: float
    size: float
    timestamp: float
    market_id: Optional[str] = None
    

# ============================================================================
# MarketStateCache - Unified Thread-Safe Shared State (2026 HFT-Grade)
# ============================================================================

class MarketStateCache:
    """
    Unified thread-safe cache for real-time market data with institutional safety guards
    
    Features:
    ---------
    - O(1) lookups via dictionary
    - Thread-safe updates with RLock
    - Timestamp integrity checks (reject outdated WebSocket messages)
    - 2-second staleness detection with circuit breaker
    - Market-level staleness tracking
    - User fills cache for instant inventory updates
    
    Safety Guards (Institutional-Grade):
    ------------------------------------
    - Timestamp Integrity: Rejects messages older than current cache state
    - Lag Circuit Breaker: HFT-GRADE 500ms threshold (was 2s - too slow)
    - Thread Safety: All operations protected by RLock
    """
    
    def __init__(self, stale_threshold_seconds: float = None):
        """Initialize market state cache
        
        Args:
            stale_threshold_seconds: Override threshold (defaults to DATA_STALENESS_THRESHOLD)
        
        PRODUCTION-GRADE STALENESS: 5s default (5s heartbeat + buffer)
        - Per production review: Must accommodate 5s WebSocket heartbeat
        - Prevents spurious "stale data" warnings from network jitter
        - Previous 0.5s caused false positives (heartbeat delay triggers circuit breaker)
        - Balance: Fast enough to detect real disconnects, tolerant of normal latency
        """
        self._cache: Dict[str, MarketSnapshot] = {}
        self._lock = Lock()
        self._stale_threshold = stale_threshold_seconds if stale_threshold_seconds is not None else DATA_STALENESS_THRESHOLD
        self._stale_markets: Set[str] = set()
        
        # Market metadata cache
        self._market_info: Dict[str, Dict[str, Any]] = {}
        
        # User fills cache (for instant inventory updates)
        self._user_fills: Dict[str, List[FillEvent]] = {}  # asset_id -> list of fills
        
        # Disconnection callback handlers (INSTITUTIONAL SAFETY: Flash Cancel)
        self._disconnection_handlers: Dict[str, Callable[[], None]] = {}
        
        # Market update handlers (EVENT-DRIVEN ARCHITECTURE)
        # Format: {handler_name: (handler_func, market_filter)}
        # handler_func signature: async def handler(asset_id: str, snapshot: MarketSnapshot) -> None
        # market_filter: Optional set of asset_ids to watch (None = all markets)
        self._market_update_handlers: Dict[str, Tuple[Callable, Optional[Set[str]]]] = {}
        
        logger.info(
            f"MarketStateCache initialized (stale threshold: {self._stale_threshold}s) "
            f"[HFT-Grade Timestamp Integrity ENABLED]"
        )
    
    def update(self, asset_id: str, snapshot: MarketSnapshot, force: bool = False) -> bool:
        """
        Update cache with new market data (thread-safe with timestamp integrity)
        
        Args:
            asset_id: Asset identifier
            snapshot: New market snapshot
            force: Skip timestamp validation (for REST fallback)
            
        Returns:
            True if update accepted, False if rejected due to stale timestamp
        """
        with self._lock:
            # INSTITUTIONAL SAFETY: Timestamp Integrity Check
            # Reject WebSocket messages older than current cache state
            if not force and asset_id in self._cache:
                existing = self._cache[asset_id]
                if snapshot.last_update <= existing.last_update:
                    logger.debug(
                        f"[TIMESTAMP_INTEGRITY] Rejected stale update for {asset_id[:8]}... "
                        f"(incoming: {snapshot.last_update:.3f}, cached: {existing.last_update:.3f})"
                    )
                    return False
            
            self._cache[asset_id] = snapshot
            
            # Clear stale flag if data is fresh
            if asset_id in self._stale_markets:
                self._stale_markets.remove(asset_id)
            
            return True
    
    def get(self, asset_id: str) -> Optional[MarketSnapshot]:
        """Get latest snapshot (synchronous, thread-safe)"""
        with self._lock:
            return self._cache.get(asset_id)
    
    def get_latest_price(self, asset_id: str) -> Optional[float]:
        """Get latest micro-price (synchronous)"""
        snapshot = self.get(asset_id)
        return snapshot.micro_price if snapshot else None
    
    def get_order_book(self, asset_id: str) -> Optional[Dict[str, List]]:
        """Get full order book (compatible with old get_order_book API)"""
        snapshot = self.get(asset_id)
        if not snapshot:
            return None
        
        return {
            'bids': snapshot.bids,
            'asks': snapshot.asks,
            'asset_id': asset_id,
            'timestamp': snapshot.last_update,
        }
    
    def is_stale(self, asset_id: str) -> bool:
        """Check if asset data is stale (>threshold old)"""
        snapshot = self.get(asset_id)
        if not snapshot:
            return True
        return snapshot.is_stale(self._stale_threshold)
    
    def is_cache_fresh(self, asset_id: str) -> bool:
        """Check if asset cache has fresh data (inverse of is_stale)
        
        Returns True if asset exists in cache AND data is fresh.
        Use this in cache warmup to verify data is ready before quoting.
        
        Args:
            asset_id: Asset identifier
            
        Returns:
            True if cache has fresh data, False if missing or stale
        """
        return not self.is_stale(asset_id)
    
    def get_stale_markets(self) -> Set[str]:
        """Get all currently stale markets (LAG CIRCUIT BREAKER)"""
        with self._lock:
            stale = set()
            for asset_id, snapshot in self._cache.items():
                if snapshot.is_stale(self._stale_threshold):
                    stale.add(asset_id)
                    self._stale_markets.add(asset_id)
            return stale
    
    def clear_asset(self, asset_id: str) -> None:
        """Remove asset from cache"""
        with self._lock:
            self._cache.pop(asset_id, None)
            self._stale_markets.discard(asset_id)
            self._user_fills.pop(asset_id, None)
    
    def get_all_assets(self) -> List[str]:
        """Get list of all cached asset IDs"""
        with self._lock:
            return list(self._cache.keys())
    
    def set_market_info(self, market_id: str, info: Dict[str, Any]) -> None:
        """Cache market metadata (question, tokens, etc.)"""
        with self._lock:
            self._market_info[market_id] = info
    
    def get_market_info(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Get cached market metadata (thread-safe)"""
        with self._lock:
            return self._market_info.get(market_id)
    
    def add_fill_event(self, fill: FillEvent) -> None:
        """Add user fill event to cache (for instant inventory updates)"""
        with self._lock:
            if fill.asset_id not in self._user_fills:
                self._user_fills[fill.asset_id] = []
            self._user_fills[fill.asset_id].append(fill)
            
            # Keep only last 100 fills per asset to prevent memory bloat
            if len(self._user_fills[fill.asset_id]) > 100:
                self._user_fills[fill.asset_id] = self._user_fills[fill.asset_id][-100:]
    
    def get_recent_fills(self, asset_id: str, max_age_seconds: float = 60.0) -> List[FillEvent]:
        """Get recent fills for an asset (within max_age_seconds) - thread-safe"""
        with self._lock:
            if asset_id not in self._user_fills:
                return []
            
            current_time = time.time()
            return [
                fill for fill in self._user_fills[asset_id]
                if (current_time - fill.timestamp) <= max_age_seconds
            ]
    
    def register_disconnection_handler(self, name: str, handler: Callable[[], None]) -> None:
        """Register callback to be invoked on WebSocket disconnection
        
        INSTITUTIONAL SAFETY: Flash Cancel on Disconnect
        - Strategies register handlers to cancel all orders immediately
        - Prevents "blind quoting" when data feed is down
        """
        with self._lock:
            self._disconnection_handlers[name] = handler
            logger.info(f"Registered disconnection handler: {name}")
    
    def trigger_disconnection_callbacks(self) -> None:
        """Invoke all disconnection handlers (called when WebSocket drops)"""
        with self._lock:
            logger.critical("🚨 TRIGGERING DISCONNECTION CALLBACKS - Flash cancelling all orders")
            for name, handler in self._disconnection_handlers.items():
                try:
                    handler()
                    logger.info(f"✅ Disconnection handler executed: {name}")
                except Exception as e:
                    logger.error(f"❌ Disconnection handler failed ({name}): {e}", exc_info=True)
    
    def register_market_update_handler(
        self,
        name: str,
        handler: Callable,
        market_filter: Optional[Set[str]] = None
    ) -> None:
        """Register callback for market price updates (EVENT-DRIVEN ARCHITECTURE)
        
        Args:
            name: Unique handler name (e.g., 'arbitrage_scanner')
            handler: Async function to call on price updates
                     Signature: async def handler(asset_id: str, snapshot: MarketSnapshot) -> None
            market_filter: Optional set of asset_ids to watch (None = all markets)
        """
        with self._lock:
            self._market_update_handlers[name] = (handler, market_filter)
            logger.info(
                f"Registered market update handler: {name} "
                f"(filter: {len(market_filter) if market_filter else 'all'} markets)"
            )
    
    def unregister_market_update_handler(self, name: str) -> None:
        """Unregister market update handler"""
        with self._lock:
            if name in self._market_update_handlers:
                del self._market_update_handlers[name]
                logger.info(f"Unregistered market update handler: {name}")
    
    def get_market_update_handlers(self) -> List[Tuple[str, Callable, Optional[Set[str]]]]:
        """Get all registered market update handlers (for triggering)"""
        with self._lock:
            return [(name, handler, filter_set) for name, (handler, filter_set) in self._market_update_handlers.items()]


# Backwards compatibility alias
GlobalMarketCache = MarketStateCache


# ============================================================================
# PolymarketWSManager - Centralized WebSocket Connection
# ============================================================================

class PolymarketWSManager:
    """
    Manages single WebSocket connection to Polymarket
    
    Features:
    ---------
    - Dynamic subscription management
    - 5-second PING/PONG heartbeat
    - Automatic reconnection with exponential backoff
    - Fill event dispatching
    - Message distribution via asyncio.Queue
    """
    
    def __init__(
        self,
        client: Any,  # PolymarketClient
        cache: GlobalMarketCache,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        heartbeat_interval: float = 5.0,
    ):
        self.client = client
        self.cache = cache
        self.ws_url = ws_url
        self.heartbeat_interval = heartbeat_interval
        
        # WebSocket connection
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._is_connected = False
        self._is_running = False
        
        # Subscription tracking
        self._subscribed_assets: Set[str] = set()
        self._subscribed_markets: Set[str] = set()
        
        # Message distribution queues (prevents blocking)
        self._orderbook_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._fill_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        
        # Fill event handlers (strategy callbacks)
        self._fill_handlers: Dict[str, Callable] = {}  # strategy_name -> handler
        
        # Background tasks
        self._tasks: List[asyncio.Task] = []
        
        # Reconnection state
        self._reconnect_attempts = 0
        self._max_reconnect_delay = 60.0
        
        logger.info(
            f"PolymarketWSManager initialized - "
            f"URL: {ws_url}, Heartbeat: {heartbeat_interval}s"
        )
    
    async def initialize(self) -> None:
        """Start WebSocket connection and background tasks"""
        if self._is_running:
            logger.warning("WSManager already running")
            return
        
        self._is_running = True
        
        # Start connection
        await self._connect()
        
        # Start background workers
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop(), name="ws_heartbeat"),
            asyncio.create_task(self._receive_loop(), name="ws_receive"),
            asyncio.create_task(self._orderbook_processor(), name="orderbook_processor"),
            asyncio.create_task(self._fill_processor(), name="fill_processor"),
            asyncio.create_task(self._stale_data_monitor(), name="stale_monitor"),
        ]
        
        logger.info("✅ WebSocket Manager started - All background tasks running")
    
    async def shutdown(self) -> None:
        """Graceful shutdown"""
        logger.info("Shutting down WebSocket Manager...")
        self._is_running = False
        
        # Cancel all background tasks
        for task in self._tasks:
            task.cancel()
        
        await asyncio.gather(*self._tasks, return_exceptions=True)
        
        # Close WebSocket
        if self._ws:
            await self._ws.close()
        
        logger.info("WebSocket Manager stopped")
    
    async def _connect(self) -> None:
        """Establish WebSocket connection"""
        try:
            logger.info(f"Connecting to WebSocket: {self.ws_url}")
            
            # Note: WebSocket authentication not required per 2026 CLOB specs
            self._ws = await websockets.connect(
                self.ws_url,
                ping_interval=None,  # We handle our own heartbeat
                close_timeout=10,
            )
            
            self._is_connected = True
            self._reconnect_attempts = 0
            
            logger.info("✅ WebSocket connected")
            
            # Resubscribe to all previous subscriptions
            if self._subscribed_assets:
                await self._resubscribe_all()
                
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            self._is_connected = False
            await self._handle_reconnect()
    
    async def _handle_reconnect(self) -> None:
        """
        Handle reconnection with exponential backoff
        
        INSTITUTIONAL SAFETY: Exponential backoff with state rehydration
        - Delay = min(2^attempts, 60s)
        - After successful reconnect, trigger REST sync before resuming trading
        
        CRITICAL SAFETY:
        - Trigger disconnection callbacks IMMEDIATELY to cancel all orders
        """
        # CRITICAL: Trigger disconnection callbacks to cancel all orders
        self.cache.trigger_disconnection_callbacks()
        
        self._reconnect_attempts += 1
        delay = min(2 ** self._reconnect_attempts, self._max_reconnect_delay)
        
        logger.warning(
            f"🔄 Reconnecting in {delay}s (attempt {self._reconnect_attempts})... "
            f"[EXPONENTIAL BACKOFF]"
        )
        
        await asyncio.sleep(delay)
        await self._connect()
        
        # RELIABILITY: State Rehydration after reconnect
        if self._is_connected:
            logger.info("✅ Reconnected - Triggering state rehydration...")
            await self._rehydrate_state()
    
    async def _heartbeat_loop(self) -> None:
        """
        Send PING every heartbeat_interval seconds and measure latency.
        
        Institutional Feature:
        - Tracks round-trip latency (PING → PONG)
        - Exposes get_latency_ms() for trading strategies
        - Used by latency-based kill switches
        """
        while self._is_running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                
                if self._is_connected and self._ws:
                    # Measure latency: send PING and wait for PONG
                    ping_start = time.time()
                    pong_waiter = await self._ws.ping()
                    await asyncio.wait_for(pong_waiter, timeout=2.0)
                    latency_ms = (time.time() - ping_start) * 1000
                    
                    # Store latency for monitoring
                    if not hasattr(self, '_last_latency_ms'):
                        self._last_latency_ms = latency_ms
                    else:
                        # EMA: 0.9 * old + 0.1 * new
                        self._last_latency_ms = 0.9 * self._last_latency_ms + 0.1 * latency_ms
                    
                    logger.debug(f"WebSocket PING/PONG: {latency_ms:.1f}ms")
                    
            except asyncio.TimeoutError:
                logger.warning("Heartbeat timeout - no PONG received")
                self._is_connected = False
                await self._handle_reconnect()
            except Exception as e:
                logger.warning(f"Heartbeat error: {e}")
                self._is_connected = False
                await self._handle_reconnect()
    
    def get_latency_ms(self) -> Optional[float]:
        """
        Get current WebSocket latency in milliseconds.
        
        Returns:
            EMA-smoothed latency or None if no measurements yet
        """
        return getattr(self, '_last_latency_ms', None)
    
    async def _receive_loop(self) -> None:
        """Receive messages from WebSocket and route to queues"""
        while self._is_running:
            try:
                if not self._is_connected or not self._ws:
                    await asyncio.sleep(1)
                    continue
                
                message = await self._ws.recv()
                
                # Skip ping/pong frames and empty messages
                if not message or not isinstance(message, str):
                    continue
                
                # Parse JSON with error handling for empty messages
                try:
                    data = json.loads(message)
                except json.JSONDecodeError:
                    # Skip empty or malformed messages (common with Polymarket WS)
                    continue
                
                # Handle unexpected message formats (lists, None, etc.)
                if not isinstance(data, dict):
                    logger.debug(f"Skipping non-dict message: {type(data)}")
                    continue
                
                # Route message to appropriate queue
                # Per Polymarket support: messages use 'event_type' field
                event_type = data.get('event_type') or data.get('type')
                
                # Debug: Log first few messages to verify format
                if not hasattr(self, '_debug_msg_count'):
                    self._debug_msg_count = 0
                if self._debug_msg_count < 5:
                    logger.info(f"[WS DEBUG] Received message: event_type={event_type}, keys={list(data.keys())[:10]}")
                    self._debug_msg_count += 1
                
                # Market channel events: book, price_change, last_trade_price
                if event_type in ['book', 'price_change', 'last_trade_price']:
                    # Order book update
                    try:
                        self._orderbook_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        logger.warning("Orderbook queue full - dropping message")
                        
                # User channel events: order (with type: PLACEMENT/UPDATE/CANCELLATION)
                elif event_type == 'order':
                    # Order event (placement, update, cancellation, fill)
                    try:
                        self._fill_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        logger.warning("Order event queue full - dropping message")
                
                else:
                    logger.debug(f"Unknown message type: {msg_type}")
                    
            except ConnectionClosed:
                logger.warning("WebSocket connection closed")
                self._is_connected = False
                
                # CRITICAL: Trigger disconnection callbacks immediately
                self.cache.trigger_disconnection_callbacks()
                
                await self._handle_reconnect()
                
            except Exception as e:
                logger.error(f"Receive loop error: {e}", exc_info=True)
                await asyncio.sleep(0.1)
    
    async def _orderbook_processor(self) -> None:
        """Process order book updates and update cache"""
        while self._is_running:
            try:
                data = await self._orderbook_queue.get()
                
                # Debug: Log first few order book messages
                if not hasattr(self, '_debug_ob_count'):
                    self._debug_ob_count = 0
                if self._debug_ob_count < 3:
                    event_type = data.get('event_type', 'unknown')
                    logger.info(f"[WS DEBUG] Processing {event_type}: keys={list(data.keys())}, data preview={str(data)[:200]}")
                    self._debug_ob_count += 1
                
                # Parse order book data
                # Per Polymarket support: messages use 'market' field (not 'asset_id')
                asset_id = data.get('market') or data.get('asset_id')
                if not asset_id:
                    logger.warning(f"[WS] Message missing 'market' field: {data}")
                    continue
                
                event_type = data.get('event_type')
                
                # Handle different event types
                if event_type == 'price_change':
                    # ═══════════════════════════════════════════════════════════════════
                    # INSTITUTIONAL RULE: Price change events are INCREMENTAL updates
                    # Per Polymarket Support (Jan 2026):
                    #   - Contains: asset_id, price, size, side (BUY/SELL), hash, best_bid, best_ask
                    #   - Designed for incremental updates, NOT full book replacement
                    #   - Update best_bid/best_ask for accurate spreads
                    #   - Mark dirty_cache=True to signal: "Full depth may be stale"
                    # ═══════════════════════════════════════════════════════════════════
                    
                    current_time = time.time()
                    
                    # Extract price_change fields per Polymarket support guidance
                    asset_id_field = data.get('asset_id') or data.get('market')
                    best_bid_new = data.get('best_bid')
                    best_ask_new = data.get('best_ask')
                    price = data.get('price')
                    size = data.get('size')
                    side = data.get('side')  # 'BUY' or 'SELL'
                    hash_new = data.get('hash')
                    
                    # Fallback: Check for price_changes array (older format)
                    price_changes = data.get('price_changes', [])
                    
                    if asset_id_field and (best_bid_new is not None or best_ask_new is not None):
                        # New format: Direct best_bid/best_ask fields
                        existing = self.cache.get(asset_id_field)
                        
                        if existing:
                            # INSTITUTIONAL: Update best_bid/best_ask incrementally
                            if best_bid_new is not None:
                                existing.best_bid = float(best_bid_new)
                            if best_ask_new is not None:
                                existing.best_ask = float(best_ask_new)
                            
                            # Recalculate mid and micro prices
                            existing.mid_price = (existing.best_bid + existing.best_ask) / 2.0
                            existing.micro_price = existing.mid_price  # Simplified for price_change
                            
                            # Update metadata
                            existing.last_update = current_time
                            existing.last_ws_activity = current_time
                            existing.dirty_cache = True  # Full depth NOT updated
                            if hash_new:
                                existing.hash = hash_new
                            
                            self.cache.update(asset_id_field, existing, force=True)
                            
                            logger.debug(
                                f"[WS] price_change: {asset_id_field[:8]}... "
                                f"bid={existing.best_bid:.4f} ask={existing.best_ask:.4f} "
                                f"(dirty_cache=True)"
                            )
                        else:
                            # No cache yet - create minimal snapshot with best_bid/ask
                            snapshot = MarketSnapshot(
                                asset_id=asset_id_field,
                                best_bid=float(best_bid_new) if best_bid_new else float(price or 0.5),
                                best_ask=float(best_ask_new) if best_ask_new else float(price or 0.5),
                                bid_size=float(size or 0.0) if side == 'BUY' else 0.0,
                                ask_size=float(size or 0.0) if side == 'SELL' else 0.0,
                                mid_price=float(price or 0.5),
                                micro_price=float(price or 0.5),
                                obi=0.0,
                                last_update=current_time,
                                last_ws_activity=current_time,
                                bids=[],  # Empty - needs REST /book for full depth
                                asks=[],  # Empty - needs REST /book for full depth
                                dirty_cache=True,  # Needs full book
                                hash=hash_new
                            )
                            self.cache.update(asset_id_field, snapshot, force=True)
                    
                    elif price_changes:
                        # Fallback: Old format with price_changes array
                        for change in price_changes:
                            token_id = change.get('token_id') or change.get('asset_id')
                            price = change.get('price')
                            if token_id and price:
                                existing = self.cache.get(token_id)
                                if existing:
                                    existing.last_update = current_time
                                    existing.last_ws_activity = current_time
                                    existing.mid_price = float(price)
                                    existing.micro_price = float(price)
                                    existing.dirty_cache = True
                                    self.cache.update(token_id, existing, force=True)
                    
                    continue
                
                # ═══════════════════════════════════════════════════════════════════
                # Handle 'book' events - FULL ORDER BOOK SNAPSHOTS ("THICK" messages)
                # ═══════════════════════════════════════════════════════════════════
                # INSTITUTIONAL RULES:
                # 1. book events contain full depth arrays (bids/asks)
                # 2. Hash validation: Detect state divergence
                # 3. Field normalization: buys/sells → bids/asks at ingestion layer
                # 4. Clear dirty_cache flag after successful book update
                # ═══════════════════════════════════════════════════════════════════
                # Per Polymarket Support (Jan 2026): "Book events are full snapshots, not deltas"
                # These arrive when orders are placed/cancelled (may be infrequent for inactive markets)
                # Structure: {"asset_id": "...", "market": "...", "buys": [...], "sells": [...], "timestamp": ..., "hash": "..."}
                
                if event_type == 'book':
                    current_time = time.time()
                    book_hash = data.get('hash')
                    
                    # Log book event arrival (helpful for debugging inactive markets)
                    logger.debug(
                        f"[WS] book event: {asset_id[:8]}... "
                        f"(hash: {book_hash[:8] if book_hash else 'none'}...)"
                    )
                
                # ═══════════════════════════════════════════════════════════════════
                # INSTITUTIONAL FIELD NORMALIZATION (Ingestion Layer)
                # ═══════════════════════════════════════════════════════════════════
                # Polymarket inconsistency:
                #   - Gamma API (REST): Returns 'bids' and 'asks'
                #   - CLOB WebSocket: Returns 'buys' and 'sells'
                # 
                # Normalize at ingestion so strategy layer ONLY sees 'bids' and 'asks'
                # ═══════════════════════════════════════════════════════════════════
                bids = data.get('buys') or data.get('bids', [])  # Normalize: buys → bids
                asks = data.get('sells') or data.get('asks', [])  # Normalize: sells → asks
                
                if not bids or not asks:
                    continue
                
                # Calculate prices
                best_bid = float(bids[0]['price'])
                best_ask = float(asks[0]['price'])
                bid_size = float(bids[0]['size'])
                ask_size = float(asks[0]['size'])
                
                mid_price = (best_bid + best_ask) / 2.0
                
                # Volume-weighted micro-price
                total_vol = bid_size + ask_size
                if total_vol > 0:
                    micro_price = ((bid_size * best_ask) + (ask_size * best_bid)) / total_vol
                else:
                    micro_price = mid_price
                
                # ═══════════════════════════════════════════════════════════════════
                # WEIGHTED ORDER BOOK IMBALANCE (OBI) - 2026 HFT INSTITUTIONAL UPGRADE
                # ═══════════════════════════════════════════════════════════════════
                # OLD (Simple Volume Ratio): OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol)
                # Problem: Massive "wall" 10 ticks away skews signal as much as best bid/ask
                #
                # NEW (Distance-Weighted Decay): Weight = Volume / Distance²
                # Rationale: Liquidity far from mid has exponentially less price impact
                # Formula: OBI = (Weighted_Bid_Vol - Weighted_Ask_Vol) / Total_Weighted_Vol
                # ═══════════════════════════════════════════════════════════════════
                
                from decimal import Decimal
                
                weighted_bid_vol = Decimal('0')
                weighted_ask_vol = Decimal('0')
                
                # Process top 10 levels with distance-weighted decay
                for bid_level in bids[:10]:
                    bid_price_dec = Decimal(str(bid_level['price']))
                    bid_size_dec = Decimal(str(bid_level['size']))
                    distance_from_mid = abs(Decimal(str(mid_price)) - bid_price_dec)
                    
                    # Avoid division by zero (if bid == mid, use small epsilon)
                    if distance_from_mid < Decimal('0.0001'):
                        distance_from_mid = Decimal('0.0001')
                    
                    # Weight = Volume / Distance²
                    weight = bid_size_dec / (distance_from_mid ** 2)
                    weighted_bid_vol += weight
                
                for ask_level in asks[:10]:
                    ask_price_dec = Decimal(str(ask_level['price']))
                    ask_size_dec = Decimal(str(ask_level['size']))
                    distance_from_mid = abs(Decimal(str(mid_price)) - ask_price_dec)
                    
                    if distance_from_mid < Decimal('0.0001'):
                        distance_from_mid = Decimal('0.0001')
                    
                    weight = ask_size_dec / (distance_from_mid ** 2)
                    weighted_ask_vol += weight
                
                # Calculate weighted OBI
                total_weighted_vol = weighted_bid_vol + weighted_ask_vol
                if total_weighted_vol > 0:
                    obi = float((weighted_bid_vol - weighted_ask_vol) / total_weighted_vol)
                else:
                    obi = 0.0
                
                # Log first calculation for transparency
                if not hasattr(self, '_obi_logged'):
                    self._obi_logged = set()
                
                if asset_id not in self._obi_logged:
                    logger.info(
                        f"[WEIGHTED OBI] {asset_id[:8]}... - "
                        f"Weighted: {obi:+.3f} (bid_weight={float(weighted_bid_vol):.2f}, "
                        f"ask_weight={float(weighted_ask_vol):.2f}) - "
                        f"Distance-decay ensures nearby liquidity dominates signal"
                    )
                    self._obi_logged.add(asset_id)
                
                # ═══════════════════════════════════════════════════════════════════
                # INSTITUTIONAL: Hash Validation & State Divergence Detection
                # ═══════════════════════════════════════════════════════════════════
                book_hash = data.get('hash')
                existing = self.cache.get(asset_id)
                
                # Duplicate detection: If hash matches last_hash, skip update
                if existing and book_hash and existing.last_hash == book_hash:
                    logger.debug(
                        f"[WS] Duplicate book message for {asset_id[:8]}... "
                        f"(hash: {book_hash[:8]}...) - skipping"
                    )
                    continue
                
                # State divergence detection would go here if we had local hash calculation
                # For now, trust the exchange hash and track it for duplicate detection
                
                current_time = time.time()
                
                # Create snapshot with full depth
                snapshot = MarketSnapshot(
                    asset_id=asset_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    mid_price=mid_price,
                    micro_price=micro_price,
                    obi=obi,
                    last_update=current_time,
                    last_ws_activity=current_time,  # Track WS activity for inactive market detection
                    bids=bids[:10],  # Keep top 10 levels
                    asks=asks[:10],
                    hash=book_hash,  # Track orderbook hash
                    last_hash=existing.hash if existing else None,  # Previous hash
                    dirty_cache=False  # Clear dirty flag - we have full book now
                )
                
                # Update cache
                updated = self.cache.update(asset_id, snapshot)
                
                # Trigger market update handlers (EVENT-DRIVEN)
                if updated:
                    await self._trigger_market_update_handlers(asset_id, snapshot)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Orderbook processor error: {e}", exc_info=True)
    
    async def _trigger_market_update_handlers(self, asset_id: str, snapshot: MarketSnapshot) -> None:
        """Trigger all registered market update handlers for an asset"""
        handlers = self.cache.get_market_update_handlers()
        
        for name, handler, market_filter in handlers:
            try:
                # Check if this handler wants updates for this market
                if market_filter is not None and asset_id not in market_filter:
                    continue
                
                # Call handler asynchronously
                if asyncio.iscoroutinefunction(handler):
                    await handler(asset_id, snapshot)
                else:
                    handler(asset_id, snapshot)
                    
            except Exception as e:
                logger.error(f"Market update handler error ({name}): {e}", exc_info=True)
    
    async def _fill_processor(self) -> None:
        """Process order events and dispatch to strategies"""
        while self._is_running:
            try:
                data = await self._fill_queue.get()
                
                # Per Polymarket support: user channel sends event_type="order" 
                # with type field (PLACEMENT/UPDATE/CANCELLATION) and size_matched for fills
                event_type = data.get('event_type')
                order_type = data.get('type', '')  # PLACEMENT, UPDATE, CANCELLATION
                
                # Only process filled orders (size_matched > 0)
                size_matched = float(data.get('size_matched', 0))
                if event_type == 'order' and size_matched > 0:
                    # Parse fill event from order event
                    fill = FillEvent(
                        order_id=data.get('id', ''),  # Support uses 'id' not 'order_id'
                        client_id=data.get('client_id'),
                        asset_id=data.get('asset_id', ''),
                        side=data.get('side', ''),
                        price=float(data.get('price', 0)),
                        size=size_matched,  # Use size_matched for filled amount
                        timestamp=float(data.get('timestamp', time.time())),
                        market_id=data.get('market'),  # Support uses 'market' not 'market_id'
                    )
                    
                    logger.info(
                        f"[FILL] {fill.side} {fill.size:.1f} @ {fill.price:.4f} "
                        f"(order: {fill.order_id[:8]}..., type: {order_type})"
                    )
                    
                    # Dispatch to all registered handlers
                    for strategy_name, handler in self._fill_handlers.items():
                        try:
                            # Call handler asynchronously
                            if asyncio.iscoroutinefunction(handler):
                                await handler(fill)
                            else:
                                handler(fill)
                        except Exception as e:
                            logger.error(
                                f"Fill handler error ({strategy_name}): {e}",
                                exc_info=True
                            )
                else:
                    # Log non-fill order events at debug level
                    logger.debug(
                        f"Order event: {order_type}, "
                        f"asset: {data.get('asset_id', '')[:8]}..., "
                        f"size_matched: {size_matched}"
                    )
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fill processor error: {e}", exc_info=True)
    
    async def _stale_data_monitor(self) -> None:
        """Monitor for stale data and log warnings
        
        Uses DATA_STALENESS_THRESHOLD (5s) to avoid false positives for inactive markets.
        Per Polymarket support: Markets with no trading activity won't send WebSocket updates.
        """
        while self._is_running:
            try:
                await asyncio.sleep(1.0)
                
                stale_markets = self.cache.get_stale_markets()
                if stale_markets:
                    logger.warning(
                        f"⚠️ STALE DATA: {len(stale_markets)} markets have not "
                        f"received updates in {self.cache._stale_threshold}+ seconds: {list(stale_markets)[:5]}"
                    )
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stale monitor error: {e}")
    
    async def subscribe_assets(self, asset_ids: List[str]) -> None:
        """Subscribe to real-time order book updates for assets
        
        INSTITUTIONAL ARCHITECTURE (per Polymarket Support Jan 2026):
        1. Subscribe to WebSocket for real-time updates
        2. Immediately fetch REST /book snapshot for initial state
        3. Cache full order book (buys/sells with depth)
        4. Apply incremental updates from WebSocket (book events = full snapshots, price_change = price updates)
        
        This ensures:
        - Zero-latency local order book access (no REST on every quote)
        - Accurate depth for market making decisions
        - Resilient to inactive markets (REST provides initial state even if no book events)
        """
        if not self._is_connected or not self._ws:
            logger.warning("Cannot subscribe - WebSocket not connected")
            return
        
        for asset_id in asset_ids:
            if asset_id in self._subscribed_assets:
                continue
            
            try:
                # Step 1: Subscribe to WebSocket for real-time updates
                # Polymarket WebSocket subscription format (per support: Jan 2026)
                # Correct format: {"type":"market","assets_ids":[tokenId]}
                subscribe_msg = {
                    "type": "market",
                    "assets_ids": [asset_id],
                }
                
                await self._ws.send(json.dumps(subscribe_msg))
                self._subscribed_assets.add(asset_id)
                
                logger.info(f"[WS] Subscribed to asset: {asset_id[:8]}... (full ID: {asset_id})")
                
                # Step 2: Immediately fetch initial order book snapshot via REST
                # Per Polymarket Support: "On connection, fetch an initial snapshot via REST /book endpoint"
                try:
                    book_data = await self.client.get_order_book(asset_id)
                    if book_data:
                        # Parse and cache order book
                        bids = book_data.bids if hasattr(book_data, 'bids') else []
                        asks = book_data.asks if hasattr(book_data, 'asks') else []
                        
                        if bids and asks:
                            # Handle both dict and object formats
                            def get_price_size(item):
                                if isinstance(item, dict):
                                    return float(item['price']), float(item.get('size', 0))
                                else:
                                    return float(item.price), float(getattr(item, 'size', 0))
                            
                            best_bid, bid_size = get_price_size(bids[0])
                            best_ask, ask_size = get_price_size(asks[0])
                            
                            mid_price = (best_bid + best_ask) / 2.0
                            total_vol = bid_size + ask_size
                            micro_price = ((bid_size * best_ask) + (ask_size * best_bid)) / total_vol if total_vol > 0 else mid_price
                            obi = (bid_size - ask_size) / total_vol if total_vol > 0 else 0.0
                            
                            # Convert to dict format for cache
                            bids_list = [{'price': get_price_size(b)[0], 'size': get_price_size(b)[1]} for b in bids]
                            asks_list = [{'price': get_price_size(a)[0], 'size': get_price_size(a)[1]} for a in asks]
                            
                            # Create snapshot and cache
                            snapshot = MarketSnapshot(
                                asset_id=asset_id,
                                best_bid=best_bid,
                                best_ask=best_ask,
                                bid_size=bid_size,
                                ask_size=ask_size,
                                mid_price=mid_price,
                                micro_price=micro_price,
                                obi=obi,
                                last_update=time.time(),
                                bids=bids_list,
                                asks=asks_list
                            )
                            self.cache.update(asset_id, snapshot, force=True)
                            logger.info(f"[REST] Cached initial order book for {asset_id[:8]}... (bid: {best_bid}, ask: {best_ask}, depth: {len(bids_list)}x{len(asks_list)})")
                        else:
                            logger.warning(f"[REST] Empty order book for {asset_id[:8]}... (may be inactive)")
                except Exception as e:
                    logger.error(f"[REST] Failed to fetch initial snapshot for {asset_id[:8]}...: {e}")
                    # Continue anyway - WebSocket updates will populate cache when book events arrive
                
            except Exception as e:
                logger.error(f"Subscription error for {asset_id[:8]}...: {e}")
    
    async def subscribe_user_channel(self) -> None:
        """Subscribe to user-specific fill events"""
        if not self._is_connected or not self._ws:
            return
        
        try:
            # Subscribe to user's order events (per support: requires apiKey, secret, passphrase)
            # Note: User channel auth format from Polymarket support (Jan 2026)
            subscribe_msg = {
                "type": "user",
                "markets": [],  # Empty array = subscribe to all markets
                "auth": {
                    "apiKey": self.client.api_key,
                    "secret": self.client.api_secret,
                    "passphrase": self.client.api_passphrase,
                }
            }
            
            await self._ws.send(json.dumps(subscribe_msg))
            logger.info("Subscribed to user order channel")
            
        except Exception as e:
            logger.error(f"User channel subscription error: {e}")
    
    async def _rehydrate_state(self) -> None:
        """
        RELIABILITY: Rehydrate cache from REST API after reconnection
        
        After every successful WebSocket reconnect, we sync local cache
        with server state via REST to ensure consistency before resuming trading.
        
        This prevents trading on stale data during connection outages.
        """
        try:
            logger.info("[REHYDRATE] Syncing cache with REST API...")
            
            # Get all subscribed assets
            assets = list(self._subscribed_assets)
            
            if not assets:
                logger.debug("[REHYDRATE] No assets to sync")
                return
            
            # Fetch fresh order books from REST API
            rehydrated_count = 0
            for asset_id in assets:
                try:
                    # Fetch from REST (this should call client.get_order_book)
                    book_data = await self.client.get_order_book(asset_id)
                    
                    if not book_data or 'bids' not in book_data or 'asks' not in book_data:
                        continue
                    
                    bids = book_data['bids']
                    asks = book_data['asks']
                    
                    if not bids or not asks:
                        continue
                    
                    # Create snapshot from REST data
                    best_bid = float(bids[0]['price'])
                    best_ask = float(asks[0]['price'])
                    bid_size = float(bids[0]['size'])
                    ask_size = float(asks[0]['size'])
                    
                    mid_price = (best_bid + best_ask) / 2.0
                    total_vol = bid_size + ask_size
                    micro_price = ((bid_size * best_ask) + (ask_size * best_bid)) / total_vol if total_vol > 0 else mid_price
                    
                    snapshot = MarketSnapshot(
                        asset_id=asset_id,
                        best_bid=best_bid,
                        best_ask=best_ask,
                        bid_size=bid_size,
                        ask_size=ask_size,
                        mid_price=mid_price,
                        micro_price=micro_price,
                        last_update=time.time(),
                        bids=bids[:10],
                        asks=asks[:10],
                    )
                    
                    # Force update (skip timestamp check since this is REST sync)
                    self.cache.update(asset_id, snapshot, force=True)
                    rehydrated_count += 1
                    
                except Exception as e:
                    logger.warning(f"[REHYDRATE] Failed to sync {asset_id[:8]}...: {e}")
                    continue
            
            logger.info(
                f"✅ [REHYDRATE] Cache synced: {rehydrated_count}/{len(assets)} assets updated from REST"
            )
            
        except Exception as e:
            logger.error(f"[REHYDRATE] State rehydration failed: {e}", exc_info=True)
    
            logger.error(f"User channel subscription error: {e}")
    
    async def _resubscribe_all(self) -> None:
        """Resubscribe to all assets after reconnection"""
        logger.info(f"Resubscribing to {len(self._subscribed_assets)} assets...")
        
        assets = list(self._subscribed_assets)
        self._subscribed_assets.clear()
        
        await self.subscribe_assets(assets)
        await self.subscribe_user_channel()
    
    def register_fill_handler(self, strategy_name: str, handler: Callable) -> None:
        """Register strategy to receive fill events"""
        self._fill_handlers[strategy_name] = handler
        logger.info(f"Registered fill handler for {strategy_name}")
    
    def unregister_fill_handler(self, strategy_name: str) -> None:
        """Unregister strategy fill handler"""
        self._fill_handlers.pop(strategy_name, None)


# ============================================================================
# MarketDataManager - Unified Interface
# ============================================================================

class MarketDataManager:
    """
    Unified interface for real-time market data
    
    Combines GlobalMarketCache + PolymarketWSManager into single API
    """
    
    def __init__(
        self,
        client: Any,
        stale_threshold: float = None,  # PRODUCTION: Uses DATA_STALENESS_THRESHOLD (5s)
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    ):
        self.client = client
        self.cache = GlobalMarketCache(stale_threshold_seconds=stale_threshold if stale_threshold is not None else DATA_STALENESS_THRESHOLD)
        self.ws_manager = PolymarketWSManager(client, self.cache, ws_url=ws_url)
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._inactive_threshold = 60.0  # 60s without WebSocket activity triggers REST refresh
        
        logger.info("MarketDataManager created")
    
    async def initialize(self) -> None:
        """Start WebSocket connection and background tasks"""
        await self.ws_manager.initialize()
        
        # Start inactive market heartbeat task
        self._heartbeat_task = asyncio.create_task(self._inactive_market_heartbeat())
        
        logger.info("✅ MarketDataManager initialized (with inactive market heartbeat)")
    
    async def shutdown(self) -> None:
        """Graceful shutdown"""
        # Cancel heartbeat task
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        
        await self.ws_manager.shutdown()
    
    async def _inactive_market_heartbeat(self) -> None:
        """
        INSTITUTIONAL STALE HEARTBEAT: Proactive REST refresh for inactive markets
        
        Logic:
        - Check all cached markets every 30s
        - If no WebSocket activity for 60s → REST refresh
        - Prevents quoting stale prices in inactive markets
        - Your own orders won't trigger price_change events until someone trades
        """
        logger.info(
            f"[HEARTBEAT] Inactive market monitor started "
            f"(check interval: 30s, inactive threshold: {self._inactive_threshold}s)"
        )
        
        while True:
            try:
                await asyncio.sleep(30.0)  # Check every 30 seconds
                
                all_assets = self.cache.get_all_assets()
                if not all_assets:
                    continue
                
                inactive_count = 0
                refreshed_count = 0
                
                for asset_id in all_assets:
                    snapshot = self.cache.get(asset_id)
                    if not snapshot:
                        continue
                    
                    # Check if market is inactive (no WS messages for 60s)
                    if snapshot.is_inactive(self._inactive_threshold):
                        inactive_count += 1
                        
                        logger.info(
                            f"[HEARTBEAT] Inactive market detected: {asset_id[:8]}... "
                            f"(no WS activity for {time.time() - snapshot.last_ws_activity:.0f}s) "
                            f"- Triggering REST refresh"
                        )
                        
                        # Refresh from REST API
                        success = await self.force_refresh_from_rest(asset_id)
                        if success:
                            refreshed_count += 1
                
                if inactive_count > 0:
                    logger.info(
                        f"[HEARTBEAT] Inactive market scan complete: "
                        f"{inactive_count} inactive, {refreshed_count} refreshed via REST"
                    )
                
            except asyncio.CancelledError:
                logger.info("[HEARTBEAT] Inactive market monitor stopped")
                break
            except Exception as e:
                logger.error(f"[HEARTBEAT] Error in inactive market monitor: {e}", exc_info=True)
                await asyncio.sleep(5.0)  # Brief pause before retry
    
    async def subscribe_markets(self, market_ids: List[str]) -> None:
        """Subscribe to markets (auto-discovers asset IDs)"""
        asset_ids = []
        
        for market_id in market_ids:
            # Get market info from client
            try:
                market = await self.client.get_market(market_id)
                if market:
                    tokens = market.get('tokens', [])
                    for token in tokens:
                        asset_id = token.get('token_id')
                        if asset_id:
                            asset_ids.append(asset_id)
                    
                    # Cache market info
                    self.cache.set_market_info(market_id, market)
            except Exception as e:
                logger.warning(f"Failed to get market info for {market_id[:8]}...: {e}")
        
        # Subscribe to all asset IDs
        if asset_ids:
            await self.ws_manager.subscribe_assets(asset_ids)
    
    async def subscribe_assets(self, asset_ids: List[str]) -> None:
        """Subscribe to specific asset IDs"""
        await self.ws_manager.subscribe_assets(asset_ids)
    
    def register_market_update_handler(self, handler_name: str, handler: Callable, filter_assets: Optional[Set[str]] = None) -> None:
        """Register handler for market update events"""
        self.ws_manager.register_market_update_handler(handler_name, handler, filter_assets)
    
    def register_disconnection_handler(self, handler_name: str, handler: Callable) -> None:
        """Register handler for WebSocket disconnection events"""
        self.ws_manager.register_disconnection_handler(handler_name, handler)
    
    def get_latest_price(self, asset_id: str) -> Optional[float]:
        """Get latest micro-price (synchronous)"""
        return self.cache.get_latest_price(asset_id)
    
    def get_order_book(self, asset_id: str) -> Optional[Dict[str, List]]:
        """Get full order book (compatible with old API)"""
        return self.cache.get_order_book(asset_id)
    
    def is_market_stale(self, asset_id: str) -> bool:
        """Check if market data is stale"""
        return self.cache.is_stale(asset_id)
    
    def register_fill_handler(self, strategy_name: str, handler: Callable) -> None:
        """Register strategy to receive fill events"""
        self.ws_manager.register_fill_handler(strategy_name, handler)
    
    def get_latency_ms(self) -> Optional[float]:
        """Get current WebSocket latency in milliseconds"""
        return self.ws_manager.get_latency_ms()
    
    def get_market_info(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Get cached market metadata"""
        return self.cache.get_market_info(market_id)
    
    def get_stale_markets(self) -> Set[str]:
        """
        Get all markets with stale data (LAG CIRCUIT BREAKER)
        
        Returns set of asset_ids that haven't been updated beyond DATA_STALENESS_THRESHOLD.
        Strategies should use this to trigger emergency quote cancellation.
        """
        return self.cache.get_stale_markets()
    
    def check_market_staleness(self, asset_ids: List[str]) -> bool:
        """
        Check if any of the given markets are stale
        
        Returns True if ANY market in the list is stale (>threshold old).
        Use this before executing trades to ensure data freshness.
        """
        for asset_id in asset_ids:
            if self.cache.is_stale(asset_id):
                return True
        return False
    
    def is_cache_fresh(self, asset_id: str) -> bool:
        """Check if cache has fresh data for asset
        
        Used by strategies to verify cache warmup before quoting.
        
        Args:
            asset_id: Asset identifier
            
        Returns:
            True if cache has fresh data, False if missing or stale
        """
        return self.cache.is_cache_fresh(asset_id)
    
    async def force_refresh_from_rest(self, asset_id: str) -> bool:
        """
        Force refresh a single asset from REST API
        
        Used as fallback when WebSocket data is stale.
        Returns True if successful.
        """
        try:
            book_data = await self.client.get_order_book(asset_id)
            
            if not book_data or 'bids' not in book_data or 'asks' not in book_data:
                return False
            
            bids = book_data['bids']
            asks = book_data['asks']
            
            if not bids or not asks:
                return False
            
            # Create snapshot from REST data
            best_bid = float(bids[0]['price'])
            best_ask = float(asks[0]['price'])
            bid_size = float(bids[0]['size'])
            ask_size = float(asks[0]['size'])
            
            mid_price = (best_bid + best_ask) / 2.0
            total_vol = bid_size + ask_size
            micro_price = ((bid_size * best_ask) + (ask_size * best_bid)) / total_vol if total_vol > 0 else mid_price
            
            snapshot = MarketSnapshot(
                asset_id=asset_id,
                best_bid=best_bid,
                best_ask=best_ask,
                bid_size=bid_size,
                ask_size=ask_size,
                mid_price=mid_price,
                micro_price=micro_price,
                last_update=time.time(),
                bids=bids[:10],
                asks=asks[:10],
            )
            
            # Force update
            self.cache.update(asset_id, snapshot, force=True)
            logger.debug(f"[REST_REFRESH] Refreshed {asset_id[:8]}... from REST API")
            return True
            
        except Exception as e:
            logger.error(f"[REST_REFRESH] Failed to refresh {asset_id[:8]}...: {e}")
            return False
