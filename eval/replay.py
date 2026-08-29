"""The eval. Replays one identical dataset through the policy ladder.

    python3 eval/replay.py                    # 2,000 payments, default seed
    python3 eval/replay.py --n 5000 --seed 3
    python3 eval/replay.py --json out.json

What makes this a controlled experiment rather than a demo:

* **One dataset.** Events are generated once and every policy sees the same list,
  in the same order, with the same amounts, issuers and error strings.
* **One classifier pass.** Classification is policy-independent, so it runs once
  and is shared. No rung can win by classifying better — they all get identical
  buckets, including identical mistakes.
* **One efficacy model.** Outcomes come from `app.simulator` for every rung.
* **Common random numbers.** The uniform that decides each outcome is keyed on
  (payment_id, attempt_no), so policy A and policy B resolve the same payment
  against the same draw. When A recovers something B does not, it is because A
  computed a higher conversion probability — never because A got luckier.
* **Circuit state is always computed, only sometimes consulted.** Policies without
  the circuit-breaker mechanism still fire into open circuits and still take the
  open-circuit penalty. That is the entire point: they cannot see the outage, so
  they pay for it. A policy is never scored on a fact it was allowed to ignore.

The one thing this cannot do is prove real-world lift. Outcomes are simulated
under the assumptions in `app/simulator.py`, so every number here is conditional
on those assumptions. What it does establish is that the decision mechanisms
dominate the baselines under assumptions that are written down, attackable, and
kept in a single file you can edit and re-run.

Read `app/simulator.py` before quoting any figure this produces.
"""

from __future__ import annotations

import argparse
import heapq
import json
import os
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Real delays, virtual clock. The demo compression hack must never touch a
# measured number.
os.environ["DEMO_TIME_COMPRESSION"] = "1"

import logging  # noqa: E402

logging.disable(logging.WARNING)

from app import classifier, generator, timing as timing_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import CircuitState, Classification, FailedPayment, RazorpayWebhookEvent  # noqa: E402
from app.simulator import (  # noqa: E402
    Treatment,
    converts,
    latent_traits,
    terminal_action,
)
from app.taxonomy import NEVER_RETRY, Action, Bucket, Rail, policy_for
from eval.policies import LADDER, Mechanisms  # noqa: E402

# Rails we will steer to, best-first. Mirrors app.policy.
RAIL_FALLBACK_ORDER = (Rail.UPI, Rail.NETBANKING, Rail.CARD, Rail.WALLET)

# Matches app.recovery.MAX_DEFERRALS.
MAX_DEFERRALS = 6

# Actions that require the customer to do something.
CONTACT_ACTIONS = frozenset(
    {Action.UPI_INTENT_LINK, Action.ALT_METHOD_LINK, Action.REQUEST_NEW_INSTRUMENT,
     Action.SCHEDULED_RETRY}
)


# ---------------------------------------------------------------------------
# In-memory issuer health
# ---------------------------------------------------------------------------


class HealthTracker:
    """Per-(rail, issuer) circuit breaker over a sliding window.

    Mirrors `app.issuer_health`'s state machine and reads the same thresholds from
    settings, but keeps its window in memory: the eval replays hundreds of
    thousands of observations across six policies, and a SQL aggregate per decision
    would dominate the runtime for no gain in fidelity.

    One instance per policy. Each policy's own retry outcomes feed back into its
    own health view, which is deliberate — a policy that hammers a failing issuer
    makes that issuer look worse to itself, exactly as it would in production.
    """

    def __init__(self) -> None:
        cfg = settings.circuit
        self.cfg = cfg
        self.events: dict[str, deque[tuple[datetime, bool]]] = defaultdict(deque)
        self.state: dict[str, CircuitState] = {}
        self.opened_at: dict[str, datetime] = {}
        self.probes: dict[str, int] = defaultdict(int)
        self.trips: dict[str, int] = defaultdict(int)

    def observe(self, key: str, *, success: bool, at: datetime) -> None:
        self.events[key].append((at, success))

    def _window(self, key: str, now: datetime) -> tuple[int, int]:
        cutoff = now - timedelta(minutes=self.cfg.window_minutes)
        q = self.events[key]
        # Observations arrive roughly in order, so trimming from the left is O(1)
        # amortised.
        while q and q[0][0] < cutoff:
            q.popleft()
        n = len(q)
        return n, sum(1 for _, ok in q if ok)

    def evaluate(self, key: str, now: datetime) -> tuple[CircuitState, float]:
        n, ok = self._window(key, now)
        rate = (ok / n) if n else 1.0
        state = self.state.get(key, CircuitState.CLOSED)
        cfg = self.cfg

        if state is CircuitState.CLOSED:
            if n >= cfg.min_samples and rate < cfg.open_below:
                state = CircuitState.OPEN
                self.opened_at[key] = now
                self.probes[key] = 0
                self.trips[key] += 1
        elif state is CircuitState.OPEN:
            opened = self.opened_at.get(key)
            if opened is None or now - opened >= timedelta(minutes=cfg.half_open_after_minutes):
                state = CircuitState.HALF_OPEN
                self.probes[key] = 0
        elif state is CircuitState.HALF_OPEN:
            if n >= max(1, cfg.probe_attempts) and rate >= cfg.close_above:
                state = CircuitState.CLOSED
                self.probes[key] = 0
            elif self.probes[key] >= cfg.probe_attempts:
                state = CircuitState.OPEN
                self.opened_at[key] = now
                self.probes[key] = 0
                self.trips[key] += 1

        self.state[key] = state
        return state, rate

    def allows(self, key: str, now: datetime) -> bool:
        state, _ = self.evaluate(key, now)
        if state is CircuitState.CLOSED:
            return True
        if state is CircuitState.HALF_OPEN:
            return self.probes[key] < self.cfg.probe_attempts
        return False

    def consume_probe(self, key: str) -> None:
        if self.state.get(key) is CircuitState.HALF_OPEN:
            self.probes[key] += 1


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@dataclass
class Work:
    """A scheduled recovery attempt in the replay queue."""

    fp: FailedPayment
    bucket: Bucket
    attempt_no: int
    deferrals: int = 0


@dataclass
class Result:
    mech: Mechanisms
    failed_count: int = 0
    failed_value: int = 0
    recovered_count: int = 0
    recovered_value: int = 0
    attempts: int = 0
    contacts: int = 0
    suppressed: int = 0
    expired: int = 0
    deferrals: int = 0
    fired_into_open_circuit: int = 0
    trips: int = 0
    by_bucket: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(
        lambda: {"failed": 0, "failed_value": 0, "attempts": 0, "recovered": 0, "recovered_value": 0}
    ))

    @property
    def recovery_rate(self) -> float:
        return self.recovered_value / self.failed_value if self.failed_value else 0.0

    @property
    def conversion(self) -> float:
        return self.recovered_count / self.attempts if self.attempts else 0.0

    @property
    def contacts_per_recovery(self) -> float:
        return self.contacts / self.recovered_count if self.recovered_count else 0.0


def _steer_rail(fp: FailedPayment, bucket: Bucket, health: HealthTracker,
                now: datetime, mech: Mechanisms) -> Rail:
    """Pick the rail to attempt on."""
    pol = policy_for(bucket)
    if not mech.rail_steering:
        # No steering: reuse whatever failed, even if that reproduces the failure.
        return fp.rail

    preferred = pol.preferred_rail if pol.preferred_rail is not Rail.ANY else fp.rail
    candidates = [preferred] + [r for r in RAIL_FALLBACK_ORDER if r != preferred]

    for rail in candidates:
        if rail in pol.excluded_rails:
            continue
        key = f"{rail.value}:{(fp.issuer or 'unknown').upper()}"
        if not mech.circuit_breaker or health.allows(key, now):
            return rail
    return preferred


def _choose_action(fp: FailedPayment, bucket: Bucket, mech: Mechanisms) -> Action:
    if not mech.bucket_actions:
        # One hammer for every nail: re-present the same charge.
        return Action.RETRY_SILENT
    return terminal_action(policy_for(bucket).action)


def run_policy(
    mech: Mechanisms,
    payments: list[FailedPayment],
    classifications: dict[str, Classification],
    successes: list[tuple[str, bool, datetime]],
) -> Result:
    """Replay every payment through one policy against a virtual clock."""
    res = Result(mech=mech)
    health = HealthTracker()

    # Baseline authorization traffic, identical for every policy. Without it the
    # circuit breaker has no denominator and every issuer looks permanently down.
    for key, ok, at in successes:
        health.observe(key, success=ok, at=at)

    queue: list[tuple[datetime, int, Work]] = []
    seq = 0
    paid: set[str] = set()
    contacts_by_customer: dict[str, int] = defaultdict(int)

    for fp in payments:
        bucket = classifications[fp.payment_id].bucket
        res.failed_count += 1
        res.failed_value += fp.amount_paise
        b = res.by_bucket[bucket.value]
        b["failed"] += 1
        b["failed_value"] += fp.amount_paise

        # Every observed failure feeds health, for every policy.
        health.observe(fp.issuer_key, success=False, at=fp.failed_at)

        if not mech.attempts_recovery:
            continue

        # ---- guardrails at decision time --------------------------------
        if mech.guardrails:
            if bucket in NEVER_RETRY:
                res.suppressed += 1
                continue
            pol = policy_for(bucket)
            if pol.contacts_customer and not (fp.contact or fp.email):
                res.suppressed += 1
                continue

        pol = policy_for(bucket)
        when = (
            timing_mod.schedule(
                pol.timing, now=fp.failed_at, attempt_no=1,
                contacts_customer=pol.contacts_customer,
            )
            if mech.smart_timing
            else fp.failed_at
        )
        seq += 1
        heapq.heappush(queue, (when, seq, Work(fp=fp, bucket=bucket, attempt_no=1)))

    # ---- process the queue in time order --------------------------------
    while queue:
        now, _, work = heapq.heappop(queue)
        fp, bucket = work.fp, work.bucket
        pol = policy_for(bucket)

        # Between scheduling and firing, the order may have been recovered by an
        # earlier attempt. Every policy gets this check — it is a correctness
        # requirement, not a mechanism, and letting a baseline double-charge would
        # inflate it for the wrong reason.
        if fp.order_id in paid:
            continue

        action = _choose_action(fp, bucket, mech)
        rail = _steer_rail(fp, bucket, health, now, mech)
        key = f"{rail.value}:{(fp.issuer or 'unknown').upper()}"

        # ---- circuit gate ------------------------------------------------
        if mech.circuit_breaker and not health.allows(key, now):
            if work.deferrals >= MAX_DEFERRALS:
                res.expired += 1
                continue
            res.deferrals += 1
            work.deferrals += 1
            seq += 1
            heapq.heappush(
                queue,
                (now + timedelta(minutes=settings.circuit.half_open_after_minutes + 1),
                 seq, work),
            )
            continue

        # The mandate gate is a legal constraint, not a mechanism, so it applies to
        # every rung. Charging a customer who is not on the page without standing
        # authority is not something any of these policies may do.
        if action is Action.RETRY_SILENT and not fp.tokenized:
            action = Action.ALT_METHOD_LINK

        contacts_customer = action in CONTACT_ACTIONS

        # ---- contact caps ------------------------------------------------
        if contacts_customer:
            ckey = fp.contact or fp.email or fp.order_id
            if not (fp.contact or fp.email):
                res.suppressed += 1
                continue
            if mech.guardrails:
                cap = settings.guardrails.max_contacts_per_customer_per_day
                if contacts_by_customer[ckey] >= cap:
                    res.suppressed += 1
                    continue
            contacts_by_customer[ckey] += 1
            res.contacts += 1

        # ---- resolve -----------------------------------------------------
        # State is computed for every policy, whether or not the policy is allowed
        # to look at it. A policy that cannot see the outage still fires into it
        # and still pays the penalty.
        state, _ = health.evaluate(key, now)
        if state is CircuitState.OPEN:
            res.fired_into_open_circuit += 1

        traits = latent_traits(fp.payment_id, bucket)
        treatment = Treatment(
            bucket=bucket,
            action=action,
            rail=rail,
            attempt_no=work.attempt_no,
            delay_hours=max(0.0, (now - fp.failed_at).total_seconds() / 3600.0),
            circuit_state=state,
            amount_paise=fp.amount_paise,
        )
        won, _p, _factors = converts(fp.payment_id, traits, treatment)

        res.attempts += 1
        res.by_bucket[bucket.value]["attempts"] += 1
        health.observe(key, success=won, at=now)
        health.consume_probe(key)

        if won:
            paid.add(fp.order_id)
            res.recovered_count += 1
            res.recovered_value += fp.amount_paise
            res.by_bucket[bucket.value]["recovered"] += 1
            res.by_bucket[bucket.value]["recovered_value"] += fp.amount_paise
            continue

        # ---- retry ------------------------------------------------------
        cap = min(mech.max_attempts, pol.max_attempts) if mech.guardrails else mech.max_attempts
        if work.attempt_no >= cap:
            continue

        nxt = work.attempt_no + 1
        if mech.smart_timing:
            retry_at = timing_mod.schedule(
                pol.timing, now=now, attempt_no=nxt, contacts_customer=pol.contacts_customer
            )
            retry_at = max(retry_at, now + timedelta(
                minutes=settings.guardrails.min_cooldown_minutes))
        else:
            retry_at = now + timedelta(minutes=5)

        seq += 1
        heapq.heappush(queue, (retry_at, seq, Work(
            fp=fp, bucket=bucket, attempt_no=nxt, deferrals=0)))

    res.trips = sum(health.trips.values())
    return res


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def rupees(paise: int) -> str:
    return f"₹{paise / 100:,.0f}"


def crore(paise: int) -> str:
    """Indian units. A reviewer reads ₹1.2Cr faster than ₹12,000,000."""
    r = paise / 100
    if r >= 1e7:
        return f"₹{r / 1e7:.2f}Cr"
    if r >= 1e5:
        return f"₹{r / 1e5:.2f}L"
    return f"₹{r:,.0f}"


def build_dataset(n: int, seed: int, span_minutes: int, start: datetime):
    """Generate one dataset and classify it once."""
    outages = generator.default_outages(span_minutes)
    events = generator.generate_events(
        n, seed=seed, start=start, span_minutes=span_minutes, outages=outages
    )
    successes = generator.generate_successes(
        int(n * 4.5), seed=seed + 1, start=start, span_minutes=span_minutes, outages=outages
    )
    payments: list[FailedPayment] = []
    for ev in events:
        entity = RazorpayWebhookEvent.model_validate(ev).payment_entity()
        if entity is not None:
            payments.append(FailedPayment.from_razorpay(entity, merchant_id="merch_eval"))
    classifications = {fp.payment_id: classifier.classify(fp) for fp in payments}
    return payments, classifications, successes, outages


def aggregate(runs: list[Result]) -> Result:
    """Sum a policy's per-seed results into one pooled result.

    Pooling rather than averaging rates: the recovery rate of the pooled dataset is
    the value-weighted figure, which is the one a merchant cares about. Averaging
    per-seed percentages would silently weight a small seed the same as a large one.
    """
    total = Result(mech=runs[0].mech)
    for r in runs:
        total.failed_count += r.failed_count
        total.failed_value += r.failed_value
        total.recovered_count += r.recovered_count
        total.recovered_value += r.recovered_value
        total.attempts += r.attempts
        total.contacts += r.contacts
        total.suppressed += r.suppressed
        total.expired += r.expired
        total.deferrals += r.deferrals
        total.fired_into_open_circuit += r.fired_into_open_circuit
        total.trips += r.trips
        for name, b in r.by_bucket.items():
            for k, v in b.items():
                total.by_bucket[name][k] += v
    return total


def spread(runs: list[Result]) -> tuple[float, float]:
    """(min, max) recovery rate across seeds."""
    rates = [r.recovery_rate for r in runs]
    return min(rates), max(rates)


def report(results: list[Result], per_seed: dict[str, list[Result]], meta: dict) -> str:
    lines: list[str] = []
    w = lines.append

    baseline = next(r for r in results if r.mech.name == "naive_retry")
    floor = next(r for r in results if r.mech.name == "do_nothing")
    full = next(r for r in results if r.mech.name == "full_agent")

    w("=" * 82)
    w("RECLAIM — POLICY ABLATION")
    w("=" * 82)
    w(f"{meta['n_per_seed']} failed payments × {len(meta['seeds'])} seeds "
      f"= {floor.failed_count} payments worth {crore(floor.failed_value)}")
    w(f"seeds {meta['seeds']} · {meta['span_hours']}h failure window · "
      f"classifier: {meta['classifier_mode']}")
    w("Outcomes are simulated under the assumptions in app/simulator.py.")
    w("")

    w(f"{'policy':<26} {'recovered':>11} {'by value':>9} {'by count':>9} "
      f"{'seed spread':>13} {'msgs/win':>9} {'vs naive':>9}")
    w("─" * 82)
    for r in results:
        lo, hi = spread(per_seed[r.mech.name])
        band = f"{lo:.1%}–{hi:.1%}" if len(meta["seeds"]) > 1 else "—"
        uplift = (
            f"{(r.recovered_value - baseline.recovered_value) / baseline.recovered_value:+.0%}"
            if baseline.recovered_value else "—"
        )
        if r.mech.name == "naive_retry":
            uplift = "baseline"
        if r.mech.name == "do_nothing":
            uplift, band = "—", "—"
        count_rate = r.recovered_count / r.failed_count if r.failed_count else 0.0
        w(f"{r.mech.label:<26} {crore(r.recovered_value):>11} {r.recovery_rate:>8.1%} "
          f"{count_rate:>8.1%} {band:>13} {r.contacts_per_recovery:>9.2f} {uplift:>9}")
    w("")
    w("  Two rates because they answer different questions. By value is what the")
    w("  merchant's revenue line sees, and it is the noisier of the two — order")
    w("  amounts are heavy-tailed, so a handful of large orders move it. By count is")
    w("  the stabler measure of whether the decisions are right. The seed spread is")
    w("  on the value rate, the one with the most room to flatter itself.")
    w("")

    # Marginal contribution of each mechanism — the number the ladder exists for.
    w("What each mechanism is worth")
    w("─" * 82)
    prev = None
    for r in results:
        if prev is not None:
            delta = r.recovered_value - prev.recovered_value
            pct = (delta / prev.recovered_value) if prev.recovered_value else float("inf")
            pcts = "     —" if pct == float("inf") else f"{pct:+6.0%}"
            w(f"  {r.mech.label:<26} {'+' if delta >= 0 else '−'}{crore(abs(delta)):>10}"
              f"  {pcts}")
        prev = r
    w("")
    w("  Read the last row as the price of compliance, not a failure: the guardrails")
    w("  refuse business the rung above them was happy to take.")
    w("")

    w("Operational cost of the uplift")
    w("─" * 82)
    w(f"  naive       {baseline.attempts:>6} attempts  {baseline.contacts:>6} customer messages  "
      f"{baseline.fired_into_open_circuit:>5} fired into a dead issuer")
    w(f"  full agent  {full.attempts:>6} attempts  {full.contacts:>6} customer messages  "
      f"{full.fired_into_open_circuit:>5} fired into a dead issuer")
    if baseline.contacts:
        w(f"  → {(full.contacts - baseline.contacts) / baseline.contacts:+.0%} messages, for "
          f"{(full.recovered_value - baseline.recovered_value) / max(1, baseline.recovered_value):+.0%} "
          f"revenue. Messages per recovered order: "
          f"{baseline.contacts_per_recovery:.2f} → {full.contacts_per_recovery:.2f}")
    w(f"  → {full.suppressed} payments deliberately left alone by the guardrails")
    w("")

    # The single clearest piece of evidence that rail steering is structural rather
    # than cosmetic, and it is not a revenue number.
    gated = next(r for r in results if r.mech.name == "circuit_aware")
    steered = next(r for r in results if r.mech.name == "rail_steering")
    w("What rail steering actually changed")
    w("─" * 82)
    w(f"  circuit breaking alone   {gated.deferrals:>6} attempts parked waiting for an issuer, "
      f"{gated.expired:>4} abandoned")
    w(f"  + rail steering          {steered.deferrals:>6} attempts parked, "
      f"{steered.expired:>4} abandoned")
    w("  The gate stops you firing into a dead issuer; steering means you no longer")
    w("  have to wait for it. Same outage, no queue — the retry leaves over a rail")
    w("  that is actually up. Neither number is revenue, and both matter.")
    w("")

    w("Full agent, by failure bucket")
    w("─" * 82)
    w(f"  {'bucket':<20} {'failed':>7} {'value':>10} {'attempts':>9} {'recov':>6} "
      f"{'conv':>6} {'recovered':>11}")
    rows = sorted(full.by_bucket.items(), key=lambda kv: -kv[1]["recovered_value"])
    for name, b in rows:
        conv = b["recovered"] / b["attempts"] if b["attempts"] else 0.0
        note = "  ← never retried, by policy" if b["attempts"] == 0 else ""
        w(f"  {name:<20} {b['failed']:>7} {crore(b['failed_value']):>10} {b['attempts']:>9} "
          f"{b['recovered']:>6} {conv:>5.0%} {crore(b['recovered_value']):>11}{note}")
    w("")

    w("Where naive loses most")
    w("─" * 82)
    diffs = []
    for name, b in full.by_bucket.items():
        nb = baseline.by_bucket.get(name, {"recovered_value": 0})
        diffs.append((b["recovered_value"] - nb["recovered_value"], name,
                      nb["recovered_value"], b["recovered_value"]))
    for delta, name, nv, fv in sorted(diffs, reverse=True)[:5]:
        w(f"  {name:<20} naive {crore(nv):>10} → agent {crore(fv):>10}   "
          f"{crore(delta):>10} more")
    w("")
    w("=" * 82)
    lo, hi = spread(per_seed["full_agent"])
    w(f"Headline: the agent recovers {crore(full.recovered_value)} of "
      f"{crore(full.failed_value)} in failed payments — {full.recovery_rate:.1%} by value")
    w(f"          (per-seed range {lo:.1%}–{hi:.1%}), "
      f"{(full.recovered_value / max(1, baseline.recovered_value) - 1):.0%} more than naive retry,")
    if full.contacts <= baseline.contacts:
        w(f"          while sending {(1 - full.contacts / max(1, baseline.contacts)):.0%} "
          f"fewer customer messages.")
    else:
        w(f"          for {(full.contacts / max(1, baseline.contacts) - 1):.0%} more customer "
          f"messages ({full.contacts_per_recovery:.2f} per win vs "
          f"{baseline.contacts_per_recovery:.2f}).")
    w("=" * 82)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay the policy ladder over one dataset.")
    ap.add_argument("--n", type=int, default=2000, help="failed payments per seed")
    ap.add_argument("--seeds", type=int, default=5, help="how many seeds to replicate over")
    ap.add_argument("--seed", type=int, default=7, help="first seed")
    ap.add_argument("--span-hours", type=int, default=3)
    ap.add_argument("--json", type=str, default=None, help="also write machine-readable results")
    args = ap.parse_args()

    span_minutes = args.span_hours * 60
    # Fixed start, so runs are reproducible. It is not a neutral choice: how far the
    # window sits from the 1st/28th changes what a payday-aligned retry is worth, so
    # the per-seed spread below is the honest measure of stability, not this date.
    start = datetime(2026, 8, 20, 4, 30, tzinfo=UTC)
    seeds = [args.seed + i for i in range(max(1, args.seeds))]

    per_seed: dict[str, list[Result]] = defaultdict(list)
    outages_meta: list = []

    for s in seeds:
        print(f"seed {s}: generating {args.n} failed payments…", end=" ", flush=True)
        payments, classifications, successes, outages = build_dataset(
            args.n, s, span_minutes, start
        )
        if not outages_meta:
            outages_meta = outages
        print(f"classified {len(payments)} · replaying {len(LADDER)} policies…", flush=True)
        for mech in LADDER:
            per_seed[mech.name].append(
                run_policy(mech, payments, classifications, successes)
            )

    results = [aggregate(per_seed[m.name]) for m in LADDER]

    meta = {
        "n_per_seed": args.n,
        "seeds": seeds,
        "span_hours": args.span_hours,
        "start": start.isoformat(),
        "classifier_mode": settings.classifier_mode,
        "llm_available": settings.llm_available,
        "outages": [{"issuer": o.issuer, "rail": o.rail,
                     "duration_minutes": o.duration_minutes} for o in outages_meta],
    }

    text = report(results, per_seed, meta)
    print("\n" + text)

    out_dir = ROOT / "eval" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.txt").write_text(text + "\n", encoding="utf-8")

    payload = {
        "meta": meta,
        "policies": [
            {
                "name": r.mech.name,
                "label": r.mech.label,
                "description": r.mech.description,
                "recovered_value_paise": r.recovered_value,
                "recovered_count": r.recovered_count,
                "failed_value_paise": r.failed_value,
                "failed_count": r.failed_count,
                "recovery_rate": r.recovery_rate,
                "attempts": r.attempts,
                "conversion": r.conversion,
                "contacts": r.contacts,
                "contacts_per_recovery": r.contacts_per_recovery,
                "suppressed": r.suppressed,
                "expired": r.expired,
                "deferrals": r.deferrals,
                "fired_into_open_circuit": r.fired_into_open_circuit,
                "circuit_trips": r.trips,
                "per_seed_recovery_rate": [x.recovery_rate for x in per_seed[r.mech.name]],
                "by_bucket": dict(r.by_bucket),
            }
            for r in results
        ],
    }
    (out_dir / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"\nWrote {out_dir / 'latest.txt'} and {out_dir / 'latest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
