# Reclaim

**A payment-failure recovery agent.** It reads *why* each payment failed, chooses the
recovery action that fits that reason, picks the moment and the rail to attempt it on —
and declines to attempt the ones that should not be attempted.

> On 10,000 simulated failed payments worth **₹17.42Cr**, Reclaim recovers **₹6.24Cr —
> 35.8% by value**, which is **105% more than naive retry** while sending **19% fewer
> customer messages**.
>
> That number comes from a simulator whose assumptions are stated and argued in
> [Calibration & honesty](#calibration--honesty). Read that section before quoting it.

---

## The problem

Razorpay's merchants care about exactly one number more than any other: **payment success
rate**. Failure rates on Indian online payments are commonly reported in the 15–30% range
depending on rail and merchant category — expired cards, issuer downtime, abandoned UPI
collect requests, balance shortfalls, bank-side timeouts. I am citing that range, not
measuring it; the point does not depend on where in it a given merchant sits.

Most of that failed volume is *recoverable*. Almost none of it is recovered well, because
the industry default is to treat "failed" as one thing and retry it on a timer. That is
wrong in both directions at once:

- **It retries what it shouldn't.** A `risk_declined` payment retried is a chargeback
  waiting to happen. A payment fired at an issuer that is currently down burns an attempt
  and an SMS for a guaranteed failure.
- **It doesn't retry what it should.** A balance shortfall retried 30 minutes later fails
  for the same reason. Retried on payday, it converts. An abandoned UPI collect doesn't
  need a retry at all — it needs an intent link while the customer's intent is still warm.

The recovery action is a *function of the failure reason*. So the failure reason has to be
understood, not pattern-matched on a substring.

---

## What it does

Five mechanisms, each of which earns its keep separately (see the ablation below):

**1. Classification into a recovery taxonomy.** Nine buckets, defined by *what you can do
about the failure* rather than by the error string the gateway happened to return.
Deterministic rules handle ~93% of traffic; an LLM is reserved for the long tail of
free-text error descriptions that rules cannot place, and **abstains** below a confidence
floor rather than guessing a bucket. `app/classifier.py`, `app/taxonomy.py`

**2. Per-bucket recovery policy.** Each bucket names its action, its timing, its preferred
rail, and its attempt budget — including two buckets whose policy is *never retry*.
`app/taxonomy.py`

**3. Timing.** `payday_aligned` for balance shortfalls, `immediate` for warm intent,
`short_backoff` for technical faults, `next_waking_hour` for anything that contacts a
human — with quiet hours enforced in the merchant's timezone. `app/timing.py`

**4. Per-issuer circuit breaking, with rail steering.** Success rate is tracked per
`(rail, issuer)` over a rolling window. Below 35% over at least 20 samples the circuit
opens; retries are then *steered to a rail that is actually up* rather than queued behind
the outage. `app/issuer_health.py`, `app/policy.py`

**5. Guardrails.** Attempt caps, cooldowns, quiet hours, per-customer contact limits,
mandate gating (a silent re-charge requires tokenization; without it the action degrades to
a customer-completed link), and idempotency enforced by a unique index. These *cost*
revenue — 11% of it — and the eval reports that cost rather than hiding it.
`app/guardrails.py`

Every decision is written to an audit trail as it is made — each gate checked, each veto,
the rail chosen, the reason for the delay. Click any row in the dashboard's decision log:

```
classified auth_abandoned @ 0.88 via rules [reason:authentication_failed]
policy: upi_intent_link / immediate / prefer upi
✓ bucket_is_retryable
✓ order_not_paid
✓ no_attempt_in_flight
✓ within_attempt_budget
✓ cooldown_elapsed
✓ customer_not_suppressed
✓ contact_channel_exists
✓ customer_contact_budget
✓ merchant_contact_budget
→ rail steered netbanking → upi (bucket preference)
✓ circuit closed for upi:ICIC (67% over 15 attempts/15m)
scheduled now (Sat 22 Aug 19:12 IST) — intent is still warm — every minute costs conversion
will contact customer via sms/whatsapp
→ created UPI-only payment link plink_3c32d555731f86
→ message [template] sent: delivered via whatsapp to +919439594689
```

A suppression is recorded the same way — as an attempt row with `action=suppress` and the
gate that stopped it. **A decision not to act is a decision, not an absence**, and the
dashboard reserves space for those rows so they cannot fall off the bottom of the log.

---

## Results

Seven rungs, each adding exactly one mechanism to the one below it, over 5 seeds with
common random numbers so the rungs face identical failures:

| policy | recovered | by value | by count | seed spread | msgs/win | vs naive |
|---|---:|---:|---:|---:|---:|---:|
| No recovery | ₹0 | 0.0% | 0.0% | — | 0.00 | — |
| Naive retry | ₹3.05Cr | 17.5% | 18.1% | 15.9%–18.6% | 8.99 | baseline |
| + failure taxonomy | ₹3.79Cr | 21.7% | 23.1% | 18.8%–24.7% | 6.98 | +24% |
| + timing | ₹4.40Cr | 25.3% | 30.9% | 21.2%–28.7% | 5.08 | +44% |
| + circuit breaking | ₹4.98Cr | 28.6% | 35.9% | 24.2%–31.9% | 4.04 | +63% |
| + rail steering | ₹7.02Cr | 40.3% | 44.0% | 36.6%–44.5% | 3.38 | +130% |
| **Full agent (+ guardrails)** | **₹6.24Cr** | **35.8%** | **42.7%** | 31.4%–40.5% | **3.10** | **+105%** |

Three things worth noticing, all of which a single agent-vs-baseline number would hide:

- **Rail steering is the biggest single win** (+41%), not the taxonomy. The circuit gate
  stops you firing into a dead issuer; steering means you no longer have to *wait* for it.
  Attempts parked waiting on an outage drop from 10,954 to 952, and abandoned-after-waiting
  from 816 to 0.
- **The guardrail rung loses ₹77.16L.** That is the price of compliance, reported as a
  negative number, because the rung above it was happy to take business the guardrails
  refuse — 651 payments deliberately left alone.
- **Fewer messages, more revenue.** 8.99 → 3.10 messages per recovered order. An agent that
  doubled revenue by tripling the SMS volume would not be a good agent, and this is the
  number that shows the difference.

Full breakdown, including per-bucket conversion and where naive loses most:
[`eval/results/latest.txt`](eval/results/latest.txt). Regenerate with:

```bash
python3 eval/replay.py
```

---

## Run it

Backend — no database to provision, no account needed, works with the repo as-is:

```bash
pip install -r requirements.txt && uvicorn app.main:app --reload --port 8000
```

Dashboard, in a second terminal:

```bash
cd dashboard && npm install && npm run dev
```

Open <http://localhost:5173>, then **Seed 400** in the top-right controls. The dashboard
polls every 2.5s; the scheduler ticks every 10s.

The demo controls exist to make a three-day recovery cycle watchable in three minutes:

| control | what it does |
|---|---|
| **Seed 400** | generates 400 realistic failed payments across 8 real Indian issuers and 4 rails, signs them, and posts them through the actual webhook endpoint |
| **Advance queue** | runs the scheduler immediately instead of waiting for the next tick |
| **Break HDFC cards** | drives `card:HDFC` failures until the circuit trips, so you can watch retries steer to UPI |
| **Recover HDFC** | injects the number of successes the close threshold actually requires and walks `open → half_open → closed` |
| **Reset** | drops all state |

Set `DEMO_TIME_COMPRESSION=240` (see `.env.example`) to make a payday-aligned retry fire in
minutes rather than four days. It divides scheduled delays and nothing else; the eval and
all test scripts pin it to 1.

### Going live

`cp .env.example .env`, set `USE_MOCK_RAZORPAY=false` and your test keys, and point a
Razorpay `payment.failed` webhook at `POST /webhooks/razorpay` with a matching
`RAZORPAY_WEBHOOK_SECRET`. No code changes: the mock mirrors the real API's request
validation and response shapes, and the same functions run in both modes. The dashboard
banner flips from amber to green on its own when keys are present — a disclaimer you have to
remember to delete is a disclaimer that ships to production.

`ANTHROPIC_API_KEY` is optional. Without it the classifier runs rules-only and messaging
falls back to templates; the system still works end to end, it just stops handling the messy
long tail.

---

## Architecture

```
Razorpay payment.failed webhook
        │  signature verified (HMAC-SHA256, constant-time compare)
        ▼
  app/classifier.py ──── rules → bucket (93%)
        │                └─ LLM for the long tail, abstains below 0.55
        ▼
  app/taxonomy.py ────── bucket → action · timing · rail · attempt budget
        ▼
  app/policy.py ──────── rail selection, issuer-health gating, steering
        ▼
  app/guardrails.py ──── attempt cap · cooldown · quiet hours · contact
        │                budgets · mandate gating   → veto ⇒ suppression row
        ▼
  app/timing.py ──────── when, in the merchant's timezone
        ▼
  app/db.py ──────────── scheduled attempt, UNIQUE(idempotency_key)
        ▼
  app/scheduler.py ───── background thread, executes what's due
        ▼
  app/recovery.py ────── executors per action; re-checks "already paid"
        │                immediately before charging
        ▼
  app/razorpay_client.py  Payment Links API (or the mock)
        ▼
  app/messaging.py ───── SMS / WhatsApp / email copy per bucket
```

| module | lines | what it owns |
|---|---:|---|
| `app/db.py` | 944 | schema, queries, all dashboard read models |
| `app/classifier.py` | 467 | rules + LLM classification, abstention |
| `app/main.py` | 466 | FastAPI app, webhook, 19 endpoints, demo controls |
| `app/recovery.py` | 455 | one executor per action, idempotent |
| `app/generator.py` | 371 | realistic failed-payment synthesis |
| `app/messaging.py` | 351 | per-bucket customer messaging |
| `app/models.py` | 336 | Pydantic models, enums |
| `app/simulator.py` | 290 | outcome model — **the only simulated part** |
| `app/razorpay_client.py` | 290 | real client + mock, same interface |
| `app/taxonomy.py` | 279 | the policy table. This is the product. |
| `app/issuer_health.py` | 250 | circuit breaker state machine |
| `app/guardrails.py` | 229 | the gates, and their traces |
| `app/policy.py` | 227 | rail selection and steering |
| `app/config.py` | 184 | env-driven settings, all with defaults |
| `app/timing.py` | 174 | payday alignment, quiet hours |
| `app/scheduler.py` | 163 | daemon-thread tick loop |
| `eval/replay.py` | 689 | the 7-rung ablation |
| `dashboard/` | 1,952 | React 19 + Vite, zero chart or CSS dependencies |

Storage is plain `sqlite3` in WAL mode with thread-local connections — no ORM. For a system
whose correctness argument rests on *one unique index*, being able to read every query as
written is worth more than the abstraction.

---

## The failure taxonomy

| bucket | action | timing | rail | max | prior |
|---|---|---|---|---:|---:|
| `issuer_down` | await_issuer_health | issuer_health_gated | any | 2 | 0.62 |
| `insufficient_funds` | scheduled_retry | payday_aligned | upi | 2 | 0.41 |
| `auth_abandoned` | upi_intent_link | immediate | upi | 2 | 0.58 |
| `instrument_invalid` | request_new_instrument | next_waking_hour | upi | 1 | 0.34 |
| `limit_exceeded` | alt_method_link | immediate | netbanking | 1 | 0.47 |
| `technical` | retry_silent | short_backoff | any | 3 | 0.71 |
| `customer_cancelled` | alt_method_link | next_waking_hour | any | 1 | 0.19 |
| `risk_declined` | **suppress** | — | — | **0** | — |
| `unknown` | **manual_review** | — | — | **0** | — |

The two zero-attempt rows are the ones a naive system gets wrong and the ones that matter
most: retrying a risk decline buys chargebacks, and acting on an unclassifiable failure is
acting on a guess. The dashboard serves this table live at `GET /api/taxonomy` so the
frontend never keeps its own copy of the policy.

---

## Calibration & honesty

**This section is the one to read before believing any number above.**

### What is production code and what is modelled

Everything in the decision path is real, and the same functions run in mock mode and against
live keys: webhook ingest and signature verification, classification, the policy table, rail
selection, every guardrail, the circuit-breaker state machine, timing, scheduling,
idempotency, message composition, and the Payment Links calls.

**One thing is simulated: whether a customer actually paid.** In mock mode that draw comes
from `app/simulator.py`. With live keys it comes from Razorpay.

### Where the priors come from

The `base_recovery_rate` for each bucket — the probability that a *correctly executed*
attempt on that bucket converts — is calibrated against **published Indian payment-gateway
benchmarks and industry reporting, not measured on live merchant traffic.** I do not have
live merchant traffic. Anyone who claims a recovery figure without saying this is either
sitting on proprietary data or making it up.

So the honest description of the headline number is: **35.8% is a measure of decision
quality under stated assumptions, not a forecast of what a merchant would see.** The
mechanism ranking is the durable finding; the absolute rupee figure is not.

### Why the eval is structured to be hard to fool

The obvious way to fake this result is to pick priors that flatter the agent's choices. Four
things are in place specifically to make that visible:

1. **Rung-by-rung ablation.** Seven policies, each adding exactly one mechanism. A prior
   tuned to favour one mechanism shows up as that rung's delta, not as a diffuse headline.
2. **Common random numbers.** Every rung faces byte-identical failures and identical
   conversion draws for the same `(payment, attempt)`. Differences are attributable to
   decisions, not to luck.
3. **Two rates and a seed spread.** By-value is what a revenue line sees and is the noisier
   measure — order amounts are heavy-tailed. By-count is the stabler measure of whether the
   decisions were right. The reported spread is on the *value* rate, the one with the most
   room to flatter itself.
4. **A rung that loses money.** The guardrails cost ₹77.16L, and that is printed as a
   negative. An eval that only ever goes up is not measuring anything.

The simulator's per-attempt factors (attempt-number decay, amount sensitivity) are stated in
`app/simulator.py` and applied identically to every rung, including naive retry.

### Verification actually run

| command | what it checks | result |
|---|---|---|
| `python3 scripts/smoke.py` | 16 end-to-end invariants: attempt caps derived from the policy table, never-retry buckets never charged, zero duplicate idempotency keys, every attempt carries a trace | **16/16** |
| `python3 scripts/api_check.py` | 28 checks across all 19 endpoints, including circuit *state* after the recovery control rather than just HTTP 200 | **28/28** |
| `python3 eval/replay.py` | the 7-rung ablation, 5 seeds | 7 rungs, monotone except the guardrail rung |
| `node scripts/jsx_check.mjs` | every dashboard file parses under Vite's JSX/ESM settings; every local import resolves | **15/15** |
| `node scripts/ssr_check.mjs` | every component server-rendered against real `/api/*` payloads, plus currency-formatting boundaries | **29/29** |

### What was *not* verified, and why

The sandbox this was built in blocks the npm registry (403) and PyPI, so two ordinary gates
could not run here:

- **`npm install` and `npm run build` were never executed.** The dashboard is
  parse-verified and server-rendered, and its components have been checked against real API
  payloads — but Vite's own transform pipeline, CSS processing, and production minification
  are unverified. `cd dashboard && npm install && npm run dev` is the real gate and it is one
  command.
- **`pip install -r requirements.txt` was never executed.** The bounds are lower bounds
  rather than pins because Python 3.14 only has wheels in recent releases; the system Python
  used here already satisfied all of them, which is how the backend has been running
  throughout. Pin exactly before deploying anything real.

Two scripts exist because of those gaps and are development tooling, not part of the app:
`scripts/fixture.py` dumps all 10 read models to `data/fixture.json` through
`httpx.ASGITransport`, and `scripts/render_check.mjs` builds `data/render-check.html`, a
single self-contained file that runs the real components against that fixture in any browser.

### Known limitations

- **Effects are not exercised by `ssr_check.mjs`.** `useEffect` does not run in a server
  render, so polling, the live toggle, and the expanded-trace click are covered only by the
  browser harness, which no browser has run in this environment.
- **Recovery timestamps cluster in the demo.** A batch tick stamps every execution at once,
  so the timeline chart puts all recoveries in the newest hour on a freshly seeded database.
  Live ticks spread out; the seeded snapshot does not.
- **The LLM path is inert without a key**, so `hybrid` mode degrades to `rules` and the
  classifier panel reports those payments as `llm_fallback_rules` (~6% of traffic) rather
  than silently counting them as rule hits. A further ~1% abstains and becomes `unknown`,
  which is *never retried* — the safe direction, but a real deployment would want the key.
- **The circuit breaker is per-process.** State lives in SQLite, so multiple workers share
  it, but there is no leader election on the probe budget; two workers could both probe a
  half-open circuit.
- **`max_contacts_per_merchant_per_day` defaults to 5000**, high enough not to bind on a
  400-payment demo. It is a real limit in production and would need setting per merchant.

---

## What I'd ship next

1. **Replace the priors with measured ones.** The whole system is instrumented for it: every
   attempt already records bucket, action, timing, rail, and outcome. Two weeks of live
   traffic turns `base_recovery_rate` from a cited benchmark into a per-merchant posterior,
   and the ablation harness becomes a regression test on real data.
2. **Contextual bandit over the policy table.** The taxonomy currently encodes one fixed
   action per bucket. Per-merchant, per-issuer, per-amount-band exploration on top of it is
   the obvious next lift, and the trace format already contains the features.
3. **Leader election on circuit probes**, so the half-open budget is honoured across workers.
4. **A merchant-facing suppression report.** Merchants will ask why 8% of failures were never
   attempted, and "here is the gate and the amount" is a better answer than a number.
5. **Webhook replay and dead-letter handling.** Signature verification is in place;
   at-least-once delivery with a durable retry queue is not.
