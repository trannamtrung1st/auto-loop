"""Mechanical planner/worker/reviewer lifecycle loop."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from auto_loop.config import AutoLoopConfig
from auto_loop.context_manifest import validate_context
from auto_loop.task_resources import compose_turn_resource_manifest
from auto_loop.exits import ExitCode
from auto_loop.git import (
    GitProtocolError,
    ReviewRequestError,
    assert_approved_baseline_ancestry,
    head_commit,
    resolve_commit,
)
from auto_loop.git_policy import git_usable, repository_required_error
from auto_loop.instructions import compose_role_instructions
from auto_loop.atomic_io import atomic_write_text
from auto_loop.lifecycle import (
    ActiveReview,
    ApprovedTargetEvidence,
    CompletedProviderTurn,
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
    PlannerResult,
    ReviewerResult,
    Role,
    WorkerResult,
)
from auto_loop.product_state import (
    assert_clean_product_tree,
    capture_product_working_fingerprint,
    is_product_tree_clean,
    product_excludes,
    product_working_fingerprints_equal,
)
from auto_loop.prompts import TurnContext, build_planner_prompt, build_reviewer_prompt, build_worker_prompt
from auto_loop.protocol import (
    ProtocolParseError,
    missing_pass_targets,
    parse_planner_result,
    parse_reviewer_result,
    parse_worker_result,
    reviewer_active_binding_mismatch,
)
from auto_loop.stop_control import (
    RunStopController,
    clear_active_run,
    persist_stopped_state,
    reconcile_stale_runtime,
    register_active_run,
)
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
from auto_loop.turn_logs import TurnLogWriter, prune_run_history, write_unseen_stream_lines
from auto_loop.run_inputs import RunInputs, load_matching_blocked_record, load_matching_completion_record
from auto_loop.paths import resolved_artifact_root
from auto_loop.run_prerequisites import RunPreconditionError, ensure_run_prerequisites
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state
from auto_loop.terminal_records import (
    BlockedRecord,
    CompletionRecord,
    IDEMPOTENT_BLOCKED_MESSAGE,
    IDEMPOTENT_COMPLETE_MESSAGE,
    assert_completion_inputs_unchanged,
    save_blocked_record,
    save_completion_record,
    task_and_plan_hashes,
)


INTERRUPTED_MESSAGE = """Run interrupted.

Your state was saved.
Resume with:
  auto-loop resume RUN_CONFIG"""


class ProviderInvoker(Protocol):
    def prepare(self, role: str) -> None: ...

    def invoke(self, argv: list[str]) -> ProviderAttemptResult: ...


@dataclass
class RunOutcome:
    exit_code: ExitCode
    state: LifecycleState | None = None
    message: str | None = None
    terminal_summary_rendered: bool = False


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
        self.artifact_root = resolved_artifact_root(repo, config.artifacts.root)
        self.options = options
        self.invoker = invoker
        self._initial_lifecycle_id = initial_lifecycle_id
        self._dirty_batch_attempts = 0
        self._terminal_exit: ExitCode | None = None
        self._terminal_message: str | None = None
        self._terminal_summary_rendered = False
        console_level = (
            "quiet"
            if options.quiet
            else ("verbose" if options.verbose else options.console_level)
        )
        self._console = RunConsole(console_level)
        self._run_started_mono = time.monotonic()
        self._stop = RunStopController(repo, artifact_root=self.artifact_root)
        self._limit_reason: str | None = None

    def _excludes(self) -> tuple[str, ...]:
        return product_excludes(self.config)

    def _load_state(self) -> LifecycleState | None:
        return load_lifecycle_state(self.repo, self.artifact_root)

    def _save_state(self, state: LifecycleState) -> None:
        save_lifecycle_state(self.repo, state, artifact_root=self.artifact_root)

    def _load_or_create_state(self) -> LifecycleState:
        state = self._load_state()
        if state is None:
            message = repository_required_error(self.repo, self.config)
            if message:
                raise GitProtocolError(message)
            lifecycle_id = self._initial_lifecycle_id or new_lifecycle_id()
            state = create_lifecycle(self._optional_head(), lifecycle_id=lifecycle_id)
            self._save_state(state)
        return state

    def _reconcile_stale_inflight(self, state: LifecycleState) -> None:
        if state.inflight is None:
            return
        state.next_session = state.inflight.session_slot
        self._save_state(state)

    def _turn_interrupted(self, state: LifecycleState, slot: SessionSlot) -> bool:
        return state.inflight is not None and state.inflight.session_slot == slot

    def _persist_inflight(
        self,
        state: LifecycleState,
        slot: SessionSlot,
        *,
        repair_reason: str | None = None,
    ) -> None:
        existing = state.inflight
        if (
            repair_reason is None
            and existing is not None
            and existing.session_slot == slot
            and existing.turn == state.turn
        ):
            repair_reason = existing.repair_reason
        session = state.sessions[slot]
        session_id = session.session_id or "pending"
        state.inflight = InflightMarker(
            session_slot=slot,
            role=ROLE_FOR_SLOT[slot],
            turn=state.turn,
            session_id=session_id,
            started_at=utc_now(),
            head_before=self._optional_head(),
            repair_reason=repair_reason,
        )
        self._save_state(state)

    def _clear_inflight(self, state: LifecycleState) -> None:
        state.inflight = None

    def _runtime_exceeded(self) -> bool:
        limit_seconds = self.options.max_runtime_minutes * 60
        return (time.monotonic() - self._run_started_mono) >= limit_seconds

    def _mark_limit_reached(self, state: LifecycleState, reason: str) -> None:
        state.status = LifecycleStatus.LIMIT_REACHED
        preserve_repair_inflight = (
            state.inflight is not None and bool(state.inflight.repair_reason)
        )
        state.completed_provider_turn = None
        if not preserve_repair_inflight:
            state.inflight = None
        state.updated_at = utc_now()
        self._save_state(state)
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
        self._publish_terminal_user_message(ExitCode.LIMIT_REACHED, reason)

    def _note_terminal_summary_rendered(self) -> None:
        if self._console.level != "quiet":
            self._terminal_summary_rendered = True

    def _publish_terminal_user_message(self, exit_code: ExitCode, message: str) -> None:
        """Show structured terminal output, or keep ``message`` for CLI in quiet mode."""
        if self._console.level == "quiet":
            self._terminal_message = message
            return
        if exit_code == ExitCode.STOPPED:
            self._console.lifecycle_stopped(message)
        elif exit_code == ExitCode.LIMIT_REACHED:
            self._console.lifecycle_limit_reached(message)
        self._note_terminal_summary_rendered()
        self._terminal_message = None

    def _terminal_run_outcome(self, exit_code: ExitCode, state: LifecycleState | None) -> RunOutcome:
        return RunOutcome(
            exit_code=exit_code,
            state=state,
            message=self._terminal_message,
            terminal_summary_rendered=self._terminal_summary_rendered,
        )

    def _stopped_outcome_message(self, *, provider_detail: str | None = None) -> str | None:
        if self._terminal_message is not None:
            return self._terminal_message
        if self._terminal_summary_rendered:
            return None
        return provider_detail

    def _handle_stop_requested(self, state: LifecycleState) -> bool:
        if not self._stop.requested:
            return False
        persist_stopped_state(self.repo, state, self.artifact_root)
        append_event(
            self.repo,
            self.config,
            {"type": "lifecycle_stopped", "lifecycle_id": state.lifecycle_id},
        )
        self._terminal_exit = ExitCode.STOPPED
        self._publish_terminal_user_message(ExitCode.STOPPED, INTERRUPTED_MESSAGE)
        return True

    def _protocol_repair_or_fail(
        self,
        state: LifecycleState,
        slot: SessionSlot,
        *,
        repair_reason: str | None = None,
    ) -> bool:
        """Return True to retry the same slot turn with a repair prompt."""
        state.consecutive_protocol_failures += 1
        if state.consecutive_protocol_failures > self.config.limits.protocol_retries:
            state.updated_at = utc_now()
            self._save_state(state)
            return False
        append_event(
            self.repo,
            self.config,
            {
                "type": "protocol_repair",
                "actor": ROLE_FOR_SLOT[slot],
                "session_purpose": slot,
                "attempt": state.consecutive_protocol_failures,
                "lifecycle_id": state.lifecycle_id,
            },
        )
        state.updated_at = utc_now()
        if repair_reason is not None:
            self._reopen_provider_turn(state, slot, repair_reason=repair_reason)
        else:
            self._save_state(state)
        return True

    def _context_manifest(self, role: Role) -> str:
        document = self.config.context
        validation = validate_context(self.repo, document)
        if not validation.ok_for_run:
            raise RunPreconditionError("context resources failed validation")
        return compose_turn_resource_manifest(self.config, role)

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
        inflight = state.inflight
        slot_inflight = inflight is not None and inflight.session_slot == slot
        repair_reason = inflight.repair_reason if slot_inflight else None
        return TurnContext(
            task_path=self.config.task_file,
            plan_path=self.config.plan_file,
            latest_review_path=self._latest_review_path(),
            head_commit=head_commit(self.repo) if self._git_usable() else None,
            product_clean=(
                is_product_tree_clean(self.repo, excludes=self._excludes())
                if self._git_usable()
                else True
            ),
            git_available=self._git_usable(),
            interrupted=slot_inflight and repair_reason is None,
            protocol_repair=protocol_repair,
            repair_reason=repair_reason,
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
        session.model = model
        self._persist_inflight(state, slot)
        self._console.turn_started(state.turn, slot, model=model)
        if session.session_id:
            self._console.session_resumed(session.session_id)
        turn_log = TurnLogWriter(
            self.repo,
            self.config,
            state.lifecycle_id,
            state.turn,
            slot,
            legacy_assistant_trace=not use_live_cursor,
        )
        self.invoker.prepare(slot)
        if hasattr(self.invoker, "stop_check"):
            self.invoker.stop_check = lambda: self._stop.requested
        if hasattr(self.invoker, "force_check"):
            self.invoker.force_check = lambda: self._stop.force
        if hasattr(self.invoker, "on_provider_pid"):

            def _provider_pid(pid: int | None) -> None:
                self._stop.set_active_provider(pid)
                register_active_run(
                    self.repo,
                    state.lifecycle_id,
                    provider_pid=pid,
                    artifact_root=self.artifact_root,
                )

            self.invoker.on_provider_pid = _provider_pid

        def _consume_provider_line(line: str) -> None:
            for event in turn_log.write_stream_line(line):
                self._console.provider_trace(event)

        if hasattr(self.invoker, "on_stream_line"):
            self.invoker.on_stream_line = _consume_provider_line
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
                    before_lines = turn_log.raw_line_count
                    try:
                        attempt_result = self.invoker.invoke(argv)
                        write_unseen_stream_lines(
                            turn_log,
                            attempt_result.lines,
                            before=before_lines,
                            on_line=_consume_provider_line,
                        )
                    finally:
                        turn_log.finish_provider_attempt()
                        self._console.finish_provider_trace()
                    parsed = self._parse_provider_attempt(attempt_result, session.session_id)
                    if parsed.session_id:
                        created = adopt_session_identity(
                            state,
                            slot,
                            parsed.session_id,
                            model,
                        )
                        self._save_state(state)
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
                            self._console.session_created(parsed.session_id)
                    if attempt_result.failure == ProviderFailureKind.INTERRUPTED:
                        turn_log.finalize()
                        if self._stop.requested:
                            persist_stopped_state(self.repo, state, self.artifact_root)
                            append_event(
                                self.repo,
                                self.config,
                                {"type": "lifecycle_stopped", "lifecycle_id": state.lifecycle_id},
                            )
                            self._terminal_exit = ExitCode.STOPPED
                            self._publish_terminal_user_message(
                                ExitCode.STOPPED, INTERRUPTED_MESSAGE
                            )
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
                        persist_stopped_state(self.repo, state, self.artifact_root)
                        append_event(
                            self.repo,
                            self.config,
                            {"type": "lifecycle_stopped", "lifecycle_id": state.lifecycle_id},
                        )
                        self._terminal_exit = ExitCode.STOPPED
                        self._publish_terminal_user_message(
                            ExitCode.STOPPED, INTERRUPTED_MESSAGE
                        )
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
            if hasattr(self.invoker, "on_stream_line"):
                self.invoker.on_stream_line = None
            if hasattr(self.invoker, "force_check"):
                self.invoker.force_check = None
            if hasattr(self.invoker, "on_provider_pid"):
                self.invoker.on_provider_pid(None)
            self._stop.set_active_provider(None)
            turn_log.finalize()
            self._console.finish_provider_trace()
        if final_text is None:
            turn_log.finalize()
            raise ProviderError(f"Provider failed for session {slot}")
        turn_log.finalize()
        return final_text

    def _git_usable(self) -> bool:
        return git_usable(self.repo, self.config)

    def _optional_head(self) -> str | None:
        if not self._git_usable():
            return None
        return head_commit(self.repo)

    def _product_snapshot(self) -> tuple[str | None, list[list[str]] | None]:
        head, rows = capture_product_working_fingerprint(
            self.repo, excludes=self._excludes(), config=self.config
        )
        return head, rows

    def _adopt_parsed_result(
        self,
        state: LifecycleState,
        slot: SessionSlot,
        kind: str,
        result: PlannerResult | WorkerResult | ReviewerResult,
        product_before: tuple[str | None, list[list[str]] | None],
    ) -> None:
        head_before, changes_before = product_before
        session = state.sessions[slot]
        state.completed_provider_turn = CompletedProviderTurn(
            session_slot=slot,
            role=ROLE_FOR_SLOT[slot],
            turn=state.turn,
            session_id=session.session_id,
            result_kind=kind,  # type: ignore[arg-type]
            result=result.model_dump(mode="json"),
            product_head_before=head_before,
            product_changes_before=changes_before,
        )
        state.inflight = None
        state.updated_at = utc_now()
        self._save_state(state)

    def _reopen_provider_turn(
        self,
        state: LifecycleState,
        slot: SessionSlot,
        *,
        repair_reason: str | None = None,
    ) -> None:
        """Protocol-invalid output stays an in-progress turn so resume can re-invoke."""
        state.completed_provider_turn = None
        self._persist_inflight(state, slot, repair_reason=repair_reason)

    def _note_transition_error(self, state: LifecycleState, exc: BaseException) -> None:
        done = state.completed_provider_turn
        if done is None:
            return
        done.transition_error = str(exc)
        state.inflight = None
        state.updated_at = utc_now()
        self._save_state(state)

    def _reopen_worker_review_request(self, state: LifecycleState, exc: ReviewRequestError) -> None:
        """Keep the worker session and re-prompt with the rejection reason."""
        self._reopen_provider_turn(state, "worker", repair_reason=str(exc))

    def _record_approved_evidence(self, state: LifecycleState, review: ActiveReview) -> None:
        for target in review.targets:
            if target.kind == "git_range":
                state.approved_evidence.append(
                    ApprovedTargetEvidence(
                        id=target.id,
                        kind="git_range",
                        fingerprint=target.head_commit,
                        git_base=target.base_commit,
                        git_head=target.head_commit,
                    )
                )
            elif target.kind == "path":
                state.approved_evidence.append(
                    ApprovedTargetEvidence(
                        id=target.id,
                        kind="path",
                        path=target.path,
                        fingerprint=target.fingerprint,
                    )
                )
            else:
                state.approved_evidence.append(
                    ApprovedTargetEvidence(
                        id=target.id,
                        kind="content",
                        fingerprint=target.content_sha256,
                    )
                )

    def _assert_planning_policy(self, state: LifecycleState) -> None:
        done = state.completed_provider_turn
        if done is None:
            return
        if self.config.git.mode == "required" and self._git_usable():
            if head_commit(self.repo) != state.initial_base_commit:
                raise GitProtocolError(
                    "Product HEAD must remain at initial baseline before plan PASS"
                )
            assert_clean_product_tree(self.repo, excludes=self._excludes())
        current_head, current_changes = self._product_snapshot()
        before = done.product_changes_before
        if current_head != done.product_head_before or not product_working_fingerprints_equal(
            before, current_changes
        ):
            raise GitProtocolError("Planner mutated product files outside the artifact root")

    def _assert_final_complete_valid(
        self,
        state: LifecycleState,
        result: ReviewerResult,
        active: ActiveReview,
    ) -> None:
        if active.targets and not set(active.target_ids) <= set(result.reviewed_target_ids):
            raise missing_pass_targets(active.target_ids, result.reviewed_target_ids)
        if self.config.git.mode != "required":
            if not active.targets:
                raise GitProtocolError(
                    "Final review requires explicit path, content, or Git targets"
                )
            if not active.has_git_target:
                return
            current_head = head_commit(self.repo)
            expected = active.git_head
            if expected and current_head != expected:
                raise GitProtocolError("COMPLETE Git target does not match current HEAD")
            if result.reviewed_head_commit and resolve_commit(
                self.repo, result.reviewed_head_commit
            ) != current_head:
                raise GitProtocolError(
                    "COMPLETE reviewed_head_commit does not match current HEAD"
                )
            return
        current_head = head_commit(self.repo)
        if current_head != state.last_approved_commit:
            raise GitProtocolError(
                "COMPLETE requires HEAD to equal last_approved_commit"
            )
        if result.reviewed_head_commit and resolve_commit(
            self.repo, result.reviewed_head_commit
        ) != current_head:
            raise GitProtocolError("COMPLETE reviewed_head_commit does not match current HEAD")
        assert_clean_product_tree(self.repo, excludes=self._excludes())

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
            raise reviewer_active_binding_mismatch("scope", active.scope, result.scope)
        if result.target != active.target:
            raise reviewer_active_binding_mismatch("target", active.target, result.target)
        if not active.has_git_target:
            return
        expected_base = active.git_base
        expected_head = active.git_head or active.current_candidate_head
        if result.reviewed_base_commit is not None and expected_base is not None:
            if resolve_commit(self.repo, result.reviewed_base_commit) != resolve_commit(
                self.repo, expected_base
            ):
                raise reviewer_active_binding_mismatch(
                    "reviewed_base_commit", expected_base, result.reviewed_base_commit
                )
        if result.reviewed_head_commit is not None and expected_head is not None:
            if resolve_commit(self.repo, result.reviewed_head_commit) != resolve_commit(
                self.repo, expected_head
            ):
                raise reviewer_active_binding_mismatch(
                    "reviewed_head_commit", expected_head, result.reviewed_head_commit
                )

    def _persist_after_turn(self, state: LifecycleState) -> None:
        state.turn += 1
        state.updated_at = utc_now()
        self._clear_inflight(state)
        state.completed_provider_turn = None
        self._save_state(state)

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
        product_before = self._product_snapshot()
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
        self._adopt_parsed_result(state, "planner", "planner", result, product_before)
        self._transition_planner(state, result)

    def _transition_planner(self, state: LifecycleState, result: PlannerResult) -> None:
        try:
            self._assert_planning_policy(state)
            self._apply_planner_result(state, result)
        except ProtocolParseError as exc:
            self._reopen_provider_turn(state, "planner", repair_reason=str(exc))
            raise
        except GitProtocolError as exc:
            self._note_transition_error(state, exc)
            raise

    def _apply_planner_result(self, state: LifecycleState, result: PlannerResult) -> None:
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
                targets=[
                    plan_path_target(
                        self.repo, self.config.plan_file, git_mode=self.config.git.mode
                    )
                ],
            )
            state.next_session = "plan_reviewer"
            self._persist_after_turn(state)
            return

        if result.review is None or result.review.scope != "plan":
            raise GitProtocolError("Planner must request plan review")
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
            targets=[
                plan_path_target(
                    self.repo, self.config.plan_file, git_mode=self.config.git.mode
                )
            ],
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
            targets=[
                plan_path_target(
                    self.repo, self.config.plan_file, git_mode=self.config.git.mode
                )
            ],
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
        self._adopt_parsed_result(state, "worker", "worker", result, (None, None))
        self._transition_worker(state, result)

    def _transition_worker(
        self,
        state: LifecycleState,
        result: WorkerResult,
        *,
        from_replay: bool = False,
    ) -> None:
        try:
            self._apply_worker_result(state, result, from_replay=from_replay)
        except ProtocolParseError as exc:
            self._reopen_provider_turn(state, "worker", repair_reason=str(exc))
            raise
        except ReviewRequestError as exc:
            self._reopen_worker_review_request(state, exc)
            return
        except GitProtocolError as exc:
            self._note_transition_error(state, exc)
            raise

    def _apply_worker_result(
        self,
        state: LifecycleState,
        result: WorkerResult,
        *,
        from_replay: bool = False,
    ) -> None:
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
            if not self._apply_final_request(state, result):
                return
        elif result.review.scope == "plan":
            state.active_review = self._make_plan_update_review(state, result)
        elif result.review.scope == "batch":
            if not self._accept_batch_tree(state, from_replay=from_replay):
                return
            self._apply_batch_request(state, result)
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
        progress_key = worker_progress_key(
            self.repo, self.config, state, result, self._optional_head() or ""
        )
        if update_worker_no_progress(state, self.config, progress_key):
            self._mark_limit_reached(state, "worker_no_progress")
            return
        self._persist_after_turn(state)

    def _accept_batch_tree(self, state: LifecycleState, *, from_replay: bool) -> bool:
        """Return False when strict mode sends the worker back to clean the tree."""
        if self.config.git.mode != "required":
            return True
        try:
            assert_clean_product_tree(self.repo, excludes=self._excludes())
        except GitProtocolError:
            if from_replay:
                raise
            self._dirty_batch_attempts += 1
            if self._dirty_batch_attempts > self.config.limits.protocol_retries:
                raise
            state.worker_no_progress_streak = 0
            state.last_worker_progress_key = None
            state.next_session = "worker"
            self._persist_after_turn(state)
            return False
        return True

    def _apply_final_request(self, state: LifecycleState, result: WorkerResult) -> bool:
        assert result.review is not None
        if self.config.git.mode == "required":
            assert_clean_product_tree(self.repo, excludes=self._excludes())
            current_head = head_commit(self.repo)
            if current_head != state.last_approved_commit:
                state.next_session = "worker"
                self._persist_after_turn(state)
                return False
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
            return True
        targets, _warnings = normalize_work_targets(
            self.repo,
            last_approved_commit=state.last_approved_commit,
            request=result.review,
            excludes=self._excludes(),
            git_mode=self.config.git.mode,
            protect_history=self.config.git.protect_approved_history,
            allow_empty=False,
        )
        if not targets:
            raise ReviewRequestError(
                "Final review requires explicit path, content, or Git targets"
            )
        state.active_review = ActiveReview(
            cycle_id=next_cycle_id(state),
            scope="final",
            target=result.review.target,
            summary=result.review.summary,
            session_purpose="reviewer",
            approved_base_commit=state.last_approved_commit,
            current_candidate_head=self._optional_head(),
            worker_summary=result.work_summary,
            plan_sha256=self._plan_hash(),
            targets=targets,
        )
        return True

    def _apply_batch_request(self, state: LifecycleState, result: WorkerResult) -> None:
        assert result.review is not None
        targets, _warnings = normalize_work_targets(
            self.repo,
            last_approved_commit=state.last_approved_commit,
            request=result.review,
            excludes=self._excludes(),
            git_mode=self.config.git.mode,
            protect_history=self.config.git.protect_approved_history,
        )
        pending = state.pending_revision
        if pending and pending.scope == "batch" and pending.target == result.review.target:
            cycle_id = pending.cycle_id
            round_no = pending.round
            production_head = pending.production_head_commit
        else:
            cycle_id = next_cycle_id(state)
            round_no = 1
            production_head = self._optional_head()
        git_target = next((item for item in targets if item.kind == "git_range"), None)
        state.active_review = ActiveReview(
            cycle_id=cycle_id,
            round=round_no,
            scope="batch",
            target=result.review.target,
            summary=result.review.summary,
            session_purpose="reviewer",
            approved_base_commit=state.last_approved_commit,
            production_head_commit=production_head,
            current_candidate_head=self._optional_head(),
            worker_summary=result.work_summary,
            plan_sha256=self._plan_hash(),
            targets=targets,
        )
        if git_target is not None:
            result.review.base_commit = git_target.base_commit
            result.review.head_commit = git_target.head_commit

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
            artifact_root=self.artifact_root,
        )
        state.status = LifecycleStatus.BLOCKED
        state.active_review = None
        state.completed_provider_turn = None
        state.updated_at = utc_now()
        self._clear_inflight(state)
        self._save_state(state)
        append_event(
            self.repo,
            self.config,
            {"type": "lifecycle_blocked", "lifecycle_id": state.lifecycle_id},
        )
        self._console.lifecycle_blocked()
        self._terminal_exit = ExitCode.BLOCKED
        self._note_terminal_summary_rendered()

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
                config=self.config,
            )
            final_text = self._invoke_slot(slot, prompt, state)
            assert_protected_unchanged(self.repo, self.config, protected)
            assert_review_snapshot_unchanged(
                self.repo,
                plan_path=self.repo / self.config.plan_file,
                before=snapshot,
                targets=state.active_review.targets,
                config=self.config,
            )
            try:
                result = parse_reviewer_result(final_text)
            except ProtocolParseError:
                if self._protocol_repair_or_fail(state, slot):
                    protocol_repair = True
                    continue
                raise
            self._adopt_parsed_result(state, slot, "reviewer", result, (None, None))
            try:
                self._apply_reviewer_result(state, slot, result)
            except ProtocolParseError as exc:
                if self._protocol_repair_or_fail(state, slot, repair_reason=str(exc)):
                    protocol_repair = True
                    continue
                raise
            state.consecutive_protocol_failures = 0
            break

    def _transition_reviewer(
        self,
        state: LifecycleState,
        slot: SessionSlot,
        result: ReviewerResult,
    ) -> None:
        try:
            self._apply_reviewer_result(state, slot, result)
        except ProtocolParseError as exc:
            self._reopen_provider_turn(state, slot, repair_reason=str(exc))
            raise
        except GitProtocolError as exc:
            self._note_transition_error(state, exc)
            raise

    def _apply_reviewer_result(
        self,
        state: LifecycleState,
        slot: SessionSlot,
        result: ReviewerResult,
    ) -> None:
        if state.active_review is None:
            raise GitProtocolError("Reviewer invoked without active review")
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
        if active is None:
            raise GitProtocolError("Reviewer invoked without active review")
        if slot == "plan_reviewer":
            if result.verdict == "pass":
                self._record_approved_evidence(state, active)
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
            self._assert_final_complete_valid(state, result, active)
            self._record_approved_evidence(state, active)
            task_hash, plan_hash = task_and_plan_hashes(self.repo, self.config)
            final_head = self._optional_head()
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
                artifact_root=self.artifact_root,
            )
            state.status = LifecycleStatus.COMPLETED
            state.active_review = None
            state.completed_provider_turn = None
            state.updated_at = utc_now()
            self._clear_inflight(state)
            self._save_state(state)
            append_event(
                self.repo,
                self.config,
                {
                    "type": "lifecycle_complete",
                    "head": final_head,
                    "lifecycle_id": state.lifecycle_id,
                },
            )
            self._console.lifecycle_completed()
            self._terminal_exit = ExitCode.COMPLETE
            self._note_terminal_summary_rendered()
            return

        if active.scope == "batch" and result.verdict == "pass":
            self._record_approved_evidence(state, active)
            if active.has_git_target and active.git_head:
                new_head = active.git_head
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

    def _replay_completed_turn(self, state: LifecycleState) -> None:
        done = state.completed_provider_turn
        if done is None:
            return
        if done.result_kind == "planner":
            self._assert_planning_slot_active(state, "planner")
            self._transition_planner(state, PlannerResult.model_validate(done.result))
        elif done.result_kind == "worker":
            self._transition_worker(
                state,
                WorkerResult.model_validate(done.result),
                from_replay=True,
            )
        else:
            slot = done.session_slot
            if slot == "plan_reviewer":
                self._assert_planning_slot_active(state, "plan_reviewer")
            self._transition_reviewer(
                state,
                slot,
                ReviewerResult.model_validate(done.result),
            )

    def run(self) -> RunOutcome:
        self._stop.install()
        try:
            return self._run_loop()
        finally:
            self._stop.restore()
            clear_active_run(self.repo, self.artifact_root)

    def _run_loop(self) -> RunOutcome:
        try:
            state = self._load_or_create_state()
        except GitProtocolError:
            return RunOutcome(
                exit_code=ExitCode.GIT_PROTOCOL_ERROR,
                state=self._load_state(),
            )
        register_active_run(self.repo, state.lifecycle_id, artifact_root=self.artifact_root)
        if state.status in (LifecycleStatus.STOPPED, LifecycleStatus.LIMIT_REACHED):
            state.status = LifecycleStatus.RUNNING
            state.updated_at = utc_now()
            self._save_state(state)
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
            artifact_root_rel=self.config.artifacts_root,
        )
        self._reconcile_stale_inflight(state)
        state = self._load_state() or state
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
                state = self._load_state() or state
                if self._handle_stop_requested(state):
                    break
                if self._runtime_exceeded():
                    self._mark_limit_reached(state, "max_runtime_minutes")
                    break
                turns += 1
                if (
                    self.config.git.protect_approved_history
                    and self._git_usable()
                    and state.last_approved_commit
                ):
                    assert_approved_baseline_ancestry(self.repo, state.last_approved_commit)
                if state.completed_provider_turn is not None and state.inflight is None:
                    self._replay_completed_turn(state)
                else:
                    slot = state.next_session
                    if slot == "planner":
                        self._planner_turn(state)
                    elif slot == "plan_reviewer":
                        self._reviewer_slot_turn(state, "plan_reviewer")
                    elif slot == "worker":
                        self._worker_turn(state)
                    else:
                        self._reviewer_slot_turn(state, "reviewer")
                state = self._load_state() or state
                if self._terminal_exit is not None:
                    break
        except GitProtocolError:
            return RunOutcome(
                exit_code=ExitCode.GIT_PROTOCOL_ERROR,
                state=self._load_state(),
            )
        except SessionError as exc:
            return RunOutcome(
                exit_code=ExitCode.SESSION_ERROR,
                state=self._load_state(),
                message=str(exc),
            )
        except ProviderError as exc:
            if self._stop.requested or self._terminal_exit == ExitCode.STOPPED:
                return RunOutcome(
                    exit_code=ExitCode.STOPPED,
                    state=self._load_state(),
                    message=self._stopped_outcome_message(provider_detail=str(exc)),
                    terminal_summary_rendered=self._terminal_summary_rendered,
                )
            return RunOutcome(
                exit_code=exc.exit_code,
                state=self._load_state(),
                message=str(exc),
            )
        except ProtectionViolationError as exc:
            return RunOutcome(
                exit_code=exc.exit_code,
                state=self._load_state(),
                message=str(exc),
            )
        except ReviewMutationError as exc:
            return RunOutcome(
                exit_code=exc.exit_code,
                state=self._load_state(),
                message=str(exc),
            )
        except ProtocolParseError as exc:
            return RunOutcome(
                exit_code=ExitCode.PROTOCOL_ERROR,
                state=self._load_state(),
                message=str(exc),
            )
        if self._terminal_exit is not None:
            return self._terminal_run_outcome(self._terminal_exit, state)
        if state.status != LifecycleStatus.LIMIT_REACHED:
            self._mark_limit_reached(state, "max_turns")
        return self._terminal_run_outcome(ExitCode.LIMIT_REACHED, state)


def _check_idempotent_blocked(repo: Path, artifact_root: Path | None = None) -> RunOutcome | None:
    record = load_matching_blocked_record(repo, artifact_root)
    if record is None:
        return None
    state = load_lifecycle_state(repo, artifact_root)
    summary = (record.summary or "").strip()
    message = summary or IDEMPOTENT_BLOCKED_MESSAGE
    return RunOutcome(
        exit_code=ExitCode.BLOCKED,
        state=state,
        message=message,
    )


def _check_idempotent_completion(
    repo: Path,
    config: AutoLoopConfig,
    artifact_root: Path | None = None,
) -> RunOutcome | None:
    record = load_matching_completion_record(repo, artifact_root)
    if record is None:
        return None
    assert_completion_inputs_unchanged(repo, config, record)
    state = load_lifecycle_state(repo, artifact_root)
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
    config: AutoLoopConfig,
    artifact_root: Path,
    inputs: RunInputs | None = None,
) -> RunOutcome:
    from auto_loop.locking import ConcurrentRunError, acquire_workspace_lock

    del inputs
    idempotent_blocked = _check_idempotent_blocked(repo, artifact_root)
    if idempotent_blocked is not None:
        return idempotent_blocked
    idempotent = _check_idempotent_completion(repo, config, artifact_root)
    if idempotent is not None:
        return idempotent
    ensure_run_prerequisites(repo, config)
    reconciled = reconcile_stale_runtime(repo, artifact_root)
    if reconciled.remote_ownership or reconciled.unverified_ownership:
        raise ConcurrentRunError(reconciled.message)
    existing = load_lifecycle_state(repo, artifact_root)
    lifecycle_id = existing.lifecycle_id if existing else new_lifecycle_id()
    lock = acquire_workspace_lock(repo, lifecycle_id, artifact_root)
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
