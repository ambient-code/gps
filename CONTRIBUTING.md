# Contributing to GPS

## Development Setup

```bash
git clone https://github.com/YOUR-ORG/gps.git && cd gps
cp .env.example .env
uv run scripts/build_db.py --force
scripts/test.sh
```

## Workflow

1. Fork the repo and create a feature branch from `main`
2. Make your changes
3. Run `scripts/test.sh` — all checks must pass
4. Open a PR against `main`
5. PRs are squash-merged

## Code Style

- Python: [ruff](https://docs.astral.sh/ruff/) enforced by pre-commit hooks
- Line length: 120 characters
- Target: Python 3.11+

Run manually:

```bash
uv run ruff check .
uv run ruff format .
```

## Test Suite

`scripts/test.sh` runs:

1. Config validation (`.env` exists)
2. Lint (`ruff check`, `ruff format --check`)
3. Database build (`build_db.py --force`)
4. SQLite integrity check
5. Key table population checks
6. Schema diff against baseline

Accept schema changes with:

```bash
scripts/test.sh --accept-schema
```

## Adding a Data Source

1. Place a CSV/XLSX/PDF in `data/`
2. Add a loader function in `scripts/build_db.py` (follow existing patterns)
3. Add the corresponding table(s) to the `SCHEMA` string
4. Wire the loader into `build_database()`
5. Add a meta hash entry for change detection
6. If the data needs an MCP tool, add one in `mcp_server.py`
7. Run `scripts/test.sh --accept-schema` to update the schema baseline

## Rules

- Never commit: `.csv`, `.xlsx`, `.pdf`, `.db`, `.env`, tokens, secrets
- Exception: `data/acme-*` example files are tracked
- Use `uv` (not `pip`)
- Database is always opened read-only by consumers
- Governance policy edits require explicit approval
