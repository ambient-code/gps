#!/usr/bin/env bash
# Pre-commit hook: block changes to protected files.
#
# Protected paths are defined in the PROTECTED_PATHS variable below.
# Customize for your project.
#
# To bypass for intentional changes, use:
#   ALLOW_PROTECTED=1 git commit -m "description"

if [ "${ALLOW_PROTECTED}" = "1" ]; then
    exit 0
fi

# Define protected file patterns (space-separated globs).
# Override via PROTECTED_PATHS env var if needed.
PROTECTED_PATHS="${PROTECTED_PATHS:-governance/}"

changed=$(git diff --cached --name-only)
blocked=""

for pattern in $PROTECTED_PATHS; do
    for file in $changed; do
        case "$file" in
            $pattern*)
                blocked="$blocked $file"
                ;;
        esac
    done
done

if [ -n "$blocked" ]; then
    echo "BLOCKED: Protected files modified:$blocked"
    echo ""
    echo "To commit intentional changes:"
    echo "  ALLOW_PROTECTED=1 git commit -m 'description of change'"
    exit 1
fi
exit 0
