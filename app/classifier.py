from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.models import Classification, ClassifierSource, FailedPayment
from app.taxonomy import Bucket

log = logging.getLogger("reclaim.classifier")


# ---------------------------------------------------------------------------
# Tier 1 — exact error_reason match
# ---------------------------------------------------------------------------

REASON_MAP: dict[str, tuple[Bucket, float]] = {
    # Balance
    "insufficient_funds": (Bucket.INSUFFICIENT_FUNDS, 0.99),
    "insufficient_balance": (Bucket.INSUFFICIENT_FUNDS, 0.99),
    "not_enough_balance": (Bucket.INSUFFICIENT_FUNDS, 0.97),

    # Authentication drop-off
    "invalid_otp": (Bucket.AUTH_ABANDONED, 0.95),
    "incorrect_otp": (Bucket.AUTH_ABANDONED, 0.95),
    "otp_attempts_exceeded": (Bucket.AUTH_ABANDONED, 0.94),
    "authentication_failed": (Bucket.AUTH_ABANDONED, 0.88),
    "payment_timeout": (Bucket.AUTH_ABANDONED, 0.85),
    "upi_collect_expired": (Bucket.AUTH_ABANDONED, 0.92),
    "collect_request_expired": (Bucket.AUTH_ABANDONED, 0.92),
    "3ds_authentication_failed": (Bucket.AUTH_ABANDONED, 0.90),

    # Explicit cancel
    "payment_cancelled": (Bucket.CUSTOMER_CANCELLED, 0.97),
    "payment_canceled_by_user": (Bucket.CUSTOMER_CANCELLED, 0.97),
    "user_cancelled": (Bucket.CUSTOMER_CANCELLED, 0.97),
    "payment_declined_by_user": (Bucket.CUSTOMER_CANCELLED, 0.95),

    # Dead instrument
    "card_expired": (Bucket.INSTRUMENT_INVALID, 0.99),
    "invalid_card": (Bucket.INSTRUMENT_INVALID, 0.96),
    "invalid_cvv": (Bucket.INSTRUMENT_INVALID, 0.96),
    "invalid_expiry": (Bucket.INSTRUMENT_INVALID, 0.96),
    "invalid_card_number": (Bucket.INSTRUMENT_INVALID, 0.96),
    "invalid_vpa": (Bucket.INSTRUMENT_INVALID, 0.97),
    "vpa_not_found": (Bucket.INSTRUMENT_INVALID, 0.97),
    "account_blocked": (Bucket.INSTRUMENT_INVALID, 0.90),
    "card_blocked": (Bucket.INSTRUMENT_INVALID, 0.92),

    # Caps
    "payment_limit_exceeded": (Bucket.LIMIT_EXCEEDED, 0.98),
    "amount_exceeds_limit": (Bucket.LIMIT_EXCEEDED, 0.98),
    "transaction_limit_exceeded": (Bucket.LIMIT_EXCEEDED, 0.98),
    "international_transaction_not_allowed": (Bucket.LIMIT_EXCEEDED, 0.95),
    "card_not_enabled_for_online": (Bucket.LIMIT_EXCEEDED, 0.90),

    # Bank / issuer side
    "issuer_down": (Bucket.ISSUER_DOWN, 0.99),
    "bank_down": (Bucket.ISSUER_DOWN, 0.99),
    "issuer_not_available": (Bucket.ISSUER_DOWN, 0.97),
    "gateway_timeout": (Bucket.ISSUER_DOWN, 0.85),
    "npci_unavailable": (Bucket.ISSUER_DOWN, 0.95),
    "psp_down": (Bucket.ISSUER_DOWN, 0.95),

    # Risk — never retried
    "risk_declined": (Bucket.RISK_DECLINED, 0.99),
    "suspected_fraud": (Bucket.RISK_DECLINED, 0.99),
    "payment_declined_by_risk": (Bucket.RISK_DECLINED, 0.99),
    "fraud_suspected": (Bucket.RISK_DECLINED, 0.99),
    "blocked_by_risk_engine": (Bucket.RISK_DECLINED, 0.99),

    # Technical
    "server_error": (Bucket.TECHNICAL, 0.90),
    "internal_error": (Bucket.TECHNICAL, 0.90),
    "gateway_error": (Bucket.TECHNICAL, 0.80),
    "invalid_request": (Bucket.TECHNICAL, 0.85),
}


# ---------------------------------------------------------------------------
# Tier 3 — description keywords
# ---------------------------------------------------------------------------

DESCRIPTION_PATTERNS: list[tuple[str, Bucket, float]] = [
    (
        r"insufficient\s+(funds|balance)",
        Bucket.INSUFFICIENT_FUNDS,
        0.96,
    ),
    (
        r"exceeds?\s+(the\s+)?(available\s+)?balance",
        Bucket.INSUFFICIENT_FUNDS,
        0.90,
    ),
    (
        r"low\s+balance",
        Bucket.INSUFFICIENT_FUNDS,
        0.93,
    ),
    (
        r"\b(otp|one[\s-]?time\s+password)\b.*"
        r"(incorrect|invalid|wrong|failed|expired)",
        Bucket.AUTH_ABANDONED,
        0.92,
    ),
    (
        r"(incorrect|invalid|wrong)\b.*\b(otp|password)",
        Bucket.AUTH_ABANDONED,
        0.92,
    ),
    (
        r"not\s+completed\s+on\s+time",
        Bucket.AUTH_ABANDONED,
        0.88,
    ),
    (
        r"(timed?\s*out|timeout)",
        Bucket.AUTH_ABANDONED,
        0.62,
    ),
    (
        r"authentication\s+(failed|could not)",
        Bucket.AUTH_ABANDONED,
        0.85,
    ),
    (
        r"collect\s+request\s+(expired|timed)",
        Bucket.AUTH_ABANDONED,
        0.90,
    ),
    (
        r"cancelled\s+by\s+(the\s+)?(user|customer)",
        Bucket.CUSTOMER_CANCELLED,
        0.95,
    ),
    (
        r"(user|customer)\s+(cancelled|aborted|abandoned)",
        Bucket.CUSTOMER_CANCELLED,
        0.93,
    ),
    (
        r"card\s+(has\s+)?expired",
        Bucket.INSTRUMENT_INVALID,
        0.97,
    ),
    (
        r"(invalid|incorrect)\s+(cvv|card|expiry|vpa|upi\s+id)",
        Bucket.INSTRUMENT_INVALID,
        0.94,
    ),
    (
        r"vpa\s+(does\s+not\s+exist|not\s+found|invalid)",
        Bucket.INSTRUMENT_INVALID,
        0.95,
    ),
    (
        r"(card|account)\s+(is\s+)?(blocked|frozen|inactive)",
        Bucket.INSTRUMENT_INVALID,
        0.90,
    ),
    (
        r"(limit|cap)\s+(exceeded|reached|crossed)",
        Bucket.LIMIT_EXCEEDED,
        0.95,
    ),
    (
        r"exceeds?\s+(the\s+)?"
        r"(per[\s-]?transaction|daily|maximum)\s+limit",
        Bucket.LIMIT_EXCEEDED,
        0.95,
    ),
    (
        r"international\s+(transactions?|cards?)\s+"
        r"(not\s+)?(allowed|enabled|permitted)",
        Bucket.LIMIT_EXCEEDED,
        0.93,
    ),
    (
        r"(bank|issuer)\s+(is\s+)?"
        r"(down|unavailable|not\s+responding|unreachable)",
        Bucket.ISSUER_DOWN,
        0.96,
    ),
    (
        r"(issue|problem)\s+with\s+(your\s+)?bank",
        Bucket.ISSUER_DOWN,
        0.82,
    ),
    (
        r"(npci|psp|upi)\s+"
        r"(down|unavailable|not\s+responding)",
        Bucket.ISSUER_DOWN,
        0.93,
    ),
    (
        r"unable\s+to\s+(reach|contact)\s+(the\s+)?(bank|issuer)",
        Bucket.ISSUER_DOWN,
        0.90,
    ),
    (
        r"(fraud|risk|suspicious)",
        Bucket.RISK_DECLINED,
        0.88,
    ),
    (
        r"declined\s+by\s+(our\s+)?(risk|fraud)",
        Bucket.RISK_DECLINED,
        0.96,
    ),
    (
        r"(internal|server)\s+error",
        Bucket.TECHNICAL,
        0.85,
    ),
    (
        r"something\s+went\s+wrong",
        Bucket.TECHNICAL,
        0.60,
    ),
]


_COMPILED = [
    (re.compile(pattern, re.I), bucket, confidence)
    for pattern, bucket, confidence in DESCRIPTION_PATTERNS
]


# ---------------------------------------------------------------------------
# Tier 2 — source + step inference
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InferenceRule:
    name: str
    predicate: Callable[[FailedPayment], bool]
    bucket: Bucket
    confidence: float


def _src(fp: FailedPayment) -> str:
    return (fp.error_source or "").lower()


def _step(fp: FailedPayment) -> str:
    return (fp.error_step or "").lower()


INFERENCE_RULES: list[InferenceRule] = [
    InferenceRule(
        "customer_dropped_at_auth",
        lambda fp: _src(fp) == "customer"
        and _step(fp) == "payment_authentication",
        Bucket.AUTH_ABANDONED,
        0.82,
    ),
    InferenceRule(
        "internal_or_gateway_fault",
        lambda fp: _src(fp) in {"internal", "gateway"},
        Bucket.TECHNICAL,
        0.78,
    ),
    InferenceRule(
        "network_fault_at_authorization",
        lambda fp: _src(fp) in {"network", "npci"}
        and _step(fp) == "payment_authorization",
        Bucket.ISSUER_DOWN,
        0.75,
    ),
    InferenceRule(
        "bank_declined_at_authorization",
        lambda fp: _src(fp) in {"bank", "issuer"}
        and _step(fp) == "payment_authorization",
        Bucket.INSUFFICIENT_FUNDS,
        0.52,
    ),
    InferenceRule(
        "business_rule_block",
        lambda fp: _src(fp) == "business",
        Bucket.LIMIT_EXCEEDED,
        0.60,
    ),
]


# ---------------------------------------------------------------------------
# Rules classifier
# ---------------------------------------------------------------------------

def classify_by_rules(fp: FailedPayment) -> Classification:
    started = time.perf_counter()

    def done(
        bucket: Bucket,
        confidence: float,
        rule: str,
        why: str,
    ) -> Classification:
        return Classification(
            bucket=bucket,
            confidence=confidence,
            source=ClassifierSource.RULES,
            reasoning=why,
            matched_rule=rule,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    reason = (fp.error_reason or "").strip().lower()

    if reason in REASON_MAP:
        bucket, confidence = REASON_MAP[reason]
        return done(
            bucket,
            confidence,
            f"reason:{reason}",
            f"Exact match on error_reason={reason!r}.",
        )

    description = fp.error_description or ""

    for pattern, bucket, confidence in _COMPILED:
        if pattern.search(description):
            return done(
                bucket,
                confidence,
                f"description:{pattern.pattern[:40]}",
                f"Matched issuer description pattern in {description!r}.",
            )

    for rule in INFERENCE_RULES:
        if rule.predicate(fp):
            return done(
                rule.bucket,
                rule.confidence,
                f"inference:{rule.name}",
                (
                    f"Inferred from error_source={fp.error_source!r}, "
                    f"error_step={fp.error_step!r}."
                ),
            )

    if fp.international:
        return done(
            Bucket.LIMIT_EXCEEDED,
            0.55,
            "inference:international_fallback",
            (
                "International payment with no usable error metadata; "
                "most commonly a card-level block."
            ),
        )

    return Classification(
        bucket=Bucket.UNKNOWN,
        confidence=0.0,
        source=ClassifierSource.ABSTAINED,
        reasoning=(
            "No rule matched: error_reason, description and "
            "source/step were all uninformative."
        ),
        matched_rule=None,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )


# ---------------------------------------------------------------------------
# LLM classifier
# ---------------------------------------------------------------------------

_BUCKET_GUIDE = """
issuer_down          The bank / issuer / NPCI / PSP was unavailable or timed out on
                     their side. Customer intent was fine.
insufficient_funds   The account or card did not have the money. Intent fine.
auth_abandoned       The customer started paying but did not finish the
                     authentication step — OTP wrong/expired, 3DS dropped, UPI
                     collect request never approved.
instrument_invalid   The payment instrument itself is unusable: expired card, bad
                     CVV, non-existent VPA, blocked card. A retry on the same
                     instrument is guaranteed to fail again.
limit_exceeded       A cap was hit: per-transaction limit, daily limit, UPI ceiling,
                     international transactions not enabled on the card.
risk_declined        Blocked deliberately by a fraud/risk engine. NEVER retried.
technical            A fault on our side or the gateway's.
customer_cancelled   The customer explicitly cancelled or backed out.
unknown              You genuinely cannot tell. Choose this rather than guessing.
""".strip()


CLASSIFIER_TOOL = {
    "name": "record_classification",
    "description": (
        "Record the failure bucket and the evidence/risk assessment "
        "for this payment."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bucket": {
                "type": "string",
                "enum": [bucket.value for bucket in Bucket],
                "description": "The single best-fitting recovery bucket.",
            },
            "confidence": {
                "type": "number",
                "description": (
                    "0.0-1.0 confidence in the classification. "
                    "Be conservative when evidence conflicts."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "One or two sentences explaining the specific evidence "
                    "used to classify the payment."
                ),
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Concrete payment fields that support the classification."
                ),
            },
            "alternative_bucket": {
                "type": "string",
                "enum": [bucket.value for bucket in Bucket],
                "description": (
                    "Strongest alternative classification if the evidence "
                    "is ambiguous."
                ),
            },
            "action_risk": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": (
                    "Risk of taking an automated recovery action if this "
                    "classification is wrong. risk_declined and "
                    "instrument_invalid should generally be high."
                ),
            },
        },
        "required": [
            "bucket",
            "confidence",
            "reasoning",
            "evidence",
            "alternative_bucket",
            "action_risk",
        ],
    },
}


SYSTEM_PROMPT = f"""You classify failed Indian online payments into recovery buckets.

The bucket influences what automated recovery policy runs next.

A wrong classification can cause:
- retries against an unusable instrument,
- unnecessary customer messaging,
- repeated attempts against an unhealthy issuer,
- or unsafe handling of risk/fraud declines.

Buckets:
{_BUCKET_GUIDE}

Rules of engagement:

- Decide only from the payment metadata provided.
- Never invent customer information, bank state, transaction history,
  or payment intent.
- Treat the deterministic rules result as a hypothesis, not ground truth.
- Independently verify the rules hypothesis against the payment evidence.
- Look for contradictory evidence between error_reason, error_source,
  error_step, error_description, rail, issuer and international status.
- If evidence is insufficient, choose `unknown`.
- Do not convert uncertainty into a confident guess.
- For `risk_declined`, require explicit evidence of risk or fraud.
- For `instrument_invalid`, require evidence that the payment instrument
  itself is unusable.
- For `issuer_down`, distinguish an issuer/network outage from a
  customer-specific decline.
- For `insufficient_funds`, require evidence related to balance/funds.
- For `auth_abandoned`, look for authentication, OTP, 3DS or UPI
  collect-expiration evidence.
- Explain exactly which fields influenced the decision.
- Provide the strongest alternative bucket when ambiguity exists.
- Estimate the risk of taking the wrong automated recovery action.
- A high-risk classification should be treated conservatively.
- `unknown` is preferable to a dangerous automated action.

Call `record_classification` exactly once.
"""


def _llm_user_message(
    fp: FailedPayment,
    rules_result: Classification | None = None,
) -> str:
    fields: dict[str, Any] = {
        "amount_inr": round(fp.amount_rupees, 2),
        "method": fp.rail.value,
        "issuer": fp.issuer,
        "international": fp.international,
        "error_code": fp.error_code,
        "error_reason": fp.error_reason,
        "error_source": fp.error_source,
        "error_step": fp.error_step,
        "error_description": fp.error_description,
        "vpa_present": bool(fp.vpa),
    }

    if rules_result is not None:
        fields["rules_hypothesis"] = {
            "bucket": rules_result.bucket.value,
            "confidence": rules_result.confidence,
            "matched_rule": rules_result.matched_rule,
            "reasoning": rules_result.reasoning,
        }

    return (
        "Classify this failed payment.\n\n"
        "Treat the rules hypothesis as an opinion that you must "
        "independently verify.\n\n"
        + json.dumps(fields, indent=2)
    )


def _parse_alternative_bucket(value: Any) -> Bucket:
    try:
        return Bucket(str(value))
    except ValueError:
        return Bucket.UNKNOWN


def classify_by_llm(
    fp: FailedPayment,
    rules_result: Classification | None = None,
) -> Classification | None:
    """
    Classify a failed payment using the configured LLM provider.

    Gemini is the primary provider for this project. xAI/Grok remains
    supported for backwards compatibility. Anthropic is reported as
    unsupported in this path.

    The LLM returns structured JSON which is validated before it reaches
    the downstream recovery policy. The AI never directly executes a
    payment action.
    """

    if not settings.llm_available:
        log.warning(
            "LLM provider is not available"
        )
        return None

    started = time.perf_counter()
    provider = settings.llm_provider.lower().strip()

    # -----------------------------------------------------------------------
    # Gemini
    # -----------------------------------------------------------------------

    if provider == "gemini":
        try:
            from google import genai  # type: ignore[import-not-found]

            client = genai.Client(
                api_key=settings.gemini_api_key
            )

            response_schema = {
                "type": "object",
                "properties": {
                    "bucket": {
                        "type": "string",
                        "enum": [
                            bucket.value
                            for bucket in Bucket
                        ],
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "reasoning": {
                        "type": "string",
                    },
                    "evidence": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                    },
                    "alternative_bucket": {
                        "type": "string",
                        "enum": [
                            bucket.value
                            for bucket in Bucket
                        ],
                    },
                    "action_risk": {
                        "type": "string",
                        "enum": [
                            "low",
                            "medium",
                            "high",
                        ],
                    },
                },
                "required": [
                    "bucket",
                    "confidence",
                    "reasoning",
                    "evidence",
                    "alternative_bucket",
                    "action_risk",
                ],
            }

            response = client.models.generate_content(
                model=settings.llm_model,
                contents=_llm_user_message(
                    fp,
                    rules_result,
                ),
                config={
                    "system_instruction": SYSTEM_PROMPT,
                    "response_mime_type": "application/json",
                    "response_json_schema": response_schema,
                },
            )

            raw_text = response.text

            if not raw_text:
                log.warning(
                    "Gemini returned empty response for %s",
                    fp.payment_id,
                )
                return None

        except Exception as exc:
            log.warning(
                "Gemini classification failed for %s: %s",
                fp.payment_id,
                exc,
            )
            return None

    elif provider == "xai":
        try:
            from openai import OpenAI  # type: ignore[import-not-found]

            client = OpenAI(
                api_key=settings.xai_api_key,
                base_url="https://api.x.ai/v1",
            )

            response = client.chat.completions.create(
                model=settings.llm_model,
                temperature=0,
                messages=[
                    {
                        "role": "system",
                        "content": SYSTEM_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": _llm_user_message(
                            fp,
                            rules_result,
                        ),
                    },
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": CLASSIFIER_TOOL["name"],
                            "description": CLASSIFIER_TOOL["description"],
                            "parameters": CLASSIFIER_TOOL[
                                "input_schema"
                            ],
                        },
                    }
                ],
                tool_choice={
                    "type": "function",
                    "function": {
                        "name": CLASSIFIER_TOOL["name"],
                    },
                },
            )

            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            if not tool_calls:
                log.warning(
                    "xAI returned no tool call for %s",
                    fp.payment_id,
                )
                return None

            tool_call = tool_calls[0]

            if tool_call.function.name != CLASSIFIER_TOOL["name"]:
                log.warning(
                    "xAI returned unexpected tool %r for %s",
                    tool_call.function.name,
                    fp.payment_id,
                )
                return None

            raw_text = tool_call.function.arguments

        except Exception as exc:
            log.warning(
                "xAI classification failed for %s: %s",
                fp.payment_id,
                exc,
            )
            return None

    else:
        log.warning(
            "Unsupported LLM provider: %s",
            settings.llm_provider,
        )
        return None

    # -----------------------------------------------------------------------
    # Parse structured response
    # -----------------------------------------------------------------------

    try:
        payload = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        log.warning(
            "LLM returned invalid classification JSON for %s: %s",
            fp.payment_id,
            exc,
        )
        return None

    if not isinstance(payload, dict):
        log.warning(
            "LLM classification payload was not an object for %s",
            fp.payment_id,
        )
        return None

    # -----------------------------------------------------------------------
    # Bucket
    # -----------------------------------------------------------------------

    try:
        bucket = Bucket(
            str(payload["bucket"])
        )
    except (KeyError, ValueError):
        log.warning(
            "LLM returned unmappable bucket %r",
            payload.get("bucket"),
        )
        return None

    # -----------------------------------------------------------------------
    # Confidence
    # -----------------------------------------------------------------------

    try:
        confidence = float(
            payload.get("confidence") or 0.0
        )
    except (TypeError, ValueError):
        confidence = 0.0

    confidence = max(
        0.0,
        min(1.0, confidence),
    )

    reasoning = str(
        payload.get("reasoning") or ""
    )

    # -----------------------------------------------------------------------
    # Evidence
    # -----------------------------------------------------------------------

    raw_evidence = payload.get("evidence") or []

    if isinstance(raw_evidence, list):
        evidence = [
            str(item)
            for item in raw_evidence
        ]
    else:
        evidence = [
            str(raw_evidence)
        ]

    # -----------------------------------------------------------------------
    # Alternative bucket
    # -----------------------------------------------------------------------

    alternative = _parse_alternative_bucket(
        payload.get("alternative_bucket")
        or Bucket.UNKNOWN.value
    )

    # -----------------------------------------------------------------------
    # Action risk
    # -----------------------------------------------------------------------

    action_risk = str(
        payload.get("action_risk")
        or "high"
    ).lower()

    if action_risk not in {
        "low",
        "medium",
        "high",
    }:
        action_risk = "high"

    # Keep high-risk payment decisions conservative.
    if bucket in {
        Bucket.RISK_DECLINED,
        Bucket.INSTRUMENT_INVALID,
    }:
        action_risk = "high"

    latency_ms = int(
        (time.perf_counter() - started) * 1000
    )

    # -----------------------------------------------------------------------
    # Confidence guardrail
    # -----------------------------------------------------------------------

    if confidence < settings.llm_abstain_below:
        return Classification(
            bucket=Bucket.UNKNOWN,
            confidence=confidence,
            source=ClassifierSource.ABSTAINED,
            reasoning=(
                f"LLM abstained "
                f"(confidence {confidence:.2f} < "
                f"{settings.llm_abstain_below}): "
                f"{reasoning}"
            ),
            evidence=evidence,
            alternative_bucket=alternative,
            action_risk=action_risk,
            latency_ms=latency_ms,
        )

    # -----------------------------------------------------------------------
    # Successful classification
    # -----------------------------------------------------------------------

    return Classification(
        bucket=bucket,
        confidence=confidence,
        source=ClassifierSource.LLM,
        reasoning=reasoning,
        evidence=evidence,
        alternative_bucket=alternative,
        action_risk=action_risk,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# Rules below this confidence are escalated to the LLM.
RULES_ESCALATE_BELOW = 0.70


def classify(fp: FailedPayment) -> Classification:
    """
    Classify a failed payment according to the configured mode.

    Hybrid mode:
        1. Rules classify first.
        2. Very-high-confidence ordinary cases remain rules-authoritative.
        3. Ambiguous or high-risk cases receive AI review.
        4. Rules + AI disagreement is preserved structurally.
        5. If AI is unavailable, rules remain the honest fallback.
    """

    mode = settings.classifier_mode.lower()

    if mode == "rules":
        return classify_by_rules(fp)

    if mode == "llm":
        return classify_by_llm(fp) or _fallback(fp)

    # hybrid (default)
    rules_result = classify_by_rules(fp)

    confident = (
        rules_result.bucket is not Bucket.UNKNOWN
        and rules_result.confidence >= RULES_ESCALATE_BELOW
    )

    # Rules remain authoritative for extremely high-confidence,
    # low-risk ordinary classifications.
    if (
        confident
        and rules_result.confidence >= 0.95
        and rules_result.bucket
        not in {
            Bucket.RISK_DECLINED,
            Bucket.INSTRUMENT_INVALID,
        }
    ):
        return rules_result.model_copy(
            update={
                "rules_bucket": rules_result.bucket,
                "rules_confidence": rules_result.confidence,
                "rules_matched_rule": rules_result.matched_rule,
                "disagreement": False,
            }
        )

    # Ambiguous / lower-confidence / sensitive classifications
    # receive an AI second opinion.
    llm_result = classify_by_llm(
        fp,
        rules_result,
    )

    if llm_result is None:
        return _fallback(
            fp,
            rules_result,
        )

    disagreement = (
        rules_result.bucket is not Bucket.UNKNOWN
        and rules_result.bucket != llm_result.bucket
    )

    # Preserve both the deterministic and AI opinions.
    llm_result = llm_result.model_copy(
        update={
            "rules_bucket": rules_result.bucket,
            "rules_confidence": rules_result.confidence,
            "rules_matched_rule": rules_result.matched_rule,
            "disagreement": disagreement,
        }
    )

    if disagreement:
        llm_result = llm_result.model_copy(
            update={
                "reasoning": (
                    f"{llm_result.reasoning} "
                    f"AI/rules disagreement detected: "
                    f"rules={rules_result.bucket.value} "
                    f"({rules_result.confidence:.2f}) vs "
                    f"AI={llm_result.bucket.value} "
                    f"({llm_result.confidence:.2f}). "
                    f"Rules matched {rules_result.matched_rule}."
                )
            }
        )

    return llm_result


def _fallback(
    fp: FailedPayment,
    precomputed: Classification | None = None,
) -> Classification:
    """
    LLM unavailable or errored: use rules, but label the result honestly
    so the dashboard's source breakdown does not overstate LLM coverage.
    """

    result = precomputed or classify_by_rules(fp)

    if result.bucket is Bucket.UNKNOWN:
        return result

    return result.model_copy(
        update={
            "source": ClassifierSource.LLM_FALLBACK_RULES,
            "rules_bucket": result.bucket,
            "rules_confidence": result.confidence,
            "rules_matched_rule": result.matched_rule,
            "disagreement": False,
            "reasoning": (
                f"{result.reasoning} "
                "[LLM unavailable; rules verdict used]"
            ),
        }
    )
