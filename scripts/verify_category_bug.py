#!/usr/bin/env python3
"""
Verify category parameter bug - does it actually filter or just ignore the parameter?
"""

import requests

BASE_URL = "https://gamma-api.polymarket.com"

print("Testing if 'category' parameter actually filters...")
print("="*80)

# Fetch markets with different category values
responses = {}
for category in ['crypto', 'politics', 'sports', 'nonexistent-category-xyz']:
    response = requests.get(
        f"{BASE_URL}/markets",
        params={'category': category, 'closed': 'false', 'limit': 3}
    )
    response.raise_for_status()
    markets = response.json()
    responses[category] = [m['id'] for m in markets]
    print(f"\n{category}: {len(markets)} markets")
    for m in markets:
        print(f"  - {m['id']}: {m.get('question', '')[:50]}...")

print("\n" + "="*80)
print("🔍 ANALYSIS: Are results identical?")
print("="*80)

# Check if all responses are identical
all_ids = list(responses.values())
if all(ids == all_ids[0] for ids in all_ids):
    print("❌ BUG CONFIRMED: 'category' parameter is IGNORED!")
    print("   All queries return IDENTICAL market IDs regardless of category value")
    print("\n✅ VERDICT: Use tag_id parameter (WORKS), not category parameter (BROKEN)")
else:
    print("✅ category parameter actually filters markets")
