#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Compare two ATIF trajectories in a Harbor job and emit a proof table.

Reads the trajectory.json files under a scenario's job directory and derives a
deterministic, side-by-side comparison: step counts, token usage, cost, tool
usage, and final-answer outcome. All figures are computed from the files — no
hand-entered numbers — so the comparison is reproducible evidence.

Usage:
    uv run scripts/compare_trajectories.py                         # default job dir
    uv run scripts/compare_trajectories.py --scenario release-risk
    uv run scripts/compare_trajectories.py --jobs-dir path/to/jobs --scenario X
    uv run scripts/compare_trajectories.py --format json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_JOBS_DIR = REPO_ROOT / "data" / "trajectories" / "jobs"

# Convention: trial whose id ends with this is the GPS-equipped ("after") run.
WITH_SUFFIX = "with-gps"
NO_SUFFIX = "no-gps"


def _load_trajectory(job_dir: Path, trial_name: str) -> dict:
    return json.loads((job_dir / trial_name / "agent" / "trajectory.json").read_text())


def _find_trials(job_dir: Path) -> tuple[str, str]:
    """Return (no_gps_trial, with_gps_trial) names found under the job dir."""
    trials = sorted(p.name for p in job_dir.iterdir() if (p / "agent" / "trajectory.json").exists())
    no_gps = next((t for t in trials if t.endswith(NO_SUFFIX)), None)
    with_gps = next((t for t in trials if t.endswith(WITH_SUFFIX)), None)
    if not no_gps or not with_gps:
        raise SystemExit(f"Expected one *-{NO_SUFFIX} and one *-{WITH_SUFFIX} trial under {job_dir}; found {trials}")
    return no_gps, with_gps


def _tool_usage(trajectory: dict) -> Counter:
    counter: Counter = Counter()
    for step in trajectory["steps"]:
        for call in step.get("tool_calls") or []:
            counter[call["function_name"]] += 1
    return counter


def _final_answer(trajectory: dict) -> str:
    for step in reversed(trajectory["steps"]):
        if step["source"] == "agent" and step.get("message"):
            return step["message"]
    return ""


def _pct_drop(before: float, after: float) -> float:
    if not before:
        return 0.0
    return round((before - after) / before * 100, 1)


def _ratio(before: float, after: float) -> float:
    if not after:
        return float("inf")
    return round(before / after, 1)


def build_comparison(job_dir: Path) -> dict:
    no_gps_name, with_gps_name = _find_trials(job_dir)
    no_gps = _load_trajectory(job_dir, no_gps_name)
    with_gps = _load_trajectory(job_dir, with_gps_name)
    nf = no_gps["final_metrics"]
    wf = with_gps["final_metrics"]

    def row(key: str) -> dict:
        before, after = nf.get(key) or 0, wf.get(key) or 0
        return {
            "before": before,
            "after": after,
            "pct_drop": _pct_drop(before, after),
            "ratio": _ratio(before, after),
        }

    return {
        "scenario": job_dir.name,
        "no_gps_trial": no_gps_name,
        "with_gps_trial": with_gps_name,
        "metrics": {
            "total_steps": row("total_steps"),
            "total_prompt_tokens": row("total_prompt_tokens"),
            "total_completion_tokens": row("total_completion_tokens"),
            "total_cost_usd": row("total_cost_usd"),
        },
        "cached_tokens": {
            "no_gps": nf.get("total_cached_tokens") or 0,
            "with_gps": wf.get("total_cached_tokens") or 0,
        },
        "tools": {
            "no_gps": dict(_tool_usage(no_gps)),
            "with_gps": dict(_tool_usage(with_gps)),
        },
        "final_answer": {
            "no_gps": _final_answer(no_gps),
            "with_gps": _final_answer(with_gps),
        },
    }


def _fmt(n: float) -> str:
    return f"{n:,.4f}".rstrip("0").rstrip(".") if isinstance(n, float) else f"{n:,}"


def render_markdown(cmp: dict) -> str:
    m = cmp["metrics"]
    lines = [
        f"## GPS impact — `{cmp['scenario']}`",
        "",
        f"Computed from `{cmp['no_gps_trial']}` vs `{cmp['with_gps_trial']}` trajectory files.",
        "",
        "| Metric | Without GPS | With GPS | Reduction | Ratio |",
        "|---|--:|--:|--:|--:|",
    ]
    labels = {
        "total_steps": "Steps",
        "total_prompt_tokens": "Prompt tokens",
        "total_completion_tokens": "Completion tokens",
        "total_cost_usd": "Cost (USD)",
    }
    for key, label in labels.items():
        r = m[key]
        before = f"${r['before']:.4f}" if key == "total_cost_usd" else _fmt(r["before"])
        after = f"${r['after']:.4f}" if key == "total_cost_usd" else _fmt(r["after"])
        lines.append(f"| {label} | {before} | {after} | −{r['pct_drop']}% | {r['ratio']}× |")

    c = cmp["cached_tokens"]
    lines += [
        "",
        f"**Prompt caching:** {c['no_gps']:,} cached tokens without GPS vs "
        f"{c['with_gps']:,} with GPS (structured tool results are cache-friendly).",
        "",
        "### Tool usage (how the work got done)",
        "",
        f"- **Without GPS:** {_tools_str(cmp['tools']['no_gps'])}",
        f"- **With GPS:** {_tools_str(cmp['tools']['with_gps'])}",
        "",
        "### Outcome",
        "",
        "**Without GPS:**",
        "",
        _blockquote(cmp["final_answer"]["no_gps"]),
        "",
        "**With GPS:**",
        "",
        _blockquote(cmp["final_answer"]["with_gps"]),
    ]
    return "\n".join(lines)


def _tools_str(usage: dict) -> str:
    if not usage:
        return "(none)"
    return ", ".join(f"`{name}` ×{count}" for name, count in usage.items())


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs-dir", type=Path, default=DEFAULT_JOBS_DIR)
    parser.add_argument("--scenario", default="release-risk")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args(argv)

    job_dir = args.jobs_dir / args.scenario
    if not job_dir.is_dir():
        raise SystemExit(f"No job dir at {job_dir} — run simulate_trajectories.py first.")

    cmp = build_comparison(job_dir)
    if args.format == "json":
        print(json.dumps(cmp, indent=2))
    else:
        print(render_markdown(cmp))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
