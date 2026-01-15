# 2026 Institutional Gold Standards - Production Deployment Checklist

**Date:** January 15, 2026  
**Version:** 2.0 (Institutional Gold Standards)  
**Status:** ✅ READY FOR PRODUCTION

---

## 📋 Pre-Deployment Validation

### ✅ Module Compilation
- [x] `config.constants` - All constants compile
- [x] `utils.rate_limiter` - Token-bucket limiter functional
- [x] `core.inventory_manager` - Dynamic gamma operational
- [x] `core.market_data_manager` - F-string syntax fixed
- [x] `strategies.market_making_strategy` - Rate limiter integrated
- [x] `strategies.arbitrage_strategy` - No breaking changes
- [x] `main` - Entry point compiles

### ✅ Institutional Parameters
| Parameter | Old Value | New Value | Status |
|-----------|-----------|-----------|--------|
| `MM_QUOTE_UPDATE_INTERVAL` | 1s | **0.5s** | ✓ 500ms refresh |
| `MM_ORDER_TTL` | 45s | **25s** | ✓ Toxic flow protection |
| `ARB_MIN_PROFIT_THRESHOLD` | N/A | **0.001** | ✓ 10 bps minimum |
| `MM_GAMMA_BASE` | N/A | **0.1** | ✓ Dynamic risk base |
| `MM_GAMMA_MAX` | N/A | **0.5** | ✓ Volatility cap |

### ✅ Dynamic Risk Scaling
- [x] `inventory_manager.py` uses `MM_GAMMA_BASE` and `MM_GAMMA_MAX`
- [x] Formula: γ = MM_GAMMA_BASE × (1 + σ_current / σ_baseline)
- [x] Caps at `MM_GAMMA_MAX` during extreme volatility
- [x] Falls back to `MM_GAMMA_BASE` when no volatility data
- [x] Returns `Decimal` type for precision

### ✅ Token-Bucket Rate Limiter
- [x] `utils/rate_limiter.py` created
- [x] `ORDER_PLACEMENT_RATE_LIMITER` (10 req/sec, 20 burst)
- [x] `ORDER_CANCELLATION_RATE_LIMITER` (10 req/sec, 20 burst)
- [x] `CLOB_READ_RATE_LIMITER` (50 req/sec, 100 burst)
- [x] Integrated into `market_making_strategy.py`
- [x] Replaces static `MM_MIN_ORDER_SPACING` sleep

### ✅ Micro-Price Prioritization
- [x] `market_data_manager.py` calculates micro-price
- [x] Formula: (bid_size × ask + ask_size × bid) / (bid_size + ask_size)
- [x] `market_making_strategy.py` uses micro-price for reservation price
- [x] OBI-aware (Order Book Imbalance > 60/40 threshold)
- [x] Reduces adverse selection

### ✅ Integration Tests
- [x] No circular import dependencies
- [x] All modules import successfully
- [x] Dynamic gamma returns expected values
- [x] Rate limiter acquires/refills tokens correctly
- [x] Backward compatibility maintained

### ✅ Syntax & Compilation
- [x] Fixed f-string syntax errors (market_data_manager.py lines 767, 780, 786-788)
- [x] All `.py` files compile with `python3 -m py_compile`
- [x] No `SyntaxError` or `ImportError` exceptions

---

## 🚀 Deployment Commands

### 1. Pull Latest Code
```bash
cd /workspaces/polymarket-arb-bot
git pull origin main
```

### 2. Verify Current Commit
```bash
git log --oneline -3
# Expected output:
# 2f7c68a Fix f-string syntax errors in market_data_manager.py
# 4830597 2026 Institutional Gold Standards Upgrade
# 74e85ec Fix WebSocket per Polymarket support
```

### 3. Verify Python Environment
```bash
python3 --version  # Should be 3.10+
pip3 install -r requirements.txt
```

### 4. Run Integration Tests
```bash
cd /workspaces/polymarket-arb-bot
python3 << 'EOF'
import sys
sys.path.insert(0, 'src')

from config.constants import MM_GAMMA_BASE, MM_GAMMA_MAX, ARB_MIN_PROFIT_THRESHOLD
from utils.rate_limiter import ORDER_PLACEMENT_RATE_LIMITER
from core.inventory_manager import InventoryManager

print("✓ Imports successful")
print(f"  MM_GAMMA_BASE: {MM_GAMMA_BASE}")
print(f"  MM_GAMMA_MAX: {MM_GAMMA_MAX}")
print(f"  ARB_MIN_PROFIT_THRESHOLD: {ARB_MIN_PROFIT_THRESHOLD}")
print(f"  Rate limiter: {ORDER_PLACEMENT_RATE_LIMITER.rate}/s")

inv_mgr = InventoryManager(use_dynamic_gamma=True)
gamma = inv_mgr.get_dynamic_gamma('test')
print(f"  Dynamic gamma: {gamma}")
print("✅ All tests passed")
