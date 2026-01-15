"""
Inventory Manager - Real-Time Position Delta Tracking

Tracks inventory across all markets and calculates risk-adjusted skew penalties
for the Avellaneda-Stoikov market making model.

Key Responsibilities:
- Real-time position tracking (delta per token)
- Inventory risk calculation (sigma^2 * position * time_to_expiry)
- Skew penalty computation for reservation price adjustment
- Position limit enforcement (per-market and global)

Mathematical Model:
==================
Inventory Skew = γ × inventory × σ² × T
Where:
- γ (gamma): Risk aversion parameter (0.1 = aggressive, 0.5 = conservative)
- inventory: Current position in shares (can be negative for shorts)
- σ² (sigma squared): Realized volatility (rolling 1-hour std dev)
- T: Time remaining until market expiry (normalized 0-1)

Reservation Price Adjustment:
=============================
reservation_price = mid_price - inventory_skew
bid = reservation_price - spread/2
ask = reservation_price + spread/2

This creates asymmetric quoting:
- If long inventory: Bid lower, Ask lower (incentivize selling)
- If short inventory: Bid higher, Ask higher (incentivize buying)

Author: Institutional HFT Team
Date: January 2026
"""

from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, field
from decimal import Decimal
from collections import defaultdict, deque
import time
import asyncio
from datetime import datetime, timedelta

from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Position:
    """Single position in a market"""
    token_id: str
    market_id: str
    shares: Decimal  # Positive = long, Negative = short
    avg_entry_price: Decimal
    realized_pnl: Decimal = Decimal('0')
    unrealized_pnl: Decimal = Decimal('0')
    last_update_time: float = field(default_factory=time.time)
    
    @property
    def notional_value(self) -> Decimal:
        """Calculate notional value of position"""
        return abs(self.shares * self.avg_entry_price)
    
    @property
    def is_flat(self) -> bool:
        """Check if position is flat (no inventory)"""
        return abs(self.shares) < Decimal('0.01')  # 0.01 share tolerance
    
    def update_pnl(self, current_price: Decimal) -> None:
        """Update unrealized P&L based on current market price"""
        if self.is_flat:
            self.unrealized_pnl = Decimal('0')
            return
        
        # P&L = (current_price - avg_entry) * shares
        self.unrealized_pnl = (current_price - self.avg_entry_price) * self.shares
        self.last_update_time = time.time()
    
    def add_trade(self, side: str, shares: Decimal, price: Decimal) -> None:
        """
        Update position from new trade
        
        Args:
            side: 'BUY' or 'SELL'
            shares: Number of shares traded
            price: Execution price
        """
        if side == 'BUY':
            # Increase long position
            new_total_shares = self.shares + shares
            
            # Recalculate average entry price (weighted average)
            if new_total_shares != 0:
                total_cost = (self.avg_entry_price * self.shares) + (price * shares)
                self.avg_entry_price = total_cost / new_total_shares
            
            self.shares = new_total_shares
            
        elif side == 'SELL':
            # Decrease long position (or increase short)
            old_shares = self.shares
            self.shares -= shares
            
            # If closing/reducing position, realize P&L
            if abs(self.shares) < abs(old_shares):
                shares_closed = min(shares, abs(old_shares))
                pnl_per_share = price - self.avg_entry_price
                self.realized_pnl += pnl_per_share * shares_closed
            
            # If flipping from long to short, reset avg entry
            if old_shares > 0 and self.shares < 0:
                self.avg_entry_price = price
        
        self.last_update_time = time.time()


@dataclass
class InventorySnapshot:
    """Snapshot of current inventory state"""
    timestamp: float
    positions: Dict[str, Position]
    total_notional: Decimal
    gross_exposure: Decimal  # Sum of abs(position values)
    net_exposure: Decimal    # Sum of position values (accounting for longs/shorts)
    total_realized_pnl: Decimal
    total_unrealized_pnl: Decimal
    position_count: int
    
    @property
    def total_pnl(self) -> Decimal:
        """Total P&L (realized + unrealized)"""
        return self.total_realized_pnl + self.total_unrealized_pnl


class InventoryManager:
    """
    Real-Time Inventory Manager for HFT Market Making
    
    Thread-safe position tracking with:
    - Atomic position updates
    - Real-time P&L calculation
    - Inventory risk scoring
    - Position limit enforcement
    """
    
    def __init__(
        self,
        max_position_per_market: Decimal = Decimal('5000'),  # $5k per market
        max_gross_exposure: Decimal = Decimal('50000'),      # $50k total
        gamma: Decimal = Decimal('0.2'),                     # Risk aversion (0.1-0.5)
        volatility_lookback_seconds: int = 3600              # 1 hour volatility window
    ):
        """
        Initialize inventory manager
        
        Args:
            max_position_per_market: Max notional value per market
            max_gross_exposure: Max total gross exposure across all markets
            gamma: Risk aversion parameter (higher = more conservative)
            volatility_lookback_seconds: Window for volatility calculation
        """
        self._positions: Dict[str, Position] = {}  # token_id -> Position
        self._lock = asyncio.Lock()
        
        # Risk parameters
        self.max_position_per_market = max_position_per_market
        self.max_gross_exposure = max_gross_exposure
        self.gamma = gamma
        self.volatility_lookback_seconds = volatility_lookback_seconds
        
        # Price history for volatility calculation (token_id -> deque of (timestamp, price))
        self._price_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        
        # Metrics
        self._trade_count = 0
        self._last_snapshot_time = 0.0
        
        logger.info(
            f"InventoryManager initialized - "
            f"Max per market: ${max_position_per_market}, "
            f"Max gross: ${max_gross_exposure}, "
            f"Gamma: {gamma}"
        )
    
    async def record_trade(
        self,
        token_id: str,
        market_id: str,
        side: str,
        shares: Decimal,
        price: Decimal
    ) -> None:
        """
        Record a trade and update position
        
        Args:
            token_id: CLOB token ID
            market_id: Market ID
            side: 'BUY' or 'SELL'
            shares: Number of shares
            price: Execution price
        """
        async with self._lock:
            # Get or create position
            if token_id not in self._positions:
                self._positions[token_id] = Position(
                    token_id=token_id,
                    market_id=market_id,
                    shares=Decimal('0'),
                    avg_entry_price=price
                )
            
            position = self._positions[token_id]
            position.add_trade(side, shares, price)
            
            self._trade_count += 1
            
            # Record price for volatility calculation
            self._price_history[token_id].append((time.time(), price))
            
            logger.debug(
                f"[{token_id[:8]}] {side} {shares} @ ${price:.4f} - "
                f"Position: {position.shares} shares, "
                f"Unrealized P&L: ${position.unrealized_pnl:.2f}"
            )
    
    async def update_positions_from_fills(self, fills: List[Dict]) -> None:
        """
        Batch update positions from WebSocket fill events
        
        Args:
            fills: List of fill events from /user channel
        """
        for fill in fills:
            try:
                await self.record_trade(
                    token_id=fill.get('asset_id'),
                    market_id=fill.get('market_id', 'unknown'),
                    side=fill.get('side'),
                    shares=Decimal(str(fill.get('size', 0))),
                    price=Decimal(str(fill.get('price', 0)))
                )
            except Exception as e:
                logger.error(f"Error processing fill: {e}")
    
    async def update_mark_prices(self, prices: Dict[str, Decimal]) -> None:
        """
        Update unrealized P&L for all positions
        
        Args:
            prices: Dict of token_id -> current_mid_price
        """
        async with self._lock:
            for token_id, position in self._positions.items():
                if token_id in prices:
                    position.update_pnl(prices[token_id])
                    
                    # Update price history for volatility
                    self._price_history[token_id].append((time.time(), prices[token_id]))
    
    def get_position(self, token_id: str) -> Optional[Position]:
        """Get position for specific token (non-blocking read)"""
        return self._positions.get(token_id)
    
    def get_all_positions(self) -> Dict[str, Position]:
        """Get all current positions"""
        return self._positions.copy()
    
    async def get_snapshot(self) -> InventorySnapshot:
        """Get complete inventory snapshot"""
        async with self._lock:
            total_notional = Decimal('0')
            gross_exposure = Decimal('0')
            net_exposure = Decimal('0')
            total_realized_pnl = Decimal('0')
            total_unrealized_pnl = Decimal('0')
            
            for position in self._positions.values():
                notional = position.notional_value
                total_notional += notional
                gross_exposure += abs(position.shares * position.avg_entry_price)
                net_exposure += position.shares * position.avg_entry_price
                total_realized_pnl += position.realized_pnl
                total_unrealized_pnl += position.unrealized_pnl
            
            self._last_snapshot_time = time.time()
            
            return InventorySnapshot(
                timestamp=self._last_snapshot_time,
                positions=self._positions.copy(),
                total_notional=total_notional,
                gross_exposure=gross_exposure,
                net_exposure=net_exposure,
                total_realized_pnl=total_realized_pnl,
                total_unrealized_pnl=total_unrealized_pnl,
                position_count=len([p for p in self._positions.values() if not p.is_flat])
            )
    
    def calculate_inventory_skew(
        self,
        token_id: str,
        mid_price: Decimal,
        time_to_expiry_hours: Optional[float] = None
    ) -> Decimal:
        """
        Calculate Avellaneda-Stoikov inventory skew penalty
        
        Formula: skew = γ × inventory × σ² × T
        
        Args:
            token_id: Token to calculate skew for
            mid_price: Current mid price
            time_to_expiry_hours: Hours until market expiry (default: 24h)
            
        Returns:
            Skew adjustment in price units (can be negative)
        """
        position = self.get_position(token_id)
        if not position or position.is_flat:
            return Decimal('0')
        
        # Calculate volatility (sigma)
        volatility = self._calculate_volatility(token_id)
        if volatility is None:
            # Default to 5% volatility if no history
            volatility = Decimal('0.05')
        
        # Normalize time to expiry (0-1 scale)
        if time_to_expiry_hours is None:
            time_to_expiry_hours = 24.0  # Default 24 hours
        
        T = Decimal(str(min(time_to_expiry_hours / 24.0, 1.0)))  # Normalize to [0, 1]
        
        # Calculate skew: γ × inventory × σ² × T
        inventory = position.shares
        sigma_squared = volatility * volatility
        
        skew = self.gamma * inventory * sigma_squared * T
        
        # Scale by mid price (convert to price units)
        skew_in_price = skew * mid_price
        
        logger.debug(
            f"[{token_id[:8]}] Inventory skew: {skew_in_price:.6f} "
            f"(inventory={inventory}, vol={volatility:.4f}, T={T:.2f})"
        )
        
        return skew_in_price
    
    def _calculate_volatility(self, token_id: str) -> Optional[Decimal]:
        """
        Calculate realized volatility from price history
        
        Uses rolling 1-hour window with log returns:
        σ = sqrt(sum((ln(p_t / p_t-1))^2) / N)
        
        Args:
            token_id: Token to calculate volatility for
            
        Returns:
            Annualized volatility or None if insufficient data
        """
        history = self._price_history.get(token_id)
        if not history or len(history) < 10:
            return None
        
        # Filter to lookback window
        cutoff_time = time.time() - self.volatility_lookback_seconds
        recent_prices = [
            (ts, price) for ts, price in history
            if ts >= cutoff_time
        ]
        
        if len(recent_prices) < 10:
            return None
        
        # Calculate log returns
        log_returns = []
        for i in range(1, len(recent_prices)):
            p_prev = recent_prices[i-1][1]
            p_curr = recent_prices[i][1]
            
            if p_prev > 0 and p_curr > 0:
                log_return = (p_curr / p_prev).ln()
                log_returns.append(log_return)
        
        if not log_returns:
            return None
        
        # Calculate standard deviation
        mean_return = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_return) ** 2 for r in log_returns) / len(log_returns)
        volatility = variance.sqrt()
        
        # Annualize (assuming 1-minute sampling)
        # vol_annual = vol_per_sample * sqrt(samples_per_year)
        minutes_per_year = 365 * 24 * 60
        volatility_annual = volatility * Decimal(str(minutes_per_year)).sqrt()
        
        return volatility_annual
    
    def check_position_limits(
        self,
        token_id: str,
        proposed_trade_shares: Decimal,
        proposed_trade_price: Decimal
    ) -> Tuple[bool, str]:
        """
        Check if proposed trade would violate position limits
        
        Args:
            token_id: Token to trade
            proposed_trade_shares: Shares to add (positive) or remove (negative)
            proposed_trade_price: Expected execution price
            
        Returns:
            (is_allowed, reason)
        """
        position = self.get_position(token_id)
        current_shares = position.shares if position else Decimal('0')
        
        # Calculate new position
        new_shares = current_shares + proposed_trade_shares
        new_notional = abs(new_shares * proposed_trade_price)
        
        # Check per-market limit
        if new_notional > self.max_position_per_market:
            return (
                False,
                f"Per-market limit exceeded: ${new_notional:.2f} > ${self.max_position_per_market:.2f}"
            )
        
        # Check gross exposure limit
        current_gross = sum(
            abs(p.shares * p.avg_entry_price)
            for p in self._positions.values()
        )
        
        trade_notional = abs(proposed_trade_shares * proposed_trade_price)
        new_gross = current_gross + trade_notional
        
        if new_gross > self.max_gross_exposure:
            return (
                False,
                f"Gross exposure limit exceeded: ${new_gross:.2f} > ${self.max_gross_exposure:.2f}"
            )
        
        return (True, "")
    
    async def flatten_position(self, token_id: str, current_price: Decimal) -> Optional[Dict]:
        """
        Flatten position (emergency liquidation)
        
        Args:
            token_id: Token to flatten
            current_price: Current market price
            
        Returns:
            Trade details or None
        """
        position = self.get_position(token_id)
        if not position or position.is_flat:
            return None
        
        # Calculate liquidation trade
        side = 'SELL' if position.shares > 0 else 'BUY'
        shares = abs(position.shares)
        
        logger.warning(
            f"[{token_id[:8]}] FLATTENING POSITION: {side} {shares} shares @ ${current_price:.4f}"
        )
        
        # Return trade details for execution
        return {
            'token_id': token_id,
            'side': side,
            'shares': shares,
            'price': current_price,
            'reason': 'emergency_flatten'
        }
    
    async def flatten_all_positions(self, prices: Dict[str, Decimal]) -> List[Dict]:
        """
        Flatten all positions (emergency stop)
        
        Args:
            prices: Current market prices for all tokens
            
        Returns:
            List of trade details
        """
        async with self._lock:
            trades = []
            
            for token_id, position in self._positions.items():
                if position.is_flat:
                    continue
                
                current_price = prices.get(token_id)
                if not current_price:
                    logger.error(f"No price available for {token_id[:8]} - cannot flatten")
                    continue
                
                trade = await self.flatten_position(token_id, current_price)
                if trade:
                    trades.append(trade)
            
            logger.warning(f"FLATTEN ALL: {len(trades)} positions queued for liquidation")
            return trades
    
    def get_stats(self) -> Dict:
        """Get inventory statistics"""
        snapshot = asyncio.run(self.get_snapshot())
        
        return {
            'position_count': snapshot.position_count,
            'total_notional': float(snapshot.total_notional),
            'gross_exposure': float(snapshot.gross_exposure),
            'net_exposure': float(snapshot.net_exposure),
            'realized_pnl': float(snapshot.total_realized_pnl),
            'unrealized_pnl': float(snapshot.total_unrealized_pnl),
            'total_pnl': float(snapshot.total_pnl),
            'trade_count': self._trade_count,
            'utilization_pct': float(
                (snapshot.gross_exposure / self.max_gross_exposure) * 100
            ) if self.max_gross_exposure > 0 else 0
        }
