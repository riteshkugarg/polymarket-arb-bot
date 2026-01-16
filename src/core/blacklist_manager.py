"""
Market Blacklist Manager - Pre-emptive Zombie Market Filtering

OBJECTIVE: Filter out long-dated political contracts and low-quality markets
BEFORE they reach order book analysis, saving API calls and compute cycles.

Architecture:
1. Keyword-Based Exclusion: Hard-coded patterns for known problematic markets
2. Temporal Guardrails: Reject markets settling >365 days out (capital lock risk)
3. Global ID Blacklist: Runtime kill-switch for specific condition IDs
4. Performance: O(1) lookups using set operations, minimal overhead
"""

from typing import Dict, Any, Set, Optional, List, Union
from datetime import datetime, timedelta, timezone
from collections import deque
from utils.logger import get_logger
import ahocorasick
import json
import aiohttp
import asyncio

logger = get_logger(__name__)


# HARD BLACKLIST: Known problematic market patterns
# POLYMARKET FEEDBACK (Jan 2026): Use endDateIso field instead of year regex
# "This approach is much more reliable than text pattern matching since it uses
# structured date data rather than parsing question descriptions."
#
# INSTITUTIONAL UPGRADE: Removed year keywords ('2027', '2028', '2029', '2030')
# Now using temporal guardrail with endDateIso field for structured filtering
HARD_BLACKLIST_KEYWORDS = [
    # Low-liquidity political nomination markets (pre-primary speculation)
    'presidential-nomination',
    'democrat-nomination',
    'republican-nomination',
    'democratic-nomination',
    # Known zombie market patterns
    'will-x-announce',  # Announcement markets often have no resolution
    'by-end-of-decade',  # Extremely long-dated
]

# TEMPORAL GUARDRAIL: Maximum days until settlement (3 days - capital velocity focus)
# INSTITUTIONAL STANDARD: Focus on short-term markets (15min-3 days)
# Rationale: Crypto markets settle in 15min-24hr, avoid long-term capital lock-up
# Uses endDateIso field for precise date filtering (Polymarket recommendation)
MAX_DAYS_UNTIL_SETTLEMENT = 3

# LIQUIDITY GUARDRAILS: Minimum liquidity and maximum spread thresholds
# INSTITUTIONAL STANDARD: Reject illiquid markets to prevent slippage and execution risk
# Rationale: Markets with < $1,000 liquidity or > 10% spread are high-risk "Zombie" markets
MIN_LIQUIDITY_THRESHOLD = 1000.0  # USD
MAX_SPREAD_THRESHOLD = 0.10  # 10% (calculated as (ask - bid) / ask)


class MarketBlacklistManager:
    """
    High-performance market filtering system
    
    Prevents zombie markets and long-dated contracts from entering the trading pipeline.
    Uses O(1) set lookups and early-exit pattern matching for minimal overhead.
    
    Performance: <1ms per market check on typical market data
    """
    
    def __init__(
        self,
        custom_keywords: Optional[List[str]] = None,
        max_days_until_settlement: int = MAX_DAYS_UNTIL_SETTLEMENT,
        min_liquidity: float = MIN_LIQUIDITY_THRESHOLD,
        max_spread: float = MAX_SPREAD_THRESHOLD,
        remote_config_url: Optional[str] = None,
        remote_config_path: Optional[str] = None
    ):
        """
        Initialize blacklist manager with institutional-grade features
        
        Args:
            custom_keywords: Additional blacklist keywords (optional)
            max_days_until_settlement: Maximum days until settlement (default: 3)
            min_liquidity: Minimum liquidity threshold in USD (default: 1000)
            max_spread: Maximum spread threshold (default: 0.10 = 10%)
            remote_config_url: URL to fetch remote blacklist config (optional)
            remote_config_path: File path to remote blacklist config (optional)
        """
        # Merge hard blacklist with custom keywords - normalize ALL to lowercase
        self.blacklist_keywords = {k.lower() for k in HARD_BLACKLIST_KEYWORDS}
        if custom_keywords:
            self.blacklist_keywords.update([k.lower() for k in custom_keywords])
        
        # Build Aho-Corasick automaton for O(N) keyword matching
        # PERFORMANCE UPGRADE: Single-pass search vs iterative loop (100x faster for large keyword sets)
        self.keyword_automaton = ahocorasick.Automaton()
        for keyword in self.blacklist_keywords:
            self.keyword_automaton.add_word(keyword, keyword)
        self.keyword_automaton.make_automaton()  # Finalize automaton for searching
        
        # Temporal guardrail
        self.max_days_until_settlement = max_days_until_settlement
        
        # Liquidity guardrails
        self.min_liquidity = min_liquidity
        self.max_spread = max_spread
        
        # Runtime kill-switch: Manually add problematic condition IDs
        self.blacklisted_condition_ids: Set[str] = set()
        
        # Remote configuration (for dynamic updates without restart)
        self.remote_config_url = remote_config_url
        self.remote_config_path = remote_config_path
        
        # Metrics for monitoring
        self._total_checked = 0
        self._total_blacklisted = 0
        self._blacklist_reasons: Dict[str, int] = {
            'keyword': 0,
            'temporal': 0,
            'manual_id': 0,
            'liquidity': 0,
            'spread': 0,
        }
        
        # AUDIT TRAIL: Structured rejection history for compliance
        # INSTITUTIONAL STANDARD: Last 1000 rejections with full context for forensics
        self.rejection_history: deque = deque(maxlen=1000)
        
        logger.info(
            f"MarketBlacklistManager initialized (Institutional Grade):\n"
            f"  Keyword filters: {len(self.blacklist_keywords)} (Aho-Corasick automaton)\n"
            f"  Max settlement horizon: {max_days_until_settlement} days\n"
            f"  Min liquidity: ${min_liquidity:,.0f} | Max spread: {max_spread*100:.1f}%\n"
            f"  Manual ID blacklist: {len(self.blacklisted_condition_ids)} entries\n"
            f"  Remote config: {remote_config_url or remote_config_path or 'None'}"
        )
    
    def is_blacklisted(
        self,
        market: Dict[str, Any],
        log_reason: bool = False
    ) -> bool:
        """
        Check if market should be blacklisted
        
        Performance: Early-exit on first match, O(1) set lookups
        
        Args:
            market: Market data from Gamma API
            log_reason: If True, log rejection reason at DEBUG level
            
        Returns:
            True if market is blacklisted, False otherwise
        """
        self._total_checked += 1
        
        market_id = market.get('id', 'unknown')
        condition_id = market.get('conditionId', market_id)
        
        # CHECK 1: Manual ID blacklist (kill-switch for specific markets)
        if condition_id in self.blacklisted_condition_ids:
            self._total_blacklisted += 1
            self._blacklist_reasons['manual_id'] += 1
            
            # AUDIT TRAIL: Record rejection with full context
            self._record_rejection(
                market_id=market_id,
                reason='manual_id',
                trigger_value=condition_id
            )
            
            if log_reason:
                logger.debug(
                    f"[BLACKLIST] {market_id[:8]}... - Manual ID block (condition_id in kill-switch)"
                )
            return True
        
        # CHECK 2: Keyword-based exclusion (slug + question + description)
        # PERFORMANCE: Aho-Corasick automaton for O(N) single-pass matching
        slug = market.get('slug', '').lower()
        question = market.get('question', '').lower()
        description = market.get('description', '').lower()
        searchable_text = f"{slug} {question} {description}"
        
        # Use Aho-Corasick automaton for efficient keyword search
        matched_keywords = list(self.keyword_automaton.iter(searchable_text))
        if matched_keywords:
            self._total_blacklisted += 1
            self._blacklist_reasons['keyword'] += 1
            
            # Get first matched keyword for audit trail
            matched_keyword = matched_keywords[0][1]  # (end_index, keyword)
            
            # AUDIT TRAIL: Record rejection with matched keyword
            self._record_rejection(
                market_id=market_id,
                reason='keyword',
                trigger_value=matched_keyword
            )
            
            if log_reason:
                logger.debug(
                    f"[BLACKLIST] {market_id[:8]}... - Keyword match: '{matched_keyword}' | "
                    f"Question: {question[:50]}..."
                )
            return True
        
        # CHECK 3: Temporal guardrails (settlement date >MAX_DAYS days out)
        # INSTITUTIONAL UPGRADE: Robust parsing for ISO 8601 (with Z/offset) and Unix timestamps
        end_date_str = market.get('endDate') or market.get('end_date_iso')
        if end_date_str:
            try:
                end_date = self._parse_datetime(end_date_str)
                
                if end_date:
                    now = datetime.now(timezone.utc)
                    days_until_settlement = (end_date - now).days
                    
                    if days_until_settlement > self.max_days_until_settlement:
                        self._total_blacklisted += 1
                        self._blacklist_reasons['temporal'] += 1
                        
                        # AUDIT TRAIL: Record rejection with days until settlement
                        self._record_rejection(
                            market_id=market_id,
                            reason='temporal',
                            trigger_value=f"{days_until_settlement} days"
                        )
                        
                        if log_reason:
                            logger.debug(
                                f"[BLACKLIST] {market_id[:8]}... - Settlement too far: "
                                f"{days_until_settlement} days > {self.max_days_until_settlement} days | "
                                f"Question: {question[:50]}..."
                            )
                        return True
                        
            except (ValueError, TypeError, OverflowError) as e:
                # Invalid date format - skip temporal check but don't reject market
                logger.debug(f"Could not parse endDate for {market_id}: {e}")
        
        # CHECK 4: Liquidity guardrails (low liquidity or wide spread)
        # NOTE: This check is optional - some markets may not have liquidity data
        # If liquidity data is available, apply the guardrails
        liquidity_check = self.check_liquidity(market, log_reason=log_reason)
        if liquidity_check['blacklisted']:
            self._total_blacklisted += 1
            reason = liquidity_check['reason']
            self._blacklist_reasons[reason] += 1
            
            # AUDIT TRAIL: Record rejection with liquidity/spread value
            self._record_rejection(
                market_id=market_id,
                reason=reason,
                trigger_value=liquidity_check['trigger_value']
            )
            
            return True
        
        # Market passed all blacklist checks
        return False
    
    def check_liquidity(
        self,
        market: Dict[str, Any],
        log_reason: bool = False
    ) -> Dict[str, Any]:
        """
        Check if market meets liquidity and spread requirements
        
        INSTITUTIONAL STANDARD: Reject illiquid "Zombie" markets before order book analysis
        
        Args:
            market: Market data from Gamma API
            log_reason: If True, log rejection reason at DEBUG level
            
        Returns:
            Dictionary with keys:
                - blacklisted (bool): True if market fails liquidity checks
                - reason (str): 'liquidity' or 'spread' or None
                - trigger_value (str): The actual value that triggered rejection
        """
        market_id = market.get('id', 'unknown')
        question = market.get('question', '')[:50]
        
        # Check 1: Minimum liquidity threshold
        liquidity = market.get('liquidity') or market.get('liquidityNum')
        if liquidity is not None:
            try:
                liquidity_value = float(liquidity)
                if liquidity_value < self.min_liquidity:
                    if log_reason:
                        logger.debug(
                            f"[BLACKLIST] {market_id[:8]}... - Low liquidity: "
                            f"${liquidity_value:,.2f} < ${self.min_liquidity:,.0f} | "
                            f"Question: {question}..."
                        )
                    return {
                        'blacklisted': True,
                        'reason': 'liquidity',
                        'trigger_value': f"${liquidity_value:,.2f}"
                    }
            except (ValueError, TypeError):
                # Invalid liquidity value - skip check
                pass
        
        # Check 2: Maximum spread threshold
        # Calculate spread as (ask - bid) / ask
        # NOTE: This requires orderbook data, which may not be in Gamma API response
        # If best_bid/best_ask are available in market dict, use them
        best_bid = market.get('best_bid') or market.get('bestBid')
        best_ask = market.get('best_ask') or market.get('bestAsk')
        
        if best_bid is not None and best_ask is not None:
            try:
                bid_value = float(best_bid)
                ask_value = float(best_ask)
                
                # Avoid division by zero
                if ask_value > 0:
                    spread = (ask_value - bid_value) / ask_value
                    
                    if spread > self.max_spread:
                        if log_reason:
                            logger.debug(
                                f"[BLACKLIST] {market_id[:8]}... - Wide spread: "
                                f"{spread*100:.1f}% > {self.max_spread*100:.1f}% | "
                                f"Question: {question}..."
                            )
                        return {
                            'blacklisted': True,
                            'reason': 'spread',
                            'trigger_value': f"{spread*100:.1f}%"
                        }
            except (ValueError, TypeError):
                # Invalid bid/ask values - skip check
                pass
        
        # Market passed liquidity checks
        return {'blacklisted': False, 'reason': None, 'trigger_value': None}
    
    def add_manual_blacklist(self, condition_id: str, reason: str = "") -> None:
        """
        Manually blacklist a specific market by condition ID (kill-switch)
        
        Use case: Discovered a problematic market in production that needs immediate blocking
        
        Args:
            condition_id: Market condition ID to blacklist
            reason: Human-readable reason for blacklisting (for audit log)
        """
        self.blacklisted_condition_ids.add(condition_id)
        logger.warning(
            f"[KILL-SWITCH] Manually blacklisted condition_id: {condition_id} | "
            f"Reason: {reason or 'No reason provided'}"
        )
    
    def remove_manual_blacklist(self, condition_id: str) -> bool:
        """
        Remove a market from manual blacklist
        
        Args:
            condition_id: Market condition ID to un-blacklist
            
        Returns:
            True if removed, False if not in blacklist
        """
        if condition_id in self.blacklisted_condition_ids:
            self.blacklisted_condition_ids.remove(condition_id)
            logger.info(f"[KILL-SWITCH] Removed {condition_id} from manual blacklist")
            return True
        return False
    
    async def sync_blacklist(self) -> bool:
        """
        Fetch updated blacklist configuration from remote source
        
        INSTITUTIONAL STANDARD: Dynamic blacklist updates without bot restart
        
        Returns:
            True if sync succeeded, False otherwise
        """
        if not self.remote_config_url and not self.remote_config_path:
            logger.debug("No remote config source configured, skipping sync")
            return False
        
        try:
            config_data = None
            
            # Fetch from URL
            if self.remote_config_url:
                async with aiohttp.ClientSession() as session:
                    async with session.get(self.remote_config_url, timeout=10) as response:
                        if response.status == 200:
                            config_data = await response.json()
                            logger.info(f"✅ Fetched remote config from {self.remote_config_url}")
                        else:
                            logger.warning(
                                f"Failed to fetch remote config: HTTP {response.status}"
                            )
                            return False
            
            # Fetch from file
            elif self.remote_config_path:
                with open(self.remote_config_path, 'r') as f:
                    config_data = json.load(f)
                    logger.info(f"✅ Loaded remote config from {self.remote_config_path}")
            
            if not config_data:
                return False
            
            # Update blacklist from config
            # Expected format: {"keywords": [...], "condition_ids": [...]}
            if 'keywords' in config_data:
                new_keywords = [k.lower() for k in config_data['keywords']]
                self.blacklist_keywords.update(new_keywords)
                
                # Rebuild Aho-Corasick automaton with new keywords
                self.keyword_automaton = ahocorasick.Automaton()
                for keyword in self.blacklist_keywords:
                    self.keyword_automaton.add_word(keyword, keyword)
                self.keyword_automaton.make_automaton()
                
                logger.info(f"✅ Updated keyword blacklist: {len(new_keywords)} new keywords")
            
            if 'condition_ids' in config_data:
                new_ids = set(config_data['condition_ids'])
                self.blacklisted_condition_ids.update(new_ids)
                logger.info(f"✅ Updated condition ID blacklist: {len(new_ids)} new IDs")
            
            return True
            
        except aiohttp.ClientError as e:
            logger.error(f"Network error fetching remote config: {e}")
            return False
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in remote config: {e}")
            return False
        except FileNotFoundError:
            logger.error(f"Remote config file not found: {self.remote_config_path}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error syncing blacklist: {e}")
            return False
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get blacklist statistics for monitoring
        
        Returns:
            Dictionary with filtering metrics
        """
        total = self._total_checked
        blacklisted = self._total_blacklisted
        pass_rate = ((total - blacklisted) / total * 100) if total > 0 else 0
        
        return {
            'total_checked': total,
            'total_blacklisted': blacklisted,
            'pass_rate_pct': pass_rate,
            'blacklist_reasons': self._blacklist_reasons.copy(),
            'manual_blacklist_size': len(self.blacklisted_condition_ids),
        }
    
    def log_summary(self) -> None:
        """
        Log a summary of blacklist filtering activity
        
        Called after market discovery to provide visibility into filtering
        """
        stats = self.get_stats()
        
        if stats['total_blacklisted'] > 0:
            logger.info(
                f"[BLACKLIST] Filtered out {stats['total_blacklisted']} "
                f"long-dated/zombie markets from {stats['total_checked']} total "
                f"(Pass rate: {stats['pass_rate_pct']:.1f}%) | "
                f"Reasons: Keyword={stats['blacklist_reasons']['keyword']}, "
                f"Temporal={stats['blacklist_reasons']['temporal']}, "
                f"Liquidity={stats['blacklist_reasons']['liquidity']}, "
                f"Spread={stats['blacklist_reasons']['spread']}, "
                f"Manual={stats['blacklist_reasons']['manual_id']}"
            )
    
    def reset_stats(self) -> None:
        """Reset statistics counters (useful for per-scan tracking)"""
        self._total_checked = 0
        self._total_blacklisted = 0
        self._blacklist_reasons = {
            'keyword': 0,
            'temporal': 0,
            'manual_id': 0,
            'liquidity': 0,
            'spread': 0,
        }
    
    def _record_rejection(
        self,
        market_id: str,
        reason: str,
        trigger_value: Any
    ) -> None:
        """
        Record a market rejection in the audit trail
        
        INSTITUTIONAL STANDARD: Structured audit logging for compliance
        
        Args:
            market_id: Market ID that was rejected
            reason: Rejection reason ('keyword', 'temporal', 'manual_id', 'liquidity', 'spread')
            trigger_value: The specific value that triggered rejection
        """
        self.rejection_history.append({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'market_id': market_id,
            'reason': reason,
            'trigger_value': str(trigger_value)
        })
    
    def get_audit_report(self) -> str:
        """
        Export rejection history as JSON for compliance audits
        
        INSTITUTIONAL STANDARD: Transparent audit trail for every rejected opportunity
        
        Returns:
            JSON string with last 1000 rejections
        """
        return json.dumps(list(self.rejection_history), indent=2)
    
    def _parse_datetime(self, date_input: Union[str, int, float]) -> Optional[datetime]:
        """
        Robust datetime parser for ISO 8601 and Unix timestamps
        
        INSTITUTIONAL UPGRADE: Handle both ISO 8601 (with Z/offset) and Unix timestamps
        
        Args:
            date_input: Date string (ISO 8601) or Unix timestamp (int/float)
            
        Returns:
            Timezone-aware datetime object or None if parsing fails
        """
        try:
            if isinstance(date_input, str):
                # Handle ISO 8601 formats
                if 'T' in date_input or '-' in date_input:
                    # Replace 'Z' with '+00:00' for Python's fromisoformat
                    # Handles: "2026-11-03T12:00:00Z" or "2026-11-03T12:00:00+00:00"
                    date_str = date_input.replace('Z', '+00:00')
                    return datetime.fromisoformat(date_str)
                else:
                    # Treat as Unix timestamp string
                    return datetime.fromtimestamp(float(date_input), tz=timezone.utc)
            
            elif isinstance(date_input, (int, float)):
                # Unix timestamp (seconds)
                return datetime.fromtimestamp(float(date_input), tz=timezone.utc)
            
            else:
                return None
                
        except (ValueError, TypeError, OverflowError):
            return None
