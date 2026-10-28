"""Background-only cache of scheduled high-impact US economic events."""
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import httpx

URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json'
RIYADH = ZoneInfo('Asia/Riyadh')
CACHE = {'events': [], 'updated_at': None}


def normalize(rows):
    if not isinstance(rows, list):
        raise ValueError('Invalid calendar response')
    events = []
    for row in rows:
        if not isinstance(row, dict) or row.get('country') != 'USD':
            continue
        title = str(row.get('title') or '').strip()
        if row.get('impact') != 'High' and not ('FOMC' in title and 'Press Conference' in title):
            continue
        try:
            when = datetime.fromisoformat(str(row['date']).replace('Z', '+00:00'))
            if when.tzinfo is None:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        if title:
            events.append({'title': title[:160], 'time': when.astimezone(timezone.utc).isoformat()})
    return sorted(events, key=lambda e: e['time'])


def snapshot(now=None):
    now = now or datetime.now(timezone.utc)
    updated = CACHE['updated_at']
    if updated is None or now - updated > timedelta(hours=2):
        return {'status': 'unavailable', 'events': []}
    today = now.astimezone(RIYADH).date()
    events = [event for event in CACHE['events'] if datetime.fromisoformat(event['time']).astimezone(RIYADH).date() == today]
    return {'status': 'ok', 'events': events, 'updated_at': updated.isoformat()}


async def worker():
    async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={'User-Agent': 'SnipeLab economic calendar/1.0'}) as client:
        while True:
            try:
                response = await client.get(URL)
                response.raise_for_status()
                events = normalize(response.json())
                CACHE.update(events=events, updated_at=datetime.now(timezone.utc))
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(1800)
