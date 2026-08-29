"""SQLite database helpers for Reclaim."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.models import (
    AttemptOutcome,
    Classification,
    ClassifierSource,
    CircuitState,
    FailedPayment,
    RecoveryAttempt,
)
from app.taxonomy import Bucket

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    payment_id        TEXT PRIMARY KEY,
    order_id          TEXT NOT NULL,
    merchant_id       TEXT NOT NULL,
    amount_paise      INTEGER NOT NULL,
    currency          TEXT NOT NULL,
    rail              TEXT NOT NULL,
    issuer            TEXT,
    issuer_key        TEXT NOT NULL,
    vpa               TEXT,
    email             TEXT,
    contact           TEXT,
    international     INTEGER NOT NULL DEFAULT 0,
    error_code        TEXT,
    error_description TEXT,
    error_source      TEXT,
    error_step        TEXT,
    error_reason      TEXT,
    tokenized         INTEGER NOT NULL DEFAULT 0,
    failed_at         TEXT NOT NULL,
    raw               TEXT NOT NULL,
    ingested_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(order_id);
CREATE INDEX IF NOT EXISTS idx_payments_failed_at ON payments(failed_at);

CREATE TABLE IF NOT EXISTS classifications (
    payment_id   TEXT PRIMARY KEY REFERENCES payments(payment_id),
    bucket       TEXT NOT NULL,
    confidence   REAL NOT NULL,
    source       TEXT NOT NULL,
    reasoning    TEXT,
    matched_rule TEXT,
    latency_ms   INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_class_bucket ON classifications(bucket);

CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    merchant_id TEXT NOT NULL,
    amount_paise INTEGER NOT NULL,
    paid        INTEGER NOT NULL DEFAULT 0,
    paid_at     TEXT,
    paid_via    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_attempts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id         TEXT NOT NULL,
    order_id           TEXT NOT NULL,
    merchant_id        TEXT NOT NULL,
    attempt_no         INTEGER NOT NULL,
    bucket             TEXT NOT NULL,
    action             TEXT NOT NULL,
    timing             TEXT NOT NULL,
    rail               TEXT NOT NULL,
    amount_paise       INTEGER NOT NULL,
    idempotency_key    TEXT NOT NULL,
    scheduled_for      TEXT NOT NULL,
    executed_at        TEXT,
    outcome            TEXT NOT NULL,
    suppression_reason TEXT,
    payment_link_id    TEXT,
    payment_link_url   TEXT,
    message_body       TEXT,
    contacted_customer INTEGER NOT NULL DEFAULT 0,
    recovered_paise    INTEGER NOT NULL DEFAULT 0,
    deferrals          INTEGER NOT NULL DEFAULT 0,
    decision_trace     TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL
);
-- Unique idempotency guard.
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_idempotency
    ON recovery_attempts(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_attempts_due
    ON recovery_attempts(outcome, scheduled_for);
CREATE INDEX IF NOT EXISTS idx_attempts_order ON recovery_attempts(order_id);
CREATE INDEX IF NOT EXISTS idx_attempts_bucket ON recovery_attempts(bucket);

-- Raw gateway events for circuit health.
CREATE TABLE IF NOT EXISTS gateway_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_key  TEXT NOT NULL,
    success     INTEGER NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gwe_key_time ON gateway_events(issuer_key, occurred_at);

CREATE TABLE IF NOT EXISTS circuits (
    issuer_key   TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    opened_at    TEXT,
    probes_used  INTEGER NOT NULL DEFAULT 0,
    -- Lifetime trip count for the circuit.
    opened_count INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customer_contacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_key TEXT NOT NULL,
    merchant_id TEXT NOT NULL,
    order_id    TEXT NOT NULL,
    channel     TEXT NOT NULL,
    sent_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_cust ON customer_contacts(customer_key, sent_at);
CREATE INDEX IF NOT EXISTS idx_contacts_merch ON customer_contacts(merchant_id, sent_at);

CREATE TABLE IF NOT EXISTS suppressed_customers (
    customer_key TEXT PRIMARY KEY,
    reason       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    """Return the SQLite connection for this thread."""
    existing = getattr(_local, "conn", None)
    if existing is not None:
        return existing
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(settings.db_path),
                           timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    _local.conn = conn
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    """Run database operations inside a transaction."""
    conn = _conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db() -> None:
    _conn().executescript(SCHEMA)


def reset_db() -> None:
    """Clear the database tables."""
    conn = _conn()
    tables = [
        "recovery_attempts",
        "classifications",
        "customer_contacts",
        "suppressed_customers",
        "gateway_events",
        "circuits",
        "orders",
        "payments",
    ]
    conn.execute("BEGIN IMMEDIATE")
    try:
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM sqlite_sequence")
    except sqlite3.OperationalError:
        pass
    finally:
        conn.execute("COMMIT")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


def insert_payment(fp: FailedPayment) -> bool:
    """Store a failed payment. Returns False if we have seen it before.

    Razorpay retries webhooks on non-2xx, and at-least-once delivery means
    duplicates are normal, not exceptional. Dedupe on payment_id.
    """
    conn = _conn()
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO payments (
            payment_id, order_id, merchant_id, amount_paise, currency, rail,
            issuer, issuer_key, vpa, email, contact, international,
            error_code, error_description, error_source, error_step, error_reason,
            tokenized, failed_at, raw, ingested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            fp.payment_id, fp.order_id, fp.merchant_id, fp.amount_paise, fp.currency,
            fp.rail.value, fp.issuer, fp.issuer_key, fp.vpa, fp.email, fp.contact,
            int(fp.international), fp.error_code, fp.error_description,
            fp.error_source, fp.error_step, fp.error_reason,
            int(fp.tokenized), _iso(fp.failed_at), json.dumps(fp.raw), _now(),
        ),
    )
    inserted = cur.rowcount > 0
    if inserted:
        conn.execute(
            """
            INSERT OR IGNORE INTO orders (order_id, merchant_id, amount_paise, paid, created_at)
            VALUES (?,?,?,0,?)
            """,
            (fp.order_id, fp.merchant_id, fp.amount_paise, _now()),
        )
    return inserted


def get_payment(payment_id: str) -> sqlite3.Row | None:
    return _conn().execute(
        "SELECT * FROM payments WHERE payment_id = ?", (payment_id,)
    ).fetchone()


def is_order_paid(order_id: str) -> bool:
    row = _conn().execute("SELECT paid FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    return bool(row and row["paid"])


def mark_order_paid(order_id: str, *, via: str, at: datetime | None = None) -> None:
    _conn().execute(
        "UPDATE orders SET paid = 1, paid_at = ?, paid_via = ? WHERE order_id = ?",
        (_iso(at or datetime.now(UTC)), via, order_id),
    )


def insert_classification(payment_id: str, c: Classification) -> None:
    _conn().execute(
        """
        INSERT OR REPLACE INTO classifications
            (payment_id, bucket, confidence, source, reasoning, matched_rule, latency_ms, created_at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            payment_id, c.bucket.value, c.confidence, c.source.value,
            c.reasoning, c.matched_rule, c.latency_ms, _now(),
        ),
    )


def get_classification(payment_id: str) -> Classification | None:
    """Read back a stored classification.

    Needed when scheduling a follow-up attempt: the second attempt is a fresh
    decision through `policy.decide`, which wants the original classification so
    the new attempt's trace cites the confidence and source that were actually
    recorded, rather than a plausible-looking value invented at retry time.
    """
    row = _conn().execute(
        "SELECT * FROM classifications WHERE payment_id = ?", (payment_id,)
    ).fetchone()
    if row is None:
        return None
    return Classification(
        bucket=Bucket(row["bucket"]),
        confidence=float(row["confidence"]),
        source=ClassifierSource(row["source"]),
        reasoning=row["reasoning"] or "",
        matched_rule=row["matched_rule"],
        latency_ms=row["latency_ms"],
    )


def count_attempts(order_id: str) -> int:
    """Attempts that consumed budget. Suppressed rows are decisions, not
    attempts, so they must not count toward the per-order cap."""
    row = _conn().execute(
        "SELECT COUNT(*) AS n FROM recovery_attempts WHERE order_id = ? AND outcome != ?",
        (order_id, AttemptOutcome.SUPPRESSED.value),
    ).fetchone()
    return int(row["n"]) if row else 0


def has_in_flight(order_id: str) -> bool:
    row = _conn().execute(
        "SELECT 1 FROM recovery_attempts WHERE order_id = ? AND outcome = ? LIMIT 1",
        (order_id, AttemptOutcome.PENDING.value),
    ).fetchone()
    return row is not None


def last_attempt_at(order_id: str) -> datetime | None:
    row = _conn().execute(
        """
        SELECT MAX(COALESCE(executed_at, scheduled_for)) AS t
        FROM recovery_attempts
        WHERE order_id = ? AND outcome != ?
        """,
        (order_id, AttemptOutcome.SUPPRESSED.value),
    ).fetchone()
    return _dt(row["t"]) if row and row["t"] else None


def create_attempt(a: RecoveryAttempt) -> int | None:
    """Persist an attempt. Returns None if the idempotency key already exists.

    A None return is the double-charge guard firing. Callers must treat it as
    "someone else already owns this attempt" and walk away, not retry.
    """
    conn = _conn()
    try:
        cur = conn.execute(
            """
            INSERT INTO recovery_attempts (
                payment_id, order_id, merchant_id, attempt_no, bucket, action, timing,
                rail, amount_paise, idempotency_key, scheduled_for, executed_at, outcome,
                suppression_reason, payment_link_id, payment_link_url, message_body,
                contacted_customer, recovered_paise, decision_trace, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                a.payment_id, a.order_id, a.merchant_id, a.attempt_no, a.bucket.value,
                a.action.value, a.timing.value, a.rail.value, a.amount_paise,
                a.idempotency_key, _iso(a.scheduled_for),
                _iso(a.executed_at) if a.executed_at else None,
                a.outcome.value,
                a.suppression_reason.value if a.suppression_reason else None,
                a.payment_link_id, a.payment_link_url, a.message_body,
                int(a.contacted_customer), a.recovered_paise,
                json.dumps(a.decision_trace), _now(),
            ),
        )
        return int(cur.lastrowid) if cur.lastrowid else None
    except sqlite3.IntegrityError:
        return None


def update_attempt(
    attempt_id: int,
    *,
    outcome: AttemptOutcome,
    executed_at: datetime | None = None,
    recovered_paise: int = 0,
    payment_link_id: str | None = None,
    payment_link_url: str | None = None,
    message_body: str | None = None,
    contacted_customer: bool | None = None,
    extra_trace: list[str] | None = None,
) -> None:
    conn = _conn()
    row = conn.execute(
        "SELECT decision_trace FROM recovery_attempts WHERE id = ?", (
            attempt_id,)
    ).fetchone()
    trace: list[str] = json.loads(row["decision_trace"]) if row else []
    if extra_trace:
        trace.extend(extra_trace)

    sets = ["outcome = ?", "recovered_paise = ?", "decision_trace = ?"]
    args: list[Any] = [outcome.value, recovered_paise, json.dumps(trace)]
    if executed_at is not None:
        sets.append("executed_at = ?")
        args.append(_iso(executed_at))
    for col, val in (
        ("payment_link_id", payment_link_id),
        ("payment_link_url", payment_link_url),
        ("message_body", message_body),
    ):
        if val is not None:
            sets.append(f"{col} = ?")
            args.append(val)
    if contacted_customer is not None:
        sets.append("contacted_customer = ?")
        args.append(int(contacted_customer))
    args.append(attempt_id)
    conn.execute(
        f"UPDATE recovery_attempts SET {', '.join(sets)} WHERE id = ?", args)


def due_attempts(now: datetime, limit: int = 100) -> list[sqlite3.Row]:
    return _conn().execute(
        """
        SELECT * FROM recovery_attempts
        WHERE outcome = ? AND scheduled_for <= ?
        ORDER BY scheduled_for ASC
        LIMIT ?
        """,
        (AttemptOutcome.PENDING.value, _iso(now), limit),
    ).fetchall()


def next_due_at() -> datetime | None:
    """Earliest `scheduled_for` among still-pending attempts, or None if the queue
    is empty.

    Lets the eval's virtual-clock replay jump straight to the next scheduled
    moment instead of stepping through empty time. A payday-aligned retry six days
    out is legitimately followed by six days of nothing, so a fixed-step loop
    either wastes thousands of no-op ticks or gives up before the interesting
    decision lands.
    """
    row = _conn().execute(
        "SELECT MIN(scheduled_for) AS t FROM recovery_attempts WHERE outcome = ?",
        (AttemptOutcome.PENDING.value,),
    ).fetchone()
    return datetime.fromisoformat(row["t"]) if row and row["t"] else None


def attempt_row(attempt_id: int) -> sqlite3.Row | None:
    return _conn().execute(
        "SELECT * FROM recovery_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()


def record_gateway_event(issuer_key: str, success: bool, occurred_at: datetime | None = None) -> None:
    _conn().execute(
        "INSERT INTO gateway_events (issuer_key, success, occurred_at) VALUES (?,?,?)",
        (issuer_key, int(success), _iso(occurred_at or datetime.now(UTC))),
    )


def health_window(issuer_key: str, window_minutes: int, now: datetime | None = None) -> tuple[int, int]:
    """(attempts, successes) for an issuer over the trailing window."""
    now = now or datetime.now(UTC)
    since = _iso(now - timedelta(minutes=window_minutes))
    row = _conn().execute(
        """
        SELECT COUNT(*) AS attempts, COALESCE(SUM(success), 0) AS successes
        FROM gateway_events
        WHERE issuer_key = ? AND occurred_at >= ?
        """,
        (issuer_key, since),
    ).fetchone()
    return (int(row["attempts"]), int(row["successes"])) if row else (0, 0)


def all_issuer_keys(window_minutes: int, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    since = _iso(now - timedelta(minutes=window_minutes))
    rows = _conn().execute(
        "SELECT DISTINCT issuer_key FROM gateway_events WHERE occurred_at >= ?", (
            since,)
    ).fetchall()
    return [r["issuer_key"] for r in rows]


def get_circuit(issuer_key: str) -> sqlite3.Row | None:
    return _conn().execute(
        "SELECT * FROM circuits WHERE issuer_key = ?", (issuer_key,)
    ).fetchone()


def upsert_circuit(
    issuer_key: str,
    state: CircuitState,
    *,
    opened_at: datetime | None,
    probes_used: int,
    count_trip: bool = False,
) -> None:

    _conn().execute(
        """
        INSERT INTO circuits (issuer_key, state, opened_at, probes_used, opened_count, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(issuer_key) DO UPDATE SET
            state = excluded.state,
            opened_at = excluded.opened_at,
            probes_used = excluded.probes_used,
            opened_count = circuits.opened_count + excluded.opened_count,
            updated_at = excluded.updated_at
        """,
        (
            issuer_key,
            state.value,
            _iso(opened_at) if opened_at else None,
            probes_used,
            1 if count_trip else 0,
            _now(),
        ),
    )


def record_contact(customer_key: str, merchant_id: str, order_id: str, channel: str) -> None:
    _conn().execute(
        """
        INSERT INTO customer_contacts (customer_key, merchant_id, order_id, channel, sent_at)
        VALUES (?,?,?,?,?)
        """,
        (customer_key, merchant_id, order_id, channel, _now()),
    )


def contacts_since(customer_key: str, since: datetime) -> int:
    row = _conn().execute(
        "SELECT COUNT(*) AS n FROM customer_contacts WHERE customer_key = ? AND sent_at >= ?",
        (customer_key, _iso(since)),
    ).fetchone()
    return int(row["n"]) if row else 0


def merchant_contacts_since(merchant_id: str, since: datetime) -> int:
    row = _conn().execute(
        "SELECT COUNT(*) AS n FROM customer_contacts WHERE merchant_id = ? AND sent_at >= ?",
        (merchant_id, _iso(since)),
    ).fetchone()
    return int(row["n"]) if row else 0


def is_suppressed(customer_key: str) -> bool:
    row = _conn().execute(
        "SELECT 1 FROM suppressed_customers WHERE customer_key = ? LIMIT 1", (
            customer_key,)
    ).fetchone()
    return row is not None


def suppress_customer(customer_key: str, reason: str) -> None:
    _conn().execute(
        """
        INSERT OR REPLACE INTO suppressed_customers (customer_key, reason, created_at)
        VALUES (?,?,?)
        """,
        (customer_key, reason, _now()),
    )


def headline_stats() -> dict[str, Any]:
    conn = _conn()
    failed = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_paise),0) AS v FROM payments"
    ).fetchone()
    recovered = conn.execute(
        """
        SELECT COUNT(*) AS n, COALESCE(SUM(recovered_paise),0) AS v
        FROM recovery_attempts WHERE outcome = ?
        """,
        (AttemptOutcome.RECOVERED.value,),
    ).fetchone()
    attempted = conn.execute(
        "SELECT COUNT(*) AS n FROM recovery_attempts WHERE outcome NOT IN (?,?)",
        (AttemptOutcome.SUPPRESSED.value, AttemptOutcome.PENDING.value),
    ).fetchone()
    suppressed = conn.execute(
        "SELECT COUNT(*) AS n FROM recovery_attempts WHERE outcome = ?",
        (AttemptOutcome.SUPPRESSED.value,),
    ).fetchone()
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM recovery_attempts WHERE outcome = ?",
        (AttemptOutcome.PENDING.value,),
    ).fetchone()
    contacts = conn.execute(
        "SELECT COUNT(*) AS n FROM customer_contacts").fetchone()

    failed_value = int(failed["v"])
    recovered_value = int(recovered["v"])
    n_attempted = int(attempted["n"])
    n_recovered = int(recovered["n"])

    return {
        "failed_payments": int(failed["n"]),
        "failed_value_paise": failed_value,
        "recovered_count": n_recovered,
        "recovered_value_paise": recovered_value,
        "attempts_made": n_attempted,
        "suppressed": int(suppressed["n"]),
        "pending": int(pending["n"]),
        "customer_contacts": int(contacts["n"]),
        "recovery_rate_by_value": (recovered_value / failed_value) if failed_value else 0.0,
        "attempt_conversion_rate": (n_recovered / n_attempted) if n_attempted else 0.0,
    }


def stats_by_bucket() -> list[dict[str, Any]]:
    rows = _conn().execute(
        """
        SELECT
            c.bucket                                        AS bucket,
            COUNT(DISTINCT p.payment_id)                    AS failed_count,
            COALESCE(SUM(p.amount_paise), 0)                AS failed_value_paise
        FROM payments p
        JOIN classifications c ON c.payment_id = p.payment_id
        GROUP BY c.bucket
        """
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        att = _conn().execute(
            """
            SELECT
                COUNT(*)                                                     AS total,
                SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END)                 AS recovered,
                COALESCE(SUM(CASE WHEN outcome = ? THEN recovered_paise ELSE 0 END), 0) AS recovered_value,
                SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END)                 AS suppressed,
                SUM(CASE WHEN outcome = ? THEN 1 ELSE 0 END)                 AS pending
            FROM recovery_attempts WHERE bucket = ?
            """,
            (
                AttemptOutcome.RECOVERED.value,
                AttemptOutcome.RECOVERED.value,
                AttemptOutcome.SUPPRESSED.value,
                AttemptOutcome.PENDING.value,
                r["bucket"],
            ),
        ).fetchone()
        executed = int(att["total"] or 0) - \
            int(att["suppressed"] or 0) - int(att["pending"] or 0)
        recovered_n = int(att["recovered"] or 0)
        out.append(
            {
                "bucket": r["bucket"],
                "failed_count": int(r["failed_count"]),
                "failed_value_paise": int(r["failed_value_paise"]),
                "attempts": executed,
                "pending": int(att["pending"] or 0),
                "suppressed": int(att["suppressed"] or 0),
                "recovered": recovered_n,
                "recovered_value_paise": int(att["recovered_value"] or 0),
                "conversion": (recovered_n / executed) if executed else 0.0,
            }
        )
    out.sort(key=lambda d: d["recovered_value_paise"], reverse=True)
    return out


def suppression_breakdown() -> list[dict[str, Any]]:
    rows = _conn().execute(
        """
        SELECT suppression_reason AS reason, COUNT(*) AS n,
               COALESCE(SUM(amount_paise),0) AS value_paise
        FROM recovery_attempts
        WHERE outcome = ? AND suppression_reason IS NOT NULL
        GROUP BY suppression_reason
        ORDER BY n DESC
        """,
        (AttemptOutcome.SUPPRESSED.value,),
    ).fetchall()
    return [
        {"reason": r["reason"], "count": int(
            r["n"]), "value_paise": int(r["value_paise"])}
        for r in rows
    ]


def classifier_stats() -> dict[str, Any]:
    rows = _conn().execute(
        """
        SELECT source, COUNT(*) AS n, AVG(confidence) AS conf, AVG(latency_ms) AS lat
        FROM classifications GROUP BY source
        """
    ).fetchall()
    total = sum(int(r["n"]) for r in rows) or 1
    return {
        "total": total,
        "by_source": [
            {
                "source": r["source"],
                "count": int(r["n"]),
                "share": int(r["n"]) / total,
                "avg_confidence": round(float(r["conf"] or 0), 3),
                "avg_latency_ms": round(float(r["lat"] or 0), 1),
            }
            for r in rows
        ],
    }


_ATTEMPT_SELECT = """
    SELECT a.*, p.issuer_key, p.error_description
    FROM recovery_attempts a
    LEFT JOIN payments p ON p.payment_id = a.payment_id
"""
_ATTEMPT_ORDER = """
    ORDER BY (a.outcome = :pending) ASC,
             COALESCE(a.executed_at, a.created_at) DESC,
             a.id DESC
"""


def recent_attempts(limit: int = 60, *, min_suppressed: int = 8) -> list[dict[str, Any]]:
    """The decision log: what the agent did, newest first, restraint included.

    Two ordering problems, both worth explaining because the obvious query gets
    both wrong.

    `id DESC` would be the natural choice and it is the worse one: a scheduler tick
    inserts every newly-queued follow-up in a single batch, so the highest ids are
    all rows nothing has happened to yet. On a seeded demo that yields sixty
    identical "auth_abandoned · attempt 2 · in 2m" lines and not one outcome. So
    completed rows sort ahead of pending ones, and within each group by the moment
    the row represents — when it ran, or failing that when it was decided.

    That fix alone introduces the second problem. A suppression is decided the
    instant the webhook lands; an execution happens whenever the retry came due.
    Strict recency therefore ranks every execution above every suppression, and the
    panel that exists partly to show the agent declining to act loses all 32 of
    those rows off the bottom. In production the two interleave because both arrive
    continuously; in a batch-seeded demo they do not.

    Hence `min_suppressed`: a small reserved slice, so the decisions not to act stay
    visible. Rows are still returned newest-first, and the reserved slice is stated
    in the panel's own subtitle rather than left as a silent sampling rule.
    """
    conn = _conn()
    params = {"pending": AttemptOutcome.PENDING.value}
    rows = conn.execute(
        f"{_ATTEMPT_SELECT}{_ATTEMPT_ORDER} LIMIT :limit", {
            **params, "limit": limit}
    ).fetchall()

    suppressed = AttemptOutcome.SUPPRESSED.value
    have = sum(1 for r in rows if r["outcome"] == suppressed)
    want = min(min_suppressed, limit)
    if have < want:
        extra = conn.execute(
            f"{_ATTEMPT_SELECT} WHERE a.outcome = :suppressed{_ATTEMPT_ORDER} LIMIT :n",
            {**params, "suppressed": suppressed, "n": want},
        ).fetchall()
        seen = {r["id"] for r in rows}
        add = [r for r in extra if r["id"] not in seen][: want - have]
        keep = [r for r in rows if r["outcome"] == suppressed]
        others = [r for r in rows if r["outcome"] != suppressed]
        rows = keep + others[: max(0, limit - len(keep) - len(add))] + add

    rows = sorted(
        rows,
        key=lambda r: (r["executed_at"] or r["created_at"]
                       or "", r["id"] or 0),
        reverse=True,
    )
    rows = sorted(rows, key=lambda r: r["outcome"]
                  == AttemptOutcome.PENDING.value)

    out = []
    for r in rows:
        d = dict(r)
        d["decision_trace"] = json.loads(d.get("decision_trace") or "[]")
        d["contacted_customer"] = bool(d.get("contacted_customer"))
        d.pop("raw", None)
        out.append(d)
    return out


def timeline(bucket_minutes: int = 60) -> list[dict[str, Any]]:
    """Failed vs recovered value over time, for the dashboard's main chart."""
    rows = _conn().execute(
        """
        SELECT substr(failed_at, 1, 13) AS hour,
               COUNT(*) AS failed,
               COALESCE(SUM(amount_paise),0) AS failed_value
        FROM payments GROUP BY hour ORDER BY hour
        """
    ).fetchall()
    rec = _conn().execute(
        """
        SELECT substr(executed_at, 1, 13) AS hour,
               COUNT(*) AS recovered,
               COALESCE(SUM(recovered_paise),0) AS recovered_value
        FROM recovery_attempts
        WHERE outcome = ? AND executed_at IS NOT NULL
        GROUP BY hour ORDER BY hour
        """,
        (AttemptOutcome.RECOVERED.value,),
    ).fetchall()
    rec_by_hour = {r["hour"]: r for r in rec}
    out = []
    for r in rows:
        m = rec_by_hour.get(r["hour"])
        out.append(
            {
                "hour": r["hour"],
                "failed": int(r["failed"]),
                "failed_value_paise": int(r["failed_value"]),
                "recovered": int(m["recovered"]) if m else 0,
                "recovered_value_paise": int(m["recovered_value"]) if m else 0,
            }
        )
    return out


def known_circuit_keys() -> list[str]:
    """Issuers with a persisted circuit row, including ones whose traffic has
    stopped entirely — which is what a hard outage looks like."""
    rows = _conn().execute("SELECT issuer_key FROM circuits").fetchall()
    return [r["issuer_key"] for r in rows]


def defer_attempt(attempt_id: int, new_time: datetime, note: str) -> int:
    """Push a pending attempt's scheduled time back. Returns the new deferral count.

    Used when a health-gated attempt comes due but the issuer's circuit is still
    open: rather than spending the attempt, we re-queue it. The counter is what
    stops a permanently-dead issuer from holding an order in limbo forever.
    """
    conn = _conn()
    row = conn.execute(
        "SELECT deferrals, decision_trace FROM recovery_attempts WHERE id = ?", (
            attempt_id,)
    ).fetchone()
    if row is None:
        return 0
    deferrals = int(row["deferrals"]) + 1
    trace: list[str] = json.loads(row["decision_trace"])
    trace.append(note)
    conn.execute(
        """
        UPDATE recovery_attempts
        SET scheduled_for = ?, deferrals = ?, decision_trace = ?
        WHERE id = ?
        """,
        (_iso(new_time), deferrals, json.dumps(trace), attempt_id),
    )
    return deferrals


def payment_to_model(row: sqlite3.Row) -> FailedPayment:
    """Rehydrate a stored payment into the domain model.

    The worker runs long after ingestion, so it reads this back from the DB
    rather than holding objects in memory across a restart.
    """
    from app.taxonomy import Rail

    return FailedPayment(
        payment_id=row["payment_id"],
        order_id=row["order_id"],
        merchant_id=row["merchant_id"],
        amount_paise=int(row["amount_paise"]),
        currency=row["currency"],
        rail=Rail(row["rail"]),
        issuer=row["issuer"],
        vpa=row["vpa"],
        email=row["email"],
        contact=row["contact"],
        international=bool(row["international"]),
        error_code=row["error_code"],
        error_description=row["error_description"],
        error_source=row["error_source"],
        error_step=row["error_step"],
        error_reason=row["error_reason"],
        tokenized=bool(row["tokenized"]),
        failed_at=datetime.fromisoformat(row["failed_at"]),
        raw=json.loads(row["raw"]),
    )


def link_to_attempt(payment_link_id: str) -> sqlite3.Row | None:
    """Find the attempt a payment link belongs to. Backs the demo checkout page."""
    return _conn().execute(
        "SELECT * FROM recovery_attempts WHERE payment_link_id = ? LIMIT 1",
        (payment_link_id,),
    ).fetchone()
