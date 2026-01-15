# Polymarket WebSocket Implementation - Questions for Support Team

**Date:** January 15, 2026  
**Bot:** Polymarket HFT Arbitrage & Market Making Bot  
**Current Implementation:** [market_data_manager.py](src/core/market_data_manager.py)

---

## 📋 Current Implementation Summary

## ✅ **RESOLVED** - Polymarket Support Response (Jan 15, 2026)

### 1. WebSocket Subscription Format

**✅ CONFIRMED FORMAT:**
```json
{
  "type": "market",
  "assets_ids": ["21742633143463906290569050155826241533067272736897614950488156847949938836455"]
}
```

**Key Points:**
- Use `"type": "market"` (not "subscribe")
- Use `"assets_ids"` as **array** (plural, not singular "asset_id")
- No `"channel"` field needed
- **FIXED** in code

### 2. User Channel Authentication

**✅ CONFIRMED FORMAT:**
```json
{
  "type": "user",
  "markets": [],
  "auth": {
    "apiKey": "your-api-key",
    "secret": "your-api-secret",
    "passphrase": "your-api-passphrase"
  }
}
```

**Key Points:**
- User channel **requires authentication**
- Use `apiKey`, `secret`, `passphrase` (not wallet address)
- Empty `markets` array = subscribe to all markets
- **FIXED** in code

### 3. WebSocket Message Types

**✅ CONFIRMED MESSAGE TYPES:**

**Market Channel:**
- `book` - Full orderbook snapshot
- `price_change` - Price update event
- `last_trade_price` - Last executed trade

**User Channel:**
- `order` - Order events with `type` field:
  - `PLACEMENT` - New order placed
  - `UPDATE` - Order updated
  - `CANCELLATION` - Order cancelled
  - (Fills indicated by `size_matched > 0`)

**Message Structure:**
```json
{
  "event_type": "book",
  "asset_id": "...",
  "market": "condition_id",
  "timestamp": 1234567890,
  "hash": "...",
  "buys": [{"price": "0.50", "size": "100"}, ...],
  "sells": [{"price": "0.51", "size": "100"}, ...]
}
```

**Key Points:**
- Messages use **`event_type`** field (not `type`)
- Order books use **`buys`/`sells`** (not `bids`/`asks`)
- **FIXED** in code

### 4. Heartbeat & Connection Management

**✅ CONFIRMED:**
- Send PING approximately every **5 seconds** to maintain connection
- Our implementation is **correct**
- No changes needed

### 5. Reconnection & State Recovery

**✅ APPROACH CONFIRMED:**
- Re-subscribe to all previous assets after reconnection
- Re-fetch order books via REST API for state rehydration
- Our implementation is **correct**

### 6. Order Book Data Format

**✅ CONFIRMED FORMAT:**
```json
{
  "event_type": "book",
  "asset_id": "token_id_here",
  "market": "condition_id_here",
  "timestamp": 1234567890,
  "hash": "checksum_here",
  "buys": [
    {"price": "0.50", "size": "100"},
    {"price": "0.49", "size": "50"}
  ],
  "sells": [
    {"price": "0.51", "size": "100"},
    {"price": "0.52", "size": "75"}
  ]
}
```

**Key Points:**
- **`buys`** array = bid orders (descending by price)
- **`sells`** array = ask orders (ascending by price)
- `size` is in **shares** (not USD)
- **FIXED** in code

### 7. Incremental Updates vs Full Snapshots

**✅ CONFIRMED:**
- Polymarket sends **full snapshots** on every `book` event
- No incremental deltas - each message contains complete order book
- Our implementation treating them as snapshots is **correct**

### 8-10. Rate Limits, Authentication, Unsubscribe

**✅ CONFIRMED:**
- **No specified max assets** or subscription rate limit (use reasonable limits)
- **Unsubscribe format**: `{"operation": "unsubscribe", "assets_ids": [...]}` for market channel
- **Public market data does NOT require auth** (only user channel does)

### 11. Ping/Pong Frame Handling

**✅ CONFIRMED:**
- Send PING every ~5 seconds
- `websockets` library handles PONG responses automatically
- Our non-JSON frame filtering is **correct**

### 12. Multi-Market Subscriptions

**✅ CONFIRMED:**
- Can subscribe to multiple assets in single message: `{"type":"market","assets_ids":[id1,id2,...]}`
- No max concurrent subscriptions specified (docs don't mention limits)
- Single WebSocket connection for multiple markets is **correct approach**

---

## 🎉 **ALL ISSUES RESOLVED**

### Changes Made:

1. ✅ **Subscription format** - Changed to `{"type":"market","assets_ids":[...]}`
2. ✅ **Message parsing** - Now checks `event_type` field first
3. ✅ **Order book fields** - Handles both `buys`/`sells` and `bids`/`asks` (backward compatible)
4. ✅ **User authentication** - Uses `apiKey`, `secret`, `passphrase`
5. ✅ **Order events** - Properly parses `event_type: "order"` with `type` and `size_matched`

### Files Modified:

- [src/core/market_data_manager.py](src/core/market_data_manager.py) - All WebSocket communication fixed

---

Our bot uses a centralized WebSocket architecture with:
- **URL:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Heartbeat:** 5-second PING/PONG interval
- **Subscriptions:** Dynamic per-asset orderbook subscriptions
- **Channels:** `orderbook` channel for price data, `user` channel for fills

---

## ❓ Critical Questions for Polymarket Support

### 1. WebSocket Subscription Format

**Current Implementation (Line 808-812):**
```python
subscribe_msg = {
    "type": "subscribe",
    "channel": "orderbook",
    "asset_id": asset_id,
}
await self._ws.send(json.dumps(subscribe_msg))
```

**Questions:**
- ✅ Is the subscription format correct for orderbook data?
- ✅ Should we use `"asset_id"` or `"token_id"` in the subscription message?
- ✅ Do we need to include any authentication headers in the subscription message?
- ✅ Is there a maximum number of assets we can subscribe to simultaneously?
- ✅ What is the correct unsubscribe format if we need to remove assets dynamically?

**Expected Response Format:**
```json
{
  "type": "book",
  "asset_id": "...",
  "bids": [{"price": "0.50", "size": "100"}, ...],
  "asks": [{"price": "0.51", "size": "100"}, ...],
  "timestamp": 1234567890
}
```

---

### 2. User Channel Authentication

**Current Implementation (Line 825-838):**
```python
subscribe_msg = {
    "type": "subscribe",
    "channel": "user",
    "auth": {
        "address": self.client.get_address(),
        # Add signature if required
    }
}
```

**Questions:**
- ❓ Does the `/user` channel require authentication?
- ❓ If yes, what is the correct authentication format?
  - Do we need to sign a message with our private key?
  - What should the message payload be?
  - Should we include `api_key`, `api_secret`, or `api_pass`?
- ❓ What fill event fields are included in the response?
  ```json
  {
    "type": "fill",
    "order_id": "...",
    "asset_id": "...",
    "side": "BUY",
    "price": "0.50",
    "size": "10",
    "timestamp": 1234567890
  }
  ```

---

### 3. WebSocket Message Types

**Current Message Type Handling (Line 572-591):**
```python
msg_type = data.get('type') or data.get('event_type')

if msg_type in ['book', 'last_trade_price', 'orderbook_delta']:
    # Order book update
    self._orderbook_queue.put_nowait(data)
    
elif msg_type in ['fill', 'order_filled', 'trade']:
    # Fill event
    self._fill_queue.put_nowait(data)
```

**Questions:**
- ✅ What are ALL possible message types we should expect?
  - `book` - Full orderbook snapshot?
  - `orderbook_delta` - Incremental updates?
  - `last_trade_price` - Last executed trade?
  - `fill` - User-specific fill notification?
  - `trade` - Public trade feed?
- ✅ Should we handle `orderbook_delta` differently from full `book` snapshots?
- ✅ Are there any error message types we should handle?
  ```json
  {
    "type": "error",
    "code": "...",
    "message": "..."
  }
  ```

---

### 4. Heartbeat & Connection Management

**Current Implementation (Line 506-540):**
```python
async def _heartbeat_loop(self):
    while self._is_running:
        await asyncio.sleep(self.heartbeat_interval)  # 5 seconds
        
        if self._is_connected and self._ws:
            ping_start = time.time()
            pong_waiter = await self._ws.ping()
            await asyncio.wait_for(pong_waiter, timeout=2.0)
            latency_ms = (time.time() - ping_start) * 1000
```

**Questions:**
- ✅ Is 5 seconds the recommended heartbeat interval?
- ❓ Does Polymarket send any server-initiated ping messages we should respond to?
- ❓ What is the idle timeout before Polymarket closes inactive connections?
- ❓ Should we expect a specific `pong` message format, or does `websockets` library handle it?

---

### 5. Reconnection & State Recovery

**Current Implementation (Line 480-503):**
```python
async def _handle_reconnect(self):
    self._reconnect_attempts += 1
    delay = min(2 ** self._reconnect_attempts, self._max_reconnect_delay)  # Max 60s
    
    await asyncio.sleep(delay)
    await self._connect()
    
    # Rehydrate state from REST after reconnect
    if self._is_connected:
        await self._rehydrate_state()
```

**Questions:**
- ✅ After reconnection, should we:
  - Re-fetch full orderbooks via REST API?
  - Re-subscribe to all previous assets?
  - Assume we'll receive full `book` snapshots automatically?
- ❓ Is there a rate limit on reconnection attempts?
- ❓ Should we use a different WebSocket URL or authentication after disconnect?

---

### 6. Order Book Data Format

**Current Processing (Line 619-718):**
```python
bids = data.get('bids', [])
asks = data.get('asks', [])

best_bid = float(bids[0]['price'])
best_ask = float(asks[0]['price'])
bid_size = float(bids[0]['size'])
ask_size = float(asks[0]['size'])

# Calculate micro-price (volume-weighted mid)
total_vol = bid_size + ask_size
micro_price = ((bid_size * best_ask) + (ask_size * best_bid)) / total_vol
```

**Questions:**
- ✅ Are bids sorted **descending** (highest first)?
- ✅ Are asks sorted **ascending** (lowest first)?
- ✅ What is the format of each level?
  ```json
  {
    "price": "0.50",
    "size": "100"
  }
  ```
- ❓ Is `size` in **shares** or **USD**?
- ❓ How many levels are included in full `book` snapshots?
- ❓ Are there any metadata fields we should parse?
  ```json
  {
    "market_id": "...",
    "timestamp": 1234567890,
    "sequence": 12345
  }
  ```

---

### 7. Incremental Updates vs Full Snapshots

**Current Implementation:**
```python
# We treat all orderbook messages as full snapshots
snapshot = MarketSnapshot(
    asset_id=asset_id,
    best_bid=best_bid,
    best_ask=best_ask,
    bids=bids[:10],
    asks=asks[:10],
    last_update=time.time()
)
self.cache.update(asset_id, snapshot)
```

**Questions:**
- ❓ Does Polymarket send:
  - **Full snapshots** on every update?
  - **Incremental deltas** requiring orderbook reconstruction?
  - A mix (snapshot on subscribe, then deltas)?
- ❓ If incremental, what is the delta format?
  ```json
  {
    "type": "orderbook_delta",
    "asset_id": "...",
    "changes": [
      {"side": "bid", "price": "0.50", "size": "0"},  // Remove
      {"side": "ask", "price": "0.51", "size": "50"}  // Add/Update
    ]
  }
  ```
- ❓ Should we maintain a full orderbook in memory and apply deltas?

---

### 8. Latency & Performance

**Current Monitoring (Line 524-534):**
```python
latency_ms = (time.time() - ping_start) * 1000

# EMA smoothing
self._last_latency_ms = 0.9 * self._last_latency_ms + 0.1 * latency_ms
```

**Questions:**
- ❓ What is the typical WebSocket latency from Polymarket's infrastructure?
  - < 50ms (excellent)
  - 50-200ms (good)
  - > 200ms (degraded)
- ❓ Are there geographically distributed WebSocket endpoints?
  - If yes, what URLs should we use for different regions?
- ❓ Does Polymarket throttle WebSocket message rates?
  - If yes, what is the max messages/second?

---

### 9. Error Handling

**Current Implementation:**
```python
except ConnectionClosed:
    logger.warning("WebSocket connection closed")
    self._is_connected = False
    self.cache.trigger_disconnection_callbacks()
    await self._handle_reconnect()
```

**Questions:**
- ❓ What are common disconnect reasons?
  - Idle timeout?
  - Authentication expired?
  - Server maintenance?
- ❓ Should we parse close codes to determine reconnection strategy?
  ```python
  except ConnectionClosed as e:
      if e.code == 1000:  # Normal close
          # Don't reconnect
      elif e.code == 1006:  # Abnormal close
          # Reconnect immediately
  ```
- ❓ Are there any error messages sent via WebSocket before disconnection?

---

### 10. Rate Limits & Throttling

**Current Usage:**
- Single WebSocket connection
- Subscribe to ~5-10 assets simultaneously
- 5-second heartbeat
- No message bursts (only subscriptions)

**Questions:**
- ✅ Are there subscription rate limits?
  - Max subscriptions per second?
  - Max total subscribed assets?
- ❓ If we exceed limits, what error do we receive?
- ❓ Should we batch subscriptions?
  ```json
  {
    "type": "subscribe",
    "channel": "orderbook",
    "asset_ids": ["asset1", "asset2", ...]  // Batch?
  }
  ```

---

### 11. Ping/Pong Frame Handling

**Issue from Logs (Previous Session):**
```
ERROR: Expecting value: line 1 column 1 (char 0)
```

This occurred when trying to parse PING/PONG control frames as JSON.

**Current Fix (Line 567-569):**
```python
# Skip ping/pong frames and empty messages
if not message or not isinstance(message, str):
    continue
```

**Questions:**
- ✅ Does Polymarket send ping/pong frames that should NOT be parsed as JSON?
- ✅ Should we respond to server-initiated pings automatically (or does `websockets` library handle it)?
- ✅ Are there any other non-JSON messages we should expect?

---

### 12. Multi-Market Subscriptions

**Use Case:** Our bot trades 5-10 markets simultaneously

**Current Implementation:**
```python
await manager.subscribe_assets(['asset_id_1', 'asset_id_2', ...])
```

**Questions:**
- ❓ Can we subscribe to multiple assets in a single message?
- ❓ What is the maximum number of concurrent subscriptions?
- ❓ Should we create separate WebSocket connections for different markets?
- ❓ Is there a "subscribe all" option for market makers?

---

## 🔧 Specific Code Verification Requests

### Request 1: Validate Subscription Format
Please confirm if this subscription message is correct:
```json
{
  "type": "subscribe",
  "channel": "orderbook",
  "asset_id": "21742633143463906290569050155826241533067272736897614950488156847949938836455"
}
```

### Request 2: Validate User Channel Format
Please provide the exact format for subscribing to user fills:
```json
{
  "type": "subscribe",
  "channel": "user",
  // What auth fields are required here?
}
```

### Request 3: Provide Example Messages
Please share example WebSocket messages for:
1. Full orderbook snapshot
2. Orderbook update/delta (if applicable)
3. Fill notification
4. Error message
5. Any other message types

---

## 📊 Current Bot Performance

With current implementation:
- **Latency:** ~50-150ms PING/PONG (varies by region)
- **Update Frequency:** Real-time (sub-second)
- **Stability:** Reconnects successfully after disconnects
- **Issues:** Occasional `JSONDecodeError` on ping/pong frames (now fixed)

---

## 🎯 Implementation Goals

1. **Minimize Latency:** < 50ms for price updates
2. **Maximize Uptime:** 99.9% connection uptime
3. **Accurate Data:** Never trade on stale/incorrect prices
4. **Efficient Subscriptions:** Subscribe to only active markets
5. **Robust Reconnection:** Handle all disconnect scenarios

---

## 📧 Contact Information

**Developer:** riteshkugarg  
**Repository:** [polymarket-arb-bot](https://github.com/riteshkugarg/polymarket-arb-bot)  
**Current Branch:** main  
**Deployment:** AWS EC2 (eu-west-1)

---

## ✅ Summary of Questions

| # | Category | Priority | Status |
|---|----------|----------|--------|
| 1 | Subscription format | 🔴 Critical | Needs verification |
| 2 | User channel auth | 🔴 Critical | Needs clarification |
| 3 | Message types | 🟡 High | Needs enumeration |
| 4 | Heartbeat | 🟢 Medium | Verify interval |
| 5 | Reconnection | 🟡 High | Needs guidance |
| 6 | Order book format | 🔴 Critical | Needs verification |
| 7 | Incremental updates | 🟡 High | Needs clarification |
| 8 | Latency | 🟢 Medium | Performance benchmark |
| 9 | Error handling | 🟡 High | Needs error codes |
| 10 | Rate limits | 🟢 Medium | Needs documentation |
| 11 | Ping/pong | 🔴 Critical | **FIXED** (confirmed) |
| 12 | Multi-market | 🟡 High | Needs best practices |

---

## 🚀 Next Steps

Once we receive clarification from Polymarket support:
1. Update [market_data_manager.py](src/core/market_data_manager.py) with correct formats
2. Add comprehensive error handling for all message types
3. Implement incremental orderbook updates (if required)
4. Add authentication for user channel (if required)
5. Update [WEBSOCKET_ARCHITECTURE.md](WEBSOCKET_ARCHITECTURE.md) with official specs
6. Run extended testing (24-hour stability test)
7. Deploy to production with confidence

---

**Thank you for your support! 🙏**
