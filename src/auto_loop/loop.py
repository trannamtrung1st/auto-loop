"""Mechanical worker/reviewer lifecycle loop."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from auto_loop.config import AutoLoopConfig
from auto_loop.context_manifest import load_context_file, render_resource_manifest, validate_context
from auto_loop.exits import ExitCode
from auto_loop.git import (
    GitProtocolError,
    assert_approved_baseline_ancestry,
    head_commit,
    normalize_batch_range,
    resolve_commit,
)
from auto_loop.instructions import compose_role_instructions
from auto_loop.atomic_io import atomic_write_text
from auto_loop.lifecycle import (
    ActiveReview,
    InflightMarker,
    LifecycleState,
    LifecycleStatus,
    create_lifecycle,
    new_lifecycle_id,
    utc_now,
)
from auto_loop.models import ReviewerResult
from auto_loop.product_state import assert_clean_product_tree, is_product_tree_clean
from auto_loop.prompts import TurnContext, build_reviewer_prompt, build_worker_prompt
from auto_loop.protocol import ProtocolParseError, parse_reviewer_result, parse_worker_result
from auto_loop.stop_control import RunStopController, clear_active_run, persist_stopped_state, register_active_run
from auto_loop.protection import (
    ProtectionViolationError,
    ReviewMutationError,
    assert_protected_unchanged,
    assert_reviewer_product_unchanged,
    capture_product_fingerprint,
    capture_protected_baseline,
)
from auto_loop.providers.base import AgentRequest
from auto_loop.providers.cursor import SessionError, build_cursor_command, parse_cursor_stream
from auto_loop.providers.fake_cursor import FakeCursorError
from auto_loop.providers.supervision import ProviderError
from auto_loop.review_store import next_review_sequence, review_artifact_path
from auto_loop.reviews import render_review_markdown
from auto_loop.console_output import RunConsole
from auto_loop.events import append_event
from auto_loop.limits import update_worker_no_progress, worker_progress_key
from auto_loop.run_options import RunOptions
from auto_loop.turn_logs import TurnLogWriter, prune_run_history
from auto_loop.run_prerequisites import RunPreconditionError, ensure_run_prerequisites
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.terminal_records import (
    BlockedRecord,
    CompletionRecord,
    IDEMPOTENT_COMPLETE_MESSAGE,
    assert_completion_inputs_unchanged,
    load_completion_record,
    save_blocked_record,
    save_completion_record,
    task_and_plan_hashes,
)


class ProviderInvoker(Protocol):
    def prepare(self, role: str) -> None: ...

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]: ...


@dataclass
class RunOutcome:
    exit_code: ExitCode
    state: LifecycleState | None = None
    message: str | None = None


class LifecycleRunner:
    def __init__(
        self,
        repo: Path,
        config: AutoLoopConfig,
        options: RunOptions,
        invoker: ProviderInvoker,
        *,
        initial_lifecycle_id: str | None = None,
    ) -> None:
        self.repo = repo
        self.config = config
        self.options = options
        self.invoker = invoker
        self._initial_lifecycle_id = initial_lifecycle_id
        self._dirty_batch_attempts = 0
        self._terminal_exit: ExitCode | None = None
        self._terminal_message: str | None = None
        console_level = (
            "quiet"
            if options.quiet
            else ("verbose" if options.verbose else options.console_level)
        )
        self._console = RunConsole(console_level)
        self._run_started_mono = time.monotonic()
        self._stop = RunStopController(repo)
        self._limit_reason: str | None = None

    def _load_or_create_state(self) -> LifecycleState:
        state = load_lifecycle_state(self.repo)
        if state is None:
            lifecycle_id = self._initial_lifecycle_id or new_lifecycle_id()
            state = create_lifecycle(head_commit(self.repo), lifecycle_id=lifecycle_id)
            save_lifecycle_state(self.repo, state)
        return state

    def _reconcile_stale_inflight(self, state: LifecycleState) -> None:
        if state.inflight is None:
            return
        state.next_actor = state.inflight.actor
        save_lifecycle_state(self.repo, state)

    def _turn_interrupted(self, state: LifecycleState, role: str) -> bool:
        return state.inflight is not None and state.inflight.actor == role

    def _persist_inflight(self, state: LifecycleState, role: str) -> None:
        session_id = state.sessions[role].session_id or "pending"
        state.inflight = InflightMarker(
            actor=role,
            turn=state.turn,
            session_id=session_id,
            started_at=utc_now(),
            head_before=head_commit(self.repo),
        )
        save_lifecycle_state(self.repo, state)

    def _clear_inflight(self, state: LifecycleState) -> None:
        state.inflight = None

    def _runtime_exceeded(self) -> bool:
        limit_seconds = self.options.max_runtime_minutes * 60
        return (time.monotonic() - self._run_started_mono) >= limit_seconds

    def _mark_limit_reached(self, state: LifecycleState, reason: str) -> None:
        state.status = LifecycleStatus.LIMIT_REACHED
        state.inflight = None
        state.updated_at = utc_now()
        self._clear_inflight(state)
        save_lifecycle_state(self.repo, state)
        append_event(
            self.repo,
            self.config,
            {
                "type": "lifecycle_limit_reached",
                "reason": reason,
                "lifecycle_id": state.lifecycle_id,
            },
        )
        self._limit_reason = reason
        self._terminal_exit = ExitCode.LIMIT_REACHED
        self._terminal_message = reason

    def _handle_stop_requested(self, state: LifecycleState) -> bool:
        if not self._stop.requested:
            return False
        persist_stopped_state(self.repo, state)
        append_event(
            self.repo,
            self.config,
            {"type": "lifecycle_stopped", "lifecycle_id": state.lifecycle_id},
        )
        self._terminal_exit = ExitCode.STOPPED
        self._terminal_message = "Lifecycle stopped"
        return True

    def _protocol_repair_or_fail(self, state: LifecycleState, role: str) -> bool:
        """Return True to retry the same role turn with a repair prompt."""
        state.consecutive_protocol_failures += 1
        if state.consecutive_protocol_failures > self.config.limits.protocol_retries:
            return False
        append_event(
            self.repo,
            self.config,
            {
                "type": "protocol_repair",
                "actor": role,
                "attempt": state.consecutive_protocol_failures,
                "lifecycle_id": state.lifecycle_id,
            },
        )
        state.updated_at = utc_now()
        save_lifecycle_state(self.repo, state)
        return True

    def _context_manifest(self, role: str) -> str:
        document = load_context_file(self.repo / self.config.context_file)
        validation = validate_context(self.repo, document)
        if not validation.ok_for_run:
            raise RunPreconditionError("context.yaml failed validation")
        return render_resource_manifest(document, "worker" if role == "worker" else "reviewer")

    def _latest_review_path(self) -> str | None:
        reviews = sorted((self.repo / self.config.reviews_dir).glob("*.md"))
        if not reviews:
            return None
        rel = reviews[-1].relative_to(self.repo)
        return str(rel)

    def _invoke_role(self, role: str, prompt: str, state: LifecycleState) -> str:
        session = state.sessions[role]
        model = self.options.worker_model if role == "worker" else self.options.reviewer_model
        mode = self.config.agents[role].mode
        request = AgentRequest(
            role=role,
            workspace=self.repo,
            prompt=prompt,
            model=model,
            mode=mode,
            timeout_seconds=self.config.limits.agent_timeout_seconds,
            idle_timeout_seconds=self.config.limits.agent_idle_timeout_seconds,
            extra_args=[],
        )
        use_live_cursor = getattr(self.invoker, "uses_live_cursor", False)
        argv = build_cursor_command(
            self.config,
            request,
            binary=None if use_live_cursor else "fake-agent",
            resume_session_id=session.session_id,
        )
        os.environ["AUTO_LOOP_FAKE_ROLE"] = role
        self._persist_inflight(state, role)
        if session.session_id:
            self._console.session_resumed(role, session.session_id)
        self._console.turn_started(state.turn, role)
        turn_log = TurnLogWriter(
            self.repo,
            self.config,
            state.lifecycle_id,
            state.turn,
            role,
        )
        self.invoker.prepare(role)
        max_attempts = 1 + max(self.config.limits.provider_retries, 0)
        parsed = None
        for attempt in range(1, max_attempts + 1):
            try:
                _code, lines = self.invoker.invoke(argv)
                turn_log.write_stream_lines(lines)
                parsed = parse_cursor_stream(
                    lines,
                    expected_session_id=session.session_id,
                )
                break
            except SessionError:
                turn_log.finalize()
                raise
            except FakeCursorError as exc:
                if attempt >= max_attempts:
                    turn_log.finalize()
                    raise ProviderError(str(exc)) from exc
                append_event(
                    self.repo,
                    self.config,
                    {
                        "type": "provider_retry",
                        "actor": role,
                        "attempt": attempt,
                        "lifecycle_id": state.lifecycle_id,
                    },
                )
        if parsed is None:
            turn_log.finalize()
            raise ProviderError(f"Provider failed for role {role}")
        turn_log.finalize()
        if session.session_id is None:
            session.session_id = parsed.session_id
            append_event(
                self.repo,
                self.config,
                {
                    "type": "session_created",
                    "actor": role,
                    "session_id": parsed.session_id,
                    "lifecycle_id": state.lifecycle_id,
                },
            )
            self._console.session_created(role, parsed.session_id)
        return parsed.final_text

    def _assert_final_complete_valid(
        self,
        state: LifecycleState,
        result: ReviewerResult,
    ) -> None:
        current_head = head_commit(self.repo)
        if current_head != state.last_approved_commit:
            raise GitProtocolError(
                "COMPLETE requires HEAD to equal last_approved_commit"
            )
        if result.reviewed_head_commit and resolve_commit(self.repo, result.reviewed_head_commit) != current_head:
            raise GitProtocolError("COMPLETE reviewed_head_commit does not match current HEAD")
        assert_clean_product_tree(self.repo)

    def _worker_turn(self, state: LifecycleState) -> None:
        protocol_repair = False
        while True:
            first = state.sessions["worker"].session_id is None
            instruction_stack = compose_role_instructions(
                self.repo, self.config, "worker", first_invocation=first
            )
            ctx = TurnContext(
                task_path=self.config.task_file,
                plan_path=self.config.plan_file,
                latest_review_path=self._latest_review_path(),
                head_commit=head_commit(self.repo),
                product_clean=is_product_tree_clean(self.repo),
                interrupted=self._turn_interrupted(state, "worker"),
                protocol_repair=protocol_repair,
                resource_manifest=self._context_manifest("worker"),
            )
            prompt = (
                instruction_stack + "\n\n" + build_worker_prompt(state, ctx)
                if instruction_stack
                else build_worker_prompt(state, ctx)
            )
            protected = capture_protected_baseline(self.repo, self.config)
            final_text = self._invoke_role("worker", prompt, state)
            assert_protected_unchanged(self.repo, self.config, protected)
            try:
                result = parse_worker_result(final_text)
            except ProtocolParseError:
                if self._protocol_repair_or_fail(state, "worker"):
                    protocol_repair = True
                    continue
                raise
            break
        state.consecutive_protocol_failures = 0

        if result.status == "blocked":
            append_event(
                self.repo,
                self.config,
                {"type": "worker_blocked", "turn": state.turn, "lifecycle_id": state.lifecycle_id},
            )
            state.active_review = ActiveReview(
                scope="batch",
                target="blocked",
                summary=result.work_summary,
            )
            state.next_actor = "reviewer"
            state.turn += 1
            state.updated_at = utc_now()
            self._clear_inflight(state)
            save_lifecycle_state(self.repo, state)
            return

        if not state.plan_approved:
            if result.review is None or result.review.scope != "plan":
                raise GitProtocolError("Worker must request plan review before implementation")
            if head_commit(self.repo) != state.initial_base_commit:
                raise GitProtocolError("Product HEAD must remain at initial baseline before plan PASS")
            assert_clean_product_tree(self.repo)

        elif result.review and result.review.scope == "final":
            if not state.plan_approved:
                raise GitProtocolError("Final review requires an approved plan")
            assert_clean_product_tree(self.repo)
            current_head = head_commit(self.repo)
            if current_head != state.last_approved_commit:
                state.next_actor = "worker"
                state.turn += 1
                state.updated_at = utc_now()
                self._clear_inflight(state)
                save_lifecycle_state(self.repo, state)
                return

        elif result.review and result.review.scope == "batch":
            try:
                assert_clean_product_tree(self.repo)
            except GitProtocolError:
                self._dirty_batch_attempts += 1
                if self._dirty_batch_attempts > self.config.limits.protocol_retries:
                    raise
                state.worker_no_progress_streak = 0
                state.last_worker_progress_key = None
                state.next_actor = "worker"
                state.turn += 1
                state.updated_at = utc_now()
                self._clear_inflight(state)
                save_lifecycle_state(self.repo, state)
                return
            normalized = normalize_batch_range(
                self.repo,
                last_approved_commit=state.last_approved_commit,
                worker_base_commit=result.review.base_commit,
                worker_head_commit=result.review.head_commit,
            )
            result.review.base_commit = normalized.range.base
            result.review.head_commit = normalized.range.head

        if result.review:
            append_event(
                self.repo,
                self.config,
                {
                    "type": "review_requested",
                    "scope": result.review.scope,
                    "target": result.review.target,
                    "turn": state.turn,
                    "lifecycle_id": state.lifecycle_id,
                },
            )
            self._console.review_requested(result.review.scope, result.review.target)
            state.active_review = ActiveReview(
                scope=result.review.scope,
                target=result.review.target,
                summary=result.review.summary,
                base_commit=result.review.base_commit,
                head_commit=result.review.head_commit,
                worker_summary=result.work_summary,
            )
            state.next_actor = "reviewer"
        current_head = head_commit(self.repo)
        progress_key = worker_progress_key(
            self.repo, self.config, state, result, current_head
        )
        if update_worker_no_progress(state, self.config, progress_key):
            self._mark_limit_reached(state, "worker_no_progress")
            return
        state.turn += 1
        state.updated_at = utc_now()
        self._clear_inflight(state)
        save_lifecycle_state(self.repo, state)

    def _review_kind(self, review: ActiveReview, result: ReviewerResult) -> str:
        if result.scope == "final" or review.scope == "final":
            return "final"
        if review.target == "blocked":
            return "batch"
        return "plan" if result.scope == "plan" else "batch"

    def _reviewer_turn(self, state: LifecycleState) -> None:
        if state.active_review is None:
            raise GitProtocolError("Reviewer invoked without active review")
        protocol_repair = False
        while True:
            first = state.sessions["reviewer"].session_id is None
            instruction_stack = compose_role_instructions(
                self.repo, self.config, "reviewer", first_invocation=first
            )
            ctx = TurnContext(
                task_path=self.config.task_file,
                plan_path=self.config.plan_file,
                latest_review_path=self._latest_review_path(),
                head_commit=head_commit(self.repo),
                product_clean=is_product_tree_clean(self.repo),
                interrupted=self._turn_interrupted(state, "reviewer"),
                protocol_repair=protocol_repair,
                resource_manifest=self._context_manifest("reviewer"),
            )
            body = build_reviewer_prompt(state, ctx, state.active_review)
            prompt = instruction_stack + "\n\n" + body if instruction_stack else body
            protected = capture_protected_baseline(self.repo, self.config)
            product_before = capture_product_fingerprint(self.repo)
            final_text = self._invoke_role("reviewer", prompt, state)
            assert_protected_unchanged(self.repo, self.config, protected)
            product_after = capture_product_fingerprint(self.repo)
            assert_reviewer_product_unchanged(product_before, product_after)
            try:
                result = parse_reviewer_result(final_text)
            except ProtocolParseError:
                if self._protocol_repair_or_fail(state, "reviewer"):
                    protocol_repair = True
                    continue
                raise
            break
        state.consecutive_protocol_failures = 0

        sequence = next_review_sequence(self.repo, self.config)
        slug = f"{result.scope}-{result.target}"
        path = review_artifact_path(self.repo, self.config, sequence=sequence, slug=slug)
        kind = self._review_kind(state.active_review, result)
        markdown = render_review_markdown(
            sequence=sequence,
            title=slug,
            kind=kind,
            worker=None,
            reviewer=result,
            worker_session_id=state.sessions["worker"].session_id,
            reviewer_session_id=state.sessions["reviewer"].session_id,
        )
        atomic_write_text(path, markdown)
        review_rel = str(path.relative_to(self.repo))

        if result.verdict == "blocked":
            save_blocked_record(
                self.repo,
                BlockedRecord(
                    blocked_at=datetime.now(timezone.utc),
                    lifecycle_id=state.lifecycle_id,
                    turn=state.turn,
                    worker_session_id=state.sessions["worker"].session_id,
                    reviewer_session_id=state.sessions["reviewer"].session_id,
                    summary=result.summary,
                    review_file=review_rel,
                ),
            )
            state.status = LifecycleStatus.BLOCKED
            state.active_review = None
            state.updated_at = utc_now()
            self._clear_inflight(state)
            save_lifecycle_state(self.repo, state)
            append_event(
                self.repo,
                self.config,
                {"type": "lifecycle_blocked", "lifecycle_id": state.lifecycle_id},
            )
            self._console.terminal("Lifecycle blocked by reviewer")
            self._terminal_exit = ExitCode.BLOCKED
            return

        append_event(
            self.repo,
            self.config,
            {
                "type": "review_result",
                "verdict": result.verdict,
                "scope": result.scope,
                "finding_count": len(result.findings),
                "turn": state.turn,
                "lifecycle_id": state.lifecycle_id,
            },
        )
        self._console.review_result(result.verdict, result.scope, len(result.findings))

        if result.verdict == "pass" and result.scope == "plan":
            state.plan_approved = True
        if result.verdict == "pass" and result.scope == "batch":
            new_head = head_commit(self.repo)
            state.last_approved_commit = new_head
            self._dirty_batch_attempts = 0
            append_event(
                self.repo,
                self.config,
                {
                    "type": "baseline_advanced",
                    "head": new_head,
                    "lifecycle_id": state.lifecycle_id,
                },
            )
            self._console.baseline_advanced(new_head)

        if result.verdict == "complete" and result.scope == "final":
            self._assert_final_complete_valid(state, result)
            task_hash, plan_hash = task_and_plan_hashes(self.repo, self.config)
            final_head = head_commit(self.repo)
            save_completion_record(
                self.repo,
                CompletionRecord(
                    completed_at=datetime.now(timezone.utc),
                    lifecycle_id=state.lifecycle_id,
                    turn=state.turn,
                    worker_session_id=state.sessions["worker"].session_id,
                    reviewer_session_id=state.sessions["reviewer"].session_id,
                    initial_base_commit=state.initial_base_commit,
                    final_commit=final_head,
                    last_approved_commit=state.last_approved_commit,
                    final_review_file=review_rel,
                    task_sha256=task_hash,
                    plan_sha256=plan_hash,
                ),
            )
            state.status = LifecycleStatus.COMPLETED
            state.active_review = None
            state.updated_at = utc_now()
            self._clear_inflight(state)
            save_lifecycle_state(self.repo, state)
            append_event(
                self.repo,
                self.config,
                {
                    "type": "lifecycle_complete",
                    "head": final_head,
                    "lifecycle_id": state.lifecycle_id,
                },
            )
            self._console.terminal("Lifecycle completed successfully")
            self._terminal_exit = ExitCode.COMPLETE
            return

        state.active_review = None
        state.next_actor = "worker"
        state.turn += 1
        state.updated_at = utc_now()
        self._clear_inflight(state)
        save_lifecycle_state(self.repo, state)

    def run(self) -> RunOutcome:
        self._stop.install()
        try:
            return self._run_loop()
        finally:
            self._stop.restore()
            clear_active_run(self.repo)

    def _run_loop(self) -> RunOutcome:
        state = self._load_or_create_state()
        register_active_run(self.repo, state.lifecycle_id)
        prune_run_history(self.repo, self.config, state.lifecycle_id)
        append_event(
            self.repo,
            self.config,
            {"type": "lifecycle_started", "lifecycle_id": state.lifecycle_id},
        )
        self._console.lifecycle_started(state.lifecycle_id)
        self._reconcile_stale_inflight(state)
        state = load_lifecycle_state(self.repo) or state
        if state.inflight is not None:
            append_event(
                self.repo,
                self.config,
                {
                    "type": "inflight_resume",
                    "actor": state.inflight.actor,
                    "turn": state.inflight.turn,
                    "lifecycle_id": state.lifecycle_id,
                },
            )
        turns = 0
        try:
            while turns < self.options.max_turns:
                if self._terminal_exit is not None:
                    break
                state = load_lifecycle_state(self.repo) or state
                if self._handle_stop_requested(state):
                    break
                if self._runtime_exceeded():
                    self._mark_limit_reached(state, "max_runtime_minutes")
                    break
                turns += 1
                assert_approved_baseline_ancestry(self.repo, state.last_approved_commit)
                if state.next_actor == "worker":
                    self._worker_turn(state)
                else:
                    self._reviewer_turn(state)
                state = load_lifecycle_state(self.repo) or state
                if self._terminal_exit is not None:
                    break
        except GitProtocolError:
            return RunOutcome(
                exit_code=ExitCode.GIT_PROTOCOL_ERROR,
                state=load_lifecycle_state(self.repo),
            )
        except SessionError as exc:
            return RunOutcome(
                exit_code=ExitCode.SESSION_ERROR,
                state=load_lifecycle_state(self.repo),
                message=str(exc),
            )
        except ProviderError as exc:
            return RunOutcome(
                exit_code=exc.exit_code,
                state=load_lifecycle_state(self.repo),
                message=str(exc),
            )
        except ProtectionViolationError as exc:
            return RunOutcome(
                exit_code=exc.exit_code,
                state=load_lifecycle_state(self.repo),
                message=str(exc),
            )
        except ReviewMutationError as exc:
            return RunOutcome(
                exit_code=exc.exit_code,
                state=load_lifecycle_state(self.repo),
                message=str(exc),
            )
        except ProtocolParseError as exc:
            return RunOutcome(
                exit_code=ExitCode.PROTOCOL_ERROR,
                state=load_lifecycle_state(self.repo),
                message=str(exc),
            )
        if self._terminal_exit is not None:
            return RunOutcome(
                exit_code=self._terminal_exit,
                state=state,
                message=self._terminal_message,
            )
        if state.status != LifecycleStatus.LIMIT_REACHED:
            self._mark_limit_reached(state, "max_turns")
        return RunOutcome(
            exit_code=ExitCode.LIMIT_REACHED,
            state=state,
            message=self._limit_reason or "max_turns",
        )


def _check_idempotent_completion(repo: Path, config: AutoLoopConfig) -> RunOutcome | None:
    record = load_completion_record(repo)
    if record is None:
        return None
    assert_completion_inputs_unchanged(repo, config, record)
    state = load_lifecycle_state(repo)
    return RunOutcome(
        exit_code=ExitCode.COMPLETE,
        state=state,
        message=IDEMPOTENT_COMPLETE_MESSAGE,
    )


def run_lifecycle(
    repo: Path,
    options: RunOptions,
    invoker: ProviderInvoker,
) -> RunOutcome:
    from auto_loop.locking import acquire_workspace_lock

    config = ensure_run_prerequisites(repo)
    idempotent = _check_idempotent_completion(repo, config)
    if idempotent is not None:
        return idempotent
    existing = load_lifecycle_state(repo)
    lifecycle_id = existing.lifecycle_id if existing else new_lifecycle_id()
    lock = acquire_workspace_lock(repo, lifecycle_id)
    try:
        runner = LifecycleRunner(
            repo,
            config,
            options,
            invoker,
            initial_lifecycle_id=lifecycle_id if existing is None else None,
        )
        return runner.run()
    finally:
        lock.release()
