#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "harbor==0.9.0",
# ]
# ///
"""ATIF trajectory simulator — demonstrate the value of the GPS MCP server.

For a named scenario, emits two ATIF v1.7 trajectories responding to the same
task: one for an agent *without* GPS tools (which fumbles through raw files) and
one *with* GPS tools (which makes precise, efficient lookups). Both are grounded
in the repo's real Acme example data and written into a Harbor jobs-directory
tree that `harbor view` reads directly.

Generation is deterministic and template-driven (no LLM). Trajectories and the
job/trial metadata are constructed with Harbor's own Pydantic models, so invalid
output fails at generation time rather than at view time.

Usage:
    uv run scripts/simulate_trajectories.py --list
    uv run scripts/simulate_trajectories.py --scenario release-risk --release 1.0
    uv run scripts/simulate_trajectories.py --scenario release-risk \\
        --output-dir data/trajectories/jobs

View the result:
    harbor view data/trajectories/jobs
"""

from __future__ import annotations

import argparse
import csv
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harbor import JobConfig, JobResult, JobStats, TrialConfig, TrialResult
from harbor.models.task.id import LocalTaskId
from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)
from harbor.models.trial.config import TaskConfig
from harbor.models.trial.result import AgentInfo

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT_DIR = DATA_DIR / "trajectories" / "jobs"

SCHEMA_VERSION = "ATIF-v1.7"
AGENT_NAME = "claude-code"
AGENT_VERSION = "1.0.0"
MODEL_NAME = "claude-sonnet-4-6"

# Deterministic base time so reruns produce identical output.
BASE_TIME = datetime(2026, 5, 29, 9, 0, 0, tzinfo=timezone.utc)

# Cost model (USD per token), illustrative Sonnet-class pricing.
COST_INPUT = 3.0 / 1_000_000
COST_CACHED_INPUT = 0.30 / 1_000_000
COST_OUTPUT = 15.0 / 1_000_000

# Latency model: a base plus a per-prompt-token component (~200ms / Ktok).
LATENCY_BASE_MS = 600
LATENCY_PER_KTOK_MS = 200


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_acme_data() -> dict[str, list[dict[str, str]]]:
    """Load the Acme example CSVs used to ground the trajectories."""
    return {
        "features": _read_csv(DATA_DIR / "acme-feature-planning.csv"),
        "schedule": _read_csv(DATA_DIR / "acme-release-schedule.csv"),
        "issues": _read_csv(DATA_DIR / "acme-issues-export.csv"),
    }


def features_for_release(features: list[dict[str, str]], release: str) -> list[dict[str, str]]:
    """Return feature rows whose Releases column targets the given release.

    `release` is a major.minor like "1.0"; rows tag it as "acmeproduct-1.0".
    """
    tag = f"acmeproduct-{release}"
    return [f for f in features if f.get("Releases", "").strip() == tag]


def schedule_for_release(schedule: list[dict[str, str]], release: str) -> list[dict[str, str]]:
    """Return milestone rows for "AcmeProduct <release>"."""
    name = f"AcmeProduct {release}"
    return [s for s in schedule if s.get("Release", "").strip() == name]


# ---------------------------------------------------------------------------
# ATIF step-builder helpers (Harbor models)
# ---------------------------------------------------------------------------


def _latency_ms(prompt_tokens: int) -> int:
    return LATENCY_BASE_MS + round(prompt_tokens / 1000 * LATENCY_PER_KTOK_MS)


def _cost_usd(prompt_tokens: int, completion_tokens: int, cached_tokens: int) -> float:
    non_cached = prompt_tokens - cached_tokens
    cost = non_cached * COST_INPUT + cached_tokens * COST_CACHED_INPUT + completion_tokens * COST_OUTPUT
    return round(cost, 6)


class _Clock:
    """Monotonic, deterministic timestamp generator."""

    def __init__(self, start: datetime) -> None:
        self._t = start

    def stamp(self) -> str:
        return self._t.isoformat()

    def advance_ms(self, ms: int) -> None:
        self._t += timedelta(milliseconds=ms)


def make_tool_call(call_id: str, name: str, arguments: dict) -> ToolCall:
    return ToolCall(tool_call_id=call_id, function_name=name, arguments=arguments)


def make_observation(call_id: str, content: str) -> Observation:
    return Observation(results=[ObservationResult(source_call_id=call_id, content=content)])


def make_metrics(prompt: int, completion: int, cached: int = 0) -> Metrics:
    return Metrics(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        cost_usd=_cost_usd(prompt, completion, cached),
    )


def build_trajectory(
    session_id: str,
    trajectory_id: str,
    tool_definitions: list[dict],
    steps: list[Step],
) -> Trajectory:
    """Wrap steps in a Trajectory, computing aggregate final_metrics."""
    total_prompt = sum(s.metrics.prompt_tokens or 0 for s in steps if s.metrics)
    total_completion = sum(s.metrics.completion_tokens or 0 for s in steps if s.metrics)
    total_cached = sum(s.metrics.cached_tokens or 0 for s in steps if s.metrics)
    total_cost = round(sum(s.metrics.cost_usd or 0.0 for s in steps if s.metrics), 6)
    return Trajectory(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        trajectory_id=trajectory_id,
        agent=Agent(
            name=AGENT_NAME,
            version=AGENT_VERSION,
            model_name=MODEL_NAME,
            tool_definitions=tool_definitions,
        ),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
            total_cached_tokens=total_cached,
            total_cost_usd=total_cost,
            total_steps=len(steps),
        ),
    )


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling schema)
# ---------------------------------------------------------------------------


def _fn(name: str, description: str, params: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": params or {},
            },
        },
    }


BASE_TOOLS = [
    _fn("bash", "Run a shell command.", {"command": {"type": "string"}}),
    _fn("read", "Read a file from disk.", {"path": {"type": "string"}}),
    _fn("glob", "Find files matching a glob pattern.", {"pattern": {"type": "string"}}),
    _fn("grep", "Search file contents with a regex.", {"pattern": {"type": "string"}}),
]

GPS_TOOLS = [
    _fn(
        "release_risk_summary",
        "Assess release risk by comparing milestone dates against feature completion.",
        {"release": {"type": "string"}},
    ),
    _fn(
        "get_release_schedule",
        "Get release schedule milestone dates for a product/version.",
        {"product": {"type": "string"}, "version": {"type": "string"}},
    ),
    _fn(
        "get_feature_status",
        "Get feature details: progress, RICE score, releases, owner, team.",
        {"issue_key": {"type": "string"}},
    ),
    _fn(
        "lookup_person",
        "Find a person by name, returning org, team, and role.",
        {"name": {"type": "string"}},
    ),
]


# ---------------------------------------------------------------------------
# Scenario: release-risk
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a senior engineering program manager assistant. Answer questions "
    "about release status, risk, ownership, and schedule using the tools "
    "available to you. Be concrete and cite specific features, people, and dates."
)


def _release_risk_user_message(release: str) -> str:
    return (
        f"Assess the release risk for AcmeProduct {release}. Which features are "
        "at risk, who owns them, and what are the key milestone dates?"
    )


def _prefix_steps(clock: _Clock, release: str) -> list[Step]:
    """The identical system + user steps shared by both variants."""
    s1 = Step(
        step_id=1,
        timestamp=clock.stamp(),
        source="system",
        message=SYSTEM_PROMPT,
    )
    clock.advance_ms(10)
    s2 = Step(
        step_id=2,
        timestamp=clock.stamp(),
        source="user",
        message=_release_risk_user_message(release),
    )
    clock.advance_ms(10)
    return [s1, s2]


def _agent_step(
    clock: _Clock,
    step_id: int,
    reasoning: str,
    message: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cached_tokens: int = 0,
    tool: tuple[str, str, dict] | None = None,
    observation_content: str | None = None,
) -> Step:
    """Build one agent turn and advance the clock by its simulated latency."""
    tool_calls = None
    observation = None
    if tool is not None:
        call_id = f"call_{step_id}"
        fn_name, _, args = tool
        tool_calls = [make_tool_call(call_id, fn_name, args)]
        if observation_content is not None:
            observation = make_observation(call_id, observation_content)
    step = Step(
        step_id=step_id,
        timestamp=clock.stamp(),
        source="agent",
        model_name=MODEL_NAME,
        message=message,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        observation=observation,
        metrics=make_metrics(prompt_tokens, completion_tokens, cached_tokens),
        llm_call_count=1,
    )
    clock.advance_ms(_latency_ms(prompt_tokens))
    return step


def build_release_risk(release: str, data: dict[str, list[dict[str, str]]]) -> tuple[Trajectory, Trajectory]:
    """Return (no_gps, with_gps) trajectories for the release-risk scenario."""
    feats = features_for_release(data["features"], release)
    sched = schedule_for_release(data["schedule"], release)
    if not feats:
        raise ValueError(f"No features target release {release!r} in the Acme data.")

    at_risk = [f for f in feats if f.get("Color Status", "").strip() == "At Risk"]
    by_date = {s["Task"]: (s["Date Start"], s["Date Finish"]) for s in sched}
    ga = by_date.get("GA Release", ("?", "?"))[1]
    code_freeze = by_date.get("Code Freeze", ("?", "?"))[0]

    session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"gps/release-risk/{release}")).upper()

    no_gps = _build_release_risk_no_gps(release, feats, sched, at_risk, ga, code_freeze, session_id)
    with_gps = _build_release_risk_with_gps(release, feats, at_risk, ga, code_freeze, session_id)
    return no_gps, with_gps


def _build_release_risk_no_gps(release, feats, sched, at_risk, ga, code_freeze, session_id):
    """Believably messy: reasonable attempts, wrong turns, a partial answer."""
    clock = _Clock(BASE_TIME)
    steps = _prefix_steps(clock, release)

    csv_list = "data/acme-feature-planning.csv\ndata/acme-release-schedule.csv\n"
    csv_list += "data/acme-issues-export.csv\ndata/acme-component-versions.csv"
    schedule_dump = "Release,Task,Date Start,Date Finish\n" + "\n".join(
        f"{s['Release']},{s['Task']},{s['Date Start']},{s['Date Finish']}" for s in sched
    )
    feature_header = (
        "Issue key,Title,Project,Hierarchy,Assignee,Sprint,Target start date,"
        "Target end date,Due date,Estimates (d),Parent,Priority,Issue status,"
        "Progress (%),...,Color Status,Release Date,RICE Score,...,Releases,"
        "Labels,Components,Team"
    )
    feat_rows = "\n".join(
        f"{f['Issue key']},{f['Title']},...,{f['Progress (%)']},...,"
        f"{f['Color Status']},...,{f['Releases']},...,{f['Components']},{f['Team']}"
        for f in feats
    )
    at_risk_keys = ", ".join(f["Issue key"] for f in at_risk) or "none found"

    steps += [
        _agent_step(
            clock,
            3,
            "The user wants release risk for AcmeProduct 1.0. I don't have a tool "
            "for that — I'll have to dig through the repo. Let me find the data files.",
            "Let me locate the release data.",
            4200,
            90,
            tool=("bash", "", {"command": "find . -name '*.csv' -maxdepth 3"}),
            observation_content=csv_list,
        ),
        _agent_step(
            clock,
            4,
            "There's a release schedule CSV. Let me read it to get the milestone dates.",
            "Reading the release schedule.",
            6800,
            80,
            tool=("bash", "", {"command": "cat data/acme-release-schedule.csv"}),
            observation_content=schedule_dump,
        ),
        _agent_step(
            clock,
            5,
            "Good, I have 1.0 milestones. Now I need to know which features target "
            "1.0 and their status. Let me search for files mentioning the release.",
            "Searching for feature-to-release associations.",
            9500,
            70,
            tool=("bash", "", {"command": "grep -rl '1.0' data/"}),
            observation_content=(
                "data/acme-feature-planning.csv\ndata/acme-component-versions.csv\ndata/acme-release-schedule.csv"
            ),
        ),
        _agent_step(
            clock,
            6,
            "acme-feature-planning.csv looks most relevant. Let me dump it — though "
            "it's very wide, so the columns may be hard to line up.",
            "Reading the feature planning data.",
            16500,
            120,
            tool=("bash", "", {"command": "cat data/acme-feature-planning.csv"}),
            observation_content=("<41-column CSV, wraps in terminal — hard to align values to headers>"),
        ),
        _agent_step(
            clock,
            7,
            "That wrapped badly and I can't reliably tell which column is status vs "
            "release vs team. Let me isolate the header row to map the columns.",
            "Inspecting the header row.",
            17800,
            110,
            tool=("bash", "", {"command": "head -1 data/acme-feature-planning.csv"}),
            observation_content=feature_header,
        ),
        _agent_step(
            clock,
            8,
            "So 'Releases' tags rows as acmeproduct-1.0 and 'Color Status' holds the "
            "risk flag. Let me filter to the 1.0 features.",
            "Filtering to 1.0 features.",
            19200,
            160,
            tool=("bash", "", {"command": "grep 'acmeproduct-1.0' data/acme-feature-planning.csv"}),
            observation_content=feat_rows,
        ),
        _agent_step(
            clock,
            9,
            f"Eyeballing the rows, {at_risk_keys} is flagged 'At Risk'. I need the "
            "owner and team. The 'Team' column says 'Platform Squad' but I want the "
            "human owner — let me check the org spreadsheet.",
            "Looking for ownership in the org data.",
            20500,
            100,
            tool=("bash", "", {"command": "find . -name '*.xlsx'"}),
            observation_content="data/acme-corp-org.xlsx",
        ),
        _agent_step(
            clock,
            10,
            "It's an xlsx. Let me try to read it.",
            "Attempting to read the org spreadsheet.",
            21000,
            90,
            tool=("bash", "", {"command": "cat data/acme-corp-org.xlsx | head -c 400"}),
            observation_content="PK\x03\x04\x14\x00\x08\x08\x08\x00 <binary — unreadable as text>",
        ),
        _agent_step(
            clock,
            11,
            "xlsx is a zip archive; I can't parse it without a library here. Let me "
            "fall back to the issues export to at least get an assignee for the auth work.",
            "Falling back to the issues export.",
            22300,
            130,
            tool=("bash", "", {"command": "grep 'acme-auth' data/acme-issues-export.csv | head -5"}),
            observation_content=(
                "ACME-2,Fix authentication token refresh race condition,In Review,"
                "Critical,Carlos Mendoza,...\nACME-16,OAuth2 PKCE flow implementation,"
                "In Progress,Major,Carlos Mendoza,...\nACME-22,Fix session invalidation,"
                "In Review,Critical,Sophie Laurent,..."
            ),
        ),
        _agent_step(
            clock,
            12,
            "Carlos Mendoza shows up on auth issues, so he's likely involved, but I "
            "can't confirm he owns the at-risk feature from this. Let me at least pin "
            "down the code-freeze date relative to today.",
            "Confirming the code freeze date.",
            24000,
            110,
            tool=("bash", "", {"command": "grep 'Code Freeze' data/acme-release-schedule.csv"}),
            observation_content="\n".join(
                f"{s['Release']},{s['Task']},{s['Date Start']},{s['Date Finish']}"
                for s in sched
                if s["Task"] == "Code Freeze"
            ),
        ),
        _agent_step(
            clock,
            13,
            "I have a partial picture. I can name the at-risk feature and the key "
            "dates, but I couldn't reliably resolve the human owner or pull the RICE "
            "score from that wide CSV. I'll report what I'm confident about and flag "
            "the gaps.",
            (
                f"**AcmeProduct {release} — partial risk assessment**\n\n"
                f"- **At risk:** {at_risk_keys} (Color Status = 'At Risk').\n"
                f"- **Key dates:** Code Freeze {code_freeze}, GA {ga}.\n"
                f"- **Ownership:** likely Platform Squad; Carlos Mendoza appears on "
                f"auth issues but I could not confirm the feature owner (the org "
                f"spreadsheet is xlsx and unreadable here).\n"
                f"- **Gaps:** RICE score and exact % complete were hard to extract "
                f"from the 41-column planning CSV; treat with caution."
            ),
            25500,
            380,
        ),
    ]
    return build_trajectory(session_id, "release-risk-no-gps", BASE_TOOLS, steps)


def _build_release_risk_with_gps(release, feats, at_risk, ga, code_freeze, session_id):
    """Precise and efficient: GPS tools answer each dimension directly."""
    clock = _Clock(BASE_TIME)
    steps = _prefix_steps(clock, release)

    def feat_line(f):
        return (
            f"{f['Issue key']} {f['Title']}: {f['Progress (%)']}% complete, "
            f"status={f['Color Status']}, owner={f['Owner']}, team={f['Team']}, "
            f"RICE={f['RICE Score']}"
        )

    risk_summary = (
        f"Release acmeproduct-{release}: {len(feats)} features, "
        f"{len(at_risk)} at risk.\n"
        + "\n".join(feat_line(f) for f in at_risk)
        + f"\nNearest milestone: Code Freeze {code_freeze} (passed), GA {ga}."
    )
    f0 = at_risk[0] if at_risk else feats[0]
    feature_detail = (
        f"{f0['Issue key']} — {f0['Title']}\n"
        f"Status: {f0['Issue status']} ({f0['Color Status']}), "
        f"{f0['Progress (%)']}% complete\n"
        f"Owner: {f0['Owner']} | Team: {f0['Team']} | RICE: {f0['RICE Score']}\n"
        f"Target end: {f0['Target end date']} | Release: {f0['Releases']}"
    )
    owner = f0["Owner"]
    person_detail = (
        f"{owner}: Engineering, Platform Squad. Owner/assignee on the "
        f"Unified Authentication System epic and related acme-auth work."
    )

    steps += [
        _agent_step(
            clock,
            3,
            "GPS exposes release_risk_summary — that answers the core question directly. Let me call it for 1.0.",
            "Pulling the release risk summary from GPS.",
            4200,
            70,
            cached_tokens=0,
            tool=("release_risk_summary", "", {"release": release}),
            observation_content=risk_summary,
        ),
        _agent_step(
            clock,
            4,
            "One feature is at risk. Let me confirm the exact milestone dates.",
            "Fetching the milestone schedule.",
            5200,
            60,
            cached_tokens=3500,
            tool=("get_release_schedule", "", {"product": "acmeproduct", "version": release}),
            observation_content=(
                f"AcmeProduct {release}: Planning, Development, Code Freeze {code_freeze}, QE, GA Release {ga}."
            ),
        ),
        _agent_step(
            clock,
            5,
            f"Let me pull full detail on the at-risk feature {f0['Issue key']} (owner, team, RICE, progress).",
            "Getting at-risk feature detail.",
            6100,
            80,
            cached_tokens=4200,
            tool=("get_feature_status", "", {"issue_key": f0["Issue key"]}),
            observation_content=feature_detail,
        ),
        _agent_step(
            clock,
            6,
            f"Owner is {owner}. Let me get their org context to round out the answer.",
            "Looking up the owner.",
            6800,
            70,
            cached_tokens=4800,
            tool=("lookup_person", "", {"name": owner}),
            observation_content=person_detail,
        ),
        _agent_step(
            clock,
            7,
            "I have every dimension: at-risk feature, owner, team, completion, RICE, "
            "and the milestone dates. I can give a complete, sourced answer.",
            (
                f"**AcmeProduct {release} — release risk assessment**\n\n"
                f"- **At risk:** {f0['Issue key']} {f0['Title']} — "
                f"{f0['Progress (%)']}% complete, flagged '{f0['Color Status']}' "
                f"(RICE {f0['RICE Score']}).\n"
                f"- **Owner:** {owner} (Platform Squad).\n"
                f"- **Key dates:** Code Freeze {code_freeze} (already passed), "
                f"GA {ga}.\n"
                f"- **Read:** with code freeze passed and the feature at "
                f"{f0['Progress (%)']}%, {f0['Issue key']} is the primary threat to "
                f"the {release} GA. The other {len(feats) - len(at_risk)} features "
                f"targeting {release} are On Track."
            ),
            7400,
            360,
            cached_tokens=5200,
        ),
    ]
    return build_trajectory(session_id, "release-risk-with-gps", GPS_TOOLS, steps)


# ---------------------------------------------------------------------------
# Scenario registry
# ---------------------------------------------------------------------------

SCENARIOS = {
    "release-risk": build_release_risk,
}


# ---------------------------------------------------------------------------
# Job writer (Harbor jobs-directory tree)
# ---------------------------------------------------------------------------


def _write_trial(trial_dir: Path, job_id: uuid.UUID, scenario: str, trajectory: Trajectory) -> None:
    """Write one trial: config.json, result.json, agent/trajectory.json."""
    trial_name = trajectory.trajectory_id  # e.g. "release-risk-no-gps"
    agent_dir = trial_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)

    (agent_dir / "trajectory.json").write_text(trajectory.model_dump_json(exclude_none=True, indent=2))

    trial_config = TrialConfig(
        task=TaskConfig(name=scenario),
        trial_name=trial_name,
        job_id=str(job_id),
    )
    (trial_dir / "config.json").write_text(trial_config.model_dump_json(exclude_none=True, indent=2))

    trial_result = TrialResult(
        task_name=scenario,
        trial_name=trial_name,
        trial_uri=f"file://{trial_dir.as_posix()}",
        task_id=LocalTaskId(path=scenario),
        task_checksum=trajectory.session_id,
        config=trial_config,
        agent_info=AgentInfo(name=AGENT_NAME, version=AGENT_VERSION),
        started_at=BASE_TIME,
        finished_at=BASE_TIME,
    )
    (trial_dir / "result.json").write_text(trial_result.model_dump_json(exclude_none=True, indent=2))


def write_job(output_dir: Path, scenario: str, trajectories: list[Trajectory]) -> Path:
    """Write a Harbor job directory containing one trial per trajectory."""
    job_dir = output_dir / scenario
    job_dir.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid5(uuid.NAMESPACE_URL, f"gps/job/{scenario}")

    job_config = JobConfig(job_name=scenario, jobs_dir=str(output_dir))
    (job_dir / "config.json").write_text(job_config.model_dump_json(exclude_none=True, indent=2))

    job_result = JobResult(
        id=job_id,
        started_at=BASE_TIME,
        finished_at=BASE_TIME,
        n_total_trials=len(trajectories),
        stats=JobStats(n_completed_trials=len(trajectories)),
    )
    (job_dir / "result.json").write_text(job_result.model_dump_json(exclude_none=True, indent=2))

    for trajectory in trajectories:
        trial_dir = job_dir / trajectory.trajectory_id
        _write_trial(trial_dir, job_id, scenario, trajectory)

    return job_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", help="Scenario name to generate.")
    parser.add_argument("--release", default="1.0", help="Release major.minor (default: 1.0).")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Harbor jobs dir.")
    parser.add_argument("--list", action="store_true", help="List available scenarios and exit.")
    args = parser.parse_args(argv)

    if args.list or not args.scenario:
        print("Available scenarios:")
        for name in SCENARIOS:
            print(f"  {name}")
        return 0 if args.list else 2

    if args.scenario not in SCENARIOS:
        print(f"Unknown scenario {args.scenario!r}. Available: {', '.join(SCENARIOS)}", file=sys.stderr)
        return 2

    data = load_acme_data()
    builder = SCENARIOS[args.scenario]
    no_gps, with_gps = builder(args.release, data)
    job_dir = write_job(args.output_dir, args.scenario, [no_gps, with_gps])

    print(f"Wrote job → {job_dir}")
    for traj in (no_gps, with_gps):
        fm = traj.final_metrics
        print(
            f"  {traj.trajectory_id}: {fm.total_steps} steps, "
            f"{fm.total_prompt_tokens:,} prompt tok, ${fm.total_cost_usd:.4f}"
        )
    print(f"\nView: harbor view {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
