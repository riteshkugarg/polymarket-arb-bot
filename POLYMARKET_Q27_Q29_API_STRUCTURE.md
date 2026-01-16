# Polymarket API Structure Clarification (Q27-Q29)

**Date**: January 16, 2026  
**Context**: Critical clarification needed on tag/category API structure for server-side filtering implementation  
**Priority**: P0 (BLOCKING production deployment)

---

## Background

Your Q22 response recommended using "server-side filtering with `tag_id` parameter in `/markets` endpoint" instead of client-side category filtering. However, our investigation revealed discrepancies between the recommendation and actual API behavior.

### What We Discovered

**1. `/tags` Endpoint Response:**
```bash
GET https://gamma-api.polymarket.com/tags?limit=100
```
Returns 100 items but ALL have `"name": "Unknown"`:
```json
[
  {"id": "103165", "name": "Unknown", "description": ""},
  {"id": "103166", "name": "Unknown", "description": ""},
  ...
]
```

**2. `/markets` Endpoint Response:**
```bash
GET https://gamma-api.polymarket.com/markets?limit=5
```
Market structure shows:
- ✅ Has `category` field (singular string): `"category": "US-current-affairs"`
- ❌ No `categories` array (plural)
- ❌ No visible `tags`, `tag`, `tagId`, or `tag_id` fields

**3. Server-Side Filtering Test:**
```bash
GET https://gamma-api.polymarket.com/markets?tag_id=103165&limit=5
```
Returns 0 markets (tag filtering appears to not work with numeric IDs)

---

## Q27: Tag System Structure & Category Relationship

**Context**: Your Q22 response stated "Tags are used for filtering while categories provide hierarchical classification" and recommended using `/tags?limit=100` to discover available tags. However, the API returns only numeric IDs with "Unknown" names.

**Questions**:

### A. Tag Naming & Discovery
1. **Why do all tags from `/tags` endpoint show `"name": "Unknown"`?**
   - Are tags supposed to have human-readable names?
   - Is there a different endpoint to get tag names/labels?
   - Are the numeric IDs (103165, 103166, etc.) the actual tag identifiers to use?

### B. Tag vs Category Relationship
2. **How do tags relate to the `category` field in market objects?**
   - Example: Market has `"category": "US-current-affairs"` (string)
   - Is this string the tag name, tag ID, or something different?
   - Should we filter by `tag_id` parameter or `category` parameter?

3. **What is the actual filtering parameter name?**
   - Your Q22 said: `tag_id` parameter (e.g., `/markets?tag_id=crypto`)
   - But our tests with numeric tag IDs return 0 markets
   - Should we use: `/markets?tag_id=crypto` or `/markets?category=US-current-affairs`?

### C. Expected Server-Side Filtering Behavior
4. **What should we pass as `tag_id` value?**
   ```bash
   # Option A: Numeric ID from /tags endpoint?
   GET /markets?tag_id=103165
   
   # Option B: Human-readable string like "crypto"?
   GET /markets?tag_id=crypto
   
   # Option C: Category string like "US-current-affairs"?
   GET /markets?category=US-current-affairs
   
   # Option D: Something else entirely?
   ```

### D. Complete Tag/Category List
5. **Can you provide the complete mapping of:**
   - Tag IDs → Tag Names (e.g., `{id: "103165", name: "Politics"}`)
   - OR Category strings used for filtering (e.g., `["Politics", "Crypto", "Sports"]`)
   - Whichever system is the CORRECT one for server-side filtering

---

## Q28: Market Object Structure - Tags Field Location

**Context**: We examined market objects from `/markets` endpoint but cannot find where tags are stored.

**Questions**:

### A. Tag Field Location
1. **Where are tags stored in market objects returned by `/markets` endpoint?**
   
   Current market structure we see:
   ```json
   {
     "id": "12",
     "question": "Will Joe Biden get Coronavirus before the election?",
     "conditionId": "0xe3b423dfad...",
     "slug": "will-joe-biden-get-coronavirus-before-the-election",
     "endDate": "2020-11-04T00:00:00Z",
     "category": "US-current-affairs",  ← Only this classification field visible
     "liquidity": "0",
     "volume": "...",
     ...
   }
   ```
   
   **Missing fields** we expected based on Q22 guidance:
   - ❌ No `tags` array
   - ❌ No `tag` field
   - ❌ No `tagId` field
   - ❌ No `tag_id` field

2. **Is the `category` field (singular string) the actual tag system?**
   - If yes, should we filter by `/markets?category=Politics` (not `tag_id`)?
   - If no, where are the tags and how do we access them?

### B. API Documentation Alignment
3. **Is there updated API documentation showing:**
   - The actual market object schema with all fields
   - Correct parameter names for filtering
   - Example API calls with responses

---

## Q29: Correct Implementation Approach

**Context**: Based on Q22 guidance, we need to implement server-side filtering but current API behavior doesn't match the description.

**Questions**:

### A. Filtering Strategy Decision
1. **Which approach should institutional clients use for efficient market filtering?**

   **Option 1: Tag-Based Filtering (Q22 Recommendation)**
   ```python
   # Fetch markets by tag
   for tag_id in ['politics', 'crypto', 'sports']:  # Or numeric IDs?
       response = requests.get('/markets', params={'tag_id': tag_id})
       markets = response.json()
   ```
   
   **Option 2: Category-Based Filtering (What we see in API)**
   ```python
   # Fetch markets by category string
   for category in ['Politics', 'Crypto', 'Sports']:
       response = requests.get('/markets', params={'category': category})
       markets = response.json()
   ```
   
   **Option 3: Client-Side Filtering (What we currently do)**
   ```python
   # Fetch all markets, filter locally
   markets = requests.get('/markets', params={'limit': 100}).json()
   filtered = [m for m in markets if m.get('category') in TARGET_CATEGORIES]
   ```

2. **What are the valid parameter names for `/markets` endpoint filtering?**
   - `tag_id`? (Q22 mentioned but doesn't work with numeric IDs)
   - `category`? (Matches the field we see in market objects)
   - `tag`? (Alternative naming)
   - Something else?

### B. Complete Working Example
3. **Can you provide a complete working example of server-side filtering?**
   
   For example, to get only "Politics" markets:
   ```bash
   # What should this exact API call be?
   GET https://gamma-api.polymarket.com/markets?____=____&limit=10
   ```
   
   Please fill in the blanks with the actual working parameter name and value.

### C. Migration Path
4. **For existing client-side filtering using `category` strings, should we:**
   - **Keep using category strings** but switch to server-side parameter: `/markets?category=Politics`
   - **Migrate to tag IDs** and use: `/markets?tag_id=<numeric_id>`
   - **Use a different system** entirely

---

## Current Implementation Status

**What We Built Based on Q22:**
```python
# scripts/discover_tags.py - Tries to fetch from /tags endpoint
tags = requests.get('https://gamma-api.polymarket.com/tags?limit=100').json()
# Returns: [{"id": "103165", "name": "Unknown"}, ...]

# Attempted filtering
markets = requests.get('/markets', params={'tag_id': '103165'}).json()
# Returns: [] (0 markets)
```

**What Actually Works:**
```python
# Client-side category filtering (but you said this is inefficient)
markets = requests.get('/markets').json()
filtered = [m for m in markets if m.get('category') == 'US-current-affairs']
```

**What We Need:**
- Correct API endpoint/parameter for server-side filtering
- Complete list of valid filter values (tag IDs or category strings)
- Working example API call with response

---

## Impact Assessment

**Production Blocker**: We cannot deploy efficient market discovery until we understand:
1. The correct API structure (tags vs categories)
2. The correct filtering parameters
3. The complete list of valid filter values

**Current Situation**:
- ❌ Tag-based filtering (Q22 recommendation) doesn't work with numeric IDs
- ✅ Category strings visible in market objects but no server-side filtering parameter documented
- ⚠️ Currently using inefficient client-side filtering (fetching all markets then filtering)

**What Works vs What Doesn't**:
| Approach | Status | Notes |
|----------|--------|-------|
| `/tags?limit=100` | ⚠️ Partial | Returns IDs but all names are "Unknown" |
| `/markets?tag_id=103165` | ❌ Fails | Returns 0 markets |
| `/markets?tag_id=crypto` | ❓ Unknown | Haven't tested string IDs |
| `/markets?category=Politics` | ❓ Unknown | Not documented but might work? |
| Client-side filtering | ✅ Works | But inefficient (Q22 said avoid this) |

---

## Expected Response Format

For Q27, please provide:
```json
{
  "tag_system_explanation": "...",
  "correct_parameter_name": "tag_id | category | other",
  "tag_id_format": "numeric | string | both",
  "example_tag_ids": ["politics", "crypto", "sports"],
  "example_api_call": "GET /markets?____=____"
}
```

For Q28, please provide:
```json
{
  "tag_field_location": "tags | tag | tagId | category | other",
  "example_market_with_tags": {
    "id": "123",
    "question": "...",
    "tags": ["politics", "elections"],  ← Show actual structure
    "category": "..."
  }
}
```

For Q29, please provide:
```bash
# Working server-side filtering example
GET https://gamma-api.polymarket.com/markets?[PARAMETER]=[VALUE]&limit=10

# Response structure showing tags
{
  "id": "...",
  "[TAG_FIELD]": "...",  ← Show where tags appear
  ...
}
```

---

## Summary

**Critical Questions**:
1. Why do `/tags` return all "Unknown" names? (Q27A)
2. Is `category` field the actual tag system? (Q27B, Q28)
3. What's the correct filtering parameter: `tag_id` or `category`? (Q27C, Q29A)
4. What values should we pass for filtering? (Q27D, Q29B)
5. Can you provide a working example? (Q29B)

**Goal**: Implement efficient server-side filtering as you recommended in Q22, but we need clarity on the actual API structure to do this correctly.

**Timeline**: This is P0 blocking - we cannot deploy optimal market discovery without this clarification.

Thank you for your patience as we work to implement your Q22 recommendation correctly!
