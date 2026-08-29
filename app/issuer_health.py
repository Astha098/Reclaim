

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta

from app import db
from app.config import settings
from app.models import CircuitState, IssuerHealth

log = logging.getLogger("reclaim.health")


def observe(issuer_key: str, *, success: bool, at: datetime | None = None) -> None:
    db.record_gateway_event(issuer_key, success, at or datetime.now(UTC))


def _stored_state(issuer_key: str) -> tuple[CircuitState, datetime | None, int, int]:
    row = db.get_circuit(issuer_key)
    if row is None:
        return CircuitState.CLOSED, None, 0, 0
    opened_at = datetime.fromisoformat(
        row["opened_at"]) if row["opened_at"] else None
    return (
        CircuitState(row["state"]),
        opened_at,
        int(row["probes_used"]),
        int(row["opened_count"]),
    )


def evaluate(issuer_key: str, now: datetime | None = None) -> IssuerHealth:
    now = now or datetime.now(UTC)
    cfg = settings.circuit

    attempts, successes = db.health_window(issuer_key, cfg.window_minutes, now)
    rate = (successes / attempts) if attempts else 1.0

    state, opened_at, probes_used, opened_count = _stored_state(issuer_key)
    new_state, new_opened_at, new_probes = state, opened_at, probes_used
    tripped = False

    match state:
        case CircuitState.CLOSED:
            # Only trip on a statistically meaningful sample.
            if attempts >= cfg.min_samples and rate < cfg.open_below:
                new_state, new_opened_at, new_probes = CircuitState.OPEN, now, 0
                tripped = True
                log.warning(
                    "circuit OPEN %s — %.0f%% success over %d attempts in %dm",
                    issuer_key, rate * 100, attempts, cfg.window_minutes,
                )

        case CircuitState.OPEN:
            cooled = opened_at is None or now - opened_at >= timedelta(
                minutes=cfg.half_open_after_minutes
            )
            if cooled:
                new_state, new_probes = CircuitState.HALF_OPEN, 0
                log.info("circuit HALF_OPEN %s — probing with %d attempts",
                         issuer_key, cfg.probe_attempts)

        case CircuitState.HALF_OPEN:
            if attempts >= max(1, cfg.probe_attempts) and rate >= cfg.close_above:
                new_state, new_opened_at, new_probes = CircuitState.CLOSED, None, 0
                log.info("circuit CLOSED %s — recovered to %.0f%%",
                         issuer_key, rate * 100)
            elif probes_used >= cfg.probe_attempts:
                # Spent our probes and the issuer is still unhealthy. Back off.
                new_state, new_opened_at, new_probes = CircuitState.OPEN, now, 0
                tripped = True
                log.warning(
                    "circuit re-OPEN %s — probes exhausted at %.0f%%", issuer_key, rate * 100)

    if (new_state, new_opened_at, new_probes) != (state, opened_at, probes_used):
        db.upsert_circuit(
            issuer_key,
            new_state,
            opened_at=new_opened_at,
            probes_used=new_probes,
            count_trip=tripped,
        )

    return IssuerHealth(
        issuer_key=issuer_key,
        state=new_state,
        attempts=attempts,
        successes=successes,
        success_rate=rate,
        window_minutes=cfg.window_minutes,
        opened_at=new_opened_at,
        probes_used=new_probes,
        opened_count=opened_count + (1 if tripped else 0),
        updated_at=now,
    )


def allows_attempt(issuer_key: str, now: datetime | None = None) -> tuple[bool, str]:
    health = evaluate(issuer_key, now)

    match health.state:
        case CircuitState.CLOSED:
            return True, (
                f"circuit closed for {issuer_key} "
                f"({health.success_rate:.0%} over {health.attempts} attempts/{health.window_minutes}m)"
            )
        case CircuitState.OPEN:
            return False, (
                f"circuit OPEN for {issuer_key} — {health.success_rate:.0%} success over "
                f"{health.attempts} attempts; holding rather than retrying into an outage"
            )
        case CircuitState.HALF_OPEN:
            cfg = settings.circuit
            if health.probes_used < cfg.probe_attempts:
                return True, (
                    f"circuit HALF_OPEN for {issuer_key} — probe "
                    f"{health.probes_used + 1}/{cfg.probe_attempts}"
                )
            return False, f"circuit HALF_OPEN for {issuer_key} — probe budget spent"

    return True, "no health signal"


def consume_probe(issuer_key: str) -> None:
    state, opened_at, probes_used, _ = _stored_state(issuer_key)
    if state is CircuitState.HALF_OPEN:
        db.upsert_circuit(
            issuer_key, state, opened_at=opened_at, probes_used=probes_used + 1
        )


def force_recover(
    issuer_key: str, *, min_successes: int = 40, now: datetime | None = None
) -> tuple[IssuerHealth, list[str], int]:
    now = now or datetime.now(UTC)
    cfg = settings.circuit

    attempts, successes = db.health_window(issuer_key, cfg.window_minutes, now)
    # Smallest S with (successes + S) / (attempts + S) >= target. The margin over
    # close_above stops a single stray failure arriving mid-demo from leaving the
    # circuit one event short of closing.
    target = min(0.95, cfg.close_above + 0.05)
    needed = math.ceil((target * attempts - successes) /
                       (1 - target)) if attempts else 0
    inject = min(500, max(max(1, min_successes), needed, cfg.probe_attempts))

    for i in range(inject):
        observe(issuer_key, success=True, at=now - timedelta(seconds=i))

    state, opened_at, probes_used, _ = _stored_state(issuer_key)
    path = [state.value]

    if state is CircuitState.OPEN:
        # Rewind the trip so the half-open timer has legitimately expired. Not
        # counted as a new trip: this is the same outage, moved in time.
        db.upsert_circuit(
            issuer_key,
            CircuitState.OPEN,
            opened_at=now - timedelta(minutes=cfg.half_open_after_minutes + 1),
            probes_used=probes_used,
        )
        health = evaluate(issuer_key, now)  # OPEN -> HALF_OPEN
        path.append(health.state.value)

    health = evaluate(issuer_key, now)  # HALF_OPEN -> CLOSED
    if path[-1] != health.state.value:
        path.append(health.state.value)

    log.info("forced recovery %s — %s after %d successes",
             issuer_key, " → ".join(path), inject)
    return health, path, inject


def snapshot_all(now: datetime | None = None) -> list[IssuerHealth]:
    now = now or datetime.now(UTC)
    keys = set(db.all_issuer_keys(settings.circuit.window_minutes, now))
    # Include issuers with an open circuit even if traffic has dried up entirely,
    # which is exactly what a hard outage looks like.
    keys.update(db.known_circuit_keys())
    out = [evaluate(k, now) for k in sorted(keys)]
    out.sort(key=lambda h: (h.state is CircuitState.CLOSED, h.success_rate))
    return out
