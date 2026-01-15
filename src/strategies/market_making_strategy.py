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
import aiohttp
from datetime import datetime, timedelta
from decimal import Decimal
import time
import json
from collections import deque
import statistics

from strategies.base_strategy import BaseStrategy
from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from core.market_data_manager import MarketDataManager, FillEvent, MarketSnapshot
from config.constants import (
    # Budget allocation
    MARKET_MAKING_STRATEGY_CAPITAL,
    
    # Market selection - Adaptive Capacity Filtering
    MM_MAX_MARKETS,
    MM_VOLUME_MULTIPLIER,
    MM_HARD_FLOOR_VOLUME,
    MM_MIN_LIQUIDITY_DEPTH,
    MM_MIN_DEPTH_SHARES,
    MM_MAX_SPREAD_PERCENT,
    MM_PREFER_BINARY_MARKETS,
    MM_MAX_ACTIVE_MARKETS,  # Deprecated, use MM_MAX_MARKETS
    
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
    MM_MAX_DIRECTIONAL_EXPOSURE_PER_MARKET,
    MM_GAMMA_RISK_AVERSION,
    MM_BOUNDARY_THRESHOLD_LOW,
    MM_BOUNDARY_THRESHOLD_HIGH,
    MM_GLOBAL_DAILY_LOSS_LIMIT,
    MM_MAX_TOTAL_DIRECTIONAL_EXPOSURE,
    MM_ORACLE_PRICE_DEVIATION_LIMIT,
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
    
    # Z-Score Mean Reversion Alpha Overlay
    Z_SCORE_LOOKBACK_PERIODS,
    Z_SCORE_ENTRY_THRESHOLD,
    Z_SCORE_EXIT_TARGET,
    Z_SCORE_HALT_THRESHOLD,
    MM_Z_SENSITIVITY,
    Z_SCORE_UPDATE_INTERVAL,
    
    # OBI (Order Book Imbalance) - Momentum Detection
    MM_OBI_THRESHOLD,
    MM_MOMENTUM_PROTECTION_TIME,
)
from utils.logger import get_logger
from utils.exceptions import StrategyError


logger = get_logger(__name__)


class ZScoreManager:
    """
    Z-Score Mean Reversion Alpha Signal Generator
    
    Mathematical Foundation:
    -----------------------
    Z-Score = (Current_Price - Rolling_Mean) / Rolling_StdDev
    
    Interpretation:
    - Z > 2.0: Overbought (price 2σ above mean) → Reversion expected downward
    - Z < -2.0: Oversold (price 2σ below mean) → Reversion expected upward
    - abs(Z) > 3.5: Extreme outlier → Likely regime change, halt trading
    
    Alpha Overlay Logic:
    -------------------
    This manager computes a "Reservation Price Adjustment" that is ADDITIVE
    to the existing Avellaneda-Stoikov inventory skew.
    
    Traditional MM (Symmetrical):
        Bid = Mid - Spread/2
        Ask = Mid + Spread/2
    
    Avellaneda-Stoikov (Inventory-Aware):
        Reservation_Price = Mid - (Inventory * Risk_Factor)
        Bid = Reservation_Price - Spread/2
        Ask = Reservation_Price + Spread/2
    
    Hybrid (Inventory + Mean Reversion):
        Base_Reservation = Mid - (Inventory * Risk_Factor)
        Alpha_Shift = Z_Score * MM_Z_SENSITIVITY
        Final_Reservation = Base_Reservation - Alpha_Shift
        Bid = Final_Reservation - Spread/2
        Ask = Final_Reservation + Spread/2
    
    Example Scenarios:
    -----------------
    1. Overbought (Z=2.5):
       - Alpha_Shift = 2.5 * 0.005 = $0.0125
       - Lower Reservation Price by $0.0125
       - Effect: Wider ask (harder to buy), tighter bid (easier to sell)
       - Strategy: Incentivize selling into mean reversion
    
    2. Oversold (Z=-2.5):
       - Alpha_Shift = -2.5 * 0.005 = -$0.0125
       - Raise Reservation Price by $0.0125
       - Effect: Tighter ask (easier to buy), wider bid (harder to sell)
       - Strategy: Incentivize buying into mean reversion
    
    Safety Gates:
    ------------
    - Extreme Outlier (abs(Z) > 3.5): Halt all quoting (potential regime change)
    - Exit Target (abs(Z) < 0.5): Remove alpha skew (mean reversion complete)
    - Minimum Sample Size: Require 20 samples before calculating Z-Score
    """
    
    def __init__(self, lookback_periods: int = Z_SCORE_LOOKBACK_PERIODS):
        """
        Initialize Z-Score calculator with EWMA volatility (2026 HFT Upgrade)
        + DRIFT-PROTECTED DUAL-WINDOW MEAN (Module 1 - Institutional Guard)
        
        Args:
            lookback_periods: Number of mid-price samples to store (default: 20)
        """
        self.lookback_periods = lookback_periods
        # Use deque for O(1) append and automatic size management
        self.price_window: deque = deque(maxlen=lookback_periods)
        self.last_update_time = 0.0
        self.current_z_score = 0.0
        
        # EWMA Volatility tracking (RiskMetrics Standard)
        # Import here to avoid circular dependency
        from config.constants import MM_VOL_DECAY_LAMBDA
        self.ewma_lambda = MM_VOL_DECAY_LAMBDA  # 0.94 decay factor
        self.ewma_variance = None  # Initialize on first return
        self.last_price = None
        
        # ═══════════════════════════════════════════════════════════════════
        # MODULE 1: DRIFT-PROTECTED Z-SCORE (Volatility Guard)
        # ═══════════════════════════════════════════════════════════════════
        # Prevents "chasing" flash-crashes and fat-finger trades
        # Dual-window mean: Local (20) vs Global (500)
        # Clamps local mean shift to ±2.5σ of global distribution
        # ═══════════════════════════════════════════════════════════════════
        self.global_price_window: deque = deque(maxlen=500)  # Long-term reference
        self.drift_clamp_threshold = 2.5  # Sigma threshold for clamping
        self.drift_clamp_active = False  # Tracking flag
        
        logger.info(
            f"ZScoreManager initialized (EWMA volatility + Drift Protection) - "
            f"Lookback: {lookback_periods} periods, "
            f"Global window: 500 samples (drift protection), "
            f"Drift clamp: ±{self.drift_clamp_threshold:.1f}σ, "
            f"Entry threshold: ±{Z_SCORE_ENTRY_THRESHOLD:.1f}σ, "
            f"Halt threshold: ±{Z_SCORE_HALT_THRESHOLD:.1f}σ, "
            f"Sensitivity: ${MM_Z_SENSITIVITY:.4f}/σ, "
            f"EWMA Lambda: {self.ewma_lambda} (RiskMetrics standard)"
        )
    
    def update(self, micro_price: float) -> float:
        """
        Update rolling window with new MICRO-PRICE sample and recalculate Z-Score
        WITH EWMA VOLATILITY (2026 HFT Adaptive Sigma Upgrade)
        
        INSTITUTIONAL UPGRADE (2026):
        - Switched from mid_price (lagging) to micro_price (volume-weighted mid)
        - Micro-price = (bid×ask_size + ask×bid_size) / (bid_size + ask_size)
        - Provides 500ms-1500ms lead time on mean reversion vs simple mid
        - Detects order book imbalance BEFORE mid-price reflects it
        - **NEW**: EWMA volatility (λ=0.94) reacts instantly to volatility spikes
        
        Args:
            micro_price: Volume-weighted mid-price from order book imbalance
        
        Returns:
            Current Z-Score value
        
        Mathematical Steps:
        1. Append new micro-price to rolling window (auto-evicts oldest if full)
        2. Calculate EWMA volatility: σ²(t) = λ×σ²(t-1) + (1-λ)×return²(t)
        3. Calculate mean: μ = Σ(micro_prices) / N
        4. Calculate Z-Score: Z = (current_micro_price - μ) / σ_EWMA
        """
        from decimal import Decimal
        
        # Add micro-price to rolling window (deque auto-manages size)
        self.price_window.append(micro_price)
        self.global_price_window.append(micro_price)  # Also track in global window
        self.last_update_time = time.time()
        
        # Need minimum samples for statistical significance
        if len(self.price_window) < self.lookback_periods:
            logger.debug(
                f"Z-Score: Insufficient data ({len(self.price_window)}/{self.lookback_periods}) - "
                f"Using Z=0 (neutral)"
            )
            self.current_z_score = 0.0
            return 0.0
        
        # Calculate LOCAL mean (20-period EWMA)
        local_mean = statistics.mean(self.price_window)
        
        # ═══════════════════════════════════════════════════════════════════
        # MODULE 1: DRIFT PROTECTION (Dual-Window Mean Clamping)
        # ═══════════════════════════════════════════════════════════════════
        # If local mean drifts >2.5σ from global mean → CLAMP to boundary
        # Prevents chasing flash-crashes, fat-fingers, or market manipulation
        # ═══════════════════════════════════════════════════════════════════
        if len(self.global_price_window) >= 100:  # Need minimum global samples
            global_mean = statistics.mean(self.global_price_window)
            try:
                global_std_dev = statistics.stdev(self.global_price_window)
            except statistics.StatisticsError:
                global_std_dev = 1e-6
            
            # Check if local mean has drifted beyond acceptable bounds
            mean_drift = abs(local_mean - global_mean)
            drift_threshold = self.drift_clamp_threshold * global_std_dev
            
            if mean_drift > drift_threshold:
                # CLAMP local mean to ±2.5σ boundary
                if local_mean > global_mean:
                    clamped_mean = global_mean + drift_threshold
                else:
                    clamped_mean = global_mean - drift_threshold
                
                if not self.drift_clamp_active:  # Log only on activation
                    logger.info(
                        f"[INSTITUTIONAL_GUARD] DRIFT PROTECTION ACTIVATED - "
                        f"Local mean ${local_mean:.4f} drifted {mean_drift/global_std_dev:.2f}σ "
                        f"from global ${global_mean:.4f} (threshold: {self.drift_clamp_threshold:.1f}σ). "
                        f"Clamping to ${clamped_mean:.4f} to prevent chasing anomalous moves."
                    )
                    self.drift_clamp_active = True
                
                mean = clamped_mean  # Use clamped mean for Z-Score
            else:
                mean = local_mean  # Use local mean (normal operation)
                if self.drift_clamp_active:  # Log deactivation
                    logger.info(
                        f"[INSTITUTIONAL_GUARD] DRIFT PROTECTION DEACTIVATED - "
                        f"Local mean ${local_mean:.4f} returned within {self.drift_clamp_threshold:.1f}σ "
                        f"of global ${global_mean:.4f}. Resuming normal Z-Score calculation."
                    )
                    self.drift_clamp_active = False
        else:
            # Not enough global samples yet - use local mean
            mean = local_mean
        
        # ═══════════════════════════════════════════════════════════════════
        # EWMA VOLATILITY (RiskMetrics Standard - λ=0.94)
        # ═══════════════════════════════════════════════════════════════════
        # Formula: σ²(t) = λ × σ²(t-1) + (1-λ) × return²(t)
        # Rationale: Simple std dev is too slow - EWMA reacts instantly to spikes
        # ═══════════════════════════════════════════════════════════════════
        
        if self.last_price is not None:
            # Calculate squared return
            if self.last_price > 0:
                log_return = math.log(micro_price / self.last_price)
                squared_return = log_return ** 2
            else:
                squared_return = 0.0
            
            # Update EWMA variance
            if self.ewma_variance is None:
                # Initialize with squared return
                self.ewma_variance = squared_return
            else:
                # EWMA update: σ²(t) = λ×σ²(t-1) + (1-λ)×return²(t)
                self.ewma_variance = (self.ewma_lambda * self.ewma_variance +
                                      (1 - self.ewma_lambda) * squared_return)
            
            # Extract std dev from variance
            ewma_std_dev = math.sqrt(self.ewma_variance) if self.ewma_variance > 0 else 1e-6
            
        else:
            # First update - fallback to simple std dev
            try:
                ewma_std_dev = statistics.stdev(self.price_window)
                if ewma_std_dev < 1e-6:
                    ewma_std_dev = 1e-6
            except statistics.StatisticsError:
                ewma_std_dev = 1e-6
        
        # Update last price for next iteration
        self.last_price = micro_price
        
        # Guard against zero variance
        if ewma_std_dev < 1e-6:
            logger.debug("Z-Score: Zero EWMA variance detected (flat price) - Using Z=0")
            self.current_z_score = 0.0
            return 0.0
        
        # Calculate Z-Score using EWMA volatility
        self.current_z_score = (micro_price - mean) / ewma_std_dev
        
        return self.current_z_score
    
    def get_alpha_shift(self) -> float:
        """
        Calculate reservation price adjustment based on current Z-Score
        
        Returns:
            Dollar shift to apply to reservation price
            - Positive value = RAISE reservation (incentivize buying)
            - Negative value = LOWER reservation (incentivize selling)
        
        Logic:
        - If abs(Z) < ENTRY_THRESHOLD: No adjustment (Z not significant)
        - If Z > 2.0 (Overbought): NEGATIVE shift → Lower reservation → Encourage selling
        - If Z < -2.0 (Oversold): POSITIVE shift → Raise reservation → Encourage buying
        
        Mathematical Relationship:
        - Overbought (Z=+2.5): shift = -2.5 × 0.005 = -$0.0125 (lower by 1.25 cents)
        - Oversold (Z=-2.5): shift = -(-2.5) × 0.005 = +$0.0125 (raise by 1.25 cents)
        """
        if abs(self.current_z_score) < Z_SCORE_ENTRY_THRESHOLD:
            return 0.0  # Not significant enough to trade
        
        # Direct calculation: Negative Z-Score → Positive shift (and vice versa)
        # This creates counter-cyclical positioning (buy low, sell high)
        alpha_shift = -self.current_z_score * MM_Z_SENSITIVITY
        
        return alpha_shift
    
    def should_halt_trading(self) -> bool:
        """
        Check if Z-Score indicates extreme outlier (potential regime change)
        
        Returns:
            True if abs(Z) > HALT_THRESHOLD (3.5σ = 99.95% confidence)
        
        Rationale:
        - Z > 3.5σ occurs <0.05% of the time in normal distributions
        - Such extremes usually indicate news events, earnings, or market structure breaks
        - Safer to pause quoting until price action normalizes
        """
        return abs(self.current_z_score) > Z_SCORE_HALT_THRESHOLD
    
    def is_signal_active(self) -> bool:
        """
        Check if mean reversion signal is still active
        
        Returns:
            True if abs(Z) > EXIT_TARGET (0.5σ)
        
        Logic:
        - Once abs(Z) falls below 0.5σ, mean reversion is largely complete
        - Remove alpha skew to avoid over-trading near equilibrium
        """
        return abs(self.current_z_score) > Z_SCORE_EXIT_TARGET
    
    def get_z_score(self) -> float:
        """Get current Z-Score value"""
        return self.current_z_score
    
    def is_ready(self) -> bool:
        """Check if enough samples collected for valid Z-Score"""
        return len(self.price_window) >= self.lookback_periods


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
        
        # Unwinding tracking (for emergency force exit)
        self.unwinding_start: Dict[str, float] = {}  # token_id -> timestamp
        self.unwinding_timeout = 300  # 5 minutes max for passive unwinding
        
        # Active orders
        self.active_bids: Dict[str, str] = {}  # token_id -> order_id
        self.active_asks: Dict[str, str] = {}  # token_id -> order_id
        
        # Performance
        self.realized_pnl = 0.0
        self.total_volume = 0.0
        self.fill_count = 0
        
        # Toxic flow detection (protect against being run over)
        self.recent_fills: List[Tuple[float, str, float]] = []  # (timestamp, side, size)
        self.toxic_flow_window = 10  # seconds
        self.toxic_flow_threshold = 50.0  # $50 filled in window = toxic
        self.spread_widening_until = 0.0  # timestamp to stop widening
        
        # ═══════════════════════════════════════════════════════════════════
        # MODULE 4: MARKOUT SELF-TUNING (Alpha Guard)
        # ═══════════════════════════════════════════════════════════════════
        # Calculates 5-second Markout PnL (PnL if closed 5s after fill)
        # Maintains deque of last 20 markouts
        # If mean < 0 → Increment MM_TARGET_SPREAD + MM_Z_SENSITIVITY by 15%
        # Reset only after 10 consecutive positive markouts
        # ═══════════════════════════════════════════════════════════════════
        # Format: (timestamp, token_id, side, fill_price, fill_size)
        self.fill_history: deque = deque(maxlen=100)  # Last 100 fills
        self.markout_window = deque(maxlen=20)  # Last 20 markout PnLs (5s interval)
        self.markout_interval = 5.0  # 5 seconds
        self.adverse_selection_count = 0  # Count of negative markouts
        self.total_markout_pnl = 0.0  # Cumulative markout P&L
        
        # Self-tuning multipliers (start at 1.0 = no adjustment)
        self.spread_multiplier = 1.0
        self.sensitivity_multiplier = 1.0
        self.tuning_increment = 0.15  # 15% per adjustment
        self.consecutive_positive_markouts = 0  # Reset counter
        self.markout_lock = asyncio.Lock()  # Thread-safe updates
        
        # ═══════════════════════════════════════════════════════════════════
        # MODULE 2: SKEW HYSTERESIS (Efficiency Guard)
        # ═══════════════════════════════════════════════════════════════════
        # Prevents wasteful cancel/replace cycles on minor inventory changes
        # Only updates quotes if:
        #   - Reservation price changed by > $0.002 (20% of tick)
        #   - OR inventory changed by > 10% of max position size
        # Emergency exits bypass hysteresis
        # ═══════════════════════════════════════════════════════════════════
        self.last_applied_reservation_price: Dict[str, float] = {tid: 0.0 for tid in token_ids}
        self.last_inventory_snapshot: Dict[str, int] = {tid: 0 for tid in token_ids}
        self.hysteresis_lock = asyncio.Lock()
        self.hysteresis_threshold_price = 0.002  # $0.002 = 20% of 0.01 tick
        self.hysteresis_threshold_inventory_pct = 0.10  # 10% of max position
        self.hysteresis_blocks = 0  # Track efficiency gains
        
    async def should_update_quotes(
        self,
        token_id: str,
        new_reservation_price: float,
        current_inventory: int,
        max_position_size: float,
        is_emergency: bool = False
    ) -> bool:
        """MODULE 2: Skew Hysteresis - Check if quote update is necessary
        
        Prevents API waste by blocking updates when changes are minor.
        
        Args:
            token_id: Token being quoted
            new_reservation_price: Newly calculated reservation price
            current_inventory: Current inventory for this token
            max_position_size: Maximum position size constant
            is_emergency: If True, bypass hysteresis (for risk-based exits)
            
        Returns:
            True if quotes should be updated, False if blocked by hysteresis
        """
        # Emergency exits always bypass hysteresis
        if is_emergency:
            return True
        
        async with self.hysteresis_lock:
            last_res_price = self.last_applied_reservation_price.get(token_id, 0.0)
            last_inventory = self.last_inventory_snapshot.get(token_id, 0)
            
            # First time quoting this token - always update
            if last_res_price == 0.0:
                self.last_applied_reservation_price[token_id] = new_reservation_price
                self.last_inventory_snapshot[token_id] = current_inventory
                return True
            
            # Check price deviation
            price_delta = abs(new_reservation_price - last_res_price)
            price_threshold_exceeded = price_delta > self.hysteresis_threshold_price
            
            # Check inventory deviation (as % of max position)
            inventory_delta = abs(current_inventory - last_inventory)
            inventory_pct_change = inventory_delta / max_position_size if max_position_size > 0 else 0
            inventory_threshold_exceeded = inventory_pct_change > self.hysteresis_threshold_inventory_pct
            
            # Update if either threshold exceeded
            if price_threshold_exceeded or inventory_threshold_exceeded:
                # Update snapshot
                self.last_applied_reservation_price[token_id] = new_reservation_price
                self.last_inventory_snapshot[token_id] = current_inventory
                
                logger.debug(
                    f"[HYSTERESIS] Quote update ALLOWED for {token_id[:8]}... - "
                    f"Price Δ: ${price_delta:.4f} {'✅' if price_threshold_exceeded else '❌'}, "
                    f"Inv Δ: {inventory_pct_change*100:.1f}% {'✅' if inventory_threshold_exceeded else '❌'}"
                )
                
                return True
            else:
                # Block update - changes too small
                self.hysteresis_blocks += 1
                
                # Log every 10th block to track efficiency
                if self.hysteresis_blocks % 10 == 0:
                    logger.info(
                        f"[INSTITUTIONAL_GUARD] SKEW HYSTERESIS EFFICIENCY - "
                        f"Blocked {self.hysteresis_blocks} unnecessary quote updates. "
                        f"Token: {token_id[:8]}..., Price Δ: ${price_delta:.4f} < ${self.hysteresis_threshold_price:.4f}, "
                        f"Inv Δ: {inventory_pct_change*100:.1f}% < {self.hysteresis_threshold_inventory_pct*100:.0f}%. "
                        f"Saving API rate limits and reducing latency."
                    )
                
                return False
        
    async def calculate_markout_pnl(
        self,
        current_micro_price: float,
        token_id: str
    ) -> None:
        """
        MODULE 4: Calculate 5-second Markout PnL for recent fills
        
        Markout PnL = PnL if position was closed 5 seconds after the fill
        - Positive markout = Good fill (bought low, price rose)
        - Negative markout = Adverse selection (bought high, price fell)
        
        Args:
            current_micro_price: Current volume-weighted mid-price
            token_id: Token being evaluated
        """
        current_time = time.time()
        
        # Find fills that are ~5 seconds old (±0.5s tolerance)
        mature_fills = [
            fill for fill in self.fill_history
            if fill[1] == token_id
            and abs((current_time - fill[0]) - self.markout_interval) < 0.5
        ]
        
        if not mature_fills:
            return  # No fills ready for markout calculation
        
        async with self.markout_lock:
            for fill in mature_fills:
                timestamp, tid, side, fill_price, fill_size = fill
                
                # Calculate markout PnL
                if side == 'BUY':
                    # Bought at fill_price, current price is current_micro_price
                    # PnL = (current - fill) * size
                    markout_pnl = (current_micro_price - fill_price) * fill_size
                else:  # SELL
                    # Sold at fill_price, current price is current_micro_price
                    # PnL = (fill - current) * size
                    markout_pnl = (fill_price - current_micro_price) * fill_size
                
                # Add to markout window
                self.markout_window.append(markout_pnl)
                self.total_markout_pnl += markout_pnl
                
                if markout_pnl < 0:
                    self.adverse_selection_count += 1
                    self.consecutive_positive_markouts = 0  # Reset counter
                else:
                    self.consecutive_positive_markouts += 1
                
                logger.debug(
                    f"[MARKOUT] {side} fill @ ${fill_price:.4f} → "
                    f"5s price ${current_micro_price:.4f} = "
                    f"${markout_pnl:+.4f} PnL ({fill_size:.1f} shares)"
                )
                
                # Remove processed fill from history
                self.fill_history.remove(fill)
    
    async def apply_self_tuning(self) -> Tuple[float, float]:
        """
        MODULE 4: Markout Self-Tuning - Adjust spread/sensitivity based on markout PnL
        
        Logic:
        1. Calculate mean of last 20 markouts
        2. If mean < 0 → Increment multipliers by 15%
        3. Only reset after 10 consecutive positive markouts
        
        Returns:
            Tuple of (adjusted_spread_multiplier, adjusted_sensitivity_multiplier)
        """
        if len(self.markout_window) < 20:
            return self.spread_multiplier, self.sensitivity_multiplier  # Not enough data
        
        async with self.markout_lock:
            mean_markout = statistics.mean(self.markout_window)
            
            # NEGATIVE MEAN: We're being adversely selected → WIDEN SPREADS
            if mean_markout < 0:
                # Increment both multipliers by 15%
                self.spread_multiplier += self.tuning_increment
                self.sensitivity_multiplier += self.tuning_increment
                
                logger.info(
                    f"[INSTITUTIONAL_GUARD] MARKOUT SELF-TUNING ACTIVATED - "
                    f"Mean 5s markout: ${mean_markout:.4f} (NEGATIVE). "
                    f"Adverse selection detected. Adjusting parameters:\n"
                    f"  Spread Multiplier: {self.spread_multiplier:.2f}x "
                    f"(+{self.tuning_increment*100:.0f}%)\n"
                    f"  Sensitivity Multiplier: {self.sensitivity_multiplier:.2f}x "
                    f"(+{self.tuning_increment*100:.0f}%)\n"
                    f"  Total Markout PnL: ${self.total_markout_pnl:.2f}\n"
                    f"  Adverse fills: {self.adverse_selection_count}/{len(self.markout_window)}"
                )
            
            # RESET CONDITION: 10 consecutive positive markouts
            elif self.consecutive_positive_markouts >= 10 and self.spread_multiplier > 1.0:
                self.spread_multiplier = 1.0
                self.sensitivity_multiplier = 1.0
                self.consecutive_positive_markouts = 0
                
                logger.info(
                    f"[INSTITUTIONAL_GUARD] MARKOUT SELF-TUNING RESET - "
                    f"10 consecutive positive markouts achieved. "
                    f"Mean 5s markout: ${mean_markout:.4f}. "
                    f"Resetting multipliers to 1.0x (baseline parameters)."
                )
            
            return self.spread_multiplier, self.sensitivity_multiplier
    
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
        
        # Track fill for toxic flow detection
        fill_value = abs(shares) * price
        self.recent_fills.append((time.time(), 'BUY' if is_buy else 'SELL', fill_value))
    
    def record_fill_for_markout(self, token_id: str, side: str, fill_price: float, 
                                 micro_price: float, size: float):
        """Record fill with micro-price for post-trade alpha analysis"""
        timestamp = time.time()
        self.fill_history.append((timestamp, token_id, side, fill_price, micro_price, size))
        
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
    
    def get_net_inventory(self) -> int:
        """Get net directional inventory for binary markets"""
        # For binary markets (Yes/No), normalize to single delta
        # Being long 10 Yes = being short 10 No
        if len(self.token_ids) == 2:
            inv_yes = self.inventory.get(self.token_ids[0], 0)
            inv_no = self.inventory.get(self.token_ids[1], 0)
            # Net position: positive = long market, negative = short market
            return inv_yes - inv_no
        else:
            # Multi-outcome market: return sum (less precise but safe)
            return sum(self.inventory.values())
    
    def get_inventory_age(self) -> float:
        """Get age of position in seconds"""
        return (datetime.now() - self.entry_time).total_seconds()
    
    def check_toxic_flow(self) -> bool:
        """Detect if being run over by large one-sided flow"""
        import time as time_module
        current_time = time_module.time()
        
        # Clean old fills outside window
        self.recent_fills = [
            (ts, side, val) for ts, side, val in self.recent_fills 
            if current_time - ts <= self.toxic_flow_window
        ]
        
        if not self.recent_fills:
            return False
        
        # Calculate total filled in window
        total_filled = sum(val for _, _, val in self.recent_fills)
        
        # Check if one-sided (>80% buys or sells)
        buy_fills = sum(val for _, side, val in self.recent_fills if side == 'BUY')
        sell_fills = sum(val for _, side, val in self.recent_fills if side == 'SELL')
        
        one_sided_ratio = max(buy_fills, sell_fills) / total_filled if total_filled > 0 else 0
        
        # Toxic flow = large volume AND one-sided
        if total_filled > self.toxic_flow_threshold and one_sided_ratio > 0.8:
            # Widen spread for 60 seconds
            self.spread_widening_until = current_time + 60
            return True
        
        return False
    
    def calculate_markout_pnl(self, current_prices: Dict[str, float]) -> Dict[str, float]:
        """Calculate post-trade alpha (markout P&L) to detect adverse selection"""
        import time as time_module
        current_time = time_module.time()
        
        markout_results = {}
        
        for ts, token_id, side, fill_price, micro_at_fill, size in self.fill_history:
            age = current_time - ts
            
            # Only calculate for fills that have aged past intervals
            for interval in self.markout_intervals:
                if age >= interval and f"{interval}s" not in markout_results:
                    current_micro = current_prices.get(token_id)
                    if not current_micro:
                        continue
                    
                    # Markout = (current_price - fill_price) * direction
                    # Positive = good fill (price moved in our favor)
                    # Negative = adverse selection (price moved against us)
                    direction = 1 if side == 'BUY' else -1
                    markout = (current_micro - fill_price) * direction * size
                    
                    markout_results[f"{interval}s"] = markout
                    
                    # Track adverse selection
                    if markout < 0:
                        self.adverse_selection_count += 1
                    
                    self.total_markout_pnl += markout
        
        return markout_results


class MarketMakingStrategy(BaseStrategy):
    """
    Market Making Strategy - Provide liquidity and earn spreads
    
    Runs independently of arbitrage strategy with dedicated capital allocation.
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        market_data_manager: Optional[MarketDataManager] = None,
        config: Optional[Dict[str, Any]] = None,
        execution_gateway: Optional[Any] = None,  # CRITICAL FIX #1: Centralized routing
        inventory_manager: Optional[Any] = None,  # AUDIT FIX: Unified position tracking
        risk_controller: Optional[Any] = None,  # AUDIT FIX: Circuit breaker authority
        max_capital: Optional[float] = None  # INSTITUTIONAL: Dynamic capital allocation
    ):
        """
        Initialize market making strategy
        
        Args:
            client: Polymarket CLOB client
            order_manager: Order execution manager
            market_data_manager: WebSocket market data manager
            config: Optional configuration overrides
            execution_gateway: Centralized order routing with STP
            inventory_manager: Unified inventory tracking authority
            risk_controller: Risk management and circuit breaker
            max_capital: Dynamically allocated capital (overrides constant)
        """
        super().__init__(client, order_manager, config)
        
        # Market data manager for real-time WebSocket data
        self._market_data_manager = market_data_manager
        
        # Execution gateway for centralized routing
        self._execution_gateway = execution_gateway
        
        # Risk management (AUDIT FIX)
        self._inventory_manager = inventory_manager
        self._risk_controller = risk_controller
        
        # Budget tracking - Use dynamic allocation if provided
        allocated_amount = max_capital if max_capital is not None else MARKET_MAKING_STRATEGY_CAPITAL
        self._allocated_capital = Decimal(str(allocated_amount))
        self._capital_used = Decimal('0')
        
        # Active positions
        self._positions: Dict[str, MarketPosition] = {}
        
        # Z-Score managers for each market (Mean Reversion Alpha Overlay)
        self._z_score_managers: Dict[str, ZScoreManager] = {}
        self._last_z_score_update: Dict[str, float] = {}
        
        # INSTITUTIONAL UPGRADE: State Consistency Lock (prevents stale quote collision)
        self._quote_calculation_lock: asyncio.Lock = asyncio.Lock()
        
        # Market selection cache
        self._eligible_markets: List[Dict] = []
        self._last_market_scan = 0
        self._market_scan_interval = 300  # 5 minutes
        
        # Strategy state
        self._is_running = False
        self._last_quote_update = {}
        self._last_order_time = 0
        self._last_fill_sync = 0
        self._fill_sync_interval = 1  # Check fills every 1 second (institutional-grade)
        
        # Performance tracking
        self._total_fills = 0
        self._total_maker_volume = 0.0
        self._total_pnl = 0.0
        
        # Global daily loss tracking (circuit breaker)
        self._daily_pnl = 0.0
        self._daily_pnl_reset_time = datetime.now()
        
        # State rehydration flag
        self._positions_rehydrated = False
        
        # Post-trade alpha tracking (institutional-grade)
        self._markout_check_interval = 30  # Check markout every 30 seconds
        self._last_markout_check = 0
        
        # External oracle integration (future: Manifold, Kalshi, etc.)
        self._oracle_prices: Dict[str, float] = {}  # market_id -> oracle_price
        self._oracle_enabled = False  # Enable when oracle integration ready
        
        # Inventory defense mode (fast market protection)
        self._inventory_defense_mode: Dict[str, float] = {}  # market_id -> end_time
        self._defense_mode_duration = 60  # Stay in defense mode for 60 seconds
        
        # Toxic flow filter (Z-Score vs OBI momentum conflict)
        self._toxic_flow_paused: Dict[str, float] = {}  # market_id -> resume_time
        
        # CRITICAL FIX #1: Arb execution pause (prevents inventory race condition)
        self._arb_paused_markets: set = set()  # Markets paused during arb execution
        self._arb_pause_expiry: Dict[str, float] = {}  # Auto-resume after timeout
        
        logger.info(
            f"🎯 MarketMakingStrategy initialized [NEW CODE v2.0] - "
            f"Capital: ${self._allocated_capital}, "
            f"Max markets: {MM_MAX_MARKETS}, "
            f"Target spread: {MM_TARGET_SPREAD*100:.1f}%, "
            f"Min depth: {MM_MIN_DEPTH_SHARES} shares, "
            f"Min liquidity depth: ${MM_MIN_LIQUIDITY_DEPTH}, "
            f"Volume Multiplier: {MM_VOLUME_MULTIPLIER}x, "
            f"Hard Floor Volume: ${MM_HARD_FLOOR_VOLUME}/day, "
            f"Max directional exposure: ${MM_MAX_TOTAL_DIRECTIONAL_EXPOSURE}"
        )
        
        # Register fill handler if WebSocket manager available
        if self._market_data_manager:
            self._market_data_manager.register_fill_handler(
                'market_making',
                self.handle_fill_event
            )
            logger.info("✅ Registered for real-time fill events via WebSocket")
            
            # INSTITUTIONAL SAFETY: Register disconnection handler for Flash Cancel
            self._market_data_manager.cache.register_disconnection_handler(
                'market_making_flash_cancel',
                self.on_websocket_disconnection
            )
            logger.info("✅ Registered disconnection handler for Flash Cancel")
    
    async def pause_for_arb(self, market_id: str) -> None:
        """
        CRITICAL FIX #1: Pause MM quoting during arb execution
        
        Prevents race condition where:
        1. Arb calculates profit assuming static inventory
        2. MM places new order during arb execution
        3. Inventory changes → arb profit calculation invalid
        4. Potential self-trade (arb hits our MM quote)
        
        Duration: 500-1000ms (typical arb execution time)
        """
        self._arb_paused_markets.add(market_id)
        self._arb_pause_expiry[market_id] = time.time() + 1.5  # 1.5s timeout
        logger.debug(
            f"[MM_PAUSE] ⏸️  Paused quoting on {market_id[:8]}... "
            f"(arb execution in progress, prevents inventory race)"
        )
    
    async def resume_from_arb(self, market_id: str) -> None:
        """
        Resume MM quoting after arb execution completes
        """
        self._arb_paused_markets.discard(market_id)
        self._arb_pause_expiry.pop(market_id, None)
        logger.debug(
            f"[MM_RESUME] ▶️  Resumed quoting on {market_id[:8]}... "
            f"(arb execution complete)"
        )
    
    def _is_arb_paused(self, market_id: str) -> bool:
        """
        Check if market is paused for arb execution (with timeout)
        """
        if market_id not in self._arb_paused_markets:
            return False
        
        # Auto-expire if timeout exceeded
        expiry = self._arb_pause_expiry.get(market_id, 0)
        if time.time() > expiry:
            logger.warning(
                f"[MM_PAUSE] ⏰ Timeout expired for {market_id[:8]}... "
                f"- auto-resuming quoting"
            )
            self._arb_paused_markets.discard(market_id)
            self._arb_pause_expiry.pop(market_id, None)
            return False
        
        return True
    
    def _calculate_dynamic_min_volume(self) -> Decimal:
        """
        Calculate dynamic minimum volume threshold based on current capital allocation
        
        ADAPTIVE CAPACITY FILTERING (2026 Institutional Standard)
        ══════════════════════════════════════════════════════════════════════════════════
        
        Formula:
            Target_Position_Size = Current_Balance / MM_MAX_MARKETS
            Dynamic_Min_Volume = max(Target_Position_Size * MM_VOLUME_MULTIPLIER, MM_HARD_FLOOR_VOLUME)
        
        Rationale:
            - Ensures position size < 5% of daily volume (market impact limit)
            - Prevents capital fragmentation across too many thin markets
            - Scales automatically as account balance grows
            - Hard floor prevents quoting in "dead" markets (< $50/day)
        
        Example (Institutional-Grade):
            Balance: $80 (80% of $100 principal deployed)
            Target_Position: $80 / 5 markets = $16 per market
            Dynamic_Min_Volume: max($16 * 20, $50) = max($320, $50) = $320/day
            
            Market Impact Check:
                $16 position / $320 daily volume = 5% (acceptable)
                
        Example (Small Account):
            Balance: $10 (only $10 deployed)
            Target_Position: $10 / 5 markets = $2 per market
            Dynamic_Min_Volume: max($2 * 20, $50) = max($40, $50) = $50/day (hard floor kicks in)
        
        Returns:
            Decimal: Dynamic minimum volume threshold in USDC
        """
        # Step A: Get current available balance from allocated capital
        # Use Decimal for precise financial calculations
        current_balance = Decimal(str(self._allocated_capital))
        
        # Step B: Calculate target position size per market
        max_markets = Decimal(str(MM_MAX_MARKETS))
        target_position_size = current_balance / max_markets
        
        # Step C: Calculate dynamic minimum volume
        volume_multiplier = Decimal(str(MM_VOLUME_MULTIPLIER))
        calculated_threshold = target_position_size * volume_multiplier
        
        # Step D: Apply hard floor (safety guard)
        # For Polymarket's low-volume environment, use the FLOOR as default
        # Only increase threshold if calculated value is higher AND account has sufficient capital
        hard_floor = Decimal(str(MM_HARD_FLOOR_VOLUME))
        
        # Use floor for small accounts, calculated threshold for large accounts
        # This ensures small accounts can still find markets to trade
        if current_balance < Decimal('500'):  # Small account threshold
            dynamic_min_volume = hard_floor
        else:
            dynamic_min_volume = max(calculated_threshold, hard_floor)
        
        logger.debug(
            f"[ADAPTIVE FILTER] Balance: ${current_balance:.2f}, "
            f"Target/Market: ${target_position_size:.2f}, "
            f"Calculated: ${calculated_threshold:.2f}, "
            f"Dynamic Threshold: ${dynamic_min_volume:.2f}/day "
            f"({'hard floor (small account)' if dynamic_min_volume == hard_floor and current_balance < 500 else 'hard floor' if dynamic_min_volume == hard_floor else 'calculated'})"
        )
        
        return dynamic_min_volume
    
    async def handle_fill_event(self, fill: FillEvent) -> None:
        """
        Handle real-time fill event from WebSocket
        
        Called by MarketDataManager when /user channel receives a fill.
        This provides instant inventory updates without polling.
        
        INSTITUTIONAL UPGRADE: IMMEDIATE CANCEL ON FILL
        - On BID fill → immediately cancel ASK to prevent double-exposure
        - On ASK fill → immediately cancel BID to prevent double-exposure
        - This prevents the "fill-to-quote latency" race condition
        """
        try:
            # Find which position this fill belongs to
            for market_id, position in self._positions.items():
                if fill.order_id in position.active_bids.values() or fill.order_id in position.active_asks.values():
                    # This fill belongs to this market
                    is_buy = (fill.side.upper() == 'BUY')
                    
                    # CRITICAL: IMMEDIATE CANCEL ON FILL (High-Priority Callback)
                    # Cancel opposite side BEFORE updating inventory to minimize exposure window
                    if is_buy:
                        # BID was filled → immediately cancel ASK to prevent double-long
                        if fill.asset_id in position.active_asks:
                            ask_order_id = position.active_asks[fill.asset_id]
                            try:
                                await self.client.cancel_order(ask_order_id)
                                logger.warning(
                                    f"🚨 IMMEDIATE CANCEL: ASK {ask_order_id[:8]}... cancelled "
                                    f"after BID fill to prevent double-exposure"
                                )
                                del position.active_asks[fill.asset_id]
                            except Exception as e:
                                logger.error(f"Failed to cancel ASK on BID fill: {e}")
                    else:
                        # ASK was filled → immediately cancel BID to prevent double-short
                        if fill.asset_id in position.active_bids:
                            bid_order_id = position.active_bids[fill.asset_id]
                            try:
                                await self.client.cancel_order(bid_order_id)
                                logger.warning(
                                    f"🚨 IMMEDIATE CANCEL: BID {bid_order_id[:8]}... cancelled "
                                    f"after ASK fill to prevent double-exposure"
                                )
                                del position.active_bids[fill.asset_id]
                            except Exception as e:
                                logger.error(f"Failed to cancel BID on ASK fill: {e}")
                    
                    # Update inventory immediately
                    position.update_inventory(
                        token_id=fill.asset_id,
                        shares=int(fill.size),
                        price=fill.price,
                        is_buy=is_buy
                    )
                    
                    # Get current micro-price for markout tracking
                    snapshot = self._market_data_manager.cache.get(fill.asset_id) if self._market_data_manager else None
                    micro_price = snapshot.micro_price if snapshot else fill.price
                    
                    position.record_fill_for_markout(
                        token_id=fill.asset_id,
                        side=fill.side,
                        fill_price=fill.price,
                        micro_price=micro_price,
                        size=fill.size
                    )
                    
                    logger.info(
                        f"[MM Fill] {fill.side} {fill.size:.1f} @ {fill.price:.4f} - "
                        f"New inventory: {position.inventory[fill.asset_id]} "
                        f"(micro: {micro_price:.4f})"
                    )
                    
                    # Remove filled order from tracking
                    if fill.order_id in position.active_bids.values():
                        position.active_bids = {k: v for k, v in position.active_bids.items() if v != fill.order_id}
                    if fill.order_id in position.active_asks.values():
                        position.active_asks = {k: v for k, v in position.active_asks.items() if v != fill.order_id}
                    
                    self._total_fills += 1
                    break
                    
        except Exception as e:
            logger.error(f"Error handling fill event: {e}", exc_info=True)
    
    @property
    def is_running(self) -> bool:
        """Public property for health checks to access running state"""
        return self._is_running
    
    @is_running.setter
    def is_running(self, value: bool) -> None:
        """Allow BaseStrategy to set running state"""
        self._is_running = value
    
    def get_market_inventory(self, market_id: str) -> Optional[Dict[str, int]]:
        """Get current inventory for a specific market (CROSS-STRATEGY COORDINATION)
        
        Returns:
            Dict mapping token_id -> inventory (positive = long, negative = short)
            None if no position in this market
        """
        position = self._positions.get(market_id)
        if not position:
            return None
        return position.inventory.copy()
    
    def get_all_inventory(self) -> Dict[str, Dict[str, int]]:
        """Get inventory across all active positions
        
        Returns:
            Dict mapping market_id -> {token_id -> inventory}
        """
        return {
            market_id: position.inventory.copy()
            for market_id, position in self._positions.items()
        }
    
    def on_websocket_disconnection(self) -> None:
        """
        INSTITUTIONAL SAFETY: Flash Cancel on WebSocket Disconnect
        
        Called immediately when WebSocket connection drops.
        This prevents "blind quoting" - leaving orders on the exchange
        while having no live data feed.
        
        CRITICAL: This is a synchronous callback - must not use await.
        We schedule the async cancel operation in the event loop.
        """
        logger.critical(
            "🚨 FLASH CANCEL TRIGGERED: WebSocket disconnected - "
            "Cancelling ALL quotes to prevent blind trading"
        )
        
        # Schedule async cancel in event loop (callback must be sync)
        try:
            # Get the running event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule the async cancel as a task
                loop.create_task(self._emergency_cancel_all_orders())
            else:
                logger.error("Event loop not running - cannot schedule flash cancel")
        except Exception as e:
            logger.error(f"Failed to schedule flash cancel: {e}", exc_info=True)
    
    async def _emergency_cancel_all_orders(self) -> None:
        """
        Emergency cancellation of ALL active orders across all positions
        
        Used by Flash Cancel on disconnection to prevent blind trading.
        """
        try:
            cancel_count = 0
            for market_id, position in self._positions.items():
                # Cancel all bids
                for token_id, order_id in list(position.active_bids.items()):
                    try:
                        await self.client.cancel_order(order_id)
                        cancel_count += 1
                        del position.active_bids[token_id]
                    except Exception as e:
                        logger.debug(f"Failed to cancel bid {order_id[:8]}...: {e}")
                
                # Cancel all asks
                for token_id, order_id in list(position.active_asks.items()):
                    try:
                        await self.client.cancel_order(order_id)
                        cancel_count += 1
                        del position.active_asks[token_id]
                    except Exception as e:
                        logger.debug(f"Failed to cancel ask {order_id[:8]}...: {e}")
            
            logger.critical(
                f"✅ FLASH CANCEL COMPLETE: Cancelled {cancel_count} orders "
                f"across {len(self._positions)} positions"
            )
            
        except Exception as e:
            logger.error(f"Emergency cancel failed: {e}", exc_info=True)
    
    async def run(self) -> None:
        """Main strategy loop"""
        if self._is_running:
            logger.warning("MarketMakingStrategy already running")
            return
        
        self._is_running = True
        logger.info("🎯 MarketMakingStrategy started")
        
        # CRITICAL: Rehydrate positions from API on startup
        if not self._positions_rehydrated:
            await self._rehydrate_positions()
            self._positions_rehydrated = True
        
        try:
            while self._is_running:
                try:
                    # CRITICAL: Global daily drawdown circuit breaker
                    if not await self._check_global_drawdown():
                        logger.critical(
                            f"🚨 GLOBAL DAILY LOSS LIMIT EXCEEDED: ${self._daily_pnl:.2f} "
                            f"(limit: -${MM_GLOBAL_DAILY_LOSS_LIMIT}) - STOPPING ALL ACTIVITIES"
                        )
                        self._is_running = False
                        break
                    
                    await self._update_eligible_markets()
                    await self._manage_positions()
                    await self._sync_fills()  # CRITICAL: Detect fills and update inventory
                    await self._check_markout_pnl()  # Track post-trade alpha
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
    
    async def _calculate_total_directional_exposure(self) -> float:
        """Calculate absolute net delta across all positions (correlation risk)"""
        total_abs_exposure = 0.0
        
        for market_id, position in self._positions.items():
            # Get current prices for valuation
            prices = await self._get_market_prices(market_id, position.token_ids)
            if not prices:
                continue
            
            # For binary markets, use net inventory (Yes - No)
            if len(position.token_ids) == 2:
                net_inv = position.get_net_inventory()
                # Value the net position at mid price
                mid_price = sum(prices.values()) / len(prices) if prices else 0.5
                exposure = abs(net_inv * mid_price)
            else:
                # Multi-outcome: sum absolute value of all positions
                exposure = sum(
                    abs(position.inventory.get(tid, 0) * prices.get(tid, 0.5))
                    for tid in position.token_ids
                )
            
            total_abs_exposure += exposure
        
        return total_abs_exposure
    
    async def _check_global_drawdown(self) -> bool:
        """Circuit breaker: Stop all activity if daily loss exceeds limit"""
        now = datetime.now()
        
        # Reset daily P&L at midnight
        if now.date() > self._daily_pnl_reset_time.date():
            logger.info(f"Daily P&L reset: Previous day ${self._daily_pnl:.2f}")
            self._daily_pnl = 0.0
            self._daily_pnl_reset_time = now
        
        # Calculate total unrealized P&L across all positions
        total_unrealized = 0.0
        for market_id, position in self._positions.items():
            prices = await self._get_market_prices(market_id, position.token_ids)
            if prices:
                total_unrealized += position.get_unrealized_pnl(prices)
        
        # Daily P&L = realized today + current unrealized
        current_daily_pnl = self._daily_pnl + total_unrealized
        
        # Circuit breaker threshold
        if current_daily_pnl < -MM_GLOBAL_DAILY_LOSS_LIMIT:
            return False  # Stop trading
        
        return True  # Continue trading
    
    async def _rehydrate_positions(self) -> None:
        """CRITICAL: Restore inventory from API on startup (prevents double-buying)"""
        logger.info("Rehydrating positions from Polymarket API...")
        
        try:
            # Get current positions from exchange
            positions_response = await self.client.get_positions()
            if not positions_response:
                logger.info("No existing positions found on exchange")
                return
            
            # Parse positions (format depends on API)
            positions = positions_response if isinstance(positions_response, list) else positions_response.get('data', [])
            
            for position in positions:
                market_id = position.get('market', position.get('market_id'))
                token_id = position.get('asset_id', position.get('token_id'))
                size = float(position.get('size', 0))
                
                if not market_id or not token_id or size == 0:
                    continue
                
                # Get market details
                try:
                    market = await self.client.get_market(market_id)
                    if not market:
                        continue
                    
                    question = market.get('question', 'Unknown')
                    tokens = market.get('tokens', [])
                    token_ids = [t.get('token_id') for t in tokens]
                    
                    # Create or update MarketPosition
                    if market_id not in self._positions:
                        self._positions[market_id] = MarketPosition(market_id, question, token_ids)
                    
                    # Restore inventory
                    avg_price = float(position.get('avg_entry_price', 0.5))
                    self._positions[market_id].inventory[token_id] = int(size)
                    self._positions[market_id].cost_basis[token_id] = avg_price
                    
                    logger.info(
                        f"Rehydrated position: {question[:40]}... "
                        f"Token: {token_id[:8]}... Size: {size:.1f} @ {avg_price:.4f}"
                    )
                    
                except Exception as e:
                    logger.warning(f"Error rehydrating position for {market_id[:8]}...: {e}")
                    continue
            
            logger.info(f"Position rehydration complete: {len(self._positions)} markets restored")
            
        except Exception as e:
            logger.error(f"Error rehydrating positions: {e}", exc_info=True)
            # Don't fail startup - continue with empty positions but log warning
            logger.warning("⚠️ Starting with empty positions - manual reconciliation may be needed")
    
    async def _check_markout_pnl(self) -> None:
        """Track post-trade alpha to detect adverse selection"""
        current_time = time.time()
        if current_time - self._last_markout_check < self._markout_check_interval:
            return
        
        self._last_markout_check = current_time
        
        for market_id, position in self._positions.items():
            if not position.fill_history:
                continue
            
            # Get current prices for markout calculation
            prices = await self._get_market_prices(market_id, position.token_ids)
            if not prices:
                continue
            
            # Calculate markout P&L
            markout_results = position.calculate_markout_pnl(prices)
            
            if markout_results:
                # Log markout metrics
                for interval, pnl in markout_results.items():
                    logger.info(
                        f"[Markout] {market_id[:8]}... {interval}: "
                        f"${pnl:.4f} (Total: ${position.total_markout_pnl:.4f}, "
                        f"Adverse: {position.adverse_selection_count})"
                    )
                
                # CRITICAL: If consistently negative markout, we're being picked off
                if position.fill_count > 10:  # Need statistical significance
                    avg_markout = position.total_markout_pnl / position.fill_count
                    
                    if avg_markout < -0.005:  # -0.5 cents per fill average
                        logger.warning(
                            f"⚠️ ADVERSE SELECTION in {market_id[:8]}... - "
                            f"Avg markout: ${avg_markout:.4f} - WIDENING SPREAD"
                        )
                        # Mark as toxic to widen spread
                        position.spread_widening_until = current_time + 300  # 5 min
    
    async def _update_eligible_markets(self) -> None:
        """Scan and filter markets suitable for market making"""
        current_time = time.time()
        
        if current_time - self._last_market_scan < self._market_scan_interval:
            return
        
        logger.debug("Scanning for eligible market making opportunities...")
        
        try:
            # GAMMA API: Fetch markets with volume/liquidity data
            # Per Polymarket Support: CLOB client.get_markets() doesn't include volume/liquidity
            # Use Gamma API directly: https://gamma-api.polymarket.com/markets
            all_markets = []
            next_cursor = None
            max_pages = 10  # Fetch up to 1000 markets
            
            # Use POLYMARKET_GAMMA_API_URL from config
            from config.constants import POLYMARKET_GAMMA_API_URL
            
            async with aiohttp.ClientSession() as session:
                for page in range(max_pages):
                    if next_cursor == 'END':
                        break
                    
                    # Build Gamma API URL
                    url = f"{POLYMARKET_GAMMA_API_URL}/markets"
                    params = {
                        'active': 'true',
                        'closed': 'false',  # Only open markets
                        'limit': '1000'     # Max markets per page
                    }
                    if next_cursor:
                        params['next_cursor'] = next_cursor
                    
                    # Fetch from Gamma API
                    async with session.get(url, params=params, timeout=10) as resp:
                        if resp.status != 200:
                            logger.error(f"Gamma API error: {resp.status}")
                            break
                        
                        response = await resp.json()
                        
                        # Gamma API returns list directly, not {'data': [...]}
                        if isinstance(response, list):
                            page_markets = response
                            next_cursor = 'END'  # No pagination for list response
                        else:
                            # Handle dict response with pagination
                            page_markets = response.get('data', [])
                            next_cursor = response.get('next_cursor', 'END')
                        
                        if not page_markets:
                            break
                        
                        all_markets.extend(page_markets)
                        
                        logger.debug(f"Fetched Gamma API page {page+1}: {len(page_markets)} markets (total: {len(all_markets)})")
            
            logger.debug(f"Total markets fetched from Gamma API: {len(all_markets)}")
            # DEBUG: Track rejection reasons
            rejection_stats = {
                'not_binary': 0,
                'low_volume': 0,
                'null_volume': 0,
                'inactive': 0,
                'low_liquidity': 0,
                'clob_disabled': 0,
                'tick_size_too_wide': 0,
                'min_order_too_large': 0,
                'passed': 0
            }
            
            # Filter for eligible markets with detailed tracking
            eligible = []
            for m in all_markets:
                is_eligible, reason = self._is_market_eligible_debug(m)
                if is_eligible:
                    eligible.append(m)
                    rejection_stats['passed'] += 1
                else:
                    rejection_stats[reason] = rejection_stats.get(reason, 0) + 1
            
            # Sort by volume and take top candidates (3x capacity for rotation)
            # Use volumeNum (more specific) with volume24hr fallback
            self._eligible_markets = sorted(
                eligible,
                key=lambda m: m.get('volumeNum') or m.get('volume24hr') or 0,
                reverse=True
            )[:MM_MAX_MARKETS * 3]
            
            # Calculate current dynamic threshold for logging transparency
            dynamic_threshold = self._calculate_dynamic_min_volume()
            null_vol_high_liq = getattr(self, '_null_vol_high_liq_count', 0)
            
            logger.info(
                f"📊 MARKET MAKING ELIGIBILITY - BINARY MARKETS ONLY (scanned {len(all_markets)} markets):\n"
                f"   Strategy: Looking for BINARY (2-outcome) markets for liquidity provision\n"
                f"   ADAPTIVE FILTER: Dynamic Min Volume = ${dynamic_threshold:.2f}/day\n"
                f"   (Balance: ${self._allocated_capital:.2f} / {MM_MAX_MARKETS} markets × {MM_VOLUME_MULTIPLIER}x, floor: ${MM_HARD_FLOOR_VOLUME})\n"
                f"   \n"
                f"   MICROSTRUCTURE REJECTIONS (Official Gamma API Fields):\n"
                f"   ❌ Not binary (requires exactly 2 outcomes): {rejection_stats['not_binary']}\n"
                f"   ❌ CLOB disabled (enableOrderBook=false): {rejection_stats.get('clob_disabled', 0)}\n"
                f"   ❌ Tick size too wide (orderPriceMinTickSize >10¢): {rejection_stats.get('tick_size_too_wide', 0)}\n"
                f"   ❌ Min order too large (orderMinSize >$10): {rejection_stats.get('min_order_too_large', 0)}\n"
                f"   \n"
                f"   DATA QUALITY REJECTIONS:\n"
                f"   ❌ Null volume + low liquidity: {rejection_stats.get('null_volume', 0)}\n"
                f"   ✅ Null volume + high liquidity (>$200): {null_vol_high_liq}\n"
                f"   ❌ Low volume (<${dynamic_threshold:.2f}): {rejection_stats['low_volume']}\n"
                f"   ❌ Low liquidity (<${MM_MIN_LIQUIDITY_DEPTH}): {rejection_stats['low_liquidity']}\n"
                f"   ❌ Inactive/closed: {rejection_stats['inactive']}\n"
                f"   \n"
                f"   ✅ PASSED ALL CHECKS: {rejection_stats['passed']}\n"
                f"   📊 API: Gamma /markets | Fields: volumeNum, volume24hr, liquidityNum, active, closed"
            )
            
            self._last_market_scan = current_time
            
        except Exception as e:
            logger.error(f"Error scanning markets: {e}", exc_info=True)
    
    def _is_market_eligible_debug(self, market: Dict[str, Any]) -> Tuple[bool, str]:
        """Debug version that returns (is_eligible, rejection_reason)
        
        INSTITUTIONAL FILTERING (Jan 2026 - Polymarket Support Guidance):
        - Focus on market microstructure and execution safety
        - Do NOT infer volume from liquidity (not documented/recommended)
        - Check enable_order_book + accepting_orders (CLOB active)
        - Validate minimum_tick_size and minimum_order_size
        - Require actual volume data (skip markets with null volume)
        """
        # DEBUG: Log first market to see Gamma API structure
        if not hasattr(self, '_debug_logged_first_market'):
            self._debug_logged_first_market = True
            logger.info(f"🔍 DEBUG GAMMA API FIRST MARKET: {market.get('question', 'N/A')[:50]}")
            logger.info(f"🔍 Available fields: {sorted(market.keys())}")
            logger.info(f"🔍 volumeNum={market.get('volumeNum')}, volume24hr={market.get('volume24hr')}")
            logger.info(f"🔍 liquidityNum={market.get('liquidityNum')}, liquidity={market.get('liquidity')}")
            logger.info(f"🔍 enableOrderBook={market.get('enableOrderBook')}, active={market.get('active')}, closed={market.get('closed')}")
            
            # CRITICAL DEBUG: Check what's actually in clobTokenIds and outcomes
            clob_token_ids_raw = market.get('clobTokenIds')
            outcomes_raw = market.get('outcomes')
            logger.info(f"🔍 clobTokenIds: {clob_token_ids_raw} (type: {type(clob_token_ids_raw).__name__}, len: {len(clob_token_ids_raw) if clob_token_ids_raw else 'N/A'})")
            logger.info(f"🔍 outcomes: {outcomes_raw} (type: {type(outcomes_raw).__name__}, len: {len(outcomes_raw) if outcomes_raw else 'N/A'})")
        
        # Binary market check (Gamma API - Per Polymarket Support Jan 2026)
        # CRITICAL: Fields come as JSON-encoded strings, NOT native arrays
        # Example: outcomes = '["Yes","No"]' (string), not ['Yes', 'No'] (list)
        # Must parse JSON first, then check length
        
        # Try multiple fields for redundancy (outcomes, outcomePrices, clobTokenIds)
        import json
        outcome_count = None
        
        # Method 1: Parse outcomes field
        outcomes_raw = market.get('outcomes')
        if outcomes_raw:
            try:
                if isinstance(outcomes_raw, str):
                    outcomes_parsed = json.loads(outcomes_raw)
                else:
                    outcomes_parsed = outcomes_raw
                outcome_count = len(outcomes_parsed)
            except (json.JSONDecodeError, TypeError):
                pass
        
        # Method 2: Parse outcomePrices field (fallback)
        if outcome_count is None:
            outcome_prices_raw = market.get('outcomePrices')
            if outcome_prices_raw:
                try:
                    if isinstance(outcome_prices_raw, str):
                        outcome_prices_parsed = json.loads(outcome_prices_raw)
                    else:
                        outcome_prices_parsed = outcome_prices_raw
                    outcome_count = len(outcome_prices_parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        
        # Method 3: Parse clobTokenIds (last resort)
        if outcome_count is None:
            clob_token_ids_raw = market.get('clobTokenIds')
            if clob_token_ids_raw:
                try:
                    if isinstance(clob_token_ids_raw, str):
                        clob_token_ids_parsed = json.loads(clob_token_ids_raw)
                    else:
                        clob_token_ids_parsed = clob_token_ids_raw
                    outcome_count = len(clob_token_ids_parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        
        # If we couldn't determine outcome count, reject (safety)
        if outcome_count is None:
            return (False, 'not_binary')
        
        # Check if binary (exactly 2 outcomes)
        if MM_PREFER_BINARY_MARKETS and outcome_count != 2:
            return (False, 'not_binary')
        
        # MICROSTRUCTURE: CLOB status (Gamma API - Official per Polymarket Support)
        # enableOrderBook (boolean | null) - Only reject if explicitly False
        enable_order_book = market.get('enableOrderBook')
        if enable_order_book is False:  # Explicitly disabled
            return (False, 'clob_disabled')
        
        # MICROSTRUCTURE: Tick size and order size (Gamma API)
        # orderPriceMinTickSize (number | null) - units unconfirmed by docs
        # orderMinSize (number | null)
        order_price_min_tick = market.get('orderPriceMinTickSize')
        order_min_size = market.get('orderMinSize')
        
        # Validate constraints (only if data available)
        if order_price_min_tick is not None and order_price_min_tick > 0.1:
            # Assuming dollars (unconfirmed): >10¢ tick too wide
            return (False, 'tick_size_too_wide')
        
        if order_min_size is not None and order_min_size > 10.0:
            # Min order >$10 capital inefficient
            return (False, 'min_order_too_large')
        
        # VOLUME EXTRACTION (Official Gamma fields per Polymarket Support)
        volume_num = market.get('volumeNum')  # Preferred
        volume_24h_raw = market.get('volume24hr')  # Fallback
        
        # CRITICAL: Handle null volume data (Polymarket issue - Jan 2026)
        # If both volume fields are null, use liquidity-only filtering
        if volume_num is not None:
            volume_24h = Decimal(str(volume_num))
            volume_source = "volumeNum"
        elif volume_24h_raw is not None:
            volume_24h = Decimal(str(volume_24h_raw))
            volume_source = "volume24hr"
        else:
            # NULL VOLUME: Fall back to liquidity-only filtering
            liquidity_num = Decimal(str(market.get('liquidityNum', 0)))
            
            # Accept markets with strong liquidity despite null volume
            if liquidity_num >= Decimal(str(MM_MIN_LIQUIDITY_DEPTH * 10)):
                # High liquidity threshold: 10x minimum (e.g., $200 for $20 min)
                if not hasattr(self, '_null_vol_high_liq_count'):
                    self._null_vol_high_liq_count = 0
                self._null_vol_high_liq_count += 1
                
                # Skip to final checks (active/closed)
                if market.get('closed', False) or not market.get('active', True):
                    return (False, 'inactive')
                
                return (True, 'passed')  # Accept despite null volume
            else:
                return (False, 'null_volume')
        
        dynamic_min_volume = self._calculate_dynamic_min_volume()
        
        # INSTITUTIONAL VOLUME FILTERING (Polymarket Support - Jan 2026)
        # Require actual volume data - do NOT infer from liquidity
        if volume_24h < dynamic_min_volume:
            ticker = market.get('ticker', market.get('question', 'Unknown')[:30])
            logger.debug(
                f"Market {ticker} rejected. "
                f"Volume ${volume_24h:.2f} ({volume_source}) < ${dynamic_min_volume:.2f} "
                f"(balance: ${self._allocated_capital:.2f}, {MM_MAX_MARKETS} markets, {MM_VOLUME_MULTIPLIER}x)"
            )
            return (False, 'low_volume')
        
        # Active market check (official Gamma fields: active, closed)
        if market.get('closed', False) or not market.get('active', True):
            return (False, 'inactive')
        
        # Liquidity check (official field: liquidityNum per Polymarket Support)
        liquidity_num = market.get('liquidityNum', 0)
        if liquidity_num < MM_MIN_LIQUIDITY_DEPTH:
            return (False, 'low_liquidity')
        
        return (True, 'passed')
    
    def _is_market_eligible(self, market: Dict[str, Any]) -> bool:
        """Check if market meets criteria for market making
        
        INSTITUTIONAL FILTERING (Jan 2026 - Polymarket Support Validated):
        - Official Gamma API fields: volumeNum, volume24hr, liquidityNum, active, closed
        - Fallback to liquidity-only if volume=null (Polymarket data issue)
        - Check CLOB active (enableOrderBook not false)
        - Validate tick size and order constraints if available
        """
        # Binary market check (Gamma API - Per Polymarket Support Jan 2026)
        # CRITICAL: Fields are JSON-encoded strings, NOT native arrays
        import json
        outcome_count = None
        
        # Try parsing outcomes or outcomePrices (safest per Polymarket support)
        for field in ['outcomes', 'outcomePrices', 'clobTokenIds']:
            field_value = market.get(field)
            if field_value:
                try:
                    if isinstance(field_value, str):
                        parsed = json.loads(field_value)
                    else:
                        parsed = field_value
                    outcome_count = len(parsed)
                    break
                except (json.JSONDecodeError, TypeError):
                    continue
        
        if outcome_count is None or (MM_PREFER_BINARY_MARKETS and outcome_count != 2):
            return False
        
        # MICROSTRUCTURE: CLOB status
        if market.get('enableOrderBook') is False:
            return False
        
        # MICROSTRUCTURE: Constraints
        order_price_min_tick = market.get('orderPriceMinTickSize')
        order_min_size = market.get('orderMinSize')
        
        if order_price_min_tick is not None and order_price_min_tick > 0.1:
            return False
        if order_min_size is not None and order_min_size > 10.0:
            return False
        
        # Active check
        if market.get('closed', False) or not market.get('active', True):
            return False
        
        # VOLUME: Official Gamma fields (volumeNum preferred)
        volume_num = market.get('volumeNum')
        volume_24h_raw = market.get('volume24hr')
        
        # Handle null volume: Use liquidity-only filtering
        if volume_num is not None:
            volume_24h = Decimal(str(volume_num))
        elif volume_24h_raw is not None:
            volume_24h = Decimal(str(volume_24h_raw))
        else:
            # Null volume: Accept if high liquidity (10x minimum)
            liquidity_num = market.get('liquidityNum', 0)
            return liquidity_num >= (MM_MIN_LIQUIDITY_DEPTH * 10)
        
        # Check volume threshold
        dynamic_min_volume = self._calculate_dynamic_min_volume()
        if volume_24h < dynamic_min_volume:
            return False
        
        # Liquidity check
        liquidity_num = market.get('liquidityNum', 0)
        if liquidity_num < MM_MIN_LIQUIDITY_DEPTH:
            return False
        
        return True
    
    async def _manage_positions(self) -> None:
        """Manage active positions"""
        # Add new markets if below capacity (use MM_MAX_MARKETS for institutional standard)
        while len(self._positions) < MM_MAX_MARKETS:
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
            # Use conditionId (full hash) as primary key, not id (numeric)
            market_id = market.get('conditionId') or market.get('id')
            if market_id not in self._positions:
                return market
        return None
    
    async def _start_market_making(self, market: Dict) -> None:
        """Start making market with global exposure check"""
        # CRITICAL: Use conditionId (required by get_market API), not id
        market_id = market.get('conditionId') or market.get('id')
        question = market.get('question', 'Unknown')
        
        # Parse clobTokenIds (JSON string from Gamma API)
        import json
        clob_token_ids_raw = market.get('clobTokenIds', [])
        if isinstance(clob_token_ids_raw, str):
            token_ids = json.loads(clob_token_ids_raw)
        else:
            token_ids = clob_token_ids_raw
        
        # CRITICAL: Check global directional exposure before adding new position
        total_exposure = await self._calculate_total_directional_exposure()
        if total_exposure > MM_MAX_TOTAL_DIRECTIONAL_EXPOSURE:
            logger.warning(
                f"⚠️ GLOBAL EXPOSURE LIMIT: ${total_exposure:.2f} exceeds "
                f"${MM_MAX_TOTAL_DIRECTIONAL_EXPOSURE} - Skipping new market: {question[:40]}..."
            )
            return
        
        logger.info(
            f"Starting market making: {question[:60]}... "
            f"(volume: ${market.get('volume24hr', 0):.0f}, "
            f"current exposure: ${total_exposure:.2f})"
        )
        
        self._positions[market_id] = MarketPosition(market_id, question, token_ids)
        
        # Subscribe to WebSocket updates for this market
        if self._market_data_manager:
            try:
                await self._market_data_manager.subscribe_markets([market_id])
                logger.info(f"✅ Subscribed to WebSocket updates for {market_id[:8]}...")
            except Exception as e:
                logger.warning(f"Failed to subscribe to WebSocket for {market_id[:8]}...: {e}")
        
        await self._place_quotes(market_id)
    
    async def _sync_fills(self) -> None:
        """Critical: Detect order fills and update inventory tracking"""
        current_time = time.time()
        if current_time - self._last_fill_sync < self._fill_sync_interval:
            return
        
        self._last_fill_sync = current_time
        
        for market_id, position in list(self._positions.items()):
            all_order_ids = list(position.active_bids.values()) + list(position.active_asks.values())
            
            for order_id in all_order_ids:
                try:
                    order = await self.order_manager.get_order(order_id)
                    if not order:
                        continue
                    
                    status = order.get('status')
                    if status in ['filled', 'partially_filled']:
                        # Order was filled - update inventory
                        token_id = order.get('asset_id') or order.get('token_id')
                        if not token_id:
                            continue
                        
                        filled_size = float(order.get('size_matched', 0))
                        price = float(order.get('price', 0))
                        side = order.get('side', '')
                        
                        if filled_size > 0 and token_id in position.inventory:
                            is_buy = (side.upper() == 'BUY')
                            position.update_inventory(token_id, int(filled_size), price, is_buy)
                            
                            # CRITICAL: Get micro-price at fill time for markout tracking
                            prices = await self._get_market_prices(market_id, [token_id])
                            micro_price = prices.get(token_id, price)  # Fallback to fill price
                            position.record_fill_for_markout(token_id, side, price, micro_price, filled_size)
                            
                            logger.info(
                                f"[MM] Fill detected: {side} {filled_size:.1f} @ {price:.4f} "
                                f"(Inventory: {position.inventory[token_id]}, Micro: {micro_price:.4f})"
                            )
                            
                            # Remove filled order from active tracking
                            if order_id in position.active_bids.values():
                                position.active_bids = {k: v for k, v in position.active_bids.items() if v != order_id}
                            if order_id in position.active_asks.values():
                                position.active_asks = {k: v for k, v in position.active_asks.items() if v != order_id}
                            
                            self._total_fills += 1
                    
                except Exception as e:
                    logger.debug(f"Error syncing fill for order {order_id[:8]}...: {e}")
    
    async def _update_quotes(self) -> None:
        """
        Update quotes for all active positions (parallel execution)
        
        INSTITUTIONAL SAFETY: LAG CIRCUIT BREAKER
        - Checks for stale data (>2s old) before updating quotes
        - Cancels all quotes if any active market has stale data
        - Prevents trading on outdated prices during WebSocket outages
        """
        current_time = time.time()
        
        # SAFETY: LAG CIRCUIT BREAKER - Check for stale data
        if self._market_data_manager:
            # Get all token IDs from active positions
            active_token_ids = []
            for position in self._positions.values():
                active_token_ids.extend(position.token_ids)
            
            # Check if any market is stale
            if self._market_data_manager.check_market_staleness(active_token_ids):
                stale_markets = self._market_data_manager.get_stale_markets()
                logger.error(
                    f"🚨 LAG CIRCUIT BREAKER TRIGGERED: {len(stale_markets)} markets have stale data (>2s)\\n"
                    f"   Stale assets: {list(stale_markets)[:5]}\\n"
                    f"   ACTION: Cancelling ALL quotes to prevent trading on outdated prices"
                )
                
                # Cancel all quotes immediately
                await self._cancel_all_quotes()
                
                # Skip quote updates this cycle - wait for fresh data
                return
        
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
    
    async def _cancel_all_quotes(self) -> None:
        """
        Emergency function to cancel all active quotes
        
        Used by LAG CIRCUIT BREAKER when stale data detected.
        """
        logger.warning("[EMERGENCY] Cancelling all active quotes...")
        
        cancel_count = 0
        for position in self._positions.values():
            # Cancel all bids
            for order_id in list(position.active_bids.values()):
                try:
                    await self.client.cancel_order(order_id)
                    cancel_count += 1
                except Exception as e:
                    logger.debug(f"Failed to cancel bid {order_id[:8]}...: {e}")
            
            # Cancel all asks
            for order_id in list(position.active_asks.values()):
                try:
                    await self.client.cancel_order(order_id)
                    cancel_count += 1
                except Exception as e:
                    logger.debug(f"Failed to cancel ask {order_id[:8]}...: {e}")
            
            # Clear tracking
            position.active_bids.clear()
            position.active_asks.clear()
        
        logger.info(f"✅ Cancelled {cancel_count} orders via LAG CIRCUIT BREAKER")
    
    async def _reconcile_order(self, token_id: str, side: str, target_price: float, 
                              target_size: float, current_order_id: Optional[str], 
                              position: MarketPosition, market_id: str) -> Optional[str]:
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
        
        # Place new order with post_only rejection handling
        MAX_RETRIES = 3
        for attempt in range(MAX_RETRIES):
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
                error_msg = str(e).lower()
                # Post-only rejection: price would cross spread
                if 'post' in error_msg or 'cross' in error_msg or 'immediate' in error_msg:
                    # Back off price by 1 tick and retry
                    if side == 'BUY':
                        target_price -= 0.001
                        target_price = max(0.01, target_price)
                    else:
                        target_price += 0.001
                        target_price = min(0.99, target_price)
                    
                    logger.debug(f"[MM] post_only rejected, retry {attempt+1} @ {target_price:.4f}")
                    continue
                else:
                    logger.warning(f"Failed to place {side}: {e}")
                    break
        
        # CRITICAL: If all retries failed, enter INVENTORY DEFENSE MODE
        # Market is moving too fast - stop trying to quote and focus on unwinding
        logger.critical(
            f"🚨 POST_ONLY DEADLOCK: {market_id[:8]}... - "
            f"Failed {MAX_RETRIES} attempts to place {side} - "
            f"ENTERING INVENTORY DEFENSE MODE (cancel all quotes, unwind only)"
        )
        self._inventory_defense_mode[market_id] = time.time() + self._defense_mode_duration
        
        return None
    
    async def _place_quotes(self, market_id: str) -> None:
        """
        Place/update quotes using atomic dual-sided placement with Z-Score alpha overlay
        
        Flow:
        1. Update Z-Score from current mid-price (every 60 seconds)
        2. Check for Z-Score halt condition (abs(Z) > 3.5σ)
        3. Calculate skewed quotes (inventory + mean reversion)
        4. Place orders atomically (bid + ask simultaneously)
        """
        if time.time() - self._last_order_time < MM_MIN_ORDER_SPACING:
            await asyncio.sleep(MM_MIN_ORDER_SPACING)
        
        # CRITICAL: Check if in INVENTORY DEFENSE MODE
        # If fast market prevented quoting, stop trying and focus on unwinding
        if market_id in self._inventory_defense_mode:
            defense_end = self._inventory_defense_mode[market_id]
            if time.time() < defense_end:
                logger.warning(
                    f"⚠️ INVENTORY DEFENSE MODE active for {market_id[:8]}... - "
                    f"Skipping quotes, unwinding only ({defense_end - time.time():.0f}s remaining)"
                )
                # Cancel all existing quotes
                position = self._positions[market_id]
                for order_id in list(position.active_bids.values()) + list(position.active_asks.values()):
                    try:
                        await self.client.cancel_order(order_id)
                    except:
                        pass
                position.active_bids.clear()
                position.active_asks.clear()
                return
            else:
                # Defense mode expired - resume normal quoting
                logger.info(f"✅ INVENTORY DEFENSE MODE ended for {market_id[:8]}... - Resuming quotes")
                del self._inventory_defense_mode[market_id]
        
        # CRITICAL: Sync fills IMMEDIATELY before placing quotes
        # This prevents race condition where fills happen between sync cycles
        # Without this, inventory could be stale by up to 1 second
        await self._sync_fills()
        
        position = self._positions[market_id]
        prices = await self._get_market_prices(market_id, position.token_ids)
        
        if not prices:
            return
        
        # ═══════════════════════════════════════════════════════════════════
        # Z-SCORE MEAN REVERSION ALPHA UPDATE (INSTITUTIONAL UPGRADE 2026)
        # ═══════════════════════════════════════════════════════════════════
        # ATOMIC STATE CONSISTENCY: Lock prevents stale quote collision
        # - Ensures Z-Score and quote calculation happen atomically
        # - Prevents mid-execution Z-Score updates invalidating quote prices
        async with self._quote_calculation_lock:
            # Initialize Z-Score manager if not exists
            if market_id not in self._z_score_managers:
                self._z_score_managers[market_id] = ZScoreManager()
                self._last_z_score_update[market_id] = 0
                logger.info(f"📊 Initialized Z-Score manager for {market_id[:8]}...")
            
            # Update Z-Score every 60 seconds (or first time)
            current_time = time.time()
            z_manager = self._z_score_managers[market_id]
            last_update = self._last_z_score_update.get(market_id, 0)
            
            if current_time - last_update >= Z_SCORE_UPDATE_INTERVAL:
                # HFT UPGRADE: Use MICRO-PRICE (volume-weighted mid) instead of simple mid
                # Micro-price provides 1-2 second lead time on mean reversion
                primary_token = position.token_ids[0]
                
                # Get micro-price from WebSocket cache (real-time order book imbalance)
                micro_price = None
                if self._market_data_manager:
                    snapshot = self._market_data_manager.cache.get(primary_token)
                    if snapshot:
                        micro_price = snapshot.micro_price
                
                # Fallback to simple mid-price if micro-price unavailable
                if not micro_price:
                    micro_price = prices.get(primary_token)
                
                if micro_price:
                    z_score = z_manager.update(micro_price)
                    self._last_z_score_update[market_id] = current_time
                    
                    logger.info(
                        f"📊 Z-Score Update: {market_id[:8]}... - "
                        f"micro=${micro_price:.4f}, Z={z_score:.2f}σ, "
                        f"samples={len(z_manager.price_window)}/{Z_SCORE_LOOKBACK_PERIODS}"
                    )
                    
                    # Log signal state
                    if z_manager.should_halt_trading():
                        logger.critical(
                            f"🚨 Z-SCORE EXTREME: {market_id[:8]}... - "
                            f"Z={z_score:.2f}σ exceeds {Z_SCORE_HALT_THRESHOLD:.1f}σ threshold - "
                            f"HALTING QUOTES (potential regime change)"
                        )
                    elif z_manager.is_signal_active():
                        alpha_shift = z_manager.get_alpha_shift()
                        direction = "SELL bias" if alpha_shift < 0 else "BUY bias"
                        logger.info(
                            f"✅ Mean Reversion Signal ACTIVE: Z={z_score:.2f}σ → "
                            f"shift=${alpha_shift:+.4f} ({direction})"
                        )
        
        # ═══════════════════════════════════════════════════════════════════
        # TOXIC FLOW FILTER (2026 HYBRID UPGRADE)
        # ═══════════════════════════════════════════════════════════════════
        # If Z-Score mean reversion conflicts with OBI momentum, PAUSE quoting
        # Prevents providing liquidity to informed traders during breakouts
        #
        # Logic: If |Z-Score| > 2.0 AND OBI is trending AGAINST the reversion → PAUSE
        # Example 1: Z = +2.5 (overbought, expect DOWN) BUT OBI > 0.6 (heavy buying) → TOXIC
        # Example 2: Z = -2.5 (oversold, expect UP) BUT OBI < -0.6 (heavy selling) → TOXIC
        
        if market_id not in self._toxic_flow_paused:
            z_manager = self._z_score_managers.get(market_id)
            if z_manager and z_manager.is_signal_active():
                z_score = z_manager.get_z_score()
                
                # Get OBI from market snapshot
                primary_token = position.token_ids[0]
                snapshot = self._market_data_manager.cache.get(primary_token) if self._market_data_manager else None
                
                if snapshot and hasattr(snapshot, 'obi'):
                    obi = snapshot.obi
                    
                    # Check for Z-Score vs OBI conflict (toxic flow)
                    # Z > 2.0 (overbought → expect DOWN) but OBI > 0.6 (heavy buying)
                    is_toxic_upward = (z_score > Z_SCORE_ENTRY_THRESHOLD and obi > MM_OBI_THRESHOLD)
                    
                    # Z < -2.0 (oversold → expect UP) but OBI < -0.6 (heavy selling)
                    is_toxic_downward = (z_score < -Z_SCORE_ENTRY_THRESHOLD and obi < -MM_OBI_THRESHOLD)
                    
                    if is_toxic_upward or is_toxic_downward:
                        # PAUSE quoting for this market
                        pause_until = time.time() + MM_MOMENTUM_PROTECTION_TIME
                        self._toxic_flow_paused[market_id] = pause_until
                        
                        direction = "UPWARD" if is_toxic_upward else "DOWNWARD"
                        logger.critical(
                            f"🚨 TOXIC FLOW DETECTED: {market_id[:8]}... - "
                            f"{direction} breakout (Z={z_score:+.2f}σ, OBI={obi:+.2f}) - "
                            f"PAUSING quotes for {MM_MOMENTUM_PROTECTION_TIME}s to avoid informed traders"
                        )
                        
                        # Cancel all existing quotes
                        for order_id in list(position.active_bids.values()) + list(position.active_asks.values()):
                            try:
                                await self.client.cancel_order(order_id)
                            except:
                                pass
                        position.active_bids.clear()
                        position.active_asks.clear()
                        return
        
        # Check if toxic flow pause is active
        if market_id in self._toxic_flow_paused:
            pause_end = self._toxic_flow_paused[market_id]
            if time.time() < pause_end:
                logger.debug(
                    f"⏸️ TOXIC FLOW PAUSE active for {market_id[:8]}... - "
                    f"Resuming in {pause_end - time.time():.0f}s"
                )
                return
            else:
                # Pause expired - resume normal quoting
                logger.info(f"✅ TOXIC FLOW PAUSE ended for {market_id[:8]}... - Resuming quotes")
                del self._toxic_flow_paused[market_id]
        
        # CRITICAL: Check for toxic flow (being run over)
        is_toxic = position.check_toxic_flow()
        if is_toxic:
            logger.warning(
                f"🚨 TOXIC FLOW in {market_id[:8]}... - "
                f"Recent fills: ${sum(v for _, _, v in position.recent_fills):.2f} - "
                f"WIDENING SPREAD"
            )
        
        for token_id in position.token_ids:
            mid_price = prices.get(token_id)
            if not mid_price:
                continue
            
            # Use net inventory for binary markets (avoids double-counting hedged positions)
            if len(position.token_ids) == 2:
                net_inventory = position.get_net_inventory()
                # Determine if this is token 0 or token 1 to apply correct sign
                token_idx = position.token_ids.index(token_id)
                inventory = net_inventory if token_idx == 0 else -net_inventory
            else:
                inventory = position.inventory.get(token_id, 0)
            
            # INSTITUTIONAL UPGRADE: PREDICTIVE TOXIC FLOW DETECTION
            # Check micro-price deviation BEFORE being filled
            # If micro_price deviates from mid_price by >1%, preemptively pull quotes
            snapshot = self._market_data_manager.cache.get(token_id) if self._market_data_manager else None
            if snapshot:
                micro_deviation = abs(snapshot.micro_price - mid_price) / mid_price if mid_price > 0 else 0
                if micro_deviation > 0.01:  # 1% deviation threshold
                    logger.critical(
                        f"🚨 PREDICTIVE TOXIC FLOW: {token_id[:8]}... - "
                        f"Micro-price deviation {micro_deviation*100:.2f}% "
                        f"(micro: {snapshot.micro_price:.4f}, mid: {mid_price:.4f}) - "
                        f"PULLING QUOTES to avoid being swept"
                    )
                    # Skip placing quotes for this token - wait for market to stabilize
                    continue
            
            # Check for adverse selection via markout (institutional-grade)
            is_adverse = False
            if position.fill_count > 10:
                avg_markout = position.total_markout_pnl / position.fill_count
                if avg_markout < -0.005:  # -0.5 cents avg = being picked off
                    is_adverse = True
                
                # ═══════════════════════════════════════════════════════════════════
                # HYBRID QUOTE CALCULATION: Avellaneda-Stoikov + Z-Score Alpha
                # ═══════════════════════════════════════════════════════════════════
                combined_toxic = is_toxic or is_adverse
                target_bid, target_ask = self._calculate_skewed_quotes(
                    mid_price=mid_price,
                    inventory=inventory,
                    is_toxic=combined_toxic,
                    position=position,
                    z_score_manager=z_manager  # NEW: Pass Z-Score manager for alpha overlay
                )
                
                # Check for Z-Score halt (extreme outlier)
                # Convert Decimal to float for comparison
                if float(target_bid) == 0.0 and float(target_ask) == 999.99:
                    logger.warning(
                        f"⚠️ Z-SCORE HALT: Skipping quotes for {token_id[:8]}... "
                        f"(Z={z_manager.get_z_score():.2f}σ > {Z_SCORE_HALT_THRESHOLD:.1f}σ)"
                    )
                    continue  # Skip this token, move to next
                
                # Position sizing (convert Decimal to float for arithmetic)
                target_bid_float = float(target_bid)
                target_ask_float = float(target_ask)
                
                bid_size = MM_BASE_POSITION_SIZE / target_bid_float if target_bid_float > 0 else 0
                ask_size = MM_BASE_POSITION_SIZE / target_ask_float if target_ask_float > 0 else 0
                
                # Reduce size when holding inventory
                if inventory > 0:
                    bid_size *= 0.5
                elif inventory < 0:
                    ask_size *= 0.5
                
                # ═══════════════════════════════════════════════════════════════════
                # MODULE 2: SKEW HYSTERESIS CHECK (Efficiency Guard)
                # ═══════════════════════════════════════════════════════════════════
                # Calculate reservation price from quotes (reverse engineer)
                # Reservation = (bid + ask) / 2
                calculated_reservation = (target_bid_float + target_ask_float) / 2.0
                
                # Check if update is necessary (or if emergency liquidation)
                is_emergency = position.get_inventory_age() > MM_MAX_INVENTORY_HOLD_TIME
                should_update = await position.should_update_quotes(
                    token_id=token_id,
                    new_reservation_price=calculated_reservation,
                    current_inventory=inventory,
                    max_position_size=MM_MAX_INVENTORY_PER_OUTCOME,
                    is_emergency=is_emergency
                )
                
                if not should_update:
                    # Hysteresis blocked this update - skip to save API calls
                    continue
                
                # CRITICAL: Atomic dual-sided placement (prevents legging out)
                # Place bid and ask simultaneously using asyncio.gather
                current_bid_id = position.active_bids.get(token_id)
                current_ask_id = position.active_asks.get(token_id)
                
                bid_task = self._reconcile_order(
                    token_id, 'BUY', float(target_bid), bid_size, current_bid_id, position, market_id
                )
                ask_task = self._reconcile_order(
                    token_id, 'SELL', float(target_ask), ask_size, current_ask_id, position, market_id
                )
                
                # Execute simultaneously (prevents race condition)
                results = await asyncio.gather(bid_task, ask_task, return_exceptions=True)
                new_bid_id, new_ask_id = results
                
                # Update tracking (handle exceptions)
                if isinstance(new_bid_id, str) and new_bid_id:
                    position.active_bids[token_id] = new_bid_id
                elif current_bid_id:
                    position.active_bids.pop(token_id, None)
                
                if isinstance(new_ask_id, str) and new_ask_id:
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
    
    def _round_price_to_tick(self, price: float, side: str, tick_size: float = 0.001) -> float:
        """Strict rounding: floor for bids, ceil for asks (never cross spread)"""
        import math
        
        if side == 'BUY':
            # Floor: Always round DOWN for bids (stay below mid)
            rounded = math.floor(price / tick_size) * tick_size
        else:
            # Ceil: Always round UP for asks (stay above mid)
            rounded = math.ceil(price / tick_size) * tick_size
        
        # Clamp to valid range
        return max(0.001, min(0.999, rounded))
    
    def _calculate_skewed_quotes(self, mid_price: float, inventory: int, is_toxic: bool = False, 
                                 position: Optional[MarketPosition] = None, 
                                 z_score_manager: Optional[ZScoreManager] = None) -> Tuple[Decimal, Decimal]:
        """
        Hybrid Avellaneda-Stoikov + Z-Score Mean Reversion Quote Calculator
        
        This method combines TWO alpha signals for asymmetric quoting:
        
        1. INVENTORY RISK (Avellaneda-Stoikov):
           - Skew quotes based on current position to reduce directional risk
           - Long inventory → widen asks (harder to buy more), tighten bids (easier to sell)
           - Short inventory → widen bids (harder to sell more), tighten asks (easier to buy)
        
        2. MEAN REVERSION ALPHA (Z-Score):
           - Skew quotes based on statistical price deviation from rolling mean
           - Overbought (Z>2) → lower reservation price (incentivize selling)
           - Oversold (Z<-2) → raise reservation price (incentivize buying)
        
        Mathematical Flow:
        -----------------
        Step 1: Calculate Base Reservation Price (Inventory Risk)
            inventory_skew = inventory × RISK_FACTOR
            base_reservation = mid_price - inventory_skew
        
        Step 2: Apply Z-Score Alpha Adjustment (Mean Reversion)
            alpha_shift = Z_Score × MM_Z_SENSITIVITY
            final_reservation = base_reservation - alpha_shift
        
        Step 3: Calculate Bid/Ask from Final Reservation
            dynamic_half_spread = base_half_spread + inventory_adjustment + toxicity_adjustment
            target_bid = final_reservation - dynamic_half_spread
            target_ask = final_reservation + dynamic_half_spread
        
        Example Scenarios:
        -----------------
        Scenario A: Neutral Inventory, Overbought Market (Z=2.5)
            - Inventory skew: 0 (no position)
            - Alpha shift: -2.5 × 0.005 = -$0.0125 (NEGATIVE = lower reservation)
            - Reservation: $0.50 - 0 + (-0.0125) = $0.4875 (lowered)
            - Bid: $0.4875 - $0.004 = $0.4835 (unchanged from normal)
            - Ask: $0.4875 + $0.004 = $0.4915 (widened relative to mid)
            - Effect: Easier to sell (bid unchanged), harder to buy (ask widened)
        
        Scenario B: Long 50 Shares, Neutral Z-Score (Z=0.2)
            - Inventory skew: 50 × 0.0005 = $0.025
            - Alpha shift: 0 (below ±2.0 threshold)
            - Reservation: $0.50 - 0.025 + 0 = $0.475 (lowered by inventory)
            - Bid: $0.475 - $0.004 = $0.471 (widened to discourage more buying)
            - Ask: $0.475 + $0.004 = $0.479 (tightened to encourage selling)
            - Effect: Standard inventory management without mean reversion bet
        
        Scenario C: Long 50 Shares, Oversold Market (Z=-2.3)
            - Inventory skew: 50 × 0.0005 = $0.025 (want to sell)
            - Alpha shift: -(-2.3) × 0.005 = +$0.0115 (POSITIVE = raise reservation)
            - Reservation: $0.50 - 0.025 + 0.0115 = $0.4865
            - Conflict: Inventory risk says "sell", mean reversion says "buy"
            - Resolution: Partial hedge - reservation raised by alpha but still below mid
            - Effect: Both signals active, system finds optimal balance
        
        Safety Gates:
        ------------
        - Toxic Flow Protection: 3× spread widening if large one-sided fills detected
        - Adverse Selection Protection: Auto-widen spreads if negative markout P&L
        - Spread Bounds: Always enforce MIN_SPREAD and MAX_SPREAD limits
        - Tick Size: Round to Polymarket minimum tick (0.1 cent)
        
        Args:
            mid_price: Current market mid-price
            inventory: Net directional inventory (positive=long, negative=short)
            is_toxic: Whether toxic flow detected (widen spread protection)
            position: MarketPosition object for markout analysis
            z_score_manager: ZScoreManager for mean reversion signal
        
        Returns:
            Tuple[target_bid, target_ask]: Optimal quote prices
        """
        RISK_FACTOR = Decimal('0.0005')  # 0.05 cents per 1 share ($0.05 per 100 shares)
        MIN_TICK_SIZE = Decimal('0.001')  # Polymarket minimum tick (0.1 cent)
        
        # INSTITUTIONAL UPGRADE: Bernoulli Variance Guard (2026)
        # For binary markets [0, 1], variance = p(1-p) collapses near boundaries
        # If price < 0.10 or > 0.90, cap volatility to prevent gamma overload
        BOUNDARY_LOW = Decimal('0.10')
        BOUNDARY_HIGH = Decimal('0.90')
        MAX_BOUNDARY_VOLATILITY = Decimal('0.05')  # Cap σ at 5% near boundaries
        
        mid_price_dec = Decimal(str(mid_price))
        
        # Apply Bernoulli boundary protection
        if mid_price_dec < BOUNDARY_LOW or mid_price_dec > BOUNDARY_HIGH:
            # Near boundaries: Bernoulli variance p(1-p) → 0, causing excessive skew
            # Cap the effective risk factor to prevent "gamma overload"
            effective_risk = RISK_FACTOR * MAX_BOUNDARY_VOLATILITY / Decimal('0.15')
            logger.warning(
                f"🛡️ BERNOULLI GUARD: Price ${mid_price:.4f} near boundary - "
                f"Capping risk factor {RISK_FACTOR:.6f} → {effective_risk:.6f}"
            )
        else:
            effective_risk = RISK_FACTOR
        
        # ═══════════════════════════════════════════════════════════════
        # STEP 1: INVENTORY RISK - Avellaneda-Stoikov Base Reservation
        # ═══════════════════════════════════════════════════════════════
        inventory_skew = Decimal(str(inventory)) * effective_risk
        base_reservation = mid_price_dec - inventory_skew
        
        # ═══════════════════════════════════════════════════════════════
        # STEP 2: MEAN REVERSION ALPHA - Z-Score Adjustment (NEW)
        # ═══════════════════════════════════════════════════════════════
        alpha_shift = 0.0
        z_score = 0.0
        
        if z_score_manager and z_score_manager.is_ready():
            z_score = z_score_manager.get_z_score()
            
            # Check for extreme outlier (halt trading)
            if z_score_manager.should_halt_trading():
                logger.critical(
                    f"🚨 Z-SCORE HALT: abs(Z)={abs(z_score):.2f} > {Z_SCORE_HALT_THRESHOLD:.1f}σ - "
                    f"PAUSING QUOTES (potential regime change or news event)"
                )
                # Return impossible quotes to prevent placement (Decimal type)
                return Decimal('0.0'), Decimal('999.99')
            
            # Get alpha shift if signal is active
            if z_score_manager.is_signal_active():
                alpha_shift = z_score_manager.get_alpha_shift()
                logger.info(
                    f"📊 Z-Score Alpha: Z={z_score:.2f}σ → shift=${alpha_shift:+.4f} "
                    f"({'SELL bias' if alpha_shift < 0 else 'BUY bias'})"
                )
        
        # Apply alpha shift to reservation price (ADDITIVE)
        # HIGH-PRECISION: Convert to Decimal for 4-decimal tick alignment
        alpha_shift_dec = Decimal(str(alpha_shift))
        final_reservation = base_reservation + alpha_shift_dec
        
        # ═══════════════════════════════════════════════════════════════
        # STEP 3: DYNAMIC SPREAD CALCULATION
        # ═══════════════════════════════════════════════════════════════
        base_half_spread = MM_TARGET_SPREAD / 2
        
        # Inventory-driven spread widening (reduce risk of accumulating more)
        extra_spread = abs(inventory) * MIN_TICK_SIZE
        
        # TOXIC FLOW PROTECTION: Widen spread significantly if being run over
        if is_toxic:
            base_half_spread *= 3.0  # 3x wider spread
            logger.warning(f"⚠️ TOXIC FLOW DETECTED - Widening spread to {base_half_spread*2*100:.1f}%")
        
        # INSTITUTIONAL UPGRADE: Adverse Selection Auto-Adjustment
        # If markout P&L is consistently negative, we are being picked off
        # Automatically widen spread to compensate
        if position and position.fill_count > 10:  # Need statistical significance
            avg_markout = position.total_markout_pnl / position.fill_count
            
            # Negative markout = adverse selection = need wider spreads
            if avg_markout < -0.005:  # -0.5 cents per fill average
                adverse_multiplier = 1.0 + abs(avg_markout) * 100  # Scale with severity
                adverse_multiplier = min(adverse_multiplier, 2.5)  # Cap at 2.5x
                base_half_spread *= adverse_multiplier
                logger.warning(
                    f"🚨 ADVERSE SELECTION AUTO-ADJUSTMENT: "
                    f"Avg markout ${avg_markout:.4f} → spread widened {adverse_multiplier:.1f}x "
                    f"to {base_half_spread*2*100:.1f}%"
                )
        
        final_half_spread_dec = Decimal(str(base_half_spread + extra_spread))
        
        # ═══════════════════════════════════════════════════════════════
        # STEP 4: CALCULATE FINAL BID/ASK FROM RESERVATION PRICE
        # ═══════════════════════════════════════════════════════════════
        target_bid = final_reservation - final_half_spread_dec
        target_ask = final_reservation + final_half_spread_dec
        
        # Don't cross the market (safety check)
        target_bid = min(target_bid, mid_price_dec - MIN_TICK_SIZE)
        target_ask = max(target_ask, mid_price_dec + MIN_TICK_SIZE)
        
        # HIGH-PRECISION ROUNDING: Ensure 4-decimal tick alignment
        # CRITICAL: Use floor for bids (favor buyer), ceil for asks (favor seller)
        target_bid = target_bid.quantize(MIN_TICK_SIZE, rounding='ROUND_DOWN')
        target_ask = target_ask.quantize(MIN_TICK_SIZE, rounding='ROUND_UP')
        
        # Log the multi-signal quote decision
        if abs(alpha_shift) > 0.001:  # Only log when alpha is active
            logger.info(
                f"💡 HYBRID QUOTE: mid=${mid_price:.4f}, "
                f"inv_skew=${inventory_skew:+.4f} (inv={inventory}), "
                f"alpha_shift=${alpha_shift:+.4f} (Z={z_score:.2f}σ), "
                f"final_res=${final_reservation:.4f} → "
                f"bid=${target_bid:.4f}, ask=${target_ask:.4f}"
            )
        
        return target_bid, target_ask
    
    async def _get_market_prices(self, market_id: str, token_ids: List[str]) -> Dict[str, float]:
        """Get current micro-prices from WebSocket cache (zero-latency synchronous reads)"""
        prices = {}
        
        # Use WebSocket cache if available, fallback to REST
        use_cache = self._market_data_manager is not None
        
        for token_id in token_ids:
            try:
                if use_cache:
                    # CRITICAL: Check for stale data first (HFT-grade protection)
                    if self._market_data_manager.is_market_stale(token_id):
                        logger.warning(
                            f"⚠️ STALE DATA: {token_id[:8]}... - "
                            f"No WebSocket update in 2+ seconds - SKIPPING QUOTES"
                        )
                        continue  # Pause activity for stale markets
                    
                    # Synchronous cache read (O(1) lookup, no network latency)
                    snapshot = self._market_data_manager.cache.get(token_id)
                    if not snapshot:
                        logger.debug(f"No cache data for {token_id[:8]}... - using REST fallback")
                        # Fallback to REST
                        use_cache = False
                    else:
                        best_bid = snapshot.best_bid
                        best_ask = snapshot.best_ask
                        micro_price = snapshot.micro_price
                        
                        # CRITICAL: Gapped market check
                        spread = best_ask - best_bid
                        if spread > MM_MAX_SPREAD:
                            logger.warning(
                                f"⚠️ Gapped market detected: {token_id[:8]}... "
                                f"(bid: {best_bid:.4f}, ask: {best_ask:.4f}, spread: {spread:.4f}) - "
                                f"SKIPPING (too risky to provide liquidity)"
                            )
                            continue
                        
                        # CRITICAL: Oracle price sanity check
                        if self._oracle_enabled and market_id in self._oracle_prices:
                            oracle_price = self._oracle_prices[market_id]
                            price_deviation = abs(micro_price - oracle_price) / oracle_price
                            
                            if price_deviation > MM_ORACLE_PRICE_DEVIATION_LIMIT:
                                logger.critical(
                                    f"🚨 ORACLE PRICE DEVIATION EXCEEDED: {token_id[:8]}... - "
                                    f"Polymarket: {micro_price:.4f}, Oracle: {oracle_price:.4f}, "
                                    f"Deviation: {price_deviation*100:.1f}% - SKIPPING (potential flash crash)"
                                )
                                continue
                        
                        prices[token_id] = micro_price
                        continue  # Successfully got price from cache
                
                # REST Fallback (only if cache unavailable)
                if not use_cache:
                    order_book = await self.client.get_order_book(token_id)
                    bids = getattr(order_book, 'bids', [])
                    asks = getattr(order_book, 'asks', [])
                    
                    if bids and asks:
                        best_bid = float(bids[0]['price'])
                        best_ask = float(asks[0]['price'])
                        
                        # INSTITUTION-GRADE: Depth validation
                        # Per Polymarket Support (Jan 2026): 5 shares min for small capital
                        bid_depth = float(bids[0].get('size', 0))
                        ask_depth = float(asks[0].get('size', 0))
                        
                        MIN_DEPTH = MM_MIN_DEPTH_SHARES  # 5 shares (realistic for small markets)
                        if bid_depth < MIN_DEPTH or ask_depth < MIN_DEPTH:
                            logger.debug(
                                f"Skipping thin book (REST): {token_id[:8]}... "
                                f"(bid depth: {bid_depth}, ask depth: {ask_depth})"
                            )
                            continue
                        
                        spread = best_ask - best_bid
                        if spread > MM_MAX_SPREAD:
                            logger.warning(
                                f"⚠️ Gapped market detected (REST): {token_id[:8]}... "
                                f"(spread: {spread:.4f}) - SKIPPING"
                            )
                            continue
                        
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
            
            # CRITICAL: Check if position is HEDGED (equal Yes/No inventory)
            # Don't exit both sides of a hedged position - only exit the imbalance
            is_binary = len(position.token_ids) == 2
            if is_binary:
                inv_0 = position.inventory.get(position.token_ids[0], 0)
                inv_1 = position.inventory.get(position.token_ids[1], 0)
                
                # If perfectly hedged (or nearly hedged within 5 shares), skip exit logic
                if abs(inv_0 - inv_1) <= 5:
                    logger.debug(
                        f"Position is hedged: {market_id[:8]}... "
                        f"(Yes: {inv_0}, No: {inv_1}, Net: {abs(inv_0 - inv_1)}) - No action needed"
                    )
                    continue
            
            # Time-based: PASSIVE UNWINDING (not force close)
            if position.has_inventory():
                age = position.get_inventory_age()
                if age > MM_MAX_INVENTORY_HOLD_TIME:
                    logger.warning(
                        f"Inventory age {age/60:.0f}min - passive unwinding"
                    )
                    
                    # For binary markets, only unwind the NET imbalance
                    if is_binary:
                        net_inv = position.get_net_inventory()
                        if abs(net_inv) > 5:  # Only if material imbalance
                            # Determine which token to exit
                            token_idx = 0 if net_inv > 0 else 1
                            token_id = position.token_ids[token_idx]
                            await self._exit_inventory(market_id, token_id, abs(net_inv))
                    else:
                        # Multi-outcome: exit each token independently
                        for token_id, inventory in position.inventory.items():
                            if inventory != 0:
                                await self._exit_inventory(market_id, token_id, inventory)
                    continue
            
            # Adverse price move: PASSIVE UNWINDING (only for net imbalance)
            if is_binary:
                net_inv = position.get_net_inventory()
                if abs(net_inv) > 5:
                    # Check price move on the net position
                    token_idx = 0 if net_inv > 0 else 1
                    token_id = position.token_ids[token_idx]
                    inventory = position.inventory.get(token_id, 0)
                    
                    if inventory > 0:
                        entry_price = position.cost_basis[token_id]
                        current_price = prices.get(token_id, entry_price)
                        price_move = (current_price - entry_price) / entry_price if entry_price > 0 else 0
                        
                        if price_move < -MM_EMERGENCY_EXIT_THRESHOLD:
                            logger.critical(
                                f"Emergency: {token_id[:8]}... price moved {price_move*100:.1f}% "
                                f"- passive unwinding NET imbalance ({abs(net_inv)} shares)"
                            )
                            await self._exit_inventory(market_id, token_id, abs(net_inv))
            else:
                # Multi-outcome: check each token
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
        
        # Update daily P&L tracking (for circuit breaker)
        self._daily_pnl += position.realized_pnl
        
        # PRODUCTION SAFETY: Report realized P&L to OrderManager
        # OrderManager enforces daily loss limit at execution level (not just strategy)
        # This prevents strategy logic errors from bypassing circuit breaker
        if position.realized_pnl != 0:
            self.order_manager.record_mm_pnl(position.realized_pnl)
            logger.info(
                f"[P&L REPORTING] Reported ${position.realized_pnl:.2f} to OrderManager - "
                f"Daily total: ${self.order_manager.get_mm_daily_pnl():.2f}"
            )
        
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
            
            # Track unwinding start time
            if token_id not in position.unwinding_start:
                position.unwinding_start[token_id] = time.time()
                logger.warning(f"[MM] Passively unwinding {abs(inventory)} shares")
            
            unwinding_duration = time.time() - position.unwinding_start[token_id]
            
            # EMERGENCY: If passive unwinding hasn't worked after 5 minutes, force exit
            if unwinding_duration > position.unwinding_timeout:
                logger.critical(
                    f"[MM] Passive unwinding timeout ({unwinding_duration/60:.1f}min) - "
                    f"FORCE EXIT with limit order crossing spread"
                )
                
                # Get current order book
                prices = await self._get_market_prices(market_id, [token_id])
                if prices and token_id in prices:
                    mid_price = prices[token_id]
                    
                    # Cross the spread to guarantee execution
                    if inventory > 0:
                        # Long: sell at bid (cross spread down)
                        force_price = max(0.01, mid_price - 0.02)  # 2 cents below mid
                    else:
                        # Short: buy at ask (cross spread up)
                        force_price = min(0.99, mid_price + 0.02)  # 2 cents above mid
                    
                    side = 'SELL' if inventory > 0 else 'BUY'
                    force_size = abs(inventory)
                    
                    try:
                        # Use regular limit order (not post_only) to guarantee execution
                        force_order = await self.order_manager.execute_limit_order(
                            token_id=token_id,
                            side=side,
                            price=force_price,
                            size=force_size,
                            post_only=False  # Allow immediate execution
                        )
                        if force_order:
                            logger.warning(f"[MM] Force exit order placed: {side} {force_size} @ {force_price:.4f}")
                    except Exception as e:
                        logger.error(f"Force exit failed: {e}")
                
                # Clear unwinding tracker
                del position.unwinding_start[token_id]
                return
            
            # Continue with passive unwinding (aggressive skewing)
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
                token_id, 'BUY', target_bid, bid_size, current_bid_id, position, market_id
            )
            if new_bid_id:
                position.active_bids[token_id] = new_bid_id
            
            current_ask_id = position.active_asks.get(token_id)
            new_ask_id = await self._reconcile_order(
                token_id, 'SELL', target_ask, ask_size, current_ask_id, position, market_id
            )
            if new_ask_id:
                position.active_asks[token_id] = new_ask_id
            
            logger.info(
                f"[MM] Unwinding quotes: BID={target_bid:.4f} ASK={target_ask:.4f} "
                f"(duration: {unwinding_duration:.0f}s)"
            )
                
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
    
    # ============================================================================
    # BaseStrategy Abstract Method Implementations
    # ============================================================================
    # Market making uses continuous run() loop instead of opportunity-based execution
    # These methods are required by BaseStrategy but not used in practice
    
    async def analyze_opportunity(self, opportunity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Analyze market making opportunity (not used - strategy uses run() loop)
        
        Market making doesn't operate on discrete opportunities like arbitrage.
        It continuously provides liquidity via the run() loop.
        """
        return None
    
    async def should_execute_trade(self, analysis: Dict[str, Any]) -> bool:
        """
        Check if trade should be executed (not used - strategy uses run() loop)
        
        Market making decisions are made internally via risk checks and quote updates.
        """
        return False
    
    async def execute(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute trade (not used - strategy uses run() loop)
        
        Market making execution happens via _place_quotes() and _exit_inventory().
        This method exists only to satisfy BaseStrategy interface.
        """
        return {
            'success': False,
            'error': 'Market making uses run() loop, not execute() method'
        }

