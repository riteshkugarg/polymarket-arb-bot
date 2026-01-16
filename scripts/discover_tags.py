#!/usr/bin/env python3
"""
Tag Discovery Script - Server-Side Filtering (CORRECT APPROACH)

POLYMARKET FEEDBACK (Q22 - Jan 2026):
"Use server-side filtering with tag_id parameter in /markets and /events endpoints.
This reduces API calls and is more efficient than client-side filtering.
Tag Discovery: Use /tags?limit=100 to get all available tags."

This script fetches available tags from Polymarket for server-side filtering.
Tags are the CORRECT system for filtering (not categories).

Usage:
    python scripts/discover_tags.py

Output:
    - Prints tag mapping to console
    - Generates tag list for constants.py
"""

import requests
import json
from typing import Dict, List, Any


def discover_tags(api_url: str = "https://gamma-api.polymarket.com") -> List[Dict[str, Any]]:
    """
    Discover all available tags from Polymarket API
    
    POLYMARKET FEEDBACK (Q22): "Use /tags?limit=100 to get all available tags"
    
    Returns:
        List of tag dictionaries with id, name, description
    """
    print(f"🔍 Fetching tags from {api_url}/tags...")
    
    try:
        # Fetch all tags (Polymarket recommended endpoint)
        response = requests.get(f"{api_url}/tags", params={'limit': 100}, timeout=30)
        response.raise_for_status()
        tags = response.json()
        
        # DEBUG: Print actual response structure
        print(f"\n🔍 DEBUG: Raw API response structure:")
        if isinstance(tags, list) and tags:
            print(f"  Response type: list with {len(tags)} items")
            print(f"  First tag structure: {json.dumps(tags[0], indent=2)}")
        else:
            print(f"  Response type: {type(tags)}")
            print(f"  Full response: {json.dumps(tags, indent=2)[:1000]}...")
        
        if not isinstance(tags, list):
            print(f"⚠️  Expected list of tags, got {type(tags)}")
            return []
        
        print(f"✅ Fetched {len(tags)} tags")
        return tags
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching tags: {e}")
        return []


def print_tag_mapping(tags: List[Dict[str, Any]]) -> None:
    """Print tag mapping in human-readable format"""
    print("\n" + "="*80)
    print("POLYMARKET TAG SYSTEM (Server-Side Filtering)")
    print("="*80)
    print("\nPOLYMARKET FEEDBACK (Q22):")
    print("  'Tags are used for filtering while categories provide hierarchical classification.'")
    print("  'Use server-side filtering with tag_id parameter in /markets endpoint.'")
    print("="*80)
    
    print(f"\n{'Tag ID':<30} {'Name':<30} {'Description':<50}")
    print("-"*110)
    
    for tag in tags:
        tag_id = tag.get('id', 'unknown')
        tag_name = tag.get('name', 'Unknown')
        tag_desc = tag.get('description', '')[:47] + '...' if len(tag.get('description', '')) > 50 else tag.get('description', '')
        print(f"{tag_id:<30} {tag_name:<30} {tag_desc:<50}")


def generate_constants_config(tags: List[Dict[str, Any]]) -> None:
    """Generate Python list for constants.py"""
    print("\n" + "="*80)
    print("CONFIGURATION FOR constants.py")
    print("="*80)
    print("\nReplace MM_TARGET_CATEGORIES with MM_TARGET_TAGS:\n")
    print("# POLYMARKET FEEDBACK (Q22): Use server-side tag filtering")
    print("# 'Use tag_id parameter with /markets endpoint for efficient filtering'")
    print("MM_TARGET_TAGS: Final[List[str]] = [")
    
    # Show all tags (user can comment out unwanted ones)
    for tag in tags[:15]:  # Show first 15 tags
        tag_id = tag.get('id', 'unknown')
        tag_name = tag.get('name', 'Unknown')
        print(f"    '{tag_id}',  # {tag_name}")
    
    if len(tags) > 15:
        print(f"    # ... {len(tags) - 15} more tags available")
    
    print("]")
    print("\n# Usage in market discovery:")
    print("# for tag_id in MM_TARGET_TAGS:")
    print("#     markets = requests.get('/markets', params={'tag_id': tag_id})")
    print("#     # Server returns only markets matching this tag!")


def suggest_high_priority_tags(tags: List[Dict[str, Any]]) -> None:
    """Suggest high-priority tags for institutional trading"""
    print("\n" + "="*80)
    print("SUGGESTED HIGH-PRIORITY TAGS (Institutional Grade)")
    print("="*80)
    
    # Priority keywords for institutional trading
    priority_keywords = [
        'politic', 'election', 'crypto', 'bitcoin', 'ethereum',
        'sport', 'nfl', 'nba', 'soccer', 'business', 'econom'
    ]
    
    print("\nTags matching institutional keywords:")
    print(f"{'Tag ID':<30} {'Name':<30}")
    print("-"*60)
    
    matched = False
    for tag in tags:
        tag_id = tag.get('id', '').lower()
        tag_name = tag.get('name', '').lower()
        tag_desc = tag.get('description', '').lower()
        
        if any(keyword in tag_id or keyword in tag_name or keyword in tag_desc 
               for keyword in priority_keywords):
            print(f"{tag.get('id'):<30} {tag.get('name'):<30}")
            matched = True
    
    if not matched:
        print("  (No matches found - use all tags above)")


def export_to_json(tags: List[Dict[str, Any]], filename: str = "polymarket_tags.json") -> None:
    """Export tags to JSON file"""
    try:
        with open(filename, 'w') as f:
            json.dump(tags, f, indent=2)
        print(f"\n✅ Exported tag mapping to {filename}")
    except IOError as e:
        print(f"❌ Error exporting to JSON: {e}")


def test_tag_filtering(tags: List[Dict[str, Any]], api_url: str = "https://gamma-api.polymarket.com") -> None:
    """Test server-side tag filtering with first tag AND check market tag structure"""
    print("\n" + "="*80)
    print("TESTING SERVER-SIDE TAG FILTERING + MARKET TAG STRUCTURE")
    print("="*80)
    
    # First, fetch some markets WITHOUT tag filtering to see their structure
    print(f"\n🔍 Fetching sample markets to examine tag structure...")
    try:
        response = requests.get(f"{api_url}/markets", params={'limit': 5}, timeout=30)
        response.raise_for_status()
        markets = response.json()
        
        if markets:
            print(f"✅ Fetched {len(markets)} sample markets")
            sample = markets[0]
            print(f"\n📊 Sample market structure:")
            print(f"  Keys: {list(sample.keys())[:20]}")
            
            # Check for tag-related fields
            if 'tags' in sample:
                print(f"  ✅ Found 'tags' field: {sample.get('tags')}")
            if 'tag' in sample:
                print(f"  ✅ Found 'tag' field: {sample.get('tag')}")
            if 'tagId' in sample:
                print(f"  ✅ Found 'tagId' field: {sample.get('tagId')}")
            if 'tag_id' in sample:
                print(f"  ✅ Found 'tag_id' field: {sample.get('tag_id')}")
            
            # Check category field (singular string we discovered earlier)
            if 'category' in sample:
                print(f"  ℹ️  Category field: '{sample.get('category')}'")
            
            print(f"\n💡 INSIGHT: Market tags structure revealed above")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching markets: {e}")
    
    if not tags:
        return
    
    # Now test with first numeric tag
    test_tag = tags[0].get('id') if isinstance(tags[0], dict) else str(tags[0])
    print(f"\n🧪 Testing server-side filtering with tag_id='{test_tag}'...")
    
    try:
        response = requests.get(
            f"{api_url}/markets",
            params={'tag_id': test_tag, 'limit': 5},
            timeout=30
        )
        response.raise_for_status()
        filtered_markets = response.json()
        
        print(f"✅ Server returned {len(filtered_markets)} markets with tag '{test_tag}'")
        
        if filtered_markets:
            print(f"\nSample market from tag filtering:")
            sample = filtered_markets[0]
            print(f"  ID: {sample.get('id')}")
            print(f"  Question: {sample.get('question', 'N/A')[:70]}...")
            print(f"  Category: {sample.get('category', 'N/A')}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Error testing tag filtering: {e}")


def main():
    """Main execution"""
    print("="*80)
    print("POLYMARKET TAG DISCOVERY TOOL")
    print("="*80)
    print("\nPOLYMARKET FEEDBACK (Q22):")
    print("  'Use server-side filtering with tag_id parameter.'")
    print("  'Tags are used for filtering while categories provide classification.'")
    print("  'Use /tags?limit=100 to get all available tags.'\n")
    
    # Discover tags
    tags = discover_tags()
    
    if not tags:
        print("❌ No tags discovered. Check API connectivity.")
        return
    
    # Print results
    print_tag_mapping(tags)
    suggest_high_priority_tags(tags)
    generate_constants_config(tags)
    
    # Export to JSON
    export_to_json(tags, "polymarket_tags.json")
    
    # Test server-side filtering
    test_tag_filtering(tags)
    
    print("\n" + "="*80)
    print("NEXT STEPS:")
    print("="*80)
    print("1. Review suggested high-priority tags above")
    print("2. Update src/config/constants.py:")
    print("   - Rename MM_TARGET_CATEGORIES → MM_TARGET_TAGS")
    print("   - Use tag IDs from above (e.g., ['politics', 'crypto', 'sports'])")
    print("3. Update market discovery to use server-side filtering:")
    print("   - requests.get('/markets', params={'tag_id': tag})")
    print("4. Remove client-side category filtering logic")
    print("5. Deploy and verify reduced API calls!")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
