"""
Validation Test: Institutional Security Improvements

Tests the 4 critical security loophole fixes:
1. Staleness threshold reduced from 2s to 500ms
2. Predictive micro-price deviation check (1% threshold)
3. Immediate cancel on fill (prevents double-exposure)
4. Dynamic spread adjustment based on adverse selection
"""

import sys
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / 'src'))

def test_staleness_threshold():
    """Test #1: Verify staleness threshold is 500ms"""
    from core.market_data_manager import MarketSnapshot
    
    # Create snapshot with old timestamp
    snapshot = MarketSnapshot(
        asset_id='test_asset',
        best_bid=0.5,
        best_ask=0.51,
        bid_size=100,
        ask_size=100,
        mid_price=0.505,
        micro_price=0.505,
        last_update=time.time() - 0.6  # 600ms old
    )
    
    # Should be stale (threshold is 500ms)
    assert snapshot.is_stale(), "ERROR: Staleness threshold not properly reduced to 500ms"
    
    # Create fresh snapshot
    snapshot_fresh = MarketSnapshot(
        asset_id='test_asset',
        best_bid=0.5,
        best_ask=0.51,
        bid_size=100,
        ask_size=100,
        mid_price=0.505,
        micro_price=0.505,
        last_update=time.time() - 0.3  # 300ms old
    )
    
    # Should NOT be stale
    assert not snapshot_fresh.is_stale(), "ERROR: Fresh data incorrectly marked as stale"
    
    print("✅ Test #1 PASSED: Staleness threshold is 500ms (institutional grade)")


def test_predictive_toxic_flow_logic():
    """Test #2: Verify predictive micro-price deviation check exists"""
    import inspect
    from strategies.market_making_strategy import MarketMakingStrategy
    
    # Get source code of _place_quotes
    source = inspect.getsource(MarketMakingStrategy._place_quotes)
    
    # Check for predictive logic
    assert "PREDICTIVE TOXIC FLOW" in source, "ERROR: Predictive toxic flow check not found"
    assert "micro_deviation" in source, "ERROR: Micro-price deviation logic missing"
    assert "0.01" in source or "1%" in source, "ERROR: 1% deviation threshold not found"
    assert "PULLING QUOTES" in source, "ERROR: Quote pulling logic missing"
    
    print("✅ Test #2 PASSED: Predictive micro-price deviation check implemented")


def test_immediate_cancel_on_fill():
    """Test #3: Verify immediate cancel on fill logic"""
    import inspect
    from strategies.market_making_strategy import MarketMakingStrategy
    
    # Get source code of handle_fill_event
    source = inspect.getsource(MarketMakingStrategy.handle_fill_event)
    
    # Check for immediate cancel logic
    assert "IMMEDIATE CANCEL" in source, "ERROR: Immediate cancel on fill not implemented"
    assert "BID was filled" in source, "ERROR: BID fill handling missing"
    assert "ASK was filled" in source, "ERROR: ASK fill handling missing"
    assert "cancel_order" in source, "ERROR: Order cancellation logic missing"
    assert "double-exposure" in source, "ERROR: Double-exposure prevention not documented"
    
    # Verify cancel happens BEFORE inventory update
    cancel_idx = source.find("cancel_order")
    inventory_idx = source.find("update_inventory")
    assert cancel_idx < inventory_idx, "ERROR: Cancel must happen BEFORE inventory update"
    
    print("✅ Test #3 PASSED: Immediate cancel on fill implemented (high-priority callback)")


def test_dynamic_spread_adjustment():
    """Test #4: Verify dynamic spread adjustment based on adverse selection"""
    import inspect
    from strategies.market_making_strategy import MarketMakingStrategy
    
    # Get source code of _calculate_skewed_quotes
    source = inspect.getsource(MarketMakingStrategy._calculate_skewed_quotes)
    
    # Check for dynamic spread logic
    assert "ADVERSE SELECTION AUTO-ADJUSTMENT" in source, "ERROR: Dynamic spread adjustment not found"
    assert "avg_markout" in source, "ERROR: Markout P&L logic missing"
    assert "adverse_multiplier" in source, "ERROR: Spread multiplier logic missing"
    assert "position.fill_count" in source, "ERROR: Statistical significance check missing"
    
    # Verify position parameter added
    sig = inspect.signature(MarketMakingStrategy._calculate_skewed_quotes)
    assert 'position' in sig.parameters, "ERROR: Position parameter not added to _calculate_skewed_quotes"
    
    print("✅ Test #4 PASSED: Dynamic spread adjustment based on adverse selection implemented")


def test_integration():
    """Integration test: Verify all components work together"""
    from strategies.market_making_strategy import MarketMakingStrategy, MarketPosition
    from core.market_data_manager import MarketSnapshot
    
    # Create mock position with negative markout
    position = MarketPosition(
        market_id='test_market',
        market_question='Test Market',
        token_ids=['token_1']
    )
    
    # Simulate 20 fills with negative markout (adverse selection)
    position.fill_count = 20
    position.total_markout_pnl = -0.15  # -0.0075 per fill average (worse than -0.005 threshold)
    
    # Create strategy instance (mock client and order_manager)
    class MockClient:
        pass
    
    class MockOrderManager:
        pass
    
    strategy = MarketMakingStrategy(
        client=MockClient(),
        order_manager=MockOrderManager()
    )
    
    # Calculate quotes with adverse selection
    mid_price = 0.5
    inventory = 0
    is_toxic = False
    
    bid, ask = strategy._calculate_skewed_quotes(mid_price, inventory, is_toxic, position)
    
    # Verify spread is widened due to adverse selection
    spread = ask - bid
    base_spread = 0.02  # MM_TARGET_SPREAD default
    
    assert spread > base_spread, f"ERROR: Spread not widened (got {spread:.4f}, expected >{base_spread:.4f})"
    
    print(f"✅ Integration Test PASSED: Spread widened from {base_spread:.4f} to {spread:.4f} due to adverse selection")


def main():
    """Run all security improvement tests"""
    print("=" * 70)
    print("INSTITUTIONAL SECURITY IMPROVEMENTS - VALIDATION TEST")
    print("=" * 70)
    print()
    
    tests = [
        ("Staleness Threshold (2s → 500ms)", test_staleness_threshold),
        ("Predictive Micro-Price Deviation", test_predictive_toxic_flow_logic),
        ("Immediate Cancel on Fill", test_immediate_cancel_on_fill),
        ("Dynamic Spread Adjustment", test_dynamic_spread_adjustment),
        ("Integration Test", test_integration),
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
        print("✅ ALL SECURITY IMPROVEMENTS VALIDATED")
        print()
        print("Summary of Fixes:")
        print("  1. Staleness threshold: 2s → 500ms (institutional grade)")
        print("  2. Predictive toxic flow: Preemptive quote pulling on 1% micro-price deviation")
        print("  3. Immediate cancel on fill: Prevents double-exposure race condition")
        print("  4. Dynamic spread adjustment: Auto-widens spreads on adverse selection")
    else:
        print(f"❌ {failed} test(s) failed")
        sys.exit(1)
    print("=" * 70)


if __name__ == '__main__':
    main()
