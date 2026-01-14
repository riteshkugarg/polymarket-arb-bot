#!/bin/bash
# Deploy health check fix to EC2 instance

set -e

EC2_HOST="${1:-ubuntu@ec2-63-32-27-153.eu-west-1.compute.amazonaws.com}"
REPO_DIR="/home/ubuntu/polymarket-arb-bot"

echo "======================================"
echo "Deploying Health Check Fix to EC2"
echo "======================================"
echo ""

# Check if bot is running
echo "1. Checking bot status..."
if ssh -o ConnectTimeout=10 "$EC2_HOST" "pgrep -f 'python.*main.py' > /dev/null"; then
    echo "   ✅ Bot is running"
    BOT_RUNNING=true
else
    echo "   ⚠️  Bot is not running"
    BOT_RUNNING=false
fi
echo ""

# Stop bot if running
if [ "$BOT_RUNNING" = true ]; then
    echo "2. Stopping bot..."
    ssh "$EC2_HOST" "pkill -f 'python.*main.py' || true"
    sleep 2
    echo "   ✅ Bot stopped"
    echo ""
fi

# Pull latest code
echo "3. Pulling latest code from repository..."
ssh "$EC2_HOST" "cd $REPO_DIR && git fetch origin && git reset --hard origin/main"
echo "   ✅ Code updated"
echo ""

# Restart bot
echo "4. Restarting bot..."
ssh "$EC2_HOST" "cd $REPO_DIR && nohup python src/main.py > logs/bot.log 2>&1 &"
sleep 3
echo "   ✅ Bot started"
echo ""

# Check if bot started successfully
echo "5. Verifying bot status..."
if ssh "$EC2_HOST" "pgrep -f 'python.*main.py' > /dev/null"; then
    echo "   ✅ Bot is running"
    
    # Show recent logs
    echo ""
    echo "6. Recent logs (last 20 lines):"
    echo "----------------------------------------"
    ssh "$EC2_HOST" "tail -20 $REPO_DIR/logs/bot.log"
else
    echo "   ❌ Bot failed to start"
    echo ""
    echo "Error logs:"
    echo "----------------------------------------"
    ssh "$EC2_HOST" "tail -50 $REPO_DIR/logs/bot.log"
    exit 1
fi

echo ""
echo "======================================"
echo "Deployment Complete!"
echo "======================================"
echo ""
echo "Monitor logs with:"
echo "  ssh $EC2_HOST 'tail -f $REPO_DIR/logs/bot.log'"
