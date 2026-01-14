#!/usr/bin/env python3
"""
WebSocket Architecture Integration Test

Validates that the WebSocket event-driven architecture is properly integrated.
Tests cache functionality, safety guards, and backward compatibility.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_imports():
    """Test all WebSocket-related imports"""
    print("\n" + "=" * 80)
    print("TESTING WEBSOCKET IMPORTS")
    print("=" * 80)
    
    try:
        from core.market_data_manager import (
            MarketDataManager,
            MarketStateCache,
            PolymarketWSManager,
            MarketSnapshot,
            FillEvent,
            GlobalMarketCache  # Backward compatibility alias
        )
        print("✅ MarketDataManager and all components imported")
        
        # Verify backward compatibility alias
        assert MarketStateCache == GlobalMarketCache, "Backward compatibility alias broken"
        print("✅ Backward compatibility alias (GlobalMarketCache) working")
        
        return True
    except Exception as e:
        print(f"❌ Import failed: {e}")
        return False


def test_strategy_integration():
    """Test that strategies can use MarketDataManager"""
    print("\n" + "=" * 80)
    print("TESTING STRATEGY INTEGRATION")
    print("=" * 80)
    
    try:
        from strategies.arb_scanner import ArbScanner
        from strategies.arbitrage_strategy import ArbitrageStrategy
        from strategies.market_making_strategy import MarketMakingStrategy
        
        print("✅ All strategies imported successfully")
        
        # Check that strategies accept market_data_manager parameter
        import inspect
        
        # Check ArbScanner
        arb_sig = inspect.signature(ArbScanner.__init__)
        assert 'market_data_manager' in arb_sig.parameters, "ArbScanner missing market_data_manager parameter"
        print("✅ ArbScanner accepts market_data_manager parameter")
        
        # Check ArbitrageStrategy
        arb_strat_sig = inspect.signature(ArbitrageStrategy.__init__)
        assert 'market_data_manager' in arb_strat_sig.parameters, "ArbitrageStrategy missing market_data_manager parameter"
        print("✅ ArbitrageStrategy accepts market_data_manager parameter")
        
        # Check MarketMakingStrategy
        mm_sig = inspect.signature(MarketMakingStrategy.__init__)
        assert 'market_data_manager' in mm_sig.parameters, "MarketMakingStrategy missing market_data_manager parameter"
        print("✅ MarketMakingStrategy accepts market_data_manager parameter")
        
        return True
    except Exception as e:
        print(f"❌ Strategy integration test failed: {e}")
        return False


def test_cache_functionality():
    """Test MarketStateCache functionality"""
    print("\n" + "=" * 80)
    print("TESTING CACHE FUNCTIONALITY")
    print("=" * 80)
    
    try:
        from core.market_data_manager import MarketStateCache, MarketSnapshot
        import time
        
        # Create cache
        cache = MarketStateCache(stale_threshold_seconds=2.0)
        print("✅ MarketStateCache created")
        
        # Create test snapshot
        snapshot = MarketSnapshot(
            asset_id="test_asset_123",
            best_bid=0.50,
            best_ask=0.52,
            bid_size=100.0,
            ask_size=100.0,
            mid_price=0.51,
            micro_price=0.51,
            last_update=time.time(),
            bids=[{"price": "0.50", "size": "100"}],
            asks=[{"price": "0.52", "size": "100"}]
        )
        
        # Test update
        result = cache.update("test_asset_123", snapshot)
        assert result == True, "Cache update should succeed"
        print("✅ Cache update successful")
        
        # Test retrieval
        retrieved = cache.get("test_asset_123")
        assert retrieved is not None, "Should retrieve snapshot"
        assert retrieved.asset_id == "test_asset_123", "Retrieved snapshot should match"
        print("✅ Cache retrieval successful")
        
        # Test staleness detection
        is_stale = cache.is_stale("test_asset_123")
        assert is_stale == False, "Fresh data should not be stale"
        print("✅ Staleness detection working (fresh data)")
        
        # Test timestamp integrity (reject older messages)
        old_snapshot = MarketSnapshot(
            asset_id="test_asset_123",
            best_bid=0.49,
            best_ask=0.51,
            bid_size=100.0,
            ask_size=100.0,
            mid_price=0.50,
            micro_price=0.50,
            last_update=time.time() - 10,  # 10 seconds ago
            bids=[{"price": "0.49", "size": "100"}],
            asks=[{"price": "0.51", "size": "100"}]
        )
        
        result = cache.update("test_asset_123", old_snapshot)
        assert result == False, "Should reject older timestamp"
        print("✅ Timestamp integrity check working (rejected stale message)")
        
        # Verify cache wasn't corrupted
        retrieved = cache.get("test_asset_123")
        assert retrieved.best_bid == 0.50, "Cache should retain newer data"
        print("✅ Cache integrity preserved after rejection")
        
        return True
    except Exception as e:
        print(f"❌ Cache functionality test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_main_integration():
    """Test main.py integration"""
    print("\n" + "=" * 80)
    print("TESTING MAIN.PY INTEGRATION")
    print("=" * 80)
    
    try:
        # Check that main.py imports MarketDataManager
        with open('src/main.py', 'r') as f:
            content = f.read()
        
        assert 'from core.market_data_manager import MarketDataManager' in content, \
            "main.py should import MarketDataManager"
        print("✅ main.py imports MarketDataManager")
        
        assert 'self.market_data_manager' in content, \
            "main.py should instantiate MarketDataManager"
        print("✅ main.py instantiates MarketDataManager")
        
        assert '_subscribe_to_active_markets' in content, \
            "main.py should have dynamic subscription method"
        print("✅ main.py has dynamic subscription logic")
        
        assert 'LAG CIRCUIT BREAKER' in content, \
            "main.py should document LAG CIRCUIT BREAKER"
        print("✅ main.py documents LAG CIRCUIT BREAKER")
        
        return True
    except Exception as e:
        print(f"❌ main.py integration test failed: {e}")
        return False


def test_safety_guards():
    """Test institutional safety guard implementation"""
    print("\n" + "=" * 80)
    print("TESTING SAFETY GUARDS")
    print("=" * 80)
    
    try:
        from core.market_data_manager import MarketDataManager
        
        # Check for required safety methods
        assert hasattr(MarketDataManager, 'get_stale_markets'), \
            "MarketDataManager should have get_stale_markets method"
        print("✅ MarketDataManager.get_stale_markets() exists")
        
        assert hasattr(MarketDataManager, 'check_market_staleness'), \
            "MarketDataManager should have check_market_staleness method"
        print("✅ MarketDataManager.check_market_staleness() exists")
        
        assert hasattr(MarketDataManager, 'force_refresh_from_rest'), \
            "MarketDataManager should have force_refresh_from_rest method"
        print("✅ MarketDataManager.force_refresh_from_rest() exists")
        
        # Check market making strategy has LAG CIRCUIT BREAKER
        with open('src/strategies/market_making_strategy.py', 'r') as f:
            mm_content = f.read()
        
        assert 'LAG CIRCUIT BREAKER' in mm_content, \
            "Market making strategy should implement LAG CIRCUIT BREAKER"
        print("✅ Market making strategy has LAG CIRCUIT BREAKER")
        
        assert '_cancel_all_quotes' in mm_content, \
            "Market making strategy should have emergency cancel method"
        print("✅ Market making strategy has _cancel_all_quotes() method")
        
        return True
    except Exception as e:
        print(f"❌ Safety guards test failed: {e}")
        return False


def run_all_tests():
    """Run all integration tests"""
    print("\n" + "=" * 80)
    print("WEBSOCKET ARCHITECTURE INTEGRATION VALIDATION")
    print("=" * 80)
    
    tests = [
        ("Imports", test_imports),
        ("Strategy Integration", test_strategy_integration),
        ("Cache Functionality", test_cache_functionality),
        ("Main Integration", test_main_integration),
        ("Safety Guards", test_safety_guards)
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"\n❌ Test '{name}' crashed: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    # Print summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print("\n" + "=" * 80)
    print(f"RESULT: {passed}/{total} tests passed")
    print("=" * 80)
    
    if passed == total:
        print("\n🎉 All tests passed! WebSocket architecture is properly integrated.")
        return 0
    else:
        print(f"\n⚠️  {total - passed} test(s) failed. Please review errors above.")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
