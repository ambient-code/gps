#!/usr/bin/env bash
# Pre-commit hook: check staged files for possible secrets.
#
# Scans for common secret patterns: API tokens, passwords, bearer tokens,
# client secrets, private keys, and connection strings.

found=0
for f in "$@"; do
    if grep -qE '(API_TOKEN|API_SECRET|API_KEY|PASSWORD|PRIVATE_KEY|Bearer |client_secret|-----BEGIN .* KEY-----)' "$f" 2>/dev/null; then
        echo "  $f"
        found=1
    fi
done

if [ "$found" = "1" ]; then
    echo "BLOCKED: Possible secret detected in file(s) above"
    exit 1
fi
exit 0
