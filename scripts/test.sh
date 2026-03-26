#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="$REPO_ROOT/data/gps.db"
SCHEMA_FILE="$REPO_ROOT/data/schema.sql"
PASS=0
FAIL=0

run() {
    local label="$1"; shift
    printf "%-40s" "$label"
    if "$@" > /dev/null 2>&1; then
        echo "PASS"
        PASS=$((PASS + 1))
    else
        echo "FAIL"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== GPS Test Suite ==="
echo ""

# 1. Config validation
echo "--- Config ---"
ENV_FILE="$REPO_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    printf "%-40s%s\n" ".env exists" "PASS"
    PASS=$((PASS + 1))
else
    printf "%-40s%s\n" ".env exists" "SKIP (optional)"
fi

# 2. Lint
echo ""
echo "--- Lint ---"
run "ruff check" uv run --extra dev ruff check "$REPO_ROOT"
run "ruff format --check" uv run --extra dev ruff format --check "$REPO_ROOT"

# 3. Build DB
echo ""
echo "--- Build ---"
printf "%-40s" "build_db.py --force"
if uv run "$REPO_ROOT/scripts/build_db.py" --force > /dev/null 2>&1; then
    echo "PASS"
    PASS=$((PASS + 1))
else
    echo "FAIL"
    FAIL=$((FAIL + 1))
    echo ""
    echo "FAILED: $FAIL  PASSED: $PASS"
    exit 1
fi

# 4. DB integrity
echo ""
echo "--- Database ---"
run "integrity_check" uv run python3 -c "
import sqlite3, sys
conn = sqlite3.connect('$DB_PATH')
result = conn.execute('PRAGMA integrity_check').fetchone()[0]
sys.exit(0 if result == 'ok' else 1)
"

# 5. Key tables populated
for table in person jira_issue feature release_schedule governance_document scrum_team_board; do
    ROW_COUNT=$(uv run python3 -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
print(conn.execute('SELECT COUNT(*) FROM $table').fetchone()[0])
")
    if [ "$ROW_COUNT" -gt 0 ]; then
        printf "%-40s%s\n" "table: $table has rows" "PASS"
        PASS=$((PASS + 1))
    elif [ "$table" = "scrum_team_board" ] && ls "$REPO_ROOT"/data/acme-*.xlsx > /dev/null 2>&1; then
        printf "%-40s%s\n" "table: $table has rows" "SKIP (acme data lacks tab)"
    else
        printf "%-40s%s\n" "table: $table has rows" "FAIL"
        FAIL=$((FAIL + 1))
    fi
done

# 6. Schema diff
echo ""
echo "--- Schema ---"
NEW_SCHEMA=$(uv run python3 -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
for row in conn.execute(\"SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name\"):
    print(row[0] + ';')
")

if [ -f "$SCHEMA_FILE" ]; then
    if echo "$NEW_SCHEMA" | diff -q "$SCHEMA_FILE" - > /dev/null 2>&1; then
        printf "%-40s%s\n" "schema unchanged" "PASS"
        PASS=$((PASS + 1))
    else
        printf "%-40s%s\n" "schema CHANGED" "DIFF"
        echo ""
        echo "$NEW_SCHEMA" | diff "$SCHEMA_FILE" - || true
        echo ""
        echo "To accept: scripts/test.sh --accept-schema"
    fi
else
    printf "%-40s%s\n" "schema baseline" "NEW"
    echo "$NEW_SCHEMA" > "$SCHEMA_FILE"
    echo "  Written to $SCHEMA_FILE"
fi

if [ "${1:-}" = "--accept-schema" ]; then
    echo "$NEW_SCHEMA" > "$SCHEMA_FILE"
    echo "  Schema baseline updated."
fi

# Summary
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ] || exit 1
