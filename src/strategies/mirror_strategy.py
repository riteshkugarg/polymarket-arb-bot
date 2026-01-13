"""
Mirror Trading Strategy - Production Grade Implementation

This strategy implements 3 loosely-coupled parallel flows for maximum efficiency:

FLOW 1: TRADE MIRRORING (Frequent - every 2-5 seconds)
────────────────────────────────────────────────────
Continuously monitors the whale's recent trades and copies them immediately.
- Fetches whale's trades from last 10 minutes
- Analyzes price, size, and market conditions
- Places buy/sell orders to match whale's trades
- Applies safety guards (price bounds, slippage, position limits)
- Updates balance cache after each trade
Benefits: Catches market opportunities early, low latency entry

FLOW 2: POSITION ALIGNMENT (Less frequent - every 60 seconds)
──────────────────────────────────────────────────────────
Detects when whale exits positions and immediately sells matching positions.
- Checks whale's closed positions
- Identifies positions we still hold
- Immediately sells positions whale has exited
- Ensures we don't hold "dead" positions whale has abandoned
Benefits: Exit following, prevents holding losing positions

FLOW 3: POSITION REDEMPTION (Less frequent - every 60 seconds)
───────────────────────────────────────────────────────────
Redeems closed/resolved positions for their settlement value.
- Detects resolved markets (market outcome determined)
- Identifies winning positions we hold
- Automatically redeems winning shares for $1 USDC each
- Collects profits from closed markets
Benefits: Realizes profits, frees up USDC for new trades

ARCHITECTURE:
All 3 flows run asynchronously and independently:
- Flow 1 operates at high frequency (2-5 sec)
- Flows 2 & 3 operate at lower frequency (60 sec)
- Each flow has its own task loop
- Minimal coupling - each can fail independently
- Central coordinator orchestrates the 3 flows
"""

from typing import Dict, Any, Optional, List
import asyncio
from datetime import datetime

from strategies.base_strategy import BaseStrategy
from core.polymarket_client import PolymarketClient
from core.order_manager import OrderManager
from config.constants import (
    MIRROR_TARGET,
    MIRROR_STRATEGY_CONFIG,
    DUST_THRESHOLD,
    MIN_BUY_PRICE,
    MAX_BUY_PRICE,
    ENTRY_PRICE_GUARD,
    ENABLE_TIME_BASED_FILTERING,
    # Flow 1 configuration
    MIRROR_TRADE_POLLING_INTERVAL_SEC,
    MIRROR_TRADE_TIME_WINDOW_MINUTES,
    MIRROR_ENTRY_DELAY_SEC,
    # Flow 2 configuration
    MIRROR_POSITION_ALIGNMENT_INTERVAL_SEC,
    MIRROR_CLOSED_POSITIONS_LOOK_BACK_LIMIT,
    MIRROR_SELL_IMMEDIATELY_ON_WHALE_EXIT,
    # Flow 3 configuration
    MIRROR_POSITION_REDEMPTION_INTERVAL_SEC,
    MIRROR_AUTO_REDEEM_CLOSED_POSITIONS,
)
from utils.logger import get_logger, log_trade_event, log_error_with_context
from utils.exceptions import StrategyError
from utils.helpers import is_dust_amount


logger = get_logger(__name__)


class MirrorStrategy(BaseStrategy):
    """
    Mirror trading strategy with 3 parallel flows.

    Flows:
    1. Trade Mirroring: Copy whale's buy/sell trades (2-5 sec frequency)
    2. Position Alignment: Sell positions whale has closed (60 sec frequency)
    3. Position Redemption: Redeem closed positions (60 sec frequency)

    All flows run asynchronously, allowing independent operation and failure handling.
    """

    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize mirror strategy with 3 parallel flows.

        Args:
            client: Polymarket client instance
            order_manager: Order manager instance
            config: Strategy configuration overrides
        """
        strategy_config = {**MIRROR_STRATEGY_CONFIG, **(config or {})}
        super().__init__(client, order_manager, strategy_config)

        self.target_address = MIRROR_TARGET
        self.last_target_positions: Dict[str, float] = {}
        self.last_check_time: Optional[datetime] = None

        # Track processed trades to avoid duplicates
        self._processed_trades: set = set()

        # Cache balance for Flow 1
        self._cached_balance: Optional[float] = None
        self._balance_cache_time: Optional[datetime] = None

        # Task handles for the 3 parallel flows
        self._flow_1_task: Optional[asyncio.Task] = None
        self._flow_2_task: Optional[asyncio.Task] = None
        self._flow_3_task: Optional[asyncio.Task] = None

        logger.info(
            f"🔄 Mirror Strategy initialized (3 parallel flows)\n"
            f"   ├─ Target whale: {self.target_address}\n"
            f"   ├─ Your wallet: {self.client.wallet_address}\n"
            f"   ├─ Flow 1 (Trade Mirroring): Every {MIRROR_TRADE_POLLING_INTERVAL_SEC}s\n"
            f"   ├─ Flow 2 (Position Alignment): Every {MIRROR_POSITION_ALIGNMENT_INTERVAL_SEC}s\n"
            f"   └─ Flow 3 (Position Redemption): Every {MIRROR_POSITION_REDEMPTION_INTERVAL_SEC}s"
        )

    # =========================================================================
    # MAIN ORCHESTRATOR
    # =========================================================================

    async def run(self) -> None:
        """
        Override base class run() to manage 3 parallel flows.
        Starts all flows and monitors their health.
        """
        if self.is_running:
            logger.warning(f"Strategy {self.name} is already running")
            return

        self.is_running = True
        logger.info(f"🚀 Starting {self.name} with 3 parallel flows...")

        try:
            # Start all 3 flows as independent asyncio tasks
            self._flow_1_task = asyncio.create_task(self._flow_1_trade_mirroring())
            self._flow_2_task = asyncio.create_task(self._flow_2_position_alignment())
            self._flow_3_task = asyncio.create_task(self._flow_3_position_redemption())

            logger.info("✅ All 3 flows started")

            # Wait for stop signal
            await self._stop_event.wait()

            logger.info("🛑 Stop signal received, shutting down flows...")

        except Exception as e:
            log_error_with_context(
                logger,
                "Mirror strategy failed",
                e,
                strategy=self.name
            )
            raise StrategyError(f"Mirror strategy error: {e}")
        finally:
            # Cancel all flows gracefully
            await self._cancel_flows()
            self.is_running = False
            logger.info("✅ Mirror strategy stopped")

    async def execute(self) -> None:
        """
        Legacy execute() method for backwards compatibility.
        Delegates to Flow 1 (trade mirroring).
        """
        await self._flow_1_single_cycle()

    # =========================================================================
    # FLOW 1: TRADE MIRRORING (High Frequency - every 2-5 seconds)
    # =========================================================================

    async def _flow_1_trade_mirroring(self) -> None:
        """
        Flow 1: Continuously monitor and copy whale's recent trades.

        Runs every MIRROR_TRADE_POLLING_INTERVAL_SEC (default 2 seconds).
        Benefits:
        - Low latency entry on whale trades
        - Catches market opportunities early
        - Frequent balance updates
        """
        logger.info("▶️  Flow 1 (Trade Mirroring) started")

        while self.is_running:
            try:
                await self._flow_1_single_cycle()
                await asyncio.sleep(MIRROR_TRADE_POLLING_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error_with_context(
                    logger,
                    "Flow 1 (Trade Mirroring) cycle failed",
                    e
                )
                await asyncio.sleep(5)  # Backoff on error

        logger.info("⏹️  Flow 1 (Trade Mirroring) stopped")

    async def _flow_1_single_cycle(self) -> None:
        """
        Single iteration of Flow 1 (Trade Mirroring).

        Steps:
        1. Check wallet balance (cached)
        2. Fetch whale's recent trades (last N minutes)
        3. Analyze trades for opportunities
        4. Execute mirror trades
        """
        logger.debug(f"🔄 Flow 1 cycle: {datetime.now().strftime('%H:%M:%S')}")

        # Step 1: Check balance (with caching)
        balance = await self._get_cached_balance()
        if balance == 0:
            logger.debug(f"⚠️  Zero balance, skipping trades")
            return

        logger.debug(f"💵 Balance: ${balance:.2f}")

        # Step 2: Check time-based filtering enabled
        if not ENABLE_TIME_BASED_FILTERING:
            logger.warning("⚠️  Time-based filtering disabled!")
            return

        # Step 3: Fetch whale's recent trades
        try:
            recent_entries = await self.client.get_recent_position_entries(
                address=self.target_address,
                time_window_minutes=MIRROR_TRADE_TIME_WINDOW_MINUTES
            )
        except Exception as e:
            logger.debug(f"Failed to fetch whale trades: {e}")
            return

        if not recent_entries:
            logger.debug(f"No recent whale activity")
            return

        logger.info(f"🐋 Found {len(recent_entries)} recent whale positions")

        # Step 4: Fetch own positions
        own_positions = await self._get_own_positions()

        # Step 5: Build opportunities from trades
        opportunities = await self._find_opportunities_from_recent_entries(
            recent_entries,
            own_positions
        )

        if not opportunities:
            logger.debug("No trading opportunities")
            return

        logger.info(f"💡 {len(opportunities)} opportunities found")

        # Step 6: Execute trades
        executed_count = 0
        for opportunity in opportunities:
            try:
                if await self.should_execute_trade(opportunity):
                    await self._execute_mirror_trade(opportunity)
                    self._cached_balance = None  # Invalidate cache
                    executed_count += 1
                    log_trade_event(
                        logger,
                        'TRADE_EXECUTED',
                        action=opportunity.get('action'),
                        size=opportunity.get('size'),
                        price=opportunity.get('whale_entry_price'),
                        market=opportunity.get('question', 'Unknown')[:50]
                    )

                    # Delay between orders if configured
                    if executed_count < len(opportunities) and MIRROR_ENTRY_DELAY_SEC > 0:
                        await asyncio.sleep(MIRROR_ENTRY_DELAY_SEC)
            except Exception as e:
                logger.debug(f"Trade execution error: {e}")
                continue

        if executed_count > 0:
            logger.info(f"✅ Flow 1: Executed {executed_count} trades")

    # =========================================================================
    # FLOW 2: POSITION ALIGNMENT (Lower Frequency - every 60 seconds)
    # =========================================================================

    async def _flow_2_position_alignment(self) -> None:
        """
        Flow 2: Detect whale exits and immediately sell matching positions.

        Runs every MIRROR_POSITION_ALIGNMENT_INTERVAL_SEC (default 60 seconds).
        Benefits:
        - Exit following - don't hold positions whale has closed
        - Prevents losses from "dead" positions
        - Frees USDC for new opportunities
        """
        logger.info("▶️  Flow 2 (Position Alignment) started")

        while self.is_running:
            try:
                await self._flow_2_single_cycle()
                await asyncio.sleep(MIRROR_POSITION_ALIGNMENT_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error_with_context(
                    logger,
                    "Flow 2 (Position Alignment) cycle failed",
                    e
                )
                await asyncio.sleep(5)  # Backoff on error

        logger.info("⏹️  Flow 2 (Position Alignment) stopped")

    async def _flow_2_single_cycle(self) -> None:
        """
        Single iteration of Flow 2 (Position Alignment).

        Steps:
        1. Fetch own current positions
        2. Fetch whale's closed positions
        3. Find positions we own but whale doesn't
        4. Execute sells
        """
        logger.debug(f"🔄 Flow 2 cycle: {datetime.now().strftime('%H:%M:%S')}")

        # Step 1: Get own positions
        own_positions = await self._get_own_positions()
        if not own_positions:
            logger.debug("No owned positions")
            return

        logger.debug(f"👤 We own {len(own_positions)} positions")

        # Step 2: Check for whale exits
        sell_opportunities = await self._check_whale_exits(own_positions)

        if not sell_opportunities:
            logger.debug("No whale exits detected")
            return

        logger.info(f"🚨 Whale exited {len(sell_opportunities)} positions we hold")

        # Step 3: Execute sells
        sold_count = 0
        for opp in sell_opportunities:
            try:
                await self._execute_mirror_trade(opp)
                sold_count += 1
                log_trade_event(
                    logger,
                    'WHALE_EXIT_SELL',
                    action='SELL',
                    size=opp.get('size'),
                    price=opp.get('current_price'),
                    market=opp.get('question', 'Unknown')[:50]
                )
            except Exception as e:
                logger.debug(f"Failed to sell exited position: {e}")
                continue

        if sold_count > 0:
            logger.info(f"✅ Flow 2: Sold {sold_count} exited positions")

    # =========================================================================
    # FLOW 3: POSITION REDEMPTION (Lower Frequency - every 60 seconds)
    # =========================================================================

    async def _flow_3_position_redemption(self) -> None:
        """
        Flow 3: Automatically redeem closed/resolved positions.

        Runs every MIRROR_POSITION_REDEMPTION_INTERVAL_SEC (default 60 seconds).
        Benefits:
        - Realizes profits from resolved markets
        - Frees USDC locked in closed positions
        - Automatic income collection

        Note: This is currently a stub - implementation depends on Polymarket's
        redemption API. When a market resolves, winning shares can be redeemed
        for $1 USDC each.
        """
        logger.info("▶️  Flow 3 (Position Redemption) started")

        while self.is_running:
            try:
                await self._flow_3_single_cycle()
                await asyncio.sleep(MIRROR_POSITION_REDEMPTION_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error_with_context(
                    logger,
                    "Flow 3 (Position Redemption) cycle failed",
                    e
                )
                await asyncio.sleep(5)  # Backoff on error

        logger.info("⏹️  Flow 3 (Position Redemption) stopped")

    async def _flow_3_single_cycle(self) -> None:
        """
        Single iteration of Flow 3 (Position Redemption).

        Steps:
        1. Fetch our closed positions
        2. Identify positions with winning outcomes
        3. Redeem winning shares for $1 USDC each
        """
        logger.debug(f"🔄 Flow 3 cycle: {datetime.now().strftime('%H:%M:%S')}")

        if not MIRROR_AUTO_REDEEM_CLOSED_POSITIONS:
            logger.debug("Auto-redeem disabled")
            return

        # TODO: Implement position redemption
        # When Polymarket API provides redemption endpoint:
        # 1. Fetch our closed/resolved positions
        # 2. Check if outcomes are determined
        # 3. Redeem winning shares
        # 4. Log redemption profits

        logger.debug("Position redemption: awaiting Polymarket redemption API")

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    async def _get_cached_balance(self) -> float:
        """Get balance with caching to reduce API calls."""
        from config.constants import MIRROR_BALANCE_CACHE_SECONDS

        if self._balance_cache_time and \
           (datetime.now() - self._balance_cache_time).total_seconds() < MIRROR_BALANCE_CACHE_SECONDS:
            return self._cached_balance or 0

        try:
            balance = await self.client.get_balance()
            self._cached_balance = balance
            self._balance_cache_time = datetime.now()
            return balance
        except Exception as e:
            logger.warning(f"Failed to fetch balance: {e}")
            return self._cached_balance or 0

    async def _cancel_flows(self) -> None:
        """Gracefully cancel all flow tasks."""
        for i, task in enumerate([self._flow_1_task, self._flow_2_task, self._flow_3_task], 1):
            if task and not task.done():
                logger.info(f"Cancelling Flow {i}...")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # =========================================================================
    # ANALYSIS & EXECUTION METHODS (from original implementation)
    # =========================================================================

    async def analyze_opportunity(self) -> Optional[Dict[str, Any]]:
        """Not used in parallel flow architecture."""
        raise NotImplementedError("Use flow-specific methods instead")

    async def should_execute_trade(self, opportunity: Dict[str, Any]) -> bool:
        """Check if opportunity meets execution criteria."""
        # Delegate to order manager for validation
        return await self.order_manager.validate_order(opportunity)

    async def _get_own_positions(self) -> Dict[str, Dict[str, Any]]:
        """Fetch our current positions from Polymarket."""
        try:
            positions = await self.client.get_positions(self.client.wallet_address)
            return positions or {}
        except Exception as e:
            logger.error(f"Failed to fetch own positions: {e}")
            return {}

    async def _check_whale_exits(
        self,
        own_positions: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Check if whale has exited positions we still hold."""
        # Implementation from original mirror_strategy.py
        # (see existing code)
        try:
            closed_positions = await self.client.get_closed_positions(
                address=self.target_address,
                limit=MIRROR_CLOSED_POSITIONS_LOOK_BACK_LIMIT
            )

            if not closed_positions:
                return []

            sell_opportunities = []

            for closed_pos in closed_positions:
                condition_id = closed_pos.get('conditionId')

                if condition_id and condition_id in own_positions:
                    own_pos = own_positions[condition_id]
                    token_id = own_pos.get('token_id')

                    if not token_id:
                        continue

                    current_price = await self.client.get_best_price(token_id, 'SELL')
                    owned_shares = own_pos.get('size', 0)
                    estimated_usd_value = owned_shares * current_price if current_price else 0

                    if estimated_usd_value < DUST_THRESHOLD:
                        continue

                    sell_opportunities.append({
                        'action': 'SELL',
                        'token_id': token_id,
                        'condition_id': condition_id,
                        'size': estimated_usd_value,
                        'shares': owned_shares,
                        'current_price': current_price,
                        'whale_entry_price': closed_pos.get('avgPrice'),
                        'target_size': 0,
                        'own_size': owned_shares,
                        'question': own_pos.get('question', 'Unknown'),
                        'outcome': own_pos.get('outcome', 'Unknown'),
                        'confidence': 1.0,
                        'metadata': {
                            'strategy': 'mirror_flow_2',
                            'reason': 'whale_closed_position'
                        }
                    })

            return sell_opportunities

        except Exception as e:
            logger.error(f"Failed to check whale exits: {e}")
            return []

    async def _find_opportunities_from_recent_entries(
        self,
        recent_entries: Dict[str, Dict],
        own_positions: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Build trading opportunities from whale's recent trades."""
        # Implementation from original mirror_strategy.py
        # (see existing code - returns list of opportunities)
        opportunities = []

        for condition_id, entry in recent_entries.items():
            # Filter by price bounds
            price = entry.get('avg_price', 0)
            if price < MIN_BUY_PRICE or price > MAX_BUY_PRICE:
                continue

            # Check if we already own this position
            if condition_id in own_positions:
                continue

            # Build opportunity
            opp = {
                'action': 'BUY',
                'token_id': entry.get('token_id'),
                'condition_id': condition_id,
                'size': entry.get('size', 0),
                'whale_entry_price': price,
                'current_price': price,
                'question': entry.get('title', 'Unknown'),
                'outcome': entry.get('outcome_name', 'Unknown'),
                'confidence': 0.8,
                'metadata': {
                    'strategy': 'mirror_flow_1',
                    'reason': 'whale_recent_trade'
                }
            }

            opportunities.append(opp)

        return opportunities

    async def _execute_mirror_trade(self, opportunity: Dict[str, Any]) -> None:
        """Execute a single mirror trade."""
        # Delegate to order manager
        await self.order_manager.execute_order(opportunity)
