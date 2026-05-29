#!/usr/bin/env bash
# Validate generated ATIF trajectories against Harbor's trajectory validator.
#
# Usage:
#   scripts/validate_trajectories.sh [JOBS_DIR]
#
# JOBS_DIR defaults to data/trajectories/jobs. Exits non-zero if any
# trajectory.json fails Harbor's ATIF validation.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
JOBS_DIR="${1:-$REPO_ROOT/data/trajectories/jobs}"

if [ ! -d "$JOBS_DIR" ]; then
    echo "No jobs directory at $JOBS_DIR — run scripts/simulate_trajectories.py first." >&2
    exit 1
fi

uv run --with harbor==0.9.0 python3 - "$JOBS_DIR" <<'PY'
import sys
from pathlib import Path

from harbor.utils.trajectory_validator import validate_trajectory

jobs_dir = Path(sys.argv[1])
files = sorted(jobs_dir.glob("*/*/agent/trajectory.json"))
if not files:
    print(f"No trajectory.json files found under {jobs_dir}", file=sys.stderr)
    sys.exit(1)

failed = 0
for p in files:
    rel = p.relative_to(jobs_dir)
    try:
        validate_trajectory(str(p))
        print(f"VALID    {rel}")
    except Exception as e:  # noqa: BLE001
        failed += 1
        print(f"INVALID  {rel}: {type(e).__name__}: {e}")

print(f"\n{len(files) - failed}/{len(files)} valid")
sys.exit(1 if failed else 0)
PY
