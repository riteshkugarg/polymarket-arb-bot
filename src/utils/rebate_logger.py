"""
Maker Rebate Tracking Logger

Logs successful maker fills for Polymarket rebate eligibility verification.
Tracks order_id, fill_amount, fee_rate, and execution details.

Output Format: JSONL (JSON Lines) for easy parsing and analysis
File Location: logs/maker_rebates.jsonl
"""

import json
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional
from decimal import Decimal

from config.constants import ENABLE_REBATE_TRACKING, REBATE_LOG_FILE
from utils.logger import get_logger

logger = get_logger(__name__)


class RebateLogger:
    """
    Tracks successful maker order fills for rebate eligibility verification.
    
    Logs include:
    - Order ID (for Polymarket support verification)
    - Fill amount (shares filled)
    - Fee rate in basis points
    - Trade side (BUY/SELL)
    - Token ID
    - Execution timestamp
    - Estimated maker volume (USD)
    """
    
    def __init__(self):
        self.log_file = Path(REBATE_LOG_FILE)
        self.enabled = ENABLE_REBATE_TRACKING
        self._lock = asyncio.Lock()
        self._ensure_log_directory()
    
    def _ensure_log_directory(self) -> None:
        """Create log directory if it doesn't exist"""
        if self.enabled:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
    
    async def log_maker_fill(
        self,
        order_id: str,
        token_id: str,
        side: str,
        fill_amount: float,
        fill_price: float,
        fee_rate_bps: int,
        market_name: Optional[str] = None,
        outcome: Optional[str] = None,
        is_post_only: bool = True,
        additional_data: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log a successful maker order fill.
        
        Args:
            order_id: Polymarket order ID
            token_id: Token identifier
            side: BUY or SELL
            fill_amount: Number of shares filled
            fill_price: Fill price per share
            fee_rate_bps: Fee rate in basis points (0 for maker)
            market_name: Human-readable market name
            outcome: Outcome being traded (Yes/No/etc)
            is_post_only: Whether order used post_only flag
            additional_data: Extra metadata to include
        """
        if not self.enabled:
            return
        
        try:
            # Calculate maker volume (USD)
            maker_volume_usd = fill_amount * fill_price
            
            # Calculate fees paid (should be 0 for maker)
            fees_paid = maker_volume_usd * (fee_rate_bps / 10000)
            
            # Build log entry
            log_entry = {
                # Core identification
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "order_id": order_id,
                "token_id": token_id,
                
                # Trade details
                "side": side,
                "fill_amount_shares": fill_amount,
                "fill_price": fill_price,
                "maker_volume_usd": round(maker_volume_usd, 2),
                
                # Fee details
                "fee_rate_bps": fee_rate_bps,
                "fees_paid_usd": round(fees_paid, 4),
                "is_maker": is_post_only,  # True if post_only order
                
                # Market context
                "market_name": market_name,
                "outcome": outcome,
                
                # Metadata
                "bot_version": "2.0",
                "execution_type": "post_only" if is_post_only else "market"
            }
            
            # Add additional data if provided
            if additional_data:
                log_entry["metadata"] = additional_data
            
            # Write to JSONL file (one JSON object per line)
            async with self._lock:
                with open(self.log_file, 'a') as f:
                    f.write(json.dumps(log_entry) + '\n')
            
            # Also log to main logger for visibility
            logger.info(
                f"💰 MAKER_FILL: order={order_id[:8]}... "
                f"volume=${maker_volume_usd:.2f} "
                f"fee={fee_rate_bps}bps "
                f"shares={fill_amount:.2f}@${fill_price:.4f}"
            )
            
        except Exception as e:
            logger.error(f"Failed to log maker rebate: {e}", exc_info=True)
    
    async def get_total_maker_volume(
        self,
        since_timestamp: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Calculate total maker volume from logs.
        
        Args:
            since_timestamp: ISO timestamp to calculate from (default: all time)
            
        Returns:
            Dictionary with volume statistics:
            - total_volume_usd: Total maker volume
            - total_orders: Number of fills
            - total_fees_paid: Total fees (should be ~0 for makers)
            - average_fill_size: Average fill size
        """
        if not self.enabled or not self.log_file.exists():
            return {
                "total_volume_usd": 0,
                "total_orders": 0,
                "total_fees_paid": 0,
                "average_fill_size": 0
            }
        
        try:
            total_volume = 0.0
            total_orders = 0
            total_fees = 0.0
            
            with open(self.log_file, 'r') as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        
                        # Filter by timestamp if specified
                        if since_timestamp:
                            if entry.get('timestamp', '') < since_timestamp:
                                continue
                        
                        # Accumulate statistics
                        total_volume += entry.get('maker_volume_usd', 0)
                        total_orders += 1
                        total_fees += entry.get('fees_paid_usd', 0)
                        
                    except json.JSONDecodeError:
                        continue
            
            avg_fill = total_volume / total_orders if total_orders > 0 else 0
            
            return {
                "total_volume_usd": round(total_volume, 2),
                "total_orders": total_orders,
                "total_fees_paid": round(total_fees, 4),
                "average_fill_size": round(avg_fill, 2)
            }
            
        except Exception as e:
            logger.error(f"Failed to calculate maker volume: {e}")
            return {
                "total_volume_usd": 0,
                "total_orders": 0,
                "total_fees_paid": 0,
                "average_fill_size": 0
            }


# Global rebate logger instance
_rebate_logger = None


def get_rebate_logger() -> RebateLogger:
    """Get global rebate logger instance"""
    global _rebate_logger
    if _rebate_logger is None:
        _rebate_logger = RebateLogger()
    return _rebate_logger
