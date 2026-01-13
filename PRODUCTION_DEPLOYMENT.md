"""
PRODUCTION DEPLOYMENT GUIDE
Polymarket Arbitrage Bot - AWS EC2 Ubuntu 24.04 LTS

This guide covers production-grade deployment with 24/7 reliability,
monitoring, and automatic recovery.

===============================================================================
ARCHITECTURE OVERVIEW
===============================================================================

Components:
1. Bot Service: Systemd service running bot.py with auto-restart
2. Monitoring: Health checks and alerting
3. Logs: Rotating JSON logs to /var/log/polymarket-bot/
4. AWS Integration: Secrets Manager for credentials
5. Database: Optional InfluxDB for metrics (future)

Reliability Features:
- Process supervisor (systemd) with auto-restart
- Health checks every 60 seconds
- Circuit breaker on large losses
- Graceful shutdown handling (SIGTERM/SIGINT)
- Structured logging for aggregation
- Error recovery with exponential backoff

===============================================================================
PREREQUISITES
===============================================================================

1. AWS EC2 Instance Setup:
   - Type: t3.micro or t3.small (minimal performance needs)
   - OS: Ubuntu 24.04 LTS
   - Storage: 30 GB EBS (for logs, 50MB per file × 10 files = 500MB max)
   - Security Group: Allow SSH (port 22), no inbound trading traffic needed

2. AWS IAM Configuration:
   - EC2 instance must have IAM role with Secrets Manager read access
   - Policy: AmazonSecretsManagerReadOnlyAccess

3. AWS Secrets Manager:
   - Create secret: /polymarket/prod/credentials
   - Keys: WALLET_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASS
   - Region: eu-west-1 (Ireland, per Polymarket support)

4. AWS VPC Configuration:
   - NAT Gateway or VPN recommended (Polymarket may block raw datacenter IPs)
   - Public instance acceptable for low-volume trading
   - No special networking required

===============================================================================
1. INITIAL SERVER SETUP
===============================================================================

Step 1: SSH into your EC2 instance
------
ssh -i your-key.pem ubuntu@your-instance-ip

Step 2: Update system packages
------
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3.10 python3-pip python3-venv git curl wget

Step 3: Create bot user (non-root operation)
------
sudo useradd -m -s /bin/bash polybot
sudo usermod -aG docker polybot  # If using Docker

Step 4: Create application directory
------
sudo mkdir -p /opt/polymarket-bot
sudo chown -R polybot:polybot /opt/polymarket-bot

Step 5: Create logs directory with proper permissions
------
sudo mkdir -p /var/log/polymarket-bot
sudo chown -R polybot:polybot /var/log/polymarket-bot
sudo chmod 755 /var/log/polymarket-bot

===============================================================================
2. APPLICATION DEPLOYMENT
===============================================================================

Step 1: Clone or copy application code
------
cd /opt/polymarket-bot
git clone https://github.com/riteshkugarg/polymarket-arb-bot.git .

Step 2: Create Python virtual environment
------
python3 -m venv venv
source venv/bin/activate

Step 3: Install dependencies
------
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

Step 4: Create .env file (with actual values from AWS Secrets Manager)
------
cat > .env << 'EOF'
# Polymarket Configuration
POLYMARKET_PROXY_ADDRESS=0x5967c88F93f202D595B9A47496b53E28cD61F4C3
MIRROR_TARGET_WALLET=0x63ce342161250d705dc0b16df89036c8e5f9ba9a

# AWS Configuration
AWS_REGION=eu-west-1
AWS_SECRET_ID=polymarket/prod/credentials

# Logging
LOG_LEVEL=INFO
LOG_FILE_PATH=/var/log/polymarket-bot/bot.log

# Trading Parameters
USE_WEBSOCKET_DETECTION=false
LOOP_INTERVAL_SEC=2
ENABLE_CIRCUIT_BREAKER=true
CIRCUIT_BREAKER_LOSS_THRESHOLD_USD=25.0

# Feature Flags
ENABLE_TIME_BASED_FILTERING=true
ENABLE_METRICS=true
EOF

Step 5: Test bot runs correctly
------
python -m src.main --test

===============================================================================
3. SYSTEMD SERVICE CONFIGURATION
===============================================================================

Step 1: Create systemd service file
------
sudo cat > /etc/systemd/system/polymarket-bot.service << 'EOF'
[Unit]
Description=Polymarket Arbitrage Bot
After=network.target

[Service]
Type=simple
User=polybot
WorkingDirectory=/opt/polymarket-bot

# Environment setup
Environment="PATH=/opt/polymarket-bot/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="PYTHONUNBUFFERED=1"
EnvironmentFile=/opt/polymarket-bot/.env

# Executable
ExecStart=/opt/polymarket-bot/venv/bin/python -m src.main

# Restart policy: automatically restart on failure with backoff
Restart=always
RestartSec=10
StartLimitIntervalSec=60
StartLimitBurst=3

# Process management
KillMode=mixed
KillSignal=SIGTERM

# Resource limits
LimitNOFILE=65536
LimitNPROC=65536

# Security
NoNewPrivileges=true
PrivateTmp=true

# Logging (systemd journal)
StandardOutput=journal
StandardError=journal
SyslogIdentifier=polymarket-bot

[Install]
WantedBy=multi-user.target
EOF

Step 2: Reload systemd daemon
------
sudo systemctl daemon-reload

Step 3: Enable service auto-start on boot
------
sudo systemctl enable polymarket-bot

Step 4: Start the service
------
sudo systemctl start polymarket-bot

Step 5: Verify service is running
------
sudo systemctl status polymarket-bot

===============================================================================
4. MONITORING & HEALTH CHECKS
===============================================================================

View Logs (real-time):
------
sudo journalctl -u polymarket-bot -f

View logs from file:
------
tail -f /var/log/polymarket-bot/bot.log

Search logs for errors:
------
grep "ERROR" /var/log/polymarket-bot/bot.log

Monitor system resources:
------
watch -n 1 "ps aux | grep polymarket-bot"

Check service status:
------
systemctl is-active polymarket-bot
systemctl is-enabled polymarket-bot

Restart service (graceful):
------
sudo systemctl restart polymarket-bot

Stop service (graceful shutdown):
------
sudo systemctl stop polymarket-bot

===============================================================================
5. OPERATIONAL PROCEDURES
===============================================================================

A. Daily Operations Checklist
----
- Check bot is running: systemctl status polymarket-bot
- Review error logs: grep ERROR /var/log/polymarket-bot/bot.log
- Monitor balance: Check USDC wallet on Polymarket
- Verify trades: Check Polymarket order history
- Check metrics: Review performance analysis (if enabled)

B. Emergency Procedures
----
If bot is stuck or unresponsive:
1. Check logs: sudo journalctl -u polymarket-bot -n 50
2. Identify issue: Look for last trade, error message
3. Restart service: sudo systemctl restart polymarket-bot

If circuit breaker is triggered (losses too high):
1. Bot automatically stops trading
2. Operator must investigate: What went wrong?
3. Manual restart required: systemctl restart polymarket-bot
4. Optional: Review trades, adjust parameters, redeploy

If AWS credentials are invalid:
1. Update secrets in AWS Secrets Manager
2. Restart bot: systemctl restart polymarket-bot
3. Verify new credentials work: Check service status

C. Scaling & Performance Tuning
----
CPU Usage:
- Normal: < 5% CPU
- High: 5-15% CPU indicates active trading or API delays
- Very High: >30% CPU indicates problem

Memory Usage:
- Normal: 100-300 MB
- High: >500 MB indicates memory leak (restart required)

If bot is too slow:
- Increase LOOP_INTERVAL_SEC in constants.py (less frequent checks)
- Consider higher CPU instance (t3.small → t3.medium)

If missing trades (high latency):
- Decrease LOOP_INTERVAL_SEC (more frequent checks)
- Enable WEBSOCKET_DETECTION=true for real-time events

===============================================================================
6. LOG MANAGEMENT
===============================================================================

Logs are stored in: /var/log/polymarket-bot/bot.log

Rotation Policy:
- Max file size: 50 MB per file
- Backup count: 10 files
- Total max: 550 MB (50 MB × 11 files)

Cleanup old logs:
------
find /var/log/polymarket-bot/ -name "bot.log.*" -mtime +30 -delete

Export logs for analysis:
------
cp /var/log/polymarket-bot/bot.log /home/ubuntu/bot-logs-$(date +%Y%m%d).log

Centralized logging (Optional - CloudWatch):
------
# Install CloudWatch logs agent
wget https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
dpkg -i -E ./amazon-cloudwatch-agent.deb

# Configure to send logs to CloudWatch
sudo systemctl start amazon-cloudwatch-agent

===============================================================================
7. SECURITY BEST PRACTICES
===============================================================================

1. Private Key Management:
   - NEVER store private key in code
   - Always use AWS Secrets Manager
   - Rotate credentials monthly
   - Use IAM roles (don't embed credentials)

2. Network Security:
   - Use VPN or bastion host for SSH access
   - Restrict security group to your IP for SSH
   - Use SSH keys (never password auth)
   - Disable root login

3. Access Control:
   - Run bot as non-root user (polybot)
   - Restrict file permissions: 600 for .env
   - Use IAM roles for AWS access (not hardcoded)

4. Monitoring & Alerts:
   - Monitor unauthorized access attempts
   - Alert on bot crash/restart
   - Log all trading activity (already done)

5. Disaster Recovery:
   - Backup application code to GitHub
   - Backup EC2 instance snapshots weekly
   - Document all configuration changes
   - Keep recovery runbook up-to-date

===============================================================================
8. AWS COST OPTIMIZATION
===============================================================================

Estimated Monthly Costs (us-east-1):
- EC2 t3.micro (730 hours): $7-10
- Data transfer: $0-5
- Secrets Manager: $0.40
- Total: ~$8-15/month

Cost reduction strategies:
- Use t3.micro (sufficient for polling-based bot)
- Enable auto-shutdown if bot not trading
- Use spot instances (for non-critical trading)
- Consolidate logs (don't keep forever)

===============================================================================
9. TROUBLESHOOTING
===============================================================================

Problem: Bot starts but immediately stops
Solution:
1. Check logs: journalctl -u polymarket-bot
2. Verify .env file exists and is readable
3. Verify AWS credentials in Secrets Manager
4. Check Python dependencies: pip list

Problem: High CPU usage
Solution:
1. Check LOOP_INTERVAL_SEC setting (increase if too frequent)
2. Monitor API calls (may be rate limited)
3. Check for infinite loops in logs

Problem: Missing trades
Solution:
1. Increase polling frequency: decrease LOOP_INTERVAL_SEC
2. Enable WebSocket detection: USE_WEBSOCKET_DETECTION=true
3. Check API rate limits haven't been exceeded

Problem: Circuit breaker triggered
Solution:
1. Review trades that caused losses
2. Adjust risk parameters in constants.py
3. Restart and monitor

Problem: Out of memory
Solution:
1. Restart service: systemctl restart polymarket-bot
2. Check for memory leaks in logs
3. Increase EC2 instance size if persistent

Problem: Connection timeout errors
Solution:
1. Check network connectivity: ping data-api.polymarket.com
2. Verify AWS region is correct
3. Check security group allows outbound HTTPS
4. Try restarting service

===============================================================================
10. UPGRADE & MAINTENANCE
===============================================================================

Updating bot code:
------
cd /opt/polymarket-bot
git pull origin main
source venv/bin/activate
pip install --upgrade -r requirements.txt
sudo systemctl restart polymarket-bot

Updating dependencies:
------
pip install --upgrade pip
pip install --upgrade -r requirements.txt
sudo systemctl restart polymarket-bot

Backing up configuration:
------
sudo cp /opt/polymarket-bot/.env /home/ubuntu/backup-env-$(date +%Y%m%d).backup

Creating system snapshot (AWS EC2):
------
# In AWS Console > EC2 > Instances > Right-click > Image and templates > Create image

===============================================================================
SUPPORT & RESOURCES
===============================================================================

Documentation:
- README.md: Project overview and features
- DEPLOYMENT.md: Deployment procedures
- PRODUCTION_OPERATIONS.md: Day-to-day operations
- QUICKSTART.md: Getting started guide

Issues & Support:
- Check logs first: journalctl -u polymarket-bot
- Review README.md for common issues
- Contact Polymarket support: support@polymarket.com
- Check GitHub issues

Emergency Contact Procedures:
1. If bot is losing money: Stop immediately (systemctl stop polymarket-bot)
2. If losses exceed threshold: Circuit breaker auto-stops
3. Investigate root cause before restarting
4. Adjust parameters if needed
5. Restart and monitor closely

===============================================================================
FINAL CHECKLIST
===============================================================================

Before going live:
☐ EC2 instance is running and accessible
☐ .env file is configured with actual values
☐ AWS Secrets Manager has all required credentials
☐ systemd service file is installed
☐ Bot runs and connects to Polymarket
☐ Logs are being written to /var/log/polymarket-bot/
☐ systemctl start/stop/restart works
☐ systemctl enable polymarket-bot is set
☐ Health checks are running
☐ Monitoring is set up
☐ Backup procedures are documented
☐ Emergency procedures are understood

Go live:
☐ Start service: sudo systemctl start polymarket-bot
☐ Verify: sudo systemctl status polymarket-bot
☐ Monitor logs: sudo journalctl -u polymarket-bot -f
☐ Check for trading activity (wait 2-5 minutes)
☐ Review first trades in logs
☐ Set up daily monitoring checks

Success! Your production bot is now running 24/7 on AWS EC2.
"""
