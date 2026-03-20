# ADR-003: MCP Transport Security Model

**Status**: Accepted
**Date**: 2026-03-11

## Context

The MCP server supports two transports: stdio (for Claude Code) and streamable-HTTP (for LLM frontends). HTTP transport exposes the server to network requests and requires DNS rebinding protection.

## Decision

- Default to stdio transport (no network exposure)
- HTTP mode (`--http`) binds to `0.0.0.0` with DNS rebinding protection enabled
- Allowed hosts: `127.0.0.1`, `localhost`, `[::1]`, `host.docker.internal` (for Docker containers)
- Allowed origins mirror the host list with `http://` scheme
- Configuration extracted into `_configure_http()` and `ALLOWED_HTTP_HOSTS` constant

## Rationale

- **stdio by default**: Safest option — no network surface. Used by Claude Code.
- **0.0.0.0 bind for HTTP**: Required for Docker containers to reach the host. `127.0.0.1` would be unreachable from inside a container.
- **host.docker.internal**: Docker's built-in DNS name for reaching the host machine. Required for containerized LLM frontends.
- **DNS rebinding protection**: Prevents browser-based attacks from accessing the MCP server via crafted DNS responses.

## Consequences

- Adding new allowed hosts (e.g., Kubernetes service names) requires updating `ALLOWED_HTTP_HOSTS`
- No TLS — assumes trusted network or reverse proxy terminates TLS
- No authentication on MCP endpoints — relies on network isolation
