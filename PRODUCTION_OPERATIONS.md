# 🔧 Production Operations Guide

Quick reference for monitoring and troubleshooting the bot in production.

---

## 🚀 Deployment Commands

```bash
# 1. Navigate to project
cd /workspaces/polymarket-arb-bot

# 2. Restart bot service
sudo systemctl restart polymarket-bot

# 3. Check status
sudo systemctl status polymarket-bot

# 4. Monitor logs in real-time
sudo journalctl -u polymarket-bot -f
```

---

## 📊 Monitoring What to Watch

### ✅ Healthy Bot Indicators

#### Cycle Logs (Every 5 seconds):
```
🔄 Starting mirror check cycle...
🐋 Target whale: 0x1234...
💼 Own wallet: 0x5678...
💵 Current USDC balance: $55.00 (cached)
⏰ Fetching whale's recent entries (last 1 min)...
📊 Data validation: 3/3 entries have token_id
✅ 3 valid positions to analyze
💡 Found 2 trading opportunities
```

#### Successful Order Execution:
```
✅ Order 1/2 placed: BUY $2.35 of Will Trump win 2024?...
⏳ Waiting 10s before next trade...
✅ Order 2/2 placed: BUY $0.77 of Bitcoin above $100k?...
🔄 Cycle complete: 2 executed, 0 skipped, 0 failed
```

#### No Opportunities (Normal):
```
✅ No recent whale activity in last 1 minutes
```

---

### ⚠️ Warning Signs (Action May Be Needed)

#### Insufficient Balance:
```
⚠️  Insufficient balance: $2.50 < $2.70 - Need to add $0.20 USDC
⏭️  Skipped (1): Insufficient balance: $2.50 USDC < $2.70 USDC
```
**Action:** Deposit more USDC to wallet

#### Missing Whale Entry Price:
```
❌ Missing whale entry price - cannot place limit order
```
**Action:** Monitor if recurring, may need code fix

---

### 🚨 Critical Issues (Requires Investigation)

#### API Failures:
```
❌ Failed to fetch whale's recent entries: Connection timeout
```
**Action:** Check internet connection, API status

#### Order Execution Failures:
```
❌ Failed to execute mirror trade (1): Insufficient balance
```
**Action:** Check wallet balance, verify API credentials

---

## 🔍 Log Search Commands

### Check for Executed Orders Today:
```bash
sudo journalctl -u polymarket-bot --since "today" | grep "✅ Order"
```

### Count Successful Executions:
```bash
sudo journalctl -u polymarket-bot --since "today" | grep -c "Cycle complete"
```

### Find Skipped Opportunities:
```bash
sudo journalctl -u polymarket-bot --since "today" | grep "⏭️  Skipped"
```

### View Balance Warnings:
```bash
sudo journalctl -u polymarket-bot --since "today" | grep "Insufficient balance"
```

### Check for Errors:
```bash
sudo journalctl -u polymarket-bot --since "today" | grep "❌"
```

### View Last 10 Cycle Summaries:
```bash
sudo journalctl -u polymarket-bot | grep "🔄 Cycle complete" | tail -10
```

---

## 📈 Key Metrics to Track

### Daily Metrics:
- **Cycles Run:** ~17,280 per day (every 5 seconds)
- **Orders Executed:** Depends on whale activity
- **Skip Rate:** Should be <5% for balance issues
- **Error Rate:** Should be <0.1%

### Example Healthy Day:
```
Cycles: 17,280
Opportunities Found: 45
Orders Executed: 42
Skipped (balance): 3
Failed: 0
```

---

## 🛠️ Troubleshooting Guide

### Problem: Bot Not Placing Orders

#### Check 1: Is bot running?
```bash
sudo systemctl status polymarket-bot
```
Expected: `active (running)`

#### Check 2: Are there opportunities?
```bash
sudo journalctl -u polymarket-bot -n 20 | grep "Found.*opportunities"
```
Expected: See "Found X trading opportunities"

#### Check 3: Balance sufficient?
```bash
sudo journalctl -u polymarket-bot -n 50 | grep "balance"
```
Expected: Balance > min order size ($0.54)

#### Check 4: Whale active?
```bash
sudo journalctl -u polymarket-bot -n 20 | grep "No recent whale activity"
```
If frequent: Whale not trading, bot working correctly

---

### Problem: "Insufficient Balance" Errors

#### Check Current Balance:
```bash
sudo journalctl -u polymarket-bot -n 5 | grep "balance:"
```

#### Calculate Orders Remaining:
```
Balance: $55.00
Avg order size: ~$2.00
Remaining orders: ~27
```

#### Add USDC:
1. Transfer USDC to bot wallet address
2. Wait for confirmation
3. Bot will detect on next cycle

---

### Problem: High Skip Rate

#### Check Skip Reasons:
```bash
sudo journalctl -u polymarket-bot --since "1 hour ago" | grep "Skipped"
```

#### Common Reasons:
- `Insufficient balance` → Add USDC
- `already own` → Already synchronized
- `Whale entered >1 min ago` → Normal filtering
- `position too small` → Below $0.54 minimum

---

### Problem: No Whale Activity

#### Verify Whale Address:
```bash
sudo journalctl -u polymarket-bot -n 5 | grep "Target whale:"
```
Expected: Shows configured whale address

#### Check Last Whale Activity:
```bash
sudo journalctl -u polymarket-bot --since "1 hour ago" | grep "Found.*positions with recent whale activity"
```

If no activity for hours: Whale may not be trading (normal)

---

## 🔄 Restart Scenarios

### When to Restart:

#### 1. After Code Updates:
```bash
git pull
sudo systemctl restart polymarket-bot
sudo journalctl -u polymarket-bot -f  # Monitor restart
```

#### 2. After .env Changes:
```bash
sudo systemctl restart polymarket-bot
```

#### 3. If Stuck/Not Responding:
```bash
sudo systemctl restart polymarket-bot
```

### Safe Restart Process:
```bash
# 1. Stop bot gracefully
sudo systemctl stop polymarket-bot

# 2. Wait 5 seconds
sleep 5

# 3. Start bot
sudo systemctl start polymarket-bot

# 4. Verify started
sudo systemctl status polymarket-bot

# 5. Watch first few cycles
sudo journalctl -u polymarket-bot -f
```

---

## 💰 Balance Management

### Check Current Balance:
```bash
sudo journalctl -u polymarket-bot -n 1 | grep "balance:"
```

### Estimate Orders Remaining:
```python
# With $55 balance and $0.54-$3.60 order sizes:
# Average order ~$2.00
# Remaining: $55 / $2.00 = ~27 orders
```

### Recommended Balance:
- **Minimum:** $20 (10 orders)
- **Optimal:** $50-100 (25-50 orders)
- **Max useful:** $200 (depends on whale frequency)

### When to Add More USDC:
- Balance < $10
- Skip rate > 20% due to balance
- Whale very active (many opportunities)

---

## 📋 Daily Checklist

### Morning Check (5 minutes):
```bash
# 1. Verify bot running
sudo systemctl status polymarket-bot

# 2. Check overnight activity
sudo journalctl -u polymarket-bot --since "yesterday" | grep "Cycle complete"

# 3. Count orders executed
sudo journalctl -u polymarket-bot --since "yesterday" | grep -c "Order.*placed"

# 4. Check for errors
sudo journalctl -u polymarket-bot --since "yesterday" | grep "❌"

# 5. Verify current balance
sudo journalctl -u polymarket-bot -n 5 | grep "balance:"
```

### Weekly Review:
- Total orders executed
- Average order size
- Balance consumed
- Error patterns
- Performance metrics

---

## 🚨 Alert Triggers

### Set Up Monitoring For:

#### Critical Alerts:
- Bot service stopped: `systemctl status polymarket-bot`
- API failures > 10/hour: `grep "❌ Failed to fetch"`
- Order execution failures > 5/hour: `grep "Failed to execute"`

#### Warning Alerts:
- Balance < $10: `grep "balance:"`
- Skip rate > 50%: `grep "Cycle complete.*skipped"`
- No whale activity for 6+ hours: `grep "No recent whale activity"`

---

## 📞 Emergency Contacts

### Stop Bot Immediately:
```bash
sudo systemctl stop polymarket-bot
```

### Check What Happened:
```bash
sudo journalctl -u polymarket-bot -n 100
```

### Restart After Fix:
```bash
sudo systemctl start polymarket-bot
```

---

## 🔗 Related Documents

- [PRODUCTION_REVIEW.md](PRODUCTION_REVIEW.md) - Full production readiness review
- [DEPLOYMENT.md](DEPLOYMENT.md) - Deployment guide
- [README.md](README.md) - Project overview
- [QUICKSTART.md](QUICKSTART.md) - Getting started guide

---

**Last Updated:** January 12, 2026  
**Version:** 1.0 - Production Release
