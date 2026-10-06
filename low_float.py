"""Independent low-float discovery/cache; never adds symbols to the split universe."""
import asyncio
import math
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import storage
from rsi import normalized_closes, wilder_rsi

MAX_FLOAT = 5_000_000
MAX_CAP = 300_000_000
DEFAULT_CAP = 100_000_000
MAX_PRICE = 5.0
CATALOG = {}
ROWS = {}
STATUS = {"status": "starting", "scanned": 0, "candidates": 0, "last_scan": None,
          "last_complete_scan": None, "last_prices": None, "error": None}
_massive_lock = asyncio.Lock()
_massive_at = 0.0


def now():
    return datetime.now(timezone.utc).isoformat()


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def fresh(value, seconds):
    try:
        return time.time() - datetime.fromisoformat(value).timestamp() < seconds
    except (ValueError, TypeError):
        return False


def eligible(row, split_symbols, cap=MAX_CAP):
    ff, mc = number(row.get("free_float")), number(row.get("market_cap"))
    return (row.get("symbol") not in split_symbols and row.get("active") is True
            and row.get("type") in {"CS", "ADRC"} and row.get("locale") == "us"
            and row.get("primary_exchange") in {"XNAS", "XNYS", "XASE", "ARCX", "BATS"}
            and ff is not None and 0 < ff <= MAX_FLOAT
            and mc is not None and 0 < mc < cap)


async def massive_get(client, url, token, params=None):
    """One request budget shared with the existing split-float worker."""
    global _massive_at
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "api.massive.com":
        raise ValueError("Unexpected provider pagination host")
    async with _massive_lock:
        await asyncio.sleep(max(0, 13 - (time.monotonic() - _massive_at)))
        _massive_at = time.monotonic()
        response = await client.get(url, params=params, headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 429:
            _massive_at = time.monotonic() + 60
        return response


def restore():
    try:
        data = storage.load(("low_float_catalog", "low_float_rows", "low_float_status"))
        CATALOG.update(data.get("low_float_catalog") or {})
        ROWS.update(data.get("low_float_rows") or {})
        STATUS.update(data.get("low_float_status") or {})
        STATUS.update(status="starting", error=None)
        # A catalog collected with a smaller ceiling omitted the new range.
        # Keep verified cached rows visible while requesting a complete rescan.
        if STATUS.get("catalog_float_max") != MAX_FLOAT:
            STATUS["last_complete_scan"] = None
    except Exception:
        STATUS.update(status="cache_error", error="تعذر استعادة بيانات القسم")


def save():
    storage.save({"low_float_catalog": CATALOG, "low_float_rows": ROWS, "low_float_status": STATUS})


async def discover(client, token):
    # Commit only a complete paginated catalog. A failed page cannot erase the cache.
    found, count, visited = {}, 0, set()
    url = "https://api.massive.com/stocks/vX/float"
    params = {"limit": 5000, "sort": "ticker.asc"}
    while url:
        if url in visited:
            raise ValueError("Repeated pagination cursor")
        visited.add(url)
        response = await massive_get(client, url, token, params)
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("Invalid float response")
        for row in results:
            count += 1
            sym = str(row.get("ticker") or "").upper()
            ff = number(row.get("free_float"))
            if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", sym) and ff is not None and 0 < ff <= MAX_FLOAT:
                found[sym] = {"symbol": sym, "free_float": ff,
                              "free_float_source": "massive_float",
                              "float_effective_date": row.get("effective_date"), "float_checked_at": now()}
        STATUS.update(status="discovering", scanned=count, last_scan=now())
        url, params = payload.get("next_url"), None
    if count == 0:
        raise ValueError("Empty float catalog")
    CATALOG.clear()
    CATALOG.update(found)
    for sym in list(ROWS):
        if sym not in found:
            ROWS.pop(sym)
        else:
            ROWS[sym].update(found[sym])
    STATUS.update(scanned=count, candidates=len(found), last_complete_scan=now(), catalog_float_max=MAX_FLOAT)
    save()


def update_borrow(parsed, received_at):
    for sym, row in ROWS.items():
        if sym in parsed:
            row["borrow"] = {**parsed[sym], "received_at": received_at}


def price_allowed(row):
    quote = row.get("price") or {}
    price = number(quote.get("price"))
    return price is not None and 0 < price < MAX_PRICE and fresh(quote.get("received_at"), 1800)


def rejection_reason(row, split_symbols):
    if row.get("symbol") in split_symbols: return "split"
    if row.get("active") is not True: return "inactive"
    if row.get("type") not in {"CS", "ADRC"}: return "security_type"
    if row.get("locale") != "us": return "locale"
    if row.get("primary_exchange") not in {"XNAS", "XNYS", "XASE", "ARCX", "BATS"}: return "exchange"
    ff, cap = number(row.get("free_float")), number(row.get("market_cap"))
    if ff is None or not 0 < ff <= MAX_FLOAT: return "float"
    if cap is None: return "missing_cap"
    if not 0 < cap < MAX_CAP: return "cap"
    if not price_allowed(row): return "price_missing_stale_or_above_limit"
    return "eligible"


def snapshot(split_symbols):
    rows = {}
    for sym, row in ROWS.items():
        # Unknown/stale fundamental records never silently pass the screen.
        if eligible(row, split_symbols) and price_allowed(row) and fresh(row.get("details_checked_at"), 7 * 86400) and fresh(row.get("float_checked_at"), 7 * 86400):
            rows[sym] = dict(row)
    reasons = {}
    samples = []
    for row in ROWS.values():
        reason = rejection_reason(row, split_symbols)
        reasons[reason] = reasons.get(reason, 0) + 1
        if len(samples) < 8:
            samples.append({k: row.get(k) for k in ("symbol", "active", "type", "locale", "primary_exchange", "market_cap", "free_float")})
    return {"rows": rows, "status": {**STATUS, "verification": {"checked": len(ROWS), "reasons": reasons, "samples": samples}}, "server_time": now(),
            "criteria": {"free_float_max": MAX_FLOAT, "market_cap_max": MAX_CAP,
                         "default_market_cap_max": DEFAULT_CAP, "last_price_max_exclusive": MAX_PRICE, "rsi_required": False}}


async def discovery_worker(split_symbols):
    await asyncio.sleep(35)
    token = os.environ.get("MASSIVE_API_KEY", "").strip()
    if not token:
        STATUS.update(status="disabled", error="مصدر الفلوت غير مفعّل؛ لا يمكن اكتشاف الأسهم حاليًا")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                if not fresh(STATUS.get("last_complete_scan"), 86400):
                    await discover(client, token)
                STATUS.update(status="checking", error=None)
                for sym, item in sorted(list(CATALOG.items()), key=lambda pair: (pair[0] != "ANPA", pair[1]["free_float"])):
                    if sym in split_symbols:
                        ROWS.pop(sym, None)
                        continue
                    cached = ROWS.get(sym) or {}
                    if fresh(cached.get("details_checked_at"), 86400):
                        continue
                    response = await massive_get(client, "https://api.massive.com/v3/reference/tickers/" + sym, token)
                    if response.status_code == 404:
                        ROWS[sym] = {**item, "active": False, "details_checked_at": now()}
                    else:
                        response.raise_for_status()
                        details = response.json().get("results") or {}
                        ROWS[sym] = {**cached, **item, "company_name": details.get("name"),
                                     "market_cap": number(details.get("market_cap")),
                                     "market_cap_source": "massive_ticker_details",
                                     "type": details.get("type"), "locale": details.get("locale"),
                                     "active": details.get("active"), "primary_exchange": details.get("primary_exchange"),
                                     "details_checked_at": now()}
                    STATUS.update(last_scan=now(), checked=len(ROWS))
                    save()
                STATUS.update(status="ready", error=None)
                save()
                await asyncio.sleep(900)
            except Exception as exc:
                # Do not expose credentials embedded in external exception URLs.
                code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                STATUS.update(status="access_error" if code in (401, 402, 403) else "retrying",
                              error="تعذر تحديث مصدر البيانات" + (f" (HTTP {code})" if code else ""))
                await asyncio.sleep(90 if code not in (401, 402, 403) else 900)


def market_queue(split_symbols):
    due = []
    for sym, row in ROWS.items():
        if not eligible(row, split_symbols) or not fresh(row.get("details_checked_at"), 7 * 86400) or not fresh(row.get("float_checked_at"), 7 * 86400):
            continue
        quote = row.get("price") or {}
        price = number(quote.get("price"))
        interval = 900 if price is not None and price >= MAX_PRICE else 120
        if fresh(quote.get("received_at"), interval) and (price is None or price >= MAX_PRICE or fresh(row.get("rsi_updated_at"), 900)):
            continue
        if fresh(row.get("market_attempted_at"), 120):
            continue
        # New records cannot wait behind a complete scan of old symbols.
        due.append((price is not None, row.get("market_attempted_at") or "", sym))
    return [sym for _, _, sym in sorted(due)]


async def refresh_market_batch(client, split_symbols, fetch_quote):
    queue = market_queue(split_symbols)
    STATUS.update(market_worker_version=2, market_queue_pending=len(queue))
    selected = queue[:12]
    jobs = asyncio.Semaphore(2)
    quote_sem = asyncio.Semaphore(2)
    async def one(sym):
        async with jobs:
            row = ROWS.get(sym)
            if not row: return
            row["market_attempted_at"] = now()
            try:
                quote = row.get("price") or {}
                interval = 900 if (number(quote.get("price")) or 0) >= MAX_PRICE else 120
                if not fresh(quote.get("received_at"), interval):
                    _, quote = await fetch_quote(client, quote_sem, sym)
                    if quote: row["price"] = quote
                    else: raise ValueError("No quote")
                # Publish price-qualified rows before requesting the slower RSI history.
                # RSI remains optional and never holds back price discovery.
                price = number((row.get("price") or {}).get("price"))
                if price is not None and 0 < price < MAX_PRICE and not fresh(row.get("rsi_updated_at"), 900):
                    response = await client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                                                params={"range": "2y", "interval": "1d", "events": "splits"})
                    response.raise_for_status()
                    result = response.json()["chart"]["result"][0]
                    tz = ZoneInfo(result.get("meta", {}).get("exchangeTimezoneName") or "America/New_York")
                    closes = result["indicators"]["quote"][0].get("close") or []
                    series, repairs = normalized_closes(sym, [(datetime.fromtimestamp(t, tz).date().isoformat(), closes[i])
                                                             for i, t in enumerate(result.get("timestamp") or []) if i < len(closes)])
                    if len(series) >= 15:
                        row.update(rsi_daily=round(wilder_rsi([v for _, v in series]), 2),
                                   rsi_updated_at=now(), rsi_last_bar_date=series[-1][0], rsi_source_repairs=repairs)
            except Exception:
                row["market_error"] = "تعذر تحديث السعر أو RSI"
            else:
                row.pop("market_error", None)
    await asyncio.gather(*(one(sym) for sym in selected))
    STATUS.update(last_prices=now(), market_batch_count=len(selected))
    return len(selected)


async def market_worker(split_symbols, fetch_quote):
    await asyncio.sleep(45)
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": "Mozilla/5.0 SnipeLab"}) as client:
        while True:
            await refresh_market_batch(client, split_symbols, fetch_quote)
            try:
                save()
            except Exception:
                STATUS["error"] = "تعذر حفظ آخر تحديث"
            # Re-evaluate newly verified symbols after every small batch.
            await asyncio.sleep(15)
