# SnipeLab

Independent reverse-split stock watcher and mobile dashboard.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

Open `/dashboard`, `/health`, or `/api/dashboard`.

## Northflank

Use branch `snipelab-2-mobile-rebuild` with the root Dockerfile, HTTP port `8080`.

## Independence

No runtime reads from Qanas Render or its database. The previous Qanas news feed is deliberately disabled until an independent SEC/news source is implemented. This service uses public StockAnalysis splits, Yahoo chart quotes, IBKR short-stock FTP, and Nasdaq HALT data. No modifications to Qanas repository are required.

## Persistence

Mount `/data` as a persistent volume and set `SNIPELAB_DATA_DIR=/data` (or set `SNIPELAB_DB_PATH` explicitly). SQLite snapshots retain the universe, prices, borrow metrics, historical bars and the independent feature caches. `/api/storage-check` reports saved collections and whether startup restored them. Repository backup branches contain code, not the live SQLite database.

## Stable reference and cleanup — 2026-10-08

The version before maintenance is preserved on branch `backup/2026-10-08-stable-before-cleanup`, commit `6faac266a9923e3d3c3fe23ed3141262b5793f16`. Historical duplicate source files remain available there and in Git history.

Maintenance removes the unused continuous FINRA short-estimate worker and disabled legacy news function. Prices, IBKR Available/CTB/Rebate, history, charts, readiness, hunt, focus, opportunities and archive routes remain active. Low-float price/detail updates save rows and status without rewriting the unchanged discovery catalog; a complete discovery still saves that catalog. SQLite operations explicitly close connections and keep writes transactional. Build context excludes old copies, tests and local database files.
