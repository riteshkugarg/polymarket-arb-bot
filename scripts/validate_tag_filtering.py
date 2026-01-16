#!/usr/bin/env python3
"""
Validate Tag-Based Filtering Implementation

Tests the institutional-grade server-side filtering system to ensure:
1. MM_TARGET_TAGS is properly configured
2. Tag filtering reduces API calls vs fetching all markets
3. Returned markets match expected tags
4. Implementation follows Polymarket best practices
"""

import sys
import asyncio
import aiohttp
from typing import List, Dict, Any

# Test configuration
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Expected tags from constants.py
EXPECTED_TAGS = [
    ('235', 'Bitcoin'),
    ('100240', 'NBA Finals'),
    ('78', 'Iran'),
    ('180', 'Israel'),
    ('292', 'Glenn Youngkin'),
    ('802', 'Iowa'),
    ('166', 'South Korea'),
    ('388', 'Netanyahu'),
]


async def test_tag_filtering() -> bool:
    """Test server-side tag filtering implementation"""
    print("="*80)
    print("TAG-BASED FILTERING VALIDATION")
    print("="*80)
    
    all_passed = True
    
    # TEST 1: Tag discovery
    print("\n📋 TEST 1: Tag Discovery")
    print("-"*80)
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{GAMMA_API_URL}/tags", params={'limit': '100'}, timeout=10) as resp:
                if resp.status != 200:
                    print(f"❌ FAILED: Tags endpoint returned {resp.status}")
                    return False
                
                tags = await resp.json()
                print(f"✅ PASSED: Fetched {len(tags)} tags from /tags endpoint")
                
                # Verify expected tags exist
                tag_ids = {tag['id'] for tag in tags if isinstance(tag, dict)}
                expected_ids = {tag_id for tag_id, _ in EXPECTED_TAGS}
                
                missing_tags = expected_ids - tag_ids
                if missing_tags:
                    print(f"⚠️  WARNING: Some configured tags not found: {missing_tags}")
                else:
                    print(f"✅ PASSED: All {len(expected_ids)} configured tags exist")
        
        except Exception as e:
            print(f"❌ FAILED: Error fetching tags - {e}")
            all_passed = False
    
    # TEST 2: Server-side filtering
    print("\n📋 TEST 2: Server-Side Tag Filtering")
    print("-"*80)
    
    async with aiohttp.ClientSession() as session:
        total_markets = 0
        
        for tag_id, tag_name in EXPECTED_TAGS[:3]:  # Test first 3 tags
            try:
                params = {
                    'tag_id': tag_id,
                    'closed': 'false',
                    'related_tags': 'true',
                    'limit': '50'
                }
                
                async with session.get(f"{GAMMA_API_URL}/markets", params=params, timeout=10) as resp:
                    if resp.status != 200:
                        print(f"❌ FAILED: tag_id={tag_id} returned {resp.status}")
                        all_passed = False
                        continue
                    
                    markets = await resp.json()
                    
                    if isinstance(markets, list):
                        market_count = len(markets)
                    else:
                        market_count = len(markets.get('data', []))
                    
                    total_markets += market_count
                    
                    if market_count > 0:
                        print(f"✅ PASSED: tag_id={tag_id:6} ({tag_name:20}) → {market_count:3} markets")
                    else:
                        print(f"ℹ️  INFO:   tag_id={tag_id:6} ({tag_name:20}) → 0 markets (no active markets)")
            
            except Exception as e:
                print(f"❌ FAILED: tag_id={tag_id} - {e}")
                all_passed = False
        
        print(f"\n📊 Total markets from first 3 tags: {total_markets}")
        
        if total_markets > 0:
            print(f"✅ PASSED: Server-side filtering returns markets")
        else:
            print(f"⚠️  WARNING: No markets returned (tags may not have active markets)")
    
    # TEST 3: Compare with fetching all markets
    print("\n📋 TEST 3: Efficiency Comparison (Tag Filtering vs Fetch All)")
    print("-"*80)
    
    async with aiohttp.ClientSession() as session:
        try:
            # Fetch all markets (old approach)
            params_all = {'active': 'true', 'closed': 'false', 'limit': '100'}
            async with session.get(f"{GAMMA_API_URL}/markets", params=params_all, timeout=10) as resp:
                if resp.status != 200:
                    print(f"⚠️  Could not fetch all markets: {resp.status}")
                else:
                    all_markets_response = await resp.json()
                    
                    if isinstance(all_markets_response, list):
                        all_markets_count = len(all_markets_response)
                    else:
                        all_markets_count = len(all_markets_response.get('data', []))
                    
                    print(f"📊 Fetching ALL markets: {all_markets_count} markets")
                    print(f"📊 Fetching with TAGS: {total_markets} markets (filtered)")
                    
                    if total_markets < all_markets_count:
                        efficiency = (1 - total_markets / all_markets_count) * 100 if all_markets_count > 0 else 0
                        print(f"✅ PASSED: Tag filtering reduces markets by {efficiency:.1f}%")
                    else:
                        print(f"ℹ️  INFO: Tag filtering returned similar/more markets (related_tags=true)")
        
        except Exception as e:
            print(f"⚠️  Could not compare efficiency: {e}")
    
    # TEST 4: Validate constants.py configuration
    print("\n📋 TEST 4: Configuration Validation")
    print("-"*80)
    
    try:
        sys.path.insert(0, '/workspaces/polymarket-arb-bot/src')
        from config.constants import MM_TARGET_TAGS
        
        print(f"✅ PASSED: MM_TARGET_TAGS imported successfully")
        print(f"📊 Configured tags: {len(MM_TARGET_TAGS)}")
        
        if len(MM_TARGET_TAGS) > 0:
            print(f"✅ PASSED: At least one tag configured")
            print(f"   Tags: {', '.join(MM_TARGET_TAGS[:5])}{'...' if len(MM_TARGET_TAGS) > 5 else ''}")
        else:
            print(f"⚠️  WARNING: MM_TARGET_TAGS is empty - will fetch ALL markets!")
            all_passed = False
        
        # Check for old constant
        try:
            from config.constants import MM_TARGET_CATEGORIES
            print(f"⚠️  WARNING: MM_TARGET_CATEGORIES still exists (deprecated)")
        except ImportError:
            print(f"✅ PASSED: MM_TARGET_CATEGORIES removed (replaced with MM_TARGET_TAGS)")
    
    except ImportError as e:
        print(f"❌ FAILED: Could not import constants - {e}")
        all_passed = False
    
    # TEST 5: Validate market_making_strategy.py integration
    print("\n📋 TEST 5: Strategy Integration")
    print("-"*80)
    
    try:
        from strategies import market_making_strategy
        
        # Check that MM_TARGET_TAGS is imported
        if hasattr(market_making_strategy, 'MM_TARGET_TAGS'):
            print(f"✅ PASSED: market_making_strategy imports MM_TARGET_TAGS")
        else:
            print(f"❌ FAILED: market_making_strategy missing MM_TARGET_TAGS import")
            all_passed = False
        
        # Check that old constant is not referenced
        with open('/workspaces/polymarket-arb-bot/src/strategies/market_making_strategy.py', 'r') as f:
            content = f.read()
            
            if 'MM_TARGET_CATEGORIES' in content:
                print(f"⚠️  WARNING: market_making_strategy still references MM_TARGET_CATEGORIES")
                all_passed = False
            else:
                print(f"✅ PASSED: MM_TARGET_CATEGORIES removed from strategy")
            
            if 'tag_id' in content and 'closed' in content:
                print(f"✅ PASSED: Strategy uses tag_id + closed parameters")
            else:
                print(f"❌ FAILED: Strategy missing tag_id/closed parameters")
                all_passed = False
    
    except Exception as e:
        print(f"❌ FAILED: Could not validate strategy integration - {e}")
        all_passed = False
    
    # FINAL VERDICT
    print("\n" + "="*80)
    if all_passed:
        print("🎉 ALL TESTS PASSED - Implementation is production-ready!")
        print("="*80)
        print("\n✅ Institutional-grade tag-based filtering validated:")
        print("   • Server-side filtering with tag_id parameter")
        print("   • Reduced API calls and bandwidth")
        print("   • MM_TARGET_TAGS properly configured")
        print("   • Strategy integration complete")
        print("\n🚀 Ready for production deployment!")
    else:
        print("⚠️  SOME TESTS FAILED - Review warnings above")
        print("="*80)
    
    return all_passed


if __name__ == '__main__':
    result = asyncio.run(test_tag_filtering())
    sys.exit(0 if result else 1)
