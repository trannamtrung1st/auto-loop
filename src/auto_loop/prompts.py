"""Turn prompt construction for persistent role sessions."""

from __future__ import annotations

from dataclasses import dataclass

from auto_loop.lifecycle import ActiveReview, LifecycleState


@dataclass(frozen=True)
class TurnContext:
    task_path: str
    plan_path: str
    latest_review_path: str | None
    head_commit: str
    product_clean: bool
    interrupted: bool = False


def _format_review_path(path: str | None) -> str:
    return path or "(none yet)"


def build_worker_prompt(state: LifecycleState, ctx: TurnContext) -> str:
    lines = [
        "Continue your worker role for this lifecycle.",
        "",
        "Durable current state:",
        f"- task: {ctx.task_path}",
        f"- plan: {ctx.plan_path}",
        f"- latest review: {_format_review_path(ctx.latest_review_path)}",
        f"- plan approved: {'true' if state.plan_approved else 'false'}",
        f"- last approved product commit: {state.last_approved_commit}",
        f"- current HEAD: {ctx.head_commit}",
        f"- product working tree: {'clean' if ctx.product_clean else 'dirty'}",
        "",
        "Reconcile this durable state with your session memory, applicable repository "
        "instructions/skills, and declared task resources, then perform the single best "
        "next worker turn according to your role instructions.",
        "",
    ]
    if ctx.interrupted:
        lines.extend(
            [
                "The prior worker invocation may have been interrupted.",
                "Inspect current Git/files first and reconcile partial work before deciding "
                "the next action.",
                "Do not assume the interrupted turn completed.",
                "",
            ]
        )
    lines.append("End with one valid AUTO_LOOP_RESULT.")
    return "\n".join(lines)


def build_reviewer_prompt(
    state: LifecycleState,
    ctx: TurnContext,
    review: ActiveReview,
) -> str:
    if review.scope == "final":
        return "\n".join(
            [
                "Perform a whole-task final review.",
                "",
                "Do not limit yourself to the most recent diff.",
                f"Re-read `{ctx.task_path}` and evaluate the entire repository at HEAD "
                f"{ctx.head_commit}.",
                "The current HEAD is already the last scoped-review-approved commit.",
                "",
                "Only return COMPLETE if the whole task is satisfied with zero findings.",
                "",
                "End with one valid AUTO_LOOP_RESULT.",
            ]
        )

    lines = [
        "Continue your reviewer role.",
        "",
        "Review request:",
        f"- scope: {review.scope}",
        f"- target: {review.target}",
    ]
    if review.base_commit:
        lines.append(f"- approved baseline: {review.base_commit}")
    if review.head_commit:
        lines.append(f"- candidate HEAD: {review.head_commit}")
    if review.base_commit and review.head_commit:
        lines.append(f"- primary diff: {review.base_commit}..{review.head_commit}")
    if review.worker_summary:
        lines.append(f"- worker summary: {review.worker_summary}")
    lines.extend(
        [
            f"- task: {ctx.task_path}",
            f"- plan: {ctx.plan_path}",
            f"- latest prior review: {_format_review_path(ctx.latest_review_path)}",
            "",
            "Independently review the complete candidate range.",
            "You may inspect related repository state outside the diff where needed.",
            "Do not modify product state.",
            "",
            "End with one valid AUTO_LOOP_RESULT.",
        ]
    )
    return "\n".join(lines)
