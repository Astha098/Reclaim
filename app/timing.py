"""Helpers for deciding when a recovery action should run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.config import settings
from app.taxonomy import Timing


# Keep retries within a reasonable window. After several days, the payment
# attempt is usually no longer worth holding onto.
MAX_HOLD_DAYS = 7


def _local(dt: datetime) -> datetime:
    """Convert a UTC timestamp to the configured local timezone."""
    return dt.astimezone(settings.timezone)


def _compress(now: datetime, target: datetime) -> datetime:
    """Shorten delays when the application is running in demo mode."""

    factor = max(1, settings.demo_time_compression)

    if factor == 1 or target <= now:
        return target

    return now + (target - now) / factor


def in_quiet_hours(dt: datetime) -> bool:
    """Check whether the timestamp falls inside the quiet-hours window."""

    guardrails = settings.guardrails
    hour = _local(dt).hour

    start = guardrails.quiet_hours_start
    end = guardrails.quiet_hours_end

    if start == end:
        return False

    # This handles windows that cross midnight, such as 21:00 to 08:00.
    if start > end:
        return hour >= start or hour < end

    return start <= hour < end


def next_waking_moment(dt: datetime) -> datetime:
    """Move a customer-facing action outside quiet hours."""

    if not in_quiet_hours(dt):
        return dt

    local = _local(dt)
    end = settings.guardrails.quiet_hours_end

    candidate = local.replace(
        hour=end,
        minute=0,
        second=0,
        microsecond=0,
    )

    if candidate <= local:
        candidate += timedelta(days=1)

    return candidate.astimezone(UTC)


def schedule(
    timing: Timing,
    *,
    now: datetime | None = None,
    attempt_no: int = 1,
    contacts_customer: bool = False,
) -> datetime:
    """Turn a timing policy into the actual UTC time for the action."""

    now = now or datetime.now(UTC)

    match timing:
        case Timing.IMMEDIATE:
            target = now

        case Timing.SHORT_DELAY:
            # Increase the wait after each failed attempt.
            delay_minutes = 5 * (2 ** max(0, attempt_no - 1))
            target = now + timedelta(minutes=delay_minutes)

        case Timing.ISSUER_HEALTH_GATED:
            # Check again shortly after the issuer circuit can become
            # half-open. The scheduler will wait again if it is still unhealthy.
            target = now + timedelta(
                minutes=settings.circuit.half_open_after_minutes + 1
            )

        case Timing.NEXT_WAKING_HOUR:
            target = next_waking_moment(now)

        case _:
            target = now

    target = _compress(now, target)

    # Customer-facing actions should never be scheduled during quiet hours.
    if contacts_customer:
        target = next_waking_moment(target)

    return target


def describe(
    timing: Timing,
    scheduled_for: datetime,
    now: datetime | None = None,
) -> str:
    """Create a short explanation of why an action was scheduled for this time."""

    now = now or datetime.now(UTC)

    delta = scheduled_for - now
    minutes = delta.total_seconds() / 60

    if minutes < 1:
        when = "now"
    elif minutes < 90:
        when = f"in {minutes:.0f}m"
    elif minutes < 48 * 60:
        when = f"in {minutes / 60:.1f}h"
    else:
        when = f"in {minutes / 1440:.1f}d"

    local = _local(scheduled_for)

    reasons = {
        Timing.IMMEDIATE:
            "the payment intent is still fresh",
        Timing.SHORT_DELAY:
            "waiting briefly before trying the temporary failure again",
        Timing.ISSUER_HEALTH_GATED:
            "waiting for the issuer to become healthy",
        Timing.NEXT_WAKING_HOUR:
            "waiting until the customer is outside quiet hours",
    }

    reason = reasons.get(
        timing,
        "following the recovery timing policy",
    )

    return (
        f"scheduled {when} "
        f"({local:%a %d %b %H:%M} {local.tzname()}) — "
        f"{reason}"
    )
