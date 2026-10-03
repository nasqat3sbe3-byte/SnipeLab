"""Cached upcoming actions from exchange notices; no network in dashboard requests."""
import asyncio
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime

import httpx
from bs4 import BeautifulSoup

FEED = 'https://www.nasdaqtrader.com/rss.aspx?feed=currentheadlines&categorylist=0'
LABELS = {'reverse_split': 'تقسيم مستقبلي', 'merger': 'دمج مستقبلي'}
MONTH_DATE = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+20\d{2}'

def market_today():
    return datetime.now(ZoneInfo('America/New_York')).date().isoformat()

def kind_of(title):
    if re.search(r'reverse (?:stock )?split|share consolidation', title, re.I):
        return 'reverse_split'
    if re.search(r'\bmerger\b|business combination', title, re.I):
        return 'merger'
    return None

def parse_notice(title, body, url, symbol):
    kind = kind_of(title)
    if not kind:
        return None
    text = re.sub(r'\s+', ' ', body)
    # Updates announcing cancellation/completion must replace the cached notice.
    if re.search(r'merger closed|cancelled|canceled|withdrawn|terminated', title, re.I):
        return None
    if re.search(r'(?:merger|business combination|reverse (?:stock )?split).{0,90}(?:has been completed|was completed|has closed|was cancelled|has been cancelled|has been terminated)', text, re.I):
        return None
    effective = None
    for sentence in re.split(r'(?<=[.!?])\s+', text):
        # Do not mistake the announcement, shareholder meeting or voting date
        # for the execution date. Only explicitly effective/trading/closing dates.
        match = re.search(r'(?:will (?:become|be) effective|become effective|effective (?:on|as of|at)|will (?:begin|commence) trading|(?:expected|scheduled|anticipated) to (?:close|be completed)(?: on)?|closing (?:date|on)).{0,100}?(' + MONTH_DATE + ')', sentence, re.I)
        if match:
            raw = re.sub(r',', '', match.group(1))
            try:
                effective = datetime.strptime(raw, '%B %d %Y').date().isoformat()
                break
            except ValueError:
                pass
    # Exchange notices announce actual corporate actions, not speculative news.
    return {'symbol': symbol, 'kind': kind, 'label': LABELS[kind],
            'effective_date': effective, 'source': 'Nasdaq', 'source_url': url,
            'title': title, 'checked_at': datetime.now(ZoneInfo('UTC')).isoformat()}

def upcoming(cache, symbol, today=None):
    today = today or market_today()
    matches = [a for a in cache.values() if a.get('symbol') == symbol
               and a.get('kind') in LABELS and a.get('status', 'pending') == 'pending'
               and (not a.get('effective_date') or a['effective_date'] > today)
               and (a.get('effective_date') or a.get('kind') == 'merger'
                    or a.get('published_date', '') >= (datetime.fromisoformat(today).date() - timedelta(days=14)).isoformat())]
    # Prefer exchange confirmation over public-calendar duplicates.
    result = {}
    for a in sorted(matches, key=lambda a: a.get('source') == 'Nasdaq', reverse=True):
        result.setdefault(a['kind'], a)
    return sorted(result.values(), key=lambda a: a.get('effective_date') or '9999')

async def scan(client, universe, cache, state, batch=24):
    response = await client.get(FEED, timeout=20)
    response.raise_for_status()
    items = ET.fromstring(response.content).findall('./channel/item')
    candidates = []
    now = time.time()
    for item in items:
        title, url = item.findtext('title', ''), item.findtext('link', '')
        try:
            published = parsedate_to_datetime(item.findtext('pubDate', '')).date().isoformat()
        except (TypeError, ValueError):
            published = ''
        if not kind_of(title) or urlparse(url).hostname not in {'www.nasdaqtrader.com', 'nasdaqtrader.com'}:
            continue
        url = url.replace('http://', 'https://', 1)
        symbols = set()
        for group in re.findall(r'\(([A-Z0-9./, ]+)\)', title):
            symbols.update(re.split(r'[/, ]+', group))
        symbols &= set(universe)
        if not symbols:
            continue
        for symbol in symbols:
            key = url + '#' + symbol
            old = cache.get(key, {})
            active = old.get('status', 'pending') == 'pending' and (not old.get('effective_date') or old['effective_date'] > market_today())
            if old.get('title') != title or (active and now - old.get('checked_epoch', 0) >= 3600):
                candidates.append((key, title, url, symbol, published))
    semaphore = asyncio.Semaphore(2)
    async def fetch(entry):
        key, title, url, symbol, published = entry
        async with semaphore:
            try:
                r = await client.get(url, timeout=15)
                r.raise_for_status()
                soup = BeautifulSoup(r.text, 'html.parser')
                body = soup.select_one('.newscontentbox2')
                if body is None:
                    raise ValueError('exchange notice body missing')
                action = parse_notice(title, body.get_text(' ', strip=True), url, symbol)
                cache[key] = action or {'symbol': symbol, 'title': title, 'status': 'closed'}
                cache[key]['checked_epoch'] = now
                cache[key]['published_date'] = published
            except Exception as exc:
                state['corporate_actions_error'] = str(exc)[:160]
    state['corporate_actions_error'] = None
    await asyncio.gather(*(fetch(e) for e in candidates[:batch]))
    state['last_corporate_actions_scan'] = datetime.now(ZoneInfo('UTC')).isoformat()
    state['corporate_actions_remaining'] = max(0, len(candidates) - batch)
    return len(candidates) > batch

async def worker(universe, cache, state, save):
    await asyncio.sleep(3)
    async with httpx.AsyncClient(follow_redirects=True, headers={'User-Agent': 'SnipeLab corporate action monitor'}) as client:
        while True:
            pending = False
            try:
                pending = await scan(client, universe, cache, state)
                save(force=True)
            except Exception as exc:
                state['corporate_actions_error'] = f'{type(exc).__name__}: {str(exc)[:140]}'
            await asyncio.sleep(60 if pending else 600)
