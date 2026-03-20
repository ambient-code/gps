# ADR-001: SQLite + MCP over RAG

**Status**: Accepted
**Date**: 2026-03-11

## Context

We need to expose organizational data (org structure, Jira issues, governance docs) through an LLM frontend so users can query it via natural language.

Two approaches were considered:

1. **RAG**: Embed documents into a vector store, retrieve chunks via semantic search
2. **SQLite + MCP**: Expose structured data via MCP tools that execute SQL queries against a materialized SQLite database

## Decision

Use SQLite + MCP tool-use. Do not implement RAG.

## Rationale

- **Structured data needs SQL precision**: Queries like "who is on team X?" or "what features are at risk for release Y?" require exact joins and filters, not fuzzy semantic retrieval.
- **Small doc corpus**: The governance documents are few and well-structured. Full document retrieval via MCP is sufficient.
- **MCP tools already exist**: The GPS MCP server has purpose-built tools (`search_issues`, `lookup_person`, `list_documents`, etc.) that return precise results.
- **RAG adds complexity without payoff**: Embedding infrastructure (vector DB, chunking pipeline, embedding model) would add operational burden for marginal benefit given the data characteristics.
- **SQLite air gap**: The LLM never touches Jira directly. It reads from a materialized, read-only database — providing a clean security boundary.

## Consequences

- No semantic search over unstructured text (acceptable given current corpus size)
- Data freshness depends on ETL pipeline runs (existing `build_db.py` workflow)
- New data sources must be materialized into SQLite before they're queryable

## Revisit When

- Governance doc corpus grows significantly (10+ documents, diverse formats)
- Users need semantic search over unstructured text (meeting notes, design docs)
