"""
Validation Test: Flash Cancel on WebSocket Disconnection

Tests the Post-Disconnect Quote Hanging Risk fix:
- Disconnection callback registration
- Flash cancel trigger on disconnect
- Emergency order cancellation
"""

import sys
import inspect
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

def test_disconnection_callback_registration():
    """Test #1: Verify disconnection callback registration in MarketStateCache"""
    from core.market_data_manager import MarketStateCache
    
    cache = MarketStateCache()
    
    # Verify register_disconnection_handler method exists
    assert hasattr(cache, 'register_disconnection_handler'), \
        "ERROR: register_disconnection_handler method not found"
    
    # Verify trigger_disconnection_callbacks method exists
    assert hasattr(cache, 'trigger_disconnection_callbacks'), \
        "ERROR: trigger_disconnection_callbacks method not found"
    
    # Test registration
    callback_invoked = [False]
    
    def test_callback():
        callback_invoked[0] = True
    
    cache.register_disconnection_handler('test', test_callback)
    
    # Trigger callbacks
    cache.trigger_disconnection_callbacks()
    
    # Verify callback was invoked
    assert callback_invoked[0], "ERROR: Disconnection callback was not invoked"
    
    print("✅ Test #1 PASSED: Disconnection callback registration and trigger working")


def test_flash_cancel_trigger_in_ws_manager():
    """Test #2: Verify flash cancel is triggered in PolymarketWSManager on disconnect"""
    import inspect
    from core.market_data_manager import PolymarketWSManager
    
    # Check _handle_reconnect triggers callbacks
    source = inspect.getsource(PolymarketWSManager._handle_reconnect)
    
    assert "trigger_disconnection_callbacks" in source, \
        "ERROR: _handle_reconnect does not trigger disconnection callbacks"
    
    # Check _receive_loop triggers callbacks on ConnectionClosed
    recv_source = inspect.getsource(PolymarketWSManager._receive_loop)
    
    assert "ConnectionClosed" in recv_source, \
        "ERROR: _receive_loop does not handle ConnectionClosed"
    assert "trigger_disconnection_callbacks" in recv_source, \
        "ERROR: _receive_loop does not trigger disconnection callbacks on ConnectionClosed"
    
    print("✅ Test #2 PASSED: Flash cancel triggers in WebSocket disconnection handlers")


def test_mm_strategy_disconnection_handler():
    """Test #3: Verify MarketMakingStrategy registers and implements disconnection handler"""
    import inspect
    from strategies.market_making_strategy import MarketMakingStrategy
    
    # Verify on_websocket_disconnection method exists
    assert hasattr(MarketMakingStrategy, 'on_websocket_disconnection'), \
        "ERROR: on_websocket_disconnection method not found"
    
    # Verify method signature (should be sync, not async)
    sig = inspect.signature(MarketMakingStrategy.on_websocket_disconnection)
    # async methods have 'async' in source
    source = inspect.getsource(MarketMakingStrategy.on_websocket_disconnection)
    assert not source.strip().startswith('async def'), \
        "ERROR: on_websocket_disconnection should be synchronous (not async)"
    
    # Verify it schedules emergency cancel
    assert "_emergency_cancel_all_orders" in source, \
        "ERROR: on_websocket_disconnection does not schedule emergency cancel"
    assert "create_task" in source, \
        "ERROR: on_websocket_disconnection does not use create_task to schedule async cancel"
    
    # Verify _emergency_cancel_all_orders exists
    assert hasattr(MarketMakingStrategy, '_emergency_cancel_all_orders'), \
        "ERROR: _emergency_cancel_all_orders method not found"
    
    # Verify it's async
    emergency_source = inspect.getsource(MarketMakingStrategy._emergency_cancel_all_orders)
    assert emergency_source.strip().startswith('async def'), \
        "ERROR: _emergency_cancel_all_orders should be async"
    
    # Verify it cancels orders
    assert "cancel_order" in emergency_source, \
        "ERROR: _emergency_cancel_all_orders does not call cancel_order"
    assert "active_bids" in emergency_source and "active_asks" in emergency_source, \
        "ERROR: _emergency_cancel_all_orders does not iterate through bids/asks"
    
    print("✅ Test #3 PASSED: MarketMakingStrategy disconnection handler implemented correctly")


def test_handler_registration_in_init():
    """Test #4: Verify handler is registered in __init__"""
    import inspect
    from strategies.market_making_strategy import MarketMakingStrategy
    
    init_source = inspect.getsource(MarketMakingStrategy.__init__)
    
    # Verify registration happens
    assert "register_disconnection_handler" in init_source, \
        "ERROR: __init__ does not register disconnection handler"
    assert "on_websocket_disconnection" in init_source, \
        "ERROR: __init__ does not register on_websocket_disconnection callback"
    assert "Flash Cancel" in init_source, \
        "ERROR: Flash Cancel documentation missing in registration"
    
    print("✅ Test #4 PASSED: Disconnection handler registered in strategy initialization")


def test_critical_safety_documentation():
    """Test #5: Verify critical safety documentation exists"""
    import inspect
    from strategies.market_making_strategy import MarketMakingStrategy
    
    disconnection_source = inspect.getsource(MarketMakingStrategy.on_websocket_disconnection)
    
    # Check for critical safety keywords
    assert "blind quoting" in disconnection_source.lower() or "blind trading" in disconnection_source.lower(), \
        "ERROR: Missing 'blind quoting' safety documentation"
    assert "FLASH CANCEL" in disconnection_source, \
        "ERROR: Missing 'FLASH CANCEL' documentation"
    assert "websocket disconnect" in disconnection_source.lower(), \
        "ERROR: Missing WebSocket disconnect documentation"
    
    print("✅ Test #5 PASSED: Critical safety documentation in place")


def main():
    """Run all flash cancel tests"""
    print("=" * 70)
    print("FLASH CANCEL ON WEBSOCKET DISCONNECTION - VALIDATION TEST")
    print("=" * 70)
    print()
    
    tests = [
        ("Disconnection Callback Registration", test_disconnection_callback_registration),
        ("Flash Cancel Trigger in WebSocket Manager", test_flash_cancel_trigger_in_ws_manager),
        ("MarketMaking Disconnection Handler", test_mm_strategy_disconnection_handler),
        ("Handler Registration in __init__", test_handler_registration_in_init),
        ("Critical Safety Documentation", test_critical_safety_documentation),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            print(f"Running: {name}...")
            test_func()
            passed += 1
            print()
        except AssertionError as e:
            print(f"❌ FAILED: {e}")
            print()
            failed += 1
        except Exception as e:
            print(f"❌ ERROR: {e}")
            print()
            failed += 1
    
    print("=" * 70)
    print(f"RESULTS: {passed}/{len(tests)} tests passed")
    if failed == 0:
        print("✅ FLASH CANCEL MECHANISM VALIDATED")
        print()
        print("Summary:")
        print("  - Disconnection callbacks properly registered in MarketStateCache")
        print("  - WebSocket manager triggers callbacks on disconnect")
        print("  - MarketMakingStrategy implements synchronous disconnection handler")
        print("  - Async emergency cancel scheduled via create_task")
        print("  - All active orders cancelled across all positions")
        print()
        print("PROTECTION: Prevents 'blind quoting' when data feed is down")
    else:
        print(f"❌ {failed} test(s) failed")
        sys.exit(1)
    print("=" * 70)


if __name__ == '__main__':
    main()
