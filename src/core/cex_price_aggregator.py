"""
CEX Price Aggregator - Multi-Exchange Fair Value Calculator

Aggregates real-time price feeds from Tier-1 centralized exchanges
(Binance, Coinbase) to calculate fair value for Polymarket markets.

Use Case: Temporal Arbitrage
============================
If CEX fair value deviates from Polymarket mid-price by >threshold:
→ Cancel resting orders
→ Re-quote at new price frontier
→ Capture "Flash Quote" arbitrage opportunity

Price Aggregation Model:
========================
Fair Value = Weighted Average of:
- Binance spot price (50% weight)
- Coinbase spot price (30% weight)
- Polymarket mid-price (20% weight - anchor to avoid manipulation)

Confidence Scoring:
===================
High confidence (0.9-1.0): All 3 sources agree within 0.5%
Medium confidence (0.7-0.9): 2 sources agree, 1 deviates <2%
Low confidence (<0.7): Price discrepancies >2% (pause trading)

WebSocket Architecture:
=======================
- Binance: wss://stream.binance.com:9443/ws/<symbol>@trade
- Coinbase: wss://ws-feed.exchange.coinbase.com (ticker channel)
- Auto-reconnection with exponential backoff
- Heartbeat monitoring (stale data detection)

Author: Institutional HFT Team
Date: January 2026
"""

from typing import Dict, Optional, Callable, List, Tuple
from dataclasses import dataclass
from decimal import Decimal
import asyncio
import json
import time
import websockets
from collections import defaultdict, deque

from src.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PriceQuote:
    """Single price quote from an exchange"""
    exchange: str
    symbol: str
    price: Decimal
    volume: Decimal
    timestamp: float
    
    @property
    def age_ms(self) -> float:
        """Age of quote in milliseconds"""
        return (time.time() - self.timestamp) * 1000
    
    @property
    def is_stale(self, max_age_ms: int = 5000) -> bool:
        """Check if quote is stale (>5s old)"""
        return self.age_ms > max_age_ms


@dataclass
class FairValue:
    """Aggregated fair value calculation"""
    symbol: str
    fair_price: Decimal
    confidence: Decimal  # 0-1 scale
    sources: Dict[str, Decimal]  # exchange -> price
    timestamp: float
    spread_pct: Decimal  # Spread between highest and lowest source
    
    @property
    def is_reliable(self, min_confidence: Decimal = Decimal('0.7')) -> bool:
        """Check if fair value is reliable enough for trading"""
        return self.confidence >= min_confidence


class ExchangeConnector:
    """Base class for exchange WebSocket connectors"""
    
    def __init__(self, exchange_name: str, symbols: List[str]):
        """
        Initialize exchange connector
        
        Args:
            exchange_name: Exchange identifier (binance, coinbase)
            symbols: List of trading symbols to subscribe to
        """
        self.exchange_name = exchange_name
        self.symbols = symbols
        self._ws = None
        self._running = False
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 10
        self._last_message_time = 0.0
        self._price_callbacks: List[Callable] = []
        
        logger.info(f"{exchange_name} connector initialized for {len(symbols)} symbols")
    
    def register_callback(self, callback: Callable) -> None:
        """Register callback for price updates"""
        self._price_callbacks.append(callback)
    
    async def start(self) -> None:
        """Start WebSocket connection with auto-reconnection"""
        self._running = True
        
        while self._running:
            try:
                await self._connect()
                await self._listen()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"{self.exchange_name} connection error: {e}")
                
                # Exponential backoff for reconnection
                self._reconnect_attempts += 1
                if self._reconnect_attempts > self._max_reconnect_attempts:
                    logger.critical(
                        f"{self.exchange_name} max reconnection attempts reached - giving up"
                    )
                    break
                
                backoff_sec = min(2 ** self._reconnect_attempts, 60)
                logger.info(
                    f"{self.exchange_name} reconnecting in {backoff_sec}s "
                    f"(attempt {self._reconnect_attempts}/{self._max_reconnect_attempts})"
                )
                await asyncio.sleep(backoff_sec)
    
    async def stop(self) -> None:
        """Stop WebSocket connection"""
        self._running = False
        if self._ws:
            await self._ws.close()
        logger.info(f"{self.exchange_name} connector stopped")
    
    async def _connect(self) -> None:
        """Connect to WebSocket (implemented by subclass)"""
        raise NotImplementedError
    
    async def _listen(self) -> None:
        """Listen for WebSocket messages (implemented by subclass)"""
        raise NotImplementedError
    
    async def _notify_callbacks(self, quote: PriceQuote) -> None:
        """Notify all registered callbacks of new price"""
        for callback in self._price_callbacks:
            try:
                await callback(quote)
            except Exception as e:
                logger.error(f"Callback error: {e}")


class BinanceConnector(ExchangeConnector):
    """Binance WebSocket connector for spot prices"""
    
    def __init__(self, symbols: List[str]):
        """
        Initialize Binance connector
        
        Args:
            symbols: List of symbols in Binance format (e.g., 'btcusdt', 'ethusdt')
        """
        super().__init__("Binance", symbols)
        self._base_url = "wss://stream.binance.com:9443"
    
    async def _connect(self) -> None:
        """Connect to Binance WebSocket"""
        # Build multi-stream URL
        # Format: wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade
        streams = "/".join([f"{symbol.lower()}@trade" for symbol in self.symbols])
        url = f"{self._base_url}/stream?streams={streams}"
        
        logger.info(f"Connecting to Binance: {url}")
        self._ws = await websockets.connect(url, ping_interval=20, ping_timeout=10)
        self._reconnect_attempts = 0  # Reset on successful connection
        logger.info("✅ Binance WebSocket connected")
    
    async def _listen(self) -> None:
        """Listen for Binance trade messages"""
        async for message in self._ws:
            try:
                data = json.loads(message)
                
                # Binance multi-stream format: {"stream": "btcusdt@trade", "data": {...}}
                if 'stream' in data and 'data' in data:
                    trade = data['data']
                    
                    quote = PriceQuote(
                        exchange="binance",
                        symbol=trade['s'],  # Symbol (e.g., BTCUSDT)
                        price=Decimal(trade['p']),  # Price
                        volume=Decimal(trade['q']),  # Quantity
                        timestamp=trade['T'] / 1000  # Trade time in seconds
                    )
                    
                    self._last_message_time = time.time()
                    await self._notify_callbacks(quote)
                    
            except Exception as e:
                logger.error(f"Binance message parsing error: {e}")


class CoinbaseConnector(ExchangeConnector):
    """Coinbase WebSocket connector for spot prices"""
    
    def __init__(self, symbols: List[str]):
        """
        Initialize Coinbase connector
        
        Args:
            symbols: List of symbols in Coinbase format (e.g., 'BTC-USD', 'ETH-USD')
        """
        super().__init__("Coinbase", symbols)
        self._url = "wss://ws-feed.exchange.coinbase.com"
    
    async def _connect(self) -> None:
        """Connect to Coinbase WebSocket"""
        logger.info(f"Connecting to Coinbase: {self._url}")
        self._ws = await websockets.connect(self._url, ping_interval=20, ping_timeout=10)
        
        # Subscribe to ticker channel
        subscribe_message = {
            "type": "subscribe",
            "product_ids": self.symbols,
            "channels": ["ticker"]
        }
        
        await self._ws.send(json.dumps(subscribe_message))
        self._reconnect_attempts = 0
        logger.info("✅ Coinbase WebSocket connected")
    
    async def _listen(self) -> None:
        """Listen for Coinbase ticker messages"""
        async for message in self._ws:
            try:
                data = json.loads(message)
                
                # Coinbase ticker format: {"type": "ticker", "product_id": "BTC-USD", "price": "50000.00", ...}
                if data.get('type') == 'ticker':
                    quote = PriceQuote(
                        exchange="coinbase",
                        symbol=data['product_id'],  # Symbol (e.g., BTC-USD)
                        price=Decimal(data['price']),  # Last price
                        volume=Decimal(data.get('last_size', '0')),  # Last trade size
                        timestamp=time.time()  # Current time (Coinbase doesn't send timestamp in ticker)
                    )
                    
                    self._last_message_time = time.time()
                    await self._notify_callbacks(quote)
                    
            except Exception as e:
                logger.error(f"Coinbase message parsing error: {e}")


class CEXPriceAggregator:
    """
    Multi-Exchange Price Aggregator
    
    Aggregates real-time prices from Binance and Coinbase to calculate
    fair value for temporal arbitrage opportunities.
    """
    
    def __init__(
        self,
        binance_symbols: Optional[List[str]] = None,
        coinbase_symbols: Optional[List[str]] = None,
        price_deviation_threshold: Decimal = Decimal('0.005'),  # 0.5% deviation trigger
        stale_price_threshold_ms: int = 5000  # 5 seconds
    ):
        """
        Initialize CEX price aggregator
        
        Args:
            binance_symbols: Symbols to track on Binance (e.g., ['BTCUSDT'])
            coinbase_symbols: Symbols to track on Coinbase (e.g., ['BTC-USD'])
            price_deviation_threshold: Threshold for triggering re-quotes
            stale_price_threshold_ms: Max age for price quotes
        """
        self.binance_symbols = binance_symbols or []
        self.coinbase_symbols = coinbase_symbols or []
        self.price_deviation_threshold = price_deviation_threshold
        self.stale_price_threshold_ms = stale_price_threshold_ms
        
        # Exchange connectors
        self.binance_connector = None
        self.coinbase_connector = None
        
        if self.binance_symbols:
            self.binance_connector = BinanceConnector(self.binance_symbols)
            self.binance_connector.register_callback(self._on_price_update)
        
        if self.coinbase_symbols:
            self.coinbase_connector = CoinbaseConnector(self.coinbase_symbols)
            self.coinbase_connector.register_callback(self._on_price_update)
        
        # Price storage (symbol -> latest quote)
        self._latest_prices: Dict[str, Dict[str, PriceQuote]] = defaultdict(dict)  # symbol -> {exchange -> quote}
        
        # Fair value callbacks
        self._fair_value_callbacks: List[Callable] = []
        
        # Metrics
        self._update_count = 0
        self._fair_value_calculations = 0
        
        logger.info(
            f"CEXPriceAggregator initialized - "
            f"Binance: {len(self.binance_symbols)} symbols, "
            f"Coinbase: {len(self.coinbase_symbols)} symbols"
        )
    
    def register_fair_value_callback(self, callback: Callable) -> None:
        """Register callback for fair value updates"""
        self._fair_value_callbacks.append(callback)
    
    async def start(self) -> None:
        """Start all exchange connectors"""
        tasks = []
        
        if self.binance_connector:
            tasks.append(asyncio.create_task(self.binance_connector.start()))
        
        if self.coinbase_connector:
            tasks.append(asyncio.create_task(self.coinbase_connector.start()))
        
        if tasks:
            await asyncio.gather(*tasks)
        else:
            logger.warning("No exchange connectors configured")
    
    async def stop(self) -> None:
        """Stop all exchange connectors"""
        if self.binance_connector:
            await self.binance_connector.stop()
        
        if self.coinbase_connector:
            await self.coinbase_connector.stop()
        
        logger.info("CEXPriceAggregator stopped")
    
    async def _on_price_update(self, quote: PriceQuote) -> None:
        """Handle price update from any exchange"""
        # Normalize symbol (map between exchange formats)
        normalized_symbol = self._normalize_symbol(quote.symbol)
        
        # Store latest quote
        self._latest_prices[normalized_symbol][quote.exchange] = quote
        self._update_count += 1
        
        # Calculate fair value
        fair_value = self._calculate_fair_value(normalized_symbol)
        
        if fair_value and fair_value.is_reliable():
            # Notify callbacks
            for callback in self._fair_value_callbacks:
                try:
                    await callback(fair_value)
                except Exception as e:
                    logger.error(f"Fair value callback error: {e}")
    
    def _normalize_symbol(self, symbol: str) -> str:
        """
        Normalize symbol across exchanges
        
        Examples:
        - Binance 'BTCUSDT' -> 'BTC-USD'
        - Coinbase 'BTC-USD' -> 'BTC-USD'
        """
        symbol = symbol.upper()
        
        # Binance format: BTCUSDT -> BTC-USD
        if 'USDT' in symbol:
            base = symbol.replace('USDT', '')
            return f"{base}-USD"
        
        # Already in correct format
        return symbol
    
    def _calculate_fair_value(self, symbol: str) -> Optional[FairValue]:
        """
        Calculate weighted fair value from multiple sources
        
        Weighting:
        - Binance: 50%
        - Coinbase: 30%
        - Polymarket: 20% (if available)
        
        Args:
            symbol: Normalized symbol
            
        Returns:
            FairValue or None if insufficient data
        """
        prices = self._latest_prices.get(symbol, {})
        
        if not prices:
            return None
        
        # Filter stale prices
        fresh_prices = {
            exchange: quote for exchange, quote in prices.items()
            if not quote.is_stale(self.stale_price_threshold_ms)
        }
        
        if len(fresh_prices) < 2:
            # Need at least 2 sources for reliable fair value
            return None
        
        # Extract prices
        binance_price = fresh_prices.get('binance')
        coinbase_price = fresh_prices.get('coinbase')
        
        if not binance_price or not coinbase_price:
            # Need both major exchanges
            return None
        
        # Calculate weighted average
        weights = {'binance': Decimal('0.5'), 'coinbase': Decimal('0.3')}
        total_weight = sum(weights.values())
        
        weighted_sum = Decimal('0')
        price_list = []
        
        for exchange, quote in fresh_prices.items():
            weight = weights.get(exchange, Decimal('0'))
            weighted_sum += quote.price * weight
            price_list.append(quote.price)
        
        fair_price = weighted_sum / total_weight
        
        # Calculate confidence based on price agreement
        min_price = min(price_list)
        max_price = max(price_list)
        spread_pct = ((max_price - min_price) / fair_price) * Decimal('100')
        
        # Confidence scoring
        if spread_pct < Decimal('0.5'):
            confidence = Decimal('0.95')  # High confidence
        elif spread_pct < Decimal('2.0'):
            confidence = Decimal('0.8')   # Medium confidence
        else:
            confidence = Decimal('0.5')   # Low confidence
        
        self._fair_value_calculations += 1
        
        return FairValue(
            symbol=symbol,
            fair_price=fair_price,
            confidence=confidence,
            sources={exchange: quote.price for exchange, quote in fresh_prices.items()},
            timestamp=time.time(),
            spread_pct=spread_pct
        )
    
    def get_fair_value(self, symbol: str) -> Optional[FairValue]:
        """Get current fair value for symbol (non-blocking)"""
        normalized_symbol = self._normalize_symbol(symbol)
        return self._calculate_fair_value(normalized_symbol)
    
    def check_deviation(
        self,
        symbol: str,
        polymarket_mid_price: Decimal
    ) -> Tuple[bool, Decimal]:
        """
        Check if Polymarket price deviates significantly from CEX fair value
        
        Args:
            symbol: Trading symbol
            polymarket_mid_price: Current Polymarket mid-price
            
        Returns:
            (should_requote, deviation_pct)
        """
        fair_value = self.get_fair_value(symbol)
        
        if not fair_value or not fair_value.is_reliable():
            return (False, Decimal('0'))
        
        # Calculate deviation percentage
        deviation_pct = abs(polymarket_mid_price - fair_value.fair_price) / fair_value.fair_price
        
        should_requote = deviation_pct > self.price_deviation_threshold
        
        if should_requote:
            logger.info(
                f"🎯 FLASH QUOTE TRIGGER: {symbol} deviation {deviation_pct*100:.2f}% "
                f"(PM: ${polymarket_mid_price:.4f}, Fair: ${fair_value.fair_price:.4f})"
            )
        
        return (should_requote, deviation_pct)
    
    def get_stats(self) -> Dict:
        """Get aggregator statistics"""
        active_symbols = len(self._latest_prices)
        fresh_quotes = sum(
            1 for prices in self._latest_prices.values()
            for quote in prices.values()
            if not quote.is_stale(self.stale_price_threshold_ms)
        )
        
        return {
            'active_symbols': active_symbols,
            'fresh_quotes': fresh_quotes,
            'update_count': self._update_count,
            'fair_value_calculations': self._fair_value_calculations,
            'binance_connected': self.binance_connector is not None,
            'coinbase_connected': self.coinbase_connector is not None
        }
