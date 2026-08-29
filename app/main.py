from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Body, FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from app import (
    classifier,
    db,
    generator,
    issuer_health,
    razorpay_client,
    recovery,
    scheduler,
)
from app.config import settings
from app.models import CircuitState, FailedPayment, RazorpayWebhookEvent
from app.taxonomy import POLICIES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-20s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("reclaim.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    scheduler.start()
    log.info("Reclaim up — razorpay=%s classifier=%s llm=%s",
             "mock" if settings.use_mock_razorpay else "live",
             settings.classifier_mode,
             "on" if settings.llm_available else "off")
    yield
    scheduler.stop()


app = FastAPI(
    title="Reclaim",
    description="AI revenue-recovery agent for failed payments",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.dashboard_origin,
                   "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def ingest_event(event: dict[str, Any]) -> dict[str, Any]:
    parsed = RazorpayWebhookEvent.model_validate(event)

    if parsed.event != "payment.failed":
        return {"ok": True, "ignored": parsed.event}

    entity = parsed.payment_entity()
    if entity is None:
        return {"ok": True, "ignored": "no payment entity"}

    merchant_id = str(event.get("_merchant_id") or "merch_demo")
    fp = FailedPayment.from_razorpay(entity, merchant_id=merchant_id)

    if not db.insert_payment(fp):
        return {"ok": True, "duplicate": fp.payment_id}

    result = classifier.classify(fp)
    plan = recovery.ingest_and_plan(fp, result)

    return {
        "ok": True,
        "payment_id": fp.payment_id,
        "order_id": fp.order_id,
        "bucket": result.bucket.value,
        "confidence": round(result.confidence, 3),
        "classifier": result.source.value,
        "action": plan.action.value,
        "rail": plan.rail.value,
        "suppressed": plan.suppressed,
        "suppression_reason": plan.suppression_reason.value if plan.suppression_reason else None,
        "scheduled_for": plan.scheduled_for.isoformat(),
        "decision_trace": plan.decision_trace,
    }


@app.post("/webhooks/razorpay")
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
) -> JSONResponse:
    raw = await request.body()

    if settings.verify_webhook_signature:
        if not razorpay_client.verify_webhook_signature(raw, x_razorpay_signature):
            # 401 is correct here and Razorpay will retry — which is what we want,
            # since a signature mismatch usually means a misconfigured secret
            # rather than a forged request.
            log.warning("rejected webhook with bad signature")
            return JSONResponse({"ok": False, "error": "invalid signature"}, status_code=401)

    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "malformed json"}, status_code=400)

    try:
        return JSONResponse(ingest_event(event))
    except Exception:  # noqa: BLE001
        log.exception("ingest failed")
        return JSONResponse({"ok": True, "error": "ingest failed, logged for backfill"})


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "scheduler": scheduler.stats()["running"]}


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    return settings.describe() | {
        "demo_time_compression": settings.demo_time_compression,
        # Surfaced so the dashboard can show a banner. Nobody should be able to
        # look at these numbers without knowing outcomes are simulated.
        "outcomes_simulated": settings.use_mock_razorpay,
    }


@app.get("/api/stats")
def api_stats() -> dict[str, Any]:
    return db.headline_stats()


@app.get("/api/buckets")
def api_buckets() -> list[dict[str, Any]]:
    return db.stats_by_bucket()


@app.get("/api/attempts")
def api_attempts(limit: int = 60) -> list[dict[str, Any]]:
    return db.recent_attempts(limit=min(limit, 300))


@app.get("/api/issuers")
def api_issuers() -> list[dict[str, Any]]:
    return [h.model_dump(mode="json") for h in issuer_health.snapshot_all()]


@app.get("/api/classifier")
def api_classifier() -> dict[str, Any]:
    return db.classifier_stats()


@app.get("/api/suppressions")
def api_suppressions() -> list[dict[str, Any]]:
    return db.suppression_breakdown()


@app.get("/api/timeline")
def api_timeline() -> list[dict[str, Any]]:
    return db.timeline()


@app.get("/api/scheduler")
def api_scheduler() -> dict[str, Any]:
    return scheduler.stats()


@app.get("/api/taxonomy")
def api_taxonomy() -> list[dict[str, Any]]:
    return [
        {
            "bucket": b.value,
            "action": p.action.value,
            "timing": p.timing.value,
            "retryable": p.retryable,
            "max_attempts": p.max_attempts,
            "preferred_rail": p.preferred_rail.value,
            "excluded_rails": [r.value for r in p.excluded_rails],
            "base_recovery_rate": p.base_recovery_rate,
            "contacts_customer": p.contacts_customer,
            "rationale": p.rationale,
        }
        for b, p in POLICIES.items()
    ]


@app.post("/api/demo/seed")
def api_seed(
    count: int = Body(default=400, embed=True),
    seed: int = Body(default=42, embed=True),
    span_minutes: int = Body(default=180, embed=True),
) -> dict[str, Any]:
    """Populate the system with a realistic failed-payment stream.

    Successful authorizations are played in first. Without them the circuit
    breaker has no denominator — every issuer's success rate would be zero by
    construction and every circuit would sit open, which would look like a working
    demo of the wrong thing.
    """
    count = max(1, min(count, 5000))
    now = datetime.now(UTC)
    start = now - timedelta(minutes=span_minutes)
    outages = generator.default_outages(span_minutes)

    successes = generator.generate_successes(
        int(count * 4.5), seed=seed + 1, start=start, span_minutes=span_minutes, outages=outages
    )
    for issuer_key, ok, at in successes:
        issuer_health.observe(issuer_key, success=ok, at=at)

    events = generator.generate_events(
        count, seed=seed, start=start, span_minutes=span_minutes, outages=outages
    )

    ingested = 0
    for event in events:
        import json as _json

        raw = _json.dumps(event).encode()
        signature = razorpay_client.sign_payload(raw)
        if not razorpay_client.verify_webhook_signature(raw, signature):
            continue
        ingest_event(event)
        ingested += 1

    handled = scheduler.tick(limit=count * 2)

    return {
        "ok": True,
        "seeded": ingested,
        "baseline_successes": len(successes),
        "outages": [
            {"issuer": o.issuer, "rail": o.rail,
                "duration_minutes": o.duration_minutes}
            for o in outages
        ],
        "attempts_executed_immediately": handled,
        "stats": db.headline_stats(),
    }


@app.post("/api/demo/reset")
def api_reset() -> dict[str, Any]:
    db.reset_db()
    return {"ok": True, "stats": db.headline_stats()}


@app.post("/api/demo/tick")
def api_tick(limit: int = Body(default=200, embed=True)) -> dict[str, Any]:
    handled = scheduler.tick(limit=limit)
    return {"ok": True, "handled": handled, "stats": db.headline_stats()}


@app.post("/api/demo/outage")
def api_outage(
    issuer: str = Body(default="HDFC", embed=True),
    rail: str = Body(default="card", embed=True),
    failures: int = Body(default=40, embed=True),
) -> dict[str, Any]:
    key = f"{rail}:{issuer.upper()}"
    now = datetime.now(UTC)
    for i in range(max(1, min(failures, 500))):
        issuer_health.observe(key, success=False,
                              at=now - timedelta(seconds=i * 2))
    health = issuer_health.evaluate(key, now)
    return {
        "ok": True,
        "issuer_key": key,
        "state": health.state.value,
        "success_rate": round(health.success_rate, 4),
        "attempts_in_window": health.attempts,
        "note": (
            f"{key} is now {health.state.value}. Recovery attempts targeting it will be "
            "held, and eligible ones will fail over to a healthy rail."
        ),
    }


@app.post("/api/demo/recover-issuer")
def api_recover_issuer(
    issuer: str = Body(default="HDFC", embed=True),
    rail: str = Body(default="card", embed=True),
    successes: int = Body(default=40, embed=True),
) -> dict[str, Any]:
    """Bring an issuer back, so the circuit closes and held retries fire.

    The second half of the outage demo. Time is compressed — see
    `issuer_health.force_recover` for exactly what that means — but every
    transition is a real one, and `path` is the route the state machine actually
    took. If it ends on anything other than `closed`, the circuit genuinely did not
    close, and the response says so instead of claiming a recovery.
    """
    key = f"{rail}:{issuer.upper()}"
    now = datetime.now(UTC)
    health, path, injected = issuer_health.force_recover(
        key, min_successes=successes, now=now
    )
    closed = health.state is CircuitState.CLOSED
    handled = scheduler.tick(limit=200) if closed else 0
    return {
        "ok": True,
        "issuer_key": key,
        "state": health.state.value,
        "path": " → ".join(path),
        "success_rate": round(health.success_rate, 4),
        "attempts_in_window": health.attempts,
        "successes_injected": injected,
        "attempts_processed": handled,
        "note": (
            f"{key} recovered ({' → '.join(path)}); {handled} held attempt(s) released."
            if closed
            else (
                f"{key} is still {health.state.value} at {health.success_rate:.0%} "
                f"success — below the {settings.circuit.close_above:.0%} close threshold."
            )
        ),
    }


CHECKOUT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Complete your payment</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
    font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:#0b1020; color:#e8ecf8; padding:24px; }}
  .card {{ width:100%; max-width:380px; background:#141a2e; border:1px solid #232b45;
    border-radius:16px; padding:28px; }}
  .brand {{ font-size:13px; letter-spacing:.12em; text-transform:uppercase;
    color:#7c89b0; margin-bottom:20px; }}
  .amt {{ font-size:38px; font-weight:650; letter-spacing:-.02em; margin:0 0 4px; }}
  .ord {{ font-size:13px; color:#7c89b0; margin-bottom:22px; font-variant-numeric:tabular-nums; }}
  .tag {{ display:inline-block; font-size:12px; padding:4px 10px; border-radius:999px;
    background:#1d2740; color:#93a4cf; margin-bottom:20px; }}
  button {{ width:100%; padding:14px; font-size:16px; font-weight:600; border:0;
    border-radius:10px; background:#3d7bfd; color:#fff; cursor:pointer; }}
  button:hover {{ background:#2f6bea; }}
  button:disabled {{ opacity:.5; cursor:default; }}
  .note {{ margin-top:16px; font-size:12px; color:#63708f; text-align:center; }}
  .ok {{ text-align:center; }}
  .ok h1 {{ font-size:20px; margin:12px 0 6px; }}
  .tick {{ width:52px; height:52px; border-radius:50%; background:#13361f;
    color:#4ade80; display:grid; place-items:center; font-size:26px; margin:0 auto; }}
</style></head>
<body><div class="card" id="root">
  <div class="brand">Kirana Club</div>
  <span class="tag">{tag}</span>
  <p class="amt">₹{amount}</p>
  <p class="ord">Order {order_id}</p>
  <button id="pay" onclick="pay()">Pay ₹{amount}</button>
  <p class="note">Mock checkout — no real money moves.</p>
</div>
<script>
async function pay() {{
  const b = document.getElementById('pay');
  b.disabled = true; b.textContent = 'Processing…';
  const r = await fetch('/simulate/pay/{link_id}/complete', {{method:'POST'}});
  const d = await r.json();
  document.getElementById('root').innerHTML = d.ok
    ? `<div class="ok"><div class="tick">✓</div><h1>Payment successful</h1>
       <p class="ord">₹{amount} · Order {order_id}</p>
       <p class="note">Recovered via Reclaim. This order is now marked paid and the
       attempt is closed — a second completion would be rejected.</p></div>`
    : `<div class="ok"><div class="tick" style="background:#3a1d1d;color:#f87171">!</div>
       <h1>Could not complete</h1><p class="note">${{d.error}}</p></div>`;
}}
</script></body></html>"""


@app.get("/simulate/pay/{link_id}", response_class=HTMLResponse)
def simulate_checkout(link_id: str) -> Response:
    row = db.link_to_attempt(link_id)
    if row is None:
        return HTMLResponse("<h1>Unknown payment link</h1>", status_code=404)

    amount = int(row["amount_paise"]) // 100
    tags = {
        "upi_intent_link": "UPI · one tap, no OTP",
        "alt_method_link": "Alternate payment method",
        "request_new_instrument": "New payment method",
        "scheduled_retry": "Retry",
    }
    return HTMLResponse(
        CHECKOUT_HTML.format(
            link_id=link_id,
            amount=f"{amount:,}",
            order_id=row["order_id"],
            tag=tags.get(row["action"], "Payment"),
        )
    )


@app.post("/simulate/pay/{link_id}/complete")
def simulate_complete(link_id: str) -> dict[str, Any]:
    return recovery.complete_by_link(link_id)
