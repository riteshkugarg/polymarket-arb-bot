# 🤖 Polymarket Whale Mirror Bot - Complete Deployment Guide

> **Last Updated:** January 11, 2026  
> **AWS Region:** eu-west-1 (Ireland)  
> **Python Version:** 3.12.3  
> **Bot Type:** 24/7 Systemd Service

---

## 📋 TABLE OF CONTENTS

1. [One-Time Initial Setup](#one-time-initial-setup)
2. [Regular Deployment & Updates](#regular-deployment--updates)
3. [Bot Management](#bot-management)
4. [Monitoring & Logs](#monitoring--logs)
5. [Troubleshooting Commands](#troubleshooting-commands)
6. [Emergency Commands](#emergency-commands)
7. [Configuration Quick Reference](#configuration-quick-reference)

---

## 🎬 ONE-TIME INITIAL SETUP
*Run these commands ONLY on first EC2 instance setup*

```bash
# ============================================================
# STEP 1: Install System Dependencies (Ubuntu)
# ============================================================
# Run ONCE per EC2 instance
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip git build-essential

# ============================================================
# STEP 2: Configure AWS Credentials (ONE-TIME)
# ============================================================
# Verify IAM Role is attached to EC2 instance
aws sts get-caller-identity

# Should show:
# {
#     "UserId": "AROAXXXXXXXXXXXXXXXXX:i-xxxxx",
#     "Account": "123456789012",
#     "Arn": "arn:aws:sts::123456789012:assumed-role/PolymarketBotRole/i-xxxxx"
# }

# ============================================================
# STEP 3: Verify AWS Secrets Manager Access (ONE-TIME)
# ============================================================
# Test secrets retrieval (ensure IAM role has correct permissions)
aws secretsmanager get-secret-value \
    --secret-id polymarket/prod/credentials \
    --region eu-west-1 \
    --query 'SecretString' \
    --output text | jq '.'

# Should show:
# {
#   "WALLET_PRIVATE_KEY": "0x...",
#   "POLY_API_KEY": "...",
#   "POLY_API_SECRET": "...",
#   "POLY_API_PASS": "..."
# }

# ============================================================
# STEP 4: Install Systemd Service (ONE-TIME)
# ============================================================
# This will be copied during initial deployment, but good to verify
# Service file location: /etc/systemd/system/polymarket-bot.service
sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot

# Verify service is enabled for auto-start on boot
sudo systemctl is-enabled polymarket-bot
# Should output: enabled
```

---

## 🚀 REGULAR DEPLOYMENT & UPDATES
*Run these commands for initial deployment AND every code update*

```bash
# ============================================================
# STEP 1: Clean & Clone Repository
# ============================================================
cd ~
rm -rf polymarket-arb-bot  # Remove old version
git clone https://github.com/riteshkumargarg/polymarket-arb-bot.git
cd polymarket-arb-bot

# ============================================================
# STEP 2: Setup Python Virtual Environment
# ============================================================
# Deactivate existing venv if active
deactivate 2>/dev/null || true

# Remove old venv (ensures clean dependencies)
rm -rf venv

# Create fresh virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# ============================================================
# STEP 3: Install Python Dependencies
# ============================================================
pip install --upgrade pip
pip install -r requirements.txt

# ============================================================
# STEP 4: Create Required Directories
# ============================================================
mkdir -p logs

# ============================================================
# STEP 5: Verify Configuration
# ============================================================
# Set Python path
export PYTHONPATH=/home/ubuntu/polymarket-arb-bot/src:$PYTHONPATH

# Verify bot configuration
python -c "from config.constants import MIRROR_STRATEGY_CONFIG, MIN_ORDER_USD, AWS_REGION; \
print(f'✓ AWS Region: {AWS_REGION}'); \
print(f'✓ Fixed Order Size: \${MIRROR_STRATEGY_CONFIG[\"fixed_order_size_usd\"]} USDC'); \
print(f'✓ Min Order Size: \${MIN_ORDER_USD} USDC'); \
print(f'✓ Max Markets: {MIRROR_STRATEGY_CONFIG.get(\"max_markets_to_track\", \"unlimited\")}'); \
print('✓ Configuration OK')"

# ============================================================
# STEP 6: Install/Update Systemd Service
# ============================================================
# Copy service file to systemd directory
sudo cp scripts/polymarket-bot.service /etc/systemd/system/

# Reload systemd to recognize changes
sudo systemctl daemon-reload

# ============================================================
# STEP 7: Start/Restart Bot
# ============================================================
# Restart bot (automatically loads new code)
sudo systemctl restart polymarket-bot

# Wait 3 seconds for startup
sleep 3

# Verify bot started successfully
sudo systemctl status polymarket-bot
```

---

## 🎮 BOT MANAGEMENT
*Day-to-day operational commands*

```bash
# ============================================================
# Start Bot
# ============================================================
sudo systemctl start polymarket-bot

# ============================================================
# Stop Bot
# ============================================================
sudo systemctl stop polymarket-bot

# ============================================================
# Restart Bot (apply configuration changes)
# ============================================================
sudo systemctl restart polymarket-bot

# ============================================================
# Check Bot Status
# ============================================================
sudo systemctl status polymarket-bot

# Expected output when running:
# ● polymarket-bot.service - Polymarket Arbitrage Bot
#    Loaded: loaded (/etc/systemd/system/polymarket-bot.service; enabled)
#    Active: active (running) since [timestamp]
#    Main PID: 12345

# ============================================================
# Reload Systemd Configuration (after editing .service file)
# ============================================================
sudo systemctl daemon-reload
sudo systemctl restart polymarket-bot
```

---

## 📊 MONITORING & LOGS
*Commands to monitor bot activity*

```bash
# ============================================================
# Live Logs (Follow Mode - Best for Real-Time Monitoring)
# ============================================================
# Watch logs in real-time (Ctrl+C to exit)
sudo journalctl -u polymarket-bot -f

# With color coding and better formatting
sudo journalctl -u polymarket-bot -f --output=short-precise

# ============================================================
# Recent Logs
# ============================================================
# Last 100 log lines
sudo journalctl -u polymarket-bot -n 100

# Last 50 lines with no pager (all at once)
sudo journalctl -u polymarket-bot -n 50 --no-pager

# ============================================================
# Time-Based Logs
# ============================================================
# Today's logs only
sudo journalctl -u polymarket-bot --since today

# Last hour
sudo journalctl -u polymarket-bot --since "1 hour ago"

# Last 30 minutes
sudo journalctl -u polymarket-bot --since "30 minutes ago"

# Specific date
sudo journalctl -u polymarket-bot --since "2026-01-11 00:00:00"

# Date range
sudo journalctl -u polymarket-bot --since "2026-01-11" --until "2026-01-12"

# ============================================================
# Application Log File (if exists)
# ============================================================
# Follow application log file
tail -f ~/polymarket-arb-bot/logs/polymarket_bot.log

# Last 100 lines
tail -n 100 ~/polymarket-arb-bot/logs/polymarket_bot.log

# ============================================================
# Search Logs for Specific Events
# ============================================================
# Find trade executions
sudo journalctl -u polymarket-bot | grep "Executing.*order"

# Find errors
sudo journalctl -u polymarket-bot | grep -i error

# Find whale exits
sudo journalctl -u polymarket-bot | grep "WHALE DOESN'T HOLD\|WHALE EXITED"

# Find SELL orders
sudo journalctl -u polymarket-bot | grep "SELL validation"

# Find balance updates
sudo journalctl -u polymarket-bot | grep "Current USDC balance"

# ============================================================
# Bot Health Checks
# ============================================================
# Check how long bot has been running
sudo systemctl status polymarket-bot | grep "Active:"

# Check if bot is enabled for auto-start
sudo systemctl is-enabled polymarket-bot

# Check for recent crashes/restarts
sudo journalctl -u polymarket-bot --since today | grep -i "started\|stopped\|failed"

# Monitor resource usage
top -p $(pgrep -f "python.*main.py")
```

---

## 🔧 TROUBLESHOOTING COMMANDS
*Use these when something goes wrong*

```bash
# ============================================================
# Check Git Repository Status
# ============================================================
cd ~/polymarket-arb-bot
git status
git log --oneline -5  # Last 5 commits
git branch -a         # Check which branch you're on

# ============================================================
# Force Pull Latest Code
# ============================================================
cd ~/polymarket-arb-bot
git fetch origin
git reset --hard origin/main
git pull origin main

# ============================================================
# Verify Dependencies
# ============================================================
source ~/polymarket-arb-bot/venv/bin/activate
pip list | grep -E "py-clob-client|web3|boto3"

# Expected versions:
# boto3           1.35.99
# py-clob-client  0.23.0
# web3            6.21.0

# ============================================================
# Test Bot Locally (Foreground Mode - for debugging)
# ============================================================
cd ~/polymarket-arb-bot
source venv/bin/activate
export PYTHONPATH=/home/ubuntu/polymarket-arb-bot/src:$PYTHONPATH
python src/main.py

# Press Ctrl+C to stop
# Use this to see full error traces that systemd might hide

# ============================================================
# Check Configuration Values
# ============================================================
cd ~/polymarket-arb-bot
source venv/bin/activate
export PYTHONPATH=/home/ubuntu/polymarket-arb-bot/src:$PYTHONPATH

# Check all constants
python -c "from config import constants; \
import inspect; \
[print(f'{k} = {v}') for k, v in vars(constants).items() \
 if not k.startswith('_') and k.isupper()]"

# Check specific values
grep -A 5 "MIRROR_STRATEGY_CONFIG" src/config/constants.py

# ============================================================
# Test AWS Secrets Access
# ============================================================
# Verify IAM role
aws sts get-caller-identity

# Test secret retrieval
python3 << 'EOF'
import sys
sys.path.insert(0, '/home/ubuntu/polymarket-arb-bot/src')
from config.aws_config import AWSConfig
config = AWSConfig()
secrets = config.get_secrets()
print(f"✓ Secrets loaded: {list(secrets.keys())}")
EOF

# ============================================================
# Check Network Connectivity
# ============================================================
# Test Polymarket API endpoints
curl -s https://clob.polymarket.com/health | jq '.'
curl -s https://data-api.polymarket.com/markets | head -c 200

# Test Gamma API
curl -s "https://gamma-api.polymarket.com/markets" | head -c 200

# ============================================================
# View Full Error Logs
# ============================================================
# All errors in last 24 hours
sudo journalctl -u polymarket-bot --since "24 hours ago" -p err

# All warning and error logs
sudo journalctl -u polymarket-bot --since today -p warning

# Show full stack traces
sudo journalctl -u polymarket-bot -n 200 --no-pager | grep -A 20 "Traceback"

# ============================================================
# Check Python Process
# ============================================================
# Find bot process
ps aux | grep python.*main.py

# Check if multiple instances are running (shouldn't be!)
pgrep -f "python.*main.py" | wc -l
# Should output: 1

# Kill zombie processes if needed (USE WITH CAUTION)
# pkill -f "python.*main.py"
```

---

## 🚨 EMERGENCY COMMANDS
*Critical issues requiring immediate action*

```bash
# ============================================================
# EMERGENCY: Bot Stuck or Crashed - Force Restart
# ============================================================
sudo systemctl stop polymarket-bot
sleep 3
pkill -9 -f "python.*main.py"  # Kill any zombie processes
sudo systemctl start polymarket-bot

# ============================================================
# EMERGENCY: 401 Unauthorized Errors - Regenerate L2 Credentials
# ============================================================
# ONLY use if you see persistent "401 Unauthorized" errors
cd ~/polymarket-arb-bot
source venv/bin/activate
python scripts/regenerate_l2_credentials.py

# Wait for success message, then restart bot
sudo systemctl restart polymarket-bot

# ============================================================
# EMERGENCY: Bot Trading Incorrectly - Stop Immediately
# ============================================================
# Stop bot to prevent further trades
sudo systemctl stop polymarket-bot

# Review recent trades
sudo journalctl -u polymarket-bot --since "1 hour ago" | grep -E "Executing|Order placed"

# Check balance
cd ~/polymarket-arb-bot
source venv/bin/activate
python -c "from core.polymarket_client import PolymarketClient; \
import asyncio; \
client = PolymarketClient(); \
print(f'Balance: \${asyncio.run(client.get_balance()):.2f}')"

# After fixing issue, restart
sudo systemctl start polymarket-bot

# ============================================================
# EMERGENCY: Disable Bot from Auto-Starting on Boot
# ============================================================
sudo systemctl disable polymarket-bot
sudo systemctl stop polymarket-bot

# To re-enable later
sudo systemctl enable polymarket-bot

# ============================================================
# EMERGENCY: Rollback to Previous Git Commit
# ============================================================
cd ~/polymarket-arb-bot
git log --oneline -10  # Find commit hash to rollback to
git reset --hard <commit-hash>  # e.g., git reset --hard 54742b9
sudo systemctl restart polymarket-bot

# ============================================================
# EMERGENCY: Complete Fresh Start
# ============================================================
# Stop bot
sudo systemctl stop polymarket-bot

# Remove everything
cd ~
rm -rf polymarket-arb-bot

# Follow "REGULAR DEPLOYMENT & UPDATES" section from scratch
```

---

## 📝 USEFUL ONE-LINERS

```bash
# ============================================================
# Quick Status Check
# ============================================================
echo "=== Bot Status ===" && \
sudo systemctl status polymarket-bot --no-pager && \
echo -e "\n=== Last 5 Log Lines ===" && \
sudo journalctl -u polymarket-bot -n 5 --no-pager

# ============================================================
# Quick Update & Restart
# ============================================================
cd ~/polymarket-arb-bot && \
git pull origin main && \
sudo systemctl restart polymarket-bot && \
echo "✓ Bot updated and restarted" && \
sudo journalctl -u polymarket-bot -f

# ============================================================
# Check Balance & Positions
# ============================================================
cd ~/polymarket-arb-bot && \
source venv/bin/activate && \
sudo journalctl -u polymarket-bot -n 100 --no-pager | \
grep -E "Current USDC balance|You have.*positions|Whale has.*positions" | tail -5

# ============================================================
# Monitor Trading Activity (Last 1 Hour)
# ============================================================
sudo journalctl -u polymarket-bot --since "1 hour ago" --no-pager | \
grep -E "Executing|Order placed|SELL validation passed|BUY.*approved|WHALE DOESN'T HOLD"
```

---

## 🎯 RECOMMENDED DAILY WORKFLOW

```bash
# Morning Check:
sudo journalctl -u polymarket-bot --since "24 hours ago" | \
grep -E "Current USDC balance|Executing|Error" | tail -20

# Check bot is running:
sudo systemctl status polymarket-bot

# Evening Check:
sudo journalctl -u polymarket-bot --since today | \
grep -E "Current USDC balance|positions|opportunities" | tail -10
```

---

## ⚙️ CONFIGURATION QUICK REFERENCE

| Setting | Value | Location |
|---------|-------|----------|
| **AWS Region** | eu-west-1 | `src/config/constants.py` line 242 |
| **Fixed Order Size** | $5.00 USDC | `src/config/constants.py` line 212 |
| **Min Order Size** | $2.00 USDC | `src/config/constants.py` line 45 |
| **Price Range (BUY)** | 0.15 - 0.85 | `src/strategies/mirror_strategy.py` lines 234-244 |
| **Polling Interval** | 15 seconds | `src/config/constants.py` line 213 |
| **Circuit Breaker** | 5 failed orders | `src/config/constants.py` line 49 |
| **Dust Threshold** | $0.10 | `src/config/constants.py` line 47 |

---

## 📞 COMMON ISSUES & SOLUTIONS

| Issue | Command | Notes |
|-------|---------|-------|
| Bot not starting | `sudo journalctl -u polymarket-bot -n 50` | Check for Python errors |
| 401 Unauthorized | `python scripts/regenerate_l2_credentials.py` | Regenerate L2 credentials |
| No trades executing | `sudo journalctl -u polymarket-bot -f` | Check filtering logs |
| High CPU usage | `top -p $(pgrep -f main.py)` | Shouldn't exceed 5% |
| Git conflicts | `git reset --hard origin/main` | Forces clean state |
| Balance not updating | Check if bot detects opportunities | Whale may not have new trades |
| SELL orders failing | Check "No shares to sell" logs | True mirroring logic working |

---

## 🔐 AWS IAM POLICY REQUIRED

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
        "secretsmanager:PutSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:eu-west-1:*:secret:polymarket/prod/credentials-*"
    }
  ]
}
```

**Role Name:** `PolymarketBotRole`  
**Attach to:** EC2 Instance Profile

---

## 📈 PERFORMANCE METRICS

- **CPU Usage:** <5% (typically 1-2%)
- **Memory Usage:** ~150-200 MB
- **Polling Frequency:** Every 15 seconds
- **API Calls per Cycle:** ~10-15 (with 99% cache hit rate)
- **Expected Uptime:** 99.9% (24/7 operation)

---

## 🎓 BOT BEHAVIOR SUMMARY

### **What the Bot Does:**
1. ✅ Monitors whale's positions every 15 seconds
2. ✅ **BUY** positions whale enters (if price 0.15-0.85)
3. ✅ **SELL** positions whale exits
4. ✅ **SELL** positions you own that whale doesn't (TRUE MIRRORING)
5. ✅ Validates share ownership before all SELL orders
6. ✅ Skips dust positions (<$0.10)
7. ✅ Respects $2.00 minimum for BUY orders only

### **What the Bot Doesn't Do:**
- ❌ Doesn't trade if whale holds very high (>0.85) or very low (<0.15) priced positions
- ❌ Doesn't execute SELL orders if you don't own shares
- ❌ Doesn't bypass circuit breaker (stops after 5 consecutive failures)
- ❌ Doesn't trade manually entered positions (unless whale exits similar position)

---

## 📚 ADDITIONAL RESOURCES

- **Repository:** https://github.com/riteshkumargarg/polymarket-arb-bot
- **Polymarket Support:** Recommend eu-west-1 region for datacenter IPs
- **API Docs:** https://docs.polymarket.com/
- **CLOB Client:** https://github.com/Polymarket/py-clob-client

---

## 🆘 SUPPORT

If you encounter issues:
1. Check logs: `sudo journalctl -u polymarket-bot -n 100`
2. Test locally: `python src/main.py`
3. Verify secrets: `aws secretsmanager get-secret-value --secret-id polymarket/prod/credentials`
4. Regenerate credentials: `python scripts/regenerate_l2_credentials.py`
5. Review this guide's troubleshooting section

---

**✅ Bot is production-ready and battle-tested!**

*Last successful deployment: January 11, 2026*  
*Current version: main branch @ commit 26c7d59*
