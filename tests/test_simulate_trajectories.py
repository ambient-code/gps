#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "harbor==0.9.0",
# ]
# ///
"""Tests for the ATIF trajectory simulator.

Runnable two ways:
    uv run tests/test_simulate_trajectories.py     # standalone, no pytest needed
    uv run --with pytest pytest tests/             # under pytest if available
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import simulate_trajectories as sim  # noqa: E402
from harbor.utils.trajectory_validator import validate_trajectory  # noqa: E402
from harbor.viewer.scanner import JobScanner  # noqa: E402

# --- Data loaders -----------------------------------------------------------


def test_features_for_release_filters_by_tag():
    data = sim.load_acme_data()
    feats = sim.features_for_release(data["features"], "1.0")
    keys = {f["Issue key"] for f in feats}
    # 1.0 features per acme-feature-planning.csv
    assert keys == {"ACME-100", "ACME-101", "ACME-102", "ACME-108"}, keys
    # ACME-103 targets 1.1, must not leak into 1.0
    assert "ACME-103" not in keys


def test_at_risk_feature_identified():
    data = sim.load_acme_data()
    feats = sim.features_for_release(data["features"], "1.0")
    at_risk = [f for f in feats if f["Color Status"].strip() == "At Risk"]
    assert [f["Issue key"] for f in at_risk] == ["ACME-101"]


def test_schedule_for_release_dates():
    data = sim.load_acme_data()
    sched = sim.schedule_for_release(data["schedule"], "1.0")
    by_task = {s["Task"]: s for s in sched}
    assert by_task["GA Release"]["Date Finish"] == "2026-05-15"
    assert by_task["Code Freeze"]["Date Start"] == "2026-04-16"


# --- Trajectory construction ------------------------------------------------


def test_both_trajectories_build():
    data = sim.load_acme_data()
    no_gps, with_gps = sim.build_release_risk("1.0", data)
    assert no_gps.schema_version == "ATIF-v1.7"
    assert with_gps.schema_version == "ATIF-v1.7"
    # Shared session id (same logical run), distinct trajectory ids.
    assert no_gps.session_id == with_gps.session_id
    assert no_gps.trajectory_id != with_gps.trajectory_id


def test_step_ids_sequential_from_one():
    data = sim.load_acme_data()
    for traj in sim.build_release_risk("1.0", data):
        ids = [s.step_id for s in traj.steps]
        assert ids == list(range(1, len(ids) + 1)), ids


def test_tool_calls_correlate_with_observations():
    data = sim.load_acme_data()
    for traj in sim.build_release_risk("1.0", data):
        for step in traj.steps:
            if not step.tool_calls:
                continue
            call_ids = {tc.tool_call_id for tc in step.tool_calls}
            if step.observation:
                obs_ids = {r.source_call_id for r in step.observation.results}
                assert obs_ids <= call_ids, (step.step_id, obs_ids, call_ids)


def test_gps_variant_is_cheaper_and_shorter():
    data = sim.load_acme_data()
    no_gps, with_gps = sim.build_release_risk("1.0", data)
    # The whole point: no-GPS costs materially more.
    assert len(no_gps.steps) > len(with_gps.steps)
    assert no_gps.final_metrics.total_prompt_tokens > with_gps.final_metrics.total_prompt_tokens
    assert no_gps.final_metrics.total_cost_usd > with_gps.final_metrics.total_cost_usd


def test_tool_definitions_diverge():
    data = sim.load_acme_data()
    no_gps, with_gps = sim.build_release_risk("1.0", data)
    no_gps_fns = {t["function"]["name"] for t in no_gps.agent.tool_definitions}
    with_gps_fns = {t["function"]["name"] for t in with_gps.agent.tool_definitions}
    assert "release_risk_summary" in with_gps_fns
    assert "release_risk_summary" not in no_gps_fns


def test_unknown_release_raises():
    data = sim.load_acme_data()
    try:
        sim.build_release_risk("9.9", data)
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown release")


# --- End-to-end: write + validate + scan ------------------------------------


def test_written_trajectories_pass_harbor_validator():
    data = sim.load_acme_data()
    no_gps, with_gps = sim.build_release_risk("1.0", data)
    with tempfile.TemporaryDirectory() as tmp:
        job_dir = sim.write_job(Path(tmp), "release-risk", [no_gps, with_gps])
        traj_files = sorted(job_dir.glob("*/agent/trajectory.json"))
        assert len(traj_files) == 2
        for p in traj_files:
            validate_trajectory(str(p))  # raises on invalid
            # sanity: valid JSON with the expected schema version
            doc = json.loads(p.read_text())
            assert doc["schema_version"] == "ATIF-v1.7"


def test_job_tree_recognized_by_scanner():
    data = sim.load_acme_data()
    no_gps, with_gps = sim.build_release_risk("1.0", data)
    with tempfile.TemporaryDirectory() as tmp:
        sim.write_job(Path(tmp), "release-risk", [no_gps, with_gps])
        scanner = JobScanner(Path(tmp))
        jobs = scanner.list_jobs()
        assert jobs == ["release-risk"], jobs
        trials = scanner.list_trials("release-risk")
        assert set(trials) == {"release-risk-no-gps", "release-risk-with-gps"}, trials
        assert scanner.get_job_config("release-risk") is not None
        assert scanner.get_job_result("release-risk") is not None
        for t in trials:
            assert scanner.get_trial_result("release-risk", t) is not None


# --- Standalone runner ------------------------------------------------------


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
