from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.taxonomy import Action, Bucket, Rail, Timing


# ---------------------------------------------------------------------------
# Razorpay wire format
# ---------------------------------------------------------------------------


class RazorpayPaymentEntity(BaseModel):
    """The `payload.payment.entity` object from a payment.failed webhook.

    Field set follows Razorpay's Payments API. Everything except `id` is
    optional because error metadata is inconsistently populated across rails —
    UPI failures often carry no `error_source`, older card failures no
    `error_reason`.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    entity: str = "payment"
    amount: int = 0  # paise
    currency: str = "INR"
    status: str = "failed"
    order_id: str | None = None
    method: str | None = None
    international: bool = False
    captured: bool = False
    description: str | None = None
    card_id: str | None = None
    bank: str | None = None
    wallet: str | None = None
    vpa: str | None = None
    email: str | None = None
    contact: str | None = None
    notes: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    acquirer_data: dict[str, Any] = Field(default_factory=dict)
    created_at: int | None = None


class RazorpayWebhookEvent(BaseModel):
    model_config = ConfigDict(extra="allow")

    entity: str = "event"
    account_id: str | None = None
    event: str
    contains: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: int | None = None

    def payment_entity(self) -> RazorpayPaymentEntity | None:
        raw = (self.payload.get("payment") or {}).get("entity")
        if not isinstance(raw, dict):
            return None
        return RazorpayPaymentEntity.model_validate(raw)


# ---------------------------------------------------------------------------
# Normalized internal shape
# ---------------------------------------------------------------------------


def _rail_from_method(method: str | None) -> Rail:
    match (method or "").lower():
        case "upi":
            return Rail.UPI
        case "card":
            return Rail.CARD
        case "netbanking":
            return Rail.NETBANKING
        case "wallet":
            return Rail.WALLET
        case "emi":
            return Rail.UPI
        case _:
            return Rail.UPI


class FailedPayment(BaseModel):
    """A failed payment attempt, normalized."""

    payment_id: str
    order_id: str
    merchant_id: str = "merch_demo"
    amount_paise: int
    currency: str = "INR"
    rail: Rail
    # Store the issuer so circuit health can be tracked per rail.
    issuer: str | None = None
    vpa: str | None = None
    email: str | None = None
    contact: str | None = None
    international: bool = False

    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None

    # Tokenized payments can support recovery without asking the customer again.
    tokenized: bool = False

    failed_at: datetime
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @property
    def amount_rupees(self) -> float:
        return self.amount_paise / 100.0

    @property
    def issuer_key(self) -> str:
        """Circuit-breaker key.

        Scoped to rail *and* issuer: HDFC cards going down says nothing about
        HDFC netbanking, and treating them as one signal blocks recoveries that
        would have worked.
        """
        return f"{self.rail.value}:{(self.issuer or 'unknown').upper()}"

    @classmethod
    def from_razorpay(
        cls,
        entity: RazorpayPaymentEntity,
        *,
        merchant_id: str = "merch_demo",
    ) -> FailedPayment:
        rail = _rail_from_method(entity.method)

        # Derive the issuer from the fields available on the payment.
        issuer = entity.bank or entity.wallet
        if issuer is None and entity.vpa and "@" in entity.vpa:
            issuer = entity.vpa.split("@", 1)[1]
        if issuer is None:
            acq = entity.acquirer_data or {}
            issuer = acq.get("bank") or acq.get("acquirer") or None

        failed_at = (
            datetime.fromtimestamp(entity.created_at, tz=UTC)
            if entity.created_at
            else datetime.now(UTC)
        )

        # Some integrations store the saved instrument ID in notes.
        extra = entity.model_dump()
        tokenized = bool(
            extra.get("token_id") or entity.notes.get(
                "token_id") or entity.notes.get("mandate_id")
        )

        return cls(
            payment_id=entity.id,
            order_id=entity.order_id or f"order_shadow_{entity.id}",
            merchant_id=merchant_id,
            amount_paise=entity.amount,
            currency=entity.currency,
            rail=rail,
            issuer=issuer,
            vpa=entity.vpa,
            email=entity.email,
            contact=entity.contact,
            international=entity.international,
            error_code=entity.error_code,
            error_description=entity.error_description,
            error_source=entity.error_source,
            error_step=entity.error_step,
            error_reason=entity.error_reason,
            tokenized=tokenized,
            failed_at=failed_at,
            raw=extra,
        )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class ClassifierSource(str, Enum):
    RULES = "rules"
    LLM = "llm"
    LLM_FALLBACK_RULES = "llm_fallback_rules"  # LLM errored, rules result used
    ABSTAINED = "abstained"


class Classification(BaseModel):
    """Classification result with an auditable decision record."""

    # Store the final classification.
    bucket: Bucket
    confidence: float = Field(ge=0.0, le=1.0)
    source: ClassifierSource

    # Explain why the classifier made this choice.
    reasoning: str = ""

    # Keep the evidence used for the classification.
    evidence: list[str] = Field(default_factory=list)

    # Keep the strongest alternative when the result is uncertain.
    alternative_bucket: Bucket | None = None

    # Record the risk of acting on this classification.
    action_risk: str = "high"

    # Keep the rules result even when the LLM makes the final choice.
    rules_bucket: Bucket | None = None
    rules_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    rules_matched_rule: str | None = None

    # Mark cases where AI and rules disagree.
    disagreement: bool = False

    # Store the rule used for a rules-only result.
    matched_rule: str | None = None

    # Record how long classification took.
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


class SuppressionReason(str, Enum):
    """Why the system decided not to act."""

    NOT_RETRYABLE = "not_retryable"
    MAX_ATTEMPTS = "max_attempts"
    ALREADY_PAID = "already_paid"
    IN_FLIGHT = "in_flight"
    COOLDOWN = "cooldown"
    CUSTOMER_CONTACT_CAP = "customer_contact_cap"
    MERCHANT_CONTACT_CAP = "merchant_contact_cap"
    NO_CONSENT = "no_consent"
    NO_CONTACT_CHANNEL = "no_contact_channel"
    CIRCUIT_OPEN_NO_ALT = "circuit_open_no_alt"


class RecoveryPlan(BaseModel):
    """The decision. Produced by the policy engine, executed by the worker."""

    payment_id: str
    order_id: str
    bucket: Bucket
    action: Action
    timing: Timing
    rail: Rail
    attempt_no: int
    scheduled_for: datetime
    # True when this plan deliberately does nothing.
    suppressed: bool = False
    suppression_reason: SuppressionReason | None = None
    # Keep the checks and reasons behind the decision.
    decision_trace: list[str] = Field(default_factory=list)
    expected_recovery_rate: float = 0.0
    amount_paise: int = 0

    @property
    def is_actionable(self) -> bool:
        return not self.suppressed


class AttemptOutcome(str, Enum):
    PENDING = "pending"
    RECOVERED = "recovered"
    FAILED_AGAIN = "failed_again"
    # The link was sent but the payment did not complete.
    NO_RESPONSE = "no_response"
    SUPPRESSED = "suppressed"
    EXPIRED = "expired"


class RecoveryAttempt(BaseModel):
    """A persisted recovery attempt and its result."""

    id: int | None = None
    payment_id: str
    order_id: str
    merchant_id: str = "merch_demo"
    attempt_no: int
    bucket: Bucket
    action: Action
    timing: Timing
    rail: Rail
    amount_paise: int
    # This key makes each recovery attempt unique.
    idempotency_key: str
    scheduled_for: datetime
    executed_at: datetime | None = None
    outcome: AttemptOutcome = AttemptOutcome.PENDING
    suppression_reason: SuppressionReason | None = None
    payment_link_id: str | None = None
    payment_link_url: str | None = None
    message_body: str | None = None
    contacted_customer: bool = False
    recovered_paise: int = 0
    decision_trace: list[str] = Field(default_factory=list)

    @staticmethod
    def make_idempotency_key(order_id: str, attempt_no: int) -> str:
        return f"{order_id}#recovery#{attempt_no}"


class CircuitState(str, Enum):
    CLOSED = "closed"  # Healthy and ready for attempts.
    OPEN = "open"  # Unhealthy, so attempts are blocked.
    HALF_OPEN = "half_open"  # Testing the issuer with limited traffic.


class IssuerHealth(BaseModel):
    issuer_key: str
    state: CircuitState
    attempts: int
    successes: int
    success_rate: float
    window_minutes: int
    opened_at: datetime | None = None
    probes_used: int = 0
    # Count how many times the circuit has opened.
    opened_count: int = 0
    updated_at: datetime
