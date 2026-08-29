"""API contract check. Exercises every endpoint through the real ASGI stack.

    python3 scripts/api_check.py

`scripts/smoke.py` calls the Python functions directly, which proves the decision
logic works but says nothing about whether the API in front of it does. This drives
the app through `httpx.ASGITransport` instead: real routing, real Pydantic
validation, real JSON serialization, real lifespan startup. The only thing not
exercised is the TCP socket, which is uvicorn's problem rather than this project's.

It exists mainly to pin the contract the dashboard consumes. Every field asserted
below is one the React app reads, so if someone renames `recovered_value_paise` in
a query, this fails here rather than as an empty chart nobody can explain.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DEMO_TIME_COMPRESSION"] = "1"
os.environ.setdefault("DB_PATH", str(ROOT / "data" / "api_check.db"))

import logging  # noqa: E402

logging.disable(logging.INFO)

import httpx  # noqa: E402

from app.main import app  # noqa: E402

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'✓' if ok else '✗'} {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def has(obj: dict, *keys: str) -> tuple[bool, str]:
    """Every key present. Returns (ok, which are missing)."""
    missing = [k for k in keys if k not in obj]
    return not missing, ("missing " + ", ".join(missing) if missing else "all fields present")


class Lifespan:
    """Drive the ASGI lifespan protocol by hand.

    `httpx.ASGITransport` deliberately does not run startup or shutdown events, and
    this app creates its schema and starts its worker thread in `lifespan`. Without
    this the first request dies on `no such table: gateway_events`, which looks like
    a database bug and is really a harness that never booted the application.

    Doing it manually rather than reaching for `asgi-lifespan` keeps the dependency
    list at zero and has the side benefit of actually asserting that startup
    completes — a `lifespan.startup.failed` reply fails the check here rather than
    surfacing as a mystery 500 later.
    """

    def __init__(self, app_) -> None:
        self.app = app_

    async def __aenter__(self) -> Lifespan:
        self._recv: asyncio.Queue = asyncio.Queue()
        self._send: asyncio.Queue = asyncio.Queue()
        await self._recv.put({"type": "lifespan.startup"})
        self._task = asyncio.create_task(
            self.app(
                {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}},
                self._recv.get,
                self._send.put,
            )
        )
        msg = await self._send.get()
        if msg["type"] != "lifespan.startup.complete":
            raise RuntimeError(f"app failed to start: {msg}")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._recv.put({"type": "lifespan.shutdown"})
        try:
            await asyncio.wait_for(self._send.get(), timeout=10)
            await asyncio.wait_for(self._task, timeout=10)
        except (TimeoutError, asyncio.TimeoutError):
            pass  # shutdown hang must not mask a real assertion failure


async def run() -> int:
    path = Path(os.environ["DB_PATH"])
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)

    transport = httpx.ASGITransport(app=app)
    async with Lifespan(app), httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=60.0
    ) as c:
        print("── Startup " + "─" * 56)
        check("ASGI lifespan startup completed", True, "schema created, scheduler thread up")
        r = await c.get("/healthz")
        check("GET /healthz", r.status_code == 200, str(r.json()))

        r = await c.get("/api/config")
        cfg = r.json()
        # The dashboard's honesty banner depends on both of these, so they are part
        # of the contract, not incidental debug output.
        check("GET /api/config declares mode and that outcomes are simulated",
              r.status_code == 200
              and cfg.get("razorpay_mode") in ("mock", "live")
              and isinstance(cfg.get("outcomes_simulated"), bool),
              f"razorpay={cfg.get('razorpay_mode')} simulated={cfg.get('outcomes_simulated')} "
              f"classifier={cfg.get('classifier_mode')}")
        check("global attempt backstop does not undercut the taxonomy",
              cfg["guardrails"]["max_attempts_per_order"] >= 3,
              f"global cap {cfg['guardrails']['max_attempts_per_order']}, "
              f"highest bucket cap 3 (technical)")

        print("\n── Seed and drain " + "─" * 49)
        r = await c.post("/api/demo/seed", json={"count": 250, "seed": 7, "span_minutes": 180})
        seeded = r.json()
        check("POST /api/demo/seed", r.status_code == 200, str(seeded)[:110])

        # Drive the queue forward so the read endpoints have resolved data.
        r = await c.post("/api/demo/tick", json={})
        check("POST /api/demo/tick", r.status_code == 200, str(r.json())[:110])

        print("\n── Dashboard reads " + "─" * 48)
        r = await c.get("/api/stats")
        stats = r.json()
        ok, detail = has(stats, "failed_payments", "failed_value_paise", "recovered_count",
                         "recovered_value_paise", "recovery_rate_by_value", "attempts_made",
                         "attempt_conversion_rate", "suppressed", "pending")
        check("GET /api/stats", r.status_code == 200 and ok, detail)
        check("stats are internally consistent",
              stats["recovered_value_paise"] <= stats["failed_value_paise"],
              f"{stats['recovered_value_paise']} recovered of {stats['failed_value_paise']}")

        r = await c.get("/api/buckets")
        buckets = r.json()
        first = buckets[0] if isinstance(buckets, list) and buckets else {}
        ok, detail = has(first, "bucket", "failed_count", "attempts", "pending", "suppressed",
                         "recovered", "recovered_value_paise", "conversion")
        check("GET /api/buckets", r.status_code == 200 and ok,
              f"{len(buckets)} buckets · {detail}")

        r = await c.get("/api/attempts?limit=25")
        attempts = r.json()
        first = attempts[0] if isinstance(attempts, list) and attempts else {}
        ok, detail = has(first, "bucket", "action", "rail", "attempt_no", "outcome",
                         "decision_trace", "idempotency_key")
        check("GET /api/attempts", r.status_code == 200 and ok,
              f"{len(attempts)} rows · {detail}")
        check("attempts carry a readable decision trace",
              bool(first.get("decision_trace")),
              f"{len(first.get('decision_trace') or [])} trace lines on the newest attempt")

        r = await c.get("/api/issuers")
        issuers = r.json()
        first = issuers[0] if isinstance(issuers, list) and issuers else {}
        ok, detail = has(first, "issuer_key", "state", "success_rate", "attempts", "opened_count")
        check("GET /api/issuers", r.status_code == 200 and ok,
              f"{len(issuers)} circuits · {detail}")

        r = await c.get("/api/classifier")
        clsf = r.json()
        check("GET /api/classifier", r.status_code == 200 and "by_source" in clsf,
              f"{len(clsf.get('by_source', []))} sources")

        r = await c.get("/api/suppressions")
        check("GET /api/suppressions", r.status_code == 200 and isinstance(r.json(), list),
              f"{len(r.json())} reasons")

        r = await c.get("/api/timeline")
        check("GET /api/timeline", r.status_code == 200 and isinstance(r.json(), list),
              f"{len(r.json())} buckets of time")

        r = await c.get("/api/scheduler")
        check("GET /api/scheduler", r.status_code == 200 and "ticks" in r.json(),
              str(r.json())[:90])

        r = await c.get("/api/taxonomy")
        tax = r.json()
        check("GET /api/taxonomy exposes the policy table",
              r.status_code == 200 and len(tax) >= 8,
              f"{len(tax)} buckets documented")

        print("\n── Webhook " + "─" * 56)
        # An unsigned request must be rejected, or anyone could post fake recoveries.
        r = await c.post("/webhooks/razorpay", json={"event": "payment.failed"})
        check("unsigned webhook is rejected", r.status_code == 401, f"HTTP {r.status_code}")

        # A signed but structurally junk payload must still return 2xx: Razorpay
        # retries non-2xx with backoff, and four more copies of an unparseable event
        # is not an improvement.
        #
        # Posted as `content=` rather than `json=` on purpose. The verifier hashes the
        # raw request body, so letting httpx re-serialize the dict would change
        # whitespace and key order and invalidate the signature — the exact failure
        # mode the verifier's docstring warns about.
        from app import razorpay_client

        raw = json.dumps({"event": "payment.failed", "payload": {}}).encode()
        sig = razorpay_client.sign_payload(raw)
        r = await c.post(
            "/webhooks/razorpay",
            content=raw,
            headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"},
        )
        check("signed-but-unparseable webhook still returns 2xx",
              200 <= r.status_code < 300,
              f"HTTP {r.status_code} {str(r.json())[:60]}")

        # And a correctly signed, well-formed event must be ingested for real.
        good = {
            "event": "payment.failed",
            "payload": {"payment": {"entity": {
                "id": "pay_apicheck001", "order_id": "order_apicheck001",
                "amount": 249900, "currency": "INR", "status": "failed",
                "method": "upi", "bank": "HDFC", "contact": "+919876543210",
                "email": "check@example.com", "created_at": 1755000000,
                "error_code": "BAD_REQUEST_ERROR", "error_description":
                    "Payment failed because of insufficient funds in the account",
                "error_source": "bank", "error_step": "payment_authorization",
                "error_reason": "insufficient_funds",
            }}},
        }
        raw = json.dumps(good).encode()
        r = await c.post(
            "/webhooks/razorpay",
            content=raw,
            headers={"X-Razorpay-Signature": razorpay_client.sign_payload(raw),
                     "Content-Type": "application/json"},
        )
        body = r.json()
        check("well-formed signed webhook is ingested and classified",
              r.status_code == 200 and body.get("bucket") == "insufficient_funds",
              str(body)[:110])

        # At-least-once delivery is normal, so the same event twice must not produce
        # a second attempt. The endpoint answers with the payment id it recognised
        # rather than a bare `true`, which is more useful in a log.
        r2 = await c.post(
            "/webhooks/razorpay",
            content=raw,
            headers={"X-Razorpay-Signature": razorpay_client.sign_payload(raw),
                     "Content-Type": "application/json"},
        )
        check("replayed webhook is recognised as a duplicate",
              r2.status_code == 200 and bool(r2.json().get("duplicate")),
              str(r2.json())[:90])

        print("\n── Live-demo controls " + "─" * 45)
        r = await c.post("/api/demo/outage", json={"issuer": "HDFC", "rail": "card"})
        check("POST /api/demo/outage trips a circuit", r.status_code == 200,
              str(r.json())[:100])

        r = await c.get("/api/issuers")
        hdfc = next((i for i in r.json() if i["issuer_key"] == "card:HDFC"), None)
        check("forced outage is visible as an open circuit",
              hdfc is not None and hdfc["state"] in ("open", "half_open"),
              f"card:HDFC = {hdfc['state'] if hdfc else 'absent'}")

        # "Returns 200" is not the claim this endpoint makes. It promises the circuit
        # closes and held retries fire, so assert the state, not the status code —
        # this check is here because an earlier version answered 200 with
        # `state: open` and the demo would have died on stage.
        r = await c.post("/api/demo/recover-issuer", json={"issuer": "HDFC", "rail": "card"})
        rec = r.json()
        check("POST /api/demo/recover-issuer actually closes the circuit",
              r.status_code == 200 and rec.get("state") == "closed",
              f"{rec.get('path')} · {rec.get('successes_injected')} successes → "
              f"{rec.get('success_rate')}")

        r = await c.get("/api/issuers")
        hdfc = next((i for i in r.json() if i["issuer_key"] == "card:HDFC"), None)
        check("recovery survives a re-read and keeps its trip history",
              hdfc is not None and hdfc["state"] == "closed" and hdfc["opened_count"] >= 1,
              f"card:HDFC = {hdfc['state'] if hdfc else 'absent'}, "
              f"{hdfc['opened_count'] if hdfc else 0} trip(s) on record")

        print("\n── Customer-facing checkout " + "─" * 39)
        # Search the full page rather than the newest 25: the most recent attempts
        # are often still pending and have no link yet, so a small window can miss
        # every link in the dataset and report a false failure.
        r = await c.get("/api/attempts?limit=300")
        link = next((a for a in r.json() if a.get("payment_link_id")), None)
        if link:
            lid = link["payment_link_id"]
            r = await c.get(f"/simulate/pay/{lid}")
            check("GET /simulate/pay/{id} renders a checkout page",
                  r.status_code == 200 and "html" in r.headers.get("content-type", ""),
                  f"{len(r.text)} bytes")
            r = await c.post(f"/simulate/pay/{lid}/complete")
            check("POST /simulate/pay/{id}/complete records payment",
                  r.status_code in (200, 303, 307),
                  f"HTTP {r.status_code}")
        else:
            check("a recovery link was generated to test checkout against", False,
                  "no attempt in the sample carried a payment_link_id")

        r = await c.get("/openapi.json")
        check("OpenAPI schema generates", r.status_code == 200,
              f"{len(r.json().get('paths', {}))} documented paths")

    print("\n" + "─" * 68)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}/{CHECKS}: " + "; ".join(FAILURES))
        return 1
    print(f"PASSED {CHECKS}/{CHECKS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
