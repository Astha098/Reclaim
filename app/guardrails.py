from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app import db
from app.config import settings
from app.models import FailedPayment, SuppressionReason
from app.taxonomy import Action, Bucket, BucketPolicy


def customer_key(fp: FailedPayment) -> str:
    if fp.contact:
        return f"phone:{fp.contact.strip()}"

    if fp.email:
        return f"email:{fp.email.strip().lower()}"

    return f"order:{fp.order_id}"


@dataclass
class GuardContext:
    payment: FailedPayment
    bucket: Bucket
    policy: BucketPolicy
    attempt_no: int
    now: datetime = field(
        default_factory=lambda: datetime.now(UTC)
    )

    @property
    def contacts_customer(self) -> bool:
        return self.policy.contacts_customer


GuardResult = tuple[SuppressionReason, str] | None
Guard = Callable[[GuardContext], GuardResult]


def bucket_is_retryable(ctx: GuardContext) -> GuardResult:
    if (
        ctx.policy.max_attempts > 0
        and ctx.policy.action not in {
            Action.SUPPRESS,
            Action.MANUAL_REVIEW,
        }
    ):
        return None

    if ctx.bucket is Bucket.RISK_DECLINED:
        return (
            SuppressionReason.NOT_RETRYABLE,
            "risk_declined is never retried — "
            "the payment needs to stay blocked",
        )

    return (
        SuppressionReason.NOT_RETRYABLE,
        f"{ctx.bucket.value} is not retryable — "
        "queued for human review instead of guessing",
    )


def order_not_paid(ctx: GuardContext) -> GuardResult:
    if db.is_order_paid(ctx.payment.order_id):
        return (
            SuppressionReason.ALREADY_PAID,
            f"order {ctx.payment.order_id} is already paid — "
            "recovery would double-charge",
        )

    return None


def no_attempt_in_flight(ctx: GuardContext) -> GuardResult:
    if db.has_in_flight(ctx.payment.order_id):
        return (
            SuppressionReason.IN_FLIGHT,
            f"a pending attempt already exists for {ctx.payment.order_id}",
        )

    return None


def within_attempt_budget(ctx: GuardContext) -> GuardResult:
    made = db.count_attempts(ctx.payment.order_id)

    cap = min(
        settings.guardrails.max_attempts_per_order,
        ctx.policy.max_attempts,
    )

    if made >= cap:
        return (
            SuppressionReason.MAX_ATTEMPTS,
            f"{made}/{cap} attempts already made on this order "
            f"(global cap {settings.guardrails.max_attempts_per_order}, "
            f"{ctx.bucket.value} cap {ctx.policy.max_attempts})",
        )

    return None


def cooldown_elapsed(ctx: GuardContext) -> GuardResult:
    last = db.last_attempt_at(ctx.payment.order_id)

    if last is None:
        return None

    cooldown = timedelta(
        minutes=settings.guardrails.min_cooldown_minutes
    )

    elapsed = ctx.now - last

    if elapsed < cooldown:
        remaining = (
            cooldown - elapsed
        ).total_seconds() / 60

        return (
            SuppressionReason.COOLDOWN,
            f"last attempt was "
            f"{elapsed.total_seconds() / 60:.0f}m ago; "
            f"{remaining:.0f}m of cooldown remaining",
        )

    return None


def customer_not_suppressed(ctx: GuardContext) -> GuardResult:
    if not ctx.contacts_customer:
        return None

    key = customer_key(ctx.payment)

    if db.is_suppressed(key):
        return (
            SuppressionReason.NO_CONSENT,
            f"{key} is on the suppression list "
            "(DND / unsubscribed)",
        )

    return None


def contact_channel_exists(ctx: GuardContext) -> GuardResult:
    if not ctx.contacts_customer:
        return None

    if not (ctx.payment.contact or ctx.payment.email):
        return (
            SuppressionReason.NO_CONTACT_CHANNEL,
            "no phone or email on the payment — "
            "a customer-contacting action would be a no-op",
        )

    return None


def customer_contact_budget(ctx: GuardContext) -> GuardResult:
    if not ctx.contacts_customer:
        return None

    key = customer_key(ctx.payment)
    since = ctx.now - timedelta(days=1)

    sent = db.contacts_since(
        key,
        since,
    )

    cap = settings.guardrails.max_contacts_per_customer_per_day

    if sent >= cap:
        return (
            SuppressionReason.CUSTOMER_CONTACT_CAP,
            f"{key} has had {sent}/{cap} "
            "recovery messages in the last 24h",
        )

    return None


def merchant_contact_budget(ctx: GuardContext) -> GuardResult:
    if not ctx.contacts_customer:
        return None

    since = ctx.now - timedelta(days=1)

    sent = db.merchant_contacts_since(
        ctx.payment.merchant_id,
        since,
    )

    cap = settings.guardrails.max_contacts_per_merchant_per_day

    if sent >= cap:
        return (
            SuppressionReason.MERCHANT_CONTACT_CAP,
            f"merchant {ctx.payment.merchant_id} has sent "
            f"{sent}/{cap} recovery messages in 24h — "
            "global contact limit reached",
        )

    return None


# Run the cheapest and most important checks first.
GUARDS: list[Guard] = [
    bucket_is_retryable,
    order_not_paid,
    no_attempt_in_flight,
    within_attempt_budget,
    cooldown_elapsed,
    customer_not_suppressed,
    contact_channel_exists,
    customer_contact_budget,
    merchant_contact_budget,
]


def evaluate(
    ctx: GuardContext,
) -> tuple[SuppressionReason | None, list[str]]:
    """Run all safety checks and build the decision trace."""

    trace: list[str] = []

    for guard in GUARDS:
        result = guard(ctx)
        name = guard.__name__

        if result is None:
            trace.append(f"✓ {name}")
            continue

        reason, explanation = result

        trace.append(
            f"✗ {name}: {explanation}"
        )

        # Stop at the first failed guard.
        return reason, trace

    return None, trace
