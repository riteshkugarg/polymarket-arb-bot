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

from typing import Final, Dict, List
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
# 2. MULTI-STRATEGY BUDGET ALLOCATION (INSTITUTIONAL-GRADE DYNAMIC SYSTEM)
# ============================================================================
# PERCENTAGE-BASED ALLOCATION (Auto-scales with account balance)
#
# Institutional Golden Standards:
# 1. Allocate capital as % of total equity (not fixed dollars)
# 2. Used by: Jane Street, Citadel, Two Sigma, Jump Trading
# 3. Auto-scales with account growth/drawdown
# 4. Maintains risk ratios regardless of balance
# 5. Kelly Criterion optimal: 5-15% per strategy
#
# Benefits:
# - Balance = $72.92 → MM: $56.88 (78%), Arb: $14.58 (20%)
# - Balance = $500 → MM: $390 (78%), Arb: $100 (20%)
# - Balance = $5,000 → MM: $500 (capped), Arb: $200 (capped)
# - No manual recalibration needed
# ============================================================================

# Market Making: Percentage-based allocation
# INSTITUTIONAL STANDARD: 70-80% of available capital
# Rationale: Primary income generator, higher turnover = more rebates
MM_CAPITAL_ALLOCATION_PCT: Final[float] = 0.78  # 78% of balance

# Arbitrage: Percentage-based allocation
# INSTITUTIONAL STANDARD: 15-20% of available capital
# Rationale: Opportunistic strategy, rare but high-conviction
ARB_CAPITAL_ALLOCATION_PCT: Final[float] = 0.20  # 20% of balance

# Reserve: Percentage-based buffer
# INSTITUTIONAL STANDARD: 2-5% cash reserve
# Rationale: Gas fees, unexpected fills, emergency exits
RESERVE_BUFFER_PCT: Final[float] = 0.02  # 2% reserve

# Safety Caps (Hard Dollar Limits)
# Prevents over-allocation even with large balances
# INSTITUTIONAL STANDARD: Cap position size regardless of bankroll
MM_MAX_CAPITAL_CAP: Final[float] = 500.0  # Max $500 for MM (even if balance > $640)
ARB_MAX_CAPITAL_CAP: Final[float] = 200.0  # Max $200 for Arb (even if balance > $1000)

# Minimum Thresholds (Strategy Activation)
# Don't trade if insufficient capital for strategy
# INSTITUTIONAL STANDARD: Minimum viable capital per strategy
MM_MIN_CAPITAL_THRESHOLD: Final[float] = 50.0  # Need ≥$50 to enable MM
ARB_MIN_CAPITAL_THRESHOLD: Final[float] = 10.0  # Need ≥$10 to enable Arb

# DEPRECATED: Legacy constants for backward compatibility
# Use dynamic calculation: mm_capital = min(balance * 0.78, MM_MAX_CAPITAL_CAP)
ARBITRAGE_STRATEGY_CAPITAL: Final[float] = 20.0  # DEPRECATED: Use ARB_CAPITAL_ALLOCATION_PCT
MARKET_MAKING_STRATEGY_CAPITAL: Final[float] = 80.0  # DEPRECATED: Use MM_CAPITAL_ALLOCATION_PCT
STRATEGY_RESERVE_BUFFER: Final[float] = 2.92  # DEPRECATED: Use RESERVE_BUFFER_PCT

# Maximum capital utilization across all strategies (safety check)
# Set to 98% to leave small buffer for fees/gas
MAX_TOTAL_CAPITAL_UTILIZATION: Final[float] = 0.98


# ============================================================================
# 2B. ARBITRAGE FEE CONFIGURATION
# ============================================================================
# Taker fee parameters for arbitrage execution
# CRITICAL: If Polymarket moves to dynamic fees, update these constants
# Current fee tier: 1.2% (competitive with 1% tier traders)

# Taker fee percentage per trade (basis for opportunity detection)
# Current Polymarket fee: 1.0% (for tier 1) to 1.5% (for tier 0)
# INSTITUTIONAL UPGRADE: Set to actual 1.0% fee tier (aggressive)
# Previous: 1.2% (too conservative - filtered profitable trades)
ARBITRAGE_TAKER_FEE_PERCENT: Final[float] = 0.010  # 1.0% (actual fee)

# Arbitrage opportunity threshold
# INSTITUTIONAL UPGRADE: Tightened to 0.992 (~0.8% inefficiency)
# Previous: 0.98 (required 2% inefficiency - too strict)
# Rationale: Most 2026 HFT arb opportunities are 0.5%-1% range
ARBITRAGE_OPPORTUNITY_THRESHOLD: Final[float] = 0.992  # sum < 99.2 cents

# Minimum profit threshold per arbitrage execution (percentage)
# INSTITUTIONAL HFT STANDARD: 0.1% minimum (10 basis points)
# Rationale:
#   - Captures thinner institutional-grade opportunities (0.1%-0.5% spreads)
#   - Previous threshold: 0.5% (filtered profitable sub-50bps trades)
#   - 2026 HFT competition: 10-20 bps is standard minimum for arb
#   - Accounts for gas fees (~$0.50) and taker fees (1.0%)
#   - Formula: gross_profit > (trade_size * ARB_MIN_PROFIT_THRESHOLD) + gas_cost
# Note: Use Decimal('0.001') in code for precision
ARB_MIN_PROFIT_THRESHOLD: Final[float] = 0.001  # 0.1% minimum profit (10 bps)


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

# ============================================================================
# WEBSOCKET DATA STALENESS THRESHOLD
# ============================================================================
# POLYMARKET FEEDBACK (Jan 2026): WebSocket feeds have ~100ms latency
# Recommended thresholds:
#   - Market Making: 1-2 seconds (allows for brief network hiccups)
#   - Arbitrage: 500ms-1 second (immediate execution needs fresher data)
# 
# INSTITUTIONAL UPGRADE: Tightened from 5.0s → 2.0s for MM, 1.0s for Arb
# Previous 5.0s was too loose - arbitrage opportunities disappear in <1s
# 
# Used by:
#   - MarketDataManager: GlobalMarketCache staleness detection
#   - MarketMakingStrategy: Quote update circuit breaker (2.0s)
#   - ArbitrageStrategy: Pre-execution staleness check (1.0s)
#   - Cache warmup: Wait for fresh data before quoting

# Market Making staleness threshold (conservative for inventory management)
MM_DATA_STALENESS_THRESHOLD: Final[float] = 2.0  # seconds (Polymarket rec: 1-2s)

# Arbitrage staleness threshold (aggressive for opportunity capture)
ARB_DATA_STALENESS_THRESHOLD: Final[float] = 1.0  # seconds (Polymarket rec: 0.5-1s)

# Legacy constant for backward compatibility (defaults to MM threshold)
DATA_STALENESS_THRESHOLD: Final[float] = MM_DATA_STALENESS_THRESHOLD

# Maximum allowed drawdown before emergency kill switch (PERCENTAGE-BASED)
# INSTITUTIONAL HFT STANDARD: 5% of peak equity for small accounts
# Rationale:
#   - Prevents catastrophic loss from market microstructure breakdown
#   - Triggers immediate halt of all strategies (cancel orders, close positions)
#   - Institutional standard: 2-5% daily drawdown limit
#   - Formula: If (peak_equity - current_equity) > peak_equity * 0.05 → KILL_SWITCH
#   - Auto-scales: $100 account = $5 limit, $1000 account = $50 limit
# Dynamic Calculation: drawdown_limit = peak_equity * DRAWDOWN_LIMIT_PCT
DRAWDOWN_LIMIT_PCT: Final[float] = 0.05  # 5% of peak equity

# DEPRECATED: Legacy constant for backward compatibility
# Use dynamic calculation: drawdown_limit = peak_equity * DRAWDOWN_LIMIT_PCT
DRAWDOWN_LIMIT_USD: Final[float] = 5.0  # DEPRECATED: Use DRAWDOWN_LIMIT_PCT

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

# Maximum total exposure (PERCENTAGE-BASED)
# INSTITUTIONAL HFT STANDARD: 95% utilization of available capital
# Rationale:
#   - Prevents over-leveraging across all strategies
#   - Maintains 5% cash buffer for gas fees and unexpected fills
#   - Institutional standard: 90-95% utilization max
#   - Leaves buffer for emergency exits without liquidation
#   - Auto-scales: $100 account = $95 max, $1000 account = $950 max
# Dynamic Calculation: max_exposure = balance * MAX_TOTAL_EXPOSURE_PCT
MAX_TOTAL_EXPOSURE_PCT: Final[float] = 0.95  # 95% of balance

# DEPRECATED: Legacy constant for backward compatibility
# Use dynamic calculation: max_exposure = balance * MAX_TOTAL_EXPOSURE_PCT
MAX_TOTAL_EXPOSURE: Final[float] = 95.0  # DEPRECATED: Use MAX_TOTAL_EXPOSURE_PCT

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
# ADAPTIVE CAPACITY FILTERING (2026 Institutional Standard)
# Dynamic volume thresholds based on available capital allocation
# Rationale:
#   - Static thresholds ($15k/day) ignore current bankroll size
#   - Adaptive filtering ensures position size < 5% of daily volume
#   - Prevents capital fragmentation across too many markets
#   - Scales automatically as account grows

# Maximum concurrent markets (capital allocation limit)
# INSTITUTIONAL STANDARD: 5 markets max for $100 principal
# Rationale:
#   - Concentrates capital in highest-quality opportunities
#   - OPTIMIZED FOR TRADING: 2 markets = $28 each (meets $5 min order size easily)
#   - Previous: 5 markets = $11 each (struggled with minimums)
#   - Prevents over-diversification with small account
#   - Easier to monitor 2 positions vs 5+
MM_MAX_MARKETS: Final[int] = 2  # TRADING OPTIMIZATION: Focus capital on 2 best markets

# Volume multiplier for dynamic threshold calculation
# INSTITUTIONAL STANDARD: 20.0 (position size < 5% of daily volume)
# Formula: Dynamic_Min_Volume = (Balance / MM_MAX_MARKETS) * MM_VOLUME_MULTIPLIER
# Rationale:
#   - Ensures our position won't move market significantly
#   - 1 / 0.05 = 20 (inverse of 5% market impact threshold)
#   - Example: $16 position requires $320/day minimum volume (16 * 20)
#   - Prevents quoting in thin markets where we ARE the market
MM_VOLUME_MULTIPLIER: Final[float] = 20.0

# Hard floor volume threshold (absolute minimum)
# TRADING OPTIMIZATION: $500/day minimum for active markets only
# Rationale:
#   - Targets high-frequency Daily/Weekly categories (Crypto, Sports, Pop Culture)
#   - Avoids long-tail markets (2028 presidential nominations with minimal trading)
#   - $500/day = real trading activity with tight spreads
#   - Previous $1/day allowed ghost town markets
#   - Focuses capital on markets where we can actually earn spreads
MM_HARD_FLOOR_VOLUME: Final[float] = 500.0  # TRADING OPTIMIZATION: Active markets only

# Minimum liquidity depth within 2% of mid-price (USD)
# TRADING OPTIMIZATION: $500 minimum depth to target ACTIVE markets only
# Rationale:
#   - Avoids "ghost town" markets (2028 presidential nominations with 99.8% spreads)
#   - Targets markets with real trading activity and tighter spreads
#   - $500 depth = institutional-grade liquidity
#   - Focuses on Daily/Weekly categories (Crypto, Sports, Pop Culture)
#   - Previous $20 allowed dead markets through filter
# Measurement: Sum of bid/ask volume within 2 ticks of best price
MM_MIN_LIQUIDITY_DEPTH: Final[float] = 500.0  # TRADING OPTIMIZATION: Active markets only

# Minimum depth (shares) on best bid/ask
# Per Polymarket Support: "Lower to 5 shares (or even lower) with $50 capital"
# Previous: 10 shares (too strict for small markets)
MM_MIN_DEPTH_SHARES: Final[float] = 5.0

# Maximum spread to consider market liquid enough
# TRADING OPTIMIZATION: 3% max to avoid gapped markets
# Rationale:
#   - Rejects markets with spreads > 3% at filter stage (not after subscription)
#   - Avoids wasting time subscribing to 99.8% spread markets
#   - Active markets typically have 0.5%-2% spreads
#   - Previous 7% allowed too many illiquid markets through
MM_MAX_SPREAD_PERCENT: Final[float] = 0.03  # 3% max spread (TRADING OPTIMIZATION)

# Prefer binary markets (2 outcomes) for simplicity
MM_PREFER_BINARY_MARKETS: Final[bool] = True

# Target Categories for Market Making (TRADING OPTIMIZATION)
# Focus on high-volume, tight-spread categories with daily trading activity
# INSTITUTIONAL STANDARD: Target event-driven markets with natural liquidity
#
# ⚠️ POLYMARKET FEEDBACK (Q18 - Jan 2026): Use category.id instead of slug/label
# "IDs are typically more stable identifiers than human-readable slugs that might
# get updated for SEO or clarity reasons. Categories have updatedAt timestamps."
#
# CRITICAL: This constant should contain category IDs (e.g., ['cat_abc123', 'cat_def456'])
# NOT human-readable slugs/labels which can change over time!
#
# TODO: Discover category IDs using: scripts/discover_category_ids.py
MM_TARGET_CATEGORIES: Final[List[str]] = [
    # DISCOVERY REQUIRED: Category IDs must be fetched from Polymarket API
    # Current values are PLACEHOLDER slugs - will fail stability requirement
    # After discovery, replace with actual IDs:
    # 'cat_politics_id',      # Election outcomes, approval ratings, policy predictions
    # 'cat_crypto_id',        # BTC/ETH price predictions, DeFi events
    # 'cat_sports_id',        # NFL, NBA, MLB, Soccer outcomes
    # 'cat_popculture_id',    # Entertainment, Oscars, Grammys, box office
    # 'cat_business_id',      # Corporate earnings, M&A, stock prices
    # 'cat_economics_id',     # CPI, Fed rates, GDP, unemployment
]
# Note: Leave empty list [] to disable category filtering (trade all markets)
# Rationale:
#   - These categories have highest daily volume and tightest spreads
#   - Avoid long-tail categories (e.g., "2028 Presidential Nominations")
#   - Markets tagged with these categories typically have <3% spreads
#   - Real traders actively participate (not just speculators)

# Maximum number of markets to make simultaneously
# DEPRECATED: Use MM_MAX_MARKETS for capital allocation limit (5 markets)
# This constant (10) is kept for backward compatibility but should be
# replaced by MM_MAX_MARKETS in production code for proper capital allocation
# Note: Adaptive filtering uses MM_MAX_MARKETS (5) as the authoritative limit
MM_MAX_ACTIVE_MARKETS: Final[int] = 10


# Position Sizing
# ----------------
# Base position size per market (USD)
# INSTITUTIONAL STANDARD: 5-10% of capital per position
# Current: $5.00 (5% of $100 principal)
# Rationale:
#   - Allows 10-20 simultaneous positions for diversification
#   - Higher turnover potential = more rebate accumulation
#   - Reduces single-position concentration risk
MM_BASE_POSITION_SIZE: Final[float] = 5.0  # HFT institutional: $5 per quote ($100 principal / 20 positions)

# Maximum position size per market (USD)
# Institutional standard: 1.5-2x base position size
# Allows scaling into high-conviction opportunities
MM_MAX_POSITION_SIZE: Final[float] = 10.0

# Maximum inventory (shares) per outcome per market
# CALIBRATED FOR $100 PRINCIPAL: 20 shares max
# Previous: 30 shares (allows ~$15 exposure at $0.50, too high for $100)
# Rationale: Caps single-market exposure to ~$10 (20 × $0.50)
#   - Prevents one market from locking up entire budget
#   - Forces diversification across multiple markets
MM_MAX_INVENTORY_PER_OUTCOME: Final[int] = 20


# Spread Management
# ------------------
# Target spread (profit per round trip before fees)
# CALIBRATED FOR $100 PRINCIPAL: 1.5 cents (0.015)
# Previous: 0.8 cents (below 1-tick minimum, unrealistic)
# Rationale: Must be >= 1 tick ($0.01) on Polymarket grid
#   - 1.5 cents = competitive while ensuring spread profit
#   - Each round trip captures 1.5 ticks after accounting for fees
MM_TARGET_SPREAD: Final[float] = 0.015  # 1.5 cents = 1.5%

# Minimum spread (don't go tighter than this)
# CALIBRATED FOR $100 PRINCIPAL: 1 cent (0.010) - matches Polymarket tick size
MM_MIN_SPREAD: Final[float] = 0.010  # 1 cent = 1% (1 tick minimum)

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
# INSTITUTIONAL STANDARD: 10-15% of capital per day
# Current: $10.00 (10% of $100 principal)
MM_GLOBAL_DAILY_LOSS_LIMIT: Final[float] = 10.0

# Global directional exposure limit - correlation protection (USD)
# INSTITUTIONAL STANDARD: 70-80% of capital (net portfolio delta)
# Current: $70.00 (70% of $100 principal)
# Prevents excessive exposure to correlated markets
MM_MAX_TOTAL_DIRECTIONAL_EXPOSURE: Final[float] = 70.0

# External oracle price deviation threshold (percentage)
MM_ORACLE_PRICE_DEVIATION_LIMIT: Final[float] = 0.15  # 15% max deviation

# Maximum time to hold inventory before force-liquidation (seconds)
# INSTITUTIONAL HFT STANDARD: 15 minutes (900 seconds)
# Rationale:
#   - Binary prediction markets move rapidly on news/events
#   - Shorter hold time = reduced directional risk exposure
#   - Industry standard: 15-30 min for binary MM, 1-2 hours for equity MM
#   - Forces discipline: take small losses fast, avoid large drawdowns
# Previous: 30 minutes (too long for small accounts)
MM_MAX_INVENTORY_HOLD_TIME: Final[int] = 900  # 15 minutes

# Position check interval (seconds)
MM_POSITION_CHECK_INTERVAL: Final[int] = 30

# Price move threshold to trigger emergency exit (percentage)
# If price moves > 15% against position, exit immediately
MM_EMERGENCY_EXIT_THRESHOLD: Final[float] = 0.15

# Maximum directional exposure per market (USD)
# INSTITUTIONAL HFT STANDARD: 10-20% of capital per market
# Current: $15.00 (15% of $100 principal)
# Rationale:
#   - Limits single-market delta risk (correlation with other positions)
#   - Ensures portfolio remains market-neutral (sum of deltas ≈ 0)
#   - Prevents concentration risk in binary outcomes
#   - Scaled proportionally for $100 account (was $200 for $5k account)
# Formula: abs(long_exposure - short_exposure) ≤ $15
MM_MAX_DIRECTIONAL_EXPOSURE_PER_MARKET: Final[float] = 15.0

# Gamma (Risk Aversion Parameter) for Avellaneda-Stoikov inventory skew
# INSTITUTIONAL HFT STANDARD: Dynamic gamma (2026 Gold Standard)
# Rationale:
#   - Static gamma (0.50) ignores volatility regime changes
#   - Dynamic gamma adapts to market stress: γ = γ_base × (1 + σ_current/σ_baseline)
#   - Low vol: γ ≈ 0.1 (aggressive fills), High vol: γ → 0.5 (defensive)
#   - Prevents over-accumulation during volatility spikes (news events)
#   - Institutional standard: Citadel, Jane Street, Two Sigma all use dynamic risk
# Formula: spread_skew = gamma × inventory_imbalance × volatility
# Note: Also referenced as MM_INVENTORY_RISK_GAMMA for OBI integration
MM_GAMMA_RISK_AVERSION: Final[float] = 0.50  # LEGACY: Use MM_GAMMA_BASE for dynamic calculation

# Dynamic Gamma Parameters (2026 Institutional Gold Standard)
# Base gamma: Minimum risk aversion in low-volatility regimes
MM_GAMMA_BASE: Final[float] = 0.1  # Aggressive fills when σ_current ≈ σ_baseline

# Maximum gamma: Cap during extreme volatility (prevents zero liquidity)
MM_GAMMA_MAX: Final[float] = 0.5  # Conservative cap when σ_current >> σ_baseline

# Boundary risk thresholds for Bernoulli variance mode
# INSTITUTIONAL HFT STANDARD: [0.10, 0.90]
# Rationale:
#   - At p < 0.10 or p > 0.90, Bernoulli variance collapses: Var = p(1-p) → 0
#   - Triggers passive-only mode (no aggressive quoting near certainty)
#   - Prevents adverse selection when market has strong directional conviction
#   - Used by BoundaryRiskEngine in polymarket_mm.py
MM_BOUNDARY_THRESHOLD_LOW: Final[float] = 0.10
MM_BOUNDARY_THRESHOLD_HIGH: Final[float] = 0.90

# NegRisk market signature buffer (ticks)
# INSTITUTIONAL STANDARD: 2 ticks
# Rationale:
#   - Accounts for price precision errors in NegRisk CTF signature calculation
#   - Prevents rejection of valid orders due to floating-point rounding
#   - Buffer: ±2 × $0.01 = ±$0.02 tolerance
NEGRISK_BUFFER_TICKS: Final[int] = 2

# Maker fee rate in basis points (bps)
# 2026 POLYMARKET STANDARD: 0 bps (maker rebate program)
# Rationale:
#   - Post-only orders currently receive 0 bps rebate (may increase to 2-5 bps)
#   - Taker fee: 100 bps (1.0%)
#   - This constant updated when Polymarket announces rebate tier adjustments
# Note: Set to 0 until rebate program confirmed (check /fees endpoint)
FEE_RATE_BPS_MAKER: Final[int] = 0


# ============================================================================
# Z-SCORE MEAN REVERSION ALPHA OVERLAY (2026 HFT Upgrade)
# ============================================================================
# Statistical arbitrage overlay for asymmetric quoting based on price deviations

# Rolling window size for Z-Score calculation (in ticks/samples)
# INSTITUTIONAL HFT STANDARD: 20 periods (1-minute ticks = 20-minute lookback)
# Rationale:
#   - Captures short-term mean-reverting inefficiencies
#   - Longer windows (60+) = slow to adapt to regime changes
#   - Shorter windows (10-) = too noisy, false signals
Z_SCORE_LOOKBACK_PERIODS: Final[int] = 20

# Z-Score entry threshold - trigger asymmetric quoting
# INSTITUTIONAL STANDARD: 2.0 sigma (97.7% confidence in mean reversion)
# Rationale:
#   - Z > 2.0 = Overbought (price 2 std devs above mean)
#   - Z < -2.0 = Oversold (price 2 std devs below mean)
#   - Lower threshold (1.5) = more trades but higher noise
#   - Higher threshold (2.5) = fewer trades but higher conviction
Z_SCORE_ENTRY_THRESHOLD: Final[float] = 2.0

# Z-Score exit target - when to stop skewing quotes
# INSTITUTIONAL STANDARD: 0.5 sigma (mean reversion near completion)
# Rationale:
#   - Exit at Z=0.5 captures majority of mean-reversion move
#   - Prevents over-holding as price normalizes
#   - Allows exit before next oscillation begins
Z_SCORE_EXIT_TARGET: Final[float] = 0.5

# Z-Score halt threshold - extreme outlier protection
# INSTITUTIONAL STANDARD: 3.5 sigma (99.95% confidence in regime change)
# Rationale:
#   - Z > 3.5 = Potential news event, earnings surprise, or toxic flow
#   - Prevents quoting into "runaway" markets where mean may have shifted
#   - Protects against adverse selection during fundamental price discovery
#   - Resume quoting once Z returns below entry threshold
Z_SCORE_HALT_THRESHOLD: Final[float] = 3.5

# Z-Score sensitivity - reservation price adjustment factor (dollars per sigma)
# INSTITUTIONAL STANDARD: 0.005 (0.5 cents per sigma)
# Rationale:
#   - Controls magnitude of asymmetric skew applied to reservation price
#   - Higher sensitivity = more aggressive mean reversion bets
#   - Formula: Reservation_Shift = Z_Score * MM_Z_SENSITIVITY
#   - Example: Z=2.5, sensitivity=0.005 → shift $0.0125 (1.25 cents)
#   - This shift is ADDITIVE to Avellaneda-Stoikov inventory skew
MM_Z_SENSITIVITY: Final[float] = 0.005

# Z-Score update interval (seconds)
# How frequently to recalculate Z-Score from rolling window
# INSTITUTIONAL HFT STANDARD: 60 seconds (1-minute OHLCV ticks)
# Rationale:
#   - Matches typical institutional alpha refresh rate
#   - More frequent updates (10s) = unstable Z-Score (not enough price discovery)
#   - Less frequent updates (300s) = stale signal, missed opportunities
Z_SCORE_UPDATE_INTERVAL: Final[int] = 60


# ============================================================================
# ORDER BOOK IMBALANCE (OBI) - MOMENTUM OVERLAY (2026 HYBRID UPGRADE)
# ============================================================================
# Institutional-grade order flow analytics to prevent adverse selection

# OBI threshold for significant imbalance detection
# INSTITUTIONAL STANDARD: 0.6 (60% weighted toward one side)
# Formula: OBI = (Bid_Size - Ask_Size) / (Bid_Size + Ask_Size)
# Rationale:
#   - OBI > 0.6 = Heavy buying pressure (price likely to rise)
#   - OBI < -0.6 = Heavy selling pressure (price likely to fall)
#   - Used to shift reservation price by 1 tick to avoid being 'picked off'
MM_OBI_THRESHOLD: Final[float] = 0.6

# Momentum protection cooldown (seconds)
# INSTITUTIONAL STANDARD: 30 seconds
# Rationale:
#   - When Z-Score mean reversion conflicts with OBI momentum (toxic flow)
#   - Pause quoting to avoid providing liquidity to informed traders
#   - Prevents being 'run over' during news-driven price discovery
#   - Resume after 30s when initial momentum wave subsides
MM_MOMENTUM_PROTECTION_TIME: Final[int] = 30

# Inventory risk gamma (Avellaneda-Stoikov)
# INSTITUTIONAL STANDARD: 0.5 (calibrated for $100 principal)
# Rationale:
#   - Controls exponential inventory penalty: spread_skew = gamma × q × σ²
#   - Higher gamma = more aggressive inventory offload (wider spreads when skewed)
#   - For $100 accounts: 0.5 ensures inventory penalty exceeds 1-cent tick
#   - Forces offload after 2-3 fills, preventing capital lock-up
# Note: Same as MM_GAMMA_RISK_AVERSION but explicit for clarity
MM_INVENTORY_RISK_GAMMA: Final[float] = 0.5

# Convex inventory risk coefficient (2026 Institution-Grade Risk Hardening)
# INSTITUTIONAL STANDARD: 2.0 (exponential escalation factor)
# Rationale:
#   - When abs(inventory) > 0.7 × MAX_INVENTORY, apply exponential multiplier
#   - Prevents "inventory pinning" where last 20% capacity costs same as first 20%
#   - Formula: skew = base_skew × exp(COEFFICIENT × overage_ratio)
#   - Example: At 80% inventory → multiplier = exp(2.0 × 0.1/0.3) ≈ 1.95x penalty
#   - Forces aggressive unwinding before hitting hard limits
MM_CONVEX_RISK_COEFFICIENT: Final[float] = 2.0

# Toxic flow detection (Anti-Sniping Protection - 2026 HFT Standard)
# INSTITUTIONAL STANDARD: 5 fills in 10 seconds
# Rationale:
#   - Detects informed order flow attempting to drain liquidity
#   - If bot receives >5 fills in 10s without favorable price movement → PAUSE
#   - Triggers 30-second cooldown to avoid being "picked off" during news events
#   - Protects against coordinated sniping attacks
MM_TOXIC_VELOCITY_THRESHOLD: Final[int] = 5  # Max fills in 10s window
MM_TOXIC_FLOW_WINDOW: Final[int] = 10  # Time window in seconds
MM_TOXIC_FLOW_COOLDOWN: Final[int] = 30  # Pause duration in seconds

# EWMA volatility calculation (RiskMetrics Standard)
# INSTITUTIONAL STANDARD: 0.94 (94% decay factor)
# Rationale:
#   - Exponentially Weighted Moving Average reacts faster than simple rolling std dev
#   - Lambda = 0.94 gives ~75% weight to last 20 observations (industry standard)
#   - Allows bot to "feel" volatility spikes instantly and widen spreads
#   - Formula: EWMA(t) = λ × EWMA(t-1) + (1-λ) × return²(t)
MM_VOL_DECAY_LAMBDA: Final[float] = 0.94

# Minimum tick size (Polymarket standard)
# INSTITUTIONAL STANDARD: 0.01 (1 cent minimum)
# Rationale:
#   - Polymarket's minimum price increment
#   - Used for zero-cross spread protection
#   - Prevents bid >= ask in extreme skew scenarios
MM_MIN_TICK_SIZE: Final[float] = 0.01


# Order Management
# -----------------
# Quote update frequency (seconds)
# INSTITUTIONAL HFT STANDARD: 0.5-second refresh (2026 Gold Standard)
# Rationale:
#   - Sub-second refresh minimizes stale quote risk during news events
#   - 2026 WebSocket architecture supports 500ms refresh without overhead
#   - Competitive with Tier-1 institutional MM firms (Jane Street, Citadel)
#   - Enables rapid inventory rebalancing after toxic flow detection
#   - Reduces adverse selection window from 1s to 500ms
# Previous: 1s (2025 standard), 3s (too slow for HFT), 20s (legacy polling)
MM_QUOTE_UPDATE_INTERVAL: Final[float] = 0.5  # 2026 Institutional Gold: 500ms quote refresh

# Order time-to-live (seconds)
# INSTITUTIONAL HFT STANDARD: 25-second TTL (2026 Gold Standard)
# Rationale:
#   - Aggressive TTL protects against toxic flow and stale quote sniping
#   - Forces rapid re-evaluation of mid-price and volatility during news events
#   - 25s balances adverse selection protection vs API rate limit usage
#   - Tier-1 firms (Citadel, Jane Street) use 15-30s TTL for binary markets
#   - Shorter TTL = higher cancellation rate but lower pick-off risk
# Previous: 45s (2025 standard), 60s (legacy conservative setting)
# Note: Requires 10 req/sec capacity (1 cancel + 1 replace per 2.5 quotes)
MM_ORDER_TTL: Final[int] = 25  # 2026 Institutional Gold: 25 second order TTL

# Minimum time between order placements (LEGACY - migrated to token-bucket)
# INSTITUTIONAL UPGRADE: Replaced with TokenBucketRateLimiter (2026 Gold Standard)
# 
# Previous Implementation:
#   Static sleep timer: Wait MM_MIN_ORDER_SPACING seconds between requests
#   Problem: Rigid timing, cannot utilize burst capacity
#
# New Implementation:
#   Token-bucket algorithm: 10 req/sec sustained, 20 req burst capacity
#   Benefits:
#     - Allows rapid order replacement (up to 20 requests instantly)
#     - Smoother rate limiting (no fixed delays)
#     - Better API utilization (fills traffic "holes")
#     - Industry standard (Jane Street, Citadel, Two Sigma)
#
# Configuration (see utils/rate_limiter.py):
#   ORDER_PLACEMENT_RATE_LIMITER(rate=10.0, capacity=20.0)
#
# Note: This constant kept for backward compatibility only
MM_MIN_ORDER_SPACING: Final[float] = 2.0  # DEPRECATED: Use TokenBucketRateLimiter


# Performance Tracking
# ---------------------
# Enable detailed market making performance logs
MM_ENABLE_PERFORMANCE_LOG: Final[bool] = True

# Performance log file
MM_PERFORMANCE_LOG_FILE: Final[str] = "logs/market_making_performance.jsonl"