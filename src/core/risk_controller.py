"""
Risk Controller - HFT-Grade Risk Management System

Implements institutional-grade risk controls for high-frequency trading:
1. Kill Switch: Auto-shutdown on connection loss or large drawdowns
2. Position Limits: Enforce per-market and global position caps
3. Circuit Breaker: Pause trading on abnormal market conditions
4. P&L Monitoring: Track real-time profit/loss with equity protection
5. Inventory Risk: Monitor and limit directional exposure

Safety Philosophy:
- Fail-safe defaults (stop trading on uncertainty)
- Multiple independent safety layers
- Transparent state tracking for auditing
- Automatic recovery procedures

Author: Senior Quant Developer
Date: January 15, 2026
"""

import asyncio
import time
from typing import Dict, List, Optional, Set, Callable, Any
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime, timedelta
from enum import Enum

from utils.logger import get_logger
from utils.exceptions import CircuitBreakerError


logger = get_logger(__name__)


class RiskLevel(Enum):
    """Risk severity levels"""
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    EMERGENCY = "EMERGENCY"


class TradingState(Enum):
    """Bot trading state"""
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"
    KILL_SWITCH = "KILL_SWITCH"
    LIQUIDATION = "LIQUIDATION"


@dataclass
class PositionRisk:
    """Position risk metrics for a single market"""
    market_id: str
    token_id: str
    position_size: float  # Shares held (signed: + = long, - = short)
    market_value: float  # Current USD value
    unrealized_pnl: float  # Mark-to-market P&L
    entry_price: float
    current_price: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class EquitySnapshot:
    """Account equity snapshot"""
    timestamp: float
    cash_balance: float
    position_value: float
    unrealized_pnl: float
    realized_pnl: float
    total_equity: float
    
    @property
    def equity_change_pct(self) -> float:
        """Calculate % change from initial equity"""
        if not hasattr(self, '_initial_equity'):
            return 0.0
        return (self.total_equity - self._initial_equity) / self._initial_equity if self._initial_equity > 0 else 0.0


class RiskController:
    """
    HFT-Grade Risk Management System
    
    Features:
    - Real-time P&L tracking with 10-minute rolling window
    - Position limits per market and globally
    - Kill switch on connection loss or drawdown
    - Circuit breaker on abnormal spreads
    - Inventory risk monitoring
    - Automatic order cancellation on risk events
    """
    
    def __init__(
        self,
        initial_capital: float,
        max_drawdown_pct: float = 0.05,  # 5% max drawdown
        max_position_size_usd: float = 5000.0,  # $5k per market
        max_total_position_usd: float = 50000.0,  # $50k total
        max_spread_ticks: int = 50,  # 50 ticks = abnormal spread
        drawdown_window_sec: int = 600,  # 10 minutes
        heartbeat_timeout_sec: int = 30,  # Connection timeout
    ):
        """
        Initialize risk controller
        
        Args:
            initial_capital: Starting capital in USD
            max_drawdown_pct: Maximum allowed drawdown (0.02 = 2%)
            max_position_size_usd: Max position size per market
            max_total_position_usd: Max total position exposure
            max_spread_ticks: Circuit breaker spread threshold
            drawdown_window_sec: Time window for drawdown calculation
            heartbeat_timeout_sec: Connection timeout threshold
        """
        self.initial_capital = Decimal(str(initial_capital))
        self.max_drawdown_pct = max_drawdown_pct
        self.max_position_size_usd = max_position_size_usd
        self.max_total_position_usd = max_total_position_usd
        self.max_spread_ticks = max_spread_ticks
        self.drawdown_window_sec = drawdown_window_sec
        self.heartbeat_timeout_sec = heartbeat_timeout_sec
        
        # State tracking
        self.trading_state = TradingState.ACTIVE
        self.risk_level = RiskLevel.NORMAL
        
        # P&L tracking
        self._equity_history: List[EquitySnapshot] = []
        self._realized_pnl = Decimal('0')
        self._peak_equity = self.initial_capital
        self._current_equity = self.initial_capital
        
        # Position tracking
        self._positions: Dict[str, PositionRisk] = {}
        self._position_limits: Dict[str, float] = {}  # market_id -> limit
        
        # Connection health
        self._last_heartbeat: Dict[str, float] = {}  # feed_name -> timestamp
        self._connection_healthy = True
        
        # Circuit breaker state
        self._circuit_breaker_count = 0
        self._circuit_breaker_reset_time: Optional[float] = None
        
        # Kill switch callbacks
        self._kill_switch_callbacks: List[Callable] = []
        self._circuit_breaker_callbacks: List[Callable] = []
        
        # Monitoring task
        self._monitor_task: Optional[asyncio.Task] = None
        self._is_monitoring = False
        
        logger.info(
            f"🛡️  RiskController initialized:\n"
            f"   Initial Capital: ${initial_capital:,.2f}\n"
            f"   Max Drawdown: {max_drawdown_pct*100:.1f}%\n"
            f"   Max Position/Market: ${max_position_size_usd:,.0f}\n"
            f"   Max Total Position: ${max_total_position_usd:,.0f}\n"
            f"   Circuit Breaker Spread: {max_spread_ticks} ticks\n"
            f"   Drawdown Window: {drawdown_window_sec}s"
        )
    
    # ========================================================================
    # Core Risk Checks
    # ========================================================================
    
    def can_open_position(
        self,
        market_id: str,
        token_id: str,
        size_usd: float,
        side: str
    ) -> tuple[bool, Optional[str]]:
        """
        Check if new position can be opened
        
        Args:
            market_id: Market identifier
            token_id: Token identifier
            size_usd: Position size in USD
            side: 'BUY' or 'SELL'
            
        Returns:
            (can_open, reason_if_not)
        """
        # Check trading state
        if self.trading_state != TradingState.ACTIVE:
            return False, f"Trading paused: {self.trading_state.value}"
        
        # Check per-market limit
        current_position = self._positions.get(token_id)
        if current_position:
            new_position_size = abs(current_position.market_value) + size_usd
        else:
            new_position_size = size_usd
        
        market_limit = self._position_limits.get(market_id, self.max_position_size_usd)
        if new_position_size > market_limit:
            return False, f"Market position limit: ${new_position_size:.0f} > ${market_limit:.0f}"
        
        # Check global limit
        total_position_value = sum(abs(p.market_value) for p in self._positions.values())
        if total_position_value + size_usd > self.max_total_position_usd:
            return False, f"Global position limit: ${total_position_value + size_usd:.0f} > ${self.max_total_position_usd:.0f}"
        
        # Check capital availability
        available_capital = float(self._current_equity)
        if size_usd > available_capital * 0.95:  # Leave 5% buffer
            return False, f"Insufficient capital: ${size_usd:.0f} > ${available_capital*0.95:.0f}"
        
        return True, None
    
    def check_spread_sanity(
        self,
        market_id: str,
        bid: float,
        ask: float,
        tick_size: float = 0.01
    ) -> tuple[bool, Optional[str]]:
        """
        Check if spread is within sane bounds (circuit breaker)
        
        Args:
            market_id: Market identifier
            bid: Best bid price
            ask: Best ask price
            tick_size: Market tick size
            
        Returns:
            (is_sane, reason_if_not)
        """
        spread = ask - bid
        spread_ticks = spread / tick_size
        
        if spread_ticks > self.max_spread_ticks:
            return False, f"Abnormal spread: {spread_ticks:.0f} ticks > {self.max_spread_ticks} ticks"
        
        # Check for crossed book
        if bid >= ask:
            return False, f"Crossed book: bid ${bid:.4f} >= ask ${ask:.4f}"
        
        # Check for zero/negative prices
        if bid <= 0 or ask <= 0:
            return False, f"Invalid prices: bid ${bid:.4f}, ask ${ask:.4f}"
        
        return True, None
    
    def update_position(
        self,
        market_id: str,
        token_id: str,
        size_change: float,
        price: float,
        side: str
    ) -> None:
        """
        Update position after trade execution
        
        Args:
            market_id: Market identifier
            token_id: Token identifier
            size_change: Change in position size (shares)
            price: Execution price
            side: 'BUY' or 'SELL'
        """
        current_position = self._positions.get(token_id)
        
        if current_position is None:
            # New position
            self._positions[token_id] = PositionRisk(
                market_id=market_id,
                token_id=token_id,
                position_size=size_change if side == 'BUY' else -size_change,
                market_value=size_change * price,
                unrealized_pnl=0.0,
                entry_price=price,
                current_price=price
            )
        else:
            # Update existing position
            old_size = current_position.position_size
            
            if side == 'BUY':
                new_size = old_size + size_change
            else:
                new_size = old_size - size_change
            
            # Calculate realized P&L if reducing position
            if (old_size > 0 and side == 'SELL') or (old_size < 0 and side == 'BUY'):
                realized = abs(size_change) * (price - current_position.entry_price)
                self._realized_pnl += Decimal(str(realized))
            
            # Update position
            if abs(new_size) < 0.01:  # Position closed
                del self._positions[token_id]
                logger.info(f"Position closed: {token_id[:8]}...")
            else:
                # Recalculate average entry if adding to position
                if (old_size > 0 and side == 'BUY') or (old_size < 0 and side == 'SELL'):
                    total_cost = (old_size * current_position.entry_price) + (size_change * price)
                    new_entry = total_cost / new_size
                else:
                    new_entry = current_position.entry_price
                
                current_position.position_size = new_size
                current_position.entry_price = new_entry
                current_position.current_price = price
                current_position.market_value = new_size * price
                current_position.timestamp = time.time()
    
    def update_mark_to_market(
        self,
        token_id: str,
        current_price: float
    ) -> None:
        """
        Update position mark-to-market with current price
        
        Args:
            token_id: Token identifier
            current_price: Current market price
        """
        position = self._positions.get(token_id)
        if position:
            position.current_price = current_price
            position.market_value = position.position_size * current_price
            position.unrealized_pnl = position.position_size * (current_price - position.entry_price)
            position.timestamp = time.time()
    
    def calculate_current_equity(self, cash_balance: float) -> EquitySnapshot:
        """
        Calculate current account equity snapshot
        
        Args:
            cash_balance: Current cash balance in USD
            
        Returns:
            EquitySnapshot with current equity metrics
        """
        position_value = sum(p.market_value for p in self._positions.values())
        unrealized_pnl = sum(p.unrealized_pnl for p in self._positions.values())
        total_equity = cash_balance + position_value
        
        snapshot = EquitySnapshot(
            timestamp=time.time(),
            cash_balance=cash_balance,
            position_value=position_value,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=float(self._realized_pnl),
            total_equity=total_equity
        )
        
        # Track equity history
        self._equity_history.append(snapshot)
        self._current_equity = Decimal(str(total_equity))
        
        # Update peak equity
        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity
        
        # Trim old history (keep only drawdown window)
        cutoff_time = time.time() - self.drawdown_window_sec
        self._equity_history = [s for s in self._equity_history if s.timestamp > cutoff_time]
        
        return snapshot
    
    # ========================================================================
    # Kill Switch & Circuit Breaker
    # ========================================================================
    
    async def check_drawdown_limit(self, current_equity: float) -> None:
        """
        Check if drawdown exceeds limit (KILL SWITCH)
        
        Args:
            current_equity: Current account equity
        """
        # Calculate drawdown from peak
        drawdown = (float(self._peak_equity) - current_equity) / float(self._peak_equity)
        
        if drawdown >= self.max_drawdown_pct:
            logger.critical(
                f"🚨 KILL SWITCH TRIGGERED: Drawdown {drawdown*100:.2f}% >= {self.max_drawdown_pct*100:.1f}%\n"
                f"   Peak Equity: ${self._peak_equity:,.2f}\n"
                f"   Current Equity: ${current_equity:,.2f}\n"
                f"   Drawdown: ${float(self._peak_equity) - current_equity:,.2f}"
            )
            
            await self.trigger_kill_switch(reason=f"Drawdown {drawdown*100:.2f}%")
    
    async def check_connection_health(self) -> None:
        """
        Check WebSocket connection health (KILL SWITCH)
        
        Triggers kill switch if no heartbeat received within timeout
        """
        if not self._last_heartbeat:
            return  # No feeds registered yet
        
        current_time = time.time()
        unhealthy_feeds = []
        
        for feed_name, last_time in self._last_heartbeat.items():
            if current_time - last_time > self.heartbeat_timeout_sec:
                unhealthy_feeds.append(feed_name)
        
        if unhealthy_feeds:
            logger.critical(
                f"🚨 CONNECTION LOSS DETECTED: {', '.join(unhealthy_feeds)}\n"
                f"   Timeout: {self.heartbeat_timeout_sec}s\n"
                f"   Last heartbeat: {current_time - min(self._last_heartbeat.values()):.0f}s ago"
            )
            
            self._connection_healthy = False
            await self.trigger_kill_switch(reason=f"Connection loss: {unhealthy_feeds}")
    
    async def trigger_kill_switch(self, reason: str) -> None:
        """
        Activate kill switch - emergency shutdown
        
        Actions:
        1. Cancel all open orders
        2. Set state to KILL_SWITCH
        3. Call registered callbacks
        4. Log emergency event
        
        Args:
            reason: Reason for kill switch activation
        """
        if self.trading_state == TradingState.KILL_SWITCH:
            return  # Already triggered
        
        logger.critical(
            f"🛑 KILL SWITCH ACTIVATED: {reason}\n"
            f"   Previous State: {self.trading_state.value}\n"
            f"   Current Equity: ${self._current_equity:,.2f}\n"
            f"   Peak Equity: ${self._peak_equity:,.2f}\n"
            f"   Realized P&L: ${self._realized_pnl:,.2f}\n"
            f"   Open Positions: {len(self._positions)}"
        )
        
        self.trading_state = TradingState.KILL_SWITCH
        self.risk_level = RiskLevel.EMERGENCY
        
        # Execute kill switch callbacks (cancel all orders, etc.)
        for callback in self._kill_switch_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(reason)
                else:
                    callback(reason)
            except Exception as e:
                logger.error(f"Kill switch callback error: {e}")
    
    async def trigger_circuit_breaker(self, reason: str, duration_sec: int = 60) -> None:
        """
        Activate circuit breaker - temporary pause
        
        Actions:
        1. Cancel all open orders
        2. Set state to CIRCUIT_BREAKER
        3. Call registered callbacks
        4. Auto-reset after duration
        
        Args:
            reason: Reason for circuit breaker
            duration_sec: Pause duration in seconds
        """
        if self.trading_state in [TradingState.KILL_SWITCH, TradingState.CIRCUIT_BREAKER]:
            return  # Already triggered or higher severity
        
        self._circuit_breaker_count += 1
        self._circuit_breaker_reset_time = time.time() + duration_sec
        
        logger.warning(
            f"⚡ CIRCUIT BREAKER ACTIVATED: {reason}\n"
            f"   Count: {self._circuit_breaker_count}\n"
            f"   Duration: {duration_sec}s\n"
            f"   Reset at: {datetime.fromtimestamp(self._circuit_breaker_reset_time).strftime('%H:%M:%S')}"
        )
        
        self.trading_state = TradingState.CIRCUIT_BREAKER
        self.risk_level = RiskLevel.CRITICAL
        
        # Execute callbacks
        for callback in self._circuit_breaker_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(reason, duration_sec)
                else:
                    callback(reason, duration_sec)
            except Exception as e:
                logger.error(f"Circuit breaker callback error: {e}")
    
    def reset_circuit_breaker(self) -> None:
        """Reset circuit breaker if time has elapsed"""
        if self.trading_state != TradingState.CIRCUIT_BREAKER:
            return
        
        if self._circuit_breaker_reset_time and time.time() >= self._circuit_breaker_reset_time:
            logger.info(
                f"✅ Circuit breaker reset\n"
                f"   Total activations: {self._circuit_breaker_count}"
            )
            
            self.trading_state = TradingState.ACTIVE
            self.risk_level = RiskLevel.NORMAL
            self._circuit_breaker_reset_time = None
    
    # ========================================================================
    # Monitoring & Callbacks
    # ========================================================================
    
    def update_heartbeat(self, feed_name: str) -> None:
        """
        Update heartbeat timestamp for feed
        
        Args:
            feed_name: Feed identifier (e.g., 'CLOB', 'RTDS', 'Binance')
        """
        self._last_heartbeat[feed_name] = time.time()
        
        if not self._connection_healthy:
            # Check if all feeds are now healthy
            all_healthy = all(
                time.time() - ts < self.heartbeat_timeout_sec
                for ts in self._last_heartbeat.values()
            )
            
            if all_healthy:
                logger.info("✅ Connection health restored")
                self._connection_healthy = True
    
    def register_kill_switch_callback(self, callback: Callable) -> None:
        """Register callback for kill switch events"""
        self._kill_switch_callbacks.append(callback)
    
    def register_circuit_breaker_callback(self, callback: Callable) -> None:
        """Register callback for circuit breaker events"""
        self._circuit_breaker_callbacks.append(callback)
    
    async def start_monitoring(self) -> None:
        """Start background risk monitoring task"""
        if self._monitor_task and not self._monitor_task.done():
            logger.warning("Risk monitoring already running")
            return
        
        self._is_monitoring = True
        self._monitor_task = asyncio.create_task(self._monitoring_loop())
        logger.info("📊 Risk monitoring started")
    
    async def stop_monitoring(self) -> None:
        """Stop background risk monitoring"""
        self._is_monitoring = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("📊 Risk monitoring stopped")
    
    async def _monitoring_loop(self) -> None:
        """Background monitoring loop - runs every second"""
        while self._is_monitoring:
            try:
                await asyncio.sleep(1)
                
                # Check circuit breaker reset
                self.reset_circuit_breaker()
                
                # Check connection health
                await self.check_connection_health()
                
            except Exception as e:
                logger.error(f"Risk monitoring error: {e}", exc_info=True)
    
    # ========================================================================
    # Status & Reporting
    # ========================================================================
    
    def get_risk_status(self) -> Dict[str, Any]:
        """Get current risk status summary"""
        total_position_value = sum(abs(p.market_value) for p in self._positions.values())
        total_unrealized_pnl = sum(p.unrealized_pnl for p in self._positions.values())
        
        return {
            'trading_state': self.trading_state.value,
            'risk_level': self.risk_level.value,
            'connection_healthy': self._connection_healthy,
            'current_equity': float(self._current_equity),
            'peak_equity': float(self._peak_equity),
            'drawdown_pct': float((self._peak_equity - self._current_equity) / self._peak_equity) if self._peak_equity > 0 else 0.0,
            'max_drawdown_pct': self.max_drawdown_pct,
            'realized_pnl': float(self._realized_pnl),
            'unrealized_pnl': total_unrealized_pnl,
            'total_pnl': float(self._realized_pnl) + total_unrealized_pnl,
            'open_positions': len(self._positions),
            'total_position_value': total_position_value,
            'position_utilization_pct': (total_position_value / self.max_total_position_usd) * 100,
            'circuit_breaker_count': self._circuit_breaker_count,
        }
    
    def get_positions_summary(self) -> List[Dict[str, Any]]:
        """Get summary of all open positions"""
        return [
            {
                'token_id': p.token_id[:12] + '...',
                'market_id': p.market_id[:12] + '...',
                'size': p.position_size,
                'entry_price': p.entry_price,
                'current_price': p.current_price,
                'market_value': p.market_value,
                'unrealized_pnl': p.unrealized_pnl,
                'pnl_pct': (p.unrealized_pnl / abs(p.market_value)) * 100 if p.market_value != 0 else 0.0,
            }
            for p in self._positions.values()
        ]
