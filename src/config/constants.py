"""
Configuration Module for Polymarket Arbitrage Bot

This module centralizes all constants and configuration parameters for the production-grade
Polymarket arbitrage bot. All values are organized by category with comprehensive documentation.

Key Principles:
- Single source of truth for all configuration
- All constants are Final (immutable)
- Clear documentation for each parameter
- Environment-specific overrides via environment variables
- Type hints for IDE support and runtime safety
"""

from typing import Final
import os


# ============================================================================
# 1. WALLET CONFIGURATION
# ============================================================================
# Polymarket uses TWO distinct addresses for each account:
#
# SIGNER WALLET (Derived from private key):
#   - Used for authentication and signing transactions
#   - Retrieved from AWS Secrets Manager
#   - Example: 0x0E12aea39cE3FeC5E1dE7BFdA7A7092B9404279a
#
# PROXY WALLET (Smart Contract Account):
#   - Used for trading operations: balances, positions, orders
#   - Where deposits go and trades originate from
#   - Shown in Polymarket profile dropdown
#   - Example: 0x5967c88F93f202D595B9A47496b53E28cD61F4C3
#
# ACTION: Set PROXY_WALLET_ADDRESS to your actual proxy wallet address
# ============================================================================

PROXY_WALLET_ADDRESS: Final[str] = os.getenv(
    'POLYMARKET_PROXY_ADDRESS',
    '0x5967c88F93f202D595B9A47496b53E28cD61F4C3'
)


# ============================================================================
# 2. MIRROR STRATEGY CONFIGURATION
# ============================================================================
# Configuration for mirroring trades from target whale wallets

# Target whale wallet to mirror (proxy address)
# ACTION: Change this to the whale wallet you want to mirror
MIRROR_TARGET: Final[str] = os.getenv(
    'MIRROR_TARGET_WALLET',
    '0x63ce342161250d705dc0b16df89036c8e5f9ba9a'
)

# Enable WebSocket-based detection (True) vs polling (False)
# WebSocket: Lower latency, fewer API calls, better for latency-sensitive trading
# Polling: Simpler implementation, suitable for lower-frequency strategies
USE_WEBSOCKET_DETECTION: Final[bool] = os.getenv(
    'USE_WEBSOCKET_DETECTION',
    'false'
).lower() == 'true'


# ============================================================================
# TRADING PARAMETERS
# ============================================================================

# Only buy if price is within 0.05% of whale's price
# Prevents buying at significantly worse prices
ENTRY_PRICE_GUARD: Final[float] = 0.0005

# Maximum order size cap - only upper bound for proportional sizing
# Polymarket requires minimum 5 shares per order (enforced by exchange)
# Strategy: Let proportional sizing determine order size, cap at max
# Orders below 5-share minimum will be rejected by Polymarket (honest failure)
# This maintains true proportional relationship with whale's trades
MAX_ORDER_USD: Final[float] = 3.6

# Ignore positions smaller than this threshold (in USDC)
DUST_THRESHOLD: Final[float] = 0.1

# Maximum acceptable slippage percentage (3%)
# Increased for market orders to allow natural price movement
# Trades will fail if slippage exceeds this limit
MAX_SLIPPAGE_PERCENT: Final[float] = 0.03

# ============================================================================
# POLYMARKET ERROR CODES (from official support)
# ============================================================================

# Common order rejection reasons:
ERROR_INSUFFICIENT_BALANCE: Final[str] = "INVALID_ORDER_NOT_ENOUGH_BALANCE"
ERROR_FOK_NOT_FILLED: Final[str] = "FOK_ORDER_NOT_FILLED_ERROR"
ERROR_INVALID_EXPIRATION: Final[str] = "INVALID_ORDER_EXPIRATION"
ERROR_MARKET_NOT_READY: Final[str] = "MARKET_NOT_READY"

# FOK order specific error message
FOK_ERROR_MESSAGE: Final[str] = "order couldn't be fully filled, FOK orders are fully filled/killed"


# Mirror strategy price bounds (for BUY orders only)
# Don't buy below this price (very unlikely outcomes with minimal value)
# Lowered from 0.15 to 0.10 to allow more trades
MIN_BUY_PRICE: Final[float] = 0.10

# Don't buy above this price (near-certain outcomes with minimal upside)
MAX_BUY_PRICE: Final[float] = 0.85

# Polymarket minimum order size (in shares)
# Orders below 5 shares are rejected by the exchange
MIN_ORDER_SHARES: Final[int] = 5

# ============================================================================
# TIME-BASED ENTRY FILTERING (per Polymarket support - Jan 2026)
# ============================================================================


# Only mirror positions where whale entered within this time window
# Prevents copying old positions where whale may already be at a loss
# Changed to 10 minutes for a slightly longer window
ENTRY_TIME_WINDOW_MINUTES: Final[int] = 10

# Enable time-based filtering (set to False to disable)
ENABLE_TIME_BASED_FILTERING: Final[bool] = True


# ============================================================================
# OPERATIONAL PARAMETERS
# ============================================================================

# Main loop interval in seconds
# How frequently the bot checks for new opportunities
# Reduced from 15s to 2s for lower latency (per optimization recommendation)
LOOP_INTERVAL_SEC: Final[int] = 2

# Request timeout for API calls (seconds)
API_TIMEOUT_SEC: Final[int] = 30

# Maximum retries for failed API calls
MAX_RETRIES: Final[int] = 3

# Exponential backoff base delay (seconds)
RETRY_BASE_DELAY: Final[float] = 1.0

# Maximum backoff delay (seconds)
MAX_BACKOFF_DELAY: Final[float] = 10.0

# ============================================================================
# API RATE LIMITS (per Polymarket support - Jan 2026)
# ============================================================================

# L2-Authenticated Endpoints (Higher Limits):
# POST /order:   3500 req/10s (burst), 36000 req/10min
# DELETE /order: 3000 req/10s (burst), 30000 req/10min
#
# L1/Public Endpoints (Standard Limits):
# Data API: 150 req/10s, 1500 req/10min
# Gamma API: 300 req/10s, 3000 req/10min  
# CLOB reads: 1500 req/10s, 15000 req/10min

# Bot trading rate (orders per minute) - conservative vs L2 limits
# L2 allows 36000/10min = 3600/min, but we stay conservative
MAX_ORDERS_PER_MINUTE: Final[int] = 100
MAX_BACKOFF_DELAY: Final[float] = 60.0


# ============================================================================
# POLYMARKET API CONFIGURATION
# ============================================================================

# Polymarket CLOB API endpoints
# Used for: Order placement, price queries, order book
# Rate Limits: /price = 1500 req/10s (single market)
#              /prices = 500 req/10s (batch queries)
#              /book = 1500 req/10s (order book depth)
CLOB_API_URL: Final[str] = "https://clob.polymarket.com"

# Polymarket Data API - recommended for querying positions
# Rate Limits: /positions = 150 req/10s, max 500 results per request, 10k offset limit
#              /v1/closed-positions = max 50 per request, 100k offset limit
POLYMARKET_DATA_API_URL: Final[str] = "https://data-api.polymarket.com"

# Polymarket Gamma API - for converting condition IDs to token IDs
# Rate Limits: General = 4000 req/10s, /markets endpoint = 300 req/10s
# Note: Token IDs are STABLE and don't change, so cache them long-term
POLYMARKET_GAMMA_API_URL: Final[str] = "https://gamma-api.polymarket.com"

# Polymarket WebSocket API - for real-time whale tracking (future enhancement)
# Latency: ~100ms for real-time trade feeds
POLYMARKET_WEBSOCKET_URL: Final[str] = "wss://ws-live-data.polymarket.com"

# Polygon network configuration (Polymarket runs on Polygon)
POLYGON_RPC_URL: Final[str] = "https://polygon-rpc.com"
POLYGON_CHAIN_ID: Final[int] = 137
CHAIN_ID: Final[int] = 137  # Alias for compatibility

# USDC token address on Polygon
USDC_ADDRESS: Final[str] = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_CONTRACT_ADDRESS: Final[str] = USDC_ADDRESS  # Alias for compatibility
FUNDER_ADDRESS: Final[str] = PROXY_WALLET_ADDRESS  # Alias for compatibility

# Polymarket CTF Exchange contract address (for placing orders)
CTF_EXCHANGE_ADDRESS: Final[str] = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Polymarket CTF Contract address (for redeeming winning positions)
# Per Polymarket support: Use this to redeem resolved positions
CTF_CONTRACT_ADDRESS: Final[str] = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"


# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

# Logging level: 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'
LOG_LEVEL: Final[str] = 'INFO'

# Path to log file (ensure write permissions)
LOG_FILE_PATH: Final[str] = 'logs/polymarket_bot.log'

# Maximum log file size in bytes (50 MB - rotate after this size)
# With 10 backups = max 550 MB total (50 MB × 11 files)
MAX_LOG_FILE_SIZE: Final[int] = 50 * 1024 * 1024

# Number of backup log files to keep (10 backups + 1 current = 11 files max)
# Older logs are automatically deleted when this limit is reached
LOG_BACKUP_COUNT: Final[int] = 10

# Enable JSON structured logging for better parsing
STRUCTURED_LOGGING: Final[bool] = True


# ============================================================================
# MONITORING & HEALTH CHECK
# ============================================================================

# Health check interval (seconds)
HEALTH_CHECK_INTERVAL_SEC: Final[int] = 60

# Maximum consecutive errors before alerting
MAX_CONSECUTIVE_ERRORS: Final[int] = 5

# Enable performance metrics collection
ENABLE_METRICS: Final[bool] = True

# Metrics export interval (seconds)
METRICS_EXPORT_INTERVAL_SEC: Final[int] = 300


# ============================================================================
# SAFETY LIMITS
# ============================================================================

# Maximum position size in USDC (per market)
# Reduced to 50 USD for initial conservative deployment
MAX_POSITION_SIZE_USD: Final[float] = 50.0

# Maximum daily trading volume in USDC
MAX_DAILY_VOLUME_USD: Final[float] = 10000.0

# Maximum number of open positions
MAX_OPEN_POSITIONS: Final[int] = 20

# Enable circuit breaker on large losses
ENABLE_CIRCUIT_BREAKER: Final[bool] = True

# Circuit breaker loss threshold (USD)
# Reduced to 25 USD for small account protection during initial deployment
CIRCUIT_BREAKER_LOSS_THRESHOLD_USD: Final[float] = 25.0


# ============================================================================
# STRATEGY-SPECIFIC CONFIGURATION - MIRROR STRATEGY
# ============================================================================
# The Mirror Strategy runs 3 parallel flows:
# 1. Trade Mirroring: Copy whale's buy/sell trades (frequent - every 2-5 sec)
# 2. Position Alignment: Sell positions whale has closed (less frequent - every 60 sec)
# 3. Position Redemption: Redeem closed positions for profit (less frequent - every 60 sec)
# ============================================================================

# ─────────────────────────────────────────────────────────────────────────
# FLOW 1: TRADE MIRRORING CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────
# Copy whale's recent trades (both buy and sell orders) with safety checks
# Runs frequently to catch market opportunities quickly

MIRROR_TRADE_POLLING_INTERVAL_SEC: Final[int] = 2  # Check whale's recent trades every 2 seconds
MIRROR_TRADE_TIME_WINDOW_MINUTES: Final[int] = 10  # Only mirror trades from last 10 minutes
MIRROR_ENTRY_DELAY_SEC: Final[float] = 0  # Delay between executing multiple trades (0 = immediate)

# Order sizing for mirrored trades
MIRROR_USE_PROPORTIONAL_SIZE: Final[bool] = False  # True = size * ratio, False = fixed size
MIRROR_POSITION_SIZE_MULTIPLIER: Final[float] = 1.0  # Follow whale's position size exactly (if proportional)
MIRROR_ORDER_SIZE_RATIO: Final[float] = 0.05  # If proportional: trade 5% of whale's order size
MIRROR_MAX_ORDER_SIZE_USD: Final[float] = 1.0  # Maximum order size cap ($1 = conservative)
MIRROR_MIN_ORDER_SIZE_USD: Final[float] = 0.01  # Minimum order size in USD (reject below this)

# Order execution strategy
MIRROR_USE_MARKET_ORDERS: Final[bool] = False  # False = limit orders, True = market orders
MIRROR_LIMIT_ORDER_PRICE_BUFFER_PERCENT: Final[float] = 4.0  # 4% buffer for limit order pricing
MIRROR_MARKET_ORDER_MAX_PRICE_DEVIATION_PERCENT: Final[float] = 50.0  # Max allowed slippage

# Balance caching to reduce API calls
MIRROR_BALANCE_CACHE_SECONDS: Final[int] = 30  # Cache balance for 30 seconds

# ─────────────────────────────────────────────────────────────────────────
# FLOW 2: POSITION ALIGNMENT CONFIGURATION  
# ─────────────────────────────────────────────────────────────────────────
# Detect whale exits and immediately sell matching positions we hold
# Ensures we don't hold positions whale has already closed (exit following)
# Runs less frequently (every 60 seconds) since whale exits are less common

MIRROR_POSITION_ALIGNMENT_INTERVAL_SEC: Final[int] = 60  # Check for whale exits every 60 seconds
MIRROR_CLOSED_POSITIONS_LOOK_BACK_LIMIT: Final[int] = 10  # Check whale's last 10 closed positions
MIRROR_SELL_IMMEDIATELY_ON_WHALE_EXIT: Final[bool] = True  # True = sell immediately when whale exits
MIRROR_SELL_ORDER_TYPE: Final[str] = 'LIMIT'  # LIMIT or MARKET (for selling positions)
MIRROR_SELL_PRICE_BUFFER_PERCENT: Final[float] = 2.0  # 2% buffer for sell limit orders (more aggressive)

# ─────────────────────────────────────────────────────────────────────────
# FLOW 3: POSITION REDEMPTION CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────
# Redeem closed/resolved positions to collect profits
# Runs less frequently (every 60 seconds) since resolution is rare
# 
# On Polymarket, when a market resolves:
# 1. Winning shares can be redeemed for $1 USDC each
# 2. Losing shares become worthless
# This flow detects resolved markets and redeems winning positions

MIRROR_POSITION_REDEMPTION_INTERVAL_SEC: Final[int] = 60  # Check for redeemable positions every 60 seconds
MIRROR_AUTO_REDEEM_CLOSED_POSITIONS: Final[bool] = True  # Automatically redeem winning positions
MIRROR_BATCH_REDEEM_SIZE: Final[int] = 5  # Redeem max 5 positions per cycle

# ─────────────────────────────────────────────────────────────────────────
# MIRROR STRATEGY OVERALL CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────

MIRROR_STRATEGY_CONFIG = {
    # Overall enabled/disabled
    'enabled': True,
    
    # ===== FLOW 1: Trade Mirroring =====
    'flow_1_trade_mirroring_enabled': True,
    'flow_1_interval_sec': MIRROR_TRADE_POLLING_INTERVAL_SEC,
    'flow_1_time_window_minutes': MIRROR_TRADE_TIME_WINDOW_MINUTES,
    'flow_1_entry_delay_sec': MIRROR_ENTRY_DELAY_SEC,
    'flow_1_use_proportional_size': MIRROR_USE_PROPORTIONAL_SIZE,
    'flow_1_position_size_multiplier': MIRROR_POSITION_SIZE_MULTIPLIER,
    'flow_1_order_size_ratio': MIRROR_ORDER_SIZE_RATIO,
    'flow_1_max_order_size_usd': MIRROR_MAX_ORDER_SIZE_USD,
    'flow_1_min_order_size_usd': MIRROR_MIN_ORDER_SIZE_USD,
    'flow_1_use_market_orders': MIRROR_USE_MARKET_ORDERS,
    'flow_1_price_buffer_percent': MIRROR_LIMIT_ORDER_PRICE_BUFFER_PERCENT,
    'flow_1_max_price_deviation_percent': MIRROR_MARKET_ORDER_MAX_PRICE_DEVIATION_PERCENT,
    'flow_1_balance_cache_seconds': MIRROR_BALANCE_CACHE_SECONDS,
    
    # ===== FLOW 2: Position Alignment =====
    'flow_2_position_alignment_enabled': True,
    'flow_2_interval_sec': MIRROR_POSITION_ALIGNMENT_INTERVAL_SEC,
    'flow_2_closed_positions_limit': MIRROR_CLOSED_POSITIONS_LOOK_BACK_LIMIT,
    'flow_2_sell_immediately': MIRROR_SELL_IMMEDIATELY_ON_WHALE_EXIT,
    'flow_2_sell_order_type': MIRROR_SELL_ORDER_TYPE,
    'flow_2_sell_price_buffer_percent': MIRROR_SELL_PRICE_BUFFER_PERCENT,
    
    # ===== FLOW 3: Position Redemption =====
    'flow_3_position_redemption_enabled': True,
    'flow_3_interval_sec': MIRROR_POSITION_REDEMPTION_INTERVAL_SEC,
    'flow_3_auto_redeem': MIRROR_AUTO_REDEEM_CLOSED_POSITIONS,
    'flow_3_batch_redeem_size': MIRROR_BATCH_REDEEM_SIZE,
}

# Arbitrage Strategy Configuration (placeholder for future implementation)
ARBITRAGE_STRATEGY_CONFIG = {
    'enabled': False,
    'min_profit_percent': 0.01,  # Minimum 1% profit to execute
    'scan_interval_sec': 10,
}


# ============================================================================
# AWS CONFIGURATION (loaded from environment)
# ============================================================================

# AWS region for Secrets Manager and other services
# Changed to eu-west-1 (Ireland) per Polymarket support recommendation
# to avoid Cloudflare bot detection on datacenter IPs
AWS_REGION: Final[str] = "eu-west-1"

# AWS Secrets Manager secret ID for credentials
AWS_SECRET_ID: Final[str] = "polymarket/prod/credentials"

# Expected secret keys in AWS Secrets Manager
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# WALLET_PRIVATE_KEY: MetaMask private key for signing transactions (L1 auth)
# 
# L2 API Credentials (required for order posting):
#   POLY_API_KEY:    UUID-format API key for CLOB authentication
#   POLY_API_SECRET: HMAC secret for signing L2 requests  
#   POLY_API_PASS:   Additional passphrase for L2 security
#
# Note: Each wallet can only have ONE active set of L2 credentials.
#       Creating new credentials invalidates previous ones.
#       Credentials are wallet-specific and cannot be shared.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECRET_KEYS = {
    'WALLET_PRIVATE_KEY': str,  # Required: Private key for signing transactions
    'POLY_API_KEY': str,        # Required: L2 API key (stored in Secrets Manager)
    'POLY_API_SECRET': str,     # Required: L2 API secret (stored in Secrets Manager)
    'POLY_API_PASS': str,       # Required: L2 API passphrase (stored in Secrets Manager)
}
