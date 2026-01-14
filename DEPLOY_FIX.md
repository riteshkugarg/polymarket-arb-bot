# Deploy Health Check Fix - Quick Guide

## Current Status
❌ Bot is running but will crash after 5 consecutive health check errors
❌ Error: `'ArbScanner' object has no attribute 'is_running'`

## Fix Instructions

### Step 1: SSH into EC2
```bash
# From your local machine
ssh ubuntu@ec2-63-32-27-153.eu-west-1.compute.amazonaws.com
```

### Step 2: Stop the running bot
```bash
pkill -f 'python.*main.py'
```

### Step 3: Pull latest code
```bash
cd ~/polymarket-arb-bot
git pull origin main
```

You should see:
```
Updating 92da9d5..b565beb
Fast-forward
 src/main.py              | 8 ++++++--
 scripts/deploy_fix.sh    | 72 ++++++++++++++++++++++++++++++++++++++++++++++++++++
```

### Step 4: Restart bot
```bash
cd ~/polymarket-arb-bot
nohup python src/main.py > logs/bot_stdout.log 2>&1 &
```

### Step 5: Verify fix
```bash
tail -f logs/bot_stdout.log
```

**Look for:**
- ✅ No "AttributeError: 'ArbScanner' object has no attribute 'is_running'"
- ✅ Health check passes (should see after 30 seconds)
- ✅ Arbitrage scanning continues

### Step 6: Monitor for 2 minutes
The old code crashed after ~1 minute of health check errors. Monitor for 2 minutes to confirm stability.

---

## Quick One-Liner (if you trust me 😊)

```bash
ssh ubuntu@ec2-63-32-27-153.eu-west-1.compute.amazonaws.com "pkill -f 'python.*main.py'; cd ~/polymarket-arb-bot && git pull origin main && nohup python src/main.py > logs/bot_stdout.log 2>&1 &"
```

Then monitor:
```bash
ssh ubuntu@ec2-63-32-27-153.eu-west-1.compute.amazonaws.com "tail -f ~/polymarket-arb-bot/logs/bot_stdout.log"
```
