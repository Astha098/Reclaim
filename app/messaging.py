from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

from app.config import settings
from app.models import FailedPayment, RecoveryPlan
from app.taxonomy import Bucket, Rail

log = logging.getLogger("reclaim.messaging")

# Keep messages short enough for SMS and WhatsApp.
MAX_SMS_CHARS = 300
MAX_EMAIL_SUBJECT = 78
LINK_TOKEN = "{link}"


class Channel(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    NONE = "none"


@dataclass
class RecoveryMessage:
    channel: Channel
    body: str
    subject: str | None = None
    generated_by: str = "template"

    def rendered(self, link: str) -> str:
        return self.body.replace(LINK_TOKEN, link)


TEMPLATES: dict[Bucket, str] = {
    Bucket.INSUFFICIENT_FUNDS: (
        "Your payment of ₹{amount} to {merchant} couldn't be completed — your bank "
        "declined it. No money was deducted. You can pay here whenever you're "
        "ready: {link}"
    ),
    Bucket.AUTH_ABANDONED: (
        "Almost there — your ₹{amount} payment to {merchant} didn't finish "
        "verifying. This link pays in one tap, no OTP needed: {link}"
    ),
    Bucket.INSTRUMENT_INVALID: (
        "Your ₹{amount} payment to {merchant} didn't go through — that card can't "
        "be charged. Pay with UPI or another card here: {link}"
    ),
    Bucket.LIMIT_EXCEEDED: (
        "Your ₹{amount} payment to {merchant} crossed your bank's transaction "
        "limit. Netbanking works for this amount: {link}"
    ),
    Bucket.ISSUER_DOWN: (
        "Your bank was temporarily unreachable, so your ₹{amount} payment to "
        "{merchant} didn't complete. Nothing was deducted. Try again here: {link}"
    ),
    Bucket.CUSTOMER_CANCELLED: (
        "Your ₹{amount} order with {merchant} is still saved. If you'd like to "
        "complete it: {link}"
    ),
    Bucket.TECHNICAL: (
        "A technical issue on our side stopped your ₹{amount} payment to "
        "{merchant}. Nothing was deducted. Here's a fresh link: {link}"
    ),
}

GENERIC_TEMPLATE = (
    "Your ₹{amount} payment to {merchant} didn't complete and nothing was "
    "deducted. You can finish it here: {link}"
)

SUBJECTS: dict[Bucket, str] = {
    Bucket.INSUFFICIENT_FUNDS: "Your payment didn't go through",
    Bucket.AUTH_ABANDONED: "One tap to finish your payment",
    Bucket.INSTRUMENT_INVALID: "Your card couldn't be charged",
    Bucket.LIMIT_EXCEEDED: "Your payment crossed your bank's limit",
    Bucket.ISSUER_DOWN: "Your bank was briefly unreachable",
    Bucket.CUSTOMER_CANCELLED: "Your order is still saved",
    Bucket.TECHNICAL: "Sorry — a technical issue on our side",
}


def _amount_str(paise: int) -> str:
    """Format an amount using Indian number grouping."""
    rupees = paise // 100
    s = str(rupees)
    if len(s) <= 3:
        body = s
    else:
        head, tail = s[:-3], s[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        body = ",".join(groups) + "," + tail
    paise_part = paise % 100
    return f"{body}.{paise_part:02d}" if paise_part else body


def pick_channel(fp: FailedPayment) -> Channel:
    # Use WhatsApp when a phone number is available.
    if fp.contact:
        return Channel.WHATSAPP
    if fp.email:
        return Channel.EMAIL
    return Channel.NONE


def template_message(
    fp: FailedPayment, plan: RecoveryPlan, merchant_name: str
) -> RecoveryMessage:
    channel = pick_channel(fp)
    body = TEMPLATES.get(plan.bucket, GENERIC_TEMPLATE).format(
        amount=_amount_str(fp.amount_paise),
        merchant=merchant_name,
        link=LINK_TOKEN,
    )
    return RecoveryMessage(
        channel=channel,
        body=body,
        subject=SUBJECTS.get(plan.bucket, "Your payment didn't complete"),
        generated_by="template",
    )


BANNED_PATTERNS: list[tuple[str, str]] = [
    (
        r"\b(hurry|last chance|act now|expires? in \d+ (minute|hour)|only \d+ left)\b",
        "manufactured urgency",
    ),
    (r"\b(guaranteed|risk[\s-]?free|100% safe)\b", "unsupportable claim"),
    (
        r"\b(your (mistake|error|fault)|you (entered|typed) (it )?wrong)\b",
        "blames the customer",
    ),
    (
        r"\b(we have (debited|charged)|amount (has been )?deducted)\b",
        "asserts a debit that did not happen",
    ),
    (
        r"\b(click here to (verify|confirm) your (card|cvv|pin|password|otp))\b",
        "phishing-shaped instruction",
    ),
    (
        r"(share|send|tell) (us )?(your )?(otp|pin|cvv|password)",
        "requests a credential",
    ),
    (r"[A-Z]{8,}", "shouting"),
]

_BANNED = [(re.compile(pattern, re.I), reason)
           for pattern, reason in BANNED_PATTERNS]


def validate_message(body: str, channel: Channel) -> tuple[bool, str]:
    """Check that a customer-facing message is safe and complete."""
    if body.count(LINK_TOKEN) != 1:
        return False, f"message must contain {LINK_TOKEN} exactly once"

    projected = len(body) - len(LINK_TOKEN) + 48
    if channel in {Channel.SMS, Channel.WHATSAPP} and projected > MAX_SMS_CHARS:
        return False, f"too long for {channel.value}: ~{projected} chars > {MAX_SMS_CHARS}"

    for pattern, reason in _BANNED:
        if pattern.search(body):
            return False, f"banned pattern ({reason})"

    if body.count("!") > 1:
        return False, "excessive exclamation marks"

    if re.search(r"\b(HDFC|ICICI|SBI|Axis|Kotak|Paytm|PhonePe|GPay)\b", body):
        return False, "names a specific bank or payment provider"

    return True, ""


COMPOSE_TOOL = {
    "name": "write_recovery_message",
    "description": "Write the customer-facing recovery message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": (
                    f"The message. MUST contain the literal token {LINK_TOKEN} where "
                    f"the payment link goes. Under {MAX_SMS_CHARS} characters for "
                    "sms/whatsapp."
                ),
            },
            "subject": {
                "type": "string",
                "description": (
                    f"Email subject line, under {MAX_EMAIL_SUBJECT} chars. "
                    "Empty for sms/whatsapp."
                ),
            },
        },
        "required": ["body"],
    },
}

COMPOSE_SYSTEM = f"""You write payment-recovery messages for Indian customers whose
online payment just failed. These are transactional messages, not marketing.

Hard requirements:
- Include the literal token {LINK_TOKEN} exactly once, where the payment link goes.
- Use plain, warm, direct English with short sentences.
- Reassure the customer that no money was deducted only when that is supported.
- Never blame the customer.
- No manufactured urgency, scarcity, guarantees, or unsupported claims.
- Never name a specific bank, card network, or UPI app.
- Never ask for an OTP, PIN, CVV, or password.
- Use at most one exclamation mark and avoid all-caps words.
- Keep the message under {MAX_SMS_CHARS} characters for SMS and WhatsApp.

Focus on what the customer needs to know and what they can do next.

Call `write_recovery_message` exactly once."""


def _compose_prompt(
    fp: FailedPayment, plan: RecoveryPlan, merchant_name: str, channel: Channel
) -> str:
    from app.taxonomy import policy_for

    pol = policy_for(plan.bucket)
    rail_hint = {
        Rail.UPI: "The link will be UPI-only: one tap in their UPI app, with no OTP or card form.",
        Rail.NETBANKING: "The link will offer netbanking, which can work better for higher amounts.",
        Rail.CARD: "The link will accept cards.",
    }.get(plan.rail, "The link accepts available payment methods.")

    return f"""Write the recovery message.

Channel: {channel.value}
Merchant: {merchant_name}
Amount: ₹{_amount_str(fp.amount_paise)}
Why it failed: {plan.bucket.value} — {pol.rationale}
What the customer should know: {rail_hint}
Original method they tried: {fp.rail.value}
Attempt number: {plan.attempt_no}
"""


def compose_by_llm(
    fp: FailedPayment, plan: RecoveryPlan, merchant_name: str, channel: Channel
) -> RecoveryMessage | None:
    if not settings.llm_available or not settings.anthropic_api_key:
        return None

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.llm_model,
            max_tokens=512,
            system=COMPOSE_SYSTEM,
            tools=[COMPOSE_TOOL],
            tool_choice={"type": "tool", "name": "write_recovery_message"},
            messages=[
                {
                    "role": "user",
                    "content": _compose_prompt(fp, plan, merchant_name, channel),
                }
            ],
        )
    except Exception as exc:
        log.warning("LLM message composition failed for %s: %s",
                    fp.payment_id, exc)
        return None

    payload = next(
        (
            block.input
            for block in response.content
            if getattr(block, "type", None) == "tool_use"
        ),
        None,
    )
    if not isinstance(payload, dict) or not payload.get("body"):
        return None

    body = str(payload["body"]).strip()
    subject = str(payload.get("subject") or "").strip() or None
    return RecoveryMessage(
        channel=channel,
        body=body,
        subject=subject,
        generated_by="llm",
    )


def compose(
    fp: FailedPayment,
    plan: RecoveryPlan,
    merchant_name: str = "the merchant",
) -> RecoveryMessage:
    """Create a validated message, using the template if the LLM is unavailable or rejected."""
    channel = pick_channel(fp)
    if channel is Channel.NONE:
        return RecoveryMessage(channel=channel, body="", generated_by="template")

    candidate = compose_by_llm(fp, plan, merchant_name, channel)
    if candidate is not None:
        valid, reason = validate_message(candidate.body, channel)
        if valid:
            if candidate.subject and len(candidate.subject) > MAX_EMAIL_SUBJECT:
                candidate.subject = candidate.subject[: MAX_EMAIL_SUBJECT - 1] + "…"
            return candidate

        log.warning(
            "Rejected LLM message for %s (%s): %r",
            fp.payment_id,
            reason,
            candidate.body,
        )
        fallback = template_message(fp, plan, merchant_name)
        fallback.generated_by = "llm_rejected_fallback"
        return fallback

    return template_message(fp, plan, merchant_name)


def send(
    message: RecoveryMessage, fp: FailedPayment, link: str
) -> tuple[bool, str]:
    """Deliver a message through the project's mock delivery seam."""
    if message.channel is Channel.NONE:
        return False, "no customer contact channel"

    rendered = message.rendered(link)
    destination = (
        fp.contact
        if message.channel in {Channel.SMS, Channel.WHATSAPP}
        else fp.email
    )
    if not destination:
        return False, "no destination for channel"

    log.info("[%s → %s] %s", message.channel.value, destination, rendered)
    return True, f"delivered via {message.channel.value} to {destination}"
