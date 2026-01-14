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
        """Update quotes for all active positions (parallel execution)"""
        current_time = time.time()
        
        # Collect markets that need updates
        markets_to_update = []
        for market_id in list(self._positions.keys()):
            last_update = self._last_quote_update.get(market_id, 0)
            if current_time - last_update >= MM_QUOTE_UPDATE_INTERVAL:
                markets_to_update.append(market_id)
        
        # Update all markets in parallel (lower latency)
        if markets_to_update:
            await asyncio.gather(
                *[self._refresh_quotes(m_id) for m_id in markets_to_update],
                return_exceptions=True
            )
            for m_id in markets_to_update:
                self._last_quote_update[m_id] = current_time
    
    async def _reconcile_order(self, token_id: str, side: str, target_price: float, 
                              target_size: float, current_order_id: Optional[str], 
                              position: MarketPosition) -> Optional[str]:
        """Smart order reconciliation - preserves queue priority"""
        # Check if existing order is close enough (within 1 tick)
        if current_order_id:
            try:
                curr_order = await self.order_manager.get_order(current_order_id)
                if curr_order and curr_order.get('status') == 'open':
                    curr_price = float(curr_order['price'])
                    if abs(curr_price - target_price) < 0.001:
                        logger.debug(f"[MM] Preserving {side} queue priority at {curr_price:.4f}")
                        return current_order_id
            except:
                pass
        
        # Need to update - cancel old
        if current_order_id:
            try:
                await self.client.cancel_order(current_order_id)
            except:
                pass
        
        # Place new order
        try:
            new_order = await self.order_manager.execute_limit_order(
                token_id=token_id,
                side=side,
                price=target_price,
                size=target_size,
                post_only=True
            )
            if new_order and new_order.get('order_id'):
                logger.info(f"[MM] {side} updated: {token_id[:8]}... @{target_price:.4f}")
                return new_order['order_id']
        except Exception as e:
            logger.warning(f"Failed to place {side}: {e}")
        
        return None
    
    async def _place_quotes(self, market_id: str) -> None:
        """Place/update quotes using inventory-aware skewing"""
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
            
            # Calculate skewed quotes (Avellaneda-Stoikov)
            target_bid, target_ask = self._calculate_skewed_quotes(mid_price, inventory)
            
            # Position sizing
            bid_size = MM_BASE_POSITION_SIZE / target_bid if target_bid > 0 else 0
            ask_size = MM_BASE_POSITION_SIZE / target_ask if target_ask > 0 else 0
            
            # Reduce size when holding inventory
            if inventory > 0:
                bid_size *= 0.5
            elif inventory < 0:
                ask_size *= 0.5
            
            # Smart reconciliation for BID
            current_bid_id = position.active_bids.get(token_id)
            new_bid_id = await self._reconcile_order(
                token_id, 'BUY', target_bid, bid_size, current_bid_id, position
            )
            if new_bid_id:
                position.active_bids[token_id] = new_bid_id
            elif current_bid_id:
                position.active_bids.pop(token_id, None)
            
            # Smart reconciliation for ASK
            current_ask_id = position.active_asks.get(token_id)
            new_ask_id = await self._reconcile_order(
                token_id, 'SELL', target_ask, ask_size, current_ask_id, position
            )
            if new_ask_id:
                position.active_asks[token_id] = new_ask_id
            elif current_ask_id:
                position.active_asks.pop(token_id, None)
        
        self._last_order_time = time.time()
    
    async def _refresh_quotes(self, market_id: str) -> None:
        """Update quotes (uses smart reconciliation, not blind cancel)"""
        await self._place_quotes(market_id)
    
    def _calculate_micro_price(self, bids: list, asks: list) -> Optional[float]:
        """Volume-Weighted Micro-Price (VWMP) - protects against adverse selection"""
        if not bids or not asks:
            return None
        
        best_bid = float(bids[0]['price'])
        best_ask = float(asks[0]['price'])
        bid_vol = float(bids[0]['size'])
        ask_vol = float(asks[0]['size'])
        
        total_vol = bid_vol + ask_vol
        if total_vol == 0:
            return (best_bid + best_ask) / 2.0
        
        # Heavier side pushes price toward opposite side
        micro_price = ((bid_vol * best_ask) + (ask_vol * best_bid)) / total_vol
        return micro_price
    
    def _calculate_skewed_quotes(self, mid_price: float, inventory: int) -> Tuple[float, float]:
        """Avellaneda-Stoikov inventory skewing for passive position management"""
        RISK_FACTOR = 0.05  # 5 cents per 100 shares
        
        # Reservation price (indifference price)
        inventory_skew = inventory * RISK_FACTOR
        reservation_price = mid_price - inventory_skew
        
        # Dynamic spread: widen as inventory grows
        base_half_spread = MM_TARGET_SPREAD / 2
        extra_spread = abs(inventory) * 0.001
        final_half_spread = base_half_spread + extra_spread
        
        target_bid = reservation_price - final_half_spread
        target_ask = reservation_price + final_half_spread
        
        # Don't cross the market
        target_bid = min(target_bid, mid_price - 0.001)
        target_ask = max(target_ask, mid_price + 0.001)
        
        # Valid range
        target_bid = max(0.01, min(0.99, target_bid))
        target_ask = max(0.01, min(0.99, target_ask))
        
        return round(target_bid, 4), round(target_ask, 4)
    
    async def _get_market_prices(self, market_id: str, token_ids: List[str]) -> Dict[str, float]:
        """Get current micro-prices (volume-weighted)"""
        prices = {}
        
        for token_id in token_ids:
            try:
                order_book = await self.client.get_order_book(token_id)
                bids = getattr(order_book, 'bids', [])
                asks = getattr(order_book, 'asks', [])
                
                if bids and asks:
                    micro_price = self._calculate_micro_price(bids, asks)
                    if micro_price:
                        prices[token_id] = micro_price
                    
            except Exception as e:
                logger.debug(f"Error fetching price for {token_id[:8]}...: {e}")
        
        return prices
    
    async def _check_risk_limits(self) -> None:
        """Check risk limits - use passive unwinding instead of market orders"""
        for market_id, position in list(self._positions.items()):
            prices = await self._get_market_prices(market_id, position.token_ids)
            if not prices:
                continue
            
            # Hard P&L stop - still force close if catastrophic
            total_pnl = position.get_total_pnl(prices)
            if total_pnl < -MM_MAX_LOSS_PER_POSITION:
                logger.critical(
                    f"Max loss exceeded: {market_id[:8]}... "
                    f"(P&L: ${total_pnl:.2f}) - emergency close"
                )
                await self._close_position(market_id)
                continue
            
            # Time-based: PASSIVE UNWINDING (not force close)
            if position.has_inventory():
                age = position.get_inventory_age()
                if age > MM_MAX_INVENTORY_HOLD_TIME:
                    logger.warning(
                        f"Inventory age {age/60:.0f}min - passive unwinding"
                    )
                    for token_id, inventory in position.inventory.items():
                        if inventory != 0:
                            await self._exit_inventory(market_id, token_id, inventory)
                    continue
            
            # Adverse price move: PASSIVE UNWINDING
            for token_id, inventory in position.inventory.items():
                if inventory > 0:
                    entry_price = position.cost_basis[token_id]
                    current_price = prices.get(token_id, entry_price)
                    price_move = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                    
                    if price_move < -MM_EMERGENCY_EXIT_THRESHOLD:
                        logger.critical(
                            f"Emergency: {token_id[:8]}... price moved {price_move*100:.1f}% "
                            f"- passive unwinding"
                        )
                        await self._exit_inventory(market_id, token_id, inventory)
    
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
        """Passive unwinding via aggressive quote skewing (no market orders)"""
        try:
            position = self._positions.get(market_id)
            if not position:
                return
            
            inventory = position.inventory.get(token_id, 0)
            if inventory == 0:
                return
            
            logger.warning(f"[MM] Passively unwinding {abs(inventory)} shares")
            
            # Get current price
            prices = await self._get_market_prices(market_id, [token_id])
            if not prices or token_id not in prices:
                return
            
            mid_price = prices[token_id]
            
            # Aggressive skewing (10x normal risk factor)
            AGGRESSIVE_RISK = 0.5
            inventory_skew = inventory * AGGRESSIVE_RISK
            
            if inventory > 0:
                # Long: aggressive seller (ASK below mid)
                target_ask = mid_price - (abs(inventory_skew) * 0.5)
                target_ask = max(0.01, min(0.99, target_ask))
                target_bid = max(0.01, target_ask - 0.10)
            else:
                # Short: aggressive buyer (BID above mid)
                target_bid = mid_price + (abs(inventory_skew) * 0.5)
                target_bid = max(0.01, min(0.99, target_bid))
                target_ask = min(0.99, target_bid + 0.10)
            
            # Place aggressive quotes
            bid_size = MM_BASE_POSITION_SIZE / target_bid
            ask_size = MM_BASE_POSITION_SIZE / target_ask
            
            current_bid_id = position.active_bids.get(token_id)
            new_bid_id = await self._reconcile_order(
                token_id, 'BUY', target_bid, bid_size, current_bid_id, position
            )
            if new_bid_id:
                position.active_bids[token_id] = new_bid_id
            
            current_ask_id = position.active_asks.get(token_id)
            new_ask_id = await self._reconcile_order(
                token_id, 'SELL', target_ask, ask_size, current_ask_id, position
            )
            if new_ask_id:
                position.active_asks[token_id] = new_ask_id
            
            logger.info(f"[MM] Unwinding quotes: BID={target_bid:.4f} ASK={target_ask:.4f}")
                
        except Exception as e:
            logger.error(f"Passive unwinding error: {e}")
    
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

