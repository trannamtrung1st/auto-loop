"""§45 smoke evidence checks against deterministic lifecycle fixtures."""

from auto_loop.exits import ExitCode
from auto_loop.live_smoke import collect_smoke_evidence, section_45_check_notes
from tests.integration.scenario_harness import run_lifecycle
from auto_loop.providers.scripted import ScriptedProvider
from auto_loop.runtime import load_lifecycle_state
from auto_loop.terminal_records import load_completion_record
from tests.integration.scenario_harness import (
    approve_plan,
    batch_worker_payload,
    commit_file,
    make_repo,
    run_opts,
)


def test_section_45_notes_empty_on_scripted_complete_lifecycle(tmp_path):
    repo = make_repo(tmp_path)
    provider = ScriptedProvider()
    approve_plan(repo, provider)
    baseline = load_lifecycle_state(repo).last_approved_commit
    head_b = commit_file(repo, "b.txt", "b\n", "feature B")
    provider.set_response("worker", batch_worker_payload(baseline, head_b))
    provider.set_reviewer_pass("batch", "W01")
    run_lifecycle(repo, run_opts(2), provider)
    mid = load_lifecycle_state(repo)
    provider.set_worker_final_request(head=mid.last_approved_commit)
    provider.set_reviewer_complete(mid.last_approved_commit)
    outcome = run_lifecycle(repo, run_opts(2), provider)
    assert outcome.exit_code == ExitCode.COMPLETE
    assert load_completion_record(repo) is not None
    notes = section_45_check_notes(repo)
    assert notes == []
    evidence = collect_smoke_evidence(repo, outcome.exit_code)
    assert evidence.notes == []
