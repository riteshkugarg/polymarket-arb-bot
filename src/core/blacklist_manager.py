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

from typing import Dict, Any, Set, Optional, List
from datetime import datetime, timedelta, timezone
from utils.logger import get_logger

logger = get_logger(__name__)


# HARD BLACKLIST: Known problematic market patterns
# These keywords trigger immediate rejection regardless of other market characteristics
HARD_BLACKLIST_KEYWORDS = [
    # Long-dated political contracts (capital lock risk)
    '2027',
    '2028', 
    '2029',
    '2030',
    '2031',
    '2032',
    # Low-liquidity political nomination markets (pre-primary speculation)
    'presidential-nomination',
    'democrat-nomination',
    'republican-nomination',
    'democratic-nomination',
    # Known zombie market patterns
    'will-x-announce',  # Announcement markets often have no resolution
    'by-end-of-decade',  # Extremely long-dated
]

# TEMPORAL GUARDRAIL: Maximum days until settlement (365 days = 1 year)
MAX_DAYS_UNTIL_SETTLEMENT = 365


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
        max_days_until_settlement: int = MAX_DAYS_UNTIL_SETTLEMENT
    ):
        """
        Initialize blacklist manager
        
        Args:
            custom_keywords: Additional blacklist keywords (optional)
            max_days_until_settlement: Maximum days until settlement (default: 365)
        """
        # Merge hard blacklist with custom keywords
        self.blacklist_keywords = set(HARD_BLACKLIST_KEYWORDS)
        if custom_keywords:
            self.blacklist_keywords.update([k.lower() for k in custom_keywords])
        
        # Temporal guardrail
        self.max_days_until_settlement = max_days_until_settlement
        
        # Runtime kill-switch: Manually add problematic condition IDs
        self.blacklisted_condition_ids: Set[str] = set()
        
        # Metrics for monitoring
        self._total_checked = 0
        self._total_blacklisted = 0
        self._blacklist_reasons: Dict[str, int] = {
            'keyword': 0,
            'temporal': 0,
            'manual_id': 0,
        }
        
        logger.info(
            f"MarketBlacklistManager initialized:\n"
            f"  Keyword filters: {len(self.blacklist_keywords)}\n"
            f"  Max settlement horizon: {max_days_until_settlement} days\n"
            f"  Manual ID blacklist: {len(self.blacklisted_condition_ids)} entries"
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
            if log_reason:
                logger.debug(
                    f"[BLACKLIST] {market_id[:8]}... - Manual ID block (condition_id in kill-switch)"
                )
            return True
        
        # CHECK 2: Keyword-based exclusion (slug + question + description)
        slug = market.get('slug', '').lower()
        question = market.get('question', '').lower()
        description = market.get('description', '').lower()
        searchable_text = f"{slug} {question} {description}"
        
        for keyword in self.blacklist_keywords:
            if keyword in searchable_text:
                self._total_blacklisted += 1
                self._blacklist_reasons['keyword'] += 1
                if log_reason:
                    logger.debug(
                        f"[BLACKLIST] {market_id[:8]}... - Keyword match: '{keyword}' | "
                        f"Question: {question[:50]}..."
                    )
                return True
        
        # CHECK 3: Temporal guardrails (settlement date >365 days out)
        end_date_str = market.get('endDate') or market.get('end_date_iso')
        if end_date_str:
            try:
                # Parse ISO 8601 timestamp
                if isinstance(end_date_str, str):
                    # Handle both formats: "2026-11-03T12:00:00Z" and Unix timestamps
                    if 'T' in end_date_str or '-' in end_date_str:
                        # ISO 8601 format
                        end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    else:
                        # Unix timestamp (seconds)
                        end_date = datetime.fromtimestamp(float(end_date_str), tz=timezone.utc)
                elif isinstance(end_date_str, (int, float)):
                    # Unix timestamp
                    end_date = datetime.fromtimestamp(float(end_date_str), tz=timezone.utc)
                else:
                    # Unparseable format - skip temporal check
                    end_date = None
                
                if end_date:
                    now = datetime.now(timezone.utc)
                    days_until_settlement = (end_date - now).days
                    
                    if days_until_settlement > self.max_days_until_settlement:
                        self._total_blacklisted += 1
                        self._blacklist_reasons['temporal'] += 1
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
        
        # Market passed all blacklist checks
        return False
    
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
        }
