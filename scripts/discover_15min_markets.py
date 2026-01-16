"""
Discover 15-minute crypto markets using correct /events endpoint
Per Polymarket support Q30-Q34 guidance (Jan 2026)
"""
import asyncio
import aiohttp
from datetime import datetime

async def find_15min_crypto_tags():
    async with aiohttp.ClientSession() as session:
        # Paginate through ALL tags (Polymarket Q34 guidance)
        all_tags = []
        for offset in [0, 100, 200, 300, 400]:
            url = f'https://gamma-api.polymarket.com/tags?limit=100&offset={offset}'
            try:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        tags = await resp.json()
                        if not tags:
                            break
                        all_tags.extend(tags)
                        print(f'Fetched {len(tags)} tags at offset {offset}')
                    else:
                        break
            except Exception as e:
                print(f'Error at offset {offset}: {e}')
                break
            await asyncio.sleep(0.2)
        
        print(f'\nTotal tags discovered: {len(all_tags)}')
        print('\n' + '='*80)
        print('ANALYZING TAGS FOR 15-MINUTE MARKETS (using /events endpoint)...')
        print('='*80)
        
        # Check each tag for short-term markets using /events endpoint (Q30 guidance)
        short_term_tags = []
        for idx, tag in enumerate(all_tags[:60], 1):  # Check first 60 tags
            tag_id = str(tag.get('id'))
            tag_label = tag.get('label', 'Unknown')
            
            # Use /events endpoint as recommended by Polymarket
            url = f'https://gamma-api.polymarket.com/events'
            params = {
                'tag_id': tag_id,
                'active': 'true',
                'closed': 'false',
                'limit': '30'
            }
            
            try:
                async with session.get(url, params=params, timeout=8) as resp:
                    if resp.status == 200:
                        events = await resp.json()
                        
                        if not events or len(events) == 0:
                            continue
                        
                        # Count markets by settlement time
                        now = datetime.utcnow()
                        under_15min = []
                        under_1hr = []
                        under_4hr = []
                        under_24hr = []
                        
                        for event in events:
                            end_date_iso = event.get('endDate') or event.get('endDateIso')
                            title = event.get('title', 'Unknown')
                            
                            if end_date_iso:
                                try:
                                    if end_date_iso.endswith('Z'):
                                        end_date = datetime.fromisoformat(end_date_iso.replace('Z', '+00:00'))
                                    else:
                                        end_date = datetime.fromisoformat(end_date_iso)
                                    
                                    if end_date.tzinfo:
                                        end_date = end_date.replace(tzinfo=None)
                                    
                                    minutes = (end_date - now).total_seconds() / 60
                                    hours = minutes / 60
                                    
                                    if minutes <= 15:
                                        under_15min.append((title[:60], minutes))
                                    elif hours <= 1:
                                        under_1hr.append((title[:60], hours))
                                    elif hours <= 4:
                                        under_4hr.append((title[:60], hours))
                                    elif hours <= 24:
                                        under_24hr.append((title[:60], hours))
                                except Exception as e:
                                    pass
                        
                        # Only include tags with short-term markets
                        if under_15min or under_1hr or len(under_4hr) >= 2:
                            short_term_tags.append({
                                'id': tag_id,
                                'label': tag_label,
                                'total_events': len(events),
                                'under_15min': len(under_15min),
                                'under_1hr': len(under_1hr),
                                'under_4hr': len(under_4hr),
                                'under_24hr': len(under_24hr),
                                'samples_15min': under_15min[:2],
                                'samples_1hr': under_1hr[:2]
                            })
                            print(f'[{idx}/60] Tag {tag_id} ({tag_label}): {len(under_15min)} <15min, {len(under_1hr)} <1hr, {len(under_4hr)} <4hr')
                            
            except Exception as e:
                print(f'Error checking tag {tag_id}: {e}')
                pass
            
            await asyncio.sleep(0.15)
        
        # Sort by <15min markets first, then <1hr
        short_term_tags.sort(key=lambda x: (x['under_15min'], x['under_1hr'], x['under_4hr']), reverse=True)
        
        print('\n' + '='*80)
        print('TAGS WITH SHORT-TERM MARKETS (15-MINUTE MARKETS HIGHLIGHTED):')
        print('='*80)
        print(f"{'ID':<8} {'Label':<30} {'Total':<8} {'<15min':<8} {'<1hr':<8} {'<4hr':<8} {'<24hr':<8}")
        print('-'*80)
        
        for t in short_term_tags[:25]:
            marker = '🎯' if t['under_15min'] > 0 else '  '
            print(f"{marker} {t['id']:<6} {t['label']:<30} {t['total_events']:<8} {t['under_15min']:<8} {t['under_1hr']:<8} {t['under_4hr']:<8} {t['under_24hr']:<8}")
            
            # Show 15-minute market samples
            if t['samples_15min']:
                for title, mins in t['samples_15min']:
                    print(f"    15min → {mins:.1f}min: {title}")
            
            # Show 1-hour market samples
            if t['samples_1hr'] and not t['samples_15min']:
                for title, hrs in t['samples_1hr']:
                    print(f"    1hr → {hrs:.2f}hr: {title}")
        
        print('\n' + '='*80)
        print('RECOMMENDED FALLBACK TAGS (15-minute crypto markets):')
        print('='*80)
        for t in short_term_tags[:8]:
            if t['under_15min'] > 0 or t['under_1hr'] > 0:
                print(f"    '{t['id']}',      # {t['label']} - {t['under_15min']} <15min, {t['under_1hr']} <1hr markets")

if __name__ == '__main__':
    asyncio.run(find_15min_crypto_tags())
