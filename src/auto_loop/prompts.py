"""Turn prompt construction for persistent role sessions."""

from __future__ import annotations

from dataclasses import dataclass

from auto_loop.lifecycle import ActiveReview, LifecycleState, PendingRevision
from auto_loop.models import SessionSlot


@dataclass(frozen=True)
class TurnContext:
    task_path: str
    plan_path: str
    latest_review_path: str | None
    head_commit: str | None
    product_clean: bool
    git_available: bool = True
    interrupted: bool = False
    protocol_repair: bool = False
    repair_reason: str | None = None
    resource_manifest: str = ""
    session_purpose: SessionSlot = "worker"
    phase: str = "execution"
    initial_plan_sha256: str | None = None
    current_plan_sha256: str | None = None
    plan_changed_since_approval: bool | None = None
    first_execution_turn: bool = False
    pending_revision: PendingRevision | None = None


def _append_manifest(body: str, manifest: str) -> str:
    manifest = manifest.strip()
    if not manifest:
        return body
    return f"{body}\n\n{manifest}"


def _format_review_path(path: str | None) -> str:
    return path or "(none yet)"


def _tree_label(ctx: TurnContext) -> str:
    if not ctx.git_available:
        return "n/a"
    return "clean" if ctx.product_clean else "dirty"


def _plan_hash_lines(ctx: TurnContext) -> list[str]:
    lines: list[str] = []
    if ctx.initial_plan_sha256:
        lines.append(f"- initial approved plan hash: {ctx.initial_plan_sha256}")
    if ctx.current_plan_sha256:
        lines.append(f"- current plan hash: {ctx.current_plan_sha256}")
    if ctx.plan_changed_since_approval is not None:
        changed = "yes" if ctx.plan_changed_since_approval else "no"
        lines.append(f"- plan changed since initial approval: {changed}")
    return lines


def _repair_lines(ctx: TurnContext) -> list[str]:
    if not ctx.protocol_repair:
        return []
    return [
        "Your previous turn completed work but did not produce a valid AUTO_LOOP_RESULT.",
        "Do not redo successful work blindly.",
        "Inspect current durable state and output the correct result for the work "
        "currently present.",
        "",
    ]


def _controller_repair_lines(ctx: TurnContext) -> list[str]:
    if not ctx.repair_reason:
        return []
    return [
        "Your previous turn produced a result, but the controller rejected it:",
        f"- {ctx.repair_reason}",
        "Inspect current durable state and emit a corrected AUTO_LOOP_RESULT for this turn.",
        "Do not assume the rejected request was accepted.",
        "",
    ]


def build_planner_prompt(state: LifecycleState, ctx: TurnContext) -> str:
    lines = [
        "Continue your planner role for the planning phase.",
        "",
        "Durable state:",
        f"- task: {ctx.task_path}",
        f"- plan: {ctx.plan_path}",
        f"- initial product HEAD: {state.initial_base_commit or 'n/a'}",
        f"- current HEAD: {ctx.head_commit or 'n/a'}",
        f"- product tree: {_tree_label(ctx)}",
        f"- latest plan review: {_format_review_path(ctx.latest_review_path)}",
        "- planning phase only; do not implement product changes",
        "",
        "Reconcile repository/task/review evidence, update the plan, and request plan review.",
        "",
    ]
    if ctx.repair_reason:
        lines.extend(_controller_repair_lines(ctx))
    elif ctx.interrupted:
        lines.extend(
            [
                "The prior planner invocation may have been interrupted.",
                "Inspect current Git/files first and reconcile partial work before deciding "
                "the next action.",
                "Do not assume the interrupted turn completed.",
                "",
            ]
        )
    lines.extend(_repair_lines(ctx))
    lines.append("End with one valid AUTO_LOOP_RESULT.")
    return _append_manifest("\n".join(lines), ctx.resource_manifest)


def build_worker_prompt(state: LifecycleState, ctx: TurnContext) -> str:
    if ctx.first_execution_turn:
        lines = [
            "This is your first implementation turn.",
            "",
            "The initial plan was produced and approved in separate planning sessions.",
            "Do not blindly trust it.",
            "",
            "Read:",
            f"- {ctx.task_path}",
            f"- current {ctx.plan_path}",
            "- planning review artifact",
            "- current repository state",
            "",
            "Treat task.md as authoritative.",
            "You now own plan.md and may revise it whenever implementation reality requires.",
            "",
        ]
    else:
        lines = [
            "Continue your worker role for this lifecycle.",
            "",
        ]

    lines.extend(
        [
            "Durable current state:",
            f"- task: {ctx.task_path}",
            f"- plan: {ctx.plan_path}",
            f"- latest review: {_format_review_path(ctx.latest_review_path)}",
            f"- plan approved: {'true' if state.plan_approved else 'false'}",
            f"- last approved product commit: {state.last_approved_commit or 'n/a'}",
            f"- current HEAD: {ctx.head_commit or 'n/a'}",
            f"- product working tree: {_tree_label(ctx)}",
        ]
    )
    lines.extend(_plan_hash_lines(ctx))
    if ctx.pending_revision:
        pending = ctx.pending_revision
        lines.extend(
            [
                f"- pending review cycle: {pending.cycle_id} round {pending.round}",
                f"- pending review target: {pending.scope}/{pending.target}",
                "- prefer amending the unapproved review-fix commit when Git fixes are needed",
            ]
        )
    lines.extend(
        [
            "",
            "Reconcile this durable state with your session memory, applicable repository "
            "instructions/skills, and declared task resources, then perform the single best "
            "next worker turn according to your role instructions.",
            "",
        ]
    )
    if ctx.repair_reason:
        lines.extend(_controller_repair_lines(ctx))
    elif ctx.interrupted:
        lines.extend(
            [
                "The prior worker invocation may have been interrupted.",
                "Inspect current Git/files first and reconcile partial work before deciding "
                "the next action.",
                "Do not assume the interrupted turn completed.",
                "",
            ]
        )
    lines.extend(_repair_lines(ctx))
    lines.append("End with one valid AUTO_LOOP_RESULT.")
    return _append_manifest("\n".join(lines), ctx.resource_manifest)


def _format_targets(review: ActiveReview) -> list[str]:
    if not review.targets:
        return []
    lines = ["Required review targets:"]
    for target in review.targets:
        if target.kind == "git_range":
            lines.append(
                f"- {target.id}: Git range {target.base_commit}..{target.head_commit}"
            )
        elif target.kind == "content":
            lines.append(
                f"- {target.id}: content sha256 {target.content_sha256}"
                + (f" ({target.purpose})" if target.purpose else "")
            )
            lines.append("```")
            lines.append(target.content)
            lines.append("```")
        else:
            exists = "exists" if target.exists else "missing"
            lines.append(
                f"- {target.id}: path `{target.path}` fingerprint {target.fingerprint} "
                f"({target.git_classification}, {exists})"
            )
    lines.append("For PASS, include every required target id in reviewed_target_ids.")
    return lines


def _append_reviewer_result_guidance(lines: list[str], ctx: TurnContext) -> None:
    if ctx.repair_reason:
        lines.extend(_controller_repair_lines(ctx))
    lines.extend(_repair_lines(ctx))
    lines.append("End with one valid AUTO_LOOP_RESULT.")


def build_reviewer_prompt(
    state: LifecycleState,
    ctx: TurnContext,
    review: ActiveReview,
) -> str:
    purpose = review.session_purpose
    if purpose == "plan_reviewer":
        lines = [
            "Review the current initial plan.",
            "",
            "This is the planning-only reviewer session.",
            "Do not assume implementation has started.",
            f"Read the task from the beginning (`{ctx.task_path}`).",
            "Inspect the repository as needed to test plan feasibility.",
            f"Review `{ctx.plan_path}` for coverage, ordering, risks, and verification.",
            "",
            "Return PASS only with zero findings.",
            "Do not modify product/control state.",
            "",
        ]
        lines.extend(_format_targets(review))
        if lines[-1] != "":
            lines.append("")
        _append_reviewer_result_guidance(lines, ctx)
        return _append_manifest("\n".join(lines), ctx.resource_manifest)

    if ctx.first_execution_turn:
        header = [
            "This is a fresh implementation-review session.",
            "",
            "The initial planning loop happened in separate sessions.",
            "Use the task, current plan, repository state, and durable review artifacts.",
            "Do not inherit or assume the plan reviewer's conclusions beyond the recorded PASS/findings.",
            "",
        ]
    else:
        header = ["Continue your reviewer role.", ""]

    if review.scope == "final":
        lines = header + [
            "Perform a whole-task final review.",
            "",
            "Do not limit yourself to the most recent diff.",
            f"Re-read `{ctx.task_path}` and evaluate the approved review evidence.",
            (
                f"Current HEAD is {ctx.head_commit}."
                if ctx.head_commit
                else "This review does not depend on a Git commit."
            ),
            "",
            "Only return COMPLETE if the whole task is satisfied with zero findings.",
            "",
        ]
        target_lines = _format_targets(review)
        if target_lines:
            lines.extend(target_lines)
            lines.append("")
        _append_reviewer_result_guidance(lines, ctx)
        return _append_manifest("\n".join(lines), ctx.resource_manifest)

    lines = header + [
        "Review request:",
        f"- session purpose: {purpose}",
        f"- review cycle: {review.cycle_id}",
        f"- round: {review.round}",
        f"- scope: {review.scope}",
        f"- target: {review.target}",
    ]
    if review.approved_base_commit or review.git_base:
        lines.append(f"- approved baseline: {review.approved_base_commit or review.git_base}")
    if review.current_candidate_head or review.git_head:
        lines.append(
            f"- candidate HEAD: {review.current_candidate_head or review.git_head}"
        )
    git_base = review.git_base
    git_head = review.git_head
    if git_base and git_head:
        lines.append(f"- primary diff: {git_base}..{git_head}")
    if review.worker_summary:
        lines.append(f"- worker summary: {review.worker_summary}")
    if review.plan_summary:
        lines.append(f"- planner summary: {review.plan_summary}")
    lines.extend(
        [
            f"- task: {ctx.task_path}",
            f"- plan: {ctx.plan_path}",
            f"- latest prior review: {_format_review_path(ctx.latest_review_path)}",
        ]
    )
    lines.extend(_plan_hash_lines(ctx))
    target_lines = _format_targets(review)
    if target_lines:
        lines.append("")
        lines.extend(target_lines)
    lines.extend(
        [
            "",
            "Independently review every required target.",
            "You may inspect related repository state outside the targets when needed.",
            "Do not modify product state, plan.md, or requested path targets.",
            "",
        ]
    )
    _append_reviewer_result_guidance(lines, ctx)
    return _append_manifest("\n".join(lines), ctx.resource_manifest)
