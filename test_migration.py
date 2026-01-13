#!/usr/bin/env python3
"""
Quick syntax and import test for migrated code
"""

import sys
import traceback

def test_imports():
    """Test that all imports work"""
    try:
        print("Testing constants import...")
        from src.config.constants import (
            POLYMARKET_DATA_API_URL,
            POLYMARKET_GAMMA_API_URL,
            PROXY_WALLET_ADDRESS,
        )
        print(f"✅ Constants imported successfully")
        print(f"   Data API: {POLYMARKET_DATA_API_URL}")
        print(f"   Gamma API: {POLYMARKET_GAMMA_API_URL}")
        print(f"   Proxy Wallet: {PROXY_WALLET_ADDRESS}")
        
        print("\nTesting polymarket_client import...")
        from src.core.polymarket_client import PolymarketClient
        print(f"✅ PolymarketClient imported successfully")
        
        print("\nTesting mirror_strategy import...")
        from src.strategies.mirror_strategy import MirrorStrategy
        print(f"✅ MirrorStrategy imported successfully")
        
        return True
        
    except Exception as e:
        print(f"❌ Import failed: {e}")
        traceback.print_exc()
        return False

if __name__ == "__main__":
    print("=" * 60)
    print("POLYMARKET API MIGRATION - SYNTAX VERIFICATION")
    print("=" * 60)
    print()
    
    success = test_imports()
    
    print()
    print("=" * 60)
    if success:
        print("✅ ALL CHECKS PASSED - Code is ready to test")
    else:
        print("❌ CHECKS FAILED - Fix errors before testing")
    print("=" * 60)
    
    sys.exit(0 if success else 1)
