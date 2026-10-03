# SnipeLab

Independent reverse-split stock watcher and mobile dashboard.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080
```

Open `/dashboard`, `/health`, or `/api/dashboard`.

## Northflank

Use this repository's `main` branch with the root Dockerfile, HTTP port `8080`.

## Independence

No runtime reads from Qanas Render or its database. The previous Qanas news feed is deliberately disabled until an independent SEC/news source is implemented. This service uses public StockAnalysis splits, Yahoo chart quotes, IBKR short-stock FTP, and Nasdaq HALT data. No modifications to Qanas repository are required.

## Important limitations

The post-split historical bootstrap is not yet implemented, so historical lows, half levels, stability sessions, and readiness for existing tickers may be incomplete until independently reconstructed. The default `/tmp` state file is ephemeral on container replacement; configure a persistent volume and `SNIPELAB_STATE_FILE` for durable state. No paid infrastructure should be enabled without approval.
