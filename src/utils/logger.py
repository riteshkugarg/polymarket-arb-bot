"""
Production-Grade Logging Module for Polymarket Arbitrage Bot

Provides structured logging with:
- Rotating file handlers for 24/7 operation
- JSON formatting for log aggregation
- Contextual information (timestamps, process ID, etc.)
- Multiple severity levels
- Performance optimized for high-frequency trading

Usage:
    logger = get_logger(__name__)
    logger.info("Trade executed", extra={'trade_id': '123', 'amount': 100})
"""

import logging
import logging.handlers
import os
import sys
import json
from datetime import datetime
from typing import Optional, Dict, Any

# Import constants (with fallback for testing)
try:
    from config.constants import LOG_LEVEL, LOG_FILE_PATH, MAX_LOG_FILE_SIZE, LOG_BACKUP_COUNT, STRUCTURED_LOGGING
except ImportError:
    LOG_LEVEL = 'INFO'
    LOG_FILE_PATH = 'logs/polymarket_bot.log'
    MAX_LOG_FILE_SIZE = 50 * 1024 * 1024
    LOG_BACKUP_COUNT = 10
    STRUCTURED_LOGGING = True


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs structured JSON logs"""

    def format(self, record: logging.LogRecord) -> str:
        """
        Format log record as JSON for better parsing and aggregation.
        Includes all relevant context in structured format.
        """
        log_data = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line_number': record.lineno,
            'process_id': record.process,
            'thread_id': record.thread,
        }

        # Add exception information if present
        if record.exc_info:
            log_data['exception'] = {
                'type': record.exc_info[0].__name__,
                'message': str(record.exc_info[1]),
                'traceback': self.formatException(record.exc_info)
            }

        # Add any extra fields passed via logger.info(..., extra={...})
        if hasattr(record, '__dict__'):
            for key, value in record.__dict__.items():
                if key not in [
                    'name', 'msg', 'args', 'created', 'filename', 'funcName',
                    'levelname', 'levelno', 'lineno', 'module', 'msecs',
                    'message', 'pathname', 'process', 'processName', 'relativeCreated',
                    'thread', 'threadName', 'exc_info', 'exc_text', 'stack_info',
                    'getMessage', 'taskName'  # Python 3.12+
                ]:
                    if isinstance(value, (str, int, float, bool, type(None), dict)):
                        log_data[key] = value

        return json.dumps(log_data)


class PlainTextFormatter(logging.Formatter):
    """Simple text formatter for readable console output"""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as plain text for console"""
        # Generate asctime attribute (required before accessing record.asctime)
        record.asctime = self.formatTime(record, self.datefmt)
        
        if record.exc_info:
            # Include exception details
            exc_text = self.formatException(record.exc_info)
            return (
                f"{record.asctime} | {record.levelname:8} | "
                f"{record.name}:{record.funcName}:{record.lineno} | "
                f"{record.getMessage()}\n{exc_text}"
            )
        else:
            return (
                f"{record.asctime} | {record.levelname:8} | "
                f"{record.name}:{record.funcName}:{record.lineno} | "
                f"{record.getMessage()}"
            )


def setup_logging(
    log_level: Optional[str] = None,
    log_file: Optional[str] = None,
    structured: Optional[bool] = None
) -> None:
    """
    Configure production-grade logging for the bot.

    Sets up:
    - Console handler: Plain text for operator visibility
    - File handler: Rotating files to prevent disk space issues
    - JSON formatting: Structured logs for log aggregation tools

    Args:
        log_level: Logging level override (DEBUG, INFO, WARNING, ERROR, CRITICAL)
                   If None, uses LOG_LEVEL from constants
        log_file: Log file path override. If None, uses LOG_FILE_PATH from constants
        structured: Use JSON formatting. If None, uses STRUCTURED_LOGGING from constants

    Raises:
        ValueError: If invalid log level specified
    """
    # Use provided values or fall back to constants
    level = log_level or LOG_LEVEL
    filepath = log_file or LOG_FILE_PATH
    use_json = structured if structured is not None else STRUCTURED_LOGGING

    # Validate log level
    valid_levels = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
    if level.upper() not in valid_levels:
        raise ValueError(f"Invalid log level: {level}. Must be one of {valid_levels}")

    level = level.upper()

    # Create logs directory if it doesn't exist
    log_dir = os.path.dirname(filepath)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level))

    # Clear any existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # ========================================================================
    # CONSOLE HANDLER - for operator visibility
    # ========================================================================
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level))
    console_formatter = PlainTextFormatter(
        fmt='%(asctime)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # ========================================================================
    # FILE HANDLER - rotating files for 24/7 operation
    # ========================================================================
    file_handler = logging.handlers.RotatingFileHandler(
        filepath,
        maxBytes=MAX_LOG_FILE_SIZE,  # Rotate after this size
        backupCount=LOG_BACKUP_COUNT  # Keep this many backups
    )
    file_handler.setLevel(getattr(logging, level))

    if use_json:
        file_formatter = JSONFormatter()
    else:
        file_formatter = PlainTextFormatter(
            fmt='%(asctime)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)

    # Log initialization
    logger = get_logger(__name__)
    logger.info(
        "Logging initialized",
        extra={
            'log_level': level,
            'log_file': filepath,
            'max_size_mb': MAX_LOG_FILE_SIZE // (1024 * 1024),
            'backup_count': LOG_BACKUP_COUNT,
            'structured_logging': use_json,
        }
    )


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a specific module.

    Args:
        name: Logger name (typically __name__ from calling module)

    Returns:
        Configured logger instance

    Example:
        logger = get_logger(__name__)
        logger.info("Bot started")
        logger.warning("High slippage detected", extra={'slippage': 2.5})
        logger.error("Order failed", exc_info=True)
    """
    return logging.getLogger(name)


def log_trade_event(
    logger: logging.Logger,
    event_type: str,
    **details
) -> None:
    """
    Log a trading event with structured information.

    Args:
        logger: Logger instance
        event_type: Type of trade event (BUY, SELL, FAILED, etc.)
        **details: Trade details to include (order_id, price, size, etc.)

    Example:
        log_trade_event(
            logger, 'ORDER_PLACED',
            order_id='123', market='YES', size=1.0, price=0.45
        )
    """
    details['event_type'] = event_type
    logger.info(f"Trade event: {event_type}", extra=details)


def log_error_with_context(
    logger: logging.Logger,
    message: str,
    error: Exception,
    **context
) -> None:
    """
    Log an error with full context and exception details.

    Args:
        logger: Logger instance
        message: Error description
        error: The exception that occurred
        **context: Additional context information

    Example:
        try:
            place_order(...)
        except OrderRejectionError as e:
            log_error_with_context(
                logger, "Failed to place order", e,
                market='YES', size=1.0, reason='insufficient_balance'
            )
    """
    context['error_type'] = type(error).__name__
    context['error_message'] = str(error)
    logger.error(message, exc_info=error, extra=context)
