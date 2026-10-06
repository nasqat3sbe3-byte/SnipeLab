# SnipeLab 2.0 — approved implementation contract

Branch: `snipelab-2-mobile-rebuild`. Preserve `main` and the independent Qanas repository. No production deployment or paid infrastructure without explicit approval.

## Mobile UI
Dark black/gold design from the approved earlier prototype, fixed bottom navigation:
1. **الكل** — every verified reverse split effective 2026-05-01 through 2026-12-30; automatic additions; search and sort. Prominently show independently reconstructed *lowest post-split low* and *highest post-split high* plus split-day opening and high, price, distance from low, Available, CTB, Rebate, daily RSI, stability sessions and per-source timestamps.
2. **الفلترة** — Ready (premium black/gold large-number cards), Near Ready (one or two missing conditions), **توب** (distinctive gold cards, >=40% from the lowest low to highest high **within the last 10 completed trading sessions**, with both dates/prices). No cooldown; a stock can appear in multiple lists.
3. **الشارت** — embedded TradingView widget, ticker navigation from stock cards; clearly distinguish embedded widget capabilities from the full TradingView app.
4. **الأخبار** — ticker lookup, independent dated linked news and SEC disclosures, Arabic evidence-based positive/negative/mixed summaries. Never label an LLM-only inference as a verified fact.

## Background server and accuracy gates
- Fetch and calculate on server regardless of dashboard sessions; persist verified universe, historical daily OHLCV, split events, borrow snapshots, signal state and event dedupe IDs to durable storage. Do not rely on ephemeral `/tmp` for production.
- Bootstrap complete post-split daily history for every ticker; don't confuse current-day high/low with historical post-split extrema. Correct corporate-action adjustments and split-day trading date; exclude pre-effective dates.
- Ready thresholds from previous specification: Available <10,000 (50-point weight); daily RSI <30; traded down to half the **split-day highest high** (not half opening or all-time high); proximity to post-split low; 2–4 trading sessions without breaking low. A new low resets stability. Display missing/stale values as unknown, not zero; readiness is indeterminate until required data is available.
- Track latest 10 **completed market sessions** using the US exchange calendar; top calculation uses minimum low and subsequent maximum high in that window (avoid counting a high that occurred before the low); show calculation method and dates. Refresh each completed session and on incoming intraday data only if explicitly labelled intraday.
- Borrow data from IBKR public FTP where accessible; record source and timestamp. Available=0 is source-specific, not market-wide.
- Event center is visible from every tab, with ticker links, timestamp, event history, deduplication and three primary events: NASDAQ HALT, positive-to-zero Available transition, and entry to Ready. Show feed errors/staleness. Push notifications are not included until separately authorized.
- Frontend should paint last persisted snapshot immediately and refresh incrementally; don't block page load on remote data calls. Display data freshness.
- Independent of Qanas Render, databases, secrets and GitHub repository.

## Acceptance checks before production merge
- Universe range includes May 1 and Dec 30 and excludes Dec 31; future effective splits appear only when appropriate.
- Compare several ticker post-split historical lows/highs and split-day highs to dated daily OHLCV; test reverse-split adjusted-price handling.
- Ten-session TOP test where high precedes low must not yield a fictitious rise; overlapping categories allowed.
- Verify zero vs missing borrow and deduped event transitions; verify HALT source and freshness.
- Reboot persistence, mobile responsiveness, navigation, quote and feed failure states, and TradingView integration.
- Keep previous main commit as rollback; merge/deploy only after tests and user's review.
