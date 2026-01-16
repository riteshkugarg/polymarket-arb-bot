#!/usr/bin/env python3
"""
Test Polymarket Tag Filtering - Resolve Q22 vs Q27/Q29 Contradictions

POLYMARKET RESPONSES:
Q22: "Use category field for client-side filtering. tag_id unreliable."
Q27/Q29: "Use tag_id parameter with numeric IDs for server-side filtering."

This script tests actual API behavior to determine which approach works.
"""

import requests
import json

BASE_URL = "https://gamma-api.polymarket.com"

print("="*80)
print("POLYMARKET TAG FILTERING TEST")
print("="*80)

# TEST 1: Check if markets actually have 'tags' field
print("\n📋 TEST 1: Do markets have 'tags' field?")
print("-"*80)
response = requests.get(f"{BASE_URL}/markets", params={'limit': 10, 'closed': 'false'})
response.raise_for_status()
markets = response.json()

has_tags_field = False
has_categories_array = False
sample_market = markets[0] if markets else {}

print(f"✅ Fetched {len(markets)} markets")
print(f"\n📊 Market field structure:")
print(f"  All fields: {list(sample_market.keys())}")

if 'tags' in sample_market:
    has_tags_field = True
    print(f"\n  ✅ 'tags' field EXISTS: {sample_market['tags']}")
    print(f"     Type: {type(sample_market['tags'])}")
    if isinstance(sample_market['tags'], list) and sample_market['tags']:
        print(f"     First tag: {json.dumps(sample_market['tags'][0], indent=6)}")
else:
    print(f"\n  ❌ 'tags' field NOT FOUND")

if 'categories' in sample_market:
    has_categories_array = True
    print(f"\n  ✅ 'categories' array EXISTS: {sample_market['categories']}")
else:
    print(f"  ❌ 'categories' array NOT FOUND")

if 'category' in sample_market:
    print(f"\n  ✅ 'category' singular string: '{sample_market['category']}'")

# TEST 2: Test tag_id filtering with known high-volume tags
print("\n\n📋 TEST 2: Does tag_id parameter work for filtering?")
print("-"*80)

# Test with Bitcoin tag (ID: 235) - should have markets
test_tags = [
    ('235', 'Bitcoin'),
    ('100240', 'NBA Finals'),
    ('78', 'Iran'),
    ('180', 'Israel'),
    ('1060', 'iowa caucus'),
]

tag_filtering_works = False

for tag_id, tag_name in test_tags:
    print(f"\n🧪 Testing tag_id={tag_id} ({tag_name})...")
    
    try:
        response = requests.get(
            f"{BASE_URL}/markets",
            params={
                'tag_id': tag_id,
                'closed': 'false',
                'limit': 5
            }
        )
        response.raise_for_status()
        filtered_markets = response.json()
        
        count = len(filtered_markets)
        print(f"   ✅ Returned {count} markets")
        
        if count > 0:
            tag_filtering_works = True
            print(f"   📌 Sample: {filtered_markets[0].get('question', '')[:60]}...")
            
            # Check if returned markets have this tag
            if 'tags' in filtered_markets[0]:
                market_tags = filtered_markets[0]['tags']
                print(f"   🏷️  Market tags: {market_tags}")
            
    except Exception as e:
        print(f"   ❌ Error: {e}")

# TEST 3: Test category parameter (not documented but might work)
print("\n\n📋 TEST 3: Does 'category' parameter work for filtering?")
print("-"*80)

test_categories = ['US-current-affairs', 'crypto', 'politics', 'sports']
category_filtering_works = False

for category in test_categories:
    print(f"\n🧪 Testing category={category}...")
    
    try:
        response = requests.get(
            f"{BASE_URL}/markets",
            params={
                'category': category,
                'closed': 'false',
                'limit': 5
            }
        )
        response.raise_for_status()
        filtered_markets = response.json()
        
        count = len(filtered_markets)
        print(f"   ✅ Returned {count} markets")
        
        if count > 0:
            category_filtering_works = True
            print(f"   📌 Sample: {filtered_markets[0].get('question', '')[:60]}...")
            print(f"   📁 Category: {filtered_markets[0].get('category')}")
            
    except Exception as e:
        print(f"   ❌ Error: {e}")

# FINAL VERDICT
print("\n\n" + "="*80)
print("🏆 VERDICT: Which Approach Works?")
print("="*80)

print(f"\n1️⃣  Markets have 'tags' field: {'✅ YES' if has_tags_field else '❌ NO'}")
print(f"2️⃣  Markets have 'categories' array: {'✅ YES' if has_categories_array else '❌ NO'}")
print(f"3️⃣  tag_id parameter filtering works: {'✅ YES' if tag_filtering_works else '❌ NO'}")
print(f"4️⃣  category parameter filtering works: {'✅ YES' if category_filtering_works else '❌ NO'}")

print("\n📝 RECOMMENDATION:")
if tag_filtering_works and has_tags_field:
    print("   ✅ Use Q27/Q29 approach: Server-side filtering with tag_id parameter")
    print("   ✅ Markets have 'tags' field for validation")
    print("   📖 Implementation: GET /markets?tag_id=235&closed=false")
elif category_filtering_works:
    print("   ✅ Use category parameter for server-side filtering")
    print("   📖 Implementation: GET /markets?category=crypto&closed=false")
elif not tag_filtering_works and not category_filtering_works:
    print("   ⚠️  Use Q22 approach: Client-side filtering with 'category' field")
    print("   ⚠️  Server-side filtering parameters don't work")
    print("   📖 Implementation: Fetch all, filter by m.get('category') in TARGET_CATEGORIES")
else:
    print("   ❓ Mixed results - need further investigation")

print("\n" + "="*80)
