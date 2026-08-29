from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Bucket(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    AUTH_ABANDONED = "auth_abandoned"
    CUSTOMER_CANCELLED = "customer_cancelled"
    INSTRUMENT_INVALID = "instrument_invalid"
    LIMIT_EXCEEDED = "limit_exceeded"
    ISSUER_DOWN = "issuer_down"
    RISK_DECLINED = "risk_declined"
    TECHNICAL = "technical"
    UNKNOWN = "unknown"


class Rail(str, Enum):
    UPI = "upi"
    NETBANKING = "netbanking"
    CARD = "card"
    WALLET = "wallet"


class Action(str, Enum):
    RETRY = "retry"
    SWITCH_RAIL = "switch_rail"
    CONTACT_CUSTOMER = "contact_customer"
    AWAIT_ISSUER_HEALTH = "await_issuer_health"
    MANUAL_REVIEW = "manual_review"
    SUPPRESS = "suppress"


class Timing(str, Enum):
    IMMEDIATE = "immediate"
    SHORT_DELAY = "short_delay"
    NEXT_WAKING_HOUR = "next_waking_hour"
    ISSUER_HEALTH_GATED = "issuer_health_gated"


@dataclass(frozen=True)
class BucketPolicy:
    action: Action
    timing: Timing
    preferred_rail: Rail
    max_attempts: int
    base_recovery_rate: float
    contacts_customer: bool
    excluded_rails: frozenset[Rail]
    retryable: bool
    rationale: str


# Recovery rules for each type of payment failure.
POLICIES: dict[Bucket, BucketPolicy] = {
    Bucket.INSUFFICIENT_FUNDS: BucketPolicy(
        action=Action.CONTACT_CUSTOMER,
        timing=Timing.NEXT_WAKING_HOUR,
        preferred_rail=Rail.UPI,
        max_attempts=1,
        base_recovery_rate=0.35,
        contacts_customer=True,
        excluded_rails=frozenset(),
        retryable=False,
        rationale="The account or card lacks funds; a retry is not a clean fix.",
    ),

    Bucket.AUTH_ABANDONED: BucketPolicy(
        action=Action.RETRY,
        timing=Timing.SHORT_DELAY,
        preferred_rail=Rail.UPI,
        max_attempts=1,
        base_recovery_rate=0.30,
        contacts_customer=True,
        excluded_rails=frozenset(),
        retryable=True,
        rationale="The customer started the flow but dropped out before completion.",
    ),

    Bucket.CUSTOMER_CANCELLED: BucketPolicy(
        action=Action.SUPPRESS,
        timing=Timing.IMMEDIATE,
        preferred_rail=Rail.UPI,
        max_attempts=1,
        base_recovery_rate=0.0,
        contacts_customer=False,
        excluded_rails=frozenset(),
        retryable=False,
        rationale="The customer explicitly cancelled, so a retry would be noisy.",
    ),

    Bucket.INSTRUMENT_INVALID: BucketPolicy(
        action=Action.CONTACT_CUSTOMER,
        timing=Timing.NEXT_WAKING_HOUR,
        preferred_rail=Rail.UPI,
        max_attempts=0,
        base_recovery_rate=0.05,
        contacts_customer=True,
        excluded_rails=frozenset(),
        retryable=False,
        rationale="The payment instrument itself is unusable or blocked.",
    ),

    Bucket.LIMIT_EXCEEDED: BucketPolicy(
        action=Action.SWITCH_RAIL,
        timing=Timing.NEXT_WAKING_HOUR,
        preferred_rail=Rail.UPI,
        max_attempts=1,
        base_recovery_rate=0.25,
        contacts_customer=True,
        excluded_rails=frozenset(),
        retryable=True,
        rationale="The request hit a bank or rail cap; switching rail is the likely fix.",
    ),

    Bucket.ISSUER_DOWN: BucketPolicy(
        action=Action.AWAIT_ISSUER_HEALTH,
        timing=Timing.ISSUER_HEALTH_GATED,
        preferred_rail=Rail.UPI,
        max_attempts=3,
        base_recovery_rate=0.45,
        contacts_customer=False,
        excluded_rails=frozenset(),
        retryable=True,
        rationale="The issuer or network is unhealthy; wait for health to recover.",
    ),

    Bucket.RISK_DECLINED: BucketPolicy(
        action=Action.SUPPRESS,
        timing=Timing.IMMEDIATE,
        preferred_rail=Rail.UPI,
        max_attempts=0,
        base_recovery_rate=0.0,
        contacts_customer=False,
        excluded_rails=frozenset(),
        retryable=False,
        rationale="The payment was declined by risk or fraud rules.",
    ),

    Bucket.TECHNICAL: BucketPolicy(
        action=Action.RETRY,
        timing=Timing.SHORT_DELAY,
        preferred_rail=Rail.UPI,
        max_attempts=3,
        base_recovery_rate=0.40,
        contacts_customer=False,
        excluded_rails=frozenset(),
        retryable=True,
        rationale="The failure looks like a gateway or backend fault rather than customer behavior.",
    ),

    Bucket.UNKNOWN: BucketPolicy(
        action=Action.MANUAL_REVIEW,
        timing=Timing.NEXT_WAKING_HOUR,
        preferred_rail=Rail.UPI,
        max_attempts=0,
        base_recovery_rate=0.0,
        contacts_customer=False,
        excluded_rails=frozenset(),
        retryable=False,
        rationale="The evidence is too weak to safely automate a recovery action.",
    ),
}


def policy_for(bucket: Bucket) -> BucketPolicy:
    """Get the recovery policy for a failure bucket."""
    return POLICIES[bucket]


def choose_rail(bucket: Bucket, current_rail: Rail) -> Rail:
    """
    Pick a usable rail for the payment.

    Keep the current rail when it is allowed. Otherwise use the
    bucket's preferred rail.
    """
    policy = policy_for(bucket)

    if current_rail not in policy.excluded_rails:
        return current_rail

    if policy.preferred_rail not in policy.excluded_rails:
        return policy.preferred_rail

    # Use any rail that the policy has not ruled out.
    for rail in Rail:
        if rail not in policy.excluded_rails:
            return rail

    return current_rail


def is_retryable(bucket: Bucket) -> bool:
    """Check whether the bucket allows an automatic recovery attempt."""
    policy = policy_for(bucket)

    return (
        policy.max_attempts > 0
        and policy.action
        not in {
            Action.SUPPRESS,
            Action.MANUAL_REVIEW,
        }
    )


def max_attempts_for(bucket: Bucket) -> int:
    """Get the maximum number of attempts allowed for a bucket."""
    return policy_for(bucket).max_attempts
