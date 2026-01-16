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
        gamma: Decimal = Decimal('0.2'),                     # Base risk aversion (0.1-0.5)
        volatility_lookback_seconds: int = 3600,             # 1 hour volatility window
        use_dynamic_gamma: bool = True                       # Enable volatility-adaptive gamma
    ):
        """
        Initialize inventory manager
        
        Args:
            max_position_per_market: Max notional value per market
            max_gross_exposure: Max total gross exposure across all markets
            gamma: Base risk aversion parameter (higher = more conservative)
            volatility_lookback_seconds: Window for volatility calculation
            use_dynamic_gamma: Enable volatility-adaptive gamma scaling
        """
        self._positions: Dict[str, Position] = {}  # token_id -> Position
        self._lock = asyncio.Lock()
        
        # Risk parameters
        self.max_position_per_market = max_position_per_market
        self.max_gross_exposure = max_gross_exposure
        self.gamma_base = gamma  # Base gamma (γ_base)
        self.volatility_lookback_seconds = volatility_lookback_seconds
        self.use_dynamic_gamma = use_dynamic_gamma
        
        # Price history for volatility calculation (token_id -> deque of (timestamp, price))
        self._price_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))
        
        # Volatility tracking for dynamic gamma
        # Baseline: 24-hour rolling average
        self._baseline_volatility: Dict[str, Decimal] = {}  # token_id -> σ_baseline
        self._baseline_vol_window_seconds = 24 * 3600  # 24 hours
        
        # Current: 1-minute EMA
        self._current_volatility: Dict[str, Decimal] = {}  # token_id -> σ_current
        self._current_vol_window_seconds = 60  # 1 minute
        self._last_vol_update: Dict[str, float] = {}  # token_id -> timestamp
        
        # Metrics
        self._trade_count = 0
        self._last_snapshot_time = 0.0
        
        logger.info(
            f"InventoryManager initialized - "
            f"Max per market: ${max_position_per_market}, "
            f"Max gross: ${max_gross_exposure}, "
            f"Base Gamma: {gamma}, "
            f"Dynamic Gamma: {'ENABLED' if use_dynamic_gamma else 'DISABLED'}"
        )
    
    def get_dynamic_gamma(self, token_id: str) -> Decimal:
        """
        Calculate volatility-adaptive gamma using 2026 institutional formula.
        
        Mathematical Formula (2026 Gold Standard):
        $$
        \\gamma_{dynamic} = \\gamma_{base} \\cdot \\left(1 + \\frac{\\sigma_{current}}{\\sigma_{baseline}}\\right)
        $$
        
        Where:
        - $\\gamma_{base}$: MM_GAMMA_BASE = 0.1 (aggressive fills in low vol)
        - $\\gamma_{max}$: MM_GAMMA_MAX = 0.5 (defensive cap in extreme vol)
        - $\\sigma_{current}$: 1-minute EMA volatility (recent market stress)
        - $\\sigma_{baseline}$: 24-hour rolling average volatility (normal regime)
        
        2026 Institutional Logic:
        ------------------------
        - Low volatility (σ_current < σ_baseline): γ → 0.1 (aggressive fills)
        - Normal volatility (σ_current = σ_baseline): γ = 0.2 (balanced)
        - High volatility (σ_current = 2× σ_baseline): γ = 0.3 (defensive)
        - Extreme volatility: γ capped at MM_GAMMA_MAX = 0.5
        
        Example (2026 Gold Standard):
        ----------------------------
        γ_base = 0.1, γ_max = 0.5, σ_baseline = 0.05, σ_current = 0.10
        γ_dynamic = 0.1 × (1 + 0.10/0.05) = 0.1 × 3 = 0.3 (capped at 0.5)
        
        This provides wider spreads during volatile markets while maintaining
        competitive fills during normal conditions.
        
        Args:
            token_id: Token to calculate dynamic gamma for
            
        Returns:
            Volatility-adjusted gamma parameter (capped at MM_GAMMA_MAX)
            
        Raises:
            None: Falls back to gamma_base if insufficient data
        """
        from config.constants import MM_GAMMA_BASE, MM_GAMMA_MAX
        
        if not self.use_dynamic_gamma:
            return Decimal(str(MM_GAMMA_BASE))
        
        # Get current and baseline volatility
        sigma_current = self._current_volatility.get(token_id)
        sigma_baseline = self._baseline_volatility.get(token_id)
        
        # Fall back to base gamma if insufficient data
        if sigma_current is None or sigma_baseline is None or sigma_baseline == 0:
            logger.debug(
                f"[{token_id[:8]}] Insufficient volatility data - using base gamma {MM_GAMMA_BASE}"
            )
            return Decimal(str(MM_GAMMA_BASE))
        
        # Calculate dynamic gamma: γ_dynamic = γ_base * (1 + σ_current / σ_baseline)
        gamma_base_decimal = Decimal(str(MM_GAMMA_BASE))
        gamma_max_decimal = Decimal(str(MM_GAMMA_MAX))
        
        vol_ratio = sigma_current / sigma_baseline
        gamma_dynamic = gamma_base_decimal * (Decimal('1') + vol_ratio)
        
        # Cap gamma at MM_GAMMA_MAX (2026 institutional standard)
        gamma_dynamic = min(gamma_dynamic, gamma_max_decimal)
        
        logger.debug(
            f"[{token_id[:8]}] Dynamic gamma: {gamma_dynamic:.4f} "
            f"(base: {MM_GAMMA_BASE:.4f}, max: {MM_GAMMA_MAX:.4f}, "
            f"σ_current: {sigma_current:.4f}, σ_baseline: {sigma_baseline:.4f}, "
            f"ratio: {vol_ratio:.2f}x)"
        )
        
        return gamma_dynamic
    
    async def update_volatility(self, token_id: str, price: Decimal) -> None:
        """
        Update volatility estimates for dynamic gamma calculation.
        
        Updates two metrics:
        1. Baseline volatility: 24-hour rolling average (σ_baseline)
        2. Current volatility: 1-minute EMA (σ_current)
        
        Mathematical Formula (Log Returns):
        $$
        r_t = \\ln\\left(\\frac{P_t}{P_{t-1}}\\right)
        $$
        
        $$
        \\sigma = \\sqrt{\\frac{1}{N}\\sum_{i=1}^{N}(r_i - \\bar{r})^2} \\cdot \\sqrt{T}
        $$
        
        Where:
        - $r_t$: Log return at time t
        - $\\sigma$: Annualized volatility
        - $T$: Annualization factor (minutes per year = 525,600)
        
        Args:
            token_id: Token to update volatility for
            price: Current market price
            
        Returns:
            None
        """
        now = time.time()
        
        # Throttle updates to once per second
        last_update = self._last_vol_update.get(token_id, 0)
        if now - last_update < 1.0:
            return
        
        self._last_vol_update[token_id] = now
        
        # Update baseline volatility (24-hour window)
        baseline_vol = self._calculate_volatility_window(
            token_id,
            window_seconds=self._baseline_vol_window_seconds
        )
        if baseline_vol is not None:
            self._baseline_volatility[token_id] = baseline_vol
        
        # Update current volatility (1-minute window)
        current_vol = self._calculate_volatility_window(
            token_id,
            window_seconds=self._current_vol_window_seconds
        )
        if current_vol is not None:
            self._current_volatility[token_id] = current_vol
    
    def _calculate_volatility_window(
        self,
        token_id: str,
        window_seconds: int
    ) -> Optional[Decimal]:
        """
        Calculate volatility for a specific time window.
        
        Args:
            token_id: Token to calculate volatility for
            window_seconds: Time window in seconds
            
        Returns:
            Annualized volatility or None if insufficient data
        """
        history = self._price_history.get(token_id)
        if not history or len(history) < 10:
            return None
        
        # Filter to window
        cutoff_time = time.time() - window_seconds
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
        minutes_per_year = 365 * 24 * 60
        volatility_annual = volatility * Decimal(str(minutes_per_year)).sqrt()
        
        return volatility_annual
    
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
            
            # Update volatility estimates for dynamic gamma
            await self.update_volatility(token_id, price)
            
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
        Calculate Avellaneda-Stoikov inventory skew penalty with dynamic gamma.
        
        Mathematical Formula:
        $$
        \\text{skew} = \\gamma_{dynamic} \\cdot q \\cdot \\sigma^2 \\cdot T \\cdot p_{mid}
        $$
        
        Where:
        - $\\gamma_{dynamic}$: Volatility-adaptive risk aversion (from get_dynamic_gamma)
        - $q$: Inventory position in shares (positive = long, negative = short)
        - $\\sigma^2$: Market volatility squared (variance)
        - $T$: Time to expiry, normalized ∈ [0, 1]
        - $p_{mid}$: Current mid price (for price unit conversion)
        
        Institutional Logic:
        -------------------
        The skew adjustment shifts the reservation price away from mid:
        - If LONG inventory (q > 0): skew > 0 → reservation price LOWER → incentivize selling
        - If SHORT inventory (q < 0): skew < 0 → reservation price HIGHER → incentivize buying
        
        The dynamic gamma amplifies this effect during high volatility regimes,
        causing more aggressive inventory flattening when markets are stressed.
        
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
        
        # Get dynamic gamma (volatility-adaptive)
        gamma = self.get_dynamic_gamma(token_id)
        
        # Calculate volatility (sigma)
        volatility = self._calculate_volatility(token_id)
        if volatility is None:
            # Default to 5% volatility if no history
            volatility = Decimal('0.05')
        
        # Normalize time to expiry (0-1 scale)
        if time_to_expiry_hours is None:
            time_to_expiry_hours = 24.0  # Default 24 hours
        
        T = Decimal(str(min(time_to_expiry_hours / 24.0, 1.0)))  # Normalize to [0, 1]
        
        # Calculate skew: γ_dynamic × inventory × σ² × T
        inventory = position.shares
        sigma_squared = volatility * volatility
        
        skew = gamma * inventory * sigma_squared * T
        
        # Scale by mid price (convert to price units)
        skew_in_price = skew * mid_price
        
        logger.debug(
            f"[{token_id[:8]}] Inventory skew: {skew_in_price:.6f} "
            f"(inventory={inventory}, γ_dynamic={gamma:.4f}, vol={volatility:.4f}, T={T:.2f})"
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
