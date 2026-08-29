"""Background worker.

A plain daemon thread polling `recovery_attempts` for due work. Deliberately not
Celery or APScheduler: the queue is a table with an index on
`(outcome, scheduled_for)`, the work is idempotent, and adding a broker would mean
a judge needs Redis running before anything works. When this needs to scale past
one process, the change is a `SELECT ... FOR UPDATE SKIP LOCKED` on Postgres and
several replicas of this same loop — the shape does not change.

Each tick is wrapped so one bad attempt cannot kill the thread. A worker that
dies silently at 3am and stops recovering revenue is a worse failure than a worker
that logs an exception and keeps going.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

from app import db, recovery
from app.config import settings
from app.models import AttemptOutcome

log = logging.getLogger("reclaim.scheduler")

_stop = threading.Event()
_thread: threading.Thread | None = None

# Stats surfaced on /api/scheduler so the dashboard can prove the worker is alive
# rather than leaving you guessing why nothing is moving.
_stats: dict[str, object] = {
    "ticks": 0,
    "executed": 0,
    "recovered": 0,
    "deferred": 0,
    "errors": 0,
    "last_tick": None,
    "running": False,
}


def tick(now: datetime | None = None, limit: int = 50) -> int:
    """Process one batch of due attempts. Returns how many were handled."""
    now = now or datetime.now(UTC)
    due = db.due_attempts(now, limit=limit)
    handled = 0

    for row in due:
        attempt_id = int(row["id"])
        try:
            outcome = recovery.execute(attempt_id, now=now)
        except Exception:  # noqa: BLE001 — one bad attempt must not stop the queue
            log.exception("attempt %s failed to execute", attempt_id)
            _stats["errors"] = int(_stats["errors"]) + 1
            db.update_attempt(
                attempt_id,
                outcome=AttemptOutcome.FAILED_AGAIN,
                executed_at=now,
                extra_trace=["✗ executor raised — see server logs"],
            )
            continue

        handled += 1
        if outcome is AttemptOutcome.PENDING:
            _stats["deferred"] = int(_stats["deferred"]) + 1
        else:
            _stats["executed"] = int(_stats["executed"]) + 1
            if outcome is AttemptOutcome.RECOVERED:
                _stats["recovered"] = int(_stats["recovered"]) + 1

    _stats["ticks"] = int(_stats["ticks"]) + 1
    _stats["last_tick"] = now.isoformat()
    return handled


def _loop() -> None:
    log.info("scheduler started (tick=%ss)", settings.scheduler_tick_seconds)
    _stats["running"] = True
    while not _stop.is_set():
        try:
            tick()
        except Exception:  # noqa: BLE001 — the loop itself must never die
            log.exception("scheduler tick failed")
            _stats["errors"] = int(_stats["errors"]) + 1
        # Interruptible sleep, so shutdown is immediate rather than up to a tick.
        _stop.wait(settings.scheduler_tick_seconds)
    _stats["running"] = False
    log.info("scheduler stopped")


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="reclaim-scheduler", daemon=True)
    _thread.start()


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=timeout)


def stats() -> dict[str, object]:
    return dict(_stats) | {"tick_seconds": settings.scheduler_tick_seconds}


def drain(
    *,
    start: datetime | None = None,
    horizon_days: float = 10.0,
    max_ticks: int = 5000,
    limit: int = 2000,
) -> dict[str, int]:
    """Run the queue forward against a *virtual* clock until it stops moving.

    The eval and the self-check both need to see the end state of decisions that
    are correctly scheduled days into the future — a payday-aligned retry, a
    circuit that needs five minutes to go half-open. Spinning `tick()` in real time
    does not work: nothing is due yet, `tick` returns 0, and the bucket reports
    "0% conversion" when the truth is "not tried yet".

    So instead of waiting, we hand `tick()` an advancing timestamp. Every
    time-dependent decision in this system takes its clock as a parameter for
    exactly this reason, which is why a 10-day replay costs a couple of seconds.

    The clock jumps to the next scheduled attempt rather than stepping at a fixed
    interval. Fixed stepping is the obvious implementation and it is wrong here: a
    payday-aligned retry is followed by six days of deliberately empty queue, so
    any step size either burns thousands of no-op ticks or gives up before the
    decision it was meant to observe.

    Unrelated to `DEMO_TIME_COMPRESSION`. That divides real delays so a human can
    watch the scheduler fire during a live demo; this leaves delays at their true
    length and moves the clock instead. The eval always uses this one, so no
    measured number depends on the demo affordance.
    """
    start = start or datetime.now(UTC)
    deadline = start + timedelta(days=horizon_days)
    totals = {"ticks": 0, "handled": 0, "jumped_hours": 0}
    virtual_now = start

    for _ in range(max_ticks):
        handled = tick(now=virtual_now, limit=limit)
        totals["ticks"] += 1
        totals["handled"] += handled

        nxt = db.next_due_at()
        if nxt is None:
            break  # queue empty
        if nxt > deadline:
            break  # scheduled beyond the horizon; report as pending, not as failure
        # Advance to the next scheduled moment (never backwards — a re-deferred
        # attempt can be due in the past relative to the clock we just used).
        virtual_now = max(nxt, virtual_now + timedelta(seconds=1))

    totals["jumped_hours"] = int((virtual_now - start).total_seconds() / 3600)
    totals["still_pending"] = len(db.due_attempts(deadline, limit=100_000))
    return totals
