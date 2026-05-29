---
description: Refresh all GPS data sources and rebuild the database
---

# Refresh GPS Data

Rebuild all data sources for the GPS MCP server.

## Steps

1. Run these in parallel (they are independent):
   - `uv run scripts/fetch_pricing.py`
   - `uv run scripts/fetch_github.py --org <org>` (ask user for org if not obvious)
   - `uv run scripts/refresh_catalog.py --refresh`
2. After all fetches complete, rebuild the main DB: `uv run scripts/build_db.py --force`
3. Run test suite: `scripts/test.sh`
4. If schema changed, show the diff and ask whether to accept: `scripts/test.sh --accept-schema`
5. Report: row counts per table, any new/removed tables, test results
