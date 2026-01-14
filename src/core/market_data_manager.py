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

from typing import Dict, Any, Optional, List, Callable, Set
import asyncio
import time
import json
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from utils.logger import get_logger
from utils.exceptions import NetworkError


logger = get_logger(__name__)


# ============================================================================
# Data Structures
# ============================================================================

@dataclass
class MarketSnapshot:
    """Real-time market data snapshot"""
    asset_id: str
    best_bid: float
    best_ask: float
    bid_size: float
    ask_size: float
    mid_price: float
    micro_price: float  # Volume-weighted mid
    last_update: float  # Unix timestamp
    bids: List[Dict[str, Any]] = field(default_factory=list)
    asks: List[Dict[str, Any]] = field(default_factory=list)
    
    def is_stale(self, threshold_seconds: float = 2.0) -> bool:
        """Check if data hasn't been updated in threshold seconds"""
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
            'last_update': self.last_update,
            'bids': self.bids,
            'asks': self.asks,
        }


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
# GlobalMarketCache - Thread-Safe Shared State
# ============================================================================

class GlobalMarketCache:
    """
    Thread-safe cache for real-time market data
    
    Features:
    ---------
    - O(1) lookups via dictionary
    - Thread-safe updates with RLock
    - Automatic stale data detection
    - Market-level staleness tracking
    """
    
    def __init__(self, stale_threshold_seconds: float = 2.0):
        self._cache: Dict[str, MarketSnapshot] = {}
        self._lock = Lock()
        self._stale_threshold = stale_threshold_seconds
        self._stale_markets: Set[str] = set()
        
        # Market metadata cache
        self._market_info: Dict[str, Dict[str, Any]] = {}
        
        logger.info(f"GlobalMarketCache initialized (stale threshold: {stale_threshold_seconds}s)")
    
    def update(self, asset_id: str, snapshot: MarketSnapshot) -> None:
        """Update cache with new market data (thread-safe)"""
        with self._lock:
            self._cache[asset_id] = snapshot
            # Clear stale flag if data is fresh
            if asset_id in self._stale_markets:
                self._stale_markets.remove(asset_id)
    
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
        """Check if asset data is stale"""
        snapshot = self.get(asset_id)
        if not snapshot:
            return True
        return snapshot.is_stale(self._stale_threshold)
    
    def get_stale_markets(self) -> Set[str]:
        """Get all currently stale markets"""
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
    
    def get_all_assets(self) -> List[str]:
        """Get list of all cached asset IDs"""
        with self._lock:
            return list(self._cache.keys())
    
    def set_market_info(self, market_id: str, info: Dict[str, Any]) -> None:
        """Cache market metadata (question, tokens, etc.)"""
        with self._lock:
            self._market_info[market_id] = info
    
    def get_market_info(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Get cached market metadata"""
        with self._lock:
            return self._market_info.get(market_id)


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
            
            # TODO: Add authentication headers if required by Polymarket
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
        """Handle reconnection with exponential backoff"""
        self._reconnect_attempts += 1
        delay = min(2 ** self._reconnect_attempts, self._max_reconnect_delay)
        
        logger.warning(
            f"Reconnecting in {delay}s (attempt {self._reconnect_attempts})..."
        )
        
        await asyncio.sleep(delay)
        await self._connect()
    
    async def _heartbeat_loop(self) -> None:
        """Send PING every heartbeat_interval seconds"""
        while self._is_running:
            try:
                await asyncio.sleep(self.heartbeat_interval)
                
                if self._is_connected and self._ws:
                    await self._ws.ping()
                    logger.debug("WebSocket PING sent")
                    
            except Exception as e:
                logger.warning(f"Heartbeat error: {e}")
                self._is_connected = False
                await self._handle_reconnect()
    
    async def _receive_loop(self) -> None:
        """Receive messages from WebSocket and route to queues"""
        while self._is_running:
            try:
                if not self._is_connected or not self._ws:
                    await asyncio.sleep(1)
                    continue
                
                message = await self._ws.recv()
                data = json.loads(message)
                
                # Route message to appropriate queue
                msg_type = data.get('type') or data.get('event_type')
                
                if msg_type in ['book', 'last_trade_price', 'orderbook_delta']:
                    # Order book update
                    try:
                        self._orderbook_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        logger.warning("Orderbook queue full - dropping message")
                        
                elif msg_type in ['fill', 'order_filled', 'trade']:
                    # Fill event
                    try:
                        self._fill_queue.put_nowait(data)
                    except asyncio.QueueFull:
                        logger.warning("Fill queue full - dropping message")
                
                else:
                    logger.debug(f"Unknown message type: {msg_type}")
                    
            except ConnectionClosed:
                logger.warning("WebSocket connection closed")
                self._is_connected = False
                await self._handle_reconnect()
                
            except Exception as e:
                logger.error(f"Receive loop error: {e}", exc_info=True)
                await asyncio.sleep(0.1)
    
    async def _orderbook_processor(self) -> None:
        """Process order book updates and update cache"""
        while self._is_running:
            try:
                data = await self._orderbook_queue.get()
                
                # Parse order book data
                asset_id = data.get('asset_id') or data.get('market')
                if not asset_id:
                    continue
                
                # Extract bids/asks
                bids = data.get('bids', [])
                asks = data.get('asks', [])
                
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
                
                # Create snapshot
                snapshot = MarketSnapshot(
                    asset_id=asset_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    mid_price=mid_price,
                    micro_price=micro_price,
                    last_update=time.time(),
                    bids=bids[:10],  # Keep top 10 levels
                    asks=asks[:10],
                )
                
                # Update cache
                self.cache.update(asset_id, snapshot)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Orderbook processor error: {e}", exc_info=True)
    
    async def _fill_processor(self) -> None:
        """Process fill events and dispatch to strategies"""
        while self._is_running:
            try:
                data = await self._fill_queue.get()
                
                # Parse fill event
                fill = FillEvent(
                    order_id=data.get('order_id', ''),
                    client_id=data.get('client_id'),
                    asset_id=data.get('asset_id', ''),
                    side=data.get('side', ''),
                    price=float(data.get('price', 0)),
                    size=float(data.get('size', 0)),
                    timestamp=time.time(),
                    market_id=data.get('market_id'),
                )
                
                logger.info(
                    f"[FILL] {fill.side} {fill.size:.1f} @ {fill.price:.4f} "
                    f"(order: {fill.order_id[:8]}...)"
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
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fill processor error: {e}", exc_info=True)
    
    async def _stale_data_monitor(self) -> None:
        """Monitor for stale data and log warnings"""
        while self._is_running:
            try:
                await asyncio.sleep(1.0)
                
                stale_markets = self.cache.get_stale_markets()
                if stale_markets:
                    logger.warning(
                        f"⚠️ STALE DATA: {len(stale_markets)} markets have not "
                        f"received updates in 2+ seconds: {list(stale_markets)[:5]}"
                    )
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Stale monitor error: {e}")
    
    async def subscribe_assets(self, asset_ids: List[str]) -> None:
        """Subscribe to real-time order book updates for assets"""
        if not self._is_connected or not self._ws:
            logger.warning("Cannot subscribe - WebSocket not connected")
            return
        
        for asset_id in asset_ids:
            if asset_id in self._subscribed_assets:
                continue
            
            try:
                # Polymarket WebSocket subscription format
                subscribe_msg = {
                    "type": "subscribe",
                    "channel": "orderbook",
                    "asset_id": asset_id,
                }
                
                await self._ws.send(json.dumps(subscribe_msg))
                self._subscribed_assets.add(asset_id)
                
                logger.info(f"Subscribed to asset: {asset_id[:8]}...")
                
            except Exception as e:
                logger.error(f"Subscription error for {asset_id[:8]}...: {e}")
    
    async def subscribe_user_channel(self) -> None:
        """Subscribe to user-specific fill events"""
        if not self._is_connected or not self._ws:
            return
        
        try:
            # Subscribe to user's order fills
            subscribe_msg = {
                "type": "subscribe",
                "channel": "user",
                "auth": {
                    "address": self.client.get_address(),
                    # Add signature if required
                }
            }
            
            await self._ws.send(json.dumps(subscribe_msg))
            logger.info("Subscribed to user fill channel")
            
        except Exception as e:
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
        stale_threshold: float = 2.0,
        ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market",
    ):
        self.client = client
        self.cache = GlobalMarketCache(stale_threshold_seconds=stale_threshold)
        self.ws_manager = PolymarketWSManager(client, self.cache, ws_url=ws_url)
        
        logger.info("MarketDataManager created")
    
    async def initialize(self) -> None:
        """Start WebSocket connection and background tasks"""
        await self.ws_manager.initialize()
        logger.info("✅ MarketDataManager initialized")
    
    async def shutdown(self) -> None:
        """Graceful shutdown"""
        await self.ws_manager.shutdown()
    
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
    
    def get_market_info(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Get cached market metadata"""
        return self.cache.get_market_info(market_id)
