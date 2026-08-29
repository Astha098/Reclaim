from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

ISSUERS: list[tuple[str, float]] = [
    ("HDFC", 0.22),
    ("ICIC", 0.18),
    ("SBIN", 0.20),
    ("UTIB", 0.12),
    ("KKBK", 0.09),
    ("PUNB", 0.07),
    ("YESB", 0.06),
    ("IDFB", 0.06),
]

UPI_HANDLES = ["okhdfcbank", "okicici", "oksbi",
               "okaxis", "ybl", "paytm", "ibl", "apl"]

RAILS: list[tuple[str, float]] = [
    ("upi", 0.55),
    ("card", 0.28),
    ("netbanking", 0.10),
    ("wallet", 0.07),
]

FAILURE_TEMPLATES: dict[str, list[tuple[str | None, str, str, str, str]]] = {
    "insufficient_funds": [
        ("insufficient_funds", "BAD_REQUEST_ERROR", "bank", "payment_authorization",
         "Payment failed because of insufficient funds in the account"),
        ("insufficient_funds", "GATEWAY_ERROR", "issuer", "payment_authorization",
         "Your payment could not be completed as the account has insufficient balance"),
        (None, "BAD_REQUEST_ERROR", "bank", "payment_authorization",
         "Transaction declined - low balance"),
    ],
    "auth_abandoned": [
        ("invalid_otp", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
         "Payment failed because the OTP entered was incorrect"),
        ("payment_timeout", "GATEWAY_ERROR", "customer", "payment_authentication",
         "Payment was not completed on time"),
        ("upi_collect_expired", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
         "The UPI collect request expired before it was approved"),
        ("authentication_failed", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
         "3D Secure authentication could not be completed"),
        (None, "GATEWAY_ERROR", "customer", "payment_authentication",
         "Customer did not complete the verification step"),
    ],
    "issuer_down": [
        ("issuer_down", "GATEWAY_ERROR", "issuer", "payment_authorization",
         "Payment failed because your bank was unavailable"),
        ("gateway_timeout", "GATEWAY_ERROR", "bank", "payment_authorization",
         "Your payment could not be completed due to an issue with your bank"),
        ("npci_unavailable", "GATEWAY_ERROR", "network", "payment_authorization",
         "NPCI is not responding, please try again"),
        (None, "GATEWAY_ERROR", "bank", "payment_authorization",
         "Unable to reach the issuing bank"),
    ],
    "customer_cancelled": [
        ("payment_cancelled", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
         "Payment was cancelled by the user"),
        ("user_cancelled", "BAD_REQUEST_ERROR", "customer", "payment_initiation",
         "The customer aborted the payment"),
    ],
    "technical": [
        ("server_error", "SERVER_ERROR", "internal", "payment_initiation",
         "An internal server error occurred while processing the payment"),
        ("gateway_error", "GATEWAY_ERROR", "gateway", "payment_capture",
         "The gateway returned a malformed response"),
        ("invalid_request", "BAD_REQUEST_ERROR", "internal", "payment_initiation",
         "The payment request was malformed"),
    ],
    "instrument_invalid": [
        ("card_expired", "BAD_REQUEST_ERROR", "customer", "payment_authorization",
         "Payment failed because the card has expired"),
        ("invalid_cvv", "BAD_REQUEST_ERROR", "customer", "payment_authentication",
         "The CVV entered was incorrect"),
        ("invalid_vpa", "BAD_REQUEST_ERROR", "customer", "payment_initiation",
         "The UPI ID entered does not exist"),
        ("card_blocked", "BAD_REQUEST_ERROR", "issuer", "payment_authorization",
         "This card is blocked for online transactions"),
    ],
    "limit_exceeded": [
        ("payment_limit_exceeded", "BAD_REQUEST_ERROR", "issuer", "payment_authorization",
         "The amount exceeds the per-transaction limit set by your bank"),
        ("international_transaction_not_allowed", "BAD_REQUEST_ERROR", "issuer",
         "payment_authorization",
         "International transactions are not enabled on this card"),
        ("transaction_limit_exceeded", "BAD_REQUEST_ERROR", "bank", "payment_authorization",
         "Daily transaction limit has been crossed"),
    ],
    "risk_declined": [
        ("risk_declined", "BAD_REQUEST_ERROR", "business", "payment_authorization",
         "Payment was declined by the risk engine"),
        ("suspected_fraud", "BAD_REQUEST_ERROR", "issuer", "payment_authorization",
         "The issuer flagged this transaction as potentially fraudulent"),
    ],
    "unlabelled": [
        (None, "GATEWAY_ERROR", "", "", "Payment failed"),
        (None, "BAD_REQUEST_ERROR", "", "", "The payment could not be processed"),
        (None, "GATEWAY_ERROR", "bank", "", "Something went wrong"),
        (None, "SERVER_ERROR", "", "payment_authorization", "Transaction unsuccessful"),
    ],
}

FAILURE_MIX: list[tuple[str, float]] = [
    ("auth_abandoned", 0.26),
    ("insufficient_funds", 0.18),
    ("issuer_down", 0.14),
    ("customer_cancelled", 0.11),
    ("technical", 0.09),
    ("instrument_invalid", 0.08),
    ("limit_exceeded", 0.06),
    ("risk_declined", 0.05),
    ("unlabelled", 0.03),
]


@dataclass
class Outage:
    issuer: str
    rail: str
    start_minute: int
    duration_minutes: int


def _weighted(rng: random.Random, options: list[tuple[str, float]]) -> str:
    r = rng.random()
    cum = 0.0
    for name, weight in options:
        cum += weight
        if r <= cum:
            return name
    return options[-1][0]


def _amount_paise(rng: random.Random, family: str = "") -> int:
    if family == "limit_exceeded":
        return rng.randint(25_000, 200_000) * 100
    if family == "insufficient_funds":
        band = rng.random()
        rupees = rng.randint(
            199, 3_000) if band < 0.75 else rng.randint(3_000, 40_000)
        return rupees * 100

    band = rng.random()
    if band < 0.60:
        rupees = rng.randint(99, 1_500)
    elif band < 0.85:
        rupees = rng.randint(1_500, 8_000)
    elif band < 0.95:
        rupees = rng.randint(8_000, 50_000)
    else:
        rupees = rng.randint(50_000, 300_000)
    return rupees * 100


def _phone(rng: random.Random) -> str:
    return f"+919{rng.randint(100000000, 999999999)}"


def generate_events(
    n: int,
    *,
    seed: int = 42,
    start: datetime | None = None,
    span_minutes: int = 240,
    outages: list[Outage] | None = None,
    merchant_id: str = "merch_demo",
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    start = start or (datetime.now(UTC) - timedelta(minutes=span_minutes))
    outages = outages if outages is not None else default_outages(span_minutes)

    events: list[dict[str, Any]] = []
    for i in range(n):
        minute = rng.uniform(0, span_minutes)
        created = start + timedelta(minutes=minute)

        rail = _weighted(rng, RAILS)
        issuer = _weighted(rng, ISSUERS)

        family: str | None = None
        for out in outages:
            in_window = out.start_minute <= minute <= out.start_minute + out.duration_minutes
            if in_window and rng.random() < 0.85:
                issuer, rail = out.issuer, out.rail
                family = "issuer_down"
                break

        if family is None:
            family = _weighted(rng, FAILURE_MIX)

        reason, code, source, step, description = rng.choice(
            FAILURE_TEMPLATES[family])
        amount = _amount_paise(rng, family)

        has_phone = rng.random() < 0.92
        has_email = rng.random() < 0.70
        if not (has_phone or has_email):
            has_phone = rng.random() < 0.5

        pid = f"pay_{seed:04d}{i:06d}"
        entity: dict[str, Any] = {
            "id": pid,
            "entity": "payment",
            "amount": amount,
            "currency": "INR",
            "status": "failed",
            "order_id": f"order_{seed:04d}{i:06d}",
            "method": rail,
            "international": family == "limit_exceeded" and rng.random() < 0.3,
            "captured": False,
            "description": f"Order #{1000 + i}",
            "email": f"cust{i}@example.com" if has_email else None,
            "contact": _phone(rng) if has_phone else None,
            "notes": {},
            "error_code": code,
            "error_description": description,
            "error_source": source or None,
            "error_step": step or None,
            "error_reason": reason,
            "acquirer_data": {"bank": issuer} if rail != "upi" else {},
            "created_at": int(created.timestamp()),
        }

        if rail == "upi":
            entity["vpa"] = f"cust{i}@{rng.choice(UPI_HANDLES)}"
            entity["bank"] = issuer
        elif rail == "netbanking":
            entity["bank"] = issuer
        elif rail == "wallet":
            entity["wallet"] = issuer
        elif rail == "card":
            entity["card_id"] = f"card_{rng.getrandbits(48):012x}"
            entity["bank"] = issuer
            if rng.random() < 0.35:
                entity["token_id"] = f"token_{rng.getrandbits(48):012x}"

        events.append(
            {
                "entity": "event",
                "account_id": "acc_demo",
                "event": "payment.failed",
                "contains": ["payment"],
                "payload": {"payment": {"entity": entity}},
                "created_at": int(created.timestamp()),
                "_merchant_id": merchant_id,
            }
        )

    events.sort(key=lambda e: e["created_at"])
    return events


def default_outages(span_minutes: int) -> list[Outage]:
    return [
        Outage(issuer="HDFC", rail="card", start_minute=int(span_minutes * 0.35),
               duration_minutes=max(12, int(span_minutes * 0.12))),
        Outage(issuer="SBIN", rail="upi", start_minute=int(span_minutes * 0.62),
               duration_minutes=max(10, int(span_minutes * 0.10))),
    ]


def generate_successes(
    n: int,
    *,
    seed: int = 99,
    start: datetime | None = None,
    span_minutes: int = 240,
    outages: list[Outage] | None = None,
) -> list[tuple[str, bool, datetime]]:
    rng = random.Random(seed)
    start = start or (datetime.now(UTC) - timedelta(minutes=span_minutes))
    outages = outages if outages is not None else default_outages(span_minutes)
    out: list[tuple[str, bool, datetime]] = []

    for _ in range(n):
        minute = rng.uniform(0, span_minutes)
        at = start + timedelta(minutes=minute)
        rail = _weighted(rng, RAILS)
        issuer = _weighted(rng, ISSUERS)

        suppressed = any(
            o.issuer == issuer
            and o.rail == rail
            and o.start_minute <= minute <= o.start_minute + o.duration_minutes
            for o in outages
        )
        if suppressed and rng.random() < 0.92:
            continue

        out.append((f"{rail}:{issuer}", True, at))

    out.sort(key=lambda t: t[2])
    return out
