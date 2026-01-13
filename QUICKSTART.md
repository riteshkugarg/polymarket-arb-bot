# Polymarket Bot - Quick Start Guide

## 🎯 Quick Setup (Development)

```bash
# 1. Setup environment
./scripts/run_bot.sh setup

# 2. Configure AWS credentials
export AWS_REGION=eu-central-1
export AWS_SECRET_ID=polymarket/prod/credentials

# 3. Start bot
./scripts/run_bot.sh start

# 4. Monitor
./scripts/run_bot.sh logs
```

## 📦 Project Overview

### Core Components

1. **Configuration** ([src/config/](src/config/))
   - `constants.py`: All bot parameters in one place
   - `aws_config.py`: AWS Secrets Manager integration

2. **Core Modules** ([src/core/](src/core/))
   - `polymarket_client.py`: Polymarket API wrapper
   - `order_manager.py`: Trade execution with safety checks

3. **Strategies** ([src/strategies/](src/strategies/))
   - `base_strategy.py`: Abstract base class for all strategies
   - `mirror_strategy.py`: Copy whale wallet positions

4. **Utilities** ([src/utils/](src/utils/))
   - `logger.py`: Structured logging
   - `exceptions.py`: Custom error types
   - `helpers.py`: Common utilities

### Key Constants

```python
# Mirror target whale
MIRROR_TARGET = '0x63ce342161250d705dc0b16df89036c8e5f9ba9a'

# Trading safety
ENTRY_PRICE_GUARD = 0.0005      # 0.05% price deviation limit
MIN_ORDER_USD = 10.0             # Minimum order size
MAX_SLIPPAGE_PERCENT = 0.005    # 0.5% max slippage
LOOP_INTERVAL_SEC = 15          # Check interval

# Safety limits
MAX_POSITION_SIZE_USD = 1000.0
CIRCUIT_BREAKER_LOSS_THRESHOLD_USD = 500.0
```

## 🔑 AWS Configuration Required

### Secrets Manager Setup

Create secret: `polymarket/prod/credentials`
Region: `eu-central-1`

```json
{
  "WALLET_PRIVATE_KEY": "0xYourPrivateKeyHere"
}
```

### IAM Permissions

Your EC2 instance IAM role needs:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "secretsmanager:GetSecretValue"
    ],
    "Resource": "arn:aws:secretsmanager:eu-central-1:*:secret:polymarket/prod/credentials*"
  }]
}
```

## 🧪 Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html

# Open coverage report
open htmlcov/index.html
```

## 📊 Monitoring Commands

```bash
# Bot status
./scripts/run_bot.sh status

# Health check
./scripts/health_check.sh

# Live logs
./scripts/run_bot.sh logs

# System logs (if using systemd)
journalctl -u polymarket-bot -f
```

## 🚨 Emergency Procedures

### Stop Trading Immediately

```bash
# Stop bot
./scripts/run_bot.sh stop

# Or kill process
kill -TERM $(cat logs/bot.pid)
```

### Check for Issues

```bash
# Recent errors
grep ERROR logs/polymarket_bot.log | tail -20

# Health status
./scripts/health_check.sh

# Resource usage
./scripts/run_bot.sh status
```

### Restart After Changes

```bash
# Restart bot
./scripts/run_bot.sh restart

# Verify startup
tail -f logs/polymarket_bot.log
```

## 🔧 Common Modifications

### Add New Strategy

1. Create `src/strategies/my_strategy.py`:
```python
from strategies.base_strategy import BaseStrategy

class MyStrategy(BaseStrategy):
    async def execute(self):
        # Your logic here
        pass
```

2. Add to `src/main.py`:
```python
from strategies.my_strategy import MyStrategy

# In PolymarketBot.initialize():
my_strategy = MyStrategy(self.client, self.order_manager)
self.strategies.append(my_strategy)
```

### Adjust Trading Parameters

Edit [src/config/constants.py](src/config/constants.py):
```python
MIN_ORDER_USD = 20.0  # Increase minimum order
MAX_SLIPPAGE_PERCENT = 0.01  # Allow more slippage
LOOP_INTERVAL_SEC = 30  # Check less frequently
```

### Change Log Level

```bash
# In .env file
LOG_LEVEL=debug  # For verbose logs
LOG_LEVEL=info   # For normal operation
LOG_LEVEL=error  # For errors only
```

## 📁 Important Files

| File | Purpose |
|------|---------|
| [src/config/constants.py](src/config/constants.py) | All configuration in one place |
| [src/main.py](src/main.py) | Bot entry point |
| [src/strategies/mirror_strategy.py](src/strategies/mirror_strategy.py) | Mirror trading logic |
| [.env.example](.env.example) | Environment template |
| [scripts/run_bot.sh](scripts/run_bot.sh) | Bot management |
| [tests/conftest.py](tests/conftest.py) | Test configuration |

## 💡 Tips

1. **Always test first**: Run tests before production deployment
2. **Start small**: Use low position sizes initially
3. **Monitor closely**: Watch logs for first few hours
4. **Set alerts**: Use health checks with alerting
5. **Keep updated**: Pull latest code regularly
6. **Backup logs**: Archive logs periodically

## 📞 Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot won't start | Check AWS credentials and secrets |
| No trades executing | Verify target wallet has positions |
| High slippage | Increase `MAX_SLIPPAGE_PERCENT` |
| Out of memory | Check `MAX_OPEN_POSITIONS` limit |
| Circuit breaker trips | Review `CIRCUIT_BREAKER_LOSS_THRESHOLD_USD` |

## 🎓 Learning Resources

- [Polymarket Docs](https://docs.polymarket.com)
- [py-clob-client](https://github.com/Polymarket/py-clob-client)
- Project tests: See `tests/` directory for examples
