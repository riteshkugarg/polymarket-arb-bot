"""
WebSocket event source for Polymarket whale trade detection.
Switches to event-driven mode if enabled in config.
"""


import asyncio
import json
import logging
import websockets
import aiohttp
from config.constants import POLYMARKET_WEBSOCKET_URL, MIRROR_TARGET

logger = logging.getLogger(__name__)

class WhaleWebSocketListener:

    def __init__(self, on_trade_callback, target_address=None, market_slug="us-presidential-election"):
        self.ws_url = POLYMARKET_WEBSOCKET_URL
        self.on_trade_callback = on_trade_callback
        self.target_address = target_address or MIRROR_TARGET
        self.market_slug = market_slug
        self._running = False

    async def fetch_token_ids(self):
        url = f"https://gamma-api.polymarket.com/events?slug={self.market_slug}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.error(f"Failed to fetch token IDs from Gamma API: {resp.status}")
                    return []
                data = await resp.json()
                # clobTokenIds is a list of token IDs for outcomes
                token_ids = data.get("clobTokenIds", [])
                outcomes = data.get("outcomes", [])
                logger.info(f"Fetched token IDs: {token_ids} for outcomes: {outcomes}")
                return token_ids

    async def listen(self):
        self._running = True
        token_ids = await self.fetch_token_ids()
        if not token_ids:
            logger.error("No token IDs found. WebSocket listener will not start.")
            return
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    logger.info(f"Connected to Polymarket WebSocket: {self.ws_url}")
                    subscribe_msg = json.dumps({
                        "type": "market",
                        "assets_ids": token_ids
                    })
                    await ws.send(subscribe_msg)
                    logger.info(f"Subscribed to market data for tokenIds: {token_ids}")
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            if data.get("type") == "market":
                                market_data = data.get("data", {})
                                await self.on_trade_callback(market_data)
                        except Exception as e:
                            logger.warning(f"Error processing WebSocket message: {e}")
            except Exception as e:
                logger.error(f"WebSocket connection error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    def stop(self):
        self._running = False
