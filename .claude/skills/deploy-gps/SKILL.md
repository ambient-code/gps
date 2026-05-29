---
description: Build, test, and deploy GPS MCP server to OpenShift
---

# Deploy GPS

Run the full deploy pipeline for the GPS MCP server.

## Steps

1. Verify clean git state: `git status --porcelain` must be empty
2. Run test suite: `scripts/test.sh`
3. If tests pass, run: `deploy/deploy.sh all --overlay openshift`
4. After deploy completes, verify health endpoint: `curl -sf $(oc get route gps-mcp-server -n gps-mcp-server -o jsonpath='{.spec.host}')/health`
5. Report: version deployed, pod status, health check result

## Pre-conditions
- Must be on main branch or have explicit approval
- `oc` must be logged in (`oc whoami` succeeds)
- Docker must be running
