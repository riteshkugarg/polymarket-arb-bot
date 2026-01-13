#!/bin/bash

###############################################################################
# Health Check Script for Polymarket Bot
# Used for monitoring and alerting
###############################################################################

set -e

# Configuration
BOT_DIR="/workspaces/polymarket-arb-bot"
LOG_DIR="$BOT_DIR/logs"
PID_FILE="$LOG_DIR/bot.pid"
LOG_FILE="$LOG_DIR/polymarket_bot.log"

# Exit codes
EXIT_SUCCESS=0
EXIT_NOT_RUNNING=1
EXIT_UNHEALTHY=2

# Check if bot process is running
check_process() {
    if [ ! -f "$PID_FILE" ]; then
        echo "UNHEALTHY: No PID file found"
        exit $EXIT_NOT_RUNNING
    fi
    
    PID=$(cat "$PID_FILE")
    
    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "UNHEALTHY: Bot process not running"
        exit $EXIT_NOT_RUNNING
    fi
    
    echo "Process: OK (PID $PID)"
}

# Check log file for recent activity
check_log_activity() {
    if [ ! -f "$LOG_FILE" ]; then
        echo "WARNING: Log file not found"
        return
    fi
    
    # Check if log has been updated in last 5 minutes
    LAST_MOD=$(stat -c %Y "$LOG_FILE")
    NOW=$(date +%s)
    DIFF=$((NOW - LAST_MOD))
    
    if [ $DIFF -gt 300 ]; then
        echo "WARNING: Log file not updated in $((DIFF/60)) minutes"
    else
        echo "Log activity: OK"
    fi
}

# Check for recent errors in logs
check_errors() {
    if [ ! -f "$LOG_FILE" ]; then
        return
    fi
    
    # Count critical/error logs in last 100 lines
    ERROR_COUNT=$(tail -100 "$LOG_FILE" | grep -ci "ERROR\|CRITICAL" || true)
    
    if [ $ERROR_COUNT -gt 10 ]; then
        echo "WARNING: $ERROR_COUNT errors in recent logs"
    else
        echo "Error rate: OK"
    fi
}

# Check disk space
check_disk_space() {
    DISK_USAGE=$(df -h "$BOT_DIR" | awk 'NR==2 {print $5}' | sed 's/%//')
    
    if [ $DISK_USAGE -gt 90 ]; then
        echo "WARNING: Disk usage at ${DISK_USAGE}%"
    else
        echo "Disk space: OK (${DISK_USAGE}% used)"
    fi
}

# Check memory usage
check_memory() {
    if [ ! -f "$PID_FILE" ]; then
        return
    fi
    
    PID=$(cat "$PID_FILE")
    
    if ps -p "$PID" > /dev/null 2>&1; then
        MEM_USAGE=$(ps -p "$PID" -o %mem --no-headers | awk '{print int($1)}')
        
        if [ $MEM_USAGE -gt 80 ]; then
            echo "WARNING: Memory usage at ${MEM_USAGE}%"
        else
            echo "Memory: OK (${MEM_USAGE}%)"
        fi
    fi
}

# Main health check
echo "================================"
echo "Polymarket Bot Health Check"
echo "================================"
echo "Time: $(date)"
echo ""

check_process
check_log_activity
check_errors
check_disk_space
check_memory

echo ""
echo "================================"
echo "Health check complete"
echo "================================"

exit $EXIT_SUCCESS
