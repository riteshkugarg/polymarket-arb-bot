"""
Fee-Rate Checking Utility for 15-Minute Market Identification

POLYMARKET GUIDANCE (Q36/Q37 - Jan 2026):
- 15-minute crypto markets have taker fees enabled (Maker Rebates program)
- Use CLOB /fee-rate endpoint to check: fee_rate_bps > 0 = 15-minute market
- This is the ONLY reliable way to identify 15-minute markets programmatically

Usage:
    fee_bps = await check_fee_rate(token_id)
    is_15min_market = fee_bps > 0
"""

import aiohttp
import logging
from typing import Optional

from src.config.constants import CLOB_API_URL

logger = logging.getLogger(__name__)


async def check_fee_rate(token_id: str, session: Optional[aiohttp.ClientSession] = None) -> int:
    """
    Check if a market has taker fees enabled (15-minute market indicator).
    
    POLYMARKET Q37: "Use the CLOB fee-rate endpoint (separate call)"
    GET https://clob.polymarket.com/fee-rate?token_id={token_id}
    
    Returns:
        fee_rate_bps (int): Basis points (1000 = 10% fee)
                            > 0 = 15-minute market (fee-enabled)
                            = 0 = Standard market (fee-free)
    
    Example:
        >>> fee_bps = await check_fee_rate("some_token_id")
        >>> if fee_bps > 0:
        ...     print("This is a 15-minute crypto market!")
    """
    url = f"{CLOB_API_URL}/fee-rate"
    params = {'token_id': token_id}
    
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
    
    try:
        async with session.get(url, params=params, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                fee_rate_bps = data.get('fee_rate_bps', 0)
                return fee_rate_bps
            else:
                logger.warning(f"Fee-rate check failed for token_id={token_id}: HTTP {resp.status}")
                return 0
    except Exception as e:
        logger.debug(f"Error checking fee-rate for token_id={token_id}: {e}")
        return 0
    finally:
        if close_session:
            await session.close()


async def is_15min_market(token_id: str, session: Optional[aiohttp.ClientSession] = None) -> bool:
    """
    Check if a market is a 15-minute crypto market (fee-enabled).
    
    POLYMARKET Q37: "fee_rate_bps > 0 is your best indicator"
    
    Returns:
        True if 15-minute market (taker fees enabled)
        False otherwise
    """
    fee_bps = await check_fee_rate(token_id, session)
    return fee_bps > 0


async def check_market_fees(market: dict, session: Optional[aiohttp.ClientSession] = None) -> dict:
    """
    Check fee rates for all outcomes in a market.
    
    Args:
        market: Market object with 'clobTokenIds' field
        session: Optional aiohttp session for connection pooling
    
    Returns:
        Dict with fee information:
        {
            'is_15min': bool,           # Any outcome has fees enabled
            'token_fees': {             # Fee rate per token
                'token_id_1': 1000,     # 10% fee
                'token_id_2': 0,        # Fee-free
            }
        }
    """
    # Parse clobTokenIds (may be JSON string or list)
    clob_token_ids = market.get('clobTokenIds', [])
    if isinstance(clob_token_ids, str):
        import json
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except:
            logger.warning(f"Failed to parse clobTokenIds for market {market.get('id')}")
            return {'is_15min': False, 'token_fees': {}}
    
    # Check fee rate for each token
    token_fees = {}
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
    
    try:
        for token_id in clob_token_ids:
            fee_bps = await check_fee_rate(token_id, session)
            token_fees[token_id] = fee_bps
        
        # Market is 15-minute if ANY outcome has fees enabled
        is_15min = any(fee > 0 for fee in token_fees.values())
        
        return {
            'is_15min': is_15min,
            'token_fees': token_fees
        }
    finally:
        if close_session:
            await session.close()


# Optional: Add to market scanning for 15-minute identification
async def annotate_15min_markets(markets: list, session: Optional[aiohttp.ClientSession] = None) -> list:
    """
    Annotate markets with 15-minute flag (for filtering/prioritization).
    
    POLYMARKET Q38: "Optionally add fee_rate_bps > 0 as a separate '15-minute crypto' segment"
    
    Args:
        markets: List of market objects
        session: Optional aiohttp session for connection pooling
    
    Returns:
        Same markets list with added 'is_15min_market' field
    
    Note: This adds API calls (1 per token), use sparingly.
          Better approach: Check fee-rate only for selected markets.
    """
    close_session = False
    if session is None:
        session = aiohttp.ClientSession()
        close_session = True
    
    try:
        for market in markets:
            fee_info = await check_market_fees(market, session)
            market['is_15min_market'] = fee_info['is_15min']
            market['token_fees'] = fee_info['token_fees']
        
        return markets
    finally:
        if close_session:
            await session.close()
