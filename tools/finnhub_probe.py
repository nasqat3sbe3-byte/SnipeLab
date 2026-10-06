#!/usr/bin/env python3
"""Standalone Finnhub WebSocket coverage probe. Does NOT modify SnipeLab market data.

Run with: FINNHUB_API_KEY=... python tools/finnhub_probe.py --symbols-file symbols.txt
Or use --dashboard-url https://YOUR-SNIPELAB-HOST/api/dashboard
Requires: pip install websockets httpx
The API key is never printed or saved. Run during US market hours for meaningful coverage.
"""
import argparse
import asyncio
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
import websockets


def extract_symbols(payload):
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next((payload[k] for k in ("stocks", "rows", "items", "universe", "data") if isinstance(payload.get(k), (list, dict))), [])
        if isinstance(rows, dict):
            rows = [dict({"symbol": symbol}, **(row if isinstance(row, dict) else {})) for symbol, row in rows.items()]
    else:
        rows = []
    out = []
    for row in rows:
        symbol = row if isinstance(row, str) else row.get("symbol", "") if isinstance(row, dict) else ""
        symbol = str(symbol).strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


async def load_symbols(args):
    if args.symbols_file:
        return extract_symbols(Path(args.symbols_file).read_text().splitlines())
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(args.dashboard_url)
        response.raise_for_status()
        symbols = extract_symbols(response.json())
    if not symbols:
        raise RuntimeError("Dashboard returned no recognizable symbols; use --symbols-file.")
    return symbols


async def probe(args):
    token = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not token:
        raise SystemExit("Set FINNHUB_API_KEY in your environment; never commit the key.")
    symbols = await load_symbols(args)
    print(f"Universe: {len(symbols)} symbols. Test: {args.seconds}s; subscription batch: {args.batch}.")
    seen, counts, last_at, errors = set(), Counter(), {}, []
    started = time.monotonic()
    subscribed = []
    try:
        async with websockets.connect("wss://ws.finnhub.io?token=" + quote(token, safe=""),
                                      ping_interval=20, open_timeout=20, max_size=2**20) as ws:
            # One connection per key; avoid parallel sockets and excessive subscribe bursts.
            for offset in range(0, len(symbols), args.batch):
                batch = symbols[offset:offset + args.batch]
                for symbol in batch:
                    await ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                    subscribed.append(symbol)
                print(f"Subscribed requests sent: {len(subscribed)}/{len(symbols)}")
                # Read provider replies after each batch, including explicit limit errors.
                until = time.monotonic() + args.batch_pause
                while time.monotonic() < until:
                    try:
                        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=min(1, max(.05, until-time.monotonic()))))
                    except asyncio.TimeoutError:
                        continue
                    if message.get("type") == "error":
                        errors.append(message.get("msg", message))
                        print("Provider error:", str(errors[-1])[:250])
                        if args.stop_on_error:
                            break
                    for trade in message.get("data", []):
                        sym = trade.get("s")
                        if sym in symbols:
                            seen.add(sym); counts[sym] += 1; last_at[sym] = trade.get("t")
                if errors and args.stop_on_error:
                    break
            print(f"Observing stream for {args.seconds}s after subscriptions...")
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                try:
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=min(2, max(.05, deadline-time.monotonic()))))
                except asyncio.TimeoutError:
                    continue
                if message.get("type") == "error":
                    errors.append(message.get("msg", message))
                    print("Provider error:", str(errors[-1])[:250])
                for trade in message.get("data", []):
                    sym = trade.get("s")
                    if sym in symbols:
                        seen.add(sym); counts[sym] += 1; last_at[sym] = trade.get("t")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {str(exc)[:250]}")
    result = {
        "tested_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_symbols": len(symbols), "subscription_requests_sent": len(subscribed),
        "symbols_with_trades": len(seen), "symbols_without_trades": sorted(set(symbols)-seen),
        "trades_per_symbol": dict(counts), "last_trade_unix_ms": last_at,
        "provider_errors": errors,
        "note": "No trades is NOT proof of missing coverage: a symbol may be illiquid or the market closed. Verify against active symbols during US trading hours.",
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Results: {len(seen)}/{len(symbols)} symbols received trades; {len(errors)} provider errors. Saved {args.output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--symbols-file", help="One ticker per line")
    src.add_argument("--dashboard-url", help="Full URL ending in /api/dashboard")
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--batch-pause", type=float, default=1.0)
    parser.add_argument("--stop-on-error", action="store_true", default=True)
    parser.add_argument("--output", default="finnhub_probe_results.json")
    asyncio.run(probe(parser.parse_args()))
