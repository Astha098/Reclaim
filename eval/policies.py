"""The policy ladder.

The eval is not a single A/B. It is an ablation: five policies where each rung
adds exactly one mechanism to the one below it, so the uplift table attributes
recovered revenue to a specific decision rather than to "the AI".

    0. do_nothing          the floor. No recovery at all — the denominator.
    1. naive_retry         retry everything immediately on the same rail, twice.
                           This is not a straw man; it is what a cron job over
                           failed orders does, and it is what most merchants have.
    2. bucket_actions      + the failure taxonomy. Right *action* per bucket
                           (UPI intent link for abandonment, new instrument for a
                           dead card) but still fired immediately.
    3. smart_timing        + timing. Payday-aligned for balance failures, intent-
                           decay-aware for abandonment, quiet hours respected.
    4. circuit_aware       + per-(rail, issuer) circuit breaking, so retries stop
                           firing into an outage.
    5. rail_steering       + rail steering, so a retry that would hit a dead card
                           issuer goes out over live UPI instead.
    6. full_agent          + guardrails. The shipped system.

Why an ablation rather than one comparison: a single "agent vs naive" number
invites the obvious objection that the gap comes from one trick, or from the
simulator being tuned to favour the agent. A ladder answers "which mechanism
earned the money" — and it exposes any rung that adds nothing, which is
information worth having even when it is unflattering.

The last two rungs are split deliberately, because collapsing them hides the most
honest number in the table. Rail steering *earns* revenue; the guardrails *cost*
it — they refuse to retry risk declines, cap attempts, and drop uncontactable
customers. Reported together they roughly cancel and the final rung looks like it
does nothing. Reported separately you can see the actual trade being made, and
decide for yourself whether the compliance is worth the delta. A system that
quietly bundles its own safety costs into an unrelated win is not being measured;
it is being marketed.


Every rung shares the same classification, the same efficacy model, and the same
random draws. The only thing that varies is the mechanism set below.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mechanisms:
    """Which decision mechanisms a policy is allowed to use."""

    name: str
    label: str
    description: str

    # Attempt anything at all.
    attempts_recovery: bool = True
    # Choose the action from the failure bucket, instead of one generic retry.
    bucket_actions: bool = False
    # Use `app.timing` instead of firing immediately.
    smart_timing: bool = False
    # Hold retries while an issuer's circuit is open.
    circuit_breaker: bool = False
    # Switch to a healthy alternate rail rather than deferring or reusing a rail
    # the customer just failed on.
    rail_steering: bool = False
    # Never-retry buckets, attempt caps, contactability, cooldowns.
    guardrails: bool = False

    max_attempts: int = 2


LADDER: list[Mechanisms] = [
    Mechanisms(
        name="do_nothing",
        label="No recovery",
        description="Failed payment stays failed. The revenue actually being lost today.",
        attempts_recovery=False,
        max_attempts=0,
    ),
    Mechanisms(
        name="naive_retry",
        label="Naive retry",
        description=(
            "Retry every failed payment immediately on the same rail, up to twice. "
            "What a cron job over failed orders does."
        ),
        max_attempts=2,
    ),
    Mechanisms(
        name="bucket_actions",
        label="+ failure taxonomy",
        description=(
            "Pick the action from the failure bucket — UPI intent link for an "
            "abandoned auth, a new instrument for a dead card — but still fire "
            "immediately."
        ),
        bucket_actions=True,
    ),
    Mechanisms(
        name="smart_timing",
        label="+ timing",
        description=(
            "Add when. Payday-aligned for balance failures, immediate for decaying "
            "intent, quiet hours respected for anything that pings a human."
        ),
        bucket_actions=True,
        smart_timing=True,
    ),
    Mechanisms(
        name="circuit_aware",
        label="+ circuit breaking",
        description=(
            "Add per-(rail, issuer) circuit breaking, so queued retries stop firing "
            "into a live issuer outage."
        ),
        bucket_actions=True,
        smart_timing=True,
        circuit_breaker=True,
    ),
    Mechanisms(
        name="rail_steering",
        label="+ rail steering",
        description=(
            "Add where. A card retry that would hit a dead issuer becomes a live "
            "UPI one instead of waiting for the card to come back."
        ),
        bucket_actions=True,
        smart_timing=True,
        circuit_breaker=True,
        rail_steering=True,
    ),
    Mechanisms(
        name="full_agent",
        label="Full agent (+ guardrails)",
        description=(
            "Add the guardrails: never retry a risk decline, cap attempts per "
            "order, require a contact channel, respect quiet hours and contact "
            "budgets. Expected to *cost* recovered revenue — that is the trade."
        ),
        bucket_actions=True,
        smart_timing=True,
        circuit_breaker=True,
        rail_steering=True,
        guardrails=True,
    ),
]

BY_NAME = {m.name: m for m in LADDER}
