"""
Mirror Trading Strategy
Copies trades from a target whale wallet with safety checks
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
    ENTRY_TIME_WINDOW_MINUTES,
    ENABLE_TIME_BASED_FILTERING,
)
from utils.logger import get_logger
from utils.exceptions import StrategyError
from utils.helpers import is_dust_amount


logger = get_logger(__name__)


class MirrorStrategy(BaseStrategy):
    """
    Mirror trading strategy that copies a target wallet's positions
    
    Strategy Logic:
    1. Monitor target wallet's positions
    2. Compare with own positions
    3. Execute trades to mirror target's portfolio
    4. Apply safety checks (price guard, position limits)
    """

    def __init__(
        self,
        client: PolymarketClient,
        order_manager: OrderManager,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize mirror strategy
        
        Args:
            client: Polymarket client instance
            order_manager: Order manager instance
            config: Strategy configuration (uses MIRROR_STRATEGY_CONFIG if not provided)
        """
        strategy_config = {**MIRROR_STRATEGY_CONFIG, **(config or {})}
        super().__init__(client, order_manager, strategy_config)
        
        self.target_address = MIRROR_TARGET
        self.last_target_positions: Dict[str, float] = {}
        self.last_check_time: Optional[datetime] = None
        
        # Track processed trades to avoid duplicates
        # Key: "conditionId_assetId_timestamp" to uniquely identify each trade
        self._processed_trades: set = set()
        
        # Cache balance to avoid redundant API calls
        self._cached_balance: Optional[float] = None
        self._balance_cache_time: Optional[datetime] = None
        
        logger.info(f"Mirror strategy initialized for target: {self.target_address}")
        
        if self.config.get('use_proportional_size', True):
            ratio = self.config.get('order_size_ratio', 0.06)
            max_size = self.config.get('max_order_size_usd', 3.6)
            logger.info(
                f"Using proportional sizing: {ratio*100:.1f}% of whale's order "
                f"(max cap: ${max_size:.2f}, no minimum - true proportional)"
            )
        else:
            multiplier = self.config.get('position_size_multiplier', 1.0)
            max_size = self.config.get('max_order_size_usd', 3.6)
            logger.info(f"Using {multiplier}x whale's size (max: ${max_size:.2f} USDC)")

    async def execute(self) -> None:
        from utils.logger import log_performance_metric
        """
        Main execution logic for mirror strategy
        Checks for changes in target's positions and mirrors them
        """
        logger.info(f"🔄 Starting mirror check cycle...")
        logger.info(f"🐋 Target whale: {self.target_address}")
        logger.info(f"💼 Own wallet: {self.client.wallet_address}")
        cycle_start_time = datetime.now()
        
        # Check current balance (with caching)
        try:
            # Use cached balance if less than 30 seconds old
            if self._balance_cache_time and \
               (datetime.now() - self._balance_cache_time).total_seconds() < 30:
                balance = self._cached_balance
                logger.info(f"💵 Current USDC balance: ${balance:.2f} (cached)")
            else:
                balance = await self.client.get_balance()
                self._cached_balance = balance
                self._balance_cache_time = datetime.now()
                logger.info(f"💵 Current USDC balance: ${balance:.2f}")
        except Exception as e:
            logger.warning(f"Could not fetch balance: {e}")
            balance = 0

        # OPTIMIZATION: Only fetch whale's RECENT entries (not all 157 positions!)
        # We only care about positions the whale entered in the last 5 minutes
        logger.info(f"⏰ Fetching whale's recent entries (last {ENTRY_TIME_WINDOW_MINUTES} min)...")
        
        if not ENABLE_TIME_BASED_FILTERING:
            logger.warning("⚠️  Time-based filtering is DISABLED - bot will not work efficiently!")
            logger.warning("⚠️  Enable ENABLE_TIME_BASED_FILTERING in constants.py")
            return
        
        try:
            recent_entries = await self.client.get_recent_position_entries(
                address=self.target_address,
                time_window_minutes=ENTRY_TIME_WINDOW_MINUTES
            )
        except Exception as e:
            logger.error(f"❌ Failed to fetch whale's recent entries: {e}")
            return
        
        if not recent_entries:
            logger.info(f"✅ No recent whale activity in last {ENTRY_TIME_WINDOW_MINUTES} minutes")
            self.last_check_time = datetime.now()
            return
        
        logger.info(f"🐋 Found {len(recent_entries)} positions with recent whale activity")
        
        # PRODUCTION VALIDATION: Log data completeness
        entries_with_token_id = sum(1 for e in recent_entries.values() if e.get('token_id'))
        logger.info(f"📊 Data validation: {entries_with_token_id}/{len(recent_entries)} entries have token_id")
        
        entries_with_size = sum(1 for e in recent_entries.values() if e.get('size'))
        logger.info(f"📊 Data validation: {entries_with_size}/{len(recent_entries)} entries have size/price from trades")
        
        # CRITICAL FIX: DON'T fetch whale's current positions!
        # This prevents mirroring old positions whale bought days/hours ago
        # recent_entries now contains size/avg_price calculated from TRADES within time window
        # We only need market metadata (question, outcome) which we'll fetch on-demand
        logger.info(f"✅ Using trade data from last {ENTRY_TIME_WINDOW_MINUTES} min only (not fetching all whale positions)")
        
        # Get token IDs from recent entries only
        recent_token_ids = [pos.get('token_id') for pos in recent_entries.values() if pos.get('token_id')]
        
        if not recent_token_ids:
            logger.info("✅ No valid token IDs in recent entries")
            return
        
        # Validate only the tokens we care about (not all 46+ tokens)
        logger.info(f"🔍 Validating {len(recent_token_ids)} tokens from recent whale trades...")
        validation_results = await self.client.validate_tokens_bulk(recent_token_ids)
        valid_token_ids = {tid for tid, is_valid in validation_results.items() if is_valid}
        
        if not valid_token_ids:
            logger.info("⚠️  All recent whale positions are in closed/invalid markets")
            return
        
        # Filter recent entries to only valid tokens
        recent_entries = {
            k: v for k, v in recent_entries.items()
            if v.get('token_id') in valid_token_ids
        }
        
        logger.info(f"✅ {len(recent_entries)} valid positions to analyze")
        
        # Fetch OWN positions for ONLY these specific tokens (not all positions)
        logger.info(f"📊 Checking which positions we already own...")
        own_positions = await self._get_own_positions()
        
        # Build opportunities from recent entries
        logger.info(f"🔍 Building trading opportunities...")
        opportunities = await self._find_opportunities_from_recent_entries(
            recent_entries,
            own_positions
        )

        if not opportunities:
            logger.info("✅ No opportunities - positions synchronized with whale")
            self.last_check_time = datetime.now()
            return

        logger.info(f"💡 Found {len(opportunities)} trading opportunities")
        for i, opp in enumerate(opportunities[:3], 1):
            whale_usd = opp.get('whale_size_usd', 0)
            bot_usd = opp.get('size', 0)
            ratio_pct = (bot_usd / whale_usd * 100) if whale_usd > 0 else 0
            logger.info(
                f"  {i}. {opp['action']} {opp.get('question', 'Unknown')[:50]}... "
                f"Whale: ${whale_usd:.2f} → Bot: ${bot_usd:.2f} ({ratio_pct:.1f}%) "
                f"@ ${opp.get('whale_entry_price', 0):.4f}"
            )

        # Execute trades for each opportunity with comprehensive tracking
        executed_count = 0
        rejected_count = 0
        failed_count = 0
        
        for opportunity in opportunities:
            try:
                # --- Latency and deviation metrics ---
                whale_entry = opportunity.get('whale_entry', {})
                whale_trade_ts = whale_entry.get('last_trade_time')
                latency_sec = None
                if whale_trade_ts:
                    try:
                        latency_sec = (datetime.now().timestamp() - float(whale_trade_ts))
                        log_performance_metric(
                            logger,
                            metric_name="mirror_latency_sec",
                            value=latency_sec,
                            unit="seconds",
                            market_id=opportunity.get('token_id'),
                            action=opportunity.get('action'),
                        )
                    except Exception as e:
                        logger.debug(f"Could not compute latency: {e}")
                # Price deviation metric
                whale_entry_price = opportunity.get('whale_entry_price')
                current_price = opportunity.get('current_price')
                price_deviation = None
                if whale_entry_price and current_price:
                    try:
                        price_deviation = ((float(current_price) - float(whale_entry_price)) / float(whale_entry_price)) * 100
                        log_performance_metric(
                            logger,
                            metric_name="mirror_price_deviation_pct",
                            value=price_deviation,
                            unit="percent",
                            market_id=opportunity.get('token_id'),
                            action=opportunity.get('action'),
                        )
                    except Exception as e:
                        logger.debug(f"Could not compute price deviation: {e}")
                if await self.should_execute_trade(opportunity):
                    await self._execute_mirror_trade(opportunity)
                    # CRITICAL: Invalidate balance cache after each order
                    self._cached_balance = None
                    self._balance_cache_time = None
                    executed_count += 1
                    logger.info(
                        f"✅ Order {executed_count}/{len(opportunities)} EXECUTED: "
                        f"{opportunity['action']} ${opportunity['size']:.2f} "
                        f"of {opportunity.get('question', 'Unknown')[:40]}..."
                    )
                    # Add delay to avoid rapid-fire orders
                    if executed_count < len(opportunities):
                        entry_delay = self.config.get('entry_delay_sec', 5)
                        if entry_delay > 0:
                            logger.info(f"⏳ Waiting {entry_delay}s before next trade...")
                            await asyncio.sleep(entry_delay)
                else:
                    reason = opportunity.get('reason', 'criteria not met')
                    rejected_count += 1
                    logger.info(f"⏭️  Skipped ({rejected_count}): {reason}")
            except Exception as e:
                from utils.exceptions import OrderRejectedError
                if isinstance(e, OrderRejectedError):
                    # Order was rejected by safety checks (not an error)
                    rejected_count += 1
                    logger.info(
                        f"⏭️  Order {rejected_count} REJECTED by safety check: {e}"
                    )
                else:
                    # Actual execution error
                    failed_count += 1
                    logger.error(
                        f"❌ Failed to execute mirror trade ({failed_count}): {e}",
                        exc_info=True
                    )
                continue

        # PRODUCTION LOGGING: Cycle completion summary
        cycle_end_time = datetime.now()
        cycle_latency = (cycle_end_time - cycle_start_time).total_seconds()
        log_performance_metric(
            logger,
            metric_name="mirror_cycle_latency_sec",
            value=cycle_latency,
            unit="seconds",
        )
        logger.info(
            f"🔄 Cycle complete: {executed_count} executed, "
            f"{rejected_count} rejected, {failed_count} failed"
        )
        # Update last check time and cache recent entries
        self.last_target_positions = recent_entries  # Only store recent entries, not all positions
        self.last_check_time = datetime.now()
        self._balance_cache_time = None
        logger.info(f"✅ Mirror cycle complete\n")

    async def analyze_opportunity(self) -> Optional[Dict[str, Any]]:
        """
        Analyze for mirror trading opportunities
        
        Returns:
            First available opportunity or None
        """
        target_positions = await self._get_target_positions()
        own_positions = await self._get_own_positions()
        
        opportunities = await self._find_position_differences(
            target_positions,
            own_positions
        )
        
        return opportunities[0] if opportunities else None

    async def should_execute_trade(self, opportunity: Dict[str, Any]) -> bool:
        """
        Determine if mirror trade should be executed
        
        Args:
            opportunity: Trade opportunity details
            
        Returns:
            True if trade should be executed
        """
        action = opportunity.get('action')
        
        # Check if strategy is enabled
        if not self.config.get('enabled', True):
            logger.debug("Strategy disabled")
            return False
        
        # CRITICAL: Validate we have whale's entry price for BUY orders
        if action == 'BUY':
            whale_entry_price = opportunity.get('whale_entry_price')
            if not whale_entry_price or whale_entry_price <= 0:
                opportunity['reason'] = "Missing whale entry price - cannot place limit order"
                logger.error(f"❌ {opportunity['reason']}")
                return False
            
            # Sanity check: price must be between 0.01 and 0.99
            if whale_entry_price < 0.01 or whale_entry_price > 0.99:
                opportunity['reason'] = f"Whale entry price {whale_entry_price:.4f} outside valid range [0.01, 0.99]"
                logger.error(f"❌ {opportunity['reason']}")
                return False
        
        # CRITICAL: Validate token_id format (must be hex string)
        token_id = opportunity.get('token_id')
        if not token_id or len(token_id) < 10:
            opportunity['reason'] = f"Invalid token_id format: {token_id}"
            logger.error(f"❌ {opportunity['reason']}")
            return False

        # Check position size against dust threshold
        size = opportunity.get('size', 0)
        if is_dust_amount(size, DUST_THRESHOLD):
            opportunity['reason'] = f"Size {size} below dust threshold"
            return False

        # Note: No minimum order size check - let Polymarket reject if < 5 shares
        # This maintains true proportional sizing with whale's trades
        
        # Check available balance - ONLY FOR BUY ORDERS (use cached balance)
        # BUY orders require USDC to purchase shares
        # SELL orders don't require balance (you receive USDC when selling shares)
        if action == 'BUY':
            # Use cached balance if available to avoid redundant API call
            if self._cached_balance and self._balance_cache_time and \
               (datetime.now() - self._balance_cache_time).total_seconds() < 30:
                current_balance = self._cached_balance
            else:
                try:
                    current_balance = await self.client.get_balance()
                    self._cached_balance = current_balance
                    self._balance_cache_time = datetime.now()
                except Exception as e:
                    logger.warning(f"Failed to check balance: {e}")
                    # Continue anyway - let the order fail if balance is insufficient
                    return True
            
            if current_balance < size:
                opportunity['reason'] = f"Insufficient balance: ${float(current_balance):.2f} < ${float(size):.2f}"
                logger.warning(f"⚠️  {opportunity['reason']} - Need to add ${float(size) - float(current_balance):.2f} USDC")
                return False

        # SELL orders: Validate we own enough shares before executing
        if action == 'SELL':
            # Double-check we have the shares to sell (defense in depth)
            owned_shares = opportunity.get('shares', 0)
            current_price = opportunity.get('current_price', 0)
            
            if owned_shares <= 0:
                opportunity['reason'] = f"No shares to sell (owned: {owned_shares:.2f})"
                logger.warning(f"❌ SELL validation failed: {opportunity['reason']}")
                return False
            
            # Ensure the USD value makes sense given shares and price
            expected_usd = owned_shares * current_price if current_price else 0
            if expected_usd < DUST_THRESHOLD:
                opportunity['reason'] = (
                    f"Position too small: {owned_shares:.2f} shares × ${current_price:.4f} "
                    f"= ${expected_usd:.2f} < ${DUST_THRESHOLD}"
                )
                logger.info(f"⏭️  Skipping SELL: {opportunity['reason']}")
                return False
            
            logger.info(
                f"✅ SELL validation passed: {owned_shares:.2f} shares × ${current_price:.4f} "
                f"= ${expected_usd:.2f} (whale exited, following immediately)"
            )
            return True

        # BUY orders only: Apply price reasonableness checks
        if action == 'BUY':
            # --- NEW LOGIC: Skip if current price is less than 50% of whale entry price ---
            current_price = opportunity.get('current_price')
            whale_entry_price = opportunity.get('whale_entry_price', 0)
            if current_price is not None and whale_entry_price > 0:
                if current_price < whale_entry_price * 0.5:
                    opportunity['reason'] = (
                        f"Current price ${current_price:.4f} is less than 50% of whale's entry price ${whale_entry_price:.4f} - likely loss-making, skipping trade."
                    )
                    logger.warning(f"🚫 PRICE DROP: {opportunity['reason']}")
                    return False

            current_price = opportunity.get('current_price')
            whale_entry_price = opportunity.get('whale_entry_price', 0)
            
            # Log price comparison for transparency
            if current_price and whale_entry_price > 0:
                price_diff_pct = ((current_price - whale_entry_price) / whale_entry_price) * 100
                logger.info(
                    f"💰 Price check: Current=${current_price:.4f}, "
                    f"Whale avg entry=${whale_entry_price:.4f} "
                    f"({price_diff_pct:+.1f}%)"
                )
            
            # CRITICAL: We place limit orders at whale's entry price, NOT current market price
            # So validate whale's entry price, not current price
            # If whale entered at $0.28 and market is now $0.38, we still place limit @ $0.28
            
            if whale_entry_price > 0:
                # SAFETY LIMITS: Block extreme whale entry prices
                # These are LAST RESORT protections against clearly bad trades
                # Applied to BOTH limit orders AND market orders
                
                # For limit orders with buffer: check if buffered price would exceed MAX_BUY_PRICE
                price_buffer_percent = self.config.get('price_buffer_percent', 0)
                use_market_orders = self.config.get('use_market_orders', False)

                # Calculate what the actual order price would be (with buffer if applicable)
                if action == 'BUY' and not use_market_orders and price_buffer_percent > 0:
                    buffered_price = whale_entry_price * (1 + price_buffer_percent / 100)
                    final_order_price = buffered_price
                else:
                    final_order_price = whale_entry_price

                # Max price deviation check (default 15%)
                max_price_deviation_percent = self.config.get('max_price_deviation_percent', 15.0)
                max_allowed_price = whale_entry_price * (1 + max_price_deviation_percent / 100)
                if final_order_price > max_allowed_price:
                    opportunity['reason'] = (
                        f"Order price {final_order_price:.4f} exceeds whale entry price {whale_entry_price:.4f} by more than {max_price_deviation_percent:.1f}% (limit: {max_allowed_price:.4f})"
                    )
                    logger.warning(f"🚫 MAX PRICE DEVIATION: {opportunity['reason']}")
                    return False

                # Check the FINAL order price (after buffer) against limits
                if final_order_price > MAX_BUY_PRICE:
                    opportunity['reason'] = (
                        f"Order price {final_order_price:.4f} (whale: {whale_entry_price:.4f} + {price_buffer_percent}% buffer) "
                        f"exceeds MAX_BUY_PRICE {MAX_BUY_PRICE} - near-certain outcome, minimal upside"
                    )
                    logger.warning(f"🚫 SAFETY LIMIT: {opportunity['reason']}")
                    return False

                if whale_entry_price < MIN_BUY_PRICE:
                    opportunity['reason'] = (
                        f"Whale entry price {whale_entry_price:.4f} too low (<{MIN_BUY_PRICE}) - extremely unlikely outcome"
                    )
                    logger.warning(f"🚫 SAFETY LIMIT: {opportunity['reason']}")
                    return False
                
                # LOGIC: Since we're placing limit orders at whale's exact price,
                # the whale's entry price IS our entry price - always acceptable!
                # No need to compare current market price vs whale price.
                # 
                # Example:
                # - Whale entered at $0.28 three minutes ago
                # - Market now at $0.38 (price moved up)
                # - We place limit order at $0.28 (whale's price)
                # - If filled, we got same price as whale! ✓
                # - If not filled, no harm done
                current_price_str = f"${current_price:.4f}" if current_price else "N/A"
                logger.info(
                    f"✅ Will place limit order at whale's entry price ${whale_entry_price:.4f} "
                    f"(current market: {current_price_str})"
                )
            else:
                # No whale entry price available - use absolute limits only
                logger.info(f"⚠️  No whale entry price, relying on absolute limits (0.02-0.98)")
            
            # Check available balance for buy orders
            balance = await self.client.get_balance()
            if balance < size:
                opportunity['reason'] = f"Insufficient balance: {balance:.2f} USDC < {size:.2f} USDC"
                # ALERT: Log missed opportunity due to insufficient funds
                balance_float = float(balance)  # Convert Decimal to float for arithmetic
                logger.warning(
                    f"⚠️  MISSED OPPORTUNITY - INSUFFICIENT BALANCE ⚠️ "
                    f"Token: {opportunity.get('token_id', 'unknown')} | "
                    f"Required: ${size:.2f} USDC | Available: ${balance_float:.2f} USDC | "
                    f"Shortfall: ${(size - balance_float):.2f} USDC | "
                    f"Price: {current_price:.3f}"
                )
                return False

        return True

    async def _get_target_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        Get target wallet's positions.
        
        Returns:
            Dictionary mapping condition_id to position data:
            {
                "condition_id": {
                    "size": 1.5,
                    "avg_price": 0.45,
                    "outcome": "Yes",
                    "question": "Will X happen?",
                    "token_id": "token_id_for_orders",
                    "outcome_index": 0
                },
                ...
            }
        """
        try:
            # Use simplified positions method from client
            position_map = await self.client.get_simplified_positions(self.target_address)
            logger.debug(f"Target has {len(position_map)} positions")
            return position_map
            
        except Exception as e:
            logger.error(f"Failed to get target positions: {e}")
            return {}

    async def _get_own_positions(self) -> Dict[str, Dict[str, Any]]:
        """
        Get own wallet's positions.
        
        Returns:
            Dictionary mapping condition_id to position data
        """
        try:
            # Use simplified positions method from client
            position_map = await self.client.get_simplified_positions()
            logger.debug(f"Own wallet has {len(position_map)} positions")
            return position_map
            
        except Exception as e:
            logger.error(f"Failed to get own positions: {e}")
            return {}

    async def _check_whale_exits(self, own_positions: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Check if whale has recently exited any positions we still hold.
        
        Uses closed positions API to detect whale exits faster than waiting
        for positions to disappear from active positions list.
        
        Args:
            own_positions: Our current positions
            
        Returns:
            List of sell opportunities for positions whale has exited
        """
        try:
            # Query whale's recent closed positions (last 10)
            closed_positions = await self.client.get_closed_positions(
                address=self.target_address,
                limit=10
            )
            
            if not closed_positions:
                return []
            
            # Check if we still hold any positions whale has closed
            sell_opportunities = []
            
            # Diagnostic logging: Show what we're checking
            logger.info(f"🔍 Whale closed {len(closed_positions)} positions")
            logger.info(f"🔍 You own {len(own_positions)} positions")
            logger.info(f"🔍 Your position condition_ids: {list(own_positions.keys())[:5]}...")  # First 5
            
            for closed_pos in closed_positions:
                condition_id = closed_pos.get('conditionId')
                whale_closed_title = closed_pos.get('title', 'Unknown')[:50]  # First 50 chars
                
                if condition_id and condition_id in own_positions:
                    own_pos = own_positions[condition_id]
                    token_id = own_pos.get('token_id')
                    
                    if not token_id:
                        continue
                    
                    # Whale exited this position, we should too
                    current_price = await self.client.get_best_price(token_id, 'SELL')
                    
                    # Log whale exit detected
                    logger.info(
                        f"🚨 Whale EXITED position: {closed_pos.get('title', 'Unknown')} "
                        f"(we hold {own_pos.get('size', 0):.2f} shares worth ~${(own_pos.get('size', 0) * current_price):.2f})"
                    )
                    
                    # IMPORTANT: Validate we actually own enough shares to sell
                    # The 'size' field from Data API is NUMBER OF SHARES, not USD value
                    owned_shares = own_pos.get('size', 0)
                    
                    # Skip if we own very few shares (below dust threshold converted to shares)
                    estimated_usd_value = owned_shares * current_price if current_price else 0
                    
                    if estimated_usd_value < DUST_THRESHOLD:
                        logger.info(
                            f"⏭️  Skipping SELL - position too small: {owned_shares:.2f} shares "
                            f"(~${estimated_usd_value:.2f} USD) < dust threshold ${DUST_THRESHOLD}"
                        )
                        continue
                    
                    sell_opportunities.append({
                        'action': 'SELL',
                        'token_id': token_id,
                        'condition_id': condition_id,
                        'size': estimated_usd_value,  # USD value for order manager
                        'shares': owned_shares,  # Actual shares we own (for validation)
                        'current_price': current_price,
                        'whale_entry_price': closed_pos.get('avgPrice'),
                        'target_size': 0,
                        'own_size': owned_shares,  # Track for debugging
                        'question': own_pos.get('question', 'Unknown'),
                        'outcome': own_pos.get('outcome', 'Unknown'),
                        'confidence': 1.0,
                        'metadata': {
                            'strategy': 'mirror',
                            'target_address': self.target_address,
                            'reason': 'whale_closed_position',
                            'whale_exit_detected_via': 'closed_positions_api'
                        }
                    })
                else:
                    # Whale closed a position we don't own - log for diagnostics
                    logger.debug(
                        f"⏭️  Whale closed '{whale_closed_title}' (condition_id: {condition_id[:16]}...) "
                        f"- you don't own this position"
                    )
            
            if sell_opportunities:
                logger.info(
                    f"📤 Detected {len(sell_opportunities)} whale exits via closed positions API"
                )
            
            return sell_opportunities
            
        except Exception as e:
            logger.error(f"Failed to check whale exits: {e}")
            return []

    async def _find_opportunities_from_recent_entries(
        self,
        recent_entries: Dict[str, Dict],
        own_positions: Dict[str, Dict]
    ) -> List[Dict[str, Any]]:
        """
        Build trading opportunities directly from whale's recent entries.
        Only processes positions the whale entered recently (last 1 min).
        
        DUPLICATE PREVENTION:
        - Tracks individual trades using conditionId_assetId_timestamp
        - If whale buys same token twice in 1 min, we mirror both trades
        - If we see same trade in next cycle, we skip it
        
        Args:
            recent_entries: Positions whale entered recently (enriched with position data)
            own_positions: Our current positions
            
        Returns:
            List of trading opportunities (one order per whale trade)
        """
        opportunities = []
        
        # Get sizing configuration
        use_proportional = self.config.get('use_proportional_size', True)
        order_ratio = self.config.get('order_size_ratio', 0.06)
        max_order_usd = self.config.get('max_order_size_usd', 3.6)
        
        for position_key, whale_pos in recent_entries.items():
            token_id = whale_pos.get('token_id')
            if not token_id:
                logger.warning(f"⚠️  Missing token_id for {position_key[:50]}... - skipping")
                continue
            
            # Get trade details
            last_trade_time = whale_pos.get('last_trade_time')
            trade_count = whale_pos.get('trade_count', 1)
            condition_id = whale_pos.get('condition_id')
            asset_id = whale_pos.get('asset_id')
            whale_size = whale_pos.get('size')
            avg_price = whale_pos.get('avg_price')
            
            # Validation: Ensure we have all required data (size/price now from trades!)
            if not all([last_trade_time, condition_id, asset_id, whale_size is not None, avg_price is not None]):
                logger.warning(
                    f"⚠️  Incomplete trade data for {position_key[:50]}...: "
                    f"time={last_trade_time}, cond={condition_id}, asset={asset_id}, "
                    f"size={whale_size}, price={avg_price}"
                )
                continue
            
            # Create unique trade identifier
            # Format: "conditionId_assetId_timestamp"
            trade_id = f"{condition_id}_{asset_id}_{int(last_trade_time)}"
            
            # CRITICAL: Skip if we already processed this exact trade
            if trade_id in self._processed_trades:
                logger.debug(f"⏭️  Already processed trade {trade_id[:50]}...")
                continue
            
            # Mark trade as processed BEFORE creating opportunity
            # This prevents double-processing if exception occurs later
            self._processed_trades.add(trade_id)
            
            # Clean up old processed trades (keep last 10 minutes worth)
            current_time = datetime.now().timestamp()
            self._processed_trades = {
                tid for tid in self._processed_trades
                if abs(current_time - int(tid.split('_')[-1])) < 600  # 10 min
            }
            
            # Check if we already own this position
            own_pos = own_positions.get(position_key)
            
            # Calculate whale's order size in USD
            whale_size_usd = float(whale_size) * float(avg_price)
            
            # Use minimal info for logging (market metadata API often unavailable)
            # Full market details not needed for trade execution
            question = f"Market {condition_id[:8]}..."
            outcome = f"Token {asset_id[:8]}..."
            
            # STRATEGY: True proportional order sizing (only max cap)
            # Scale order size based on whale's conviction (order size)
            if use_proportional:
                # Proportional: Take X% of whale's order, cap at maximum only
                raw_order_size = whale_size_usd * order_ratio
                order_size = min(raw_order_size, max_order_usd)  # Only upper cap, no minimum
                
                # CRITICAL: Ensure minimum 5 shares (Polymarket requirement)
                # Must calculate based on BUFFERED price (not whale's entry price)
                from config.constants import MIN_ORDER_SHARES
                price_buffer_percent = self.config.get('price_buffer_percent', 0)
                buffered_price = float(avg_price) * (1 + price_buffer_percent / 100)
                
                shares_at_current_size = order_size / buffered_price
                if shares_at_current_size < MIN_ORDER_SHARES:
                    min_usd_required = MIN_ORDER_SHARES * buffered_price
                    logger.info(
                        f"⚠️  Order ${order_size:.2f} only buys {shares_at_current_size:.2f} shares "
                        f"at buffered price ${buffered_price:.4f} (< {MIN_ORDER_SHARES} minimum) - "
                        f"increasing to ${min_usd_required:.2f}"
                    )
                    order_size = min_usd_required
                
                logger.info(
                    f"🆕 New whale trade: {question[:60]}... "
                    f"({outcome}) @ ${avg_price:.4f} - "
                    f"Whale: ${whale_size_usd:.2f} → Bot: ${order_size:.2f} ({order_ratio*100:.1f}% ratio) - "
                    f"{whale_pos.get('minutes_ago', 0):.1f}min ago"
                )
            else:
                # Fallback: Use fixed multiplier of whale's size
                multiplier = self.config.get('position_size_multiplier', 1.0)
                order_size = min(whale_size_usd * multiplier, max_order_usd)
                
                # CRITICAL: Ensure minimum 5 shares (Polymarket requirement)
                # Must calculate based on BUFFERED price (not whale's entry price)
                from config.constants import MIN_ORDER_SHARES
                price_buffer_percent = self.config.get('price_buffer_percent', 0)
                buffered_price = float(avg_price) * (1 + price_buffer_percent / 100)
                
                shares_at_current_size = order_size / buffered_price
                if shares_at_current_size < MIN_ORDER_SHARES:
                    min_usd_required = MIN_ORDER_SHARES * buffered_price
                    logger.info(
                        f"⚠️  Order ${order_size:.2f} only buys {shares_at_current_size:.2f} shares "
                        f"at buffered price ${buffered_price:.4f} (< {MIN_ORDER_SHARES} minimum) - "
                        f"increasing to ${min_usd_required:.2f}"
                    )
                    order_size = min_usd_required
                
                logger.info(
                    f"🆕 New whale trade: {question[:60]}... "
                    f"({outcome}) @ ${avg_price:.4f} - "
                    f"Whale: ${whale_size_usd:.2f} → Bot: ${order_size:.2f} - "
                    f"{whale_pos.get('minutes_ago', 0):.1f}min ago"
                )
            
            # Check if we already own this position
            if own_pos:
                # We already have this position
                own_size_usd = float(own_pos.get('size', 0)) * float(own_pos.get('avg_price', avg_price))
                
                # Check if we should add more
                if own_size_usd >= order_size:
                    logger.debug(f"✅ Already own ${own_size_usd:.2f} (>= ${order_size:.2f}) - synchronized")
                    continue
                
                # Add another proportional order
                opportunities.append({
                    'action': 'BUY',
                    'token_id': token_id,
                    'size': order_size,
                    'size_diff': order_size,  # For compatibility
                    'whale_size': whale_size,
                    'whale_size_usd': whale_size_usd,  # Add USD value for logging
                    'own_size': float(own_pos.get('size', 0)),
                    'whale_entry_price': avg_price,
                    'whale_entry': whale_pos,
                    'position_key': position_key,
                    'trade_id': trade_id,
                    'trade_count': trade_count
                })
            else:
                # We don't have this position - enter with proportional size
                opportunities.append({
                    'action': 'BUY',
                    'token_id': token_id,
                    'size': order_size,  # Proportional to whale's order
                    'size_diff': order_size,
                    'whale_size': whale_size,
                    'whale_size_usd': whale_size_usd,  # Add USD value for logging
                    'own_size': 0,
                    'whale_entry_price': avg_price,
                    'whale_entry': whale_pos,
                    'position_key': position_key,
                    'trade_id': trade_id,
                    'trade_count': trade_count
                })
        
        return opportunities
    
    async def _find_position_differences(
        self,
        target_positions: Dict[str, Dict[str, Any]],
        own_positions: Dict[str, Dict[str, Any]],
        recent_entries: Dict[str, Dict[str, Any]] = None  # Time-based filtering
    ) -> List[Dict[str, Any]]:
        """
        Find differences between target and own positions.
        
        Per Polymarket support: Match by condition_id + outcome (asset).
        Whale can hold multiple positions in same conditionId (different outcomes).
        
        Args:
            target_positions: Target's positions ("condition_id_asset" -> position data)
            own_positions: Own positions ("condition_id_asset" -> position data)
            recent_entries: Positions entered within time window (same key format)
            
        Returns:
            List of trade opportunities to mirror positions
        """
        opportunities = []
        use_fixed_size = self.config.get('use_fixed_size', True)
        fixed_order_size = self.config.get('fixed_order_size_usd', 5.0)
        position_multiplier = self.config.get('position_size_multiplier', 1.0)

        # Check for new positions in target
        for position_key, target_pos in target_positions.items():
            target_size = target_pos.get('size', 0)  # Whale's size in shares
            own_pos = own_positions.get(position_key, {})  # Match by full key
            own_size = own_pos.get('size', 0)  # Our size in shares
            
            # Get token_id and condition_id from position data
            token_id = target_pos.get('token_id')
            condition_id = target_pos.get('condition_id')
            if not token_id or not condition_id:
                logger.warning(f"Missing token_id or condition_id for position {position_key}, skipping")
                continue
            
            # Get average prices for USD value calculation
            whale_avg_price = target_pos.get('avg_price', 0)
            own_avg_price = own_pos.get('avg_price', 0) if own_pos else 0
            
            # Use fixed order size if enabled (for users with smaller balances)
            # Otherwise mirror whale's position size
            if use_fixed_size:
                # Fixed size mode: Buy fixed USD amount once per position
                # Convert own position to USD value to compare
                own_value_usd = own_size * own_avg_price if own_avg_price > 0 else 0
                target_value_usd = fixed_order_size if target_size > 0 else 0
                
                # Compare USD values (prevents duplicate buys)
                size_diff_usd = target_value_usd - own_value_usd
                
                # Store for opportunity creation
                size_diff = size_diff_usd
                target_size_adjusted = target_size  # For metadata/logging
            else:
                # Mirror mode: Match whale's share count
                target_size_adjusted = target_size * position_multiplier
                size_diff = target_size_adjusted - own_size

            if abs(size_diff) > DUST_THRESHOLD:
                # Need to adjust position
                action = 'BUY' if size_diff > 0 else 'SELL'
                
                # SKIP SELL ACTIONS - Whale doesn't make sells, so ignore position reductions
                if action == 'SELL':
                    logger.debug(
                        f"⏭️  Skipping SELL for {position_key[:16]}... - "
                        f"SELL logic disabled (you own more than whale)"
                    )
                    continue
                
                # Log position comparison for debugging
                if use_fixed_size:
                    logger.debug(
                        f"Position {position_key[:16]}...: "
                        f"Own=${own_value_usd:.2f} vs Target=${target_value_usd:.2f} → "
                        f"Diff=${size_diff:.2f} → {action}"
                    )
                
                # Apply time-based filtering for BUY orders
                if action == 'BUY' and ENABLE_TIME_BASED_FILTERING and recent_entries is not None:
                    # Check if this position has recent whale entries
                    if position_key not in recent_entries:
                        # Whale entered this position BEFORE time window
                        question_title = target_pos.get('question', 'Unknown market')[:50]
                        logger.info(
                            f"⏭️  Skipping BUY: Whale entered >{ ENTRY_TIME_WINDOW_MINUTES} min ago - "
                            f"'{question_title}...' (position_key: {position_key[:24]}...)"
                        )
                        continue
                    
                    # Position has recent entries - log it
                    entry_info = recent_entries[position_key]
                    question_title = target_pos.get('question', 'Unknown market')[:50]
                    logger.info(
                        f"✅ Time filter passed: Whale entered {entry_info['minutes_ago']:.1f} min ago "
                        f"({entry_info['trade_count']} trades) - '{question_title}...'"
                    )
                
                # Get current market price from CLOB API
                # For BUY: get ask price (what we'd pay)
                # For SELL: get bid price (what we'd receive)
                side = "buy" if action == 'BUY' else "sell"
                current_price = await self.client.get_market_price(token_id, side)
                
                # Fallback to get_best_price if CLOB API fails
                if current_price is None:
                    logger.warning(f"CLOB price unavailable, falling back to order book")
                    current_price = await self.client.get_best_price(token_id, action)
                
                # Get whale's entry price for comparison
                whale_entry_price = target_pos.get('avg_price', 0)
                
                opportunities.append({
                    'action': action,
                    'token_id': token_id,
                    'condition_id': condition_id,
                    'size': abs(size_diff),
                    'current_price': current_price,
                    'whale_entry_price': whale_entry_price,  # For price comparison
                    'target_size': target_size_adjusted,
                    'own_size': own_size,
                    'question': target_pos.get('question', 'Unknown'),
                    'outcome': target_pos.get('outcome', 'Unknown'),
                    'confidence': 1.0,  # High confidence for mirror strategy
                    'metadata': {
                        'strategy': 'mirror',
                        'target_address': self.target_address,
                        'mirror_logic': 'quick_follow',  # Follow whale within 15-sec polling
                    }
                })

        # SELL LOGIC DISABLED - Whale doesn't make sell orders, so commenting out to improve performance
        # Uncomment below if whale starts exiting positions and you want to mirror exits
        """
        # Check for positions we have but target doesn't (close them)
        # This is TRUE MIRRORING: If whale doesn't hold this specific outcome, neither should we
        # Per Polymarket support: Match by condition_id + asset (not just condition_id)
        for position_key, own_pos in own_positions.items():
            # 'size' from Data API = number of shares (not USD value)
            owned_shares = own_pos.get('size', 0)
            
            # Check if whale holds this SPECIFIC position (condition_id + asset)
            if position_key not in target_positions and owned_shares > 0:
                token_id = own_pos.get('token_id')
                condition_id = own_pos.get('condition_id')
                if not token_id or not condition_id:
                    logger.warning(f"Missing token_id or condition_id for position {position_key}, skipping")
                    continue
                
                # Check if market is closed/resolved (per Polymarket support)
                is_closed = await self.client.is_market_closed(condition_id)
                if is_closed:
                    logger.info(
                        f"⏭️  Market closed/resolved for "
                        f"'{own_pos.get('question', 'Unknown')[:40]}...' - attempting redemption"
                    )
                    
                    # Attempt to redeem winning position
                    redemption_tx = await self.client.redeem_winning_positions(
                        condition_id,
                        own_pos
                    )
                    
                    if redemption_tx:
                        logger.info(f"✅ Automatically redeemed winning position: {redemption_tx}")
                    else:
                        logger.info(f"No redemption needed (either losing position or already redeemed)")
                    
                    continue
                    
                # Get current market price for SELL (bid price)
                # Market may be inactive - handle gracefully
                current_price = None
                try:
                    current_price = await self.client.get_market_price(token_id, "sell")
                    if current_price is None:
                        current_price = await self.client.get_best_price(token_id, 'SELL')
                except Exception as e:
                    error_str = str(e)
                    if "No orderbook exists" in error_str or "404" in error_str:
                        logger.info(
                            f"⏭️  Skipping SELL: No order book for "
                            f"'{own_pos.get('question', 'Unknown')[:40]}...' "
                            f"(market may be closed)"
                        )
                        continue
                    else:
                        logger.warning(f"Error getting price for SELL: {e}, attempting anyway")
                
                # Calculate USD value: shares × current_price
                estimated_usd_value = owned_shares * current_price if current_price else 0
                
                # Skip if position too small (dust) or no price available
                if estimated_usd_value < DUST_THRESHOLD:
                    logger.debug(
                        f"⏭️  Skipping SELL - position too small: {owned_shares:.2f} shares "
                        f"(~${estimated_usd_value:.2f}) for {own_pos.get('question', 'Unknown')[:40]}..."
                    )
                    continue
                
                # Log whale exit detection
                logger.info(
                    f"🚨 WHALE DOESN'T HOLD: {own_pos.get('question', 'Unknown')[:50]} "
                    f"(you hold {owned_shares:.2f} shares worth ~${estimated_usd_value:.2f}) - SELLING"
                )
                
                opportunities.append({
                    'action': 'SELL',
                    'token_id': token_id,
                    'condition_id': condition_id,
                    'size': estimated_usd_value,  # USD value for order manager
                    'shares': owned_shares,  # Actual shares we own (for validation)
                    'current_price': current_price,
                    'whale_entry_price': None,
                    'target_size': 0,
                    'own_size': owned_shares,  # For debugging
                    'question': own_pos.get('question', 'Unknown'),
                    'outcome': own_pos.get('outcome', 'Unknown'),
                    'confidence': 1.0,
                    'metadata': {
                        'strategy': 'mirror',
                        'target_address': self.target_address,
                        'reason': 'whale_doesnt_hold_position'
                    }
                })
        """
        
        return opportunities

    async def _execute_mirror_trade(self, opportunity: Dict[str, Any]) -> None:
        """
        Execute a mirror trade with graceful FOK error handling
        
        Per Polymarket support: FOK orders fail with FOK_ORDER_NOT_FILLED_ERROR
        when no immediate match exists. This is NOT a critical error - just means
        market conditions don't allow immediate fill. Bot will retry on next cycle.
        
        Args:
            opportunity: Trade opportunity details
            
        Raises:
            StrategyError: For critical errors that should stop trading
            FOKOrderNotFilledError: For FOK failures (handled gracefully)
        """
        action = opportunity['action']
        token_id = opportunity['token_id']
        size = opportunity['size']
        
        # For SELL orders where we're exiting positions whale doesn't hold,
        # pass actual shares directly - TRUE MIRRORING means sell regardless of price
        if action == 'SELL' and 'shares' in opportunity:
            shares = opportunity['shares']
            
            # Log diagnostic info to help debug token ID issues
            logger.info(
                f"🔍 SELL Diagnostic: Question='{opportunity.get('question', 'Unknown')[:50]}', "
                f"Outcome='{opportunity.get('outcome', 'Unknown')}', "
                f"Token={token_id[:16]}..., Shares={shares:.2f}"
            )
            
            logger.info(
                f"Executing mirror trade: {action} {shares:.2f} shares of {token_id}"
            )
            execution_size = shares  # Pass shares directly
            is_shares = True
        else:
            logger.info(
                f"Executing mirror trade: {action} {size} USDC of {token_id}"
            )
            execution_size = size  # Pass USD value
            is_shares = False

        # Get whale's entry/exit price for limit order
        whale_entry_price = opportunity.get('whale_entry_price', 0)
        
        # CRITICAL SAFEGUARD: Validate price before execution
        if not whale_entry_price or whale_entry_price <= 0:
            logger.error(
                f"❌ CRITICAL: Missing whale_entry_price for {action} order. "
                f"Opportunity: {opportunity.get('position_key', 'unknown')[:50]}..."
            )
            raise StrategyError("Cannot execute order without whale entry price")
        
        # Apply price buffer for BUY orders to increase fill rate
        # (allows paying slightly more than whale to get filled faster)
        price_buffer_percent = self.config.get('price_buffer_percent', 0)
        
        try:
            # Polymarket support recommendations (Q1, Q2): 
            # Use limit orders at whale's exact price for both BUY and SELL
            # Q1: Stick with exact whale price (don't adjust to 0.99x or 1.01x)
            # Q2: Use limit orders for SELL too, target whale's exact exit price
            # 
            # MODIFICATION: Added configurable price_buffer_percent to allow slightly
            # higher prices for BUY orders (improves fill rate at cost of worse entry)
            # MODIFICATION 2: Added use_market_orders option for instant fills with slippage
            
            use_market_orders = self.config.get('use_market_orders', False)
            
            if use_market_orders:
                # AGGRESSIVE MODE: Use market orders for instant fills
                # CRITICAL: Must validate current market price before execution
                
                # Get current best price from order book
                current_price = await self.client.get_best_price(token_id, action)
                
                if not current_price:
                    logger.warning(
                        f"⚠️  No current price available for {token_id[:8]}... - skipping market order"
                    )
                    from utils.exceptions import OrderRejectedError
                    raise OrderRejectedError("No current price available")
                
                # SAFETY CHECK 1: Validate current price against MAX_BUY_PRICE
                if action == 'BUY' and current_price > MAX_BUY_PRICE:
                    logger.warning(
                        f"🚫 REJECTED: Current market price ${current_price:.4f} exceeds MAX_BUY_PRICE {MAX_BUY_PRICE}"
                    )
                    from utils.exceptions import OrderRejectedError
                    raise OrderRejectedError(f"Current price ${current_price:.4f} exceeds MAX_BUY_PRICE {MAX_BUY_PRICE}")
                
                # SAFETY CHECK 2: Validate current price is not too far from whale's entry
                max_deviation = self.config.get('max_price_deviation_percent', 15.0)
                price_deviation = abs((current_price - whale_entry_price) / whale_entry_price) * 100
                
                if price_deviation > max_deviation:
                    logger.warning(
                        f"🚫 REJECTED: Current price ${current_price:.4f} deviates {price_deviation:.1f}% "
                        f"from whale's entry ${whale_entry_price:.4f} (max: {max_deviation}%)"
                    )
                    from utils.exceptions import OrderRejectedError
                    raise OrderRejectedError(
                        f"Price deviation {price_deviation:.1f}% exceeds max {max_deviation}%"
                    )
                
                # SAFETY CHECK 3: Skip orders below $1 (Polymarket minimum for market orders)
                if action == 'BUY' and execution_size < 1.0:
                    logger.warning(
                        f"🚫 REJECTED: Order size ${execution_size:.2f} below Polymarket's $1 minimum for market orders"
                    )
                    from utils.exceptions import OrderRejectedError
                    raise OrderRejectedError(f"Order size ${execution_size:.2f} below $1 minimum")
                
                logger.info(
                    f"🚀 Using MARKET order - Whale: ${whale_entry_price:.4f}, Current: ${current_price:.4f} "
                    f"(deviation: {price_deviation:.1f}%, within {max_deviation}% limit)"
                )
                
                result = await self.order_manager.execute_market_order(
                    token_id=token_id,
                    side=action,
                    size=execution_size,
                    is_shares=is_shares
                )
                logger.info(
                    f"✅ Market order executed: {result.get('order_id')} {action} @ ${current_price:.4f}"
                )
            elif whale_entry_price > 0:
                # CONSERVATIVE MODE: Use limit orders with price buffer
                # SMART BUFFER LOGIC: Avoid marketable order minimum ($1) for small orders
                # If order < $1: Use exact whale price (ensures non-marketable, no rejection)
                # If order ≥ $1: Use buffer (safe from minimum, better fill rate)
                
                if action == 'BUY' and price_buffer_percent > 0 and execution_size >= 1.0:
                    # Large order: Safe to use buffer for better fill rate
                    buffered_price = whale_entry_price * (1 + price_buffer_percent / 100)
                    # Cap at MAX_BUY_PRICE to enforce strict boundary
                    limit_price = min(buffered_price, MAX_BUY_PRICE)
                    
                    logger.info(
                        f"💰 Price buffer applied: Whale=${whale_entry_price:.4f} → "
                        f"Buffered=${buffered_price:.4f} → Limit=${limit_price:.4f} "
                        f"(+{price_buffer_percent}%, capped at MAX_BUY_PRICE {MAX_BUY_PRICE})"
                    )
                elif action == 'BUY' and execution_size < 1.0:
                    # Small order: Use exact whale price to avoid marketable minimum
                    limit_price = whale_entry_price
                    logger.info(
                        f"📊 Small order (${execution_size:.2f} < $1.00): "
                        f"Using exact whale price ${whale_entry_price:.4f} (no buffer to avoid marketable minimum)"
                    )
                else:
                    # No buffer for SELL orders or when buffer=0
                    limit_price = whale_entry_price
                
                # Use limit order at calculated price
                result = await self.order_manager.execute_limit_order(
                    token_id=token_id,
                    side=action,
                    size=execution_size,
                    price=limit_price  # Whale's price + buffer
                )
                logger.info(
                    f"✅ Limit order placed: {result.get('order_id')} {action} at ${limit_price:.4f}"
                )
            else:
                # Fall back to market order when whale price unavailable
                result = await self.order_manager.execute_market_order(
                    token_id=token_id,
                    side=action,
                    size=execution_size,
                    is_shares=is_shares
                )
                logger.info(
                    f"Mirror trade executed successfully: {result.get('order_id')}"
                )

        except Exception as e:
            from utils.exceptions import FOKOrderNotFilledError, InsufficientBalanceError, OrderRejectedError
            
            # Handle OrderRejectedError - let it bubble up as-is (not a failure, it's a rejection)
            if isinstance(e, OrderRejectedError):
                # Don't wrap it - let it propagate so caller can track as "rejected"
                raise
            
            # Handle FOK failures gracefully (not critical - will retry next cycle)
            elif isinstance(e, FOKOrderNotFilledError):
                logger.info(
                    f"⏸️  FOK order not filled for {action} {size} USDC of {token_id[:8]}... "
                    f"No immediate match available. Will retry on next cycle."
                )
                # Don't raise - this is expected behavior for FOK orders
                return
            
            # Handle insufficient balance (critical - can't retry without funds)
            elif isinstance(e, InsufficientBalanceError):
                logger.error(f"❌ Insufficient balance for {action}: {e}")
                raise StrategyError(f"Cannot execute {action}: {e}")
            
            # All other errors are critical
            else:
                logger.error(f"Mirror trade execution failed: {e}")
                raise StrategyError(f"Failed to execute mirror trade: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get detailed strategy status"""
        status = super().get_status()
        status.update({
            'target_address': self.target_address,
            'last_check_time': self.last_check_time.isoformat() if self.last_check_time else None,
            'target_positions_count': len(self.last_target_positions),
        })
        return status
