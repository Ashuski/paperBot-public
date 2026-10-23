# Security notes

Keep API credentials, endpoint keys, Telegram sessions, dashboard passwords, research records, backups, and logs out of Git. `.env.example` contains empty secret fields; `.env` and generated state are ignored.

`generate_session.py` prints a live Telegram authentication secret. Do not capture or share its output. The optional forwarder sends messages, and the main application reads and writes Telegram Saved Messages for backup/restore.

Dashboard access uses username `viewer` and `DASHBOARD_PASSWORD`. Use HTTPS for hosted access and keep the dashboard private. The `/json` handler exists in the source but is not registered as a route. Authentication is not a substitute for a broader review of HTML escaping and external data handling.

A fresh Git history prevents old commits from being included in this repository. It does not revoke credentials exposed elsewhere. Treat any previously exposed credentials as compromised and rotate them through the relevant provider while coordinating changes with private deployments.

For a suspected leak, do not post the credential or session in a public issue. Revoke or rotate it through the provider first, then describe only the affected file and location to the maintainer privately.
