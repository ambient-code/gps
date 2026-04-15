#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Refresh DATA_CATALOG.yaml from the filesystem.

Scans the data directory, renames files to match the naming convention
(YYYY-MM-DD-kebab-case-name.ext), auto-populates filename/date/source_type,
and preserves manually-entered fields (description, source_url, notes)
for files already in the catalog. New files get stub entries.
Entries for deleted files are removed.

With --refresh, fetches fresh data from upstream sources (Jira, web URLs)
before renaming and cataloging. Only updates files when content has actually
changed.

Configuration:
  - Jira: set JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN env vars (or in .env)
  - JQL aliases: config/jql-aliases.json (override with JQL_ALIASES_PATH env var)
  - Data sources: each catalog entry's source_url/jql_alias drives refresh
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DATA_DIR = REPO_ROOT / "data"
CONFIG_DIR = REPO_ROOT / "config"
DOTENV_PATH = REPO_ROOT / ".env"
CATALOG_PATH = DATA_DIR / "DATA_CATALOG.yaml"
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.*)")
SKIP_FILES = {
    "DATA_CATALOG.yaml",
    "DATA_CATALOG.yaml.bak",
    ".DS_Store",
    ".gitignore",
    ".env",
    "schema.sql",
    "README.md",
}
# Also skip any .db files (databases are build artifacts, not cataloged sources)
JQL_ALIASES_PATH = Path(
    os.environ.get("JQL_ALIASES_PATH", str(CONFIG_DIR / "jql-aliases.json"))
)
SKIP_SUFFIXES = {".bak", ".tmp", ".db-shm", ".db-wal", ".db"}

EXTENSION_SOURCE_MAP = {
    ".csv": "csv",
    ".pdf": "pdf",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".txt": "text",
    ".xlsx": "spreadsheet",
}

MIME_TO_EXT = {
    "PDF": ".pdf",
    "CSV": ".csv",
    "JSON": ".json",
    "HTML": ".html",
    "XML": ".xml",
    "ASCII text": ".txt",
}


def load_dotenv(path: Path) -> None:
    """Load .env file into os.environ. Does not override existing vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def is_data_file(path: Path) -> bool:
    """Return True if path is a data file we should catalog."""
    return (
        path.is_file()
        and path.name not in SKIP_FILES
        and path.suffix not in SKIP_SUFFIXES
        and not path.name.startswith(".")
    )


def sniff_extension(filepath: Path) -> str:
    """Use `file --brief` to guess the file extension."""
    try:
        result = subprocess.run(
            ["file", "--brief", str(filepath)],
            capture_output=True,
            text=True,
            check=True,
        )
        output = result.stdout.strip()
        for keyword, ext in MIME_TO_EXT.items():
            if keyword in output:
                return ext
    except subprocess.CalledProcessError:
        pass
    return ""


def get_birth_date(filepath: Path) -> str:
    """Get the file's birth date as YYYY-MM-DD using stat."""
    try:
        result = subprocess.run(
            ["stat", "-f", "%SB", "-t", "%Y-%m-%d", str(filepath)],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return datetime.now().strftime("%Y-%m-%d")


def normalize_filename(filepath: Path) -> str:
    """Compute the normalized filename for a data file.

    Convention: YYYY-MM-DD-kebab-case-name.ext

    Rules applied in order:
    1. Extract or add date prefix from filesystem birth time.
    2. Lowercase the name portion.
    3. Spaces -> hyphens, underscores -> hyphens.
    4. Collapse consecutive hyphens.
    5. Sniff extension if missing.
    6. Preserve existing correct extensions.
    """
    name = filepath.name

    # Split off date prefix if present
    match = DATE_PREFIX_RE.match(name)
    if match:
        date_prefix = match.group(1)
        remainder = match.group(2)
    else:
        date_prefix = get_birth_date(filepath)
        remainder = name

    # Split remainder into stem and extension
    # Only treat known extensions as real extensions; dots in names like
    # "example.com-foo" should not be treated as extensions.
    known_extensions = set(EXTENSION_SOURCE_MAP.keys()) | set(MIME_TO_EXT.values())
    rest_path = Path(remainder)
    ext = rest_path.suffix.lower()
    if ext in known_extensions:
        stem = remainder[: len(remainder) - len(rest_path.suffix)]
    else:
        ext = ""
        stem = remainder

    # Normalize the stem
    stem = stem.lower()
    stem = stem.replace(" ", "-").replace("_", "-")
    stem = re.sub(r"-{2,}", "-", stem)
    stem = stem.strip("-")

    # Sniff extension if missing
    if not ext:
        ext = sniff_extension(filepath)

    return f"{date_prefix}-{stem}{ext}"


def guess_source_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in EXTENSION_SOURCE_MAP:
        return EXTENSION_SOURCE_MAP[ext]
    try:
        result = subprocess.run(
            ["file", "--brief", str(DATA_DIR / filename)],
            capture_output=True,
            text=True,
            check=True,
        )
        if "PDF" in result.stdout:
            return "pdf"
    except subprocess.CalledProcessError:
        pass
    return "unknown"


def rename_files(dry_run: bool = False) -> dict[str, str]:
    """Rename data files to match naming convention.

    Returns a mapping of {old_name: new_name} for files that were renamed.
    """
    renames: dict[str, str] = {}

    data_files = sorted(f for f in DATA_DIR.iterdir() if is_data_file(f))

    for filepath in data_files:
        old_name = filepath.name
        new_name = normalize_filename(filepath)

        if old_name == new_name:
            continue

        # Handle collision: if target already exists, skip
        new_path = DATA_DIR / new_name
        if new_path.exists() and new_path != filepath:
            print(f"  SKIP (target exists): {old_name} -> {new_name}")
            continue

        renames[old_name] = new_name

        if dry_run:
            print(f"  Would rename: {old_name} -> {new_name}")
        else:
            filepath.rename(new_path)
            print(f"  Renamed: {old_name} -> {new_name}")

    return renames


def parse_catalog(path: Path) -> dict[str, dict]:
    """Parse existing YAML catalog into {filename: fields} dict.

    Uses a simple line-based parser to avoid a PyYAML dependency.
    """
    entries: dict[str, dict] = {}
    if not path.exists():
        return entries

    current: dict | None = None
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if line.startswith("- filename:"):
            if current and "filename" in current:
                entries[current["filename"]] = current
            current = {}
            val = line.split(":", 1)[1].strip().strip('"')
            current["filename"] = val
        elif current is not None and ":" in line and not line.startswith("#"):
            key, val = line.split(":", 1)
            key = key.strip()
            val = val.strip().strip('"')
            if val == "null":
                val = None
            current[key] = val

    if current and "filename" in current:
        entries[current["filename"]] = current

    return entries


def render_catalog(entries: list[dict]) -> str:
    """Render catalog entries to YAML string."""
    lines = [
        "# Data Catalog",
        "# Describes each file: what it is, when it was captured, and where it came from.",
        "",
        "files:",
    ]

    for entry in entries:
        lines.append("")
        fn = entry["filename"]
        needs_quote = " " in fn or ":" in fn or "_" in fn
        fn_str = f'"{fn}"' if needs_quote else fn
        lines.append(f"  - filename: {fn_str}")

        for key in (
            "date",
            "description",
            "source_type",
            "source_url",
            "jql_alias",
            "notes",
        ):
            val = entry.get(key)
            if val is None:
                lines.append(f"    {key}: null")
            else:
                needs_q = any(c in str(val) for c in (" ", ":", "_", "#", '"'))
                if key == "source_url":
                    needs_q = False  # URLs are fine unquoted in YAML
                val_str = f'"{val}"' if needs_q else str(val)
                lines.append(f"    {key}: {val_str}")

    lines.append("")
    return "\n".join(lines)


def write_catalog(path: Path, content: str) -> None:
    """Atomically write catalog: backup existing, write to temp, then replace.

    Guarantees the catalog is never left in a partial/corrupt state.
    """
    # Back up existing catalog
    if path.exists():
        backup = path.with_suffix(".yaml.bak")
        backup.write_text(path.read_text())

    # Write to temp file in the same directory, then atomic rename
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_path, str(path))
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


JIRA_CSV_FIELDS = [
    "key",
    "summary",
    "status",
    "priority",
    "assignee",
    "reporter",
    "issuetype",
    "created",
    "updated",
    "labels",
    "components",
]


def load_jql_aliases(path: Path) -> dict:
    """Load jql-aliases.json and return the parsed dict."""
    if not path.exists():
        raise FileNotFoundError(f"JQL aliases file not found: {path}")
    with open(path) as f:
        return json.load(f)


def fetch_url(url: str, extra_headers: dict | None = None) -> bytes:
    """HTTP GET a URL using stdlib. Returns the response body as bytes."""
    headers = {"User-Agent": "refresh_catalog/1.0"}
    if extra_headers:
        headers.update(extra_headers)
    # Truncate URL for display (hide long query strings)
    display_url = url.split("?")[0] if len(url) > 80 else url
    print(f"    GET {display_url}")
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
        print(f"    -> {resp.status} ({len(data)} bytes)")
        return data


def _jira_search(
    jql: str,
    jira_url: str,
    jira_username: str,
    jira_token: str,
    fields: str,
    max_results: int = 1000,
    expand: str | None = None,
    next_page_token: str | None = None,
) -> dict:
    """Single Jira Cloud REST search request. Returns parsed JSON response.

    Uses the /rest/api/3/search/jql endpoint with token-based pagination.
    """
    param_dict: dict[str, str | int] = {
        "jql": jql,
        "maxResults": max_results,
        "fields": fields,
    }
    if expand:
        param_dict["expand"] = expand
    if next_page_token:
        param_dict["nextPageToken"] = next_page_token
    params = urllib.parse.urlencode(param_dict)
    url = f"{jira_url.rstrip('/')}/rest/api/3/search/jql?{params}"
    basic_cred = base64.b64encode(f"{jira_username}:{jira_token}".encode()).decode()
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {basic_cred}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "refresh_catalog/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip

            raw = gzip.decompress(raw)
        return json.loads(raw)


def fetch_jira_csv(
    jql: str, jira_url: str, jira_username: str, jira_token: str
) -> bytes:
    """Paginated Jira Cloud REST search -> CSV bytes.

    Uses token-based pagination via /rest/api/3/search/jql.
    Results are deduped by issue key.
    """
    jira_fields = "summary,status,priority,assignee,reporter,issuetype,created,updated,labels,components"
    max_results = 1000

    print(f"    GET {jira_url.rstrip('/')}/rest/api/3/search/jql (page: 1)")
    data = _jira_search(
        jql,
        jira_url,
        jira_username,
        jira_token,
        jira_fields,
        max_results=max_results,
    )
    issues = data.get("issues", [])
    seen_keys: set[str] = set()
    all_issues: list[dict] = []

    for issue in issues:
        key = issue.get("key", "")
        if key not in seen_keys:
            seen_keys.add(key)
            all_issues.append(issue)

    page = 1
    print(f"    -> {len(issues)} issues ({len(all_issues)} total)")

    while not data.get("isLast", True) and data.get("nextPageToken"):
        page += 1
        print(f"    GET {jira_url.rstrip('/')}/rest/api/3/search/jql (page: {page})")
        data = _jira_search(
            jql,
            jira_url,
            jira_username,
            jira_token,
            jira_fields,
            max_results=max_results,
            next_page_token=data["nextPageToken"],
        )
        issues = data.get("issues", [])

        for issue in issues:
            key = issue.get("key", "")
            if key not in seen_keys:
                seen_keys.add(key)
                all_issues.append(issue)

        print(f"    -> {len(issues)} issues ({len(all_issues)} total)")

    print(f"    Total: {len(all_issues)} unique issues ({page} pages)")

    # Flatten to CSV
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=JIRA_CSV_FIELDS)
    writer.writeheader()
    for issue in all_issues:
        fields = issue.get("fields", {})
        row = {
            "key": issue.get("key", ""),
            "summary": fields.get("summary", ""),
            "status": (fields.get("status") or {}).get("name", ""),
            "priority": (fields.get("priority") or {}).get("name", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", ""),
            "reporter": (fields.get("reporter") or {}).get("displayName", ""),
            "issuetype": (fields.get("issuetype") or {}).get("name", ""),
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
            "labels": ",".join(fields.get("labels", [])),
            "components": ",".join(
                c.get("name", "") for c in fields.get("components", [])
            ),
        }
        writer.writerow(row)

    return buf.getvalue().encode("utf-8")


def file_hash(path: Path) -> str:
    """Return SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(data: bytes) -> str:
    """Return SHA-256 hex digest of in-memory bytes."""
    return hashlib.sha256(data).hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write data to path atomically via temp file + os.replace."""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, str(path))
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def refresh_sources(
    entries: list[dict], aliases_data: dict, dry_run: bool, only: str | None = None
) -> list[dict]:
    """Fetch fresh data for catalog entries that have upstream sources.

    Returns the (possibly modified) entries list with updated filenames/dates.
    If `only` is set, only entries whose filename or source_type contains
    the substring (case-insensitive) are refreshed.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    static_aliases = aliases_data.get("aliases", {})
    dynamic_alias_keys = set(aliases_data.get("dynamic_aliases", {}).keys())

    jira_url = os.environ.get("JIRA_URL", "")
    jira_username = os.environ.get("JIRA_USERNAME", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")

    refreshed = 0
    unchanged = 0
    errors = 0

    for entry in entries:
        source_type = entry.get("source_type", "")
        source_url = entry.get("source_url")
        jql_alias = entry.get("jql_alias")
        filename = entry["filename"]

        # Apply --only filter
        if only:
            pattern = only.lower()
            if pattern not in filename.lower() and pattern not in source_type.lower():
                continue

        # Determine if this entry is refreshable
        is_jira = source_type == "jira" and jql_alias
        is_url = source_url and source_type in ("web-pdf", "csv")
        if not is_jira and not is_url:
            continue

        if dry_run:
            if is_jira:
                print(f"  Would refresh (jira/{jql_alias}): {filename}")
            else:
                print(f"  Would refresh ({source_type}): {filename}")
            continue

        # Fetch content
        try:
            if is_jira:
                if not jira_url or not jira_username or not jira_token:
                    print(
                        f"  ERROR: JIRA_URL, JIRA_USERNAME, and JIRA_API_TOKEN env vars required for: {filename}"
                    )
                    errors += 1
                    continue
                if jql_alias in dynamic_alias_keys:
                    print(f"  SKIP (dynamic alias '{jql_alias}'): {filename}")
                    continue
                if jql_alias not in static_aliases:
                    print(
                        f"  ERROR: jql_alias '{jql_alias}' not found in jql-aliases.json: {filename}"
                    )
                    errors += 1
                    continue
                jql = static_aliases[jql_alias]["jql"]
                new_data = fetch_jira_csv(jql, jira_url, jira_username, jira_token)
            else:
                new_data = fetch_url(source_url)
        except Exception as exc:
            print(f"  ERROR fetching {filename}: {exc}")
            errors += 1
            continue

        # Validate -- refuse to write empty content
        if not new_data:
            print(f"  ERROR: empty response for {filename}, skipping")
            errors += 1
            continue

        # Diff against existing file
        existing_path = DATA_DIR / filename
        if existing_path.exists() and content_hash(new_data) == file_hash(
            existing_path
        ):
            print(f"  Unchanged: {filename}")
            unchanged += 1
            continue

        # Compute new filename with today's date
        match = DATE_PREFIX_RE.match(filename)
        if match:
            name_part = match.group(2)
        else:
            name_part = filename
        new_filename = f"{today}-{name_part}"
        new_path = DATA_DIR / new_filename

        # Back up old file if we'd overwrite the same name
        if new_path.exists() and new_path == existing_path:
            bak_path = new_path.with_suffix(new_path.suffix + ".bak")
            bak_path.write_bytes(new_path.read_bytes())

        # Write new file atomically
        atomic_write_bytes(new_path, new_data)

        # Remove old dated file if different from new path
        if existing_path.exists() and existing_path != new_path:
            existing_path.unlink()

        # Update catalog entry
        entry["filename"] = new_filename
        entry["date"] = today
        print(f"  Refreshed: {filename} -> {new_filename} ({len(new_data)} bytes)")
        refreshed += 1

    print(f"Refresh: {refreshed} updated, {unchanged} unchanged, {errors} errors")
    return entries


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refresh DATA_CATALOG.yaml from the filesystem."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without modifying anything.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch fresh data from upstream sources before cataloging.",
    )
    parser.add_argument(
        "--only",
        metavar="PATTERN",
        help="Only refresh entries whose filename or source_type contains PATTERN (substring match).",
    )
    args = parser.parse_args()

    load_dotenv(DOTENV_PATH)

    # Step 0 (optional): Refresh data from upstream sources
    existing_catalog = parse_catalog(CATALOG_PATH)
    if args.refresh:
        try:
            aliases_data = load_jql_aliases(JQL_ALIASES_PATH)
        except FileNotFoundError as exc:
            print(f"WARNING: {exc} -- skipping Jira sources")
            aliases_data = {"aliases": {}, "dynamic_aliases": {}}

        # Build entry list from catalog for refresh
        refresh_entries = list(existing_catalog.values())
        refresh_sources(
            refresh_entries, aliases_data, dry_run=args.dry_run, only=args.only
        )

        # Re-key catalog after refresh may have changed filenames
        existing_catalog = {e["filename"]: e for e in refresh_entries}

    # Step 1: Rename files to match naming convention
    renames = rename_files(dry_run=args.dry_run)

    if renames:
        print(f"Renames: {len(renames)} file(s)")
    else:
        print("Renames: none needed")

    # Step 2: Re-key existing catalog entries to match new filenames
    # so that manually-entered metadata is preserved across renames.
    for old_name, new_name in renames.items():
        if old_name in existing_catalog:
            entry = existing_catalog.pop(old_name)
            entry["filename"] = new_name
            existing_catalog[new_name] = entry

    # Step 3: Build the catalog from current filesystem state
    # In dry-run mode, simulate the renames so the catalog reflects final state.
    reverse_renames = {v: k for k, v in renames.items()}
    disk_files = sorted(f.name for f in DATA_DIR.iterdir() if is_data_file(f))
    if args.dry_run and renames:
        data_files = sorted(renames.get(f, f) for f in disk_files)
    else:
        data_files = disk_files

    entries = []
    added = []
    removed = []

    for fname in data_files:
        match = DATE_PREFIX_RE.match(fname)
        date = match.group(1) if match else None

        if fname in existing_catalog:
            entry = existing_catalog[fname]
            entry["filename"] = fname
            if date:
                entry["date"] = date
        else:
            entry = {
                "filename": fname,
                "date": date,
                "description": None,
                "source_type": guess_source_type(reverse_renames.get(fname, fname)),
                "source_url": None,
                "jql_alias": None,
                "notes": None,
            }
            added.append(fname)

        entries.append(entry)

    REFRESHABLE_TYPES = {
        "jira",
        "web-pdf",
        "csv",
    }
    for old_name in existing_catalog:
        if old_name not in {e["filename"] for e in entries}:
            old_entry = existing_catalog[old_name]
            # Keep entries with a source_url that can be refreshed -- the file
            # may not exist yet because it hasn't been fetched successfully.
            if (
                old_entry.get("source_url")
                and old_entry.get("source_type") in REFRESHABLE_TYPES
            ):
                entries.append(old_entry)
            else:
                removed.append(old_name)

    has_changes = bool(renames or added or removed)

    # Render new catalog content and compare to existing
    new_content = render_catalog(entries)
    old_content = CATALOG_PATH.read_text() if CATALOG_PATH.exists() else ""
    content_changed = new_content != old_content

    if not args.dry_run and content_changed:
        write_catalog(CATALOG_PATH, new_content)

    # Summary
    print(f"Catalog: {len(entries)} files")
    if added:
        for fname in added:
            print(f"  + {fname}")
    if removed:
        for fname in removed:
            print(f"  - {fname}")
    if not has_changes and not content_changed:
        print("  No changes")
    elif not has_changes and content_changed:
        print("  Catalog reformatted (no file changes)")

    if args.dry_run:
        print("\n(dry run -- no changes made)")


if __name__ == "__main__":
    main()
