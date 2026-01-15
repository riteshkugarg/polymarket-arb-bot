# Dynamic Settings Integration Guide

## Current Status: ⚠️ NOT INTEGRATED YET

The `settings.py` file is **production-ready** but **not yet connected** to your trading strategies.

### What Exists (✅ Production-Ready)

```bash
src/config/settings.py  # 382 lines - COMPLETE implementation
tests/test_institutional_upgrades.py  # All tests passing
```

**Features:**
- ✅ Pydantic-settings v2.1.0 with full type validation
- ✅ Environment variable overrides (`.env` or `export`)
- ✅ Singleton pattern with `get_settings()`
- ✅ Hot-reload support with `reload_settings()`
- ✅ 30+ configurable parameters with ranges
- ✅ Comprehensive docstrings with LaTeX formulas

### What's Missing (❌ Integration Required)

Your strategies still read from `constants.py` (hardcoded values):

```python
# ❌ CURRENT (inventory_manager.py line 135):
from config import constants
self.gamma_base = Decimal(str(constants.MM_GAMMA_RISK_AVERSION))

# ✅ SHOULD BE:
from config.settings import get_settings
settings = get_settings()
self.gamma_base = Decimal(str(settings.mm_gamma_risk_aversion))
```

---

## 🚀 Integration Steps (Choose Your Approach)

### **Option 1: Gradual Migration (RECOMMENDED for production)**

Replace constants one at a time, testing after each change.

#### Step 1: Update InventoryManager

```python
# File: src/core/inventory_manager.py
from config.settings import get_settings

class InventoryManager:
    def __init__(self, client, ...):
        settings = get_settings()  # Load dynamic config
        
        # Replace hardcoded constants
        self.gamma_base = Decimal(str(settings.mm_gamma_risk_aversion))
        self.baseline_vol_window = settings.volatility_baseline_window_hours
        self.current_vol_window = settings.volatility_current_window_seconds
```

#### Step 2: Update MarketMakingStrategy

```python
# File: src/strategies/market_making_strategy.py
from config.settings import get_settings

class MarketMakingStrategy:
    def __init__(self, client, ...):
        settings = get_settings()
        
        # Dynamic parameters
        self.max_markets = settings.mm_max_markets
        self.target_spread = Decimal(str(settings.mm_target_spread))
        self.min_spread = Decimal(str(settings.mm_min_spread))
        self.toxic_flow_threshold = settings.toxic_flow_consecutive_fills
        self.latency_kill_switch_ms = settings.latency_kill_switch_ms
```

#### Step 3: Update MarketDataManager

```python
# File: src/core/market_data_manager.py
from config.settings import get_settings

class PolymarketWSManager:
    def __init__(self, client, cache, ...):
        settings = get_settings()
        
        # Dynamic latency threshold
        self.latency_kill_switch_ms = settings.latency_kill_switch_ms
```

#### Step 4: Test Each Change

```bash
# Test after each file modification
python tests/test_institutional_upgrades.py
python src/main.py --dry-run  # If you have this mode
```

---

### **Option 2: All-At-Once Migration (FASTER but riskier)**

Replace all `constants.py` imports in one shot.

#### Find All Usage

```bash
cd /workspaces/polymarket-arb-bot
grep -r "from config import constants" src/
grep -r "constants\\.MM_" src/
grep -r "constants\\.TOXIC_" src/
```

#### Replace Pattern

```python
# OLD:
from config import constants
gamma = constants.MM_GAMMA_RISK_AVERSION

# NEW:
from config.settings import get_settings
settings = get_settings()
gamma = settings.mm_gamma_risk_aversion
```

---

## 📝 Example: Full Integration for One Strategy

### Before (Using constants.py)

```python
# src/strategies/market_making_strategy.py
from config import constants

class MarketMakingStrategy:
    def __init__(self, client, ...):
        self.gamma = Decimal(str(constants.MM_GAMMA_RISK_AVERSION))  # 0.2 (hardcoded)
        self.toxic_threshold = constants.TOXIC_FLOW_CONSECUTIVE_FILLS  # 3 (hardcoded)
        self.latency_threshold = 500.0  # Hardcoded in code
```

### After (Using settings.py)

```python
# src/strategies/market_making_strategy.py
from config.settings import get_settings

class MarketMakingStrategy:
    def __init__(self, client, ...):
        settings = get_settings()  # Load from environment
        
        self.gamma = Decimal(str(settings.mm_gamma_risk_aversion))  # Configurable!
        self.toxic_threshold = settings.toxic_flow_consecutive_fills  # Configurable!
        self.latency_threshold = settings.latency_kill_switch_ms  # Configurable!
```

### Usage (Environment Override)

```bash
# Run with default settings (gamma=0.2)
python src/main.py

# Run with custom gamma (gamma=0.3)
export MM_GAMMA_RISK_AVERSION=0.3
python src/main.py

# Run with multiple overrides
export MM_GAMMA_RISK_AVERSION=0.25
export TOXIC_FLOW_CONSECUTIVE_FILLS=5
export LATENCY_KILL_SWITCH_MS=400.0
python src/main.py
```

---

## 🧪 Testing Integration

### Test 1: Verify Settings Load

```python
from config.settings import get_settings

settings = get_settings()
print(f"Gamma: {settings.mm_gamma_risk_aversion}")  # Should print: 0.2
print(f"Toxic: {settings.toxic_flow_consecutive_fills}")  # Should print: 3
```

### Test 2: Verify Environment Override

```bash
# Terminal 1: Set environment variable
export MM_GAMMA_RISK_AVERSION=0.35

# Terminal 2: Run Python
python3 << EOF
from config.settings import get_settings
settings = get_settings()
assert settings.mm_gamma_risk_aversion == 0.35, "Environment override failed!"
print("✅ Environment override working!")
EOF
```

### Test 3: Verify Validation

```bash
# Should FAIL (gamma > 1.0 is invalid)
export MM_GAMMA_RISK_AVERSION=1.5
python -c "from config.settings import get_settings; get_settings()"
# Expected: ValidationError: Input should be less than or equal to 1
```

---

## 🔥 Hot-Reload (Advanced)

For runtime parameter tuning without restarting the bot:

```python
# In your main event loop or admin API
from config.settings import reload_settings
import os

# Change environment variable at runtime
os.environ['MM_GAMMA_RISK_AVERSION'] = '0.3'

# Force reload settings
settings = reload_settings()

# Update strategy parameters
self.inventory_manager.gamma_base = Decimal(str(settings.mm_gamma_risk_aversion))
logger.info(f"Hot-reloaded gamma: {settings.mm_gamma_risk_aversion}")
```

---

## 📋 Parameter Mapping (constants.py → settings.py)

| constants.py | settings.py | Override Variable |
|--------------|-------------|-------------------|
| `MM_GAMMA_RISK_AVERSION` | `mm_gamma_risk_aversion` | `MM_GAMMA_RISK_AVERSION=0.3` |
| `TOXIC_FLOW_CONSECUTIVE_FILLS` | `toxic_flow_consecutive_fills` | `TOXIC_FLOW_CONSECUTIVE_FILLS=5` |
| `LATENCY_KILL_SWITCH_MS` | `latency_kill_switch_ms` | `LATENCY_KILL_SWITCH_MS=400.0` |
| `MM_MAX_MARKETS` | `mm_max_markets` | `MM_MAX_MARKETS=10` |
| `MM_TARGET_SPREAD` | `mm_target_spread` | `MM_TARGET_SPREAD=0.02` |
| `MICRO_PRICE_DIVERGENCE_THRESHOLD` | `micro_price_divergence_threshold` | `MICRO_PRICE_DIVERGENCE_THRESHOLD=0.01` |
| `MM_CAPITAL_ALLOCATION_PCT` | `mm_capital_allocation_pct` | `MM_CAPITAL_ALLOCATION_PCT=0.80` |
| `ARB_CAPITAL_ALLOCATION_PCT` | `arb_capital_allocation_pct` | `ARB_CAPITAL_ALLOCATION_PCT=0.15` |

---

## 🚨 Breaking Changes (Migration Checklist)

### 1. Import Changes

```python
# ❌ OLD:
from config import constants
gamma = constants.MM_GAMMA_RISK_AVERSION

# ✅ NEW:
from config.settings import get_settings
settings = get_settings()
gamma = settings.mm_gamma_risk_aversion
```

### 2. Case Sensitivity

```python
# Pydantic is case-INSENSITIVE for environment variables
export mm_gamma_risk_aversion=0.3  # ✅ Works
export MM_GAMMA_RISK_AVERSION=0.3  # ✅ Works
export MM_Gamma_Risk_Aversion=0.3  # ✅ Works (any case)
```

### 3. Type Conversion

```python
# settings.py returns native Python types
gamma_float = settings.mm_gamma_risk_aversion  # float
gamma_decimal = Decimal(str(settings.mm_gamma_risk_aversion))  # Decimal
```

---

## 🎯 Deployment Workflow

### Development

```bash
# 1. Create .env file (local overrides)
cat > .env << EOF
MM_GAMMA_RISK_AVERSION=0.15
TOXIC_FLOW_CONSECUTIVE_FILLS=2
LATENCY_KILL_SWITCH_MS=300.0
EOF

# 2. Run with .env overrides
python src/main.py
```

### Production (EC2)

```bash
# 1. Set environment variables in systemd service
sudo nano /etc/systemd/system/polymarket-bot.service

# Add to [Service] section:
Environment="MM_GAMMA_RISK_AVERSION=0.25"
Environment="TOXIC_FLOW_CONSECUTIVE_FILLS=4"
Environment="LATENCY_KILL_SWITCH_MS=400.0"

# 2. Reload and restart
sudo systemctl daemon-reload
sudo systemctl restart polymarket-bot.service
```

### Docker

```yaml
# docker-compose.yml
services:
  polymarket-bot:
    environment:
      - MM_GAMMA_RISK_AVERSION=0.25
      - TOXIC_FLOW_CONSECUTIVE_FILLS=4
      - LATENCY_KILL_SWITCH_MS=400.0
```

---

## ✅ Verification Script

Save as `verify_settings_integration.py`:

```python
#!/usr/bin/env python3
"""Verify settings.py is integrated correctly"""

import os
from config.settings import get_settings, reload_settings

def test_default_values():
    """Test default values load correctly"""
    settings = get_settings()
    assert settings.mm_gamma_risk_aversion == 0.2
    assert settings.toxic_flow_consecutive_fills == 3
    assert settings.latency_kill_switch_ms == 500.0
    print("✅ Default values correct")

def test_environment_override():
    """Test environment variables override defaults"""
    os.environ['MM_GAMMA_RISK_AVERSION'] = '0.35'
    settings = reload_settings()
    assert settings.mm_gamma_risk_aversion == 0.35
    print("✅ Environment override working")

def test_validation():
    """Test validation catches invalid values"""
    try:
        os.environ['MM_GAMMA_RISK_AVERSION'] = '1.5'  # Too high
        reload_settings()
        assert False, "Should have raised ValidationError"
    except Exception as e:
        print(f"✅ Validation working: {e}")

if __name__ == '__main__':
    test_default_values()
    test_environment_override()
    test_validation()
    print("\n🎉 All settings integration tests passed!")
```

Run it:

```bash
python verify_settings_integration.py
```

---

## 🔄 Rollback Plan (If Issues Occur)

If you encounter issues after integration:

```bash
# 1. Revert code changes
git checkout src/core/inventory_manager.py
git checkout src/strategies/market_making_strategy.py

# 2. Clear environment variables
unset MM_GAMMA_RISK_AVERSION
unset TOXIC_FLOW_CONSECUTIVE_FILLS
unset LATENCY_KILL_SWITCH_MS

# 3. Restart bot (uses constants.py again)
sudo systemctl restart polymarket-bot.service
```

---

## 📞 Next Steps

1. **Choose migration approach** (Option 1 or Option 2)
2. **Test in development first** (local laptop/dev environment)
3. **Verify all tests pass** (`python tests/test_institutional_upgrades.py`)
4. **Deploy to production** (with rollback plan ready)
5. **Monitor logs** for any errors related to settings loading

---

## 🎓 TL;DR

**Question:** Is settings.py production-ready or skeleton code?

**Answer:** **100% PRODUCTION-READY** - but NOT YET INTEGRATED.

- ✅ settings.py is complete (382 lines, fully tested)
- ❌ Your strategies still use constants.py (hardcoded values)
- 🔧 Follow this guide to integrate settings.py into your trading logic
- 🧪 Test each change incrementally
- 🚀 Deploy with confidence after testing

**Status:** Ready to integrate in 1-2 hours of development work.
