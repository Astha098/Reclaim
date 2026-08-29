"""Simulation helpers used by mock mode and evaluation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.models import CircuitState
from app.taxonomy import Action, Bucket, Rail, policy_for


# Generate repeatable random values for each payment.
def _uniform(*parts: object) -> float:
    """Return a stable value between 0 and 1 for the given inputs."""
    key = "|".join(str(p) for p in parts).encode()
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


@dataclass(frozen=True)
class LatentTraits:
    """Store the hidden characteristics used by the simulator."""

    intrinsic_recoverability: float
    best_action: Action
    balance_arrives: bool


def latent_traits(payment_id: str, bucket: Bucket) -> LatentTraits:
    """Create stable simulated traits for a payment."""
    policy = policy_for(bucket)

    spread = _uniform(payment_id, "intrinsic")
    intrinsic = max(
        0.02,
        min(
            0.98,
            policy.base_recovery_rate * (0.55 + 0.9 * spread),
        ),
    )

    return LatentTraits(
        intrinsic_recoverability=intrinsic,
        best_action=terminal_action(policy.action),
        balance_arrives=_uniform(payment_id, "balance") < 0.72,
    )


# Convert holding actions into the action they eventually perform.
TERMINAL_FOR: dict[Action, Action] = {
    Action.AWAIT_ISSUER_HEALTH: Action.RETRY,
}


def terminal_action(action: Action) -> Action:
    """Return the final action represented by a holding action."""
    return TERMINAL_FOR.get(action, action)


# Keep legal fallbacks from being treated as completely wrong actions.
LAWFUL_DEGRADATIONS: dict[Action, frozenset[Action]] = {
    Action.RETRY: frozenset(),
    Action.SWITCH_RAIL: frozenset(),
    Action.CONTACT_CUSTOMER: frozenset(),
    Action.AWAIT_ISSUER_HEALTH: frozenset(),
    Action.MANUAL_REVIEW: frozenset(),
    Action.SUPPRESS: frozenset(),
}

DEGRADED_ACTION_PENALTY = 0.78


# Penalize actions that do not match the simulated best action.
WRONG_ACTION_PENALTY = 0.22

# Penalize attempts made while the issuer circuit is open.
OPEN_CIRCUIT_PENALTY = 0.04

HALF_OPEN_PENALTY = 0.55

# Penalize using a rail that the bucket excludes.
EXCLUDED_RAIL_PENALTY = 0.28


# Penalize retries that happen too soon for these failure types.
IMMEDIATE_RETRY_PENALTY = {
    Bucket.INSUFFICIENT_FUNDS: 0.18,
    Bucket.ISSUER_DOWN: 0.12,
    Bucket.CUSTOMER_CANCELLED: 0.35,
}


# Model how quickly intent fades for these failures.
INTENT_DECAY_PER_HOUR = {
    Bucket.AUTH_ABANDONED: 0.72,
    Bucket.TECHNICAL: 0.88,
}


# Later attempts have lower simulated conversion.
ATTEMPT_DECAY = {
    1: 1.0,
    2: 0.62,
    3: 0.38,
}


def _amount_factor(amount_paise: int) -> float:
    """Return a conversion multiplier based on payment amount."""
    rupees = amount_paise / 100

    if rupees <= 2_000:
        return 1.0

    if rupees <= 10_000:
        return 0.93

    if rupees <= 50_000:
        return 0.82

    return 0.68


@dataclass
class Treatment:
    """Describe the action being evaluated by the simulator."""

    bucket: Bucket
    action: Action
    rail: Rail
    attempt_no: int
    delay_hours: float
    circuit_state: CircuitState
    amount_paise: int


def conversion_probability(
    traits: LatentTraits,
    treatment: Treatment,
) -> tuple[float, list[str]]:
    """Calculate conversion probability and return the factor breakdown."""
    policy = policy_for(treatment.bucket)

    probability = traits.intrinsic_recoverability
    factors = [f"intrinsic={probability:.3f}"]

    # Non-retryable buckets cannot produce an automatic recovery.
    if (
        policy.max_attempts <= 0
        or treatment.action is Action.SUPPRESS
        or treatment.action is Action.MANUAL_REVIEW
    ):
        return 0.0, factors + ["suppressed or non-retryable → 0"]

    # Compare the actual action with the best action for this simulated payment.
    if treatment.action is not traits.best_action:
        chosen = terminal_action(treatment.action)

        if chosen is traits.best_action:
            pass
        elif chosen in LAWFUL_DEGRADATIONS.get(
            traits.best_action,
            frozenset(),
        ):
            probability *= DEGRADED_ACTION_PENALTY
            factors.append(
                f"lawful_degradation("
                f"{chosen.value}←{traits.best_action.value})"
                f"×{DEGRADED_ACTION_PENALTY}"
            )
        else:
            probability *= WRONG_ACTION_PENALTY
            factors.append(
                f"wrong_action("
                f"{chosen.value} vs {traits.best_action.value})"
                f"×{WRONG_ACTION_PENALTY}"
            )

    # Model the effect of the issuer circuit state.
    match treatment.circuit_state:
        case CircuitState.OPEN:
            probability *= OPEN_CIRCUIT_PENALTY
            factors.append(f"circuit_open×{OPEN_CIRCUIT_PENALTY}")

        case CircuitState.HALF_OPEN:
            probability *= HALF_OPEN_PENALTY
            factors.append(f"circuit_half_open×{HALF_OPEN_PENALTY}")

        case _:
            pass

    # Penalize a rail that the bucket does not allow.
    if treatment.rail in policy.excluded_rails:
        probability *= EXCLUDED_RAIL_PENALTY
        factors.append(
            f"excluded_rail({treatment.rail.value})"
            f"×{EXCLUDED_RAIL_PENALTY}"
        )

    # Some failures are especially sensitive to retry timing.
    if (
        treatment.delay_hours < 0.5
        and treatment.bucket in IMMEDIATE_RETRY_PENALTY
    ):
        multiplier = IMMEDIATE_RETRY_PENALTY[treatment.bucket]
        probability *= multiplier
        factors.append(
            f"too_soon({treatment.bucket.value})×{multiplier}"
        )

    # A later balance can make an insufficient-funds recovery more likely.
    if (
        treatment.bucket is Bucket.INSUFFICIENT_FUNDS
        and treatment.delay_hours >= 12
    ):
        if traits.balance_arrives:
            probability *= 1.85
            factors.append("balance_arrived×1.85")
        else:
            probability *= 0.30
            factors.append("balance_never_arrived×0.30")

    # Abandoned authentication and technical failures lose intent over time.
    if (
        treatment.bucket in INTENT_DECAY_PER_HOUR
        and treatment.delay_hours > 0.5
    ):
        decay = (
            INTENT_DECAY_PER_HOUR[treatment.bucket]
            ** treatment.delay_hours
        )
        probability *= decay
        factors.append(
            f"intent_decay^{treatment.delay_hours:.1f}h={decay:.3f}"
        )

    # Repeated attempts are less effective than the first attempt.
    attempt_multiplier = ATTEMPT_DECAY.get(
        treatment.attempt_no,
        0.2,
    )
    probability *= attempt_multiplier
    factors.append(
        f"attempt_{treatment.attempt_no}×{attempt_multiplier}"
    )

    # Higher-value payments receive a small conversion penalty.
    amount_multiplier = _amount_factor(treatment.amount_paise)
    probability *= amount_multiplier
    factors.append(f"amount×{amount_multiplier}")

    probability = max(
        0.0,
        min(0.97, probability),
    )

    factors.append(f"→ p={probability:.4f}")

    return probability, factors


def converts(
    payment_id: str,
    traits: LatentTraits,
    treatment: Treatment,
) -> tuple[bool, float, list[str]]:
    """Return a repeatable simulated outcome for the payment attempt."""
    probability, factors = conversion_probability(
        traits,
        treatment,
    )

    # The same payment and attempt always use the same random value.
    random_value = _uniform(
        payment_id,
        "outcome",
        treatment.attempt_no,
    )

    return (
        random_value < probability,
        probability,
        factors + [f"u={random_value:.4f}"],
    )
