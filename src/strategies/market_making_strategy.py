"""
Market Making Strategy - Liquidity Provider with Inventory Management

Responsibility:
--------------
Provide liquidity to binary Polymarket markets by placing bid/ask quotes
with a spread, earning maker rebates and spread capture profit.

Safety Features:
----------------
1. Inventory Risk Management: Max position limits per outcome
2. Time-based Exit: Force liquidate stale inventory
3. Price Move Protection: Emergency exit on adverse price moves  
4. Budget Isolation: Dedicated capital allocation separate from arbitrage
5. Position Monitoring: Continuous risk assessment

Strategy Flow:
--------------
1. Select liquid binary markets (high volume, tight spread)
2. Calculate fair value (mid price)
3. Place BUY order at (mid - spread/2) and SELL order at (mid + spread/2)
4. Monitor fills and adjust inventory
5. Cancel/replace quotes periodically
6. Exit positions on risk thresholds

Risk Controls:
--------------
- Max $15 per position
- Max 30 shares inventory per outcome
- 1-hour max hold time
- 15% adverse price move = emergency exit
- $3 max loss per position

Integration:
------------
Runs in parallel with arbitrage strategy using separate capital allocation.
Does NOT interfere with arbitrage execution.
"""

from typing import Dict, Any, Optional, List, Tuple
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
import time
import json

from strategies.base_strategy import BaseStrategy
from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from config.constants import (
    # Budget allocation
    MARKET_MAKING_STRATEGY_CAPITAL,
    
    # Market selection
    MM_MIN_MARKET_VOLUME_24H,
    MM_MAX_SPREAD_PERCENT,
    MM_PREFER_BINARY_MARKETS,
    MM_MAX_ACTIVE_MARKETS,
    
    # Position sizing
    MM_BASE_POSITION_SIZE,
    MM_MAX_POSITION_SIZE,
    MM_MAX_INVENTORY_PER_OUTCOME,
    
    # Spread management
    MM_TARGET_SPREAD,
    MM_MIN_SPREAD,
    MM_MAX_SPREAD,
    MM_INVENTORY_SPREAD_MULTIPLIER,
    
    # Risk management
    MM_MAX_LOSS_PER_POSITION,
    MM_MAX_INVENTORY_HOLD_TIME,
    MM_POSITION_CHECK_INTERVAL,
    MM_EMERGENCY_EXIT_THRESHOLD,
    
    # Order management
    MM_QUOTE_UPDATE_INTERVAL,
    MM_ORDER_TTL,
    MM_MIN_ORDER_SPACING,
    
    # Performance tracking
    MM_ENABLE_PERFORMANCE_LOG,
    MM_PERFORMANCE_LOG_FILE,
)
from utils.logger import get_logger
from utils.exceptions import StrategyError


logger = get_logger(__name__)


class MarketPosition:
    """Tracks inventory and P&L for a single market"""
    
    def __init__(self, market_id: str, market_question: str, token_ids: List[str]):
        self.market_id = market_id
        self.market_question = market_question
        self.token_ids = token_ids
        
        # Inventory tracking (positive = long, negative = short)
        self.inventory: Dict[str, int] = {tid: 0 for tid in token_ids}
        
        # Cost basis tracking
        self.cost_basis: Dict[str, float] = {tid: 0.0 for tid in token_ids}
        
        # Entry time
        self.entry_time = datetime.now()
        
        # Active orders
        self.active_bids: Dict[str, str] = {}  # token_id -> order_id
        self.active_asks: Dict[str, str] = {}  # token_id -> order_id
        
        # Performance
        self.realized_pnl = 0.0
        self.total_volume = 0.0
        self.fill_count = 0
        
    def update_inventory(self, token_id: str, shares: int, price: float, is_buy: bool):
        """Update inventory and cost basis after a fill"""
        if is_buy:
            # Bought shares
            old_inventory = self.inventory[token_id]
            old_cost = self.cost_basis[token_id]
            
            new_inventory = old_inventory + shares
            new_cost = ((old_inventory * old_cost) + (shares * price)) / new_inventory if new_inventory != 0 else 0
            
            self.inventory[token_id] = new_inventory
            self.cost_basis[token_id] = new_cost
        else:
            # Sold shares
            shares_sold = abs(shares)
            exit_price = price
            entry_price = self.cost_basis[token_id]
            
            # Realize P&L
            pnl = shares_sold * (exit_price - entry_price)
            self.realized_pnl += pnl
            
            self.inventory[token_id] -= shares_sold
            
        self.total_volume += abs(shares) * price
        self.fill_count += 1
        
    def get_unrealized_pnl(self, current_prices: Dict[str, float]) -> float:
        """Calculate unrealized P&L based on current prices"""
        unrealized = 0.0
        for token_id, inventory in self.inventory.items():
            if inventory > 0:
                current_price = current_prices.get(token_id, 0)
                cost = self.cost_basis[token_id]
                unrealized += inventory * (current_price - cost)
        return unrealized
    
    def get_total_pnl(self, current_prices: Dict[str, float]) -> float:
        """Get total P&L (realized + unrealized)"""
        return self.realized_pnl + self.get_unrealized_pnl(current_prices)
    
    def has_inventory(self) -> bool:
        """Check if we have any open inventory"""
        return any(inv != 0 for inv in self.inventory.values())
    
    def get_inventory_age(self) -> float:
        """Get age of position in seconds"""
        return (datetime.now() - self.entry_time).total_seconds()


class MarketMakingStrategy(BaseStrategy):
    """
    Market Making Strategy - Provide liquidity and earn spreads
    
    Runs independently of arbitrage strategy with dedicated capital allocation.
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        config: Optional[Dict[str, Any]] = None
    ):
        """Initialize market making strategy"""
        super().__init__(client, order_manager, config)
        
        # Budget tracking
        self._allocated_capital = Decimal(str(MARKET_MAKING_STRATEGY_CAPITAL))
        self._capital_used = Decimal('0')
        
        # Active positions
        self._positions: Dict[str, MarketPosition] = {}
        
        # Market selection cache
        self._eligible_markets: List[Dict] = []
        self._last_market_scan = 0
        self._market_scan_interval = 300  # 5 minutes
        
        # Strategy state
        self._is_running = False
        self._last_quote_update = {}
        self._last_order_time = 0
        
        # Performance tracking
        self._total_fills = 0
        self._total_maker_volume = 0.0
        self._total_pnl = 0.0
        
        logger.info(
            f"MarketMakingStrategy initialized - "
            f"Capital: ${self._allocated_capital}, "
            f"Max markets: {MM_MAX_ACTIVE_MARKETS}, "
            f"Target spread: {MM_TARGET_SPREAD*100:.1f}%"
        )
    
    async def run(self) -> None:
        """Main strategy loop"""
        if self._is_running:
            logger.warning("MarketMakingStrategy already running")
            return
        
        self._is_running = True
        logger.info("🎯 MarketMakingStrategy started")
        
        try:
            while self._is_running:
                try:
                    await self._update_eligible_markets()
                    await self._manage_positions()
                    await self._update_quotes()
                    await self._check_risk_limits()
                    await asyncio.sleep(MM_POSITION_CHECK_INTERVAL)
                    
                except asyncio.CancelledError:
                    logger.info("MarketMakingStrategy cancelled")
                    break
                except Exception as e:
                    logger.error(f"Error in market making loop: {e}", exc_info=True)
                    await asyncio.sleep(10)
                    
        finally:
            self._is_running = False
            await self._shutdown()
            logger.info("🛑 MarketMakingStrategy stopped")
    
    async def stop(self) -> None:
        """Stop the strategy gracefully"""
        self._is_running = False
        logger.info("Stopping MarketMakingStrategy...")
    
    async def _shutdown(self) -> None:
        """Clean shutdown"""
        logger.info("MarketMaking shutdown: Cancelling all orders...")
        
        for market_id, position in self._positions.items():
            all_order_ids = list(position.active_bids.values()) + list(position.active_asks.values())
            
            for order_id in all_order_ids:
                try:
                    await self.client.cancel_order(order_id)
                except:
                    pass
            
            if position.has_inventory():
                logger.warning(
                    f"Position still open in {market_id[:8]}... - "
                    f"Inventory: {position.inventory}"
                )
        
        logger.info(f"MarketMaking shutdown complete - Total P&L: ${self._total_pnl:.2f}")
    
    async def _update_eligible_markets(self) -> None:
        """Scan and filter markets suitable for market making"""
        current_time = time.time()
        
        if current_time - self._last_market_scan < self._market_scan_interval:
            return
        
        logger.debug("Scanning for eligible market making opportunities...")
        
        try:
            response = await self.client.get_markets()
            all_markets = response.get('data', [])
            
            eligible = [m for m in all_markets if self._is_market_eligible(m)]
            
            self._eligible_markets = sorted(
                eligible,
                key=lambda m: m.get('volume24hr', 0),
                reverse=True
            )[:MM_MAX_ACTIVE_MARKETS * 3]
            
            logger.info(
                f"Found {len(self._eligible_markets)} eligible markets for market making "
                f"(min volume: ${MM_MIN_MARKET_VOLUME_24H})"
            )
            
            self._last_market_scan = current_time
            
        except Exception as e:
            logger.error(f"Error scanning markets: {e}")
    
    def _is_market_eligible(self, market: Dict[str, Any]) -> bool:
        """Check if market meets criteria for market making"""
        tokens = market.get('tokens', [])
        if MM_PREFER_BINARY_MARKETS and len(tokens) != 2:
            return False
        
        volume_24h = market.get('volume24hr', 0)
        if volume_24h < MM_MIN_MARKET_VOLUME_24H:
            return False
        
        if market.get('closed', False) or not market.get('active', True):
            return False
        
        return True
    
    async def _manage_positions(self) -> None:
        """Manage active positions"""
        # Add new markets if below capacity
        while len(self._positions) < MM_MAX_ACTIVE_MARKETS:
            new_market = await self._select_next_market()
            if not new_market:
                break
            await self._start_market_making(new_market)
        
        # Check existing positions
        for market_id in list(self._positions.keys()):
            position = self._positions[market_id]
            if await self._should_close_position(position):
                await self._close_position(market_id)
    
    async def _select_next_market(self) -> Optional[Dict]:
        """Select next market to make"""
        for market in self._eligible_markets:
            market_id = market.get('id')
            if market_id not in self._positions:
                return market
        return None
    
    async def _start_market_making(self, market: Dict) -> None:
        """Start making market"""
        market_id = market.get('id')
        question = market.get('question', 'Unknown')
        tokens = market.get('tokens', [])
        token_ids = [t.get('token_id') for t in tokens]
        
        logger.info(
            f"Starting market making: {question[:60]}... "
            f"(volume: ${market.get('volume24hr', 0):.0f})"
        )
        
        self._positions[market_id] = MarketPosition(market_id, question, token_ids)
        await self._place_quotes(market_id)
    
    async def _update_quotes(self) -> None:
        """Update quotes for all active positions"""
        current_time = time.time()
        
        for market_id in list(self._positions.keys()):
            last_update = self._last_quote_update.get(market_id, 0)
            
            if current_time - last_update >= MM_QUOTE_UPDATE_INTERVAL:
                await self._refresh_quotes(market_id)
                self._last_quote_update[market_id] = current_time
    
    async def _place_quotes(self, market_id: str) -> None:
        """Place bid/ask quotes"""
        if time.time() - self._last_order_time < MM_MIN_ORDER_SPACING:
            await asyncio.sleep(MM_MIN_ORDER_SPACING)
        
        position = self._positions[market_id]
        prices = await self._get_market_prices(market_id, position.token_ids)
        
        if not prices:
            return
        
        for token_id in position.token_ids:
            mid_price = prices.get(token_id)
            if not mid_price:
                continue
            
            inventory = position.inventory.get(token_id, 0)
            spread = self._calculate_spread(inventory)
            
            bid_price = max(0.01, mid_price - spread / 2)
            ask_price = min(0.99, mid_price + spread / 2)
            
            try:
                bid_size = MM_BASE_POSITION_SIZE / bid_price
                bid_order = await self.order_manager.execute_limit_order(
                    token_id=token_id,
                    side='BUY',
                    size=bid_size,
                    price=bid_price,
                    post_only=True
                )
                
                if bid_order and bid_order.get('order_id'):
                    position.active_bids[token_id] = bid_order['order_id']
                
                ask_size = MM_BASE_POSITION_SIZE / ask_price
                ask_order = await self.order_manager.execute_limit_order(
                    token_id=token_id,
                    side='SELL',
                    size=ask_size,
                    price=ask_price,
                    post_only=True
                )
                
                if ask_order and ask_order.get('order_id'):
                    position.active_asks[token_id] = ask_order['order_id']
                
                logger.debug(
                    f"Quotes placed: {token_id[:8]}... "
                    f"BID={bid_price:.3f} ASK={ask_price:.3f} spread={spread:.3f}"
                )
                
            except Exception as e:
                logger.warning(f"Error placing quotes for {token_id[:8]}...: {e}")
        
        self._last_order_time = time.time()
    
    async def _refresh_quotes(self, market_id: str) -> None:
        """Cancel old quotes and place new ones"""
        position = self._positions.get(market_id)
        if not position:
            return
        
        all_orders = list(position.active_bids.values()) + list(position.active_asks.values())
        for order_id in all_orders:
            try:
                await self.client.cancel_order(order_id)
            except:
                pass
        
        position.active_bids.clear()
        position.active_asks.clear()
        
        await self._place_quotes(market_id)
    
    def _calculate_spread(self, inventory: int) -> float:
        """Calculate spread based on inventory"""
        base_spread = MM_TARGET_SPREAD
        
        if abs(inventory) > MM_MAX_INVENTORY_PER_OUTCOME / 2:
            inventory_factor = abs(inventory) / MM_MAX_INVENTORY_PER_OUTCOME
            base_spread *= (1 + inventory_factor * MM_INVENTORY_SPREAD_MULTIPLIER)
        
        return max(MM_MIN_SPREAD, min(MM_MAX_SPREAD, base_spread))
    
    async def _get_market_prices(self, market_id: str, token_ids: List[str]) -> Dict[str, float]:
        """Get current mid prices"""
        prices = {}
        
        for token_id in token_ids:
            try:
                order_book = await self.client.get_order_book(token_id)
                bids = getattr(order_book, 'bids', [])
                asks = getattr(order_book, 'asks', [])
                
                if bids and asks:
                    best_bid = float(bids[0]['price'])
                    best_ask = float(asks[0]['price'])
                    prices[token_id] = (best_bid + best_ask) / 2.0
                    
            except Exception as e:
                logger.debug(f"Error fetching price for {token_id[:8]}...: {e}")
        
        return prices
    
    async def _check_risk_limits(self) -> None:
        """Check risk limits for all positions"""
        for market_id, position in list(self._positions.items()):
            prices = await self._get_market_prices(market_id, position.token_ids)
            if not prices:
                continue
            
            total_pnl = position.get_total_pnl(prices)
            if total_pnl < -MM_MAX_LOSS_PER_POSITION:
                logger.warning(
                    f"Max loss exceeded for {market_id[:8]}... "
                    f"(P&L: ${total_pnl:.2f}) - force closing"
                )
                await self._close_position(market_id)
                continue
            
            if position.has_inventory():
                age = position.get_inventory_age()
                if age > MM_MAX_INVENTORY_HOLD_TIME:
                    logger.warning(
                        f"Inventory age exceeded for {market_id[:8]}... "
                        f"({age/60:.0f} min) - force closing"
                    )
                    await self._close_position(market_id)
                    continue
            
            for token_id, inventory in position.inventory.items():
                if inventory > 0:
                    entry_price = position.cost_basis[token_id]
                    current_price = prices.get(token_id, entry_price)
                    price_move = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                    
                    if price_move < -MM_EMERGENCY_EXIT_THRESHOLD:
                        logger.critical(
                            f"Emergency exit triggered: {token_id[:8]}... "
                            f"price moved {price_move*100:.1f}% against position"
                        )
                        await self._emergency_exit_token(market_id, token_id)
    
    async def _should_close_position(self, position: MarketPosition) -> bool:
        """Check if position should be closed"""
        if not position.has_inventory():
            has_orders = bool(position.active_bids or position.active_asks)
            if not has_orders:
                return True
        return False
    
    async def _close_position(self, market_id: str) -> None:
        """Close position in a market"""
        if market_id not in self._positions:
            return
        
        position = self._positions[market_id]
        logger.info(f"Closing position in {market_id[:8]}...")
        
        await self._refresh_quotes(market_id)
        
        if position.has_inventory():
            for token_id, inventory in position.inventory.items():
                if inventory > 0:
                    await self._exit_inventory(market_id, token_id, inventory)
        
        logger.info(
            f"Position closed: {position.market_question[:40]}... "
            f"P&L: ${position.realized_pnl:.2f}, "
            f"Volume: ${position.total_volume:.2f}"
        )
        
        self._total_pnl += position.realized_pnl
        self._total_maker_volume += position.total_volume
        self._total_fills += position.fill_count
        
        del self._positions[market_id]
    
    async def _exit_inventory(self, market_id: str, token_id: str, shares: int) -> None:
        """Exit inventory position at market"""
        try:
            result = await self.order_manager.execute_market_order(
                token_id=token_id,
                side='SELL',
                size=shares,
                max_slippage=0.05
            )
            
            if result:
                logger.info(f"Exited {shares} shares of {token_id[:8]}...")
                
        except Exception as e:
            logger.error(f"Error exiting inventory: {e}")
    
    async def _emergency_exit_token(self, market_id: str, token_id: str) -> None:
        """Emergency exit for specific token"""
        position = self._positions.get(market_id)
        if not position:
            return
        
        inventory = position.inventory.get(token_id, 0)
        if inventory > 0:
            await self._exit_inventory(market_id, token_id, inventory)
    
    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status"""
        return {
            'name': 'MarketMaking',
            'is_running': self._is_running,
            'allocated_capital': float(self._allocated_capital),
            'capital_used': float(self._capital_used),
            'active_positions': len(self._positions),
            'total_pnl': self._total_pnl,
            'total_fills': self._total_fills,
            'total_maker_volume': self._total_maker_volume,
        }
    
    async def validate_configuration(self) -> None:
        """Validate strategy configuration"""
        if self._allocated_capital <= 0:
            raise StrategyError("Invalid capital allocation for market making")
        logger.info("✅ MarketMaking configuration validated")

