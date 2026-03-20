#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=1.9",
#   "pydantic>=2.0",
# ]
# ///
"""GPS MCP server — read-only caching tier for org and engineering data.

Gives agents and humans sub-millisecond access to people, teams, issues,
features, releases, and governance documents via MCP tools. No auth required.

Run: uv run mcp_server.py              # stdio (Claude Code / ACP)
     uv run mcp_server.py --http       # HTTP on :8000 (shared deployment)
"""

import argparse
import json
import os
import re
import sqlite3
from datetime import date, datetime
from difflib import get_close_matches
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "gps.db"
CATALOG_PATH = DATA_DIR / "DATA_CATALOG.yaml"
VERSION_PATH = REPO_ROOT / "VERSION"
VERSION = VERSION_PATH.read_text().strip() if VERSION_PATH.exists() else "unknown"
MAX_QUERY_ROWS = 200

mcp = FastMCP(
    "gps",
    instructions=(
        "GPS is a read-only caching tier for org and engineering data. "
        "Query people, teams, issues, features, release schedules, "
        "component mappings, scrum team boards, and governance documents with sub-ms latency. "
        "Use list_scrum_team_boards for scrum team staffing data (FTE counts by role). "
        "Use list_documents/get_document/get_document_section for governance docs. "
        "All data is read-only."
    ),
)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok", "version": VERSION})


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is not None:
        return _conn
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    _conn = conn
    return conn


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def _normalize_release(release: str) -> tuple[str, str]:
    """Extract (product, major.minor) from release strings in various formats.

    Examples:
        'acmeproduct-1.0'      -> ('acmeproduct', '1.0')
        'acmeproduct-1.1'      -> ('acmeproduct', '1.1')
        'acmeproduct-2.0 EA-1' -> ('acmeproduct', '2.0')
        'myproduct-2.25.2'     -> ('myproduct', '2.25')
    """
    s = release.strip().lower().replace(" ", "")
    # Split on first hyphen to get product and version
    if "-" in s:
        parts = s.split("-", 1)
        product = parts[0]
        ver = parts[1]
    else:
        product = s
        ver = ""
    # Strip EA suffix and patch version — keep only major.minor
    ver = re.sub(r"\s*ea.*$", "", ver, flags=re.IGNORECASE)
    match = re.match(r"(\d+\.\d+)", ver)
    major_minor = match.group(1) if match else ver
    return product, major_minor


def _milestone_release_key(product: str, version: str) -> tuple[str, str]:
    """Extract (product, major.minor) from milestone product+version.

    Examples:
        ('AcmeProduct', '1.0.0')   -> ('acmeproduct', '1.0')
        ('AcmeProduct', '2.0 EA1') -> ('acmeproduct', '2.0')
        ('My Product', '3.3.1')    -> ('myproduct', '3.3')
    """
    product_key = product.lower().replace(" ", "")
    ver = re.sub(r"\s*ea.*$", "", version, flags=re.IGNORECASE)
    match = re.match(r"(\d+\.\d+)", ver)
    major_minor = match.group(1) if match else ver
    return product_key, major_minor


def _parse_date(val: str, reference_year: int | None = None) -> date | None:
    """Parse dates in various formats found in the DB.

    Handles: 'YYYY-MM-DD', 'Mon-DD' (e.g. 'Apr-10'), 'Mon DD'.
    For formats without a year, uses reference_year (default: current year).
    """
    if not val or not val.strip():
        return None
    val = val.strip()
    # ISO format
    try:
        return date.fromisoformat(val)
    except ValueError:
        pass
    # Mon-DD or Mon DD
    year = reference_year or date.today().year
    for fmt in ("%b-%d", "%b %d"):
        try:
            parsed = datetime.strptime(val, fmt).replace(year=year)
            return parsed.date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@mcp.resource(
    "gps://schema",
    name="Database Schema",
    description="DDL and row counts for all tables and views in gps.db",
)
def schema_resource() -> str:
    conn = _get_conn()
    lines = []
    # Tables with DDL and row counts
    tables = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    lines.append("-- TABLES\n")
    for t in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM [{t['name']}]").fetchone()[0]
        lines.append(f"-- {t['name']}: {count:,} rows")
        lines.append(t["sql"] + ";\n")

    # Views with DDL
    views = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name").fetchall()
    if views:
        lines.append("\n-- VIEWS\n")
        for v in views:
            lines.append(v["sql"] + ";\n")

    return "\n".join(lines)


@mcp.resource(
    "gps://catalog",
    name="Data Catalog",
    description="Source file inventory with descriptions, dates, and provenance",
)
def catalog_resource() -> str:
    if CATALOG_PATH.exists():
        return CATALOG_PATH.read_text()
    return "DATA_CATALOG.yaml not found."


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def lookup_person(name: str | None = None, uid: str | None = None, email: str | None = None) -> str:
    """Find people in the org by name, user ID, or email.

    Searches are case-insensitive and partial-match. Provide at least one parameter.
    Returns full person detail including org, components, teams, and specialty.
    """
    if not any([name, uid, email]):
        return json.dumps({"error": "Provide at least one of: name, uid, email"})

    conditions, params = [], []
    if name:
        conditions.append("p.name LIKE ?")
        params.append(f"%{name}%")
    if uid:
        conditions.append("p.user_id LIKE ?")
        params.append(f"%{uid}%")
    if email:
        conditions.append("p.email LIKE ?")
        params.append(f"%{email}%")

    where = " AND ".join(conditions)

    conn = _get_conn()
    sql = f"""
    SELECT
        p.person_id, p.name, p.user_id, p.manager, p.manager_uid,
        o.org_name, o.org_key, s.specialty_name AS specialty,
        p.job_title, p.email, p.location, p.status, p.source, p.last_modified,
        GROUP_CONCAT(DISTINCT jc.component_name) AS components,
        GROUP_CONCAT(DISTINCT mt.miro_team_name) AS miro_teams,
        GROUP_CONCAT(DISTINCT st.team_name) AS scrum_teams
    FROM person p
    LEFT JOIN org o ON p.org_id = o.org_id
    LEFT JOIN specialty s ON p.specialty_id = s.specialty_id
    LEFT JOIN person_component pc ON p.person_id = pc.person_id
    LEFT JOIN jira_component jc ON pc.component_id = jc.component_id
    LEFT JOIN person_miro_team pmt ON p.person_id = pmt.person_id
    LEFT JOIN miro_team mt ON pmt.miro_team_id = mt.miro_team_id
    LEFT JOIN person_scrum_team pst ON p.person_id = pst.person_id
    LEFT JOIN scrum_team st ON pst.team_id = st.team_id
    WHERE {where}
    GROUP BY p.person_id
    ORDER BY p.name
    LIMIT 50
    """
    rows = conn.execute(sql, params).fetchall()
    return json.dumps({"results": _rows_to_dicts(rows), "count": len(rows)}, default=str)


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def list_team_members(team_name: str) -> str:
    """List all members of a scrum team with their roles and details.

    Fuzzy-matches team name. Returns team info (PM, eng lead) plus full member roster.
    """
    conn = _get_conn()
    # Find matching team(s)
    teams = conn.execute(
        "SELECT team_id, team_name, pm, eng_lead FROM scrum_team WHERE team_name LIKE ?",
        (f"%{team_name}%",),
    ).fetchall()

    if not teams:
        return json.dumps(
            {
                "error": f"No team matching '{team_name}'",
                "hint": "Use lookup to find available team names",
            }
        )

    results = []
    for team in teams:
        members = conn.execute(
            """
            SELECT p.name, p.user_id, p.job_title, p.email, p.location,
                   o.org_name, s.specialty_name AS specialty,
                   GROUP_CONCAT(DISTINCT jc.component_name) AS components
            FROM person_scrum_team pst
            JOIN person p ON pst.person_id = p.person_id
            LEFT JOIN org o ON p.org_id = o.org_id
            LEFT JOIN specialty s ON p.specialty_id = s.specialty_id
            LEFT JOIN person_component pc ON p.person_id = pc.person_id
            LEFT JOIN jira_component jc ON pc.component_id = jc.component_id
            WHERE pst.team_id = ?
            GROUP BY p.person_id
            ORDER BY p.name
            """,
            (team["team_id"],),
        ).fetchall()
        results.append(
            {
                "team_name": team["team_name"],
                "pm": team["pm"],
                "eng_lead": team["eng_lead"],
                "headcount": len(members),
                "members": _rows_to_dicts(members),
            }
        )

    return json.dumps({"teams": results}, default=str)


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def list_scrum_team_boards(organization: str | None = None) -> str:
    """List scrum team boards with staffing breakdown by role.

    Optionally filter by organization (fuzzy match). Returns team name,
    Jira board URL, PM, and non-zero FTE counts per role.
    """
    conn = _get_conn()
    query = (
        "SELECT organization, scrum_team_name, jira_board_url, pm, "
        "agilist, architects, bff, backend_engineer, devops, manager, "
        "operations_manager, qe, staff_engineers, ui, total_staff "
        "FROM scrum_team_board"
    )
    if organization:
        query += " WHERE organization LIKE ?"
        rows = conn.execute(query + " ORDER BY total_staff DESC", (f"%{organization}%",)).fetchall()
    else:
        rows = conn.execute(query + " ORDER BY total_staff DESC").fetchall()
    role_cols = [
        "agilist",
        "architects",
        "bff",
        "backend_engineer",
        "devops",
        "manager",
        "operations_manager",
        "qe",
        "staff_engineers",
        "ui",
    ]
    teams = []
    for row in rows:
        d = dict(row)
        roles = {k: d.pop(k) for k in role_cols if d.get(k)}
        d["roles"] = roles
        teams.append(d)
    return json.dumps({"teams": teams, "count": len(teams)}, default=str)


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def search_issues(
    status: str | None = None,
    priority: str | None = None,
    assignee: str | None = None,
    component: str | None = None,
    label: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> str:
    """Search issues with optional filters. All filters are case-insensitive partial match.

    Provide at least one filter. Use keyword to search summary text.
    Returns up to `limit` issues (default 50, max 200).
    """
    if not any([status, priority, assignee, component, label, keyword]):
        return json.dumps({"error": "Provide at least one filter"})

    limit = min(limit, MAX_QUERY_ROWS)
    conditions, params = [], []

    if status:
        conditions.append("ji.status LIKE ?")
        params.append(f"%{status}%")
    if priority:
        conditions.append("ji.priority LIKE ?")
        params.append(f"%{priority}%")
    if assignee:
        conditions.append("ji.assignee LIKE ?")
        params.append(f"%{assignee}%")
    if keyword:
        conditions.append("ji.summary LIKE ?")
        params.append(f"%{keyword}%")
    if component:
        conditions.append(
            "EXISTS (SELECT 1 FROM issue_component ic WHERE ic.issue_id = ji.issue_id AND ic.component_name LIKE ?)"
        )
        params.append(f"%{component}%")
    if label:
        conditions.append("EXISTS (SELECT 1 FROM issue_label il WHERE il.issue_id = ji.issue_id AND il.label LIKE ?)")
        params.append(f"%{label}%")

    where = " AND ".join(conditions)

    conn = _get_conn()
    sql = f"""
    SELECT ji.key, ji.summary, ji.status, ji.priority,
           ji.assignee, ji.reporter, ji.issue_type,
           ji.created, ji.updated
    FROM jira_issue ji
    WHERE {where}
    ORDER BY ji.updated DESC
    LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return json.dumps({"issues": _rows_to_dicts(rows), "count": len(rows)}, default=str)


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def get_feature_status(issue_key: str | None = None, title: str | None = None) -> str:
    """Get feature details including progress, RICE score, releases, components, and teams.

    Search by exact issue key (e.g. "PROJ-1234") or fuzzy title match.
    """
    if not issue_key and not title:
        return json.dumps({"error": "Provide issue_key or title"})

    conn = _get_conn()
    if issue_key:
        features = conn.execute("SELECT * FROM feature WHERE issue_key = ?", (issue_key,)).fetchall()
    else:
        features = conn.execute("SELECT * FROM feature WHERE title LIKE ? LIMIT 20", (f"%{title}%",)).fetchall()

    if not features:
        return json.dumps({"error": f"No feature found for {'key=' + issue_key if issue_key else 'title=' + title}"})

    # Batch-load junction tables to avoid N+1 queries
    fids = [f["feature_id"] for f in features]
    ph = ",".join("?" * len(fids))
    rel_rows = conn.execute(f"SELECT feature_id, release FROM feature_release WHERE feature_id IN ({ph})", fids)
    comp_rows = conn.execute(f"SELECT feature_id, component FROM feature_component WHERE feature_id IN ({ph})", fids)
    team_rows = conn.execute(f"SELECT feature_id, team FROM feature_team WHERE feature_id IN ({ph})", fids)

    rels_by_fid: dict[int, list[str]] = {}
    for r in rel_rows:
        rels_by_fid.setdefault(r["feature_id"], []).append(r["release"])
    comps_by_fid: dict[int, list[str]] = {}
    for r in comp_rows:
        comps_by_fid.setdefault(r["feature_id"], []).append(r["component"])
    teams_by_fid: dict[int, list[str]] = {}
    for r in team_rows:
        teams_by_fid.setdefault(r["feature_id"], []).append(r["team"])

    results = []
    for f in features:
        fid = f["feature_id"]
        results.append(
            {
                "issue_key": f["issue_key"],
                "title": f["title"],
                "status": f["issue_status"],
                "priority": f["priority"],
                "progress_pct": f["progress_pct"],
                "rice_score": f["rice_score"],
                "assignee": f["assignee"],
                "target_start": f["target_start"],
                "target_end": f["target_end"],
                "due_date": f["due_date"],
                "releases": rels_by_fid.get(fid, []),
                "components": comps_by_fid.get(fid, []),
                "teams": teams_by_fid.get(fid, []),
                "product_manager": f["product_manager"],
                "tech_lead": f["tech_lead"],
                "developer": f["developer"],
                "tester": f["tester"],
                "todo_ic": f["todo_ic"],
                "in_progress_ic": f["in_progress_ic"],
                "done_ic": f["done_ic"],
                "total_ic": f["total_ic"],
            }
        )

    return json.dumps({"features": results, "count": len(results)}, default=str)


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def release_risk_summary(release: str | None = None) -> str:
    """Assess release risk by comparing milestone dates against feature completion.

    If no release specified, analyzes all releases with upcoming milestones.
    Flags features under 80% complete when milestone is within 30 days.
    """
    today = date.today()
    conn = _get_conn()
    # Get all milestones (filtering done in Python due to non-ISO date formats)
    if release:
        milestones = conn.execute(
            """SELECT product, version, event_type, event_date
               FROM release_milestone
               WHERE version LIKE ?""",
            (f"%{release}%",),
        ).fetchall()
    else:
        milestones = conn.execute("SELECT product, version, event_type, event_date FROM release_milestone").fetchall()

    # Parse dates and filter to upcoming only
    releases_info = {}
    for m in milestones:
        parsed = _parse_date(m["event_date"], today.year)
        if not parsed or parsed < today:
            continue
        key = f"{m['product']} {m['version']}"
        if key not in releases_info:
            releases_info[key] = {
                "product": m["product"],
                "version": m["version"],
                "milestones": [],
            }
        releases_info[key]["milestones"].append(
            {
                "event_type": m["event_type"],
                "event_date": m["event_date"],
                "days_away": (parsed - today).days,
            }
        )

    if not releases_info:
        return json.dumps(
            {
                "message": "No upcoming milestones found",
                "hint": "Check release_milestone table for available releases",
            }
        )

    # Build feature lookup by normalized (product, major.minor)
    all_feature_releases = conn.execute("SELECT fr.feature_id, fr.release FROM feature_release fr").fetchall()
    features_by_key: dict[tuple[str, str], list[int]] = {}
    for fr in all_feature_releases:
        norm_key = _normalize_release(fr["release"])
        features_by_key.setdefault(norm_key, []).append(fr["feature_id"])

    # For each release, find features and assess risk
    results = []
    for key, info in releases_info.items():
        info["milestones"].sort(key=lambda m: m["days_away"])
        next_milestone = info["milestones"][0]
        days_away = next_milestone["days_away"]

        # Find features targeting this release via normalized product+major.minor
        milestone_key = _milestone_release_key(info["product"], info["version"])
        feature_ids = features_by_key.get(milestone_key, [])
        if feature_ids:
            placeholders = ",".join("?" * len(feature_ids))
            features = conn.execute(
                f"""SELECT f.issue_key, f.title, f.issue_status, f.progress_pct,
                          f.rice_score, f.assignee
                   FROM feature f
                   WHERE f.feature_id IN ({placeholders})""",
                feature_ids,
            ).fetchall()
        else:
            features = []

        at_risk = []
        for f in features:
            pct = f["progress_pct"] or 0
            if pct < 80 and days_away <= 30:
                at_risk.append(
                    {
                        "issue_key": f["issue_key"],
                        "title": f["title"],
                        "status": f["issue_status"],
                        "progress_pct": pct,
                        "assignee": f["assignee"],
                    }
                )

        results.append(
            {
                "release": key,
                "next_milestone": next_milestone["event_type"],
                "milestone_date": next_milestone["event_date"],
                "days_away": days_away,
                "total_features": len(features),
                "at_risk_count": len(at_risk),
                "risk_level": "HIGH" if len(at_risk) > 5 else "MEDIUM" if at_risk else "LOW",
                "at_risk_features": at_risk,
                "all_milestones": info["milestones"],
            }
        )

    results.sort(key=lambda r: r["days_away"])
    return json.dumps({"releases": results, "assessed_on": today.isoformat()}, default=str)


# ---------------------------------------------------------------------------
# Governance tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def list_documents(doc_type: str | None = None) -> str:
    """List governance documents with metadata and table of contents (no full content).

    Optional filter by doc_type: constitution, policy, standard, reference.
    """
    conn = _get_conn()
    if doc_type:
        rows = conn.execute(
            """SELECT doc_id, doc_type, title, version, category, source_file, sections
               FROM governance_document WHERE doc_type = ? ORDER BY doc_id""",
            (doc_type,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT doc_id, doc_type, title, version, category, source_file, sections
               FROM governance_document ORDER BY doc_type, doc_id"""
        ).fetchall()

    results = []
    for r in rows:
        sections = json.loads(r["sections"]) if r["sections"] else []
        toc = [{"heading": s["heading"], "level": s["level"]} for s in sections]
        results.append(
            {
                "doc_id": r["doc_id"],
                "doc_type": r["doc_type"],
                "title": r["title"],
                "version": r["version"],
                "category": r["category"],
                "source_file": r["source_file"],
                "table_of_contents": toc,
            }
        )

    return json.dumps({"documents": results, "count": len(results)}, default=str)


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def get_document(doc_id: int) -> str:
    """Get a governance document's full content by ID.

    Use list_documents() first to find the doc_id.
    """
    conn = _get_conn()
    row = conn.execute("SELECT * FROM governance_document WHERE doc_id = ?", (doc_id,)).fetchone()
    if not row:
        return json.dumps({"error": f"No document with doc_id={doc_id}"})
    return json.dumps(dict(row), default=str)


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def get_document_section(doc_id: int, heading: str) -> str:
    """Get a single section from a governance document by fuzzy heading match.

    Use list_documents() to see available headings, then drill into a specific section.
    """
    conn = _get_conn()
    row = conn.execute("SELECT sections FROM governance_document WHERE doc_id = ?", (doc_id,)).fetchone()
    if not row:
        return json.dumps({"error": f"No document with doc_id={doc_id}"})

    sections = json.loads(row["sections"]) if row["sections"] else []
    headings = [s["heading"] for s in sections]
    matches = get_close_matches(heading, headings, n=1, cutoff=0.4)
    if not matches:
        return json.dumps({"error": f"No section matching '{heading}'", "available": headings})

    matched = matches[0]
    for s in sections:
        if s["heading"] == matched:
            return json.dumps(
                {
                    "heading": s["heading"],
                    "level": s["level"],
                    "content": s["content"],
                }
            )

    return json.dumps({"error": "Section not found"})


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def get_gps_version() -> str:
    """Get the GPS version and build metadata."""
    result = {"version": VERSION}

    conn = _get_conn()
    try:
        meta = conn.execute("SELECT key, value FROM _meta").fetchall()
        result["build_info"] = {r["key"]: r["value"] for r in meta}
        return json.dumps(result, default=str)
    except sqlite3.Error:
        return json.dumps(result, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

ALLOWED_HTTP_HOSTS = [
    "127.0.0.1:*",
    "localhost:*",
    "[::1]:*",
    "host.docker.internal:*",
]

# Extra hosts from env (comma-separated), e.g. OpenShift route hostnames
_extra = os.environ.get("ALLOWED_HTTP_HOSTS", "")
if _extra:
    ALLOWED_HTTP_HOSTS.extend(h.strip() for h in _extra.split(",") if h.strip())


def _configure_http(port: int = 8000) -> None:
    """Configure MCP server for HTTP transport with DNS rebinding protection."""
    from mcp.server.fastmcp.server import TransportSecuritySettings

    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = port
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HTTP_HOSTS,
        allowed_origins=[f"http://{h}" for h in ALLOWED_HTTP_HOSTS],
    )


def _wrap_basic_auth(app):
    """Wrap a Starlette app with basic auth if GPS_AUTH_USER/GPS_AUTH_PASS are set."""
    import base64
    import secrets

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import Response

    auth_user = os.environ.get("GPS_AUTH_USER", "")
    auth_pass = os.environ.get("GPS_AUTH_PASS", "")
    if not (auth_user and auth_pass):
        return app

    expected = base64.b64encode(f"{auth_user}:{auth_pass}".encode()).decode()

    class BasicAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path != "/mcp":
                return await call_next(request)
            # Check Authorization header
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Basic "):
                if secrets.compare_digest(auth_header[6:], expected):
                    return await call_next(request)
            # Check query param fallback (for clients that don't support headers)
            token = request.query_params.get("token", "")
            if token and secrets.compare_digest(token, expected):
                return await call_next(request)
            return Response("Unauthorized", status_code=401, headers={"WWW-Authenticate": "Basic"})

    app.add_middleware(BasicAuthMiddleware)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GPS MCP Server")
    parser.add_argument("--http", action="store_true", help="Use HTTP transport (default: stdio)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")
    args = parser.parse_args()

    if args.http:
        _configure_http(args.port)
        app = mcp.streamable_http_app()
        app = _wrap_basic_auth(app)
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=args.port)
    else:
        mcp.run(transport="stdio")
