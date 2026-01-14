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

from typing import Final, Dict
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
# 2. MULTI-STRATEGY BUDGET ALLOCATION
# ============================================================================
# The bot runs multiple strategies in parallel, each with dedicated capital.
# This prevents strategies from competing for the same funds and provides
# clear risk isolation.
#
# Capital Allocation Philosophy:
# - Arbitrage: Reserved capital for rare, high-confidence opportunities
# - Market Making: Active capital for steady income generation
# - Reserve: Safety buffer for unexpected scenarios
#
# Total available: $72.92 USDC (as of 2026-01-14)
# ============================================================================

# Arbitrage strategy allocation
# Conservative allocation since opportunities are rare
# Enough for 1-2 simultaneous arbitrage baskets at $10 each
ARBITRAGE_STRATEGY_CAPITAL: Final[float] = 20.0

# Market making strategy allocation  
# Majority of capital for active deployment
# Supports 4-5 simultaneous market making positions at $10-12 each
MARKET_MAKING_STRATEGY_CAPITAL: Final[float] = 50.0

# Reserve buffer (emergency fund, gas, unexpected fees)
STRATEGY_RESERVE_BUFFER: Final[float] = 2.92

# Maximum capital utilization across all strategies (safety check)
# Set to 97% to leave small buffer for fees/gas
MAX_TOTAL_CAPITAL_UTILIZATION: Final[float] = 0.97


# ============================================================================
# 2B. ARBITRAGE FEE CONFIGURATION
# ============================================================================
# Taker fee parameters for arbitrage execution
# CRITICAL: If Polymarket moves to dynamic fees, update these constants
# Current fee tier: 1.2% (competitive with 1% tier traders)

# Taker fee percentage per trade (basis for opportunity detection)
# Current Polymarket fee: 1.0% (for tier 1) to 1.5% (for tier 0)
# We use 1.2% as competitive buffer (beats tier 0, loses to tier 1)
ARBITRAGE_TAKER_FEE_PERCENT: Final[float] = 0.012  # 1.2%

# Arbitrage opportunity threshold
# Only execute if sum(prices) < this threshold (accounts for fees)
ARBITRAGE_OPPORTUNITY_THRESHOLD: Final[float] = 0.98  # sum < 98 cents


# ============================================================================
# 3. TRADING PARAMETERS
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

# Maximum acceptable slippage percentage (3%)
# Increased for market orders to allow natural price movement
# Trades will fail if slippage exceeds this limit
MAX_SLIPPAGE_PERCENT: Final[float] = 0.03

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
# Production setting: 60s for resilience during API outages
MAX_BACKOFF_DELAY: Final[float] = 60.0

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


# ============================================================================
# SAFETY LIMITS
# ============================================================================

# Maximum position size in USDC (per market)
# Reduced to 50 USD for initial conservative deployment
MAX_POSITION_SIZE_USD: Final[float] = 50.0

# Enable circuit breaker on large losses
ENABLE_CIRCUIT_BREAKER: Final[bool] = True

# Circuit breaker loss threshold (USD)
# Reduced to 25 USD for small account protection during initial deployment
CIRCUIT_BREAKER_LOSS_THRESHOLD_USD: Final[float] = 25.0


# ============================================================================
# ATOMIC EXECUTION PARAMETERS (Zero Legging-In Risk)
# ============================================================================
# Configuration for AtomicDepthAwareExecutor - prevents partial fills and
# ensures all legs execute atomically or not at all

# Minimum liquidity depth required per outcome before execution (in shares)
# Ensures sufficient market depth exists for atomic execution
# Higher values = safer but fewer opportunities
ATOMIC_MIN_DEPTH_SHARES: Final[float] = 10.0

# Number of price levels (pips) to check in order book depth
# Checks top N levels of the order book for available liquidity
# Must have MIN_DEPTH_SHARES within TOP_PIPS_DEPTH levels
ATOMIC_TOP_PIPS_DEPTH: Final[int] = 3

# Order fill timeout in seconds
# Maximum time to wait for all orders to fill completely
# If exceeded, cancels all pending orders
ATOMIC_ORDER_TIMEOUT_SEC: Final[int] = 5

# Order status check interval in milliseconds
# Frequency of polling order status during fill monitoring
# Lower = faster detection of partial fills, higher API usage
ATOMIC_CHECK_INTERVAL_MS: Final[int] = 100

# Maximum price slippage tolerance per outcome (in dollars)
# Prevents execution if market moves unfavorably during placement
ATOMIC_MAX_SLIPPAGE_USD: Final[float] = 0.005

# Cooldown period after execution failure (in seconds)
# Prevents rapid retries after CRITICAL failures (partial fills, errors)
ATOMIC_FAILURE_COOLDOWN_SEC: Final[int] = 30

# Maximum consecutive failures before circuit breaker activation
# Bot pauses atomic execution after this many consecutive failures
ATOMIC_MAX_CONSECUTIVE_FAILURES: Final[int] = 3

# ============================================================================
# 2026 PRODUCTION SAFEGUARDS
# ============================================================================
# Critical production settings for NegRisk, balance management, and FOK handling

# Maximum percentage of available balance to commit per trade (safety guard)
# Never risk more than 90% of total balance in single execution
MAX_BALANCE_UTILIZATION_PERCENT: Final[float] = 0.90  # 90% max

# Cooldown period after FOK order fails to fill (in seconds)
# Prevents chasing moving prices with rapid retries
FOK_FILL_FAILURE_COOLDOWN_SEC: Final[int] = 10

# NegRisk market detection and handling
# Automatically detect and flag NegRisk markets for proper signature
ENABLE_NEGRISK_AUTO_DETECTION: Final[bool] = True

# ============================================================================
# PRODUCTION MONITORING & SAFETY (Heartbeat System)
# ============================================================================
# Critical production safeguards for automated trading

# Heartbeat interval - how often to log balance and health metrics
HEARTBEAT_INTERVAL_SEC: Final[int] = 300  # 5 minutes

# Maximum allowed drawdown before emergency kill switch
# If balance drops by more than this amount, stop all trading
DRAWDOWN_LIMIT_USD: Final[float] = 10.0  # $10 (10% of $100 budget)

# Auto-redeem check interval - how often to check for resolved markets
AUTO_REDEEM_INTERVAL_SEC: Final[int] = 600  # 10 minutes

# Graceful shutdown timeout - max time to wait for order cancellations
GRACEFUL_SHUTDOWN_TIMEOUT_SEC: Final[int] = 30  # 30 seconds

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


# ============================================================================
# MAKER-FIRST EXECUTION (2026 Institutional Upgrade)
# ============================================================================

# Enable post-only orders (maker-only, never taker)
# Ensures we always provide liquidity and prepare for maker rebates
ENABLE_POST_ONLY_ORDERS: Final[bool] = True

# FIX 2: Dynamic spread offset configuration
# Instead of fixed $0.01, calculate offset based on current spread
# Capture this percentage of the spread for optimal queue position
# Example: 15% of $0.10 spread = $0.015 offset (1.5 ticks)
DYNAMIC_SPREAD_CAPTURE_PCT: Final[float] = 0.15  # 15% of spread

# Maximum ticks to jump above best bid (prevents overpaying)
# Caps offset even in wide spreads (e.g., max 3 × $0.01 = $0.03)
MAX_DYNAMIC_OFFSET_TICKS: Final[int] = 3

# Post-only spread offset (dollars) - LEGACY fallback only
# Target_Price = Best_Bid + OFFSET (for BUY orders)
# This ensures we "join the bid" rather than "hit the ask"
POST_ONLY_SPREAD_OFFSET: Final[float] = 0.01

# Cooldown after INVALID_POST_ONLY_ORDER error (spread crossed)
# When our maker order would cross the spread, wait for next price scan
# This prevents us from accidentally becoming a taker
POST_ONLY_ERROR_COOLDOWN_SEC: Final[int] = 60

# Maximum order age before auto-cancel (seconds)
# If order remains unfilled for this long, cancel to avoid stale prices
MAX_ORDER_AGE_SEC: Final[int] = 300  # 5 minutes

# Order monitoring interval (seconds)
# How often to check unfilled orders
ORDER_MONITOR_INTERVAL_SEC: Final[int] = 10

# Order heartbeat interval (seconds)
# Monitor GTC orders and cancel if not filled within this time
# User requirement: 60 seconds to prevent stale orders
ORDER_HEARTBEAT_INTERVAL_SEC: Final[int] = 60

# Price walking step for maker orders (dollars)
# If INVALID_POST_ONLY_ORDER, retry with this increment if still profitable
# User requirement: $0.001 increments
MAKER_RETRY_PRICE_STEP: Final[float] = 0.001


# ============================================================================
# REBATE OPTIMIZATION (2026 Institutional Trading)
# ============================================================================

# Rebate priority weight for scanner ranking
# Markets in optimal price range ($0.20-$0.80) get this multiplier
# Per Polymarket team: rebate pools are largest in this range
REBATE_PRIORITY_WEIGHT: Final[float] = 2.0

# Optimal price range for rebate accumulation
# Polymarket rebates are highest for trades between these prices
REBATE_OPTIMAL_PRICE_MIN: Final[float] = 0.20
REBATE_OPTIMAL_PRICE_MAX: Final[float] = 0.80

# Auto-redemption check interval (seconds)
# How often to scan for resolved markets and redeem shares
# User requirement: hourly (3600 seconds)
CHECK_AND_REDEEM_INTERVAL_SEC: Final[int] = 3600


# ============================================================================
# BATCH ORDER EXECUTION (HFT Optimization)
# ============================================================================

# Maximum orders per batch (Polymarket limit)
MAX_BATCH_SIZE: Final[int] = 15

# Retry cooldown after batch failure (seconds)
RETRY_COOLDOWN: Final[int] = 15

# Rate limit: Burst capacity (requests per second)
RATE_LIMIT_BURST: Final[int] = 100

# Rate limit: Sustained capacity (requests per second)
RATE_LIMIT_SUSTAINED: Final[int] = 25

# Re-sync wait time after partial batch failure (seconds)
BATCH_RESYNC_WAIT: Final[int] = 15

# ============================================================================
# CAPITAL MANAGEMENT (Negative Risk & Exposure)
# ============================================================================

# Maximum total exposure in USDC (principal protection on $100 budget)
MAX_TOTAL_EXPOSURE: Final[float] = 95.0

# NegRisk Adapter contract address for token conversion
NEGRISK_ADAPTER_ADDRESS: Final[str] = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# CTF Exchange contract (for allowance checks)
CTF_EXCHANGE_ADDRESS: Final[str] = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# USDC contract address on Polygon
USDC_ADDRESS: Final[str] = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# CTF (Conditional Token Framework) contract for merge operations
CTF_CONTRACT_ADDRESS: Final[str] = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Merge pause duration after relayer transaction failure (seconds)
MERGE_FAILURE_PAUSE_SEC: Final[int] = 60


# ============================================================================
# RELIABILITY & REBOOT RECOVERY
# ============================================================================

# Delayed order monitoring timeout (seconds)
DELAYED_ORDER_TIMEOUT_SEC: Final[int] = 30

# Delayed order check interval (seconds)
DELAYED_ORDER_CHECK_INTERVAL_SEC: Final[int] = 5

# Self-trade prevention: check interval (seconds)
STP_CHECK_INTERVAL_SEC: Final[int] = 2

# Self-trade prevention: cooldown after cancellation (seconds)
# Per Polymarket support: Wait 1 second after cancelling to prevent race conditions
# Ensures matching engine has fully cleared the old order before posting new one
STP_COOLDOWN: Final[float] = 1.0

# Nonce resynchronization on boot
ENABLE_NONCE_SYNC_ON_BOOT: Final[bool] = True

# State persistence interval (seconds)
STATE_PERSISTENCE_INTERVAL_SEC: Final[int] = 60

# State file path
BOT_STATE_FILE: Final[str] = "bot_state.json"

# ============================================================================
# HFT ORDER STATE MACHINE (2026 Market-Aware Timing)
# ============================================================================

# Market-aware delay thresholds (seconds)
# Different market types have different matching engine latencies
DELAY_THRESHOLDS: Final[Dict[str, float]] = {
    "sports": 12.0,    # Sports events: slower matching (12s)
    "crypto": 5.0,     # Crypto markets: fast matching (5s)
    "politics": 7.0,   # Politics: standard matching (7s)
    "default": 7.0     # Other markets: standard matching (7s)
}

# Order state machine polling interval (seconds)
# Poll order status every 2 seconds for PENDING/DELAYED orders
ORDER_STATE_POLL_INTERVAL_SEC: Final[int] = 2

# Batch partial-fill hold duration (seconds)
# If 1 leg is DELAYED while others are MATCHED, hold for this duration
# Protects arbitrage hedge by allowing delayed leg to fill
BATCH_DELAYED_LEG_HOLD_SEC: Final[int] = 10

# Clean exit: cancel all DELAYED orders on shutdown
CANCEL_DELAYED_ON_SHUTDOWN: Final[bool] = True
# ============================================================================
# REBATE TRACKING (Maker Volume Logging)
# ============================================================================

# Enable detailed maker rebate tracking logs
# Logs successful fills for rebate eligibility verification
ENABLE_REBATE_TRACKING: Final[bool] = True

# Rebate log file location
REBATE_LOG_FILE: Final[str] = "logs/maker_rebates.jsonl"

# ============================================================================
# MARKET MAKING STRATEGY PARAMETERS
# ============================================================================

# Market Selection
# -----------------
# Minimum 24h volume to consider for market making (USD)
# DISCOVERY MODE: $10/day (ultra-low for finding ANY active markets)
# Per Polymarket Support: Start low, increase based on what's available
# Previous: $50 (still too high - found 0 markets)
#
# PRODUCTION NOTE: If bot trades too many low-quality markets (wide spreads,
# unprofitable fills), increase to $50-100 to filter for better liquidity
MM_MIN_MARKET_VOLUME_24H: Final[float] = 10.0

# Minimum liquidity (orderbook depth) - MORE CRITICAL THAN VOLUME
# DISCOVERY MODE: $5 (ultra-low to discover what's actually available)
# Previous: $20 (too high - found 0 markets)
MM_MIN_LIQUIDITY: Final[float] = 5.0

# Minimum depth (shares) on best bid/ask
# Per Polymarket Support: "Lower to 5 shares (or even lower) with $50 capital"
# Previous: 10 shares (too strict for small markets)
MM_MIN_DEPTH_SHARES: Final[float] = 5.0

# Maximum spread to consider market liquid enough
# INSTITUTION-GRADE: 7% max (reduced from 10%)
# Wider spreads = higher adverse selection risk
MM_MAX_SPREAD_PERCENT: Final[float] = 0.07  # 7% max spread

# Prefer binary markets (2 outcomes) for simplicity
MM_PREFER_BINARY_MARKETS: Final[bool] = True

# Maximum number of markets to make simultaneously
MM_MAX_ACTIVE_MARKETS: Final[int] = 3


# Position Sizing
# ----------------
# Base position size per market (USD)
MM_BASE_POSITION_SIZE: Final[float] = 10.0

# Maximum position size per market (USD)
MM_MAX_POSITION_SIZE: Final[float] = 15.0

# Maximum inventory (shares) per outcome per market
# Prevents accumulating too much directional risk
MM_MAX_INVENTORY_PER_OUTCOME: Final[int] = 30


# Spread Management
# ------------------
# Target spread (profit per round trip before fees)
MM_TARGET_SPREAD: Final[float] = 0.03  # 3 cents = 3%

# Minimum spread (don't go tighter than this)
MM_MIN_SPREAD: Final[float] = 0.02  # 2 cents = 2%

# Maximum spread (if wider, market too illiquid)
MM_MAX_SPREAD: Final[float] = 0.08  # 8 cents = 8%

# Spread adjustment based on inventory imbalance
# If long 20 shares, widen ask by this factor to encourage selling
MM_INVENTORY_SPREAD_MULTIPLIER: Final[float] = 1.5


# Risk Management
# ----------------
# Maximum loss per position before force-exit (USD)
MM_MAX_LOSS_PER_POSITION: Final[float] = 3.0

# Global daily loss limit - circuit breaker (USD)
MM_GLOBAL_DAILY_LOSS_LIMIT: Final[float] = 50.0

# Global directional exposure limit - correlation protection (USD)
# Prevents excessive exposure to correlated markets
MM_MAX_TOTAL_DIRECTIONAL_EXPOSURE: Final[float] = 100.0

# External oracle price deviation threshold (percentage)
MM_ORACLE_PRICE_DEVIATION_LIMIT: Final[float] = 0.15  # 15% max deviation

# Maximum time to hold inventory before force-liquidation (seconds)
# INSTITUTION-GRADE: 30 minutes (reduced from 1 hour)
# Binary markets move fast - shorter hold time reduces directional risk
MM_MAX_INVENTORY_HOLD_TIME: Final[int] = 1800  # 30 minutes

# Position check interval (seconds)
MM_POSITION_CHECK_INTERVAL: Final[int] = 30

# Price move threshold to trigger emergency exit (percentage)
# If price moves > 15% against position, exit immediately
MM_EMERGENCY_EXIT_THRESHOLD: Final[float] = 0.15


# Order Management
# -----------------
# Quote update frequency (seconds)
# INSTITUTION-GRADE: 3s refresh (was 20s)
# Per optimization: With WebSockets, can refresh faster without rate limits
# 20s was too slow - quotes go stale in fast markets (debates, sports)
MM_QUOTE_UPDATE_INTERVAL: Final[int] = 3

# Order time-to-live (seconds)
# Cancel and replace orders after this duration even if not filled
MM_ORDER_TTL: Final[int] = 120

# Minimum time between order placements (prevent spam)
MM_MIN_ORDER_SPACING: Final[float] = 2.0


# Performance Tracking
# ---------------------
# Enable detailed market making performance logs
MM_ENABLE_PERFORMANCE_LOG: Final[bool] = True

# Performance log file
MM_PERFORMANCE_LOG_FILE: Final[str] = "logs/market_making_performance.jsonl"