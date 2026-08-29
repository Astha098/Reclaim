from __future__ import annotations

import logging
from datetime import UTC, datetime

from app import db, guardrails, issuer_health, timing as timing_mod
from app.models import (
    Classification,
    FailedPayment,
    RecoveryAttempt,
    RecoveryPlan,
    SuppressionReason,
)
from app.taxonomy import Action, Bucket, Rail, Timing, choose_rail, policy_for

log = logging.getLogger("reclaim.policy")


RAIL_FALLBACK_ORDER: tuple[Rail, ...] = (
    Rail.UPI,
    Rail.NETBANKING,
    Rail.CARD,
    Rail.WALLET,
)


def _issuer_key_for(fp: FailedPayment, rail: Rail) -> str:
    return f"{rail.value}:{(fp.issuer or 'unknown').upper()}"


def _select_rail(
    fp: FailedPayment,
    bucket: Bucket,
    now: datetime,
) -> tuple[Rail, str, bool, list[str]]:
    trace: list[str] = []
    pol = policy_for(bucket)

    # Choose the preferred rail for this type of failure.
    preferred = choose_rail(bucket, fp.rail)

    if preferred != fp.rail:
        trace.append(
            f"→ rail steered {fp.rail.value} → {preferred.value} "
            f"({'excluded by bucket' if fp.rail in pol.excluded_rails else 'bucket preference'})"
        )

    # Check whether the issuer is healthy enough for another attempt.
    key = _issuer_key_for(fp, preferred)
    allowed, why = issuer_health.allows_attempt(key, now)

    trace.append(("✓ " if allowed else "✗ ") + why)

    if allowed:
        return preferred, key, True, trace

    # Try another rail if the preferred one is currently unavailable.
    for alt in RAIL_FALLBACK_ORDER:
        if alt == preferred or alt in pol.excluded_rails or alt == fp.rail:
            continue

        alt_key = _issuer_key_for(fp, alt)
        alt_allowed, alt_why = issuer_health.allows_attempt(
            alt_key,
            now,
        )

        if alt_allowed:
            trace.append(
                f"→ circuit open on {preferred.value}, "
                f"failing over to {alt.value}: {alt_why}"
            )
            return alt, alt_key, True, trace

        trace.append(
            f"  ✗ {alt.value} also unavailable: {alt_why}"
        )

    trace.append(
        "→ no healthy rail available; "
        "deferring rather than burning an attempt"
    )

    return preferred, key, False, trace


def _classification_trace(
    classification: Classification,
) -> list[str]:
    """Build the classification details shown in the decision log."""

    source = classification.source.value

    # Show whether Gemini or the rules made the final call.
    if source == "llm":
        label = "AI diagnosis"
        provider = "Gemini"

    elif source == "llm_fallback_rules":
        label = "Fallback classification"
        provider = "rules (Gemini unavailable)"

    elif source == "rules":
        label = "Classification"
        provider = "rules"

    elif source == "abstained":
        label = "AI abstention"
        provider = "Gemini"

    else:
        label = "Classification"
        provider = source

    trace: list[str] = [
        (
            f"{label}: {classification.bucket.value} "
            f"@ {classification.confidence:.0%} "
            f"via {provider}"
        )
    ]

    # Keep the rule that matched so we can understand the decision later.
    if classification.matched_rule:
        trace.append(
            f"rules matched: {classification.matched_rule}"
        )

    # In hybrid mode, keep the original rules result for comparison.
    if classification.rules_bucket is not None:
        trace.append(
            f"rules hypothesis: "
            f"{classification.rules_bucket.value} "
            f"@ {classification.rules_confidence:.0%}"
        )

    # Flag cases where Gemini and the rules reached different conclusions.
    if classification.disagreement:
        trace.append(
            "⚠ AI/rules disagreement — "
            "AI decision reviewed against deterministic rules"
        )

    # Show the extra information when an AI decision was involved.
    if source in {"llm", "abstained", "llm_fallback_rules"}:

        if classification.alternative_bucket is not None:
            trace.append(
                f"AI alternative: "
                f"{classification.alternative_bucket.value}"
            )

        if classification.action_risk:
            trace.append(
                f"AI action risk: "
                f"{classification.action_risk}"
            )

        if classification.reasoning:
            trace.append(
                f"AI reasoning: "
                f"{classification.reasoning}"
            )

        # Keep the log readable by showing only the first few evidence items.
        for evidence in classification.evidence[:4]:
            trace.append(
                f"AI evidence: {evidence}"
            )

        if classification.latency_ms is not None:
            trace.append(
                f"AI latency: "
                f"{classification.latency_ms}ms"
            )

    # Explain when Gemini was unavailable and the rules took over.
    if source == "llm_fallback_rules":
        trace.append(
            "⚠ Gemini unavailable — "
            "deterministic rules verdict used safely"
        )

    # Explain when Gemini chose not to make a confident decision.
    if source == "abstained":
        trace.append(
            "⚠ AI abstained — "
            "confidence was below the safety threshold"
        )

    return trace


def _why_not_actions(
    bucket: Bucket,
    action: Action,
    action_risk: str | None,
) -> list[str]:
    """Explain why the policy chose this recovery path."""

    reasons: list[str] = []

    if bucket is Bucket.RISK_DECLINED:
        reasons.append(
            "why this action: risk declines must stay blocked"
        )
        reasons.append(
            "why not retry: retrying a risk decline is unsafe"
        )
        reasons.append(
            "why not switch rail: changing rails should not bypass a risk decision"
        )
        reasons.append(
            "why not contact customer: no automated recovery action is appropriate"
        )

    elif bucket is Bucket.INSTRUMENT_INVALID:
        reasons.append(
            "why this action: the payment instrument itself is not usable"
        )
        reasons.append(
            "why not retry: the same instrument is expected to fail again"
        )
        reasons.append(
            "why not switch rail: the customer needs to use a valid payment method"
        )

    elif bucket is Bucket.ISSUER_DOWN:
        reasons.append(
            "why this action: wait for the issuer to recover"
        )
        reasons.append(
            "why not retry now: another attempt may fail while the issuer is unhealthy"
        )
        reasons.append(
            "why not contact customer: the problem is on the issuer/network side"
        )

    elif bucket is Bucket.INSUFFICIENT_FUNDS:
        reasons.append(
            "why this action: give the customer time to restore the balance"
        )
        reasons.append(
            "why not immediate retry: the balance is unlikely to change within minutes"
        )

    elif bucket is Bucket.AUTH_ABANDONED:
        reasons.append(
            "why this action: the customer showed payment intent but did not finish authentication"
        )
        reasons.append(
            "why not wait too long: payment intent can drop quickly"
        )

    elif bucket is Bucket.LIMIT_EXCEEDED:
        reasons.append(
            "why this action: the current payment route has reached a limit"
        )
        reasons.append(
            "why not repeat the same attempt: the same limit is likely to reject it again"
        )

    elif bucket is Bucket.TECHNICAL:
        reasons.append(
            "why this action: the failure looks temporary and may succeed on retry"
        )
        reasons.append(
            "why not contact customer: no customer action is needed for a technical fault"
        )

    elif bucket is Bucket.CUSTOMER_CANCELLED:
        reasons.append(
            "why this action: the customer explicitly cancelled the payment"
        )
        reasons.append(
            "why not retry: restarting a cancelled payment would ignore customer intent"
        )

    else:
        reasons.append(
            "why this action: the failure cannot be classified safely"
        )
        reasons.append(
            "why not retry: unknown failures are not safe to retry automatically"
        )
        reasons.append(
            "why not switch rail: the underlying failure is not understood"
        )

    if action is Action.AWAIT_ISSUER_HEALTH:
        reasons.append(
            "why wait: the issuer circuit must recover before another attempt"
        )

    if action is Action.SUPPRESS:
        reasons.append(
            "final safety check: no automated recovery attempt will be made"
        )

    if action is Action.MANUAL_REVIEW:
        reasons.append(
            "final safety check: a human should review the payment before acting"
        )

    if action_risk == "high":
        reasons.append(
            "safety: high-risk classification requires conservative recovery"
        )

    return reasons


def decide(
    fp: FailedPayment,
    classification: Classification,
    *,
    now: datetime | None = None,
    attempt_no: int | None = None,
) -> RecoveryPlan:
    now = now or datetime.now(UTC)

    bucket = classification.bucket
    pol = policy_for(bucket)

    if attempt_no is None:
        attempt_no = db.count_attempts(fp.order_id) + 1

    # Start the trace with the classification details.
    trace: list[str] = _classification_trace(classification)

    trace.append(
        f"policy: {pol.action.value} / "
        f"{pol.timing.value} / "
        f"prefer {pol.preferred_rail.value}"
    )

    def suppressed(
        reason: SuppressionReason,
        extra: list[str],
    ) -> RecoveryPlan:
        return RecoveryPlan(
            payment_id=fp.payment_id,
            order_id=fp.order_id,
            bucket=bucket,
            action=Action.SUPPRESS,
            timing=Timing.IMMEDIATE,
            rail=fp.rail,
            attempt_no=attempt_no,
            scheduled_for=now,
            suppressed=True,
            suppression_reason=reason,
            decision_trace=trace + extra,
            expected_recovery_rate=0.0,
            amount_paise=fp.amount_paise,
        )

    # Run the safety checks before scheduling any recovery.
    ctx = guardrails.GuardContext(
        payment=fp,
        bucket=bucket,
        policy=pol,
        attempt_no=attempt_no,
        now=now,
    )

    veto, guard_trace = guardrails.evaluate(ctx)

    if veto is not None:
        return suppressed(
            veto,
            guard_trace,
        )

    trace.extend(guard_trace)

    # Explain the policy decision before choosing the final rail.
    trace.extend(
        _why_not_actions(
            bucket,
            pol.action,
            classification.action_risk,
        )
    )

    # Check issuer health and choose the best available rail.
    rail, issuer_key, healthy, rail_trace = _select_rail(
        fp,
        bucket,
        now,
    )

    trace.extend(rail_trace)

    action = pol.action
    plan_timing = pol.timing

    # Don't spend an attempt while the issuer is unhealthy.
    if not healthy:
        action = Action.AWAIT_ISSUER_HEALTH
        plan_timing = Timing.ISSUER_HEALTH_GATED

        trace.append(
            "decision changed: issuer is unhealthy, "
            "so recovery is waiting for issuer health"
        )

        trace.extend(
            _why_not_actions(
                bucket,
                action,
                classification.action_risk,
            )
        )

    # Schedule the recovery according to the policy.
    scheduled_for = timing_mod.schedule(
        plan_timing,
        now=now,
        attempt_no=attempt_no,
        contacts_customer=pol.contacts_customer,
    )

    trace.append(
        timing_mod.describe(
            plan_timing,
            scheduled_for,
            now,
        )
    )

    # Record whether the customer will be contacted.
    if pol.contacts_customer:
        trace.append(
            "will contact customer via "
            f"{'sms/whatsapp' if fp.contact else 'email'}"
        )
    else:
        trace.append(
            "silent recovery — no customer contact"
        )

    return RecoveryPlan(
        payment_id=fp.payment_id,
        order_id=fp.order_id,
        bucket=bucket,
        action=action,
        timing=plan_timing,
        rail=rail,
        attempt_no=attempt_no,
        scheduled_for=scheduled_for,
        suppressed=False,
        decision_trace=trace,
        expected_recovery_rate=pol.base_recovery_rate,
        amount_paise=fp.amount_paise,
    )


def plan_to_attempt(
    plan: RecoveryPlan,
    fp: FailedPayment,
) -> RecoveryAttempt:
    from app.models import AttemptOutcome

    # Turn the recovery plan into an attempt for the scheduler.
    return RecoveryAttempt(
        payment_id=plan.payment_id,
        order_id=plan.order_id,
        merchant_id=fp.merchant_id,
        attempt_no=plan.attempt_no,
        bucket=plan.bucket,
        action=plan.action,
        timing=plan.timing,
        rail=plan.rail,
        amount_paise=plan.amount_paise,
        idempotency_key=RecoveryAttempt.make_idempotency_key(
            plan.order_id,
            plan.attempt_no,
        ),
        scheduled_for=plan.scheduled_for,
        outcome=(
            AttemptOutcome.SUPPRESSED
            if plan.suppressed
            else AttemptOutcome.PENDING
        ),
        suppression_reason=plan.suppression_reason,
        executed_at=(
            datetime.now(UTC)
            if plan.suppressed
            else None
        ),
        decision_trace=plan.decision_trace,
    )


def explain(plan: RecoveryPlan) -> str:
    # Return a short explanation for logs and the dashboard.
    if plan.suppressed:
        return (
            f"[{plan.order_id}] "
            f"SUPPRESSED "
            f"({plan.suppression_reason.value if plan.suppression_reason else '?'}) "
            f"bucket={plan.bucket.value}"
        )

    return (
        f"[{plan.order_id}] "
        f"{plan.action.value} "
        f"on {plan.rail.value} "
        f"attempt {plan.attempt_no} "
        f"bucket={plan.bucket.value} "
        f"at {plan.scheduled_for:%H:%M:%S} "
        f"(p≈{plan.expected_recovery_rate:.0%})"
    )
