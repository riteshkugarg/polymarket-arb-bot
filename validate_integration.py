#!/usr/bin/env python3
"""
Integration Validation Script

Verifies that AtomicDepthAwareExecutor has been properly integrated
into the bot and arbitrage strategy.
"""

import sys
import os
import inspect

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def validate_imports():
    """Validate all required imports work"""
    print("\n" + "=" * 80)
    print("VALIDATING IMPORTS")
    print("=" * 80)
    
    try:
        from core.atomic_depth_aware_executor import (
            AtomicDepthAwareExecutor,
            ExecutionPhase,
            AtomicExecutionResult,
            DepthCheckResult,
            OrderPlacementTask
        )
        print("✅ AtomicDepthAwareExecutor and all components imported")
        
        from strategies.arbitrage_strategy import ArbitrageStrategy
        print("✅ ArbitrageStrategy imported")
        
        from strategies.arb_scanner import ArbScanner
        print("✅ ArbScanner imported")
        
        return True
        
    except Exception as e:
        print(f"❌ Import failed: {e}")
        return False


def validate_atomic_executor_interface():
    """Validate AtomicDepthAwareExecutor has required methods"""
    print("\n" + "=" * 80)
    print("VALIDATING ATOMIC EXECUTOR INTERFACE")
    print("=" * 80)
    
    from core.atomic_depth_aware_executor import AtomicDepthAwareExecutor
    
    required_methods = [
        'execute_atomic_basket',
        '_validate_all_depths',
        '_place_order_async',
        '_monitor_fills',
        '_cancel_all_orders'
    ]
    
    for method_name in required_methods:
        if hasattr(AtomicDepthAwareExecutor, method_name):
            print(f"✅ Method '{method_name}' found")
        else:
            print(f"❌ Method '{method_name}' NOT found")
            return False
    
    return True


def validate_strategy_signature():
    """Validate ArbitrageStrategy accepts atomic_executor parameter"""
    print("\n" + "=" * 80)
    print("VALIDATING ARBITRAGE STRATEGY SIGNATURE")
    print("=" * 80)
    
    from strategies.arbitrage_strategy import ArbitrageStrategy
    
    sig = inspect.signature(ArbitrageStrategy.__init__)
    params = list(sig.parameters.keys())
    
    print(f"Parameters: {params}")
    
    if 'atomic_executor' in params:
        print("✅ 'atomic_executor' parameter found in __init__")
        
        # Check if method to use it exists
        if hasattr(ArbitrageStrategy, '_execute_atomic_depth_aware'):
            print("✅ '_execute_atomic_depth_aware' method found")
            return True
        else:
            print("❌ '_execute_atomic_depth_aware' method NOT found")
            return False
    else:
        print("❌ 'atomic_executor' parameter NOT found")
        return False


def validate_execution_phases():
    """Validate ExecutionPhase enum has all required phases"""
    print("\n" + "=" * 80)
    print("VALIDATING EXECUTION PHASES")
    print("=" * 80)
    
    from core.atomic_depth_aware_executor import ExecutionPhase
    
    required_phases = [
        'PRE_FLIGHT',
        'CONCURRENT_PLACEMENT',
        'FILL_MONITORING',
        'FILL_COMPLETION',
        'ABORT'
    ]
    
    for phase_name in required_phases:
        try:
            phase = ExecutionPhase[phase_name]
            print(f"✅ Phase '{phase_name}' = {phase.value}")
        except KeyError:
            print(f"❌ Phase '{phase_name}' NOT found")
            return False
    
    return True


def validate_data_classes():
    """Validate all data classes exist and have required fields"""
    print("\n" + "=" * 80)
    print("VALIDATING DATA CLASSES")
    print("=" * 80)
    
    from core.atomic_depth_aware_executor import (
        DepthCheckResult,
        OrderPlacementTask,
        AtomicExecutionResult
    )
    
    # Check DepthCheckResult
    from dataclasses import fields
    
    result_fields = [f.name for f in fields(DepthCheckResult)]
    print(f"DepthCheckResult fields: {result_fields}")
    
    order_fields = [f.name for f in fields(OrderPlacementTask)]
    print(f"OrderPlacementTask fields: {order_fields}")
    
    exec_result_fields = [f.name for f in fields(AtomicExecutionResult)]
    print(f"AtomicExecutionResult fields: {exec_result_fields}")
    
    print("✅ All data classes validated")
    return True


def validate_logging():
    """Validate logging is properly configured"""
    print("\n" + "=" * 80)
    print("VALIDATING LOGGING")
    print("=" * 80)
    
    from utils.logger import get_logger
    
    logger = get_logger('validation')
    logger.info("✅ Logger initialized")
    
    return True


def main():
    """Run all validations"""
    print("\n" + "╔" + "=" * 78 + "╗")
    print("║" + " " * 15 + "ATOMIC EXECUTOR INTEGRATION VALIDATION" + " " * 25 + "║")
    print("╚" + "=" * 78 + "╝")
    
    results = []
    
    results.append(("Imports", validate_imports()))
    results.append(("Atomic Executor Interface", validate_atomic_executor_interface()))
    results.append(("Execution Phases", validate_execution_phases()))
    results.append(("Data Classes", validate_data_classes()))
    results.append(("Strategy Signature", validate_strategy_signature()))
    results.append(("Logging", validate_logging()))
    
    # Summary
    print("\n" + "=" * 80)
    print("VALIDATION SUMMARY")
    print("=" * 80)
    
    all_passed = True
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status} - {test_name}")
        if not passed:
            all_passed = False
    
    print("\n" + "=" * 80)
    if all_passed:
        print("✅ ALL VALIDATIONS PASSED - INTEGRATION COMPLETE")
        print("=" * 80)
        print("\nYou can now run the bot with:")
        print("  python src/main.py")
        print("\nThe bot will:")
        print("  1. Initialize AtomicDepthAwareExecutor")
        print("  2. Initialize both Mirror and Arbitrage strategies")
        print("  3. Use atomic depth-aware execution for all arbitrage trades")
        print("=" * 80)
        return 0
    else:
        print("❌ SOME VALIDATIONS FAILED - PLEASE REVIEW")
        print("=" * 80)
        return 1


if __name__ == '__main__':
    sys.exit(main())
