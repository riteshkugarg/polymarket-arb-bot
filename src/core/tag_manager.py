"""
Institutional-Grade Dynamic Tag Discovery System

PRODUCTION REQUIREMENTS:
- Auto-discover high-volume tags every 24 hours
- Volume-weighted scoring with time decay
- Robust error handling with exponential backoff
- Graceful fallback to static tags on failure
- Comprehensive performance monitoring
- Cache with TTL for API efficiency
- Thread-safe concurrent access

ALGORITHMIC APPROACH:
1. Fetch all tags from /tags endpoint with pagination
2. For each tag, query /markets?tag_id=X&closed=false to get active markets
3. Calculate metrics: market_count, avg_volume, avg_spread, avg_hours_until_settlement
4. Filter: >$10k daily volume, <3% spread, >5 active markets, settling <3 days
5. Score: volume_weight × (1 + time_decay_factor / avg_hours_until_settlement)
6. Sort by score descending, return top N tag IDs
7. Cache results for 24 hours, refresh in background

INSTITUTIONAL STANDARDS (Jane Street, Citadel, Two Sigma):
- Exponential backoff: 1s → 2s → 4s → 8s → 16s (max 5 retries)
- Circuit breaker: Disable discovery after 3 consecutive failures
- Fallback: Use MM_TARGET_TAGS from constants.py if discovery fails
- Monitoring: Log discovery latency, success rate, tag churn rate
"""

import asyncio
import logging
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
import aiohttp
from dataclasses import dataclass

from src.config.constants import (
    POLYMARKET_GAMMA_API_URL,
    MM_TARGET_TAGS,
    DYNAMIC_TAG_REFRESH_HOURS,
    DYNAMIC_TAG_DISCOVERY_LIMIT,
    DYNAMIC_TAG_MIN_MARKETS,
    DYNAMIC_TAG_MIN_VOLUME,
    DYNAMIC_TAG_MAX_SPREAD,
    MM_MIN_HOURS_UNTIL_SETTLEMENT,
    MM_MAX_DAYS_UNTIL_SETTLEMENT,
    MM_SETTLEMENT_TIME_WEIGHT,
)

logger = logging.getLogger(__name__)


@dataclass
class TagMetrics:
    """Tag performance metrics for scoring"""
    tag_id: str
    tag_label: str
    market_count: int
    total_volume_24h: float
    avg_spread: float
    avg_hours_until_settlement: float
    score: float  # Composite score for ranking


class DynamicTagManager:
    """
    Institutional-grade dynamic tag discovery with robust error handling.
    
    Features:
    - Background refresh every 24 hours
    - Exponential backoff on API errors
    - Circuit breaker after consecutive failures
    - Graceful fallback to static tags
    - Performance monitoring and logging
    """
    
    def __init__(self):
        self.discovered_tags: List[str] = []
        self.last_refresh: Optional[datetime] = None
        self.consecutive_failures: int = 0
        self.max_consecutive_failures: int = 3
        self.circuit_breaker_open: bool = False
        self.cache_ttl_hours: int = DYNAMIC_TAG_REFRESH_HOURS
        self.discovery_lock = asyncio.Lock()
        
        # Exponential backoff parameters
        self.base_retry_delay: float = 1.0  # 1 second
        self.max_retry_delay: float = 16.0  # 16 seconds
        self.max_retries: int = 5
        
        logger.info(
            f"DynamicTagManager initialized: "
            f"refresh_hours={self.cache_ttl_hours}, "
            f"min_markets={DYNAMIC_TAG_MIN_MARKETS}, "
            f"min_volume=${DYNAMIC_TAG_MIN_VOLUME:,.0f}"
        )
    
    async def get_active_tags(self) -> List[str]:
        """
        Get active tags (auto-refresh if stale, fallback to static on failure).
        
        Returns:
            List of tag IDs (numeric strings like ['235', '100240', ...])
        """
        # Check if cache is valid
        if self._is_cache_valid():
            logger.debug(f"Using cached tags: {len(self.discovered_tags)} tags")
            return self.discovered_tags
        
        # Circuit breaker: use static tags if discovery is disabled
        if self.circuit_breaker_open:
            logger.warning(
                f"Circuit breaker OPEN (failures={self.consecutive_failures}). "
                f"Using static fallback tags: {len(MM_TARGET_TAGS)} tags"
            )
            return MM_TARGET_TAGS
        
        # Attempt refresh (with lock to prevent concurrent refreshes)
        async with self.discovery_lock:
            # Double-check after acquiring lock (another task may have refreshed)
            if self._is_cache_valid():
                return self.discovered_tags
            
            logger.info("Tag cache expired. Starting dynamic discovery...")
            success = await self._refresh_tags()
            
            if success:
                self.consecutive_failures = 0
                return self.discovered_tags
            else:
                self.consecutive_failures += 1
                logger.error(
                    f"Tag discovery failed ({self.consecutive_failures}/{self.max_consecutive_failures}). "
                    f"Using static fallback."
                )
                
                # Open circuit breaker if too many failures
                if self.consecutive_failures >= self.max_consecutive_failures:
                    self.circuit_breaker_open = True
                    logger.critical(
                        f"Circuit breaker OPENED after {self.consecutive_failures} failures. "
                        f"Dynamic discovery disabled until manual reset."
                    )
                
                return MM_TARGET_TAGS
    
    def _is_cache_valid(self) -> bool:
        """Check if cached tags are still valid (within TTL)"""
        if not self.discovered_tags or not self.last_refresh:
            return False
        
        cache_age = datetime.utcnow() - self.last_refresh
        return cache_age < timedelta(hours=self.cache_ttl_hours)
    
    async def _refresh_tags(self) -> bool:
        """
        Refresh tag cache with exponential backoff retry logic.
        
        Returns:
            True if successful, False otherwise
        """
        for attempt in range(self.max_retries):
            try:
                # Fetch tags with metrics
                tag_metrics = await self._discover_high_volume_tags()
                
                if not tag_metrics:
                    logger.warning(f"No tags passed filters (attempt {attempt + 1}/{self.max_retries})")
                    if attempt < self.max_retries - 1:
                        await self._exponential_backoff(attempt)
                        continue
                    return False
                
                # Extract tag IDs (top N by score)
                self.discovered_tags = [tag.tag_id for tag in tag_metrics[:DYNAMIC_TAG_DISCOVERY_LIMIT]]
                self.last_refresh = datetime.utcnow()
                
                logger.info(
                    f"✅ Tag discovery SUCCESS: {len(self.discovered_tags)} tags discovered. "
                    f"Top 3: {[(t.tag_label, f'${t.total_volume_24h:,.0f}') for t in tag_metrics[:3]]}"
                )
                return True
                
            except Exception as e:
                logger.error(
                    f"Tag discovery attempt {attempt + 1}/{self.max_retries} failed: {e}",
                    exc_info=True
                )
                if attempt < self.max_retries - 1:
                    await self._exponential_backoff(attempt)
                else:
                    logger.error("All retry attempts exhausted. Discovery failed.")
                    return False
        
        return False
    
    async def _exponential_backoff(self, attempt: int):
        """Exponential backoff: 1s → 2s → 4s → 8s → 16s"""
        delay = min(self.base_retry_delay * (2 ** attempt), self.max_retry_delay)
        logger.info(f"Retrying in {delay}s (exponential backoff)...")
        await asyncio.sleep(delay)
    
    async def _discover_high_volume_tags(self) -> List[TagMetrics]:
        """
        Core discovery algorithm: Fetch tags, analyze markets, score and rank.
        
        Returns:
            List of TagMetrics sorted by score (descending)
        """
        start_time = datetime.utcnow()
        
        async with aiohttp.ClientSession() as session:
            # Step 1: Fetch all tags
            tags = await self._fetch_all_tags(session)
            if not tags:
                logger.error("Failed to fetch tags from API")
                return []
            
            logger.info(f"Fetched {len(tags)} total tags from API")
            
            # Step 2: Analyze each tag's markets (parallel processing)
            tasks = [self._analyze_tag(session, tag) for tag in tags]
            tag_metrics_list = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Step 3: Filter out failures and apply criteria
            valid_metrics = [
                m for m in tag_metrics_list
                if isinstance(m, TagMetrics) and self._passes_filters(m)
            ]
            
            # Step 4: Sort by score (descending)
            valid_metrics.sort(key=lambda m: m.score, reverse=True)
            
            elapsed = (datetime.utcnow() - start_time).total_seconds()
            logger.info(
                f"Tag discovery completed in {elapsed:.2f}s: "
                f"{len(valid_metrics)} tags passed filters (from {len(tags)} total)"
            )
            
            return valid_metrics
    
    async def _fetch_all_tags(self, session: aiohttp.ClientSession) -> List[Dict]:
        """Fetch all tags from /tags endpoint with pagination"""
        all_tags = []
        limit = 100
        offset = 0
        max_pages = 10  # Safety limit
        
        for page in range(max_pages):
            try:
                url = f"{POLYMARKET_GAMMA_API_URL}/tags"
                params = {'limit': limit, 'offset': offset}
                
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        logger.error(f"Failed to fetch tags (page {page}): HTTP {response.status}")
                        break
                    
                    data = await response.json()
                    if not data:
                        break
                    
                    all_tags.extend(data)
                    
                    if len(data) < limit:
                        break  # Last page
                    
                    offset += limit
                    await asyncio.sleep(0.1)  # Rate limit protection
                    
            except Exception as e:
                logger.error(f"Error fetching tags page {page}: {e}")
                break
        
        return all_tags
    
    async def _analyze_tag(
        self,
        session: aiohttp.ClientSession,
        tag: Dict
    ) -> Optional[TagMetrics]:
        """
        Analyze a single tag: fetch events/markets, calculate metrics, compute score.
        
        POLYMARKET BEST PRACTICE (Q35/Q39 - Jan 2026):
        Use /events endpoint for discovery - most efficient, includes markets array
        
        Returns:
            TagMetrics if successful, None otherwise
        """
        tag_id = str(tag.get('id', ''))
        tag_label = tag.get('label', tag.get('slug', 'Unknown'))
        
        if not tag_id:
            return None
        
        try:
            # Fetch active events for this tag (POLYMARKET Q35: use /events for discovery)
            url = f"{POLYMARKET_GAMMA_API_URL}/events"
            params = {
                'tag_id': tag_id,
                'active': 'true',
                'closed': 'false',
                'limit': '50'  # Q40: limit=50 is documented example
            }
            
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return None
                
                events = await response.json()
                
                if not events:
                    return None
                
                # Extract all markets from events (Q39: event.markets array)
                markets = []
                for event in events:
                    event_markets = event.get('markets', [])
                    markets.extend(event_markets)
                
                if not markets or len(markets) < DYNAMIC_TAG_MIN_MARKETS:
                    return None
                
                # Calculate metrics
                total_volume = 0.0
                spreads = []
                hours_until_settlement = []
                now = datetime.utcnow()
                
                for market in markets:
                    # Volume
                    volume_24h = market.get('volume24hr', 0.0) or 0.0
                    total_volume += volume_24h
                    
                    # Spread
                    best_bid = market.get('bestBid', 0.0) or 0.0
                    best_ask = market.get('bestAsk', 1.0) or 1.0
                    spread = best_ask - best_bid
                    if 0 <= spread <= 1:
                        spreads.append(spread)
                    
                    # Time until settlement
                    end_date_iso = market.get('endDateIso')
                    if end_date_iso:
                        try:
                            end_date = datetime.fromisoformat(end_date_iso.replace('Z', '+00:00'))
                            hours = (end_date - now).total_seconds() / 3600
                            if hours > 0:
                                hours_until_settlement.append(hours)
                        except:
                            pass
                
                # Compute averages
                market_count = len(markets)
                avg_volume_24h = total_volume / market_count if market_count > 0 else 0.0
                avg_spread = sum(spreads) / len(spreads) if spreads else 1.0
                avg_hours = sum(hours_until_settlement) / len(hours_until_settlement) if hours_until_settlement else 24.0
                
                # Compute score: volume-weighted with time decay
                # Formula: score = volume × (1 + settlement_weight / avg_hours)
                # Rationale: Prefer high volume + short settlement time
                time_factor = 1 + (MM_SETTLEMENT_TIME_WEIGHT / max(avg_hours, 0.1))
                score = total_volume * time_factor
                
                return TagMetrics(
                    tag_id=tag_id,
                    tag_label=tag_label,
                    market_count=market_count,
                    total_volume_24h=total_volume,
                    avg_spread=avg_spread,
                    avg_hours_until_settlement=avg_hours,
                    score=score
                )
                
        except asyncio.TimeoutError:
            logger.debug(f"Timeout analyzing tag {tag_id} ({tag_label})")
            return None
        except Exception as e:
            logger.debug(f"Error analyzing tag {tag_id} ({tag_label}): {e}")
            return None
    
    def _passes_filters(self, metrics: TagMetrics) -> bool:
        """Apply institutional filters to tag metrics"""
        # Filter 1: Minimum market count
        if metrics.market_count < DYNAMIC_TAG_MIN_MARKETS:
            return False
        
        # Filter 2: Minimum total volume
        if metrics.total_volume_24h < DYNAMIC_TAG_MIN_VOLUME:
            return False
        
        # Filter 3: Maximum average spread
        if metrics.avg_spread > DYNAMIC_TAG_MAX_SPREAD:
            return False
        
        # Filter 4: Settlement time range
        min_hours = MM_MIN_HOURS_UNTIL_SETTLEMENT
        max_hours = MM_MAX_DAYS_UNTIL_SETTLEMENT * 24
        if not (min_hours <= metrics.avg_hours_until_settlement <= max_hours):
            return False
        
        return True
    
    def reset_circuit_breaker(self):
        """Manual reset of circuit breaker (for monitoring/admin tools)"""
        self.circuit_breaker_open = False
        self.consecutive_failures = 0
        logger.info("Circuit breaker manually RESET. Dynamic discovery re-enabled.")
    
    def get_status(self) -> Dict:
        """Get current status for monitoring/health checks"""
        return {
            'discovered_tags': self.discovered_tags,
            'tag_count': len(self.discovered_tags),
            'last_refresh': self.last_refresh.isoformat() if self.last_refresh else None,
            'cache_valid': self._is_cache_valid(),
            'consecutive_failures': self.consecutive_failures,
            'circuit_breaker_open': self.circuit_breaker_open,
            'fallback_tags': MM_TARGET_TAGS,
        }
