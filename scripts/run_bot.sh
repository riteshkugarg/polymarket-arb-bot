#!/bin/bash

###############################################################################
# Polymarket Arbitrage Bot - Production Run Script
# This script manages the bot lifecycle on AWS EC2 Ubuntu instances
###############################################################################

set -e  # Exit on error

# Configuration
BOT_DIR="/home/ubuntu/polymarket-arb-bot"
VENV_DIR="$BOT_DIR/venv"
LOG_DIR="$BOT_DIR/logs"
PID_FILE="$LOG_DIR/bot.pid"
PYTHON_BIN="$VENV_DIR/bin/python"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Functions
log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_requirements() {
    log_info "Checking requirements..."
    
    # Check Python version
    if ! command -v python3.10 &> /dev/null; then
        log_error "Python 3.10+ is required"
        exit 1
    fi
    
    # Check if running on EC2
    if [ -f /sys/hypervisor/uuid ] && [ `head -c 3 /sys/hypervisor/uuid` == ec2 ]; then
        log_info "Running on AWS EC2 instance"
    else
        log_warn "Not running on AWS EC2 instance"
    fi
    
    # Create log directory
    mkdir -p "$LOG_DIR"
}

setup_environment() {
    log_info "Setting up environment..."
    
    cd "$BOT_DIR"
    
    # Create virtual environment if it doesn't exist
    if [ ! -d "$VENV_DIR" ]; then
        log_info "Creating virtual environment..."
        python3.10 -m venv "$VENV_DIR"
    fi
    
    # Activate virtual environment
    source "$VENV_DIR/bin/activate"
    
    # Upgrade pip
    pip install --upgrade pip
    
    # Install dependencies
    log_info "Installing dependencies..."
    pip install -r requirements.txt
}

start_bot() {
    log_info "Starting Polymarket Bot..."
    
    # Check if already running
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ps -p "$PID" > /dev/null 2>&1; then
            log_warn "Bot is already running (PID: $PID)"
            exit 0
        else
            log_warn "Removing stale PID file"
            rm -f "$PID_FILE"
        fi
    fi
    
    # Activate virtual environment
    source "$VENV_DIR/bin/activate"
    
    # Start bot in background
    cd "$BOT_DIR/src"
    nohup "$PYTHON_BIN" -m main > "$LOG_DIR/bot_stdout.log" 2>&1 &
    
    # Save PID
    echo $! > "$PID_FILE"
    
    log_info "Bot started with PID: $(cat $PID_FILE)"
    log_info "Logs: $LOG_DIR/polymarket_bot.log"
}

stop_bot() {
    log_info "Stopping Polymarket Bot..."
    
    if [ ! -f "$PID_FILE" ]; then
        log_warn "Bot is not running (no PID file)"
        exit 0
    fi
    
    PID=$(cat "$PID_FILE")
    
    if ps -p "$PID" > /dev/null 2>&1; then
        # Send SIGTERM for graceful shutdown
        kill -TERM "$PID"
        
        # Wait for shutdown (max 30 seconds)
        for i in {1..30}; do
            if ! ps -p "$PID" > /dev/null 2>&1; then
                log_info "Bot stopped gracefully"
                rm -f "$PID_FILE"
                exit 0
            fi
            sleep 1
        done
        
        # Force kill if still running
        log_warn "Bot did not stop gracefully, forcing shutdown..."
        kill -KILL "$PID"
        rm -f "$PID_FILE"
        log_info "Bot stopped forcefully"
    else
        log_warn "Bot is not running (stale PID file)"
        rm -f "$PID_FILE"
    fi
}

restart_bot() {
    log_info "Restarting Polymarket Bot..."
    stop_bot
    sleep 2
    start_bot
}

status_bot() {
    if [ ! -f "$PID_FILE" ]; then
        log_info "Bot is NOT running"
        exit 1
    fi
    
    PID=$(cat "$PID_FILE")
    
    if ps -p "$PID" > /dev/null 2>&1; then
        log_info "Bot is RUNNING (PID: $PID)"
        
        # Show resource usage
        ps -p "$PID" -o pid,ppid,%cpu,%mem,etime,cmd
        
        exit 0
    else
        log_info "Bot is NOT running (stale PID file)"
        rm -f "$PID_FILE"
        exit 1
    fi
}

show_logs() {
    if [ -f "$LOG_DIR/polymarket_bot.log" ]; then
        tail -f "$LOG_DIR/polymarket_bot.log"
    else
        log_error "Log file not found"
        exit 1
    fi
}

# Main script
case "$1" in
    setup)
        check_requirements
        setup_environment
        log_info "Setup complete"
        ;;
    start)
        check_requirements
        start_bot
        ;;
    stop)
        stop_bot
        ;;
    restart)
        restart_bot
        ;;
    status)
        status_bot
        ;;
    logs)
        show_logs
        ;;
    *)
        echo "Usage: $0 {setup|start|stop|restart|status|logs}"
        echo ""
        echo "Commands:"
        echo "  setup   - Setup environment and install dependencies"
        echo "  start   - Start the bot"
        echo "  stop    - Stop the bot"
        echo "  restart - Restart the bot"
        echo "  status  - Check bot status"
        echo "  logs    - View bot logs (tail -f)"
        exit 1
        ;;
esac

exit 0
