"""
PolymarketMM - Avellaneda-Stoikov Market Maker for Binary Prediction Markets

Implementation of the Avellaneda-Stoikov optimal market-making model adapted
for Polymarket's binary outcome markets [0, 1].

Mathematical Framework:
═══════════════════════

1. Reservation Price:
   r = s - q·γ·σ²
   where:
   - s = mid-price
   - q = inventory position (positive = long, negative = short)
   - γ = risk aversion parameter (default 0.25)
   - σ = volatility (60-second rolling std dev)

2. Optimal Spread:
   δ = (2/γ) · ln(1 + γ/κ)
   where:
   - κ = liquidity parameter (order book depth within 5 ticks)

3. Quote Placement:
   bid = r - δ/2
   ask = r + δ/2

Safety Features (2026 Institution-Grade):
═════════════════════════════════════════
- Cumulative Loss Kill-Switch: >2% loss in 60-min window
- API Latency Monitor: >450ms for 3 consecutive pings
- Order Rejection Circuit Breaker: >5% rejection rate in 5-min window
- Inventory Trap Protection: |q| > 5,000 contracts hard limit
- WebSocket Heartbeat Monitor: Ensure connection health

Polymarket SDK v3.x (2026):
═══════════════════════════
- Post-Only orders for maker rebates
- asyncio-based WebSocket handling
- Real-time order book updates
"""

import asyncio
import time
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from collections import deque
import math
import statistics
import numpy as np

from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from core.market_data_manager import MarketDataManager, FillEvent
from utils.logger import get_logger, log_trade_event
from utils.exceptions import TradingError, CircuitBreakerError


logger = get_logger(__name__)


@dataclass
class MarketState:
    """Real-time market state for a single token"""
    token_id: str
    mid_price: float
    bid_price: float
    ask_price: float
    bid_size: float
    ask_size: float
    spread: float
    timestamp: float
    
    # Order book depth (for κ calculation)
    bid_depth_5ticks: float = 0.0
    ask_depth_5ticks: float = 0.0
    
    # Price history for volatility calculation
    price_history: deque = field(default_factory=lambda: deque(maxlen=120))  # 60s @ 0.5s updates


@dataclass
class Position:
    """Current position state"""
    token_id: str
    quantity: float  # q in A-S model (positive = long, negative = short)
    avg_entry_price: float
    realized_pnl: float
    unrealized_pnl: float
    last_update: float


@dataclass
class SafetyMetrics:
    """Safety monitoring metrics"""
    # PnL tracking for kill-switch
    pnl_history: deque = field(default_factory=lambda: deque(maxlen=120))  # 60 minutes @ 30s intervals
    cumulative_pnl: float = 0.0
    max_drawdown_1h: float = 0.0
    
    # API latency tracking
    latency_history: deque = field(default_factory=lambda: deque(maxlen=10))
    consecutive_high_latency: int = 0
    
    # Order rejection tracking
    order_history: deque = field(default_factory=lambda: deque(maxlen=100))  # Last 100 orders
    rejection_count_5min: int = 0
    total_orders_5min: int = 0
    
    # Inventory tracking
    max_inventory_breach_count: int = 0
    
    # WebSocket health
    last_heartbeat: float = 0.0


@dataclass
class BoundaryCondition:
    """Boundary risk state for extreme price regimes"""
    is_extreme: bool
    regime: str  # 'high' (>0.90), 'low' (<0.10), 'normal'
    volatility_multiplier: float
    skew_adjustment_bps: float
    passive_only_mode: bool
    price_magnet_detected: bool
    boundary_distance: float  # Distance to nearest boundary


class BoundaryRiskEngine:
    """
    Boundary Risk Management for Binary Outcomes (2026 Institution-Grade)
    
    Handles non-linear risks near price boundaries [0, 1] where:
    - Variance approaches zero (Bernoulli: Var = p(1-p))
    - Jump risk (Black Swan) increases
    - Toxic flow concentrates
    - Asymmetric risk/reward profile dominates
    
    Mathematical Framework:
    ═══════════════════════
    1. Boundary-Adjusted Volatility:
       σ_boundary = σ_base × (1 + k × boundary_proximity)
       where k ∈ [1.5, 2.0] for p > 0.90 or p < 0.10
    
    2. Exponential Inventory Penalty:
       penalty = q × γ × σ² × exp(α × |p - 0.5|)
       where α scales penalty exponentially near boundaries
    
    3. Asymmetric Spread:
       spread_up = δ × (1 + (1 - p))  # Wider at high prices
       spread_down = δ × (1 + p)       # Wider at low prices
    
    4. Price Magnet Detection:
       If Δp > 5 ticks in <1 second → Passive mode
    """
    
    def __init__(self):
        # Boundary thresholds
        self.EXTREME_HIGH_THRESHOLD = 0.90
        self.EXTREME_LOW_THRESHOLD = 0.10
        self.CRITICAL_HIGH_THRESHOLD = 0.95
        self.CRITICAL_LOW_THRESHOLD = 0.05
        
        # Volatility adjustments
        self.BASE_VOL_MULTIPLIER = 1.5
        self.EXTREME_VOL_MULTIPLIER = 2.0
        
        # Exponential penalty parameters
        self.EXPONENTIAL_ALPHA = 3.0  # Controls exponential decay rate
        
        # Price magnet detection
        self.MAGNET_TICK_THRESHOLD = 5  # 5 ticks = 0.005
        self.MAGNET_TIME_THRESHOLD = 1.0  # 1 second
        self.price_history: deque = deque(maxlen=100)  # Last 100 price updates
        
        logger.info(
            "BoundaryRiskEngine initialized:\n"
            f"  Extreme thresholds: [{self.EXTREME_LOW_THRESHOLD}, {self.EXTREME_HIGH_THRESHOLD}]\n"
            f"  Critical thresholds: [{self.CRITICAL_LOW_THRESHOLD}, {self.CRITICAL_HIGH_THRESHOLD}]\n"
            f"  Volatility multipliers: {self.BASE_VOL_MULTIPLIER}x - {self.EXTREME_VOL_MULTIPLIER}x"
        )
    
    def analyze_boundary_condition(
        self,
        mid_price: float,
        inventory: float,
        base_sigma: float
    ) -> BoundaryCondition:
        """
        Analyze current boundary risk state
        
        Args:
            mid_price: Current mid-price [0, 1]
            inventory: Current position (signed)
            base_sigma: Base volatility estimate
            
        Returns:
            BoundaryCondition with risk adjustments
        """
        # Determine regime
        is_high = mid_price > self.EXTREME_HIGH_THRESHOLD
        is_low = mid_price < self.EXTREME_LOW_THRESHOLD
        is_critical_high = mid_price > self.CRITICAL_HIGH_THRESHOLD
        is_critical_low = mid_price < self.CRITICAL_LOW_THRESHOLD
        
        is_extreme = is_high or is_low
        
        if is_high:
            regime = 'critical_high' if is_critical_high else 'high'
            boundary_distance = 1.0 - mid_price
        elif is_low:
            regime = 'critical_low' if is_critical_low else 'low'
            boundary_distance = mid_price
        else:
            regime = 'normal'
            boundary_distance = min(mid_price, 1.0 - mid_price)
        
        # Calculate volatility multiplier
        vol_multiplier = self._calculate_volatility_multiplier(
            mid_price, regime, boundary_distance
        )
        
        # Calculate skew adjustment
        skew_adjustment_bps = self._calculate_extreme_skew(
            mid_price, inventory, base_sigma, regime
        )
        
        # Check for price magnet
        magnet_detected = self._detect_price_magnet(mid_price)
        
        # Determine if passive-only mode
        passive_only = (is_critical_high or is_critical_low) or magnet_detected
        
        condition = BoundaryCondition(
            is_extreme=is_extreme,
            regime=regime,
            volatility_multiplier=vol_multiplier,
            skew_adjustment_bps=skew_adjustment_bps,
            passive_only_mode=passive_only,
            price_magnet_detected=magnet_detected,
            boundary_distance=boundary_distance
        )
        
        # Log if in extreme regime
        if is_extreme and not passive_only:
            logger.debug(
                f"[BOUNDARY] Regime: {regime}, Price: {mid_price:.4f}, "
                f"Vol Multiplier: {vol_multiplier:.2f}x, "
                f"Skew: {skew_adjustment_bps:.1f}bps"
            )
        
        if passive_only:
            logger.warning(
                f"[BOUNDARY] PASSIVE MODE ACTIVATED - "
                f"Regime: {regime}, Magnet: {magnet_detected}"
            )
        
        return condition
    
    def _calculate_volatility_multiplier(
        self,
        mid_price: float,
        regime: str,
        boundary_distance: float
    ) -> float:
        """
        Calculate boundary-adjusted volatility multiplier
        
        Logic:
        - Normal regime: 1.0x (no adjustment)
        - Extreme regime: 1.5x - 2.0x based on proximity
        - Critical regime: 2.0x (maximum adjustment)
        
        Compensates for:
        - Bernoulli variance decrease: Var = p(1-p) → 0 at boundaries
        - Jump risk increase: Black Swan events near resolution
        - Toxic flow concentration: Informed traders near settlement
        """
        if regime == 'normal':
            return 1.0
        
        if 'critical' in regime:
            return self.EXTREME_VOL_MULTIPLIER
        
        # Scale between BASE and EXTREME based on proximity
        # boundary_distance ∈ [0, 0.1]
        proximity = 1.0 - (boundary_distance / 0.1)  # 0 at threshold, 1 at boundary
        
        multiplier = self.BASE_VOL_MULTIPLIER + (
            (self.EXTREME_VOL_MULTIPLIER - self.BASE_VOL_MULTIPLIER) * proximity
        )
        
        return multiplier
    
    def _calculate_extreme_skew(
        self,
        mid_price: float,
        inventory: float,
        base_sigma: float,
        regime: str
    ) -> float:
        """
        Calculate non-linear inventory skew for boundary conditions
        
        Replaces linear penalty (q·γ·σ²) with exponential decay:
        
        penalty = q × γ × σ² × exp(α × |p - 0.5|)
        
        where α = 3.0 creates exponential scaling near boundaries
        
        Logic:
        - If LONG and price HIGH: Aggressively widen BID, tighten ASK (offload)
        - If SHORT and price LOW: Aggressively widen ASK, tighten BID (cover)
        - Skew Cap: At p > 0.95 or p < 0.05, only provide exit liquidity
        
        Returns:
            Skew adjustment in basis points
        """
        if regime == 'normal':
            return 0.0  # No adjustment in normal regime
        
        # Calculate exponential penalty factor
        # exp(α × |p - 0.5|) ranges from 1.0 (at p=0.5) to ~20 (at p=1.0)
        distance_from_center = abs(mid_price - 0.5)
        exponential_factor = math.exp(self.EXPONENTIAL_ALPHA * distance_from_center)
        
        # Base skew from inventory (in basis points)
        # Assume typical gamma=0.25, sigma=0.1 → base_penalty = 0.25 * 0.01 = 0.0025 = 25bps per contract
        base_skew_per_contract = 0.25 * (base_sigma ** 2) * 10000  # Convert to bps
        
        # Apply exponential scaling
        skew_bps = inventory * base_skew_per_contract * exponential_factor
        
        # Direction-aware skew
        if regime in ['high', 'critical_high']:
            # High price regime: Long position is toxic
            if inventory > 0:
                # Long → Widen BID (negative skew), tighten ASK (positive skew)
                # This encourages selling to reduce long exposure
                skew_adjustment = -abs(skew_bps)  # Negative = lower reservation price
            else:
                # Short → Less urgent, use standard scaling
                skew_adjustment = skew_bps * 0.5
        
        elif regime in ['low', 'critical_low']:
            # Low price regime: Short position is toxic
            if inventory < 0:
                # Short → Widen ASK (positive skew), tighten BID (negative skew)
                # This encourages buying to cover short exposure
                skew_adjustment = abs(skew_bps)  # Positive = higher reservation price
            else:
                # Long → Less urgent, use standard scaling
                skew_adjustment = skew_bps * 0.5
        else:
            skew_adjustment = 0.0
        
        # Skew cap for critical regimes
        if 'critical' in regime:
            # At p > 0.95 or p < 0.05, only provide exit liquidity
            # Maximum skew: 500 bps (5%)
            skew_adjustment = np.clip(skew_adjustment, -500, 500)
            
            # If inventory is in wrong direction, cease aggressive quoting
            if (regime == 'critical_high' and inventory > 50) or \
               (regime == 'critical_low' and inventory < -50):
                logger.warning(
                    f"[BOUNDARY] Skew Cap: Inventory {inventory:+.0f} in {regime} regime - "
                    f"limiting skew to exit liquidity only"
                )
        
        return skew_adjustment
    
    def calculate_asymmetric_spread(
        self,
        mid_price: float,
        base_spread: float,
        regime: str
    ) -> Tuple[float, float]:
        """
        Calculate asymmetric spread for boundary conditions
        
        At p > 0.90:
        - Upside potential: 10 ticks (1.0 - 0.90 = 0.10)
        - Downside risk: 900 ticks (0.90 - 0.0 = 0.90)
        
        Asymmetric formulas:
        - spread_up = δ × (1 + (1 - p))  # Wider for upside quotes
        - spread_down = δ × (1 + p)      # Wider for downside quotes
        
        This compensates for asymmetric resolution risk.
        
        Returns:
            (bid_spread, ask_spread) - spreads to apply on each side
        """
        if regime == 'normal':
            # Symmetric spread in normal regime
            return (base_spread / 2, base_spread / 2)
        
        # Asymmetric scaling factors
        upside_factor = 1.0 + (1.0 - mid_price)  # Large when p is high
        downside_factor = 1.0 + mid_price        # Large when p is low
        
        if regime in ['high', 'critical_high']:
            # Price is high -> Upside is limited, downside is large
            # Widen ASK (upside) more aggressively
            bid_spread = base_spread / 2 * downside_factor * 0.7  # Tighten bid slightly
            ask_spread = base_spread / 2 * upside_factor * 1.3    # Widen ask significantly
            
        elif regime in ['low', 'critical_low']:
            # Price is low -> Downside is limited, upside is large
            # Widen BID (downside) more aggressively
            bid_spread = base_spread / 2 * downside_factor * 1.3  # Widen bid significantly
            ask_spread = base_spread / 2 * upside_factor * 0.7    # Tighten ask slightly
        else:
            bid_spread = base_spread / 2
            ask_spread = base_spread / 2
        
        # Ensure minimum spread (1 tick = 0.001)
        bid_spread = max(0.001, bid_spread)
        ask_spread = max(0.001, ask_spread)
        
        return (bid_spread, ask_spread)
    
    def _detect_price_magnet(self, current_price: float) -> bool:
        """
        Detect 'Price Magnet' condition
        
        Trigger: Price moves >5 ticks toward boundary in <1 second
        
        This indicates:
        - Strong momentum toward resolution
        - High probability of toxic flow
        - Elevated adverse selection risk
        
        Response: Activate 'Passive Only' mode
        - No new orders
        - Only cancel/re-quote existing orders
        - Wait for stabilization
        
        Returns:
            True if magnet detected
        """
        # Record current price
        current_time = time.time()
        self.price_history.append((current_time, current_price))
        
        # Need at least 2 samples
        if len(self.price_history) < 2:
            return False
        
        # Check last second of price movement
        recent_prices = [
            (t, p) for t, p in self.price_history
            if current_time - t <= self.MAGNET_TIME_THRESHOLD
        ]
        
        if len(recent_prices) < 2:
            return False
        
        # Calculate price change toward nearest boundary
        oldest_price = recent_prices[0][1]
        price_change = abs(current_price - oldest_price)
        
        # Determine direction toward boundary
        if current_price > 0.5:
            # High regime - moving toward 1.0
            moving_toward_boundary = current_price > oldest_price
        else:
            # Low regime - moving toward 0.0
            moving_toward_boundary = current_price < oldest_price
        
        # Convert to ticks (1 tick = 0.001)
        ticks_moved = price_change * 1000
        
        # Trigger if moved >5 ticks toward boundary in <1 second
        if moving_toward_boundary and ticks_moved > self.MAGNET_TICK_THRESHOLD:
            logger.warning(
                f"[PRICE MAGNET DETECTED] Price moved {ticks_moved:.1f} ticks "
                f"toward boundary in <1s (from {oldest_price:.4f} to {current_price:.4f})"
            )
            return True
        
        return False
    
    def should_cease_aggressive_quoting(
        self,
        condition: BoundaryCondition,
        inventory: float
    ) -> bool:
        """
        Determine if bot should cease aggressive quoting
        
        Skew Cap Logic:
        - At p > 0.95 or p < 0.05, only provide exit liquidity
        - Do not "buy into the trend" (accumulate inventory in boundary direction)
        
        Returns:
            True if should only provide passive quotes
        """
        if not condition.is_extreme:
            return False
        
        # Critical regime + significant inventory in wrong direction
        if condition.regime == 'critical_high' and inventory > 50:
            # Don't accumulate more longs near p=1.0
            return True
        
        if condition.regime == 'critical_low' and inventory < -50:
            # Don't accumulate more shorts near p=0.0
            return True
        
        # Passive mode triggered
        if condition.passive_only_mode:
            return True
        
        return False
    heartbeat_failures: int = 0


class PolymarketMM:
    """
    Avellaneda-Stoikov Market Maker for Polymarket Binary Outcomes
    
    2026 HFT Implementation with:
    - Real-time volatility estimation
    - Dynamic risk aversion based on order flow imbalance
    - Adaptive liquidity parameter from order book depth
    - Multi-layered safety systems
    - Post-only orders for maker rebates
    """
    
    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        market_data_manager: MarketDataManager,
        markets: List[str],  # List of token IDs to make markets on
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize Avellaneda-Stoikov Market Maker
        
        Args:
            client: Polymarket CLOB client (SDK v3.x)
            order_manager: Order execution manager
            market_data_manager: WebSocket market data manager
            markets: List of token IDs to quote
            config: Optional configuration overrides
        """
        self.client = client
        self.order_manager = order_manager
        self.market_data_manager = market_data_manager
        self.markets = markets
        
        # Initialize Boundary Risk Engine
        self.boundary_engine = BoundaryRiskEngine()
        
        # Configuration (with sensible defaults)
        self.config = {
            # Avellaneda-Stoikov parameters
            'gamma_base': 0.25,  # Base risk aversion
            'gamma_min': 0.1,    # Min gamma (aggressive)
            'gamma_max': 0.5,    # Max gamma (conservative)
            'volatility_window_sec': 60,  # Rolling window for σ calculation
            'tick_range_for_kappa': 5,    # Ticks to analyze for κ
            
            # Quoting parameters
            'quote_size': 10.0,  # Default quote size (shares)
            'min_spread_bps': 5,  # Minimum spread (5 basis points)
            'max_spread_bps': 200,  # Maximum spread (200 basis points = 2%)
            'requote_threshold_bps': 20,  # Requote if price moves >20bps
            
            # Safety parameters
            'max_drawdown_pct': 2.0,  # Kill-switch at 2% loss
            'max_api_latency_ms': 450,  # Kill-switch at 450ms
            'max_rejection_rate_pct': 5.0,  # Circuit breaker at 5%
            'max_inventory_contracts': 5000,  # Hard inventory limit
            'heartbeat_timeout_sec': 10,  # WebSocket heartbeat timeout
            
            # Update intervals
            'quote_update_interval_ms': 100,  # Re-evaluate quotes every 100ms
            'safety_check_interval_sec': 5,  # Run safety checks every 5s
            'heartbeat_interval_sec': 3,  # Send heartbeat every 3s
        }
        
        if config:
            self.config.update(config)
        
        # State management
        self.is_running = False
        self.is_killed = False  # Emergency kill-switch activated
        self.market_states: Dict[str, MarketState] = {}
        self.positions: Dict[str, Position] = {}
        self.active_quotes: Dict[str, List[str]] = {}  # token_id -> [bid_order_id, ask_order_id]
        
        # Safety monitoring
        self.safety_metrics = SafetyMetrics()
        
        # Performance tracking
        self.total_quotes_placed = 0
        self.total_fills = 0
        self.total_pnl = Decimal('0')
        self.start_time = 0.0
        
        # Locks for thread safety
        self._quote_lock = asyncio.Lock()
        self._position_lock = asyncio.Lock()
        
        logger.info(
            f"🤖 PolymarketMM initialized (Avellaneda-Stoikov):\n"
            f"   Markets: {len(markets)}\n"
            f"   Base γ: {self.config['gamma_base']}\n"
            f"   Volatility window: {self.config['volatility_window_sec']}s\n"
            f"   Max drawdown: {self.config['max_drawdown_pct']}%\n"
            f"   Max inventory: {self.config['max_inventory_contracts']:,} contracts"
        )
    
    # ========================================================================
    # Main Strategy Loop
    # ========================================================================
    
    async def start(self) -> None:
        """Start the market maker"""
        if self.is_running:
            logger.warning("PolymarketMM already running")
            return
        
        self.is_running = True
        self.is_killed = False
        self.start_time = time.time()
        
        logger.info("🚀 Starting Avellaneda-Stoikov Market Maker...")
        
        try:
            # Initialize market states
            await self._initialize_market_states()
            
            # Register WebSocket handlers
            self._register_handlers()
            
            # Start concurrent tasks
            tasks = [
                asyncio.create_task(self._quote_loop(), name="quote_loop"),
                asyncio.create_task(self._safety_monitor(), name="safety_monitor"),
                asyncio.create_task(self._heartbeat_monitor(), name="heartbeat"),
                asyncio.create_task(self._metrics_logger(), name="metrics"),
            ]
            
            logger.info("✅ All components started - Avellaneda-Stoikov MM LIVE")
            
            # Wait for all tasks
            await asyncio.gather(*tasks, return_exceptions=True)
            
        except asyncio.CancelledError:
            logger.info("PolymarketMM cancelled")
        except Exception as e:
            logger.error(f"Fatal error in PolymarketMM: {e}", exc_info=True)
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """Stop the market maker gracefully"""
        if not self.is_running:
            return
        
        logger.info("🛑 Stopping Avellaneda-Stoikov Market Maker...")
        self.is_running = False
        
        try:
            # Cancel all outstanding quotes
            await self._cancel_all_quotes()
            
            # Unregister handlers
            self._unregister_handlers()
            
            # Log final metrics
            self._log_final_metrics()
            
            logger.info("✅ PolymarketMM stopped gracefully")
            
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)
    
    # ========================================================================
    # Market State Management
    # ========================================================================
    
    async def _initialize_market_states(self) -> None:
        """Initialize market states for all tokens"""
        logger.info(f"Initializing market states for {len(self.markets)} tokens...")
        
        for token_id in self.markets:
            try:
                # Fetch initial order book
                book = await self.client.get_order_book(token_id)
                
                if not book or not hasattr(book, 'bids') or not hasattr(book, 'asks'):
                    logger.warning(f"Invalid order book for {token_id[:8]}...")
                    continue
                
                bids = book.bids
                asks = book.asks
                
                if not bids or not asks:
                    logger.warning(f"Empty order book for {token_id[:8]}...")
                    continue
                
                # Calculate initial state
                bid_price = float(bids[0]['price'])
                ask_price = float(asks[0]['price'])
                mid_price = (bid_price + ask_price) / 2
                
                state = MarketState(
                    token_id=token_id,
                    mid_price=mid_price,
                    bid_price=bid_price,
                    ask_price=ask_price,
                    bid_size=float(bids[0]['size']),
                    ask_size=float(asks[0]['size']),
                    spread=ask_price - bid_price,
                    timestamp=time.time()
                )
                
                # Initialize price history
                state.price_history.append((time.time(), mid_price))
                
                # Calculate initial order book depth
                state.bid_depth_5ticks = self._calculate_depth(bids, mid_price, 'bid')
                state.ask_depth_5ticks = self._calculate_depth(asks, mid_price, 'ask')
                
                self.market_states[token_id] = state
                
                # Initialize position
                self.positions[token_id] = Position(
                    token_id=token_id,
                    quantity=0.0,
                    avg_entry_price=0.0,
                    realized_pnl=0.0,
                    unrealized_pnl=0.0,
                    last_update=time.time()
                )
                
                logger.info(
                    f"✅ {token_id[:8]}... initialized: "
                    f"Mid=${mid_price:.4f}, Spread={state.spread*10000:.1f}bps"
                )
                
            except Exception as e:
                logger.error(f"Failed to initialize {token_id[:8]}...: {e}")
        
        logger.info(f"Initialized {len(self.market_states)}/{len(self.markets)} markets")
    
    def _calculate_depth(
        self,
        levels: List[Dict],
        mid_price: float,
        side: str
    ) -> float:
        """
        Calculate order book depth within N ticks of mid-price (for κ estimation)
        
        Args:
            levels: Order book levels (bids or asks)
            mid_price: Current mid-price
            side: 'bid' or 'ask'
            
        Returns:
            Total size within tick_range_for_kappa ticks
        """
        tick_size = 0.001  # Polymarket tick size = $0.001
        tick_range = self.config['tick_range_for_kappa']
        max_distance = tick_size * tick_range
        
        total_depth = 0.0
        
        for level in levels:
            price = float(level['price'])
            size = float(level['size'])
            
            distance = abs(price - mid_price)
            
            if distance <= max_distance:
                total_depth += size
            else:
                break  # Levels are sorted, stop when out of range
        
        return total_depth
    
    def _register_handlers(self) -> None:
        """Register WebSocket event handlers"""
        # Register fill handler
        self.market_data_manager.register_fill_handler(
            'avellaneda_stoikov_mm',
            self._handle_fill_event
        )
        
        # Register book update handler for each market
        for token_id in self.markets:
            self.market_data_manager.cache.register_market_update_handler(
                f'mm_{token_id}',
                lambda tid=token_id, snap=None: asyncio.create_task(
                    self._handle_book_update(tid, snap)
                ),
                market_filter={token_id}
            )
        
        logger.info(f"✅ Registered handlers for {len(self.markets)} markets")
    
    def _unregister_handlers(self) -> None:
        """Unregister WebSocket event handlers"""
        try:
            # Unregister fill handler
            if hasattr(self.market_data_manager, 'unregister_fill_handler'):
                self.market_data_manager.unregister_fill_handler('avellaneda_stoikov_mm')
            
            # Unregister book handlers
            for token_id in self.markets:
                self.market_data_manager.cache.unregister_market_update_handler(f'mm_{token_id}')
        except Exception as e:
            logger.error(f"Error unregistering handlers: {e}")
    
    async def _handle_book_update(self, token_id: str, snapshot: Any) -> None:
        """Handle real-time order book update"""
        try:
            if token_id not in self.market_states:
                return
            
            state = self.market_states[token_id]
            
            # Extract book data
            if hasattr(snapshot, 'bids') and hasattr(snapshot, 'asks'):
                bids = snapshot.bids
                asks = snapshot.asks
            else:
                # Fallback to cache format
                bids = snapshot.get('bids', [])
                asks = snapshot.get('asks', [])
            
            if not bids or not asks:
                return
            
            # Update state
            state.bid_price = float(bids[0]['price'])
            state.ask_price = float(asks[0]['price'])
            state.mid_price = (state.bid_price + state.ask_price) / 2
            state.spread = state.ask_price - state.bid_price
            state.bid_size = float(bids[0]['size'])
            state.ask_size = float(asks[0]['size'])
            state.timestamp = time.time()
            
            # Update price history
            state.price_history.append((state.timestamp, state.mid_price))
            
            # Update order book depth (for κ)
            state.bid_depth_5ticks = self._calculate_depth(bids, state.mid_price, 'bid')
            state.ask_depth_5ticks = self._calculate_depth(asks, state.mid_price, 'ask')
            
        except Exception as e:
            logger.error(f"Error handling book update for {token_id[:8]}...: {e}")
    
    async def _handle_fill_event(self, fill: FillEvent) -> None:
        """Handle fill event (position update)"""
        try:
            async with self._position_lock:
                token_id = fill.asset_id
                
                if token_id not in self.positions:
                    logger.warning(f"Received fill for unknown token {token_id[:8]}...")
                    return
                
                position = self.positions[token_id]
                
                # Update position
                side_multiplier = 1.0 if fill.side == 'BUY' else -1.0
                new_quantity = position.quantity + (fill.size * side_multiplier)
                
                # Update average entry price
                if new_quantity != 0:
                    if position.quantity == 0:
                        position.avg_entry_price = fill.price
                    else:
                        # Weighted average
                        total_value = (position.quantity * position.avg_entry_price) + \
                                     (fill.size * side_multiplier * fill.price)
                        position.avg_entry_price = abs(total_value / new_quantity)
                
                # Calculate realized PnL (if reducing position)
                if (position.quantity > 0 and fill.side == 'SELL') or \
                   (position.quantity < 0 and fill.side == 'BUY'):
                    pnl_per_share = fill.price - position.avg_entry_price
                    if fill.side == 'SELL':
                        realized_pnl = pnl_per_share * fill.size
                    else:
                        realized_pnl = -pnl_per_share * fill.size
                    
                    position.realized_pnl += realized_pnl
                    self.safety_metrics.cumulative_pnl += realized_pnl
                
                position.quantity = new_quantity
                position.last_update = time.time()
                
                # Update unrealized PnL
                if token_id in self.market_states:
                    mid_price = self.market_states[token_id].mid_price
                    position.unrealized_pnl = (mid_price - position.avg_entry_price) * position.quantity
                
                self.total_fills += 1
                
                logger.info(
                    f"[FILL] {fill.side} {fill.size:.1f} @ ${fill.price:.4f} "
                    f"(position: {position.quantity:+.1f}, realized PnL: ${position.realized_pnl:+.2f})"
                )
                
                # Log trade event
                log_trade_event(
                    event_type='MARKET_MAKER_FILL',
                    market_id=fill.market_id or 'unknown',
                    action=fill.side,
                    token_id=token_id,
                    shares=fill.size,
                    price=fill.price,
                    reason=f"A-S MM fill - position now {position.quantity:+.1f}"
                )
                
        except Exception as e:
            logger.error(f"Error handling fill event: {e}", exc_info=True)
    
    # ========================================================================
    # Avellaneda-Stoikov Core Logic
    # ========================================================================
    
    def _calculate_volatility(self, state: MarketState) -> float:
        """
        Calculate rolling volatility (σ) from price history
        
        Uses 60-second rolling window of mid-price log returns.
        
        Returns:
            Annualized volatility (for use in A-S formulas)
        """
        if len(state.price_history) < 2:
            return 0.01  # Default 1% volatility
        
        # Calculate log returns
        log_returns = []
        prices = list(state.price_history)
        
        for i in range(1, len(prices)):
            t1, p1 = prices[i-1]
            t2, p2 = prices[i]
            
            if p1 > 0 and p2 > 0:
                log_return = math.log(p2 / p1)
                log_returns.append(log_return)
        
        if len(log_returns) < 2:
            return 0.01
        
        # Calculate standard deviation
        std_dev = statistics.stdev(log_returns)
        
        # Annualize (assuming 0.5s update interval)
        # σ_annual = σ_interval * sqrt(periods_per_year)
        # periods_per_year = (365.25 * 24 * 3600) / 0.5 = 63,115,200
        annualization_factor = math.sqrt(63_115_200)
        volatility = std_dev * annualization_factor
        
        # Clamp to reasonable range [0.1%, 100%]
        volatility = max(0.001, min(1.0, volatility))
        
        return volatility
    
    def _calculate_gamma(self, state: MarketState) -> float:
        """
        Calculate dynamic risk aversion (γ) based on order flow imbalance
        
        Logic:
        - If bid depth >> ask depth: Market is bullish, reduce γ (be more aggressive)
        - If ask depth >> bid depth: Market is bearish, increase γ (be more conservative)
        - Base γ = 0.25, scales between [0.1, 0.5]
        
        Returns:
            Dynamic risk aversion parameter
        """
        gamma_base = self.config['gamma_base']
        gamma_min = self.config['gamma_min']
        gamma_max = self.config['gamma_max']
        
        # Calculate order book imbalance
        total_depth = state.bid_depth_5ticks + state.ask_depth_5ticks
        
        if total_depth == 0:
            return gamma_base
        
        imbalance = (state.bid_depth_5ticks - state.ask_depth_5ticks) / total_depth
        
        # Scale gamma inversely with imbalance
        # imbalance = +1 (all bids) -> gamma_min (aggressive)
        # imbalance = -1 (all asks) -> gamma_max (conservative)
        # imbalance = 0 (balanced) -> gamma_base
        
        if imbalance > 0:
            # More bids than asks -> reduce gamma
            gamma = gamma_base - (gamma_base - gamma_min) * abs(imbalance)
        else:
            # More asks than bids -> increase gamma
            gamma = gamma_base + (gamma_max - gamma_base) * abs(imbalance)
        
        return max(gamma_min, min(gamma_max, gamma))
    
    def _calculate_kappa(self, state: MarketState) -> float:
        """
        Calculate liquidity parameter (κ) from order book depth
        
        κ represents market liquidity - higher values indicate deeper books.
        
        Formula: κ ≈ average_depth_within_5_ticks
        
        Returns:
            Liquidity parameter
        """
        avg_depth = (state.bid_depth_5ticks + state.ask_depth_5ticks) / 2
        
        # Clamp to reasonable range [1, 1000]
        kappa = max(1.0, min(1000.0, avg_depth))
        
        return kappa
    
    def _calculate_reservation_price(
        self,
        state: MarketState,
        position: Position
    ) -> float:
        """
        Calculate reservation price (r) using Avellaneda-Stoikov formula
        WITH BOUNDARY ADJUSTMENTS (2026 Institution-Grade)
        
        Standard Formula: r = s - q*gamma*sigma^2
        
        Boundary-Adjusted Formula: r = s - q*gamma*(sigma * sigma_multiplier)^2
        
        where:
        - s = mid-price
        - q = inventory position
        - gamma = risk aversion
        - sigma = base volatility
        - sigma_multiplier = 1.0 (normal) to 2.0 (extreme boundaries)
        
        Returns:
            Reservation price (boundary-adjusted)
        """
        s = state.mid_price
        q = position.quantity
        gamma = self._calculate_gamma(state)
        base_sigma = self._calculate_volatility(state)
        
        # Analyze boundary condition
        boundary = self.boundary_engine.analyze_boundary_condition(
            mid_price=s,
            inventory=q,
            base_sigma=base_sigma
        )
        
        # Apply boundary-adjusted volatility
        adjusted_sigma = base_sigma * boundary.volatility_multiplier
        
        # Calculate base reservation price with adjusted volatility
        r_base = s - q * gamma * (adjusted_sigma ** 2)
        
        # Apply exponential skew adjustment (in basis points)
        skew_adjustment = boundary.skew_adjustment_bps / 10000  # Convert bps to decimal
        r = r_base - skew_adjustment
        
        # Log boundary adjustments
        if boundary.is_extreme:
            logger.debug(
                f"[BOUNDARY RESERVATION] Base: ${r_base:.4f}, "
                f"Skew: {boundary.skew_adjustment_bps:+.1f}bps, "
                f"Final: ${r:.4f} (regime: {boundary.regime})"
            )
        
        sigma = self._calculate_volatility(state)
        
        r = s - q * gamma * (sigma ** 2)
        
        # Clamp to valid range [0.01, 0.99] for binary outcomes
        r = max(0.01, min(0.99, r))
        
        return r
    
    def _calculate_optimal_spread(
        self,
        state: MarketState
    ) -> float:
        """
        Calculate optimal spread (δ) using Avellaneda-Stoikov formula
        WITH BOUNDARY RISK ADJUSTMENTS (2026 Institution-Grade)
        
        Enhancements:
        1. Boundary-adjusted volatility (1.5x - 2.0x at extremes)
        2. Exponential inventory skew (non-linear penalty)
        3. Asymmetric spread calculation (compensates for resolution risk)
        4. Passive-only mode detection (price magnet)
        
        Returns:
            (bid_price, ask_price) or None if quotes cannot be calculated
        """
        try:
            if token_id not in self.market_states or token_id not in self.positions:
                return None
            
            state = self.market_states[token_id]
            position = self.positions[token_id]
            
            # Analyze boundary condition
            base_sigma = self._calculate_volatility(state)
            boundary = self.boundary_engine.analyze_boundary_condition(
                mid_price=state.mid_price,
                inventory=position.quantity,
                base_sigma=base_sigma
            )
            
            # Check if should cease aggressive quoting
            if self.boundary_engine.should_cease_aggressive_quoting(
                boundary, position.quantity
            ):
                logger.warning(
                    f"[{token_id[:8]}] SKEW CAP ACTIVE - "
                    f"Ceasing aggressive quoting in {boundary.regime} regime "
                    f"(inventory: {position.quantity:+.0f})"
                )
                # Don't place new quotes, only manage existing
                return None
            
            # Calculate reservation price (with boundary adjustments)
            r = self._calculate_reservation_price(state, position)
            
            # Calculate optimal spread
            base_spread = self._calculate_optimal_spread(state)
            
            # Apply asymmetric spread for boundary conditions
            bid_spread, ask_spread = self.boundary_engine.calculate_asymmetric_spread(
                mid_price=state.mid_price,
                base_spread=base_spread,
                regime=boundary.regime
            )
            
            # Calculate bid/ask with asymmetric spreads
            bid = r - bid_spread
            ask = r + ask_spread
            
            # Ensure quotes are within valid range [0.01, 0.99]
            bid = max(0.01, min(0.99, bid))
            ask = max(0.01, min(0.99, ask))
            
            # Ensure bid < ask
            if bid >= ask:
                mid = (bid + ask) / 2
                min_spread = 0.001  # Minimum 0.1% spread
                bid = mid - min_spread / 2
                ask = mid + min_spread / 2
            
            # Log boundary-adjusted quotes
            if boundary.is_extreme:
                logger.debug(
                    f"[{token_id[:8]}] BOUNDARY QUOTES: "
                    f"Bid=${bid:.4f} (spread: {bid_spread*10000:.1f}bps), "
                    f"Ask=${ask:.4f} (spread: {ask_spread*10000:.1f}bps), "
                    f"Regime: {boundary.regime}, "
                    f"Passive: {boundary.passive_only_mode}"
                )
        
        except Exception as e:
            logger.error(f"Error in boundary quote calculation: {e}")
            # Fall through to standard calculation
        
        try:
            if token_id not in self.market_states or token_id not in self.positions:
                return None
            
            state = self.market_states[token_id]
            position = self.positions[token_id]
            
            # Calculate reservation price
            r = self._calculate_reservation_price(state, position)
            
            # Calculate optimal spread
            delta = self._calculate_optimal_spread(state)
            
            # Calculate bid/ask
            bid = r - delta / 2
            ask = r + delta / 2
            
            # Ensure quotes are within valid range [0.01, 0.99]
            bid = max(0.01, min(0.99, bid))
            ask = max(0.01, min(0.99, ask))
            
            # Ensure bid < ask
            if bid >= ask:
                mid = (bid + ask) / 2
                spread = 0.001  # Minimum 0.1% spread
                bid = mid - spread / 2
                ask = mid + spread / 2
            
            return (bid, ask)
            
        except Exception as e:
            logger.error(f"Error calculating quotes for {token_id[:8]}...: {e}")
            return None
    
    # ========================================================================
    # Quote Management
    # ========================================================================
    
    async def _quote_loop(self) -> None:
        """Main quoting loop - re-evaluate and update quotes"""
        logger.info("Quote loop started")
        
        interval_ms = self.config['quote_update_interval_ms']
        interval_sec = interval_ms / 1000.0
        
        while self.is_running and not self.is_killed:
            try:
                # Update all markets
                for token_id in self.markets:
                    if token_id in self.market_states:
                        await self._update_quotes(token_id)
                
                await asyncio.sleep(interval_sec)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in quote loop: {e}", exc_info=True)
                await asyncio.sleep(1)
    
    async def _update_quotes(self, token_id: str) -> None:
        """Update quotes for a single token"""
        try:
            async with self._quote_lock:
                # Check if should requote
                if not await self._should_requote(token_id):
                    return
                
                # Calculate new quotes
                quotes = await self._calculate_quotes(token_id)
                
                if quotes is None:
                    return
                
                bid_price, ask_price = quotes
                
                # Cancel existing quotes
                await self._cancel_quotes_for_token(token_id)
                
                # Place new quotes (POST-ONLY for maker rebates)
                quote_size = self.config['quote_size']
                
                # Place bid
                bid_result = await self.order_manager.place_limit_order(
                    token_id=token_id,
                    side='BUY',
                    size=quote_size,
                    price=bid_price,
                    post_only=True,  # 2026 maker rebates
                    time_in_force='GTC'
                )
                
                # Place ask
                ask_result = await self.order_manager.place_limit_order(
                    token_id=token_id,
                    side='SELL',
                    size=quote_size,
                    price=ask_price,
                    post_only=True,  # 2026 maker rebates
                    time_in_force='GTC'
                )
                
                # Track active quotes
                bid_order_id = bid_result.get('orderID') if bid_result else None
                ask_order_id = ask_result.get('orderID') if ask_result else None
                
                self.active_quotes[token_id] = [
                    oid for oid in [bid_order_id, ask_order_id] if oid
                ]
                
                self.total_quotes_placed += 2
                
                # Track order rejections for safety monitoring
                self.safety_metrics.total_orders_5min += 2
                if not bid_order_id:
                    self.safety_metrics.rejection_count_5min += 1
                if not ask_order_id:
                    self.safety_metrics.rejection_count_5min += 1
                
                state = self.market_states[token_id]
                logger.debug(
                    f"[{token_id[:8]}] Quotes: Bid={quote_size}@${bid_price:.4f}, "
                    f"Ask={quote_size}@${ask_price:.4f}, "
                    f"Mid=${state.mid_price:.4f}, "
                    f"Spread={(ask_price - bid_price)*10000:.1f}bps"
                )
                
        except Exception as e:
            logger.error(f"Error updating quotes for {token_id[:8]}...: {e}")
    
    async def _should_requote(self, token_id: str) -> bool:
        """Determine if should update quotes for token"""
        if token_id not in self.market_states:
            return False
        
        # Always requote if no active quotes
        if token_id not in self.active_quotes or not self.active_quotes[token_id]:
            return True
        
        # Check if price moved beyond threshold
        state = self.market_states[token_id]
        
        # Get last quoted mid-price (from reservation price calculation)
        # For simplicity, requote on any significant move
        threshold_bps = self.config['requote_threshold_bps']
        threshold = threshold_bps / 10000
        
        # Simple heuristic: requote every N updates
        # In production, would track last quoted price
        return True  # Always evaluate (actual placement throttled by quote_update_interval)
    
    async def _cancel_quotes_for_token(self, token_id: str) -> None:
        """Cancel all active quotes for a token"""
        if token_id not in self.active_quotes:
            return
        
        order_ids = self.active_quotes.get(token_id, [])
        
        for order_id in order_ids:
            try:
                await self.order_manager.cancel_order(order_id, token_id)
            except Exception as e:
                logger.debug(f"Failed to cancel order {order_id[:8]}...: {e}")
        
        self.active_quotes[token_id] = []
    
    async def _cancel_all_quotes(self) -> None:
        """Cancel all active quotes across all markets"""
        logger.info("Cancelling all active quotes...")
        
        cancel_tasks = []
        
        for token_id in list(self.active_quotes.keys()):
            cancel_tasks.append(self._cancel_quotes_for_token(token_id))
        
        if cancel_tasks:
            await asyncio.gather(*cancel_tasks, return_exceptions=True)
        
        logger.info(f"Cancelled quotes for {len(cancel_tasks)} markets")
    
    # ========================================================================
    # Safety Monitoring (2026 Institution-Grade)
    # ========================================================================
    
    async def _safety_monitor(self) -> None:
        """Continuous safety monitoring with emergency kill-switch.
        
        Monitors:
        1. Cumulative PnL (over 2% loss in 60-min window)
        2. API latency (over 450ms for 3 consecutive pings)
        3. Order rejection rate (over 5% in 5-min window)
        4. Inventory limits (over 5,000 contracts)
        """
        logger.info("Safety monitor started")
        
        interval = self.config['safety_check_interval_sec']
        
        while self.is_running and not self.is_killed:
            try:
                await asyncio.sleep(interval)
                
                # Run all safety checks
                await self.check_safety()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in safety monitor: {e}", exc_info=True)
    
    async def check_safety(self) -> None:
        """
        Emergency kill-switch checks
        
        Triggers cancel_all_orders() and halts bot if:
        1. Cumulative Loss: PnL drops >2% in 60-min window
        2. API Latency: Response time >450ms for 3 consecutive pings
        3. Order Rejection Rate: >5% in 5-min window
        4. Inventory Trap: |q| > 5,000 contracts for any token
        """
        try:
            # Check 1: Cumulative PnL
            await self._check_pnl_drawdown()
            
            # Check 2: API latency
            await self._check_api_latency()
            
            # Check 3: Order rejection rate
            await self._check_rejection_rate()
            
            # Check 4: Inventory limits
            await self._check_inventory_limits()
            
        except CircuitBreakerError as e:
            # Emergency kill-switch triggered
            logger.critical(f"🚨 KILL-SWITCH ACTIVATED: {e}")
            await self._emergency_shutdown(str(e))
        except Exception as e:
            logger.error(f"Error in safety checks: {e}", exc_info=True)
    
    async def _check_pnl_drawdown(self) -> None:
        """Check if PnL drawdown exceeds threshold"""
        # Calculate cumulative PnL
        total_realized_pnl = sum(p.realized_pnl for p in self.positions.values())
        total_unrealized_pnl = sum(p.unrealized_pnl for p in self.positions.values())
        total_pnl = total_realized_pnl + total_unrealized_pnl
        
        # Update metrics
        self.safety_metrics.cumulative_pnl = total_pnl
        self.safety_metrics.pnl_history.append((time.time(), total_pnl))
        
        # Calculate max drawdown in 60-min window
        if len(self.safety_metrics.pnl_history) > 0:
            pnls = [pnl for _, pnl in self.safety_metrics.pnl_history]
            max_pnl = max(pnls)
            current_pnl = pnls[-1]
            drawdown = max_pnl - current_pnl
            drawdown_pct = (drawdown / abs(max_pnl) * 100) if max_pnl != 0 else 0
            
            self.safety_metrics.max_drawdown_1h = drawdown_pct
            
            # Trigger kill-switch if exceeds threshold
            max_dd_pct = self.config['max_drawdown_pct']
            if drawdown_pct > max_dd_pct:
                raise CircuitBreakerError(
                    f"PnL drawdown {drawdown_pct:.2f}% exceeds limit {max_dd_pct}%"
                )
    
    async def _check_api_latency(self) -> None:
        """Check API latency and trigger kill-switch if excessive"""
        try:
            # Ping CLOB with simple request
            start_time = time.time()
            
            # Use a lightweight API call
            if self.markets:
                await self.client.get_order_book(self.markets[0])
            
            latency_ms = (time.time() - start_time) * 1000
            
            # Update metrics
            self.safety_metrics.latency_history.append(latency_ms)
            
            # Check if latency exceeds threshold
            max_latency = self.config['max_api_latency_ms']
            
            if latency_ms > max_latency:
                self.safety_metrics.consecutive_high_latency += 1
                logger.warning(
                    f"High API latency: {latency_ms:.1f}ms "
                    f"({self.safety_metrics.consecutive_high_latency}/3)"
                )
            else:
                self.safety_metrics.consecutive_high_latency = 0
            
            # Trigger kill-switch after 3 consecutive high-latency pings
            if self.safety_metrics.consecutive_high_latency >= 3:
                raise CircuitBreakerError(
                    f"API latency {latency_ms:.1f}ms exceeds {max_latency}ms "
                    f"for 3 consecutive pings"
                )
                
        except CircuitBreakerError:
            raise
        except Exception as e:
            logger.error(f"Error checking API latency: {e}")
    
    async def _check_rejection_rate(self) -> None:
        """Check order rejection rate and trigger circuit breaker if excessive"""
        metrics = self.safety_metrics
        
        if metrics.total_orders_5min == 0:
            return
        
        rejection_rate = (metrics.rejection_count_5min / metrics.total_orders_5min) * 100
        
        max_rejection_rate = self.config['max_rejection_rate_pct']
        
        if rejection_rate > max_rejection_rate:
            raise CircuitBreakerError(
                f"Order rejection rate {rejection_rate:.1f}% exceeds limit {max_rejection_rate}%"
            )
        
        # Reset counters every 5 minutes
        if time.time() % 300 < 5:  # Reset at 5-minute boundaries
            metrics.rejection_count_5min = 0
            metrics.total_orders_5min = 0
    
    async def _check_inventory_limits(self) -> None:
        """Check inventory limits and trigger kill-switch if exceeded"""
        max_inventory = self.config['max_inventory_contracts']
        
        for token_id, position in self.positions.items():
            abs_quantity = abs(position.quantity)
            
            if abs_quantity > max_inventory:
                self.safety_metrics.max_inventory_breach_count += 1
                
                raise CircuitBreakerError(
                    f"Inventory for {token_id[:8]}... ({abs_quantity:,.0f} contracts) "
                    f"exceeds limit {max_inventory:,}"
                )
    
    async def _emergency_shutdown(self, reason: str) -> None:
        """
        Emergency shutdown - cancel all orders and halt bot
        
        Args:
            reason: Reason for shutdown
        """
        logger.critical(
            f"🚨 EMERGENCY SHUTDOWN INITIATED 🚨\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now().isoformat()}"
        )
        
        self.is_killed = True
        
        try:
            # Cancel all outstanding orders
            await self._cancel_all_quotes()
            
            # Log final positions
            logger.critical("Final positions at shutdown:")
            for token_id, position in self.positions.items():
                logger.critical(
                    f"  {token_id[:8]}...: {position.quantity:+.1f} contracts, "
                    f"Realized PnL: ${position.realized_pnl:+.2f}, "
                    f"Unrealized PnL: ${position.unrealized_pnl:+.2f}"
                )
            
            # Stop the bot
            self.is_running = False
            
        except Exception as e:
            logger.critical(f"Error during emergency shutdown: {e}", exc_info=True)
    
    # ========================================================================
    # WebSocket Heartbeat Monitor
    # ========================================================================
    
    async def _heartbeat_monitor(self) -> None:
        """
        Monitor WebSocket connection health with heartbeat
        
        Ensures connection to Polymarket CLOB is active.
        """
        logger.info("Heartbeat monitor started")
        
        interval = self.config['heartbeat_interval_sec']
        timeout = self.config['heartbeat_timeout_sec']
        
        while self.is_running and not self.is_killed:
            try:
                # Send heartbeat (simple book fetch)
                if self.markets:
                    start_time = time.time()
                    
                    # Attempt to fetch book via WebSocket cache
                    token_id = self.markets[0]
                    book_data = self.market_data_manager.get_order_book(token_id)
                    
                    if book_data:
                        self.safety_metrics.last_heartbeat = time.time()
                        self.safety_metrics.heartbeat_failures = 0
                    else:
                        # Fallback to REST
                        await self.client.get_order_book(token_id)
                        self.safety_metrics.last_heartbeat = time.time()
                        self.safety_metrics.heartbeat_failures = 0
                
                await asyncio.sleep(interval)
                
                # Check if heartbeat timed out
                time_since_heartbeat = time.time() - self.safety_metrics.last_heartbeat
                
                if time_since_heartbeat > timeout:
                    self.safety_metrics.heartbeat_failures += 1
                    logger.warning(
                        f"Heartbeat timeout: {time_since_heartbeat:.1f}s "
                        f"(failures: {self.safety_metrics.heartbeat_failures})"
                    )
                    
                    # Trigger emergency shutdown after 3 consecutive failures
                    if self.safety_metrics.heartbeat_failures >= 3:
                        raise CircuitBreakerError(
                            f"WebSocket heartbeat failed for {time_since_heartbeat:.1f}s"
                        )
                
            except CircuitBreakerError as e:
                logger.critical(f"Heartbeat failure: {e}")
                await self._emergency_shutdown(str(e))
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in heartbeat monitor: {e}")
                await asyncio.sleep(interval)
    
    # ========================================================================
    # Metrics and Logging
    # ========================================================================
    
    async def _metrics_logger(self) -> None:
        """Periodic metrics logging"""
        while self.is_running and not self.is_killed:
            try:
                await asyncio.sleep(60)  # Log every 60 seconds
                
                self._log_metrics()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in metrics logger: {e}")
    
    def _log_metrics(self) -> None:
        """Log current metrics"""
        uptime = time.time() - self.start_time
        uptime_str = f"{int(uptime // 3600)}h {int((uptime % 3600) // 60)}m"
        
        # Calculate total PnL
        total_realized = sum(p.realized_pnl for p in self.positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        total_pnl = total_realized + total_unrealized
        
        # Calculate total inventory
        total_inventory = sum(abs(p.quantity) for p in self.positions.values())
        
        # Get average latency
        avg_latency = (
            statistics.mean(self.safety_metrics.latency_history)
            if self.safety_metrics.latency_history else 0
        )
        
        logger.info(
            f"📊 [A-S MM Metrics] Uptime: {uptime_str}\n"
            f"   Quotes placed: {self.total_quotes_placed:,}\n"
            f"   Fills: {self.total_fills:,}\n"
            f"   Total PnL: ${total_pnl:+.2f} (R: ${total_realized:+.2f}, U: ${total_unrealized:+.2f})\n"
            f"   Total inventory: {total_inventory:,.0f} contracts\n"
            f"   Avg API latency: {avg_latency:.1f}ms\n"
            f"   Max drawdown (1h): {self.safety_metrics.max_drawdown_1h:.2f}%"
        )
    
    def _log_final_metrics(self) -> None:
        """Log final metrics at shutdown"""
        total_realized = sum(p.realized_pnl for p in self.positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        total_pnl = total_realized + total_unrealized
        
        logger.info(
            f"📊 [FINAL METRICS]\n"
            f"   Total quotes: {self.total_quotes_placed:,}\n"
            f"   Total fills: {self.total_fills:,}\n"
            f"   Final PnL: ${total_pnl:+.2f}\n"
            f"   Kill-switch triggered: {self.is_killed}"
        )
    
    # ========================================================================
    # Public API
    # ========================================================================
    
    def get_status(self) -> Dict[str, Any]:
        """Get current strategy status"""
        total_realized = sum(p.realized_pnl for p in self.positions.values())
        total_unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        
        return {
            'is_running': self.is_running,
            'is_killed': self.is_killed,
            'uptime_seconds': time.time() - self.start_time,
            'markets': len(self.markets),
            'total_quotes': self.total_quotes_placed,
            'total_fills': self.total_fills,
            'realized_pnl': total_realized,
            'unrealized_pnl': total_unrealized,
            'total_pnl': total_realized + total_unrealized,
            'positions': {
                tid[:8]: {
                    'quantity': p.quantity,
                    'realized_pnl': p.realized_pnl,
                    'unrealized_pnl': p.unrealized_pnl
                }
                for tid, p in self.positions.items()
            },
            'safety_metrics': {
                'max_drawdown_1h_pct': self.safety_metrics.max_drawdown_1h,
                'consecutive_high_latency': self.safety_metrics.consecutive_high_latency,
                'rejection_rate_5min': (
                    self.safety_metrics.rejection_count_5min / 
                    self.safety_metrics.total_orders_5min * 100
                    if self.safety_metrics.total_orders_5min > 0 else 0
                ),
                'heartbeat_failures': self.safety_metrics.heartbeat_failures
            }
        }
