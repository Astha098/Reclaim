"""Execute recovery plans after checking the latest payment state."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app import (
    db,
    guardrails,
    issuer_health,
    messaging,
    policy,
    razorpay_client,
    timing as timing_mod,
)
from app.config import settings
from app.models import (
    AttemptOutcome,
    CircuitState,
    FailedPayment,
    RecoveryPlan,
    SuppressionReason,
)
from app.simulator import Treatment, converts, latent_traits
from app.taxonomy import Action, Bucket, Rail, policy_for

log = logging.getLogger("reclaim.recovery")


# Limit how long an issuer health check can keep an attempt waiting.
MAX_DEFERRALS = 6

MERCHANT_NAME = "Kirana Club"


# These actions require the customer to complete a payment link.
LINK_ACTIONS = frozenset(
    {
        Action.CONTACT_CUSTOMER,
    }
)


def ingest_and_plan(fp: FailedPayment, classification) -> RecoveryPlan:
    """Classify a payment, create a recovery plan, and save the attempt."""

    db.insert_classification(fp.payment_id, classification)

    issuer_health.observe(
        fp.issuer_key,
        success=False,
        at=fp.failed_at,
    )

    plan = policy.decide(fp, classification)
    attempt = policy.plan_to_attempt(plan, fp)
    attempt_id = db.create_attempt(attempt)

    if attempt_id is None:
        log.info(
            "idempotency collision on %s, skipping",
            attempt.idempotency_key,
        )
        plan.decision_trace.append(
            "✗ idempotency collision — another worker owns this attempt"
        )
        plan.suppressed = True
        plan.suppression_reason = SuppressionReason.IN_FLIGHT
        return plan

    log.info("%s", policy.explain(plan))
    return plan


def execute(
    attempt_id: int,
    *,
    now: datetime | None = None,
) -> AttemptOutcome:
    """Execute one pending recovery attempt."""

    now = now or datetime.now(UTC)

    row = db.attempt_row(attempt_id)
    if row is None:
        return AttemptOutcome.EXPIRED

    if row["outcome"] != AttemptOutcome.PENDING.value:
        return AttemptOutcome(row["outcome"])

    payment_row = db.get_payment(row["payment_id"])
    if payment_row is None:
        db.update_attempt(
            attempt_id,
            outcome=AttemptOutcome.EXPIRED,
            executed_at=now,
            extra_trace=["✗ payment record missing"],
        )
        return AttemptOutcome.EXPIRED

    fp = db.payment_to_model(payment_row)
    bucket = Bucket(row["bucket"])
    action = Action(row["action"])
    rail = Rail(row["rail"])
    attempt_no = int(row["attempt_no"])
    pol = policy_for(bucket)

    trace: list[str] = []

    # Re-check the order before doing anything that can charge or contact.
    if db.is_order_paid(fp.order_id):
        db.update_attempt(
            attempt_id,
            outcome=AttemptOutcome.SUPPRESSED,
            executed_at=now,
            extra_trace=[
                "✗ order already paid — recovery stopped to avoid a duplicate charge"
            ],
        )
        return AttemptOutcome.SUPPRESSED

    # Make sure the issuer is healthy before attempting recovery.
    issuer_key = f"{rail.value}:{(fp.issuer or 'unknown').upper()}"
    allowed, why = issuer_health.allows_attempt(
        issuer_key,
        now,
    )
    health = issuer_health.evaluate(
        issuer_key,
        now,
    )

    if not allowed:
        deferrals = int(row["deferrals"])

        if deferrals >= MAX_DEFERRALS:
            db.update_attempt(
                attempt_id,
                outcome=AttemptOutcome.EXPIRED,
                executed_at=now,
                extra_trace=[
                    f"✗ {why}",
                    f"✗ abandoned after {deferrals} deferrals",
                ],
            )
            return AttemptOutcome.EXPIRED

        next_check = timing_mod.schedule(
            timing_mod.Timing.ISSUER_HEALTH_GATED,
            now=now,
            attempt_no=attempt_no,
        )

        db.defer_attempt(
            attempt_id,
            next_check,
            f"⏸ {why} — deferred #{deferrals + 1}",
        )

        return AttemptOutcome.PENDING

    trace.append(f"✓ {why}")

    # Waiting for issuer health ends with the policy action.
    if action is Action.AWAIT_ISSUER_HEALTH:
        action = (
            pol.action
            if pol.action is not Action.AWAIT_ISSUER_HEALTH
            else Action.RETRY
        )
        trace.append(
            f"→ issuer recovered; continuing with {action.value}"
        )

    # A retry without a saved payment instrument cannot be silent.
    if action is Action.RETRY and not fp.tokenized:
        action = Action.CONTACT_CUSTOMER
        trace.append(
            "→ no saved payment authorization — customer must complete the payment"
        )

    # Execute a direct retry.
    if action is Action.RETRY:
        return _execute_retry(
            attempt_id,
            fp,
            bucket,
            rail,
            attempt_no,
            health,
            now,
            trace,
        )

    # Execute a customer-contact flow.
    if action is Action.CONTACT_CUSTOMER:
        return _execute_customer_contact(
            attempt_id,
            fp,
            bucket,
            rail,
            attempt_no,
            health,
            now,
            trace,
        )

    # Rail switching still uses the customer-completed flow.
    if action is Action.SWITCH_RAIL:
        return _execute_customer_contact(
            attempt_id,
            fp,
            bucket,
            rail,
            attempt_no,
            health,
            now,
            trace,
        )

    # These actions should normally be stopped by the policy or guardrails.
    db.update_attempt(
        attempt_id,
        outcome=AttemptOutcome.SUPPRESSED,
        executed_at=now,
        extra_trace=trace + [
            f"✗ no executor for action {action.value}"
        ],
    )

    return AttemptOutcome.SUPPRESSED


def _delay_hours(
    fp: FailedPayment,
    now: datetime,
) -> float:
    """Return the time between the original failure and execution."""

    return max(
        0.0,
        (now - fp.failed_at).total_seconds() / 3600.0,
    )


def _resolve(
    attempt_id: int,
    fp: FailedPayment,
    bucket: Bucket,
    action: Action,
    rail: Rail,
    attempt_no: int,
    health,
    now: datetime,
    trace: list[str],
    *,
    contacted: bool,
    link_id: str | None = None,
    link_url: str | None = None,
    message_body: str | None = None,
) -> AttemptOutcome:
    """Simulate the result and save the attempt outcome."""

    traits = latent_traits(
        fp.payment_id,
        bucket,
    )

    treatment = Treatment(
        bucket=bucket,
        action=action,
        rail=rail,
        attempt_no=attempt_no,
        delay_hours=_delay_hours(fp, now),
        circuit_state=(
            health.state
            if health
            else CircuitState.CLOSED
        ),
        amount_paise=fp.amount_paise,
    )

    won, probability, factors = converts(
        fp.payment_id,
        traits,
        treatment,
    )

    trace.append(
        f"simulated outcome: p={probability:.3f} → "
        f"{'RECOVERED' if won else 'no conversion'}"
    )
    trace.append(
        "factors: " + " · ".join(factors)
    )

    issuer_key = (
        f"{rail.value}:{(fp.issuer or 'unknown').upper()}"
    )

    issuer_health.observe(
        issuer_key,
        success=won,
        at=now,
    )
    issuer_health.consume_probe(issuer_key)

    outcome = (
        AttemptOutcome.RECOVERED
        if won
        else (
            AttemptOutcome.NO_RESPONSE
            if contacted
            else AttemptOutcome.FAILED_AGAIN
        )
    )

    recovered = fp.amount_paise if won else 0

    if won:
        db.mark_order_paid(
            fp.order_id,
            via=f"recovery:{action.value}:{rail.value}",
            at=now,
        )

    db.update_attempt(
        attempt_id,
        outcome=outcome,
        executed_at=now,
        recovered_paise=recovered,
        payment_link_id=link_id,
        payment_link_url=link_url,
        message_body=message_body,
        contacted_customer=contacted,
        extra_trace=trace,
    )

    if not won:
        _schedule_followup(
            fp,
            bucket,
            attempt_no,
            now,
        )

    return outcome


def _schedule_followup(
    fp: FailedPayment,
    bucket: Bucket,
    attempt_no: int,
    now: datetime,
) -> None:
    """Create the next attempt when the bucket still allows one."""

    pol = policy_for(bucket)

    cap = min(
        settings.guardrails.max_attempts_per_order,
        pol.max_attempts,
    )

    if attempt_no >= cap:
        return

    earliest = now + timedelta(
        minutes=settings.guardrails.min_cooldown_minutes
    )

    try:
        classification = db.get_classification(
            fp.payment_id
        )

        if classification is None:
            return

        plan = policy.decide(
            fp,
            classification,
            now=earliest,
            attempt_no=attempt_no + 1,
        )

        attempt = policy.plan_to_attempt(
            plan,
            fp,
        )

        if plan.suppressed:
            db.create_attempt(attempt)
            return

        if db.create_attempt(attempt) is not None:
            log.info(
                "follow-up %s attempt %s scheduled for %s",
                fp.order_id,
                plan.attempt_no,
                plan.scheduled_for.isoformat(),
            )

    except Exception:
        log.exception(
            "failed to schedule follow-up for %s",
            fp.order_id,
        )


def _execute_retry(
    attempt_id: int,
    fp: FailedPayment,
    bucket: Bucket,
    rail: Rail,
    attempt_no: int,
    health,
    now: datetime,
    trace: list[str],
) -> AttemptOutcome:
    """Run a retry using the existing payment authorization."""

    trace.append(
        f"→ retrying payment on {rail.value}"
    )

    return _resolve(
        attempt_id,
        fp,
        bucket,
        Action.RETRY,
        rail,
        attempt_no,
        health,
        now,
        trace,
        contacted=False,
    )


def _execute_customer_contact(
    attempt_id: int,
    fp: FailedPayment,
    bucket: Bucket,
    rail: Rail,
    attempt_no: int,
    health,
    now: datetime,
    trace: list[str],
) -> AttemptOutcome:
    """Create a payment link and send it to the customer."""

    action = Action.CONTACT_CUSTOMER

    plan = RecoveryPlan(
        payment_id=fp.payment_id,
        order_id=fp.order_id,
        bucket=bucket,
        action=action,
        timing=policy_for(bucket).timing,
        rail=rail,
        attempt_no=attempt_no,
        scheduled_for=now,
        amount_paise=fp.amount_paise,
    )

    try:
        link = razorpay_client.client().create_payment_link(
            amount_paise=fp.amount_paise,
            reference_id=f"{fp.order_id}-r{attempt_no}",
            description=f"Recovery for order {fp.order_id}",
            contact=fp.contact,
            email=fp.email,
            upi_only=rail is Rail.UPI,
            notes={
                "reclaim_bucket": bucket.value,
                "reclaim_attempt": str(attempt_no),
                "original_payment_id": fp.payment_id,
            },
        )

    except razorpay_client.RazorpayError as exc:
        db.update_attempt(
            attempt_id,
            outcome=AttemptOutcome.FAILED_AGAIN,
            executed_at=now,
            extra_trace=[
                *trace,
                f"✗ payment link creation failed: {exc}",
            ],
        )
        return AttemptOutcome.FAILED_AGAIN

    trace.append(
        f"→ created payment link {link.id} ({link.short_url})"
    )

    message = messaging.compose(
        fp,
        plan,
        MERCHANT_NAME,
    )

    rendered: str | None = None
    contacted = False

    if message.channel is messaging.Channel.NONE:
        trace.append(
            "✗ no contact channel — link created but not delivered"
        )
    else:
        delivered, detail = messaging.send(
            message,
            fp,
            link.short_url,
        )

        rendered = message.rendered(
            link.short_url
        )
        contacted = delivered

        trace.append(
            f"→ message [{message.generated_by}] "
            f"{'sent' if delivered else 'FAILED'}: {detail}"
        )

        if delivered:
            db.record_contact(
                guardrails.customer_key(fp),
                fp.merchant_id,
                fp.order_id,
                message.channel.value,
            )

    return _resolve(
        attempt_id,
        fp,
        bucket,
        action,
        rail,
        attempt_no,
        health,
        now,
        trace,
        contacted=contacted,
        link_id=link.id,
        link_url=link.short_url,
        message_body=rendered,
    )


def complete_by_link(
    payment_link_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Mark a payment link as completed by the customer."""

    now = now or datetime.now(UTC)

    row = db.link_to_attempt(
        payment_link_id
    )

    if row is None:
        return {
            "ok": False,
            "error": "unknown payment link",
        }

    order_id = row["order_id"]

    if db.is_order_paid(order_id):
        return {
            "ok": False,
            "error": "order already paid",
            "order_id": order_id,
        }

    amount = int(
        row["amount_paise"]
    )

    db.mark_order_paid(
        order_id,
        via="recovery:link_click",
        at=now,
    )

    db.update_attempt(
        int(row["id"]),
        outcome=AttemptOutcome.RECOVERED,
        executed_at=now,
        recovered_paise=amount,
        extra_trace=[
            "✓ payment link completed by customer"
        ],
    )

    issuer_health.observe(
        f"{row['rail']}:UNKNOWN",
        success=True,
        at=now,
    )

    if settings.use_mock_razorpay:
        razorpay_client.mock().mark_paid(
            payment_link_id
        )

    return {
        "ok": True,
        "order_id": order_id,
        "recovered_paise": amount,
        "bucket": row["bucket"],
    }
