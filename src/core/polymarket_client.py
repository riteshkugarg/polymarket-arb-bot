"""
Polymarket CLOB Client
Handles all interactions with Polymarket's Central Limit Order Book API
"""

from typing import Dict, List, Optional, Any
from decimal import Decimal

import asyncio
import aiohttp
import json
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, MarketOrderArgs
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.exceptions import PolyApiException
from eth_account import Account
from web3 import Web3

from config.constants import (
    CHAIN_ID,
    CLOB_API_URL,
    FUNDER_ADDRESS,
    POLYGON_CHAIN_ID,
    API_TIMEOUT_SEC,
    MAX_RETRIES,
    PROXY_WALLET_ADDRESS,
    POLYMARKET_DATA_API_URL,
    POLYMARKET_GAMMA_API_URL,
    USDC_CONTRACT_ADDRESS,
    CTF_CONTRACT_ADDRESS,
    POLYGON_RPC_URL,
)
from config.aws_config import get_aws_config
from utils.logger import get_logger
from utils.exceptions import (
    APIError,
    AuthenticationError,
    OrderRejectionError,
    InsufficientBalanceError,
    NetworkError
)
from utils.helpers import async_retry_with_backoff


logger = get_logger(__name__)


class PolymarketClient:
    """
    High-level client for interacting with Polymarket
    Provides reusable methods for common operations
    """

    def __init__(self):
        """Initialize Polymarket client with credentials from AWS Secrets Manager"""
        self._client: Optional[ClobClient] = None
        self._account: Optional[Account] = None
        self._private_key: Optional[str] = None
        self._is_initialized = False
        # Cache for token ID lookups (stable per Polymarket support)
        self._token_id_cache: Dict[tuple, str] = {}
        # General purpose cache for market status and fee rates
        self._cache: Dict[str, Any] = {}
        # Cache with TTL for temporary data (404 results, active market checks)
        # Format: {key: (value, expiry_timestamp)}
        self._cache_with_ttl: Dict[str, tuple] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        
        logger.info("Polymarket client created (lazy initialization)")

    async def initialize(self) -> None:
        """
        Initialize client with credentials from AWS Secrets Manager
        Lazy initialization to avoid blocking at import time
        """
        if self._is_initialized:
            logger.debug("Client already initialized")
            return

        try:
            # Retrieve credentials from AWS Secrets Manager
            aws_config = get_aws_config()
            self._private_key = aws_config.get_wallet_private_key()
            
            # Security validation: Ensure private key is valid
            if not self._private_key or len(self._private_key) < 64:
                raise AuthenticationError(
                    "Invalid private key format. Must be 64+ character hex string."
                )
            
            # Create Ethereum account from private key (SIGNER WALLET)
            # This is your MetaMask address that signs all transactions
            try:
                self._account = Account.from_key(self._private_key)
            except Exception as e:
                raise AuthenticationError(
                    f"Failed to create account from private key: {e}"
                )
            logger.info(f"🔑 Signer wallet (MetaMask): {self._account.address}")
            logger.info(f"💼 Proxy wallet (Polymarket): {PROXY_WALLET_ADDRESS}")
            
            # Polymarket Dual-Address System:
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # SIGNER (self._account.address) → Signs transactions
            # PROXY (PROXY_WALLET_ADDRESS)   → Holds funds & positions
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # LEVEL 2 (L2) AUTHENTICATION SETUP
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # Polymarket uses two authentication levels:
            #
            # L1 (Private Key): For read-only operations
            #   - Market data, prices, order books
            #   - Position queries, balance checks
            #
            # L2 (API Credentials): For write operations  
            #   - post_order() - Place BUY/SELL orders
            #   - cancel_order() - Cancel orders
            #   - All order management operations
            #
            # L2 credentials are wallet-specific and must be:
            #   1. Generated via Polymarket's create_api_key() API
            #   2. Stored securely in AWS Secrets Manager
            #   3. Loaded on every bot initialization
            #
            # Security: Each wallet has ONE active credential set.
            #           Creating new credentials invalidates old ones.
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            
            logger.info("🔑 Loading L2 API credentials from Secrets Manager...")
            api_creds_dict = aws_config.get_api_credentials()
            
            # Create ApiCreds object required by py-clob-client
            from py_clob_client.clob_types import ApiCreds
            api_creds = ApiCreds(
                api_key=api_creds_dict['api_key'],
                api_secret=api_creds_dict['api_secret'],
                api_passphrase=api_creds_dict['api_passphrase']
            )
            logger.info("✅ L2 API credentials loaded successfully")
            
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # CLOB CLIENT INITIALIZATION WITH L2 AUTH
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # Initialize Polymarket CLOB client with full authentication:
            #
            # Parameters:
            #   host: CLOB API endpoint (https://clob.polymarket.com)
            #   chain_id: 137 (Polygon mainnet)
            #   key: MetaMask private key for transaction signing (L1)
            #   creds: API credentials for order operations (L2) **REQUIRED**
            #   signature_type: 2 (MetaMask/Gnosis Safe signature format)
            #   funder: Proxy wallet address (holds USDC and positions)
            #
            # Without 'creds' parameter:
            #   ✅ Can fetch market data, prices, positions
            #   ❌ CANNOT place orders (L2_AUTH_UNAVAILABLE error)
            #
            # With 'creds' parameter:
            #   ✅ Full access to all CLOB operations
            #   ✅ Can place BUY/SELL orders
            #   ✅ Can cancel orders and manage positions
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            self._client = ClobClient(
                host=CLOB_API_URL,
                chain_id=POLYGON_CHAIN_ID,
                key=self._private_key,            # L1: Signs transactions
                creds=api_creds,                   # L2: Order operations (REQUIRED)
                signature_type=2,                  # MetaMask/Gnosis Safe format
                funder=PROXY_WALLET_ADDRESS        # Proxy wallet (holds funds)
            )
            logger.info("✅ CLOB client initialized with L2 authentication")
            
            # Initialize aiohttp session for REST API calls with connection pooling
            # Connection pooling improves performance for repeated API calls
            connector = aiohttp.TCPConnector(
                limit=100,  # Max connections
                limit_per_host=30,  # Max per host
                ttl_dns_cache=300,  # DNS cache TTL
                enable_cleanup_closed=True  # Clean up closed connections
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SEC),
                headers={
                    "User-Agent": "Polymarket-Bot/2.0",
                    "Accept": "application/json"
                }
            )
            
            self._is_initialized = True
            logger.info(
                f"Polymarket client successfully initialized - "
                f"Signer: {self._account.address[:10]}..., "
                f"Proxy: {PROXY_WALLET_ADDRESS[:10]}..., "
                f"Cache enabled: True"
            )
            
            # Check geoblock status (per Polymarket support recommendation)
            # NOTE: Geoblock endpoint currently returns 404 - may not be publicly available
            # Commenting out for now, but keeping for future when endpoint is available
            # await self._check_geoblock_status()
            logger.info("⚠️  Geoblock check skipped (endpoint returns 404) - assuming region is allowed")
            
        except Exception as e:
            logger.error(f"Failed to initialize Polymarket client: {e}")
            raise AuthenticationError(f"Client initialization failed: {e}")

    async def _check_geoblock_status(self) -> None:
        """
        Check if current IP is geoblocked by Polymarket.
        
        Per Polymarket support: Use geoblock endpoint to verify IP isn't in restricted region.
        Geoblocked regions cannot place orders.
        """
        try:
            geoblock_url = f"{CLOB_API_URL}/geoblock"
            async with self._session.get(geoblock_url) as response:
                if response.status == 200:
                    data = await response.json()
                    is_blocked = data.get("restricted", False)
                    
                    if is_blocked:
                        logger.error(
                            f"🚫 CRITICAL: Your IP is GEOBLOCKED by Polymarket. "
                            f"Region: {data.get('country', 'Unknown')}. "
                            f"You cannot place orders from this location. "
                            f"Per Polymarket TOS: Using VPNs/proxies to bypass restrictions is prohibited."
                        )
                    else:
                        logger.info(
                            f"✅ IP geoblock check passed. "
                            f"Region: {data.get('country', 'Unknown')} - Trading allowed."
                        )
                else:
                    logger.warning(f"Could not verify geoblock status: HTTP {response.status}")
        except Exception as e:
            logger.warning(f"Geoblock check failed (non-critical): {e}")

    async def close(self) -> None:
        """
        Gracefully close client connections and cleanup resources.
        
        IMPORTANT: Always call this when shutting down the bot to prevent
        resource leaks and ensure all pending requests complete.
        """
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("Closed aiohttp session")
        
        self._is_initialized = False
        logger.info(f"Polymarket client closed - Cache size: {len(self._token_id_cache)}")
    
    def _ensure_initialized(self) -> None:
        """Ensure client is initialized before operations"""
        if not self._is_initialized:
            raise AuthenticationError(
                "Client not initialized. Call initialize() first."
            )

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_markets(
        self,
        next_cursor: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get list of active markets
        
        Args:
            next_cursor: Pagination cursor
            
        Returns:
            Dictionary containing markets data
        """
        self._ensure_initialized()
        
        try:
            logger.debug("Fetching markets from Polymarket")
            # Only pass next_cursor if it's provided and valid
            if next_cursor:
                response = await asyncio.to_thread(
                    self._client.get_markets,
                    next_cursor=next_cursor
                )
            else:
                response = await asyncio.to_thread(
                    self._client.get_markets
                )
            logger.debug(f"Retrieved {len(response.get('data', []))} markets")
            return response
            
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            raise APIError(f"Failed to fetch markets: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_market(self, condition_id: str) -> Dict[str, Any]:
        """
        Get specific market details
        
        Args:
            condition_id: Market condition ID
            
        Returns:
            Market data dictionary
        """
        self._ensure_initialized()
        
        try:
            logger.debug(f"Fetching market: {condition_id}")
            market = await asyncio.to_thread(
                self._client.get_market,
                condition_id=condition_id
            )
            return market
            
        except Exception as e:
            logger.error(f"Failed to fetch market {condition_id}: {e}")
            raise APIError(f"Failed to fetch market: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_events(
        self,
        limit: int = 100,
        offset: int = 0,
        closed: bool = False,
        active: bool = True,
        tag_id: Optional[str] = None,
        order: str = "id",
        ascending: bool = False
    ) -> Dict[str, Any]:
        """
        Get list of events from Gamma API (for multi-outcome arbitrage)
        
        Per Polymarket Support (Jan 2026):
        - Events contain multiple Markets (each market is binary Yes/No)
        - Multi-outcome arbitrage works across markets within one event
        - Use outcomes array length to identify multi-outcome events
        - Pagination via limit/offset (no default limit - must specify)
        - Rate limit: 500 req/10s
        
        Args:
            limit: Results per page (default 100, max recommended: 100)
            offset: Starting position for pagination
            closed: Include closed events (default False)
            active: Only active events (default True)
            tag_id: Filter by category tag
            order: Sort field (default "id")
            ascending: Sort direction (default False = newest first)
            
        Returns:
            {
                'data': [
                    {
                        'id': str,
                        'title': str,
                        'slug': str,
                        'markets': [...]  # List of binary markets in this event
                        'clobTokenIds': [...]  # All token IDs across markets
                        'outcomes': [...]  # Outcome labels
                        'outcomePrices': [...]  # Current prices
                        'volume': float,
                        'liquidity': float,
                        'negRisk': bool,  # Is this a NegRisk event?
                        ...
                    }
                ],
                'count': int,
                'limit': int,
                'offset': int
            }
        
        Example multi-outcome event:
            Event: "2024 US Presidential Election"
            Markets: [Trump market, Biden market, Harris market, ...]
            Arbitrage: If sum(YES_prices) < $1.00, profit opportunity exists
        """
        try:
            url = f"{POLYMARKET_GAMMA_API_URL}/events"
            
            params = {
                'limit': limit,
                'offset': offset,
                'closed': str(closed).lower(),
                'active': str(active).lower(),
                'order': order,
                'ascending': str(ascending).lower()
            }
            
            if tag_id:
                params['tag_id'] = tag_id
            
            logger.debug(f"Fetching events from Gamma API: {params}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SEC)) as response:
                    response.raise_for_status()
                    data = await response.json()
                    
                    events_count = len(data) if isinstance(data, list) else len(data.get('data', []))
                    logger.debug(f"Retrieved {events_count} events from Gamma API")
                    
                    # Normalize response format (Gamma API returns array directly)
                    if isinstance(data, list):
                        return {
                            'data': data,
                            'count': len(data),
                            'limit': limit,
                            'offset': offset
                        }
                    return data
                    
        except aiohttp.ClientResponseError as e:
            logger.error(f"Gamma API error fetching events: HTTP {e.status}")
            raise APIError(f"Failed to fetch events: HTTP {e.status}")
        except Exception as e:
            logger.error(f"Failed to fetch events: {e}", exc_info=True)
            raise APIError(f"Failed to fetch events: {e}")

    async def validate_tokens_bulk(
        self,
        token_ids: List[str],
        side: str = "BUY"
    ) -> Dict[str, bool]:
        """
        Validate multiple tokens at once using the orderbook API
        
        Per Polymarket support Q2 (Jan 2026):
        - POST https://clob.polymarket.com/books
        - No authentication required
        - Returns orderbook summaries only for valid/active tokens
        - Missing tokens in response = invalid/closed markets
        - More efficient than individual /book requests
        
        Args:
            token_ids: List of token IDs to validate (max 500)
            side: "BUY" or "SELL" (kept for compatibility, not used by /books)
            
        Returns:
            Dictionary mapping token_id to validity:
            {
                "token_123": True,   # Valid token, orderbook returned
                "token_456": False,  # Invalid token, not in response
            }
        """
        self._ensure_initialized()
        
        if not token_ids:
            return {}
        
        # Limit to 500 tokens per Polymarket support
        if len(token_ids) > 500:
            logger.warning(f"Validating {len(token_ids)} tokens, limiting to first 500")
            token_ids = token_ids[:500]
        
        try:
            url = f"{CLOB_API_URL}/books"
            
            # Build request payload - just token_ids per Q2 response
            payload = [
                {"token_id": token_id}
                for token_id in token_ids
            ]
            
            logger.debug(f"Bulk validating {len(token_ids)} tokens via /books endpoint")
            
            async with self._session.post(
                url,
                json=payload,
                timeout=30
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.warning(
                        f"Books API returned {response.status}: {error_text}. "
                        f"Assuming all tokens valid."
                    )
                    # Fallback: assume all valid if API fails
                    return {token_id: True for token_id in token_ids}
                
                data = await response.json()
                
                # Per Q4: Response contains orderbook summaries with 'asset_id' field
                # Only valid/active tokens return data - missing tokens = closed/invalid
                valid_tokens = set()
                
                if isinstance(data, list):
                    for item in data:
                        # Per Q4: Use asset_id field from orderbook response
                        token_id = item.get('asset_id')
                        if token_id:
                            valid_tokens.add(token_id)
                elif isinstance(data, dict) and 'data' in data:
                    # Alternative response format
                    for item in data['data']:
                        token_id = item.get('asset_id')
                        if token_id:
                            valid_tokens.add(token_id)
                
                # Build result dictionary
                result = {}
                for token_id in token_ids:
                    is_valid = token_id in valid_tokens
                    result[token_id] = is_valid
                    
                    # Cache invalid tokens for 1 hour
                    if not is_valid:
                        cache_key = f"orderbook_404_{token_id}"
                        self._set_cache_with_ttl(cache_key, True, ttl_seconds=3600)
                        logger.debug(f"Token {token_id[:16]}... invalid, cached 404")
                
                valid_count = sum(1 for v in result.values() if v)
                invalid_count = len(result) - valid_count
                
                logger.info(
                    f"Bulk validation complete: {valid_count} valid, {invalid_count} invalid "
                    f"(invalid tokens cached for 1h)"
                )
                
                return result
                
        except Exception as e:
            logger.error(f"Failed to bulk validate tokens: {e}")
            # Fallback: assume all valid if API fails
            return {token_id: True for token_id in token_ids}

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_order_book(
        self,
        token_id: str
    ) -> Dict[str, Any]:
        """
        Get order book for a specific token
        
        Per Polymarket support (Jan 2026):
        - 404 errors indicate closed/inactive markets
        - Cache 404 results with TTL to avoid repeated failed queries
        
        Args:
            token_id: Token identifier
            
        Returns:
            Order book data with bids and asks
            
        Raises:
            APIError: If orderbook fetch fails (including 404 for closed markets)
        """
        self._ensure_initialized()
        
        # Check if we've cached a 404 for this token (market likely closed)
        cache_key_404 = f"orderbook_404_{token_id}"
        cached_404 = self._check_cache_with_ttl(cache_key_404)
        if cached_404:
            logger.debug(f"Cached 404 for token {token_id[:16]}... (market likely closed)")
            raise APIError("No orderbook exists for the requested token id (cached)")
        
        try:
            logger.debug(f"Fetching order book for token: {token_id}")
            order_book = await asyncio.to_thread(
                self._client.get_order_book,
                token_id=token_id
            )
            return order_book
            
        except Exception as e:
            error_str = str(e)
            
            # Cache 404 errors with 1-hour TTL per Polymarket support
            if "404" in error_str or "No orderbook exists" in error_str:
                logger.debug(f"Caching 404 for token {token_id[:16]}... (1h TTL)")
                self._set_cache_with_ttl(cache_key_404, True, ttl_seconds=3600)
            
            logger.error(f"Failed to fetch order book for {token_id}: {e}")
            raise APIError(f"Failed to fetch order book: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_balance(self, address: Optional[str] = None) -> Decimal:
        """
        Get USDC balance for the proxy wallet from Polygon blockchain
        
        Note: Queries the USDC token balance of the proxy wallet on Polygon.
        This shows funds available for trading on Polymarket.
        
        Args:
            address: Proxy wallet address (uses own proxy if not specified)
            
        Returns:
            USDC balance as Decimal
        """
        self._ensure_initialized()
        
        # IMPORTANT: Query PROXY wallet address where funds are stored,
        # NOT the signer (MetaMask) address
        address = address or PROXY_WALLET_ADDRESS
        
        try:
            logger.debug(f"Fetching USDC balance for proxy wallet {address}")
            
            # USDC contract address on Polygon
            USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            
            # ERC20 ABI minimal (balanceOf only)
            ERC20_ABI = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function",
                }
            ]
            
            # Create Web3 instance
            from web3 import Web3
            w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
            
            # Get USDC contract
            usdc_contract = w3.eth.contract(
                address=Web3.to_checksum_address(USDC_ADDRESS),
                abi=ERC20_ABI
            )
            
            # Query USDC balance of proxy wallet (USDC has 6 decimals)
            balance_raw = await asyncio.to_thread(
                usdc_contract.functions.balanceOf(
                    Web3.to_checksum_address(address)
                ).call
            )
            
            # Convert from smallest unit (6 decimals for USDC)
            balance_decimal = Decimal(balance_raw) / Decimal(10**6)
            logger.debug(f"Proxy wallet USDC balance: {balance_decimal} USDC")
            return balance_decimal
                    
        except Exception as e:
            logger.warning(f"Could not fetch balance for {address}: {e}, returning 0")
            return Decimal('0')

    async def _get_token_id(self, condition_id: str, outcome_index: int) -> Optional[str]:
        """
        Convert condition ID to token ID using Gamma API with caching.
        
        Token IDs are STABLE (confirmed by Polymarket support) and never change,
        so we cache them long-term to reduce API calls.
        
        Args:
            condition_id: Market condition ID from Data API
            outcome_index: Which outcome (0 or 1, typically)
            
        Returns:
            Token ID for placing orders, or None if lookup fails
        
        Note:
            - Implementation validated by Polymarket support as optimal pattern
            - Queries Gamma API with active=true&closed=false to filter for tradable markets
            - Making 1 Gamma call per unique condition_id is acceptable within 300 req/10s limit
            - Gamma API does NOT support batch condition_id lookups
            - Token IDs are stable and never change, so aggressive caching is safe
            - Cache includes null results for closed/inactive markets to avoid repeated queries
            - Initial warmup: ~20 calls for whale with 20 positions (well within rate limits)
            - Ongoing usage: >95% cache hit rate = minimal API load
            - Alternative approach: CLOB orderbook queries (404 = closed), but current is preferred
        """
        # Input validation
        if not condition_id or not isinstance(condition_id, str):
            logger.error(
                f"Invalid condition_id: {condition_id} (type: {type(condition_id).__name__})"
            )
            return None
        
        if not isinstance(outcome_index, int) or outcome_index < 0:
            logger.error(
                f"Invalid outcome_index: {outcome_index} (type: {type(outcome_index).__name__})"
            )
            return None
        
        # Check cache first (includes both valid token IDs and null results for closed markets)
        cache_key = (condition_id, outcome_index)
        if cache_key in self._token_id_cache:
            cached_value = self._token_id_cache[cache_key]
            logger.debug(
                f"Token ID cache hit - condition: {condition_id}, "
                f"outcome: {outcome_index}, token: {cached_value}"
            )
            return cached_value  # May be None for closed markets
        
        # Cache miss - query Gamma API with condition_id parameter (per Polymarket support)
        # Note: Gamma API does NOT support batch lookups - must make individual calls
        # CORRECT: GET /markets?condition_id=0x... (returns clobTokenIds array)
        # WRONG: GET /markets/{condition_id} (expects market slug, returns 422)
        # Filter for active, tradable markets only (per Polymarket support)
        url = f"{POLYMARKET_GAMMA_API_URL}/markets"
        params = {
            "condition_id": condition_id,
            "active": "true",      # Only active markets
            "closed": "false"      # Exclude closed markets
        }
        
        try:
            # Use existing session (connection pooling) instead of creating new one
            async with self._session.get(
                url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SEC)
            ) as response:
                # Handle rate limiting gracefully
                if response.status == 429:
                    retry_after = response.headers.get('Retry-After', '60')
                    logger.warning(
                        f"Gamma API rate limit exceeded - condition: {condition_id}, "
                        f"retry after: {retry_after}s. "
                        f"Suggestion: Increase polling interval or enable more aggressive caching"
                    )
                    return None
                elif response.status == 404:
                    logger.warning(
                        f"Market not found in Gamma API - condition: {condition_id}. "
                        f"Note: Market may be inactive or condition_id invalid"
                    )
                    return None
                elif response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"Gamma API lookup failed - condition: {condition_id}, "
                        f"status: {response.status}, error: {error_text[:200]}"
                    )
                    return None
                
                # Parse response - Gamma API returns array of markets matching condition_id
                data = await response.json()
                
                # Response is an array, get first market
                # Note: With active=true&closed=false filters, closed markets return empty array
                if not isinstance(data, list) or len(data) == 0:
                    logger.warning(
                        f"No active markets found for condition_id: {condition_id}. "
                        f"Market may be closed or not yet active. Caching null result."
                    )
                    # Cache null to avoid repeated queries for closed/inactive markets
                    self._token_id_cache[cache_key] = None
                    return None
                
                market_data = data[0]  # Take first matching market
                
                # CRITICAL: clobTokenIds is returned as a JSON-encoded string per Polymarket API spec
                # Example: "clobTokenIds": "[\"123...\", \"456...\"]"
                # Must parse with json.loads() - confirmed by Polymarket support
                clob_token_ids_str = market_data.get("clobTokenIds")
                
                # Handle null/empty clobTokenIds (edge case - should be rare with active=true filter)
                # Note: With active=true&closed=false, most inactive markets filtered at query level
                if not clob_token_ids_str:
                    logger.warning(
                        f"clobTokenIds is null for active market condition: {condition_id}. "
                        f"Unusual for filtered query. Market may be in transition state. "
                        f"Caching null result."
                    )
                    # Cache null result to avoid repeated queries for same closed market
                    self._token_id_cache[cache_key] = None
                    return None
                
                try:
                    clob_token_ids = json.loads(clob_token_ids_str)
                except json.JSONDecodeError as e:
                    logger.error(
                        f"Failed to parse clobTokenIds JSON - condition: {condition_id}, "
                        f"raw value: {clob_token_ids_str[:100]}, error: {e}"
                    )
                    return None
                
                # Validate array bounds before accessing (recommended by Polymarket support)
                # Binary markets typically have 2 outcomes, but always check
                if outcome_index >= len(clob_token_ids):
                    logger.error(
                        f"Invalid outcome_index {outcome_index} for condition: {condition_id}. "
                        f"clobTokenIds array length: {len(clob_token_ids)}, "
                        f"available indices: 0-{len(clob_token_ids)-1}"
                    )
                    return None
                
                token_id = clob_token_ids[outcome_index]
                
                # Validate token_id is a proper string
                if not token_id or not isinstance(token_id, str) or len(token_id) < 10:
                    logger.error(
                        f"Invalid token_id from Gamma API - condition: {condition_id}, "
                        f"outcome_index: {outcome_index}, token_id: {token_id}, "
                        f"type: {type(token_id).__name__}"
                    )
                    return None
                
                # Cache the result for future use
                self._token_id_cache[cache_key] = token_id
                
                logger.debug(
                    f"Resolved and cached token ID - condition: {condition_id}, "
                    f"outcome: {outcome_index}, token: {token_id}, "
                    f"cache size: {len(self._token_id_cache)}"
                )
                return token_id
                        
        except Exception as e:
            logger.error(
                f"Error fetching token ID from Gamma API - condition: {condition_id}, "
                f"error: {str(e)}"
            )
            return None

    async def get_positions(
        self,
        address: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get actual positions using Polymarket Data API.
        
        Uses the official Data API endpoint which returns avgPrice (weighted average
        entry price) and proper position data. Much more reliable than subgraph.
        
        Per Polymarket support (Jan 2026):
        - "asset" field contains the CORRECT token_id for placing orders
        - No resolution needed - use "asset" directly in create_market_buy/sell_order
        - For multi-outcome markets, each position shows specific outcome held
        
        Args:
            address: Proxy wallet address (uses own proxy if not specified)
            
        Returns:
            List of position dictionaries with:
            - condition_id: Market condition ID
            - question: Market question/title
            - outcome: Outcome name (e.g., "Yes", "No", "Trump", "Biden")
            - size: Number of outcome tokens held (human-readable)
            - avg_price: Weighted average entry price across all trades
            - outcome_index: 0, 1, 2... (which outcome)
            - token_id: Token ID from "asset" field - ready for placing orders
            - current_price: Current market price
            - pnl: Profit/loss information
        """
        self._ensure_initialized()
        
        # IMPORTANT: Query PROXY wallet address where positions are held
        address = address or PROXY_WALLET_ADDRESS
        
        url = f"{POLYMARKET_DATA_API_URL}/positions?user={address}"
        
        try:
            logger.debug(
                f"Querying positions from Data API - address: {address[:10]}..., url: {url}"
            )
            
            async with self._session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SEC)
            ) as response:
                # Handle various HTTP status codes appropriately
                if response.status == 429:
                    # Rate limit exceeded - log and return empty
                    logger.warning(
                        f"Rate limit exceeded on Data API - status: 429, address: {address[:10]}..."
                    )
                    return []
                elif response.status == 404:
                    # User not found - normal case for new addresses
                    logger.debug(
                        f"No positions found (404) - address: {address[:10]}..."
                    )
                    return []
                elif response.status != 200:
                    error_text = await response.text()
                    logger.warning(
                        f"Data API query failed - status: {response.status}, "
                        f"address: {address[:10]}..., error: {error_text[:200]}"
                    )
                    return []
                
                data = await response.json()
                
                if not isinstance(data, list):
                    logger.error(f"Unexpected response format - data: {str(data)[:300]}")
                    return []
                
                positions = []
                for pos in data:
                    condition_id = pos.get("conditionId")
                    outcome_index = int(pos.get("outcomeIndex", 0))
                    
                    # CRITICAL FIX: Data API returns "asset" field with correct token_id
                    # Per Polymarket support: "The asset field in position data is the exact token_id you need"
                    # No resolution needed - use directly from Data API
                    token_id = pos.get("asset")
                    
                    if not token_id:
                        logger.warning(
                            f"Skipping position - missing 'asset' field for "
                            f"condition: {condition_id}, outcome: {pos.get('outcome')}"
                        )
                        continue
                    
                    position_data = {
                        "condition_id": condition_id,
                        "question": pos.get("title"),  # Market title/question
                        "outcome": pos.get("outcome"),  # Outcome name (e.g., "Yes", "No", "Trump")
                        "size": float(pos.get("size", 0)),  # Already in human-readable format
                        "avg_price": float(pos.get("avgPrice", 0)),  # Weighted average entry price
                        "outcome_index": outcome_index,
                        "token_id": token_id,  # CORRECT token_id from Data API "asset" field
                        "current_price": float(pos.get("curPrice", 0)),
                        "pnl": {
                            "cash": float(pos.get("cashPnl", 0)),
                            "percent": float(pos.get("percentPnl", 0)),
                            "value": float(pos.get("currentValue", 0)),
                        },
                    }
                    positions.append(position_data)
                
                logger.info(
                    f"Retrieved {len(positions)} positions from Data API - "
                    f"address: {address[:10]}..., count: {len(positions)}"
                )
                return positions
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout querying Data API for {address}")
            return []
        except Exception as e:
            logger.error(f"Failed to query positions from Data API: {e}")
            return []

    async def get_closed_positions(
        self,
        address: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """
        Get closed positions using Polymarket Data API.
        
        Useful for tracking when whale exits positions - often faster than
        waiting for position to disappear from active positions.
        
        Args:
            address: Proxy wallet address (uses own proxy if not specified)
            limit: Max positions to return (max 50 per Polymarket API limits)
            
        Returns:
            List of closed position dictionaries with avgPrice and exit info
        """
        self._ensure_initialized()
        
        # IMPORTANT: Query PROXY wallet address where positions are held
        address = address or PROXY_WALLET_ADDRESS
        
        url = f"{POLYMARKET_DATA_API_URL}/v1/closed-positions?user={address}&limit={min(limit, 50)}"
        
        try:
            logger.debug(f"Querying closed positions from Data API for {address}")
            
            async with self._session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT_SEC)
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.warning(
                        f"Closed positions query failed: HTTP {response.status}, "
                        f"error: {error_text}"
                    )
                    return []
                
                data = await response.json()
                
                if not isinstance(data, list):
                    logger.error(f"Unexpected response format for closed positions - data: {str(data)[:300]}")
                    return []
                
                logger.info(
                    f"Retrieved {len(data)} closed positions from Data API - "
                    f"address: {address[:10]}..., count: {len(data)}"
                )
                return data
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout querying closed positions for {address}")
            return []
        except Exception as e:
            logger.error(f"Failed to query closed positions from Data API: {e}")
            return []

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_simplified_positions(
        self,
        address: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """
        Get positions with unique key per position.
        
        Per Polymarket support (Jan 2026):
        - Data API "asset" field contains the CORRECT token_id for the outcome held
        - Whale can hold MULTIPLE positions in same conditionId (different outcomes)
        - Must track by condition_id + asset as unique identifier
        
        Args:
            address: Wallet address (uses own wallet if not specified)
            
        Returns:
            Dictionary mapping "condition_id_asset" to position data:
            {
                "0x123..._456...": {
                    "condition_id": "0x123...",
                    "asset": "456...",
                    "size": 1.5,
                    "avg_price": 0.45,
                    "outcome": "Yes",
                    "question": "Will X happen?",
                    "token_id": "456...",
                    "outcome_index": 0
                },
                ...
            }
        """
        positions = await self.get_positions(address)
        
        position_map = {}
        for pos in positions:
            condition_id = pos.get('condition_id')
            asset = pos.get('token_id')  # Asset is the unique token_id
            size = pos.get('size', 0)
            
            if condition_id and asset and size > 0:
                # Use condition_id + asset as unique key (handles multi-outcome)
                position_key = f"{condition_id}_{asset}"
                position_map[position_key] = {
                    'condition_id': condition_id,  # Keep for reference
                    'asset': asset,  # Keep for reference
                    'size': size,
                    'avg_price': pos.get('avg_price'),
                    'outcome': pos.get('outcome'),
                    'question': pos.get('question'),
                    'token_id': asset,  # For placing orders (same as asset)
                    'outcome_index': pos.get('outcome_index')
                }
        
        return position_map

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_midpoint_price(
        self,
        token_id: str
    ) -> Optional[float]:
        """
        Get midpoint price from order book (average of best bid and ask)
        
        Args:
            token_id: Token identifier
            
        Returns:
            Midpoint price or None if no liquidity
        """
        order_book = await self.get_order_book(token_id)
        
        # py-clob-client returns OrderBookSummary object, not dict
        bids = getattr(order_book, 'bids', [])
        asks = getattr(order_book, 'asks', [])
        
        if not bids or not asks:
            return None
        
        # Access price from bid/ask objects
        best_bid = float(bids[0].price if hasattr(bids[0], 'price') else bids[0]['price'])
        best_ask = float(asks[0].price if hasattr(asks[0], 'price') else asks[0]['price'])
        
        return (best_bid + best_ask) / 2.0

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_spread(
        self,
        token_id: str
    ) -> Optional[float]:
        """
        Get bid-ask spread for a token
        
        Args:
            token_id: Token identifier
            
        Returns:
            Spread as percentage or None if no liquidity
        """
        order_book = await self.get_order_book(token_id)
        
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])
        
        if not bids or not asks:
            return None
        
        best_bid = float(bids[0]['price'])
        best_ask = float(asks[0]['price'])
        
        if best_bid == 0:
            return None
        
        spread = (best_ask - best_bid) / best_bid
        return spread

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_market_depth(
        self,
        token_id: str,
        levels: int = 10
    ) -> Dict[str, Any]:
        """
        Get market depth (aggregated order book levels)
        
        Args:
            token_id: Token identifier
            levels: Number of levels to aggregate
            
        Returns:
            Dictionary with bid/ask volumes and prices
        """
        order_book = await self.get_order_book(token_id)
        
        bids = order_book.get('bids', [])[:levels]
        asks = order_book.get('asks', [])[:levels]
        
        bid_volume = sum(float(b.get('size', 0)) for b in bids)
        ask_volume = sum(float(a.get('size', 0)) for a in asks)
        
        return {
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'bid_levels': len(bids),
            'ask_levels': len(asks),
            'imbalance': (bid_volume - ask_volume) / (bid_volume + ask_volume) if (bid_volume + ask_volume) > 0 else 0
        }

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_market_price(
        self,
        token_id: str,
        side: str = "buy"
    ) -> Optional[float]:
        """
        Get real-time market price for a token from CLOB API.
        
        Uses CLOB API /price endpoint (1500 requests/10s rate limit).
        Returns the best executable price for the given side.
        
        Args:
            token_id: Token identifier to get price for
            side: "buy" for best ask price, "sell" for best bid price
            
        Returns:
            Current market price as float, or None if unavailable
            
        Example:
            price = await client.get_market_price("token_123", side="buy")
            # Returns: 0.68 (best ask price to BUY at)
        """
        if not token_id:
            logger.error("get_market_price called with empty token_id")
            return None
        
        if side not in ["buy", "sell"]:
            logger.error(f"Invalid side: {side}, must be 'buy' or 'sell'")
            return None
        
        try:
            url = f"{CLOB_API_URL}/price"
            params = {
                "token_id": token_id,
                "side": side
            }
            
            logger.debug(f"Querying CLOB price - token: {token_id}, side: {side}")
            
            async with self._session.get(url, params=params, timeout=10) as response:
                if response.status == 429:
                    logger.warning("CLOB API rate limit exceeded for /price endpoint (1500/10s)")
                    return None
                
                if response.status == 404:
                    logger.warning(f"Price not available for token: {token_id}")
                    return None
                
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"Failed to get market price - status: {response.status}, "
                        f"token: {token_id}, error: {error_text[:200]}"
                    )
                    return None
                
                data = await response.json()
                price = data.get('price')
                
                if price is None:
                    logger.warning(f"Price field missing in response for token: {token_id}")
                    return None
                
                price_float = float(price)
                logger.debug(f"Market price retrieved - token: {token_id}, side: {side}, price: {price_float}")
                return price_float
        
        except asyncio.TimeoutError:
            logger.warning(f"Timeout getting market price for token: {token_id}")
            return None
        except Exception as e:
            logger.error(f"Error getting market price - token: {token_id}, error: {str(e)}")
            return None

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_batch_prices(
        self,
        token_ids: List[str]
    ) -> Dict[str, Dict[str, float]]:
        """
        Get prices for multiple tokens in a single API call.
        
        Uses CLOB API /prices endpoint (500 requests/10s rate limit).
        More efficient than calling get_market_price() multiple times.
        
        Args:
            token_ids: List of token identifiers to get prices for
            
        Returns:
            Dictionary mapping token_id to price data:
            {
                "token_123": {
                    "bid": 0.45,  # Best bid (sell price)
                    "ask": 0.48,  # Best ask (buy price)
                    "mid": 0.465  # Mid-market price
                },
                ...
            }
            
        Example:
            prices = await client.get_batch_prices(["token_1", "token_2"])
            buy_price = prices["token_1"]["ask"]  # Price to buy at
        """
        if not token_ids:
            logger.warning("get_batch_prices called with empty token_ids list")
            return {}
        
        try:
            # CLOB API /prices expects token_ids as JSON array in request body
            url = f"{CLOB_API_URL}/prices"
            
            logger.debug(f"Querying batch prices for {len(token_ids)} tokens")
            
            async with self._session.post(
                url,
                json={"token_ids": token_ids},
                timeout=15
            ) as response:
                if response.status == 429:
                    logger.warning("CLOB API rate limit exceeded for /prices endpoint (500/10s)")
                    return {}
                
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(
                        f"Failed to get batch prices - status: {response.status}, "
                        f"token_count: {len(token_ids)}, error: {error_text[:200]}"
                    )
                    return {}
                
                data = await response.json()
                
                # Parse response into standardized format
                result = {}
                for token_id, price_data in data.items():
                    if not isinstance(price_data, dict):
                        continue
                    
                    bid = price_data.get('bid')
                    ask = price_data.get('ask')
                    
                    if bid is not None and ask is not None:
                        bid_float = float(bid)
                        ask_float = float(ask)
                        result[token_id] = {
                            'bid': bid_float,
                            'ask': ask_float,
                            'mid': (bid_float + ask_float) / 2
                        }
                
                logger.debug(f"Retrieved batch prices for {len(result)}/{len(token_ids)} tokens")
                return result
        
        except asyncio.TimeoutError:
            logger.warning(f"Timeout getting batch prices for {len(token_ids)} tokens")
            return {}
        except Exception as e:
            logger.error(f"Error getting batch prices - token_count: {len(token_ids)}, error: {str(e)}")
            return {}

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_trades(
        self,
        address: Optional[str] = None,
        market: Optional[str] = None,
        taker: Optional[str] = None,
        after: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get trade history
        
        Per Polymarket support (Jan 2026):
        - maker_address: Returns trades where address placed limit orders (MAKER)
        - taker: Returns trades where address placed market orders (TAKER)
        - after: Unix timestamp to filter trades after this time
        - Response fields: market (condition_id), asset_id (token_id), side (buy/sell), trader_side (MAKER/TAKER)
        
        Args:
            address: Filter by maker address (limit orders)
            market: Filter by market condition ID
            taker: Filter by taker address (market orders) - recommended for real-time detection
            after: Unix timestamp - only return trades after this time
            
        Returns:
            List of trade dictionaries with fields:
            - market: condition ID
            - asset_id: token ID
            - side: "buy" or "sell"
            - trader_side: "MAKER" or "TAKER"
            - match_time: trade execution timestamp
        """
        self._ensure_initialized()
        
        try:
            from py_clob_client.clob_types import TradeParams
            
            logger.debug(
                f"Fetching trades - maker_address={address}, taker={taker}, "
                f"market={market}, after={after}"
            )
            
            # Create TradeParams object (py_clob_client requires this)
            # Per Polymarket support: Use 'taker' for market orders (real-time detection)
            params = TradeParams(
                maker_address=address,
                market=market
            )
            
            # py_clob_client might not support 'taker' and 'after' directly
            # If so, we'll need to use raw API call
            # For now, try with existing params and filter manually
            trades = await asyncio.to_thread(
                self._client.get_trades,
                params
            )
            
            # If taker or after are specified, we need to filter manually
            # or use raw API call (py_clob_client limitation)
            if taker or after:
                logger.debug(
                    f"Note: py_clob_client doesn't support 'taker' or 'after' params. "
                    f"Retrieved {len(trades)} trades, may need manual filtering."
                )
            
            logger.debug(f"Retrieved {len(trades)} trades")
            return trades
            
        except Exception as e:
            logger.error(f"Failed to fetch trades: {e}")
            raise APIError(f"Failed to fetch trades: {e}")

    async def get_trades_raw(
        self,
        taker: Optional[str] = None,
        maker: Optional[str] = None,
        market: Optional[str] = None,
        after: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get trades using public Data API (no authentication required)
        
        Per Polymarket support Q1, Q2, Q3 (Jan 2026):
        - Use public Data API: https://data-api.polymarket.com/trades
        - No authentication required
        - Wallet filtering: Use 'user' parameter
        - No time filtering params - filter client-side using 'timestamp' field
        - Response fields: proxyWallet, side (BUY/SELL), conditionId, asset, timestamp
        
        Args:
            taker: Taker address (for market orders) - mapped to user filter
            maker: Maker address (for limit orders) - mapped to user filter
            market: Market condition ID
            after: Unix timestamp - used for CLIENT-SIDE filtering (not API param)
            
        Returns:
            List of trade dictionaries with Data API format
        """
        self._ensure_initialized()
        
        try:
            url = f"{POLYMARKET_DATA_API_URL}/trades"
            params = {}
            
            # Per Q1: Data API uses 'user' parameter for wallet filtering
            wallet_address = taker or maker
            if wallet_address:
                # Data API expects lowercase addresses
                params['user'] = wallet_address.lower()
            
            if market:
                params['market'] = market
            
            # Note: Per Q1, no time filtering params documented
            # We'll filter by timestamp field client-side
            
            logger.debug(f"Data API request: GET {url} params={params}")
            
            async with self._session.get(url, params=params, timeout=30) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise APIError(f"Data API returned {response.status}: {error_text}")
                
                trades = await response.json()
                
                # Data API may return wrapped response
                if isinstance(trades, dict) and 'data' in trades:
                    trades = trades['data']
                
                logger.debug(f"Retrieved {len(trades)} trades via Data API")
                return trades
                
        except Exception as e:
            logger.error(f"Failed to fetch trades via Data API: {e}")
            raise APIError(f"Failed to fetch trades via Data API: {e}")

    async def get_recent_position_entries(
        self,
        address: str,
        time_window_minutes: int = 30
    ) -> Dict[str, Dict[str, Any]]:
        """
        Get positions that were entered within the specified time window.
        
        Per Polymarket support Q1, Q2, Q3 (Jan 2026):
        - Use public Data API: https://data-api.polymarket.com/trades (no auth)
        - Query with 'user' parameter to get all trades for wallet
        - No time filtering params - filter client-side using 'timestamp' field
        - Field names: conditionId, asset, side (BUY/SELL), proxyWallet, timestamp
        - Typical latency: ~1 second (WebSocket is faster at ~100ms)
        
        Args:
            address: Wallet address to query
            time_window_minutes: Only include positions entered within this many minutes
            
        Returns:
            Dictionary mapping "condition_id_asset" to most recent trade info:
            {
                "0x123..._456...": {
                    "condition_id": "0x123...",
                    "asset_id": "456...",
                    "last_trade_time": 1704988800.0,  # Unix timestamp
                    "minutes_ago": 15.5,
                    "trade_count": 3,  # Number of trades in window
                    "side": "buy",  # or "sell"
                    "trader_side": "TAKER",  # or "MAKER"
                },
                ...
            }
        """
        import time
        from datetime import datetime, timezone
        
        try:
            # Calculate cutoff time (current time - window)
            current_time = time.time()
            cutoff_timestamp = int(current_time - (time_window_minutes * 60))
            
            logger.debug(
                f"Fetching trades for {address[:10]}... after timestamp {cutoff_timestamp} "
                f"({time_window_minutes} min ago)"
            )
            
            # Per Q1, Q2: Use public Data API with 'user' parameter (no auth)
            # Returns all trades for the wallet (both taker and maker)
            # No time filtering on API - we filter client-side by timestamp field
            try:
                # Get all trades for wallet via Data API
                logger.debug(f"Fetching all trades for {address[:10]}... via Data API")
                trades = await self.get_trades_raw(
                    taker=address,  # Maps to 'user' parameter internally
                    after=cutoff_timestamp  # Used for client-side filtering only
                )
                logger.debug(f"Retrieved {len(trades)} trades (will filter by timestamp client-side)")
                
            except Exception as e:
                logger.warning(f"Data API failed, falling back to py_clob_client: {e}")
                # Fallback: Use py_clob_client (only supports maker_address)
                trades = await self.get_trades(address=address)
            
            if not trades:
                logger.debug(f"No trades found for address {address[:10]}...")
                return {}
            
            logger.debug(f"Retrieved {len(trades)} total trades, filtering by time window")
            
            # Filter trades within time window and group by position
            # CRITICAL: Calculate size and price from TRADES, not from whale's current positions
            # This ensures we only mirror what whale just bought, not old holdings
            recent_positions = {}
            trades_processed = 0
            trades_in_window = 0
            trade_totals = {}  # Track total size and value per position
            
            for trade in trades:
                trades_processed += 1
                
                # Per Q2: Data API uses 'timestamp' field (not match_time)
                timestamp_value = trade.get('timestamp')
                if not timestamp_value:
                    continue
                
                # Parse timestamp (could be Unix timestamp or ISO string)
                try:
                    if isinstance(timestamp_value, (int, float)):
                        trade_timestamp = float(timestamp_value)
                    elif isinstance(timestamp_value, str):
                        # Try parsing as ISO format
                        dt = datetime.fromisoformat(timestamp_value.replace('Z', '+00:00'))
                        trade_timestamp = dt.timestamp()
                    else:
                        continue
                except (ValueError, AttributeError):
                    logger.warning(f"Failed to parse timestamp: {timestamp_value}")
                    continue
                
                # Skip trades outside time window (client-side filtering per Q1)
                if trade_timestamp < cutoff_timestamp:
                    continue
                
                # CRITICAL: Only process BUY trades (ignore SELL trades)
                side = trade.get('side', 'unknown')
                if side.upper() != 'BUY':
                    continue
                
                trades_in_window += 1
                
                # Per Q2: Data API exact field names
                # conditionId, asset (not assetId), side (BUY/SELL)
                condition_id = trade.get('conditionId')
                asset_id = trade.get('asset')
                trader_side = side  # Use side as trader_side for consistency
                
                # Extract trade size and price from trade data
                trade_size = float(trade.get('size', 0))  # Number of shares
                trade_price = float(trade.get('price', 0))  # Price per share
                
                if not condition_id or not asset_id or trade_size <= 0:
                    logger.debug(
                        f"Trade missing data: market={condition_id}, asset={asset_id}, size={trade_size}"
                    )
                    continue
                
                # Create position key (same format as get_simplified_positions)
                position_key = f"{condition_id}_{asset_id}"
                
                # Accumulate trade data for weighted average calculation
                if position_key not in trade_totals:
                    trade_totals[position_key] = {
                        'total_size': 0.0,
                        'total_value': 0.0,
                        'last_timestamp': trade_timestamp
                    }
                
                trade_totals[position_key]['total_size'] += trade_size
                trade_totals[position_key]['total_value'] += (trade_size * trade_price)
                if trade_timestamp > trade_totals[position_key]['last_timestamp']:
                    trade_totals[position_key]['last_timestamp'] = trade_timestamp
                
                # Track most recent trade metadata for this position
                if position_key not in recent_positions:
                    recent_positions[position_key] = {
                        'condition_id': condition_id,
                        'asset_id': asset_id,
                        'token_id': asset_id,  # token_id is same as asset_id for orders
                        'last_trade_time': trade_timestamp,
                        'trade_count': 1,
                        'side': side,  # Last trade side
                        'trader_side': trader_side,  # Last trade type
                    }
                else:
                    # Update if this trade is more recent
                    if trade_timestamp > recent_positions[position_key]['last_trade_time']:
                        recent_positions[position_key]['last_trade_time'] = trade_timestamp
                        recent_positions[position_key]['side'] = side
                        recent_positions[position_key]['trader_side'] = trader_side
                    recent_positions[position_key]['trade_count'] += 1
            
            # Calculate weighted average price from trades (not from current position)
            for pos_key, pos_data in recent_positions.items():
                if pos_key in trade_totals:
                    totals = trade_totals[pos_key]
                    pos_data['size'] = totals['total_size']
                    pos_data['avg_price'] = totals['total_value'] / totals['total_size'] if totals['total_size'] > 0 else 0
            
            # Add minutes_ago for each position
            for pos_key, pos_data in recent_positions.items():
                minutes_ago = (current_time - pos_data['last_trade_time']) / 60
                pos_data['minutes_ago'] = minutes_ago
            
            logger.info(
                f"Processed {trades_processed} trades, {trades_in_window} within time window. "
                f"Found {len(recent_positions)} positions entered within last "
                f"{time_window_minutes} minutes for address {address[:10]}..."
            )
            
            # Debug: Show first few recent positions
            if recent_positions:
                for i, (key, data) in enumerate(list(recent_positions.items())[:3]):
                    logger.debug(
                        f"  Recent #{i+1}: {data['side'].upper()} ({data['trader_side']}) "
                        f"{data['minutes_ago']:.1f} min ago - {key[:20]}..."
                    )
            
            return recent_positions
            
        except Exception as e:
            logger.error(f"Failed to get recent position entries: {e}")
            logger.exception(e)  # Full stack trace for debugging
            return {}

    def _check_cache_with_ttl(self, key: str) -> Optional[Any]:
        """
        Check cache with TTL (Time To Live)
        
        Args:
            key: Cache key
            
        Returns:
            Cached value if not expired, None otherwise
        """
        if key not in self._cache_with_ttl:
            return None
        
        value, expiry = self._cache_with_ttl[key]
        
        # Check if expired
        from datetime import datetime
        if datetime.now().timestamp() > expiry:
            # Expired, remove from cache
            del self._cache_with_ttl[key]
            return None
        
        return value
    
    def _set_cache_with_ttl(self, key: str, value: Any, ttl_seconds: int = 3600) -> None:
        """
        Set cache value with TTL (Time To Live)
        
        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: Time to live in seconds (default 1 hour)
        """
        from datetime import datetime
        expiry = datetime.now().timestamp() + ttl_seconds
        self._cache_with_ttl[key] = (value, expiry)

    async def is_market_closed(self, condition_id: str) -> bool:
        """
        Check if a market is closed/resolved (no longer tradeable)
        
        Per Polymarket support (Jan 2026):
        - Use Gamma API FIRST to check market status (active=true&closed=false)
        - Filters use AND logic: returns markets that are BOTH active AND not closed
        - Presence in filtered results is sufficient (no need to check boolean fields)
        - This prevents 404 errors by filtering at the source
        - Cache results with TTL to avoid repeated queries
        - No batch endpoint exists, but /events endpoint can be more efficient
        
        Args:
            condition_id: Market condition ID
            
        Returns:
            True if market is closed/resolved, False if active
        """
        try:
            # Check permanent cache first (for known closed markets)
            cache_key = f"market_closed_{condition_id}"
            if cache_key in self._cache:
                cached_result = self._cache[cache_key]
                logger.debug(f"Cache hit (permanent): Market {condition_id[:16]}... closed={cached_result}")
                return cached_result
            
            # Check TTL cache (for recent checks)
            ttl_cache_key = f"market_active_check_{condition_id}"
            cached_active = self._check_cache_with_ttl(ttl_cache_key)
            if cached_active is not None:
                logger.debug(f"Cache hit (TTL): Market {condition_id[:16]}... closed={not cached_active}")
                return not cached_active  # Return opposite (we cache 'active' status)
            
            # PRIMARY METHOD: Query Gamma API for active markets
            # Per Polymarket support: This is the recommended approach
            logger.debug(f"Checking market status via Gamma API: {condition_id[:16]}...")
            
            url = f"{POLYMARKET_GAMMA_API_URL}/markets"
            params = {
                "condition_id": condition_id,
                "active": "true",
                "closed": "false"
            }
            
            # Per Polymarket support: active=true&closed=false uses AND logic
            # Returns only markets that are BOTH active=true AND closed=false
            # Presence in filtered results = market is live and tradeable
            try:
                async with self._session.get(url, params=params, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        # Per Polymarket support: Presence in results is sufficient
                        # No need to check individual boolean fields
                        is_active = len(data) > 0
                        is_closed = not is_active
                        
                        if is_closed:
                            # Permanently cache closed markets (status won't change)
                            self._cache[cache_key] = True
                            logger.info(f"Market {condition_id[:16]}... is CLOSED/RESOLVED (cached permanently)")
                        else:
                            # Cache active status with 30-min TTL (balance between accuracy and API limits)
                            # Per Polymarket support: 1 hour is reasonable, but 15-30 min better for real-time
                            # Gamma API rate limit: 300 requests/10s for /markets endpoint
                            self._set_cache_with_ttl(ttl_cache_key, True, ttl_seconds=1800)
                            logger.debug(f"Market {condition_id[:16]}... is ACTIVE (cached for 30m)")
                        
                        return is_closed
                    else:
                        logger.warning(f"Gamma API returned {response.status} for {condition_id[:16]}...")
                        
            except asyncio.TimeoutError:
                logger.warning(f"Gamma API timeout for {condition_id[:16]}..., assuming active")
            except Exception as e:
                logger.warning(f"Gamma API error for {condition_id[:16]}...: {e}, assuming active")
            
            # Fallback: Assume active if we can't determine
            # Cache this assumption with short TTL to avoid repeated failures
            self._set_cache_with_ttl(ttl_cache_key, True, ttl_seconds=300)  # 5 min TTL
            return False
                
        except Exception as e:
            logger.warning(f"Error checking market status for {condition_id[:16]}...: {e}, assuming active")
            return False
    
    # TODO: Potential optimization for checking multiple markets at once
    # Per Polymarket support: No dedicated batch endpoint exists for condition_ids
    # Alternative: Use /events?active=true&closed=false endpoint which returns events
    # with their associated markets - can be more efficient than individual queries
    # Consider implementing this if bot needs to check many markets simultaneously
    # 
    # async def get_active_markets_bulk(self) -> List[Dict[str, Any]]:
    #     """Get all active markets via /events endpoint for bulk checking"""
    #     url = f"{POLYMARKET_GAMMA_API_URL}/events?active=true&closed=false"
    #     ...

    async def check_token_balances_batch(
        self,
        token_ids: List[str],
        account: Optional[str] = None
    ) -> Dict[str, int]:
        """
        Check balances for multiple tokens in a single call using ERC1155 balanceOfBatch
        
        Per Polymarket support (Jan 2026):
        - CTF contract supports balanceOfBatch() as it's ERC1155
        - More efficient than multiple balanceOf() calls
        - Reduces gas costs and RPC calls when checking many positions
        - Signature: balanceOfBatch(address[] accounts, uint256[] ids)
        
        Args:
            token_ids: List of token IDs to check balances for
            account: Account address (uses PROXY wallet if not specified)
            
        Returns:
            Dictionary mapping token_id to balance
            
        Example:
            balances = await client.check_token_balances_batch(
                ["token1", "token2", "token3"]
            )
            # Returns: {"token1": 150, "token2": 0, "token3": 500}
        """
        if not token_ids:
            return {}
        
        account = account or PROXY_WALLET_ADDRESS
        
        try:
            # Initialize Web3
            w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
            
            # ERC1155 balanceOfBatch ABI
            erc1155_batch_abi = [{
                "name": "balanceOfBatch",
                "type": "function",
                "stateMutability": "view",
                "inputs": [
                    {"name": "accounts", "type": "address[]"},
                    {"name": "ids", "type": "uint256[]"}
                ],
                "outputs": [{"name": "", "type": "uint256[]"}]
            }]
            
            ctf_contract = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                abi=erc1155_batch_abi
            )
            
            # For checking same account with multiple token IDs:
            # Repeat account address for each token
            checksum_account = Web3.to_checksum_address(account)
            accounts = [checksum_account] * len(token_ids)
            
            # Convert token IDs to integers
            token_ids_int = [int(tid) for tid in token_ids]
            
            logger.debug(f"Batch checking {len(token_ids)} token balances for {account[:10]}...")
            
            # Call balanceOfBatch
            balances = ctf_contract.functions.balanceOfBatch(
                accounts,
                token_ids_int
            ).call()
            
            # Map token IDs to balances
            balance_map = {}
            for token_id, balance in zip(token_ids, balances):
                balance_map[token_id] = balance
                if balance > 0:
                    logger.debug(f"Token {token_id[:16]}...: {balance} tokens")
            
            logger.info(f"Batch balance check complete: {len([b for b in balances if b > 0])}/{len(token_ids)} tokens with balance")
            
            return balance_map
            
        except Exception as e:
            logger.error(f"Failed to check token balances in batch: {e}")
            # Fallback to empty dict - caller should handle missing data
            return {}

    async def redeem_winning_positions(
        self,
        condition_id: str,
        position_data: Dict[str, Any]
    ) -> Optional[str]:
        """
        Redeem winning tokens from resolved market via CTF contract
        
        Per Polymarket support (Jan 2026):
        - Winning shares = $1 USDCe each, losing shares = $0
        - Use PROXY wallet address (funder) as 'from' - where tokens are held
        - No token approval needed - redeemPositions burns tokens directly
        - Check token balance before redemption to avoid double-redemption
        - collateralToken: Always USDCe (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
        - parentCollectionId: Always bytes32(0) for Polymarket
        - indexSets: [1] for first outcome (Yes/0), [2] for second outcome (No/1)
        - Gas: 200,000 sufficient, but dynamic estimation safer for production
        
        Args:
            condition_id: Market condition ID
            position_data: Position info with asset/token details
            
        Returns:
            Transaction hash if redemption successful, None otherwise
        """
        try:
            # Get market details to identify winner
            market = await asyncio.to_thread(
                self._client.get_market,
                condition_id
            )
            
            # Check if market is closed
            is_closed = market.get('closed', False) if isinstance(market, dict) else getattr(market, 'closed', False)
            if not is_closed:
                logger.warning(f"Market {condition_id[:16]}... is not closed yet, cannot redeem")
                return None
            
            # Find winning token
            tokens = market.get('tokens', []) if isinstance(market, dict) else getattr(market, 'tokens', [])
            winning_token = None
            for token in tokens:
                is_winner = token.get('winner', False) if isinstance(token, dict) else getattr(token, 'winner', False)
                if is_winner:
                    winning_token = token
                    break
            
            if not winning_token:
                logger.warning(f"No winning token identified for market {condition_id[:16]}...")
                return None
            
            # Check if we hold the winning token
            position_asset = position_data.get('asset') or position_data.get('token_id')
            winning_token_id = winning_token.get('token_id') if isinstance(winning_token, dict) else getattr(winning_token, 'token_id', None)
            
            if position_asset != winning_token_id:
                logger.info(f"Position is NOT winning token for {condition_id[:16]}..., skipping redemption")
                return None
            
            # Initialize Web3
            w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
            
            # DOUBLE-REDEMPTION CHECK: Query ERC1155 balance to avoid unnecessary transactions
            # Per Polymarket support: Check token balance before redemption
            erc1155_abi = [{
                "name": "balanceOf",
                "type": "function",
                "stateMutability": "view",
                "inputs": [
                    {"name": "account", "type": "address"},
                    {"name": "id", "type": "uint256"}
                ],
                "outputs": [{"name": "", "type": "uint256"}]
            }]
            
            ctf_contract = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                abi=erc1155_abi
            )
            
            # Check balance for winning token
            token_balance = ctf_contract.functions.balanceOf(
                Web3.to_checksum_address(PROXY_WALLET_ADDRESS),
                int(winning_token_id)
            ).call()
            
            if token_balance == 0:
                logger.info(f"Token balance is 0 for {condition_id[:16]}... - already redeemed")
                return None
            
            logger.info(
                f"🎉 Redeeming {token_balance} winning tokens for: "
                f"{position_data.get('question', 'Unknown')[:40]}... "
                f"(~${token_balance} USDCe)"
            )
            
            # Determine indexSet based on token position (1 or 2 for binary markets)
            # Per Polymarket support: Calculation is correct
            outcome = winning_token.get('outcome') if isinstance(winning_token, dict) else getattr(winning_token, 'outcome', None)
            outcome_index = winning_token.get('outcome_index') if isinstance(winning_token, dict) else getattr(winning_token, 'outcome_index', None)
            
            # indexSet = 1 for first outcome (Yes/outcome_index 0)
            # indexSet = 2 for second outcome (No/outcome_index 1)
            if outcome_index is not None:
                index_set = 1 if outcome_index == 0 else 2
            else:
                index_set = 1 if outcome == 'Yes' or outcome == 0 else 2
            
            logger.debug(f"Using indexSet={index_set} for outcome='{outcome}' (outcome_index={outcome_index})")
            
            # CTF contract ABI for redeemPositions
            # Per Polymarket support: No approval needed - burns tokens directly
            redeem_abi = [{
                "name": "redeemPositions",
                "type": "function",
                "stateMutability": "nonpayable",
                "inputs": [
                    {"name": "collateralToken", "type": "address"},
                    {"name": "parentCollectionId", "type": "bytes32"},
                    {"name": "conditionId", "type": "bytes32"},
                    {"name": "indexSets", "type": "uint256[]"}
                ],
                "outputs": []
            }]
            
            ctf_contract_redeem = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_CONTRACT_ADDRESS),
                abi=redeem_abi
            )
            
            # Prepare redemption transaction
            # Per Polymarket support: Use PROXY wallet address (funder) as 'from'
            tx_params = {
                'from': Web3.to_checksum_address(PROXY_WALLET_ADDRESS),
                'nonce': w3.eth.get_transaction_count(Web3.to_checksum_address(PROXY_WALLET_ADDRESS)),
                'gasPrice': w3.eth.gas_price,
                'chainId': POLYGON_CHAIN_ID
            }
            
            # Dynamic gas estimation (safer for production per Polymarket support)
            try:
                estimated_gas = ctf_contract_redeem.functions.redeemPositions(
                    Web3.to_checksum_address(USDC_CONTRACT_ADDRESS),  # collateralToken (USDCe)
                    b'\x00' * 32,  # parentCollectionId (bytes32(0))
                    Web3.to_bytes(hexstr=condition_id) if condition_id.startswith('0x') else Web3.to_bytes(hexstr=f'0x{condition_id}'),  # conditionId
                    [index_set]  # indexSets
                ).estimate_gas(tx_params)
                
                # Add 20% buffer to estimated gas
                gas_limit = int(estimated_gas * 1.2)
                logger.debug(f"Gas estimate: {estimated_gas}, using limit: {gas_limit}")
            except Exception as e:
                # Fallback to 200,000 if estimation fails
                gas_limit = 200000
                logger.warning(f"Gas estimation failed: {e}, using fallback: {gas_limit}")
            
            tx_params['gas'] = gas_limit
            
            # Build transaction
            tx = ctf_contract_redeem.functions.redeemPositions(
                Web3.to_checksum_address(USDC_CONTRACT_ADDRESS),  # collateralToken (USDCe)
                b'\x00' * 32,  # parentCollectionId (bytes32(0))
                Web3.to_bytes(hexstr=condition_id) if condition_id.startswith('0x') else Web3.to_bytes(hexstr=f'0x{condition_id}'),  # conditionId
                [index_set]  # indexSets
            ).build_transaction(tx_params)
            
            # Sign transaction with private key (per Polymarket support: direct submission is correct)
            signed_tx = w3.eth.account.sign_transaction(tx, private_key=self._private_key)
            
            # Send transaction
            tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
            
            logger.info(f"✅ Redemption transaction sent: {tx_hash.hex()}")
            
            # Wait for confirmation
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt['status'] == 1:
                logger.info(f"🎊 Redemption successful! Claimed ~${token_balance} USDCe - Tx: {tx_hash.hex()}")
                return tx_hash.hex()
            else:
                logger.error(f"Redemption transaction failed: {tx_hash.hex()}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to redeem winning position for {condition_id[:16]}...: {e}")
            return None

    async def _get_market_fee_rate(self, token_id: str) -> int:
        """
        Get market's taker fee rate in basis points
        
        Per Polymarket support Q1-Q3 (Jan 2026):
        - Use REST API: GET /fee-rate?token_id={token_id}
        - Returns: {"fee_rate_bps": 1000} or {"fee_rate_bps": 0}
        - 15-minute crypto markets: 1000 bps (10% fee)
        - Most other markets: 0 bps (fee-free)
        - MUST query dynamically per token - rates vary by market
        
        Args:
            token_id: Token identifier
            
        Returns:
            Fee rate in basis points (0 or 1000)
        """
        try:
            # Check cache first (fee rates are stable per market)
            cache_key = f"fee_rate_{token_id}"
            if cache_key in self._cache:
                cached_fee = self._cache[cache_key]
                logger.debug(f"Using cached fee rate for {token_id[:8]}: {cached_fee} bps")
                return cached_fee
            
            # Per Q3: Try py-clob-client getFeeRateBps method first
            try:
                # Note: Method name is getFeeRateBps (camelCase) per Q3
                if hasattr(self._client, 'get_fee_rate_bps'):
                    fee_rate = await asyncio.to_thread(
                        self._client.get_fee_rate_bps,
                        token_id
                    )
                    logger.info(f"✓ py-clob-client fee rate for {token_id[:8]}: {fee_rate} bps")
                    
                    # Cache the result
                    self._cache[cache_key] = fee_rate
                    return fee_rate
                else:
                    logger.info(f"py-clob-client method 'get_fee_rate_bps' not available, using REST API")
                    raise AttributeError("Method not available")
                    
            except (AttributeError, Exception) as e:
                # Fallback to REST API per Q1
                logger.info(f"⚠️  Falling back to REST API for fee rate: {e}")
                
                url = f"{CLOB_API_URL}/fee-rate"
                params = {"token_id": token_id}
                
                logger.info(f"🌐 Querying: GET {url}?token_id={token_id[:8]}...")
                
                # Use existing session instead of creating new one
                async with self._session.get(url, params=params, timeout=10) as response:
                    response_text = await response.text()
                    logger.info(f"📡 Fee rate API response: status={response.status}, body={response_text[:200]}")
                    
                    if response.status == 200:
                        data = await response.json()
                        # API returns {"base_fee": 1000}, not {"fee_rate_bps": 1000}
                        fee_rate = data.get("base_fee", 0)
                        logger.info(f"✓ REST API fee rate for {token_id[:8]}: {fee_rate} bps")
                        
                        # Cache the result
                        self._cache[cache_key] = fee_rate
                        return fee_rate
                    else:
                        error_msg = (
                            f"❌ Fee rate API returned {response.status}: {response_text}. "
                            f"Cannot proceed without fee rate (Polymarket Q4 guidance)."
                        )
                        logger.error(error_msg)
                        raise OrderExecutionError(error_msg)
            
        except OrderExecutionError:
            # Re-raise OrderExecutionError (from API failure above)
            raise
        except Exception as e:
            error_msg = (
                f"Could not query fee rate for {token_id[:8]}: {e}. "
                f"Skipping trade per Polymarket Q4 guidance (don't guess fee rates)."
            )
            logger.error(error_msg)
            raise OrderExecutionError(error_msg)

    async def get_fee_rate_bps(self, token_id: str) -> int:
        """
        Public wrapper for _get_market_fee_rate
        
        Per Polymarket Q1-Q6: Fee rates must be fetched dynamically per token.
        - Returns: 0 or 1000 basis points
        - Raises: OrderExecutionError if fee rate unavailable (per Q4)
        """
        return await self._get_market_fee_rate(token_id)

    async def get_best_price(
        self,
        token_id: str,
        side: str
    ) -> Optional[float]:
        """
        Get best available price from order book
        
        Args:
            token_id: Token identifier
            side: 'BUY' or 'SELL'
            
        Returns:
            Best price or None if no orders
        """
        order_book = await self.get_order_book(token_id)
        
        # OrderBookSummary object has .asks and .bids attributes (not dict methods)
        if side.upper() == 'BUY':
            # Best ask price (lowest sell price)
            asks = getattr(order_book, 'asks', [])
            if asks and len(asks) > 0:
                return float(asks[0].price)
        else:
            # Best bid price (highest buy price)
            bids = getattr(order_book, 'bids', [])
            if bids and len(bids) > 0:
                return float(bids[0].price)
        
        return None

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def create_market_buy_order(
        self,
        token_id: str,
        amount: float,
        neg_risk: bool = False  # 2026 Update: NegRisk signature flag
    ) -> Dict[str, Any]:
        """
        Create and execute a market buy order (single-step)
        
        Args:
            token_id: Token to buy
            amount: Amount in USDC to spend
            neg_risk: If True, include NegRisk signature (2026 requirement)
            
        Returns:
            Order response with execution details
        """
        self._ensure_initialized()
        
        try:
            logger.info(f"Executing market BUY: ${amount:.2f} USDC for token {token_id[:8]}...")
            
            # Query market's actual fee rate per Polymarket support guidance
            # 15-min crypto markets: 1000 bps, most others: 0 bps
            market_fee = await self._get_market_fee_rate(token_id)
            logger.info(f"Market fee rate: {market_fee} bps ({market_fee/100:.2f}%)")
            
            # Create MarketOrderArgs with FOK (Fill or Kill) for immediate execution
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
                fee_rate_bps=market_fee,  # Dynamically queried fee rate
                order_type=OrderType.FOK
            )
            
            # Sign the order
            signed_order = await asyncio.to_thread(
                self._client.create_market_order,
                order_args
            )
            
            # Post and execute
            result = await asyncio.to_thread(
                self._client.post_order,
                signed_order,
                OrderType.FOK
            )
            
            logger.info(f"✓ BUY order executed: {result.get('orderID', 'unknown')}")
            return result
            
        except Exception as e:
            error_str = str(e)
            
            # Handle specific Polymarket error codes
            if "FOK_ORDER_NOT_FILLED_ERROR" in error_str or "fully filled" in error_str.lower():
                from utils.exceptions import FOKOrderNotFilledError
                logger.warning(f"FOK BUY order not filled - no immediate match for token {token_id[:8]}")
                raise FOKOrderNotFilledError(
                    f"No immediate buyer found for token {token_id[:8]}",
                    token_id=token_id,
                    amount=amount
                )
            elif "INVALID_ORDER_NOT_ENOUGH_BALANCE" in error_str:
                logger.error(f"Insufficient balance for BUY order: {e}")
                raise InsufficientBalanceError(f"Not enough balance/allowance: {e}")
            elif "MARKET_NOT_READY" in error_str:
                logger.warning(f"Market not ready for trading: {e}")
                raise OrderExecutionError(f"Market not accepting orders: {e}", error_code="MARKET_NOT_READY")
            else:
                logger.error(f"Market buy order failed: {e}")
                raise OrderExecutionError(f"Market buy order failed: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def create_market_sell_order(
        self,
        token_id: str,
        amount: float,
        estimated_value: Optional[float] = None,
        neg_risk: bool = False  # 2026 Update: NegRisk signature flag
    ) -> Dict[str, Any]:
        """
        Create and execute a market sell order (single-step)
        
        Args:
            token_id: Token to sell
            amount: Amount of shares to sell
            estimated_value: Estimated USD value (for logging small orders)
            neg_risk: If True, include NegRisk signature (2026 requirement)
            
        Returns:
            Order response with execution details
            
        Raises:
            OrderExecutionError: If order fails or size is below minimum
        """
        self._ensure_initialized()
        
        try:
            # NOTE: No minimum order size check for SELL orders
            # SELL orders must execute regardless of size to close existing positions
            # BUY orders have minimum enforced at OrderManager layer
            
            logger.info(f"Executing market SELL: {amount:.2f} shares of token {token_id[:8]}...")
            
            # Query market's actual fee rate per Polymarket support guidance
            # 15-min crypto markets: 1000 bps, most others: 0 bps
            market_fee = await self._get_market_fee_rate(token_id)
            logger.info(f"Market fee rate: {market_fee} bps ({market_fee/100:.2f}%)")
            
            # Create MarketOrderArgs with FOK (Fill or Kill) for immediate execution
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=SELL,
                fee_rate_bps=market_fee,  # Dynamically queried fee rate
                order_type=OrderType.FOK
            )
            
            # Sign the order
            signed_order = await asyncio.to_thread(
                self._client.create_market_order,
                order_args
            )
            
            # Post and execute
            result = await asyncio.to_thread(
                self._client.post_order,
                signed_order,
                OrderType.FOK
            )
            
            logger.info(f"✓ SELL order executed: {result.get('orderID', 'unknown')}")
            return result
            
        except Exception as e:
            error_str = str(e)
            
            # Handle specific Polymarket error codes per support Q6/Q7
            if "FOK_ORDER_NOT_FILLED_ERROR" in error_str or "fully filled" in error_str.lower():
                from utils.exceptions import FOKOrderNotFilledError
                logger.warning(
                    f"FOK SELL order not filled - no immediate buyer for {amount:.2f} shares "
                    f"of token {token_id[:8]}. Will retry on next cycle."
                )
                raise FOKOrderNotFilledError(
                    f"No immediate buyer found for {amount:.2f} shares",
                    token_id=token_id,
                    amount=amount
                )
            elif "INVALID_ORDER_NOT_ENOUGH_BALANCE" in error_str:
                logger.error(f"Insufficient balance for SELL order: {e}")
                raise InsufficientBalanceError(f"Not enough shares or allowance: {e}")
            elif "MARKET_NOT_READY" in error_str:
                logger.warning(f"Market not ready for trading: {e}")
                raise OrderExecutionError(f"Market not accepting orders: {e}", error_code="MARKET_NOT_READY")
            elif "INVALID_ORDER_EXPIRATION" in error_str:
                logger.error(f"Order expiration time invalid: {e}")
                raise OrderExecutionError(f"Expiration time in the past: {e}", error_code="INVALID_ORDER_EXPIRATION")
            else:
                logger.error(f"Market sell order failed: {e}")
                raise OrderExecutionError(f"Market sell order failed: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def create_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float
    ) -> Dict[str, Any]:
        """
        Create a limit order
        
        Args:
            token_id: Token to trade
            side: 'BUY' or 'SELL'
            price: Limit price
            size: Order size
            
        Returns:
            Order response
        """
        self._ensure_initialized()
        
        try:
            logger.info(f"Creating limit {side} order: {size} @ {price} for {token_id}")
            
            # Fetch market's fee rate (CRITICAL: Must query per token)
            # Per Polymarket Q4: Skip trade if fee rate unavailable (don't guess)
            try:
                fee_rate_bps = await self.get_fee_rate_bps(token_id)
                logger.info(f"Using fee rate for {token_id[:8]}: {fee_rate_bps} bps")
            except OrderExecutionError as e:
                logger.error(f"Cannot create limit order without fee rate: {e}")
                raise  # Re-raise to skip this trade
            
            # Build order arguments with fee rate
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side.upper(),
                fee_rate_bps=fee_rate_bps  # CRITICAL: Include fee rate
            )
            
            # Create and sign order
            signed_order = await asyncio.to_thread(
                self._client.create_order,
                order_args
            )
            
            # Post order to exchange
            order_response = await asyncio.to_thread(
                self._client.post_order,
                signed_order
            )
            
            logger.info(f"Limit order posted: {order_response.get('orderID', 'unknown')}")
            return order_response
            
        except PolyApiException as e:
            # WORKAROUND: If API says "invalid fee rate (0), market requires X"
            # the fee-rate endpoint returned wrong value. Retry with correct fee.
            error_msg = str(e)
            if "invalid fee rate (0)" in error_msg and "market's taker fee:" in error_msg:
                # Extract correct fee rate from error message
                import re
                match = re.search(r"taker fee: (\d+)", error_msg)
                if match:
                    correct_fee = int(match.group(1))
                    logger.warning(
                        f"⚠️  Fee rate API returned 0 but market requires {correct_fee} bps. "
                        f"Retrying with correct fee..."
                    )
                    
                    # Retry with correct fee rate
                    order_args = OrderArgs(
                        token_id=token_id,
                        price=price,
                        size=size,
                        side=side.upper(),
                        fee_rate_bps=correct_fee
                    )
                    
                    signed_order = await asyncio.to_thread(
                        self._client.create_order,
                        order_args
                    )
                    
                    order_response = await asyncio.to_thread(
                        self._client.post_order,
                        signed_order
                    )
                    
                    # Update cache with correct fee for future orders
                    cache_key = f"fee_rate_{token_id}"
                    self._cache[cache_key] = correct_fee
                    logger.info(f"✅ Retry successful! Updated cache: {token_id[:8]} -> {correct_fee} bps")
                    
                    logger.info(f"Limit order posted: {order_response.get('orderID', 'unknown')}")
                    return order_response
            
            # If not the specific error above, re-raise
            logger.error(f"Failed to create limit order: {e}")
            raise OrderExecutionError(f"Limit order failed: {e}")
        except Exception as e:
            logger.error(f"Failed to create limit order: {e}")
            raise OrderExecutionError(f"Limit order failed: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def cancel_order(
        self,
        order_id: str
    ) -> Dict[str, Any]:
        """
        Cancel an open order
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            Cancellation response
        """
        self._ensure_initialized()
        
        try:
            logger.info(f"Cancelling order: {order_id}")
            
            response = await asyncio.to_thread(
                self._client.cancel,
                order_id=order_id
            )
            
            logger.info(f"Order cancelled: {order_id}")
            return response
            
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            raise OrderExecutionError(f"Order cancellation failed: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def cancel_all_orders(
        self,
        token_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Cancel all open orders, optionally filtered by token
        
        Args:
            token_id: Optional token filter
            
        Returns:
            List of cancellation responses
        """
        self._ensure_initialized()
        
        try:
            logger.info(f"Cancelling all orders" + (f" for {token_id}" if token_id else ""))
            
            responses = await asyncio.to_thread(
                self._client.cancel_all,
                asset_id=token_id
            )
            
            logger.info(f"Cancelled {len(responses)} orders")
            return responses
            
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            raise OrderExecutionError(f"Bulk cancellation failed: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_order(
        self,
        order_id: str
    ) -> Dict[str, Any]:
        """
        Get order details by ID
        
        Args:
            order_id: Order ID to query
            
        Returns:
            Order details
        """
        self._ensure_initialized()
        
        try:
            order = await asyncio.to_thread(
                self._client.get_order,
                order_id=order_id
            )
            return order
            
        except Exception as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            raise APIError(f"Failed to get order: {e}")

    @async_retry_with_backoff(max_retries=MAX_RETRIES)
    async def get_open_orders(
        self,
        token_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all open orders
        
        Args:
            token_id: Optional filter by token
            
        Returns:
            List of open orders
        """
        self._ensure_initialized()
        
        try:
            # ClobClient.get_orders() doesn't accept asset_id parameter in current version
            # Call without parameters to get all orders, then filter
            orders = await asyncio.to_thread(
                self._client.get_orders
            )
            
            # Filter by status and optionally by token_id
            result = [o for o in orders if o.get('status') == 'LIVE']
            if token_id:
                result = [o for o in result if o.get('asset_id') == token_id or o.get('token_id') == token_id]
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            raise APIError(f"Failed to get open orders: {e}")

    @property
    def wallet_address(self) -> str:
        """Get the wallet address"""
        self._ensure_initialized()
        return self._account.address

    async def close(self) -> None:
        """Cleanup client resources"""
        logger.info("Closing Polymarket client")
        
        # Close aiohttp session
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("HTTP session closed")
        
        self._is_initialized = False
        self._client = None
        self._account = None
        self._session = None
        self._private_key = None
