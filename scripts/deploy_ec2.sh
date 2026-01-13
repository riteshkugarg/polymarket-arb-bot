#!/bin/bash

###############################################################################
# Deployment Script for AWS EC2
# Sets up and configures the bot for production deployment
###############################################################################

set -e

echo "====================================================="
echo "Polymarket Bot - AWS EC2 Deployment"
echo "====================================================="

# Install system dependencies
echo "Installing system dependencies..."
sudo apt-get update
sudo apt-get install -y \
    python3.10 \
    python3.10-venv \
    python3-pip \
    git \
    build-essential \
    libssl-dev \
    libffi-dev \
    python3-dev

# Setup bot directory
BOT_DIR="/home/ubuntu/polymarket-arb-bot"
cd "$BOT_DIR"

# Run setup
echo "Running bot setup..."
./scripts/run_bot.sh setup

# Install systemd service
echo "Installing systemd service..."
sudo cp scripts/polymarket-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot.service

# Create cron job for health checks
echo "Setting up health check cron job..."
(crontab -l 2>/dev/null; echo "*/5 * * * * $BOT_DIR/scripts/health_check.sh >> $BOT_DIR/logs/health_check.log 2>&1") | crontab -

# Set proper permissions
echo "Setting permissions..."
sudo chown -R ubuntu:ubuntu "$BOT_DIR"
chmod +x scripts/*.sh

echo ""
echo "====================================================="
echo "Deployment Complete!"
echo "====================================================="
echo ""
echo "Next steps:"
echo "1. Configure AWS credentials (IAM role or credentials file)"
echo "2. Ensure AWS Secrets Manager has required secrets"
echo "3. Start the bot:"
echo "   sudo systemctl start polymarket-bot"
echo ""
echo "Check status:"
echo "   sudo systemctl status polymarket-bot"
echo "   ./scripts/run_bot.sh status"
echo ""
echo "View logs:"
echo "   ./scripts/run_bot.sh logs"
echo "   journalctl -u polymarket-bot -f"
echo ""
