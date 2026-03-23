# GPS MCP Server — Quick Start

GPS gives Claude Code instant access to org and engineering data (scrum teams, staffing, issues, features, releases).

## Setup

```bash
claude mcp add gps-remote "<MCP_URL_WITH_TOKEN>" -s user -t http
```

Get the URL from your team lead — it includes an auth token.

Then restart Claude Code.

## Verify

```bash
claude mcp list | grep gps-remote
# Should show: ✓ Connected
```

## Example Queries

### "Which scrum teams have the most staff?"

```
Top 3 scrum teams by total staff:

  Platform           Platform Squad                  10.0
  Data Services      Data Squad                       6.0
  ML Engineering     ML Squad                         4.0
```

### "Show me the Platform scrum team boards"

```
Platform scrum team boards:

  Platform Squad                  Staff: 10.0   PM: Dana Chen
```

### "What's the staffing breakdown for the ML Squad?"

```
ML Squad (ML Engineering) — 4.0 total staff

  ML:            2.0
  Backend:       1.0
  QE:            1.0
  PM: Sarah Kim
  Board: https://acme-corp.atlassian.net/jira/software/c/projects/ACME/boards/100
```
