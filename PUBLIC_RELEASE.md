# Public snapshot provenance

Prepared from the owner's supplied `paperBot-secure-full-deploy` source snapshot on 23 October 2026.

All six Python source files, `requirements.txt`, `runtime.txt`, and `Procfile` are byte-for-byte copies of that snapshot. No source implementation has been replaced, simplified, or refactored for this release.

The original README, deployment documentation, and ignore rules were replaced with public-facing documentation and stronger exclusions. The historical `RAILWAY_SETUP.md` operational guide is excluded. A configuration example, security notes, and synthetic offline tests were added.

The public repository starts from a single new root commit. The original private repository contains the development history with many commits and remains separate. No old Git objects, branches, tags, credentials, authenticated sessions, historical records, or private host settings are imported.

The existing private Render deployment is managed separately. No public live dashboard URL or private operational data is included here.
