# GPS - Global Positioning System MCP Server

Read-only caching tier for org and engineering data. Materializes people, teams, issues, features, releases, and governance into a SQLite database. Optimized for sub-millisecond agent queries over MCP — no auth, no rate limits.

## Structure
- `mcp_server.py` — MCP server (stdio default, `--http` for HTTP)
- `scripts/` — ETL pipeline (build_db.py, test.sh)
- `data/` — source files + gps.db (user data gitignored, acme-* examples tracked)
- `deploy/` — Kubernetes manifests + deploy.sh automation
- `docs/` — ADRs, deployment, customization, schema reference
- `governance/` — org policies (auto-loaded into DB)

## Commands
```bash
uv run scripts/build_db.py --force           # rebuild database
scripts/test.sh                              # run test suite
uv run mcp_server.py                         # start MCP server (stdio)
uv run mcp_server.py --http                  # start MCP server (HTTP :8000)
```

## MCP Tools (read-only, no auth)
- `lookup_person` — find people by name, email, or user ID
- `list_team_members` — list team roster with roles and components
- `search_issues` — filter issues by status, priority, assignee, component, label, keyword
- `get_feature_status` — feature details: progress, RICE score, releases, teams
- `release_risk_summary` — flags features under 80% complete near milestones
- `list_documents` — governance documents with table of contents
- `get_document` — full governance document by ID
- `get_document_section` — single section by fuzzy heading match
- `get_gps_version` — version and build metadata

## MCP Resources
- `gps://schema` — full DDL with row counts (read this first)
- `gps://catalog` — data source inventory

## ACP Integration
GPS runs as a sidecar MCP in every ACP pod. No auth needed — read-only org data. Wire via managed settings, init container, or HTTP sidecar. See docs/DEPLOYMENT.md.

## Rules
- Never commit: csv, xlsx, pdf, db, .env, tokens, secrets (except data/acme-* examples)
- Database (data/gps.db) is always opened read-only
- Use uv (not pip). Use ruff (enforced by pre-commit).
- `scripts/test.sh` is bash — run directly, not via `uv run`
