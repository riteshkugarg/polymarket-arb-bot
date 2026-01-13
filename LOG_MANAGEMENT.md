# Log Management Guide

## Automatic Log Rotation (Already Configured)

Your bot uses **Python's RotatingFileHandler** which automatically:
- ✅ Rotates logs when they reach **50 MB**
- ✅ Keeps **10 backup files** (550 MB total max)
- ✅ Deletes oldest backups automatically
- ✅ Never deletes the active log file

**Files:**
- `logs/polymarket_bot.log` - Current active log
- `logs/polymarket_bot.log.1` - Previous rotation
- `logs/polymarket_bot.log.2` - Older rotation
- ... up to `.log.10`

## Manual Log Cleanup

### Quick Cleanup (Delete logs older than 7 days)
```bash
cd ~/polymarket-arb-bot
./scripts/cleanup_logs.sh
```

### Options
```bash
# Delete logs older than 3 days
./scripts/cleanup_logs.sh --days 3

# Delete if total size exceeds 200 MB
./scripts/cleanup_logs.sh --size-mb 200

# Preview what would be deleted (safe to test)
./scripts/cleanup_logs.sh --dry-run

# Aggressive cleanup (1 day old)
./scripts/cleanup_logs.sh --days 1
```

## Automated Cleanup with Cron

### Setup Daily Cleanup (3 AM every day)
```bash
# Edit crontab
crontab -e

# Add this line:
0 3 * * * /home/ubuntu/polymarket-arb-bot/scripts/cleanup_logs.sh --days 7 >> /home/ubuntu/polymarket-arb-bot/logs/cleanup.log 2>&1
```

### Alternative: Weekly Cleanup (Sunday 3 AM)
```bash
0 3 * * 0 /home/ubuntu/polymarket-arb-bot/scripts/cleanup_logs.sh --days 7
```

## Check Current Log Size

```bash
# Check log directory size
du -sh ~/polymarket-arb-bot/logs

# List all log files with sizes
ls -lh ~/polymarket-arb-bot/logs/*.log*

# Find large log files
find ~/polymarket-arb-bot/logs -type f -size +10M -exec ls -lh {} \;
```

## Disk Space Monitoring

### Check available disk space
```bash
df -h /
```

### Check bot's disk usage
```bash
du -sh ~/polymarket-arb-bot
```

### Get detailed breakdown
```bash
cd ~/polymarket-arb-bot
du -h --max-depth=1 | sort -h
```

## Log Compression (Save Space)

### Compress old logs manually
```bash
cd ~/polymarket-arb-bot/logs
gzip polymarket_bot.log.{5..10}
```

### Automated compression in cron
```bash
# Add to crontab (compress logs older than 2 days)
0 4 * * * find /home/ubuntu/polymarket-arb-bot/logs -name "*.log.[3-9]*" -mtime +2 ! -name "*.gz" -exec gzip {} \;
```

## Current Configuration

**Log Rotation Settings** (in `src/config/constants.py`):
- `MAX_LOG_FILE_SIZE`: 50 MB (rotates at this size)
- `LOG_BACKUP_COUNT`: 10 files (total 550 MB max)

**Log Files:**
- `logs/polymarket_bot.log` - Main application log
- `logs/bot_stdout.log` - Systemd stdout (from service)
- `logs/bot_stderr.log` - Systemd stderr (from service)

## Emergency Cleanup (If Disk Full)

### Delete all old backups immediately
```bash
cd ~/polymarket-arb-bot/logs
rm -f *.log.[2-9]* *.log.1[0-9]*
```

### Keep only current logs
```bash
cd ~/polymarket-arb-bot/logs
ls | grep -E '\.(log\.[0-9]+|gz)$' | xargs rm -f
```

### After cleanup, restart bot
```bash
sudo systemctl restart polymarket-bot
```

## Cost Estimation (AWS)

**EBS Storage Cost:** ~$0.08 per GB-month (eu-west-1)

**Current setup:**
- Max 550 MB logs = ~$0.044/month
- Very minimal cost! 💚

**If logs grow to 5 GB:**
- Cost would be ~$0.40/month
- Still extremely cheap

**Recommendation:** 
Keep current auto-rotation settings. They're already optimized for cost vs. debugging capability.

## Troubleshooting

### "No space left on device" error

1. Check disk space: `df -h`
2. Find large files: `du -h ~/polymarket-arb-bot | sort -h | tail -20`
3. Run cleanup: `./scripts/cleanup_logs.sh --days 1`
4. If needed, increase EBS volume size in AWS console

### Logs not rotating

1. Check permissions: `ls -la ~/polymarket-arb-bot/logs`
2. Verify configuration: `grep -E "MAX_LOG|BACKUP" ~/polymarket-arb-bot/src/config/constants.py`
3. Test manually: `python3 -c "from src.utils.logger import setup_logging; setup_logging()"`

### Lost old logs

Python's RotatingFileHandler automatically deletes them - this is by design!
If you need longer retention:
1. Increase `LOG_BACKUP_COUNT` in `constants.py`
2. Or archive them externally before deletion

## Best Practices

✅ **Keep automatic rotation enabled** (already done)  
✅ **Run manual cleanup weekly** (optional)  
✅ **Monitor disk space monthly**  
✅ **Don't increase log retention beyond 30 days** (not needed)  
✅ **Compress old logs if disk space is limited**

## Quick Reference

```bash
# View recent logs
tail -f ~/polymarket-arb-bot/logs/bot_stdout.log

# Check log size
du -sh ~/polymarket-arb-bot/logs

# Manual cleanup
./scripts/cleanup_logs.sh

# Setup auto-cleanup (cron)
crontab -e
# Add: 0 3 * * * /home/ubuntu/polymarket-arb-bot/scripts/cleanup_logs.sh --days 7
```
