# ATIF Trajectory Simulator

Generate paired agent **trajectory** files that demonstrate the impact of the GPS
MCP server. For a given scenario the simulator emits two
[ATIF v1.7](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md)
trajectories responding to the *same* task:

- **without GPS** — the agent fumbles through raw files (`find`/`cat`/`grep`, can't
  read the xlsx), burns tokens, and lands a *partial* answer;
- **with GPS** — the agent makes a few targeted MCP tool calls and lands a
  *complete* answer for a fraction of the cost.

Both are grounded in the repo's real Acme example data and written into a
[Harbor](https://github.com/harbor-framework/harbor) jobs-directory tree, so they
validate and visualize with Harbor's existing tooling — no custom viewer.

> **Scope:** these are *simulated* trajectories (deterministic, template-driven) that
> model the mechanism by which GPS helps. They are reproducible illustrative
> evidence, not a capture of a live LLM run. For empirical capture, see
> [Real trajectories](#real-trajectories-future).

## Quickstart

```bash
# 1. Generate the paired trajectories for a scenario
uv run scripts/simulate_trajectories.py --scenario release-risk --release 1.0

# 2. Validate them against Harbor's ATIF validator
./scripts/validate_trajectories.sh

# 3. Prove the impact — deterministic comparison table from the files
uv run scripts/compare_trajectories.py --scenario release-risk

# 4. Visualize side-by-side in the Harbor viewer
uv run --with harbor==0.9.0 harbor view data/trajectories/jobs
#   → open http://127.0.0.1:8080  (release-risk → release-risk → a trial)
#   → use ← / → on a trial page to flip between no-gps and with-gps
```

List available scenarios:

```bash
uv run scripts/simulate_trajectories.py --list
```

## What gets produced

A Harbor jobs tree (`data/trajectories/jobs/<scenario>/`):

```
release-risk/
├── config.json                 # Harbor JobConfig
├── result.json                 # Harbor JobResult (+ aggregate token/cost stats)
├── release-risk-no-gps/
│   ├── config.json             # TrialConfig
│   ├── result.json             # TrialResult (+ agent_result token/cost)
│   └── agent/trajectory.json   # ATIF v1.7
└── release-risk-with-gps/
    ├── config.json
    ├── result.json
    └── agent/trajectory.json
```

Every file is constructed with **Harbor's own Pydantic models** (Harbor 0.9.0), so
invalid output fails at generation time rather than at view time. The two trials
share a `session_id` (same logical run) and diverge only in the agent's tool access.

## How it maps to ATIF / Harbor

| Concept | Where it lives |
|---|---|
| Thought | `step.reasoning_content` |
| Action | `step.tool_calls[]` |
| Observation | `step.observation.results[]` |
| Per-step cost/tokens | `step.metrics` |
| Trajectory totals | `final_metrics` |
| Trial summary (viewer columns) | `TrialResult.agent_result` (AgentContext) |
| Job summary (viewer columns) | `JobResult.stats` (JobStats) |

The viewer renders each step as a Thought/Action/Observation triple, matching the
paradigm in IBM Research's *Agent Trajectory Explorer* (AAAI-25).

## Components

| File | Purpose |
|---|---|
| `scripts/simulate_trajectories.py` | The generator (scenario registry + Harbor model emitter) |
| `scripts/validate_trajectories.sh` | Wrapper over Harbor's ATIF validator |
| `scripts/compare_trajectories.py` | Deterministic with/without comparison (proof table) |
| `tests/test_simulate_trajectories.py` | Unit + end-to-end tests (validator + JobScanner) |
| `docs/atif-simulator/RESULTS.md` | Documented GPS-impact proof for the release-risk scenario |

## Adding a scenario

1. Write `build_<name>(release, data) -> (Trajectory, Trajectory)` in
   `scripts/simulate_trajectories.py`, returning the no-GPS and with-GPS variants.
2. Register it: `SCENARIOS["<name>"] = build_<name>`.
3. `uv run scripts/simulate_trajectories.py --scenario <name>`.

The shared prefix (system + user steps) and the Harbor emitter helpers are reused;
a scenario only defines the divergent agent behavior.

## Real trajectories (future)

The simulator stands in for real capture. Harbor can produce **real** ATIF
trajectories by running Claude Code with the data-layer MCP server attached
(stdio-bundled in the task container) versus not — `harbor run` converts Claude
Code's session log into ATIF automatically. That path is documented separately and
is the empirical complement to this simulator.

## Tests

```bash
uv run tests/test_simulate_trajectories.py   # standalone, no pytest required
scripts/test.sh                              # full repo suite (includes the above)
```
