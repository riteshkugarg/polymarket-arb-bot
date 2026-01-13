# Polymarket Arbitrage Bot

**Production-grade bot for exploiting Polymarket inefficiencies** using mirror trading and arbitrage strategies. Built for 24/7 AWS EC2 operation with enterprise-class reliability, monitoring, and safety controls.

## 🎯 Features

### Core Trading
- **Mirror Trading Strategy**: Automatically replicate trades from whale wallets
- **Multiple Strategies**: Extensible architecture supports mirror, arbitrage, and custom strategies
- **L2 Authentication**: High-rate-limit endpoints (3500 req/10s vs 500 req/10s)
- **FOK Orders**: Fill-Or-Kill execution with smart retry logic
- **Balance-Based Trading**: Continuously trade based on available USDC

### Production Reliability
- **24/7 Operation**: Systemd service with auto-restart and graceful shutdown
- **Circuit Breaker**: Automatic trading halt on losses exceeding threshold
- **Error Recovery**: Exponential backoff for transient failures
- **Health Checks**: Continuous monitoring with alerting
- **Structured Logging**: JSON logs for aggregation and analysis
- **AWS Integration**: Secure credential management via Secrets Manager

### Safety & Risk Management
- **Price Guards**: Prevent buying at significantly worse prices
- **Slippage Protection**: Validate execution prices
- **Position Limits**: Configurable maximum position sizes
- **Daily Volume Limits**: Prevent runaway trading
- **Entry Time Filtering**: Only mirror recent whale positions
- **Dust Thresholds**: Ignore insignificant positions

### Enterprise Architecture
- **Centralized Configuration**: All constants in one place with clear documentation
- **Comprehensive Logging**: Debug, info, warning, error levels with context
- **Security Validators**: Address validation, order parameter checking
- **Async/Await**: High-performance async I/O
- **Type Hints**: Full Python type annotations for IDE support
- **Clean Code**: Meaningful comments, DRY principles, separation of concerns

## 📋 Quick Start

### Prerequisites
- Python 3.10+
- AWS Account with EC2 + Secrets Manager
- Polymarket wallet with USDC balance

### Local Development (5 minutes)

```bash
# Clone the repo
git clone https://github.com/riteshkugarg/polymarket-arb-bot.git
cd polymarket-arb-bot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your values (use AWS Secrets Manager in production)

# Run tests
pytest tests/

# Start bot (polling mode)
python -m src.main
```

### Production AWS EC2 Deployment (15 minutes)

See [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) for comprehensive AWS setup guide including:
- EC2 instance configuration
- IAM roles and Secrets Manager setup
- Systemd service installation
- Monitoring and alerting
- Emergency procedures
- Cost optimization

**TL;DR:**
```bash
# On EC2 instance
sudo bash scripts/deploy_ec2.sh
sudo systemctl start polymarket-bot
sudo systemctl status polymarket-bot
```

## 🏗️ Project Structure

```
polymarket-arb-bot/
├── src/                           # Main application code
│   ├── main.py                    # Bot entry point with lifecycle management
│   ├── config/
│   │   ├── constants.py           # ALL configuration in one place ⭐
│   │   └── aws_config.py          # AWS Secrets Manager integration
│   ├── core/
│   │   ├── polymarket_client.py   # Polymarket API client
│   │   ├── order_manager.py       # Order execution & risk management
│   │   └── whale_ws_listener.py   # WebSocket listener for whale tracking
│   ├── strategies/
│   │   ├── base_strategy.py       # Abstract strategy interface
│   │   └── mirror_strategy.py     # Mirror trading implementation
│   └── utils/
│       ├── logger.py              # Production logging with rotation ⭐
│       ├── exceptions.py          # Custom exception hierarchy ⭐
│       └── helpers.py             # Security validators & utilities ⭐
├── tests/                         # Comprehensive test suite
│   ├── conftest.py               # Pytest fixtures
│   ├── test_config.py
│   ├── test_mirror_strategy.py
│   └── test_polymarket_client.py
├── scripts/
│   ├── deploy_ec2.sh            # AWS deployment automation
│   ├── health_check.sh          # Health monitoring
│   └── polymarket-bot.service   # Systemd service file
├── requirements.txt             # Python dependencies
├── setup.py                     # Package configuration
├── PRODUCTION_DEPLOYMENT.md     # AWS deployment guide ⭐
└── README.md                    # This file
```

**Key Production-Grade Files (⭐):**
- `config/constants.py` - Centralized configuration with 1000+ lines of documentation
- `utils/logger.py` - Production logging with rotating files and JSON formatting
- `utils/exceptions.py` - Custom exception hierarchy for precise error handling
- `utils/helpers.py` - Security validators (address, price, order, slippage checks)
- `PRODUCTION_DEPLOYMENT.md` - Complete AWS EC2 deployment and operations guide

## ⚙️ Configuration

All configuration is centralized in [src/config/constants.py](src/config/constants.py). Environment variables override defaults.

### Key Parameters

```python
# Trading
MIRROR_TARGET = "0x63ce342161250d705dc0b16df89036c8e5f9ba9a"  # Whale to mirror
PROXY_WALLET_ADDRESS = "0x5967c88F93f202D595B9A47496b53E28cD61F4C3"  # Your trading address
MAX_ORDER_USD = 1.0  # Max order size
ENTRY_PRICE_GUARD = 0.0005  # Don't buy >0.05% worse than whale

# Safety
ENABLE_CIRCUIT_BREAKER = True  # Stop on large losses
CIRCUIT_BREAKER_LOSS_THRESHOLD_USD = 25.0  # Loss limit (USD)
MAX_POSITION_SIZE_USD = 50.0  # Max per market
MAX_DAILY_VOLUME_USD = 10000.0  # Daily limit

# Operational
LOOP_INTERVAL_SEC = 2  # How often to check for opportunities
USE_WEBSOCKET_DETECTION = False  # WebSocket vs polling
ENABLE_TIME_BASED_FILTERING = True  # Only recent whale positions

# AWS
AWS_REGION = "eu-west-1"  # Ireland (per Polymarket support)
AWS_SECRET_ID = "polymarket/prod/credentials"  # Secrets Manager
LOG_LEVEL = "INFO"  # Debug level
LOG_FILE_PATH = "logs/polymarket_bot.log"  # Log location
```

All constants include detailed comments explaining purpose and impact. See [src/config/constants.py](src/config/constants.py) for full reference.

## 🚀 Running the Bot

### Development Mode (Console Output)
```bash
# Logging to console + file, local mode
python -m src.main --log-level DEBUG
```

### Production Mode (Systemd Service)
```bash
# Start service (auto-starts on boot)
sudo systemctl start polymarket-bot

# View logs
sudo journalctl -u polymarket-bot -f

# Stop service (graceful shutdown)
sudo systemctl stop polymarket-bot

# Check status
sudo systemctl status polymarket-bot
```

### Monitoring
```bash
# Real-time logs
tail -f /var/log/polymarket-bot/bot.log

# Check for errors
grep ERROR /var/log/polymarket-bot/bot.log

# Health check
curl http://localhost:8080/health  # Future: health endpoint

# Performance metrics
python scripts/analyze_performance.py
```

## 🧪 Testing

```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_mirror_strategy.py -v

# Run with coverage
pytest tests/ --cov=src --cov-report=html

# Run specific test
pytest tests/test_mirror_strategy.py::test_mirror_entry -v
```

Tests include:
- Configuration validation
- Mirror strategy logic
- Polymarket API client
- Order execution
- Error handling
- Fixtures for mocking

## 📊 Architecture Decisions

### Why These Patterns?

| Component | Why | Benefit |
|-----------|-----|---------|
| Centralized Constants | Single source of truth | Easy updates, no magic strings |
| Custom Exceptions | Precise error handling | Specific recovery strategies |
| Async/Await | High I/O efficiency | Trade higher latency opportunities |
| Structured Logging | Machine-readable logs | Log aggregation, automated alerts |
| Strategy Pattern | Easy to extend | Add arbitrage, grid, etc. later |
| Systemd Service | Standard Linux approach | Auto-restart, standard monitoring |
| Circuit Breaker | Financial risk management | Stop on large losses |
| Validators | Fail fast with clear errors | Debug issues quickly |

### Scalability

- **Performance**: 2-5s loop interval supports 30-50 markets
- **Memory**: ~150MB base + ~10MB per 100 open positions
- **Network**: ~100 API calls/min (well below rate limits)
- **Future**: Sharded strategies across multiple instances

See [PERFORMANCE_ANALYSIS.md](PERFORMANCE_ANALYSIS.md) for benchmarks.

## 🔒 Security

### Authentication
- Private keys stored in AWS Secrets Manager (never in code)
- L2 API credentials with HMAC signing
- IAM roles for AWS access

### Network
- AWS VPC for isolated network
- HTTPS for all external APIs
- Optional VPN/bastion host for SSH

### Operation
- Non-root user (`polybot`) runs service
- Restricted file permissions on .env
- Comprehensive audit logging
- Circuit breaker prevents large losses

See [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) #7 for security best practices.

## 📈 Monitoring & Observability

### Logging

Structured JSON logs to [/var/log/polymarket-bot/bot.log](logs/):
```json
{
  "timestamp": "2026-01-13T10:30:45.123Z",
  "level": "INFO",
  "logger": "strategies.mirror_strategy",
  "message": "Order placed",
  "order_id": "123",
  "market": "YES",
  "size": 1.0,
  "price": 0.45,
  "side": "BUY"
}
```

### Health Checks
- Every 60 seconds: API connectivity, USDC balance, position count
- Alerts on: Circuit breaker, missing trades, consecutive errors
- Auto-recovery: Exponential backoff on transient failures

### Metrics (Future)
- Trading volume (orders/hour)
- Win rate (profitable vs losing trades)
- PnL (realized + unrealized)
- API performance (response times)

## 🤝 Contributing

1. Create feature branch: `git checkout -b feature/my-strategy`
2. Add tests: `pytest tests/`
3. Follow code style: Black, isort, pylint
4. Update docs: Comments for complex logic
5. Submit PR: Describe feature and test coverage

## 📞 Support

### Troubleshooting

| Issue | Solution |
|-------|----------|
| Bot won't start | Check logs: `journalctl -u polymarket-bot` |
| No trades | Increase `LOOP_INTERVAL_SEC`, check whale activity |
| High API errors | Reduce trading frequency, check rate limits |
| Circuit breaker triggered | Review trades, adjust loss threshold |
| Out of memory | Restart service, check for leaks in logs |

See [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) #9 for full troubleshooting guide.

### Resources

- **Polymarket Docs**: https://docs.polymarket.com/
- **py-clob-client**: https://github.com/polymarket/py-clob-client
- **Web3.py**: https://web3py.readthedocs.io/
- **AWS EC2**: https://docs.aws.amazon.com/ec2/

## 📄 License

MIT License - See LICENSE file

## 🙏 Acknowledgments

- Polymarket for providing trading infrastructure
- py-clob-client team for Polymarket SDK
- Web3.py team for Ethereum tools
- Open source community for excellent libraries

---

**Ready to deploy?** See [PRODUCTION_DEPLOYMENT.md](PRODUCTION_DEPLOYMENT.md) for AWS setup guide.

**Questions?** Review [src/config/constants.py](src/config/constants.py) for detailed documentation on every parameter.

## 🔧 Installation

### Local Development

```bash
# Clone repository
git clone <repository-url>
cd polymarket-arb-bot

# Create virtual environment
python3.10 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements-dev.txt

# Copy environment template
cp .env.example .env
# Edit .env with your configuration
```

### AWS EC2 Deployment

```bash
# SSH into EC2 instance
ssh ubuntu@<ec2-ip-address>

# Clone repository
git clone <repository-url>
cd polymarket-arb-bot

# Run deployment script
chmod +x scripts/*.sh
./scripts/deploy_ec2.sh
```

## ⚙️ Configuration

### AWS Secrets Manager

Create a secret in AWS Secrets Manager with the following structure:

```json
{
  "WALLET_PRIVATE_KEY": "0x...",
  "POLY_API_KEY": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "POLY_API_SECRET": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "POLY_API_PASS": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
}
```

**Required Credentials:**
- `WALLET_PRIVATE_KEY`: Your MetaMask private key (L1 auth for signing)
- `POLY_API_KEY`: L2 API key for order posting (get from Polymarket)
- `POLY_API_SECRET`: L2 API secret for HMAC signing
- `POLY_API_PASS`: L2 API passphrase

**How to Get L2 Credentials:**
1. Visit Polymarket developer portal or use their API
2. Generate API credentials for your wallet
3. Store securely in AWS Secrets Manager
4. Each wallet can only have ONE active set of credentials

Secret ID: `polymarket/prod/credentials`
Region: `eu-central-1`

### Environment Variables

Create a `.env` file (or use environment variables):

```bash
ENVIRONMENT=production
AWS_REGION=eu-central-1
AWS_SECRET_ID=polymarket/prod/credentials
LOG_LEVEL=info
LOG_FILE_PATH=logs/polymarket_bot.log
```

### Constants Configuration

Edit [src/config/constants.py](src/config/constants.py) to customize:

- Trading parameters (order sizes, slippage limits)
- Strategy configuration
- Safety limits and circuit breakers
- Monitoring settings

## 🚀 Usage

### Using Management Script

```bash
# Setup environment
./scripts/run_bot.sh setup

# Start bot
./scripts/run_bot.sh start

# Check status
./scripts/run_bot.sh status

# View logs
./scripts/run_bot.sh logs

# Stop bot
./scripts/run_bot.sh stop

# Restart bot
./scripts/run_bot.sh restart
```

### Using Systemd (Production)

```bash
# Start service
sudo systemctl start polymarket-bot

# Enable auto-start on boot
sudo systemctl enable polymarket-bot

# Check status
sudo systemctl status polymarket-bot

# View logs
journalctl -u polymarket-bot -f
```

### Direct Python Execution

```bash
cd src
python -m main
```

## 🧪 Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html

# Run specific test file
pytest tests/test_polymarket_client.py

# Run with verbose output
pytest -v

# Run only unit tests
pytest -m unit
```

## 📊 Monitoring

### Health Checks

```bash
# Manual health check
./scripts/health_check.sh

# Automated health checks run every 5 minutes via cron
```

### Log Files

- **Application logs**: `logs/polymarket_bot.log` (JSON structured)
- **Stdout/Stderr**: `logs/bot_stdout.log`, `logs/bot_stderr.log`
- **Health checks**: `logs/health_check.log`

### Metrics

The bot logs performance metrics including:
- Trade execution details
- Slippage measurements
- Daily volume statistics
- Error rates and types

## 🎯 Strategies

### Mirror Strategy

Copies positions from a target whale wallet.

**Configuration** ([src/config/constants.py](src/config/constants.py)):
```python
MIRROR_STRATEGY_CONFIG = {
    'enabled': True,
    'check_interval_sec': 15,
    'position_size_multiplier': 1.0,
    'max_markets': 10,
    'entry_delay_sec': 5,
}
```

### Adding New Strategies

1. Create new strategy class inheriting from `BaseStrategy`
2. Implement required methods: `execute()`, `analyze_opportunity()`, `should_execute_trade()`
3. Add strategy to [main.py](src/main.py) initialization
4. Add tests in `tests/`

Example:
```python
from strategies.base_strategy import BaseStrategy

class MyStrategy(BaseStrategy):
    async def execute(self):
        # Strategy logic
        pass
    
    async def analyze_opportunity(self):
        # Opportunity detection
        pass
    
    async def should_execute_trade(self, opportunity):
        # Trade validation
        pass
```

## 🔒 Security

- ✅ Private keys stored in AWS Secrets Manager
- ✅ No credentials in code or logs
- ✅ IAM role-based access for EC2
- ✅ Principle of least privilege
- ✅ Encrypted logs and backups
- ✅ Input validation and sanitization
- ✅ Rate limiting and circuit breakers

## 🛡️ Safety Features

- **Price Guards**: Only trade if price is within acceptable range
- **Slippage Protection**: Reject trades with excessive slippage
- **Position Limits**: Maximum position sizes per market
- **Daily Volume Limits**: Cap daily trading volume
- **Circuit Breaker**: Auto-stop on excessive losses
- **Dust Threshold**: Ignore insignificant positions
- **Health Checks**: Continuous monitoring

## 📈 Performance

- Asynchronous architecture for high throughput
- Connection pooling for API efficiency
- Exponential backoff retry logic
- Optimized for 24/7 operation
- Low latency trade execution

## 🐛 Troubleshooting

### Common Errors

**L2_AUTH_UNAVAILABLE**
- Missing or invalid L2 API credentials in Secrets Manager
- Solution: Add `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASS` to secrets

**FOK_ORDER_NOT_FILLED_ERROR**
- FOK order couldn't find immediate match (normal behavior)
- Bot will retry on next cycle - not a critical error
- Check logs for: "⏸️ FOK order not filled. Will retry."

**INVALID_ORDER_NOT_ENOUGH_BALANCE**
- Insufficient USDC or token balance
- Solution: Deposit more USDC to proxy wallet

**MARKET_NOT_READY**
- Market not yet accepting orders
- Bot will skip temporarily and retry later

### Bot won't start

```bash
# Check logs
./scripts/run_bot.sh logs

# Verify AWS credentials
aws sts get-caller-identity

# Check Secrets Manager access
aws secretsmanager get-secret-value --secret-id polymarket/prod/credentials
```

### High error rate

```bash
# Check health
./scripts/health_check.sh

# Review error logs
grep ERROR logs/polymarket_bot.log | tail -50

# Check system resources
./scripts/run_bot.sh status
```

### Circuit breaker triggered

1. Check total PnL in logs
2. Review recent trades
3. Verify market conditions
4. Adjust `CIRCUIT_BREAKER_LOSS_THRESHOLD_USD` if needed
5. Restart bot after investigation

## 📝 Development

### Code Style

```bash
# Format code
black src/ tests/

# Sort imports
isort src/ tests/

# Lint
flake8 src/ tests/
pylint src/
```

### Type Checking

```bash
mypy src/
```

### Security Scan

```bash
bandit -r src/
safety check
```

## ⚠️ Disclaimer

This bot is for educational purposes. Trading cryptocurrencies involves substantial risk of loss. Use at your own risk. The developers assume no responsibility for financial losses.

## 🔄 Version History

- **1.0.0** (2026-01-11): Initial release
  - Mirror trading strategy
  - AWS integration
  - Production-ready deployment
  - Comprehensive test suite
