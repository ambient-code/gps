# Customization Guide

GPS is a read-only caching tier for org and engineering data. It's designed to be forked and adapted — point it at your own data sources and every agent in your fleet gets sub-millisecond access to your org structure, issues, features, and releases.

## ETL Data Flow

```mermaid
flowchart TD
    XLSX["*.xlsx\nOrg Spreadsheet"] -->|GPS_TAB_ORG_MAP| TABS
    subgraph TABS["XLSX Tab Parsing"]
        T1["Team Breakdown tabs\n→ person, org, component,\nscrum_team, miro_team"]
        T2["Jira/Scrum ref tab\n→ jira_scrum_mapping"]
    end

    CSV_I["*issues*.csv"] --> ISSUES["load_jira_issues()\n→ jira_issue, issue_label,\nissue_component"]
    CSV_F["*feature*.csv"] --> FEATURES["load_features()\n→ feature, feature_release,\nfeature_component, feature_team"]
    CSV_R["*release-schedule*.csv"] --> RELEASES["load_release_schedule()\n→ release_schedule"]
    CSV_V["*component-versions*.csv"] --> VERSIONS["load_release_version_map()\n→ release_milestone,\ncomponent_version_map"]
    CSV_C["*changelog*.csv"] --> CHANGELOG["load_jira_changelog()\n→ issue_changelog"]
    MD["governance/*.md"] --> GOV_MD["load_governance_policies()\n→ governance_document"]
    PDF["data/*.pdf"] --> GOV_PDF["load_governance_pdfs()\n→ governance_document"]

    TABS --> DB
    ISSUES --> DB
    FEATURES --> DB
    RELEASES --> DB
    VERSIONS --> DB
    CHANGELOG --> DB
    GOV_MD --> DB
    GOV_PDF --> DB

    DB[("gps.db")]

    style DB fill:#e8f4f8,stroke:#2196F3,stroke-width:2px
    style XLSX fill:#c8e6c9,stroke:#4CAF50
    style CSV_I fill:#fff9c4,stroke:#FFC107
    style CSV_F fill:#fff9c4,stroke:#FFC107
    style CSV_R fill:#fff9c4,stroke:#FFC107
    style CSV_V fill:#fff9c4,stroke:#FFC107
    style CSV_C fill:#fff9c4,stroke:#FFC107
    style MD fill:#e1bee7,stroke:#9C27B0
    style PDF fill:#e1bee7,stroke:#9C27B0
```

## Org Spreadsheet Mapping

The ETL pipeline reads team breakdowns from XLSX tabs. Configure which tabs to load via the `GPS_TAB_ORG_MAP` environment variable (JSON):

```bash
# .env
GPS_TAB_ORG_MAP='{"Platform - Team Breakdown": ["platform", "Platform"], "Data Services - Team Breakdown": ["data-services", "Data Services"]}'
```

Each key is an XLSX tab name. Each value is `[org_key, org_name]`:
- `org_key` — short identifier used in queries
- `org_name` — display name

The Jira-to-Scrum-team reference tab is also configurable:

```bash
GPS_JIRA_SCRUM_REF_TAB=Jira and Scrum teams
```

## CSV File Naming

`build_db.py` auto-discovers CSV files by pattern matching against filenames. The default patterns are:

| Pattern | Data Source |
|---------|-------------|
| `acme-issues` | Jira issues export |
| `acme-feature` | Feature planning / RICE scores |
| `acme-release-schedule` | Release schedule |
| `acme-changelog` | Jira issue changelog |
| `acme-component-versions` | Release-to-component version mapping |

To use different patterns, update the `csv_sources` list in `build_db.py`.

## XLSX Auto-Detection

`build_db.py` picks the newest `.xlsx` file in `data/`. To use a specific file:

```bash
uv run scripts/build_db.py --xlsx data/my-org.xlsx
```

## Adding Data Sources

See [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-data-source) for the step-by-step guide.

## Governance Documents

Place markdown files in `governance/` — they are loaded as policy documents automatically.

Place PDFs in `data/` — they are extracted via [docling](https://github.com/DS4SD/docling) and loaded as reference documents. Install the optional dependency:

```bash
uv pip install "gps[pdf]"
```

## Database Path

By default the database is `data/gps.db`. Override with:

```bash
uv run scripts/build_db.py --db /path/to/custom.db
```

## MCP Server

The MCP server binds to all interfaces (`0.0.0.0`) in HTTP mode. Allowed hosts for DNS rebinding protection are defined in `ALLOWED_HTTP_HOSTS` in `mcp_server.py`. Add your service hostnames there if deploying behind a proxy.
