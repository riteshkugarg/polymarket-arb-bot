#!/usr/bin/env python3
"""
Category ID Discovery Script

POLYMARKET FEEDBACK (Q18 - Jan 2026):
"Use category.id instead of slug for stability. IDs are typically more stable
identifiers than human-readable slugs that might get updated for SEO or clarity reasons."

This script fetches active markets from Polymarket Gamma API and extracts all unique
category IDs with their corresponding slugs and labels.

Usage:
    python scripts/discover_category_ids.py

Output:
    - Prints category mapping to console
    - Generates category ID list for constants.py
"""

import requests
import json
from typing import Dict, Set, List, Any
from collections import defaultdict


def discover_category_ids(api_url: str = "https://gamma-api.polymarket.com") -> Dict[str, Dict[str, Any]]:
    """
    Discover all category IDs from Polymarket API
    
    Returns:
        Dictionary mapping category_id -> {slug, label, count}
    """
    print(f"🔍 Fetching markets from {api_url}/markets...")
    
    try:
        # Fetch sample of markets (default limit is 100)
        response = requests.get(f"{api_url}/markets", timeout=30)
        response.raise_for_status()
        markets = response.json()
        
        if not isinstance(markets, list):
            raise ValueError(f"Expected list of markets, got {type(markets)}")
        
        print(f"✅ Fetched {len(markets)} markets")
        
        # DEBUG: Print first market structure to understand API response
        if markets:
            print("\n🔍 DEBUG: First market structure sample:")
            first_market = markets[0]
            print(f"  Keys available: {list(first_market.keys())[:20]}")  # First 20 keys
            if 'categories' in first_market:
                print(f"  Categories field: {first_market['categories']}")
            else:
                print(f"  ⚠️  No 'categories' field found!")
                print(f"  Sample market: {json.dumps(first_market, indent=2)[:500]}...")
        
        # Extract all unique categories
        category_map: Dict[str, Dict[str, Any]] = {}
        market_count_by_category = defaultdict(int)
        
        for market in markets:
            categories = market.get('categories', [])
            if not isinstance(categories, list):
                continue
                
            for cat in categories:
                if not isinstance(cat, dict):
                    continue
                    
                cat_id = cat.get('id')
                cat_slug = cat.get('slug')
                cat_label = cat.get('label')
                
                if cat_id:
                    if cat_id not in category_map:
                        category_map[cat_id] = {
                            'slug': cat_slug or 'unknown',
                            'label': cat_label or 'Unknown',
                            'count': 0
                        }
                    
                    category_map[cat_id]['count'] += 1
        
        print(f"✅ Discovered {len(category_map)} unique categories")
        return category_map
        
    except requests.exceptions.RequestException as e:
        print(f"❌ Error fetching markets: {e}")
        return {}


def print_category_mapping(category_map: Dict[str, Dict[str, Any]]) -> None:
    """Print category mapping in human-readable format"""
    print("\n" + "="*80)
    print("CATEGORY ID MAPPING")
    print("="*80)
    
    # Sort by market count (most popular first)
    sorted_categories = sorted(
        category_map.items(),
        key=lambda x: x[1]['count'],
        reverse=True
    )
    
    print(f"{'Category ID':<30} {'Slug':<20} {'Label':<30} {'Markets':<10}")
    print("-"*80)
    
    for cat_id, info in sorted_categories:
        print(f"{cat_id:<30} {info['slug']:<20} {info['label']:<30} {info['count']:<10}")


def generate_constants_config(category_map: Dict[str, Dict[str, Any]]) -> None:
    """Generate Python list for constants.py"""
    print("\n" + "="*80)
    print("CONFIGURATION FOR constants.py")
    print("="*80)
    print("\nReplace MM_TARGET_CATEGORIES in src/config/constants.py with:\n")
    print("MM_TARGET_CATEGORIES: Final[List[str]] = [")
    
    # Sort by market count (most popular first)
    sorted_categories = sorted(
        category_map.items(),
        key=lambda x: x[1]['count'],
        reverse=True
    )
    
    # Show top 10 most popular categories
    for cat_id, info in sorted_categories[:10]:
        print(f"    '{cat_id}',  # {info['label']} ({info['slug']}) - {info['count']} markets")
    
    print("]")
    print("\n# Note: Above list shows top 10 categories by market count")
    print("# Choose categories based on your trading strategy:")
    print("#   - Politics, Crypto, Sports for high-volume daily trading")
    print("#   - Economics, Business for event-driven strategies")


def suggest_high_priority_categories(category_map: Dict[str, Dict[str, Any]]) -> None:
    """Suggest high-priority categories for institutional trading"""
    print("\n" + "="*80)
    print("SUGGESTED HIGH-PRIORITY CATEGORIES (Institutional Grade)")
    print("="*80)
    print("\nCategories with highest liquidity and daily activity:\n")
    
    # Sort by market count
    sorted_categories = sorted(
        category_map.items(),
        key=lambda x: x[1]['count'],
        reverse=True
    )
    
    # Filter for institutional-grade categories
    priority_keywords = [
        'politic', 'election', 'crypto', 'bitcoin', 'ethereum',
        'sport', 'nfl', 'nba', 'soccer', 'business', 'econom'
    ]
    
    print("Categories matching institutional keywords:")
    print(f"{'Category ID':<30} {'Label':<30} {'Markets':<10}")
    print("-"*80)
    
    for cat_id, info in sorted_categories:
        slug_lower = info['slug'].lower()
        label_lower = info['label'].lower()
        
        if any(keyword in slug_lower or keyword in label_lower for keyword in priority_keywords):
            print(f"{cat_id:<30} {info['label']:<30} {info['count']:<10}")


def export_to_json(category_map: Dict[str, Dict[str, Any]], filename: str = "category_ids.json") -> None:
    """Export category mapping to JSON file"""
    try:
        with open(filename, 'w') as f:
            json.dump(category_map, f, indent=2)
        print(f"\n✅ Exported category mapping to {filename}")
    except IOError as e:
        print(f"❌ Error exporting to JSON: {e}")


def main():
    """Main execution"""
    print("="*80)
    print("POLYMARKET CATEGORY ID DISCOVERY TOOL")
    print("="*80)
    print("\nThis tool discovers stable category.id values for institutional filtering.")
    print("Polymarket Feedback (Q18): Use category.id instead of slug/label for stability.\n")
    
    # Discover categories
    category_map = discover_category_ids()
    
    if not category_map:
        print("❌ No categories discovered. Check API connectivity.")
        return
    
    # Print results
    print_category_mapping(category_map)
    suggest_high_priority_categories(category_map)
    generate_constants_config(category_map)
    
    # Export to JSON
    export_to_json(category_map, "polymarket_category_ids.json")
    
    print("\n" + "="*80)
    print("NEXT STEPS:")
    print("="*80)
    print("1. Review suggested high-priority categories above")
    print("2. Copy relevant category IDs to src/config/constants.py")
    print("3. Replace MM_TARGET_CATEGORIES with the generated list")
    print("4. Deploy to production with stable category filtering")
    print("\nFor tag_id filtering approach, ask Polymarket support for available tag IDs.")
    print("="*80 + "\n")


if __name__ == "__main__":
    main()
