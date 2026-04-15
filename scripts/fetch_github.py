#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Fetch GitHub org data into data/github.db.

Builds a separate SQLite database with repos, commits, PRs, issues, releases,
contributors, and code frequency for a specified GitHub org.
Uses `gh api` via subprocess (must be authenticated).

Usage:
    uv run scripts/fetch_github.py --org MY_ORG                    # incremental from _meta
    uv run scripts/fetch_github.py --org MY_ORG --repo my-repo     # single repo
    uv run scripts/fetch_github.py --org MY_ORG --since 2026-03-01
    uv run scripts/fetch_github.py --org MY_ORG --full              # ignore _meta, fetch all
    uv run scripts/fetch_github.py --org MY_ORG --skip-commits      # fast metadata-only refresh
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "github.db"

API_DELAY = 0.5
SLOW_DELAY = 2.0

# Track rate limit state from last API call
_rate_limit_remaining: int | None = None
_rate_limit_reset: float | None = None

# ---------------------------------------------------------------------------
# GitHub API via `gh`
# ---------------------------------------------------------------------------


def gh_api(
    endpoint: str,
    paginate: bool = False,
    jq: str | None = None,
    method: str = "GET",
) -> dict | list | None:
    """Call GitHub API via `gh api`. Returns parsed JSON or None on error."""
    global _rate_limit_remaining, _rate_limit_reset

    # Check rate limit before calling
    if _rate_limit_remaining is not None and _rate_limit_remaining < 3:
        if _rate_limit_reset:
            wait = max(0, _rate_limit_reset - time.time()) + 1
            print(f"  Rate limit nearly exhausted, sleeping {wait:.0f}s...")
            time.sleep(wait)

    delay = API_DELAY
    if _rate_limit_remaining is not None and _rate_limit_remaining < 10:
        delay = SLOW_DELAY

    time.sleep(delay)

    cmd = ["gh", "api"]
    if paginate:
        cmd.append("--paginate")
    if jq:
        cmd.extend(["--jq", jq])
    if method != "GET":
        cmd.extend(["--method", method])
    # Include response headers so we can parse rate limit info
    cmd.extend(["--include", endpoint])

    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"  Timeout on {endpoint}, attempt {attempt + 1}/3")
            time.sleep(2 ** (attempt + 1))
            continue

        # Parse headers and body from --include output
        output = result.stdout
        headers_str = ""
        body_str = output

        # --include puts headers before blank line then body
        if "\r\n\r\n" in output:
            headers_str, body_str = output.split("\r\n\r\n", 1)
        elif "\n\n" in output:
            headers_str, body_str = output.split("\n\n", 1)

        # Parse rate limit headers
        for line in headers_str.splitlines():
            if line.lower().startswith("x-ratelimit-remaining:"):
                try:
                    _rate_limit_remaining = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.lower().startswith("x-ratelimit-reset:"):
                try:
                    _rate_limit_reset = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Check for rate limiting (403)
            if "403" in stderr or "rate limit" in stderr.lower():
                if _rate_limit_reset:
                    wait = max(0, _rate_limit_reset - time.time()) + 1
                    print(f"  Rate limited on {endpoint}, sleeping {wait:.0f}s...")
                    time.sleep(wait)
                    continue
                time.sleep(2 ** (attempt + 1))
                continue
            # 5xx or transient error
            if any(str(c) in stderr for c in range(500, 600)):
                time.sleep(2 ** (attempt + 1))
                continue
            print(f"  gh api error on {endpoint}: {stderr}", file=sys.stderr)
            return None

        body_str = body_str.strip()
        if not body_str:
            return None

        try:
            return json.loads(body_str)
        except json.JSONDecodeError:
            print(f"  JSON decode error on {endpoint}", file=sys.stderr)
            return None

    print(f"  Exhausted retries on {endpoint}", file=sys.stderr)
    return None


def gh_api_simple(endpoint: str, paginate: bool = False) -> dict | list | None:
    """Simpler gh api call without --include (for paginated calls where --include breaks)."""
    global _rate_limit_remaining, _rate_limit_reset

    delay = API_DELAY
    if _rate_limit_remaining is not None and _rate_limit_remaining < 10:
        delay = SLOW_DELAY
    if _rate_limit_remaining is not None and _rate_limit_remaining < 3:
        if _rate_limit_reset:
            wait = max(0, _rate_limit_reset - time.time()) + 1
            print(f"  Rate limit nearly exhausted, sleeping {wait:.0f}s...")
            time.sleep(wait)

    time.sleep(delay)

    cmd = ["gh", "api"]
    if paginate:
        cmd.append("--paginate")
    cmd.append(endpoint)

    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            print(f"  Timeout on {endpoint}, attempt {attempt + 1}/3")
            time.sleep(2 ** (attempt + 1))
            continue

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "403" in stderr or "rate limit" in stderr.lower():
                time.sleep(60)
                continue
            if any(str(c) in stderr for c in range(500, 600)):
                time.sleep(2 ** (attempt + 1))
                continue
            print(f"  gh api error on {endpoint}: {stderr}", file=sys.stderr)
            return None

        body = result.stdout.strip()
        if not body:
            return None

        # Paginated output may be multiple JSON arrays concatenated
        if paginate and body.startswith("["):
            # gh --paginate concatenates arrays as ][
            # or sometimes just newline-separated arrays
            body = body.replace("]\n[", ",").replace("][", ",")

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            print(f"  JSON decode error on {endpoint}", file=sys.stderr)
            return None

    print(f"  Exhausted retries on {endpoint}", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS gh_user (
    user_id    INTEGER PRIMARY KEY,
    login      TEXT NOT NULL UNIQUE,
    name       TEXT,
    avatar_url TEXT,
    html_url   TEXT,
    user_type  TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gh_repo (
    repo_id        INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    full_name      TEXT,
    description    TEXT,
    html_url       TEXT,
    default_branch TEXT,
    is_fork        INTEGER DEFAULT 0,
    is_archived    INTEGER DEFAULT 0,
    is_private     INTEGER DEFAULT 0,
    created_at     TEXT,
    updated_at     TEXT,
    pushed_at      TEXT,
    stars          INTEGER DEFAULT 0,
    forks          INTEGER DEFAULT 0,
    open_issues    INTEGER DEFAULT 0,
    size_kb        INTEGER DEFAULT 0,
    fetched_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gh_repo_topic (
    repo_id INTEGER NOT NULL,
    topic   TEXT NOT NULL,
    PRIMARY KEY (repo_id, topic),
    FOREIGN KEY (repo_id) REFERENCES gh_repo(repo_id)
);

CREATE TABLE IF NOT EXISTS gh_repo_language (
    repo_id  INTEGER NOT NULL,
    language TEXT NOT NULL,
    bytes    INTEGER DEFAULT 0,
    PRIMARY KEY (repo_id, language),
    FOREIGN KEY (repo_id) REFERENCES gh_repo(repo_id)
);

CREATE TABLE IF NOT EXISTS gh_contributor (
    repo_id       INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    contributions INTEGER DEFAULT 0,
    PRIMARY KEY (repo_id, user_id),
    FOREIGN KEY (repo_id) REFERENCES gh_repo(repo_id),
    FOREIGN KEY (user_id) REFERENCES gh_user(user_id)
);

CREATE TABLE IF NOT EXISTS gh_commit (
    repo_id    INTEGER NOT NULL,
    sha        TEXT NOT NULL,
    author_login TEXT,
    author_date  TEXT,
    message      TEXT,
    additions    INTEGER,
    deletions    INTEGER,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (repo_id, sha),
    FOREIGN KEY (repo_id) REFERENCES gh_repo(repo_id)
);

CREATE TABLE IF NOT EXISTS gh_pull_request (
    pr_id       INTEGER PRIMARY KEY,
    repo_id     INTEGER NOT NULL,
    number      INTEGER NOT NULL,
    title       TEXT,
    state       TEXT,
    author_login TEXT,
    created_at  TEXT,
    updated_at  TEXT,
    closed_at   TEXT,
    merged_at   TEXT,
    additions   INTEGER,
    deletions   INTEGER,
    changed_files INTEGER,
    html_url    TEXT,
    fetched_at  TEXT NOT NULL,
    UNIQUE(repo_id, number),
    FOREIGN KEY (repo_id) REFERENCES gh_repo(repo_id)
);

CREATE TABLE IF NOT EXISTS gh_pr_label (
    pr_id INTEGER NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (pr_id, label),
    FOREIGN KEY (pr_id) REFERENCES gh_pull_request(pr_id)
);

CREATE TABLE IF NOT EXISTS gh_pr_assignee (
    pr_id INTEGER NOT NULL,
    login TEXT NOT NULL,
    PRIMARY KEY (pr_id, login),
    FOREIGN KEY (pr_id) REFERENCES gh_pull_request(pr_id)
);

CREATE TABLE IF NOT EXISTS gh_pr_reviewer (
    pr_id INTEGER NOT NULL,
    login TEXT NOT NULL,
    PRIMARY KEY (pr_id, login),
    FOREIGN KEY (pr_id) REFERENCES gh_pull_request(pr_id)
);

CREATE TABLE IF NOT EXISTS gh_pr_review (
    review_id    INTEGER PRIMARY KEY,
    pr_id        INTEGER NOT NULL,
    user_login   TEXT,
    state        TEXT,
    submitted_at TEXT,
    FOREIGN KEY (pr_id) REFERENCES gh_pull_request(pr_id)
);

CREATE TABLE IF NOT EXISTS gh_issue (
    issue_id     INTEGER PRIMARY KEY,
    repo_id      INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    title        TEXT,
    state        TEXT,
    author_login TEXT,
    created_at   TEXT,
    updated_at   TEXT,
    closed_at    TEXT,
    html_url     TEXT,
    fetched_at   TEXT NOT NULL,
    UNIQUE(repo_id, number),
    FOREIGN KEY (repo_id) REFERENCES gh_repo(repo_id)
);

CREATE TABLE IF NOT EXISTS gh_issue_label (
    issue_id INTEGER NOT NULL,
    label    TEXT NOT NULL,
    PRIMARY KEY (issue_id, label),
    FOREIGN KEY (issue_id) REFERENCES gh_issue(issue_id)
);

CREATE TABLE IF NOT EXISTS gh_issue_assignee (
    issue_id INTEGER NOT NULL,
    login    TEXT NOT NULL,
    PRIMARY KEY (issue_id, login),
    FOREIGN KEY (issue_id) REFERENCES gh_issue(issue_id)
);

CREATE TABLE IF NOT EXISTS gh_release (
    release_id  INTEGER PRIMARY KEY,
    repo_id     INTEGER NOT NULL,
    tag_name    TEXT NOT NULL,
    name        TEXT,
    draft       INTEGER DEFAULT 0,
    prerelease  INTEGER DEFAULT 0,
    created_at  TEXT,
    published_at TEXT,
    author_login TEXT,
    html_url    TEXT,
    fetched_at  TEXT NOT NULL,
    UNIQUE(repo_id, tag_name),
    FOREIGN KEY (repo_id) REFERENCES gh_repo(repo_id)
);

CREATE TABLE IF NOT EXISTS gh_release_asset (
    asset_id    INTEGER PRIMARY KEY,
    release_id  INTEGER NOT NULL,
    name        TEXT,
    size_bytes  INTEGER DEFAULT 0,
    download_count INTEGER DEFAULT 0,
    content_type TEXT,
    FOREIGN KEY (release_id) REFERENCES gh_release(release_id)
);

CREATE TABLE IF NOT EXISTS gh_code_frequency (
    repo_id  INTEGER NOT NULL,
    week_ts  INTEGER NOT NULL,
    additions INTEGER DEFAULT 0,
    deletions INTEGER DEFAULT 0,
    PRIMARY KEY (repo_id, week_ts),
    FOREIGN KEY (repo_id) REFERENCES gh_repo(repo_id)
);

CREATE TABLE IF NOT EXISTS _meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Views

CREATE VIEW IF NOT EXISTS v_gh_repo_summary AS
SELECT
    r.repo_id, r.name, r.description, r.html_url, r.default_branch,
    r.is_fork, r.is_archived, r.stars, r.forks, r.open_issues, r.size_kb,
    r.created_at, r.updated_at, r.pushed_at,
    (SELECT COUNT(*) FROM gh_commit c WHERE c.repo_id = r.repo_id) AS commit_count,
    (SELECT COUNT(*) FROM gh_pull_request p WHERE p.repo_id = r.repo_id) AS pr_count,
    (SELECT COUNT(*) FROM gh_pull_request p WHERE p.repo_id = r.repo_id AND p.merged_at IS NOT NULL) AS merged_pr_count,
    (SELECT COUNT(*) FROM gh_issue i WHERE i.repo_id = r.repo_id) AS issue_count,
    (SELECT COUNT(DISTINCT c2.user_id) FROM gh_contributor c2 WHERE c2.repo_id = r.repo_id) AS contributor_count,
    (SELECT GROUP_CONCAT(t.topic, ', ') FROM gh_repo_topic t WHERE t.repo_id = r.repo_id) AS topics,
    (SELECT SUM(l.bytes) FROM gh_repo_language l WHERE l.repo_id = r.repo_id) AS total_language_bytes
FROM gh_repo r;

CREATE VIEW IF NOT EXISTS v_gh_org_stats AS
SELECT
    (SELECT COUNT(*) FROM gh_repo) AS total_repos,
    (SELECT COUNT(*) FROM gh_repo WHERE is_archived = 0) AS active_repos,
    (SELECT COUNT(*) FROM gh_commit) AS total_commits,
    (SELECT COUNT(*) FROM gh_pull_request) AS total_prs,
    (SELECT COUNT(*) FROM gh_pull_request WHERE merged_at IS NOT NULL) AS merged_prs,
    (SELECT COUNT(*) FROM gh_issue) AS total_issues,
    (SELECT COUNT(*) FROM gh_release) AS total_releases,
    (SELECT COUNT(DISTINCT login) FROM gh_user) AS total_contributors,
    (SELECT SUM(bytes) FROM gh_repo_language) AS total_language_bytes,
    (SELECT SUM(additions) FROM gh_code_frequency) AS total_additions,
    (SELECT SUM(ABS(deletions)) FROM gh_code_frequency) AS total_deletions;

CREATE VIEW IF NOT EXISTS v_gh_contributor_summary AS
SELECT
    u.login, u.name, u.user_id,
    COUNT(DISTINCT c.repo_id) AS repos_contributed,
    SUM(c.contributions) AS total_contributions,
    (SELECT COUNT(*) FROM gh_pull_request p WHERE p.author_login = u.login) AS prs_authored,
    (SELECT COUNT(*) FROM gh_pull_request p WHERE p.author_login = u.login AND p.merged_at IS NOT NULL) AS prs_merged,
    (SELECT COUNT(*) FROM gh_pr_review rv WHERE rv.user_login = u.login) AS reviews_given,
    (SELECT COUNT(*) FROM gh_commit cm WHERE cm.author_login = u.login) AS commits_authored
FROM gh_user u
JOIN gh_contributor c ON u.user_id = c.user_id
GROUP BY u.user_id;

-- Indexes

CREATE INDEX IF NOT EXISTS idx_gh_commit_repo_date ON gh_commit(repo_id, author_date DESC);
CREATE INDEX IF NOT EXISTS idx_gh_commit_author ON gh_commit(author_login);
CREATE INDEX IF NOT EXISTS idx_gh_commit_message ON gh_commit(message);
CREATE INDEX IF NOT EXISTS idx_gh_pr_repo_state ON gh_pull_request(repo_id, state);
CREATE INDEX IF NOT EXISTS idx_gh_pr_author ON gh_pull_request(author_login);
CREATE INDEX IF NOT EXISTS idx_gh_pr_merged ON gh_pull_request(merged_at) WHERE merged_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_gh_pr_updated ON gh_pull_request(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_gh_pr_created ON gh_pull_request(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gh_issue_repo_state ON gh_issue(repo_id, state);
CREATE INDEX IF NOT EXISTS idx_gh_issue_author ON gh_issue(author_login);
CREATE INDEX IF NOT EXISTS idx_gh_issue_updated ON gh_issue(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_gh_release_repo ON gh_release(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_code_freq_repo ON gh_code_frequency(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_contributor_repo ON gh_contributor(repo_id);
CREATE INDEX IF NOT EXISTS idx_gh_contributor_user ON gh_contributor(user_id);
CREATE INDEX IF NOT EXISTS idx_gh_pr_review_pr ON gh_pr_review(pr_id);
CREATE INDEX IF NOT EXISTS idx_gh_pr_review_user ON gh_pr_review(user_login);
CREATE INDEX IF NOT EXISTS idx_gh_user_login ON gh_user(login);
"""


def init_db() -> sqlite3.Connection:
    """Create github.db and apply schema."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_SQL)
    return conn


# ---------------------------------------------------------------------------
# Fetch functions
# ---------------------------------------------------------------------------


def upsert_user(conn: sqlite3.Connection, user: dict, now: str) -> int | None:
    """Upsert a GitHub user. Returns user_id or None."""
    if not user or not user.get("id"):
        return None
    conn.execute(
        """INSERT INTO gh_user (user_id, login, name, avatar_url, html_url, user_type, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               login=excluded.login, name=excluded.name,
               avatar_url=excluded.avatar_url, html_url=excluded.html_url,
               user_type=excluded.user_type, fetched_at=excluded.fetched_at""",
        (
            user["id"],
            user.get("login", ""),
            user.get("name"),
            user.get("avatar_url"),
            user.get("html_url"),
            user.get("type", "User"),
            now,
        ),
    )
    return user["id"]


def fetch_repos(conn: sqlite3.Connection, org: str, now: str) -> list[dict]:
    """Fetch all repos in the org."""
    print(f"\nFetching repos for {org}...")
    repos = gh_api_simple(f"/orgs/{org}/repos?per_page=100&type=all", paginate=True)
    if not repos:
        print("  No repos found or API error")
        return []

    for r in repos:
        conn.execute(
            """INSERT INTO gh_repo (repo_id, name, full_name, description, html_url,
                   default_branch, is_fork, is_archived, is_private,
                   created_at, updated_at, pushed_at, stars, forks,
                   open_issues, size_kb, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_id) DO UPDATE SET
                   name=excluded.name, full_name=excluded.full_name,
                   description=excluded.description, html_url=excluded.html_url,
                   default_branch=excluded.default_branch,
                   is_fork=excluded.is_fork, is_archived=excluded.is_archived,
                   is_private=excluded.is_private,
                   updated_at=excluded.updated_at, pushed_at=excluded.pushed_at,
                   stars=excluded.stars, forks=excluded.forks,
                   open_issues=excluded.open_issues, size_kb=excluded.size_kb,
                   fetched_at=excluded.fetched_at""",
            (
                r["id"],
                r["name"],
                r.get("full_name", ""),
                r.get("description"),
                r.get("html_url"),
                r.get("default_branch", "main"),
                1 if r.get("fork") else 0,
                1 if r.get("archived") else 0,
                1 if r.get("private") else 0,
                r.get("created_at"),
                r.get("updated_at"),
                r.get("pushed_at"),
                r.get("stargazers_count", 0),
                r.get("forks_count", 0),
                r.get("open_issues_count", 0),
                r.get("size", 0),
                now,
            ),
        )
        # Topics
        conn.execute("DELETE FROM gh_repo_topic WHERE repo_id = ?", (r["id"],))
        for topic in r.get("topics", []):
            conn.execute(
                "INSERT OR IGNORE INTO gh_repo_topic (repo_id, topic) VALUES (?, ?)",
                (r["id"], topic),
            )

    print(f"  {len(repos)} repos")
    return repos


def fetch_languages(conn: sqlite3.Connection, repo_id: int, name: str, org: str, now: str) -> None:
    """Fetch language breakdown for a repo."""
    langs = gh_api_simple(f"/repos/{org}/{name}/languages")
    if not langs or not isinstance(langs, dict):
        return
    conn.execute("DELETE FROM gh_repo_language WHERE repo_id = ?", (repo_id,))
    for lang, byte_count in langs.items():
        conn.execute(
            "INSERT OR REPLACE INTO gh_repo_language (repo_id, language, bytes) VALUES (?, ?, ?)",
            (repo_id, lang, byte_count),
        )


def fetch_contributors(conn: sqlite3.Connection, repo_id: int, name: str, org: str, now: str) -> None:
    """Fetch contributors for a repo."""
    contribs = gh_api_simple(f"/repos/{org}/{name}/contributors?per_page=100", paginate=True)
    if not contribs or not isinstance(contribs, list):
        return
    for c in contribs:
        if not c.get("id"):
            continue
        upsert_user(conn, c, now)
        conn.execute(
            """INSERT INTO gh_contributor (repo_id, user_id, contributions)
               VALUES (?, ?, ?)
               ON CONFLICT(repo_id, user_id) DO UPDATE SET
                   contributions=excluded.contributions""",
            (repo_id, c["id"], c.get("contributions", 0)),
        )


def fetch_commits(
    conn: sqlite3.Connection,
    repo_id: int,
    name: str,
    org: str,
    now: str,
    since: str | None = None,
) -> None:
    """Fetch commits for a repo."""
    endpoint = f"/repos/{org}/{name}/commits?per_page=100"
    if since:
        endpoint += f"&since={since}"

    commits = gh_api_simple(endpoint, paginate=True)
    if not commits or not isinstance(commits, list):
        return

    print(f"    {len(commits)} commits")
    for c in commits:
        author = c.get("author") or {}
        commit_data = c.get("commit", {})
        author_info = commit_data.get("author", {})
        conn.execute(
            """INSERT INTO gh_commit (repo_id, sha, author_login, author_date, message,
                   additions, deletions, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(repo_id, sha) DO UPDATE SET
                   author_login=excluded.author_login, message=excluded.message,
                   fetched_at=excluded.fetched_at""",
            (
                repo_id,
                c["sha"],
                author.get("login"),
                author_info.get("date"),
                commit_data.get("message", "")[:500],
                None,  # additions not available in list endpoint
                None,
                now,
            ),
        )


def fetch_prs(
    conn: sqlite3.Connection,
    repo_id: int,
    name: str,
    org: str,
    now: str,
    since: str | None = None,
) -> None:
    """Fetch pull requests for a repo."""
    endpoint = f"/repos/{org}/{name}/pulls?per_page=100&state=all&sort=updated&direction=desc"
    prs = gh_api_simple(endpoint, paginate=True)
    if not prs or not isinstance(prs, list):
        return

    fetched = 0
    for pr in prs:
        # Incremental: stop if PR is older than since
        if since and pr.get("updated_at", "") < since:
            break

        pr_id = pr["id"]
        conn.execute(
            """INSERT INTO gh_pull_request (pr_id, repo_id, number, title, state,
                   author_login, created_at, updated_at, closed_at, merged_at,
                   additions, deletions, changed_files, html_url, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(pr_id) DO UPDATE SET
                   title=excluded.title, state=excluded.state,
                   updated_at=excluded.updated_at, closed_at=excluded.closed_at,
                   merged_at=excluded.merged_at, additions=excluded.additions,
                   deletions=excluded.deletions, changed_files=excluded.changed_files,
                   fetched_at=excluded.fetched_at""",
            (
                pr_id,
                repo_id,
                pr["number"],
                pr.get("title"),
                pr.get("state"),
                (pr.get("user") or {}).get("login"),
                pr.get("created_at"),
                pr.get("updated_at"),
                pr.get("closed_at"),
                pr.get("merged_at"),
                pr.get("additions"),
                pr.get("deletions"),
                pr.get("changed_files"),
                pr.get("html_url"),
                now,
            ),
        )

        # Labels
        conn.execute("DELETE FROM gh_pr_label WHERE pr_id = ?", (pr_id,))
        for lbl in pr.get("labels", []):
            conn.execute(
                "INSERT OR IGNORE INTO gh_pr_label (pr_id, label) VALUES (?, ?)",
                (pr_id, lbl.get("name", "")),
            )

        # Assignees
        conn.execute("DELETE FROM gh_pr_assignee WHERE pr_id = ?", (pr_id,))
        for a in pr.get("assignees", []):
            conn.execute(
                "INSERT OR IGNORE INTO gh_pr_assignee (pr_id, login) VALUES (?, ?)",
                (pr_id, a.get("login", "")),
            )

        # Requested reviewers
        conn.execute("DELETE FROM gh_pr_reviewer WHERE pr_id = ?", (pr_id,))
        for rv in pr.get("requested_reviewers", []):
            conn.execute(
                "INSERT OR IGNORE INTO gh_pr_reviewer (pr_id, login) VALUES (?, ?)",
                (pr_id, rv.get("login", "")),
            )
        fetched += 1

    print(f"    {fetched} PRs")


def fetch_pr_reviews(conn: sqlite3.Connection, repo_id: int, name: str, org: str, now: str) -> None:
    """Fetch reviews for all PRs in a repo that don't have reviews yet."""
    # Get PRs that have no reviews stored
    prs = conn.execute(
        """SELECT pr_id, number FROM gh_pull_request
           WHERE repo_id = ? AND pr_id NOT IN (SELECT DISTINCT pr_id FROM gh_pr_review)""",
        (repo_id,),
    ).fetchall()
    if not prs:
        return

    review_count = 0
    for pr in prs:
        reviews = gh_api_simple(f"/repos/{org}/{name}/pulls/{pr['number']}/reviews?per_page=100")
        if not reviews or not isinstance(reviews, list):
            continue
        for rv in reviews:
            conn.execute(
                """INSERT OR REPLACE INTO gh_pr_review
                       (review_id, pr_id, user_login, state, submitted_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    rv["id"],
                    pr["pr_id"],
                    (rv.get("user") or {}).get("login"),
                    rv.get("state"),
                    rv.get("submitted_at"),
                ),
            )
            review_count += 1
    if review_count:
        print(f"    {review_count} reviews")


def fetch_issues(
    conn: sqlite3.Connection,
    repo_id: int,
    name: str,
    org: str,
    now: str,
    since: str | None = None,
) -> None:
    """Fetch issues (excluding PRs) for a repo."""
    endpoint = f"/repos/{org}/{name}/issues?per_page=100&state=all&sort=updated&direction=desc"
    if since:
        endpoint += f"&since={since}"

    issues = gh_api_simple(endpoint, paginate=True)
    if not issues or not isinstance(issues, list):
        return

    fetched = 0
    for issue in issues:
        # Skip pull requests (GitHub includes them in issues endpoint)
        if "pull_request" in issue:
            continue

        issue_id = issue["id"]
        conn.execute(
            """INSERT INTO gh_issue (issue_id, repo_id, number, title, state,
                   author_login, created_at, updated_at, closed_at, html_url, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(issue_id) DO UPDATE SET
                   title=excluded.title, state=excluded.state,
                   updated_at=excluded.updated_at, closed_at=excluded.closed_at,
                   fetched_at=excluded.fetched_at""",
            (
                issue_id,
                repo_id,
                issue["number"],
                issue.get("title"),
                issue.get("state"),
                (issue.get("user") or {}).get("login"),
                issue.get("created_at"),
                issue.get("updated_at"),
                issue.get("closed_at"),
                issue.get("html_url"),
                now,
            ),
        )

        # Labels
        conn.execute("DELETE FROM gh_issue_label WHERE issue_id = ?", (issue_id,))
        for lbl in issue.get("labels", []):
            conn.execute(
                "INSERT OR IGNORE INTO gh_issue_label (issue_id, label) VALUES (?, ?)",
                (issue_id, lbl.get("name", "")),
            )

        # Assignees
        conn.execute("DELETE FROM gh_issue_assignee WHERE issue_id = ?", (issue_id,))
        for a in issue.get("assignees", []):
            conn.execute(
                "INSERT OR IGNORE INTO gh_issue_assignee (issue_id, login) VALUES (?, ?)",
                (issue_id, a.get("login", "")),
            )
        fetched += 1

    print(f"    {fetched} issues")


def fetch_releases(conn: sqlite3.Connection, repo_id: int, name: str, org: str, now: str) -> None:
    """Fetch releases for a repo."""
    releases = gh_api_simple(f"/repos/{org}/{name}/releases?per_page=100")
    if not releases or not isinstance(releases, list):
        return

    for rel in releases:
        conn.execute(
            """INSERT INTO gh_release (release_id, repo_id, tag_name, name, draft,
                   prerelease, created_at, published_at, author_login, html_url, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(release_id) DO UPDATE SET
                   tag_name=excluded.tag_name, name=excluded.name,
                   draft=excluded.draft, prerelease=excluded.prerelease,
                   published_at=excluded.published_at, fetched_at=excluded.fetched_at""",
            (
                rel["id"],
                repo_id,
                rel["tag_name"],
                rel.get("name"),
                1 if rel.get("draft") else 0,
                1 if rel.get("prerelease") else 0,
                rel.get("created_at"),
                rel.get("published_at"),
                (rel.get("author") or {}).get("login"),
                rel.get("html_url"),
                now,
            ),
        )

        # Assets
        for asset in rel.get("assets", []):
            conn.execute(
                """INSERT OR REPLACE INTO gh_release_asset
                       (asset_id, release_id, name, size_bytes, download_count, content_type)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    asset["id"],
                    rel["id"],
                    asset.get("name"),
                    asset.get("size", 0),
                    asset.get("download_count", 0),
                    asset.get("content_type"),
                ),
            )

    if releases:
        print(f"    {len(releases)} releases")


def fetch_code_frequency(conn: sqlite3.Connection, repo_id: int, name: str, org: str, now: str) -> None:
    """Fetch weekly code frequency stats for a repo."""
    for attempt in range(5):
        data = gh_api_simple(f"/repos/{org}/{name}/stats/code_frequency")
        if data is None:
            # 202 means GitHub is computing; retry
            if attempt < 4:
                time.sleep(10)
                continue
            return
        if isinstance(data, list):
            break
    else:
        return

    for week in data:
        if len(week) >= 3:
            conn.execute(
                """INSERT OR REPLACE INTO gh_code_frequency
                       (repo_id, week_ts, additions, deletions)
                   VALUES (?, ?, ?, ?)""",
                (repo_id, week[0], week[1], week[2]),
            )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def process_repo(
    conn: sqlite3.Connection,
    repo: dict,
    org: str,
    now: str,
    since: str | None = None,
    skip_commits: bool = False,
    full: bool = False,
) -> list[str]:
    """Process a single repo. Returns list of skipped entity types."""
    repo_id = repo["id"] if isinstance(repo, dict) else repo[0]
    name = repo["name"] if isinstance(repo, dict) else repo[1]
    print(f"\n--- {name} ---")

    # Determine incremental since for this repo
    repo_since = since
    if not full and not since:
        row = conn.execute("SELECT value FROM _meta WHERE key = ?", (f"last_fetch_{name}",)).fetchone()
        if row:
            repo_since = row[0]
            print(f"  Incremental since {repo_since}")

    skipped = []
    entity_fetchers = [
        ("languages", lambda: fetch_languages(conn, repo_id, name, org, now)),
        ("contributors", lambda: fetch_contributors(conn, repo_id, name, org, now)),
        ("releases", lambda: fetch_releases(conn, repo_id, name, org, now)),
        ("code_frequency", lambda: fetch_code_frequency(conn, repo_id, name, org, now)),
        ("prs", lambda: fetch_prs(conn, repo_id, name, org, now, repo_since)),
        ("pr_reviews", lambda: fetch_pr_reviews(conn, repo_id, name, org, now)),
        ("issues", lambda: fetch_issues(conn, repo_id, name, org, now, repo_since)),
    ]

    if not skip_commits:
        entity_fetchers.insert(
            0,
            ("commits", lambda: fetch_commits(conn, repo_id, name, org, now, repo_since)),
        )

    for entity_name, fetcher in entity_fetchers:
        try:
            fetcher()
        except Exception as e:
            print(f"  ERROR fetching {entity_name}: {e}", file=sys.stderr)
            skipped.append(f"{name}/{entity_name}")

    # Update _meta timestamp for this repo
    conn.execute(
        "INSERT INTO _meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"last_fetch_{name}", now),
    )

    # Commit after each repo
    conn.commit()
    return skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch GitHub org data into github.db")
    parser.add_argument("--repo", help="Fetch a single repo by name")
    parser.add_argument("--since", help="Fetch from date (ISO 8601, e.g. 2026-03-01)")
    parser.add_argument("--full", action="store_true", help="Ignore _meta, fetch all history")
    parser.add_argument(
        "--skip-commits",
        action="store_true",
        help="Skip commit fetching (faster metadata refresh)",
    )
    parser.add_argument("--org", required=True, help="GitHub org to fetch data from")
    args = parser.parse_args()

    conn = init_db()
    now = datetime.now(timezone.utc).isoformat()
    all_skipped: list[str] = []

    try:
        if args.repo:
            # Fetch single repo
            repo_data = gh_api_simple(f"/repos/{args.org}/{args.repo}")
            if not repo_data:
                print(f"ERROR: Repo {args.org}/{args.repo} not found", file=sys.stderr)
                sys.exit(1)
            # Upsert the repo first
            repos = fetch_repos(conn, args.org, now)
            target = None
            for r in repos:
                if r["name"] == args.repo:
                    target = r
                    break
            if not target:
                target = repo_data
            skipped = process_repo(conn, target, args.org, now, args.since, args.skip_commits, args.full)
            all_skipped.extend(skipped)
        else:
            # Fetch all repos
            repos = fetch_repos(conn, args.org, now)
            conn.commit()
            for repo in repos:
                try:
                    skipped = process_repo(conn, repo, args.org, now, args.since, args.skip_commits, args.full)
                    all_skipped.extend(skipped)
                except KeyboardInterrupt:
                    print("\nInterrupted — committing pending data...")
                    conn.commit()
                    raise
                except Exception as e:
                    print(f"  ERROR processing {repo['name']}: {e}", file=sys.stderr)
                    all_skipped.append(f"{repo['name']}/*")
                    conn.commit()

    except KeyboardInterrupt:
        print("\nInterrupted — saving progress...")
        conn.commit()
    finally:
        # Post-build
        print("\n--- Post-build ---")
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"  integrity_check: {result}")
        conn.execute("ANALYZE")
        print("  ANALYZE: done")

        conn.execute(
            "INSERT INTO _meta (key, value) VALUES ('last_build', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        conn.close()

    # Summary
    print(f"\nGitHub DB written to {DB_PATH}")
    if all_skipped:
        print(f"\nSkipped due to errors ({len(all_skipped)}):")
        for s in all_skipped:
            print(f"  - {s}")
    else:
        print("No errors.")


if __name__ == "__main__":
    main()
