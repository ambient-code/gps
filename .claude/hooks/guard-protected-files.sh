#!/usr/bin/env bash
# Block Claude from editing governance/ files at edit-time (pre-commit catches at commit-time)
FILE="$TOOL_INPUT_FILE_PATH"
if [[ -z "$FILE" ]]; then
  FILE=$(echo "$TOOL_INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('file_path',''))" 2>/dev/null)
fi

if [[ -z "$FILE" ]]; then
  echo "ERROR: Could not determine target file path" >&2
  exit 1
fi

case "$FILE" in
  governance/*|*/governance/*)
    echo "BLOCKED: governance/ files are protected."
    exit 2
    ;;
esac
