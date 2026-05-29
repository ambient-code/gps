# ATIF Trajectory Simulator — Design

**Date:** 2026-05-29
**Status:** Approved
**Branch:** `worktree-atif-simulator`

## Purpose

Generate paired ATIF v1.7 agent trajectory files that demonstrate the value of the
GPS MCP server. For a given scenario, the simulator emits two trajectories — one for
an agent **without** GPS tools (which fumbles through raw files) and one **with** GPS
tools (which makes precise, efficient lookups) — both responding to the same task and
grounded in the repo's real Acme example data.

The output is consumed by **Harbor's existing tooling** (`harbor view`, trajectory
validator). We build no analysis or visualization of our own; we follow Harbor's lead
on format and directory layout.

## Goals

- Deterministic, template-driven generation (no LLM at runtime).
- Output is valid ATIF v1.7, constructed via Harbor's own Pydantic models.
- Output sits in a Harbor jobs-directory tree that `harbor view` reads directly.
- The two trajectories are structurally comparable: same system prompt and user
  message, diverging only where GPS changes the agent's approach.
- The no-GPS trajectory is *believably messy* — reasonable attempts, wrong turns,
  backtracking, partial answers, higher token/cost — not a comedy of errors.
- Extensible: adding a scenario is one builder function plus a registry entry.

## Non-Goals

- No LLM-powered or free-text/plain-text input modes (Mode 1 only: named scenarios).
- No analysis pipeline, diagnostics, or hotspot detection (Harbor / out of scope).
- No visualization (Harbor Viewer).
- No custom ATIF validation (use Harbor's validator).
- No prompt-to-scenario matching.

## Background

Three sources informed this design:

- **ATIF v1.7 spec** (Harbor RFC 0001) — the trajectory interchange format: root
  metadata, `agent`, `steps[]` with `source` ∈ {system, user, agent}, `tool_calls`,
  `observation`, `metrics`, `final_metrics`.
- **IBM "Agent Trajectory Explorer"** (AAAI-25) — trajectories visualized as
  Thought/Action/Observation triples; ATIF maps cleanly (`reasoning_content` →
  Thought, `tool_calls` → Action, `observation` → Observation).
- **Harbor framework** — provides the ATIF Pydantic models, a trajectory validator,
  and the `harbor view` web viewer that reads a jobs-directory tree.

## User Decisions (locked)

1. Scenario: **release risk assessment** is the first (and initially only) scenario.
2. No-GPS realism: **believably messy** (reasonable attempts, partial answer).
3. Input: **Mode 1 only** — `--scenario <name>`, fully deterministic, no LLM.
4. Generation: **template-driven (Approach B)** — scenario builders, no LLM.
5. Dependency: **depend on Harbor**; match whatever layout/schema Harbor wants.
6. Construction: **use Harbor's Pydantic models** for both trajectory and
   job/trial metadata; stub a model field only where a real container/verifier
   field is genuinely meaningless for synthetic data, and document each stub.

## Architecture

```
uv run scripts/simulate_trajectories.py --scenario release-risk --release 1.0
    │
    ▼
┌─ simulate_trajectories.py ─────────────────────────────────┐
│  1. Load Acme data (data/acme-*.csv)                        │
│  2. Look up named scenario in SCENARIOS registry            │
│  3. Builder emits (no_gps_trajectory, with_gps_trajectory)  │
│     as Harbor Trajectory model instances, grounded in data  │
│  4. Job writer wraps each trajectory in a Harbor trial,     │
│     constructs JobConfig/JobResult/TrialConfig/TrialResult  │
│  5. Write the full jobs-directory tree                      │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
data/trajectories/jobs/release-risk/...   →   harbor view data/trajectories/jobs
```

### Code structure (single file, sectioned)

`scripts/simulate_trajectories.py`, organized as:

- **Data loaders** — read `acme-*.csv` into dicts. Reuse parsing patterns from
  `scripts/build_db.py` where sensible. (Release-risk scenario uses the CSVs only;
  the xlsx org file is not required for v1.)
- **Scenario builders** — `build_release_risk(release, data) -> (Trajectory, Trajectory)`
  returning Harbor `Trajectory` model instances for the no-GPS and with-GPS variants.
- **Job writer** — given a job name and a list of `(trial_name, Trajectory)` pairs,
  constructs the Harbor `JobConfig`/`JobResult`/`TrialConfig`/`TrialResult` models and
  writes the jobs-directory tree.
- **Scenario registry** — `SCENARIOS = {"release-risk": build_release_risk}`.
- **`main()`** — argparse (`--scenario`, `--release`, `--list`, `--output-dir`),
  dispatch, write.

### Scenario registry pattern

```python
SCENARIOS = {
    "release-risk": build_release_risk,
}
```

Adding a scenario later = write one builder + register it. `--scenario` with no match
(or `--list`) prints available scenarios and exits.

## Harbor Jobs-Directory Layout (confirmed via Harbor source)

```
data/trajectories/jobs/
├── release-risk/                   # job
│   ├── config.json                 # Harbor JobConfig
│   ├── result.json                 # Harbor JobResult
│   ├── no-gps/                     # trial
│   │   ├── config.json             # Harbor TrialConfig
│   │   ├── result.json             # Harbor TrialResult
│   │   └── agent/
│   │       └── trajectory.json     # ATIF v1.7 (Harbor Trajectory model)
│   └── with-gps/                   # trial
│       ├── config.json
│       ├── result.json
│       └── agent/
│           └── trajectory.json
```

Confirmed from Harbor's `JobScanner` and viewer server:

- A **job** is recognized by a parsable `config.json` (`JobConfig`) + `result.json`
  (`JobResult`).
- A **trial** is recognized by `result.json` (`TrialResult`); `config.json`
  (`TrialConfig`) is expected for a complete record.
- The viewer's trajectory endpoint reads `agent/trajectory.json`. `recording.cast`
  is **not** required. `verifier/` and `artifacts/` are **not** required for viewing.

## Trajectory Content

### Shared between both variants (identical)

- `schema_version`: `"ATIF-v1.7"`
- `session_id`: same value for the pair (same logical run)
- `agent.name`: `"claude-code"`, `agent.version`: `"1.0.0"`,
  `agent.model_name`: `"claude-sonnet-4-6"`
- Step 1 — `source: "system"`: system prompt (identical)
- Step 2 — `source: "user"`: the task message (identical), e.g.
  *"Assess the release risk for AcmeProduct 1.0."*

### Divergence: tool definitions

- **No-GPS** `agent.tool_definitions`: standard Claude Code tools — bash, read,
  glob, grep, edit.
- **With-GPS** `agent.tool_definitions`: the above **plus** GPS MCP tools —
  `release_risk_summary`, `get_release_schedule`, `get_feature_status`,
  `lookup_person`, `list_team_members`, `search_issues`.

### No-GPS behavior (release-risk, ~15–20 steps)

Agent steps (each `source: "agent"` with `reasoning_content`, `tool_calls`, and a
following observation) show a believable struggle, grounded in the real files:

1. Reason about where release info lives; `find . -name "*.csv"`.
2. `cat data/acme-release-schedule.csv` — gets milestone rows.
3. `grep` for the version across files to find feature associations.
4. `cat data/acme-feature-planning.csv` — wide column layout, partially misparsed.
5. Attempt to find owners of at-risk features; `grep` names in the `.xlsx` (fails —
   binary).
6. Backtrack: `find . -name "*.xlsx"`, realize xlsx isn't readable as text.
7. `grep "At Risk"` across CSVs; find some at-risk epics (e.g. ACME-101).
8. Miss the component-version mapping dimension.
9. Produce a **partial** answer — covers some at-risk features but omits team
   ownership and/or milestone dates.

Metrics reflect dumping full file contents into context: high prompt tokens.

### With-GPS behavior (release-risk, ~5–7 steps)

1. `release_risk_summary(release="1.0")` → structured at-risk feature data.
2. `get_release_schedule(product="acmeproduct", version="1.0")` → milestone dates.
3. `get_feature_status(...)` on the at-risk features for detail.
4. `lookup_person(...)` on owners for context.
5. Produce a **complete**, structured answer covering all dimensions.

Metrics reflect compact structured tool results: low prompt tokens.

### Simulated metrics (illustrative targets, tuned during implementation)

| | No-GPS | With-GPS |
|---|---|---|
| prompt tokens | ~50–80k | ~15–20k |
| completion tokens | ~3–5k | ~2k |
| cost (USD) | ~0.30–0.50 | ~0.08–0.12 |
| steps | ~15–20 | ~5–7 |

`final_metrics` aggregates each trajectory's step metrics. Per-step latency is set
proportional to that step's token counts so latency/token correlation looks real.
All values are grounded in plausible ratios, not measured.

## Dependencies

- Add **Harbor** as a dependency of the simulator script (PEP 723 inline metadata
  in `scripts/simulate_trajectories.py`, consistent with other scripts and the
  `uv` workflow). Pin to the latest stable version at implementation time; record
  the pinned version in the script and the spec's follow-up notes.
- The simulator imports Harbor's trajectory and job/trial Pydantic models. If the
  public import paths differ from the internal `src/harbor/...` paths, the planning
  phase resolves the correct public API.

## Validation & Viewing

- **Validation**: a companion step (in the script after writing, or a small
  `scripts/validate_trajectories.sh`) runs Harbor's trajectory validator over the
  generated `trajectory.json` files and fails loudly on any error.
- **Viewing**: `harbor view data/trajectories/jobs` serves the pair for
  side-by-side inspection.

## Testing & Verification

- **Unit — data loaders**: parsing `acme-*.csv` yields known values
  (e.g., ACME-103 "Real-Time Data Pipeline" is "At Risk"; 1.0 milestone dates match
  `acme-component-versions.csv`).
- **Unit — trajectory construction**: builders produce valid Harbor `Trajectory`
  instances (Pydantic construction succeeds; step_ids sequential from 1;
  every `tool_call_id` is matched by a `source_call_id` in a later observation).
- **Integration — Harbor validator**: generated `trajectory.json` files pass
  Harbor's validator.
- **Integration — job scan**: the generated tree is recognized (job + both trials),
  verified either via Harbor's `JobScanner` or by `harbor view` starting cleanly.
- **Sanity — the whole point**: no-GPS trajectory has materially more steps, prompt
  tokens, and cost than with-GPS.

## File Layout (new/changed)

```
scripts/simulate_trajectories.py        # new — the simulator
scripts/validate_trajectories.sh        # new (optional) — Harbor validator wrapper
data/trajectories/jobs/                  # new — generated output (gitignore policy TBD)
tests/test_simulate_trajectories.py     # new — unit tests
docs/superpowers/specs/2026-05-29-atif-simulator-design.md  # this doc
```

Note: `data/trajectories/jobs/` is generated. Per repo rules, `*.db`/csv/xlsx are
gitignored but `data/acme-*` examples are tracked. The generated JSON trajectories
are derived example artifacts; whether to commit them or gitignore them is a small
planning-phase decision (default: commit the release-risk pair as a tracked example,
consistent with the repo's "tracked Acme examples" convention).

## Open Items for Planning Phase

1. Resolve Harbor's **public import paths** for the trajectory and job/trial models.
2. Determine the **minimum viable construction** of `TrialConfig`/`TrialResult`
   (and `JobConfig`/`JobResult`) for synthetic trials; document any stubbed fields.
3. Confirm the **latest stable Harbor version** and pin it.
4. Decide **commit vs gitignore** for the generated release-risk pair.
