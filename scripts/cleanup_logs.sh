#!/bin/bash
#
# Log Cleanup Script for Polymarket Bot
# 
# Usage:
#   ./scripts/cleanup_logs.sh [options]
#
# Options:
#   --days N       Delete logs older than N days (default: 7)
#   --size-mb N    Delete logs if total size exceeds N MB (default: 500)
#   --dry-run      Show what would be deleted without actually deleting
#
# This script is safe to run manually or via cron job.
# It will NEVER delete the current active log file.
#

set -e  # Exit on error

# Default settings
DAYS_OLD=7
MAX_SIZE_MB=500
DRY_RUN=false
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$BOT_DIR/logs"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --days)
            DAYS_OLD="$2"
            shift 2
            ;;
        --size-mb)
            MAX_SIZE_MB="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help)
            head -n 20 "$0" | grep "^#"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Check if log directory exists
if [ ! -d "$LOG_DIR" ]; then
    echo "Log directory not found: $LOG_DIR"
    exit 1
fi

cd "$LOG_DIR"

echo "=================================================="
echo "Polymarket Bot - Log Cleanup"
echo "=================================================="
echo "Log directory: $LOG_DIR"
echo "Delete files older than: $DAYS_OLD days"
echo "Max total size: $MAX_SIZE_MB MB"
echo "Dry run: $DRY_RUN"
echo ""

# Get current log size
TOTAL_SIZE_KB=$(du -sk . 2>/dev/null | cut -f1)
TOTAL_SIZE_MB=$((TOTAL_SIZE_KB / 1024))
echo "Current log directory size: $TOTAL_SIZE_MB MB"

# Find old backup log files (but never delete active logs)
# Pattern matches: bot_stdout.log.1, bot_stderr.log.2, polymarket_bot.log.3, etc.
OLD_FILES=$(find . -maxdepth 1 -type f \
    \( -name "*.log.[0-9]*" -o -name "*.log.[0-9]*.*" \) \
    -mtime +$DAYS_OLD 2>/dev/null | sort)

if [ -z "$OLD_FILES" ]; then
    echo "No old log files found (older than $DAYS_OLD days)"
else
    echo ""
    echo "Found old log files (older than $DAYS_OLD days):"
    echo "$OLD_FILES" | while read -r file; do
        SIZE=$(du -h "$file" 2>/dev/null | cut -f1)
        AGE=$(find "$file" -mtime +$DAYS_OLD -printf "%Td days ago\n" 2>/dev/null)
        echo "  - $file ($SIZE, $AGE)"
    done
    
    if [ "$DRY_RUN" = true ]; then
        echo ""
        echo "[DRY RUN] Would delete ${#OLD_FILES[@]} old log files"
    else
        echo ""
        read -p "Delete these files? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "$OLD_FILES" | while read -r file; do
                echo "Deleting: $file"
                rm -f "$file"
            done
            echo "Deleted old log files"
        else
            echo "Cancelled"
        fi
    fi
fi

# Check if we're over size limit
if [ $TOTAL_SIZE_MB -gt $MAX_SIZE_MB ]; then
    echo ""
    echo "WARNING: Log directory size ($TOTAL_SIZE_MB MB) exceeds limit ($MAX_SIZE_MB MB)"
    
    # Find oldest backup files to delete
    EXCESS_MB=$((TOTAL_SIZE_MB - MAX_SIZE_MB))
    echo "Need to free up approximately $EXCESS_MB MB"
    
    LARGE_FILES=$(find . -maxdepth 1 -type f \
        \( -name "*.log.[0-9]*" -o -name "*.log.[0-9]*.*" \) \
        -printf "%T@ %s %p\n" 2>/dev/null | sort -n | head -n 10)
    
    if [ -n "$LARGE_FILES" ]; then
        echo ""
        echo "Oldest backup files (candidates for deletion):"
        echo "$LARGE_FILES" | while read -r timestamp size file; do
            SIZE_MB=$((size / 1024 / 1024))
            echo "  - $file (${SIZE_MB} MB)"
        done
        
        if [ "$DRY_RUN" = false ]; then
            echo ""
            echo "Consider running with --days parameter to clean up more aggressively"
        fi
    fi
fi

# Summary
echo ""
echo "=================================================="
echo "Cleanup Summary:"
echo "  - Initial size: $TOTAL_SIZE_MB MB"

if [ "$DRY_RUN" = false ]; then
    NEW_SIZE_KB=$(du -sk . 2>/dev/null | cut -f1)
    NEW_SIZE_MB=$((NEW_SIZE_KB / 1024))
    FREED_MB=$((TOTAL_SIZE_MB - NEW_SIZE_MB))
    echo "  - Final size: $NEW_SIZE_MB MB"
    echo "  - Space freed: $FREED_MB MB"
else
    echo "  - No changes made (dry run)"
fi
echo "=================================================="
echo ""
echo "Tip: Add this to crontab for automatic cleanup:"
echo "  # Run daily at 3 AM"
echo "  0 3 * * * $BOT_DIR/scripts/cleanup_logs.sh --days 7"
echo ""
