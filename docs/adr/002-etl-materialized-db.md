# ADR-002: Materialized ETL Pipeline over Live Queries

**Status**: Accepted
**Date**: 2026-03-11

## Context

Data comes from multiple sources with different access patterns: Jira (REST API or CSV export), Google Sheets (HTTP export), an org spreadsheet (XLSX), and PDFs (static files).

Two approaches were considered:

1. **Live queries**: MCP tools query upstream sources in real-time
2. **Materialized ETL**: Batch pipeline builds a unified SQLite database, MCP tools query the DB

## Decision

Use a materialized ETL pipeline. All data is loaded into `gps.db` by `build_db.py`, and the MCP server opens it read-only.

## Rationale

- **Heterogeneous sources**: Each source has different auth, formats, and reliability. Unifying at query time would require the MCP server to handle all of them.
- **Offline resilience**: The DB works without VPN, active sessions, or network access. Useful for local dev and demos.
- **Performance**: SQLite queries are sub-millisecond. Live Jira queries against large issue sets would be seconds per request.
- **Security boundary**: The LLM never gets credentials or direct access to upstream APIs.

## Consequences

- Data freshness depends on running `build_db.py` after refreshing source files
- Adding a new data source requires writing a loader in `build_db.py`
