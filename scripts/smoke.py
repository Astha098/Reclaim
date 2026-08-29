"""End-to-end self-check.

    python3 scripts/smoke.py

Seeds a fresh dataset, drains the queue, and asserts the things that actually
matter. Not a unit-test suite — it is the check that answers "is the interesting
logic firing, or am I looking at 300 rows of the same trivial path?"

Each assertion exists because its absence would be a silent failure that still
produces a plausible-looking dashboard:

    no suppressions          → the guardrails aren't wired in
    no circuit ever opened   → the outage generator or the breaker is broken
    every bucket identical   → the taxonomy isn't reaching the policy layer
    zero deferrals           → the health gate never actually held anything
    two attempts same key    → the idempotency index isn't doing its job

Exit code is non-zero if any check fails, so this works in CI.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Real timing, virtual clock. DEMO_TIME_COMPRESSION is a live-demo affordance for
# watching the scheduler fire; the self-check must exercise true delays — a
# payday-aligned retry has to actually land days later for the balance-arrival
# path to be reached at all.
os.environ["DEMO_TIME_COMPRESSION"] = "1"
os.environ.setdefault("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "smoke.db"))

import logging  # noqa: E402

logging.disable(logging.INFO)

from app import db, issuer_health, scheduler  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import api_seed  # noqa: E402
from app.taxonomy import POLICIES  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f"  — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)


def rupees(paise: int) -> str:
    return f"₹{paise // 100:,}"


def main() -> int:
    path = Path(os.environ["DB_PATH"])
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)

    db.init_db()
    print("Seeding 400 failed payments over a 3-hour window…")
    seeded = api_seed(count=400, seed=7, span_minutes=180)

    # Replay 10 simulated days against a virtual clock, so decisions scheduled
    # days out actually resolve instead of sitting pending.
    print("Replaying 10 simulated days of scheduled decisions…\n")
    drained = scheduler.drain(horizon_days=10.0)

    stats = db.headline_stats()
    buckets = db.stats_by_bucket()
    supp = db.suppression_breakdown()
    cls = db.classifier_stats()
    health = issuer_health.snapshot_all()
    attempts = db.recent_attempts(limit=300)

    # ---------------------------------------------------------------- headline
    print("\n── Headline " + "─" * 55)
    print(f"  failed payments      {stats['failed_payments']}  worth {rupees(stats['failed_value_paise'])}")
    print(f"  recovered            {stats['recovered_count']}  worth {rupees(stats['recovered_value_paise'])}")
    print(f"  recovery rate (value) {stats['recovery_rate_by_value']:.1%}")
    print(f"  attempts made        {stats['attempts_made']}   conversion {stats['attempt_conversion_rate']:.1%}")
    print(f"  suppressed           {stats['suppressed']}")
    print(f"  still pending        {stats['pending']}")
    print(f"  replay               {drained['ticks']} virtual ticks over {drained['jumped_hours']}h, "
          f"{drained['handled']} attempts handled")

    # ---------------------------------------------------------------- buckets
    print("\n── By bucket " + "─" * 54)
    print(f"  {'bucket':<20} {'failed':>6} {'exec':>5} {'pend':>5} {'recov':>6} {'conv':>6}  recovered")
    for b in sorted(buckets, key=lambda x: -x["recovered_value_paise"]):
        print(
            f"  {b['bucket']:<20} {b['failed_count']:>6} {b['attempts']:>5} "
            f"{b['pending']:>5} {b['recovered']:>6} {b['conversion']:>5.0%}  "
            f"{rupees(b['recovered_value_paise']):>12}"
        )

    # ------------------------------------------------------------ suppressions
    print("\n── Suppressed, and why " + "─" * 44)
    if not supp:
        print("  (none)")
    for s in supp:
        print(f"  {s['reason']:<28} {s['count']:>4}  {rupees(s['value_paise']):>12} held back")

    # -------------------------------------------------------------- classifier
    print("\n── Classifier " + "─" * 53)
    for row in sorted(cls["by_source"], key=lambda r: -r["count"]):
        print(
            f"  {row['source']:<22} {row['count']:>4}  {row['share']:>5.0%}  "
            f"conf={row['avg_confidence']:.2f}  {row['avg_latency_ms']:>6.1f}ms"
        )

    # ------------------------------------------------------------ issuer health
    print("\n── Issuer health (worst first) " + "─" * 36)
    for h in health[:8]:
        print(
            f"  {h.issuer_key:<18} {h.state.value:<10} success={h.success_rate:>5.1%} "
            f"n={h.attempts:<5} opened={h.opened_count}"
        )

    # ----------------------------------------------------------------- checks
    print("\n── Assertions " + "─" * 53)

    check("payments ingested", stats["failed_payments"] > 350,
          f"{stats['failed_payments']} of 400 (dupes rejected by design)")

    check("recovery attempts executed", stats["attempts_made"] > 50,
          f"{stats['attempts_made']} attempts")

    check("revenue recovered", stats["recovered_value_paise"] > 0,
          rupees(stats["recovered_value_paise"]))

    check("recovery rate is plausible, not magical",
          0.05 < stats["recovery_rate_by_value"] < 0.60,
          f"{stats['recovery_rate_by_value']:.1%} by value")

    check("guardrails suppressed something", len(supp) > 0,
          f"{stats['suppressed']} suppressed across {len(supp)} distinct reasons")

    risk_bucket = next((b for b in buckets if b["bucket"] == "risk_declined"), None)
    check("risk declines were never retried",
          risk_bucket is None or risk_bucket["attempts"] == 0,
          f"{risk_bucket['failed_count']} risk declines, {risk_bucket['attempts']} attempts"
          if risk_bucket else "none in sample")

    check("at least one circuit opened during an outage",
          any(h.opened_count > 0 for h in health),
          ", ".join(f"{h.issuer_key}×{h.opened_count}" for h in health if h.opened_count) or "none")

    check("circuits are per-(rail, issuer), not global",
          len({h.issuer_key.split(":")[0] for h in health}) > 1,
          f"{len(health)} circuits across "
          f"{len({h.issuer_key.split(':')[0] for h in health})} rails")

    traces = [t for a in attempts for t in (a.get("decision_trace") or [])]
    check("health gate deferred at least one attempt",
          any("deferred" in t for t in traces),
          f"{sum('deferred' in t for t in traces)} deferrals in trace")

    check("rail failover happened (card → alternate)",
          any("alternate rail" in t or "failover" in t or "→ rail" in t for t in traces),
          "see decision traces")

    check("silent retries were mandate-gated",
          any("no saved token/mandate" in t for t in traces),
          f"{sum('no saved token/mandate' in t for t in traces)} degraded to a link")

    dupes = [k for k, n in Counter(a["idempotency_key"] for a in attempts).items() if n > 1]
    check("no duplicate idempotency keys", not dupes, f"{len(dupes)} dupes")

    # Checked against the *declared* cap per bucket rather than a literal, for two
    # reasons. A hardcoded `> 2` silently became wrong the moment the global backstop
    # moved to 3, and it passed for the wrong reason before that: it never looked at
    # the taxonomy at all, so a bucket declaring `max_attempts=1` could have been
    # attempted twice and this check would have shrugged. Suppressions are excluded —
    # a `suppress` row is a recorded decision not to charge, which is why the
    # never-retry buckets legitimately hold rows despite declaring zero attempts.
    real = [a for a in attempts if a["action"] != "suppress"]
    declared = {b.value: p.max_attempts for b, p in POLICIES.items()}
    over = [
        a for a in real
        if a["attempt_no"] > min(
            declared.get(a["bucket"], 0), settings.guardrails.max_attempts_per_order
        )
    ]
    worst = {
        b: max(a["attempt_no"] for a in real if a["bucket"] == b)
        for b in {a["bucket"] for a in real}
    }
    check("per-bucket attempt caps respected",
          not over,
          f"{len(over)} over cap · deepest: "
          + ", ".join(f"{b} {n}/{declared.get(b, 0)}" for b, n in sorted(worst.items())))

    check("never-retry buckets were never charged",
          not [a for a in real if declared.get(a["bucket"], 0) == 0],
          "risk_declined and unknown hold suppression rows only")

    check("multiple distinct actions chosen",
          len({a["action"] for a in attempts}) >= 3,
          ", ".join(sorted({a["action"] for a in attempts})))

    check("every attempt carries a decision trace",
          all(a.get("decision_trace") for a in attempts),
          f"{sum(1 for a in attempts if not a.get('decision_trace'))} without")

    print("\n" + "─" * 68)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}/{CHECKS}: " + "; ".join(FAILURES))
        return 1
    print(f"PASSED {CHECKS}/{CHECKS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
