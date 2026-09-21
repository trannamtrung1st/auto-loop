"""Mechanical planner/worker/reviewer lifecycle loop."""

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
    resolve_commit,
)
from auto_loop.instructions import compose_role_instructions
from auto_loop.atomic_io import atomic_write_text
from auto_loop.lifecycle import (
    ActiveReview,
    InflightMarker,
    LifecycleState,
    LifecycleStatus,
    PendingRevision,
    SessionSlot,
    adopt_session_identity,
    create_lifecycle,
    next_cycle_id,
    new_lifecycle_id,
    utc_now,
)
from auto_loop.models import (
    ROLE_FOR_SLOT,
    ReviewerResult,
    Role,
    WorkerResult,
)
from auto_loop.product_state import assert_clean_product_tree, is_product_tree_clean
from auto_loop.prompts import TurnContext, build_planner_prompt, build_reviewer_prompt, build_worker_prompt
from auto_loop.protocol import (
    ProtocolParseError,
    missing_pass_targets,
    parse_planner_result,
    parse_reviewer_result,
    parse_worker_result,
)
from auto_loop.stop_control import RunStopController, clear_active_run, persist_stopped_state, register_active_run
from auto_loop.protection import (
    ProtectionViolationError,
    ReviewMutationError,
    assert_protected_unchanged,
    assert_review_snapshot_unchanged,
    capture_protected_baseline,
    capture_review_snapshot,
)
from auto_loop.providers.base import AgentRequest
from auto_loop.providers.cursor import (
    CursorStreamParseResult,
    SessionError,
    StreamParseError,
    build_cursor_command,
    parse_cursor_stream,
)
from auto_loop.providers.fake_cursor import FakeCursorError
from auto_loop.providers.supervision import (
    ProviderAttemptResult,
    ProviderError,
    ProviderFailureKind,
    is_retryable_provider_failure,
)
from auto_loop.review_store import next_review_sequence, review_artifact_path
from auto_loop.review_targets import (
    normalize_work_targets,
    plan_path_target,
    sha256_file,
)
from auto_loop.reviews import render_review_markdown
from auto_loop.console_output import RunConsole
from auto_loop.events import append_event
from auto_loop.limits import update_worker_no_progress, worker_progress_key
from auto_loop.run_options import RunOptions
from auto_loop.turn_logs import TurnLogWriter, prune_run_history
from auto_loop.run_inputs import RunInputs
from auto_loop.run_prerequisites import RunPreconditionError, ensure_run_prerequisites
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.terminal_records import (
    BlockedRecord,
    CompletionRecord,
    IDEMPOTENT_BLOCKED_MESSAGE,
    IDEMPOTENT_COMPLETE_MESSAGE,
    assert_completion_inputs_unchanged,
    load_blocked_record,
    load_completion_record,
    save_blocked_record,
    save_completion_record,
    task_and_plan_hashes,
)


INTERRUPTED_MESSAGE = """Run interrupted.

Your state was saved.
Resume with:
  auto-loop resume"""


class ProviderInvoker(Protocol):
    def prepare(self, role: str) -> None: ...

    def invoke(self, argv: list[str]) -> ProviderAttemptResult: ...


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
        state.next_session = state.inflight.session_slot
        save_lifecycle_state(self.repo, state)

    def _turn_interrupted(self, state: LifecycleState, slot: SessionSlot) -> bool:
        return state.inflight is not None and state.inflight.session_slot == slot

    def _persist_inflight(self, state: LifecycleState, slot: SessionSlot) -> None:
        session = state.sessions[slot]
        session_id = session.session_id or "pending"
        state.inflight = InflightMarker(
            session_slot=slot,
            role=ROLE_FOR_SLOT[slot],
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
        self._terminal_message = INTERRUPTED_MESSAGE
        return True

    def _protocol_repair_or_fail(self, state: LifecycleState, slot: str) -> bool:
        """Return True to retry the same slot turn with a repair prompt."""
        state.consecutive_protocol_failures += 1
        if state.consecutive_protocol_failures > self.config.limits.protocol_retries:
            return False
        append_event(
            self.repo,
            self.config,
            {
                "type": "protocol_repair",
                "actor": ROLE_FOR_SLOT[slot],  # type: ignore[index]
                "session_purpose": slot,
                "attempt": state.consecutive_protocol_failures,
                "lifecycle_id": state.lifecycle_id,
            },
        )
        state.updated_at = utc_now()
        save_lifecycle_state(self.repo, state)
        return True

    def _context_manifest(self, role: Role) -> str:
        document = load_context_file(self.repo / self.config.context_file)
        validation = validate_context(self.repo, document)
        if not validation.ok_for_run:
            raise RunPreconditionError("context.yaml failed validation")
        return render_resource_manifest(document, role)

    def _latest_review_path(self) -> str | None:
        reviews = sorted((self.repo / self.config.reviews_dir).glob("*.md"))
        if not reviews:
            return None
        rel = reviews[-1].relative_to(self.repo)
        return str(rel)

    def _plan_hash(self) -> str | None:
        path = self.repo / self.config.plan_file
        if not path.is_file():
            return None
        return sha256_file(path)

    def _resolved_model(self, slot: SessionSlot, state: LifecycleState) -> str:
        session = state.sessions[slot]
        if session.session_id:
            return session.model
        role = ROLE_FOR_SLOT[slot]
        if role == "planner":
            return self.options.planner_model
        if role == "worker":
            return self.options.worker_model
        return self.options.reviewer_model

    def _turn_context(
        self,
        state: LifecycleState,
        slot: SessionSlot,
        *,
        protocol_repair: bool,
    ) -> TurnContext:
        role = ROLE_FOR_SLOT[slot]
        current = self._plan_hash()
        initial = state.initial_approved_plan_sha256
        changed = None
        if initial and current:
            changed = current != initial
        first_execution = (
            state.phase == "execution"
            and state.sessions[slot].session_id is None
            and slot in ("worker", "reviewer")
        )
        return TurnContext(
            task_path=self.config.task_file,
            plan_path=self.config.plan_file,
            latest_review_path=self._latest_review_path(),
            head_commit=head_commit(self.repo),
            product_clean=is_product_tree_clean(self.repo),
            interrupted=self._turn_interrupted(state, slot),
            protocol_repair=protocol_repair,
            resource_manifest=self._context_manifest(role),
            session_purpose=slot,
            phase=state.phase,
            initial_plan_sha256=initial,
            current_plan_sha256=current,
            plan_changed_since_approval=changed,
            first_execution_turn=first_execution,
            pending_revision=state.pending_revision,
        )

    def _parse_provider_attempt(
        self,
        attempt: ProviderAttemptResult,
        stored_session_id: str | None,
    ) -> CursorStreamParseResult:
        if attempt.failure is None and attempt.parsed is not None:
            return attempt.parsed
        expected = stored_session_id
        strict = expected is not None
        return parse_cursor_stream(
            attempt.lines,
            expected_session_id=expected,
            fail_on_malformed=strict,
        )

    def _invoke_slot(self, slot: SessionSlot, prompt: str, state: LifecycleState) -> str:
        session = state.sessions[slot]
        role = ROLE_FOR_SLOT[slot]
        model = self._resolved_model(slot, state)
        mode = self.config.agents[role].mode
        request = AgentRequest(
            role=role,
            session_purpose=slot,
            workspace=self.repo,
            prompt=prompt,
            model=model,
            mode=mode,
            timeout_seconds=self.config.limits.agent_timeout_seconds,
            idle_timeout_seconds=self.config.limits.agent_idle_timeout_seconds,
            extra_args=[],
        )
        use_live_cursor = getattr(self.invoker, "uses_live_cursor", False)
        os.environ["AUTO_LOOP_FAKE_ROLE"] = slot
        os.environ["AUTO_LOOP_FAKE_SLOT"] = slot
        self._persist_inflight(state, slot)
        if session.session_id:
            self._console.session_resumed(slot, session.session_id)
        self._console.turn_started(state.turn, slot)
        turn_log = TurnLogWriter(
            self.repo,
            self.config,
            state.lifecycle_id,
            state.turn,
            slot,
        )
        self.invoker.prepare(slot)
        if hasattr(self.invoker, "stop_check"):
            self.invoker.stop_check = lambda: self._stop.requested
        if hasattr(self.invoker, "on_provider_pid"):

            def _provider_pid(pid: int | None) -> None:
                self._stop.set_active_provider(pid)
                register_active_run(self.repo, state.lifecycle_id, provider_pid=pid)

            self.invoker.on_provider_pid = _provider_pid
        max_attempts = 1 + max(self.config.limits.provider_retries, 0)
        final_text: str | None = None
        try:
            for attempt in range(1, max_attempts + 1):
                argv = build_cursor_command(
                    self.config,
                    request,
                    binary=None if use_live_cursor else "fake-agent",
                    resume_session_id=session.session_id,
                )
                try:
                    attempt_result = self.invoker.invoke(argv)
                    turn_log.write_stream_lines(attempt_result.lines)
                    parsed = self._parse_provider_attempt(attempt_result, session.session_id)
                    if parsed.session_id:
                        created = adopt_session_identity(
                            state,
                            slot,
                            parsed.session_id,
                            model,
                        )
                        save_lifecycle_state(self.repo, state)
                        if created:
                            append_event(
                                self.repo,
                                self.config,
                                {
                                    "type": "session_created",
                                    "actor": role,
                                    "session_purpose": slot,
                                    "session_id": parsed.session_id,
                                    "model": model,
                                    "lifecycle_id": state.lifecycle_id,
                                },
                            )
                            self._console.session_created(slot, parsed.session_id)
                    if attempt_result.failure == ProviderFailureKind.INTERRUPTED:
                        turn_log.finalize()
                        if self._stop.requested:
                            persist_stopped_state(self.repo, state)
                            append_event(
                                self.repo,
                                self.config,
                                {"type": "lifecycle_stopped", "lifecycle_id": state.lifecycle_id},
                            )
                            self._terminal_exit = ExitCode.STOPPED
                            self._terminal_message = INTERRUPTED_MESSAGE
                        raise ProviderError(f"Provider interrupted for session {slot}")
                    if attempt_result.failure is not None:
                        if not is_retryable_provider_failure(attempt_result.failure):
                            turn_log.finalize()
                            raise ProviderError(
                                f"Provider failure for session {slot}: {attempt_result.failure}"
                            )
                        if attempt >= max_attempts:
                            turn_log.finalize()
                            raise ProviderError(
                                f"Provider failed after {max_attempts} attempt(s) for session {slot}: "
                                f"{attempt_result.failure}"
                            )
                    elif parsed.final_text:
                        final_text = parsed.final_text
                        break
                    elif attempt >= max_attempts:
                        turn_log.finalize()
                        raise ProviderError(
                            f"Provider stream for session {slot} missing terminal result"
                        )
                except SessionError:
                    turn_log.finalize()
                    raise
                except StreamParseError:
                    if attempt >= max_attempts:
                        turn_log.finalize()
                        raise ProviderError(
                            f"Provider stream for session {slot} malformed after retries"
                        ) from None
                except ProviderError:
                    turn_log.finalize()
                    if self._stop.requested:
                        persist_stopped_state(self.repo, state)
                        append_event(
                            self.repo,
                            self.config,
                            {"type": "lifecycle_stopped", "lifecycle_id": state.lifecycle_id},
                        )
                        self._terminal_exit = ExitCode.STOPPED
                        self._terminal_message = INTERRUPTED_MESSAGE
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
                        "session_purpose": slot,
                        "attempt": attempt,
                        "lifecycle_id": state.lifecycle_id,
                    },
                )
        finally:
            if hasattr(self.invoker, "on_provider_pid"):
                self.invoker.on_provider_pid(None)
            self._stop.set_active_provider(None)
        if final_text is None:
            turn_log.finalize()
            raise ProviderError(f"Provider failed for session {slot}")
        turn_log.finalize()
        return final_text

    def _assert_planning_clean(self, state: LifecycleState) -> None:
        if head_commit(self.repo) != state.initial_base_commit:
            raise GitProtocolError("Product HEAD must remain at initial baseline before plan PASS")
        assert_clean_product_tree(self.repo)

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

    def _assert_pass_targets(self, review: ActiveReview, result: ReviewerResult) -> None:
        if result.verdict != "pass":
            return
        required = review.target_ids
        if required and not set(required) <= set(result.reviewed_target_ids):
            raise missing_pass_targets(required, result.reviewed_target_ids)

    def _planning_review_cycle(self, state: LifecycleState, target: str) -> tuple[str, int]:
        pending = state.pending_revision
        if pending and pending.scope == "plan" and pending.target == target:
            return pending.cycle_id, pending.round
        return next_cycle_id(state), 1

    def _assert_reviewer_matches_active(
        self,
        active: ActiveReview,
        result: ReviewerResult,
    ) -> None:
        if result.scope != active.scope:
            raise GitProtocolError(
                f"Reviewer scope {result.scope!r} does not match active review {active.scope!r}"
            )
        if result.target != active.target:
            raise GitProtocolError(
                f"Reviewer target {result.target!r} does not match active review {active.target!r}"
            )
        if not active.has_git_target:
            return
        expected_base = active.git_base
        expected_head = active.git_head or active.current_candidate_head
        if result.reviewed_base_commit is not None and expected_base is not None:
            if resolve_commit(self.repo, result.reviewed_base_commit) != resolve_commit(
                self.repo, expected_base
            ):
                raise GitProtocolError(
                    "Reviewer reviewed_base_commit does not match active Git review"
                )
        if result.reviewed_head_commit is not None and expected_head is not None:
            if resolve_commit(self.repo, result.reviewed_head_commit) != resolve_commit(
                self.repo, expected_head
            ):
                raise GitProtocolError(
                    "Reviewer reviewed_head_commit does not match active Git review"
                )

    def _persist_after_turn(self, state: LifecycleState) -> None:
        state.turn += 1
        state.updated_at = utc_now()
        self._clear_inflight(state)
        save_lifecycle_state(self.repo, state)

    def _assert_planning_slot_active(self, state: LifecycleState, slot: SessionSlot) -> None:
        if slot not in ("planner", "plan_reviewer"):
            return
        if state.phase != "planning":
            raise GitProtocolError(f"Cannot invoke {slot} during execution phase")
        if state.sessions[slot].status == "retired":
            raise GitProtocolError(f"Planning session {slot} is retired and cannot be resumed")

    def _planner_turn(self, state: LifecycleState) -> None:
        self._assert_planning_slot_active(state, "planner")
        protocol_repair = False
        while True:
            first = state.sessions["planner"].session_id is None
            instruction_stack = compose_role_instructions(
                self.repo, self.config, "planner", first_invocation=first
            )
            ctx = self._turn_context(state, "planner", protocol_repair=protocol_repair)
            body = build_planner_prompt(state, ctx)
            prompt = instruction_stack + "\n\n" + body if instruction_stack else body
            protected = capture_protected_baseline(self.repo, self.config)
            final_text = self._invoke_slot("planner", prompt, state)
            assert_protected_unchanged(self.repo, self.config, protected)
            try:
                result = parse_planner_result(final_text)
            except ProtocolParseError:
                if self._protocol_repair_or_fail(state, "planner"):
                    protocol_repair = True
                    continue
                raise
            break
        state.consecutive_protocol_failures = 0
        self._assert_planning_clean(state)

        if result.status == "blocked":
            append_event(
                self.repo,
                self.config,
                {"type": "planner_blocked", "turn": state.turn, "lifecycle_id": state.lifecycle_id},
            )
            cycle_id, round_no = self._planning_review_cycle(state, "blocked")
            state.active_review = ActiveReview(
                cycle_id=cycle_id,
                round=round_no,
                scope="plan",
                target="blocked",
                summary=result.plan_summary,
                session_purpose="plan_reviewer",
                plan_summary=result.plan_summary,
                plan_sha256=self._plan_hash(),
                targets=[plan_path_target(self.repo, self.config.plan_file)],
            )
            state.next_session = "plan_reviewer"
            self._persist_after_turn(state)
            return

        if result.review is None or result.review.scope != "plan":
            raise GitProtocolError("Planner must request plan review")
        self._assert_planning_clean(state)
        append_event(
            self.repo,
            self.config,
            {
                "type": "review_requested",
                "scope": "plan",
                "target": result.review.target,
                "session_purpose": "plan_reviewer",
                "turn": state.turn,
                "lifecycle_id": state.lifecycle_id,
            },
        )
        self._console.review_requested(result.review.scope, result.review.target)
        cycle_id, round_no = self._planning_review_cycle(state, result.review.target)
        state.active_review = ActiveReview(
            cycle_id=cycle_id,
            round=round_no,
            scope="plan",
            target=result.review.target,
            summary=result.review.summary,
            session_purpose="plan_reviewer",
            plan_summary=result.plan_summary,
            plan_sha256=self._plan_hash(),
            targets=[plan_path_target(self.repo, self.config.plan_file)],
        )
        state.next_session = "plan_reviewer"
        self._persist_after_turn(state)

    def _make_plan_update_review(self, state: LifecycleState, result: WorkerResult) -> ActiveReview:
        assert result.review is not None
        pending = state.pending_revision
        if pending and pending.scope == "plan" and pending.target == result.review.target:
            cycle_id = pending.cycle_id
            round_no = pending.round
        else:
            cycle_id = next_cycle_id(state)
            round_no = 1
        return ActiveReview(
            cycle_id=cycle_id,
            round=round_no,
            scope="plan",
            target=result.review.target,
            summary=result.review.summary,
            session_purpose="reviewer",
            worker_summary=result.work_summary,
            plan_sha256=self._plan_hash(),
            targets=[plan_path_target(self.repo, self.config.plan_file)],
        )

    def _worker_turn(self, state: LifecycleState) -> None:
        protocol_repair = False
        while True:
            first = state.sessions["worker"].session_id is None
            instruction_stack = compose_role_instructions(
                self.repo, self.config, "worker", first_invocation=first
            )
            ctx = self._turn_context(state, "worker", protocol_repair=protocol_repair)
            prompt = (
                instruction_stack + "\n\n" + build_worker_prompt(state, ctx)
                if instruction_stack
                else build_worker_prompt(state, ctx)
            )
            protected = capture_protected_baseline(self.repo, self.config)
            final_text = self._invoke_slot("worker", prompt, state)
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
                cycle_id=next_cycle_id(state),
                scope="batch",
                target="blocked",
                summary=result.work_summary,
                session_purpose="reviewer",
                worker_summary=result.work_summary,
            )
            state.next_session = "reviewer"
            self._persist_after_turn(state)
            return

        if not state.plan_approved:
            raise GitProtocolError("Worker cannot run before initial plan PASS")

        if result.review is None:
            raise GitProtocolError("Worker review_requested requires a review request")

        if result.review.scope == "final":
            assert_clean_product_tree(self.repo)
            current_head = head_commit(self.repo)
            if current_head != state.last_approved_commit:
                state.next_session = "worker"
                self._persist_after_turn(state)
                return
            state.active_review = ActiveReview(
                cycle_id=next_cycle_id(state),
                scope="final",
                target=result.review.target,
                summary=result.review.summary,
                session_purpose="reviewer",
                approved_base_commit=state.last_approved_commit,
                current_candidate_head=current_head,
                worker_summary=result.work_summary,
                plan_sha256=self._plan_hash(),
            )
        elif result.review.scope == "plan":
            state.active_review = self._make_plan_update_review(state, result)
        elif result.review.scope == "batch":
            try:
                assert_clean_product_tree(self.repo)
            except GitProtocolError:
                self._dirty_batch_attempts += 1
                if self._dirty_batch_attempts > self.config.limits.protocol_retries:
                    raise
                state.worker_no_progress_streak = 0
                state.last_worker_progress_key = None
                state.next_session = "worker"
                self._persist_after_turn(state)
                return
            targets, _warnings = normalize_work_targets(
                self.repo,
                last_approved_commit=state.last_approved_commit,
                request=result.review,
            )
            pending = state.pending_revision
            if (
                pending
                and pending.scope == "batch"
                and pending.target == result.review.target
            ):
                cycle_id = pending.cycle_id
                round_no = pending.round
                production_head = pending.production_head_commit
            else:
                cycle_id = next_cycle_id(state)
                round_no = 1
                production_head = head_commit(self.repo)
            git_target = next((t for t in targets if t.kind == "git_range"), None)
            state.active_review = ActiveReview(
                cycle_id=cycle_id,
                round=round_no,
                scope="batch",
                target=result.review.target,
                summary=result.review.summary,
                session_purpose="reviewer",
                approved_base_commit=state.last_approved_commit,
                production_head_commit=production_head,
                current_candidate_head=head_commit(self.repo),
                worker_summary=result.work_summary,
                plan_sha256=self._plan_hash(),
                targets=targets,
            )
            if git_target is not None:
                result.review.base_commit = git_target.base_commit
                result.review.head_commit = git_target.head_commit
        else:
            raise GitProtocolError(f"Unsupported worker review scope: {result.review.scope}")

        append_event(
            self.repo,
            self.config,
            {
                "type": "review_requested",
                "scope": result.review.scope,
                "target": result.review.target,
                "session_purpose": "reviewer",
                "turn": state.turn,
                "lifecycle_id": state.lifecycle_id,
            },
        )
        self._console.review_requested(result.review.scope, result.review.target)
        state.next_session = "reviewer"
        current_head = head_commit(self.repo)
        progress_key = worker_progress_key(
            self.repo, self.config, state, result, current_head
        )
        if update_worker_no_progress(state, self.config, progress_key):
            self._mark_limit_reached(state, "worker_no_progress")
            return
        self._persist_after_turn(state)

    def _review_kind(self, review: ActiveReview, result: ReviewerResult) -> str:
        if result.scope == "final" or review.scope == "final":
            return "final"
        if review.target == "blocked":
            return "batch"
        if review.round > 1:
            return "revision"
        return "plan" if result.scope == "plan" else "batch"

    def _write_review_artifact(
        self,
        state: LifecycleState,
        result: ReviewerResult,
        *,
        implementer_slot: SessionSlot,
        reviewer_slot: SessionSlot,
    ) -> str:
        sequence = next_review_sequence(self.repo, self.config)
        slug = f"{result.scope}-{result.target}"
        path = review_artifact_path(self.repo, self.config, sequence=sequence, slug=slug)
        kind = self._review_kind(state.active_review, result) if state.active_review else "batch"
        markdown = render_review_markdown(
            sequence=sequence,
            title=slug,
            kind=kind,  # type: ignore[arg-type]
            worker=None,
            reviewer=result,
            worker_session_id=state.sessions[implementer_slot].session_id,
            reviewer_session_id=state.sessions[reviewer_slot].session_id,
            session_purpose=reviewer_slot,
            active_review=state.active_review,
        )
        atomic_write_text(path, markdown)
        return str(path.relative_to(self.repo))

    def _handle_reviewer_blocked(
        self,
        state: LifecycleState,
        result: ReviewerResult,
        review_rel: str,
        implementer_slot: SessionSlot,
        reviewer_slot: SessionSlot,
    ) -> None:
        save_blocked_record(
            self.repo,
            BlockedRecord(
                blocked_at=datetime.now(timezone.utc),
                lifecycle_id=state.lifecycle_id,
                turn=state.turn,
                worker_session_id=state.sessions[implementer_slot].session_id,
                reviewer_session_id=state.sessions[reviewer_slot].session_id,
                planner_session_id=state.sessions["planner"].session_id,
                plan_reviewer_session_id=state.sessions["plan_reviewer"].session_id,
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

    def _reviewer_slot_turn(self, state: LifecycleState, slot: SessionSlot) -> None:
        if state.active_review is None:
            raise GitProtocolError("Reviewer invoked without active review")
        if slot == "plan_reviewer":
            self._assert_planning_slot_active(state, "plan_reviewer")
        role = ROLE_FOR_SLOT[slot]
        protocol_repair = False
        while True:
            first = state.sessions[slot].session_id is None
            instruction_stack = compose_role_instructions(
                self.repo, self.config, role, first_invocation=first
            )
            ctx = self._turn_context(state, slot, protocol_repair=protocol_repair)
            body = build_reviewer_prompt(state, ctx, state.active_review)
            prompt = instruction_stack + "\n\n" + body if instruction_stack else body
            protected = capture_protected_baseline(self.repo, self.config)
            snapshot = capture_review_snapshot(
                self.repo,
                plan_path=self.repo / self.config.plan_file,
                targets=state.active_review.targets,
            )
            final_text = self._invoke_slot(slot, prompt, state)
            assert_protected_unchanged(self.repo, self.config, protected)
            assert_review_snapshot_unchanged(
                self.repo,
                plan_path=self.repo / self.config.plan_file,
                before=snapshot,
                targets=state.active_review.targets,
            )
            try:
                result = parse_reviewer_result(final_text)
            except ProtocolParseError:
                if self._protocol_repair_or_fail(state, slot):
                    protocol_repair = True
                    continue
                raise
            break
        state.consecutive_protocol_failures = 0
        self._assert_reviewer_matches_active(state.active_review, result)
        self._assert_pass_targets(state.active_review, result)
        if slot == "plan_reviewer" and result.verdict == "complete":
            raise GitProtocolError("Plan reviewer cannot declare task completion")

        implementer_slot: SessionSlot = "planner" if slot == "plan_reviewer" else "worker"
        review_rel = self._write_review_artifact(
            state, result, implementer_slot=implementer_slot, reviewer_slot=slot
        )

        if result.verdict == "blocked":
            self._handle_reviewer_blocked(state, result, review_rel, implementer_slot, slot)
            return

        append_event(
            self.repo,
            self.config,
            {
                "type": "review_result",
                "verdict": result.verdict,
                "scope": result.scope,
                "session_purpose": slot,
                "finding_count": len(result.findings),
                "turn": state.turn,
                "lifecycle_id": state.lifecycle_id,
            },
        )
        self._console.review_result(result.verdict, result.scope, len(result.findings))

        active = state.active_review
        if slot == "plan_reviewer":
            if result.verdict == "pass":
                plan_hash = self._plan_hash()
                state.plan_approved = True
                state.initial_approved_plan_sha256 = plan_hash
                state.current_plan_sha256 = plan_hash
                state.planning_completed_at = utc_now()
                state.phase = "execution"
                state.sessions["planner"].status = "retired"
                state.sessions["plan_reviewer"].status = "retired"
                state.active_review = None
                state.pending_revision = None
                state.next_session = "worker"
                self._console.plan_ready(self.config.plan_file)
            else:
                state.pending_revision = PendingRevision(
                    cycle_id=active.cycle_id,
                    scope=active.scope,
                    target=active.target,
                    round=active.round + 1,
                    finding_review_file=review_rel,
                )
                state.active_review = None
                state.next_session = "planner"
            self._persist_after_turn(state)
            return

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
                    planner_session_id=state.sessions["planner"].session_id,
                    plan_reviewer_session_id=state.sessions["plan_reviewer"].session_id,
                    worker_session_id=state.sessions["worker"].session_id,
                    reviewer_session_id=state.sessions["reviewer"].session_id,
                    planner_model=state.sessions["planner"].model,
                    worker_model=state.sessions["worker"].model,
                    reviewer_model=state.sessions["reviewer"].model,
                    initial_base_commit=state.initial_base_commit,
                    final_commit=final_head,
                    last_approved_commit=state.last_approved_commit,
                    final_review_file=review_rel,
                    task_sha256=task_hash,
                    plan_sha256=plan_hash,
                    initial_approved_plan_sha256=state.initial_approved_plan_sha256,
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

        if active.scope == "batch" and result.verdict == "pass":
            if active.has_git_target:
                new_head = active.git_head or head_commit(self.repo)
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
            state.pending_revision = None
        elif result.verdict == "revise":
            state.pending_revision = PendingRevision(
                cycle_id=active.cycle_id,
                scope=active.scope,
                target=active.target,
                base_commit=active.approved_base_commit,
                production_head_commit=active.production_head_commit,
                last_reviewed_head_commit=active.current_candidate_head,
                round=active.round + 1,
                finding_review_file=review_rel,
            )
        else:
            state.pending_revision = None

        state.current_plan_sha256 = self._plan_hash()
        state.active_review = None
        state.next_session = "worker"
        self._persist_after_turn(state)

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
        self._console.lifecycle_started(
            state.lifecycle_id,
            goal_summary=self.options.goal_summary,
            user_config_rel=self.options.user_config_rel,
            resuming=self.options.resuming,
        )
        self._reconcile_stale_inflight(state)
        state = load_lifecycle_state(self.repo) or state
        if state.inflight is not None:
            append_event(
                self.repo,
                self.config,
                {
                    "type": "inflight_resume",
                    "actor": state.inflight.role,
                    "session_purpose": state.inflight.session_slot,
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
                slot = state.next_session
                if slot == "planner":
                    self._planner_turn(state)
                elif slot == "plan_reviewer":
                    self._reviewer_slot_turn(state, "plan_reviewer")
                elif slot == "worker":
                    self._worker_turn(state)
                else:
                    self._reviewer_slot_turn(state, "reviewer")
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
            if self._stop.requested or self._terminal_exit == ExitCode.STOPPED:
                return RunOutcome(
                    exit_code=ExitCode.STOPPED,
                    state=load_lifecycle_state(self.repo),
                    message=self._terminal_message or str(exc),
                )
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


def _check_idempotent_blocked(repo: Path, config: AutoLoopConfig) -> RunOutcome | None:
    record = load_blocked_record(repo)
    if record is None:
        return None
    state = load_lifecycle_state(repo)
    summary = (record.summary or "").strip()
    message = summary or IDEMPOTENT_BLOCKED_MESSAGE
    return RunOutcome(
        exit_code=ExitCode.BLOCKED,
        state=state,
        message=message,
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
    *,
    inputs: RunInputs | None = None,
) -> RunOutcome:
    from auto_loop.locking import acquire_workspace_lock

    config = ensure_run_prerequisites(repo, inputs)
    idempotent_blocked = _check_idempotent_blocked(repo, config)
    if idempotent_blocked is not None:
        return idempotent_blocked
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
