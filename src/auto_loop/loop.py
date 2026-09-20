"""Mechanical worker/reviewer lifecycle loop."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from auto_loop.config import AutoLoopConfig
from auto_loop.context_manifest import load_context_file, render_resource_manifest, validate_context
from auto_loop.exits import ExitCode
from auto_loop.git import GitProtocolError, assert_approved_baseline_ancestry, head_commit, normalize_batch_range
from auto_loop.instructions import compose_role_instructions
from auto_loop.lifecycle import ActiveReview, LifecycleState, create_lifecycle, utc_now
from auto_loop.product_state import assert_clean_product_tree, is_product_tree_clean
from auto_loop.prompts import TurnContext, build_reviewer_prompt, build_worker_prompt
from auto_loop.protocol import parse_reviewer_result, parse_worker_result
from auto_loop.providers.base import AgentRequest
from auto_loop.providers.cursor import build_cursor_command, parse_cursor_stream
from auto_loop.review_store import next_review_sequence, review_artifact_path
from auto_loop.reviews import render_review_markdown
from auto_loop.run_options import RunOptions
from auto_loop.run_prerequisites import RunPreconditionError, ensure_run_prerequisites
from auto_loop.runtime import load_lifecycle_state, save_lifecycle_state


class ProviderInvoker(Protocol):
    def prepare(self, role: str) -> None: ...

    def invoke(self, argv: list[str]) -> tuple[int, list[str]]: ...


@dataclass
class RunOutcome:
    exit_code: ExitCode
    state: LifecycleState | None = None


class LifecycleRunner:
    def __init__(
        self,
        repo: Path,
        config: AutoLoopConfig,
        options: RunOptions,
        invoker: ProviderInvoker,
    ) -> None:
        self.repo = repo
        self.config = config
        self.options = options
        self.invoker = invoker
        self._dirty_batch_attempts = 0

    def _load_or_create_state(self) -> LifecycleState:
        state = load_lifecycle_state(self.repo)
        if state is None:
            state = create_lifecycle(head_commit(self.repo))
            save_lifecycle_state(self.repo, state)
        return state

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
        argv = build_cursor_command(
            self.config,
            request,
            binary="fake-agent",
            resume_session_id=session.session_id,
        )
        os.environ["AUTO_LOOP_FAKE_ROLE"] = role
        self.invoker.prepare(role)
        _code, lines = self.invoker.invoke(argv)
        parsed = parse_cursor_stream(
            lines,
            expected_session_id=session.session_id,
        )
        if session.session_id is None:
            session.session_id = parsed.session_id
        return parsed.final_text

    def _worker_turn(self, state: LifecycleState) -> None:
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
            resource_manifest=self._context_manifest("worker"),
        )
        prompt = instruction_stack + "\n\n" + build_worker_prompt(state, ctx) if instruction_stack else build_worker_prompt(state, ctx)
        final_text = self._invoke_role("worker", prompt, state)
        result = parse_worker_result(final_text)

        if not state.plan_approved:
            if result.review is None or result.review.scope != "plan":
                raise GitProtocolError("Worker must request plan review before implementation")
            if head_commit(self.repo) != state.initial_base_commit:
                raise GitProtocolError("Product HEAD must remain at initial baseline before plan PASS")
            assert_clean_product_tree(self.repo)

        elif result.review and result.review.scope == "batch":
            try:
                assert_clean_product_tree(self.repo)
            except GitProtocolError:
                self._dirty_batch_attempts += 1
                if self._dirty_batch_attempts > self.config.limits.protocol_retries:
                    raise
                state.next_actor = "worker"
                state.turn += 1
                state.updated_at = utc_now()
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
            state.active_review = ActiveReview(
                scope=result.review.scope,
                target=result.review.target,
                summary=result.review.summary,
                base_commit=result.review.base_commit,
                head_commit=result.review.head_commit,
                worker_summary=result.work_summary,
            )
            state.next_actor = "reviewer"
        state.turn += 1
        state.updated_at = utc_now()
        save_lifecycle_state(self.repo, state)

    def _reviewer_turn(self, state: LifecycleState) -> None:
        if state.active_review is None:
            raise GitProtocolError("Reviewer invoked without active review")
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
            resource_manifest=self._context_manifest("reviewer"),
        )
        body = build_reviewer_prompt(state, ctx, state.active_review)
        prompt = instruction_stack + "\n\n" + body if instruction_stack else body
        final_text = self._invoke_role("reviewer", prompt, state)
        result = parse_reviewer_result(final_text)

        sequence = next_review_sequence(self.repo, self.config)
        slug = f"{result.scope}-{result.target}"
        path = review_artifact_path(self.repo, self.config, sequence=sequence, slug=slug)
        markdown = render_review_markdown(
            sequence=sequence,
            title=slug,
            kind="plan" if result.scope == "plan" else "batch",
            worker=None,
            reviewer=result,
            worker_session_id=state.sessions["worker"].session_id,
            reviewer_session_id=state.sessions["reviewer"].session_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")

        if result.verdict == "pass" and result.scope == "plan":
            state.plan_approved = True
        if result.verdict == "pass" and result.scope == "batch":
            state.last_approved_commit = head_commit(self.repo)
            self._dirty_batch_attempts = 0

        state.active_review = None
        state.next_actor = "worker"
        state.turn += 1
        state.updated_at = utc_now()
        save_lifecycle_state(self.repo, state)

    def run(self) -> RunOutcome:
        state = self._load_or_create_state()
        turns = 0
        while turns < self.options.max_turns:
            turns += 1
            assert_approved_baseline_ancestry(self.repo, state.last_approved_commit)
            if state.next_actor == "worker":
                self._worker_turn(state)
            else:
                self._reviewer_turn(state)
            state = load_lifecycle_state(self.repo) or state
        return RunOutcome(exit_code=ExitCode.LIMIT_REACHED, state=state)


def run_lifecycle(
    repo: Path,
    options: RunOptions,
    invoker: ProviderInvoker,
) -> RunOutcome:
    config = ensure_run_prerequisites(repo)
    runner = LifecycleRunner(repo, config, options, invoker)
    return runner.run()
