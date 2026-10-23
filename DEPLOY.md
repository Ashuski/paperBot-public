# Deployment notes

This public source snapshot is separate from the original private repository and its working Render deployment. Publishing this repository does not require reconnecting, restarting, or modifying that service.

For a separate deployment, install `requirements.txt`, use Python 3.12, and start `python paper_trading_bot.py`. The supplied `Procfile` is retained from the original snapshot and declares a worker. A hosted browser dashboard requires a service type that exposes an HTTP port; the application reads `PORT` and binds to `0.0.0.0`.

Supply the values described in the README through the host's secret environment settings. Require `DASHBOARD_PASSWORD` and HTTPS. Missing dashboard credentials reject requests, so unauthenticated health checks against the dashboard will receive HTTP 401.

Use a separate Telegram session/account for a separate instance. The bot restores and backs up research state through that account's Saved Messages and closes open positions on restart. Do not connect a new instance to private operational data or run simultaneous instances against the same state.

The original host-specific redeployment guide is intentionally excluded from this public snapshot; this document describes the checked-in application's behavior. Hosting-provider settings and live external API compatibility have not been validated as part of public-release preparation.
