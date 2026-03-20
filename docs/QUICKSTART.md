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
Top 5 scrum teams by total staff:

  AIPCC                PyTorch                         31.0
  Inf Engineering      Runtimes                        27.0
  AI Platform          General                         26.0
  AIPCC                Accelerator Enablement          22.0
  watsonx Team         AutoRAG/AutoML                  22.0
```

### "Show me the AI Platform scrum team boards"

```
AI Platform scrum team boards:

  General                         Staff: 26.0   PM: Jeff DeMoss, Myriam Fentanes Gutierrez, Christoph Görn
  Trusty-AI                       Staff: 20.0   PM: Adel Zaalouk, William Caban
  Model Serving                   Staff: 18.0   PM: Adam Bellusci, Naina Singh, Jonathan Zarecki
  Inference Extensions            Staff: 13.0   PM: Adam Bellusci, Naina Singh
  TestOps                         Staff: 11.5   PM: N/A
  AI Hub                          Staff: 10.5   PM: Adam Bellusci, Peter Double, Jenny Yi
  DevOps & InfraOps               Staff:  9.5   PM: N/A
  Heimdall                        Staff:  7.5   PM: Adam Bellusci
  Kubeflow Training               Staff:  7.0   PM: Christoph Görn
  ...and 21 more teams
```

### "What's the staffing breakdown for the PyTorch team?"

```
PyTorch (AIPCC) — 31.0 total staff

  Backend Engineer:  18.0
  QE:                 6.0
  Manager:            4.0
  Staff Engineers:    2.0
  Agilist:            1.0
  PM: Erwan Gallen
  Board: https://redhat.atlassian.net/jira/software/c/projects/AIPCC/boards/3735
```
