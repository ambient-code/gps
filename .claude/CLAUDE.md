# GPS - Global Positioning System MCP Server

Read-only caching tier for org and engineering data. Materializes people, teams, issues, features, releases, and governance into a SQLite database and serves them over MCP.

## Structure
- `mcp_server.py` — MCP server (stdio default, `--http` for HTTP)
- `scripts/` — ETL pipeline (build_db.py, test.sh)
- `data/` — source files + gps.db (user data gitignored, acme-* examples tracked)
- `deploy/` — Kubernetes manifests + deploy.sh automation
- `governance/` — org policies (auto-loaded into DB)

## Commands
```bash
uv run scripts/build_db.py --force           # rebuild database
scripts/test.sh                              # run test suite
uv run mcp_server.py                         # start MCP server (stdio)
uv run mcp_server.py --http                  # start MCP server (HTTP :8000)
```

## Rules
- Never commit: csv, xlsx, pdf, db, .env, tokens, secrets (except data/acme-* examples)
- Database (data/gps.db) is always opened read-only
- Use uv (not pip). Use ruff (enforced by pre-commit).
- `scripts/test.sh` is bash — run directly, not via `uv run`

## More Info
See [BOOKMARKS.md](BOOKMARKS.md) for deployment docs, schema reference, and ADRs.
